# 5장. CUDA API 계층과 훅 코드 완전 분해

> 📘 **이 장을 읽고 나면**
>
> - CUDA 가 GPU 메모리를 다루는 API 가 왜 여러 층(Runtime / Driver / VMM)으로 나뉘는지, 그리고 왜 그 층을 다 후킹해야 하는지 알게 돼요.
> - `cudaMalloc`(메모리)과 `cudaLaunchKernel`(연산)의 차이를 이해하게 돼요.
> - `fgpu_hook.c` 의 quota lazy 계산 → ALLOW/DENY 판단 → free 시 되돌리기 흐름을 코드로 따라갈 수 있어요.
> - Stage 12 duty-cycle 스로틀(시분할)이 시간축에서 어떻게 동작하는지 그림으로 이해하게 돼요.
> - 각 `.cu` 테스트가 무엇을 검증하는지 알게 돼요.

4장에서 "어떻게 가로채는가(LD_PRELOAD)" 를 배웠어요. 이번 장은 **"가로챈 다음 무슨 일을 하는가"** 입니다. 이게 이 프로젝트 코드의 진짜 알맹이예요.

---

## 5.1 CUDA 는 왜 여러 층의 API 를 가질까

### (1) 왜 중요한가

우리가 메모리 quota 를 강제하려면, 사용자가 GPU 메모리를 잡는 **모든 통로** 를 막아야 해요. 그런데 CUDA 는 GPU 메모리를 잡는 방법이 **하나가 아니에요.** 통로가 여러 개인데 하나만 막으면, 사용자가 다른 통로로 quota 를 몰래 넘길 수 있죠. 그래서 여러 층을 다 후킹합니다.

### (2) 일상 비유

건물에 들어가는 문이 여러 개라고 생각해보세요. 정문(Runtime), 후문(Driver), 지하 주차장 통로(VMM). 경비원이 정문만 지키면 후문으로 들어오는 사람은 못 잡아요. 모든 문에 경비원을 세워야 인원 수(=메모리 사용량)를 정확히 셀 수 있어요.

### (3) 세 층 소개 (초보 눈높이)

| 층 | 대표 함수 | 누가 쓰나 | 비유 |
|----|-----------|-----------|------|
| **Runtime API** | `cudaMalloc` / `cudaFree` | 대부분의 사용자, PyTorch 등 | 정문 (제일 흔한 입구) |
| **Driver API (classic)** | `cuMemAlloc_v2` / `cuMemFree_v2` | 저수준 CUDA, JAX, 직접 driver 쓰는 코드 | 후문 |
| **VMM API** | `cuMemCreate` / `cuMemRelease` | CUDA 10.2+ 최신 메모리 풀 라이브러리 | 지하 통로 |

- **Runtime API** 는 가장 쓰기 쉬운 고수준 통로예요. `cudaMalloc` 하나로 GPU 메모리가 뚝딱 나와요. 사실 이 함수는 내부적으로 아래층(Driver)을 부르기도 해요.
- **Driver API** 는 좀 더 저수준이에요. `cuMemAlloc_v2` 처럼 `cu` 로 시작하고 `_v2` 가 붙어요. (`_v2` 는 CUDA 4.0 에서 ABI 가 바뀐 뒤 유지되는 공식 심볼명이에요.)
- **VMM(Virtual Memory Management) API** 는 CUDA 10.2 에서 도입된 최신 방식이에요. 여기선 "물리 메모리 할당" 과 "가상 주소 예약/매핑" 이 **분리** 돼 있어요. 우리는 **물리 메모리가 실제로 잡히는** `cuMemCreate` 만 quota 로 계산해요.

### (4) fgpu_hook.c 실제 코드에서 보기

