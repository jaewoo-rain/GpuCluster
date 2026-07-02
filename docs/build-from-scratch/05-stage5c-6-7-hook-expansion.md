# 5장. Stage 5-C·6·7 — 훅을 여러 계층으로 확장

## 이 장에서 만들 것

- Stage 1 의 Runtime 훅(`cudaMalloc`) 옆에 **Driver API 훅**(`cuMemAlloc_v2`/`cuMemFree_v2`)을 나란히 붙입니다. 반환 타입이 `CUresult` 로 바뀌는 것 말고는 거의 동형입니다.
- Driver 훅을 붙이는 순간 **처음으로 이중 카운트 버그가 터집니다.** 여기서 재진입 가드(`__thread g_in_hook`)를 도입합니다 — 이 장의 하이라이트입니다.
- **VMM API 훅**(`cuMemCreate`/`cuMemRelease`)을 추가하되, 왜 VA 예약/매핑은 건드리지 않는지 이해합니다.
- **`cudaLaunchKernel` 카운터**를 lock-free `__atomic` 으로 얹고, `atexit` 요약을 붙입니다.
- 각 계층을 `test_driver_alloc.cu` / `test_vmm_alloc.cu` / `test_launch.cu` 로 **격리 검증**하는 개발 방식을 익힙니다.

> 전제: 1장의 Runtime 훅이 이미 완성되어 있고 게이트 6을 통과했습니다.

---

## 개발 순서 체크리스트

1. Driver 훅을 Runtime 훅 복붙 → 타입만 고쳐 붙인다 (가드는 **아직 안 넣음**)
2. `test_driver_alloc.cu` 로 실행 → **이중 카운트가 관측되는 걸 일부러 본다**
3. 재진입 가드(`__thread g_in_hook`)를 도입하고, **모든** 훅의 **모든 return 경로**에 일관되게 적용
4. 다시 실행 → 이중 카운트가 사라진 걸 확인
5. VMM 훅(`cuMemCreate`/`cuMemRelease`) 추가 + `test_vmm_alloc.cu`
6. `cudaLaunchKernel` 카운터(`__atomic`) + `FGPU_LAUNCH_LOG_EVERY` + `atexit` 요약 + `test_launch.cu`

---

## 스텝 1 — Driver 훅을 "가드 없이" 먼저 붙인다

Driver API 는 libcuda 의 저수준 할당 경로입니다. Runtime 훅과 세 가지가 다릅니다:

- 반환 타입이 `cudaError_t` 가 아니라 **`CUresult`** (둘 다 enum 이지만 별개).
- 포인터가 `void*` 가 아니라 **`CUdeviceptr`** (= `unsigned long long`).
- 심볼 이름에 **`_v2`** 접미사가 붙습니다. `cuMemAlloc`(no `_v2`)은 `cuda.h` 안에서 매크로로 `_v2` 로 redirect 되므로, `dlsym` 으로 잡을 때는 `_v2` 를 명시해야 합니다.

