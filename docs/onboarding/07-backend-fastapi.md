# 7장. 백엔드 아키텍처 — Spring 개발자를 위한 FastAPI 안내서

> 📘 **이 장을 읽고 나면**
>
> - FastAPI 의 라우터 / 의존성 주입 / DTO / 설정이 Spring 의 무엇과 1:1로 대응되는지 머릿속에 지도가 생깁니다.
> - `create_app()` 앱 팩토리와 `app.state` 가 Spring 의 빈 컨테이너와 어떻게 닮았는지 이해합니다.
> - `api → services → schemas` 3계층이 각각 무슨 책임을 지는지 설명할 수 있습니다.
> - Bearer 토큰 인증과 `hmac.compare_digest` 가 왜 "상수 시간 비교"여야 하는지 알게 됩니다.
> - **가장 중요**: async/await 와 `asyncio.to_thread` 가 왜 필요한지, 톰캣 스레드풀과 비교해서 확실히 잡고 갑니다.

---

## 7.1 FastAPI 는 Spring 의 어떤 부분인가

Spring 을 아신다면 FastAPI 는 놀랄 만큼 빠르게 익숙해집니다. 개념 대부분이 이름만 다르고 역할이 같거든요. 먼저 대응표부터 머리에 넣어두고 시작하겠습니다.

| 개념 | Spring (Java) | FastAPI (Python) | 이 프로젝트에서 |
|---|---|---|---|
| 컨트롤러 | `@RestController` + `@RequestMapping` | `APIRouter(prefix=...)` | `api/sessions.py` |
| 엔드포인트 | `@GetMapping` / `@PostMapping` | `@router.get(...)` / `@router.post(...)` | `create_session`, `list_sessions` |
| 요청/응답 DTO | POJO + `@Valid` | Pydantic `BaseModel` | `schemas/session.py` |
| 의존성 주입 | `@Autowired` / 생성자 주입 | `Depends(...)` | `Depends(_get_manager)` |
| 빈(Bean) 컨테이너 | `ApplicationContext` | `app.state` | `main.py` |
| 설정 파일 주입 | `application.yml` + `@Value` / `@ConfigurationProperties` | `pydantic-settings` `BaseSettings` | `core/config.py` |
| 서블릿 필터 / 인터셉터 | `OncePerRequestFilter` | 라우터 레벨 `dependencies=[...]` | `_require_auth` |
| 서비스 계층 | `@Service` | 평범한 클래스 | `services/session_manager.py` |
| 리포지토리 | `JpaRepository` | 직접 만든 CRUD 클래스 | `services/session_store.py` |
| 트랜잭션 경계 | `@Transactional` | `asyncio.Lock` (느낌만) | `SessionManager._create_lock` |

핵심 차이는 딱 하나입니다. **Spring 은 어노테이션 + 리플렉션으로 마법처럼 엮어주지만, FastAPI 는 "그냥 함수와 객체"를 명시적으로 엮습니다.** 마법이 적어서 처음엔 손이 더 가는 것 같지만, 대신 "이게 어디서 주입됐지?"를 추적하기가 훨씬 쉽습니다. 코드를 눈으로 따라가면 다 보이거든요.

> 한 줄 요약: FastAPI 는 "명시적으로 배선하는 Spring" 이라고 생각하시면 됩니다.

---

## 7.2 앱 팩토리 패턴 — `create_app()` 은 빈 컨테이너를 조립하는 곳

### 왜 필요한가
Spring 에서는 `@SpringBootApplication` 이 뜨면서 컴포넌트 스캔으로 빈들을 만들고, 서로 주입해서 하나의 `ApplicationContext` 를 완성합니다. FastAPI 에는 그런 자동 스캔이 없습니다. 그래서 **"어플리케이션이 부팅될 때 어떤 서비스 객체를 만들고, 어디에 꽂아둘지"를 우리 손으로 한 함수 안에 모아둡니다.** 이 함수를 관례적으로 `create_app()` 이라고 부르고, 이걸 "앱 팩토리 패턴"이라고 합니다.

