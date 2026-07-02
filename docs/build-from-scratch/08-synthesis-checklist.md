# 8장. 종합 — 전체 순서·의존성·함정 체크리스트

> 축하합니다. 여기까지 왔다면 훅부터 스로틀까지 밑바닥부터 다시 지을 수 있는 지도를 손에 쥔 셈입니다. 이 마지막 장은 흩어진 단계들을 **하나의 의존성 그래프와 체크리스트**로 묶어, 실제로 손을 움직일 때 옆에 펼쳐두는 "정비 매뉴얼" 역할을 합니다.

---

## 이 장에서 얻는 것

- 전체 개발 단계의 **의존성 그래프** (무엇이 무엇을 필요로 하는가).
- 처음부터 끝까지의 **한 줄 요약 순서표**.
- 단계마다 반드시 통과해야 할 **검증 게이트 모음**.
- 밑바닥부터 짤 때 **가장 자주 터지는 함정 체크리스트**.
- 다 만든 뒤의 **확장 아이디어**.

---

## 1. 의존성 그래프 — 무엇이 무엇을 필요로 하나

```
                         [개발 환경]
              (드라이버·CUDA·Docker·nvidia-toolkit·Python)
                              │
                              ▼
                  ┌───────────────────────┐
                  │ Stage 1: 최소 훅      │  ← 다른 모든 것의 뿌리
                  │ libfgpu.so + quota    │
                  └───────────┬───────────┘
                              │ (호스트에서 검증됨)
                              ▼
                  ┌───────────────────────┐
                  │ Stage 2·4: 컨테이너화 │  훅 .so 를 마운트해 사용
                  │ + PyTorch 이미지      │
                  └───────────┬───────────┘
                              │ (docker run 으로 수동 검증됨)
                              ▼
                  ┌───────────────────────┐
                  │ Stage 3·8: 백엔드     │  docker run 을 코드로 자동화
                  │ FastAPI+Docker+SQLite │
                  └─────┬─────────────┬───┘
                        │             │
         ┌──────────────▼──┐      ┌───▼───────────────────┐
         │ Stage 5: 평가·UI │     │ Stage 5C·6·7: 훅 확장  │  (훅에만 의존)
         │ (백엔드+훅 필요) │     │ Driver/VMM/launch      │
         └──────────────────┘     └───┬───────────────────┘
                        │             │
                        ▼             ▼
                  ┌───────────────────────┐
                  │ Stage 9·10·11: 운영   │  백엔드 위에 얹힘
                  │ 인증·멀티GPU·Jupyter· │
                  │ 어드미션              │
                  └───────────┬───────────┘
                              ▼
                  ┌───────────────────────┐
                  │ Stage 12: 컴퓨트 스로틀│  훅의 launch 경로(Stage 7)에 의존
                  └───────────────────────┘
```

읽는 법:
- **Stage 1이 뿌리**입니다. 이게 없으면 아무것도 검증할 수 없어요.
- **훅 라인(1 → 5C·6·7 → 12)** 과 **백엔드 라인(3·8 → 9·10·11)** 은 어느 정도 **독립적으로 병렬 진행**할 수 있습니다. 실제로 훅을 확장하는 동안 백엔드 운영 기능을 따로 만들어도 됩니다. 단, 둘 다 Stage 1·2가 끝난 뒤에.
- **Stage 12는 Stage 7(launch 카운터)의 코드 경로를 재활용**합니다. 그래서 7 없이는 12를 못 해요.

---

## 2. 처음부터 끝까지 — 한 줄 요약 순서표

