# Chapter 15 — 한계와 위협 모델

마지막 챕터입니다. 이 프로젝트가 *무엇을 하지 못하는지* 와 *왜 그게 받아들여지는지* 를 정리합니다. 캡스톤 발표나 논문에서 가장 많이 나오는 질문들이 여기서 나와요.

## 학습 목표

- "협조적 위협 모델(cooperative threat model)" 의 의미를 안다.
- 본질적 한계와 구현상 한계의 차이를 안다.
- 각 한계를 *왜 지금은 받아들이는지* 정당화할 수 있다.
- 후속 연구로 풀 수 있는 한계와 풀 수 없는 한계를 구분한다.

---

## 15.1 위협 모델 — 협조적 사용자

본 프로토타입의 가정:

> *사용자는 quota 를 우회하려 *적극적으로 시도하지 않는다*. 시스템 도구를 *정상적으로* 사용하다 quota 에 걸리면 거부를 받아들인다.*

이게 "**협조적(cooperative) 위협 모델**" 입니다. 반대 개념은 "적대적(adversarial)" — 사용자가 모든 우회 기법을 동원해 quota 를 깨려 함.

### 왜 협조적 모델로 잡았나?

1. **컨슈머 GPU 공유 시나리오의 현실**: 같은 랩 / 같은 회사 안의 동료들이 GPU 를 나눠 쓰는 상황. 의도적 공격 동기가 약함.
2. **적대적 모델의 비용**: 진짜 격리는 MIG (하드웨어 분할) 나 hypervisor 수준의 가상화가 필요. 컨슈머 GPU 에선 불가능 또는 매우 어려움.
3. **본 연구의 가치 명제**: *"단순한 메커니즘으로 *실용적* 인 quota 를 컨슈머 GPU 에서 제공"*.

### 협조적 모델에서 깨지는 시나리오

| 우회 기법 | 우리 hook 의 대응 |
|---|---|
| LD_PRELOAD 환경변수 unset | 우회 가능 — 컨테이너 안 사용자가 `unset LD_PRELOAD` 가능 |
| `dlopen("libcudart.so", RTLD_NOW)` 직접 + `dlsym` | 우회 가능 — 우리 .so 가 검색 범위에서 빠짐 |
| `nvcc -cudart=static` 으로 정적 링크 | 우회 가능 — PLT 자체가 없음 |
| 직접 `ioctl` 로 `/dev/nvidia*` 호출 | 우회 가능 — 사용자 공간 라이브러리 우회 |

이 모든 것이 *기술적으로 가능* 하지만, *정상적 사용자* 는 안 합니다. 협조적 모델 = "안 한다고 가정".

---

## 15.2 본질적 한계 — 구조적, 해결 불가

### (1) SM 격리 불가능

- 메모리는 후크로 막아도, 한 컨테이너가 GPU 100% 점유하면 다른 컨테이너의 latency 영향.
- 진짜 SM 분할은 MIG (A100/H100 만) 또는 MPS (같은 user 안에서만, 멀티 컨테이너 부적합) 가 필요.
- 본 프로토타입은 [Chapter 12](12-launch-monitoring.md) 의 launch counter 로 *측정* 만 함, *시행* 안 함.

**왜 받아들이나**: 컨슈머 GPU 가 SM 분할 하드웨어 지원이 없음. 소프트웨어 hooking 으로 SM 격리는 *근본적으로* 불가능.

### (2) PyTorch caching allocator 의 sub-allocation 불가시성

- [Chapter 10](10-pytorch-caching.md) 에서 본 그대로.
- caching 이 큰 슬랩을 잡으면 그 안의 sub-alloc 은 user-space 자료구조라 어떤 GPU API hook 도 못 봄.

**왜 받아들이나**: 슬랩 단위 quota 만으로도 실용적으로 충분. 텐서 단위 quota 가 필요하면 PyTorch 자체 patch 필요 (별도 프로젝트 규모).

### (3) 컨슈머 GPU 한정

- A100/H100 환경에선 MIG 가 더 강력. 본 프로토타입이 *대체* 가 아니라 *컨슈머 GPU 의 빈틈* 을 메우는 것.
- RTX 4060 같은 8GB 카드에서는 4명 이상 동시 사용 시 메모리 부족이 본질적 제약.

**왜 받아들이나**: 본 프로젝트의 의의 자체가 *컨슈머 GPU 환경* 에 있음. 한계가 아니라 *scope*.

---

## 15.3 구현상 한계 — 해결 / 미해결 현황

