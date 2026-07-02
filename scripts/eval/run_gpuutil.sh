#!/usr/bin/env bash
# GPU 유휴 검증 — occupancy_sum(throughput 프록시) vs 실측 nvidia-smi util.
#
# concurrent 구간 동안 nvidia-smi utilization.gpu 를 고빈도(100ms)로 캡처하고,
# meta_concurrent_{A,B}.json 의 t_start/t_end_epoch 로 "둘 다 도는 overlap 윈도우" 만
# 잘라 평균 util / idle% 를 구한다. 그 값을 같은 run 의 occupancy_sum 과 나란히 비교.
#
# 사용: THROTTLE_ALGO=antiphase NITERS=20 IMAGE=fgpu-runtime-pytorch:stage4-infer \
#         bash scripts/eval/run_gpuutil.sh
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ALGO="${THROTTLE_ALGO:-antiphase}"
NITERS="${NITERS:-20}"
IMAGE="${IMAGE:-fgpu-runtime-pytorch:stage4-infer}"
TS="$(date +%Y-%m-%d_%H-%M-%S)"
OUT="${OUT:-$REPO/experiments/gpuutil_${ALGO}_${TS}}"
mkdir -p "$OUT"

echo "[gpuutil] algo=$ALGO niters=$NITERS image=$IMAGE"
echo "[gpuutil] OUT=$OUT"

# 1) nvidia-smi util 로거 시작 (배경)
nvidia-smi --query-gpu=timestamp,utilization.gpu,memory.used \
  --format=csv,nounits -lms 100 > "$OUT/util.csv" 2>/dev/null &
SMIPID=$!
echo "[gpuutil] nvidia-smi logger pid=$SMIPID"

# 2) sharing 실행 (solo A/B + concurrent). concurrent 구간만 분석에 쓴다.
#    OUT 을 하위 디렉토리로 넘겨 산출물을 모은다.
THROTTLE_MODE=conc THROTTLE_ALGO="$ALGO" NITERS="$NITERS" IMAGE="$IMAGE" \
  OUT="$OUT/sharing" bash "$REPO/scripts/eval/run_sharing.sh" \
  > "$OUT/sharing_run.log" 2>&1 || { echo "[gpuutil] run_sharing 실패"; }

# 3) 로거 정지
kill "$SMIPID" 2>/dev/null || true
wait "$SMIPID" 2>/dev/null || true
echo "[gpuutil] util 샘플 수: $(wc -l < "$OUT/util.csv")"

# 4) 분석 — overlap 윈도우의 실측 util vs occupancy_sum
python3 "$REPO/scripts/eval/_gpuutil_report.py" "$OUT" | tee "$OUT/gpuutil_report.txt"

echo "[gpuutil] 완료: $OUT"
