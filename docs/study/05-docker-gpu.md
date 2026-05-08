# Chapter 05 — Docker + nvidia-container-toolkit

## 학습 목표

- `docker run --gpus all` 이 *실제로* 어떤 일을 하는지 안다.
- nvidia-container-toolkit 의 prestart hook 메커니즘을 안다.
- 우리 hook `.so` 가 컨테이너 안 어디로 어떻게 들어가는지 그릴 수 있다.
- bind mount 의 read-only / read-write 구분을 한다.
- 멀티 GPU 호스트에서 `--gpus device=N` 으로 디바이스 핀하는 효과를 안다.

---

## 5.1 컨테이너 = chroot + namespace + cgroup

먼저 단단한 기초부터. Docker 컨테이너는 가상머신이 아니에요.

```
[VM]    호스트 OS → 하이퍼바이저 → 게스트 OS 커널 → 게스트 프로세스
[컨테이너] 호스트 OS 커널 → 격리된 namespace 안의 호스트 프로세스
```

컨테이너 안 프로세스는 *호스트 커널을 직접* 씁니다. 격리는 다음 셋의 합:

- **namespace**: PID, mount, network, user 등을 분리. 컨테이너 안에서 `ps` 해도 자기만 보임.
- **cgroup**: CPU/메모리 *총량* 제한.
- **filesystem**: chroot-like 한 root → 컨테이너 이미지의 layered filesystem.

GPU 는 디바이스 파일 (`/dev/nvidia0`, `/dev/nvidiactl`, `/dev/nvidia-uvm`) 로 노출되는데, 기본 컨테이너는 호스트 디바이스 파일을 못 봅니다. 그래서 **별도 메커니즘** 이 필요해요.

