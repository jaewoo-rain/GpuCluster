# 3장. Stage 3·8 — 백엔드를 밑바닥부터 (FastAPI + Docker + SQLite)

## 이 장에서 만들 것

- 빈 `backend/` 디렉토리에서 시작해, `curl` 로 컨테이너를 띄우고 죽일 수 있는 세션 API 서버를 손으로 쌓아 올립니다.
- 핵심은 "한 번에 다 짜지 않는" 리듬입니다. `/healthz` 한 줄부터 시작해 → 스키마 → docker 스텁 → 실제 spawn → in-memory 저장 → SQLite 영속 → async 리팩터 순서로 조금씩 키웁니다.
- 각 단계마다 "여기서 이걸 실행해 확인" 게이트를 두어, 다음으로 넘어가기 전에 지금까지 만든 것이 진짜 동작하는지 눈으로 봅니다.
- 완성본은 이미 저장소에 있습니다. 이 장은 그 완성본에 "도달하는 순서"를 재현합니다. 완성 코드는 [backend/app/](../../backend/app/) 링크로 참조합니다.

> 개념(FastAPI vs Spring, async, Docker SDK, SQLite 가 무엇인지)은 온보딩 교재에서 이미 다뤘습니다. 여기서는 "무엇을 먼저, 왜 그 순서로 만드는가"에만 집중합니다.

---

## 3.1 목표: 무엇이 되면 성공인가

Stage 3 이 끝나면 `scripts/smoke_test_api.sh` 한 방으로 아래가 돌아야 합니다.

1. `POST /sessions {"ratio":0.4}` → 컨테이너가 뜨고 `container_id` 가 채워진 `Session` JSON 을 201 로 돌려받음.
2. 몇 초 뒤 `GET /sessions/{id}` → `status: exited`, `exit_code: 0`.
3. `GET /sessions/{id}/logs` → `[entrypoint]` + `[fgpu] ALLOW/DENY/FREE` 로그가 보임.
4. `DELETE /sessions/{id}` → `{"deleted": "<id>"}`, 이후 `GET` 은 404.

Stage 8 이 끝나면 여기에 두 가지가 더해집니다.

5. 백엔드를 껐다 켜도 세션 record 가 살아있고(SQLite), status 는 docker 데몬과 다시 맞춰짐(reconcile).
6. 동시에 들어온 두 `POST /sessions` 가 서로를 막지 않고 병렬로 처리됨(`asyncio.to_thread`).

---

## 3.2 개발 순서 체크리스트

이 장 전체의 로드맵입니다. 위에서 아래로 하나씩 게이트를 통과하며 내려갑니다.

1. `pyproject.toml` — 의존성 + pytest 설정을 먼저 박아 `pip install -e .` 가 되게.
2. `app/main.py` 최소 버전 — `create_app()` 팩토리 + `/healthz` 한 줄. **뜨는지 확인.**
3. `schemas/session.py` — 요청/응답 모델(`SessionCreate`, `Session`)을 먼저 설계.
4. `core/config.py` — `FGPU_*` 환경변수, hook 경로 자동 탐지.
5. `services/docker_manager.py` — 처음엔 목록만 반환하는 스텁 → 그다음 실제 `create_container`.
6. `services/session_manager.py` — 처음엔 in-memory dict. **`POST/GET/DELETE` 확인.**
7. `api/sessions.py` — 라우터 배선. `smoke_test_api.sh` 통과.
8. **Stage 8 진화**: `session_store.py`(SQLite)로 저장소 교체. **재시작 후에도 살아있는지 확인.**
9. 모든 blocking 호출을 `asyncio.to_thread` 로 감싸는 리팩터.
10. status reconcile(docker 데몬 동기화) 추가.
11. `tests/test_session_store.py` — GPU/docker 없이 단위테스트 하는 개발 방식 확립.

---

## 3.3 스텝 1 — pyproject.toml 부터

빈 `backend/` 에서 가장 먼저 하는 일은 코드가 아니라 "설치 가능한 패키지 껍데기"를 만드는 것입니다. 그래야 `pip install -e .` 로 개발 모드 설치가 되고, `import app.xxx` 가 어디서든 동작하며, `pytest` 가 테스트를 찾습니다.

의존성은 **일부러 표준 라이브러리에 가깝게** 최소로 유지합니다. 웹(`fastapi`+`uvicorn`), docker 제어(`docker`), 모델/설정(`pydantic`+`pydantic-settings`). 이게 전부입니다.

