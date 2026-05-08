# Chapter 01 — LD_PRELOAD 와 동적 링킹

이 챕터가 *전체 프로젝트의 심장* 입니다. 이게 흔들리면 뒤에 뭘 봐도 사상누각이에요.

## 학습 목표

- 동적 링커(`ld.so`) 가 심볼을 어떤 순서로 찾는지 그릴 수 있다.
- `LD_PRELOAD` 가 그 순서를 어떻게 흔드는지 한 문장으로 설명한다.
- `dlsym(RTLD_NEXT, "name")` 이 *왜* 무한 재귀를 일으키지 않는지 안다.
- 위 셋을 합쳐 "왜 우리 `cudaMalloc` 이 진짜 `cudaMalloc` 보다 먼저 호출되는가" 를 직접 데모할 수 있다.

---

## 1.1 동적 링커가 하는 일 — 정상 흐름

당신이 작성한 프로그램 `myapp` 이 `cudaMalloc(&p, N)` 을 호출한다고 합시다.

```
[컴파일러]   nvcc 가 myapp 의 .o 안에 "이 자리에 cudaMalloc 함수 주소를
              채워주세요. 단, cudaMalloc 의 *주소* 는 지금은 모름" 이라는
              구멍(=relocation entry)을 남김.

[링커]       ld 가 .o 들을 모아 ELF 실행파일을 만들면서, 그 구멍에 대해
              "런타임에 채울 거다, libcudart.so 라는 .so 에서 찾으세요"
              라는 주문을 남김 (DT_NEEDED 엔트리).

[실행 시]    커널이 myapp 을 exec 하면 가장 먼저 ld.so (동적 링커) 가
              뜸 → ld.so 가 myapp 의 DT_NEEDED 를 보고 libcudart.so,
              libc.so, ... 를 차례대로 메모리에 매핑 → 모든 .so 의 심볼
              테이블을 합쳐 "이름 → 주소" 표 (procedure linkage table) 를
              만듦 → main 으로 점프.

[main 실행]  myapp 안의 cudaMalloc 호출이 PLT 를 통해 libcudart 의 진짜
              cudaMalloc 주소로 점프.
```

여기서 핵심은 **심볼 검색 순서**입니다. 같은 이름의 함수가 여러 `.so` 에 있으면, **먼저 로드된 .so** 가 이깁니다. `ld.so` 는 발견 즉시 그 주소를 PLT 에 박고 다음 .so 는 안 봐요.

