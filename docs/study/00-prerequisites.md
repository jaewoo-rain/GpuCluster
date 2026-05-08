# Chapter 00 — 사전 지식

## 학습 목표

이 챕터가 끝나면 다음 질문에 답할 수 있어야 합니다.

1. C 의 함수 포인터(`int (*f)(int)`)는 어떻게 선언하고 호출하는가?
2. Linux 의 동적 라이브러리(`.so`) 와 정적 라이브러리(`.a`) 는 무엇이 다른가?
3. GPU 메모리는 호스트 메모리(RAM) 와 어떻게 분리돼 있고, 왜 별도 할당 API 가 필요한가?
4. 환경변수(environment variable) 가 부모→자식 프로세스로 전달되는 메커니즘은?

이 4 가지가 흐릿하면 1 챕터로 넘어가도 길을 잃습니다. 다 알면 [Chapter 01](01-ld-preload.md) 로 직행.

---

## 0.1 C 함수 포인터 — 5분 속성

```c
int add(int a, int b) { return a + b; }

int main(void) {
    int (*f)(int, int);   // "int 둘 받고 int 반환하는 함수의 주소"
    f = &add;             // & 는 생략 가능 — 함수 이름은 그 자체로 주소
    int r = f(2, 3);      // (*f)(2,3) 와 같음 — * 도 생략 가능
    return r;             // 5
}
```