```toml
[project]
name = "fgpu-backend"
requires-python = ">=3.11"
dependencies = [
    "fastapi>=0.110",
    "uvicorn[standard]>=0.27",
    "docker>=7.0",
    "pydantic>=2.5",
    "pydantic-settings>=2.1",
]

[project.optional-dependencies]
dev = ["pytest>=7.0"]

[tool.pytest.ini_options]
testpaths = ["tests"]
```

두 가지 결정을 이 시점에 못박습니다.

- `dev` extra 로 `pytest` 를 분리 → 운영 설치엔 테스트 의존성이 안 딸려옵니다.
- `testpaths = ["tests"]` → `backend/` 에서 그냥 `pytest` 만 쳐도 `tests/` 를 찾습니다. (11 스텝에서 쓸 게이트를 미리 깔아두는 것)

완성본: [backend/pyproject.toml](../../backend/pyproject.toml).

> **함정.** `sqlite3` 와 `docker` 를 헷갈리지 마세요. SQLite 는 파이썬 **표준 라이브러리**(`import sqlite3`)라 의존성 목록에 없습니다. `docker` 는 docker 데몬과 통신하는 SDK 라 별도 패키지입니다.

---

## 3.4 스텝 2 — 최소 FastAPI 앱 + /healthz

여기서 원칙 하나를 세웁니다. **앱은 팩토리 함수(`create_app()`)로 만든다.** 전역에 `app = FastAPI()` 를 바로 두는 대신 함수로 감싸는 이유는, 나중에 설정을 주입하고 서비스 객체들을 `app.state` 에 매달아야 하기 때문입니다. 지금 당장은 오버킬처럼 보여도 3 스텝 뒤엔 반드시 필요해집니다.

처음 버전은 정말 이것만 있으면 됩니다.

```python
# app/main.py (최소 버전)
from fastapi import FastAPI

def create_app() -> FastAPI:
    app = FastAPI(title="fGPU Backend")

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    return app

app = create_app()
```

### 여기서 확인 (게이트 1)

```bash
./scripts/run_backend.sh        # venv 만들고 pip install -e . 후 uvicorn :8000
# 다른 터미널에서:
curl -sS http://localhost:8000/healthz
# → {"ok":true}
```

