#!/usr/bin/env bash
# Stage 11: Admission control E2E 검증.
#
# 시나리오
#   0) baseline: /sessions/admission 의 used 캡처
#   1) ratio=0.5 세션 spawn → 201, used += 0.5
#   2) ratio=(남은 capacity + 0.1) 시도 → 409 admission_denied
#   3) 같은 요청 + force=true → 201 (oversubscription)
#   4) /sessions/admission 의 used > 1.0, by_gpu.active_sessions += 2
#   5) 두 세션 정리 → used 가 baseline 으로 복원
#   6) Concurrency: ratio=0.6 두 개 동시 POST → 정확히 1개만 201
#
# 사전 조건
#   - scripts/run_backend.sh 동작 중
#   - 호스트에 docker daemon + GPU + libfgpu.so 빌드돼있음
#
# 산출물
#   experiments/admission_<TS>/
#     baseline.json  step1.json  step2_409.json  step3_force.json
#     step4_after_force.json   step5_cleanup.json
#     concurrency_a.json  concurrency_b.json   summary.txt
#
# 사용법
#   ./scripts/eval/run_admission.sh
#   API_TOKEN=secret ./scripts/eval/run_admission.sh   # auth on 일 때

set -uo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
TS="$(date +%Y%m%d_%H%M%S)"
OUT="${ROOT_DIR}/experiments/admission_${TS}"
mkdir -p "${OUT}"

API="${API:-http://localhost:8000}"
IMAGE="${IMAGE:-fgpu-runtime:stage2}"
API_TOKEN="${API_TOKEN:-}"

curl_args=(-sS)
[[ -n "${API_TOKEN}" ]] && curl_args+=(-H "Authorization: Bearer ${API_TOKEN}")

CREATED_IDS=()

cleanup_created() {
    for sid in "${CREATED_IDS[@]:-}"; do
        [[ -z "${sid}" ]] && continue
        curl "${curl_args[@]}" -X DELETE "${API}/sessions/${sid}" >/dev/null 2>&1 || true
    done
}
trap cleanup_created EXIT

PASS=1
note() { echo "  $*"; }
fail() { echo "  [FAIL] $*"; PASS=0; }

# JSON 헬퍼 — stdlib python 만 사용
jq_get() {
    python3 -c "import json,sys;d=json.load(open('$1'));print(${2})"
}

post_session() {
    local body="$1" out="$2"
    curl "${curl_args[@]}" -o "${out}" -w "%{http_code}" \
        -X POST "${API}/sessions" \
        -H 'Content-Type: application/json' \
        -d "${body}"
}

echo "[admission] artifacts → ${OUT}"
echo