이 프로젝트의 후킹 코드는 다음 줄로 시작합니다 ([fgpu_hook.c:91-92](../../hook/src/fgpu_hook.c#L91-L92)):

```c
static cudaError_t (*real_cudaMalloc)(void **, size_t)        = NULL;
static cudaError_t (*real_cudaFree)(void *)                   = NULL;
```

번역하면 "**`real_cudaMalloc`** 은 `void **` 와 `size_t` 를 받고 `cudaError_t` 를 반환하는 *함수의 주소* 를 담는 변수다. 처음엔 NULL." 인 거예요. 나중에 `dlsym` 으로 진짜 `cudaMalloc` 의 주소를 받아서 이 변수에 채웁니다.

**핵심 직관**: 함수 포인터는 *변수* 입니다. 다른 변수처럼 바꿀 수 있고, 호출 시점에 어디로 점프할지가 그 변수의 값에 따라 달라집니다.

### 더 공부하려면
- [Beej's Guide to C — Pointers to Functions](https://beej.us/guide/bgc/html/split/pointers-to-functions.html) — 짧고 명료
- [cppreference — Pointer declaration](https://en.cppreference.com/w/c/language/pointer) — 레퍼런스

---

## 0.2 정적 vs 동적 라이브러리 — 이게 *왜* 중요한가

| 구분 | 정적 (`.a`) | 동적 (`.so`) |
|---|---|---|
| 언제 합쳐지나 | **컴파일 시** 실행파일 안에 박힘 | **실행 시** 메모리에 로드 |
| 파일 크기 | 큼 (라이브러리가 들어가 있음) | 작음 (참조만 있음) |
| 업데이트 | 라이브러리 갱신 → 재컴파일 필요 | `.so` 만 교체하면 됨 |
| **LD_PRELOAD 가능?** | **불가능** (이미 박혔음) | **가능** (실행 시 결정) |

이 프로젝트가 *동적 링크* 를 전제로 한다는 점은 곧 한계이기도 합니다. `nvcc -cudart=static` 으로 빌드된 바이너리는 `cudaMalloc` 이 `.so` 가 아니라 실행파일 안에 직접 박혀 있어서 우리가 끼어들 틈이 없어요. → [Chapter 15](15-limitations.md) 에서 자세히.

### 손으로 확인

```bash
# 시스템에 있는 어떤 실행파일이든:
ldd $(which python3)
# python3 실행 시 동적으로 로드되는 .so 들이 좌라락 출력됨
# 이 중 libcudart.so.* 가 보이면 그게 우리가 가로챌 표적
```

`ldd` 출력의 각 줄은 "이 .so 를 어디서 찾을 거다" 를 말합니다. `LD_PRELOAD` 는 이 목록의 *맨 앞* 에 우리 .so 를 끼워 넣는 환경변수예요.

### 더 공부하려면
- [TLDP — Program Library HOWTO](https://tldp.org/HOWTO/Program-Library-HOWTO/) — 정적/동적/공유라이브러리의 차이를 처음부터 설명. 좀 오래됐지만 개념은 그대로.
- `man 8 ld.so` — 동적 링커의 공식 문서 ([man7.org](https://man7.org/linux/man-pages/man8/ld.so.8.html))
- `man 1 ldd`

---

## 0.3 GPU 메모리 모델 — 최소한

GPU 는 **별도 디바이스** 입니다. RAM 과 GPU 메모리(VRAM) 는 물리적으로 분리돼 있고, CPU 가 직접 GPU 주소를 `*ptr` 로 역참조할 수 없어요.

```
[CPU]  RAM (예: 32 GB)
        │
        │ PCIe (느림)
        ▼
[GPU]  VRAM (예: RTX 4060 = 8 GB)
        └── 여기에 cudaMalloc 으로 잡은 메모리가 산다
```

이 때문에 `cudaMalloc(&p, N)` 의 의미가 일반 `malloc` 과 미묘하게 다릅니다.

```c
void *p = NULL;
cudaMalloc(&p, 1024);   // p 자체는 RAM 에 있는 변수, 그 안에 GPU 주소가 들어감
*p = 5;                 // ❌ Segfault! GPU 주소를 CPU 가 직접 못 만짐
cudaMemcpy(p, &x, sizeof(int), cudaMemcpyHostToDevice);  // ✅ DMA 로 복사
```

후킹 관점에서 중요한 건: **`cudaMalloc` 은 *VRAM* 의 자리를 잡는 함수**라 8 GB GPU 면 합쳐서 8 GB 까지가 한계라는 점이에요. 우리 hook 의 quota 는 이 VRAM 사용량을 제한합니다.

### 더 공부하려면
- [NVIDIA CUDA Toolkit Documentation](https://docs.nvidia.com/cuda/) — 공식. 처음엔 *Programming Guide* §3 (Programming Interface) 정도만.
- [An Even Easier Introduction to CUDA (NVIDIA blog)](https://developer.nvidia.com/blog/even-easier-introduction-cuda/) — 30분이면 읽음

---

## 0.4 환경변수와 자식 프로세스

```bash
export LD_PRELOAD=/opt/fgpu/libfgpu.so   # 셸의 환경에 추가
docker run ...                            # docker 자식 프로세스가 이를 *상속*
```

`fork()` + `execve()` 시 부모의 environ 배열이 자식에게 그대로 복사됩니다. `docker run -e KEY=VAL` 도 같은 메커니즘으로 컨테이너 안 PID 1 의 environ 에 들어가요.

이 프로젝트에서:
- 백엔드가 `docker run` 할 때 `-e LD_PRELOAD=... -e FGPU_RATIO=0.4` 같은 식으로 주입.
- 컨테이너 안 entrypoint(`/bin/sh`) 가 그 env 를 들고 다음 자식(사용자 CUDA 프로그램) 에게 다시 상속.
- 사용자 프로그램이 시작되는 순간 `ld.so` 가 `LD_PRELOAD` 를 읽고 우리 `.so` 를 가장 먼저 로드.

확인:
```bash
docker run --rm -e MYVAR=hello busybox env | grep MY
# MYVAR=hello
```

### 더 공부하려면
- `man 7 environ` ([man7.org](https://man7.org/linux/man-pages/man7/environ.7.html))
- `man 2 execve` — env 가 자식으로 어떻게 전달되는지

---

## 자가점검 질문

답이 입에서 줄줄 나오면 통과:

1. `int (*f)(int);` 와 `int f(int);` 의 차이는?
2. `LD_PRELOAD` 에 `.so` 를 넣으면 그 라이브러리가 실행 흐름 중 *언제* 로드되나?
3. GPU 가 8 GB 인데 두 컨테이너에 ratio 0.6 씩 줬다. 둘 다 동시에 풀로 쓰려고 하면 어떻게 되나? (힌트: hook 은 *프로세스별* state 라 둘 다 자기 quota = 4.8GiB 까지 OK 라고 보지만, 실제 GPU 는 8GB 한계)
4. `docker run -e FOO=bar ubuntu printenv FOO` 의 출력은?

다 OK 면 → [Chapter 01: LD_PRELOAD 와 동적 링킹](01-ld-preload.md)
