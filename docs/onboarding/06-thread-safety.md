# 6장. 동시성과 스레드 안전성 — 왜 자물쇠(mutex)가 필요한가

> 📘 **이 장을 읽고 나면**
>
> - 여러 스레드가 동시에 같은 변수(`g_used`)를 건드리면 왜 값이 깨지는지(race condition)를 은행 예시로 이해하게 돼요.
> - `pthread_mutex` 자물쇠의 lock/unlock 과, 이 프로젝트의 `_locked` 접미사 관례를 알게 돼요.
> - `__thread g_in_hook` 재진입 가드가 "이중 과금" 을 어떻게 막는지, 왜 스레드마다 따로(thread-local)여야 하는지 이해하게 돼요.
> - 왜 launch 카운터/스로틀은 자물쇠 대신 `__atomic` 을 쓰는지(핫패스 성능) 알게 돼요.

5장에서 훅이 `g_used += size` 처럼 공유 변수를 건드리는 걸 봤어요. 그런데 GPU 프로그램은 보통 **여러 스레드** 로 돌아가요. 여러 스레드가 동시에 이 변수를 만지면 무슨 일이 벌어질까요? 이 장은 그 위험과, 우리가 쓰는 세 가지 방어 장치를 다뤄요.

---

## 6.1 race condition — 동시에 건드리면 값이 깨진다

### (1) 왜 중요한가

`g_used` 는 "지금까지 할당한 총 메모리량" 이에요. 이게 틀리면 quota 판단이 통째로 틀려져요. 그런데 여러 스레드가 **동시에** `g_used += size` 를 하면, 겉보기엔 한 줄이지만 실제로는 여러 단계(① 읽기 → ② 더하기 → ③ 쓰기)로 나뉘어서, 중간에 다른 스레드가 끼어들면 값이 깨질 수 있어요. 이걸 **race condition(경쟁 상태)** 이라고 불러요.

### (2) 일상 비유 — 은행 잔액

부부가 같은 통장(잔액 10만원)을 각자 ATM 에서 동시에 씁니다.

- **남편** ATM: 잔액 확인(10만원) → 8만원 출금해도 되겠네 → 출금
- **아내** ATM: 잔액 확인(10만원) → 8만원 출금해도 되겠네 → 출금

두 ATM 이 **거의 동시에** "잔액 10만원" 을 읽었어요. 둘 다 "8만원 출금 가능" 이라 판단하고, 둘 다 출금해서 총 16만원이 빠져나가요. 통장엔 10만원밖에 없었는데! 잔액이 -6만원이 됩니다.

우리 코드에서도 똑같아요. 두 스레드가 동시에 "`g_used`(현재 8GB) + 내 할당이 quota(10GB) 안에 들어가나?" 를 확인하고 **둘 다 통과** 라고 판단해서, 실제로는 quota 를 초과하는 할당이 벌어질 수 있어요.

### (3) 작은 예시 (자물쇠 없는 경우)

```
초기: g_used = 3GB, quota = 4GB

스레드 A: g_used(3GB) + 1.5GB = 4.5GB > 4GB? ... 를 읽는 순간
스레드 B: g_used(3GB) + 1.5GB = 4.5GB > 4GB? ... 도 같은 3GB 를 읽음
```
락이 없으면 둘 다 3GB 기준으로 판단해서, 합쳐서 6GB 를 할당해버릴 수 있어요.

### (4) 한 줄 요약
> race condition = 여러 스레드가 공유 변수를 동시에 읽고 쓰면서 "확인 → 갱신" 사이에 끼어들어 값이 망가지는 것. 은행 동시 출금처럼.

---

## 6.2 pthread_mutex — 한 번에 한 명만 들어가는 자물쇠

### (1) 왜 필요한가

race condition 을 막으려면, "잔액 확인부터 출금까지" 를 **쪼개지지 않는 한 덩어리** 로 만들어야 해요. 즉 한 스레드가 이 구간에 들어가면 다른 스레드는 끝날 때까지 문 앞에서 기다리게 해야죠. 그 문지기가 **mutex(뮤텍스, mutual exclusion = 상호 배제)** 예요.

