# 6장. Stage 9·10·11 — 운영 기능 (인증·멀티GPU·Jupyter·어드미션)

## 이 장에서 만들 것

- 3장에서 완성한 "컨테이너를 API 로 띄우는" 백엔드 위에, 운영에 필요한 네 기능을 **한 번에 하나씩** 얹습니다.
- Stage 9: Bearer 토큰 인증(빈 토큰 = 비활성 기본값) + 멀티-GPU 디바이스 핀(`gpu_index` → `--gpus device=N`).
- Stage 10: `mode="jupyter"` — Jupyter Lab 세션. 여기서 개발상 가장 까다로운 부분은 **docker 가 자동 할당한 ephemeral 포트를 나중에 찾아내는 재시도 루프**입니다.
- Stage 11: 어드미션 컨트롤. `sum(ratios) ≤ 1.0` 정책을 **순수함수로 먼저 만들고 단위테스트한 뒤**, 그것을 `asyncio.Lock` 으로 감싸 통합합니다. 이 장의 관통 주제가 바로 **"순수함수 먼저, 통합 나중"** 입니다.

> 이 기능들은 각각 독립적으로 개발·검증됩니다. 하나 얹고 게이트 통과, 다음 얹고 게이트 통과. 3장의 리듬을 그대로 이어갑니다.

---

## 6.1 목표: 무엇이 되면 성공인가

- **Stage 9 인증 off(기본):** `FGPU_API_TOKEN` 미설정 시 `smoke_test_api.sh` 가 헤더 없이 그대로 통과. `/healthz` 는 `auth_enabled: false`.
- **Stage 9 인증 on:** `FGPU_API_TOKEN=secret` 이면 `POST /sessions` 이 헤더 없으면 401(`missing bearer token`), 틀린 토큰이면 401(`invalid bearer token`), 맞으면 201. `/healthz` 와 `/`(UI)는 여전히 public.
- **Stage 9 멀티-GPU:** `{"ratio":0.4,"gpu_index":1}` → `--gpus device=1` 로 spawn, 재시작해도 `gpu_index` 보존.
- **Stage 10 Jupyter:** `mode="jupyter"` 세션이 뜨고, `http://<host>:<ephemeral_port>/lab?token=...` 로 접속되며, 워크스페이스가 bind-mount 로 영속.
- **Stage 11 어드미션:** `pytest backend/tests/test_admission.py` 17/17 통과. 0.7 세션이 있는 상태에서 0.4 요청 → 409. `force:true` 면 통과. 동시 POST 0.6 두 개 → 정확히 하나만 201, 나머지 409.

---

## 6.2 Stage 9 — Bearer 인증: 라우터 레벨 의존성 하나

### 개발 순서

1. `Settings` 에 `api_token: str = ""` 추가(빈 값 = 비활성).
2. `main.py` 에서 `app.state.api_token = settings.api_token` 로 매닮.
3. `_require_auth` 의존성 함수 작성.
4. 라우터 생성 시 `dependencies=[Depends(_require_auth)]` 한 줄로 **전체 라우트에** 적용.

인증을 각 핸들러에 흩뿌리지 않고 **라우터 레벨 의존성 하나**로 거는 게 핵심입니다. `/sessions/*` 전부가 자동으로 보호되고, `/healthz` 와 `/`(UI)는 다른 곳에 정의돼 있어 영향받지 않습니다.

```python
# api/sessions.py
import hmac
from fastapi import APIRouter, Depends, Header, HTTPException, Request, status

def _require_auth(request: Request, authorization: str | None = Header(default=None)):
    expected = getattr(request.app.state, "api_token", "") or ""
    if not expected:
        return                                    # 빈 토큰 = 인증 비활성
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing bearer token",
                            headers={"WWW-Authenticate": "Bearer"})
    given = authorization[len("Bearer "):]
    if not hmac.compare_digest(given, expected):  # 상수 시간 비교
        raise HTTPException(401, "invalid bearer token",
                            headers={"WWW-Authenticate": "Bearer"})

router = APIRouter(prefix="/sessions", tags=["sessions"],
                   dependencies=[Depends(_require_auth)])
```

