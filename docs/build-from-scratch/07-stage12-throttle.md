# 7장. Stage 12 — Duty-cycle 컴퓨트 스로틀 구현

## 이 장에서 만들 것

- 5장에서 만든 `cudaLaunchKernel` 카운터 **바로 옆에** 시간 기반 throttle 을 얹습니다.
- 시간 윈도우(기본 100ms) 안에서 `compute_ratio × window` 만큼만 launch 를 즉시 통과시키고, 초과분은 `nanosleep` 으로 대기시킵니다. **launch 는 절대 드롭하지 않습니다** — 지연만 넣습니다.
- 결과적으로 컴퓨트 throughput 이 `FGPU_COMPUTE_RATIO` 에 비례하게 됩니다(예: 0.4 → baseline 의 약 40%).
- `test_throttle.cu` 로 throughput 이 ratio 에 비례하는지 검증하고, `nanosleep` 정밀도 같은 함정을 짚습니다.

> 전제: 5장의 `cudaLaunchKernel` 훅(카운터 + `atexit` 요약)이 완성돼 있습니다. 이 장은 그 훅 안에 로직 한 블록을 끼워넣는 작업입니다.

---

## 목표: "무엇이 되면 성공인가"

두 컨테이너가 같은 noop 커널을 최대 속도로 launch 하는데, 하나는 `FGPU_COMPUTE_RATIO=0.3`, 다른 하나는 `0.6` 이라면, 측정된 launches/sec 비율이 대략 0.3 : 0.6 = 1 : 2 로 벌어져야 합니다. 즉 **wall-clock 시간의 일부만 launch 를 흘려보내고 나머지는 재우는** 협력적(cooperative) 시분할입니다.

명심할 한계(진짜 SM 격리가 아님):
- sleep 중에 다른 컨테이너가 GPU 를 안 쓰면 GPU 는 그냥 idle 입니다. work-conserving 이 아닙니다.
- 커널 실행 시간을 반영하지 못합니다 — 100ms heavy 커널 1회와 1μs noop 1000회를 구분 못 합니다.

이건 버그가 아니라 문서화된 설계 범위입니다. 우리가 파는 가치는 "hook 하나로 memory quota(Stage 1-6) + compute time-slice(Stage 12) 두 축을 다 구현" 입니다.

---

## 개발 순서 체크리스트

1. 환경변수 파싱 (`FGPU_THROTTLE_ENABLE` / `COMPUTE_RATIO` / `WINDOW_MS`) 을 `fgpu_init_locked` 에 추가
2. throttle 상태 전역(atomic 윈도우 시작 시각 등) 선언
3. launch 훅 안에서 elapsed vs active_limit 계산
4. 초과 시 `nanosleep` 으로 지연 (드롭 아님) + 윈도우 리셋
5. throttle 카운터/로그 + `atexit` 요약에 합류
6. `test_throttle.cu` 로 throughput ∝ ratio 검증

---

## 스텝 1 — 환경변수 파싱

