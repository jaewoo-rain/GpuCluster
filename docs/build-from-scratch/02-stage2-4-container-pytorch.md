# 2장. Stage 2·4 — 컨테이너화와 PyTorch 통합

> **이 장에서 만들 것**
> - 호스트에서만 돌던 훅(`libfgpu.so`)을 **Docker 컨테이너 안**에서 검증하는 파이프라인.
> - `nvcc`로 테스트 바이너리를 이미지 안에 미리 굽되, **훅은 굽지 않고 런타임 마운트**하는 이미지(`runtime-image/`).
> - env를 찍고 훅 존재를 확인한 뒤 사용자 명령을 넘기는 `entrypoint.sh`.
> - baseline vs hooked 를 나란히 돌리는 `build_image.sh` / `run_in_container.sh`.
> - PyTorch 이미지(`runtime-image-pytorch/`)로 넘어가 `cudaErrorMemoryAllocation`이 `torch.cuda.OutOfMemoryError`까지 전파되는지 눈으로 확인.

이전 장에서 여러분은 호스트에서 `./scripts/build_hook.sh` → `./scripts/run_test.sh`로 `[fgpu] ALLOW` / `[fgpu] DENY`를 봤습니다. 이제 그걸 **컨테이너 안으로 옮기는 여정**을 시작합니다. 순서와 "왜 이 순서인가"에 집중하겠습니다.

---

## 1. 이 단계 목표

한 문장으로: **"호스트에서 되던 훅이, 컨테이너 안에서도 똑같이 동작한다"를 증명하는 것.**

왜 굳이 컨테이너로 옮길까요? 두 가지입니다.

1. **재현성.** 캡스톤/논문 실험은 "이 코드로 이 결과가 나왔다"를 남에게 재현시킬 수 있어야 합니다. 호스트의 CUDA 버전, 파이썬 버전, apt 패키지에 의존하면 재현이 깨집니다. 이미지에 박아두면 `docker run` 한 방으로 끝납니다.
2. **격리.** 최종 목표는 "한 GPU를 여러 컨테이너가 나눠 쓴다"입니다. 그러려면 **컨테이너가 실행 단위**여야 합니다. Stage 2는 그 실행 단위를 처음 세우는 단계입니다.

Stage 2는 아직 "여러 개"가 아닙니다. 컨테이너 **하나**에서 훅이 사는지만 봅니다. 여러 컨테이너 동시 격리는 4장(Stage 5-A)에서 합니다. 한 번에 하나씩.

---

## 2. 개발 순서 체크리스트

```
[ ] 1. 베이스 이미지 고르기 — nvidia/cuda:*-devel-ubuntu22.04 (왜 devel?)
[ ] 2. Dockerfile 골격 — 테스트 .cu 를 COPY → 이미지 안에서 nvcc 컴파일
[ ] 3. -cudart shared 함정 이해 (이게 없으면 훅이 안 잡힌다)
[ ] 4. 훅은 굽지 말 것 — 런타임에 -v 로 마운트하도록 설계
[ ] 5. entrypoint.sh — env 로깅 + 훅 존재 확인 + exec "$@"
[ ] 6. build_image.sh — 빌드 래퍼
[ ] 7. run_in_container.sh — baseline / hooked 두 번 실행
[ ] 8. 검증 게이트: baseline 은 [fgpu] 없음, hooked 는 ALLOW+DENY
[ ] --- 여기서 Stage 2 완료. "다음" 하고 넘어감 ---
[ ] 9. Stage 4: runtime-image-pytorch/Dockerfile — FROM 베이스 + torch(cu121)
[ ] 10. PYTORCH_NO_CUDA_MEMORY_CACHING=1 을 ENV 기본값으로 (왜?)
[ ] 11. test_pytorch.py — 256MB OK, 4GB 조건부 → OOM 전파 확인
[ ] 12. 검증 게이트: ratio=0.4 → OOM, ratio=0.6 → OK
```

