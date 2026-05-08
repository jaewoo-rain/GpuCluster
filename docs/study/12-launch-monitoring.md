# Chapter 12 — `cudaLaunchKernel` 모니터링

## 학습 목표

- `cudaLaunchKernel` 이 무엇을 하는 함수인지, 우리가 *왜* 카운트만 하고 시행은 안 하는지 안다.
- lock-free atomic counter 의 의미를 다시 한 번 (다른 각도에서) 안다.
- launch *횟수* 와 GPU *device time* 의 차이 (= 우리가 보는 것의 *한계*) 를 안다.
- "측정 가능 + 정책 가능" 모델의 의의를 설명할 수 있다.

---

## 12.1 `cudaLaunchKernel` 이 뭔가

CUDA 커널을 GPU 에 *실행 요청* 하는 함수. `<<<grid, block>>>` 문법이 컴파일 시 이 함수 호출로 lowered 됨.

```cuda
__global__ void mykernel(...) { ... }

int main() {
    mykernel<<<256, 256>>>(args...);   // ← 컴파일러가 cudaLaunchKernel 로 변환
}
```

특징:
- **비동기**. 호출 즉시 반환, 커널은 GPU 에서 별도 진행. CPU 가 결과 보려면 `cudaDeviceSynchronize` 또는 `cudaMemcpy` 등 동기화 필요.
- **빈도가 높음**. PyTorch 의 한 forward pass 에서 수십 ~ 수백 번 호출. 학습 한 step 에 수천 번.
- **메모리 변화 없음**. 실행만 요청하지 메모리 할당/해제는 안 함. 그래서 quota 와 직접 관계 X.

