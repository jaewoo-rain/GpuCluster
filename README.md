# fGPU

> **GPU 한 장을 여러 사람이 비율대로 나눠 쓰는 시스템.** Backend.AI의 fractional GPU(fGPU) 핵심 메커니즘을 작은 코드로 재현한 프로토타입.

---

## 한 줄 소개

NVIDIA GPU 1장에서 여러 Docker 컨테이너가 동시에 돌고, 각 컨테이너는 사전에 지정한 메모리 비율(예: `0.4`, `0.6`)을 넘는 GPU 메모리를 할당하지 못하도록 강제한다. `LD_PRELOAD`로 컨테이너 안에 작은 C 라이브러리(`libfgpu.so`)를 주입해서 `cudaMalloc` 같은 CUDA API 호출을 가로채는 방식.

각 사용자는 브라우저에서 자기 **Jupyter Lab 세션**을 열어 PyTorch 코드를 작성할 수 있다.

```
사용자 A (브라우저)        사용자 B (브라우저)
    │  ratio 0.4              │  ratio 0.6
    ▼                         ▼
  Jupyter Lab               Jupyter Lab
  (4.6 GB까지 허용)          (7.2 GB까지 허용)
    │                         │
    └────────┬────────────────┘
             ▼
       NVIDIA GPU (12 GB)
```

---

## 왜 만들었나

- 딥러닝용 GPU는 비싸지만 작은 모델은 GPU 전체 메모리를 다 안 씀.
- 한 GPU를 여러 사용자가 나눠 쓰면 활용률이 높아짐.
- 단, 한 사용자의 메모리 사용이 다른 사용자를 망가뜨리지 않게 **격리**가 필요함.
- 데이터센터 GPU(A100 등)는 하드웨어 격리(MIG)를 지원하지만 **consumer GPU (RTX 4070 등)는 미지원**.
- 그래서 **소프트웨어로** quota를 강제하는 방식이 필요 → 이 프로젝트.

---

## 핵심 메커니즘 — 3단어로

1. **Docker** — 사용자마다 격리된 컨테이너
2. **LD_PRELOAD** — 컨테이너 시작 시 우리 라이브러리를 PyTorch보다 먼저 로드
3. **API 가로채기 (hook)** — `cudaMalloc` 호출을 우리 코드가 먼저 받아 quota 검사

```
PyTorch 가 cudaMalloc(2GB) 호출
   │
   ├─→ libfgpu.so 의 cudaMalloc 가 먼저 받음    ← LD_PRELOAD 효과
   │     │
   │     ├─ used + 2GB ≤ quota?   → 진짜 cudaMalloc 호출 → ALLOW
   │     └─ used + 2GB >  quota?  → cudaErrorMemoryAllocation 반환 → DENY
   │
   └─→ PyTorch 는 진짜 GPU 메모리 부족인 줄 알고 OutOfMemoryError
```

---

## 무엇이 들어있나

| 컴포넌트 | 무엇 | 위치 |
|---|---|---|
| **Hook** (`libfgpu.so`) | 컨테이너 안에서 CUDA API 가로채는 C 라이브러리 (~300줄) | [hook/](hook/) |
| **베이스 이미지** | CUDA devel + 후크 검증 binary | [runtime-image/](runtime-image/) |
| **PyTorch 이미지** | 위 + PyTorch + JupyterLab | [runtime-image-pytorch/](runtime-image-pytorch/) |
| **백엔드** | FastAPI 세션 매니저 | [backend/](backend/) |
| **UI** | 단일 HTML (vanilla JS, build step 없음) | [backend/app/static/index.html](backend/app/static/index.html) |
| **빌드 / 검증 스크립트** | 단계별 PASS/FAIL 검증 | [scripts/](scripts/) |
| **구조 설명서** | 파일/디렉토리 책임 매트릭스 | [ARCHITECTURE.md](ARCHITECTURE.md) |
| **학습 문서 (교과서)** | 완전 초보용 온보딩·구현 교과서 | [docs/](docs/README.md) |

---

## 📚 학습 문서 (교과서)

코드를 보기 전에, 이 교과서로 개념부터 잡는 걸 추천합니다. **C·쉘·Docker 를 처음 접하는 완전 초보**도 읽을 수 있도록 비유·그림·단계별로 썼습니다.

