# Chapter 06 — FastAPI 백엔드 구조

## 학습 목표

- FastAPI 의 app factory / router / dependency 패턴을 안다.
- Pydantic 모델이 *왜* 자동 검증과 OpenAPI 스키마를 동시에 해주는지 이해한다.
- 우리 백엔드의 4 개 레이어 (router / service / store / schema) 분리를 그림으로 설명할 수 있다.
- `uvicorn --reload` 가 개발 시점 무엇을 해주는지 안다.

---

## 6.1 한 그림 요약

```
┌─────────────────────────────────────────────────────┐
│ HTTP 요청 (curl, 브라우저, Web UI fetch)            │
└──────────────────────┬──────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────┐
│ uvicorn (ASGI 서버)                                 │
│   └─ FastAPI app                                    │
│       ├─ routers (api/sessions.py)                  │ ← HTTP 변환 + 검증
│       │     ├─ Pydantic 으로 body 검증              │
│       │     └─ Depends 로 인증 등 횡단 관심사        │
│       │                                             │
│       ▼                                             │
│       services (services/*.py)                      │ ← 비즈니스 로직
│       │   ├─ SessionManager (asyncio 조율)          │
│       │   ├─ DockerManager  (docker SDK 래퍼)       │
│       │   └─ admission     (순수 함수)              │
│       │                                             │
│       ▼                                             │
│       stores (services/session_store.py)            │ ← 영속성
│           SessionStore (sqlite3 stdlib)             │
│                                                     │
└─────────────────────────────────────────────────────┘
```

이 분리가 왜 좋은지:
- **router 변경** (예: REST → GraphQL) 시 service 는 그대로.
- **store 교체** (예: SQLite → Postgres) 시 service interface 만 만족하면 됨.
- 순수 함수(`admission.py`) 는 **docker / GPU 없이** 단위 테스트 가능 ([Chapter 13](13-admission-control.md)).

---

## 6.2 App Factory 패턴

[backend/app/main.py](../../backend/app/main.py) 가 비슷한 모양일 거예요:

```python
def create_app() -> FastAPI:
    settings = Settings()                      # env 로부터 설정 로드
    docker_mgr = DockerManager(...)            # 의존성 조립
    store = SessionStore(settings.db_path)
    session_mgr = SessionManager(docker_mgr, ..., store, ...)

    app = FastAPI(title="fGPU backend")
    app.state.session_manager = session_mgr    # state 로 공유
    app.state.api_token = settings.api_token
    app.include_router(sessions.router)
    return app

app = create_app()
```

### 왜 함수로 감싸나?

- **테스트 격리**: 테스트마다 새 app 인스턴스를 만들 수 있어 상태 leak 방지.
- **의존성 주입**: `Settings` 를 인자로 받으면 테스트에서 mock 설정으로 교체 가능.
- **모듈 import 시 사이드이펙트 X**: `import app.main` 하는 것만으로 docker daemon 에 connect 하지 않음.

### `app.state` 의 의미

FastAPI 의 `app.state` 는 그냥 객체. 라우터 함수 안에서 `request.app.state.session_manager` 로 접근 가능. **요청별이 아니라 *앱 전역* 자원** 을 담는 자리.

---

## 6.3 라우터 — `api/sessions.py`

```python
router = APIRouter(prefix="/sessions", tags=["sessions"],
                   dependencies=[Depends(_require_auth)])

@router.post("", response_model=Session, status_code=201)
async def create_session(body: SessionCreate, request: Request):
    mgr: SessionManager = request.app.state.session_manager
    try:
        return await mgr.create(
            ratio=body.ratio, image=body.image, command=body.command,
            gpu_index=body.gpu_index, force=body.force, mode=body.mode,
        )
    except AdmissionDenied as e:
        raise HTTPException(status_code=409, detail={...})
```

읽을 포인트:

- **`response_model=Session`**: 반환값이 자동으로 Pydantic 으로 직렬화 + OpenAPI 문서에 스키마 등록.
- **`status_code=201`**: 생성 성공의 의미적 코드.
- **`body: SessionCreate`**: 함수 인자에 Pydantic 모델 타입 → FastAPI 가 *자동으로* request body JSON 을 파싱하고 검증. 검증 실패 시 422 자동 반환.
- **`async def`**: 핸들러가 코루틴 — 안에서 `await mgr.create(...)` 가능.
- **`HTTPException`**: 비즈니스 에러를 HTTP 코드로 매핑.

