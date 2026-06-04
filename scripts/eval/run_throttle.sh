#!/usr/bin/env bash
# Stage 12-D: duty-cycle throttle 정량 평가.
#
# 두 컨테이너를 순차 실행 (A: ratio=0.3, B: ratio=0.6) 하고,
# 각각의 launches/sec 를 파싱하여 throughput 비율이 ratio 비율에
# tolerance 내로 수렴하는지 검증.
#
# PASS 조건:
#   throughput_ratio = A_lps / B_lps
#   expected_ratio   = RATIO_A / RATIO_B
#   |throughput_ratio - expected_ratio| < TOLERANCE
#
# 사전 조건:
#   - scripts/build_hook.sh   → build/libfgpu.so
#   - scripts/build_image.sh  → fgpu-runtime:stage2 (test_throttle 포함)
#
# 산출물:
#   experiments/throttle_<TS>/summary.txt
#
# 사용법:
#   ./scripts/eval/run_throttle.sh
#   RATIO_A=0.2 RATIO_B=0.8 ./scripts/eval/run_throttle.sh

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
IMAGE="${IMAGE:-fgpu-runtime:stage2}"
HOOK_SO_HOST="${ROOT_DIR}/build/libfgpu.so"
LAUNCH_N="${PYTEST_LAUNCH_N:-5000}"
WINDOW_MS="${FGPU_WINDOW_MS:-100}"
RATIO_A="${RATIO_A:-0.3}"
RATIO_B="${RATIO_B:-0.6}"
TOLERANCE="${TOLERANCE:-0.15}"

TS="$(date +%Y%m%d_%H%M%S)"
OUT_DIR="${ROOT_DIR}/experiments/throttle_${TS}"
mkdir -p "${OUT_DIR}"

echo "=== Stage 12-D: throttle eval ==="
echo "  RATIO_A=${RATIO_A}  RATIO_B=${RATIO_B}  N=${LAUNCH_N}  WINDOW_MS=${WINDOW_MS}"
echo "  TOLERANCE=${TOLERANCE}"
echo "  output: ${OUT_DIR}/"
echo

# ---- run container A ----
echo "[eval-throttle] running container A (ratio=${RATIO_A}) ..."
docker run --rm --gpus all \
    --entrypoint /opt/fgpu/test_throttle \
    -v "${HOOK_SO_HOST}:/opt/fgpu/libfgpu.so:ro" \
    -e LD_PRELOAD=/opt/fgpu/libfgpu.so \
    -e FGPU_THROTTLE_ENABLE=1 \
    -e FGPU_COMPUTE_RATIO="${RATIO_A}" \
    -e FGPU_WINDOW_MS="${WINDOW_MS}" \
    -e FGPU_THROTTLE_LOG_EVERY=0 \
    -e FGPU_LAUNCH_LOG_EVERY=0 \
    -e PYTEST_LAUNCH_N="${LAUNCH_N}" \
    "${IMAGE}" 2>"${OUT_DIR}/container_a_stderr.log" | tee "${OUT_DIR}/container_a_stdout.log"

# ---- run container B ----
echo
echo "[eval-throttle] running container B (ratio=${RATIO_B}) ..."
docker run --rm --gpus all \
    --entrypoint /opt/fgpu/test_throttle \
    -v "${HOOK_SO_HOST}:/opt/fgpu/libfgpu.so:ro" \
    -e LD_PRELOAD=/opt/fgpu/libfgpu.so \
    -e FGPU_THROTTLE_ENABLE=1 \
    -e FGPU_COMPUTE_RATIO="${RATIO_B}" \
    -e FGPU_WINDOW_MS="${WINDOW_MS}" \
    -e FGPU_THROTTLE_LOG_EVERY=0 \
    -e FGPU_LAUNCH_LOG_EVERY=0 \
    -e PYTEST_LAUNCH_N="${LAUNCH_N}" \
    "${IMAGE}" 2>"${OUT_DIR}/container_b_stderr.log" | tee "${OUT_DIR}/container_b_stdout.log"

# ---- parse launches_per_sec ----
A_LPS=$(grep -oP 'launches_per_sec=\K[0-9.]+' "${OUT_DIR}/container_a_stdout.log" || echo "0")
B_LPS=$(grep -oP 'launches_per_sec=\K[0-9.]+' "${OUT_DIR}/container_b_stdout.log" || echo "0")

echo
echo "[eval-throttle] A (ratio=${RATIO_A}): ${A_LPS} launches/sec"
echo "[eval-throttle] B (ratio=${RATIO_B}): ${B_LPS} launches/sec"

# ---- compute verdict ----
# Use awk for floating point math
VERDICT=$(awk -v a_lps="${A_LPS}" -v b_lps="${B_LPS}" \
              -v ratio_a="${RATIO_A}" -v ratio_b="${RATIO_B}" \
              -v tol="${TOLERANCE}" '
BEGIN {
    if (b_lps + 0 == 0) { print "FAIL (B throughput is zero)"; exit }
    throughput_ratio = a_lps / b_lps
    expected_ratio   = ratio_a / ratio_b
    delta = throughput_ratio - expected_ratio
    if (delta < 0) delta = -delta
    printf "throughput_ratio=%.3f expected_ratio=%.3f delta=%.3f ", throughput_ratio, expected_ratio, delta
    if (delta < tol) print "VERDICT: PASS"
    else             print "VERDICT: FAIL"
}')

echo
echo "${VERDICT}"

# ---- write summary ----
{
    echo "Stage 12-D: Throttle Evaluation"
    echo "================================"
    echo "timestamp:     ${TS}"
    echo "launch_n:      ${LAUNCH_N}"
    echo "window_ms:     ${WINDOW_MS}"
    echo "ratio_a:       ${RATIO_A}"
    echo "ratio_b:       ${RATIO_B}"
    echo "tolerance:     ${TOLERANCE}"
    echo ""
    echo "A launches/sec: ${A_LPS}"
    echo "B launches/sec: ${B_LPS}"
    echo ""
    echo "${VERDICT}"
} > "${OUT_DIR}/summary.txt"

echo
echo "[eval-throttle] summary written to ${OUT_DIR}/summary.txt"

# exit code based on verdict
if echo "${VERDICT}" | grep -q "VERDICT: PASS"; then
    exit 0
else
    exit 1
fi