| 순서 | 스테이지 | 만드는 것 | "됐다"의 신호 | 챕터 |
|------|----------|-----------|----------------|------|
| 0 | 준비 | 환경·저장소 골격 | `nvidia-smi`, `docker run --gpus all` 동작 | [0장](00-roadmap-and-setup.md) |
| 1 | Stage 1 | `fgpu_hook.c`(runtime) + `build_hook.sh` + `test_alloc.cu` | 호스트에서 `[fgpu] ALLOW`/`DENY` 출력 | [1장](01-stage1-minimal-hook.md) |
| 2 | Stage 2 | `runtime-image/Dockerfile` + `entrypoint.sh` | 컨테이너 안에서 같은 ALLOW/DENY | [2장](02-stage2-4-container-pytorch.md) |
| 3 | Stage 4 | PyTorch 이미지 + `test_pytorch.py` | `torch.cuda.OutOfMemoryError` 전파 | [2장](02-stage2-4-container-pytorch.md) |
| 4 | Stage 3 | 백엔드 골격 + REST + `docker_manager` | `smoke_test_api.sh` 통과 | [3장](03-stage3-8-backend.md) |
| 5 | Stage 8 | SQLite 영속성 + `asyncio.to_thread` | 재시작 후 세션 살아있음 | [3장](03-stage3-8-backend.md) |
| 6 | Stage 5-A/D | isolation·overhead 하네스 | `experiments/`에 PASS·CSV | [4장](04-stage5-eval-and-ui.md) |
| 7 | Stage 5-B | 웹 UI(`index.html`) | 브라우저에서 세션 생성/조회 | [4장](04-stage5-eval-and-ui.md) |
| 8 | Stage 5-C | Driver API 훅 + 재진입 가드 | driver-only 테스트에서 DENY | [5장](05-stage5c-6-7-hook-expansion.md) |
| 9 | Stage 6 | VMM 훅 | vmm-only 테스트에서 DENY | [5장](05-stage5c-6-7-hook-expansion.md) |
| 10 | Stage 7 | launch 카운터 + atexit 요약 | `total cudaLaunchKernel = N` | [5장](05-stage5c-6-7-hook-expansion.md) |
| 11 | Stage 9 | Bearer 인증 + 멀티 GPU | 401/정상 분기, `--gpus device=N` | [6장](06-stage9-11-ops.md) |
| 12 | Stage 10 | Jupyter 모드 | 브라우저에서 노트북 셀 실행 | [6장](06-stage9-11-ops.md) |
| 13 | Stage 11 | 어드미션 + `asyncio.Lock` | 지분 합 초과 시 409 | [6장](06-stage9-11-ops.md) |
| 14 | Stage 12 | duty-cycle 스로틀 | throughput ∝ compute_ratio | [7장](07-stage12-throttle.md) |

---

## 3. 검증 게이트 모음 (건너뛰지 마세요)

각 단계가 "정말 됐는지"는 반드시 **눈으로** 확인합니다. 이 프로젝트는 로그를 `grep`하는 게 표준 검증법이에요.

- **Stage 1**: `LD_PRELOAD=build/libfgpu.so FGPU_RATIO=0.4 ./build/test_alloc` → stderr에 `[fgpu] init` → `ALLOW`(256MB) → `DENY`(6GB) → `FREE`.
- **Stage 2**: baseline(마운트 없음)엔 `[fgpu]` 로그가 **아예 없고**, hooked엔 있어야 함. 이 대조가 핵심.
- **Stage 4**: hooked·ratio=0.4에서 4GB 할당이 `torch.cuda.OutOfMemoryError`로 튀어야 함. (캐싱 껐는지 확인!)
- **Stage 3·8**: `smoke_test_api.sh`가 create→get→logs→delete 완주. 백엔드 재시작 후에도 `GET /sessions/<id>` 유지.
- **Stage 5-C/6/7**: 각 계층 전용 테스트(`test_driver_alloc`/`test_vmm_alloc`/`test_launch`)가 **다른 계층을 안 건드리고** 자기 계층만으로 DENY/카운트 확인.
- **Stage 11**: 0.7 세션이 있을 때 0.4 요청 → **409**. 동시 POST 0.6 두 개 → 정확히 하나만 201.
- **Stage 12**: throttle ON에서 throughput이 baseline × compute_ratio에 근접.
- **전체**: `run_all_tests.sh` 한 방으로 전 스테이지 PASS 요약.

---

## 4. 가장 자주 터지는 함정 체크리스트

밑바닥부터 짤 때 실제로 겪게 되는 순서대로 정리했습니다.

