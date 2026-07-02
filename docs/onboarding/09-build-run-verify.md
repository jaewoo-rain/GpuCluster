# 9장. 빌드·실행·검증 — 스테이지 워크플로우와 스크립트로 굴리기

> 📘 **이 장을 읽고 나면**
>
> - 이 프로젝트가 왜 "Stage(스테이지) 단위" 로 조금씩 쌓아 올리는지 이해해요.
> - 개발자가 실제로 뭘 어떤 **순서**로 실행하는지(`build_hook.sh` → `build_image.sh` → `run_*_in_container.sh` → `run_backend.sh`) 손에 잡혀요.
> - "훅 없이 한 번, 훅 켜고 한 번" 두 번 돌리는 패턴이 왜 검증의 심장인지 알게 돼요.
> - `run_all_tests.sh` 오케스트레이터와 `experiments/` 산출물, 그리고 eval 하네스들이 각각 뭘 "증명" 하는지 초보 눈높이로 설명할 수 있어요.

---

## 9.1 왜 "스테이지 단위" 로 만드나

### (1) 왜 필요한가 / 이 프로젝트에서 왜 중요한가

이 프로젝트는 한 번에 다 만들지 않습니다. **아주 작은 단위(Stage)로 쪼개서, 한 단계를 만들고 → 눈으로 검증하고 → 통과하면 다음으로** 넘어가요. 이유는 간단합니다. LD_PRELOAD 훅, GPU, 도커가 얽혀 있어서 한꺼번에 만들면 어디서 깨졌는지 찾기가 지옥이거든요. 한 스테이지에 한 가지만 추가하면, 문제가 생겨도 "방금 추가한 그거" 가 범인입니다.

이건 이 저장소의 **작업 규칙(workflow rule)** 으로 명문화돼 있어요. `CLAUDE.md` 를 보면: "The owner builds this in numbered stages and verifies each stage before moving on. **Do not jump ahead**" — 즉 한 스테이지가 끝나기 전에 앞서 나가지 말라는 겁니다. 각 스테이지의 결과물은 **그 자체로 빌드되고 검증 가능**해야 해요.

### (2) 일상 비유

레고 설명서예요. 1페이지에서 바닥판, 2페이지에서 기둥, 3페이지에서 지붕... 한 페이지 완성할 때마다 "어, 여기 잘 끼워졌네" 확인하고 넘어갑니다. 만약 100페이지를 한꺼번에 조립하고 마지막에 안 맞으면? 어느 페이지가 틀렸는지 처음부터 다 뜯어야 하죠.

### (3) 스테이지 한눈에 보기

각 스테이지가 "무엇을 더하는지" 만 간단히 표로 정리했어요. 자세한 성공 기준은 `CLAUDE.md` 의 "Stage N success criteria" 섹션마다 있습니다.

| Stage | 무엇을 더하나 (한 줄) |
|-------|----------------------|
| 1 | 호스트에서 훅이 `cudaMalloc` 을 가로채 쿼터 초과 시 거부(DENY)하는지 확인 |
| 2 | 그 훅을 **도커 컨테이너 안**에서도 동작시킴(`-v` 마운트 + `LD_PRELOAD`) |
| 3 | FastAPI **백엔드**가 컨테이너를 세션으로 만들고/조회/삭제(REST API) |
| 4 | **PyTorch** 워크로드에서도 훅이 먹히는지(`torch.empty` OOM 전파) |
| 5-A | 컨테이너 **두 개 동시 실행** — 프로세스별 쿼터 격리 실험 |
| 5-B | 아주 작은 **웹 UI** (세션 만들기/보기) |
| 5-C | **Driver API**(`cuMemAlloc_v2`)까지 훅 확장 |
| 5-D | 훅의 per-call **오버헤드** 마이크로벤치마크 |
| 6 | **VMM API**(`cuMemCreate`)까지 훅 확장 |
| 7 | `cudaLaunchKernel` **커널 실행 횟수 카운팅**(모니터링) |
| 8 | 세션 정보를 **SQLite 에 영속** + 비동기 처리(`asyncio.to_thread`) |
| 9(min) | **Bearer 토큰 인증** + 멀티 GPU 디바이스 핀 지정 |
| 10 | **Jupyter Lab** 인터랙티브 세션 |
| 11 | **입장 제어(admission)** — 세션 ratio 합이 1.0 초과면 거부 |
| 12 | **듀티사이클 컴퓨트 스로틀** — 커널 실행을 시간으로 조절 |