`run_backend.sh` 는 venv 생성 → `pip install -q -e .` → `uvicorn app.main:app --reload` 까지 알아서 합니다([scripts/run_backend.sh](../../scripts/run_backend.sh#L22-L46)). `--reload` 덕에 앞으로 파일을 저장할 때마다 자동 재기동되니, 이 터미널은 켜둔 채로 개발합니다.

`{"ok":true}` 를 봤다면 "패키징 + 서버 부팅"이라는 가장 지루하지만 가장 중요한 토대가 검증된 겁니다. 이제 살을 붙입니다.

완성된 `/healthz` 는 설정값들을 그대로 노출해서 디버깅에 쓰입니다([main.py:75-85](../../backend/app/main.py#L75-L85)) — hook `.so` 존재 여부, DB 경로, 인증 활성 여부까지. 하지만 그건 나중 이야기고, 지금은 `{"ok":true}` 로 충분합니다.

---

## 3.5 스텝 3 — schemas/session.py: 요청/응답 모델 먼저

코드 로직보다 **데이터 모양을 먼저** 정합니다. 왜 이 순서일까요? API 의 입력(`POST` body)과 출력(`Session` JSON)이 확정돼야, docker_manager 와 session_manager 가 "무엇을 받아 무엇을 돌려줄지"가 명확해지기 때문입니다. 모델이 계약서 역할을 합니다.

MVP 단계에선 필드를 최소로 시작하세요.

```python
# schemas/session.py (Stage 3 최소 버전)
from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field

class SessionCreate(BaseModel):
    ratio: float = Field(..., gt=0.0, le=1.0)   # 0 < ratio <= 1
    command: Optional[list[str]] = None
    image: Optional[str] = None

class Session(BaseModel):
    id: str
    container_id: str
    container_name: str
    status: str = "created"
    ratio: float
    image: str
    command: list[str]
    created_at: datetime
    exit_code: Optional[int] = None
```

설계 메모 두 가지:

- `SessionCreate` 와 `Session` 을 나눕니다. 전자는 "사용자가 주는 것", 후자는 "우리가 관리하는 record + 응답". `ratio` 의 `gt=0.0, le=1.0` 제약을 여기 박아두면 잘못된 값은 라우터에 닿기도 전에 pydantic 이 422 로 걷어냅니다.
- `Session` 하나를 **내부 record 겸 API 응답**으로 씁니다. Stage 3 MVP 에선 변환 로직이 필요 없을 만큼 단순하기 때문입니다. 완성본 상단 주석도 이 결정을 명시합니다([session.py:4-6](../../backend/app/schemas/session.py#L4-L6)).

이후 스테이지에서 이 모델은 계속 자랍니다. `gpu_index`(Stage 9), `mode`/`host_port`/`jupyter_token`(Stage 10), `compute_ratio`/`force`(Stage 11·12) 가 **추가만** 됩니다(기존 필드는 안 건드림). 이 "추가만" 규칙 덕에 하위 호환이 유지됩니다. 완성본: [session.py:25-82](../../backend/app/schemas/session.py#L25-L82).

> **함정.** 응답 모델을 나중으로 미루고 dict 를 대충 반환하기 시작하면, 필드가 늘 때마다 반환 지점 전부를 손봐야 합니다. `response_model=Session` 을 라우터에 붙이면(7 스텝) FastAPI 가 자동 검증·직렬화·문서화까지 해주니, 모델을 먼저 정하는 투자가 금방 회수됩니다.

---

## 3.6 스텝 4 — core/config.py: 환경변수 + 경로 자동 탐지

하드코딩된 경로/이미지 이름은 지금 당장 편하지만 곧 발목을 잡습니다. 설정을 한 곳(`Settings`)에 모으고 `FGPU_*` 환경변수로 override 가능하게 합니다.

```python
# core/config.py
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    runtime_image: str = "fgpu-runtime:stage2"
    container_hook_path: str = "/opt/fgpu/libfgpu.so"
    host_hook_path: str = ""   # 빈 값이면 아래에서 auto-detect
    db_path: str = ""          # 빈 값이면 <repo>/data/sessions.db

    model_config = SettingsConfigDict(env_prefix="FGPU_", extra="ignore")

@lru_cache
def get_settings() -> Settings:
    s = Settings()
    if not s.host_hook_path:
        s.host_hook_path = ...  # <repo>/build/libfgpu.so 계산
    return s
```

두 가지 패턴을 기억하세요.

- **"빈 문자열이면 자동 계산"** 패턴. `host_hook_path` 기본값을 빈 문자열로 두고, `get_settings()` 에서 이 파일 위치 기준으로 repo root 를 거슬러 올라가 `<repo>/build/libfgpu.so` 를 채웁니다([config.py:32-61](../../backend/app/core/config.py#L32-L61)). 환경변수로 주면 그걸, 안 주면 자동 탐지.
- `@lru_cache` — 설정을 한 번만 읽어 재사용. 부팅 때 여러 곳에서 `get_settings()` 를 불러도 파일 시스템을 반복 조회하지 않습니다.

완성본에는 `default_command`, `api_token`, `workspace_root` 등이 더 있지만([config.py:40-49](../../backend/app/core/config.py#L40-L49)), Stage 3 에선 위 네 개면 충분합니다. 나머지는 필요할 때 추가합니다.

---

## 3.7 스텝 5 — docker_manager.py: 스텁부터 시작

이 지점이 "한 번에 다 짜지 말라"의 핵심입니다. docker 로 컨테이너를 띄우는 건 실패 가능성이 큰 작업(이미지 없음, GPU 없음, 데몬 다운)입니다. 그래서 **먼저 아무것도 안 띄우는 스텁**으로 배선만 확인합니다.

```python
# docker_manager.py (0단계 스텁)
import docker

class DockerManager:
    def __init__(self, host_hook_path, container_hook_path, runtime_image):
        self.client = docker.from_env()
        self.host_hook_path = host_hook_path
        self.container_hook_path = container_hook_path
        self.runtime_image = runtime_image

    def list_containers(self):
        return self.client.containers.list(all=True)  # 데몬 연결만 확인
```

이 스텁으로 확인하는 것: `docker.from_env()` 가 예외 없이 붙는가(= 현재 사용자가 docker 그룹인가), 데몬이 살아있는가. 여기서 막히면 이후 spawn 은 시도할 필요도 없습니다.

배선이 확인되면 **진짜 `create_container`** 를 조립합니다. 이 메서드가 fGPU 의 심장입니다 — 컨테이너 하나에 (1)`--gpus`, (2)hook `.so` bind-mount, (3)`LD_PRELOAD`+`FGPU_RATIO` env 를 한 곳에서 조립합니다.

```python
def create_container(self, name, ratio, command, image=None, gpu_index=None):
    env = {
        "FGPU_RATIO": str(ratio),
        "LD_PRELOAD": self.container_hook_path,   # 훅 주입의 핵심
    }
    # --gpus all  또는  --gpus device=N
    if gpu_index is None:
        device_requests = [DeviceRequest(count=-1, capabilities=[["gpu"]])]
    else:
        device_requests = [DeviceRequest(device_ids=[str(gpu_index)],
                                         capabilities=[["gpu"]])]
    # 호스트의 libfgpu.so 를 컨테이너 안 경로로 read-only 마운트
    volumes = {self.host_hook_path: {"bind": self.container_hook_path, "mode": "ro"}}

    return self.client.containers.run(
        image=image or self.runtime_image,
        command=command, name=name,
        detach=True, remove=False,           # 종료 후에도 logs 조회 가능하게 보존
        device_requests=device_requests,
        volumes=volumes, environment=env,
    )
```

핵심 결정 세 가지:

- `remove=False` — 컨테이너가 끝나도 지우지 않습니다. 안 그러면 `GET .../logs` 로 `[fgpu]` 로그를 못 봅니다. 정리는 `DELETE` 가 명시적으로 합니다.
- `.so` 를 이미지에 굽지 않고 **런타임에 `-v` 로 마운트**. hook 을 다시 빌드해도 이미지 재빌드가 필요 없습니다.
- `gpu_index=None` → 전 GPU(`count=-1`), 정수면 특정 device. Stage 9 의 멀티-GPU 를 이 시그니처가 미리 수용합니다.

완성본은 여기에 `quota_bytes`, `compute_ratio`(Stage 12), jupyter 포트/워크스페이스(Stage 10), 그리고 env 화이트리스트 passthrough(`_PASSTHROUGH_ENV`)가 붙습니다([docker_manager.py:67-138](../../backend/app/services/docker_manager.py#L67-L138)). 조회/정리용 `get_status`/`get_logs`/`stop_container`/`remove_container` 도 함께 있습니다([docker_manager.py:141-176](../../backend/app/services/docker_manager.py#L141-L176)).

> **레이어 규칙.** `docker_manager` 는 docker SDK 를 아는 **유일한** 파일입니다. 위층(`session_manager`)은 절대 SDK 를 직접 부르지 않고 이 래퍼를 거칩니다. 이렇게 해야 나중에 docker → containerd 같은 교체가 이 한 파일에 갇힙니다.

> **함정.** `environment` 에 임의 env 를 통째로 넘기지 마세요. 완성본은 `_PASSTHROUGH_ENV` 화이트리스트로 딱 정해진 키만 전달합니다([docker_manager.py:32](../../backend/app/services/docker_manager.py#L32), [94-97](../../backend/app/services/docker_manager.py#L94-L97)). 백엔드 프로세스의 비밀 env 가 사용자 컨테이너로 새는 걸 막습니다.

---

## 3.8 스텝 6 — session_manager.py: 먼저 in-memory dict 로

저장소를 SQLite 로 바로 가지 마세요. 라이프사이클 로직(생성→조회→삭제)이 맞는지 먼저 확인하려면 **가장 단순한 저장소 = 파이썬 dict** 로 시작합니다. 나중에 dict 를 SQLite 로 갈아끼우는 게 Stage 8 입니다.

```python
# session_manager.py (Stage 3 in-memory 버전)
import uuid
from datetime import datetime, timezone
from app.schemas.session import Session

class SessionManager:
    def __init__(self, docker_manager, runtime_image, default_command):
        self.docker = docker_manager
        self.runtime_image = runtime_image
        self.default_command = default_command
        self._sessions: dict[str, Session] = {}   # ← 임시 저장소

    def create(self, ratio, command=None, image=None) -> Session:
        sid = uuid.uuid4().hex[:12]
        name = f"fgpu-{sid}"
        cmd = command or list(self.default_command)
        c = self.docker.create_container(
            name=name, ratio=ratio, command=cmd, image=image or self.runtime_image
        )
        rec = Session(
            id=sid, container_id=c.id, container_name=name,
            ratio=ratio, image=image or self.runtime_image, command=cmd,
            created_at=datetime.now(timezone.utc), status=c.status or "created",
        )
        self._sessions[sid] = rec
        return rec

    def get(self, sid):   return self._sessions.get(sid)
    def list_all(self):   return list(self._sessions.values())
    def delete(self, sid):
        rec = self._sessions.pop(sid, None)
        if rec is None: return False
        self.docker.remove_container(rec.container_id)
        return True
```

이 버전은 아직 **동기(sync)** 이고 상태 reconcile 도 없습니다. 그래도 됩니다 — 목표는 "생성/조회/삭제가 돈다"를 확인하는 것뿐입니다.

`main.py` 에서 서비스들을 조립해 `app.state` 에 매답니다. 여기서 스텝 2 에서 `create_app()` 팩토리로 만든 결정이 빛을 봅니다.

```python
def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="fGPU Backend")
    docker_mgr = DockerManager(settings.host_hook_path,
                               settings.container_hook_path, settings.runtime_image)
    app.state.session_manager = SessionManager(docker_mgr, settings.runtime_image,
                                               settings.default_command)
    # ... /healthz, 라우터 include
    return app
```

완성본의 조립은 [main.py:33-92](../../backend/app/main.py#L33-L92) 입니다(SessionStore·workspace·api_token 이 추가됨).

---

## 3.9 스텝 7 — api/sessions.py: 라우터 배선

이제 HTTP 표면을 붙입니다. **레이어 규칙: `api/` 는 비즈니스 로직을 담지 않는다.** 라우터는 요청을 파싱하고 매니저를 호출하고 예외를 HTTP 상태로 번역할 뿐입니다.

```python
# api/sessions.py (Stage 3 버전)
from fastapi import APIRouter, Depends, HTTPException, Request
from app.schemas.session import Session, SessionCreate, SessionLogs

router = APIRouter(prefix="/sessions", tags=["sessions"])

def _get_manager(request: Request):
    return request.app.state.session_manager

@router.post("", response_model=Session, status_code=201)
def create_session(body: SessionCreate, mgr=Depends(_get_manager)):
    try:
        return mgr.create(ratio=body.ratio, command=body.command, image=body.image)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"create failed: {e}")

@router.get("", response_model=list[Session])
def list_sessions(mgr=Depends(_get_manager)):
    return mgr.list_all()

@router.get("/{sid}", response_model=Session)
def get_session(sid: str, mgr=Depends(_get_manager)):
    rec = mgr.get(sid)
    if rec is None:
        raise HTTPException(status_code=404, detail="session not found")
    return rec

@router.delete("/{sid}")
def delete_session(sid: str, mgr=Depends(_get_manager)):
    if not mgr.delete(sid):
        raise HTTPException(status_code=404, detail="session not found")
    return {"deleted": sid}
```

`_get_manager` 를 `Depends` 로 주입하는 패턴에 주목하세요. 라우터가 전역 상태를 직접 뒤지지 않고 의존성으로 받으면, 테스트에서 매니저를 갈아끼우기 쉽습니다. 완성본은 `/logs`, `/stop` 도 추가하고([sessions.py:116-133](../../backend/app/api/sessions.py#L116-L133)), 라우터 레벨에 인증 의존성(Stage 9)과 409 admission 처리(Stage 11)를 얹습니다([sessions.py:51-94](../../backend/app/api/sessions.py#L51-L94)) — 6장에서 다룹니다.

### 여기서 확인 (게이트 2 — Stage 3 합격선)

```bash
./scripts/run_backend.sh          # 한 터미널
./scripts/smoke_test_api.sh       # 다른 터미널
```

[smoke_test_api.sh](../../scripts/smoke_test_api.sh) 는 healthz → POST → sleep 4 → GET → logs → DELETE 를 한 바퀴 돕니다. 통과 기준:

- POST 응답의 `container_id` 가 비어있지 않음.
- `sleep 4` 후 GET 의 `status: exited`, `exit_code: 0`.
- logs 에 `[entrypoint]` + `[fgpu] ALLOW/DENY/FREE`.
- DELETE 후 `{"deleted": "<id>"}`, 재조회 시 404.

여기까지가 **Stage 3 완성**입니다. 컨테이너를 API 로 띄우고 죽일 수 있게 됐습니다.

---

## 3.10 스텝 8 — Stage 8 진화: 저장소를 SQLite 로 교체

in-memory dict 의 치명적 한계: 백엔드를 재시작하면 세션 record 가 통째로 사라집니다. 컨테이너는 docker 데몬이 여전히 들고 있는데 백엔드만 그 존재를 잊는 겁니다. Stage 8 은 dict 를 **SQLite** 로 갈아끼워 이를 해결합니다.

핵심은 `SessionStore` 인터페이스가 dict 와 **같은 모양의 CRUD** 를 제공하는 것입니다(`insert`/`get`/`list_all`/`update_status`/`delete`). 그래야 `SessionManager` 는 저장소가 dict 든 SQLite 든 신경 안 씁니다. 이게 미래의 Redis/Postgres 교체를 위한 추상화 경계입니다.

```python
# session_store.py (핵심만)
import sqlite3, json
from contextlib import closing

class SessionStore:
    def __init__(self, db_path):
        self.db_path = str(db_path)
        with closing(self._conn()) as c:
            c.execute(SCHEMA_SQL)                    # CREATE TABLE IF NOT EXISTS
            existing = {row[1] for row in c.execute("PRAGMA table_info(sessions)")}
            for col, ddl in _MIGRATIONS:             # 멱등 ADD COLUMN
                if col not in existing:
                    c.execute(ddl)

    def _conn(self):
        # 매 호출 새 커넥션 + autocommit
        return sqlite3.connect(self.db_path, isolation_level=None, timeout=5.0)

    def insert(self, s):
        with closing(self._conn()) as c:
            c.execute("INSERT INTO sessions (...) VALUES (...)", (...))
```

이 파일의 설계 결정 세 가지가 이 장 시스템 프롬프트의 concurrency 규칙과 직결됩니다.

- **매 호출마다 새 커넥션.** `sqlite3.Connection` 은 스레드 안전이 보장되지 않습니다. 9 스텝에서 각 호출을 `asyncio.to_thread` 로 감싸면 임의의 워커 스레드에서 돌 수 있으므로, 커넥션을 공유하면 위험합니다. 그래서 `_conn()` 이 매번 새로 열고 `contextlib.closing` 으로 반드시 닫습니다([session_store.py:84-87](../../backend/app/services/session_store.py#L84-L87)).
- **`isolation_level=None`(autocommit) + 단일 statement 만.** 트랜잭션을 수동 관리하지 않아도 안전하도록, 각 메서드는 statement 하나만 실행합니다([session_store.py:112-165](../../backend/app/services/session_store.py#L112-L165)).
- **멱등 마이그레이션.** 마이그레이션 프레임워크를 쓰지 않습니다. `PRAGMA table_info` 로 컬럼 존재를 검사해, 없을 때만 `ALTER TABLE ... ADD COLUMN` 합니다([session_store.py:55-63](../../backend/app/services/session_store.py#L55-L63), [77-82](../../backend/app/services/session_store.py#L77-L82)). 추가만 하고 삭제/타입변경은 안 합니다. 스키마를 깨는 변경이 필요하면 `rm -rf data/` 로 초기화합니다.

`datetime` 은 `.isoformat()` 문자열로 저장하고 `datetime.fromisoformat()` 으로 복원합니다([session_store.py:99](../../backend/app/services/session_store.py#L99), [126](../../backend/app/services/session_store.py#L126)). `command` 리스트는 JSON 문자열로([session_store.py:98](../../backend/app/services/session_store.py#L98), [125](../../backend/app/services/session_store.py#L125)).

그리고 `SessionManager` 에서 `self._sessions` dict 를 `self.store` 로 교체합니다. `main.py` 에서 `SessionStore(settings.db_path)` 를 만들어 주입하면([main.py:54](../../backend/app/main.py#L54), [57-63](../../backend/app/main.py#L57-L63)) 끝입니다.

### 여기서 확인 (게이트 3 — 재시작 내구성)

```bash
./scripts/run_backend.sh          # 첫 부팅 시 data/sessions.db 자동 생성
# 세션 하나 만들고:
curl -sS -X POST http://localhost:8000/sessions -H 'Content-Type: application/json' -d '{"ratio":0.4}'
# 백엔드 Ctrl+C 로 종료 → 다시 run_backend.sh
curl -sS http://localhost:8000/sessions
# → 아까 만든 세션 record 가 그대로 있음 (status 는 reconcile 됨)
```

`sqlite3 data/sessions.db "SELECT id, status FROM sessions"` 로도 직접 확인 가능합니다.

---

## 3.11 스텝 9 — 왜 지금 asyncio.to_thread 리팩터인가

저장소가 SQLite 가 된 지금이 async 리팩터를 넣기에 **딱 맞는 시점**입니다. 이유:

- 이제 blocking 호출이 두 종류가 됐습니다 — docker SDK(항상 sync)와 `sqlite3`(항상 sync). 둘 다 이벤트 루프에서 직접 부르면 그동안 **다른 모든 요청이 멈춥니다.** 한 `POST /sessions` 가 `docker.run()` 하는 몇 초 동안 `/healthz` 조차 응답 못 하게 됩니다.
- 해결: 모든 blocking 호출을 `asyncio.to_thread(...)` 로 워커 스레드에 던지고, 매니저 메서드를 `async def` 로 바꿉니다. 이벤트 루프는 즉시 다른 요청을 받을 수 있습니다.

```python
# session_manager.py — sync → async 변환 패턴
async def get(self, sid):
    rec = await asyncio.to_thread(self.store.get, sid)      # sqlite → 스레드
    ...
    status, exit_code = await asyncio.to_thread(
        self.docker.get_status, rec.container_id            # docker → 스레드
    )
```

`create` 안의 `docker.create_container` 도([session_manager.py:145-157](../../backend/app/services/session_manager.py#L145-L157)), `store.insert` 도([session_manager.py:196](../../backend/app/services/session_manager.py#L196)) 전부 `to_thread` 로 감쌉니다. 라우터 핸들러도 `async def` + `await mgr.xxx()` 로 바꿉니다([sessions.py:62-68](../../backend/app/api/sessions.py#L62-L68)).

`list_all` 은 한 걸음 더 나갑니다 — 각 record 의 reconcile 을 `asyncio.gather` 로 **동시에** 돌려 목록 응답 지연을 줄입니다([session_manager.py:226-232](../../backend/app/services/session_manager.py#L226-L232)).

> **함정.** "async 니까 빠르겠지" 하고 sync 함수 안에서 `to_thread` 를 빼먹으면 아무 소용 없습니다. `async def` 안에서 blocking sync 함수를 그냥 부르면 이벤트 루프가 그대로 막힙니다. 규칙은 단순합니다 — **docker SDK 호출과 sqlite3 호출은 예외 없이 `to_thread` 로 감싼다.**

### 여기서 확인

동시에 두 개의 `POST /sessions` 를 던져(`&` 로 백그라운드) 둘 다 비슷한 시각에 응답이 오는지, 그리고 그 사이 `curl /healthz` 가 즉시 응답하는지 봅니다. sync 버전이었다면 두 번째 POST 와 healthz 가 첫 POST 의 docker.run 뒤로 줄 서게 됩니다.

---

## 3.12 스텝 10 — 상태 reconcile 추가

in-memory 시절엔 `status` 를 create 때 한 번 찍고 끝이었습니다. 하지만 컨테이너는 백엔드 몰래 상태가 바뀝니다(실행→종료). 그래서 **읽을 때마다 docker 데몬에게 실제 상태를 물어** record 와 다르면 갱신(write-back)합니다.

```python
async def get(self, sid):
    rec = await asyncio.to_thread(self.store.get, sid)
    if rec is None:
        return None
    try:
        status, exit_code = await asyncio.to_thread(self.docker.get_status, rec.container_id)
    except docker.errors.NotFound:
        # 컨테이너가 데몬에서 사라짐 → record 보존, status 만 'removed'
        if rec.status != "removed":
            await asyncio.to_thread(self.store.update_status, sid, "removed", rec.exit_code)
        rec.status = "removed"
        return rec
    if rec.status != status or rec.exit_code != exit_code:
        await asyncio.to_thread(self.store.update_status, sid, status, exit_code)
    rec.status, rec.exit_code = status, exit_code
    return rec
```

완성본: [session_manager.py:200-224](../../backend/app/services/session_manager.py#L200-L224).

두 가지 엣지 케이스를 이 자리에서 처리합니다.

- **컨테이너가 데몬에서 사라짐**(`docker rm -f` 를 외부에서 함) → `NotFound` 를 잡아 record 는 남기고 status 만 `removed` 로. record 를 지우지는 않습니다(감사 흔적 보존).
- 재시작 직후 첫 `GET /sessions` 이 곧 이 reconcile 을 태워, SQLite 에 저장돼 있던 `running` 을 실제 `exited` 로 맞춥니다. 이게 게이트 3 에서 "status 는 reconcile 됨"의 정체입니다.

> **함정(문서화된 한계).** docker 데몬 자체가 죽어 있으면 `get_status` 가 `NotFound` 가 아닌 다른 예외를 던져 500 으로 전파됩니다. 이건 의도된 미해결 영역입니다 — 데몬이 없으면 애초에 아무것도 못 하니까요.

---

## 3.13 스텝 11 — GPU/docker 없이 단위테스트 하는 개발 방식

여기서 개발 습관 하나를 확립합니다. **hardware 없이 검증 가능한 부분은 pytest 로 묶는다.** docker+GPU 가 필요한 end-to-end 는 Linux GPU 박스에서 `scripts/eval/*.sh` 로 돌리지만, `SessionStore` 의 SQLite 로직은 순수해서 Windows/CI 어디서든 `pytest` 로 검증됩니다.

`tests/test_session_store.py` 가 커버하는 것([test_session_store.py](../../backend/tests/test_session_store.py)):

- insert→get 라운드트립에서 `datetime`, `list[str]`, `Optional[int]` 가 SQLite 를 거쳐도 보존되는가([test:57-71](../../backend/tests/test_session_store.py#L57-L71)).
- `list_all` 이 최신순 정렬인가([test:78-87](../../backend/tests/test_session_store.py#L78-L87)).
- `update_status` 로 `exit_code=None` 유지 경로(removed)([test:101-110](../../backend/tests/test_session_store.py#L101-L110)).
- **같은 DB 파일을 두 `SessionStore` 인스턴스가 차례로 열어 같은 데이터를 보는가** — 백엔드 재시작 시나리오의 pytest 판([test:123-137](../../backend/tests/test_session_store.py#L123-L137)).

`tmp_path` fixture 로 테스트마다 격리된 임시 DB 를 씁니다([test:52-54](../../backend/tests/test_session_store.py#L52-L54)) — 실제 `data/sessions.db` 를 건드리지 않습니다.

### 여기서 확인 (게이트 4)

```bash
cd backend && pip install -e ".[dev]" && pytest
# → test_session_store.py 전부 통과 (docker/GPU 불필요)
```

> **규칙.** store 스키마나 저장 로직을 바꾸면 이 테스트를 **반드시** 같이 갱신합니다. 이 테스트가 초록불이어야 "SQLite 계약은 안 깨졌다"를 하드웨어 없이 보장할 수 있습니다.

---

## 완성 체크리스트

- [ ] `pyproject.toml` 로 `pip install -e ".[dev]"` 가 되고 `pytest` 가 `tests/` 를 찾음.
- [ ] `create_app()` 팩토리 + `/healthz` 가 `{"ok":true}` 반환.
- [ ] `SessionCreate`/`Session` 모델 정의(요청/응답 계약 먼저).
- [ ] `core/config.py` 가 `FGPU_*` env + hook 경로 자동 탐지.
- [ ] `docker_manager.create_container` 가 `--gpus` + `-v` hook 마운트 + `LD_PRELOAD`/`FGPU_RATIO` 조립.
- [ ] `session_manager` 가 create/get/list/delete 를 관리(처음 dict → 나중 SQLite).
- [ ] `api/sessions.py` 라우터 배선, `smoke_test_api.sh` 통과 (**Stage 3 합격**).
- [ ] `session_store.py`(SQLite): 매 호출 새 커넥션, 멱등 마이그레이션. 재시작 후 세션 생존 (**Stage 8 합격**).
- [ ] 모든 docker/sqlite 호출을 `asyncio.to_thread` 로 감쌈.
- [ ] `get`/`list_all` 에서 docker 데몬과 status reconcile.
- [ ] `pytest` 로 SessionStore 단위테스트 통과(하드웨어 불필요).

## 다음 챕터

**6장. Stage 9·10·11 — 운영 기능 (인증·멀티GPU·Jupyter·어드미션).** 지금 만든 백엔드 위에 Bearer 인증, 멀티-GPU 핀, Jupyter 인터랙티브 세션, 그리고 "순수함수 먼저, 통합 나중" 패턴으로 어드미션 컨트롤을 얹습니다. 특히 왜 `asyncio.Lock` 없이는 동시 POST 두 개가 둘 다 admission 을 통과해 오버서브되는지를 손으로 재현합니다.
