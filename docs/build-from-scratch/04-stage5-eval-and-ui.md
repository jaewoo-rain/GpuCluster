# 4장. Stage 5 — 평가 인프라와 웹 UI

> **이 장에서 만들 것**
> - 두 컨테이너를 동시에 띄워 쿼터 격리를 증명하는 **isolation 실험**(`test_hold.py` + `run_isolation.sh`).
> - 훅의 per-call 오버헤드를 μs 단위로 뽑는 **overhead 마이크로벤치**(`bench_alloc.cu` + `run_overhead.sh`).
> - launch 카운터 ↔ nvidia-smi 메모리를 PID로 조인하는 **correlation 확장**(`test_compute.py` + `run_correlation.sh` + `_correlate.py`).
> - 빌드 도구 0개로 만드는 **vanilla JS 웹 UI**(`app/static/index.html`, `FileResponse` 서빙, 3초 폴링).
> - 위 전부를 한 번에 돌리는 **`run_all_tests.sh` 오케스트레이터**와 `experiments/` 아티팩트.

3장까지 백엔드가 `POST /sessions`로 컨테이너를 띄우고 SQLite에 세션을 남기게 됐습니다. 기능은 됩니다. 그런데 캡스톤/논문은 **"된다"만으로는 부족합니다.** "얼마나 잘 되는지, 무엇을 증명하는지"를 **데이터**로 보여야 합니다. Stage 5는 그 데이터를 만드는 인프라를 짓는 단계입니다.

---

## 1. 이 단계 목표

왜 하필 지금 평가 인프라를 만들까요? 순서의 논리입니다.

- Stage 1~4에서 **기능**(호스트 훅 → 컨테이너 → PyTorch)이 검증됐습니다.
- Stage 3에서 **여러 개를 띄우는 수단**(백엔드 API)이 생겼습니다.
- 이제 "여러 개를 동시에 띄웠을 때 정말 격리되나?", "훅이 느리진 않나?", "메모리와 컴퓨트를 동시에 관측할 수 있나?" 같은 **정량 질문**에 답할 때입니다.

핵심은 **재사용 가능한 실험 스크립트**를 만드는 것입니다. 한 번 손으로 돌려보고 끝이 아니라, `experiments/<TS>/` 아래 아티팩트를 남기고 PASS/FAIL을 자동 판정해서, 파라미터만 바꿔 몇 번이고 재현할 수 있어야 합니다. 새 실험 스크립트는 전부 `scripts/eval/`에 모읍니다 — 흩어지면 관리가 안 됩니다.

---

## 2. 개발 순서 체크리스트

```
[ ] 1. Stage 5-A: test_hold.py — alloc 후 HOLD_SEC 초 보유 (시간 겹침 만들기)
[ ] 2. run_isolation.sh — nvidia-smi 백그라운드 캡처 → 두 세션 spawn → 폴링 → PASS/FAIL
[ ] 3. 검증 게이트: A(작은 ratio)=OOM, B(큰 ratio)=OK, VERDICT: PASS
[ ] --- Stage 5-A 완료 ---
[ ] 4. Stage 5-D: bench_alloc.cu — CSV 스트리밍 마이크로벤치
[ ] 5. run_overhead.sh — baseline/hooked 두 번 → 파이썬으로 p50/p99 요약
[ ] 6. 검증 게이트: summary.txt 에 markdown 표 + Δ mean %
[ ] --- Stage 5-D 완료 ---
[ ] 7. 5-A 확장: test_compute.py — alloc + matmul 루프 (launch 다발 + 메모리)
[ ] 8. run_correlation.sh + _correlate.py — PID join 으로 두 시계열 병합
[ ] --- correlation 완료 ---
[ ] 9. Stage 5-B: app/static/index.html — vanilla JS UI, FileResponse 서빙
[ ] 10. 검증 게이트: GET / → UI, create → 행 추가, 3초 폴링
[ ] --- Stage 5-B 완료 ---
[ ] 11. run_all_tests.sh — 전부 오케스트레이션, PASS/FAIL 표
```

---

## 3. Stage 5-A — 동시 격리 실험

### 스텝 1. `test_hold.py` — 왜 새 워크로드가 필요한가