- **[온보딩 교과서](docs/onboarding/README.md)** — 이 프로젝트가 *무엇이고 왜* 이렇게 생겼는지 0부터 (0~10장)
- **[밑바닥부터 만들기](docs/build-from-scratch/README.md)** — 빈 디렉토리에서 *어떻게* 다시 구현하는지 (0~8장)
- **[심화 학습 자료](docs/study/README.md)** — 전문 챕터 모음

MkDocs(Material) 로 검색·다크모드가 되는 **예쁜 사이트로도** 볼 수 있어요:

```bash
pip install mkdocs-material            # 최초 1회
python -m mkdocs serve -a 127.0.0.1:8080   # → http://127.0.0.1:8080/
```

문서 실행·작성법 자세히 → **[docs/README.md](docs/README.md)**

---

## 빠른 시작

### 사전 조건
- Ubuntu 22.04 (네이티브 또는 WSL2)
- NVIDIA 드라이버 535+ + nvidia-container-toolkit
- CUDA 12.x (host)
- Docker 24+
- Python 3.11+

### 빌드 + 실행
```bash
chmod +x scripts/*.sh scripts/eval/*.sh runtime-image/entrypoint.sh


chmod +x scripts/*.sh runtime-image/entrypoint.sh runtime-image-pytorch/*.sh 2>/dev/null
./scripts/build_hook.sh            # libfgpu.so
./scripts/build_image.sh           # fgpu-runtime:stage2 (베이스)
./scripts/build_pytorch_image.sh   # fgpu-runtime-pytorch:stage4 (주피터)
./scripts/run_backend.sh           # :8000
# http://localhost:8000/ → mode=jupyter


# 1) 한 번만 빌드 (PyTorch 이미지 첫 빌드는 5~10분)
./scripts/build_hook.sh                 # → build/libfgpu.so
./scripts/build_image.sh                # → fgpu-runtime:stage2
./scripts/build_pytorch_image.sh        # → fgpu-runtime-pytorch:stage4

# 2) 백엔드 실행 (foreground, Ctrl+C 로 중단)
./scripts/run_backend.sh                # uvicorn http://0.0.0.0:8000

# 3) 브라우저
#    http://localhost:8000/
#    (외부 접속 시  http://<호스트 IP>:8000/)
```

---

## UI 사용법

브라우저에 접속하면 다음 화면이 뜸:

1. **Create** 폼에서 mode 선택
   - **batch** — 명령 1회 실행 후 종료 (테스트/벤치마크)
   - **jupyter** — 브라우저에 Jupyter Lab 띄움 (인터랙티브 코딩)
2. **ratio** (0 < r ≤ 1) 입력 — 메모리 비율
3. **create** 클릭

`jupyter` 모드면 새 탭에 Jupyter Lab이 자동으로 뜸. 거기서 PyTorch 코드 작성:

```python
import torch
x = torch.empty(5*1024*1024*1024 // 4, dtype=torch.float32, device='cuda')
# ratio=0.4 세션에선 OutOfMemoryError
# ratio=0.7 세션에선 통과
```

세션을 두 개 동시에 띄워서 같은 코드를 양쪽에서 돌려보면 quota가 다르게 작동하는 게 보임.

---

## 데모 시나리오 — A/B 비교

1. ratio `0.4` jupyter 세션 생성 → 자동으로 새 탭 열림
2. ratio `0.7` jupyter 세션 생성 → 또 다른 새 탭
3. 양쪽 노트북에 같은 코드 입력:

```python
import os, torch
print("FGPU_RATIO =", os.environ.get("FGPU_RATIO"))
free, total = torch.cuda.mem_get_info()
print(f"quota = {total * float(os.environ['FGPU_RATIO']) / 1024**3:.2f} GiB")

# 5 GiB 텐서 시도
x = torch.empty(5*1024**3 // 4, dtype=torch.float32, device='cuda')
print("OK")
```

→ ratio 0.4 세션에선 `OutOfMemoryError`, ratio 0.7 세션에선 `OK`.

UI 의 Logs 패널에서 다음과 같은 hook 로그를 직접 확인:

```
[fgpu] init: ratio=0.400 quota_bytes=0
[fgpu] quota lazily 계산: ratio=0.400 * total=12426543104 = 4970617241 bytes
[fgpu] DENY  cudaMalloc size=5368709120 used=0 quota=4970617241    ← 차단
```

### 실제 LLM 공유 실험 (fractional 공유 이득 측정)

위 데모를 한 단계 확장해, **실제 소형 LLM(Qwen2-0.5B) 추론**을 두 Jupyter 세션
(예: ratio `0.4` / `0.6`)에서 돌려 **단독 vs 순차(A→B) vs 동시(A+B 공유)** 의
makespan·tokens/sec·GPU util 을 노트북 안 파이썬으로 측정하고 공유 이득(speedup)을 비교한다.