### 훅 관련
- [ ] **`_GNU_SOURCE`를 `#include`보다 먼저 정의**했나? 안 하면 `RTLD_NEXT undeclared` 컴파일 에러. ([fgpu_hook.c:65-70](../../hook/src/fgpu_hook.c#L65-L70))
- [ ] **quota를 라이브러리 로드 시점에 계산하려다 실패**하지 않았나? `cudaMemGetInfo`는 CUDA 컨텍스트가 생긴 **첫 할당 이후**에나 되므로 lazy 계산해야 함.
- [ ] **여러 계층을 후킹하자 `g_used`가 두 배로** 뛰지 않나? → 재진입 가드(`__thread g_in_hook`)를 **모든 return 경로에서** 리셋했는지 확인. (진입 시 set, unlock 직후 reset)
- [ ] **`cudaFree`에서 size를 못 찾아** `g_used`가 안 줄지 않나? → `track_alloc`/`pop_alloc`로 ptr→size를 기억하는지.
- [ ] **로그를 stdout에 찍어** 사용자 프로그램 출력을 오염시키지 않았나? 모든 `[fgpu]` 로그는 **stderr**로.
- [ ] **정적 링크 바이너리로 테스트**하고 있지 않나? LD_PRELOAD는 정적 링크를 못 잡음. 테스트 바이너리는 `-cudart shared`로 컴파일.

### 컨테이너 관련
- [ ] **호스트 CUDA 버전 ≠ 컨테이너 CUDA 버전**? 메이저 버전을 맞춰야 훅 .so가 컨테이너 `libcudart`에 링크됨.
- [ ] **Dockerfile을 고치고 이미지를 재빌드 안 했나?** 코드는 마운트라 바로 반영되지만, 이미지 안 테스트 바이너리·의존성은 재빌드해야 함.
- [ ] **PyTorch 캐싱을 안 껐나?** `PYTORCH_NO_CUDA_MEMORY_CACHING=1` 없으면 캐싱 앨로케이터가 큰 슬랩 하나만 잡아 per-call 쿼터가 안 보임.

### 백엔드 관련
- [ ] **blocking 호출(docker SDK, sqlite3)을 `to_thread` 없이** 직접 호출하지 않나? 이벤트 루프가 막혀 동시 요청이 직렬화됨.
- [ ] **sqlite3 커넥션을 스레드 간 공유**하지 않나? 매 호출 새 커넥션이 원칙.
- [ ] **어드미션 검사와 spawn 사이에 `asyncio.Lock`이 없나?** 없으면 동시 POST 둘이 같은 용량을 둘 다 통과해 오버서브.
- [ ] **컨테이너 spawn 후 DB insert가 실패**하면? 고아 컨테이너가 남음(알려진 갭 — 롤백 고려).

---

## 5. 다 만든 뒤 — 확장 아이디어

밑바닥부터 완주했다면, 이제 프로젝트를 넘어설 차례입니다. 실제 이 프로토타입이 **의도적으로 남겨둔** 확장 지점들:

- **`cuMemAllocAsync` / `cuMemAllocManaged` 후킹** — 현재 미지원. 스트림 순서 할당·UVM을 쓰는 워크로드까지 덮으려면.
- **컨테이너 spawn 실패 롤백** — DB insert 실패 시 고아 컨테이너 정리(try/finally).
- **GPU 메모리 자동 감지** — `run_all_tests.sh`가 6144MB를 하드코딩. `nvidia-smi`로 총량을 읽어 할당 크기 스케일링.
- **멀티 호스트 스케줄링(Stage 9 full)** — SQLite를 Redis/Postgres로 바꾸고 여러 서버에 걸친 어드미션. `SessionManager` 계약은 그대로 두고 저장소만 교체.
- **실 device-time 측정** — 현재 launch는 "횟수"만 셈. `cudaEvent`로 실제 GPU 시간을 재면 스로틀을 더 정교하게.
- **RBAC/토큰 회전** — 현재는 단일 정적 Bearer 토큰.

각 아이디어는 위 의존성 그래프의 어느 노드에 붙는지 생각하면, 어디를 건드려야 할지 바로 보일 거예요.

---

## 스스로 점검

1. 훅 라인과 백엔드 라인이 왜 어느 정도 병렬로 진행 가능한지, 의존성 그래프로 설명해 보세요.
2. Stage 12를 만들려면 반드시 먼저 있어야 하는 스테이지는 무엇이고 왜인가요?
3. "재진입 가드가 없어서 `g_used`가 두 배"가 되는 상황을 재현하는 시나리오를 말해보세요.

---

**축하합니다! 🎉** 이제 여러분은 fGPU를 **이해**할 뿐 아니라, 빈 디렉토리에서 **다시 지을** 수 있습니다.

**← 이전 챕터** [7장. Stage 12 — 컴퓨트 스로틀](07-stage12-throttle.md) | **처음으로 →** [0장. 개발 로드맵](00-roadmap-and-setup.md)
