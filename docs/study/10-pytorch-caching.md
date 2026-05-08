# Chapter 10 — PyTorch caching allocator 가 후킹을 가리는 이유

## 학습 목표

- PyTorch 의 caching allocator 가 *왜* 존재하는지 안다.
- 그게 왜 우리 hook 의 *세부* quota 적용을 가리는지 안다.
- `PYTORCH_NO_CUDA_MEMORY_CACHING=1` 의 정확한 효과를 안다.
- *근본적 한계* 와 *워크어라운드 한계* 를 구분한다.

---

## 10.1 왜 caching allocator 가 존재하는가

`cudaMalloc` 은 **GPU OS 호출 수준** 의 비싼 작업입니다. 한 번에 수십 μs ~ ms. PyTorch 처럼 텐서를 자주 만들고 버리는 워크로드에선 `malloc/free` 를 매번 부르면 throughput 이 죽어요.

해결: **사용자 공간 슬랩 할당기 (user-space slab allocator)**.

```
[PyTorch]  torch.empty(1MB) 부름
              │
              ▼
[CUDACachingAllocator]
   ├─ 풀에 1MB 빈 자리 있나?
   │     YES → 그 자리 돌려줌 (cudaMalloc 안 부름)
   │     NO  → 큰 chunk (예: 64MB) 를 cudaMalloc 으로 잡고,
   │           그 안에서 1MB sub-allocate
   │
   ▼
torch.empty 가 돌려준 텐서가 garbage-collect 되면
allocator 가 *재사용* — cudaFree 안 부름
```

결과:
- 사용자 코드에서 `torch.empty` 가 100번 불려도 `cudaMalloc` 은 보통 *몇 번* 만.
- GPU 메모리는 한번 확보하면 process 종료 전까지 안 풀림 (재사용).

