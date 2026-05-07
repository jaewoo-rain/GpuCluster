# fGPU 프로토타입 — 구조 설명서

> [`README.md`](README.md)는 *어떻게 쓰는지*, [`description.md`](description.md)는 *왜 이렇게 만들었는지*, [`CLAUDE.md`](CLAUDE.md)는 *AI agent 작업 가이드*. 본 문서는 **"파일/디렉토리가 어떻게 묶여 있고 각자 무슨 역할인지"**에 집중한 빠른 reference.

## 1. 한 줄 정의

NVIDIA RTX 4070(또는 유사 컨슈머 GPU) 하나를 여러 Docker 컨테이너가 `ratio=0.4`/`0.6` 식으로 메모리만 나눠 쓰는 **cooperative fractional GPU 프로토타입**. 메커니즘은 `LD_PRELOAD`로 `libfgpu.so`를 컨테이너에 주입해서 `cudaMalloc` 등의 CUDA API 호출 시점에 per-container quota 검사를 거치게 함. Backend.AI fGPU의 enforcement layer를 최소 코드로 재현.

## 2. 데이터 흐름 (한 요청의 생애)

```
사용자 (브라우저 / curl)
   │ POST /sessions {ratio: 0.4, mode: "jupyter"}
   ▼
FastAPI 백엔드 (backend/app/main.py, :8000)
   │  1. _require_auth (Stage 9 — Bearer 토큰)
   │  2. SessionManager.create()  ←── asyncio.Lock
   │       a. admission.check()  (Stage 11 — sum(ratios) ≤ 1)
   │       b. (jupyter 모드면) 워크스페이스 디렉토리 + 토큰 생성
   │       c. DockerManager.create_container()
   │       d. ephemeral host port read-back
   │       e. SessionStore.insert()  ──→ data/sessions.db
   ▼
Docker daemon
   │  docker run --gpus all
   │     -v build/libfgpu.so:/opt/fgpu/libfgpu.so:ro      ← hook 주입
   │     -v data/sessions/<id>:/workspace                  ← jupyter 노트북 영속
   │     -e LD_PRELOAD=/opt/fgpu/libfgpu.so
   │     -e FGPU_RATIO=0.4
   │     -p <ephemeral>:8888                               ← jupyter 모드만
   │     fgpu-runtime-pytorch:stage4
   │     jupyter lab ... --ServerApp.token=<랜덤>
   ▼
컨테이너 안 jupyter 커널 (PyTorch 임포트 시점)
   │  LD_PRELOAD으로 libfgpu.so 가 먼저 로드됨
   │  내부의 dlsym으로 진짜 cudaMalloc 주소 캐시
   │  사용자 셀에서 torch.empty(...) → cudaMalloc → hook 가로챔
   │     ├─ used + size ≤ quota  → 진짜 cudaMalloc 호출 → ALLOW 로그
   │     └─ 초과                 → cudaErrorMemoryAllocation 반환 → DENY 로그
   ▼
NVIDIA driver → GPU
```

## 3. 디렉토리 트리