이미지 리빌드·백엔드 수정 없이(노트북 첫 셀에서 런타임 `pip install`) 바로 실행 가능.

→ 실행 가이드: **[notebooks/README.md](notebooks/README.md)** (세션 생성 → 노트북 업로드 →
시나리오별 실행 → 종합 분석). 노트북: [`notebooks/fgpu_infer.ipynb`](notebooks/fgpu_infer.ipynb),
[`notebooks/fgpu_analysis.ipynb`](notebooks/fgpu_analysis.ipynb).

---

## 주요 기능

### Quota enforcement (Stage 1, 5-C, 6)
4가지 CUDA API 표면을 hook함:
- **Runtime API**: `cudaMalloc` / `cudaFree` (PyTorch 등이 가장 자주 쓰는 path)
- **Driver API**: `cuMemAlloc_v2` / `cuMemFree_v2`
- **VMM API**: `cuMemCreate` / `cuMemRelease`
- **Launch counter**: `cudaLaunchKernel` (모니터링만, enforcement X)

### Jupyter Lab 인터랙티브 모드 (Stage 10)
- 세션마다 자체 Jupyter Lab 서버
- 호스트 ephemeral port에 publish (32768~)
- 세션마다 랜덤 토큰 발급 (`secrets.token_urlsafe(24)`)
- 노트북 파일은 호스트의 `data/sessions/<id>/` 에 영속화 — 컨테이너 삭제해도 보존

### Admission control (Stage 11)
- 새 세션 생성 시 `sum(ratios) ≤ 1.0` 검사
- 초과 시 HTTP 409 반환, `force=true` 옵션으로 우회 가능 (oversubscription 데모용)
- `asyncio.Lock`으로 check-then-spawn 직렬화 → 동시 요청 race 방지
- `GET /sessions/admission` 으로 GPU 별 capacity 조회

### 영속성 (Stage 8)
- 세션 record 는 SQLite (`data/sessions.db`)
- 백엔드 재시작해도 세션 record 유지, docker daemon 과 자동 reconcile

### 인증 (Stage 9 minimal)
- `FGPU_API_TOKEN` 환경변수 설정 시 Bearer 토큰 인증 활성화
- 미설정 시 인증 비활성 (개발 편의)

### 멀티 GPU 지원
- `gpu_index` 필드로 특정 GPU device 핀 가능
- 코드는 멀티 GPU 호환, 단 1-GPU 호스트(RTX 4070)에선 의미 없음

---

## 동작 검증

```bash
# 단위 테스트 (docker / GPU 불필요)
cd backend && pip install -e ".[dev]" && pytest -q
# → 25 passed (Stage 8 store + Stage 11 admission)

# 통합 테스트 (docker + GPU 필요)
./scripts/eval/run_jupyter.sh           # Stage 10 Jupyter mode E2E
./scripts/eval/run_admission.sh         # Stage 11 admission E2E + concurrency
./scripts/eval/run_isolation.sh         # Stage 5-A 두 컨테이너 격리
./scripts/eval/run_overhead.sh          # Stage 5-D hook 오버헤드 마이크로벤치

# 전체 stage 자동 검증 (한 번에)
./scripts/run_all_tests.sh
```

각 검증 스크립트는 `experiments/<name>_<timestamp>/summary.txt` 에 PASS/FAIL 판정 + 원시 데이터 저장.

---

## 외부 접속 (LAN)

호스트의 다른 컴퓨터에서 접속하려면:

```bash
# 1) 방화벽 열기
sudo ufw allow 8000/tcp                         # 백엔드
sudo ufw allow 32768:60999/tcp                  # Jupyter ephemeral 포트 범위

# 2) 인증 켜는 것 권장 (LAN 노출이면 필수)
FGPU_API_TOKEN=$(openssl rand -hex 16) ./scripts/run_backend.sh
# 출력된 토큰 메모, UI 상단 토큰 칸에 입력 후 save
```

다른 PC 브라우저: `http://<호스트 IP>:8000/`

UI 의 jupyter URL 은 `location.hostname` 기반으로 자동 조립되므로 외부 접속도 자연스럽게 작동.

---

## 한계 (의도된 것)