### 더 공부하려면
- [PyTorch — CUDA semantics: Memory management](https://pytorch.org/docs/stable/notes/cuda.html#memory-management)
- [PyTorch source — c10/cuda/CUDACachingAllocator.cpp](https://github.com/pytorch/pytorch/blob/main/c10/cuda/CUDACachingAllocator.cpp) — 본체 (어렵지만 보면 인생 바뀜)

---

## 10.2 우리 hook 입장에서 보이는 모습

caching ON (PyTorch 기본):

```
사용자 코드:    torch.empty(1MB)  ← 100번
                torch.empty(1MB)
                ...
                torch.empty(1MB)

caching:        한 번만 cudaMalloc(64MB) → 그 안에서 sub-alloc

우리 hook:      [fgpu] ALLOW cudaMalloc size=67108864 ... ← 단 1줄
                (이후 99번의 텐서 생성은 hook 안 보임)
```

문제점:
- *세부* 텐서 단위 quota 강제 불가능.
- caching 이 한 번에 큰 슬랩을 잡으면 우리 hook 의 quota 초과 검사가 *그 큰 덩어리* 로만 결정.
- 사용자 워크로드의 진짜 메모리 패턴을 못 봄.

caching OFF (`PYTORCH_NO_CUDA_MEMORY_CACHING=1`):
```
사용자 코드:    torch.empty(1MB)  ← 100번

PyTorch:        매번 cudaMalloc(1MB) → cudaFree(1MB)

우리 hook:      [fgpu] ALLOW cudaMalloc size=1048576 ...
                [fgpu] FREE  ...
                (× 100)
```

이때야 비로소 hook 이 모든 텐서 alloc 을 봅니다.

---

## 10.3 `PYTORCH_NO_CUDA_MEMORY_CACHING=1` — 정확히 무엇을 하나

PyTorch 의 환경변수. 설정되면 caching allocator 의 *재사용* 경로를 끄고 매번 `cudaMalloc/cudaFree` 를 직접 호출합니다.

### Trade-off

- **장점**: 우리 hook 이 모든 alloc 을 봄. quota 가 텐서 단위로 정확히 적용.
- **단점**: throughput 이 5~10배 감소 가능. 학습 시 이 옵션 켜면 실용성 X.

따라서 본 프로토타입에서:
- **검증/평가용** 컨테이너 ([runtime-image-pytorch/Dockerfile](../../runtime-image-pytorch/Dockerfile)) 는 default 로 이 env 를 박음.
- **프로덕션** 시나리오라면 caching ON 으로 두고 *전체 슬랩 단위* quota 만 보장하는 걸로 타협.

---

## 10.4 근본적 한계: VMM hook 도 못 푼다

[Chapter 02](02-cuda-api-layers.md) 에서 본 VMM API 가 modern path 를 잡지만, *caching* 자체는 **사용자 공간** 이라 어떤 hook 으로도 안 보입니다.

```
caching allocator 가 한 번에 64MB 풀을 잡음:
  - cudaMalloc 으로 잡으면  → Runtime hook 이 봄 (✓)
  - cuMemAlloc_v2 로 잡으면 → Driver hook 이 봄 (✓)
  - cuMemCreate 로 잡으면   → VMM hook 이 봄 (✓)

그 64MB 안에서 사용자 텐서가 1MB sub-allocate 되는 건:
  - PyTorch 의 *user-space* 자료구조 — GPU API 호출 X
  - 어떤 LD_PRELOAD hook 으로도 보이지 않음 ✗
```

이게 **근본적** 한계입니다. *해결* 하려면 PyTorch 자체에 patch 를 넣거나(`torch.cuda.memory._set_allocator_settings` 등 활용), hook 이 아닌 다른 layer 가 필요해요.

본 프로토타입의 입장: **이 한계를 인정하고 논문에 명시** — "메모리 quota 는 *어느 GPU API 경로* 든 잡지만, 사용자 공간 풀 안의 sub-allocation 은 보이지 않는다".

---

## 10.5 Stage 4 검증 시나리오 분석

[CLAUDE.md](../../CLAUDE.md) 의 Stage 4 success criteria 를 한 번 더 짚어봅시다:

> **`FGPU_RATIO=0.4`** (quota ≈ 3.2 GiB):
> - 256 MiB → `OK`
> - 4 GiB → `OOM ← cudaErrorMemoryAllocation 이 PyTorch 까지 전파됨`

caching off 가 켜져 있어 두 alloc 모두 cudaMalloc 을 직접 호출. 두 번째 4GiB 는 quota 3.2 GiB 를 *단번에* 초과 → `cudaErrorMemoryAllocation` → PyTorch CUDACachingAllocator → `torch.cuda.OutOfMemoryError`.

이 propagation 체인이 깨끗이 작동한다는 것 = *"hook 이 표준 CUDA 에러 코드를 정확히 돌려준다"* 를 입증.

> caching on 이라면? → 첫 번째 4GiB 시도 시 caching 이 슬랩으로 잡으려 *cudaMalloc(4GiB 또는 그 이상의 풀)* → quota 3.2 GiB 초과 → OOM. **결과는 같지만** 실제 잡힌 양이 의도와 다를 수 있음 (caching 의 풀 크기 결정).

---

## 10.6 직접 해보기

```bash
./scripts/build_pytorch_image.sh

# 1) caching off (기본) — 두 alloc 모두 cudaMalloc 으로 가서 두 번째에서 OOM
./scripts/run_pytorch_in_container.sh

# 2) caching on 으로 강제 (env override) — 동작 비교
docker run --rm --gpus all \
    -v $PWD/build/libfgpu.so:/opt/fgpu/libfgpu.so:ro \
    -e LD_PRELOAD=/opt/fgpu/libfgpu.so \
    -e FGPU_RATIO=0.4 \
    -e PYTORCH_NO_CUDA_MEMORY_CACHING= \
    fgpu-runtime-pytorch:stage4 python3 /opt/fgpu/test_pytorch.py
```

stderr 의 `[fgpu] ALLOW/DENY` 라인 수와 size 를 비교하면, caching 이 *합쳐서 잡는다* 는 게 눈으로 보입니다.

---

## 10.7 다른 프레임워크들

| 프레임워크 | caching 정책 | 우리 hook 가시성 |
|---|---|---|
| PyTorch | 기본 ON | 슬랩 단위만 |
| TensorFlow | 기본 ON (`gpu_options.allow_growth=False` 시 처음에 전부 잡음) | 한 번만 보임 |
| JAX | XLA allocator (preallocate by default) | 한 번에 전체 잡음 |
| 직접 CUDA C++ | 사용자 명시 | 매 호출 보임 |

→ **모든 ML 프레임워크는 사실상 caching 을 합니다**. 프로덕션 시나리오에선 슬랩 단위 quota 가 한계.

---

## 자가점검 질문

1. caching allocator 가 *왜* 존재하는가? 한 단어로 답하면 무엇?
2. `PYTORCH_NO_CUDA_MEMORY_CACHING=1` 을 *프로덕션* 에서 안 쓰는 이유는?
3. VMM hook 을 추가했음에도 caching 의 sub-allocation 이 여전히 안 보이는 *근본적* 이유는?
4. 본 프로토타입이 caching 한계를 *해결하려* 하지 않고 *명시* 만 하는 디자인 결정의 정당성은?
5. caching ON 상태에서 `torch.empty(1MB)` 를 1000번 부르면 `[fgpu] ALLOW` 는 *대략* 몇 줄 나올까?

→ [Chapter 11: 마이크로벤치 방법론](11-benchmarking.md)

---

## 외부 자료 종합

- 📚 [PyTorch — CUDA Memory Management](https://pytorch.org/docs/stable/notes/cuda.html#memory-management)
- 📚 [PyTorch — `torch.cuda.memory_summary()`](https://pytorch.org/docs/stable/generated/torch.cuda.memory_summary.html) — caching allocator 내부 들여다보기
- 🛠 환경변수 `PYTORCH_CUDA_ALLOC_CONF` 도 알아두면 좋음 — `max_split_size_mb` 등 caching 동작 튜닝