먼저 "throttle 을 켤지, 얼마나, 어떤 윈도우로" 를 읽습니다. `fgpu_init_locked` 안, 기존 `FGPU_LAUNCH_LOG_EVERY` 파싱 다음에 붙입니다([fgpu_hook.c:341](../../hook/src/fgpu_hook.c#L341)):

```c
const char *te_env = getenv("FGPU_THROTTLE_ENABLE");
if (te_env) g_throttle_enable = (atoi(te_env) != 0);

const char *cr_env = getenv("FGPU_COMPUTE_RATIO");
if (cr_env) {
    g_compute_ratio = atof(cr_env);
    if (g_compute_ratio <= 0.0 || g_compute_ratio > 1.0)
        g_compute_ratio = 1.0;
} else {
    /* 미설정 → 메모리 ratio(g_ratio) 재사용 */
    g_compute_ratio = g_ratio;
}

const char *wm_env = getenv("FGPU_WINDOW_MS");
if (wm_env) {
    long wm = strtol(wm_env, NULL, 10);
    if (wm > 0) g_window_ns = (int64_t)wm * 1000000LL;
}
```

> **설계 결정 하나:** `FGPU_COMPUTE_RATIO` 를 안 주면 메모리 `FGPU_RATIO` 를 그대로 씁니다([fgpu_hook.c:355](../../hook/src/fgpu_hook.c#L355)). "40% 메모리 준 세션은 대략 40% 컴퓨트도" 라는 합리적 기본값이라, 운영자가 변수 하나만 관리해도 됩니다.

`init` 로그에 throttle 상태를 한 줄 추가해서 켜졌는지 눈으로 보이게 합니다([fgpu_hook.c:393](../../hook/src/fgpu_hook.c#L393)):

```c
fprintf(stderr, "[fgpu] init: throttle=%s compute_ratio=%.3f window_ms=%ld\n",
        g_throttle_enable ? "on" : "off",
        g_compute_ratio, (long)(g_window_ns / 1000000LL));
```

> **여기서 실행해서 확인하세요 (게이트 1)**
> `FGPU_THROTTLE_ENABLE=1 FGPU_COMPUTE_RATIO=0.4` 로 아무거나 얹어, `[fgpu] init: throttle=on compute_ratio=0.400 window_ms=100` 이 뜨는지 확인. 변수 안 주면 `throttle=off`. 아직 throttle 동작은 없고 파싱만 확인하는 단계입니다.

---

## 스텝 2 — throttle 상태 전역 (atomic 윈도우)

윈도우 상태를 전역으로 둡니다([fgpu_hook.c:190](../../hook/src/fgpu_hook.c#L190)):

```c
static int          g_throttle_enable    = 0;
static double       g_compute_ratio      = 1.0;
static int64_t      g_window_ns          = 100000000LL;  /* 100ms */
static int64_t      g_window_start_ns    = 0;            /* 현재 윈도우 시작 시각 */
static size_t       g_throttle_count     = 0;            /* 총 sleep 횟수 (통계) */
static unsigned int g_throttle_log_every = 100;
```

> **왜 `g_window_start_ns` 를 mutex 로 안 감싸고 atomic 으로 두는가?** launch 훅은 5장에서 lock-free 로 만들었습니다. throttle 도 그 hot path 위에 얹히므로 mutex 를 걸면 안 됩니다. 두 스레드가 동시에 윈도우를 리셋해도 **둘 다 "지금"** 으로 설정하므로 결과가 무해합니다 — 그래서 `__atomic_store_n`(RELAXED)만으로 충분합니다([fgpu_hook.c:184](../../hook/src/fgpu_hook.c#L184) 주석).

---

## 스텝 3~4 — 윈도우 안에서 계산하고 재우기

이제 핵심입니다. 100ms 윈도우를 시간축 그림으로 보면:

```
윈도우 (100ms), compute_ratio = 0.4  →  active_limit = 40ms
|<---- active 40ms ---->|<-------- sleep 60ms -------->|
 launch 즉시 통과        active 초과분은 다음 윈도우까지 재움
0ms                    40ms                          100ms
```

- 윈도우 시작~40ms(=`active_limit`): launch 를 **즉시** 통과.
- 40ms 이후: 그 윈도우의 남은 시간(60ms)만큼 `nanosleep` 으로 재운 뒤, 새 윈도우를 연다.

이걸 launch 훅 안, 카운터 증가 **다음, 진짜 launch 호출 앞**에 끼워넣습니다([fgpu_hook.c:779](../../hook/src/fgpu_hook.c#L779)):

```c
if (g_throttle_enable && g_compute_ratio < 1.0) {
    struct timespec now_ts;
    clock_gettime(CLOCK_MONOTONIC, &now_ts);
    int64_t now_ns = (int64_t)now_ts.tv_sec * 1000000000LL + now_ts.tv_nsec;

    int64_t ws = __atomic_load_n(&g_window_start_ns, __ATOMIC_RELAXED);
    if (ws == 0) {                       /* 첫 호출 → 첫 윈도우 시작 */
        __atomic_store_n(&g_window_start_ns, now_ns, __ATOMIC_RELAXED);
        ws = now_ns;
    }

    int64_t elapsed = now_ns - ws;
    if (elapsed >= g_window_ns) {        /* 윈도우 만료 → 리셋 */
        __atomic_store_n(&g_window_start_ns, now_ns, __ATOMIC_RELAXED);
        elapsed = 0;
    }

    int64_t active_limit = (int64_t)(g_compute_ratio * (double)g_window_ns);
    if (elapsed >= active_limit) {       /* active 시간 초과 → sleep */
        int64_t sleep_ns = g_window_ns - elapsed;
        struct timespec sleep_ts = {
            .tv_sec  = sleep_ns / 1000000000LL,
            .tv_nsec = sleep_ns % 1000000000LL,
        };
        nanosleep(&sleep_ts, NULL);      /* ← 드롭이 아니라 지연 */

        /* sleep 후 새 윈도우 시작 */
        clock_gettime(CLOCK_MONOTONIC, &now_ts);
        __atomic_store_n(&g_window_start_ns,
            (int64_t)now_ts.tv_sec * 1000000000LL + now_ts.tv_nsec,
            __ATOMIC_RELAXED);

        size_t tc = __atomic_add_fetch(&g_throttle_count, 1, __ATOMIC_RELAXED);
        if (g_throttle_log_every > 0 && (tc % g_throttle_log_every) == 0)
            fprintf(stderr, "[fgpu] THROTTLE sleep=%ldms count=%zu\n",
                    (long)(sleep_ns / 1000000L), tc);
    }
}

/* 그리고 나서 — 무슨 일이 있어도 진짜 launch 는 호출된다. */
cudaError_t err = real_cudaLaunchKernel(func, gridDim, blockDim,
                                        args, sharedMem, stream);
```

핵심을 다시 짚으면:

- **`g_compute_ratio < 1.0` 일 때만** 동작. 1.0 이면 throttle 이 의미 없으니 아예 시간 측정도 건너뜁니다 — throttle off 나 ratio=1.0 인 세션에 overhead 를 안 얹습니다.
- **launch 를 드롭하지 않습니다.** sleep 은 launch **앞**에 넣고, `real_cudaLaunchKernel` 은 항상 불립니다. 즉 커널 실행 횟수는 그대로고 **속도만** 눌립니다. 5장에서 커널 atomics = N 을 검증했듯, throttle 켜도 이 등식은 유지돼야 합니다.
- **`CLOCK_MONOTONIC`** 을 씁니다. wall-clock(`CLOCK_REALTIME`)은 NTP 등으로 뒤로 갈 수 있어 elapsed 가 음수가 될 위험이 있습니다.

---

## 스텝 5 — `atexit` 요약에 합류

5장에서 만든 `fgpu_launch_atexit_dump` 에 throttle 통계를 덧붙입니다([fgpu_hook.c:204](../../hook/src/fgpu_hook.c#L204)):

```c
static void fgpu_launch_atexit_dump(void) {
    size_t n = __atomic_load_n(&g_launch_count, __ATOMIC_RELAXED);
    fprintf(stderr, "[fgpu] exit summary: total cudaLaunchKernel = %zu\n", n);
    if (g_throttle_enable) {
        size_t tc = __atomic_load_n(&g_throttle_count, __ATOMIC_RELAXED);
        fprintf(stderr, "[fgpu] exit summary: total throttle sleeps = %zu\n", tc);
    }
}
```

> **여기서 실행해서 확인하세요 (게이트 2)**
> `FGPU_THROTTLE_ENABLE=1 FGPU_COMPUTE_RATIO=0.4` 로 launch 를 많이 하는 프로그램을 얹으면:
> - `[fgpu] THROTTLE sleep=NNms count=...` 라인이 주기적으로 나온다.
> - 종료 시 `[fgpu] exit summary: total throttle sleeps = ...` 가 non-zero.
> throttle off 로 돌리면 `THROTTLE` 라인이 하나도 없고 throughput 이 baseline 과 비슷해야 합니다.

---

## 스텝 6 — `test_throttle.cu` 로 비례성 검증

검증 전략은 단순합니다: **noop 커널을 tight loop 로 N 번(기본 5000) launch 하고 wall-clock 을 재서 launches/sec 를 뽑는다.** ratio 를 바꿔가며 이 수치가 비례하는지 봅니다.

완성본 [test_throttle.cu](../../hook/tests/test_throttle.cu) 는 `clock_gettime(CLOCK_MONOTONIC)` 으로 루프 앞뒤를 재고([test_throttle.cu:59](../../hook/tests/test_throttle.cu#L59)) `[test-throttle] n=... elapsed_ms=... launches_per_sec=... kernel_atomics=...` 를 stdout 으로 찍습니다([test_throttle.cu:73](../../hook/tests/test_throttle.cu#L73)). `kernel_atomics` 가 여전히 N 과 같으면 "throttle 이 launch 를 드롭하지 않았다" 는 확인입니다.

세 번 돌려 비교하는 게 정석입니다:

- **baseline** (훅 없음) → `[fgpu]` 라인 없음, 최고 throughput.
- **throttle OFF** (훅 있음, `FGPU_THROTTLE_ENABLE=0`) → throughput ≈ baseline. throttle overhead 가 거의 없음을 증명.
- **throttle ON** (`FGPU_COMPUTE_RATIO=0.4`) → throughput ≈ baseline × 0.4.

> **여기서 실행해서 확인하세요 (게이트 3 — 최종)**
> `test_throttle` 을 위 세 조건으로 돌려, ON 의 launches/sec 가 baseline 의 대략 40% 근처인지 확인하세요. 정량 평가 스크립트는 두 컨테이너를 다른 ratio(예 0.3 / 0.6)로 돌려 throughput 비율이 compute_ratio 비율에 tolerance(±0.15) 내로 수렴하면 PASS 로 판정합니다. tolerance 가 넉넉한 이유는 바로 아래 함정 때문입니다.

---

## 내가 겪을 함정

- **`nanosleep` 정밀도.** Linux 의 최소 sleep 은 스케줄러 tick 때문에 대략 50μs 수준입니다. 그래서 `FGPU_WINDOW_MS` 를 1ms 미만으로 잡으면 sleep 이 요청보다 훨씬 길어져 비례성이 깨집니다. 기본 100ms 윈도우는 이 오차를 흡수하려고 넉넉하게 잡은 값입니다. 윈도우를 줄이려면 정밀도 손실을 각오하세요.
- **sleep 을 진짜 launch 뒤에 넣음.** 그러면 이미 던진 커널이 GPU 에서 도는 동안 CPU 만 재우는 꼴이라 throttle 효과가 어긋납니다. 반드시 `real_cudaLaunchKernel` **앞**에서 재우세요.
- **launch 를 드롭.** `nanosleep` 후 `return` 해버리면 커널이 실행 안 됩니다. `kernel_atomics < N` 으로 바로 드러납니다. sleep 은 지연일 뿐, launch 는 항상 통과.
- **`CLOCK_REALTIME` 사용.** 시간이 뒤로 점프하면 elapsed 가 음수가 돼 로직이 깨집니다. `CLOCK_MONOTONIC` 만.
- **throttle 을 mutex 로 감쌈.** launch hot path 를 직렬화해 성능이 무너지고, 5장에서 애써 lock-free 로 만든 이유가 사라집니다. 윈도우 상태는 atomic 으로만.
- **`compute_ratio == 1.0` 인데도 시간 측정.** 매 launch 마다 `clock_gettime` 두 번은 공짜가 아닙니다. `g_compute_ratio < 1.0` 가드로 불필요한 세션은 건너뛰세요.
- **오차를 버그로 오해.** noop 커널은 워낙 짧아 launch 오버헤드가 지배적이라, 관측 비율이 정확히 0.40 이 아니라 0.35~0.45 로 흔들립니다. 이건 정상이고, 그래서 평가 tolerance 가 ±0.15 입니다.

---

## 완성 체크리스트 (눈으로 확인)

- [ ] throttle off: `[fgpu] init: ... throttle=off`, `THROTTLE` 라인 없음, throughput ≈ baseline.
- [ ] throttle on(0.4): `[fgpu] init: ... throttle=on compute_ratio=0.400 window_ms=100`.
- [ ] `[fgpu] THROTTLE sleep=NNms` 라인이 주기적으로 나온다.
- [ ] `[fgpu] exit summary: total throttle sleeps = ...` 가 non-zero.
- [ ] `test_throttle` 의 `kernel_atomics` 가 throttle 켜도 여전히 N (드롭 없음).
- [ ] ratio 0.4 의 launches/sec 가 baseline 의 대략 40%.
- [ ] 다른 ratio 쌍(0.3 / 0.6)의 throughput 비율이 ratio 비율에 수렴.

## 다음 챕터

여기까지가 hook(C) 계층의 전부입니다: memory quota(Stage 1~6) + compute duty-cycle throttle(Stage 12)을 단일 `.so` 하나로 구현했습니다. 이 hook 은 **per-container(프로세스)** 강제만 합니다 — 스케줄러 관점의 "전체 GPU 를 여러 세션이 어떻게 나눠 가질지" 는 백엔드의 admission control(Stage 11) 이 담당합니다. hook 은 런타임에 컨테이너 하나가 자기 몫을 넘는 걸 막고, admission 은 spawn 시점에 총합이 GPU 용량을 넘는 걸 막습니다 — 두 개의 독립된 강제 계층입니다. 다음 문서(백엔드 파트)에서 그 위층을 다룹니다.
