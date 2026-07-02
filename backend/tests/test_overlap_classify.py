"""
overlap 구간 분류·throughput·latency 순수함수 단위 테스트.

docker / GPU 불필요 — 합성 타임스탬프만 사용.
대상: scripts/eval/_overlap.py (재실험 분석 헬퍼).

검증 포인트 (docs/plan/2026-06-15_17-15_...md §12)
  1 기본 분류 카운트
  2 overlap_start skew (B 먼저 시작한 단독 iter 제외)
  3 overlap_end 경계 걸친 iter → boundary
  4 boundary 는 overlap/drain 어느 합산에도 미포함
  5 OOM(iter 0개) → skip
  6 throughput = sum/wall-clock (산술평균과 다름)
  7 overlap iter < MIN_OVERLAP_ITERS → reliability_warning
  8 makespan/speedup/overhead 산식
  9 latency p50/p95 보간
"""
import sys
from pathlib import Path

import pytest

# scripts/eval 를 import 경로에 추가 (_overlap.py 는 패키지 아님)
_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "scripts" / "eval"))

import _overlap  # noqa: E402


def mk(start, dur, gen_tokens=128, latency=None):
    """IterRec 헬퍼. latency 기본 = dur."""
    return {
        "start": float(start),
        "end": float(start + dur),
        "gen_tokens": gen_tokens,
        "latency": float(latency if latency is not None else dur),
    }


# ── 1. 기본 분류 ────────────────────────────────────────────────────
def test_basic_classification_counts():
    # A: 10~20 구간에 iter 5개 (간격 2s), B: 10~20 에 iter 5개
    iters_a = [mk(10 + 2 * i, 1.5) for i in range(5)]  # start 10,12,14,16,18
    iters_b = [mk(10 + 2 * i, 1.5) for i in range(5)]
    win = _overlap.overlap_window(iters_a, iters_b)
    assert win is not None
    os_, oe_ = win
    # overlap_start = max(10,10)=10, overlap_end = min(19.5,19.5)=19.5
    assert os_ == 10.0
    assert oe_ == 19.5
    cls = _overlap.classify(iters_a, os_, oe_)
    # 마지막 iter start=18 end=19.5 <= 19.5 → overlap. 전부 overlap.
    assert len(cls["overlap"]) == 5
    assert len(cls["drain"]) == 0
    assert len(cls["boundary"]) == 0


# ── 2. overlap_start skew — B 가 먼저 시작 ──────────────────────────
def test_overlap_start_skew_excludes_solo_warmup():
    # B 가 t=0 부터 2 iter 단독 → A 는 t=10 부터 시작
    iters_b = [mk(0, 1.5), mk(2, 1.5)] + [mk(10 + 2 * i, 1.5) for i in range(4)]
    iters_a = [mk(10 + 2 * i, 1.5) for i in range(4)]  # 10,12,14,16
    os_, oe_ = _overlap.overlap_window(iters_a, iters_b)
    assert os_ == 10.0  # max(10, 0) = 10 → B 의 단독 구간 배제
    cls_b = _overlap.classify(iters_b, os_, oe_)
    # B 의 t=0, t=2 iter 는 start < overlap_start → boundary (overlap 아님)
    assert len(cls_b["boundary"]) == 2
    assert all(it["start"] >= os_ for it in cls_b["overlap"])


# ── 3. overlap_end 경계 걸침 → boundary ─────────────────────────────
def test_boundary_iter_straddling_overlap_end():
    # A 가 B 보다 오래 돔. overlap_end 를 걸치는 A iter 는 boundary.
    iters_b = [mk(10 + 2 * i, 1.5) for i in range(3)]  # 10,12,14 → end 15.5
    # A: 10,12,14(걸침: start14<15.5<end), 16(drain)
    iters_a = [mk(10, 1.5), mk(12, 1.5), mk(14, 2.0), mk(16, 1.5)]
    os_, oe_ = _overlap.overlap_window(iters_a, iters_b)
    assert oe_ == 15.5  # min(A끝 17.5, B끝 15.5)
    cls_a = _overlap.classify(iters_a, os_, oe_)
    # start=14 end=16 > 15.5 → 경계 걸침 → boundary
    boundary_starts = [it["start"] for it in cls_a["boundary"]]
    assert 14.0 in boundary_starts
    # start=16 >= overlap_end 15.5 → drain
    assert any(it["start"] == 16.0 for it in cls_a["drain"])


# ── 4. boundary 는 어느 합산에도 미포함 ─────────────────────────────
def test_boundary_excluded_from_sums():
    iters_b = [mk(10, 1.5), mk(12, 1.5)]            # end 13.5
    iters_a = [mk(10, 1.5), mk(12, 2.0), mk(15, 1)]  # 12번 걸침, 15 drain
    os_, oe_ = _overlap.overlap_window(iters_a, iters_b)  # (10, 13.5)
    cls_a = _overlap.classify(iters_a, os_, oe_)
    counted = len(cls_a["overlap"]) + len(cls_a["drain"]) + len(cls_a["boundary"])
    assert counted == len(iters_a)
    # boundary iter 의 토큰은 overlap throughput 합에 안 들어감
    ov_tokens = sum(it["gen_tokens"] for it in cls_a["overlap"])
    assert ov_tokens == 128  # iter@10 만 overlap (128 tok), 나머지는 boundary/drain


