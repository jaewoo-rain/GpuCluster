# Chapter 04 — 스레드 안전성: mutex, atomic, reentrancy

## 학습 목표

- mutex 와 atomic 의 *서로 다른 용도* 를 한 줄로 설명한다.
- 우리 hook 이 alloc 에는 mutex, launch 에는 atomic 을 쓰는 이유를 안다.
- "재진입(reentrancy)" 이 *왜* 멀티스레드와 별개의 문제인지, `__thread` 변수가 어떻게 그걸 푸는지 안다.
- `__atomic_add_fetch` 의 메모리 순서(`__ATOMIC_RELAXED`) 의미를 안다.

---

## 4.1 동시성 문제의 두 종류

| 문제 | 누가 일으키나 | 우리 솔루션 |
|---|---|---|
| **Race condition** (두 스레드가 동시에 같은 변수 읽고 쓰기) | 멀티스레드 | mutex 또는 atomic |
| **Reentrancy** (한 함수가 자기 자신 흐름 안에서 다시 진입) | 단일 스레드여도 발생 가능 | thread-local flag (`__thread`) |

이 둘이 헷갈리기 쉬워서 항상 분리해서 생각해야 합니다.

---

## 4.2 Mutex — alloc bookkeeping 보호

[fgpu_hook.c:124](../../hook/src/fgpu_hook.c#L124):
```c
static pthread_mutex_t g_lock = PTHREAD_MUTEX_INITIALIZER;
```

이 자물쇠가 보호하는 *상태* (= "임계영역 안에서만 만질 수 있는 자료"):
- `g_used`, `g_quota`, `g_ratio`, `g_inited`, `g_allocs`, `real_*` 함수 포인터들.

사용 패턴:
```c
pthread_mutex_lock(&g_lock);
... 위 변수들 만지는 모든 코드 ...
pthread_mutex_unlock(&g_lock);
```

### 왜 atomic 으로 충분하지 않나?

`g_used += size` 만 보면 atomic 이면 될 것 같지만, 실제 임계영역은 더 큽니다:

```c
if (g_used + size > g_quota) { ... DENY ... }   // 검사
err = real_cudaMalloc(...);                       // 진짜 호출
if (err == cudaSuccess) {
    g_used += size;                               // 성공이면 증가
    track_alloc(*ptr, size);                      // 리스트 push
}
```

검사와 증가 사이에 다른 스레드가 끼면 *두 스레드 모두 quota 안에 들어온다고 판단* 한 뒤 둘 다 진짜 함수를 부르고, 합쳐서 quota 초과. 이게 전형적인 TOCTOU(Time-of-check to time-of-use) race. **검사–호출–갱신을 통째로 lock 으로 감싸야** 안전합니다.

### Lock contention 비용

매번 lock 잡으면 비용이 듭니다. 다행히:
- alloc 빈도 = 보통 초당 수십 번 정도 (PyTorch 의 매 텐서 alloc 도 caching off 시 그 정도).
- 임계영역 짧음 (몇 마이크로초).

→ lock 비용은 [Chapter 11 의 마이크로벤치](11-benchmarking.md) 에서 정량적으로 측정됩니다.

### 더 공부하려면
- `man 3 pthread_mutex_lock` ([man7.org](https://man7.org/linux/man-pages/man3/pthread_mutex_lock.3p.html))
- *The Linux Programming Interface* (Michael Kerrisk) — 30장 (Threads). 책 한 권 사두면 평생 든든.

---

## 4.3 Atomic — launch counter

[fgpu_hook.c:708](../../hook/src/fgpu_hook.c#L708):
```c
size_t count = __atomic_add_fetch(&g_launch_count, 1, __ATOMIC_RELAXED);
```

이건 lock 없이 안전하게 +1 하는 명령. 왜 lock 안 쓰나?

- launch 는 PyTorch 에서 초당 *수천 번* 호출됨. mutex 의 lock/unlock 오버헤드(보통 50-100ns) × 수천 = 무시할 수 없음.
- 우리가 보호할 상태는 단 한 변수, 단순 +1. mutex 의 무거운 도구가 필요 없음.

### `__ATOMIC_RELAXED` 가 뭔가?

C11/C++11 atomic 은 *메모리 순서* 라는 옵션을 받습니다. RELAXED 는 가장 약한 보장 — "원자성만 보장, 다른 메모리 작업과의 순서는 보장 X".

- **Sequential consistency (`__ATOMIC_SEQ_CST`)**: 가장 강함. 모든 스레드가 모든 atomic 작업의 *전역 순서* 를 똑같이 본다. 비쌈.
- **Acquire/Release (`__ATOMIC_ACQ_REL`)**: 락 구현 등에 쓰이는 중간 강도.
- **Relaxed (`__ATOMIC_RELAXED`)**: 단순 카운터 등 "내 카운터 값이 단조증가하기만 하면 됨" 인 케이스.

우리 launch counter 는 다른 변수와의 의존이 없는 *순수 monotonic counter* 라 RELAXED 로 충분. 다른 변수와 짝지어 쓰면 더 강한 순서가 필요합니다.

### 더 공부하려면
- [Preshing on Programming — Memory Ordering at Compile Time](https://preshing.com/20120625/memory-ordering-at-compile-time/) — 시리즈 전체 추천
- [GCC manual — Built-in Functions for Memory Model Aware Atomic Operations](https://gcc.gnu.org/onlinedocs/gcc/_005f_005fatomic-Builtins.html)
- [cppreference — std::memory_order](https://en.cppreference.com/w/cpp/atomic/memory_order)

---

## 4.4 Reentrancy — `__thread` 가드

이게 이 챕터의 핵심입니다. 멀티스레드와 *별개의* 문제예요.

### 시나리오

[fgpu_hook.c:42-50](../../hook/src/fgpu_hook.c#L42-L50) 의 주석을 보면:

> 만약 libcudart 의 cudaMalloc 이 내부적으로 libcuda 의 cuMemAlloc_v2 를 호출하면, 사용자 한 번의 cudaMalloc 호출이 우리 hook 을 *두 번* 통과하면서 g_used 가 두 배로 누적될 수 있다.

순서를 따라가면:

```
[사용자]            cudaMalloc(p, N)
                       │ (PLT → 우리 cudaMalloc)
[우리 hook ①]        cudaMalloc — g_in_hook=0 이라 정상 진입
                       lock, g_used += N, track_alloc(...)
                       real_cudaMalloc(p, N) 호출
                          │
[cudart 내부]            cuMemAlloc_v2(p, N)
                          │ (PLT → 우리 cuMemAlloc_v2)
[우리 hook ②]            cuMemAlloc_v2 — g_in_hook=? ← 여기가 관건
                          만약 가드 없으면: g_used += N (✗ 두 번째 카운트)
                          가드 있으면:    그냥 진짜 함수 위임 (✓)
```

### 왜 mutex 로 못 막나?

같은 스레드가 자기가 잡은 lock 을 *재귀적으로* 잡으려 들면 deadlock 입니다. POSIX `pthread_mutex_t` 의 기본 type 은 `PTHREAD_MUTEX_NORMAL` 인데, 같은 스레드의 재귀 lock 은 정의되지 않은 동작(보통 deadlock).

`PTHREAD_MUTEX_RECURSIVE` 라는 옵션도 있긴 하지만 그래도 **카운트가 두 번 더해지는 문제** 자체는 해결 못 합니다 (락은 재진입 가능해도, 그 안의 `g_used += size` 가 두 번 실행되는 건 막을 수 없음).

### `__thread` (TLS — Thread-Local Storage)

[fgpu_hook.c:151](../../hook/src/fgpu_hook.c#L151):
```c
static __thread int g_in_hook = 0;
```

`__thread` 는 GCC/Clang 의 키워드 (C11 의 `_Thread_local` 과 동등). 이 변수는 **각 스레드가 자기만의 사본을 가집니다**. 메모리 레이아웃상으론 보통 fs/gs segment register 가 가리키는 영역에 스레드별로 박힘.

특성:
- 다른 스레드의 `g_in_hook` 와 독립 → lock 없이 안전.
- 한 스레드의 hook 진입/탈출만 표시 → 그 스레드 안에서의 재진입을 정확히 감지.

사용 패턴:
```c
cudaError_t cudaMalloc(void **p, size_t n) {
    if (g_in_hook) {                              // ← 재진입?
        return real_cudaMalloc ? real_cudaMalloc(p, n)
                               : cudaErrorInitializationError;
    }
    g_in_hook = 1;                                // ← 진입 마킹
    ... 본 hook 로직 (lock + bookkeeping) ...
    g_in_hook = 0;                                // ← 탈출 마킹 — 모든 return 경로에서!
    return err;
}
```

### 함정: 모든 return 경로에서 `g_in_hook = 0` 잊지 말 것

[fgpu_hook.c](../../hook/src/fgpu_hook.c) 를 보면 DENY 경로, FAIL 경로, 정상 경로 모두에서 `g_in_hook = 0` 이 들어가 있어요. 한 군데라도 빠뜨리면 **그 스레드의 후속 hook 호출이 영영 skip** 됩니다 (`g_in_hook` 이 1 인 채로 남아 매번 위임만 하고 카운트 안 됨).

C++ 의 RAII 가 있다면 destructor 가 자동으로 처리해주겠지만 우리는 C 라 *수동*. 코드 검토 시 항상 모든 분기에서 reset 됐는지 확인하세요.

### 더 공부하려면
- [GCC — Thread-Local Storage](https://gcc.gnu.org/onlinedocs/gcc/Thread-Local.html)
- [Ulrich Drepper — ELF Handling For Thread-Local Storage](https://www.akkadia.org/drepper/tls.pdf) — TLS 의 ELF 레이아웃 깊이 파기
- C11 표준 §6.7.1 — `_Thread_local`

---

## 4.5 atexit — 정상 종료 시점에만 동작

[fgpu_hook.c:175-182](../../hook/src/fgpu_hook.c#L175-L182):
```c
static void fgpu_launch_atexit_dump(void) {
    size_t n = __atomic_load_n(&g_launch_count, __ATOMIC_RELAXED);
    fprintf(stderr, "[fgpu] exit summary: total cudaLaunchKernel = %zu\n", n);
}
```

`atexit(fn)` 으로 등록하면 `main` 이 `return` 하거나 `exit()` 호출 시 등록 역순으로 실행됩니다.

**한계**:
- `_exit()`, `abort()`, signal kill 은 atexit 핸들러 안 부름. → best-effort 수준.
- 다중 등록 가능 (LIFO 순서) — 우리는 한 번만 등록 ([:313-316](../../hook/src/fgpu_hook.c#L313-L316)).

### 더 공부하려면
- `man 3 atexit`
- C99 §7.20.4.2

---

## 4.6 직접 해보기 — 재진입 가드 끄기 실험

(이건 *위험한* 실험이라 결과만 적어둡니다, 직접은 안 권장)

`g_in_hook` 가드를 모두 주석 처리하고 빌드한 뒤 hook 을 돌리면, *어떤 시나리오에서는* `g_used` 가 의도보다 빨리 누적되어 진짜 quota 의 절반 정도에서 DENY 가 발생할 수 있습니다. 정확히 언제 발생할지는 cudart 내부 구현에 의존 — 그래서 *방어적으로* 가드를 두는 거예요.

대신 안전한 실험: [Chapter 03 직접해보기](03-hook-walkthrough.md#39-직접-해보기--로그-패턴-읽기) 의 로그에서 `[fgpu] ALLOW cudaMalloc` 이 *정확히 한 번* 만 나오는지 (사용자가 한 번 부른 것에 대해) 확인.

---

## 자가점검 질문

1. mutex 와 atomic 중 어느 게 *재진입* 을 해결할 수 있나? (정답: 둘 다 못 함. `__thread` 가 필요)
2. `__ATOMIC_RELAXED` 와 `__ATOMIC_SEQ_CST` 중 더 비싼 건? 왜?
3. 만약 `g_in_hook = 0` 을 한 return 경로에서 빠뜨리면 *그 스레드* 의 다음 cudaMalloc 호출은 어떻게 되는가?
4. `pthread_mutex_t` 를 `PTHREAD_MUTEX_RECURSIVE` 로 만들면 reentrancy 문제가 풀릴까?
5. 우리 `g_launch_count` 를 보호하는 lock 이 없는 이유 두 가지를 말하라.

→ [Chapter 05: Docker + nvidia-container-toolkit](05-docker-gpu.md)