### 더 공부하려면
- [CUDA Runtime API — cudaLaunchKernel](https://docs.nvidia.com/cuda/cuda-runtime-api/group__CUDART__EXECUTION.html)
- [NVIDIA blog — How CUDA Programming Works](https://developer.nvidia.com/blog/how-cuda-programming-works/)

---

## 12.2 왜 카운트만 하고 *시행* 은 안 하나

후크가 *기술적으로* 할 수 있는 일과 *해야 하는* 일은 다릅니다. launch 를 거부할 수도 있지만 그러면:

1. **SM 격리 자체가 hook 영역 밖**. 진짜 SM 분배는 MIG / MPS 가 합니다. 우리 hook 이 launch 거부해도 *공정성* 보장 안 됨 (그저 사용자 코드를 망가뜨림).
2. **launch 거부의 사용자 영향이 큼**. quota 초과 alloc 거부는 *받아들일 만함* (OOM 처리 코드 흔함). 하지만 launch 실패는 거의 처리 안 됨 — 사용자 코드가 죽는다.
3. **본 프로토타입의 가치 명제**: *"측정 가능 + 정책적 스케줄러가 그 측정에 기반해 fairness 결정"* 이지 *"강제 차단"* 이 아님.

따라서 **cudaLaunchKernel 은 카운트만**, [fgpu_hook.c:684](../../hook/src/fgpu_hook.c#L684).

---

## 12.3 Lock-free atomic — 왜?

[fgpu_hook.c:708](../../hook/src/fgpu_hook.c#L708):
```c
size_t count = __atomic_add_fetch(&g_launch_count, 1, __ATOMIC_RELAXED);
```

mutex 안 잡는 이유 ([Chapter 04](04-thread-safety.md) 의 재방문):

- launch 빈도가 매우 높음 (초당 수천 번).
- mutex lock/unlock 이 hot path 가 됨.
- 우리가 보호할 상태 = 단 한 변수, 단순 +1.
- atomic 으로 충분.

`__ATOMIC_RELAXED` 의미: 원자성만 보장, 다른 메모리 작업과의 순서는 보장 X. 단순 monotonic counter 라 RELAXED 충분.

### 측정의 정확성

multi-thread 에서 모든 launch 가 카운트되나? — *예*. atomic add 는 lost update 가 없습니다. PyTorch DataLoader 가 워커 4개로 동시에 launch 해도 합계는 정확.

### 더 공부하려면
- [Preshing — Atomic Operations](https://preshing.com/20130618/atomic-vs-non-atomic-operations/)

---

## 12.4 Periodic dump — `FGPU_LAUNCH_LOG_EVERY`

```c
if (g_launch_log_every > 0 && (count % g_launch_log_every) == 0) {
    fprintf(stderr, "[fgpu] LAUNCH count=%zu (every %u)\n",
            count, g_launch_log_every);
}
```

`FGPU_LAUNCH_LOG_EVERY=K` 면 K번에 한 번씩 stderr 로 누적값을 찍음. 0이면 off.

용도:
- *진행 상황 모니터링* — 컨테이너 안에서 PyTorch 가 진짜로 launch 를 부르고 있는지 확인.
- *상관 분석* — 시간축에 carved log 를 [Chapter 13 (correlation)](../../scripts/eval/run_correlation.sh) 에서 join.
- *off 모드* (`=0`) — [Chapter 11 의 overhead 측정](11-benchmarking.md) 시 fprintf 비용을 빼고 순수 atomic 비용만 보기.

---

## 12.5 `atexit` 최종 dump

[fgpu_hook.c:175-182](../../hook/src/fgpu_hook.c#L175-L182):
```c
static void fgpu_launch_atexit_dump(void) {
    size_t n = __atomic_load_n(&g_launch_count, __ATOMIC_RELAXED);
    fprintf(stderr, "[fgpu] exit summary: total cudaLaunchKernel = %zu\n", n);
}
```

[fgpu_init_locked](../../hook/src/fgpu_hook.c#L313-L316) 에서 `atexit(fgpu_launch_atexit_dump)` 등록 — 한 번만.

`atexit` 핸들러는 정상 종료 (`main` return, `exit()`) 시 등록 역순으로 실행. **한계**:
- `_exit()` 직접 호출 시 안 불림.
- `abort()` / signal kill 시 안 불림.
- 따라서 *best-effort* — Ctrl+C 로 죽은 컨테이너는 summary 못 봄.

### 더 공부하려면
- `man 3 atexit`
- [GNU libc — Cleanups on Exit](https://www.gnu.org/software/libc/manual/html_node/Cleanups-on-Exit.html)

---

## 12.6 launch *횟수* ≠ device *시간*

이게 가장 중요한 *한계 명시*.

```
[컨테이너 A] cudaLaunchKernel 1000회   — 각 커널 1ms 실행 → 총 GPU 1초
[컨테이너 B] cudaLaunchKernel 1000회   — 각 커널 100ms 실행 → 총 GPU 100초
```

우리 카운터로는 **둘 다 1000으로 똑같이 보임**. 하지만 GPU 실 점유 시간은 100배 차이.

이는 본질적 한계로, *진짜 device time* 을 알려면 `cudaEventRecord` 를 매 launch 앞뒤에 끼워 넣어 측정해야 합니다 — 그러면:
- launch 마다 sync 발생 → 성능 폭락.
- 또는 별도 polling 스레드 → 복잡도 폭발.

따라서 본 프로토타입은:
- launch 횟수만 측정 — 가볍고 정확.
- *대략적 활동도(temporal activity) proxy* 로 사용.
- 진짜 fairness 는 후속 stage 또는 MIG/MPS 영역.

---

## 12.7 Driver API `cuLaunchKernel` 미커버

`cudaLaunchKernel` 는 Runtime API. Driver API 는 `cuLaunchKernel`. 우리는 후자를 후킹 안 함.

영향:
- PyTorch 등 대부분 ML 프레임워크 → cudart 의 `cudaLaunchKernel` 사용. 우리 카운터에 잡힘.
- 직접 driver API 만 쓰는 코드 (드물지만 일부 hand-rolled CUDA C++) → 우리 카운터 누락.

→ Stage 7+ 후속 작업.

---

## 12.8 직접 해보기

### 기본 시나리오

```bash
./scripts/run_launch_in_container.sh
# baseline (no hook): [test-launch] 가 카운트만 직접 출력
# hooked: [fgpu] LAUNCH count=100, =200, ... 그리고 [fgpu] exit summary

# 더 많은 launch 로 부하 테스트
PYTEST_LAUNCH_N=10000 FGPU_LAUNCH_LOG_EVERY=1000 \
    ./scripts/run_launch_in_container.sh

# Off 모드 — overhead 측정 시 사용
FGPU_LAUNCH_LOG_EVERY=0 ./scripts/run_launch_in_container.sh
# stderr 에 LAUNCH count 라인 안 나옴, 하지만 exit summary 는 남
```

### 검증 — 카운터가 정확한가

[hook/tests/test_launch.cu](../../hook/tests/test_launch.cu) 가 자기 atomic counter 도 들고 있어요. 커널이 이를 +1 하므로 `[test-launch] kernel atomics = N` 와 `[fgpu] exit summary: total cudaLaunchKernel = N` 가 *정확히 일치* 해야 합니다. 일치하면 hook 이 launch 를 *드롭하지 않고* 카운트했다는 증명.

---

## 12.9 5-A 상관 분석 확장

[scripts/eval/run_correlation.sh](../../scripts/eval/run_correlation.sh) 가 이 카운터를 다른 메트릭과 join 합니다:

```
시간축 (1초 step):
  t=0   container_A: launch=0,    used_mem=0 MiB
  t=1   container_A: launch=1500, used_mem=2048 MiB
  t=2   container_A: launch=3100, used_mem=2048 MiB
  ...

  t=0   container_B: launch=0,    used_mem=0 MiB
  t=1   container_B: launch=200,  used_mem=2048 MiB
  ...
```

이로부터 두 컨테이너가 *같은 GPU 를 어떻게 나눠 쓰는지* 그래프로 그려집니다. 논문 figure 의 핵심 데이터.

자세한 건 [scripts/eval/_correlate.py](../../scripts/eval/_correlate.py) — stdlib 만으로 구현.

---

## 자가점검 질문

1. `cudaLaunchKernel` 호출이 *비동기* 라는 사실은 우리 카운터의 정확성에 영향을 주는가?
2. 두 컨테이너의 launch 카운트가 같다면 *반드시* GPU 점유 시간도 같다고 결론 낼 수 있는가?
3. `FGPU_LAUNCH_LOG_EVERY=0` 은 카운터 자체를 끄는가, 아니면 dump 만 끄는가?
4. atexit 핸들러가 *호출되지 않을* 시나리오 두 가지를 말하라.
5. PyTorch DataLoader 가 워커 4개로 launch 를 동시에 부르면 우리 카운터가 4중 카운트되거나 누락될 위험이 있나?

→ [Chapter 13: Admission control](13-admission-control.md)

---

## 외부 자료 종합

- 📚 [CUDA C Programming Guide — Asynchronous Concurrent Execution](https://docs.nvidia.com/cuda/cuda-c-programming-guide/index.html#asynchronous-concurrent-execution)
- 📄 [NVIDIA — Nsight Systems](https://developer.nvidia.com/nsight-systems) — 본격 GPU 활동 분석 도구. 우리 카운터의 *완성형* 같은 도구.
- 🛠 `nvidia-smi pmon` — 프로세스별 GPU 활동 모니터링
