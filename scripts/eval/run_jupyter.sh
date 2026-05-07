#!/usr/bin/env bash
# Stage 10: Jupyter 인터랙티브 세션 검증.
#
# 목표
#   POST /sessions {mode:"jupyter"} 가 컨테이너를 띄우고, 호스트 ephemeral
#   포트가 publish 되며, 그 포트의 jupyter lab 서버가 응답하고, 워크스페이스
#   디렉토리가 호스트와 컨테이너 양쪽에서 보이는지 end-to-end 확인.
#
# 사전 조건
#   - scripts/run_backend.sh 가 별도 터미널에서 동작 중
#   - scripts/build_pytorch_image.sh 로 fgpu-runtime-pytorch:stage4 빌드됨
#     (jupyterlab 포함 버전 — Stage 10 Dockerfile 수정 후 재빌드 필수)
#
# 산출물
#   experiments/jupyter_<TS>/
#     create_response.json
#     api_status.json       jupyter /api/status 응답 (token 인증 통과 증거)
#     workspace_listing.txt 호스트 워크스페이스 ls
#     summary.txt           PASS/FAIL 판정
#
# 사용법
#   ./scripts/eval/run_jupyter.sh
#   RATIO=0.3 ./scripts/eval/run_jupyter.sh
#   API_TOKEN=secret-dev-token ./scripts/eval/run_jupyter.sh   # auth on 일 때

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
TS="$(date +%Y%m%d_%H%M%S)"
OUT_DIR="${ROOT_DIR}/experiments/jupyter_${TS}"
mkdir -p "${OUT_DIR}"

API="${API:-http://localhost:8000}"
RATIO="${RATIO:-0.4}"
IMAGE="${IMAGE:-fgpu-runtime-pytorch:stage4}"
API_TOKEN="${API_TOKEN:-}"

curl_args=(-sS)
if [[ -n "${API_TOKEN}" ]]; then
    curl_args+=(-H "Authorization: Bearer ${API_TOKEN}")
fi

cleanup() {
    if [[ -n "${SID:-}" ]]; then
        echo "[jupyter] cleanup: deleting session ${SID}"
        curl "${curl_args[@]}" -X DELETE \
            "${API}/sessions/${SID}?purge_workspace=true" >/dev/null || true
    fi
}
trap cleanup EXIT

echo "[jupyter] artifacts → ${OUT_DIR}"
echo "[jupyter] healthz check"
curl "${curl_args[@]}" "${API}/healthz" > "${OUT_DIR}/healthz.json"
cat "${OUT_DIR}/healthz.json"; echo

echo "[jupyter] POST /sessions  mode=jupyter ratio=${RATIO} image=${IMAGE}"
curl "${curl_args[@]}" -X POST "${API}/sessions" \
    -H "Content-Type: application/json" \
    -d "{\"ratio\": ${RATIO}, \"mode\": \"jupyter\", \"image\": \"${IMAGE}\"}" \
    > "${OUT_DIR}/create_response.json"
cat "${OUT_DIR}/create_response.json" | python3 -m json.tool > "${OUT_DIR}/create_pretty.json" || true

SID=$(python3 -c "import json,sys;print(json.load(open('${OUT_DIR}/create_response.json'))['id'])")
HOST_PORT=$(python3 -c "import json,sys;print(json.load(open('${OUT_DIR}/create_response.json'))['host_port'])")
TOKEN=$(python3 -c "import json,sys;print(json.load(open('${OUT_DIR}/create_response.json'))['jupyter_token'])")
JUPYTER_URL=$(python3 -c "import json,sys;print(json.load(open('${OUT_DIR}/create_response.json'))['jupyter_url'])")
WORKSPACE=$(python3 -c "import json,sys;print(json.load(open('${OUT_DIR}/create_response.json'))['workspace_dir'])")

echo "[jupyter] sid=${SID}  host_port=${HOST_PORT}  workspace=${WORKSPACE}"
echo "[jupyter] jupyter_url=${JUPYTER_URL}"

# Jupyter 서버가 부팅하는 데 ~1-3초 필요. 짧은 폴링으로 /api/status 가
# 200 떨어질 때까지 대기.
echo "[jupyter] waiting for jupyter server to boot ..."
JUPYTER_OK=0
for i in $(seq 1 30); do
    if curl -sS -o "${OUT_DIR}/api_status.json" -w "%{http_code}" \
        -H "Authorization: token ${TOKEN}" \
        "http://localhost:${HOST_PORT}/api/status" | grep -q '^200$'; then
        JUPYTER_OK=1
        break
    fi
    sleep 0.5
done

if (( JUPYTER_OK == 1 )); then
    echo "[jupyter] /api/status OK after ${i} polls"
else
    echo "[jupyter] /api/status FAIL — server may not have started"
    docker logs "fgpu-${SID}" 2>&1 | tail -40 > "${OUT_DIR}/container.log" || true
fi

# 워크스페이스 호스트 측 ls — touch 한 파일이 컨테이너 안에서 보이는지 확인.
TEST_FILE="${WORKSPACE}/host_touched.txt"
echo "host wrote at ${TS}" > "${TEST_FILE}"
echo "[jupyter] wrote ${TEST_FILE}"

# 컨테이너 안에서 /workspace ls — bind-mount 가 양방향 visibility 인지 검증.
docker exec "fgpu-${SID}" ls -la /workspace > "${OUT_DIR}/container_workspace_ls.txt" 2>&1 || true
ls -la "${WORKSPACE}" > "${OUT_DIR}/host_workspace_ls.txt" 2>&1 || true

# Jupyter 의 /api/contents/host_touched.txt 로도 같은 파일이 보여야 함.
curl -sS -o "${OUT_DIR}/api_contents.json" -w "%{http_code}" \
    -H "Authorization: token ${TOKEN}" \
    "http://localhost:${HOST_PORT}/api/contents/host_touched.txt" \
    > "${OUT_DIR}/api_contents.code" || true

# ---- summary -------------------------------------------------------------
SUMMARY="${OUT_DIR}/summary.txt"
{
    echo "=== Stage 10 Jupyter session experiment ==="
    echo "timestamp:    ${TS}"
    echo "image:        ${IMAGE}"
    echo "ratio:        ${RATIO}"
    echo "session id:   ${SID}"
    echo "host_port:    ${HOST_PORT}"
    echo "jupyter_url:  ${JUPYTER_URL}"
    echo "workspace:    ${WORKSPACE}"
    echo
    echo "Jupyter /api/status:        $([[ ${JUPYTER_OK} -eq 1 ]] && echo OK || echo FAIL)"
    echo "Host wrote test file:        $(test -f "${TEST_FILE}" && echo OK || echo FAIL)"
    echo "Container sees host file:    $(grep -q host_touched.txt "${OUT_DIR}/container_workspace_ls.txt" && echo OK || echo FAIL)"
    echo "Jupyter /api/contents code:  $(cat "${OUT_DIR}/api_contents.code" 2>/dev/null || echo '?')"

    PASS=1
    [[ ${JUPYTER_OK} -eq 1 ]] || PASS=0
    grep -q host_touched.txt "${OUT_DIR}/container_workspace_ls.txt" || PASS=0
    grep -q '^200$' "${OUT_DIR}/api_contents.code" 2>/dev/null || PASS=0
    echo
    if (( PASS == 1 )); then
        echo "VERDICT: PASS"
    else
        echo "VERDICT: FAIL — see container.log / api_status.json / *_workspace_ls.txt"
    fi
} | tee "${SUMMARY}"