### 더 공부하려면
- [Julia Evans — How containers work: overlayfs / namespaces / cgroups](https://wizardzines.com/zines/containers/)
- `man 7 namespaces`, `man 7 cgroups`

---

## 5.2 nvidia-container-toolkit 의 역할

NVIDIA 가 만든 패키지입니다. 핵심 컴포넌트:

```
docker run --gpus all
   │
   │ docker daemon 이 OCI runtime 을 호출
   ▼
nvidia-container-runtime (= runc + prestart hook)
   │
   │ 컨테이너 시작 *직전* 에 nvidia-container-cli 호출
   ▼
nvidia-container-cli configure
   - 호스트의 /dev/nvidia* 디바이스 파일을 컨테이너 안으로 expose
   - 호스트의 libcuda.so, libnvidia-*.so 를 컨테이너 안 적절한 경로로 bind mount
   - 환경변수 (NVIDIA_VISIBLE_DEVICES 등) 처리
   ▼
runc 가 격리된 환경에서 컨테이너 entrypoint 실행
```

요약: **컨테이너 자체에는 NVIDIA 드라이버가 없고**, *호스트의* 드라이버 파일을 컨테이너 안으로 *바인딩* 해서 보여주는 거예요. 이 때문에 컨테이너 이미지(`nvidia/cuda:*`) 는 가벼울 수 있습니다 — 헤더와 toolkit 만 들어가고 드라이버는 호스트 것을 빌려씀.

### 호스트와 컨테이너의 버전 매칭

호스트의 NVIDIA 드라이버 버전이 컨테이너의 CUDA toolkit 버전보다 *같거나 높아야* 합니다. 예: 호스트 driver 535 (CUDA 12.x 호환) → 컨테이너 CUDA 12.4 OK. 반대로 호스트 driver 470 (CUDA 11.x) + 컨테이너 CUDA 12.x 는 깨짐.

### 더 공부하려면
- [NVIDIA Container Toolkit — 공식 GitHub](https://github.com/NVIDIA/nvidia-container-toolkit)
- [공식 설치 가이드](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)

---

## 5.3 우리 hook `.so` 는 어떻게 컨테이너에 들어가나?

[runtime-image/Dockerfile](../../runtime-image/Dockerfile) 은 hook `.so` 를 *이미지에 굽지 않습니다*. 대신 [docker_manager.py:106-111](../../backend/app/services/docker_manager.py#L106-L111) 에서 bind mount:

```python
volumes = {
    self.host_hook_path: {                # 예: /home/jaewoo/.../build/libfgpu.so
        "bind": self.container_hook_path,  # 예: /opt/fgpu/libfgpu.so
        "mode": "ro",                      # 읽기 전용 — 컨테이너가 못 고침
    }
}
```

이게 왜 좋나? — **hook 을 고치면 이미지 재빌드 없이** 다시 `docker run` 하면 새 hook 이 적용됩니다. 빠른 개발 사이클에 결정적.

### LD_PRELOAD 주입

같은 docker_manager.py 에서:
```python
env = {
    "FGPU_RATIO": str(ratio),
    "LD_PRELOAD": self.container_hook_path,   # = /opt/fgpu/libfgpu.so
}
```

컨테이너가 떠서 entrypoint 가 사용자 명령을 `exec` 하는 순간, `ld.so` 가 `LD_PRELOAD` 를 보고 우리 `.so` 를 가장 먼저 로드 — 이 흐름은 [Chapter 01](01-ld-preload.md) 그대로.

### 왜 별도 경로(`/opt/fgpu/`) 인가?

nvidia-container-toolkit 이 `/usr/lib/x86_64-linux-gnu/libcuda.so.*` 같은 경로에 호스트 라이브러리를 bind mount 합니다. 우리가 같은 경로를 쓰면 충돌 가능. **전혀 안 겹치는 새 디렉토리** 에 두는 게 안전 — 그게 `/opt/fgpu/`.

---

## 5.4 `--gpus all` vs `--gpus device=N`

[docker_manager.py:97-103](../../backend/app/services/docker_manager.py#L97-L103):

```python
if gpu_index is None:
    device_requests = [DeviceRequest(count=-1, capabilities=[["gpu"]])]
else:
    device_requests = [
        DeviceRequest(device_ids=[str(gpu_index)],
                      capabilities=[["gpu"]])
    ]
```

매핑:
- `count=-1` → docker CLI 의 `--gpus all` 과 동등. 모든 GPU 노출.
- `device_ids=["0"]` → `--gpus device=0`. 0번 GPU 만 노출, 컨테이너 안에서 `nvidia-smi -L` 하면 GPU 한 개만 보임.

### 멀티 GPU 호스트에서의 의미

8 GB GPU 두 개가 있고 컨테이너 A 에 `gpu_index=0`, B 에 `gpu_index=1` 을 주면:
- A 의 hook 은 `cudaMemGetInfo(total) = 8GB` 를 봄 → quota = ratio × 8GB.
- B 도 동일. 하지만 *물리적으로 다른 카드* 라 서로의 메모리에 영향 없음.

이게 [Chapter 13 — admission control](13-admission-control.md) 에서 "GPU overlap" 을 정의하는 배경입니다. 다른 device 끼리는 격리.

---

## 5.5 entrypoint 의 역할

[runtime-image/entrypoint.sh](../../runtime-image/entrypoint.sh) 는 매우 짧습니다. 보통 이런 모양:

```bash
#!/usr/bin/env bash
echo "[entrypoint] FGPU_RATIO=${FGPU_RATIO:-(unset)}"
echo "[entrypoint] LD_PRELOAD=${LD_PRELOAD:-(unset)}"
[ -f "$LD_PRELOAD" ] && echo "[entrypoint] hook .so ✓" || echo "[entrypoint] hook .so 없음 ✗"
exec "$@"
```

세 가지 일:
1. 디버그 로그 (어떤 env 가 들어왔는지 확인 가능).
2. hook `.so` 가 마운트됐는지 sanity check.
3. `exec "$@"` — 사용자가 `docker run ... <CMD>` 로 준 명령을 *현재 PID* 그대로 갈아끼움 (자식 프로세스가 아니라).

### `exec` 가 *왜* 중요한가?

`exec` 없이 `$@` 만 부르면 entrypoint 가 부모로 남고 사용자 프로그램이 자식으로 뜹니다. 그러면 docker 가 보낸 SIGTERM 이 entrypoint 한테 가고 사용자 프로그램은 못 받음 → graceful shutdown 안 됨.

`exec` 는 동일 PID 에서 프로세스 이미지를 *교체* — 사용자 프로그램이 PID 1 이 되어 시그널을 직접 받음.

### 더 공부하려면
- `man 1 exec` (bash builtin)
- [Container PID 1 problem 글들 검색](https://www.google.com/search?q=docker+pid+1+zombie+reaping)

---

## 5.6 이미지 계층

```
fgpu-runtime-pytorch:stage4
   ↓ FROM
fgpu-runtime:stage2
   ↓ FROM
nvidia/cuda:12.4.1-devel-ubuntu22.04
   ↓ FROM
ubuntu:22.04
```

각 단계가 추가하는 것:
- `ubuntu:22.04`: 기본 OS.
- `nvidia/cuda:*-devel`: CUDA toolkit 헤더 + nvcc + libcudart 등 *개발* 도구.
- `fgpu-runtime:stage2`: 우리 `test_alloc` 등 검증용 binary 미리 컴파일 + entrypoint.sh.
- `fgpu-runtime-pytorch:stage4`: 위에 PyTorch (cu121 wheel) + `PYTORCH_NO_CUDA_MEMORY_CACHING=1` ENV.

각 `FROM` 은 *변경분만 layer 로 쌓는* docker 의 구조 그대로. 같은 base 를 여러 상위 이미지가 공유하면 디스크 절약.

---

## 5.7 직접 해보기 — 컨테이너 내부에서 hook 확인

```bash
# 1) 이미지 + hook 빌드
./scripts/build_hook.sh
./scripts/build_image.sh

# 2) 컨테이너 안에 들어가서 직접 둘러보기
docker run -it --rm --gpus all \
    -v $PWD/build/libfgpu.so:/opt/fgpu/libfgpu.so:ro \
    -e LD_PRELOAD=/opt/fgpu/libfgpu.so \
    -e FGPU_RATIO=0.4 \
    fgpu-runtime:stage2 bash

# 컨테이너 안에서:
ls -la /opt/fgpu/                        # libfgpu.so 가 있어야 함
echo $LD_PRELOAD                          # /opt/fgpu/libfgpu.so
nvidia-smi                                # GPU 보여야 함
ldd /opt/fgpu/test_alloc | grep cuda      # libcudart 가 동적으로 매핑돼 있어야 함
/opt/fgpu/test_alloc                      # hook stderr 출력 + ALLOW/DENY
```

마지막 명령이 의도대로 동작하면 — *우리가 만든 .so 가 NVIDIA 의 cudart 보다 먼저 로드되어 cudaMalloc 을 가로챘다* — 가 한 번에 증명됩니다.

---

## 5.8 자주 만나는 함정

| 증상 | 원인 | 해결 |
|---|---|---|
| `docker: Error response from daemon: could not select device driver "" with capabilities: [[gpu]]` | nvidia-container-toolkit 미설치 또는 daemon 재시작 누락 | 공식 설치 가이드 다시 + `sudo systemctl restart docker` |
| 컨테이너 안에서 `nvidia-smi: command not found` | base image 에 utility 없음 | `nvidia/cuda:*-base` 대신 `*-runtime` 또는 `*-devel` 사용 |
| `[fgpu]` 라인이 한 줄도 안 나옴 | LD_PRELOAD 환경변수 미주입 또는 `.so` 가 컨테이너 안에 없음 | `docker exec ... env` 로 LD_PRELOAD 확인, `ls /opt/fgpu/` 로 마운트 확인 |
| 컨테이너에서 `cudaMemGetInfo` 가 0 / 0 반환 | nvidia-container-toolkit 가 디바이스를 못 expose | `nvidia-container-cli info` 로 호스트 디바이스 점검 |

---

## 자가점검 질문

1. 컨테이너 안 프로세스가 GPU 를 쓰려면 *호스트의* 어떤 파일들이 컨테이너 안으로 들어와 있어야 하나? (힌트: 디바이스 파일 + 라이브러리)
2. `--gpus all` 과 `--gpus device=0` 의 차이를 말하라.
3. 우리 hook `.so` 를 이미지에 굽지 않고 bind mount 로 주입하는 *장점* 두 가지는?
4. entrypoint 마지막에 `exec "$@"` 가 *없으면* 어떤 문제가 생기나?
5. 호스트 NVIDIA 드라이버가 너무 오래된 경우 컨테이너 CUDA 12.x 가 동작할까?

→ [Chapter 06: FastAPI 백엔드 구조](06-fastapi-backend.md)

---

## 외부 자료 종합

- 📚 [NVIDIA Container Toolkit Docs](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/index.html)
- 📚 [Docker Engine — Runtime options with Memory, CPUs, and GPUs](https://docs.docker.com/config/containers/resource_constraints/#gpu)
- 📖 *Docker Deep Dive* by Nigel Poulton — 책 한 권. 컨테이너 처음이면 추천.
- 🎥 Julia Evans 의 컨테이너 zine — 그림 많음