완성본: [sessions.py:28-55](../../backend/app/api/sessions.py#L28-L55).

세 가지 설계 결정:

- **빈 토큰 = 인증 비활성.** 개발 기본값입니다. `expected` 가 빈 문자열이면 즉시 `return` 해서 통과([sessions.py:32-34](../../backend/app/api/sessions.py#L32-L34)). 그래서 3장의 `smoke_test_api.sh` 가 변경 없이 계속 돕니다.
- **`hmac.compare_digest` 로 상수 시간 비교.** `given == expected` 로 하면 문자열 앞부분만 맞아도 빨리 실패해 타이밍 공격에 정보가 샙니다. 상수 시간 비교로 이를 막습니다([sessions.py:43](../../backend/app/api/sessions.py#L43)).
- **`/healthz` 는 인증 상태를 노출**하되 자신은 public 유지([main.py:84](../../backend/app/main.py#L84)). 운영자가 인증이 켜졌는지 확인할 수 있게.

### 멀티-GPU: 시그니처는 이미 준비돼 있다

멀티-GPU 는 사실 3장에서 미리 깔아둔 자리에 값만 흘려보내면 됩니다. `docker_manager.create_container` 는 이미 `gpu_index` 를 받아 `--gpus device=N` 을 조립합니다([docker_manager.py:102-108](../../backend/app/services/docker_manager.py#L102-L108)). 할 일은:

1. `SessionCreate` 에 `gpu_index: Optional[int]` 추가([session.py:47-50](../../backend/app/schemas/session.py#L47-L50)).
2. `Session` 에도 `gpu_index` 추가(응답/영속용)([session.py:76](../../backend/app/schemas/session.py#L76)).
3. `session_store` 에 멱등 마이그레이션 한 줄 — `ALTER TABLE sessions ADD COLUMN gpu_index INTEGER`([session_store.py:56](../../backend/app/services/session_store.py#L56)).
4. 라우터 → 매니저 → docker_manager 로 `gpu_index` 를 관통시킴.

3장에서 세운 "필드는 추가만" + "멱등 마이그레이션" 규칙 덕에 스키마 변경이 무섭지 않습니다.

### 여기서 확인 (Stage 9 게이트)

```bash
FGPU_API_TOKEN=secret-dev-token ./scripts/run_backend.sh
curl -sS http://localhost:8000/healthz            # auth_enabled: true
curl -i -X POST http://localhost:8000/sessions -H 'Content-Type: application/json' -d '{"ratio":0.4}'
#   → 401 {"detail":"missing bearer token"},  WWW-Authenticate: Bearer
curl -i -X POST http://localhost:8000/sessions \
  -H 'Authorization: Bearer wrong' -H 'Content-Type: application/json' -d '{"ratio":0.4}'
#   → 401 {"detail":"invalid bearer token"}
curl -X POST http://localhost:8000/sessions \
  -H 'Authorization: Bearer secret-dev-token' -H 'Content-Type: application/json' -d '{"ratio":0.4}'
#   → 201 정상 생성
```

> **함정.** `gpu_index` 검증은 GPU 가 2개 이상인 호스트에서만 의미가 있습니다. 단일 GPU 박스에서 `gpu_index:1` 을 주면 docker 가 spawn 단계에서 실패해 500 이 됩니다. 개발/데모 시엔 단일 GPU 면 `gpu_index` 를 생략(=`None`=전 GPU)하세요.

---

## 6.3 Stage 10 — Jupyter 인터랙티브 세션

배치(batch) 세션은 "명령 실행 후 종료"였습니다. Jupyter 세션은 "사용자가 stop 할 때까지 살아있는 서버"라 성격이 다릅니다. 추가로 (1)명령 강제, (2)랜덤 토큰, (3)ephemeral 포트 발견, (4)워크스페이스 영속이 필요합니다.

### 개발 순서

1. `SessionCreate.mode: Literal["batch","jupyter"] = "batch"` 추가([session.py:22](../../backend/app/schemas/session.py#L22), [29-32](../../backend/app/schemas/session.py#L29-L32)). 기본이 `batch` 라 하위 호환.
2. `build_jupyter_command(token)` — `jupyter lab` 명령 조립([docker_manager.py:38-51](../../backend/app/services/docker_manager.py#L38-L51)).
3. 매니저의 `create` 에서 mode 분기: jupyter 면 토큰 생성 + 워크스페이스 디렉토리 + 포트 publish.
4. **spawn 후 ephemeral 포트를 재시도 루프로 발견.** ← 가장 까다로운 부분.
5. `jupyter_url` 조립 + 관련 필드 영속(스키마 + 마이그레이션).

### mode 분기: 명령을 강제한다

jupyter 모드에선 사용자가 준 `command` 를 **무시**하고 `jupyter lab` 로 강제합니다. 사용자가 임의 명령을 주면 서버가 안 뜨니까요.

```python
# session_manager._create_locked 안 (핵심만)
if mode == "jupyter":
    img = image or _DEFAULT_JUPYTER_IMAGE          # jupyterlab 깔린 pytorch 이미지
    jupyter_token = secrets.token_urlsafe(24)      # 세션마다 랜덤 토큰
    cmd = build_jupyter_command(jupyter_token)      # 사용자 command 무시, 강제
    workspace_dir = os.path.join(self.workspace_root, sid)
    await asyncio.to_thread(lambda: Path(workspace_dir).mkdir(parents=True, exist_ok=True))
    ports = {f"{_JUPYTER_CONTAINER_PORT}/tcp": None}   # None = ephemeral 할당
    jupyter_mode = True
else:
    img = image or self.runtime_image
    cmd = command or list(self.default_command)
    jupyter_token = workspace_dir = ports = None
    jupyter_mode = False
```

완성본: [session_manager.py:122-142](../../backend/app/services/session_manager.py#L122-L142).

- `secrets.token_urlsafe(24)` — 세션마다 예측 불가능한 토큰. 이게 Jupyter 접속 인증이 됩니다.
- `ports = {"8888/tcp": None}` — `None` 이 "호스트 포트를 docker 가 알아서 골라라(ephemeral)"라는 뜻입니다. 우리가 포트를 고정하지 않는 이유는 여러 세션이 동시에 뜰 때 충돌을 피하기 위함입니다.
- 워크스페이스 디렉토리를 호스트에 만들어 `/workspace` 로 bind-mount([docker_manager.py:119-123](../../backend/app/services/docker_manager.py#L119-L123)). 컨테이너를 지워도 노트북이 살아남습니다.

### 까다로운 부분: ephemeral 포트 발견 재시도 루프

`ports=None` 으로 맡겼으니 **어느 호스트 포트가 배정됐는지 우리가 나중에 물어봐야** 합니다. 문제는 타이밍입니다. `containers.run()` 이 반환한 직후엔 컨테이너가 막 시작돼서 포트 바인딩 정보(`NetworkSettings.Ports`)가 아직 `attrs` 에 안 올라와 있을 수 있습니다. 그래서 **짧은 백오프로 재시도**합니다.

```python
if jupyter_mode:
    for delay in (0.0, 0.1, 0.2, 0.4, 0.8):        # 지수 백오프
        if delay:
            await asyncio.sleep(delay)
        host_port = await asyncio.to_thread(
            self.docker.get_host_port, c.id, _JUPYTER_CONTAINER_PORT
        )
        if host_port:
            break
    if host_port:
        jupyter_url = f"http://{_PUBLIC_HOST}:{host_port}/lab?token={jupyter_token}"
```

완성본: [session_manager.py:162-176](../../backend/app/services/session_manager.py#L162-L176). `get_host_port` 는 `NetworkSettings.Ports["8888/tcp"][0]["HostPort"]` 를 읽습니다([docker_manager.py:146-160](../../backend/app/services/docker_manager.py#L146-L160)).

> **함정.** 첫 시도(`delay=0.0`)에 바로 성공한다고 가정하면 가끔 `host_port=None` 인 세션이 나옵니다. 재시도 루프가 없으면 개발 중엔 대부분 되다가 부하가 있을 때 간헐적으로 실패합니다 — 재현이 어려운 가장 나쁜 버그입니다. 처음부터 백오프를 넣으세요. 반대로 무한 재시도도 금물 — 진짜 실패(포트 publish 자체가 안 됨) 시 create 가 영영 안 끝납니다. 유한한 백오프 리스트로 상한을 둡니다.

### 워크스페이스는 삭제하지 않는 것이 기본

`DELETE /sessions/{id}` 는 컨테이너와 record 는 지우지만 워크스페이스 디렉토리는 **보존**합니다(노트북은 사용자 데이터). 명시적으로 `?purge_workspace=true` 를 줘야 삭제합니다([session_manager.py:256-273](../../backend/app/services/session_manager.py#L256-L273), [sessions.py:136-150](../../backend/app/api/sessions.py#L136-L150)).

### 여기서 확인 (Stage 10 게이트)

```bash
./scripts/eval/run_jupyter.sh    # jupyter 세션 생성 → /api/status 200 → 워크스페이스 파일 검증
# summary.txt 마지막이 VERDICT: PASS
```

---

## 6.4 Stage 11 — 어드미션 컨트롤: 순수함수 먼저, 통합 나중

이 장의 하이라이트입니다. 개발 패턴 자체가 교훈입니다.

hook(3장의 `LD_PRELOAD`)은 컨테이너 **내부**의 `cudaMalloc` 시점에 per-container quota 를 강제합니다. 하지만 그것만으론 `ratio=0.7` 과 `ratio=0.4` 세션이 **둘 다** 성공합니다 — 각자는 자기 quota 안이니까요. 합치면 1.1 로 GPU 를 초과하는데도요. 어드미션은 spawn **시점**에 "합이 1.0 을 넘으면 거부"하는 **스케줄러 레이어 게이트**입니다. 두 레이어는 서로 다른 실패를 잡습니다.

### 왜 순수함수 먼저인가

정책 로직(합산, GPU overlap 규칙, FP 오차 허용)은 docker 도 DB 도 필요 없는 **순수 계산**입니다. 이걸 I/O 에서 떼어 놓으면:

- 하드웨어 없이 `pytest` 로 17가지 경우를 빠르게 검증할 수 있습니다.
- 통합 코드(`session_manager`)는 "이미 검증된 정책을 언제/어떤 락 아래서 부르느냐"만 신경 쓰면 됩니다.

그래서 **1단계: `admission.py` 순수함수 작성 → 2단계: `test_admission.py` 로 검증 → 3단계: `session_manager.create` 에 통합** 순서로 갑니다.

### 1단계 — admission.py (순수함수, I/O 없음)

```python
# services/admission.py
_TOL = 1e-9   # FP 오차 허용 (0.3+0.3+0.4 = 1.0000000000000002)

def gpu_overlaps(a, b) -> bool:
    if a is None or b is None:      # None = 전체 GPU → 무엇과도 overlap
        return True
    return a == b

def sum_used_ratio(sessions, gpu_index) -> tuple[float, int]:
    used, n = 0.0, 0
    for r in sessions:
        if r.status not in ("created", "running"):   # 종료된 세션은 quota 해제됨
            continue
        if not gpu_overlaps(r.gpu_index, gpu_index):
            continue
        used += r.ratio; n += 1
    return used, n

def check(sessions, requested_ratio, gpu_index) -> None:
    used, n = sum_used_ratio(sessions, gpu_index)
    if used + requested_ratio > 1.0 + _TOL:
        raise AdmissionDenied(requested=requested_ratio, currently_used=used,
                              gpu_index=gpu_index, active_sessions=n)
```

완성본: [admission.py:62-105](../../backend/app/services/admission.py#L62-L105). GPU overlap 규칙이 핵심입니다:

- `None`(전 GPU)은 **모든** device 와 overlap → `None` 세션은 모든 device 의 quota 에 합산됨.
- 같은 정수는 overlap, 다른 정수는 격리([admission.py:62-69](../../backend/app/services/admission.py#L62-L69)).
- 단일 GPU 호스트에선 전부 overlap → 정책이 그냥 `sum(active_ratios) ≤ 1.0` 으로 축소됩니다.

`_TOL` 은 왜 필요할까요? `0.3+0.3+0.4` 가 부동소수점에서 정확히 1.0 이 아니라 `1.0000000000000002` 가 되기 때문입니다. tolerance 없이는 "정확히 꽉 채우기"가 거부돼 버립니다([admission.py:99](../../backend/app/services/admission.py#L99)).

이 모듈은 **docker 도 DB 도 안 건드립니다.** 호출자가 이미 조회한 `sessions` 리스트를 넘겨줍니다. UI 용 `usage_snapshot()` 도 여기 순수함수로 둡니다([admission.py:108-135](../../backend/app/services/admission.py#L108-L135)).

### 2단계 — test_admission.py 로 정책만 검증

순수함수라 docker 없이 전부 테스트됩니다([test_admission.py](../../backend/tests/test_admission.py)). `_sess(...)` 헬퍼로 가짜 `Session` 을 만들어([test_admission.py:19-36](../../backend/tests/test_admission.py#L19-L36)) 17가지 경우를 검증합니다:

- `gpu_overlaps` 진리표([test:41-50](../../backend/tests/test_admission.py#L41-L50)).
- 종료된 세션은 합산 제외([test:55-64](../../backend/tests/test_admission.py#L55-L64)).
- GPU 격리 — 다른 device 는 서로 영향 없음([test:67-77](../../backend/tests/test_admission.py#L67-L77), [134-138](../../backend/tests/test_admission.py#L134-L138)).
- 정확히 1.0 통과 + FP tolerance([test:101-109](../../backend/tests/test_admission.py#L101-L109)).
- 오버서브 거부 시 `AdmissionDenied` 필드 검증([test:112-121](../../backend/tests/test_admission.py#L112-L121)).

**force 를 여기서 테스트하지 않는 이유**: `force=True` 는 통합 레이어의 "check 를 아예 안 부른다"일 뿐입니다. 순수함수 테스트에선 `check` 를 호출하지 않는 것 자체가 force 와 동치라, 순수 정책만 검증하면 됩니다.

```bash
cd backend && pytest tests/test_admission.py    # 17 passed
```

### 3단계 — session_manager.create 에 통합 + asyncio.Lock

이제 검증된 순수함수를 통합합니다. 여기서 **원자성**이 결정적입니다.

```python
class SessionManager:
    def __init__(self, ...):
        ...
        self._create_lock = asyncio.Lock()      # check-then-spawn 직렬화

    async def create(self, ..., force=False):
        async with self._create_lock:            # 전체 create 를 락 하에
            return await self._create_locked(...)

    async def _create_locked(self, ..., force):
        if not force:
            sessions = await self.list_all()     # docker 와 reconcile 된 상태로
            admission.check(sessions, requested_ratio=ratio, gpu_index=gpu_index)
        # ... 여기서 컨테이너 spawn + store.insert
```

완성본: [session_manager.py:82-116](../../backend/app/services/session_manager.py#L82-L116).

### 왜 Lock 이 없으면 오버서브되는가 (손으로 재현)

빈 GPU 에 `ratio=0.6` POST 두 개가 **동시에** 들어온다고 합시다. Lock 없이 각각 이렇게 진행합니다:

```
요청 A: list_all() → used=0.0 → check(0.0+0.6 ≤ 1.0) 통과 → spawn 시작
요청 B: list_all() → used=0.0 → check(0.0+0.6 ≤ 1.0) 통과 → spawn 시작
        (A 의 insert 가 아직 안 끝나서 B 의 list_all 이 A 를 못 봄!)
결과: 0.6 + 0.6 = 1.2  → 오버서브 통과
```

문제의 핵심은 **check(읽기)와 insert(쓰기) 사이의 틈**입니다. 그 틈에 다른 요청이 끼어들면 둘 다 같은 낡은 capacity 로 판단합니다. 게다가 3장에서 `create` 안의 docker/sqlite 호출을 `to_thread` 로 감쌌기 때문에, 그 `await` 지점마다 이벤트 루프가 다른 요청으로 전환됩니다 — 틈이 벌어질 기회가 오히려 더 많아진 겁니다.

`asyncio.Lock` 으로 `check → spawn → insert` 전체를 하나의 임계 구역으로 묶으면, B 는 A 가 insert 를 끝낼 때까지 기다렸다가 `list_all()` 에서 A 를 보고 `0.6+0.6 > 1.0` 으로 거부됩니다. 정확히 하나만 통과합니다.

> **함정.** `to_thread` 는 이벤트 루프를 안 막지만(좋음), 바로 그 때문에 락 없는 check-then-insert 가 더 쉽게 깨집니다. "async 로 만들었으니 동시성 안전"이 아니라, **check 와 insert 를 원자적으로 묶어야** 안전합니다. 반대로 Lock 을 `docker.run` 을 포함한 전 구간에 걸어도 `docker.run` 자체는 `to_thread` 안에서 돌므로 이벤트 루프는 계속 다른 종류의 요청(GET, healthz)을 처리합니다 — 직렬화되는 건 오직 create 끼리입니다([session_manager.py:69-73](../../backend/app/services/session_manager.py#L69-L73)).

### 409 응답 구조

라우터는 `AdmissionDenied` 를 잡아 **구조화된 409** 로 번역합니다(raw 문자열이 아니라 클라이언트가 파싱 가능한 필드로).

```python
except AdmissionDenied as e:
    raise HTTPException(status_code=409, detail={
        "error": "admission_denied",
        "message": str(e),
        "requested_ratio": e.requested,
        "currently_used": e.currently_used,
        "available": max(0.0, 1.0 - e.currently_used),
        "gpu_index": e.gpu_index,
        "active_sessions": e.active_sessions,
    })
```

완성본: [sessions.py:78-91](../../backend/app/api/sessions.py#L78-L91). `available` 을 함께 주니 클라이언트가 "얼마면 통과되는지"를 바로 알 수 있습니다. 현황 조회용 `GET /sessions/admission` 도 추가합니다([sessions.py:97-100](../../backend/app/api/sessions.py#L97-L100), [session_manager.py:76-79](../../backend/app/services/session_manager.py#L76-L79)).

### 여기서 확인 (Stage 11 게이트)

```bash
cd backend && pytest tests/test_admission.py               # 17 passed

# 실행 중 백엔드에서:
curl -X POST .../sessions -d '{"ratio":0.7}'               # 201
curl -i -X POST .../sessions -d '{"ratio":0.4}'            # 409 admission_denied, available:0.3
curl -X POST .../sessions -d '{"ratio":0.4,"force":true}'  # 201 (명시적 오버서브)
curl -sS .../sessions/admission                            # {"by_gpu":{"all":{"ratio_used":...}}}

# 동시성: 빈 상태에서 0.6 두 개 동시 POST → 정확히 하나만 201, 나머지 409
```

---

## 완성 체크리스트

- [ ] `_require_auth` 를 라우터 레벨 `dependencies` 로 걸어 `/sessions/*` 전체 보호, `/healthz`·`/` 는 public 유지.
- [ ] 빈 `FGPU_API_TOKEN` = 인증 비활성(기본), `hmac.compare_digest` 상수 시간 비교.
- [ ] `gpu_index` 를 스키마 → 매니저 → docker(`--gpus device=N`)로 관통, 멱등 마이그레이션 추가.
- [ ] `mode="jupyter"` — 명령 강제, `secrets` 토큰, `ports=None`(ephemeral), 워크스페이스 bind-mount.
- [ ] ephemeral 포트를 유한 백오프 재시도 루프로 발견, `jupyter_url` 조립.
- [ ] `admission.py` 순수함수(`gpu_overlaps`/`sum_used_ratio`/`check`) 작성 — docker/DB 미접촉.
- [ ] `test_admission.py` 17/17 통과(하드웨어 불필요).
- [ ] `create` 를 `asyncio.Lock` 으로 감싸 check-then-spawn 원자화.
- [ ] `AdmissionDenied` → 구조화된 409, `GET /sessions/admission` 현황 조회.
- [ ] 동시 POST 두 개 중 정확히 하나만 통과(락 검증).

## 다음 챕터

여기까지로 백엔드의 운영 기능(인증·멀티GPU·인터랙티브·어드미션)이 완성됐습니다. 다음은 hook 레이어(`libfgpu.so`)와 백엔드를 하나로 엮는 end-to-end 평가(`scripts/eval/*.sh`)로, GPU 박스에서 두 컨테이너가 실제로 quota 를 나눠 갖는 것을 측정합니다.
