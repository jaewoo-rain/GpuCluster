#!/usr/bin/env python3
"""GPU 유휴 검증 분석 — run_gpuutil.sh 산출물 후처리.

- util.csv (nvidia-smi: timestamp, utilization.gpu, memory.used) 파싱.
- sharing/meta_concurrent_{A,B}.json 의 t_start/t_end_epoch 로 overlap 윈도우 결정.
- overlap 구간(양끝 GUARD초 트림 = 모델로드/드레인 제외)의 평균 util / idle% 산출.
- 같은 run 의 occupancy_sum(throughput 프록시)과 나란히 출력 → 둘의 일치/괴리 비교.

stdlib 만. 사용: python3 _gpuutil_report.py <OUT_DIR>
"""
import json
import sys
from datetime import datetime
from pathlib import Path

GUARD_S = 3.0  # overlap 양끝에서 잘라낼 초 (램프업/드레인 경계 노이즈 제거)


def parse_smi_ts(s: str) -> float:
    # "2026/06/16 17:35:58.275" (로컬시각) → epoch. meta 의 time.time() 과 동일 tz.
    return datetime.strptime(s.strip(), "%Y/%m/%d %H:%M:%S.%f").timestamp()


def load_util(path: Path):
    rows = []
    for ln in path.read_text().splitlines()[1:]:  # 헤더 스킵
        parts = [p.strip() for p in ln.split(",")]
        if len(parts) < 3:
            continue
        try:
            rows.append((parse_smi_ts(parts[0]), float(parts[1]), float(parts[2])))
        except ValueError:
            continue
    return rows


def main() -> None:
    out = Path(sys.argv[1])
    util = load_util(out / "util.csv")
    ma = json.loads((out / "sharing" / "meta_concurrent_A.json").read_text())
    mb = json.loads((out / "sharing" / "meta_concurrent_B.json").read_text())

    a0, a1 = ma["t_start_epoch"], ma["t_end_epoch"]
    b0, b1 = mb["t_start_epoch"], mb["t_end_epoch"]
    ov0, ov1 = max(a0, b0) + GUARD_S, min(a1, b1) - GUARD_S

    in_ov = [u for (t, u, _m) in util if ov0 <= t <= ov1]
    mem_ov = [m for (t, _u, m) in util if ov0 <= t <= ov1]

    # occupancy_sum 은 sharing 리포트에서 가져온다(같은 run).
    occ = "?"
    rep = out / "sharing" / "overlap_report.txt"
    if rep.exists():
        for ln in rep.read_text().splitlines():
            if "occupancy_sum" in ln:
                occ = ln.split("=")[-1].split("(")[0].strip()
                break

    print("=" * 64)
    print(" GPU 유휴 검증 — occupancy_sum(프록시) vs 실측 nvidia-smi util")
    print("=" * 64)
    print(f" overlap 윈도우(±{GUARD_S}s 트림): {ov1-ov0:.1f}s, util 샘플 {len(in_ov)}개")
    if not in_ov:
        print(" [경고] overlap 구간 util 샘플 없음 — 타임스탬프 정렬 확인 필요")
        return
    in_ov.sort()
    mean_util = sum(in_ov) / len(in_ov)
    p50 = in_ov[len(in_ov) // 2]
    idle_pct = 100.0 - mean_util
    busy_frac = sum(1 for u in in_ov if u >= 5) / len(in_ov)  # util>=5% 인 샘플 비율

    print(f"")
    print(f" [실측 GPU util — overlap 구간]")
    print(f"   평균 utilization.gpu = {mean_util:.1f}%   (p50={p50:.0f}%)")
    print(f"   → GPU idle 추정       = {idle_pct:.1f}%   (= 100 − 평균 util)")
    print(f"   util>=5% 샘플 비율(busy) = {busy_frac*100:.1f}%")
    if mem_ov:
        print(f"   메모리 사용(평균/최대) = {sum(mem_ov)/len(mem_ov):.0f} / {max(mem_ov):.0f} MiB")
    print(f"")
    print(f" [throughput 프록시]")
    print(f"   occupancy_sum = {occ}  (1.0=포화, <1=낭비)")
    print(f"")
    print(f" [해석]")
    print(f"   occupancy_sum 이 가리키는 '낭비'와 실측 idle({idle_pct:.1f}%) 를 비교.")
    print(f"   둘이 비슷하면 프록시가 idle 을 잘 반영, 크게 다르면 경합/오버헤드 성분이 큼.")


if __name__ == "__main__":
    main()
