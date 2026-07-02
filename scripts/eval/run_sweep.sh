#!/usr/bin/env bash
# 포화 스윕 — run_sharing.sh 를 (batch × throttle × reps) 조합으로 반복 실행 후 평균±편차 집계.
#
# 각 조합: solo A(throttle 모드 따름) / solo B / concurrent. run_sharing.sh 그대로 사용.
# 결과 디렉토리: experiments/sweep_<TS>/b<batch>_thr<mode>_rep<r>/
set -u

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export IMAGE="${IMAGE:-fgpu-runtime-pytorch:stage4-infer}"
export MODEL="${MODEL:-Qwen/Qwen2-0.5B-Instruct}"
export RATIO_A="${RATIO_A:-0.4}" RATIO_B="${RATIO_B:-0.6}"
export NITERS="${NITERS:-12}"
REPS="${REPS:-3}"
BATCHES="${BATCHES:-1 8 16}"
THROTTLES="${THROTTLES:-off conc}"
# throttle 알고리즘 패스스루 — run_sharing.sh 가 읽는다. dutycycle(기본)|antiphase.
export THROTTLE_ALGO="${THROTTLE_ALGO:-dutycycle}"
TS="$(date +%Y-%m-%d_%H-%M-%S)"
SWEEP="${SWEEP:-$REPO/experiments/sweep_$TS}"
mkdir -p "$SWEEP"

echo "[sweep] dir=$SWEEP"
echo "[sweep] model=$MODEL ratio=$RATIO_A/$RATIO_B niters=$NITERS reps=$REPS"
echo "[sweep] batches=[$BATCHES] throttles=[$THROTTLES] algo=$THROTTLE_ALGO"

n=0
for b in $BATCHES; do
  for thr in $THROTTLES; do
    for r in $(seq 1 "$REPS"); do
      n=$((n+1))
      name="b${b}_thr${thr}_rep${r}"
      OUT="$SWEEP/$name"
      echo "[sweep] ($n) start $name"
      BATCH="$b" THROTTLE_MODE="$thr" OUT="$OUT" \
        bash "$REPO/scripts/eval/run_sharing.sh" > "$SWEEP/$name.log" 2>&1
      echo "[sweep] ($n) done  $name"
    done
  done
done

echo "[sweep] aggregating ..."
python3 "$REPO/scripts/eval/_sweep_aggregate.py" "$SWEEP" | tee "$SWEEP/summary.txt"
echo "[sweep] 완료: $SWEEP"