# ---- 0) baseline -------------------------------------------------------- #
echo "=== step 0: baseline /sessions/admission ==="
curl "${curl_args[@]}" "${API}/sessions/admission" > "${OUT}/baseline.json"
cat "${OUT}/baseline.json" | python3 -m json.tool
BASELINE_USED=$(python3 -c "
import json
d = json.load(open('${OUT}/baseline.json'))
all_g = d.get('by_gpu', {}).get('all', {})
print(all_g.get('ratio_used', 0.0))
")
note "baseline used (gpu=all): ${BASELINE_USED}"
echo

# ---- 1) 0.5 세션 spawn -------------------------------------------------- #
echo "=== step 1: POST /sessions {ratio: 0.5}  → expect 201 ==="
CODE=$(post_session "{\"ratio\":0.5,\"image\":\"${IMAGE}\"}" "${OUT}/step1.json")
note "http=${CODE}"
if [[ "${CODE}" == "201" ]]; then
    SID1=$(jq_get "${OUT}/step1.json" "d['id']")
    CREATED_IDS+=("${SID1}")
    note "created sid=${SID1}"
else
    fail "step1: expected 201, got ${CODE}"
fi
echo

# ---- 2) 남은 capacity 보다 큰 요청 → 409 ------------------------------- #
echo "=== step 2: oversubscribe attempt → expect 409 admission_denied ==="
# baseline + 0.5 + 0.6 → 거의 항상 1.0 초과 (baseline 0 이라도 1.1)
CODE=$(post_session "{\"ratio\":0.6,\"image\":\"${IMAGE}\"}" "${OUT}/step2_409.json")
note "http=${CODE}"
ERR=$(python3 -c "
import json
d = json.load(open('${OUT}/step2_409.json'))
print(d.get('detail', {}).get('error', ''))
" 2>/dev/null || echo "")
note "detail.error=${ERR}"
if [[ "${CODE}" == "409" && "${ERR}" == "admission_denied" ]]; then
    note "OK admission rejected"
else
    fail "step2: expected 409 admission_denied, got ${CODE} / ${ERR}"
fi
echo

# ---- 3) force=true 로 우회 → 201 --------------------------------------- #
echo "=== step 3: same request + force=true → expect 201 ==="
CODE=$(post_session "{\"ratio\":0.6,\"image\":\"${IMAGE}\",\"force\":true}" "${OUT}/step3_force.json")
note "http=${CODE}"
if [[ "${CODE}" == "201" ]]; then
    SID2=$(jq_get "${OUT}/step3_force.json" "d['id']")
    CREATED_IDS+=("${SID2}")
    note "created sid=${SID2} (oversubscription)"
else
    fail "step3: expected 201 with force, got ${CODE}"
fi
echo

# ---- 4) admission snapshot 이 oversubscribed 보여줘야 함 -------------- #
echo "=== step 4: /sessions/admission shows used >= baseline + 1.1 ==="
curl "${curl_args[@]}" "${API}/sessions/admission" > "${OUT}/step4_after_force.json"
USED_NOW=$(python3 -c "
import json
d = json.load(open('${OUT}/step4_after_force.json'))
print(d.get('by_gpu', {}).get('all', {}).get('ratio_used', 0.0))
")
EXPECTED=$(python3 -c "print(${BASELINE_USED} + 1.1)")
note "used_now=${USED_NOW}  expected≈${EXPECTED}"
DELTA_OK=$(python3 -c "print(abs(${USED_NOW} - ${EXPECTED}) < 0.001)")
if [[ "${DELTA_OK}" == "True" ]]; then
    note "OK capacity reflects oversubscription"
else
    fail "step4: capacity didn't update as expected"
fi
echo

# ---- 5) cleanup → baseline 으로 복원 ---------------------------------- #
echo "=== step 5: delete both sessions → used returns to baseline ==="
for sid in "${CREATED_IDS[@]}"; do
    curl "${curl_args[@]}" -X DELETE "${API}/sessions/${sid}" >/dev/null
done
CREATED_IDS=()
sleep 0.3
curl "${curl_args[@]}" "${API}/sessions/admission" > "${OUT}/step5_cleanup.json"
USED_AFTER=$(python3 -c "
import json
d = json.load(open('${OUT}/step5_cleanup.json'))
print(d.get('by_gpu', {}).get('all', {}).get('ratio_used', 0.0))
")
note "used_after_cleanup=${USED_AFTER}  baseline=${BASELINE_USED}"
DELTA_OK=$(python3 -c "print(abs(${USED_AFTER} - ${BASELINE_USED}) < 0.001)")
if [[ "${DELTA_OK}" == "True" ]]; then
    note "OK capacity restored"
else
    fail "step5: capacity didn't restore"
fi
echo

# ---- 6) Concurrency: 0.6 + 0.6 동시 POST → 정확히 1개만 통과 ---------- #
echo "=== step 6: concurrent POST {0.6, 0.6} → exactly one 201 ==="
# baseline 이 0 이 아니면 0.6+0.6 둘 다 거부될 수 있음 — baseline 0 가정
# baseline > 0.4 면 step6 skip
CAN_RUN=$(python3 -c "print(${BASELINE_USED} < 0.4)")
if [[ "${CAN_RUN}" != "True" ]]; then
    note "skip — baseline used=${BASELINE_USED} > 0.4, no room for two 0.6 attempts"
else
    (post_session "{\"ratio\":0.6,\"image\":\"${IMAGE}\"}" "${OUT}/concurrency_a.json" > "${OUT}/code_a") &
    (post_session "{\"ratio\":0.6,\"image\":\"${IMAGE}\"}" "${OUT}/concurrency_b.json" > "${OUT}/code_b") &
    wait
    CA=$(cat "${OUT}/code_a")
    CB=$(cat "${OUT}/code_b")
    note "A http=${CA}   B http=${CB}"
    n201=0
    n409=0
    for c in "${CA}" "${CB}"; do
        [[ "${c}" == "201" ]] && n201=$((n201+1))
        [[ "${c}" == "409" ]] && n409=$((n409+1))
    done
    if [[ ${n201} -eq 1 && ${n409} -eq 1 ]]; then
        note "OK exactly one 201 + one 409 (asyncio.Lock serialized check-then-spawn)"
    else
        fail "step6: expected one 201 + one 409, got ${n201} 201s + ${n409} 409s"
    fi
    # cleanup 통과한 세션
    for f in "${OUT}/concurrency_a.json" "${OUT}/concurrency_b.json"; do
        sid=$(python3 -c "
import json
try:
    d=json.load(open('${f}'))
    print(d.get('id', ''))
except: pass
" 2>/dev/null || echo "")
        [[ -n "${sid}" ]] && CREATED_IDS+=("${sid}")
    done
fi
echo

# ---- summary ------------------------------------------------------------ #
SUM="${OUT}/summary.txt"
{
    echo "=== Stage 11 admission control E2E ==="
    echo "timestamp: ${TS}"
    echo "baseline used: ${BASELINE_USED}"
    echo
    if [[ ${PASS} -eq 1 ]]; then
        echo "VERDICT: PASS"
    else
        echo "VERDICT: FAIL — see step*.json under ${OUT}"
    fi
} | tee "${SUM}"
exit $((1 - PASS))
