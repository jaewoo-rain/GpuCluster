# 3장. Docker 와 GPU 컨테이너 — 격리된 실행 상자

> 📘 **이 장을 읽고 나면**
>
> - 컨테이너가 무엇인지, 가상머신(VM)과 뭐가 다른지 한 문장으로 설명할 수 있어요.
> - 이미지와 컨테이너의 차이, Dockerfile 이 "레시피"라는 걸 이해해요.
> - 이 프로젝트가 `docker run` 에 왜 `--gpus`, `-v`, `-e`, `--rm` 같은 옵션을 붙이는지 알 수 있어요.
> - 무엇보다 **"훅(`libfgpu.so`)을 이미지에 굽지 않고 실행할 때 마운트한다"** 는 이 프로젝트 핵심 설계 결정의 이유를 이해하게 됩니다.

> 이 프로젝트가 뭐 하는 물건인지 아직 흐릿하다면, 저장소 루트의 `CLAUDE.md` 를 먼저 훑어보세요. 한 줄 요약: **NVIDIA GPU 한 장을 여러 도커 컨테이너가 나눠 쓰게 하고, 각 컨테이너가 쓸 수 있는 GPU 메모리에 상한(쿼터)을 거는 프로토타입** 입니다.

---

## 3.1 컨테이너란 무엇인가

### (1) 왜 필요한가 / 이 프로젝트에서 왜 중요한가

이 프로젝트의 목표는 "GPU 한 장을 여러 사용자가 나눠 쓰기" 입니다. 그러려면 사용자 A 의 프로그램과 사용자 B 의 프로그램이 **서로 간섭하지 않는 별도의 상자** 안에서 돌아야 해요. A 가 설치한 파이썬 버전, A 가 만든 파일, A 의 환경변수가 B 에게 새어 나가면 곤란하죠.

컨테이너는 바로 그 "별도의 상자" 입니다. 프로그램 하나를, 그 프로그램이 필요로 하는 라이브러리/파일/환경까지 통째로 담아서, 다른 상자들과 격리된 채로 실행합니다.

### (2) 일상 비유

컨테이너는 **밀폐 도시락통** 이라고 생각하세요.

- 도시락통 안에는 밥, 반찬, 젓가락(= 프로그램 + 라이브러리 + 설정)이 다 들어 있어요.
- 여러 도시락통을 한 책상(= 한 대의 컴퓨터, 여기선 GPU 서버) 위에 올려놔도 서로 반찬이 섞이지 않아요.
- 다 먹으면 통째로 버리면 됩니다. 책상엔 흔적이 안 남아요.

가상머신(VM)과의 차이는? VM 은 **책상마다 냉장고+주방+가스레인지를 통째로 새로 놓는 것** 에 가까워요(운영체제 전체를 통째로 복제). 무겁고 느리게 뜹니다. 컨테이너는 주방(=호스트의 리눅스 커널)은 공유하고 도시락통만 따로 두는 방식이라 훨씬 가볍고 1초 만에 뜹니다. 그래서 "세션 하나 만들 때마다 컨테이너 하나"라는 이 프로젝트 방식이 현실적인 거예요.

### (3) 작은 예시

```bash
# ubuntu 라는 이미지로 컨테이너를 하나 띄우고, 그 안에서 echo 한 줄 실행
docker run --rm ubuntu echo "안녕, 나는 격리된 상자 안이야"
```

- `docker run` : 상자를 하나 만들어서 실행해라.
- `ubuntu` : 어떤 재료(이미지)로 만들지.
- `--rm` : 다 끝나면 상자를 버려라(뒤에서 설명).
- `echo ...` : 상자 안에서 실행할 명령.

### (4) 이미지 vs 컨테이너 — 헷갈리는 두 단어

| 용어 | 비유 | 설명 |
|------|------|------|
| **이미지(image)** | 붕어빵 **틀** / 도시락 **레시피** | 실행에 필요한 파일 묶음. 읽기 전용. 변하지 않음. |
| **컨테이너(container)** | 틀로 찍어낸 붕어빵 / 실제로 싼 도시락 | 이미지를 실제로 "실행한" 살아있는 인스턴스. 여러 개 찍어낼 수 있음. |