이 로드맵의 배경(왜 이 순서인지)은 [`description.md`](../../description.md) 에 한국어로 길게 적혀 있어요. "왜" 가 궁금하면 그걸 읽으세요.

### (5) 흔한 함정

- "빨리 Stage 12 부터 만들자" 는 유혹. 규칙상 금지예요. 현재 어디까지 왔는지는 **실제 트리에 뭐가 있는지** 로 판단합니다(파일이 곧 진척도).

### (6) 한 줄 요약

> 한 스테이지에 한 가지 기능만 더하고 매번 검증한다 — 그래야 뭐가 깨졌는지 바로 알 수 있어요.

---

## 9.2 개발자가 실제로 실행하는 순서

이 4개 스크립트가 기본 흐름입니다. **위에서 아래로** 흘러가요.

```
build_hook.sh   →   build_image.sh   →   run_*_in_container.sh   →   run_backend.sh
 (훅 .so 만듦)      (도커 이미지 만듦)     (컨테이너로 검증)         (백엔드 API 띄움)
```

### 1단계: `build_hook.sh` — 훅 라이브러리를 만든다

**무엇을 만드나** — 호스트에서 C 소스([`hook/src/fgpu_hook.c`])를 컴파일해 `build/libfgpu.so` 를 만듭니다. 실제 컴파일 줄은 [`scripts/build_hook.sh:15`](../../scripts/build_hook.sh#L15):

```bash
gcc -O2 -fPIC -shared -Wall -Wextra \
    -I"${CUDA_HOME}/include" \
    -o "${BUILD_DIR}/libfgpu.so" \
    "${SRC_DIR}/hook/src/fgpu_hook.c" \
    -L"${CUDA_HOME}/lib64" -lcudart -ldl -lpthread
```

- `-shared -fPIC` : 공유 라이브러리(`.so`)로 만든다는 뜻(다른 프로그램에 끼워 넣을 수 있는 형태).
- `-ldl` : `dlsym`(원래 `cudaMalloc` 주소 찾기)에 필요. `-lpthread` : 락(lock)에 필요.

**무엇을 확인하나** — 마지막에 `ls -lh` 로 파일이 생겼는지 보여줍니다([`scripts/build_hook.sh:22`](../../scripts/build_hook.sh#L22)).

> 훅 코드(`fgpu_hook.c`)를 고쳤다면 **이걸 다시 돌려야** 바뀐 `.so` 가 생깁니다. 3장에서 봤듯이 이 `.so` 는 컨테이너에 마운트되므로, 이미지 재빌드는 필요 없어요.

### 2단계: `build_image.sh` — 런타임 이미지를 만든다

**무엇을 만드나** — [`runtime-image/Dockerfile`](../../runtime-image/Dockerfile) 로 `fgpu-runtime:stage2` 이미지를 빌드합니다(테스트 바이너리들이 컴파일되어 들어 있는 이미지). 핵심 줄 [`scripts/build_image.sh:22`](../../scripts/build_image.sh#L22):

```bash
docker build -f runtime-image/Dockerfile --build-arg CUDA_VERSION="${CUDA_VERSION}" \
    -t "${IMAGE_NAME}:${IMAGE_TAG}" .
```

**무엇을 확인하나** — 빌드 후 `docker images` 로 이미지가 목록에 뜨는지 확인([`scripts/build_image.sh:30`](../../scripts/build_image.sh#L30)).

> Dockerfile 이나 `.cu` 테스트 소스를 고쳤을 때만 다시 돌리면 됩니다. 훅만 고쳤을 땐 안 해도 돼요(그게 3장의 설계 이점).

### 3단계: `run_*_in_container.sh` — 컨테이너로 검증한다

여기가 실제로 "훅이 동작하는가" 를 눈으로 보는 단계예요. 스테이지마다 전용 스크립트가 있습니다.

| 스크립트 | 검증하는 것 |
|----------|-------------|
| [`scripts/run_in_container.sh`](../../scripts/run_in_container.sh) | Stage 2 — `cudaMalloc` 훅 |
| `scripts/run_driver_in_container.sh` | Stage 5-C — Driver API 훅 |
| `scripts/run_vmm_in_container.sh` | Stage 6 — VMM 훅 |
| `scripts/run_launch_in_container.sh` | Stage 7 — 커널 실행 카운팅 |
| `scripts/run_throttle_in_container.sh` | Stage 12 — 컴퓨트 스로틀 |

이 스크립트들의 공통 패턴이 다음 절의 핵심입니다.

### 4단계: `run_backend.sh` — 백엔드 API 를 띄운다

**무엇을 하나** — 파이썬 가상환경 만들고, 의존성 설치하고, uvicorn 으로 FastAPI 를 `:8000` 에 띄웁니다. 마지막 줄 [`scripts/run_backend.sh:46`](../../scripts/run_backend.sh#L46):

```bash
exec uvicorn app.main:app --host "${HOST}" --port "${PORT}" --reload
```

**중요 포인트** — 백엔드는 GPU 가 필요 없어요! 도커 소켓만 있으면 됩니다. 대신 컨테이너를 띄울 때 마운트할 호스트 훅 경로를 환경변수로 알려줍니다 — [`scripts/run_backend.sh:35`](../../scripts/run_backend.sh#L35):

```bash
export FGPU_HOST_HOOK_PATH="${FGPU_HOST_HOOK_PATH:-${ROOT_DIR}/build/libfgpu.so}"
```

즉 백엔드가 컨테이너를 만들 때, 방금 `build_hook.sh` 로 만든 그 `.so` 를 마운트하는 거예요(3장의 `docker_manager.py` 조립부와 연결됩니다).

> 한 줄 요약: 훅 빌드 → 이미지 빌드 → 컨테이너로 검증 → 백엔드로 API 화. 훅만 바꿨으면 1·3만, Dockerfile 바꿨으면 2도, API 만 바꿨으면 4만 다시 돌리면 됩니다.

---

## 9.3 baseline vs hooked — 두 번 실행이 검증의 심장

### (1) 왜 필요한가

훅이 "제대로 일하고 있다" 를 어떻게 증명할까요? 그냥 훅 켜고 한 번 돌려서 로그가 나오는 것만으로는 부족해요. **훅이 없을 때와 있을 때를 나란히 비교** 해야, 차이가 순전히 훅 때문이라고 말할 수 있습니다. 이게 과학 실험의 대조군(control group)과 똑같은 논리예요.

### (2) 일상 비유

약 효과를 증명하려면 "약 안 먹은 사람(baseline)" 과 "약 먹은 사람(hooked)" 을 비교하죠. 약 먹은 사람만 보면 원래 나을 병인지 약 덕분인지 알 수 없어요.

### (3) 작은 예시 — 무엇이 어떻게 달라지나

- **baseline(훅 없음)**: `-v` 마운트도, `-e LD_PRELOAD` 도 안 붙임. 그러면 6 GiB 할당도 (GPU 에 여유만 있으면) 성공하고, `[fgpu]` 로그가 **한 줄도** 안 나옵니다.
- **hooked(훅 켬)**: `-v` 마운트 + `LD_PRELOAD` + `FGPU_RATIO=0.4`. 256 MiB 는 `[fgpu] ALLOW`, 6 GiB 는 쿼터(≈3.2 GiB) 초과라 `[fgpu] DENY` 가 뜨고 프로그램에 OOM 이 전달됩니다.

### (4) 실제 코드

[`scripts/run_in_container.sh`](../../scripts/run_in_container.sh) 가 바로 이 두 번 실행 패턴이에요.

**첫 번째 — baseline** ([`scripts/run_in_container.sh:35`](../../scripts/run_in_container.sh#L35)):

```bash
docker run --rm --gpus all \
    "${IMAGE_NAME}:${IMAGE_TAG}" \
    /opt/fgpu/test_alloc
```

마운트도 `LD_PRELOAD` 도 없죠? 순수한 GPU 그대로입니다.

**두 번째 — hooked** ([`scripts/run_in_container.sh:43`](../../scripts/run_in_container.sh#L43)):

```bash
docker run --rm --gpus all \
    -v "${HOOK_SO_HOST}:/opt/fgpu/libfgpu.so:ro" \
    -e LD_PRELOAD=/opt/fgpu/libfgpu.so \
    -e FGPU_RATIO="${RATIO}" \
    "${IMAGE_NAME}:${IMAGE_TAG}"
```

같은 이미지, 같은 프로그램인데 **훅만 끼웠을 때** DENY 가 나온다는 게 핵심 증거입니다.

### (5) 흔한 함정

- baseline 에서 `[fgpu]` 로그가 한 줄이라도 나오면 뭔가 잘못된 거예요(이전 마운트가 남았거나 이미지에 `.so` 가 실수로 구워졌거나). baseline 은 반드시 "깨끗" 해야 합니다.
- 반대로 hooked 인데 DENY 가 안 나오면? `-cudart shared` 없이 컴파일됐거나(3장 참조), CUDA 버전 불일치로 `.so` 가 안 끼워졌을 가능성이 큽니다.

### (6) 한 줄 요약

> "훅 없이 한 번, 훅 켜고 한 번" 돌려서 차이(DENY 발생)를 보여줘야, 그 차이가 순전히 훅 때문임을 증명할 수 있어요.

---

## 9.4 `run_all_tests.sh` — 모든 스테이지를 한 방에

### (1) 왜 필요한가

스테이지가 12개나 되면 하나씩 손으로 돌리기 벅찹니다. `run_all_tests.sh` 는 **모든 스테이지의 happy-path 검증을 자동으로 한 번에** 돌리고, 마지막에 PASS/FAIL 표를 뽑아주는 오케스트레이터(지휘자)예요.

### (2) 일상 비유

자동차 출고 전 종합 점검 라인. 엔진, 브레이크, 전조등... 하나씩 다 켜보고 "전체 합격/불합격" 도장을 찍어줍니다.

### (4) 실제로 하는 일

[`scripts/run_all_tests.sh`](../../scripts/run_all_tests.sh) 의 흐름:

1. **preflight** — `nvidia-smi` 되나, 도커로 GPU 통과되나 확인([`scripts/run_all_tests.sh:76`](../../scripts/run_all_tests.sh#L76)). 여기서 실패하면 아예 시작 못 해요(드라이버/toolkit 문제).
2. **빌드(있으면 건너뜀)** — 훅 `.so`, 런타임 이미지, PyTorch 이미지를 필요할 때만 빌드([`scripts/run_all_tests.sh:88`](../../scripts/run_all_tests.sh#L88)).
3. **스테이지별 검증** — 각 `run_*_in_container.sh` 를 돌리고, 로그에 기대한 패턴(예: `[fgpu] DENY`)이 있으면 PASS. 예: Stage 2 검사 [`scripts/run_all_tests.sh:118`](../../scripts/run_all_tests.sh#L118):
   ```bash
   pattern_check stage2_container \
       "${ROOT_DIR}/scripts/run_in_container.sh" \
       -- '\[fgpu\] DENY'
   ```
   이 `pattern_check` 헬퍼가 "명령을 돌리고 → 로그에 정규식이 있으면 PASS" 를 해줍니다([`scripts/run_all_tests.sh:55`](../../scripts/run_all_tests.sh#L55)).
4. **백엔드 단위 테스트** — GPU 없이 도는 pytest([`scripts/run_all_tests.sh:146`](../../scripts/run_all_tests.sh#L146)).
5. **백엔드 띄우고** `/healthz` 응답 대기 → smoke API, 5-A 격리, 5-A 상관, 5-D 오버헤드 실행([`scripts/run_all_tests.sh:164`](../../scripts/run_all_tests.sh#L164) 이후).
6. **최종 요약표** — PASS/Fail/Skip 개수와 목록([`scripts/run_all_tests.sh:242`](../../scripts/run_all_tests.sh#L242)). 하나라도 FAIL 이면 종료 코드 1.

핵심 특징 하나: `set -e` 를 **일부러 안 씁니다**([`scripts/run_all_tests.sh:25`](../../scripts/run_all_tests.sh#L25) 주석). 한 단계가 실패해도 멈추지 않고 끝까지 돌려서, "무엇 무엇이 깨졌는지" 를 한 번에 다 수집하려는 거예요.

### 산출물: `experiments/`

모든 실행의 stdout/stderr 는 `experiments/runall_<타임스탬프>/<스텝>.log` 로 저장됩니다([`scripts/run_all_tests.sh:29`](../../scripts/run_all_tests.sh#L29)). 나중에 "왜 이 스텝이 FAIL 이었지?" 를 그 로그 파일에서 확인할 수 있어요. 이 폴더는 커지기 쉬워서 git 에는 안 올립니다(gitignore 대상).

### (5) 흔한 함정

- 처음 한 번은 PyTorch 이미지(약 5 GB 휠 다운로드) 때문에 10분 넘게 걸립니다. 두 번째부터는 빌드를 건너뛰어 훨씬 빨라요.
- GPU/도커가 없는 노트북(예: 지금 이 개발 환경)에서는 preflight 에서 멈춥니다. 이 스크립트는 **GPU 서버(Ubuntu)** 에서 돌리는 거예요.

### (6) 한 줄 요약

> `run_all_tests.sh` 는 모든 스테이지를 자동으로 돌려 PASS/FAIL 표를 뽑고, 각 단계 로그를 `experiments/runall_<TS>/` 에 남깁니다.

---

## 9.5 eval 하네스들 — 각각 무엇을 "증명" 하나

`scripts/eval/` 아래 스크립트들은 단순 통과/실패를 넘어, **논문/발표에 쓸 정량 데이터** 를 만드는 실험 하네스예요. 초보 눈높이로 "이게 뭘 보여주려는 실험인가" 만 정리합니다.

| 스크립트 | 무엇을 증명하려는 실험인가 (쉽게) |
|----------|-----------------------------------|
| [`run_isolation.sh`](../../scripts/eval/run_isolation.sh) | 컨테이너 **둘을 동시에** 띄워, 같은 4 GiB 워크로드가 ratio 차이 **하나 때문에** 한쪽은 OOM, 한쪽은 성공함을 보임. 즉 쿼터가 프로세스별로 격리됨. |
| [`run_overhead.sh`](../../scripts/eval/run_overhead.sh) | 훅을 끼우면 `cudaMalloc`/`cudaFree` 가 얼마나 **느려지는지**(per-call 오버헤드)를 μs 단위로 측정. baseline vs hooked 비교표. |
| [`run_correlation.sh`](../../scripts/eval/run_correlation.sh) | 컨테이너 둘이 공존할 때 (a) `nvidia-smi` 메모리 점유와 (b) 커널 실행 횟수를 **시간축으로 함께** 관측 — 메모리와 컴퓨트 활동을 동시에 그림. |
| [`run_throttle.sh`](../../scripts/eval/run_throttle.sh) | 두 컨테이너에 다른 `FGPU_COMPUTE_RATIO` 를 주고, 커널 **처리량(launches/sec) 비율** 이 설정한 ratio 비율에 수렴하는지(스로틀이 먹히는지) 검증. |
| [`run_admission.sh`](../../scripts/eval/run_admission.sh) | ratio 합이 1.0 을 넘는 두 번째 세션이 **입장 거부(HTTP 409)** 되는지, `force` 로는 통과하는지 확인. |
| [`run_jupyter.sh`](../../scripts/eval/run_jupyter.sh) | Jupyter 세션이 뜨고, 호스트에서 만든 파일이 컨테이너 `/workspace` 에 보이는지(bind mount + 인터랙티브 동작) 확인. |

이 하네스들은 대부분 마지막에 `VERDICT: PASS` 또는 수치 비교 결과를 `experiments/<이름>_<TS>/summary.txt` 에 씁니다. 예를 들어 스로틀 실험의 PASS 조건은 [`scripts/eval/run_throttle.sh:8`](../../scripts/eval/run_throttle.sh#L8) 에 명시돼 있어요:

```
throughput_ratio = A_lps / B_lps
expected_ratio   = RATIO_A / RATIO_B
|throughput_ratio - expected_ratio| < TOLERANCE   → PASS
```

격리 실험(`run_isolation.sh`)은 [`scripts/eval/run_isolation.sh:16`](../../scripts/eval/run_isolation.sh#L16) 에 시나리오가 적혀 있어요: A(ratio 0.4)는 OOM, B(ratio 0.6)는 성공.

> 중요: 이 실험들이 **증명하지 않는 것** 도 알아두세요. "SM(코어) 격리" 는 하지 않아요. 우리는 메모리 쿼터와 (Stage 12의) 시간분할만 다룹니다. 자세한 한계는 CLAUDE.md 의 각 "What it does NOT prove" 를 보세요.

> 한 줄 요약: eval 하네스는 스테이지 기능을 **정량적으로** 증명하는 실험이고, 결과는 `experiments/<이름>_<TS>/summary.txt` 의 PASS/수치로 남습니다.

---

## 9.6 성공 기준을 눈으로 확인하는 법

이 프로젝트의 검증은 결국 **로그에서 특정 문자열을 찾는 것** 으로 귀결됩니다. `[fgpu]` 접두사가 붙은 로그가 그 열쇠예요(그래서 코딩 규칙상 이 접두사를 절대 바꾸지 않습니다).

예를 들어 어떤 실행의 로그에서 성공을 확인하고 싶다면:

```bash
# 훅이 큰 할당을 거부했는가?
grep '\[fgpu\] DENY' 로그파일

# 작은 할당은 허용했는가?
grep '\[fgpu\] ALLOW' 로그파일

# 커널 실행 카운팅(Stage 7) 최종 합계가 찍혔는가?
grep 'exit summary: total cudaLaunchKernel' 로그파일
```

`run_all_tests.sh` 도 사람 대신 이 `grep` 을 자동으로 해주는 것뿐이에요([`scripts/run_all_tests.sh:65`](../../scripts/run_all_tests.sh#L65) 의 `grep -qE "${pattern}"`). 각 스테이지가 로그에 어떤 문자열을 남겨야 PASS 인지는 `CLAUDE.md` 의 "Stage N success criteria" 섹션에 한 줄씩 정확히 적혀 있으니, 새 스테이지를 검증할 땐 그걸 기준표처럼 쓰면 됩니다.

> 한 줄 요약: 성공 확인 = `[fgpu] ALLOW/DENY` 같은 로그 문자열을 grep 으로 찾는 것. CLAUDE.md 의 성공 기준이 곧 채점표예요.

---

## ✍️ 스스로 점검

1. 훅 코드(`fgpu_hook.c`)만 고쳤을 때 다시 돌려야 하는 스크립트는 무엇이고, 다시 안 돌려도 되는 건 무엇인가요? (힌트: 3장의 마운트 설계)
2. baseline 실행과 hooked 실행에서 각각 `[fgpu]` 로그가 나와야 하나요, 안 나와야 하나요? 왜 두 번 다 돌리는 게 검증에 중요한가요?
3. `run_isolation.sh` 실험이 "증명하는 것" 과 "증명하지 못하는 것" 을 각각 하나씩 말해 보세요.

## 🎯 다음 챕터

축하해요, 온보딩 텍스트북의 핵심 흐름을 다 훑었습니다. 이제 실제로 GPU 서버에서 [`LINUX_SETUP.md`](../../LINUX_SETUP.md) 를 따라 환경을 세팅하고, 이 장에서 배운 순서대로 `build_hook.sh` → `build_image.sh` → `run_in_container.sh` 를 직접 돌려 `[fgpu] ALLOW`/`DENY` 를 눈으로 보는 것을 추천합니다. 더 깊은 "왜" 가 궁금하면 [`description.md`](../../description.md) 로 넘어가세요.

---

⟵ [이전: 8장. 세션 생명주기](08-backend-lifecycle-and-admission.md) ・ [📚 전체 목차](README.md) ・ [다음: 10장. 전체를 하나로](10-putting-it-together.md) ⟶
