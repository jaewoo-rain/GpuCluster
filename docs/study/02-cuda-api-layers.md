# Chapter 02 — CUDA API 3 계층

NVIDIA 가 GPU 메모리를 잡는 *방법* 을 한 가지만 만들었으면 후킹이 쉬웠겠지만, 역사적으로 **세 계층** 의 API 가 공존합니다. 이 챕터는 그 셋의 차이와, 우리 hook 이 *왜 셋 모두를 잡아야 하는지* 를 설명합니다.

## 학습 목표

- Runtime / Driver-classic / VMM 세 API 의 호출 형태와 역할 차이를 안다.
- PyTorch 가 *주로* 어느 layer 를 쓰는지, 왜 그것만 잡으면 부족한지 설명할 수 있다.
- 세 layer 가 같은 quota state(`g_used`)를 공유할 때 일어날 수 있는 *이중 카운트* 위험을 이해한다.

---

## 2.1 한 그림 요약

```
[사용자 프로그램]
    │
    ├─ Runtime API   (libcudart.so)    ← 가장 흔함, PyTorch 기본
    │      cudaMalloc / cudaFree
    │      cudaMemcpy, cudaLaunchKernel, ...
    │
    ├─ Driver-classic API (libcuda.so)  ← cuBLAS, 일부 ML 프레임워크
    │      cuMemAlloc_v2 / cuMemFree_v2
    │      cuCtxCreate, cuLaunchKernel, ...
    │
    └─ VMM API (libcuda.so, CUDA 10.2+)  ← 최신 메모리 풀, RAPIDS 일부
           cuMemCreate / cuMemRelease
           cuMemAddressReserve / cuMemMap / ...
              │
              ▼
      [NVIDIA driver → GPU]
```

Runtime API 는 *Driver API 의 사용자 친화적 래퍼* 라고 보면 대체로 맞습니다. `cudaMalloc` 안에서 `cuMemAlloc_v2` 를 부른다고 *생각하기 쉽지만*, 실제로는 cudart 가 driver API 를 내부에서 PLT 를 거치지 않고 직접 호출하는 경우가 많아 우리 hook 으로는 한 layer 호출만 보입니다. **이 점이 5-C 의 핵심 디자인 결정** 이에요 (이중 카운트 가드).

---

## 2.2 Runtime API — `cudaMalloc` / `cudaFree`

가장 익숙한 이름들. 시그니처:

```c
cudaError_t cudaMalloc(void **devPtr, size_t size);
cudaError_t cudaFree(void *devPtr);
```

특징:
- 반환 타입 `cudaError_t` 는 enum. `cudaSuccess = 0`, `cudaErrorMemoryAllocation = 2` 등.
- 에러를 *반환값* 으로 알려줌 (예외나 errno 가 아니라).
- PyTorch 의 `torch.empty(...)` 가 caching off 일 때 결국 이걸 부른다.