| 한계 | 상태 | 해결책 |
|---|---|---|
| Driver-classic API (`cuMemAlloc_v2`) | ✅ Stage 5-C | 후킹 추가 |
| VMM API (`cuMemCreate`) | ✅ Stage 6 | 후킹 추가, 동일 quota state 공유 |
| 백엔드 영속성 | ✅ Stage 8 | SQLite |
| 동시 POST 직렬화 문제 | ✅ Stage 8 | asyncio.to_thread |
| Admission control | ✅ Stage 11 | 순수 함수 모듈 + asyncio.Lock |
| 멀티-GPU device pinning | ✅ Stage 9 minimal | `--gpus device=N` |
| Bearer 인증 | ✅ Stage 9 minimal | static token |
| Jupyter 인터랙티브 | ✅ Stage 10 | bind mount + ephemeral port |
| `cuMemAllocAsync` (CUDA 11.2+) | ❌ 미커버 | Stage 6+ 후속 |
| `cuMemAllocManaged` (UVM) | ❌ 미커버 | demand paging 으로 인해 추가 설계 필요 |
| Driver API `cuLaunchKernel` | ❌ 미커버 | Stage 7+ 후속 |
| 멀티 호스트 스케줄러 | ❌ 미구현 | Stage 9 full (별도 프로젝트 규모) |
| Token 회전 / RBAC / OAuth | ❌ 미구현 | Stage 9 full |
| Idle 세션 자동 정리 | ❌ 미구현 | 사용자 명시 stop/delete 필요 |
| GPU 디바이스 시간 측정 | ❌ 미구현 | `cudaEventRecord` 자동 주입 — 성능 trade-off |
| 실시간 로그 streaming (WebSocket) | ❌ 미구현 | UI 가 polling — 본 프로토타입엔 충분 |

각 미해결 항목이 *논문의 Future Work 섹션* 의 직접 재료입니다.

---

## 15.4 그럼에도 의의 — 무엇이 새로운가

### 학술적 기여

1. **컨슈머 GPU + Docker 환경의 fractional GPU 메커니즘 오픈소스 재현**: Backend.AI 의 fGPU 가 비공개라, 그 핵심 메커니즘인 LD_PRELOAD CUDA hooking 을 *공개적으로* 검증.
2. **세 alloc layer (Runtime / Driver / VMM) 통합 후킹**: 단일 `g_used` state 와 reentrancy guard 로 같은 quota 정책을 *어느 alloc 경로* 에든 적용.
3. **Quantitative overhead 측정**: [Chapter 11](11-benchmarking.md) 의 mean / p50 / p99 표 — hook overhead 가 size 와 무관하게 ~1.5~2 μs 수준 (논문의 핵심 정량 결과).

### 실용적 기여

1. **0 의존성 stdlib 백엔드** — 캡스톤 / 학생 환경에서 부담 없음.
2. **단계별 검증 가능 구조** — 각 stage 가 독립적으로 빌드/실행/검증.
3. **재사용 가능 layer 분리** — admission 은 순수 함수, store 는 인터페이스, hook 은 hookable layer 별 분리. 후속 확장 시 변경 비용 최소.

---

## 15.5 자주 받는 질문 (FAQ)

### Q1. "MIG 가 더 좋은데 왜 이걸 만드나?"

A: MIG 는 A100/H100 에서만 동작. RTX 4060/4070 같은 컨슈머 GPU 는 MIG 미지원. 학생 / 소규모 랩 환경에서 GPU 공유가 필요한데 MIG 가 없는 시나리오를 위함.

### Q2. "PyTorch caching 때문에 quota 가 정확하지 않다며?"

A: *세부 텐서 단위* 가 부정확. *전체 슬랩* 단위는 정확. 그리고 caching 자체가 user-space 자료구조라 어떤 hook 으로도 안 보임. 한계 명시 후 수용.

### Q3. "사용자가 LD_PRELOAD 를 unset 하면 어떻게 되나?"

A: 우회 가능. 본 프로토타입은 협조적 사용자 가정. 적대적 사용자 차단은 별도 보안 layer (커널 모듈, 컨테이너 정책 등) 가 필요한 별개 문제.

### Q4. "왜 NVIDIA 의 MPS 를 안 쓰나?"

A: MPS 는 같은 *user* 의 process 들 사이에서만 동작. 멀티 컨테이너 / 멀티 user 시나리오엔 부적합. 또 MPS 는 메모리 quota 를 직접 제공하지 않음 — 본 프로토타입과 직교.

### Q5. "VMM hook 까지 넣었는데 왜 PyTorch 가 여전히 caching 의 sub-alloc 을 우리에게 안 보여주나?"

A: VMM 은 GPU API layer. caching 은 user-space layer. 둘이 다른 추상화 레벨. caching 이 64MB 풀을 한 번 잡을 때 VMM hook 이 보고, 그 *안에서의* 사용자 텐서 1MB sub-alloc 은 GPU API 호출 자체가 일어나지 않으므로 어떤 hook 도 못 봄.

### Q6. "Backend.AI 와 정확히 뭐가 다른가?"

A: Backend.AI 는 commercial product, 핵심 fGPU 로직 비공개, K8s scheduler 통합, RBAC, 과금. 본 프로토타입은 *오픈소스 PoC*: 핵심 메커니즘 (CUDA hooking) 만 추출해 *학술적으로 검증*. 둘은 비교 대상이 아니라 본 프로젝트가 Backend.AI 의 *핵심 idea* 를 *재현 가능한 형태로* 증명.

