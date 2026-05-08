# Chapter 14 — Jupyter Lab 통합

## 학습 목표

- Jupyter Lab 이 노트북을 어떻게 서빙하는지 (HTTP + WebSocket) 한 줄로 안다.
- ephemeral port 의 의미와 docker 가 *어떻게* 호스트 포트를 자동 할당하는지 안다.
- bind mount 기반 워크스페이스가 *왜* 컨테이너 삭제와 독립인지 안다.
- token 기반 인증이 *왜* 임시 노트북에 충분한지 안다.

---

## 14.1 한 그림

```
브라우저 (사용자 PC)
    │ HTTP + WebSocket
    ▼
http://<host>:<host_port>/lab?token=...
    │
    │ docker 가 host_port → 컨테이너의 8888 로 forward
    ▼
[컨테이너]   jupyter lab --ip=0.0.0.0 --port=8888
    │
    │ /workspace 로 bind mount
    ▼
[호스트]     <repo>/data/sessions/<id>/   ← 노트북 .ipynb 들이 여기 산다
```

특징:
- Jupyter 자체는 *컨테이너 안에서* 도는 별도 프로세스. 우리 백엔드 (FastAPI) 와 무관.
- 사용자가 노트북 셀에서 `import torch; torch.empty(...)` 하면 우리 hook 이 가로챕니다 (LD_PRELOAD 가 그대로 살아 있어서).
- 노트북 파일은 호스트에 영속 — 컨테이너 삭제해도 안 사라짐.

---

## 14.2 SessionCreate 에 `mode` 추가

[backend/app/schemas/session.py](../../backend/app/schemas/session.py):

```python
SessionMode = Literal["batch", "jupyter"]

class SessionCreate(BaseModel):
    ratio: float = ...
    mode: SessionMode = "batch"
    ...
```

기본 `batch` — Stage 1~9 그대로. `jupyter` 면 다른 분기.

