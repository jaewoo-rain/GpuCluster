#!/usr/bin/env python3
"""
재실험 post-processing — overlap 구간 분석 + 2축 지표(오버헤드 / 요청당 latency).

배경 (docs/plan/2026-06-15_17-15_재실험-오버헤드-latency-정직재측정.md)
  지난 H1 분석은 concurrent 의 `mean_tokens_per_s` 를 20 iter **전체 평균**으로 냈다.
  B 가 먼저 끝나 A 가 혼자 도는 **drain 구간**이 평균에 섞여 "공유 중 처리량" 을 부풀렸다.
  이 모듈은 두 세션의 iter 타임스탬프로 **둘 다 활성인 overlap 구간만** 잘라
  per-job throughput / latency 를 계산한다. makespan(오버헤드)은 drain 포함이 정당하므로
  전체 구간 기준 그대로 둔다.

설계
  - 입력은 "iter 레코드 리스트" — 각 레코드 = {start, end, gen_tokens, latency}.
    (fgpu_infer.ipynb 의 session_result_*.csv 한 행에 대응:
     iter_start_epoch, iter_end_epoch, gen_tokens, latency_s)
  - epoch = CLOCK_REALTIME 라 컨테이너 간 직접 비교 가능 (Docker time namespace 미사용 전제).
  - overlap_start = max(A 첫 start, B 첫 start)   ← 둘 다 활성이 된 시점
    overlap_end   = min(A 마지막 end, B 마지막 end) ← 먼저 끝난 잡의 종료
  - iter 3분류: overlap / drain / boundary(경계 걸침 → 제외, 시간비율 배분 안 함).
  - throughput = 구간 sum(gen_tokens) / 구간 wall-clock (iter별 tps 산술평균 아님).

외부 dep 0 — stdlib 만. matplotlib/pandas 안 씀 (노트북이 import 해서 그래프는 거기서).
"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Optional, TypedDict


# overlap iter 가 이보다 적으면 신뢰도 경고 (구간이 얇아 추정 노이즈 큼).
MIN_OVERLAP_ITERS = 5


class IterRec(TypedDict):
    start: float       # iter_start_epoch
    end: float         # iter_end_epoch
    gen_tokens: int
    latency: float     # latency_s


# ────────────────────────────────────────────────────────────────────
# 기본 통계
# ────────────────────────────────────────────────────────────────────
def percentile(values: list[float], p: float) -> float:
    """선형 보간 백분위수. p 는 0~100. 빈 리스트면 0.0."""
    if not values:
        return 0.0
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    idx = (len(s) - 1) * p / 100.0
    lo = int(idx)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (idx - lo)


def latency_stats(iters: list[IterRec]) -> dict:
    """mean / p50 / p95 latency (초). 빈 리스트면 전부 0."""
    lats = [it["latency"] for it in iters]
    if not lats:
        return {"n": 0, "mean": 0.0, "p50": 0.0, "p95": 0.0}
    return {
        "n": len(lats),
        "mean": sum(lats) / len(lats),
        "p50": percentile(lats, 50),
        "p95": percentile(lats, 95),
    }


# ────────────────────────────────────────────────────────────────────
# overlap 구간
# ────────────────────────────────────────────────────────────────────
def overlap_window(
    iters_a: list[IterRec], iters_b: list[IterRec]
) -> Optional[tuple[float, float]]:
    """
    두 세션이 동시에 활성인 시간 창 (overlap_start, overlap_end).
    한쪽이라도 iter 0개(OOM)거나 겹치는 창이 없으면 None.
    """
    if not iters_a or not iters_b:
        return None
    start = max(iters_a[0]["start"], iters_b[0]["start"])
    end = min(iters_a[-1]["end"], iters_b[-1]["end"])
    if end <= start:
        return None
    return (start, end)


def classify(
    iters: list[IterRec], overlap_start: float, overlap_end: float
) -> dict[str, list[IterRec]]:
    """
    한 세션의 iter 를 overlap / drain / boundary 로 분류.
      overlap : 창 안에 완전히 내포 (start>=overlap_start AND end<=overlap_end)
      drain   : 창 종료 후 시작 (start>=overlap_end) — 상대 종료 후 혼자 도는 구간
      boundary: 경계 걸침 또는 overlap_start 이전(skew) — 분석 제외
    """
    out: dict[str, list[IterRec]] = {"overlap": [], "drain": [], "boundary": []}
    for it in iters:
        if it["start"] >= overlap_start and it["end"] <= overlap_end:
            out["overlap"].append(it)
        elif it["start"] >= overlap_end:
            out["drain"].append(it)
        else:
            out["boundary"].append(it)
    return out


def window_throughput(iters: list[IterRec], window_s: float) -> float:
    """구간 처리량 = sum(gen_tokens) / window_wall_clock (tok/s). window<=0 이면 0."""
    if window_s <= 0:
        return 0.0
    total = sum(it["gen_tokens"] for it in iters)
    return total / window_s


# ────────────────────────────────────────────────────────────────────
# 종합 분석
# ────────────────────────────────────────────────────────────────────
def makespan(iters: list[IterRec]) -> float:
    """단일 세션 makespan = 마지막 end - 첫 start. 빈 리스트면 0."""
    if not iters:
        return 0.0
    return iters[-1]["end"] - iters[0]["start"]


def analyze_concurrent(
    iters_a: list[IterRec],
    iters_b: list[IterRec],
    solo_makespan_a: float,
    solo_makespan_b: float,
    solo_tps_a: float = 0.0,
    solo_tps_b: float = 0.0,
) -> dict:
    """
    concurrent run 종합 분석.

    반환 (핵심 키)
      skip            : 한쪽 OOM(iter 0개)이면 True, 나머지 키 의미 없음
      makespan_conc   : max(end) - min(start) (전체, drain 포함 — 정당)
      makespan_seq    : solo_makespan_a + solo_makespan_b
      speedup         : seq/conc (서술값 — 판정 잣대로 쓰지 말 것)
      overhead_s      : conc - max(solo_a, solo_b) (이상적 완전병렬 대비 초과시간)
      overlap_start/end, overlap_window_s
      a / b           : 세션별 {n_overlap, n_drain, n_boundary,
                          overlap_tps, drain_tps, overlap_latency{mean,p50,p95}}
      occupancy_sum   : overlap_tps_a/solo_tps_a + overlap_tps_b/solo_tps_b
                        (≈1.0 = GPU 포화 = 여유 없음). solo_tps 미제공 시 None
      reliability_warning : overlap iter < MIN_OVERLAP_ITERS 인 세션 있으면 True
    """
    win = overlap_window(iters_a, iters_b)
    if win is None:
        return {
            "skip": True,
            "reason": "한쪽 세션 iter 0개(OOM) 또는 겹치는 구간 없음",
        }
    overlap_start, overlap_end = win
    overlap_window_s = overlap_end - overlap_start

    def _per_session(iters: list[IterRec]) -> dict:
        cls = classify(iters, overlap_start, overlap_end)
        ov, dr = cls["overlap"], cls["drain"]
        drain_window_s = (dr[-1]["end"] - overlap_end) if dr else 0.0
        return {
            "n_overlap": len(ov),
            "n_drain": len(dr),
            "n_boundary": len(cls["boundary"]),
            "overlap_tps": window_throughput(ov, overlap_window_s),
            "drain_tps": window_throughput(dr, drain_window_s),
            "overlap_latency": latency_stats(ov),
            "drain_latency": latency_stats(dr),
        }

    a = _per_session(iters_a)
    b = _per_session(iters_b)

    t_start = min(iters_a[0]["start"], iters_b[0]["start"])
    t_end = max(iters_a[-1]["end"], iters_b[-1]["end"])
    makespan_conc = t_end - t_start
    makespan_seq = solo_makespan_a + solo_makespan_b
    speedup = (makespan_seq / makespan_conc) if makespan_conc > 0 else 0.0
    overhead_s = makespan_conc - max(solo_makespan_a, solo_makespan_b)

    occupancy_sum: Optional[float] = None
    if solo_tps_a > 0 and solo_tps_b > 0:
        occupancy_sum = a["overlap_tps"] / solo_tps_a + b["overlap_tps"] / solo_tps_b

    reliability_warning = (
        a["n_overlap"] < MIN_OVERLAP_ITERS or b["n_overlap"] < MIN_OVERLAP_ITERS
    )

    return {
        "skip": False,
        "overlap_start": overlap_start,
        "overlap_end": overlap_end,
        "overlap_window_s": overlap_window_s,
        "makespan_conc": makespan_conc,
        "makespan_seq": makespan_seq,
        "speedup": speedup,
        "overhead_s": overhead_s,
        "a": a,
        "b": b,
        "occupancy_sum": occupancy_sum,
        "reliability_warning": reliability_warning,
    }


# ────────────────────────────────────────────────────────────────────
# CSV 로더 (노트북/서버용 — 테스트는 합성 데이터 직접 주입)
# ────────────────────────────────────────────────────────────────────
def load_iters_csv(path: Path) -> list[IterRec]:
    """session_result_*.csv → IterRec 리스트. OOM 행(gen_tokens=0, iter=0)은 건너뜀."""
    out: list[IterRec] = []
    if not Path(path).is_file():
        return out
    with Path(path).open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                it = int(row["iter"])
                if it <= 0:
                    continue  # OOM placeholder 행
                out.append(
                    {
                        "start": float(row["iter_start_epoch"]),
                        "end": float(row["iter_end_epoch"]),
                        "gen_tokens": int(row["gen_tokens"]),
                        "latency": float(row["latency_s"]),
                    }
                )
            except (KeyError, ValueError):
                continue
    out.sort(key=lambda r: r["start"])
    return out
