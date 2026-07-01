# 1장. Stage 1 — 최소 훅을 밑바닥부터

## 이 장에서 만들 것

- 완전히 빈 `hook/src/fgpu_hook.c` 에서 시작해, `LD_PRELOAD` 가 실제로 내 `.so` 를 로드하는지부터 눈으로 확인합니다.
- `cudaMalloc` / `cudaFree` 를 가로채서 quota(할당 상한)를 강제하는 최소 훅을 **한 조각씩** 쌓아 올립니다.
- 한 번에 완성본을 베끼지 않습니다. "스텁 → 컴파일 → 실행으로 확인 → 로직 한 조각 추가 → 다시 확인" 리듬을 몸에 익힙니다.
- 마지막에는 `test_alloc.cu` 로 `FGPU_RATIO=0.4` 에서 256 MiB 는 ALLOW, 6 GiB 는 DENY 가 나오는 걸 검증합니다.

> 이 단계에서는 **mutex 도, 재진입 가드도 없습니다.** 단일 스레드에서 동작하는 가장 작은 훅만 만듭니다. 멀티스레드 안전성과 이중 카운트 문제는 5장(Stage 5-C)에서 실제로 "터진 다음" 도입합니다. 지금 미리 넣으면 왜 필요한지 감을 못 잡습니다.

---

## 개발 순서 체크리스트

1. 빈 파일 + 초소형 스텁으로 "`LD_PRELOAD` 가 내 `.so` 를 정말 로드하는가" 만 확인
2. `build_hook.sh` 를 처음 작성 → 컴파일 성공 확인
3. `dlsym(RTLD_NEXT)` 로 진짜 `cudaMalloc` 을 잡아 **그냥 위임**(pass-through) — quota 없음
4. `FGPU_RATIO` 읽기 + lazy quota 계산(`cudaMemGetInfo`) 추가
5. ALLOW / DENY 로직 추가 (`g_used + size > g_quota`)
6. `cudaFree` + 추적 리스트(`track_alloc`/`pop_alloc`)로 `g_used` 감소
7. `test_alloc.cu` + `run_test.sh` 로 baseline vs hooked 검증

각 스텝 끝에는 **"여기서 컴파일/실행해서 이걸 확인하세요"** 게이트가 있습니다. 게이트를 통과 못 하면 다음 스텝으로 넘어가지 마세요.

---

## 스텝 1 — "정말 로드되나?" 초소형 스텁

가장 먼저 궁금해야 할 건 quota 로직이 아니라 **"내 코드가 애초에 실행되기는 하는가"** 입니다. 그걸 확인하는 가장 싼 방법은 라이브러리가 로드될 때 자동으로 불리는 생성자 함수 하나입니다.

빈 `hook/src/fgpu_hook.c` 에 이것만 씁니다:

```c
#define _GNU_SOURCE
#include <stdio.h>

/* 라이브러리가 프로세스에 로드되는 순간 자동 실행되는 함수.
 * quota 도, dlsym 도, 아무것도 아직 없다. "내가 로드됐다" 한 줄만. */
__attribute__((constructor))
static void fgpu_hello(void) {
    fprintf(stderr, "[fgpu] hello — .so loaded\n");
}
```

`__attribute__((constructor))` 는 "이 함수를 `main` 보다 먼저, 라이브러리 로드 시점에 불러라" 라는 GCC 지시입니다. quota 훅과는 아무 상관 없지만, **로드 자체를 증명**하기엔 완벽합니다.

> 왜 이 순서인가? — 훅의 나머지 전부(dlsym, quota, 링크드 리스트)는 "내 `.so` 가 실제로 프로세스에 끼어든다" 는 전제 위에 서 있습니다. 그 전제가 깨져 있으면(경로 오타, 아키텍처 불일치, 컴파일 실패) 뒤 로직을 아무리 잘 짜도 한 줄도 안 돕니다. 그래서 가장 먼저 이 전제만 검증합니다.

---

## 스텝 2 — `build_hook.sh` 첫 작성 + 컴파일 확인

`.so` 를 만들 빌드 스크립트를 씁니다. 완성본은 [build_hook.sh](../../scripts/build_hook.sh) 를 참고하되, 스텝 1 시점에는 이 정도 최소형이면 충분합니다:

