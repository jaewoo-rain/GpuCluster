# Chapter 11 — Overhead 마이크로벤치 방법론

## 학습 목표

- `clock_gettime(CLOCK_MONOTONIC)` 이 *왜* 벤치마크의 표준 시계인지 안다.
- 평균(mean) / 중앙값(p50) / 99 분위(p99) 의 의미와 *각자 다른 정보* 를 갖는 이유를 안다.
- "워밍업(warmup)" 이 *왜* 필요한지 안다.
- 마이크로벤치를 *데이터로 받아 후처리* 하는 패턴 (CSV → summary) 을 따라할 수 있다.

---

## 11.1 시계 선택 — `CLOCK_MONOTONIC` vs 다른 것들

| 시계 | 특성 | 언제 |
|---|---|---|
| `CLOCK_REALTIME` | 벽시계 시간 (UTC 등). NTP 가 *뒤로* 점프시킬 수 있음 | 로그 timestamp 용 |
| **`CLOCK_MONOTONIC`** | 단조증가, 시스템 부팅 후 경과. 점프 없음 | **벤치 측정 표준** |
| `CLOCK_PROCESS_CPUTIME_ID` | 프로세스가 *CPU 에서 실제로* 쓴 시간 | CPU bound 작업 분석 |
| `CLOCK_THREAD_CPUTIME_ID` | 스레드 단위 CPU 시간 | 멀티스레드 분석 |
| `rdtsc` | CPU 사이클 카운터 | 매우 짧은 측정. 코어/주파수 변동 주의 |

`CLOCK_MONOTONIC` 이 벤치 표준인 이유:
- *과거로 점프하지 않음* — `t1 - t0` 이 음수가 되지 않음.
- 나노초 정밀도.
- POSIX 표준이라 이식성 좋음.

[hook/tests/bench_alloc.cu](../../hook/tests/bench_alloc.cu) 가 이 시계를 사용해 `cudaMalloc/cudaFree` 를 측정합니다.

```c
#include <time.h>

struct timespec t0, t1;
clock_gettime(CLOCK_MONOTONIC, &t0);
cudaMalloc(&p, size);
clock_gettime(CLOCK_MONOTONIC, &t1);
long ns = (t1.tv_sec - t0.tv_sec) * 1000000000L + (t1.tv_nsec - t0.tv_nsec);
```