```
fgpu/
├── README.md                 외부용 quick start + 빌드 절차
├── description.md            장문 설계 의도 (Korean) — 캡스톤 paper 1차 원천
├── CLAUDE.md                 AI agent 작업 가이드 (stage별 acceptance criteria)
├── ARCHITECTURE.md           ← 이 파일
├── LINUX_SETUP.md            fresh 머신 세팅 runbook
├── LICENSE                   MIT
├── .gitignore                build/, data/, experiments/, __pycache__ 등
│
├── hook/                     ★ LD_PRELOAD 후크 (C, 컨테이너에 주입됨)
│   ├── src/fgpu_hook.c       단일 파일 hook 구현 — 4개 layer 후킹
│   └── tests/                각 hook layer 검증용 standalone CUDA 바이너리
│       ├── test_alloc.cu         Stage 1 — Runtime API (cudaMalloc/Free)
│       ├── test_driver_alloc.cu  Stage 5-C — Driver API (cuMemAlloc_v2)
│       ├── test_vmm_alloc.cu     Stage 6 — VMM API (cuMemCreate)
│       ├── test_launch.cu        Stage 7 — cudaLaunchKernel counter
│       └── bench_alloc.cu        Stage 5-D — overhead microbench
│
├── runtime-image/            ★ 베이스 컨테이너 (CUDA devel + 후크 binaries)
│   ├── Dockerfile            FROM nvidia/cuda:12.4.1-devel-ubuntu22.04
│   └── entrypoint.sh         FGPU env / hook .so 검증 후 exec "$@"
│
├── runtime-image-pytorch/    ★ PyTorch + JupyterLab 변형
│   ├── Dockerfile            FROM fgpu-runtime:stage2 + torch + jupyterlab
│   ├── test_pytorch.py       256 MiB / 6 GiB 두 번 할당 (OOM 시연)
│   ├── test_hold.py          ALLOC_MIB MiB 잡고 HOLD_SEC 초 hold (격리 실험)
│   └── test_compute.py       matmul 루프 (launch counter + 메모리 시계열)
│
├── backend/                  ★ FastAPI 세션 매니저
│   ├── pyproject.toml        deps: fastapi, uvicorn, docker, pydantic-settings
│   ├── app/
│   │   ├── main.py           앱 팩토리 + /healthz + / (UI) + sessions router 와이어링
│   │   ├── core/config.py    FGPU_* env 자동 인식
│   │   ├── api/sessions.py   REST 라우트 (POST/GET/DELETE) + Bearer auth dependency
│   │   ├── schemas/session.py  Session, SessionCreate, SessionLogs Pydantic 모델
│   │   ├── services/
│   │   │   ├── docker_manager.py   Docker SDK 래퍼 (--gpus, hook .so 마운트, 포트)
│   │   │   ├── session_manager.py  라이프사이클 + asyncio.to_thread
│   │   │   ├── session_store.py    SQLite CRUD (Stage 8)
│   │   │   └── admission.py        Stage 11 — sum(ratios) ≤ 1 정책
│   │   └── static/index.html       단일 파일 UI (vanilla JS)
│   └── tests/
│       ├── test_session_store.py   Stage 8 SQLite 단위 테스트 (8개)
│       └── test_admission.py       Stage 11 admission 단위 테스트 (17개)
│
├── scripts/                  ★ 빌드 + 실행 + 검증 드라이버
│   ├── build_hook.sh             gcc -shared -fPIC → build/libfgpu.so
│   ├── build_image.sh            docker build → fgpu-runtime:stage2
│   ├── build_pytorch_image.sh    docker build → fgpu-runtime-pytorch:stage4
│   ├── run_backend.sh            venv + pip install -e + uvicorn :8000
│   ├── smoke_test_api.sh         curl POST/GET/logs/DELETE 라운드트립
│   ├── run_all_tests.sh          오케스트레이터 — 모든 stage PASS/FAIL summary
│   ├── run_test.sh               Stage 1 호스트 검증
│   ├── run_in_container.sh       Stage 2 컨테이너 검증
│   ├── run_driver_in_container.sh    Stage 5-C
│   ├── run_vmm_in_container.sh       Stage 6
│   ├── run_launch_in_container.sh    Stage 7
│   ├── run_pytorch_in_container.sh   Stage 4
│   └── eval/                 ★ 논문 figure / table 생성 스크립트
│       ├── run_isolation.sh        Stage 5-A — 두 컨테이너 격리
│       ├── run_overhead.sh         Stage 5-D — cudaMalloc 오버헤드 마이크로벤치
│       ├── run_correlation.sh      5-A 확장 — 메모리+launch 시계열
│       ├── _correlate.py           위 스크립트의 post-process helper
│       ├── run_jupyter.sh          Stage 10 — jupyter mode E2E
│       └── run_admission.sh        Stage 11 — admission control E2E + concurrency
│
├── build/                    (gitignored) 빌드 산출물
│   ├── libfgpu.so                  ← LD_PRELOAD으로 컨테이너에 마운트되는 .so
│   └── test_alloc                  Stage 1 호스트 검증용 임시 binary
│
├── data/                     (gitignored) 영속 상태
│   ├── sessions.db                 SQLite — 모든 세션 record (Stage 8)
│   └── sessions/<sid>/             Stage 10 — jupyter 노트북 워크스페이스
│
└── experiments/              (gitignored) eval 스크립트의 출력 (figure 원본)
    └── <name>_<TS>/                각 실험마다 timestamp 디렉토리
```