### 더 공부하려면
- [FastAPI 공식 튜토리얼 — Bigger Applications (multiple files)](https://fastapi.tiangolo.com/tutorial/bigger-applications/)
- [FastAPI — Path Operation](https://fastapi.tiangolo.com/tutorial/path-operation-configuration/)

---

## 6.4 Pydantic — 검증 + 직렬화 동시에

[backend/app/schemas/session.py](../../backend/app/schemas/session.py):

```python
class SessionCreate(BaseModel):
    ratio: float = Field(0.4, ge=0.0, le=1.0)
    image: Optional[str] = None
    command: Optional[List[str]] = None
    gpu_index: Optional[int] = Field(None, ge=0)
    mode: SessionMode = "batch"
    force: bool = False
```

이 한 클래스로:
- HTTP 요청 body 의 타입/범위 자동 검증 (`ratio` 가 1.5 면 자동 422 + 친절한 에러 메시지).
- OpenAPI 스키마 자동 생성 → `/docs` 에서 try-it-out 가능.
- 코드 안에서 `body.ratio` 로 타입 안전한 접근 (IDE 자동완성).
- 응답 객체로 쓰면 자동 JSON 직렬화 (datetime → ISO8601 등).

### 더 공부하려면
- [Pydantic 공식 문서](https://docs.pydantic.dev/)
- [FastAPI — Request Body](https://fastapi.tiangolo.com/tutorial/body/)

---

## 6.5 Dependency Injection — `Depends`

```python
def _require_auth(request: Request, authorization: str | None = Header(None)):
    expected = request.app.state.api_token
    if not expected:
        return  # auth disabled
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing bearer token",
                            headers={"WWW-Authenticate": "Bearer"})
    token = authorization[len("Bearer "):]
    if not hmac.compare_digest(token, expected):
        raise HTTPException(401, "invalid bearer token")
```

라우터에 `dependencies=[Depends(_require_auth)]` 로 등록하면 **모든 엔드포인트 진입 전에** 이 함수가 실행. 401 raise 면 핸들러 자체가 안 불림.

장점:
- 횡단 관심사 (인증, 로깅, rate limit) 를 핸들러 코드에서 분리.
- 새 엔드포인트 추가 시 자동 보호.
- 테스트 시 `app.dependency_overrides[_require_auth] = lambda: None` 로 우회 가능.

자세한 건 [Chapter 09](09-auth.md).

### 더 공부하려면
- [FastAPI — Dependencies](https://fastapi.tiangolo.com/tutorial/dependencies/)
- [FastAPI — Security](https://fastapi.tiangolo.com/tutorial/security/)

---

## 6.6 `uvicorn` — ASGI 서버

FastAPI 자체는 *프레임워크* 일 뿐이고, HTTP 를 받아주는 *서버* 는 별도. 표준 선택이 [uvicorn](https://www.uvicorn.org/).

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

옵션 의미:
- `app.main:app` — `app/main.py` 의 `app` 변수를 ASGI app 으로 인식.
- `--host 0.0.0.0` — 모든 인터페이스에서 listen (외부 접속 허용). 개발 시 위험할 수 있어 firewall 필요.
- `--reload` — 코드 변경 시 자동 재시작. 개발용. 프로덕션에선 끄세요.

[scripts/run_backend.sh](../../scripts/run_backend.sh) 가 venv + `pip install -e .` + uvicorn 한 줄로 자동화.

### ASGI 가 뭔가? — 한 줄

WSGI 의 후속. async/await 지원 + WebSocket 지원하는 표준 인터페이스. FastAPI 는 ASGI 위에서 돌아갑니다.

### 더 공부하려면
- [ASGI 공식 사이트](https://asgi.readthedocs.io/)
- [Uvicorn 공식 문서](https://www.uvicorn.org/)

---

## 6.7 Settings — env 기반 설정

[backend/app/core/config.py](../../backend/app/core/config.py) 의 `Settings` 는 `pydantic-settings` 의 `BaseSettings` 를 상속:

```python
class Settings(BaseSettings):
    host_hook_path: str = ".../build/libfgpu.so"  # 자동 탐지
    container_hook_path: str = "/opt/fgpu/libfgpu.so"
    runtime_image: str = "fgpu-runtime:stage2"
    api_token: str = ""
    db_path: str = ".../data/sessions.db"
    workspace_root: str = ".../data/sessions"

    model_config = SettingsConfigDict(env_prefix="FGPU_", env_file=".env")
```

이 한 클래스가:
- 환경변수 `FGPU_API_TOKEN` 을 읽어 `api_token` 채움.
- `.env` 파일이 있으면 거기서도 읽음.
- 타입 자동 변환 (`FGPU_PORT=8000` 문자열 → int).
- 기본값 fallback.

### 더 공부하려면
- [pydantic-settings 공식 문서](https://docs.pydantic.dev/latest/concepts/pydantic_settings/)

---

## 6.8 직접 해보기

```bash
./scripts/run_backend.sh
# 별도 터미널에서:
curl http://localhost:8000/healthz
# {"ok": true, "auth_enabled": false}

# OpenAPI 문서 (브라우저에서 열어보세요):
# http://localhost:8000/docs

# 세션 생성:
curl -X POST http://localhost:8000/sessions \
    -H 'Content-Type: application/json' \
    -d '{"ratio": 0.4}'
```

OpenAPI 페이지(`/docs`) 에서 *Try it out* 으로 같은 요청을 GUI 로도 보내볼 수 있어요. Pydantic 의 가치를 가장 빨리 체감하는 길.

---

## 자가점검 질문

1. router / service / store 를 분리할 때 *각각의 변경 이유* 는 무엇인가? (단일 책임 원칙 관점)
2. `Depends(_require_auth)` 가 라우터 단위가 아니라 *엔드포인트별로* 적용된다면 어떤 단점이 있나?
3. Pydantic 모델로 받은 body 의 `ratio` 가 1.5 일 때 사용자에게 가는 응답 코드와 본문은?
4. `uvicorn --reload` 를 프로덕션에서 *쓰지 말아야 할* 이유는?
5. `app.state.session_manager` 를 *요청 스코프* 로 만들면 어떤 부작용이 생기나?

→ [Chapter 07: asyncio.to_thread](07-async-io.md)

---

## 외부 자료 종합

- 📚 [FastAPI 공식 튜토리얼](https://fastapi.tiangolo.com/tutorial/) — 공식, 친절. 한 번은 끝까지.
- 📚 [Pydantic 공식](https://docs.pydantic.dev/)
- 📖 *Architecture Patterns with Python* — Cosmic Python. service / repository 분리 패턴의 정석. [무료 온라인](https://www.cosmicpython.com/book/preface.html)
- 🛠 [docker-py 공식 문서](https://docker-py.readthedocs.io/)