---

## 3. 스텝별 코드/명령

### 스텝 1~2. 베이스 이미지와 Dockerfile 골격

빈 `runtime-image/Dockerfile`에서 시작합니다. 첫 결정은 **어떤 베이스 이미지를 쓸 것인가**입니다.

```dockerfile
ARG CUDA_VERSION=12.4.1
FROM nvidia/cuda:${CUDA_VERSION}-devel-ubuntu22.04
```

`nvidia/cuda` 이미지에는 `-runtime`과 `-devel` 두 종류가 있습니다.

- `-runtime`: `libcudart` 등 **실행에 필요한 라이브러리만**. 작다.
- `-devel`: 거기에 `nvcc`(CUDA 컴파일러), 헤더까지. 크다.

우리는 **`-devel`을 고릅니다.** 이유: 테스트 바이너리(`test_alloc.cu`)를 **이미지를 빌드하는 동안 컨테이너 안에서 직접 `nvcc`로 컴파일**하고 싶기 때문입니다. 실제 완성본 주석도 같은 이유를 답니다 — "runtime 이미지보다 크지만 프로토타입이라 디스크 크기보다 재현성이 우선"([Dockerfile:12-14](../../runtime-image/Dockerfile#L12)).

왜 호스트에서 컴파일해 바이너리만 넣지 않고, 이미지 안에서 컴파일할까요? 바이너리는 **컨테이너의 `libcudart`에 링크**되어야 ABI가 맞습니다. 이미지 안에서 컴파일하면 그 이미지의 CUDA 런타임과 자동으로 맞아떨어집니다. 호스트 CUDA와 컨테이너 CUDA가 미묘하게 다를 때 생기는 지옥을 처음부터 피하는 겁니다.

그다음, 테스트 소스들을 `COPY`하고 한 `RUN` 레이어에서 전부 컴파일합니다([Dockerfile:36-52](../../runtime-image/Dockerfile#L36)):

```dockerfile
COPY hook/tests/test_alloc.cu        /tmp/test_alloc.cu
# ... (bench_alloc, test_driver_alloc, test_launch, test_vmm_alloc, test_throttle)

RUN nvcc -O2 -cudart shared -o /opt/fgpu/test_alloc        /tmp/test_alloc.cu        \
 && nvcc -O2 -cudart shared -o /opt/fgpu/test_driver_alloc /tmp/test_driver_alloc.cu -lcuda \
 && ...
 && rm /tmp/*.cu
```

> **처음엔 `test_alloc.cu` 하나만.** 완성본에는 6개(`bench_alloc`, `test_driver_alloc`, `test_launch`, `test_vmm_alloc`, `test_throttle`)가 들어 있지만, 그건 이후 스테이지에서 하나씩 추가된 결과입니다. **Stage 2 시점에는 `test_alloc` 한 줄이면 충분합니다.** 나머지는 해당 스테이지에서 이 `RUN` 블록에 `&&`로 한 줄씩 붙여 나가는 게 정석입니다. 이 문서의 "점진적 리듬"이 바로 이겁니다.

### 스텝 3. `-cudart shared` — 이거 하나 때문에 하루를 날릴 수 있습니다

훅이 걸리는 원리는 LD_PRELOAD가 **동적 링크된** `cudaMalloc` 심볼을 가로채는 것입니다. 그런데 CUDA 12.x의 `nvcc`는 **기본이 static 링크**입니다. static으로 링크하면 `cudaMalloc`이 바이너리 안에 박혀버려서, LD_PRELOAD가 끼어들 틈이 없습니다.

그래서 `-cudart shared`가 **필수**입니다([Dockerfile:44-45](../../runtime-image/Dockerfile#L44)):

```
# -cudart shared: CUDA 12.x 는 nvcc 기본이 static link 이라 LD_PRELOAD 가
# cudaMalloc 심볼을 가로챌 수 없음. shared 링크해야 hook 이 동작.
```

이걸 빠뜨리면 증상이 아주 헷갈립니다: 훅은 정상 로드되고 `[fgpu] init`도 찍히는데, `ALLOC`/`DENY`가 **하나도 안 나옵니다**. "훅이 왜 안 걸리지?" 하며 훅 코드를 뒤지기 쉽지만, 범인은 테스트 바이너리의 링크 방식입니다.

Driver/VMM API를 직접 쓰는 테스트(`test_driver_alloc`, `test_vmm_alloc`)는 `libcuda`를 호출하므로 `-lcuda`도 추가로 붙습니다([Dockerfile:48](../../runtime-image/Dockerfile#L48), [L50](../../runtime-image/Dockerfile#L50)). 이건 Stage 5-C/6 얘기라 지금은 몰라도 됩니다.

### 스텝 4. 훅은 굽지 마세요 — 런타임 마운트가 정답

가장 중요한 설계 결정입니다. `libfgpu.so`를 이미지에 `COPY`해서 굽고 싶은 유혹이 있는데, **하지 마세요.** 완성본은 명시적으로 마운트 지점만 만들어 둡니다([Dockerfile:30-31](../../runtime-image/Dockerfile#L30)):

```dockerfile
# hook 마운트 지점 + 컨테이너 내장 테스트 바이너리 위치
RUN mkdir -p /opt/fgpu
```

그리고 실행 시 `-v`로 호스트의 `.so`를 꽂습니다:

```bash
docker run --rm --gpus all \
  -v $PWD/build/libfgpu.so:/opt/fgpu/libfgpu.so:ro \
  -e LD_PRELOAD=/opt/fgpu/libfgpu.so \
  -e FGPU_RATIO=0.4 \
  fgpu-runtime:stage2
```

왜 이렇게 할까요? 주석의 이유([Dockerfile:15-17](../../runtime-image/Dockerfile#L15)):

- **훅을 고칠 때마다 이미지를 재빌드하지 않아도 됩니다.** 훅 C 코드는 개발 중에 자주 바뀝니다. 굽혀 있으면 매번 몇 분씩 이미지를 다시 만들어야 합니다. 마운트면 `build_hook.sh`만 다시 돌리면 끝.
- **백엔드가 여러 훅 버전을 운용하기 유리합니다.** 나중에 백엔드가 컨테이너를 띄울 때(Stage 3~), 같은 이미지에 다른 `.so`를 꽂을 수 있습니다.

또 하나: **`LD_PRELOAD`도 이미지 ENV로 넣지 않습니다**([Dockerfile:18-20](../../runtime-image/Dockerfile#L18)). `.so`가 마운트 안 된 상태로 `LD_PRELOAD`만 켜져 있으면 동적 링커가 `object cannot be preloaded`로 시끄럽게 실패합니다. `docker run -e`로 명시 전달하는 게 의도도 분명하고 사고도 없습니다.

### 스텝 5. `entrypoint.sh` 작성

컨테이너가 뜰 때 "지금 훅이 제대로 꽂혔나?"를 사람이 바로 알 수 있게 하는 게 목적입니다. 세 가지 책임([entrypoint.sh:5-13](../../runtime-image/entrypoint.sh#L5)):

1. fGPU 관련 env / 마운트 상태를 stdout에 찍는다(시연·디버깅 가시성).
2. `LD_PRELOAD`가 가리키는 `.so`가 실제 존재하는지 확인. **없으면 경고만 하고 계속 진행** — 사용자가 일부러 unhooked로 돌릴 수도 있으니 fail-fast 하지 않습니다.
3. `exec "$@"`로 사용자 명령을 그대로 실행 — entrypoint가 PID 1이 되어 SIGTERM 등 시그널이 자식에 전달되게.

핵심 부분([entrypoint.sh:19-35](../../runtime-image/entrypoint.sh#L19)):

```bash
echo "[entrypoint] container starting"
echo "[entrypoint]   FGPU_RATIO       = ${FGPU_RATIO:-<unset>}"
echo "[entrypoint]   LD_PRELOAD       = ${LD_PRELOAD:-<unset>}"

if [[ -n "${LD_PRELOAD:-}" ]]; then
    if [[ -f "${LD_PRELOAD}" ]]; then
        echo "[entrypoint]   hook .so OK  -> ..."
    else
        echo "[entrypoint]   WARN: LD_PRELOAD=${LD_PRELOAD} 인데 파일이 없음."
        echo "[entrypoint]         '-v <host_libfgpu.so>:... :ro' 빠뜨렸을 가능성."
    fi
fi

echo "[entrypoint] exec: $*"
exec "$@"
```

`exec`를 쓰는 이유가 중요합니다. `exec` 없이 그냥 `"$@"`를 부르면 entrypoint 셸이 PID 1로 남고 사용자 프로그램은 자식이 됩니다. 그러면 `docker stop`이 보내는 SIGTERM이 셸에서 멈추고 자식까지 안 갑니다. `exec`로 프로세스를 **치환**해야 사용자 프로그램이 PID 1이 되어 시그널을 직접 받습니다. 나중에 백엔드가 `stop`할 때 이게 안 되어 있으면 컨테이너가 안 죽습니다.

Dockerfile에서 이 entrypoint를 연결합니다([Dockerfile:55-59](../../runtime-image/Dockerfile#L55)):

```dockerfile
COPY runtime-image/entrypoint.sh /usr/local/bin/fgpu-entrypoint
RUN chmod +x /usr/local/bin/fgpu-entrypoint
ENTRYPOINT ["/usr/local/bin/fgpu-entrypoint"]
CMD ["/opt/fgpu/test_alloc"]
```

`ENTRYPOINT`(항상 실행) + `CMD`(기본 인자, override 가능) 조합입니다. `docker run ... fgpu-runtime:stage2`만 하면 `test_alloc`이 돌고, 뒤에 다른 명령을 붙이면 그게 `"$@"`로 들어갑니다.

> **COPY 경로 함정.** Dockerfile이 `runtime-image/`에 있어도 `COPY` 경로는 `runtime-image/entrypoint.sh`처럼 **빌드 컨텍스트(리포 루트) 기준**입니다. 이건 `build_image.sh`가 컨텍스트를 루트로 잡기 때문입니다(다음 스텝).

### 스텝 6. `build_image.sh` — 빌드 래퍼

수동 `docker build`를 매번 치지 않도록 얇게 감쌉니다([build_image.sh:22-26](../../scripts/build_image.sh#L22)):

```bash
docker build \
    -f runtime-image/Dockerfile \
    --build-arg CUDA_VERSION="${CUDA_VERSION}" \
    -t "${IMAGE_NAME}:${IMAGE_TAG}" \
    .
```

포인트는 마지막 `.` — **빌드 컨텍스트가 리포 루트**입니다([build_image.sh:11](../../scripts/build_image.sh#L11), `ROOT_DIR`로 `cd`). 그래서 Dockerfile의 `COPY hook/tests/test_alloc.cu`가 루트 기준으로 파일을 찾습니다. `-f`로 Dockerfile 위치만 따로 지정하는 패턴입니다. 이미지 이름/태그/CUDA 버전은 전부 env로 override 가능하게 기본값을 둡니다.

### 스텝 7. `run_in_container.sh` — baseline vs hooked

검증의 핵심은 **대조군**입니다. "훅 없이는 두 할당 다 성공, 훅 있으면 하나만 성공"을 나란히 보여줘야 훅이 원인이라고 말할 수 있습니다. 그래서 이 스크립트는 **두 번 실행**합니다.

먼저 사전 조건을 확인합니다([run_in_container.sh:22-30](../../scripts/run_in_container.sh#L22)) — `build/libfgpu.so`가 있는지, 이미지가 있는지. 없으면 어떤 스크립트를 먼저 돌리라고 알려주고 종료합니다. (초보가 순서를 헷갈릴 때 친절하게 잡아주는 안전망입니다.)

**(1) baseline** — 마운트도, `LD_PRELOAD`도 없이([run_in_container.sh:35-37](../../scripts/run_in_container.sh#L35)):

```bash
docker run --rm --gpus all \
    "${IMAGE_NAME}:${IMAGE_TAG}" \
    /opt/fgpu/test_alloc
```

**(2) hooked** — `.so` 마운트 + `LD_PRELOAD` + `FGPU_RATIO`([run_in_container.sh:43-48](../../scripts/run_in_container.sh#L43)):

```bash
docker run --rm --gpus all \
    -v "${HOOK_SO_HOST}:/opt/fgpu/libfgpu.so:ro" \
    -e LD_PRELOAD=/opt/fgpu/libfgpu.so \
    -e FGPU_RATIO="${RATIO}" \
    ${FGPU_QUOTA_BYTES:+-e FGPU_QUOTA_BYTES="${FGPU_QUOTA_BYTES}"} \
    "${IMAGE_NAME}:${IMAGE_TAG}"
```

마지막 줄의 `${FGPU_QUOTA_BYTES:+...}`는 "이 env가 설정돼 있을 때만 `-e` 옵션을 붙여라"라는 bash 관용구입니다 — ratio 대신 절대 바이트로 쿼터를 주고 싶을 때만 쓰입니다.

---

## 4. 여기서 실행해서 이걸 확인 (Stage 2 검증 게이트)

```bash
chmod +x scripts/*.sh runtime-image/entrypoint.sh
./scripts/build_hook.sh      # build/libfgpu.so
./scripts/build_image.sh     # fgpu-runtime:stage2
./scripts/run_in_container.sh
```

**baseline 실행에서 확인:**
- `[entrypoint]` 라인들이 뜨고, `LD_PRELOAD = <unset>`.
- `[fgpu]` 라인이 **하나도 없어야** 합니다.
- 256 MiB, 6 GiB 할당 모두 성공(GPU에 여유 메모리가 있다면).

**hooked 실행에서 확인:**
- `[entrypoint]`가 `hook .so OK ->` 로 마운트를 확인.
- `[fgpu] init` → `[fgpu] quota lazily 계산` 순서.
- 256 MiB에 대해 `ALLOW` 한 줄.
- 6 GiB에 대해 `DENY` 한 줄 (ratio 0.4 → 쿼터 ≈ 3.2 GiB이므로 6 GiB는 초과).
- 그 뒤 `FREE` 라인들로 `used`가 0으로 돌아옴.

이게 [CLAUDE.md의 Stage 2 성공 기준](../../CLAUDE.md)과 정확히 일치합니다. baseline에 `[fgpu]`가 하나라도 있으면 마운트/ENV가 새는 것이고, hooked에 `DENY`가 없으면 `-cudart shared`를 의심하세요.

---

## 5. 함정 모음

- **`-cudart shared` 누락.** 위에서 강조했듯, 훅이 로드는 되는데 아무것도 안 잡히면 십중팔구 이겁니다. `nvcc` 명령에 이 플래그가 있는지 먼저 보세요.
- **`--gpus all` 누락.** nvidia-container-toolkit이 있어야 컨테이너가 GPU를 봅니다. 없으면 `test_alloc`이 CUDA 자체를 못 찾습니다. baseline이 실패하면 훅 문제가 아니라 이 문제일 확률이 높습니다.
- **훅을 이미지에 구워버림.** 개발 루프가 느려지고, "고쳤는데 왜 반영이 안 되지?"의 원인이 됩니다. 마운트로 유지하세요.
- **CUDA major 버전 불일치.** 호스트에서 빌드한 `libfgpu.so`가 컨테이너의 `libcudart`와 동적 링크됩니다. 양쪽 major(12.x)가 맞아야 합니다([Dockerfile:21-22](../../runtime-image/Dockerfile#L21)).
- **`exec` 없이 entrypoint 종료.** 시그널 전달이 안 되어 나중에 `docker stop`/백엔드 `stop`이 안 먹힙니다.

---

## 6. Stage 4 — PyTorch 통합으로 넘어가기

Stage 2가 통과했으면 이제 "진짜 워크로드"를 얹습니다. `test_alloc`은 우리가 만든 C 프로그램이지만, 실제 사용자는 PyTorch를 씁니다. **훅이 PyTorch의 `cudaMalloc`도 잡는가?** 이걸 증명하는 게 Stage 4입니다.

### 스텝 9. PyTorch 이미지 — 베이스 위에 얹기

`runtime-image-pytorch/Dockerfile`은 처음부터 만들지 않고 **Stage 2 이미지를 상속**합니다([Dockerfile:21-22](../../runtime-image-pytorch/Dockerfile#L21)):

```dockerfile
ARG BASE_IMAGE=fgpu-runtime:stage2
FROM ${BASE_IMAGE}
```

이렇게 하면 entrypoint, 테스트 바이너리, 마운트 구조를 전부 물려받습니다. entrypoint도 그대로 상속되고 `CMD`만 PyTorch 테스트로 바꿉니다([Dockerfile:61-63](../../runtime-image-pytorch/Dockerfile#L61)).

devel 베이스에는 파이썬이 없으니 설치하고, PyTorch를 **CUDA 12.1 휠**로 깝니다([Dockerfile:27-37](../../runtime-image-pytorch/Dockerfile#L27)):

```dockerfile
RUN python3 -m pip install --no-cache-dir \
        --index-url https://download.pytorch.org/whl/cu121 \
        torch
```

`--index-url`을 PyTorch CDN으로 못 박는 이유: 이게 없으면 pip이 PyPI에서 **CPU-only 휠**을 잘못 깔아버리는 사고가 흔합니다([Dockerfile:33](../../runtime-image-pytorch/Dockerfile#L33)). CUDA 12.1 휠은 베이스의 CUDA 12.4 런타임과 호환됩니다.

### 스텝 10. `PYTORCH_NO_CUDA_MEMORY_CACHING=1` — 이게 핵심입니다

PyTorch에는 **CUDACachingAllocator**가 있습니다. 텐서를 free해도 실제 `cudaFree`를 부르지 않고 내부 풀에 캐시했다가 재사용합니다. 성능엔 좋지만, **우리 훅에겐 치명적**입니다.

캐싱이 켜져 있으면 첫 텐서 할당 때 큰 chunk 하나를 `cudaMalloc`으로 잡고, 이후 텐서들은 그 chunk를 잘라 씁니다. 즉 **`cudaMalloc`이 딱 한 번만 불립니다.** 우리의 per-call 쿼터 검사가 완전히 가려집니다.

그래서 이미지 ENV 기본값으로 캐싱을 끕니다([Dockerfile:58-59](../../runtime-image-pytorch/Dockerfile#L58)):

```dockerfile
# caching off 기본값 (필수). -e 로 override 가능 (논문 비교 실험용).
ENV PYTORCH_NO_CUDA_MEMORY_CACHING=1
```

이렇게 하면 매 텐서 할당이 `cudaMalloc`으로 직행해 쿼터가 적용됩니다([Dockerfile:9-12](../../runtime-image-pytorch/Dockerfile#L9)). 이건 버그 회피가 아니라 **의도된 실험 조건**입니다. 캐싱을 켠 채로 돌리면 "쿼터가 안 먹네?"가 나오는데, 그건 정상이고 알려진 한계입니다(Stage 6+ Driver/VMM 훅의 동기이기도 합니다).

또 하나: `PYTORCH_CUDA_ALLOC_CONF`는 **설정하지 않습니다**([Dockerfile:16-18](../../runtime-image-pytorch/Dockerfile#L16)). 이걸 `cudaMallocAsync` 백엔드로 바꾸면 훅(Stage 1은 `cudaMalloc`만 잡음)을 우회합니다. default native 경로를 유지해야 `cudaMalloc`으로 들어옵니다.

### 스텝 11. `test_pytorch.py` — OOM 전파를 눈으로

이 파일의 목적은 단 하나: **훅이 반환한 `cudaErrorMemoryAllocation`이 `libcudart` → PyTorch CUDACachingAllocator → 파이썬 `torch.cuda.OutOfMemoryError`까지 전파되는지** 확인하는 것.

시나리오는 "명확한 경계선"을 노립니다([test_pytorch.py:5-10](../../runtime-image-pytorch/test_pytorch.py#L5)):

- 256 MiB 텐서 → 어떤 합리적 쿼터에서도 ALLOW.
- 4 GiB 텐서 → baseline ALLOW / ratio 0.4(쿼터 3.2 GiB) DENY / ratio 0.6(쿼터 4.8 GiB) ALLOW.

할당 시도는 예외를 정확히 분기해서 잡습니다([test_pytorch.py:49-65](../../runtime-image-pytorch/test_pytorch.py#L49)):

```python
def try_alloc(size_mib: int):
    n_floats = (size_mib * 1024 * 1024) // 4   # float32 = 4 bytes
    try:
        t = torch.empty(n_floats, dtype=torch.float32, device="cuda:0")
        torch.cuda.synchronize()               # lazy 할당 강제 commit
        print(f"[pytorch-test]   OK   data_ptr={hex(t.data_ptr())}", flush=True)
        return t
    except torch.cuda.OutOfMemoryError as e:
        print(f"[pytorch-test]   OOM  ← cudaErrorMemoryAllocation 이 PyTorch 까지 전파됨", ...)
        return None
```

두 디테일이 실전에서 중요합니다:

1. **`torch.cuda.synchronize()`** — PyTorch 할당은 lazy할 수 있어서, sync로 강제 커밋하지 않으면 훅이 안 불릴 수 있습니다([test_pytorch.py:55](../../runtime-image-pytorch/test_pytorch.py#L55)).
2. **`torch.cuda.OutOfMemoryError`를 명시적으로 잡기** — 이게 잡히는 것 자체가 "우리 훅의 에러 코드가 파이썬 최상위까지 올라왔다"의 증거입니다. 이걸 못 잡고 죽으면 전파가 어딘가에서 끊긴 겁니다.

`try_alloc`은 크기를 env로 override할 수 있어(`PYTEST_ALLOC1_MIB`, `PYTEST_ALLOC2_MIB`) GPU 메모리 크기에 맞춰 실험을 조정합니다([test_pytorch.py:69-70](../../runtime-image-pytorch/test_pytorch.py#L69)).

### 이미지 빌드

`scripts/build_pytorch_image.sh`가 베이스 위에 이 이미지를 만듭니다. 첫 실행은 PyTorch 휠(~5 GB)을 받느라 몇 분 걸립니다.

---

## 검증 게이트 (Stage 4)

```bash
./scripts/build_pytorch_image.sh                        # fgpu-runtime-pytorch:stage4
./scripts/run_pytorch_in_container.sh                   # baseline + ratio=0.4
FGPU_RATIO=0.6 ./scripts/run_pytorch_in_container.sh    # ratio=0.6
```

세 가지 관찰이 나와야 합니다([CLAUDE.md Stage 4 성공 기준](../../CLAUDE.md)):

- **baseline**: 두 할당 다 `[pytorch-test] OK`, `[fgpu]` 라인 없음.
- **ratio=0.4**: 256 MiB → `OK`, 4 GiB → `OOM ← cudaErrorMemoryAllocation 이 PyTorch 까지 전파됨`. 훅 로그에 `ALLOW` 1 + `DENY` 1.
- **ratio=0.6**: 두 할당 다 `OK`, 훅 로그에 `ALLOW` 2.

`OOM` 라인이 나오는 순간이 이 장의 하이라이트입니다 — C로 짠 우리 훅의 반환값이, 남이 만든 거대한 PyTorch를 뚫고 파이썬 예외로 튀어나온 것입니다.

---

## 함정 (Stage 4 특유)

- **캐싱을 안 껐을 때.** `PYTORCH_NO_CUDA_MEMORY_CACHING=1`이 없으면 첫 큰 slab 하나만 `cudaMalloc`으로 잡혀 쿼터 효과가 사라집니다. 이미지 ENV로 기본 설정되어 있지만, `-e`로 덮어쓰면 다시 문제가 됩니다.
- **CPU-only torch가 깔림.** `--index-url` 없이 설치하면 `torch.cuda.is_available()`이 False. 스크립트가 "`--gpus all` 빠뜨렸나?"라고 오해하게 만듭니다.
- **`synchronize()` 없이 판정.** lazy 할당 때문에 OOM이 지연되거나 엉뚱한 지점에서 터질 수 있습니다.
- **⚠ 이미지 재빌드가 필요한 시점을 놓침.** 이게 가장 흔한 실수입니다. **Dockerfile이나 이미지 안에 굽는 파일(`.cu`, `test_*.py`)을 바꾸면 반드시 이미지를 다시 빌드**해야 합니다. 예: `test_hold.py`를 추가해 놓고 `build_pytorch_image.sh`를 안 돌리면, 컨테이너 안에는 예전 파일만 있습니다. 반대로 **훅 `.so`는 마운트라 재빌드 불필요** — `build_hook.sh`만 다시 돌리면 됩니다. "무엇이 구워지고 무엇이 마운트되나"를 늘 구분하세요. (이 구분 덕에 훅 개발 루프는 빠르고, 이미지 변경은 드물게 일어납니다.)

---

## 완성 체크리스트

- [ ] `runtime-image/Dockerfile` — `-devel` 베이스, `-cudart shared`로 `test_alloc` 컴파일, 훅은 마운트 지점만.
- [ ] `entrypoint.sh` — env 로깅 + 훅 존재 확인 + `exec "$@"`.
- [ ] `build_image.sh` / `run_in_container.sh` — 컨텍스트=루트, baseline/hooked 대조.
- [ ] Stage 2 게이트: baseline `[fgpu]` 없음, hooked `ALLOW`+`DENY`+`FREE`.
- [ ] `runtime-image-pytorch/Dockerfile` — `FROM` 베이스 + torch cu121 + 캐싱 off ENV.
- [ ] `test_pytorch.py` — 256 MB OK, 4 GB 조건부 OOM, `OutOfMemoryError` 포착.
- [ ] Stage 4 게이트: ratio 0.4 → OOM 전파, ratio 0.6 → 둘 다 OK.
- [ ] "구워지는 것 vs 마운트되는 것"을 구분하고 재빌드 시점을 안다.

## 다음 챕터

**3장 — 백엔드(Stage 3, 8~11)**로 넘어가면, 지금까지 손으로 치던 `docker run`을 FastAPI + Docker SDK가 대신하게 됩니다. `POST /sessions` 한 방으로 컨테이너를 띄우고, SQLite로 세션을 영속화하고, admission으로 sum(ratios) ≤ 1을 지키는 흐름을 빈 `backend/`에서부터 쌓아 올립니다. (이 문서 세트의 3장을 참고하세요.) 그리고 **4장(Stage 5)**에서는 이 백엔드 위에 평가 인프라와 웹 UI를 얹어 논문 데이터를 뽑습니다.