### 더 공부하려면
- Ulrich Drepper, **["How To Write Shared Libraries"](https://akkadia.org/drepper/dsohowto.pdf)** — glibc 메인테이너가 쓴 결정판 PDF. 어렵지만 한 번은 봐야.
- [Eli Bendersky — Position Independent Code (PIC) in shared libraries](https://eli.thegreenplace.net/2011/11/03/position-independent-code-pic-in-shared-libraries/) — PLT/GOT 의 동작을 그림으로
- `man 8 ld.so`

---

## 1.2 LD_PRELOAD — 한 줄 트릭

`LD_PRELOAD=/opt/fgpu/libfgpu.so myapp` 으로 실행하면, `ld.so` 가 *DT_NEEDED 보다 먼저* `libfgpu.so` 를 로드합니다. 결과:

```
로드 순서: libfgpu.so → libcudart.so → libc.so → ...
심볼 검색: 같은 이름이면 libfgpu.so 가 이김
```

우리 `libfgpu.so` 안에 `cudaMalloc` 이라는 *같은 이름* 함수가 있으면, `myapp` 이 호출하는 `cudaMalloc` 은 우리 함수가 됩니다. 사용자 코드 한 줄도 안 고쳤는데, libcudart 의 진짜 `cudaMalloc` 은 호출되지 않아요.

### 시각화

```
사용자 프로그램 코드: cudaMalloc(&p, N);
                          │
                          ▼  (PLT 통해 점프)
        ┌─────────────────────────────────────────┐
        │  PLT: cudaMalloc → ?                    │
        │                                         │
        │  ld.so 가 검색 시 본 순서대로:           │
        │   1. libfgpu.so   ← 우리 cudaMalloc     │ ← 여기로 결정
        │   2. libcudart.so ← 진짜 cudaMalloc     │
        └─────────────────────────────────────────┘
```

---

## 1.3 그러면 진짜 cudaMalloc 은 어떻게? — `dlsym(RTLD_NEXT, ...)`

문제: 우리 hook 안에서 `cudaMalloc(devPtr, size)` 라고 적으면 *자기 자신* 을 호출하는 무한 재귀가 됩니다.

해결: `dlsym(RTLD_NEXT, "cudaMalloc")` — "이 .so 다음에 로드된 라이브러리들 중에서 `cudaMalloc` 을 찾아 그 주소를 줘".

```c
#define _GNU_SOURCE          // RTLD_NEXT 를 노출하기 위한 GNU 확장
#include <dlfcn.h>

static cudaError_t (*real_cudaMalloc)(void**, size_t) = NULL;

cudaError_t cudaMalloc(void **p, size_t n) {
    if (!real_cudaMalloc)
        real_cudaMalloc = dlsym(RTLD_NEXT, "cudaMalloc");
    // ... quota check ...
    return real_cudaMalloc(p, n);   // 진짜 호출 — 무한재귀 X
}
```

세 가지 디테일:

1. **`_GNU_SOURCE` 매크로**. `RTLD_NEXT` 는 POSIX 가 아니라 GNU 확장이에요. `dlfcn.h` 가 `RTLD_NEXT` 를 *조건부* 로 노출하므로 `#include` 보다 먼저 매크로를 정의해야 합니다. 빠뜨리면 `RTLD_NEXT undeclared` 컴파일 에러. 코드: [fgpu_hook.c:68](../../hook/src/fgpu_hook.c#L68).

2. **Lazy initialization**. `real_cudaMalloc` 을 라이브러리 로드 시점이 아니라 **첫 호출 시점에** 채웁니다. 이유: ① 라이브러리 생성자(constructor)에서 `dlsym` 을 부르면 모든 `.so` 가 로드되기 전이라 NULL 이 나올 수 있음. ② 사용자가 늦게 `dlopen("libcuda.so")` 하는 경우, 처음엔 NULL 이고 두 번째 호출에 채워져도 자연스럽게 회복.

3. **반환 타입과 시그니처가 정확히 일치해야** 함. `cudaError_t (*)(void**, size_t)` — 한 글자만 어긋나도 ABI 가 깨집니다. `cuda_runtime_api.h` 헤더의 선언을 그대로 본떠서 적는 게 안전.

코드: [fgpu_hook.c:266-285](../../hook/src/fgpu_hook.c#L266-L285).

### 더 공부하려면
- `man 3 dlsym` — `RTLD_NEXT`, `RTLD_DEFAULT` 차이 정확히
- [GNU libc manual — Dynamic Linker](https://www.gnu.org/software/libc/manual/html_node/Dynamic-Linker-Introspection.html)

---

## 1.4 컴파일 옵션 — `-shared -fPIC`

[scripts/build_hook.sh](../../scripts/build_hook.sh) 가 다음과 비슷한 명령으로 빌드합니다.

```bash
gcc -shared -fPIC -o build/libfgpu.so hook/src/fgpu_hook.c -ldl -lcudart
```

각 플래그의 의미:

| 플래그 | 의미 |
|---|---|
| `-shared` | 실행파일이 아니라 `.so` 를 만들어라 |
| `-fPIC` | Position-Independent Code — 메모리 어디에 매핑돼도 동작하는 코드를 생성. `.so` 는 보통 프로세스마다 다른 주소에 매핑되니 필수 |
| `-ldl` | `dlsym`, `dlopen` 함수가 들어 있는 `libdl` 링크 |
| `-lcudart` | `cudaError_t` 등의 enum 정의가 헤더에서 오지만, 실제 심볼 일부도 link 시점에 필요 (lazy 가 아니라 immediate 인 함수도 있을 수 있음) |

---

## 1.5 직접 해보기 — `LD_PRELOAD` 데모 (CUDA 없는 미니 예제)

CUDA 가 부담스러우면 `malloc` 으로 같은 트릭을 연습할 수 있어요. *완전히 같은 메커니즘* 입니다.

```c
// myhook.c — malloc 을 가로채서 크기를 stderr 로 찍는다
#define _GNU_SOURCE
#include <stdio.h>
#include <dlfcn.h>
#include <stdlib.h>

static void *(*real_malloc)(size_t) = NULL;

void *malloc(size_t n) {
    if (!real_malloc) real_malloc = dlsym(RTLD_NEXT, "malloc");
    void *p = real_malloc(n);
    fprintf(stderr, "[hook] malloc(%zu) -> %p\n", n, p);
    return p;
}
```

빌드 + 실행:
```bash
gcc -shared -fPIC -o myhook.so myhook.c -ldl
LD_PRELOAD=./myhook.so ls /
# stderr 에 [hook] malloc(...) 가 와르르 — ls 의 *내부* malloc 호출들이 다 잡힘
```

성공하면 같은 원리로 `cudaMalloc` 도 가로챌 수 있다는 확신이 옵니다.

> **주의**: `fprintf` 자체가 내부에서 `malloc` 을 부를 수 있어 무한재귀처럼 보일 수 있어요. 실제로는 `stdio` 가 별도 버퍼를 갖고 있어 보통 OK 지만, 혹시 stack overflow 가 나면 `write(2, ...)` 로 syscall 직접 호출하도록 바꾸세요. 이게 우리 프로젝트가 [fgpu_hook.c:151](../../hook/src/fgpu_hook.c#L151) 에서 reentrancy guard 를 두는 이유 중 하나입니다 — 자세한 건 [Chapter 04](04-thread-safety.md).

---

## 1.6 LD_PRELOAD 의 알려진 함정

논문에 미리 써둘 만한 한계 4 가지:

1. **정적 링크 우회** — `nvcc -cudart=static` 으로 빌드하면 `cudaMalloc` 이 `.so` 가 아니라 실행파일 안에 들어 있어 PLT 를 안 거침. `LD_PRELOAD` 가 자기 위치에 못 끼어듬.
2. **`dlopen` 직접 호출** — 어떤 라이브러리가 `dlopen("libcudart.so", RTLD_NOW)` 후 직접 `dlsym(handle, "cudaMalloc")` 으로 가져가면, 그 핸들의 검색 범위는 우리 `.so` 를 안 봄.
3. **setuid 바이너리** — 보안상 setuid 프로세스에는 `LD_PRELOAD` 가 무시됨. 우리 시나리오에선 컨테이너 안에서 사용자 프로그램이 일반 권한이라 무관, 하지만 알아둘 것.
4. **마운트 순서 충돌** — `nvidia-container-runtime` 이 호스트의 `libcudart.so` 를 컨테이너 안 특정 경로에 bind mount 해요. 우리 hook `.so` 도 별도 경로에 두고 명시적 PRELOAD 를 해야 충돌이 없음. ([Chapter 05](05-docker-gpu.md) 참조)

---

## 자가점검 질문

1. `LD_PRELOAD` 가 *무엇을* 정확히 바꾸는가? (한 문장)
2. `dlsym(RTLD_NEXT, "foo")` 와 `dlsym(RTLD_DEFAULT, "foo")` 의 차이는? (힌트: `RTLD_DEFAULT` 는 검색을 *맨 처음부터* 해서 우리 hook 자신을 찾을 수 있음 → 무한재귀 위험)
3. 정적 링크된 바이너리가 LD_PRELOAD 로 후킹 안 되는 이유를 ELF 관점에서 설명하라.
4. 우리 hook 의 `cudaMalloc` 시그니처 한 글자라도 틀리면 어떤 일이 생기는가?
5. 위 1.5 의 `myhook.so` 데모를 직접 빌드하고 실행해 stderr 출력을 봤는가? (보지 않았다면 지금 보세요!)

다 OK 면 → [Chapter 02: CUDA API 3 계층](02-cuda-api-layers.md)

---

## 외부 자료 종합

- 📄 [How To Write Shared Libraries — Drepper](https://akkadia.org/drepper/dsohowto.pdf)
- 📄 [Eli Bendersky — Load-time relocation of shared libraries](https://eli.thegreenplace.net/2011/08/25/load-time-relocation-of-shared-libraries)
- 🛠 `man 8 ld.so`, `man 3 dlsym`, `man 1 ldd`
- 📚 *Linkers and Loaders* by John R. Levine — 책 한 권. 깊이 파고 싶을 때.
- 🎥 LWN.net 에 LD_PRELOAD 관련 글이 다수 — 검색해서 참고 (예: "LD_PRELOAD" 키워드)