### (2) 일상 비유

**1인용 화장실** 이에요. 들어가면서 문을 잠그고(`lock`), 볼일 다 보면 문을 열어요(`unlock`). 잠겨 있는 동안 다른 사람은 밖에서 기다려요. 한 번에 딱 한 명만 안에서 일할 수 있으니, 안에서 뭘 하든 안전해요.

### (3) 작은 예시

```c
pthread_mutex_lock(&g_lock);      // 문 잠금 (다른 스레드는 여기서 대기)
    // ── 여기 안은 한 번에 한 스레드만 ──
    if (g_used + size > g_quota) { ... }   // 확인
    g_used += size;                        // 갱신
    // ─────────────────────────────────
pthread_mutex_unlock(&g_lock);    // 문 열기
```

이제 "확인 → 갱신" 이 한 덩어리라 은행 문제가 안 생겨요.

### (4) fgpu_hook.c 실제 코드에서 보기
- [fgpu_hook.c:127](../../hook/src/fgpu_hook.c#L127) — `static pthread_mutex_t g_lock = PTHREAD_MUTEX_INITIALIZER;` : 자물쇠 자체. `g_used`/`g_quota`/`g_allocs` 를 보호해요.
- [fgpu_hook.c:450](../../hook/src/fgpu_hook.c#L450) — `cudaMalloc` 훅이 진입 직후 `pthread_mutex_lock(&g_lock)`.
- [fgpu_hook.c:477](../../hook/src/fgpu_hook.c#L477) — 정상 종료 시 `pthread_mutex_unlock(&g_lock)`.
- [fgpu_hook.c:459](../../hook/src/fgpu_hook.c#L459) — **DENY 로 빠지는 경로에서도** `unlock` 을 잊지 않아요. 이게 진짜 중요해요 (아래 함정 참고).

### (5) `_locked` 접미사 관례

우리 프로젝트에는 이름이 `_locked` 로 끝나는 함수들이 있어요:
- [fgpu_hook.c:295](../../hook/src/fgpu_hook.c#L295) — `fgpu_init_locked`
- [fgpu_hook.c:402](../../hook/src/fgpu_hook.c#L402) — `compute_quota_if_needed_locked`
- `track_alloc`([fgpu_hook.c:238](../../hook/src/fgpu_hook.c#L238)) / `pop_alloc`([fgpu_hook.c:257](../../hook/src/fgpu_hook.c#L257)) 도 락 안에서만 부르는 함수예요.

`_locked` 는 **"이 함수를 부르는 사람이 이미 `g_lock` 을 쥐고 있다고 가정한다"** 는 약속이에요. 그래서 이 함수들 안에서는 **락을 다시 걸면 안 돼요.** 만약 안에서 또 `pthread_mutex_lock(&g_lock)` 을 하면, 이미 자기가 잠근 문을 자기가 또 열려고 기다리는 꼴이 돼서 영원히 멈춰요(**데드락, deadlock**). 화장실에 이미 내가 들어와 있는데, 밖에서 문 열리기를 나 자신이 기다리는 상황이에요.

### (5-bis) 흔한 함정
- **어느 한 return 경로에서 `unlock` 을 빼먹으면** 그 자물쇠는 영영 잠긴 채로 남아, 그다음 모든 스레드가 `lock` 에서 멈춰버려요(프로그램 정지). 그래서 우리 코드는 DENY 경로([fgpu_hook.c:459](../../hook/src/fgpu_hook.c#L459)), 심볼 미해석 실패 경로([fgpu_hook.c:555](../../hook/src/fgpu_hook.c#L555)) 등 **모든** 갈림길에서 반드시 unlock 을 해요.
- **`_locked` 함수 안에서 락을 또 걸면 데드락.** 관례를 반드시 지키세요.

### (6) 한 줄 요약
> mutex = 한 번에 한 스레드만 들어가는 1인용 화장실. `lock`~`unlock` 사이를 한 덩어리로 만들어 race 를 막고, `_locked` 접미사는 "호출자가 이미 락을 쥐었다" 는 약속.

---

## 6.3 __thread g_in_hook — 이중 과금을 막는 재진입 가드

### (1) 왜 필요한가

이건 mutex 와는 **다른 문제** 예요. mutex 는 "서로 다른 스레드끼리의 충돌" 을 막아요. 하지만 여기엔 "**한 스레드가 자기 훅 안에서 또 자기 훅으로 들어오는**" 문제가 있어요.

무슨 소리냐면: 사용자가 `cudaMalloc` 을 한 번 불렀어요. 우리 훅이 진짜 `cudaMalloc` 을 부르는데, 그 진짜 `cudaMalloc` 이 내부적으로 **`cuMemAlloc_v2`(driver API)를 또 불러요.** 그런데 `cuMemAlloc_v2` 도 우리가 후킹했죠! 그러면 사용자의 **한 번** 할당이 우리 훅을 **두 번** 통과하면서 `g_used` 에 크기가 **두 배** 로 더해질 수 있어요. 이게 **이중 과금(double counting)** 이에요.

### (2) 일상 비유

톨게이트를 생각해보세요. 차 한 대가 고속도로에 들어갈 때 통행료를 한 번만 내야 해요. 그런데 정문 톨게이트(cudaMalloc)를 지난 차가 내부 톨게이트(cuMemAlloc_v2)를 또 지나면서 요금이 두 번 부과되면 억울하죠. 그래서 "이미 요금 낸 차예요" 라는 **스티커(g_in_hook)** 를 붙여둬요. 두 번째 톨게이트는 스티커가 붙은 차를 보면 "아, 이미 냈네" 하고 그냥 통과시켜요(요금 안 매김).

### (3) 작은 예시 — 동작 패턴

```c
if (g_in_hook) {                 // 이미 스티커 붙음? → 요금 안 매기고 그냥 통과
    return real_cudaMalloc(...);
}
g_in_hook = 1;                   // 스티커 붙이기
    ... 락 + quota 검사 + g_used 갱신 (요금 부과) ...
g_in_hook = 0;                   // 스티커 떼기 (다음 호출을 위해)
```

첫 진입(외부에서 들어옴)에서는 `g_in_hook == 0` 이라 정상적으로 요금(quota 카운트)을 매겨요. 그 안에서 재진입하면 `g_in_hook == 1` 이라 카운트를 건너뛰고 진짜 함수만 위임해요.

### (4) fgpu_hook.c 실제 코드에서 보기
- [fgpu_hook.c:154](../../hook/src/fgpu_hook.c#L154) — `static __thread int g_in_hook = 0;` : 재진입 플래그 선언.
- [fgpu_hook.c:445](../../hook/src/fgpu_hook.c#L445) — `cudaMalloc` 훅 맨 앞의 `if (g_in_hook) return real_cudaMalloc(...)` (재진입이면 위임만).
- [fgpu_hook.c:449](../../hook/src/fgpu_hook.c#L449) — `g_in_hook = 1;` (스티커 붙이기).
- [fgpu_hook.c:478](../../hook/src/fgpu_hook.c#L478) — 정상 종료 시 `g_in_hook = 0;` (스티커 떼기).
- [fgpu_hook.c:460](../../hook/src/fgpu_hook.c#L460) — **DENY 경로에서도** unlock 다음에 `g_in_hook = 0;` 을 한 뒤 return 해요. (과거에 이걸 "빠졌다" 고 오해한 분석이 있었는데, 실제 코드엔 정확히 들어가 있어요. 모든 return 경로에서 스티커를 떼는 게 철칙입니다.)

같은 패턴이 driver 훅([fgpu_hook.c:541](../../hook/src/fgpu_hook.c#L541)), VMM 훅([fgpu_hook.c:648](../../hook/src/fgpu_hook.c#L648)), launch 훅([fgpu_hook.c:752](../../hook/src/fgpu_hook.c#L752))에도 똑같이 들어 있어요.

### (5) 왜 `__thread`(thread-local)여야 하나?

`__thread` 는 "이 변수를 **스레드마다 각자 따로** 갖게 하라" 는 키워드예요. 이게 왜 중요할까요?

만약 `g_in_hook` 이 모든 스레드가 공유하는 보통 전역 변수라면:
- 스레드 A 가 훅에 들어가서 `g_in_hook = 1` 로 만들어요.
- 그 순간 스레드 B 가 훅에 들어오는데, `g_in_hook` 이 이미 1 이니까 **B 는 자기가 재진입한 줄 착각** 하고 quota 카운트를 건너뛰어요! → B 의 할당이 누락되는 버그.

`__thread` 로 스레드마다 자기만의 `g_in_hook` 사본을 주면, A 의 스티커와 B 의 스티커가 완전히 별개예요. "재진입" 은 항상 **같은 스레드 안에서** 일어나는 일이니, 스레드별 플래그가 정확히 맞아요. 게다가 각자 자기 것만 건드리니 **이 플래그엔 락도 필요 없어요.** 관련 주석은 [fgpu_hook.c:133](../../hook/src/fgpu_hook.c#L133) 블록에 자세히 있어요.

### (5-bis) 흔한 함정
- **어느 한 return 경로에서 `g_in_hook = 0` 을 빼먹으면**, 그 스레드는 스티커를 붙인 채로 훅을 나가버려요. 그러면 그 스레드의 **이후 모든 훅 호출이 "재진입" 으로 오인되어 영영 quota 카운트를 건너뜁니다.** 그 스레드에 한해 후킹이 죽는 거예요. 그래서 unlock 과 마찬가지로 모든 갈림길에서 반드시 리셋해요.

### (6) 한 줄 요약
> `__thread g_in_hook` = 한 번의 사용자 할당이 여러 훅 층을 통과해도 quota 를 딱 한 번만 세게 하는 "요금 냈어요" 스티커. 스레드마다 따로라 락 없이 안전하고, 모든 return 에서 떼야 한다.

---

## 6.4 launch 카운터/스로틀은 왜 락 대신 atomic 을 쓰나

### (1) 왜 중요한가

`cudaLaunchKernel` 은 PyTorch 같은 프레임워크가 **초당 수천 번** 부르는 매우 뜨거운 경로(hot path)예요. 만약 매 호출마다 `pthread_mutex_lock`/`unlock` 을 하면, 그 자물쇠 여닫는 비용이 쌓여서 프로그램 전체가 느려져요. 여기선 그냥 "카운터 하나 +1" 하는 게 전부라, 무거운 자물쇠 대신 **더 가벼운 도구** 를 써요.

### (2) 일상 비유

문 여닫는 화장실(mutex)은 "안에서 여러 작업을 안전하게 해야 할 때" 쓰는 거예요. 그런데 "출입 인원수 카운터 +1" 처럼 **딱 한 동작** 만 안전하면 될 때는, 문 잠글 것 없이 **자동 계수기 버튼** 하나면 충분해요. 여러 명이 동시에 눌러도 계수기가 알아서 정확히 세줘요. 이 "동시에 눌러도 안 깨지는 한 방 연산" 이 **atomic(원자적) 연산** 이에요.

### (3) 작은 예시

```c
// 락 없이, 한 방에 안전하게 +1
size_t count = __atomic_add_fetch(&g_launch_count, 1, __ATOMIC_RELAXED);
```

`__atomic_add_fetch` 는 "읽기 → +1 → 쓰기" 를 **쪼개질 수 없는 한 동작** 으로 CPU 가 보장해줘요. 그래서 6.1 의 은행 문제가 애초에 안 생겨요. `__ATOMIC_RELAXED` 는 "단순 카운터라 순서 보장까지는 필요 없다, 값만 정확하면 된다" 는 가장 가벼운 옵션이에요.

### (4) fgpu_hook.c 실제 코드에서 보기
- [fgpu_hook.c:773](../../hook/src/fgpu_hook.c#L773) — `size_t count = __atomic_add_fetch(&g_launch_count, 1, __ATOMIC_RELAXED);` : 락 없는 카운터 증가.
- [fgpu_hook.c:742](../../hook/src/fgpu_hook.c#L742) — "Lock 안 잡는 이유: PyTorch 가 launch 를 초당 수천 회 호출하므로 mutex 가 hot path 가 됨" 이라는 주석.
- [fgpu_hook.c:784](../../hook/src/fgpu_hook.c#L784), [786](../../hook/src/fgpu_hook.c#L786) — 스로틀의 윈도우 시작 시각 `g_window_start_ns` 도 `__atomic_load_n`/`__atomic_store_n` 으로 락 없이 읽고 써요.
- [fgpu_hook.c:200](../../hook/src/fgpu_hook.c#L200) — atexit 요약도 `__atomic_load_n` 으로 카운터를 읽어요.

### (5) 그럼 alloc 은 왜 atomic 을 안 쓰고 mutex 를 쓰나?

좋은 질문이에요. `cudaMalloc` 은 단순히 카운터 +1 이 아니라 **여러 가지를 한 덩어리로** 해야 해요: ① quota 검사 → ② 진짜 할당 → ③ `g_used` 증가 → ④ 연결 리스트(`g_allocs`)에 노드 추가. 이 여러 단계 전체가 "쪼개지면 안 되는 한 덩어리" 라, atomic 하나로는 못 묶어요. 그래서 mutex 가 필요해요. 반면 launch 카운터는 진짜로 "숫자 +1" 하나뿐이라 atomic 이 딱 맞아요.

정리: **여러 동작을 묶어야 하면 mutex, 단일 숫자 연산이면 atomic.**

### (5-bis) 흔한 함정
- **launch 카운터를 mutex 뒤에 넣지 마세요.** hot path 라서 성능이 무너져요. 우리 관례상 이건 반드시 lock-free `__atomic` 이어야 해요.
- 반대로 **`g_used` 를 atomic 하나로 바꾸려는 유혹** 도 위험해요. quota 검사와 갱신이 분리되면 6.1 의 race 가 다시 살아나요.

### (6) 한 줄 요약
> 초당 수천 번 불리는 launch 카운터는 "숫자 +1" 하나뿐 → 무거운 mutex 대신 lock-free `__atomic` 으로. 여러 동작을 묶어야 하는 alloc 은 여전히 mutex.

---

## ✍️ 스스로 점검

1. 은행 동시 출금 예시로, 자물쇠(mutex)가 없을 때 `g_used + size > g_quota` 검사가 왜 틀릴 수 있는지 설명해보세요.
2. 사용자가 `cudaMalloc` 을 한 번 불렀는데, `g_in_hook` 이 없으면 왜 `g_used` 가 두 배로 더해질 수 있나요? 그리고 `g_in_hook` 이 왜 `__thread`(스레드마다 따로)여야 하나요?
3. launch 카운터는 `__atomic` 을 쓰는데 `cudaMalloc` 은 mutex 를 씁니다. 이 둘의 차이를 결정하는 기준은 무엇인가요?

## 🎯 다음 챕터

여기까지가 훅의 핵심 3부작(4~6장)이에요. 이제 여러분은 **가로채기(LD_PRELOAD/dlsym) → 계층별 quota 판단과 스로틀 → 스레드 안전성** 이라는 이 프로젝트의 심장을 전부 이해했어요. 다음 장부터는 이 훅을 감싸는 바깥층 — 도커 런타임 이미지, FastAPI 백엔드 세션 관리, 어드미션 컨트롤(sum-of-ratios ≤ 1.0) — 로 넘어갑니다. 훅이 "컨테이너 한 개의 quota" 를 담당했다면, 다음 층은 "여러 컨테이너를 GPU 한 장에 어떻게 공평하게 배치하는가" 를 담당해요.

---

⟵ [이전: 5장. 훅 코드 완전 분해](05-cuda-layers-and-hook-walkthrough.md) ・ [📚 전체 목차](README.md) ・ [다음: 7장. FastAPI 백엔드](07-backend-fastapi.md) ⟶