이미지 하나로 컨테이너 여러 개를 찍어낼 수 있습니다. 이 프로젝트에서 세션 A, 세션 B 는 같은 이미지(`fgpu-runtime:stage2`)에서 찍어낸 서로 다른 컨테이너예요.

### (5) 흔한 함정

- 이미지를 고쳤는데 이미 떠 있는 컨테이너에는 반영이 안 돼요. 컨테이너는 "찍어낸 순간의 스냅샷"이라, 이미지를 바꾸면 **다시 찍어야(다시 `docker run`)** 합니다.
- 컨테이너 안에서 파일을 만들고 컨테이너를 지우면 그 파일도 같이 사라집니다(도시락통을 버리면 안의 밥도 사라지듯). 살려두려면 뒤에 나오는 `-v`(bind mount)가 필요해요.

### (6) 한 줄 요약

> 컨테이너는 프로그램을 통째로 담은 가벼운 격리 도시락통이고, 이미지는 그 도시락통을 찍어내는 레시피예요.

---

## 3.2 Dockerfile — 이미지를 만드는 레시피

### (1) 왜 필요한가

이 프로젝트는 컨테이너 안에서 CUDA 프로그램(예: `test_alloc`)이 돌아야 하고, 그 프로그램은 CUDA 컴파일러(`nvcc`)로 미리 컴파일돼 있어야 합니다. "어떤 베이스 위에, 무엇을 설치하고, 무엇을 컴파일해서 이미지를 만들지"를 글로 적어둔 파일이 **Dockerfile** 이에요. 사람이 매번 손으로 설치하지 않고, 이 레시피만 있으면 누구나 똑같은 이미지를 재현할 수 있습니다.

### (2) 일상 비유

Dockerfile 은 **요리 레시피 카드** 입니다. "1) 기본 육수(베이스 이미지)를 준비한다. 2) 재료를 넣는다. 3) 끓인다." 처럼 위에서 아래로 한 줄씩 실행돼요. 각 줄이 이미지에 층(layer)을 하나씩 쌓습니다.

### (3) 작은 예시 (개념용)

```dockerfile
FROM ubuntu           # 1) 우분투를 기본 재료로 삼는다
RUN apt install curl  # 2) curl 을 설치한다
COPY app.py /app.py   # 3) 내 파일을 상자 안으로 복사한다
CMD ["python3", "/app.py"]  # 4) 상자를 실행하면 이 명령을 돌린다
```

### (4) 이 프로젝트의 실제 Dockerfile 해설

파일: [`runtime-image/Dockerfile`](../../runtime-image/Dockerfile). 초보 눈높이로 핵심 줄만 뜯어볼게요.