[session_manager.py:119-139](../../backend/app/services/session_manager.py#L119-L139) 에서 분기:

```python
if mode == "jupyter":
    img = image or _DEFAULT_JUPYTER_IMAGE
    jupyter_token = secrets.token_urlsafe(24)
    cmd = build_jupyter_command(jupyter_token)
    workspace_dir = os.path.join(self.workspace_root, sid)
    Path(workspace_dir).mkdir(parents=True, exist_ok=True)
    ports = {f"{_JUPYTER_CONTAINER_PORT}/tcp": None}  # ephemeral
    jupyter_mode = True
else:
    img = image or self.runtime_image
    cmd = command or list(self.default_command)
    ...
```

세 가지 자동:
1. 사용자 명령 무시, `jupyter lab ...` 강제.
2. 토큰을 `secrets.token_urlsafe(24)` 로 생성 — URL-safe 문자만 32 자리쯤.
3. 호스트 워크스페이스 디렉토리 생성.

---

## 14.3 `jupyter lab` 명령

[docker_manager.py:38-51](../../backend/app/services/docker_manager.py#L38-L51):

```python
def build_jupyter_command(token: str) -> list[str]:
    return [
        "jupyter", "lab",
        "--ip=0.0.0.0",
        f"--port={_JUPYTER_CONTAINER_PORT}",     # 컨테이너 안 8888
        "--no-browser",
        "--allow-root",
        f"--ServerApp.token={token}",
        "--ServerApp.password=",
        "--ServerApp.root_dir=/workspace",
        "--ServerApp.allow_origin=*",
        "--ServerApp.allow_remote_access=True",
    ]
```

옵션 의미:
- `--ip=0.0.0.0` — 컨테이너 내부의 모든 인터페이스에서 listen. docker port forwarding 이 외부에서 접근.
- `--no-browser` — 컨테이너 안에는 브라우저가 없으니 자동 열기 시도 안 함.
- `--allow-root` — 컨테이너가 root 로 도므로 (보안 경고 우회).
- `--ServerApp.token=...` — 우리가 만든 임의 token. 안 맞으면 401.
- `--ServerApp.root_dir=/workspace` — 노트북 시작 디렉토리.

### 더 공부하려면
- [Jupyter Server — Configuration](https://jupyter-server.readthedocs.io/en/latest/operators/security.html)
- [JupyterLab CLI options](https://jupyterlab.readthedocs.io/en/stable/getting_started/installation.html)

---

## 14.4 ephemeral port — 누가 할당하나?

```python
ports = {f"{_JUPYTER_CONTAINER_PORT}/tcp": None}   # None = ephemeral
```

`None` 을 호스트 포트로 주면 **docker daemon 이 호스트의 사용 가능한 임의 포트** 를 골라 forwarding 설정. 보통 32768~60999 범위.

이걸 *왜* 임의로 두나?
- 여러 jupyter 세션이 동시에 있을 수 있음. 같은 호스트 포트는 하나만.
- 사용자가 직접 포트를 고르게 하면 충돌 책임이 사용자에게.
- docker 에 맡기면 깔끔.

### 어디서 알게 되나?

컨테이너 시작 *후* `c.attrs['NetworkSettings']['Ports']` 를 읽으면 docker 가 정한 포트가 보입니다.

[docker_manager.py:141-155](../../backend/app/services/docker_manager.py#L141-L155):
```python
def get_host_port(self, container_id, container_port):
    c = self.client.containers.get(container_id)
    c.reload()
    bindings = c.attrs.get("NetworkSettings", {}).get("Ports") or {}
    entries = bindings.get(f"{container_port}/tcp") or []
    if not entries:
        return None
    return int(entries[0]["HostPort"])
```

[session_manager.py:158-172](../../backend/app/services/session_manager.py#L158-L172) 가 짧은 백오프로 재시도:

```python
for delay in (0.0, 0.1, 0.2, 0.4, 0.8):
    if delay:
        await asyncio.sleep(delay)
    host_port = await asyncio.to_thread(
        self.docker.get_host_port, c.id, _JUPYTER_CONTAINER_PORT
    )
    if host_port:
        break
```

컨테이너가 막 떴을 때 attrs 가 아직 채워지기 전일 수 있어요. 점차 길어지는 백오프로 부드럽게 폴링.

### 더 공부하려면
- [Docker — Container networking — port publishing](https://docs.docker.com/engine/network/)

---

## 14.5 bind mount 기반 워크스페이스

```
호스트:                컨테이너:
<repo>/data/sessions/<id>/  ←──→  /workspace
```

[docker_manager.py:114-118](../../backend/app/services/docker_manager.py#L114-L118):
```python
if jupyter_mode and workspace_host_dir:
    volumes[workspace_host_dir] = {
        "bind": "/workspace",
        "mode": "rw",
    }
```

특성:
- **rw 모드** — 컨테이너 안 jupyter 가 노트북을 저장하면 호스트 파일에 *그대로* 쓰임.
- **컨테이너 삭제 ≠ 데이터 삭제** — 호스트 디렉토리는 그대로 남음.
- 같은 워크스페이스에 다음에 다른 컨테이너를 마운트해도 노트북은 보임.

### 권한 이슈

호스트에서 `mkdir` 한 디렉토리는 보통 `root:root` 또는 운영자의 UID. 컨테이너 안 jupyter 가 root 로 도므로 (`--allow-root`) 충돌 없음. 다른 UID 로 실행하려면 `chown` 필요 — 본 프로토타입에선 불필요.

### 삭제 시 워크스페이스 처리

`DELETE /sessions/<id>` 의 default 는 워크스페이스 *보존* — 노트북은 사용자 데이터.
`DELETE /sessions/<id>?purge_workspace=true` 면 함께 삭제.

[session_manager.py:251-268](../../backend/app/services/session_manager.py#L251-L268):
```python
async def delete(self, sid, purge_workspace: bool = False) -> bool:
    rec = await asyncio.to_thread(self.store.get, sid)
    ...
    if purge_workspace and rec.workspace_dir:
        await asyncio.to_thread(lambda: shutil.rmtree(rec.workspace_dir, ignore_errors=True))
```

---

## 14.6 URL 조립

```
http://<PUBLIC_HOST>:<host_port>/lab?token=<token>
```

- `PUBLIC_HOST` 는 환경변수 `FGPU_PUBLIC_HOST` 로 결정 (기본 `localhost`).
- `host_port` 는 docker 가 정한 ephemeral.
- `token` 은 백엔드가 만든 secret.

URL 은 백엔드가 record 에 저장 + UI 에 노출. 사용자는 클릭만 하면 노트북 진입.

### 외부 접속 시나리오

랩 GPU 서버에서 fGPU 를 띄우고 학생 노트북에서 접근하려면:
- `FGPU_PUBLIC_HOST=gpu-server.lab.local ./scripts/run_backend.sh`
- 학생이 그 URL 로 접근.
- 방화벽이 ephemeral port 범위를 열어둬야 함.

### 보안

URL = host + port + token. 셋 다 알아야 진입 가능. 토큰만 32자 URL-safe 문자라 brute-force 매우 비현실적.

다만 **누구든 그 URL 을 가지면** 풀 권한이라는 점은 인지. 본 프로토타입의 위협 모델은 *협조적 사용자*. 본격 시나리오라면 reverse proxy + per-user 인증이 필요.

---

## 14.7 LD_PRELOAD 의 살아있음

핵심 디테일: jupyter lab 도 *컨테이너 안 프로세스* 이고, entrypoint 가 `LD_PRELOAD` 환경변수와 함께 `exec` 하므로 jupyter 자체에 hook 이 붙습니다. jupyter 가 fork 하는 ipython kernel 은 이 환경을 상속 → 노트북 셀이 `torch.empty(...)` 를 부르면 우리 hook 이 가로챔.

확인:
- 노트북에서 `!env | grep LD_PRELOAD` → `/opt/fgpu/libfgpu.so`
- 텐서 alloc 후 `docker logs <container>` 보면 `[fgpu] ALLOW cudaMalloc ...` 등장.

이게 Stage 10 의 가치 명제: **"인터랙티브 세션 안에서도 quota 가 강제된다"**.

---

## 14.8 검증 — `scripts/eval/run_jupyter.sh`

핵심 PASS 조건 ([CLAUDE.md](../../CLAUDE.md) Stage 10):

1. `Jupyter /api/status` 가 ~15초 안에 200 응답 — 서버가 떴고 토큰이 맞다.
2. 호스트에서 만든 `host_touched.txt` 가 컨테이너 안 `/workspace` 에 보임 — bind mount 동작.
3. Jupyter 의 `/api/contents/host_touched.txt` 가 200 — Jupyter 가 그 파일을 *자기 root_dir* 로 인식.

이 셋이 다 되면 "노트북에서 GPU + 호스트 영속 워크스페이스" 시나리오가 end-to-end.

```bash
./scripts/build_pytorch_image.sh    # jupyter lab 깔린 이미지
./scripts/run_backend.sh
./scripts/eval/run_jupyter.sh
cat experiments/jupyter_*/summary.txt
# VERDICT: PASS
```

---

## 14.9 한계

이 stage 가 *증명하지 않는* 것 ([CLAUDE.md](../../CLAUDE.md)):

- 다중 사용자 동시 접속의 안정성 — 노트북 하나, 사용자 한 명만 검증.
- idle 세션 자동 정리 — 노트북 안 닫고 떠나면 quota 영구 점유.
- backend 를 통한 reverse proxy — 포트가 외부에 직접 노출, jupyter 토큰만 게이트.
- per-user RBAC — URL+token 가지면 풀 권한.

이 한계들이 *개선 여지* 자 *논문 future work* 거리.

---

## 자가점검 질문

1. jupyter token 이 *어떻게 생성* 되며 *어디 저장* 되는가?
2. 컨테이너 삭제 시 노트북 파일이 살아남는 이유를 한 줄로 설명하라.
3. ephemeral port 를 사용자가 직접 고르게 하면 어떤 운영상 문제가 생기나?
4. 노트북 셀의 `torch.empty(...)` 가 우리 hook 으로 잡히려면 *어떤* 환경변수가 컨테이너 PID 1 에 살아 있어야 하는가?
5. `purge_workspace=true` 옵션의 default 가 false 인 이유는?

→ [Chapter 15: 한계와 위협 모델](15-limitations.md)

---

## 외부 자료 종합

- 📚 [JupyterLab 공식 문서](https://jupyterlab.readthedocs.io/)
- 📚 [Jupyter Server — security](https://jupyter-server.readthedocs.io/en/latest/operators/security.html)
- 📚 [Docker Networking — Port mapping](https://docs.docker.com/engine/network/)
- 📚 [Python secrets module](https://docs.python.org/3/library/secrets.html) — 안전한 토큰 생성
