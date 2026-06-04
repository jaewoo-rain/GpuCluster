#!/usr/bin/env bash
# Stage 12 검증: duty-cycle throttle — cudaLaunchKernel 에 시간 제어 삽입.
#
# 컨테이너 안에서 test_throttle 를 세 번 실행:
#   (1) baseline       — hook 없이. 원시 throughput 측정.
#   (2) hooked, OFF    — FGPU_THROTTLE_ENABLE=0. Stage 7 카운터만 동작,
#                         throughput ≈ baseline.
#   (3) hooked, ON     — FGPU_THROTTLE_ENABLE=1, FGPU_COMPUTE_RATIO=0.4,
#                         FGPU_WINDOW_MS=100. throughput ≈ baseline × 0.4.
#
# 사전 조건:
#   - scripts/build_hook.sh   → build/libfgpu.so   (Stage 12 변경 반영된 새 hook)
#   - scripts/build_image.sh  → fgpu-runtime:stage2 (test_throttle 포함된 새 이미지)
#
# 사용법:
#   ./scripts/run_throttle_in_container.sh
#   PYTEST_LAUNCH_N=10000 FGPU_COMPUTE_RATIO=0.3 ./scripts/run_throttle_in_container.sh

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
IMAGE="${IMAGE:-fgpu-runtime:stage2}"
HOOK_SO_HOST="${ROOT_DIR}/build/libfgpu.so"
LAUNCH_N="${PYTEST_LAUNCH_N:-5000}"
COMPUTE_RATIO="${FGPU_COMPUTE_RATIO:-0.4}"
WINDOW_MS="${FGPU_WINDOW_MS:-100}"
THROTTLE_LOG_EVERY="${FGPU_THROTTLE_LOG_EVERY:-100}"

if [[ ! -f "${HOOK_SO_HOST}" ]]; then
    echo "ERROR: ${HOOK_SO_HOST} 가 없음. scripts/build_hook.sh 먼저." >&2
    exit 1
fi
if ! docker image inspect "${IMAGE}" >/dev/null 2>&1; then
    echo "ERROR: 이미지 ${IMAGE} 없음. scripts/build_image.sh 먼저." >&2
    exit 1
fi
if ! docker run --rm --entrypoint /bin/sh "${IMAGE}" \
        -c '[ -x /opt/fgpu/test_throttle ]' >/dev/null 2>&1; then
    echo "ERROR: ${IMAGE} 안에 /opt/fgpu/test_throttle 가 없음." >&2
    echo "       Dockerfile 갱신 후 scripts/build_image.sh 로 재빌드 필요." >&2
    exit 1
fi

echo "============================================================"
echo "[throttle-test] (1/3) baseline — hook 없이  (n=${LAUNCH_N})"
echo "============================================================"
docker run --rm --gpus all \
    --entrypoint /opt/fgpu/test_throttle \
    -e PYTEST_LAUNCH_N="${LAUNCH_N}" \
    "${IMAGE}"

echo
echo "============================================================"
echo "[throttle-test] (2/3) hooked, throttle OFF  (n=${LAUNCH_N})"
echo "============================================================"
docker run --rm --gpus all \
    --entrypoint /opt/fgpu/test_throttle \
    -v "${HOOK_SO_HOST}:/opt/fgpu/libfgpu.so:ro" \
    -e LD_PRELOAD=/opt/fgpu/libfgpu.so \
    -e FGPU_THROTTLE_ENABLE=0 \
    -e FGPU_LAUNCH_LOG_EVERY=0 \
    -e PYTEST_LAUNCH_N="${LAUNCH_N}" \
    "${IMAGE}"

echo
echo "============================================================"
echo "[throttle-test] (3/3) hooked, throttle ON  (ratio=${COMPUTE_RATIO}, window=${WINDOW_MS}ms, n=${LAUNCH_N})"
echo "============================================================"
docker run --rm --gpus all \
    --entrypoint /opt/fgpu/test_throttle \
    -v "${HOOK_SO_HOST}:/opt/fgpu/libfgpu.so:ro" \
    -e LD_PRELOAD=/opt/fgpu/libfgpu.so \
    -e FGPU_THROTTLE_ENABLE=1 \
    -e FGPU_COMPUTE_RATIO="${COMPUTE_RATIO}" \
    -e FGPU_WINDOW_MS="${WINDOW_MS}" \
    -e FGPU_THROTTLE_LOG_EVERY="${THROTTLE_LOG_EVERY}" \
    -e FGPU_LAUNCH_LOG_EVERY=0 \
    -e PYTEST_LAUNCH_N="${LAUNCH_N}" \
    "${IMAGE}"

echo
echo "[throttle-test] done. 기대 결과:"
echo "  (1) baseline: [fgpu] 라인 없음. throughput 보고."
echo "  (2) throttle OFF: [fgpu] init 에 throttle=off. throughput ≈ baseline."
echo "  (3) throttle ON (ratio=${COMPUTE_RATIO}):"
echo "      [fgpu] init 에 throttle=on compute_ratio=${COMPUTE_RATIO} window_ms=${WINDOW_MS}"
echo "      [fgpu] THROTTLE sleep=NNms 라인 존재."
echo "      throughput ≈ baseline × ${COMPUTE_RATIO}."
echo "      exit summary 에 throttle sleep count 포함."
