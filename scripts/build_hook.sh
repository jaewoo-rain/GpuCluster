#!/usr/bin/env bash
# Build libfgpu.so.
#
# 두 가지 경로를 자동 선택한다:
#   1) 호스트에 CUDA 툴킷 (gcc + ${CUDA_HOME}/include/cuda_runtime.h) 이 있으면
#      네이티브로 빌드 — 원래 동작 (Ubuntu GPU 서버).
#   2) 호스트에 CUDA 툴킷이 없으면 (예: WSL2 / Docker Desktop, 호스트엔 드라이버만)
#      CUDA devel 이미지 컨테이너 안에서 gcc 로 빌드. 산출물 .so 는 -v 마운트로
#      호스트 build/ 에 떨어진다. 호스트 CUDA 설치 불필요.
#
# env override:
#   CUDA_HOME           기본 /usr/local/cuda  (네이티브 빌드 경로)
#   BUILD_IMAGE         폴백 컨테이너 이미지. 기본은 fgpu-runtime:stage2 가
#                       있으면 그걸, 없으면 nvidia/cuda:${CUDA_VERSION}-devel-ubuntu22.04
#   CUDA_VERSION        폴백 base 이미지 태그. 기본 12.4.1 (build_image.sh 와 일치)
#   FGPU_FORCE_CONTAINER_BUILD=1  네이티브 가능해도 강제로 컨테이너 빌드

set -euo pipefail

CUDA_HOME="${CUDA_HOME:-/usr/local/cuda}"
CUDA_VERSION="${CUDA_VERSION:-12.4.1}"
SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_DIR="${SRC_DIR}/build"

mkdir -p "${BUILD_DIR}"

# gcc 컴파일 인자 (네이티브/컨테이너 공통). 컨테이너 안에서는 CUDA 가 항상
# /usr/local/cuda 에 있으므로 그 경로를 그대로 쓴다.
build_args() {  # $1 = cuda_home
    local ch="$1"
    echo "-O2 -fPIC -shared -Wall -Wextra \
        -I${ch}/include \
        -o build/libfgpu.so \
        hook/src/fgpu_hook.c \
        -L${ch}/lib64 -lcudart -ldl -lpthread"
}

native_possible() {
    [[ "${FGPU_FORCE_CONTAINER_BUILD:-0}" != "1" ]] \
        && command -v gcc >/dev/null 2>&1 \
        && [[ -f "${CUDA_HOME}/include/cuda_runtime.h" ]]
}

if native_possible; then
    echo "[build] 네이티브 빌드 (host CUDA=${CUDA_HOME})"
    cd "${SRC_DIR}"
    # shellcheck disable=SC2046
    gcc $(build_args "${CUDA_HOME}")
else
    # 폴백 이미지 결정.
    if [[ -n "${BUILD_IMAGE:-}" ]]; then
        img="${BUILD_IMAGE}"
    elif docker image inspect fgpu-runtime:stage2 >/dev/null 2>&1; then
        img="fgpu-runtime:stage2"
    else
        img="nvidia/cuda:${CUDA_VERSION}-devel-ubuntu22.04"
    fi
    echo "[build] host CUDA 없음 → 컨테이너 빌드 (image=${img})"
    # --entrypoint gcc: fgpu-runtime 이미지는 entrypoint 가 fgpu-entrypoint 라
    #   gcc 를 직접 실행하도록 덮어쓴다. nvidia/cuda base 는 entrypoint 가 없어
    #   영향 없음.
    # --user: 산출물 .so 가 root 소유로 떨어지지 않게 호스트 uid 로 실행.
    # GPU 불필요 (컴파일만) — --gpus 안 붙임.
    # shellcheck disable=SC2046
    docker run --rm \
        --user "$(id -u):$(id -g)" \
        --entrypoint gcc \
        -v "${SRC_DIR}:/src" -w /src \
        "${img}" \
        $(build_args /usr/local/cuda)
fi

echo "[build] wrote ${BUILD_DIR}/libfgpu.so"
ls -lh "${BUILD_DIR}/libfgpu.so"
