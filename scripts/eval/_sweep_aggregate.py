#!/usr/bin/env python3
"""
포화 스윕 집계 — sweep_<TS>/ 안의 b<batch>_thr<mode>_rep<r>/ 들을 읽어
(batch, throttle) 조합별로 핵심 지표의 평균±편차를 표로 출력.

검증된 _overlap.analyze_concurrent 재사용. stdlib 만.
"""
import csv as _csv
import json
import re
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _overlap

KEYS = ["speedup", "overhead_seq", "occupancy", "a_share", "a_lat_x", "b_lat_x"]


def _meta(d: Path, name: str) -> dict:
    p = d / name
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}


def metrics_for(d: Path):
    ma, mb = _meta(d, "meta_solo_A.json"), _meta(d, "meta_solo_B.json")
    ia = _overlap.load_iters_csv(d / "result_concurrent_A.csv")
    ib = _overlap.load_iters_csv(d / "result_concurrent_B.csv")
    sa = _overlap.load_iters_csv(d / "result_solo_A.csv")
    sb = _overlap.load_iters_csv(d / "result_solo_B.csv")
    if not (ia and ib and sa and sb):
        return None
    res = _overlap.analyze_concurrent(
        ia, ib, ma.get("makespan_s", 0.0), mb.get("makespan_s", 0.0),
        ma.get("mean_tokens_per_s", 0.0), mb.get("mean_tokens_per_s", 0.0))
    if res.get("skip"):
        return None
    a_tps, b_tps = res["a"]["overlap_tps"], res["b"]["overlap_tps"]
    a_share = a_tps / (a_tps + b_tps) if (a_tps + b_tps) > 0 else 0.0
    sa_p50 = _overlap.latency_stats(sa)["p50"]
    sb_p50 = _overlap.latency_stats(sb)["p50"]
    return {
        "speedup": res["speedup"],                       # seq/conc (>1 = 공유가 빠름)
        "overhead_seq": res["makespan_conc"] - res["makespan_seq"],  # 초, + = 손해
        "occupancy": res["occupancy_sum"] or 0.0,        # ~1=포화, <1=throttle 낭비
        "a_share": a_share,                              # A 컴퓨트 점유(목표 0.4)
        "a_lat_x": (res["a"]["overlap_latency"]["p50"] / sa_p50) if sa_p50 > 0 else 0.0,
        "b_lat_x": (res["b"]["overlap_latency"]["p50"] / sb_p50) if sb_p50 > 0 else 0.0,
    }


def _ms(vals):
    if not vals:
        return "n/a"
    mu = statistics.mean(vals)
    sd = statistics.pstdev(vals) if len(vals) > 1 else 0.0
    return f"{mu:.3f}±{sd:.3f}"


def main():
    if len(sys.argv) < 2:
        print("usage: _sweep_aggregate.py <sweep_dir>", file=sys.stderr)
        sys.exit(2)
    sweep = Path(sys.argv[1])
    runs: dict = {}
    skipped = []
    for d in sorted(sweep.iterdir()):
        if not d.is_dir():
            continue
        m = re.match(r"b(\d+)_thr(\w+)_rep(\d+)", d.name)
        if not m:
            continue
        key = (int(m.group(1)), m.group(2))
        mx = metrics_for(d)
        if mx is None:
            skipped.append(d.name)
            continue
        runs.setdefault(key, []).append(mx)

    print("=" * 104)
    print(" 포화 스윕 집계 (각 셀 = 평균±편차, n=반복수)")
    print(" speedup=따로/같이 총시간(>1 공유가 빠름) | overhead=같이-따로(초,+=손해)")
    print(" occ=occupancy_sum(~1 포화,<1 throttle낭비) | a_share=A컴퓨트점유(목표 0.40) | lat_x=요청당 느려진배수")
    print("=" * 104)
    hdr = f"{'batch':>5} {'thr':>5} {'n':>2} | " + " ".join(f"{k:>13}" for k in KEYS)
    print(hdr)
    print("-" * len(hdr))
    for key in sorted(runs):
        b, thr = key
        vs = runs[key]
        cells = " ".join(f"{_ms([x[k] for x in vs]):>13}" for k in KEYS)
        print(f"{b:>5} {thr:>5} {len(vs):>2} | {cells}")
    if skipped:
        print(f"\n[skip] {len(skipped)} run(s) 분석 불가(OOM/overlap 없음): {', '.join(skipped)}")

    out_csv = sweep / "sweep_summary.csv"
    with out_csv.open("w", newline="", encoding="utf-8") as f:
        w = _csv.writer(f)
        w.writerow(["batch", "throttle", "n"]
                   + [f"{k}_mean" for k in KEYS] + [f"{k}_std" for k in KEYS])
        for key in sorted(runs):
            b, thr = key
            vs = runs[key]
            means = [statistics.mean([x[k] for x in vs]) for k in KEYS]
            stds = [statistics.pstdev([x[k] for x in vs]) if len(vs) > 1 else 0.0 for k in KEYS]
            w.writerow([b, thr, len(vs)]
                       + [f"{m:.4f}" for m in means] + [f"{s:.4f}" for s in stds])
    print(f"\nCSV: {out_csv}")


if __name__ == "__main__":
    main()