`test_pytorch.py`는 alloc → free → **즉시 종료**입니다. 그런데 격리 실험의 핵심은 **두 컨테이너가 GPU 메모리를 동시에 점유하는 시간 윈도우**를 만드는 것입니다. 즉시 끝나면 겹칠 틈이 없죠. 그래서 "한 번 잡고 잠깐 붙들고 있는" 워크로드가 따로 필요합니다([test_hold.py:9-12](../../runtime-image-pytorch/test_hold.py#L9)).

설계에서 가장 영리한 부분은 **종료 코드로 결과를 전달**하는 것입니다([test_hold.py:17-20](../../runtime-image-pytorch/test_hold.py#L17)):

```
0  할당 성공 + hold 완료   (PASS-B 케이스)
1  할당 실패 — DENY 가 OOM 으로 전파   (PASS-A 케이스)
2  CUDA 자체 불가   (환경 오류)
```

핵심 로직([test_hold.py:48-62](../../runtime-image-pytorch/test_hold.py#L48)):

```python
try:
    t = torch.empty(n_floats, dtype=torch.float32, device="cuda:0")
    torch.cuda.synchronize()
    print(f"[hold-test] OK   ptr={hex(t.data_ptr())}", flush=True)
except torch.cuda.OutOfMemoryError as e:
    print(f"[hold-test] OOM  ← cudaErrorMemoryAllocation 이 PyTorch 까지 전파됨", ...)
    sys.exit(1)

time.sleep(hold_sec)      # ← 이 sleep 이 시간 겹침을 만든다
```

`sys.exit(1)`로 나가면, 나중에 스크립트가 세션 JSON의 `exit_code`만 보고 "이 컨테이너는 OOM으로 죽었다"를 알 수 있습니다. 로그 파싱과 종료 코드, **두 경로로 교차 검증**하는 게 포인트입니다.

### 스텝 2. `run_isolation.sh` — 실험 드라이버 짜는 법

이 스크립트가 이 장에서 배울 "실험 드라이버 패턴"의 원형입니다. 순서를 뜯어봅시다.

**(a) 사전 조건 확인**([run_isolation.sh:68-76](../../scripts/eval/run_isolation.sh#L68)): 백엔드가 떠 있나(`/healthz`), `nvidia-smi`가 PATH에 있나. 없으면 뭘 먼저 하라고 알려주고 종료.

**(b) nvidia-smi를 세션보다 *먼저* 백그라운드로 캡처**([run_isolation.sh:78-84](../../scripts/eval/run_isolation.sh#L78)):

```bash
nvidia-smi --query-compute-apps=timestamp,pid,process_name,used_memory \
           --format=csv,noheader -l 1 >> "${NVSMI_LOG}" &
NVSMI_PID=$!
trap "kill ${NVSMI_PID} 2>/dev/null || true" EXIT
```

왜 세션보다 먼저? 컨테이너가 메모리를 잡는 **순간부터** trace가 있어야 하기 때문입니다. `-l 1`은 1초 폴링. `trap ... EXIT`는 스크립트가 어떻게 끝나든 이 백그라운드 프로세스를 반드시 죽이는 안전장치입니다 — 안 그러면 좀비 `nvidia-smi`가 남습니다. **이게 독립적 ground truth**입니다: 우리 훅 로그를 못 믿겠다는 사람에게도 nvidia-smi는 객관적 증거가 됩니다.

**(c) 두 세션을 다른 ratio로 spawn**([run_isolation.sh:90-93](../../scripts/eval/run_isolation.sh#L90)). `post_session` 헬퍼가 `curl -X POST /sessions`로 `test_hold.py`를 다른 ratio로 띄웁니다. 응답 JSON에서 `id`를 뽑는 건 stdlib `json`을 쓰는 작은 파이썬 one-liner([run_isolation.sh:50-52](../../scripts/eval/run_isolation.sh#L50)) — `jq` 의존성을 피합니다.

> **정직한 주석 하나.** 스크립트는 "현재 백엔드는 docker SDK 호출이 sync라 *세션 생성* 자체는 직렬화되지만, 워크로드의 hold 구간은 시간이 겹친다"고 밝힙니다([run_isolation.sh:87-89](../../scripts/eval/run_isolation.sh#L87)). Stage 8에서 `asyncio.to_thread`로 감싸면 생성도 진짜 동시가 됩니다. 실험이 무엇을 증명하고 무엇을 아직 못 하는지 정확히 적어두는 태도가 논문 신뢰도를 만듭니다.

**(d) 둘 다 `exited`될 때까지 폴링**([run_isolation.sh:103-115](../../scripts/eval/run_isolation.sh#L103)): `DEADLINE`으로 타임아웃을 걸어 무한 대기를 막습니다.

**(e) 로그·세션 메타 저장 → DELETE로 정리**([run_isolation.sh:122-134](../../scripts/eval/run_isolation.sh#L122)).

**(f) PASS/FAIL 판정 — 다중 신호 AND**([run_isolation.sh:136-147](../../scripts/eval/run_isolation.sh#L136)):

```bash
[[ "${EXIT_A}" == "1" && "${LOG_A_OOM}" -ge 1 && "${HOOK_A_DENY}" -ge 1 ]] && PASS_A=1
[[ "${EXIT_B}" == "0" && "${LOG_B_OK}"  -ge 1 && "${HOOK_B_ALLOW}" -ge 1 ]] && PASS_B=1
```

판정 철학이 여기 담겨 있습니다: **하나의 신호만 믿지 않습니다.** A가 PASS하려면 (1) 종료 코드 1 **그리고** (2) 로그에 `[hold-test] OOM` **그리고** (3) 훅 로그에 `DENY` — 세 가지가 전부 맞아야 합니다. 이렇게 AND로 묶으면 우연히 통과하는 false positive를 막습니다.

### 검증 게이트 (Stage 5-A)

```bash
./scripts/build_pytorch_image.sh    # test_hold.py 추가됐으니 재빌드
./scripts/run_backend.sh            # 다른 터미널
./scripts/eval/run_isolation.sh     # → experiments/isolation_<TS>/
```

`experiments/isolation_<TS>/summary.txt`가 `VERDICT: PASS`여야 합니다([CLAUDE.md Stage 5-A 기준](../../CLAUDE.md)). A(ratio 0.4)는 OOM+DENY, B(ratio 0.6)는 OK+ALLOW. `nvidia_smi.csv`에 겹침 구간 데이터가 있어야 합니다.

**이 실험이 증명하는 것:** 두 프로세스가 각자 *자기만의* 쿼터/카운터를 봅니다(훅 상태는 프로세스별 — LD_PRELOAD가 컨테이너마다 새 인스턴스를 주입). 같은 4 GiB 워크로드가 **오직 ratio 차이 때문에** 성공/실패로 갈립니다. **증명 못 하는 것:** SM 격리(둘 다 같은 SM을 경쟁), 캐싱 켰을 때의 정확성, 비협조적 테넌트에 대한 저항(협조적 위협 모델).

---

## 4. Stage 5-D — 오버헤드 마이크로벤치

격리를 증명했으니 이제 "훅이 얼마나 느린가"를 정량화합니다. 논문 evaluation 섹션의 headline 숫자입니다.

### 스텝 4. `bench_alloc.cu` — CSV를 스트리밍하는 이유

측정 도구는 **호스트 단일 시계**로 `cudaMalloc`/`cudaFree` 한 호출의 wall-clock을 잽니다([bench_alloc.cu:7-16](../../hook/tests/bench_alloc.cu#L7)). `cudaMalloc`은 host-side 동기 함수라 kernel launch와 달리 단일 시계로 충분합니다.

측정 방식([bench_alloc.cu:106-143](../../hook/tests/bench_alloc.cu#L106)): 사이즈별로 WARMUP회(미측정, 페이지 매핑/드라이버 캐시 워밍) → N회 본 측정. 매 회 alloc/free 시각을 찍어 **한 줄 CSV로 stdout에 즉시 출력**합니다:

```c
printf("%zu,%d,%lld,%lld\n", sz_mib, i, t1 - t0, t3 - t2);
```

**왜 스트리밍 CSV일까요?** 두 가지 설계 결정이 겹칩니다:

1. **`[fgpu]` 훅 로그는 stderr, CSV 데이터는 stdout.** 그래서 stdout만 파이프하면 순수 CSV가 나옵니다([bench_alloc.cu:17-23](../../hook/tests/bench_alloc.cu#L17)). 훅 로그와 측정값이 안 섞입니다.
2. 메모리에 다 모았다가 마지막에 뱉지 않고 매 iteration 즉시 출력 — 중간에 죽어도 데이터가 남고, 파이프로 바로 후처리에 흘려보낼 수 있습니다.

CUDA 컨텍스트를 `cudaFree(0)`으로 미리 초기화하는 것도 포인트([bench_alloc.cu:94-100](../../hook/tests/bench_alloc.cu#L94)) — 첫 호출의 컨텍스트 생성 비용이 측정 첫 샘플에 섞이지 않게 합니다.

### 스텝 5. `run_overhead.sh` — baseline/hooked → 통계

**(a) bench_alloc이 이미지에 있는지 먼저 확인**([run_overhead.sh:48-54](../../scripts/eval/run_overhead.sh#L48)):

```bash
if ! docker run --rm --entrypoint /bin/sh "${IMAGE}" \
        -c '[ -x /opt/fgpu/bench_alloc ]' >/dev/null 2>&1; then
    echo "ERROR: ${IMAGE} 안에 /opt/fgpu/bench_alloc 가 없음."
    echo "       scripts/build_image.sh 로 이미지 재빌드 필요."
```

이게 2장에서 강조한 "**Dockerfile 바뀌면 재빌드**" 함정을 실행 시점에 잡아주는 방어 코드입니다. `bench_alloc.cu`를 추가하고 이미지를 안 다시 만들었으면 여기서 멈춥니다.

**(b) 같은 바이너리를 두 번**([run_overhead.sh:62-91](../../scripts/eval/run_overhead.sh#L62)): baseline(훅 없이) → hooked(`FGPU_RATIO=0.95` — 모든 사이즈가 쿼터 안에 들어가 전부 ALLOW되도록 일부러 크게). 여기서는 **백엔드를 거치지 않고 `docker run --entrypoint /opt/fgpu/bench_alloc`로 직접** 실행합니다([run_overhead.sh:66-73](../../scripts/eval/run_overhead.sh#L66)) — 측정 대상이 훅 오버헤드지 API 오버헤드가 아니기 때문입니다. stdout은 `*_raw.csv`, stderr는 `*_stderr.log`로 분리 저장.

**(c) 파이썬으로 요약**([run_overhead.sh:94-199](../../scripts/eval/run_overhead.sh#L94)): stdlib만으로 두 CSV를 읽어 사이즈별 mean/p50/p99를 μs로 계산하고, `summary.csv`(기계용)와 `summary.txt`(논문용 markdown 표, `Δ mean %` 컬럼 포함)를 만듭니다. p99를 직접 구현한 백분위 함수로 뽑는 것도 볼 만합니다([run_overhead.sh:117-123](../../scripts/eval/run_overhead.sh#L117)).

### 검증 게이트 (Stage 5-D)

```bash
./scripts/build_image.sh            # bench_alloc.cu 추가 → 재빌드
./scripts/eval/run_overhead.sh      # → experiments/overhead_<TS>/summary.txt
```

`summary.txt`에 cudaMalloc / cudaFree 두 개의 markdown 표가 나오고, 각 행이 baseline vs hooked의 mean/p50/p99 + Δ입니다. 오버헤드의 주범은 `pthread_mutex_lock` + dlsym 간접 호출 + `fprintf(stderr)`이지 쿼터 산술이 아닙니다. 큰 사이즈일수록 상수 오버헤드가 희석됩니다.

---

## 5. 5-A correlation 확장 — 두 시계열을 PID로 조인

isolation은 "격리되나?"에, overhead는 "느린가?"에 답했습니다. 이번엔 "메모리와 컴퓨트를 **동시에** 관측할 수 있나?"입니다. 두 컨테이너가 quota 내에서 **공존**하는 시나리오죠.

### 스텝 7. `test_compute.py` — 메모리 + launch 다발

`test_hold.py`는 sleep만 해서 launch=0입니다. 상관 실험은 메모리도 잡고 **`cudaLaunchKernel`도 많이 발생**시켜야 합니다. 그 빈자리를 채우는 워크로드입니다([test_compute.py:5-10](../../runtime-image-pytorch/test_compute.py#L5)): 큰 텐서 1개 할당(메모리 점유) + 1024×1024 matmul/relu/scale 루프(매 iter ≈ 3회 launch)([test_compute.py:11-16](../../runtime-image-pytorch/test_compute.py#L11)).

루프에서 100회마다 `synchronize()`로 압력을 조절하는 디테일이 있습니다([test_compute.py:81-83](../../runtime-image-pytorch/test_compute.py#L81)) — 안 하면 launch queue가 폭주합니다.

### 스텝 8. `run_correlation.sh` + `_correlate.py` — 조인의 핵심

`run_correlation.sh`는 isolation과 골격이 같지만(nvidia-smi 캡처 → 두 세션 spawn → 폴링), 두 가지가 추가됩니다:

1. **`docker top`으로 컨테이너 PID 캡처**([run_correlation.sh:108-115](../../scripts/eval/run_correlation.sh#L108)). python3이 뜰 시간을 `sleep 2` 준 뒤 각 컨테이너의 PID를 `pids_a.txt`/`pids_b.txt`로 저장. **이 PID가 나중에 조인 키**입니다.
2. **`docker logs --timestamps`로 타임스탬프 붙은 로그 저장**([run_correlation.sh:135-136](../../scripts/eval/run_correlation.sh#L135)). 훅의 `[fgpu] LAUNCH count=N` 라인 앞에 ISO8601 시각이 붙습니다.

그리고 후처리를 `_correlate.py`에 위임합니다([run_correlation.sh:145](../../scripts/eval/run_correlation.sh#L145)). 이 파이썬(stdlib only)이 조인의 심장입니다:

- **launch 카운터** = 훅 stderr의 `[fgpu] LAUNCH count=N` 정규식 추출([_correlate.py:39-41](../../scripts/eval/_correlate.py#L39)). docker 타임스탬프의 나노초를 fromisoformat이 못 받으니 6자리로 자르는 처리까지([_correlate.py:57-61](../../scripts/eval/_correlate.py#L57)).
- **nvidia-smi 메모리** = `YYYY/MM/DD HH:MM:SS.fff` 포맷을 따로 파싱([_correlate.py:69-92](../../scripts/eval/_correlate.py#L69)).
- **조인** = nvidia-smi의 각 행 PID가 어느 컨테이너의 PID set에 속하는지로 분류, 같은 컨테이너의 여러 PID는 메모리 합산([_correlate.py:140-146](../../scripts/eval/_correlate.py#L140)).
- **t=0** = 모든 타임스탬프 중 최소([_correlate.py:129-138](../../scripts/eval/_correlate.py#L129)).

결과는 `correlation.csv`(long format: `t_seconds, container, launch_count, used_memory_mib`)([_correlate.py:164-169](../../scripts/eval/_correlate.py#L164)) — pandas로 pivot해서 바로 그래프를 그릴 수 있는 형태입니다. 백엔드가 `FGPU_LAUNCH_LOG_EVERY`를 컨테이너로 forward하므로, trace가 촘촘하려면 `FGPU_LAUNCH_LOG_EVERY=500 ./scripts/run_backend.sh`로 백엔드를 띄워야 합니다([run_correlation.sh:85-91](../../scripts/eval/run_correlation.sh#L85)).

> **패턴 재사용.** isolation → correlation을 보면, 실험 드라이버가 "nvidia-smi 백그라운드 → spawn → 폴링 → 아티팩트 → 판정/후처리"라는 **공통 골격**을 공유합니다. 새 실험을 만들 땐 이 골격을 복사해 판정/후처리만 바꾸면 됩니다.

---

## 6. Stage 5-B — 빌드 도구 없는 웹 UI

이제 사람이 브라우저로 세션을 만들고 상태를 볼 수 있게 합니다.

### 스텝 9. 왜 React/Vue를 안 쓰는가

의도적으로 **vanilla JS + inline CSS, 빌드 스텝 0개**입니다([index.html:7-8](../../backend/app/static/index.html#L7)). 이유:

- **캡스톤 범위.** 프레임워크는 node/npm/webpack 툴체인을 끌고 옵니다. GPU 서버에 node를 깔고 빌드 파이프라인을 유지하는 건 이 프로토타입의 목적(GPU 공유 메커니즘 증명)과 무관한 부담입니다.
- **에셋이 파일 하나.** UI가 `index.html` 딱 하나라 `StaticFiles` 마운트조차 필요 없습니다. FastAPI가 `FileResponse`로 그냥 던집니다([main.py:88-89](../../backend/app/main.py#L88)):

```python
@app.get("/")
def ui_index() -> FileResponse:
    return FileResponse(STATIC_INDEX)
```

- **의존성 없이 오래 간다.** 빌드가 없으니 몇 년 뒤에도 브라우저에서 그냥 열립니다.

### 스텝 10. 어떻게 짜는가 — 폴링, 토큰, 델리게이션

프레임워크 없이도 필요한 건 다 있습니다:

- **3초 폴링.** `setInterval`로 세션 목록/capacity/로그를 주기 갱신([index.html:467-469](../../backend/app/static/index.html#L467)). WebSocket 스트리밍 같은 건 캡스톤 범위 밖 — 폴링이면 충분합니다.
- **토큰 localStorage.** Stage 9 auth용 API 토큰을 `localStorage`에 저장하고, 모든 fetch에 `Authorization: Bearer <token>`을 붙이는 헬퍼([index.html:156-166](../../backend/app/static/index.html#L156)). `authHeaders()`가 토큰 없으면 빈 객체를 반환해서 auth off일 땐 그냥 안 붙습니다.
- **fetch 헬퍼 3종.** `jget`/`jpost`/`jdelete`([index.html:169-190](../../backend/app/static/index.html#L169))로 에러 처리를 한 곳에 모읍니다. 409(admission denied)는 파싱해서 친절한 힌트로 렌더링([index.html:394-411](../../backend/app/static/index.html#L394)) — 원시 JSON 대신 "force 체크박스를 켜라"는 안내를 보여줍니다.
- **이벤트 델리게이션.** 행마다 리스너를 붙이지 않고 `tbody`에 하나만 붙여 클릭 대상을 판별([index.html:425-457](../../backend/app/static/index.html#L425)). 폴링으로 행이 계속 다시 그려져도 리스너가 새지 않습니다.
- **XSS 방어.** 서버에서 온 문자열은 `escapeHtml`로 이스케이프([index.html:196-199](../../backend/app/static/index.html#L196)) — 프레임워크의 자동 이스케이프가 없으니 손으로 챙깁니다. 이게 vanilla의 대가입니다.

이 UI는 Stage 5-B에서 최소로 시작해 Stage 9(토큰/gpu_index), 10(jupyter 라디오/open 버튼), 11(capacity 라인/force 체크박스), 12(compute_ratio)까지 **한 파일에 점진적으로 얹힌** 결과입니다. 처음부터 다 넣지 마세요 — 5-B는 create 폼 + 세션 테이블 + 로그 pane, 이 세 개면 충분합니다.

### 검증 게이트 (Stage 5-B)

```bash
./scripts/run_backend.sh
# 브라우저: http://localhost:8000/
```

`GET /`가 200 + `text/html`이어야 합니다([CLAUDE.md Stage 5-B 기준](../../CLAUDE.md)). create 폼 제출 → ~1초 내 새 행, `created → running → exited`로 폴링 중 전이, 행 클릭 → 로그 pane에 `[entrypoint]`/`[fgpu]`/`[hold-test]` 라인, stop/delete 동작.

---

## 7. `run_all_tests.sh` — 전부 한 번에

마지막으로, 지금까지 만든 per-stage 검증을 **하나의 오케스트레이터**로 묶습니다. "커밋 전에 이거 한 방으로 전 스테이지 확인"을 위한 도구입니다.

동작 순서([run_all_tests.sh:5-12](../../scripts/run_all_tests.sh#L5)): preflight → 멱등 빌드(있으면 skip) → 백엔드 없는 스테이지들(1, 2, 5-C, 6, 7, 4) → backend pytest → 백엔드 spawn → 3, 5-A, 5-A correlation → 5-D → 정리 → PASS/FAIL 표.

설계 포인트 몇 가지:

- **`set -uo pipefail`이되 `-e`는 안 씁니다**([run_all_tests.sh:25](../../scripts/run_all_tests.sh#L25)). 한 스테이지가 실패해도 멈추지 않고 나머지를 계속 돌려 **최대 정보를 수집**하기 위함입니다. 대신 각 스텝의 결과를 `RESULTS` 배열에 기록했다가 마지막에 표로 출력([run_all_tests.sh:242-260](../../scripts/run_all_tests.sh#L242)).
- **`pattern_check` 헬퍼**([run_all_tests.sh:55-73](../../scripts/run_all_tests.sh#L55)): 명령을 돌려 로그에 특정 regex가 있으면 PASS. 예를 들어 stage2는 `\[fgpu\] DENY`가 로그에 있어야 PASS([run_all_tests.sh:118-120](../../scripts/run_all_tests.sh#L118)).
- **멱등 빌드**([run_all_tests.sh:88-111](../../scripts/run_all_tests.sh#L88)): `build/libfgpu.so`, `fgpu-runtime:stage2`, `fgpu-runtime-pytorch:stage4`가 이미 있으면 빌드 skip. 처음 1회만 ~10분, 이후 ~5분.
- **백엔드 생명주기 관리**([run_all_tests.sh:164-198](../../scripts/run_all_tests.sh#L164)): 백엔드를 백그라운드로 띄우고 `trap ... EXIT`로 반드시 죽입니다. `/healthz`를 최대 30초 폴링해서 준비되면 진행, 안 되면 이후 백엔드 의존 스텝을 전부 SKIP 처리(5-D만 백엔드 없이 별도 실행).
- **아티팩트**: 모든 스텝의 stdout/stderr가 `experiments/runall_<TS>/<step>.log`로 캡처됩니다([run_all_tests.sh:29-30](../../scripts/run_all_tests.sh#L29)). 실패하면 해당 로그를 보면 됩니다. 종료 코드는 FAIL 하나라도 있으면 1([run_all_tests.sh:263-266](../../scripts/run_all_tests.sh#L263)).

### 검증 게이트 (오케스트레이터)

```bash
./scripts/run_all_tests.sh
```

마지막에 PASS/FAIL 표가 뜨고, `experiments/runall_<TS>/`에 스텝별 로그가 남습니다. 전부 PASS면 exit 0.

> **참고: `experiments/`는 커진다.** isolation/overhead/correlation/runall이 매번 새 `<TS>` 디렉토리를 쌓습니다. gitignore 후보이며, 주기적으로 오래된 걸 지우세요.

---

## 함정 모음

- **nvidia-smi 백그라운드 프로세스 누수.** `trap ... EXIT`으로 반드시 kill. 안 그러면 좀비가 GPU를 계속 폴링합니다.
- **세션보다 nvidia-smi를 늦게 시작.** 초기 할당 구간의 trace가 비어 겹침을 못 보여줍니다. 항상 spawn **전에** 캡처를 켜세요.
- **이미지 재빌드 누락(또 나옵니다).** `test_hold.py`/`test_compute.py`/`bench_alloc.cu`는 이미지에 **구워집니다**. 추가/수정하면 재빌드 필수. `run_overhead.sh`의 사전 확인처럼 방어 코드를 넣어두면 좋습니다.
- **correlation에서 `FGPU_LAUNCH_LOG_EVERY` 미설정.** 백엔드 default가 1000이라 짧은 워크로드에선 dump가 1~2줄뿐 → CSV가 거의 빔. 백엔드를 띄울 때 env로 낮춰야 합니다.
- **판정을 단일 신호로.** exit_code만, 혹은 로그만 보면 우연 통과가 생깁니다. isolation처럼 여러 신호를 AND로 묶으세요.
- **UI에서 이스케이프 누락.** 프레임워크가 없으니 서버 문자열은 손으로 `escapeHtml`. 빠뜨리면 XSS.

---

## 완성 체크리스트

- [ ] `test_hold.py` — alloc+hold, 종료 코드(0/1/2)로 결과 전달.
- [ ] `run_isolation.sh` — nvidia-smi 선(先)캡처, 두 세션, 다중 신호 AND 판정, `VERDICT: PASS`.
- [ ] `bench_alloc.cu` — stdout CSV / stderr 로그 분리, 스트리밍 출력.
- [ ] `run_overhead.sh` — baseline/hooked 직접 실행, 파이썬 p50/p99 요약, markdown 표.
- [ ] `test_compute.py` + `run_correlation.sh` + `_correlate.py` — PID join으로 launch↔메모리 병합.
- [ ] `index.html` — vanilla JS, `FileResponse` 서빙, 3초 폴링, 토큰 localStorage, 델리게이션.
- [ ] `run_all_tests.sh` — 멱등 빌드, `-e` 미사용 + RESULTS 표, 백엔드 생명주기, `experiments/runall_<TS>/`.

## 다음 챕터

**5장 — Stage 5-C·6·7·12 (심화 훅 계층)**로 가면, 지금까지 `cudaMalloc`만 잡던 훅에 Driver API(`cuMemAlloc_v2`), VMM API(`cuMemCreate`), launch 모니터링(`cudaLaunchKernel`), 그리고 duty-cycle 컴퓨트 스로틀을 하나씩 얹습니다. 4장에서 만든 평가 인프라(특히 correlation과 `run_all_tests.sh`)가 그 새 계층들을 검증하는 밑바탕이 됩니다.
