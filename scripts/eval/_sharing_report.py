#!/usr/bin/env python3
"""
재실험 overlap 2축 리포트 — run_sharing.sh 가 측정 후 호출.

입력 (out_dir 안, test_infer_measure.py 가 생성)
  meta_solo_A.json / meta_solo_B.json       : solo makespan / mean_tps
  result_solo_A.csv / result_solo_B.csv     : solo iter (latency 기준선)
  result_concurrent_A.csv / _B.csv          : concurrent iter

출력: stdout 2축 리포트 (run_sharing.sh 가 overlap_report.txt 로 tee).
검증된 순수함수 scripts/eval/_overlap.py 사용 (단위테스트 test_overlap_classify.py).
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _overlap


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: _sharing_report.py <out_dir>", file=sys.stderr)
        sys.exit(2)
    out = Path(sys.argv[1])

    def _meta(name: str) -> dict:
        p = out / name
        return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}

    ma, mb = _meta("meta_solo_A.json"), _meta("meta_solo_B.json")
    mca, mcb = _meta("meta_concurrent_A.json"), _meta("meta_concurrent_B.json")
    ia = _overlap.load_iters_csv(out / "result_concurrent_A.csv")
    ib = _overlap.load_iters_csv(out / "result_concurrent_B.csv")
    sa = _overlap.load_iters_csv(out / "result_solo_A.csv")
    sb = _overlap.load_iters_csv(out / "result_solo_B.csv")

    def _thr(m: dict) -> str:
        return "ON" if (m.get("env_snapshot") or {}).get("FGPU_THROTTLE_ENABLE") else "OFF"

    res = _overlap.analyze_concurrent(
        ia, ib,
        solo_makespan_a=ma.get("makespan_s", 0.0),
        solo_makespan_b=mb.get("makespan_s", 0.0),
        solo_tps_a=ma.get("mean_tokens_per_s", 0.0),
        solo_tps_b=mb.get("mean_tokens_per_s", 0.0),
    )

    print("=" * 64)
    print(" 재실험 — overlap 2축 리포트 (오버헤드 + 요청당 latency)")
    print("=" * 64)
    print(f" 설정: solo throttle={_thr(ma)} / concurrent throttle={_thr(mca)}, "
          f"ratio A={ma.get('ratio','?')} B={mb.get('ratio','?')}, model={ma.get('model','?')}")
    print(f" solo: A makespan={ma.get('makespan_s',0):.1f}s tps={ma.get('mean_tokens_per_s',0):.1f} | "
          f"B makespan={mb.get('makespan_s',0):.1f}s tps={mb.get('mean_tokens_per_s',0):.1f}")

    if res.get("skip"):
        print(f"\n SKIP — {res.get('reason')}")
        print(" (A 또는 B 가 OOM 으로 iter 0개. CSV/로그 확인.)")
        return

    print("\n[축 A] 시간 오버헤드 (교수님 ① / '내 일 2개' 단일 소유자 관점)")
    print(f"  makespan_seq  (A+B 따로 합) = {res['makespan_seq']:.2f} s")
    print(f"  makespan_conc (같이)        = {res['makespan_conc']:.2f} s")
    print(f"  overhead (vs 완전병렬)      = {res['overhead_s']:.2f} s")
    print(f"  speedup (서술값, 판정 아님) = {res['speedup']:.3f}  (~1.0 = 총시간 손해 없음)")
    if res["occupancy_sum"] is not None:
        print(f"  occupancy_sum              = {res['occupancy_sum']:.3f}  (~1.0 = GPU 포화·여유 없음)")

    print("\n[축 B] 요청당 latency (교수님 ② / '다른 사람' 관점, overlap 구간·drain 제외)")
    for lbl, side, solo in (("A", "a", sa), ("B", "b", sb)):
        sl = _overlap.latency_stats(solo)
        ov = res[side]["overlap_latency"]
        ratio = (ov["p50"] / sl["p50"]) if sl["p50"] > 0 else 0.0
        print(f"  {lbl}: solo  p50={sl['p50']:.3f}s p95={sl['p95']:.3f}s"
              f"   →  공유 p50={ov['p50']:.3f}s p95={ov['p95']:.3f}s"
              f"   (x{ratio:.2f} 느려짐; overlap iter={res[side]['n_overlap']},"
              f" drain={res[side]['n_drain']}, boundary={res[side]['n_boundary']})")

    if res["reliability_warning"]:
        print("\n  [경고] overlap iter < 5 — 추정 노이즈 큼. N_ITERS 상향/동시시작 위상차 점검.")

    if res["occupancy_sum"] is not None and res["occupancy_sum"] >= 0.9:
        print("\n  해석: GPU 포화형(학습 성격) → 총시간 중립 + 요청 latency 증가가 비용.")
        print("        추론(상호작용)이면 이 latency가 견딜 만한 trade-off인지로 효용 판단(②).")


if __name__ == "__main__":
    main()