| 한계 | 영향 | 대응 |
|---|---|---|
| **SM/compute 격리 없음** | 한 컨테이너가 GPU 100% 차지 못 막음 | MIG 필요 (consumer GPU 미지원), MPS 는 격리 모델 깨짐 |
| **정적 링크 binary 우회** | `nvcc -cudart=static` 컴파일된 코드는 hook 우회 | "Cooperative threat model" — 문서화된 한계 |
| **`cudaMallocAsync` / UVM 미훅** | stream-ordered allocator 사용 시 우회 | 표면 추가 가능, 현재 미구현 |
| **CUDA 컨텍스트 ~150 MiB 오버헤드** | 세션마다 quota 외 추가 점유 | driver 내부 할당이라 hook 못 봄 |
| **단일 호스트** | 컴퓨터 1대만 | Backend.AI 처럼 multi-host 가려면 manager-agent 분리 필요 |
| **인증 단순함** | 정적 토큰 1개, RBAC 없음 | OAuth + 사용자 그룹 추가 가능 (stage 9 full 의 영역) |

---

## Backend.AI 와의 관계

이 프로젝트는 [Backend.AI](https://www.backend.ai/) 의 fGPU 기능을 **개념적으로 재현**한 것이다.

**같은 부분**:
- LD_PRELOAD CUDA hook
- 컨테이너 단위 격리
- ratio 기반 메모리 분할
- cooperative threat model

**Backend.AI 가 추가로 갖춘 것** (이 프로젝트엔 없음):
- 멀티 호스트 cluster scheduler (Sokovan)
- 분산 메타데이터 (PostgreSQL + Redis)
- vfolders (분산 스토리지 가상화)
- 사용자/그룹/도메인 RBAC
- 다중 GPU 벤더 (NVIDIA / AMD / Intel / Habana)
- 큐레이션된 이미지 카탈로그
- Grafana / Prometheus 통합
- JupyterHub-style 게이트웨이
- `cudaMallocAsync`, `cuMemAllocAsync`, UVM 등 추가 API hook

**즉**: enforcement 메커니즘은 동일하지만 인프라 규모가 다르다. 이 프로토타입은 **fGPU 의 본질적 메커니즘을 작은 코드로 시연**하는 것이 목적.

---

## 폴더 구조

```
fgpu/
├── README.md                 ← 이 파일
├── ARCHITECTURE.md           파일 / 디렉토리 책임 매트릭스 + 데이터 흐름
├── description.md            장문 설계 의도 (Korean)
├── CLAUDE.md                 AI agent 작업 가이드
├── LINUX_SETUP.md            fresh 머신 세팅 runbook
│
├── hook/                     LD_PRELOAD 후크 (C)
├── runtime-image/            베이스 컨테이너 (CUDA devel)
├── runtime-image-pytorch/    PyTorch + JupyterLab 변형
├── backend/                  FastAPI 세션 매니저
├── scripts/                  빌드 + 실행 + 검증 드라이버
│
├── build/                    (gitignored) libfgpu.so 등 빌드 산출물
├── data/                     (gitignored) SQLite + jupyter 워크스페이스
└── experiments/              (gitignored) 검증 스크립트 산출물
```

자세한 구조 설명은 [ARCHITECTURE.md](ARCHITECTURE.md).

---

## 환경변수 reference

| 이름 | 기본값 | 용도 |
|---|---|---|
| `FGPU_API_TOKEN` | `` (비활성) | Bearer 토큰 인증 |
| `FGPU_RUNTIME_IMAGE` | `fgpu-runtime:stage2` | 기본 docker 이미지 |
| `FGPU_HOST_HOOK_PATH` | `<repo>/build/libfgpu.so` | host 의 hook .so 경로 |
| `FGPU_DB_PATH` | `<repo>/data/sessions.db` | SQLite 경로 |
| `FGPU_WORKSPACE_ROOT` | `<repo>/data/sessions` | jupyter 워크스페이스 root |
| `FGPU_PUBLIC_HOST` | `localhost` | jupyter URL 의 호스트명 (UI 는 `location.hostname` 우선) |
| `FGPU_BACKEND_PORT` | `8000` | uvicorn 포트 |
| `FGPU_BACKEND_HOST` | `0.0.0.0` | uvicorn bind 주소 |
| `FGPU_LAUNCH_LOG_EVERY` | `1000` | hook 의 launch counter dump 주기 (0 = off) |
| `PYTORCH_NO_CUDA_MEMORY_CACHING` | `1` (이미지 default) | PyTorch caching off (hook quota 정확성) |

---

## 라이선스

MIT — see [LICENSE](LICENSE). © 2026 양재우 (Jaewoo Yang).