# ── 5. OOM(iter 0개) → skip ─────────────────────────────────────────
def test_oom_one_session_skips():
    iters_a = [mk(10, 1.5) for _ in range(3)]
    iters_b = []  # OOM
    assert _overlap.overlap_window(iters_a, iters_b) is None
    res = _overlap.analyze_concurrent(iters_a, iters_b, 5.0, 0.0)
    assert res["skip"] is True


# ── 6. throughput = sum/wall-clock (산술평균 아님) ──────────────────
def test_throughput_is_window_based_not_mean():
    # 느린 iter 1개 + 빠른 iter 3개. 산술평균 tps 와 구간 tps 가 다름을 보임.
    iters = [
        mk(0, 4.0, gen_tokens=100),   # 25 tok/s
        mk(4, 1.0, gen_tokens=100),   # 100 tok/s
        mk(5, 1.0, gen_tokens=100),   # 100 tok/s
        mk(6, 1.0, gen_tokens=100),   # 100 tok/s
    ]
    window_s = 7.0  # 0~7
    tps_window = _overlap.window_throughput(iters, window_s)
    assert tps_window == pytest.approx(400 / 7.0)  # = 57.1
    # 산술평균 tps = (25+100+100+100)/4 = 81.25 → 다름 (저속 outlier 과소가중)
    arith = (25 + 100 + 100 + 100) / 4
    assert abs(tps_window - arith) > 10


# ── 7. overlap iter 부족 → reliability_warning ──────────────────────
def test_reliability_warning_when_few_overlap_iters():
    # overlap 에 2개만 (MIN=5 미만)
    iters_a = [mk(10, 1.0), mk(11.5, 1.0)]
    iters_b = [mk(10, 1.0), mk(11.5, 1.0)]
    res = _overlap.analyze_concurrent(iters_a, iters_b, 3.0, 3.0)
    assert res["skip"] is False
    assert res["reliability_warning"] is True


def test_no_warning_when_enough_overlap_iters():
    iters_a = [mk(10 + i, 0.8) for i in range(6)]
    iters_b = [mk(10 + i, 0.8) for i in range(6)]
    res = _overlap.analyze_concurrent(iters_a, iters_b, 6.0, 6.0)
    assert res["reliability_warning"] is False
    assert res["a"]["n_overlap"] >= _overlap.MIN_OVERLAP_ITERS


# ── 8. makespan / speedup / overhead 산식 ───────────────────────────
def test_makespan_speedup_overhead():
    # A: 10~30 (makespan 20), B: 10~25 (makespan 15)
    iters_a = [mk(10, 1), mk(28, 2)]   # span 10~30
    iters_b = [mk(10, 1), mk(23, 2)]   # span 10~25
    res = _overlap.analyze_concurrent(
        iters_a, iters_b, solo_makespan_a=12.0, solo_makespan_b=8.0
    )
    # makespan_conc = max(30,25) - min(10,10) = 20
    assert res["makespan_conc"] == pytest.approx(20.0)
    # makespan_seq = 12 + 8 = 20
    assert res["makespan_seq"] == pytest.approx(20.0)
    # speedup = 20/20 = 1.0 (이 조건의 '이득 없음' 참값)
    assert res["speedup"] == pytest.approx(1.0)
    # overhead = conc - max(solo) = 20 - 12 = 8
    assert res["overhead_s"] == pytest.approx(8.0)


# ── 9. latency 백분위 보간 ──────────────────────────────────────────
def test_percentile_interpolation():
    vals = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert _overlap.percentile(vals, 50) == pytest.approx(3.0)
    assert _overlap.percentile(vals, 0) == pytest.approx(1.0)
    assert _overlap.percentile(vals, 100) == pytest.approx(5.0)
    # p95 of 5 values: idx = 4*0.95 = 3.8 → 4.0 + (5.0-4.0)*0.8 = 4.8
    assert _overlap.percentile(vals, 95) == pytest.approx(4.8)


def test_latency_stats_empty():
    s = _overlap.latency_stats([])
    assert s["n"] == 0 and s["p50"] == 0.0


# ── occupancy_sum (포화 지표) ───────────────────────────────────────
def test_occupancy_sum_saturated():
    # 두 잡 overlap_tps 가 각자 solo 의 절반이면 occupancy_sum ≈ 1.0 (포화)
    iters_a = [mk(10 + i, 0.8, gen_tokens=50) for i in range(6)]  # ~50 tok/s
    iters_b = [mk(10 + i, 0.8, gen_tokens=50) for i in range(6)]
    res = _overlap.analyze_concurrent(
        iters_a, iters_b,
        solo_makespan_a=6.0, solo_makespan_b=6.0,
        solo_tps_a=res_solo_tps(iters_a) * 2,  # solo 가 2배 빨랐다고 가정
        solo_tps_b=res_solo_tps(iters_b) * 2,
    )
    assert res["occupancy_sum"] == pytest.approx(1.0, abs=0.05)


def res_solo_tps(iters):
    """헬퍼: 구간 tps (테스트 가독용)."""
    span = iters[-1]["end"] - iters[0]["start"]
    return _overlap.window_throughput(iters, span)
