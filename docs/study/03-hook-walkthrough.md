# Chapter 03 — `fgpu_hook.c` 라인별 해부

이 챕터는 [hook/src/fgpu_hook.c](../../hook/src/fgpu_hook.c) 한 파일을 처음부터 끝까지 읽는 챕터입니다. 코드 옆에 한국어 주석이 이미 풍부하게 들어가 있으니, 이 문서는 *왜 그렇게 짰는지* 와 *어떤 함정을 피하려 했는지* 를 보충합니다.

## 학습 목표

- 파일을 8 개의 논리 블록으로 나눠 각 블록의 역할을 한 줄로 설명한다.
- 사용자가 `cudaMalloc(&p, N)` 을 부른 순간부터 진짜 함수가 호출되기까지의 흐름을 시퀀스로 그릴 수 있다.
- "왜 lazy quota 계산이 첫 `cudaMalloc` 안에서 일어나야 하는가" 를 답할 수 있다.

준비물: 옆에 [fgpu_hook.c](../../hook/src/fgpu_hook.c) 를 열어놓고 같이 읽기.

---

## 3.1 파일의 8 블록

```
(0) 헤더 + 매크로 (line 1-77)
       _GNU_SOURCE 정의, include
(1) 함수 포인터 선언 (line 80-110)
       real_cudaMalloc / cuMemAlloc_v2 / cuMemCreate / cudaLaunchKernel
(2) 전역 quota state (line 113-128)
       g_lock, g_used, g_quota, g_ratio, g_inited
(2-bis) Reentrancy guard (line 130-151)
       __thread g_in_hook
(2-ter) Launch counter state (line 154-182)
       g_launch_count, g_launch_log_every, atexit dump
(3) 추적 리스트 (line 185-241)
       alloc_entry_t, track_alloc, pop_alloc
(4) 초기화 루틴 (line 244-353)
       fgpu_init_locked, compute_quota_if_needed_locked
(5) Runtime hook (line 356-451)
       cudaMalloc, cudaFree
(7) Driver-classic hook (line 454-553)
       cuMemAlloc_v2, cuMemFree_v2
(7-bis) VMM hook (line 556-659)
       cuMemCreate, cuMemRelease
(8) Launch hook (line 662-721)
       cudaLaunchKernel
```

---

## 3.2 블록 (1): 함수 포인터들