## 4. 컴포넌트 책임 매트릭스

| 컴포넌트 | 책임 | 책임 *아님* |
|---|---|---|
| **`hook/src/fgpu_hook.c`** | per-container quota arithmetic, cudaMalloc/Free 가로채기 | 스케줄링, 사용자 인증, cross-container 통신 |
| **`runtime-image/`** | hook이 주입될 *기반* 환경 | hook 자체를 굽지 않음 (mount-in 시점 결정) |
| **`runtime-image-pytorch/`** | PyTorch + Jupyter 사용 시나리오 | hook 동작 — 위와 동일 |
| **`backend/api/`** | HTTP 인터페이스 + Bearer auth | 비즈니스 로직 — `services/`로 위임 |
| **`backend/services/session_manager.py`** | 컨테이너 라이프사이클, store 호출, admission 호출 | docker SDK 직접 호출 — `docker_manager`로 위임 |
| **`backend/services/admission.py`** | sum(ratios) ≤ 1 정책 (순수 함수) | docker / DB 접근 (호출자가 sessions 넘겨줌) |
| **`backend/services/session_store.py`** | SQLite CRUD | reconciliation — `session_manager`가 docker daemon에 물어봄 |
| **`scripts/eval/`** | 논문 figure 생성 (PASS/FAIL 판정) | 운영 코드 (`scripts/run_*`와 분리) |

## 5. Stage 진행 흐름

각 stage는 독립 검증 가능. 빌드 / 코드는 누적.

| Stage | 산출물 | 검증 스크립트 |
|---|---|---|
| 1 | Runtime API hook (`cudaMalloc`/`Free`) | `scripts/run_test.sh` |
| 2 | 컨테이너 안 검증 | `scripts/run_in_container.sh` |
| 3 | FastAPI `/sessions` REST | `scripts/smoke_test_api.sh` |
| 4 | PyTorch 통합 | `scripts/run_pytorch_in_container.sh` |
| 5-A | 두 컨테이너 격리 | `scripts/eval/run_isolation.sh` |
| 5-B | 최소 Web UI | `backend/app/static/index.html` |
| 5-C | Driver API hook | `scripts/run_driver_in_container.sh` |
| 5-D | hook 오버헤드 마이크로벤치 | `scripts/eval/run_overhead.sh` |
| 6 | VMM API hook | `scripts/run_vmm_in_container.sh` |
| 7 | `cudaLaunchKernel` counter | `scripts/run_launch_in_container.sh` |
| 8 | SQLite 영속 + asyncio | `pytest backend/tests/test_session_store.py` |
| 9 (min) | Bearer auth + multi-GPU pin | (수동 curl) |
| 10 | Jupyter Lab 인터랙티브 모드 | `scripts/eval/run_jupyter.sh` |
| 11 | admission control (sum ≤ 1) | `scripts/eval/run_admission.sh` + `pytest backend/tests/test_admission.py` |

## 6. 두 가지 enforcement layer (Stage 11 추가의 핵심)

```
                         사용자 요청
                              │
                              ▼
              ┌─────────────────────────────────┐
              │ Layer A: Admission control       │  ← Stage 11
              │ (SessionManager._create_locked)  │     spawn 시점에
              │ "sum(ratios) ≤ 1 인가?"          │     체크
              │  실패 → 409 admission_denied     │
              │  통과 → 컨테이너 spawn ────┐    │
              └─────────────────────────────│────┘
                                            │
                                            ▼
              ┌─────────────────────────────────┐
              │ Layer B: Per-container hook      │  ← Stage 1/5-C/6
              │ (libfgpu.so in container)        │     cudaMalloc
              │ "used + size ≤ quota 인가?"      │     호출 시점에
              │  실패 → cudaErrorMemoryAlloc...  │     체크
              │  통과 → 진짜 driver 호출 ───┐   │
              └─────────────────────────────│────┘
                                            │
                                            ▼
                                       NVIDIA driver
                                       (Layer C — physical OOM 가능)
```