```bash
#!/usr/bin/env bash
set -euo pipefail
CUDA_HOME="${CUDA_HOME:-/usr/local/cuda}"
SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_DIR="${SRC_DIR}/build"
mkdir -p "${BUILD_DIR}"

gcc -O2 -fPIC -shared -Wall -Wextra \
    -I"${CUDA_HOME}/include" \
    -o "${BUILD_DIR}/libfgpu.so" \
    "${SRC_DIR}/hook/src/fgpu_hook.c" \
    -L"${CUDA_HOME}/lib64" -lcudart -ldl -lpthread

echo "[build] wrote ${BUILD_DIR}/libfgpu.so"
```

플래그를 하나씩 이해하고 넘어갑시다:

- `-shared -fPIC` : 공유 라이브러리(`.so`)를 만든다. `LD_PRELOAD` 로 끼워넣으려면 필수.
- `-ldl` : `dlsym`(스텝 3부터)을 위한 링크. 지금은 안 써도 미리 붙여둡니다.
- `-lpthread` : mutex(나중 단계)용. 역시 미리.
- `-lcudart` : `cudaMalloc` 심볼 타입을 헤더에서 가져오기 위한 링크.

> **여기서 컴파일해서 확인하세요 (게이트 1)**
> ```bash
> chmod +x scripts/build_hook.sh
> ./scripts/build_hook.sh
> ```
> `[build] wrote .../build/libfgpu.so` 가 뜨고 에러가 없어야 합니다. 그다음 아무 프로그램에나 얹어서 hello 가 찍히는지 봅니다:
> ```bash
> LD_PRELOAD=./build/libfgpu.so /bin/true
> ```
> stderr 에 `[fgpu] hello — .so loaded` 가 한 줄 나오면 성공. **여기서 안 나오면 절대 다음으로 넘어가지 마세요.** 십중팔구 경로 오타이거나 `_GNU_SOURCE`/헤더 문제입니다.

---

## 스텝 3 — `dlsym(RTLD_NEXT)` 으로 잡아서 그냥 위임

이제 hello 스텁을 버리고 진짜 가로채기를 합니다. 다만 **아직 quota 는 넣지 않습니다.** 오직 "우리 `cudaMalloc` 이 불리고, 우리가 진짜 `cudaMalloc` 을 다시 부를 수 있는가" 만 확인합니다. 이게 훅의 심장이라, 여기서 확실히 되는 걸 봐야 합니다.

```c
#define _GNU_SOURCE
#include <stdio.h>
#include <dlfcn.h>            /* dlsym, RTLD_NEXT */
#include <cuda_runtime_api.h> /* cudaError_t, cudaSuccess */

/* 진짜 cudaMalloc 의 주소를 담을 함수 포인터. 처음엔 NULL. */
static cudaError_t (*real_cudaMalloc)(void **, size_t) = NULL;

cudaError_t cudaMalloc(void **devPtr, size_t size) {
    /* 아직 안 잡았으면 지금 잡는다 (lazy).
     * RTLD_NEXT = "나 다음에 로드된 라이브러리(= 진짜 cudart)에서 찾아라". */
    if (!real_cudaMalloc)
        real_cudaMalloc = dlsym(RTLD_NEXT, "cudaMalloc");

    fprintf(stderr, "[fgpu] cudaMalloc intercepted size=%zu\n", size);
    return real_cudaMalloc(devPtr, size);  /* 그냥 위임 */
}
```

여기서 벌어지는 일:

1. 사용자 프로그램이 `cudaMalloc(&p, N)` 을 부른다.
2. `LD_PRELOAD` 덕분에 진짜 cudart 대신 **우리** `cudaMalloc` 이 먼저 불린다.
3. 우리가 `dlsym(RTLD_NEXT, "cudaMalloc")` 으로 **진짜** 주소를 얻어 위임한다.
4. 사용자는 정상적으로 메모리를 받는다 — 우리가 한 줄 찍은 것만 다르다.