세 층의 훅 함수가 파일 안에 나란히 있어요. 전부 **같은 quota 상태**(`g_used`/`g_quota`/`g_lock`/`g_allocs`)를 공유하는 게 포인트예요:
- [fgpu_hook.c:441](../../hook/src/fgpu_hook.c#L441) — Runtime: `cudaMalloc`
- [fgpu_hook.c:494](../../hook/src/fgpu_hook.c#L494) — Runtime: `cudaFree`
- [fgpu_hook.c:540](../../hook/src/fgpu_hook.c#L540) — Driver: `cuMemAlloc_v2`
- [fgpu_hook.c:588](../../hook/src/fgpu_hook.c#L588) — Driver: `cuMemFree_v2`
- [fgpu_hook.c:645](../../hook/src/fgpu_hook.c#L645) — VMM: `cuMemCreate`
- [fgpu_hook.c:693](../../hook/src/fgpu_hook.c#L693) — VMM: `cuMemRelease`

공유 상태 변수 선언은 [fgpu_hook.c:127](../../hook/src/fgpu_hook.c#L127) 근처에 모여 있어요. 어느 층으로 메모리를 잡든 같은 `g_used` 에 더해지니, 통합 quota 가 강제됩니다.

### (5) 흔한 함정

- **VMM 에서 `cuMemMap`/`cuMemAddressReserve` 는 일부러 후킹 안 해요.** 이 함수들은 **가상 주소** 만 다루고 실제 물리 메모리를 잡거나 풀지 않아요. quota(물리량)에 영향이 없으니 안 잡는 게 맞아요. 관련 설명은 [fgpu_hook.c:621](../../hook/src/fgpu_hook.c#L621) 주석 블록에 있어요.
- **`cuMemAllocAsync` / `cuMemAllocManaged` 도 아직 안 잡아요** — 의도적인 향후 과제예요.

### (6) 한 줄 요약
> GPU 메모리 통로가 여러 층(Runtime/Driver/VMM)이라 하나만 막으면 우회 가능 → 세 층을 다 후킹하되 같은 quota 상태를 공유한다.

---

## 5.2 메모리 말고 "연산" — cudaLaunchKernel

### (1) 왜 필요한가

지금까지는 **메모리** 얘기였어요. 그런데 GPU 의 진짜 일은 **계산(연산)** 이죠. GPU 에서 계산을 시키는 코드 조각을 **커널(kernel)** 이라고 부르고, 그 커널을 GPU 에서 실행시키는 함수가 `cudaLaunchKernel` 이에요.

즉:
- `cudaMalloc` = "GPU 에 메모리 방을 하나 잡아줘"
- `cudaLaunchKernel` = "GPU 야, 이 계산을 지금 실행해!"

우리는 이 연산 실행 지점도 가로채서 (a) 몇 번 실행됐는지 세고(모니터링), (b) Stage 12 에서는 실행 속도를 조절(스로틀)해요.

### (2) 일상 비유

메모리(cudaMalloc)가 "주방에 재료 놓을 공간을 확보하는 것" 이라면, 커널 실행(cudaLaunchKernel)은 "요리사에게 요리해! 라고 주문을 넣는 것" 이에요. 우리는 주문이 몇 번 들어갔는지 세고(카운터), 너무 빨리 주문이 쏟아지면 잠깐 기다리게(스로틀) 할 수 있어요.

### (4) fgpu_hook.c 실제 코드에서 보기
- [fgpu_hook.c:749](../../hook/src/fgpu_hook.c#L749) — `cudaLaunchKernel` 훅 함수. 여기서 실행 횟수를 세고(스테이지 7), 스로틀을 적용해요(스테이지 12).

### (6) 한 줄 요약
> `cudaLaunchKernel` = GPU 연산 실행 지점. 메모리가 아니라 "연산" 을 감시(카운터)하고 조절(스로틀)한다.

---

## 5.3 훅 코드 완전 분해 — cudaMalloc 을 위에서 아래로

이제 가장 중요한 함수 `cudaMalloc` 훅([fgpu_hook.c:441](../../hook/src/fgpu_hook.c#L441))을 처음부터 끝까지 한 단계씩 따라가 봅시다. 나머지 alloc 훅(Driver/VMM)도 **완전히 똑같은 뼈대** 라, 이거 하나만 이해하면 전부 이해한 거예요.

### 단계 0. 재진입 가드 (자세한 건 6장에서)

```c
if (g_in_hook) {
    return real_cudaMalloc ? real_cudaMalloc(devPtr, size)
                           : cudaErrorInitializationError;
}
g_in_hook = 1;
```
- [fgpu_hook.c:445](../../hook/src/fgpu_hook.c#L445) — 이미 우리 훅 안에서 다시 들어온 경우(예: `cudaMalloc` 이 내부적으로 `cuMemAlloc_v2` 를 불러서 재진입)에는 **quota 를 두 번 세지 않도록** 그냥 진짜 함수만 부르고 빠져요. 이 이중 과금 방지가 6장 주제예요. 지금은 "이런 게 있구나" 만 알고 넘어가요.

### 단계 1. 자물쇠 잠그고 초기화

```c
pthread_mutex_lock(&g_lock);
fgpu_init_locked();
compute_quota_if_needed_locked();
```
- [fgpu_hook.c:450](../../hook/src/fgpu_hook.c#L450) — `g_lock` 을 잠가요. 여러 스레드가 동시에 `g_used` 를 건드리면 큰일 나거든요(6장). 함수 이름 끝의 `_locked` 는 "이 함수를 부를 땐 이미 락을 쥐고 있어야 한다" 는 우리 프로젝트의 관례예요.
- `fgpu_init_locked()` 에서 4장에서 본 `dlsym` 심볼 해석 + 환경변수 읽기가 일어나요.

### 단계 2. quota 를 "필요할 때 처음" 계산 (lazy quota)

- [fgpu_hook.c:402](../../hook/src/fgpu_hook.c#L402) — `compute_quota_if_needed_locked()`. `g_quota` 가 아직 0 이면 `cudaMemGetInfo` 로 GPU 전체 메모리를 알아낸 뒤 `ratio` 를 곱해요.

```c
cudaError_t r = cudaMemGetInfo(&free_b, &total_b);
if (r == cudaSuccess && total_b > 0) {
    g_quota = (size_t)((double)total_b * g_ratio);
}
```
- [fgpu_hook.c:407](../../hook/src/fgpu_hook.c#L407) — 예: RTX 4060(8 GB) 에서 `FGPU_RATIO=0.4` 면 quota ≈ 3.2 GiB.

**왜 처음부터 안 하고 "필요할 때" 할까요?** `cudaMemGetInfo` 는 CUDA 컨텍스트가 만들어진 뒤에야 동작해요. 라이브러리가 로드되는 순간(너무 이른 시점)에 부르면 "no CUDA-capable device" 같은 엉뚱한 에러가 나요. 그게 안전하게 보장되는 가장 빠른 시점이 **사용자가 처음 `cudaMalloc` 을 부른 순간** 이에요. 그래서 "lazy(지연) quota" 라고 부릅니다. 성공하면 이런 로그가 찍혀요:
- [fgpu_hook.c:410](../../hook/src/fgpu_hook.c#L410) — `[fgpu] quota lazily 계산: ratio=... * total=... = ... bytes`

### 단계 3. ALLOW / DENY 판단 — 핵심 한 줄

```c
if (g_quota > 0 && g_used + size > g_quota) {
    fprintf(stderr, "[fgpu] DENY  cudaMalloc size=%zu used=%zu quota=%zu\n", ...);
    pthread_mutex_unlock(&g_lock);
    g_in_hook = 0;
    return cudaErrorMemoryAllocation;
}
```
- [fgpu_hook.c:455](../../hook/src/fgpu_hook.c#L455) — 판단 공식은 아주 단순해요: **"지금까지 쓴 양(`g_used`) + 이번에 요청한 양(`size`) 이 상한(`g_quota`)을 넘는가?"**
  - 넘으면 → **진짜 `cudaMalloc` 을 부르지도 않고** `DENY` 로그를 찍고 `cudaErrorMemoryAllocation`(에러 코드 2)을 돌려줘요. 이 에러는 PyTorch 등이 "GPU 메모리 부족(OOM)" 으로 인식하는 표준 에러라, 사용자 프로그램까지 자연스럽게 전파돼요.

> **여기 아주 중요한 포인트(오해 주의):** DENY 로 빠져나갈 때도 **반드시** ① `pthread_mutex_unlock(&g_lock)` 으로 자물쇠를 풀고 → ② `g_in_hook = 0` 으로 재진입 가드를 되돌린 뒤 → ③ `return` 해요([fgpu_hook.c:459](../../hook/src/fgpu_hook.c#L459)~[461](../../hook/src/fgpu_hook.c#L461)). 과거에 어떤 분석이 "DENY 경로에서 `g_in_hook` 리셋이 빠졌다" 고 버그로 지적한 적이 있는데, **그건 오탐이에요.** 코드를 직접 보면 unlock 다음에 `g_in_hook = 0` 후 return 하는 게 정확히 들어가 있어요. **모든** return 경로에서 락 해제 + 가드 리셋을 하는 게 이 코드의 철칙입니다.

### 단계 4. 통과 → 진짜 할당하고 기록하기

```c
cudaError_t err = real_cudaMalloc(devPtr, size);
if (err == cudaSuccess) {
    g_used += size;
    track_alloc(*devPtr, size);
    fprintf(stderr, "[fgpu] ALLOW cudaMalloc ptr=%p size=%zu used=%zu/%zu\n", ...);
}
```
- [fgpu_hook.c:465](../../hook/src/fgpu_hook.c#L465) — 진짜 `cudaMalloc` 호출 (4장에서 배운 함수 포인터).
- [fgpu_hook.c:467](../../hook/src/fgpu_hook.c#L467) — 성공하면 `g_used` 를 요청 크기만큼 늘려요.
- [fgpu_hook.c:468](../../hook/src/fgpu_hook.c#L468) — `track_alloc(*devPtr, size)` : **"이 포인터는 몇 바이트짜리" 라는 정보를 따로 저장** 해요. 이게 왜 필요할까요?

### 단계 5. ptr → size 를 기억해야 하는 이유 (track_alloc / pop_alloc)

나중에 사용자가 `cudaFree(ptr)` 를 부르면, 우리는 "그 `ptr` 이 몇 바이트였는지" 알아야 `g_used` 를 정확히 줄일 수 있어요. 그런데 **CUDA 는 그 크기를 알려주지 않아요.** `cudaFree` 는 포인터 하나만 받거든요. 그래서 우리가 직접 "포인터 → 크기" 매핑을 들고 있어야 해요.

이 매핑을 저장하는 자료구조가 **단일 연결 리스트(linked list)** 예요:
- [fgpu_hook.c:226](../../hook/src/fgpu_hook.c#L226) — `alloc_entry` 구조체 (`ptr`, `size`, `next`)
- [fgpu_hook.c:238](../../hook/src/fgpu_hook.c#L238) — `track_alloc` : 새 할당을 리스트 맨 앞에 추가 (O(1))
- [fgpu_hook.c:257](../../hook/src/fgpu_hook.c#L257) — `pop_alloc` : 리스트에서 `ptr` 을 찾아 제거하고 그 `size` 를 돌려줌

(할당이 보통 수십~수백 개라 단순 연결 리스트로 충분해요. 성능이 문제되면 나중에 해시맵으로 바꾸면 됩니다.)

### 단계 6. 마무리 — 락 풀고 가드 리셋
```c
    pthread_mutex_unlock(&g_lock);
    g_in_hook = 0;
    return err;
```
- [fgpu_hook.c:477](../../hook/src/fgpu_hook.c#L477) — 성공/실패와 상관없이 마지막에 락 풀고 가드 리셋하고 결과 반환.

### 그리고 free — 되돌리기

`cudaFree` 훅([fgpu_hook.c:494](../../hook/src/fgpu_hook.c#L494))은 반대로 동작해요:
```c
cudaError_t err = real_cudaFree(devPtr);
if (err == cudaSuccess && devPtr != NULL) {
    size_t freed = pop_alloc(devPtr);
    if (freed > 0 && freed <= g_used) {
        g_used -= freed;
    }
    fprintf(stderr, "[fgpu] FREE  ptr=%p size=%zu used=%zu/%zu\n", ...);
}
```
- [fgpu_hook.c:503](../../hook/src/fgpu_hook.c#L503) — 진짜 `cudaFree` 부르고,
- [fgpu_hook.c:505](../../hook/src/fgpu_hook.c#L505) — `pop_alloc` 으로 그 포인터의 크기를 회수한 뒤,
- [fgpu_hook.c:507](../../hook/src/fgpu_hook.c#L507) — `g_used` 에서 빼요. 이렇게 해서 free 후 `used` 가 다시 0 으로 돌아옵니다.

### 흔한 함정
- **`track_alloc` 을 빼먹으면** 나중에 `cudaFree` 때 크기를 못 찾아(`pop_alloc` 이 0 반환) `g_used` 가 영영 안 줄어요. 그러면 메모리를 다 풀었는데도 DENY 가 나는 이상한 현상이 생겨요.
- **DENY 는 진짜 함수를 아예 안 부른다** 는 걸 기억하세요. 그래서 GPU 에는 아무 흔적도 안 남고, `g_used` 도 안 바뀌어요(할당이 없었으니까).

### 한 줄 요약
> 훅의 뼈대: 가드 → 락 → 초기화 → (lazy) quota 계산 → `g_used+size>g_quota` 판단 → 통과면 진짜 호출 + `track_alloc`, free 면 `pop_alloc` 으로 `g_used` 감소 → 락 풀고 가드 리셋.

---

## 5.4 Stage 12 — duty-cycle 스로틀 (연산 시분할)

### (1) 왜 필요한가

메모리는 quota 로 나눴는데, **연산 속도** 는요? RTX 4060 은 MIG(하드웨어 분할)가 없어서 SM(연산 유닛)을 물리적으로 못 쪼개요. 그래서 대신 **시간을 쪼개는(duty-cycle)** 협력적 방법을 써요. "너는 이 시간 구간의 40% 만 커널을 실행하고, 나머지 60% 는 잠깐 쉬어" 라고 하는 거예요.

### (2) 일상 비유

**신호등** 을 생각해보세요. 100초짜리 주기(윈도우)에서 초록불 40초 동안만 차가 지나가고(`compute_ratio=0.4`), 나머지 60초는 빨간불이라 대기해요. 차(커널 실행)를 **없애는 게 아니라** 잠깐 기다리게 할 뿐이에요. 그래서 launch 를 **드롭하지 않고 지연(delay)만** 넣습니다.

### (3) 시간축 그림

윈도우 크기 100ms, `compute_ratio=0.4` 인 경우:

```
|<--------------- 윈도우 100ms --------------->|
[  통과 구간 40ms  ][      대기 구간 60ms      ]
 ← launch 즉시 통과 →  ← 이 구간에 온 launch 는 nanosleep 으로 대기 →
```

- 윈도우 시작부터 40ms(= `compute_ratio × window`) 안에 들어온 커널 실행은 **즉시 통과.**
- 40ms 를 넘어서 들어온 커널 실행은, 이번 윈도우가 끝날 때까지(남은 시간만큼) `nanosleep` 으로 재워요. 그다음 새 윈도우가 시작돼요.

이렇게 하면 전체적으로 GPU 를 40% 시간만 쓰게 되어, throughput(초당 처리량)이 대략 baseline 의 40% 로 떨어집니다.

### (4) fgpu_hook.c 실제 코드에서 보기

전체 스로틀 로직은 `cudaLaunchKernel` 훅 안에 있어요:
- [fgpu_hook.c:773](../../hook/src/fgpu_hook.c#L773) — `g_launch_count` 를 **lock 없이** `__atomic_add_fetch` 로 증가 (Stage 7 카운터). 왜 락을 안 쓰는지는 6장에서.
- [fgpu_hook.c:779](../../hook/src/fgpu_hook.c#L779) — `if (g_throttle_enable && g_compute_ratio < 1.0)` : 스로틀이 켜져 있고 비율이 1 미만일 때만 동작.
- [fgpu_hook.c:781](../../hook/src/fgpu_hook.c#L781) — 현재 시각을 `clock_gettime(CLOCK_MONOTONIC)` 으로 나노초 단위로 읽어요.
- [fgpu_hook.c:790](../../hook/src/fgpu_hook.c#L790) — `elapsed`(윈도우 시작 후 경과 시간) 계산. 윈도우가 만료됐으면([fgpu_hook.c:791](../../hook/src/fgpu_hook.c#L791)) 새 윈도우로 리셋.
- [fgpu_hook.c:796](../../hook/src/fgpu_hook.c#L796) — `active_limit = compute_ratio × window` (통과 구간 크기).
- [fgpu_hook.c:797](../../hook/src/fgpu_hook.c#L797) — `if (elapsed >= active_limit)` : 통과 구간을 넘었으면,
- [fgpu_hook.c:803](../../hook/src/fgpu_hook.c#L803) — `nanosleep(&sleep_ts, NULL)` 으로 남은 시간만큼 대기.
- [fgpu_hook.c:818](../../hook/src/fgpu_hook.c#L818) — 그리고 **어느 경로든 결국** 진짜 `real_cudaLaunchKernel` 을 호출해요. **launch 는 절대 드롭되지 않아요.**

윈도우 시작 시점 `g_window_start_ns` 는 여러 스레드가 건드릴 수 있어서 `__atomic_load_n`/`__atomic_store_n` 으로 읽고 써요([fgpu_hook.c:784](../../hook/src/fgpu_hook.c#L784), [786](../../hook/src/fgpu_hook.c#L786)). 두 스레드가 동시에 "지금" 으로 리셋해도 둘 다 "지금" 이라 무해해요.

관련 환경변수 파싱은 [fgpu_hook.c:346](../../hook/src/fgpu_hook.c#L346) 근처에 있어요(`FGPU_THROTTLE_ENABLE`, `FGPU_COMPUTE_RATIO`, `FGPU_WINDOW_MS`). `FGPU_COMPUTE_RATIO` 를 안 주면 메모리용 `g_ratio` 를 그대로 써요([fgpu_hook.c:356](../../hook/src/fgpu_hook.c#L356)).

### (4-bis) 카운터 로그와 atexit 요약

- [fgpu_hook.c:823](../../hook/src/fgpu_hook.c#L823) — `g_launch_log_every` 마다 한 번씩 `[fgpu] LAUNCH count=...` 를 찍어요.
- [fgpu_hook.c:197](../../hook/src/fgpu_hook.c#L197) — `fgpu_launch_atexit_dump` : 프로그램이 **정상 종료** 될 때 `atexit` 콜백이 불려서 `[fgpu] exit summary: total cudaLaunchKernel = ...` 최종 요약을 찍어요. 스로틀이 켜져 있으면 총 sleep 횟수도 같이 찍혀요([fgpu_hook.c:205](../../hook/src/fgpu_hook.c#L205)). 이 콜백 등록은 [fgpu_hook.c:373](../../hook/src/fgpu_hook.c#L373) 에서 한 번만 이뤄져요.

### (5) 흔한 함정
- **이건 진짜 SM 격리가 아니에요.** 내가 sleep 하는 동안 다른 컨테이너가 GPU 를 안 쓰면 GPU 는 그냥 놀아요(work-conserving 아님). 협력적 시분할일 뿐이에요.
- **커널 실행 시간을 반영 못 해요.** 100ms 짜리 무거운 커널 1번과 1μs 짜리 가벼운 커널 1000번을, 우리 카운터는 구분 못 해요(둘 다 "실행 횟수" 로만 봄).
- **`nanosleep` 은 정밀하지 않아요.** 리눅스에서 최소 ~50μs 라, 1ms 미만 윈도우에서는 부정확해요.

### (6) 한 줄 요약
> 스로틀 = 시간 윈도우(기본 100ms)에서 `compute_ratio` 비율만 커널 통과, 나머지는 `nanosleep` 으로 지연(드롭 아님). 신호등처럼 시간을 쪼개는 협력적 시분할.

---

## 5.5 각 테스트(.cu) 는 무엇을 검증하나

각 테스트는 **딱 한 층만** 건드려서, 그 층의 훅이 다른 훅 간섭 없이 잘 도는지 **고립해서** 검증해요. 이게 중요한 이유: 만약 `test_alloc` 에서 driver 훅까지 같이 불리면, 문제가 생겼을 때 어느 층 잘못인지 알기 어렵거든요.

- **`test_alloc.cu`** ([test_alloc.cu:20](../../hook/tests/test_alloc.cu#L20)) — Runtime API 만. 256 MiB(ALLOW) + 6 GiB(DENY) 를 `cudaMalloc` 으로 잡아, `FGPU_RATIO=0.4` 에서 ALLOW 1개 + DENY 1개가 나오는지 확인. 6 GiB 시도의 반환 코드가 에러 2(`cudaErrorMemoryAllocation`)로 사용자에게 전파되는지도 봐요([test_alloc.cu:28](../../hook/tests/test_alloc.cu#L28)).
- **`test_driver_alloc.cu`** ([test_driver_alloc.cu:41](../../hook/tests/test_driver_alloc.cu#L41)) — Driver API(`cuMemAlloc_v2`/`cuMemFree_v2`)만. Runtime(`cudaMalloc`)은 **일부러 안 불러서**, 오직 driver 훅만으로 quota 가 강제되는지 검증([test_driver_alloc.cu:69](../../hook/tests/test_driver_alloc.cu#L69)).
- **`test_vmm_alloc.cu`** ([test_vmm_alloc.cu:48](../../hook/tests/test_vmm_alloc.cu#L48)) — VMM API(`cuMemCreate`/`cuMemRelease`)만. `cuMemMap`/`cuMemAddressReserve` 는 안 부르는데, 우리 quota 모델이 **물리 alloc(`cuMemCreate`)** 만 잡기 때문에 VA 매핑 없이도 ALLOW/DENY 가 나와야 함을 확인.
- **`test_launch.cu`** ([test_launch.cu:40](../../hook/tests/test_launch.cu#L40)) — noop 커널을 N번(기본 1000) launch 해서 `cudaLaunchKernel` 이 가로채지고 카운트되는지 확인. quota 와 무관. 커널 안의 `atomicAdd` 카운터가 N 과 같으면([test_launch.cu:64](../../hook/tests/test_launch.cu#L64)) 훅이 launch 를 **드롭하지 않았다** 는 증거예요.
- **`test_throttle.cu`** ([test_throttle.cu:44](../../hook/tests/test_throttle.cu#L44)) — noop 커널을 tight loop 로 N번(기본 5000) launch 하고 **wall-clock 경과 시간 + launches/sec** 를 측정([test_throttle.cu:73](../../hook/tests/test_throttle.cu#L73)). 스로틀 ON 일 때 throughput 이 baseline 대비 `compute_ratio` 비율로 떨어지는지 확인.

### 한 줄 요약
> 각 `.cu` 테스트는 한 API 층만 고립 검증한다: alloc/driver/vmm 은 256 MiB ALLOW + 6 GiB DENY 패턴, launch 는 카운트 정확성, throttle 은 throughput 비례성.

---

## ✍️ 스스로 점검

1. 사용자가 `cudaMalloc` 대신 `cuMemCreate`(VMM) 로 메모리를 잡아도 quota 가 강제되는 이유는 무엇인가요? (힌트: 공유하는 것)
2. `cudaFree` 훅이 `g_used` 를 정확히 줄이려면 왜 `track_alloc`/`pop_alloc` 로 "포인터 → 크기" 를 따로 기억해야 하나요?
3. 스로틀에서 `nanosleep` 으로 대기시킨 launch 는 결국 어떻게 되나요? "드롭" 과 "지연" 의 차이를 설명해보세요.

## 🎯 다음 챕터

6장 **「동시성과 스레드 안전성 — 왜 자물쇠(mutex)가 필요한가」** 로 갑니다. 여러 스레드가 동시에 `g_used` 를 건드리면 왜 값이 깨지는지(race condition), `pthread_mutex` 자물쇠와 `_locked` 관례, 이중 과금을 막는 `__thread g_in_hook` 재진입 가드, 그리고 launch 카운터가 왜 락 대신 atomic 을 쓰는지를 파고듭니다.

---

⟵ [이전: 4장. LD_PRELOAD와 dlsym](04-ld-preload-and-dlsym.md) ・ [📚 전체 목차](README.md) ・ [다음: 6장. 스레드 안전성](06-thread-safety.md) ⟶
