# fGPU 프로토타입 학습 교재

이 디렉토리는 **이 레포의 코드를 처음 보는 사람이** 차근차근 읽고 따라하면서 직접 만든 듯한 이해를 갖게 만드는 것이 목표입니다. 각 챕터는 독립적으로 읽을 수 있지만, 처음이라면 순서대로 가는 걸 권장합니다.

## 학습 로드맵

순서는 "프로젝트 기둥 → 가지" 입니다. 먼저 **LD_PRELOAD 와 CUDA API 후킹**(이 프로젝트의 *심장*)을 이해한 뒤, 그 위에 얹힌 백엔드/Docker/UI 를 봅니다.

| # | 챕터 | 무엇을 배우나 | 사전 지식 |
|---|---|---|---|
| 00 | [Prerequisites](00-prerequisites.md) | C 포인터, Linux 동적 링킹, GPU 메모리 모델의 최소한 | (없음) |
| 01 | [LD_PRELOAD 와 동적 링킹](01-ld-preload.md) | 어떻게 `cudaMalloc` 을 가로챌 수 있는가 | 00 |
| 02 | [CUDA API 3 계층](02-cuda-api-layers.md) | Runtime / Driver / VMM API 의 차이 | 01 |
| 03 | [후킹 코드 라인별 해부](03-hook-walkthrough.md) | `fgpu_hook.c` 를 처음부터 끝까지 읽기 | 01, 02 |
| 04 | [스레드 안전성 — mutex, atomic, reentrancy](04-thread-safety.md) | 왜 `__thread` 가드가 필요한가, atomic 의미 | 03 |
| 05 | [Docker + nvidia-container-toolkit](05-docker-gpu.md) | `--gpus all` 이 실제로 뭘 하는가 | 00 |
| 06 | [FastAPI 백엔드 구조](06-fastapi-backend.md) | app factory, router, dependency | (없음) |
| 07 | [asyncio.to_thread 와 동시성](07-async-io.md) | 왜 docker SDK 호출을 thread 로 던지는가 | 06 |
| 08 | [SQLite 영속성](08-sqlite-persistence.md) | stdlib `sqlite3` 만으로 충분한 이유 | 07 |
| 09 | [Bearer 인증 + 타이밍 공격](09-auth.md) | `hmac.compare_digest` 와 401 흐름 | 06 |
| 10 | [PyTorch caching allocator 가 후킹을 가리는 이유](10-pytorch-caching.md) | `PYTORCH_NO_CUDA_MEMORY_CACHING` 의 의미 | 02, 03 |
| 11 | [Overhead 마이크로벤치 방법론](11-benchmarking.md) | `clock_gettime`, p50/p99 의미 | 03 |
| 12 | [`cudaLaunchKernel` 모니터링](12-launch-monitoring.md) | atomic counter + atexit dump | 04 |
| 13 | [Admission control (capacity gate)](13-admission-control.md) | 후크와 다른 *층* 의 강제 | 06, 07 |
| 14 | [Jupyter Lab 통합](14-jupyter.md) | bind mount + ephemeral port | 05, 06 |
| 15 | [한계와 위협 모델](15-limitations.md) | 무엇이 깨지고 왜 그게 수용 가능한가 | 전체 |

## 추천 학습 흐름

- **빠른 길 (1주)** — 00 → 01 → 03 → 05 → 06. 핵심만. 발표 직전 벼락치기용.
- **표준 길 (2~3주)** — 위 표 순서대로. 매 챕터 끝의 "직접 해보기" 를 실제로 실행.
- **연구자 길 (1달+)** — 표준 길 + 각 챕터의 외부 자료(논문/공식 문서) 까지 읽고 자가점검 질문에 답을 적어둠.

## 각 챕터의 구성

```
1. 학습 목표 — 이 챕터가 끝났을 때 무엇을 할 수 있어야 하는가
2. 핵심 개념 — 그림 + 한 줄 정의
3. 코드 위치 — 레포 어디를 읽어야 하는지 (file:line 링크)
4. 왜 이 선택을 했는가 — 대안과의 비교
5. 직접 해보기 — 손으로 만져보는 작은 실험
6. 자가점검 질문 — 답을 말로 설명할 수 있어야 통과
7. 외부 자료 — 공식 문서 / 깊이 있는 글 / 강의
```

## 참고: 이 교재가 *덮지 않는* 것

- 기초 C 문법(`malloc`, 포인터, 구조체) — 모르면 K&R *The C Programming Language* 또는 [Beej's Guide to C Programming](https://beej.us/guide/bgc/) 먼저.
- 기초 Python — 모르면 [공식 튜토리얼](https://docs.python.org/3/tutorial/) 먼저.
- CUDA 커널 작성(`__global__`, `<<<grid, block>>>`) — 본 프로젝트는 *알로케이션* 후킹이라 커널 본체는 안 다룸. 궁금하면 NVIDIA의 [CUDA C++ Programming Guide](https://docs.nvidia.com/cuda/cuda-c-programming-guide/).

준비됐으면 [Chapter 00](00-prerequisites.md) 으로.