**베이스 이미지 고르기** — [`runtime-image/Dockerfile:26`](../../runtime-image/Dockerfile#L26)

```dockerfile
FROM nvidia/cuda:${CUDA_VERSION}-devel-ubuntu22.04
```

- `nvidia/cuda:...` : NVIDIA 가 공식으로 만들어 배포하는, CUDA 가 이미 깔려 있는 이미지예요. 우리가 CUDA 를 손으로 설치할 필요가 없죠.
- `-devel-` : 이 부분이 중요해요. NVIDIA 는 두 종류를 줍니다.
  - `runtime` 이미지: CUDA 프로그램을 **실행만** 할 수 있음(가벼움).
  - `devel` 이미지: 실행 + **컴파일**까지 가능(`nvcc` 포함, 무거움).
  - 우리는 컨테이너 안에서 테스트 프로그램을 컴파일해야 하므로 `devel` 을 골랐습니다. 이유는 파일 주석에도 적혀 있어요([`runtime-image/Dockerfile:12`](../../runtime-image/Dockerfile#L12)): "runtime 이미지보다 크지만 프로토타입이라 디스크 크기보다 재현성이 우선."

**테스트 소스 복사** — [`runtime-image/Dockerfile:36`](../../runtime-image/Dockerfile#L36)

```dockerfile
COPY hook/tests/test_alloc.cu /tmp/test_alloc.cu
```

`COPY 호스트경로 컨테이너경로` : 내 컴퓨터(빌드 문맥)의 파일을 이미지 안으로 복사합니다. `.cu` 는 CUDA 소스 코드예요.

**컨테이너 안에서 컴파일** — [`runtime-image/Dockerfile:46`](../../runtime-image/Dockerfile#L46)

```dockerfile
RUN nvcc -O2 -cudart shared -o /opt/fgpu/test_alloc /tmp/test_alloc.cu ...
```

- `RUN` : 이미지를 만드는 동안 이 명령을 실행하고 결과를 이미지에 굽습니다.
- `nvcc` : CUDA 컴파일러. `.cu` 소스를 실행 파일(`/opt/fgpu/test_alloc`)로 만듭니다.
- `-cudart shared` : **이 옵션이 없으면 우리 프로젝트가 통째로 안 돌아갑니다.** CUDA 12.x 의 `nvcc` 는 기본적으로 CUDA 런타임을 프로그램 안에 통째로 박아버리는(static link) 방식인데, 그러면 우리 훅이 `cudaMalloc` 을 가로챌 수가 없어요. `shared`(동적 링크)로 해야 훅이 끼어들 틈이 생깁니다. 왜 그런지는 2장(LD_PRELOAD)에서 다뤘어요. 주석은 [`runtime-image/Dockerfile:44`](../../runtime-image/Dockerfile#L44) 에 있습니다.

**진입점과 기본 명령** — [`runtime-image/Dockerfile:58`](../../runtime-image/Dockerfile#L58)

```dockerfile
ENTRYPOINT ["/usr/local/bin/fgpu-entrypoint"]
CMD ["/opt/fgpu/test_alloc"]
```

- `ENTRYPOINT` : 컨테이너가 뜰 때 **항상 먼저 실행**되는 스크립트(3.5절에서 설명).
- `CMD` : 아무 명령도 안 주면 실행할 **기본** 명령. `docker run ... 다른명령` 으로 덮어쓸 수 있어요.

### (5) 흔한 함정

- Dockerfile 을 고쳤으면 **이미지를 다시 빌드**해야 반영됩니다(`scripts/build_image.sh`). 안 그러면 옛날 이미지가 계속 쓰여요.
- `COPY` 의 호스트 경로는 "빌드 문맥(build context)" 기준의 상대 경로예요. 이 프로젝트는 저장소 루트에서 빌드하므로 `hook/tests/...` 처럼 루트 기준으로 적혀 있습니다([`scripts/build_image.sh:22`](../../scripts/build_image.sh#L22) 의 `docker build ... .` 에서 맨 끝 `.` 이 "빌드 문맥 = 현재 폴더" 라는 뜻).

### (6) 한 줄 요약

> Dockerfile 은 "어떤 베이스 위에 뭘 설치·컴파일해서 이미지를 만들지"를 위에서 아래로 적은 레시피이고, 우리는 `devel` 베이스에 테스트 바이너리를 `-cudart shared` 로 미리 구워 둡니다.

---

## 3.3 `docker run` 의 핵심 옵션 — 이 프로젝트 맥락으로

이제 실제로 컨테이너를 띄우는 명령을 뜯어봅니다. 이 프로젝트에서 훅이 켜진 컨테이너를 띄우는 실제 명령은 [`scripts/run_in_container.sh:43`](../../scripts/run_in_container.sh#L43) 에 있어요.

```bash
docker run --rm --gpus all \
    -v "${HOOK_SO_HOST}:/opt/fgpu/libfgpu.so:ro" \
    -e LD_PRELOAD=/opt/fgpu/libfgpu.so \
    -e FGPU_RATIO="${RATIO}" \
    "${IMAGE_NAME}:${IMAGE_TAG}"
```

이 한 덩어리에 이 프로젝트의 핵심 아이디어가 다 들어 있어요. 옵션 하나씩 봅시다.

### `--gpus all` / `--gpus device=N` — 상자에 GPU 를 넣어준다

**(1) 왜** — 기본적으로 컨테이너는 GPU 를 못 봐요(격리되어 있으니까). 도시락통에 GPU 를 넣어달라고 명시해야 안에서 CUDA 프로그램이 GPU 를 쓸 수 있습니다.

**(2) 비유** — 밀폐 도시락통에 "젓가락 넣어주세요" 라고 요청하는 것. `all` 은 젓가락 전부, `device=1` 은 두 번째 젓가락만.

**(4) 실제 코드** — 스크립트에선 [`scripts/run_in_container.sh:43`](../../scripts/run_in_container.sh#L43) 의 `--gpus all`. 백엔드에서는 파이썬 도커 SDK 로 같은 걸 조립합니다 — [`backend/app/services/docker_manager.py:102`](../../backend/app/services/docker_manager.py#L102):

```python
if gpu_index is None:
    device_requests = [DeviceRequest(count=-1, capabilities=[["gpu"]])]   # = --gpus all
else:
    device_requests = [DeviceRequest(device_ids=[str(gpu_index)], ...)]   # = --gpus device=N
```

`gpu_index` 가 없으면 전체 GPU, 지정하면 그 번호의 GPU 만 붙입니다(멀티 GPU 서버용, Stage 9).

**(5) 함정** — `--gpus` 를 빼먹으면 컨테이너 안에서 `nvidia-smi` 가 안 보이고 CUDA 초기화가 실패합니다. GPU 관련 오류가 나면 제일 먼저 이 옵션부터 의심하세요.

### `-v 호스트경로:컨테이너경로:ro` — bind mount (이 프로젝트의 심장)

**(1) 왜 이게 이 프로젝트의 핵심인가** — 우리 훅 `libfgpu.so` 는 **호스트에서 빌드**됩니다([`scripts/build_hook.sh`](../../scripts/build_hook.sh)). 이 `.so` 를 컨테이너 안으로 넣어야 `LD_PRELOAD` 로 끼워 넣을 수 있는데, **이미지에 굽지 않고 실행할 때 갖다 붙입니다.** 그 수단이 `-v`(bind mount)예요.

**(2) 비유** — bind mount 는 도시락통에 **뚜껑 구멍을 뚫어서 내 책상 위 물건을 그대로 들여다보게** 하는 것. 컨테이너가 그 경로를 열면 사실은 호스트의 파일을 보고 있는 거예요. 복사가 아니라 "연결"입니다.

**(3) 작은 예시**

```bash
docker run -v /home/me/data:/data ubuntu ls /data
# 컨테이너 안의 /data 를 열면 사실 호스트의 /home/me/data 가 보임
```

**(4) 실제 코드** — 스크립트: [`scripts/run_in_container.sh:44`](../../scripts/run_in_container.sh#L44)

```bash
-v "${HOOK_SO_HOST}:/opt/fgpu/libfgpu.so:ro"
```

호스트의 `build/libfgpu.so` 를 컨테이너 안 `/opt/fgpu/libfgpu.so` 로 연결하고, `:ro` 로 읽기 전용(컨테이너가 실수로 못 고치게)으로 만듭니다.

백엔드도 똑같이 조립해요 — [`backend/app/services/docker_manager.py:111`](../../backend/app/services/docker_manager.py#L111):

```python
volumes = {
    self.host_hook_path: {"bind": self.container_hook_path, "mode": "ro"}
}
```

**(5) 함정** — 호스트 경로는 반드시 **절대 경로**여야 합니다. 상대 경로를 주면 도커가 이름 붙은 볼륨으로 오해할 수 있어요. 그리고 마운트한 `.so` 가 컨테이너 안 CUDA 와 버전이 안 맞으면(예: 호스트 CUDA 11, 컨테이너 CUDA 12) 동적 링크가 실패합니다. 그래서 "CUDA major 버전 맞추기" 를 계속 강조하는 거예요([`runtime-image/Dockerfile:21`](../../runtime-image/Dockerfile#L21)).

### `-e 변수=값` — 환경변수 주입 (훅에게 명령 전달)

**(1) 왜** — 훅은 "메모리를 몇 %까지 허용할지" 를 알아야 합니다. 그 값을 `-e FGPU_RATIO=0.4` 로 넣어주면 훅이 읽어서 쿼터를 계산해요. 그리고 `-e LD_PRELOAD=...` 로 "이 `.so` 를 프로그램보다 먼저 끼워라" 고 리눅스 동적 링커에게 지시합니다.

**(2) 비유** — 도시락통에 붙이는 **메모 쪽지**. "이 통은 매운맛 40%로 해주세요(FGPU_RATIO=0.4)."

**(4) 실제 코드** — 스크립트: [`scripts/run_in_container.sh:45`](../../scripts/run_in_container.sh#L45)

```bash
-e LD_PRELOAD=/opt/fgpu/libfgpu.so \
-e FGPU_RATIO="${RATIO}" \
```

백엔드: [`backend/app/services/docker_manager.py:80`](../../backend/app/services/docker_manager.py#L80)

```python
env = {
    "FGPU_RATIO": str(ratio),
    "LD_PRELOAD": self.container_hook_path,
}
```

**(5) 함정** — `LD_PRELOAD` 를 이미지에 기본값(ENV)으로 박아두면 안 돼요. 마운트를 깜빡하고 컨테이너를 띄우면 "가리키는 `.so` 가 없다" 며 동적 링커가 요란하게 실패합니다. 그래서 이 프로젝트는 일부러 실행할 때 `-e` 로만 넘깁니다([`runtime-image/Dockerfile:18`](../../runtime-image/Dockerfile#L18)).

### `--rm` — 끝나면 상자를 버려라

**(1) 왜** — 검증용 컨테이너는 한 번 쓰고 버립니다. `--rm` 이 없으면 종료된 컨테이너 껍데기가 계속 쌓여서 `docker ps -a` 가 지저분해져요.

**(2) 비유** — 일회용 도시락통. 다 먹으면 자동으로 버려짐.

**(4) 실제 코드** — [`scripts/run_in_container.sh:43`](../../scripts/run_in_container.sh#L43) 의 `--rm`.

**(5) 주의 — 백엔드는 일부러 `--rm` 을 안 씁니다.** [`backend/app/services/docker_manager.py:130`](../../backend/app/services/docker_manager.py#L130) 를 보면 `remove=False` 예요. 이유: 종료된 뒤에도 로그를 조회할 수 있어야 하니까요(주석에 명시). 검증 스크립트(한 번 쓰고 버림)와 백엔드(로그 남김)의 목적이 달라서 그렇습니다.

### `--entrypoint` — 진입점 덮어쓰기

**(1) 왜** — 이미지에는 기본 진입점(`ENTRYPOINT`)이 정해져 있는데, 특정 실험에서는 그걸 무시하고 다른 프로그램을 바로 실행하고 싶을 때가 있어요. 예를 들어 오버헤드 벤치마크는 entrypoint 스크립트를 거치지 않고 `bench_alloc` 을 바로 돌립니다(측정 대상이 순수 훅 오버헤드라서).

**(2) 비유** — 도시락통에 붙은 "먼저 데우세요" 스티커를 떼고 곧장 먹는 것.

**(4) 실제 코드** — `run_overhead.sh` 가 `docker run --entrypoint /opt/fgpu/bench_alloc ...` 형태로 씁니다(CLAUDE.md 의 5-D 설명 참조). 기본 진입점은 3.5절의 `entrypoint.sh` 예요.

### 한 줄 요약

> `docker run` 옵션들은 각각 "GPU 넣기(`--gpus`)", "훅 파일 연결하기(`-v`)", "훅에게 설정 알려주기(`-e`)", "쓰고 버리기(`--rm`)" 를 담당하고, 이 조합이 곧 fGPU 의 실행 방식입니다.

---

## 3.4 nvidia-container-toolkit — 왜 필요한가

컨테이너는 원래 GPU 를 못 봅니다. `--gpus all` 이라고 적어도, 도커 혼자서는 호스트의 NVIDIA 드라이버와 GPU 장치 파일을 컨테이너 안으로 어떻게 넣어줘야 할지 모릅니다. **nvidia-container-toolkit** 은 도커와 NVIDIA 드라이버 사이의 통역사예요. 이게 설치돼 있어야 도커가 `--gpus` 옵션을 이해하고, 컨테이너 시작 시점에 GPU 장치와 드라이버 라이브러리를 상자 안에 자동으로 넣어줍니다. 그래서 이 프로젝트를 새 서버에서 처음 돌릴 때 반드시 설치해야 하는 항목이고([`LINUX_SETUP.md`](../../LINUX_SETUP.md) 참조), `run_all_tests.sh` 도 시작하자마자 `docker run --gpus all ... nvidia-smi` 로 이게 되는지 먼저 확인합니다([`scripts/run_all_tests.sh:81`](../../scripts/run_all_tests.sh#L81)). 이 preflight 가 실패하면 "nvidia-container-toolkit 설치 필요" 라고 알려주고 멈춰요.

> 한 줄 요약: nvidia-container-toolkit 은 "`--gpus` 옵션을 도커가 실제 GPU 로 연결해주는 통역사" 이고, 없으면 컨테이너 안에서 GPU 가 안 보입니다.

---

## 3.5 entrypoint.sh — 컨테이너가 뜰 때 가장 먼저 하는 일

### (1) 왜 필요한가

컨테이너가 뜰 때, 사용자 프로그램을 곧장 실행하기 전에 "환경이 제대로 갖춰졌는지" 를 한번 점검하면 디버깅이 훨씬 쉬워집니다. 훅 파일이 정말 마운트됐나? `FGPU_RATIO` 는 얼마로 들어왔나? 이걸 로그로 찍어주는 게 진입점 스크립트예요.

### (2) 일상 비유

도시락통을 열기 전, **뚜껑에 적힌 라벨을 소리 내어 확인**하는 사람. "매운맛 40%, 젓가락 있음, 반찬통 연결됨. 좋아, 이제 먹자."

### (4) 실제 코드 해설

파일: [`runtime-image/entrypoint.sh`](../../runtime-image/entrypoint.sh). 하는 일은 세 가지예요.

**1) 환경 상태 출력** — [`runtime-image/entrypoint.sh:19`](../../runtime-image/entrypoint.sh#L19)

```bash
echo "[entrypoint] container starting"
echo "[entrypoint]   FGPU_RATIO       = ${FGPU_RATIO:-<unset>}"
echo "[entrypoint]   LD_PRELOAD       = ${LD_PRELOAD:-<unset>}"
```

`${FGPU_RATIO:-<unset>}` 는 "변수가 있으면 그 값, 없으면 `<unset>` 을 출력" 이라는 셸 문법이에요. 시연이나 디버깅 때 한눈에 상태를 보여줍니다.

**2) 훅 파일 존재 확인** — [`runtime-image/entrypoint.sh:24`](../../runtime-image/entrypoint.sh#L24)

```bash
if [[ -n "${LD_PRELOAD:-}" ]]; then
    if [[ -f "${LD_PRELOAD}" ]]; then
        echo "[entrypoint]   hook .so OK ..."
    else
        echo "[entrypoint]   WARN: ... 인데 파일이 없음."
        ...
    fi
fi
```

`LD_PRELOAD` 가 가리키는 파일이 실제로 있는지 확인합니다. 없으면 **경고만 하고 계속 진행**해요(멈추지 않음). 사용자가 일부러 훅 없이 돌리고 싶을 수도 있으니까요.

**3) 사용자 명령 실행** — [`runtime-image/entrypoint.sh:35`](../../runtime-image/entrypoint.sh#L35)

```bash
exec "$@"
```

`"$@"` 는 "docker run 뒤에 붙인(또는 CMD 의) 명령 전체" 예요. `exec` 는 "이 스크립트를 그 명령으로 완전히 교체" 하라는 뜻입니다. 이렇게 해야 그 프로그램이 컨테이너의 PID 1 이 되어, 도커가 보내는 종료 신호(SIGTERM)를 제대로 받아 깔끔하게 멈출 수 있어요(주석 [`runtime-image/entrypoint.sh:11`](../../runtime-image/entrypoint.sh#L11)).

### (5) 흔한 함정

- `exec` 없이 그냥 `"$@"` 를 실행하면, 스크립트가 부모로 남아서 종료 신호가 자식 프로그램에 잘 전달되지 않아 `docker stop` 이 느려지거나 실패할 수 있어요.
- 진입점의 `[entrypoint]` 로그는 **stdout** 으로 나가고, 훅의 `[fgpu]` 로그는 stderr 로 나갑니다. 백엔드는 둘 다 합쳐서 보여줘요([`backend/app/services/docker_manager.py:165`](../../backend/app/services/docker_manager.py#L165)).

### (6) 한 줄 요약

> entrypoint.sh 는 컨테이너가 뜰 때 환경/마운트 상태를 라벨처럼 찍어 확인하고, `exec "$@"` 로 사용자 프로그램에 자리를 넘깁니다.

---

## 3.6 왜 훅을 "굽지 않고 마운트" 하는가 — 이 장의 핵심 설계 결정

앞에서 조각조각 봤지만, 한 번 더 강조할 만큼 중요합니다.

**선택지 두 가지가 있었어요.**

- (A) `libfgpu.so` 를 이미지 안에 `COPY` 로 구워버리기.
- (B) 이미지에는 안 굽고, 실행할 때 `-v` 로 마운트하기. ← **이 프로젝트가 고른 방식.**

**왜 (B)인가?**

1. **훅을 고쳐도 이미지를 다시 안 만들어도 됩니다.** 훅 코드는 개발 중에 계속 바뀝니다(Stage 5-C, 6, 7, 12에서 새 API 를 추가하죠). (A)였다면 훅을 고칠 때마다 몇 분씩 걸리는 이미지 재빌드를 해야 해요. (B)면 `build_hook.sh` 로 `.so` 만 다시 만들고, 다음 `docker run` 이 바뀐 파일을 그대로 집어 갑니다.
2. **훅 버전을 갈아끼우기 쉽습니다.** 백엔드가 여러 버전의 훅을 운용하거나, A/B 비교(같은 이미지에 다른 훅)를 하기에 유리해요.
3. **역할이 깔끔하게 나뉩니다.** 이미지 = "테스트 바이너리 + CUDA 환경"(잘 안 바뀜). 훅 = 호스트 산출물(자주 바뀜). 서로 독립적으로 진화합니다.

이 결정의 근거는 Dockerfile 주석에 그대로 적혀 있어요 — [`runtime-image/Dockerfile:15`](../../runtime-image/Dockerfile#L15): "libfgpu.so 는 이미지에 굽지 않고 런타임 마운트한다. 이렇게 해야 hook 을 고쳤을 때 이미지 재빌드가 필요 없다."

> 한 줄 요약: 훅은 자주 바뀌는 호스트 산출물이라, 이미지에 굽는 대신 실행 시 `-v` 로 갈아끼워서 재빌드 비용 없이 자유롭게 교체합니다.

---

## ✍️ 스스로 점검

1. 이미지와 컨테이너의 차이를 도시락 비유로 설명해 보세요. 같은 이미지에서 컨테이너 2개를 띄우면 왜 서로 격리되나요?
2. `docker run` 명령에서 `--gpus all`, `-v ...:...:ro`, `-e FGPU_RATIO=0.4` 는 각각 무슨 역할인가요? 이 중 하나를 빼먹으면 어떤 증상이 생길까요?
3. 이 프로젝트가 `libfgpu.so` 를 이미지에 `COPY` 로 굽지 않고 `-v` 로 마운트하기로 한 이유를 두 가지 이상 대보세요.

## 🎯 다음 챕터

다음은 **9장. 빌드·실행·검증 — 스테이지 워크플로우와 스크립트로 굴리기** 입니다. 지금까지 배운 컨테이너/이미지/훅 마운트가 실제로 어떤 순서의 스크립트(`build_hook.sh` → `build_image.sh` → `run_*_in_container.sh` → `run_backend.sh`)로 엮여 돌아가는지, 그리고 "훅 없이 한 번, 훅 켜고 한 번" 두 번 실행이 왜 검증의 핵심인지 배웁니다.

---

⟵ [이전: 2장. 쉘 스크립트](02-shell-scripts.md) ・ [📚 전체 목차](README.md) ・ [다음: 4장. LD_PRELOAD와 dlsym](04-ld-preload-and-dlsym.md) ⟶