[fgpu_hook.c:91-110](../../hook/src/fgpu_hook.c#L91-L110)

```c
static cudaError_t (*real_cudaMalloc)(void **, size_t) = NULL;
static cudaError_t (*real_cudaFree)(void *)            = NULL;
static CUresult    (*real_cuMemAlloc_v2)(CUdeviceptr *, size_t) = NULL;
... (5개 더)
```

읽는 법: "*static 한* `real_cudaMalloc` 은, `void **` 와 `size_t` 를 받아 `cudaError_t` 를 반환하는 *함수의 주소* 를 담는 변수다. 초기값 NULL."

3 가지 작은 결정:

- **`static`**: 이 변수가 *이 .c 파일 안에서만* 보이게 한다. 다른 .so/실행파일에 같은 이름이 있어도 충돌 X.
- **NULL 초기값**: lazy resolution. 처음 hook 진입 시 `dlsym` 으로 채움.
- **시그니처가 헤더와 정확히 일치해야 함**. 한 글자라도 어긋나면 호출 시 ABI 가 깨져 segfault.

---

## 3.3 블록 (2): 전역 state — 한 자물쇠로 모두 보호

[fgpu_hook.c:124-128](../../hook/src/fgpu_hook.c#L124-L128)

```c
static pthread_mutex_t g_lock = PTHREAD_MUTEX_INITIALIZER;
static size_t g_used   = 0;
static size_t g_quota  = 0;
static double g_ratio  = 1.0;
static int    g_inited = 0;
```

설계 결정: **하나의 `g_lock` 으로 모든 alloc bookkeeping** 보호. 이유:

- 프로토타입에서 동시에 살아있는 alloc 이 보통 수십~수백 개라 lock contention 이 작음.
- 여러 lock 으로 나누면 deadlock 회피 순서를 신경 써야 해서 코드가 복잡해짐.
- lock 비용은 실제로 [Stage 5-D 벤치](11-benchmarking.md) 가 측정해줌 — 지나치게 크면 그때 가서 분할.

`PTHREAD_MUTEX_INITIALIZER` 매크로는 정적 초기화. 동적으로 `pthread_mutex_init` 부를 필요 없음. ([POSIX threads — pthread_mutex_init(3)](https://man7.org/linux/man-pages/man3/pthread_mutex_init.3p.html))

---

## 3.4 블록 (3): 추적 리스트 — `void* → size_t` 매핑

[fgpu_hook.c:197-241](../../hook/src/fgpu_hook.c#L197-L241)

문제: 사용자가 `cudaFree(p)` 를 부를 때 우리는 *그 ptr 이 몇 바이트였는지* 를 알아야 `g_used` 를 정확히 줄임. CUDA 가 이 정보를 안 알려주므로 자체적으로 매핑을 들고 있어야 함.

자료구조: 단일 연결 리스트. `track_alloc` (push, O(1)), `pop_alloc` (find+remove, O(N)).

이중 포인터 트릭 — `pop_alloc` 의 `alloc_entry_t **cur = &g_allocs;` 패턴은 head 가 제거 대상일 때도 분기 없이 우아하게 처리합니다 ([:229](../../hook/src/fgpu_hook.c#L229)). Linus Torvalds 가 좋아하는 그 트릭이에요. 한 번 그림 그려보면 명확해집니다.

> 왜 hash map 안 쓰나? — 동시 alloc 수가 작은 프로토타입 단계에선 linked list 가 단순하고 충분. 평가에서 overhead 가 visible 해지면 그때 교체. *이른 최적화 지양* 의 좋은 예.

### 더 공부하려면
- Linus 의 ["good taste" 영상](https://github.com/mkirchner/linked-list-good-taste) — 같은 이중포인터 패턴 설명

---

## 3.5 블록 (4): 초기화 — 두 단계 lazy

[fgpu_hook.c:266-353](../../hook/src/fgpu_hook.c#L266-L353)

두 함수가 분리돼 있어요:

```
fgpu_init_locked()                  ← dlsym 으로 진짜 함수 주소 가져오기
                                       env (FGPU_RATIO, FGPU_QUOTA_BYTES) 읽기
                                       atexit 등록 — 한 번만

compute_quota_if_needed_locked()    ← cudaMemGetInfo 로 GPU 전체 메모리
                                       알아낸 뒤 ratio 곱해서 quota 계산
                                       — 한 번만 (g_quota == 0 이면)
```

### 왜 두 단계로 나눴나?

`cudaMemGetInfo` 는 **CUDA 컨텍스트가 만들어진 뒤에야** 동작합니다. 그게 보장되는 가장 빠른 시점이 사용자가 처음 `cudaMalloc` 을 부른 순간이에요. 라이브러리 로드 시점에 부르면 "no CUDA-capable device" 같은 엉뚱한 에러가 나옵니다.

따라서:
- `fgpu_init_locked` 은 *언제 불러도 안전한* 작업만 (env 읽기, dlsym).
- `compute_quota_if_needed_locked` 은 cudart 가 살아있어야 하는 작업.

[fgpu_hook.c:387](../../hook/src/fgpu_hook.c#L387) 에서 두 함수를 *순서대로* 호출.

### `_locked` 접미사 컨벤션

함수 이름 끝에 `_locked` 가 붙으면 "**호출자가 g_lock 을 *이미* 잡고 있어야 한다**" 는 표시입니다. 안 잡고 부르면 race condition. 이걸 어기지 않도록 코드 검토 시 항상 확인하세요. 이 컨벤션은 Linux 커널에서도 흔히 쓰는 관례입니다.

### 환경변수 읽기

```c
const char *ratio_env = getenv("FGPU_RATIO");
g_ratio = ratio_env ? atof(ratio_env) : 1.0;
if (g_ratio <= 0.0 || g_ratio > 1.0) g_ratio = 1.0;
```

방어 코드: 사용자가 `FGPU_RATIO=-0.5` 같은 이상한 값을 넣어도 1.0 으로 무효화. *알 수 없는 입력은 안전한 기본값으로* 패턴.

`FGPU_QUOTA_BYTES` 가 명시되면 ratio 보다 우선 — 디버깅/테스트의 escape hatch.

### 더 공부하려면
- `man 3 getenv`
- [GNU libc — Environment Access](https://www.gnu.org/software/libc/manual/html_node/Environment-Access.html)

---

## 3.6 블록 (5): `cudaMalloc` 후킹의 흐름

[fgpu_hook.c:376-415](../../hook/src/fgpu_hook.c#L376-L415)

사용자가 `cudaMalloc(&p, 256MiB)` 를 부르는 순간 일어나는 일:

```
1. g_in_hook 검사 — 이미 hook 안인가? (다른 layer 에서 재진입)
   YES → 그냥 진짜 함수 위임하고 빠진다 (bookkeeping skip)

2. g_in_hook = 1   (이제 우리가 hook 안)

3. pthread_mutex_lock(&g_lock)

4. fgpu_init_locked()  → dlsym 들 채움 (NULL 인 것만)
                       → 환경변수 읽기 (g_inited 면 skip)
5. compute_quota_if_needed_locked()  → cudaMemGetInfo, g_quota 계산

6. quota 검사: g_used + size > g_quota?
   YES → [fgpu] DENY 로그 + return cudaErrorMemoryAllocation
         + lock 풀고 g_in_hook = 0

7. NO → 진짜 cudaMalloc 호출
   성공 → g_used += size
        → track_alloc(*devPtr, size) 추적 리스트 등록
        → [fgpu] ALLOW 로그
   실패 → [fgpu] FAIL 로그 (g_used 안 건드림)

8. lock 풀고 g_in_hook = 0
9. cudaError_t 반환
```

### 왜 quota 검사를 *먼저* 하고 진짜 cudaMalloc 은 *뒤* 인가?

이게 hooking 의 본질입니다. 이미 GPU 에 메모리를 잡은 *뒤* 에 quota 초과를 발견하면 바로 free 해야 하는 race window 가 생기고, 그 사이 다른 스레드가 우리가 막 잡은 영역을 쓰려 들 수도 있어요. **할당 *전* 에 거절** 하는 게 안전.

### `cudaErrorMemoryAllocation` 이 *왜* OOM 처럼 보이는가

값 = 2. 이는 cudart 가 GPU 가 정말로 메모리 부족일 때 돌려주는 그 코드입니다. 우리가 같은 코드를 돌려주면 PyTorch 의 `CUDACachingAllocator` 가 "아, GPU OOM 이구나" 하고 자기 catch 흐름으로 들어가, 결국 `torch.cuda.OutOfMemoryError` 로 사용자까지 propagate. ([Stage 4 의 검증 시나리오](../../CLAUDE.md))

### `cudaFree` 흐름

[fgpu_hook.c:429-451](../../hook/src/fgpu_hook.c#L429-L451)

대칭적이지만 한 가지 다름: `cudaFree` 는 *항상* 진짜 함수를 부릅니다. 추적 리스트에 없는 ptr 도 있을 수 있어서 (hook 이 늦게 붙어 빠진 ptr) 거기서 거부하면 사용자 코드가 깨질 수 있어요. 진짜 cudart 의 판단을 신뢰.

성공이면 `pop_alloc` 으로 size 회수해 `g_used -= freed`.

### `cudaFree(NULL)` no-op

CUDA 표준상 `cudaFree(NULL)` 은 성공 + 아무것도 안 함. 우리 hook 도 같은 동작 (`devPtr != NULL` 검사로 추적 갱신 skip).

---

## 3.7 블록 (7), (7-bis): Driver-classic 와 VMM hook

[fgpu_hook.c:475-553](../../hook/src/fgpu_hook.c#L475-L553) 와 [:580-659](../../hook/src/fgpu_hook.c#L580-L659).

Runtime 과 *거의 동형* 이지만 세 가지 차이:

1. 반환 타입 `CUresult`, OOM 코드는 `CUDA_ERROR_OUT_OF_MEMORY` (= 2 가 아님 — 다른 enum 이지만 의미는 같음).
2. 포인터 타입 `CUdeviceptr` / `CUmemGenericAllocationHandle` → `void *` 캐스트.
3. `real_cuMemAlloc_v2 == NULL` 인 케이스를 명시 처리 — libcuda 가 사용자 프로세스에 안 로드돼 있는 경우.

VMM hook 의 흥미로운 결정: **`cuMemAddressReserve` / `cuMemMap` 등은 후킹 안 함**. 물리 메모리 변화가 없는 *순수 가상 메모리* 작업이라 우리 quota 와 무관. [Chapter 02](02-cuda-api-layers.md) 에서 다룬 그 결정.

---

## 3.8 블록 (8): Launch counter — quota 시행 *없음*

[fgpu_hook.c:684-721](../../hook/src/fgpu_hook.c#L684-L721)

```c
cudaError_t cudaLaunchKernel(...) {
    if (g_in_hook) return real_cudaLaunchKernel(...);
    g_in_hook = 1;
    size_t count = __atomic_add_fetch(&g_launch_count, 1, __ATOMIC_RELAXED);
    cudaError_t err = real_cudaLaunchKernel(...);
    g_in_hook = 0;
    if (g_launch_log_every > 0 && (count % g_launch_log_every) == 0)
        fprintf(stderr, "[fgpu] LAUNCH count=%zu (every %u)\n", count, g_launch_log_every);
    return err;
}
```

차이점:
- **mutex 안 잡음**. PyTorch 가 launch 를 초당 수천 번 부르므로 lock 이 hot path 가 됨. lock-free atomic 으로 충분.
- **quota 검사 안 함**. SM 격리는 hook 영역 밖 (MIG/MPS 가 필요). 단순 *측정* 만.
- **periodic dump + atexit 최종 dump**. 둘 다 stderr 로.

자세한 건 [Chapter 12](12-launch-monitoring.md).

---

## 3.9 직접 해보기 — 로그 패턴 읽기

```bash
./scripts/build_hook.sh
./scripts/run_test.sh
```

`hooked` 실행의 stderr 에서 다음 시퀀스를 찾으세요:

```
[fgpu] init: ratio=0.400 quota_bytes=0 (0 = lazy 계산)
[fgpu] init: real cudaMalloc=0x...
[fgpu] quota lazily 계산: ratio=0.400 * total=8589934592 = 3435973836 bytes
[fgpu] ALLOW cudaMalloc ptr=0x... size=268435456 used=268435456/3435973836
[fgpu] DENY  cudaMalloc size=6442450944 used=268435456 quota=3435973836
[fgpu] FREE  ptr=0x... size=268435456 used=0/3435973836
[fgpu] exit summary: total cudaLaunchKernel = 0
```

각 라인을 [fgpu_hook.c](../../hook/src/fgpu_hook.c) 의 어느 `fprintf` 가 만들었는지 한 번씩 매칭해보세요. 그게 본 챕터의 졸업 시험입니다.

---

## 자가점검 질문

1. `_locked` 접미사가 붙은 함수의 호출 규칙은?
2. `compute_quota_if_needed_locked` 가 *왜* 라이브러리 로드 시점이 아니라 첫 cudaMalloc 안에서 호출되는가?
3. `cudaFree` 는 quota 검사를 하지 않는다. 왜?
4. `g_in_hook` 이 1 인 상태에서 사용자 코드의 `cudaMalloc` 이 호출되면 어떤 일이 벌어지는가? (정답: bookkeeping skip + 진짜 함수 위임 — 하지만 사용자 호출이 *외부에서* 들어왔다면 g_in_hook 은 0 일 것이므로 이 케이스는 hook 내부 재진입에만 해당)
5. 추적 리스트에 없는 ptr 을 `cudaFree` 했을 때 hook 의 동작은?

→ [Chapter 04: 스레드 안전성](04-thread-safety.md)