후킹 위치: [fgpu_hook.c:376](../../hook/src/fgpu_hook.c#L376) 와 [:429](../../hook/src/fgpu_hook.c#L429).

### 더 공부하려면
- [CUDA Runtime API Reference](https://docs.nvidia.com/cuda/cuda-runtime-api/) — 공식
- 주요 챕터: *Memory Management*

---

## 2.3 Driver-classic API — `cuMemAlloc_v2` / `cuMemFree_v2`

```c
CUresult cuMemAlloc_v2(CUdeviceptr *dptr, size_t bytesize);
CUresult cuMemFree_v2(CUdeviceptr dptr);
```

차이점 정리:

| 항목 | Runtime | Driver-classic |
|---|---|---|
| 반환 타입 | `cudaError_t` | `CUresult` (별도 enum) |
| 디바이스 포인터 타입 | `void *` | `CUdeviceptr` (= `unsigned long long`) |
| 컨텍스트 관리 | 자동 (lazy) | 수동 (`cuCtxCreate`, `cuCtxSetCurrent`) |
| 헤더 | `cuda_runtime_api.h` | `cuda.h` |
| 라이브러리 | `libcudart.so` | `libcuda.so` (드라이버 일부) |

`_v2` 접미사: CUDA 4.0 에서 ABI 가 바뀌면서 도입. `cuMemAlloc` 이라는 이름은 헤더에서 `#define cuMemAlloc cuMemAlloc_v2` 로 redirect 돼요. **`dlsym` 으로 잡을 땐 반드시 `_v2` 표기** — 그게 진짜 심볼명이라서. 코드: [fgpu_hook.c:280-281](../../hook/src/fgpu_hook.c#L280-L281).

후킹 위치: [fgpu_hook.c:475](../../hook/src/fgpu_hook.c#L475) 와 [:523](../../hook/src/fgpu_hook.c#L523).

### `CUdeviceptr` ↔ `void*` 캐스트의 이론적 미묘함

`g_allocs` 추적 리스트는 키를 `void*` 로 들고 있어요. Driver API 는 키가 `CUdeviceptr` (= `unsigned long long`). 64-bit Linux x86_64 에서는 둘 다 8바이트 정수형이라 `(void *)(uintptr_t)dptr` 캐스트가 손실 없이 동작합니다. 다른 ABI(32-bit, 또는 미래 어떤 비주류 플랫폼)였다면 위험할 수 있지만, 본 프로토타입은 x86_64 가정. 코드: [fgpu_hook.c:509](../../hook/src/fgpu_hook.c#L509).

### 더 공부하려면
- [CUDA Driver API Reference](https://docs.nvidia.com/cuda/cuda-driver-api/) — 공식

---

## 2.4 VMM API — `cuMemCreate` / `cuMemRelease`

CUDA 10.2 (2019) 에서 도입된 *modern* allocation 경로. 핵심 디자인 변화: **물리 메모리 할당과 가상 주소(VA) 매핑이 분리**됨.

```c
// 1) 물리 메모리만 할당 — VA 안 받음, handle 만 받음
CUresult cuMemCreate(
    CUmemGenericAllocationHandle *handle,
    size_t size,
    const CUmemAllocationProp *prop,
    unsigned long long flags);

// 2) VA 공간만 예약 — 물리 안 받음
CUresult cuMemAddressReserve(
    CUdeviceptr *ptr, size_t size, size_t alignment,
    CUdeviceptr addr, unsigned long long flags);

// 3) handle 을 VA 에 매핑 — 이때야 비로소 ptr 이 사용 가능
CUresult cuMemMap(
    CUdeviceptr ptr, size_t size, size_t offset,
    CUmemGenericAllocationHandle handle, unsigned long long flags);

// 4) 해제도 분리 — Unmap → AddressFree → Release
```

**왜 이렇게 복잡하게 나눠놨나?** 두 가지 큰 동기:

1. **메모리 풀(memory pool)**: 큰 VA 영역을 한 번 예약해놓고, 안에서 sub-allocation 을 직접 관리 (PyTorch caching allocator 같은 user-space slab 의 미래형).
2. **다중 디바이스 매핑**: 같은 물리 handle 을 여러 GPU 의 VA 에 매핑 (multi-GPU 시나리오).

### 우리 hook 이 *둘만* 잡는 이유

[fgpu_hook.c:580](../../hook/src/fgpu_hook.c#L580) (cuMemCreate), [:628](../../hook/src/fgpu_hook.c#L628) (cuMemRelease).

quota 의 단위는 **물리 메모리량** 입니다. 위 4 단계 중 *물리* 가 변하는 건 `cuMemCreate` (할당) 와 `cuMemRelease` (해제) 두 시점. `AddressReserve` / `Map` / `Unmap` / `AddressFree` 는 VA 만 만지지 물리량은 그대로 → 후킹 의미 없음.

이게 **의도적 미구현**의 좋은 예입니다. 모든 함수를 후킹하지 않고 *상태를 바꾸는 시점* 만 골라낸 거예요.

### 추적 키

`CUmemGenericAllocationHandle` 도 `unsigned long long` typedef. 같은 64-bit 캐스트로 `g_allocs` 에 담을 수 있어요. *이론적으로* runtime/driver/vmm 의 토큰 값이 충돌할 가능성이 있지만 — opaque token 공간이 매우 크고 같은 GPU context 에선 NVIDIA 가 알아서 분배 — 무시 가능 수준입니다.

### 더 공부하려면
- [CUDA Virtual Memory Management — Programming Guide §3.2.10](https://docs.nvidia.com/cuda/cuda-c-programming-guide/index.html#virtual-memory-management)
- [NVIDIA blog: Introducing Low-Level GPU Virtual Memory Management](https://developer.nvidia.com/blog/introducing-low-level-gpu-virtual-memory-management/) — VMM API 의 도입 동기

---

## 2.5 미커버 — `cuMemAllocAsync` / `cuMemAllocManaged`

후속 작업으로 남겨둔 두 경로:

- **`cuMemAllocAsync` (CUDA 11.2+)**: stream-ordered allocation. 메모리 풀(`CUmemoryPool`)에서 재사용 — 우리 hook 시점에 보이는 *물리* 변화는 풀 확장 시 1회뿐.
- **`cuMemAllocManaged`**: Unified Virtual Memory (UVM). CPU 와 GPU 가 같은 가상 주소 공간을 공유. demand paging 으로 *실제* 물리 위치가 동적으로 바뀜.

둘 다 hook 하려면 추가 설계가 필요해서 후속 stage 로 미뤄둔 상태입니다.

---

## 2.6 누가 어느 layer 를 쓰나? — 실전 매트릭스

| 사용자 코드 | 어느 layer | 비고 |
|---|---|---|
| `cudaMalloc` 직접 | Runtime | 학습 예제, 우리 `test_alloc.cu` |
| PyTorch (caching off) | Runtime | `torch.empty` → cudart |
| PyTorch (caching on, default) | Runtime | 단, 첫 큰 chunk 한 번뿐 — 이후는 user-space slab |
| cuBLAS / cuDNN | Driver-classic 일부 | 라이브러리 내부에서 직접 |
| RAPIDS (cuDF, cuML) | VMM 경유 가능 | RMM (RAPIDS Memory Manager) 가 풀 사용 |
| 직접 작성한 CUDA C++ 코드 (Driver API 사용) | Driver-classic | `cuMemAlloc_v2` 명시 호출 |

**결론**: 어느 layer 를 쓸지 사용자가 결정하므로, 우리 hook 은 *세 layer 모두* 잡아야 빈틈이 없습니다. 잡지 않은 layer 는 그대로 quota 우회.

---

## 2.7 세 layer 가 같은 state 를 공유할 때의 위험

[Chapter 04](04-thread-safety.md) 에서 자세히 다루는 내용 미리보기:

만약 cudart 의 `cudaMalloc` 이 *내부적으로* libcuda 의 `cuMemAlloc_v2` 를 호출하면, 사용자 한 번의 `cudaMalloc(N)` 에 대해 우리 hook 이 두 번 발동:

```
cudaMalloc(N) → 우리 hook ① g_used += N → 진짜 cudart::cudaMalloc(N)
                                              → cuMemAlloc_v2(N) → 우리 hook ② g_used += N
                                              → 진짜 cuMemAlloc_v2(N)
                                                  → ✗ g_used 가 2N 이 됨
```

해결책: per-thread reentrancy guard `__thread g_in_hook`. [Chapter 04](04-thread-safety.md) 에서 라인별로.

---

## 자가점검 질문

1. `cudaMalloc` 과 `cuMemAlloc_v2` 의 시그니처상 다른 점 두 가지를 말하라.
2. VMM API 가 메모리 할당을 4 단계로 나눈 이유 두 가지를 말하라.
3. 우리 hook 이 `cuMemAddressReserve` 를 후킹하지 않는 이유는?
4. 사용자가 `cudaMalloc` 한 번 부르면 우리 hook 의 카운터(`g_used`) 는 *최대* 몇 번 증가할 수 있는가? (정답: 1번이어야 함. 왜?)
5. PyTorch 기본 모드에서 `torch.empty(size=4GB)` 를 100번 부르면 hook 의 `[fgpu] ALLOW` 로그가 100번 나올까?

→ [Chapter 03: 후킹 코드 라인별 해부](03-hook-walkthrough.md)