### Q7. "캡스톤 / 논문에서 가장 강조할 한 줄은?"

A: *"LD_PRELOAD 기반 CUDA API hooking 만으로 컨슈머 GPU 에서 메모리 quota 와 launch frequency monitoring 이 정량적 overhead ~1.5 μs 수준으로 동작함을 실측 검증"* — 그게 핵심 contribution.

---

## 15.6 그래서 무엇을 *증명했나* — 정리표

| 명제 | 증명 방법 | 어디서 |
|---|---|---|
| LD_PRELOAD 로 cudaMalloc 가로챌 수 있다 | Stage 1 hook stderr 의 ALLOW/DENY | [Chapter 01](01-ld-preload.md), [Chapter 03](03-hook-walkthrough.md) |
| Runtime + Driver + VMM 세 layer 모두 동일 정책으로 잡힌다 | Stage 2 / 5-C / 6 검증 스크립트 | [Chapter 02](02-cuda-api-layers.md) |
| 두 컨테이너의 quota 가 독립이다 | Stage 5-A 격리 실험 | [Chapter 13](13-admission-control.md) 의 시나리오 |
| Hook overhead 가 ~ μs 수준이다 | Stage 5-D 마이크로벤치 | [Chapter 11](11-benchmarking.md) |
| `cudaErrorMemoryAllocation` 이 PyTorch 까지 전파된다 | Stage 4 OOM 실험 | [Chapter 10](10-pytorch-caching.md) |
| launch frequency 측정이 lock-free 로 정확하다 | Stage 7 launch counter | [Chapter 12](12-launch-monitoring.md) |
| 백엔드 재시작 후 세션이 살아남는다 | Stage 8 SQLite | [Chapter 08](08-sqlite-persistence.md) |
| 동시 POST 가 진짜 병렬로 처리된다 | asyncio.to_thread | [Chapter 07](07-async-io.md) |
| Admission gate 가 oversubscription 을 막는다 | Stage 11 + 17개 unit test | [Chapter 13](13-admission-control.md) |
| Jupyter 인터랙티브 세션도 hook 적용 | Stage 10 검증 | [Chapter 14](14-jupyter.md) |

이 표가 곧 캡스톤 슬라이드의 결과 섹션 백본입니다.

---

## 15.7 다음으로 — 개인 학습 가이드

이 교재를 다 보면:
- 각 챕터의 자가점검 질문에 답을 *말로* 할 수 있어야 함.
- 한 컨테이너에서 hook 이 동작하는 흐름을 화이트보드에 그릴 수 있어야 함.
- 본인이 만든 게 *어디까지 동작하고 어디서 깨지는지* 확신 있게 답할 수 있어야 함.

그 다음 단계 (선택):
1. **`cuMemAllocAsync` hook 추가**해보기 — Stage 6+ 미해결 과제. 좋은 졸업 작품 확장.
2. **K8s operator 작성** — Stage 9 full. SessionStore 인터페이스 그대로, store 만 etcd 로.
3. **다른 프레임워크 검증** — JAX / TensorFlow 에서도 hook 이 동작하는지.
4. **`cuLaunchKernel` (driver) 추가** — Stage 7+ 후속.

---

## 자가점검 질문

1. "협조적 위협 모델" 한 줄 정의.
2. 본질적 한계와 구현상 한계의 차이는?
3. 우리가 *증명* 한 것 중 캡스톤 발표에서 가장 강조할 *하나* 를 고른다면?
4. PyTorch caching 의 sub-allocation 을 *완전히* 보려면 어떤 layer 의 변경이 필요한가?
5. RTX 4060 + 사용자 4명 시나리오에서 본 프로토타입의 *실용적* 한계는?

---

## 끝났습니다

여기까지 읽으셨다면 이 프로젝트의 *기술적 표면* 은 완전히 파악하신 거예요. 자가점검 질문에 답하면서 가끔 막히면 해당 챕터로 돌아가세요. 한 번에 다 외울 필요 없고, *어디 가면 답이 있는지* 만 알아도 충분합니다.

발표 준비 시 도움 될 슬라이드 흐름 제안:
1. **문제** — 컨슈머 GPU 공유 시나리오의 빈틈 (1 슬라이드)
2. **해결책 한 줄** — LD_PRELOAD CUDA hooking (1 슬라이드)
3. **아키텍처 그림** — [description.md §3](../../description.md) 의 다이어그램 (1 슬라이드)
4. **세 alloc layer** — [Chapter 02](02-cuda-api-layers.md) 의 표 (1 슬라이드)
5. **검증 결과** — [Chapter 11](11-benchmarking.md) overhead 표 + [Chapter 13](13-admission-control.md) 의 격리 그래프 (2 슬라이드)
6. **한계와 future work** — [§15.3](#153-구현상-한계--해결--미해결-현황) (1 슬라이드)

[목차로 돌아가기](README.md)
