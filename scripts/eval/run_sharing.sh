#!/usr/bin/env bash
# 재실험 M3 — fGPU 공유: solo A, solo B, concurrent A+B 측정 + overlap 2축 분석.
#
# 세션 매니저(FastAPI) 우회 — eval 패턴(run_overhead.sh 처럼 docker run 직접).
# throttle ON (메모리 quota + duty-cycle compute), 기본 ratio A=0.4 / B=0.6.
# 측정 워크로드: runtime-image-pytorch/test_infer_measure.py (노트북 루프의 헤드리스판).
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
IMAGE="${IMAGE:-fgpu-runtime-pytorch:stage4}"
HOOK_SO="${HOOK_SO:-$REPO/build/libfgpu.so}"
MEAS_PY="$REPO/runtime-image-pytorch/test_infer_measure.py"
RATIO_A="${RATIO_A:-0.4}"
RATIO_B="${RATIO_B:-0.6}"
NITERS="${NITERS:-40}"
MAXTOK="${MAXTOK:-128}"
MODEL="${MODEL:-Qwen/Qwen2-0.5B-Instruct}"
BATCH="${BATCH:-1}"   # 배치>1 = GPU 포화 ↑ (포화 스윕용)
# caching: 기본 ON(현실적 latency + 빠름). 이 실험은 컴퓨트 공유(throttle)+latency 측정이라
# 메모리 quota 가시화(caching off)가 불필요. CACHING_OFF=1 로 강제 off 가능(H1 재현용).
CACHING_OFF="${CACHING_OFF:-0}"
# caching 은 측정 스크립트가 import torch 전에 env 제거로 제어(존재-기반 torch 버전 대응).
if [ "$CACHING_OFF" = "1" ]; then MEAS_CACHING=off; else MEAS_CACHING=on; fi
# throttle 모드:
#   conc = solo 는 throttle OFF(full 속도=원래 작업시간), concurrent 만 ON(4:6) — 정석 오버헤드 측정 (기본)
#   all  = 모든 시나리오 throttle ON (solo 도 묶임 — 비교 왜곡, 비권장)
#   off  = 전부 OFF (메모리만 공유)
THROTTLE_MODE="${THROTTLE_MODE:-conc}"
# throttle 알고리즘: dutycycle(기본, per-process) | antiphase(절대시각 슬롯 게이팅).
# antiphase 일 때 concurrent 슬롯을 겹치지 않게 타일링 → A offset=0, B offset=RATIO_A.
THROTTLE_ALGO="${THROTTLE_ALGO:-dutycycle}"
TS="$(date +%Y-%m-%d_%H-%M-%S)"
OUT="${OUT:-$REPO/experiments/sharing_${TS}}"
HF_CACHE="${HF_CACHE:-$REPO/experiments/hf_cache}"
mkdir -p "$OUT" "$HF_CACHE"

[ -f "$HOOK_SO" ] || { echo "[err] hook .so 없음: $HOOK_SO (build_hook.sh 먼저)"; exit 1; }
[ -f "$MEAS_PY" ] || { echo "[err] 측정 스크립트 없음: $MEAS_PY"; exit 1; }
docker image inspect "$IMAGE" >/dev/null 2>&1 || { echo "[err] 이미지 없음: $IMAGE"; exit 1; }

echo "[run_sharing] REPO=$REPO"
echo "[run_sharing] IMAGE=$IMAGE  ratio A=$RATIO_A B=$RATIO_B  n_iters=$NITERS"
echo "[run_sharing] model=$MODEL  batch=$BATCH  throttle_mode=$THROTTLE_MODE  throttle_algo=$THROTTLE_ALGO  caching_off=$CACHING_OFF"
echo "[run_sharing] OUT=$OUT"

run_one() {
  # $1=label $2=ratio $3=scenario $4=container-name
  local thr=() do_thr=0
  case "$THROTTLE_MODE" in
    all)  do_thr=1 ;;
    conc) [ "$3" = "concurrent" ] && do_thr=1 ;;
    off)  do_thr=0 ;;
  esac
  if [ "$do_thr" = "1" ]; then
    thr=(-e FGPU_THROTTLE_ENABLE=1 -e FGPU_COMPUTE_RATIO="$2"
         -e FGPU_THROTTLE_ALGO="$THROTTLE_ALGO")
    # anti-phase: 슬롯 오프셋 주입. A=0(슬롯 앞), B=RATIO_A(A 슬롯 뒤부터).
    # dutycycle 에선 offset 무시되므로 항상 줘도 무해.
    local off=0
    [ "$1" = "B" ] && off="$RATIO_A"
    thr+=(-e FGPU_COMPUTE_OFFSET="$off")
  fi
  # 컨테이너 stderr(hook [fgpu] 로그) → 별도 파일. stdout([measure] 라인)만 메인 로그로.
  docker run --rm --gpus all --name "$4" \
    -v "$HOOK_SO":/opt/fgpu/libfgpu.so:ro \
    -v "$MEAS_PY":/opt/fgpu/test_infer_measure.py:ro \
    -v "$OUT":/out \
    -v "$HF_CACHE":/cache \
    -e HF_HOME=/cache \
    -e LD_PRELOAD=/opt/fgpu/libfgpu.so \
    -e FGPU_RATIO="$2" \
    "${thr[@]}" \
    -e FGPU_MEAS_CACHING="$MEAS_CACHING" \
    -e FGPU_SCENARIO="$3" -e FGPU_SESSION_LABEL="$1" \
    -e FGPU_MEAS_RATIO="$2" -e FGPU_MEAS_OUT=/out \
    -e FGPU_MEAS_NITERS="$NITERS" -e FGPU_MEAS_MAXTOK="$MAXTOK" \
    -e FGPU_MEAS_MODEL="$MODEL" -e FGPU_MEAS_BATCH="$BATCH" \
    --entrypoint python3 "$IMAGE" /opt/fgpu/test_infer_measure.py \
    2> "$OUT/hooklog_${3}_${1}.txt"
}

echo ""
echo "== [1/3] solo A (ratio=$RATIO_A) — 모델 캐시 워밍 포함 =="
run_one A "$RATIO_A" solo fgpu-meas-solo-a

echo ""
echo "== [2/3] solo B (ratio=$RATIO_B) =="
run_one B "$RATIO_B" solo fgpu-meas-solo-b

echo ""
echo "== [3/3] concurrent A+B (거의 동시 시작) =="
run_one A "$RATIO_A" concurrent fgpu-meas-conc-a & PA=$!
run_one B "$RATIO_B" concurrent fgpu-meas-conc-b & PB=$!
set +e
wait "$PA"; RA=$?
wait "$PB"; RB=$?
set -e
echo "[run_sharing] concurrent exit codes: A=$RA B=$RB (0=OK, 1=OOM)"

echo ""
echo "== 분석 — overlap 2축 리포트 =="
python3 "$REPO/scripts/eval/_sharing_report.py" "$OUT" | tee "$OUT/overlap_report.txt"

echo ""
echo "[run_sharing] 완료. 산출물: $OUT"
ls -1 "$OUT"