완성본에서 `real_cudaMalloc` 선언은 [fgpu_hook.c:94](../../hook/src/fgpu_hook.c#L94) 에, 위임 로직은 [fgpu_hook.c:441](../../hook/src/fgpu_hook.c#L441) 부근에 있습니다(최종본은 quota 까지 붙어 있어 훨씬 깁니다).

> **여기서 실행해서 확인하세요 (게이트 2)**
> 아직 테스트 프로그램이 없다면, `cudaMalloc` 을 한 번 부르는 아무 CUDA 프로그램이나 얹어봅니다. `[fgpu] cudaMalloc intercepted size=...` 가 찍히고 프로그램이 **여전히 정상 동작**(메모리 받고 죽지 않음)하면 통과. 위임을 빠뜨리면(그냥 `return cudaSuccess` 같은 걸 하면) `devPtr` 가 안 채워져서 사용자 프로그램이 곧바로 죽습니다 — 그게 "가로채기만 하고 진짜를 안 부른" 전형적 버그입니다.

> **흔한 함정:** `dlsym` 결과가 `NULL` 인데 그대로 `real_cudaMalloc(...)` 을 부르면 세그폴트입니다. 지금은 단순화를 위해 넘어가지만, 완성본은 심볼이 아직 안 잡혔을 때를 위해 `fgpu_init_locked()` 에서 매번 재시도합니다([fgpu_hook.c:307](../../hook/src/fgpu_hook.c#L307)). 이유는 사용자가 `libcuda.so` 를 늦게 `dlopen` 하는 경우가 있어서인데, 자세한 건 5장에서.

---

## 스텝 4 — `FGPU_RATIO` 읽기 + lazy quota 계산

이제 "얼마까지 허용할지" 를 정할 차례입니다. 두 가지가 필요합니다:

- **비율**: 환경변수 `FGPU_RATIO` (예: 0.4 = GPU 전체의 40%).
- **GPU 전체 메모리**: `cudaMemGetInfo` 로 알아냅니다.

전역 상태를 몇 개 추가합니다:

```c
#include <stdlib.h>   /* getenv, atof */

static size_t g_used  = 0;    /* 지금까지 우리가 허용한 총 바이트 */
static size_t g_quota = 0;    /* 상한. 0 = "아직 안 정함" */
static double g_ratio = 1.0;  /* FGPU_RATIO */
static int    g_inited = 0;   /* env 읽기를 한 번만 하기 위한 플래그 */
```

그리고 초기화 루틴을 둘로 나눕니다. **왜 둘로 나누는지**가 중요합니다:

```c
/* env 읽기 — 한 번만. dlsym 재시도도 여기서. */
static void fgpu_init(void) {
    if (!real_cudaMalloc)
        real_cudaMalloc = dlsym(RTLD_NEXT, "cudaMalloc");

    if (g_inited) return;
    const char *r = getenv("FGPU_RATIO");
    g_ratio = r ? atof(r) : 1.0;
    if (g_ratio <= 0.0 || g_ratio > 1.0) g_ratio = 1.0;  /* 이상치 방어 */
    fprintf(stderr, "[fgpu] init: ratio=%.3f\n", g_ratio);
    g_inited = 1;
}

/* quota 계산 — quota 가 0 일 때만. */
static void compute_quota_if_needed(void) {
    if (g_quota != 0) return;
    size_t free_b = 0, total_b = 0;
    if (cudaMemGetInfo(&free_b, &total_b) == cudaSuccess && total_b > 0) {
        g_quota = (size_t)((double)total_b * g_ratio);
        fprintf(stderr, "[fgpu] quota lazily 계산: ratio=%.3f * total=%zu = %zu\n",
                g_ratio, total_b, g_quota);
    }
}
```

> **왜 quota 를 "지금" 계산하지 않고 lazy 로 미루는가?** — `cudaMemGetInfo` 는 CUDA 컨텍스트가 만들어진 뒤에야 동작합니다. 라이브러리 로드 시점(스텝 1의 constructor)에 부르면 아직 컨텍스트가 없어서 "no CUDA-capable device" 같은 엉뚱한 에러를 만납니다. 컨텍스트 존재가 보장되는 가장 빠른 시점은 **사용자가 처음 `cudaMalloc` 을 부른 순간**입니다. 그래서 quota 계산을 `cudaMalloc` 안으로 밀어넣습니다. 완성본의 이 판단은 [fgpu_hook.c:402](../../hook/src/fgpu_hook.c#L402) 주석에 그대로 적혀 있습니다.

`cudaMalloc` 안에서 두 초기화를 호출하도록 고칩니다:

```c
cudaError_t cudaMalloc(void **devPtr, size_t size) {
    fgpu_init();
    compute_quota_if_needed();
    fprintf(stderr, "[fgpu] size=%zu used=%zu quota=%zu\n", size, g_used, g_quota);
    return real_cudaMalloc(devPtr, size);  /* 아직 위임만 — DENY 는 다음 스텝 */
}
```

> **여기서 실행해서 확인하세요 (게이트 3)**
> `FGPU_RATIO=0.4 LD_PRELOAD=./build/libfgpu.so <아무 CUDA 프로그램>` 을 돌려서:
> - `[fgpu] init: ratio=0.400`
> - `[fgpu] quota lazily 계산: ... = <숫자>`
> 두 줄이 **순서대로** 나오면 통과. quota 숫자가 GPU 전체의 40% 근처인지 눈으로 확인하세요(8 GB 카드면 ~3.2 GB). 아직 아무것도 거부하지 않습니다.

---

## 스텝 5 — ALLOW / DENY 로직

드디어 quota 를 강제합니다. 규칙은 한 줄입니다: **"이번 걸 더하면 상한을 넘는가?"**

```c
cudaError_t cudaMalloc(void **devPtr, size_t size) {
    fgpu_init();
    compute_quota_if_needed();

    /* (a) 초과 검사. quota 가 0(계산 실패)이면 검사 skip. */
    if (g_quota > 0 && g_used + size > g_quota) {
        fprintf(stderr, "[fgpu] DENY  cudaMalloc size=%zu used=%zu quota=%zu\n",
                size, g_used, g_quota);
        return cudaErrorMemoryAllocation;   /* 진짜를 부르지도 않고 거부 */
    }

    /* (b) 통과 → 진짜 호출, 성공 시 used 증가. */
    cudaError_t err = real_cudaMalloc(devPtr, size);
    if (err == cudaSuccess) {
        g_used += size;
        fprintf(stderr, "[fgpu] ALLOW cudaMalloc ptr=%p size=%zu used=%zu/%zu\n",
                *devPtr, size, g_used, g_quota);
    }
    return err;
}
```

핵심 두 가지:

- **DENY 는 진짜 `cudaMalloc` 을 부르지도 않습니다.** 즉시 `cudaErrorMemoryAllocation`(값 2)을 돌려줍니다. 이건 PyTorch / cuBLAS 가 "GPU OOM" 으로 인식하는 표준 에러라서, 상위 프레임워크까지 자연스럽게 전파됩니다.
- **`g_used` 는 진짜 호출이 성공한 뒤에만** 증가시킵니다. CUDA 가 자체적으로 실패했는데 카운트를 올리면 상태가 어긋납니다.

완성본의 검사 로직은 [fgpu_hook.c:455](../../hook/src/fgpu_hook.c#L455) 에 있습니다.

> **여기서 실행해서 확인하세요 (게이트 4)**
> 작은 alloc 하나(예: 256 MiB)와 quota 를 넘는 큰 alloc 하나(예: 6 GiB)를 순서대로 부르는 프로그램을 얹으면:
> - 작은 것 → `[fgpu] ALLOW ...`
> - 큰 것 → `[fgpu] DENY ...` + 프로그램이 받은 에러 코드가 `2`
> 가 나와야 합니다. 이제 quota 가 실제로 작동합니다.

---

## 스텝 6 — `cudaFree` + 추적 리스트로 `g_used` 감소

지금까지는 alloc 만 셌습니다. free 하면 `g_used` 를 줄여야 하는데, 문제가 하나 있습니다: **`cudaFree(ptr)` 는 `ptr` 만 주지, 그게 몇 바이트였는지 안 알려줍니다.** 그래서 우리가 직접 "ptr → size" 매핑을 들고 있어야 합니다.

프로토타입 규모에서는 동시에 살아있는 할당이 수십~수백 개이므로 단순 연결 리스트면 충분합니다.

```c
#include <stdlib.h>  /* malloc, free */

typedef struct alloc_entry {
    void               *ptr;
    size_t              size;
    struct alloc_entry *next;
} alloc_entry_t;

static alloc_entry_t *g_allocs = NULL;

/* 새 할당을 맨 앞에 push (O(1)). */
static void track_alloc(void *ptr, size_t size) {
    alloc_entry_t *e = malloc(sizeof(*e));
    if (!e) return;
    e->ptr = ptr; e->size = size; e->next = g_allocs;
    g_allocs = e;
}

/* ptr 을 찾아 제거하고 size 반환. 못 찾으면 0. */
static size_t pop_alloc(void *ptr) {
    alloc_entry_t **cur = &g_allocs;   /* 이중 포인터로 노드 제거 단순화 */
    while (*cur) {
        if ((*cur)->ptr == ptr) {
            size_t s = (*cur)->size;
            alloc_entry_t *dead = *cur;
            *cur = dead->next;
            free(dead);
            return s;
        }
        cur = &(*cur)->next;
    }
    return 0;
}
```

`pop_alloc` 이 이중 포인터(`alloc_entry_t **`)를 쓰는 게 처음엔 헷갈릴 수 있는데, 이렇게 하면 "리스트 맨 앞 노드 삭제" 와 "중간 노드 삭제" 를 특수 케이스 없이 한 코드로 처리할 수 있습니다. 완성본은 [fgpu_hook.c:238](../../hook/src/fgpu_hook.c#L238)(`track_alloc`) 과 [fgpu_hook.c:257](../../hook/src/fgpu_hook.c#L257)(`pop_alloc`).

이제 `cudaMalloc` 성공 시 `track_alloc` 을 부르고, `cudaFree` 를 만듭니다:

```c
/* cudaMalloc 성공 분기에 한 줄 추가: */
    if (err == cudaSuccess) {
        g_used += size;
        track_alloc(*devPtr, size);   /* ← 추가 */
        fprintf(stderr, "[fgpu] ALLOW ...");
    }

/* 새 함수: */
static cudaError_t (*real_cudaFree)(void *) = NULL;

cudaError_t cudaFree(void *devPtr) {
    fgpu_init();
    if (!real_cudaFree) real_cudaFree = dlsym(RTLD_NEXT, "cudaFree");

    cudaError_t err = real_cudaFree(devPtr);   /* 먼저 진짜 free */
    if (err == cudaSuccess && devPtr != NULL) {
        size_t freed = pop_alloc(devPtr);
        if (freed > 0 && freed <= g_used) g_used -= freed;
        fprintf(stderr, "[fgpu] FREE  ptr=%p size=%zu used=%zu/%zu\n",
                devPtr, freed, g_used, g_quota);
    }
    return err;
}
```

주의할 점:

- `cudaFree` 는 **진짜 free 를 먼저** 부르고, 성공했을 때만 장부를 갱신합니다. free 는 거부할 이유가 없으니 통과가 기본입니다.
- `cudaFree(NULL)` 은 CUDA 표준상 no-op 이라 추적 갱신을 건너뜁니다.
- 훅이 붙기 전에 할당된 ptr 을 free 하면 `pop_alloc` 이 0 을 돌려주고, 그 경우 `g_used` 를 건드리지 않습니다.

완성본 `cudaFree` 는 [fgpu_hook.c:494](../../hook/src/fgpu_hook.c#L494).

> **여기서 실행해서 확인하세요 (게이트 5)**
> alloc → free 를 반복하는 프로그램에서 `FREE` 라인마다 `used=` 값이 줄어들고, 전부 free 한 뒤 `used=0` 으로 돌아오면 통과입니다. 만약 free 후에도 `used` 가 안 줄면 `track_alloc` 을 안 불렀거나 `pop_alloc` 이 ptr 을 못 찾는 것입니다.

---

## 스텝 7 — `test_alloc.cu` + `run_test.sh` 로 최종 검증

이제 전용 테스트를 만들어 전체를 한 번에 검증합니다. 완성본 [test_alloc.cu](../../hook/tests/test_alloc.cu) 는 딱 두 번 할당합니다:

- `alloc1 = 256 MiB` → 어떤 합리적 ratio 에서도 ALLOW
- `alloc2 = 6 GiB` → `FGPU_RATIO=0.4`(8 GB 카드 → quota ~3.2 GiB)에서 DENY

핵심은 [test_alloc.cu:28](../../hook/tests/test_alloc.cu#L28) 의 6 GiB 할당이 `err=2` 를 받아 오는 것이고, 마지막 free 들이 `used` 를 0 으로 되돌리는 것입니다.

실행은 [run_test.sh](../../scripts/run_test.sh) 가 baseline(훅 없음)과 hooked(훅 얹음) 두 번 돌립니다. 핵심 라인은 [run_test.sh:31](../../scripts/run_test.sh#L31) 의 `LD_PRELOAD=... FGPU_RATIO=... 테스트바이너리` 입니다.

> **여기서 실행해서 확인하세요 (게이트 6 — 최종)**
> ```bash
> ./scripts/build_hook.sh
> ./scripts/run_test.sh          # 기본 FGPU_RATIO=0.4
> ```
> hooked 실행의 stderr 에 다음이 **이 순서로** 나와야 합니다:
> 1. `[fgpu] init`
> 2. `[fgpu] quota lazily 계산`
> 3. 256 MiB 에 대한 `ALLOW`
> 4. 6 GiB 에 대한 `DENY` (테스트 프로그램은 `err=2` 수신)
> 5. `used` 를 0 으로 되돌리는 `FREE` 라인들
>
> baseline 실행에는 `[fgpu]` 라인이 **하나도** 없어야 합니다. 이 대비가 "훅이 실제로 개입하는가" 의 증거입니다.

---

## 내가 겪을 함정

- **위임을 빠뜨림.** 가로채기만 하고 진짜 `cudaMalloc` 을 안 부르면 `devPtr` 가 안 채워져 사용자 프로그램이 즉시 죽습니다. ALLOW 경로에서 `real_cudaMalloc(...)` 반환값을 꼭 돌려주세요.
- **quota 를 너무 일찍 계산.** constructor 나 `fgpu_init` 안에서 `cudaMemGetInfo` 를 부르면 컨텍스트가 없어 실패합니다. 반드시 첫 `cudaMalloc` 시점(lazy)에.
- **`g_used` 갱신 위치 실수.** DENY 인데 카운트를 올리거나, 진짜 호출이 실패했는데 올리면 장부가 어긋납니다. "진짜 성공 후에만 증가/감소" 규칙을 지키세요.
- **`_GNU_SOURCE` 누락.** `#include` 보다 먼저 정의하지 않으면 `RTLD_NEXT undeclared` 컴파일 에러가 납니다([fgpu_hook.c:70](../../hook/src/fgpu_hook.c#L70)).
- **stdout 에 로그를 찍음.** `[fgpu]` 로그는 반드시 **stderr** 로. stdout 으로 찍으면 사용자 프로그램 출력과 섞여서 논문 스크린샷/grep 이 깨집니다.
- **멀티스레드를 지금 걱정.** 하지 마세요. Stage 1 은 단일 스레드 테스트만 통과하면 됩니다. mutex 와 재진입 가드는 실제로 문제가 터지는 5장에서 도입합니다.

---

## 완성 체크리스트 (눈으로 확인)

- [ ] `./scripts/build_hook.sh` 가 경고 없이 `libfgpu.so` 를 만든다.
- [ ] baseline 실행에는 `[fgpu]` 라인이 하나도 없다.
- [ ] hooked 실행에 `init` → `quota lazily 계산` → `ALLOW`(256 MiB) → `DENY`(6 GiB) → `FREE` 순서가 나온다.
- [ ] 6 GiB 할당의 반환 에러가 테스트 프로그램에서 `err=2` 로 관측된다.
- [ ] 모든 free 후 마지막 `used=` 값이 0 이다.

## 다음 챕터

Stage 1 은 `cudaMalloc`/`cudaFree`(Runtime API) 한 계층만 잡았습니다. 하지만 PyTorch 나 JAX, 손으로 짠 CUDA 는 종종 **Driver API**(`cuMemAlloc_v2`)나 **VMM API**(`cuMemCreate`)로 직접 메모리를 잡습니다. 다음 5장에서는 이 계층들을 추가로 후킹합니다. 그리고 그 과정에서 처음으로 **이중 카운트 버그가 터지며**, 그때 비로소 재진입 가드(`__thread g_in_hook`)를 도입하게 됩니다 — Stage 1 을 단일 스레드로 짜둔 이유가 여기서 드러납니다.