함수 포인터부터 추가합니다([fgpu_hook.c:100](../../hook/src/fgpu_hook.c#L100)):

```c
#include <cuda.h>      /* CUresult, CUdeviceptr, CUDA_SUCCESS */
#include <stdint.h>    /* uintptr_t — CUdeviceptr ↔ void* 캐스트용 */

static CUresult (*real_cuMemAlloc_v2)(CUdeviceptr *, size_t) = NULL;
static CUresult (*real_cuMemFree_v2)(CUdeviceptr)            = NULL;
```

그리고 `fgpu_init` 의 dlsym 재시도 목록에 두 줄을 더합니다:

```c
    if (!real_cuMemAlloc_v2) real_cuMemAlloc_v2 = dlsym(RTLD_NEXT, "cuMemAlloc_v2");
    if (!real_cuMemFree_v2)  real_cuMemFree_v2  = dlsym(RTLD_NEXT, "cuMemFree_v2");
```

이제 Runtime 훅을 그대로 복붙해서 타입만 바꿉니다. **일부러 재진입 가드 없이** 짭니다 — 문제를 눈으로 보기 위해서입니다:

```c
CUresult cuMemAlloc_v2(CUdeviceptr *dptr, size_t bytesize) {
    pthread_mutex_lock(&g_lock);
    fgpu_init_locked();
    compute_quota_if_needed_locked();

    if (g_quota > 0 && g_used + bytesize > g_quota) {
        fprintf(stderr, "[fgpu] DENY  cuMemAlloc_v2 size=%zu used=%zu quota=%zu\n",
                bytesize, g_used, g_quota);
        pthread_mutex_unlock(&g_lock);
        return CUDA_ERROR_OUT_OF_MEMORY;
    }

    CUresult err = real_cuMemAlloc_v2(dptr, bytesize);
    if (err == CUDA_SUCCESS) {
        g_used += bytesize;
        /* CUdeviceptr(64-bit 정수) → void* 캐스트. x86_64 Linux 가정. */
        track_alloc((void *)(uintptr_t)(*dptr), bytesize);
        fprintf(stderr, "[fgpu] ALLOW cuMemAlloc_v2 ptr=0x%llx size=%zu used=%zu/%zu\n",
                (unsigned long long)(*dptr), bytesize, g_used, g_quota);
    }
    pthread_mutex_unlock(&g_lock);
    return err;
}
```

> **왜 `(void *)(uintptr_t)(*dptr)` 캐스트인가?** `CUdeviceptr` 은 64-bit 정수이고, x86_64 Linux 에서 `void*` 도 64-bit 입니다. 같은 너비라 `uintptr_t` 를 거쳐 안전하게 `g_allocs` 리스트의 `void*` 슬롯에 끼워넣을 수 있습니다. 이 판단의 근거는 [fgpu_hook.c:572](../../hook/src/fgpu_hook.c#L572) 주석에 있습니다. (다른 ABI 였다면 위험할 수 있는 트릭입니다.)

> 이 시점에서 `pthread_mutex_t g_lock` 과 `_locked` 접미사 규칙을 도입합니다. Stage 1 을 단일 스레드로 짰다면, 여기서 `g_lock` 을 추가하고 `fgpu_init`/`compute_quota_if_needed` 를 `_locked` 버전으로 바꾸세요. `_locked` 는 "호출자가 이미 `g_lock` 을 잡고 있다" 는 뜻이라 함수 안에서 다시 잠그면 안 됩니다.

---

## 스텝 2 — 이중 카운트를 "일부러" 본다

이제 `cuMemAlloc_v2` 만 쓰는 테스트를 돌려봅니다. 완성본 [test_driver_alloc.cu](../../hook/tests/test_driver_alloc.cu) 는 Runtime API(`cudaMalloc`)를 **일부러 안 부릅니다** — 오직 Driver 계층만으로 quota 가 강제되는지 깨끗하게 보기 위해서입니다([test_driver_alloc.cu:4](../../hook/tests/test_driver_alloc.cu#L4)).

핵심은 이겁니다. 어떤 CUDA 배포/드라이버에서는 **libcudart 의 `cudaMalloc` 이 내부적으로 libcuda 의 `cuMemAlloc_v2` 를 호출**합니다. 그러면:

1. 사용자가 `cudaMalloc(&p, N)` 을 한 번 부른다.
2. **우리** `cudaMalloc` 이 불려서 `g_used += N`, `track_alloc` 한다.
3. 우리가 위임한 진짜 `cudaMalloc` 이 내부에서 `cuMemAlloc_v2` 를 부른다.
4. **우리** `cuMemAlloc_v2` 가 또 불려서 `g_used += N`, `track_alloc` 을 **또** 한다.

사용자는 한 번 할당했는데 `g_used` 는 **두 배**로 뜁니다. 로그에 `ALLOW cudaMalloc` 바로 뒤에 `ALLOW cuMemAlloc_v2` 가 같은 크기로 한 번 더 찍히고, `used` 가 2N 이 되는 걸 보게 됩니다. 더 나쁘게는, 우리가 이미 `g_lock` 을 잡은 채 진짜 함수를 불렀는데 그 안에서 우리 훅이 또 `pthread_mutex_lock(&g_lock)` 을 시도하면 **데드락**입니다.

> **여기서 실행해서 확인하세요 (진단 게이트)**
> `test_driver_alloc` 만 얹은 실행이면 순수 Driver 경로라 이중 카운트가 안 보일 수 있습니다. 이중 카운트를 재현하려면 **Runtime 테스트**(`test_alloc`)를 얹어, `cudaMalloc` 한 번에 `ALLOW` 가 두 줄(runtime + driver) 찍히거나 데드락으로 멈추는 걸 관찰하세요. 이게 "왜 가드가 필요한가" 의 실물 증거입니다. 배포판에 따라 안 터질 수도 있지만, **터질 수 있는 구조 자체가 버그**이므로 가드는 필수입니다.

---

## 스텝 3 — 재진입 가드 도입 (이 장의 하이라이트)

해결책은 스레드마다 "지금 내가 이미 훅 안에 있는가" 를 기억하는 플래그입니다. thread-local 로 두면 lock 없이도 스레드 간 간섭이 없습니다.

```c
/* 각 스레드가 자기만의 사본을 갖는 플래그. 0 = 외부 진입, 1 = 이미 훅 안. */
static __thread int g_in_hook = 0;
```

[fgpu_hook.c:154](../../hook/src/fgpu_hook.c#L154) 에 정의되어 있고, 설계 의도는 [fgpu_hook.c:133](../../hook/src/fgpu_hook.c#L133) 주석 블록에 상세히 적혀 있습니다.

**모든** 훅에 **똑같은 패턴**으로 적용합니다:

```c
cudaError_t cudaMalloc(void **devPtr, size_t size) {
    /* 이미 다른 훅 안에서 재진입 → bookkeeping skip, 위임만. */
    if (g_in_hook) {
        return real_cudaMalloc ? real_cudaMalloc(devPtr, size)
                               : cudaErrorInitializationError;
    }
    g_in_hook = 1;                    /* ← 진입 즉시 set */
    pthread_mutex_lock(&g_lock);
    /* ... 본 로직 (init, quota, track) ... */
    pthread_mutex_unlock(&g_lock);
    g_in_hook = 0;                    /* ← 나가기 전 reset */
    return err;
}
```

이 패턴에서 **절대 틀리면 안 되는 규칙**:

> **모든 return 경로에서 `g_in_hook` 을 0 으로 되돌려야 한다.** 하나라도 빠뜨리면, 그 스레드는 이후 모든 훅 호출에서 `if (g_in_hook)` 에 걸려 영영 bookkeeping 을 skip 합니다 — 그 스레드에 한해 훅이 조용히 죽습니다. 디버깅하기 최악인 버그입니다.

`cudaMalloc` 한 함수 안에 return 경로가 **세 개**나 됩니다. 완성본에서 각각을 세어 보세요:

- 재진입 early return — [fgpu_hook.c:445](../../hook/src/fgpu_hook.c#L445) (가드 set 전이라 reset 불필요)
- DENY 경로 — [fgpu_hook.c:459](../../hook/src/fgpu_hook.c#L459)~460 (unlock **다음** reset)
- 정상 경로 — [fgpu_hook.c:477](../../hook/src/fgpu_hook.c#L477)~478

`cuMemAlloc_v2` 는 여기에 "심볼 미해결" 경로까지 있어 return 이 **네 개**입니다 — [fgpu_hook.c:550](../../hook/src/fgpu_hook.c#L550) 부근. 네 곳 모두 `pthread_mutex_unlock` + `g_in_hook = 0` 을 짝지어 두었는지 확인하세요.

이제 재진입 시 흐름은:

1. 사용자 `cudaMalloc` → `g_in_hook==0` → set 1 → 정상 bookkeeping → 진짜 `cudaMalloc` 위임.
2. 진짜가 내부에서 `cuMemAlloc_v2` 호출 → **우리** `cuMemAlloc_v2` 진입 → `g_in_hook==1` → **위임만** 하고 카운트/락 skip.
3. 돌아와서 `g_in_hook = 0`.

한 번의 사용자 할당 = 한 번의 카운트. 데드락도 사라집니다(재진입이 lock 을 다시 안 잡으니까).

> **여기서 실행해서 확인하세요 (게이트 A)**
> 스텝 2에서 이중 `ALLOW` 나 데드락을 봤다면, 가드 도입 후 다시 돌려서 **`cudaMalloc` 한 번에 `ALLOW` 한 줄**, `used` 가 정확히 할당 크기만큼만 오르는 걸 확인하세요. `test_driver_alloc` 도 돌려서 `[fgpu] init: real cuMemAlloc_v2=0x...`(non-NULL) + 256 MiB `ALLOW` + 6 GiB `DENY`(호출자는 `result=2 CUDA_ERROR_OUT_OF_MEMORY`)를 확인합니다.

`cuMemFree_v2` 도 같은 가드 패턴 + `pop_alloc((void*)(uintptr_t)dptr)` 로 대칭적으로 만듭니다([fgpu_hook.c:588](../../hook/src/fgpu_hook.c#L588)).

---

## 스텝 4 — VMM 훅: 물리 할당 시점만

VMM(Virtual Memory Management) API 는 CUDA 10.2+ 의 modern 할당 경로입니다. 핵심 차이는 **할당이 여러 단계로 쪼개진다** 는 점입니다:

- `cuMemCreate` — **물리** 메모리만 잡고 handle 반환.
- `cuMemAddressReserve` — 가상 주소(VA) 범위 예약. **물리 변화 없음.**
- `cuMemMap` — handle 을 VA 에 바인딩. **물리 변화 없음.**
- `cuMemRelease` — 물리 메모리 해제.

그래서 **quota 부과/회수는 `cuMemCreate` / `cuMemRelease` 두 시점에만** 겁니다. VA 예약/매핑은 후킹하지 않습니다 — 물리량이 안 바뀌는데 후킹하면 같은 handle 을 여러 VA 에 매핑할 때 이중 카운트만 유발합니다. 이 결정의 근거는 [fgpu_hook.c:621](../../hook/src/fgpu_hook.c#L621) 블록에 상세합니다.

```c
static CUresult (*real_cuMemCreate)(CUmemGenericAllocationHandle *, size_t,
                                    const CUmemAllocationProp *,
                                    unsigned long long) = NULL;
static CUresult (*real_cuMemRelease)(CUmemGenericAllocationHandle) = NULL;
```

훅 본체는 `cuMemAlloc_v2` 와 완전히 동형입니다 — 가드, lock, quota 검사, `track_alloc`. 추적 키는 `handle`(역시 `unsigned long long`)을 `(void*)(uintptr_t)` 로 캐스트해 같은 `g_allocs` 리스트에 넣습니다([fgpu_hook.c:679](../../hook/src/fgpu_hook.c#L679)). 완성본 `cuMemCreate` 는 [fgpu_hook.c:645](../../hook/src/fgpu_hook.c#L645), `cuMemRelease` 는 [fgpu_hook.c:693](../../hook/src/fgpu_hook.c#L693).

> **의도적으로 안 하는 것:** `cuMemAllocAsync`(stream-ordered), `cuMemAllocManaged`(UVM)는 별개 경로라 여전히 미후킹입니다. 이건 버그가 아니라 문서화된 범위 밖 항목입니다.

검증은 [test_vmm_alloc.cu](../../hook/tests/test_vmm_alloc.cu) 로. 이 테스트는 `cuMemAddressReserve`/`cuMemMap` 을 **일부러 안 부릅니다**([test_vmm_alloc.cu:4](../../hook/tests/test_vmm_alloc.cu#L4)) — 물리 alloc 만으로 ALLOW/DENY 가 검증돼야 하기 때문입니다. VMM 은 size 가 granularity 배수여야 해서 `cuMemGetAllocationGranularity` 로 round up 하는 부분([test_vmm_alloc.cu:77](../../hook/tests/test_vmm_alloc.cu#L77))만 Driver 테스트와 다릅니다.

> **여기서 실행해서 확인하세요 (게이트 B)**
> `test_vmm_alloc` 을 `FGPU_RATIO=0.4` 로 얹으면:
> - `[fgpu] init: ... cuMemCreate=0x... cuMemRelease=0x...` (non-NULL)
> - 256 MiB `ALLOW cuMemCreate`
> - 6 GiB `DENY  cuMemCreate` + 호출자 `result=2`

---

## 스텝 5 — `cudaLaunchKernel` 카운터: lock-free

지금까지는 메모리였고, 이제 **컴퓨트 활동을 관찰**합니다. `cudaLaunchKernel` 은 커널을 GPU 에 던지는 함수라, 이걸 세면 "얼마나 바쁜지" 를 근사할 수 있습니다.

핵심 설계 결정 두 가지:

- **quota 시행 안 함.** launch 를 거부하는 건 너무 거친 동작이고, 진짜 SM 격리는 훅으로 못 합니다. 여기서는 **세기만** 합니다.
- **lock 안 잡음.** PyTorch 는 launch 를 초당 수천 번 부릅니다. mutex 를 걸면 그게 곧 hot path 병목입니다. 그래서 `__atomic_fetch_add`(RELAXED)로 lock 없이 셉니다.

```c
static size_t       g_launch_count      = 0;
static unsigned int g_launch_log_every  = 1000;  /* N 번마다 dump. 0=off */
static cudaError_t (*real_cudaLaunchKernel)(const void *, dim3, dim3,
                                            void **, size_t, cudaStream_t) = NULL;

cudaError_t cudaLaunchKernel(const void *func, dim3 g, dim3 b,
                             void **args, size_t sh, cudaStream_t st) {
    if (g_in_hook)
        return real_cudaLaunchKernel(func, g, b, args, sh, st);

    if (!real_cudaLaunchKernel) { /* 첫 호출 보호: init 후 재시도 */ }

    g_in_hook = 1;
    /* lock 없이 원자적 증가 — RELAXED 로 충분 (단순 monotonic counter). */
    size_t count = __atomic_add_fetch(&g_launch_count, 1, __ATOMIC_RELAXED);

    cudaError_t err = real_cudaLaunchKernel(func, g, b, args, sh, st);
    g_in_hook = 0;

    if (g_launch_log_every > 0 && (count % g_launch_log_every) == 0)
        fprintf(stderr, "[fgpu] LAUNCH count=%zu (every %u)\n",
                count, g_launch_log_every);
    return err;
}
```

> **왜 카운터 증가를 `g_lock` 밖에 두는가?** 완성본은 alloc 훅과 launch 훅이 같은 상태(`g_used` 등)를 공유하지만, launch 카운터만은 **의도적으로** mutex 밖 atomic 입니다([fgpu_hook.c:772](../../hook/src/fgpu_hook.c#L772)). 이걸 mutex 뒤로 옮기면 안 됩니다 — 그 순간 launch hot path 가 직렬화되어 성능이 무너집니다.

`FGPU_LAUNCH_LOG_EVERY` 파싱은 `fgpu_init_locked` 에 추가합니다([fgpu_hook.c:335](../../hook/src/fgpu_hook.c#L335)). `0` 이면 dump off — overhead 측정 시 유용합니다.

마지막으로 **`atexit` 요약**을 등록합니다. 정상 종료 시 총 launch 수를 한 번 더 찍습니다:

```c
static void fgpu_launch_atexit_dump(void) {
    size_t n = __atomic_load_n(&g_launch_count, __ATOMIC_RELAXED);
    fprintf(stderr, "[fgpu] exit summary: total cudaLaunchKernel = %zu\n", n);
}
```

`fgpu_init_locked` 안에서 `atexit(fgpu_launch_atexit_dump)` 를 **한 번만** 등록합니다([fgpu_hook.c:372](../../hook/src/fgpu_hook.c#L372)). 완성본 dump 함수는 [fgpu_hook.c:197](../../hook/src/fgpu_hook.c#L197), launch 훅 본체는 [fgpu_hook.c:749](../../hook/src/fgpu_hook.c#L749).

검증은 [test_launch.cu](../../hook/tests/test_launch.cu) — noop 커널을 N 번(기본 1000) launch 합니다. 커널이 `atomicAdd` 로 자기 실행 횟수를 세므로([test_launch.cu:21](../../hook/tests/test_launch.cu#L21)), 우리 카운터와 커널 원자 카운터가 **둘 다 N** 이면 "훅이 launch 를 하나도 안 흘렸다" 는 증거가 됩니다.

> **여기서 실행해서 확인하세요 (게이트 C)**
> `PYTEST_LAUNCH_N=1000 FGPU_LAUNCH_LOG_EVERY=100` 으로 얹으면:
> - `[fgpu] LAUNCH count=100/200/.../1000` 정확히 10줄
> - `[fgpu] exit summary: total cudaLaunchKernel = 1000` (atexit)
> - stdout `[test-launch] kernel atomics = 1000` (커널이 실제로 다 돌았다는 확인)

---

## 내가 겪을 함정

- **return 경로 하나에서 `g_in_hook = 0` 을 빠뜨림.** 그 스레드의 이후 훅이 전부 죽습니다. 함수마다 return 을 손으로 세고, 각각에 `unlock + reset` 이 짝지어졌는지 확인하세요. `cudaMalloc` 은 3개, `cuMemAlloc_v2`/`cuMemCreate` 는 4개입니다.
- **재진입 early return 다음에 reset 을 넣음.** 재진입 분기는 가드를 set 하기 **전에** 돌아가므로 reset 하면 안 됩니다(원래 값을 지우면 바깥 훅이 오작동). set 전 return 은 reset 없음, set 후 return 은 전부 reset — 이 구분을 지키세요.
- **launch 카운터를 mutex 뒤로 옮김.** hot path 직렬화로 성능이 무너집니다. atomic 유지.
- **`_v2` 접미사 누락.** `dlsym(RTLD_NEXT, "cuMemAlloc")`(no `_v2`)은 심볼을 못 찾습니다. 반드시 `cuMemAlloc_v2`.
- **VMM 에서 VA 예약/매핑까지 후킹.** `cuMemMap` 은 물리량을 안 바꾸므로 후킹하면 이중 카운트만 생깁니다. `cuMemCreate`/`cuMemRelease` 만.
- **`CUdeviceptr`/`handle` 을 `void*` 로 직접 캐스트.** 컴파일러 경고를 피하려고 `(void*)*dptr` 로 쓰면 위험합니다. 반드시 `(void*)(uintptr_t)` 로 정수 폭을 명시.
- **계층을 섞어서 테스트.** 각 계층 테스트는 **그 계층 API 만** 부릅니다(`test_driver_alloc` 은 `cudaMalloc` 안 씀). 이렇게 격리해야 "이 훅 단독으로 quota 가 강제되는가" 가 깨끗하게 나옵니다.

---

## 완성 체크리스트 (눈으로 확인)

- [ ] `[fgpu] init` 에 `cuMemAlloc_v2`, `cuMemCreate`, `cudaLaunchKernel` 포인터가 모두 non-NULL.
- [ ] Runtime 테스트에서 `cudaMalloc` 한 번에 `ALLOW` 가 **한 줄만**(이중 아님).
- [ ] `test_driver_alloc`: 256 MiB ALLOW, 6 GiB DENY(`result=2`).
- [ ] `test_vmm_alloc`: 256 MiB ALLOW, 6 GiB DENY(`result=2`).
- [ ] `test_launch`: LAUNCH 라인 10개 + exit summary = N + 커널 atomics = N.
- [ ] 모든 훅에서 free/release 후 `used` 가 0 으로 복귀.

## 다음 챕터

지금까지 훅은 메모리를 **막고**, 컴퓨트를 **관찰**만 했습니다. 다음 7장(Stage 12)에서는 `cudaLaunchKernel` 경로에 **시간 기반 duty-cycle throttle** 을 얹어, launch 를 드롭하지 않으면서 `nanosleep` 으로 지연을 삽입해 컴퓨트 throughput 을 `compute_ratio` 에 비례시킵니다. 이번에 만든 lock-free launch 카운터 바로 옆에 붙는 로직이라, 이 장의 launch 훅 구조를 정확히 이해하고 넘어가는 게 중요합니다.