### 더 공부하려면
- `man 3 clock_gettime` ([man7.org](https://man7.org/linux/man-pages/man3/clock_gettime.3.html))
- [LWN — Time, clocks, and ordering](https://lwn.net/Articles/761739/)

---

## 11.2 통계량 — mean / median / p99

같은 횟수의 측정에서 다른 정보를 줍니다.

### 예: 100 회 측정값 (μs 단위)

```
[5, 5, 5, 5, 5, 5, 5, 5, 5, ..., 5, 5, 200]
```

99 개는 5 μs, 마지막 한 번이 200 μs.

| 통계량 | 값 |
|---|---|
| mean | (99×5 + 200) / 100 = 6.95 μs |
| median (p50) | 5 μs (정렬 후 50번째) |
| p99 | 5 μs (99번째까지는 5) |
| p99.9 | 200 μs (꼬리에 잡힘) |

해석:
- **mean** 이 가장 익숙하지만 *outlier 에 약함*. 한 번의 spike 에 평균이 끌려감.
- **median (p50)** 은 *전형적 케이스*. 중앙에 있는 값.
- **p99** 는 *꼬리 latency*. SLA / 사용자 체감 측정 시 중요.

벤치에서 셋 다 보고하는 이유: 각각 다른 질문에 답함.

### 더 공부하려면
- [Gil Tene — How NOT to Measure Latency](https://www.youtube.com/watch?v=lJ8ydIuPFeU) — 1시간 영상이지만 인생 경험
- [HdrHistogram](http://hdrhistogram.org/) — Tene 가 만든 latency 분포 측정 라이브러리

---

## 11.3 워밍업 — 첫 호출이 느린 이유

`cudaMalloc` 의 첫 호출은 보통 *수십 배* 느립니다. 이유:
- CUDA context lazy init.
- 드라이버가 PCI 디바이스 와 첫 통신.
- 페이지 테이블 setup.

이 첫 호출이 측정에 들어가면 mean/p99 가 다 망가져요. 해결: **워밍업 사이클** 을 측정 *전에* 돌려서 버립니다.

[bench_alloc.cu](../../hook/tests/bench_alloc.cu) 의 `BENCH_WARMUP` 변수 (기본 5):

```
for (i = 0; i < BENCH_WARMUP; i++) {
    cudaMalloc + cudaFree    // 결과 안 씀
}
for (i = 0; i < BENCH_N; i++) {
    측정 + 출력
}
```

워밍업 횟수는 워크로드에 따라 달라요. CUDA 의 경우 5~10 정도면 충분.

---

## 11.4 측정 결과를 *데이터로* 출력 — CSV first

[bench_alloc.cu](../../hook/tests/bench_alloc.cu) 는 *raw 측정값* 을 CSV 로 stdout 에 출력합니다:

```
size_mib,iter,malloc_ns,free_ns
16,0,4521,1230
16,1,4302,1145
...
```

운영자 친화 표 (mean / p99 / Δ%) 를 *바이너리에서* 만들지 않는 이유:
- 다양한 통계량이 필요할 수 있음 (p50, p95, p99, p99.9, max).
- 시각화 (히스토그램, CDF) 에 raw 가 필요.
- 후처리 시 다른 메타데이터 (cgroup info, GPU temperature) 와 join.

[scripts/eval/run_overhead.sh](../../scripts/eval/run_overhead.sh) 가 baseline / hooked 두 번 돌려 두 CSV 를 만들고, Python 후처리로 `summary.csv` + `summary.txt` (markdown 표) 를 생성. 논문 표는 그대로 paste.

### 패턴: "raw → 후처리 → 보고서"

```
[측정 코드]   가장 단순. raw 만 출력
     ↓
[parser]      Python / awk
     ↓
[summary]     mean / p50 / p99 + Δ%
     ↓
[plot]        gnuplot / matplotlib
```

각 단계가 독립이라 한 단계만 고쳐도 다른 게 안 깨짐.

---

## 11.5 baseline vs hooked — 차이 분리

논문에서 "hook 의 overhead 는 X μs" 라고 말하려면, hook 외의 모든 변수를 통제해야 합니다.

| 변수 | 통제 방법 |
|---|---|
| GPU 자체 가변성 | 같은 머신, 같은 시간대 |
| 컨테이너 시작 비용 | bench 가 *수백 회* iter 라 amortize |
| 다른 프로세스 간섭 | 측정 시 nvidia-smi 로 다른 사용자 없음 확인 |
| caching / 워밍업 | warmup 사이클 + iteration 충분히 많이 |
| measurement 자체 비용 | `clock_gettime` 자체 ~50 ns — 측정 대상이 μs 단위라 무시 가능 |

run_overhead.sh 가 baseline (no LD_PRELOAD) 와 hooked (`FGPU_RATIO=0.95`, 양보 큼) 을 *연속해서* 돌리고 같은 size 끼리 비교 → Δ% 에서 hook 의 overhead 만 추출.

### `FGPU_RATIO=0.95` 이유

ratio 를 *너무 작게* 잡으면 (예: 0.1) 첫 번째 alloc 만 ALLOW 되고 나머지는 모두 DENY → hook 의 *quota 검사* 비용만 측정하고 *진짜 cudaMalloc* 비용은 못 측정. 0.95 로 두면 quota 안에서 모든 호출이 진짜 함수까지 가서 둘 다 포함된 latency 측정 가능.

---

## 11.6 결과 해석 — 무엇이 정상인가

이런 표가 나옵니다:

| size (MiB) | baseline mean μs | hooked mean μs | Δ mean % |
|---|---|---|---|
| 16 | 4.5 | 6.2 | +37.8% |
| 64 | 6.1 | 7.9 | +29.5% |
| 256 | 28.4 | 30.3 | +6.7% |
| 1024 | 102.1 | 104.0 | +1.9% |

읽기:
- **작은 size 에서 overhead %가 큼**: 진짜 `cudaMalloc` 자체가 빠르니 hook 의 고정비용 (mutex, dlsym, fprintf) 이 비율로 크게 보임.
- **큰 size 에서 overhead %가 작음**: 진짜 `cudaMalloc` 이 비싸지니 hook 비용은 amortize.
- **절대값** 으로 보면 모든 size 에서 hook 의 추가 비용은 ~1.5~2 μs 정도 — 일정.

논문 결론 후보: "Hook 의 추가 비용은 size 무관 약 1.5~2 μs. PyTorch 같은 *평균 alloc 크기 큰* 워크로드에선 무시 가능 (% 한 자릿수)."

---

## 11.7 직접 해보기

```bash
./scripts/build_image.sh         # bench_alloc 이 들어간 이미지

./scripts/eval/run_overhead.sh
ls experiments/overhead_*/
# baseline_raw.csv  hooked_raw.csv  summary.csv  summary.txt

cat experiments/overhead_*/summary.txt
```

이 markdown 표를 그대로 논문 §9.4 에 넣을 수 있어요.

### 더 의미 있는 sweep

```bash
BENCH_N=200 BENCH_SIZES_MIB=1,4,16,64,256,1024,4096 \
    ./scripts/eval/run_overhead.sh
```

- iter 수 ↑ → 통계 안정.
- 작은 size (1 MiB) 까지 sweep → hook 의 *고정비용* 이 더 잘 보임.

---

## 11.8 실수하기 쉬운 점

| 실수 | 무엇이 잘못됐나 |
|---|---|
| 첫 iter 부터 측정 | warmup 안 함 — outlier가 mean 망가뜨림 |
| `printf` 를 측정 안에 둠 | I/O 가 하이퍼볼릭하게 들어감 |
| 한 번만 측정 | 분산 0 — 통계 의미 없음. 최소 100회 |
| mean 만 보고 | tail 못 봄. p99 도 같이 |
| 한 size 만 측정 | 여러 size sweep 해서 *변화* 를 봐야 |
| `time` 명령으로 측정 | 사용자 + 시스템 시간만 — sub-μs 정밀도 부족 |

---

## 자가점검 질문

1. `CLOCK_MONOTONIC` 과 `CLOCK_REALTIME` 의 차이를 NTP 관점에서 설명하라.
2. p99 가 mean 보다 훨씬 큰 결과가 나왔다. 이게 의미하는 것은?
3. 워밍업 사이클을 안 돌리면 측정 결과의 어떤 통계량이 가장 왜곡되는가?
4. 우리 벤치가 `FGPU_RATIO=0.95` 를 쓰는 이유는?
5. baseline 과 hooked 측정을 *동시에* 돌리지 않고 *연속해서* 돌리는 이유는?

→ [Chapter 12: cudaLaunchKernel 모니터링](12-launch-monitoring.md)

---

## 외부 자료 종합

- 🎥 [Gil Tene — How NOT to Measure Latency](https://www.youtube.com/watch?v=lJ8ydIuPFeU) — *반드시* 한 번
- 📄 [Brendan Gregg — Latency Heatmaps](https://www.brendangregg.com/HeatMaps/latency.html)
- 📚 [perf 도구](https://perf.wiki.kernel.org/index.php/Main_Page) — 본격 프로파일링이 필요할 때
- 📖 *Systems Performance* by Brendan Gregg — 책 한 권. Linux 성능 분석의 정석