### Spring 비유
`create_app()` 은 Spring 의 `@Configuration` 클래스 + `@Bean` 메서드들을 한 곳에 모아둔 것이라고 보시면 정확합니다. 여기서 만들어진 객체(=빈)들은 `app.state.xxx` 에 담깁니다. **`app.state` 가 곧 `ApplicationContext` 입니다.**

### 실제 코드
`create_app()` 안에서 벌어지는 일은 딱 "빈 생성 → 서로 주입 → app.state 에 등록" 순서입니다.

```python
docker_mgr   = DockerManager(...)          # 빈 1: docker SDK 래퍼
session_store = SessionStore(...)          # 빈 2: SQLite 리포지토리
session_mgr  = SessionManager(             # 빈 3: 서비스, 위 두 빈을 주입받음
    docker_manager=docker_mgr,
    store=session_store, ...
)
app.state.session_manager = session_mgr    # ApplicationContext 에 등록
app.state.api_token = settings.api_token   # 설정값도 여기에 보관
```

- 빈 조립: [main.py:49](../../backend/app/main.py#L49) ~ [main.py:68](../../backend/app/main.py#L68)
- 여기서 `SessionManager` 가 `DockerManager` 와 `SessionStore` 를 **생성자 주입**받는 게 보이시죠? Spring 의 생성자 주입과 100% 같은 그림입니다.
- 라우터 등록(= 컨트롤러 스캔에 해당): [main.py:112](../../backend/app/main.py#L112)
- 맨 마지막 줄 `app = create_app()` ([main.py:116](../../backend/app/main.py#L116)) 이 uvicorn(=톰캣 역할)이 잡아서 실행하는 진입점입니다.

### 흔한 함정
- **컨트롤러(api) 안에서 `DockerManager()` 를 직접 `new` 하지 마세요.** 그러면 docker 소켓 커넥션이 요청마다 새로 생기고, 테스트에서 목(mock)으로 갈아끼울 수도 없습니다. 반드시 `create_app()` 에서 한 번 만들어 `app.state` 를 통해 주입받으세요. (Spring 에서 서비스 안에서 `new Repository()` 하면 안 되는 것과 똑같은 이유입니다.)

> 한 줄 요약: `create_app()` = `@Configuration`, `app.state` = `ApplicationContext`.

---

## 7.3 계층 구조 — 누가 무슨 책임을 지는가

이 백엔드는 책임을 4개 층으로 명확히 나눴습니다. **이 경계를 넘나드는 코드는 리뷰에서 거절됩니다.** 그만큼 중요한 규칙이에요.

```
api/sessions.py            HTTP 껍데기 + 인증만. 비즈니스 로직 금지.
   ↓ (호출)
services/session_manager.py  생명주기 오케스트레이션, 어드미션 락, to_thread 감싸기
   ↓                    ↓
docker_manager.py      session_store.py
(docker SDK 래퍼)       (SQLite CRUD = 유일한 진실의 원천)

admission.py           순수 함수 정책 (sum(ratios) ≤ 1) — I/O 전혀 없음
schemas/session.py     Pydantic 모델 (DTO)
```

각 층의 책임을 Spring 에 대응시키면:

| 층 | 하는 일 | 절대 하면 안 되는 일 | Spring 대응 |
|---|---|---|---|
| `api/sessions.py` | 요청 파싱, 인증, 예외→HTTP 상태코드 변환 | 비즈니스 로직, DB 접근 | `@RestController` |
| `session_manager.py` | 생성/조회/정지/삭제 조율, 락, 상태 동기화 | docker SDK **직접** 호출 (반드시 docker_manager 경유) | `@Service` |
| `docker_manager.py` | `docker run` 옵션 조립, 컨테이너 조작 | DB 접근, 정책 판단 | 외부 시스템 어댑터 |
| `session_store.py` | SQLite CRUD | docker 접근, 정책 판단 | `@Repository` |
| `admission.py` | 순수 계산 (합이 1 넘나?) | docker/DB 접근 (호출자가 데이터를 넘겨줌) | 도메인 정책 유틸 |

**책임 규칙을 문장으로 외워두세요:**
> `api/` 는 비즈니스 로직을 절대 담지 않는다. `session_manager` 는 docker SDK 를 직접 부르지 않는다(반드시 `docker_manager` 를 거친다). `admission` 은 docker/DB 를 절대 만지지 않는다(호출자가 세션 목록을 넘겨준다).

이 규칙 덕분에 나중에 SQLite 를 Redis/Postgres 로 갈아끼울 때 `SessionStore` 인터페이스만 지키면 `SessionManager` 는 한 줄도 안 바뀝니다. (Spring 에서 `JpaRepository` 구현을 바꿔도 서비스가 안 바뀌는 것과 같아요.)

> 한 줄 요약: 층마다 책임이 하나씩. 위 층은 아래 층을 부르되, 건너뛰거나 거꾸로 부르지 않습니다.

---

## 7.4 설정 주입 — `FGPU_*` 환경변수와 pydantic-settings

### 왜 필요한가
런타임 이미지 이름, DB 경로, API 토큰 같은 값은 코드에 하드코딩하면 안 됩니다. 개발 노트북과 리눅스 GPU 서버에서 값이 달라야 하니까요. Spring 에서 `application.yml` + `@Value("${...}")` 로 외부 주입하는 그 필요성과 동일합니다.

### Spring 비유
`pydantic-settings` 의 `BaseSettings` 는 Spring 의 `@ConfigurationProperties(prefix="fgpu")` 와 거의 같습니다. **클래스 필드를 선언해두면, 같은 이름의 환경변수를 자동으로 읽어서 타입 변환까지 해서 채워줍니다.**

### 실제 코드
```python
class Settings(BaseSettings):
    runtime_image: str = "fgpu-runtime:stage2"   # 기본값
    api_token: str = ""                          # 빈 값 = 인증 비활성
    model_config = SettingsConfigDict(env_prefix="FGPU_", ...)
```

- `Settings` 클래스: [config.py:40](../../backend/app/core/config.py#L40)
- `env_prefix="FGPU_"` ([config.py:49](../../backend/app/core/config.py#L49)) 덕분에 필드 `runtime_image` 는 환경변수 `FGPU_RUNTIME_IMAGE` 로 override 됩니다. (Spring 의 prefix 와 동일한 개념.)
- `@lru_cache` 가 붙은 `get_settings()`: [config.py:52](../../backend/app/core/config.py#L52) — 한 번 만든 설정 객체를 캐시해서 매번 다시 안 읽습니다. 이건 **싱글턴 빈**과 같은 효과예요.
- 경로 자동 탐지: `host_hook_path` 나 `db_path` 가 비어 있으면 repo 루트를 추정해 채워 넣습니다 ([config.py:55](../../backend/app/core/config.py#L55)~[config.py:60](../../backend/app/core/config.py#L60)).

### 흔한 함정
- `@lru_cache` 때문에 **테스트에서 환경변수를 바꿔도 설정이 안 바뀌는** 함정이 있습니다. 이미 캐시된 값을 돌려주거든요. 테스트에서 다른 설정이 필요하면 `get_settings.cache_clear()` 를 호출하세요.
- 필드에 기본값을 안 주면 해당 환경변수가 필수가 됩니다. 없으면 부팅이 실패해요.

> 한 줄 요약: `BaseSettings` = `@ConfigurationProperties`, `env_prefix="FGPU_"` 로 환경변수와 자동 매핑.

---

## 7.5 DTO — Pydantic 모델

### 왜 필요한가
클라이언트가 보낸 JSON 을 신뢰할 수 없습니다. `ratio` 가 문자열로 오거나, 음수거나, 없을 수도 있어요. 들어오는 순간 검증하고 정제해야 합니다.

### Spring 비유
Pydantic `BaseModel` = 요청/응답 DTO + `@Valid` 검증 어노테이션이 하나로 합쳐진 것입니다. `Field(..., gt=0.0, le=1.0)` 이 `@DecimalMin/@DecimalMax` 역할을 합니다.

### 실제 코드
```python
class SessionCreate(BaseModel):        # POST 요청 body DTO
    ratio: float = Field(..., gt=0.0, le=1.0)   # 0 < ratio <= 1 아니면 422
    mode: SessionMode = Field(default="batch")
    force: bool = Field(default=False)
```

- 요청 DTO `SessionCreate`: [session.py:25](../../backend/app/schemas/session.py#L25) — `ratio` 검증 규칙은 [session.py:27](../../backend/app/schemas/session.py#L27).
- 응답/레코드 겸용 `Session`: [session.py:69](../../backend/app/schemas/session.py#L69).

> 참고: 이 프로젝트는 요청 DTO(`SessionCreate`)와 응답 DTO(`Session`)를 분리했지만, `Session` 하나가 "내부 레코드 + API 응답"을 겸합니다 ([session.py:4](../../backend/app/schemas/session.py#L4) 설계 메모 참고). Spring 이라면 Entity 와 응답 DTO 를 나누라고 배웠겠지만, 이 프로토타입 규모에선 변환 로직이 아까워서 통합했습니다.

컨트롤러에서 DTO 를 받는 모습은 이렇게 타입 힌트 한 줄이면 끝입니다 (`@RequestBody @Valid` 가 필요 없어요):

```python
@router.post("", response_model=Session, status_code=201)
async def create_session(body: SessionCreate, ...):   # 파싱+검증 자동
```
- [sessions.py:62](../../backend/app/api/sessions.py#L62)~[sessions.py:66](../../backend/app/api/sessions.py#L66)

### 흔한 함정
- Pydantic 검증에 걸리면 FastAPI 가 자동으로 **HTTP 422** 를 돌려줍니다. Spring 의 400 이 아니라 422 라는 점만 기억하세요.

> 한 줄 요약: Pydantic 모델 = DTO + 검증. 컨트롤러 인자에 타입만 써주면 파싱·검증 자동.

---

## 7.6 인증 — 라우터 레벨 의존성 + 상수 시간 비교

### 왜 필요한가
누구나 `POST /sessions` 로 컨테이너를 띄우면 GPU 서버가 순식간에 털립니다. 최소한의 문지기가 필요합니다.

### Spring 비유
이건 Spring 의 **인터셉터(또는 시큐리티 필터)** 에 해당합니다. 다만 FastAPI 는 이걸 "라우터에 붙는 의존성" 하나로 처리합니다.

```python
router = APIRouter(
    prefix="/sessions",
    dependencies=[Depends(_require_auth)],   # 이 라우터의 모든 엔드포인트에 적용
)
```
- 라우터 전체에 인증 의존성 부착: [sessions.py:51](../../backend/app/api/sessions.py#L51)~[sessions.py:55](../../backend/app/api/sessions.py#L55)

`dependencies=[...]` 에 넣은 의존성은 **해당 라우터의 모든 엔드포인트가 실행되기 전에 반드시 통과**해야 합니다. 인터셉터의 `preHandle` 과 같은 위치예요. `/sessions` 아래만 걸리고 `/healthz` 나 `/`(UI)는 별도로 등록돼서 인증 없이 열려 있습니다.

### 검증 로직
```python
def _require_auth(request, authorization: str | None = Header(default=None)):
    expected = getattr(request.app.state, "api_token", "") or ""
    if not expected:
        return                       # 토큰 미설정 = 인증 비활성 (개발 기본값)
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing bearer token", ...)
    given = authorization[len("Bearer "):]
    if not hmac.compare_digest(given, expected):   # ← 여기가 핵심
        raise HTTPException(401, "invalid bearer token", ...)
```
- 함수 전체: [sessions.py:28](../../backend/app/api/sessions.py#L28)~[sessions.py:48](../../backend/app/api/sessions.py#L48)
- 빈 토큰이면 통과(개발 기본): [sessions.py:33](../../backend/app/api/sessions.py#L33)
- 상수 시간 비교: [sessions.py:43](../../backend/app/api/sessions.py#L43)

### 왜 `hmac.compare_digest` 인가 (timing attack)
그냥 `given == expected` 로 비교하면 안 될까요? 안 됩니다. 파이썬의 `==` 는 **첫 글자가 다르면 즉시 False 를 반환**합니다. 그래서 공격자가 응답 시간을 정밀하게 재면 "앞의 몇 글자가 맞았는지"를 알아낼 수 있어요. 한 글자씩 브루트포스로 토큰을 복원할 수 있는 겁니다. 이게 **타이밍 공격(timing attack)** 입니다.

`hmac.compare_digest` 는 **일치하든 안 하든 항상 같은 시간**을 쓰도록 만들어져서, 응답 시간으로는 아무 정보도 새어나가지 않습니다. 비밀번호/토큰/서명 비교에는 반드시 이걸 쓰세요.

### 흔한 함정
- 401 응답에 `WWW-Authenticate: Bearer` 헤더를 같이 보내야 표준을 지키는 겁니다 ([sessions.py:39](../../backend/app/api/sessions.py#L39)). 빠뜨리기 쉬워요.
- `FGPU_API_TOKEN` 을 안 걸어두면 인증이 **꺼진 채로** 돌아갑니다. 개발엔 편하지만, 프로덕션에서 이 상태면 사고입니다.

> 한 줄 요약: 인증은 라우터 레벨 `Depends` 한 줄, 토큰 비교는 반드시 `hmac.compare_digest` 로 타이밍 공격 방어.

---

## 7.7 (제일 중요) async/await 와 `asyncio.to_thread` — 이벤트 루프를 막지 마라

이 절이 이 챕터의 핵심입니다. Spring 개발자가 FastAPI 에서 가장 크게 헷갈리는 지점이거든요. 천천히 갑니다.

### 7.7.1 Spring(톰캣)은 어떻게 동시 요청을 처리하나
Spring MVC 는 **스레드 풀 모델**입니다. 요청이 오면 톰캣이 풀에서 스레드 하나를 꺼내 그 요청 하나를 처음부터 끝까지 담당시킵니다. 그 스레드가 DB 쿼리를 기다리며 `blocking` 상태로 멈춰 있어도, **다른 요청은 풀의 다른 스레드가 처리**하니까 괜찮습니다. 스레드가 200개면 대략 200개 요청을 동시에 다룰 수 있죠. "느린 요청 하나가 다른 요청을 막지 않는" 이유는 **스레드가 많기 때문**입니다.

### 7.7.2 FastAPI(uvicorn)는 다르다 — 이벤트 루프 하나
FastAPI 의 기본 모델은 정반대입니다. **단일 이벤트 루프(single event loop)** — 즉 스레드 하나가 모든 요청을 번갈아가며 처리합니다. 요리사 한 명이 주방을 다 도는 그림이에요.

- 요리사(이벤트 루프)가 A 요청의 파스타를 삶기 시작하고(`await`), 물이 끓는 동안 **손을 놓고** B 요청의 샐러드를 만들러 갑니다.
- 물이 끓으면(비동기 작업 완료) 다시 A 로 돌아옵니다.
- 이렇게 "기다리는 시간"에 다른 일을 하기 때문에 스레드 하나로도 수천 요청을 다룹니다.

문제는 여기 있습니다. **만약 요리사가 "손을 놓지 않는" 작업(=blocking)을 만나면?** 예를 들어 그 자리에 서서 물이 끓을 때까지 멍하니 지켜본다면, 그동안 주방 전체가 멈춥니다. B, C, D 요청 전부 대기예요. 이게 **"이벤트 루프를 막는다(block the event loop)"** 는 말의 의미입니다.

### 7.7.3 무엇이 이벤트 루프를 막는가
- `await` 가 붙은 비동기 I/O (예: `async` DB 드라이버, `asyncio.sleep`) → **안 막습니다.** 손을 놓거든요.
- **동기(sync) 블로킹 호출** → **막습니다.** 대표적으로:
  - **docker SDK** (`self.client.containers.run(...)`) — 순수 동기 라이브러리. `docker run` 이 끝날 때까지 스레드가 통째로 멈춤.
  - **`sqlite3`** — 이것도 동기 라이브러리.

이 두 개를 `async def` 안에서 그냥 호출하면, 그 순간 이벤트 루프(요리사)가 그 자리에 붙잡혀서 **다른 모든 요청이 정지**합니다. 심지어 `/healthz` 헬스체크조차 응답을 못 합니다. 동시 `POST /sessions` 두 개가 사실상 순차 처리(직렬화)되어 버리고요.

### 7.7.4 해결책 — `asyncio.to_thread` 로 blocking 작업을 별도 스레드에 던진다
`asyncio.to_thread(함수, 인자...)` 는 **"이 동기 함수를 별도의 워커 스레드에서 돌리고, 끝날 때까지 나(이벤트 루프)는 손 놓고 있을게(`await`)"** 라는 뜻입니다.

```python
# 나쁜 예 — docker.run 이 이벤트 루프를 통째로 막음
c = self.docker.create_container(...)

# 좋은 예 — 별도 스레드에 던지고 이벤트 루프는 다른 요청을 계속 처리
c = await asyncio.to_thread(self.docker.create_container, name=name, ...)
```

효과를 그림으로 보면:
- 요리사(이벤트 루프)가 "이 무거운 동기 작업은 보조 요리사(워커 스레드)한테 맡겨" 하고 넘깁니다.
- 보조가 `docker run` 을 처리하는 동안 요리사는 다른 요청을 계속 받습니다.
- 보조가 끝나면 요리사가 결과를 받아 이어갑니다.

**결국 FastAPI 는 "비동기 이벤트 루프 + 필요할 때만 스레드 풀"의 하이브리드가 됩니다.** Spring 은 처음부터 끝까지 스레드 풀, FastAPI 는 평소엔 이벤트 루프 하나로 버티다가 blocking 작업만 스레드로 격리하는 거예요.

### 실제 코드 (규칙: 모든 docker/sqlite 호출은 to_thread 로 감싼다)
`SessionManager` 는 이 규칙을 철저히 지킵니다.

- docker 컨테이너 생성: [session_manager.py:211](../../backend/app/services/session_manager.py#L211)
- 상태 조회(docker): [session_manager.py:294](../../backend/app/services/session_manager.py#L294)
- store insert(sqlite): [session_manager.py:271](../../backend/app/services/session_manager.py#L271)
- store get(sqlite): [session_manager.py:289](../../backend/app/services/session_manager.py#L289)
- 로그/정지/삭제도 전부 `to_thread`: [session_manager.py:327](../../backend/app/services/session_manager.py#L327), [session_manager.py:339](../../backend/app/services/session_manager.py#L339), [session_manager.py:349](../../backend/app/services/session_manager.py#L349)

그리고 목록 조회는 한발 더 나갑니다. 각 세션의 상태를 docker 와 동기화할 때 **`asyncio.gather` 로 동시에** 처리해서 응답 지연을 줄입니다:

```python
results = await asyncio.gather(*(self.get(r.id) for r in recs))
```
- [session_manager.py:317](../../backend/app/services/session_manager.py#L317)~[session_manager.py:319](../../backend/app/services/session_manager.py#L319)

`gather` 는 여러 비동기 작업을 동시에 시작해 모두 끝날 때까지 기다립니다. Java 의 `CompletableFuture.allOf(...)` 와 같은 개념이에요.

### 흔한 함정 (실무에서 진짜 자주 터짐)
- **`async def` 안에서 동기 blocking 호출을 그냥 부르는 것** — 가장 흔하고 가장 치명적입니다. 코드는 잘 도는 것처럼 보이지만, 부하가 걸리면 처리량이 급락합니다. "왜 동시 요청이 순차로 도는 것 같지?" 싶으면 십중팔구 이겁니다.
- **`to_thread` 를 `await` 없이 호출** — 그러면 코루틴 객체만 만들어지고 실행이 안 됩니다. 반드시 `await asyncio.to_thread(...)`.
- **CPU 를 오래 쓰는 순수 계산을 `async def` 에 넣는 것** — 이것도 이벤트 루프를 막습니다. (다만 이 프로젝트의 `admission.check` 같은 계산은 마이크로초 단위라 무시해도 됩니다.)

> 한 줄 요약: FastAPI 는 요리사 한 명(이벤트 루프)이 돌아가는 주방. docker/sqlite 같은 동기 blocking 작업은 반드시 `await asyncio.to_thread(...)` 로 보조 요리사에게 넘겨 주방이 멈추지 않게 하세요.

---

## 7.8 REST 엔드포인트 한눈에 보기

`/sessions` 라우터가 제공하는 엔드포인트 전체입니다. (`/healthz`, `/` 는 `main.py` 에 별도 등록, 인증 면제.)

| 메서드 & 경로 | 하는 일 | 성공 코드 | 코드 위치 |
|---|---|---|---|
| `POST /sessions` | 세션 생성(컨테이너 spawn). 어드미션 초과 시 409, force 로 우회 | 201 | [sessions.py:62](../../backend/app/api/sessions.py#L62) |
| `GET /sessions` | 전체 세션 목록 (docker 와 상태 reconcile) | 200 | [sessions.py:104](../../backend/app/api/sessions.py#L104) |
| `GET /sessions/admission` | GPU 별 ratio 사용 현황 (capacity 표시용) | 200 | [sessions.py:98](../../backend/app/api/sessions.py#L98) |
| `GET /sessions/{id}` | 세션 상세 (status/exit_code 자동 갱신) | 200 / 404 | [sessions.py:109](../../backend/app/api/sessions.py#L109) |
| `GET /sessions/{id}/logs` | 컨테이너 stdout+stderr 일부 (`?tail=`) | 200 / 404 | [sessions.py:117](../../backend/app/api/sessions.py#L117) |
| `POST /sessions/{id}/stop` | 컨테이너 정지 (레코드는 보존) | 200 / 404 | [sessions.py:129](../../backend/app/api/sessions.py#L129) |
| `DELETE /sessions/{id}` | 컨테이너 삭제 + 레코드 제거 (`?purge_workspace=`) | 200 / 404 | [sessions.py:137](../../backend/app/api/sessions.py#L137) |
| `GET /healthz` | 헬스체크 + 인증 활성 여부 | 200 | [main.py:96](../../backend/app/main.py#L96) |
| `GET /` | 단일 파일 웹 UI | 200 | [main.py:108](../../backend/app/main.py#L108) |

예외를 HTTP 상태로 번역하는 것도 컨트롤러의 몫입니다. 어드미션 초과(`AdmissionDenied`)는 409, 그 밖의 docker 오류는 500 으로 감싸는 부분을 보세요: [sessions.py:79](../../backend/app/api/sessions.py#L79)~[sessions.py:95](../../backend/app/api/sessions.py#L95). (Spring 의 `@ExceptionHandler` 를 인라인으로 쓴 셈입니다.)

> 한 줄 요약: 컨트롤러는 "HTTP 껍데기 + 예외→상태코드 변환"만 하고, 실제 일은 전부 `SessionManager` 에 위임합니다.

---

## ✍️ 스스로 점검

1. Spring 의 `@Autowired`, `ApplicationContext`, `@ConfigurationProperties` 는 이 백엔드에서 각각 무엇에 대응하나요? 코드 위치를 하나씩 대보세요.
2. `async def create_session` 안에서 `self.docker.create_container(...)` 를 `to_thread` 없이 그냥 호출하면 어떤 일이 벌어질까요? 톰캣 스레드풀과 비교해서 설명해 보세요.
3. `given == expected` 대신 `hmac.compare_digest(given, expected)` 를 쓰는 이유는 무엇인가요? 어떤 공격을 막나요?

## 🎯 다음 챕터

8장 **세션 생명주기와 3계층 자원 집행** 에서는 이번에 배운 계층 구조를 실제로 따라갑니다. 세션 하나가 `POST /sessions` 로 태어나서 → 어드미션 검사 → 컨테이너 spawn → SQLite 기록 → 상태 reconcile → 삭제까지 어떻게 흘러가는지, 그리고 어드미션 / 훅 / 드라이버 **세 층**이 각각 어떤 종류의 실패를 잡아내는지 표로 정리합니다.

---

⟵ [이전: 6장. 스레드 안전성](06-thread-safety.md) ・ [📚 전체 목차](README.md) ・ [다음: 8장. 세션 생명주기](08-backend-lifecycle-and-admission.md) ⟶
