// =====================================================================
// Stage 12 검증 바이너리: duty-cycle throttle.
//
// noop kernel 을 N 번 tight loop 로 launch 하고 wall-clock 경과 시간
// + launches/sec 출력. throttle ON 일 때 throughput 이 baseline 대비
// compute_ratio 비율로 떨어지는지 확인.
//
// env 로 override:
//   PYTEST_LAUNCH_N    기본 5000  (총 launch 횟수)
//
// 종료 코드:
//   0  성공
//   1  CUDA error
// =====================================================================

#include <cuda_runtime.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>

__global__ void noop_throttle_kernel(int *p) {
    int idx = blockIdx.x * blockDim.x + threadIdx.x;
    if (idx == 0) {
        atomicAdd(p, 1);
    }
}

#define CHECK(call)                                                       \
    do {                                                                  \
        cudaError_t _e = (call);                                          \
        if (_e != cudaSuccess) {                                          \
            fprintf(stderr, "[test-throttle] %s -> %s\n",                 \
                    #call, cudaGetErrorString(_e));                       \
            return 1;                                                     \
        }                                                                 \
    } while (0)

static int64_t now_ns(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (int64_t)ts.tv_sec * 1000000000LL + ts.tv_nsec;
}

int main(void) {
    int n = 5000;
    const char *n_env = getenv("PYTEST_LAUNCH_N");
    if (n_env) {
        long v = strtol(n_env, NULL, 10);
        if (v > 0) n = (int)v;
    }

    int *d_counter = NULL;
    CHECK(cudaMalloc(&d_counter, sizeof(int)));
    CHECK(cudaMemset(d_counter, 0, sizeof(int)));

    fprintf(stdout, "[test-throttle] launching %d kernels ...\n", n);
    fflush(stdout);

    int64_t t0 = now_ns();
    for (int i = 0; i < n; i++) {
        noop_throttle_kernel<<<32, 64>>>(d_counter);
    }
    CHECK(cudaDeviceSynchronize());
    int64_t t1 = now_ns();

    double elapsed_ms = (double)(t1 - t0) / 1000000.0;
    double lps = (elapsed_ms > 0) ? (n / (elapsed_ms / 1000.0)) : 0;

    int host_counter = 0;
    CHECK(cudaMemcpy(&host_counter, d_counter, sizeof(int),
                     cudaMemcpyDeviceToHost));

    fprintf(stdout, "[test-throttle] n=%d elapsed_ms=%.1f launches_per_sec=%.0f "
                    "kernel_atomics=%d\n",
            n, elapsed_ms, lps, host_counter);
    fflush(stdout);

    cudaFree(d_counter);
    return 0;
}