| Layer | 시점 | 무엇을 catch | bypass 가능? |
|---|---|---|---|
| A — admission | 컨테이너 spawn 직전 | "이 ratio면 capacity 깨짐" | `force=true` |
| B — hook | 컨테이너 안 cudaMalloc 호출 | "이 컨테이너의 누적이 quota 초과" | 정적 링크 / `cuMemAllocAsync` |
| C — driver | 진짜 GPU 메모리 부족 | "물리적으로 메모리 없음" | 우회 불가 (real ground truth) |

**A + B + C가 모두 같은 `cudaErrorMemoryAllocation` / Python `OutOfMemoryError`로 사용자에게 보임**. paper figure에 이 layer 다이어그램을 넣으면 Backend.AI 같은 production system의 enforcement model이 한 눈에 들어옴.

## 7. 주요 환경변수 / 설정

| 이름 | 위치 | 용도 |
|---|---|---|
| `FGPU_RATIO` | container env | hook의 quota = ratio × total. 0.0 < r ≤ 1.0 |
| `FGPU_QUOTA_BYTES` | container env (선택) | 절대 quota. 설정 시 ratio 무시 |
| `FGPU_LAUNCH_LOG_EVERY` | backend env → container env | Stage 7 launch counter dump 주기 |
| `LD_PRELOAD` | container env | `/opt/fgpu/libfgpu.so` (자동) |
| `FGPU_API_TOKEN` | backend env | Stage 9 — Bearer 인증 활성화 |
| `FGPU_RUNTIME_IMAGE` | backend env | 기본 docker 이미지 태그 |
| `FGPU_HOST_HOOK_PATH` | backend env | host의 `libfgpu.so` 위치 |
| `FGPU_DB_PATH` | backend env | SQLite 파일 경로 |
| `FGPU_WORKSPACE_ROOT` | backend env | jupyter 노트북 호스트 디렉토리 root |
| `FGPU_PUBLIC_HOST` | backend env | jupyter URL의 호스트명 (UI는 location.hostname 우선) |
| `PYTORCH_NO_CUDA_MEMORY_CACHING` | container env | PyTorch caching off (이미지 default `1`) |

## 8. 의도된 한계 (paper에 limitation 섹션)

1. **SM-level isolation 없음** — RTX 4070은 MIG 미지원, MPS는 multi-tenant 격리 모델 깨짐
2. **정적 링크 우회** — `nvcc -cudart=static` 빌드된 binary는 LD_PRELOAD 우회 (Stage 1/2 `test_alloc`이 그 예)
3. **PyTorch caching allocator masking** — `PYTORCH_NO_CUDA_MEMORY_CACHING=1` 필요
4. **`cuMemAllocAsync`/`cuMemAllocManaged` 미훅** — Stage 6+ 미완
5. **CUDA context 오버헤드 (~150 MiB/세션) quota 외 추가 점유** — driver 내부 할당이라 hook 못 봄
6. **단일 호스트** — multi-host 스케줄링은 Stage 9 full
7. **인증 단순함** — Bearer 정적 토큰 1개, 사용자 그룹 / RBAC 없음

## 9. Quick Start (5분 안에 동작 확인)

```bash
chmod +x scripts/*.sh scripts/eval/*.sh runtime-image/entrypoint.sh

# 1) 전체 빌드 + 모든 stage 자동 검증
./scripts/run_all_tests.sh

# 또는 daily 사용:
./scripts/build_hook.sh
./scripts/build_image.sh
./scripts/build_pytorch_image.sh
./scripts/run_backend.sh                 # 다른 터미널에서
                                          # 브라우저: http://localhost:8000/

# Stage 별 자동 검증 (논문 데이터)
./scripts/eval/run_isolation.sh          # → experiments/isolation_<TS>/
./scripts/eval/run_overhead.sh           # → experiments/overhead_<TS>/
./scripts/eval/run_correlation.sh        # → experiments/correlation_<TS>/
./scripts/eval/run_jupyter.sh            # → experiments/jupyter_<TS>/
./scripts/eval/run_admission.sh          # → experiments/admission_<TS>/

# 단위 테스트 (docker / GPU 불필요)
cd backend && pytest -q                   # 25 passed (8 store + 17 admission)
```
