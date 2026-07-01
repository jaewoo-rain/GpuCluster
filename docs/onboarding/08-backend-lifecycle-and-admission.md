# 8장. 세션 생명주기와 3계층 자원 집행

> 📘 **이 장을 읽고 나면**
>
> - 세션 하나가 `POST /sessions` 로 태어나 삭제될 때까지의 전 과정을 코드 위치와 함께 그릴 수 있습니다.
> - `DockerManager` 가 `docker run` 옵션(`--gpus`, 훅 마운트, `LD_PRELOAD`, jupyter 포트)을 어떻게 조립하는지 이해합니다.
> - `SessionStore` 가 왜 매 호출마다 새 SQLite 커넥션을 여는지, 재시작 후에도 세션이 살아있는 이유가 무엇인지 설명할 수 있습니다.
> - **어드미션 / 훅 / 드라이버** 3계층이 각각 어떤 실패를 잡는지 표로 구분할 수 있습니다.
> - 백엔드가 훅에게 "환경변수로만" 쿼터를 전달하는 설계 철학을 이해합니다.

이 장은 7장의 계층 구조(api → services → schemas)를 "실제로 한 요청이 흐르는" 관점에서 다시 봅니다. 7장이 지도였다면 이 장은 그 지도를 따라 걷는 답사입니다.

---

## 8.1 세션의 일생 — `POST /sessions` 부터 삭제까지

한 세션이 태어나서 죽기까지의 전체 흐름입니다. 각 단계가 어느 파일 어느 함수인지 붙여뒀어요.

```
[클라이언트] POST /sessions {ratio: 0.4}
      │
      ▼
① 인증  _require_auth              api/sessions.py
      │
      ▼
② 컨트롤러 create_session          api/sessions.py  → mgr.create(...) 로 위임
      │
      ▼
③ 어드미션 락 획득 create()         session_manager.py  (asyncio.Lock)
      │
      ▼
④ 어드미션 검사 admission.check()   admission.py  (합이 1 넘으면 409)
      │
      ▼
⑤ 컨테이너 spawn                    docker_manager.create_container()  (to_thread)
      │
      ▼
⑥ SQLite 기록 store.insert()        session_store.py  (to_thread)
      │
      ▼
⑦ 락 해제, Session 응답 (201)
      ─────────────────────────────
      이후 조회할 때마다:
⑧ 상태 reconcile get()             session_manager.py  (docker 데몬과 동기화)
      ─────────────────────────────
      마지막에:
⑨ stop / delete                    session_manager.py  → docker_manager
```

### 진입점 — 컨트롤러는 위임만
컨트롤러는 body 를 받아 `SessionManager.create(...)` 로 넘기고, 예외를 HTTP 로 번역하는 게 전부입니다.
- [sessions.py:62](backend/app/api/sessions.py#L62)~[sessions.py:94](backend/app/api/sessions.py#L94)

### 락으로 감싼 create
```python
async def create(self, ratio, ..., force=False):
    async with self._create_lock:              # ③ 락 획득
        return await self._create_locked(...)  # ④~⑥ 을 원자적으로
```
- `create()`: [session_manager.py:82](backend/app/services/session_manager.py#L82)
- 락 필드 `_create_lock` 정의(주석에 이유까지): [session_manager.py:69](backend/app/services/session_manager.py#L69)~[session_manager.py:73](backend/app/services/session_manager.py#L73)
- 실제 작업 `_create_locked()`: [session_manager.py:102](backend/app/services/session_manager.py#L102)

`_create_locked` 안을 순서대로 보면:
1. 어드미션 검사 (force=False 일 때만): [session_manager.py:114](backend/app/services/session_manager.py#L114)~[session_manager.py:116](backend/app/services/session_manager.py#L116)
2. 컨테이너 spawn (`to_thread`): [session_manager.py:145](backend/app/services/session_manager.py#L145)
3. `Session` 레코드 조립: [session_manager.py:178](backend/app/services/session_manager.py#L178)
4. SQLite insert (`to_thread`): [session_manager.py:196](backend/app/services/session_manager.py#L196)

**왜 이 셋을 한 락 안에 묶었는지**는 8.4 에서 자세히 다룹니다. 지금은 "검사 → spawn → 기록이 도중에 끼어들기 없이 통째로 실행된다"만 기억하세요. (Spring 의 `@Transactional` 로 감싼 임계 구역을 떠올리시면 느낌이 비슷합니다. 정확히 같진 않지만요.)

> 한 줄 요약: 컨트롤러는 위임만, 실제 생명주기는 `SessionManager` 가 락 안에서 "검사→spawn→기록" 순으로 조율합니다.

---

## 8.2 `DockerManager` — `docker run` 옵션 조립 공장

### 왜 필요한가
`docker run` 명령 하나에는 GPU 노출, 훅 라이브러리 마운트, 환경변수, 포트 매핑 등 옵션이 잔뜩 붙습니다. 이 조립을 여기저기서 하면 실수가 나므로 **한 곳(`DockerManager.create_container`)에 몰아넣습니다.** 7장에서 본 "docker SDK 는 여기서만 부른다" 규칙의 실체예요.

### Spring 비유
외부 시스템(도커 데몬)과 통신하는 **어댑터/게이트웨이** 클래스입니다. 비즈니스 로직은 없고, "도커가 알아듣는 형식으로 번역"만 합니다.

### 조립되는 옵션들
`create_container()` 가 만드는 `docker run` 은 대략 이런 명령과 같습니다:

```bash
docker run -d --name fgpu-<id> \
  --gpus all \                                  # 또는 device=N
  -e FGPU_RATIO=0.4 \                           # 훅에게 쿼터 전달
  -e LD_PRELOAD=/opt/fgpu/libfgpu.so \          # 훅 주입
  -v /host/build/libfgpu.so:/opt/fgpu/libfgpu.so:ro \   # 훅 마운트
  <image> <command>
```

코드에서 각 조각의 위치:
- 환경변수(`FGPU_RATIO`, `LD_PRELOAD`) 조립: [docker_manager.py:80](backend/app/services/docker_manager.py#L80)~[docker_manager.py:83](backend/app/services/docker_manager.py#L83)
- 절대 쿼터(`FGPU_QUOTA_BYTES`)와 throttle(`FGPU_COMPUTE_RATIO`) 옵션: [docker_manager.py:84](backend/app/services/docker_manager.py#L84)~[docker_manager.py:89](backend/app/services/docker_manager.py#L89)
- **환경변수 화이트리스트 전달**: [docker_manager.py:94](backend/app/services/docker_manager.py#L94)~[docker_manager.py:97](backend/app/services/docker_manager.py#L97)
- `--gpus all` vs `--gpus device=N` (`gpu_index`): [docker_manager.py:102](backend/app/services/docker_manager.py#L102)~[docker_manager.py:108](backend/app/services/docker_manager.py#L108)
- 훅 `.so` read-only bind mount: [docker_manager.py:111](backend/app/services/docker_manager.py#L111)~[docker_manager.py:116](backend/app/services/docker_manager.py#L116)
- 실제 실행 `client.containers.run(**run_kwargs)`: [docker_manager.py:138](backend/app/services/docker_manager.py#L138)

### 환경변수 화이트리스트 — 왜 통째로 안 넘기나
백엔드 프로세스의 환경변수를 컨테이너에 통째로 넘기면, DB 비밀번호나 API 토큰 같은 게 실수로 새어나갈 수 있습니다. 그래서 **명시적으로 허용한 키만** 전달합니다:

```python
_PASSTHROUGH_ENV = ("FGPU_LAUNCH_LOG_EVERY", "FGPU_WINDOW_MS")
```
- [docker_manager.py:32](backend/app/services/docker_manager.py#L32)

새 키를 넘기고 싶으면 이 튜플에 **의도적으로** 추가해야 합니다. "아무 env 나 forward 하지 않는다"가 규칙이에요.

### Jupyter 모드 (Stage 10)
`mode="jupyter"` 이면 사용자 command 를 무시하고 `jupyter lab` 명령으로 강제하며, 컨테이너의 8888 포트를 호스트 임의 포트로 publish 하고, 워크스페이스 디렉토리를 `/workspace` 로 마운트합니다.
- jupyter 명령 조립: [docker_manager.py:38](backend/app/services/docker_manager.py#L38)
- 워크스페이스 마운트: [docker_manager.py:119](backend/app/services/docker_manager.py#L119)~[docker_manager.py:123](backend/app/services/docker_manager.py#L123)
- 할당된 호스트 포트 읽기(`get_host_port`): [docker_manager.py:146](backend/app/services/docker_manager.py#L146)
- 매니저 쪽에서 백오프로 포트 폴링: [session_manager.py:162](backend/app/services/session_manager.py#L162)~[session_manager.py:176](backend/app/services/session_manager.py#L176)

> 도커가 포트를 즉시 attrs 에 안 올릴 수 있어서 `0.0, 0.1, 0.2, 0.4, 0.8` 초로 점점 늘리며 재시도하는(exponential backoff) 부분이 재밌습니다.

### 흔한 함정
- 훅 `.so` 는 **호스트에서 빌드된 파일을 마운트**합니다. `scripts/build_hook.sh` 로 `build/libfgpu.so` 가 없으면 컨테이너는 뜨긴 뜨는데 `LD_PRELOAD` 대상이 없어 훅이 안 걸립니다. `main.py` 가 부팅 때 경고를 찍습니다 ([main.py:42](backend/app/main.py#L42)).
- `remove=False` ([docker_manager.py:130](backend/app/services/docker_manager.py#L130)) 라 컨테이너가 종료돼도 자동 삭제되지 않습니다. 종료 후에도 로그를 조회하려는 의도예요. 대신 `DELETE` 를 호출하기 전까지 컨테이너 껍데기가 남습니다.

> 한 줄 요약: `DockerManager` 는 `docker run` 옵션을 한 곳에서 조립하는 어댑터. env 는 화이트리스트로만 넘기고, docker SDK 는 오직 여기서만 부릅니다.

---

## 8.3 `SessionStore` — SQLite 리포지토리와 영속성

### 왜 필요한가
백엔드를 재시작하면 세션 정보가 다 날아가면 곤란합니다. 컨테이너는 도커 데몬이 계속 들고 있는데 백엔드만 "얘가 뭐였는지" 잊어버리면 관리가 불가능하죠. 그래서 세션 레코드를 SQLite 파일(`data/sessions.db`)에 저장합니다.

### Spring 비유
`SessionStore` 는 `JpaRepository` 에 해당합니다. 다만 ORM 없이 stdlib `sqlite3` 로 CRUD 를 직접 짰습니다(새 의존성 추가를 극도로 아끼는 프로젝트 방침). **인터페이스가 곧 추상화 경계**라서, 나중에 이 클래스만 Redis/Postgres 구현으로 갈아끼우면 `SessionManager` 는 안 바뀝니다.

### 왜 매 호출마다 새 커넥션인가 (제일 중요)
```python
def _conn(self):
    return sqlite3.connect(self.db_path, isolation_level=None, timeout=5.0)
```
- [session_store.py:84](backend/app/services/session_store.py#L84)~[session_store.py:87](backend/app/services/session_store.py#L87)

이유는 7장의 `to_thread` 와 직결됩니다. `SessionManager` 가 모든 store 호출을 `asyncio.to_thread` 로 감싸는데, **`to_thread` 는 스레드 풀의 아무 워커에서나 실행**됩니다. 그런데 **`sqlite3.Connection` 은 스레드 안전이 보장되지 않습니다.** 커넥션 하나를 여러 스레드가 공유하면 깨질 수 있어요.

해결책은 단순합니다. **커넥션을 공유하지 않는다** — 매 호출마다 새로 열고 쓰고 닫습니다. 어느 워커 스레드에서 돌든 각자 자기 커넥션만 만지므로 안전합니다.

- `contextlib.closing` 으로 반드시 닫기: [session_store.py:113](backend/app/services/session_store.py#L113) (그리고 모든 CRUD 메서드에서 동일 패턴)
- `isolation_level=None` = autocommit. 단일 statement 만 실행하므로 명시적 트랜잭션이 불필요: [session_store.py:85](backend/app/services/session_store.py#L85)~[session_store.py:87](backend/app/services/session_store.py#L87)

### CRUD
| 메서드 | SQL | 코드 |
|---|---|---|
| `insert` | INSERT | [session_store.py:112](backend/app/services/session_store.py#L112) |
| `get` | SELECT ... WHERE id | [session_store.py:139](backend/app/services/session_store.py#L139) |
| `list_all` | SELECT ... ORDER BY created_at DESC | [session_store.py:146](backend/app/services/session_store.py#L146) |
| `update_status` | UPDATE status, exit_code | [session_store.py:153](backend/app/services/session_store.py#L153) |
| `delete` | DELETE WHERE id | [session_store.py:162](backend/app/services/session_store.py#L162) |

### 마이그레이션 (마이그레이션 프레임워크 없이)
이 프로젝트는 Flyway/Liquibase 같은 도구를 안 씁니다. 대신 새 컬럼은 `_MIGRATIONS` 리스트에 `ALTER TABLE ... ADD COLUMN` 으로 추가하고, `PRAGMA table_info` 로 **이미 있으면 건너뛰어** 멱등(idempotent)하게 만듭니다.
- 마이그레이션 목록: [session_store.py:55](backend/app/services/session_store.py#L55)~[session_store.py:63](backend/app/services/session_store.py#L63)
- 멱등 적용 로직: [session_store.py:79](backend/app/services/session_store.py#L79)~[session_store.py:82](backend/app/services/session_store.py#L82)

컬럼 삭제나 타입 변경은 SQLite 가 까다로우니, 개발 중이라면 `rm -rf data/` 로 통째로 밀어버리는 게 권장 방식입니다.

### 재시작 후에도 살아있는 이유 = 영속성
백엔드를 껐다 켜도 SQLite 파일은 그대로 남아있으니 레코드가 복원됩니다. 다만 "status" 는 재시작 동안 바뀌었을 수 있어요(그 사이 컨테이너가 종료됐을 수도). 그래서 조회할 때 도커 데몬과 다시 맞춥니다 — 이게 다음 절의 reconcile 입니다.

> 한 줄 요약: 매 호출 새 커넥션(스레드 안전) + `data/sessions.db` 영속화 덕에 재시작해도 세션이 살아남습니다. `SessionStore` 는 갈아끼우기 쉬운 리포지토리 경계입니다.

---

## 8.4 상태 reconcile — SQLite 는 캐시, 도커 데몬이 진짜

### 왜 필요한가
SQLite 에 `status="running"` 이라고 적혀 있어도, 그 컨테이너는 이미 종료됐을 수 있습니다(작업이 끝났거나, 재시작 사이에 죽었거나). **진짜 상태는 도커 데몬이 압니다.** 그래서 세션을 읽을 때마다 도커에게 "얘 지금 어때?"를 물어보고, 달라졌으면 SQLite 를 갱신합니다. 이걸 reconcile(조정/동기화)이라고 합니다.

### 실제 코드 — `get()`
```python
async def get(self, sid):
    rec = await asyncio.to_thread(self.store.get, sid)      # SQLite 에서 읽고
    ...
    status, exit_code = await asyncio.to_thread(            # 도커에게 실제 상태 물어봄
        self.docker.get_status, rec.container_id
    )
    if rec.status != status or rec.exit_code != exit_code:  # 달라졌으면
        await asyncio.to_thread(self.store.update_status, ...)  # SQLite 갱신 (write-back)
```
- `get()` 전체: [session_manager.py:200](backend/app/services/session_manager.py#L200)~[session_manager.py:224](backend/app/services/session_manager.py#L224)
- 컨테이너가 데몬에서 사라진 경우(`NotFound`)는 레코드를 지우지 않고 status 만 `"removed"` 로: [session_manager.py:209](backend/app/services/session_manager.py#L209)~[session_manager.py:216](backend/app/services/session_manager.py#L216)

`list_all()` 은 이 `get()` 을 모든 세션에 대해 `asyncio.gather` 로 **동시에** 돌립니다 ([session_manager.py:226](backend/app/services/session_manager.py#L226)~[session_manager.py:232](backend/app/services/session_manager.py#L232)). 세션이 10개면 10번 순차로 도커에 묻는 대신 한꺼번에 물어서 응답이 빨라집니다.

### 흔한 함정
- 도커 데몬 자체가 죽어 있으면 `get_status` 가 예외를 던지고, 그게 500 으로 전파됩니다. (문서화된 알려진 한계입니다.)
- 누가 백엔드 밖에서 `docker rm -f` 로 컨테이너를 지우면, 다음 조회 때 `"removed"` 로 표시되지만 레코드 자체는 남습니다.

> 한 줄 요약: SQLite 는 캐시, 진실은 도커 데몬. 읽을 때마다 reconcile 해서 상태를 맞추고, 바뀐 건 write-back 합니다.

---

## 8.5 (핵심) 3계층 자원 집행 모델

이 프로젝트에서 GPU 자원 초과를 막는 장치는 **하나가 아니라 세 층**입니다. 각 층이 잡는 실패의 종류가 다릅니다. 이 구분을 정확히 이해하는 게 이 장의 하이라이트입니다.

### 세 층의 정체

**Layer A — 어드미션 (spawn 시점, 스케줄러 층)**
컨테이너를 띄우기 *전에*, "지금 만들면 같은 GPU 위 세션들의 ratio 합이 1.0 을 넘나?"를 검사합니다. 넘으면 아예 안 띄우고 **HTTP 409** 를 돌려줍니다. Backend.AI 의 클러스터 스케줄러가 하는 일을 단일 호스트 버전으로 옮긴 겁니다.
- 정책 순수 함수: `admission.check()` [admission.py:92](backend/app/services/admission.py#L92)

**Layer B — 훅 (런타임, per-container)**
컨테이너 *안에서* 실행 중인 프로그램이 `cudaMalloc` 을 부를 때마다, `LD_PRELOAD` 로 주입된 `libfgpu.so` 가 가로채서 "이 컨테이너의 누적 사용량이 자기 쿼터(`FGPU_RATIO`)를 넘나?"를 검사합니다. 넘으면 `cudaErrorMemoryAllocation` 을 돌려줍니다. (앞 챕터들에서 배운 그 훅입니다.)

**Layer C — 드라이버 (물리 OOM, NVIDIA 드라이버)**
어드미션과 훅을 다 통과해도, 여러 컨테이너가 실제 할당을 물리 한계까지 밀어붙이면 NVIDIA 드라이버가 진짜 OOM 을 반환합니다. CUDA 컨텍스트 오버헤드(프로세스당 ~700MiB) 같은 건 위 두 층이 계산에 넣지 않기 때문에, 최후의 방어선이 여기입니다.

### 각 층이 잡는 실패 — 표로 구분

| | Layer A 어드미션 | Layer B 훅 | Layer C 드라이버 |
|---|---|---|---|
| **시점** | 컨테이너 spawn 전 | 컨테이너 안, `cudaMalloc` 순간 | 물리 메모리 실제 할당 순간 |
| **위치** | 백엔드 (`admission.py`) | 컨테이너 내부 `libfgpu.so` | NVIDIA 드라이버 |
| **잡는 실패** | *예약 단계의* 과할당 (합 > 1.0) | *한 컨테이너의* 쿼터 초과 | 컨텍스트 오버헤드 포함 진짜 물리 초과 |
| **실패 신호** | HTTP 409 | `cudaErrorMemoryAllocation` | `cudaErrorMemoryAllocation` |
| **우회** | `force=true` | 협조적 위협 모델(정적 링크 바이너리는 우회 가능) | 없음(하드웨어) |
| **범위** | 여러 컨테이너 총합 | 컨테이너 하나 | GPU 물리 전체 |

**핵심 통찰**: 세 층은 서로를 대체하지 않고 **보완**합니다.
- 어드미션만 있으면? 한 컨테이너가 자기 ratio 안에서도 실수로 과할당하는 걸 못 막습니다 → 훅이 필요.
- 훅만 있으면? 각 컨테이너는 자기 쿼터만 볼 뿐, 서로 합쳐서 1.0 을 넘는 걸 모릅니다(훅은 per-container) → 어드미션이 필요.
- 둘 다 있어도? 컨텍스트 오버헤드 때문에 물리 한계를 넘을 수 있습니다 → 드라이버가 최후 방어.

이게 캡스톤 논문의 그림 하나입니다: "요청 흐름 다이어그램 + 각 층이 잡는 실패 종류 표".

> 한 줄 요약: 어드미션(예약 총합) → 훅(컨테이너별 런타임) → 드라이버(물리 한계). 세 층이 서로 다른 실패를 잡는 상호보완 방어선입니다.

---

## 8.6 어드미션 세부 — `gpu_overlaps` 규칙과 원자성

### `gpu_overlaps` — "같은 GPU 를 다투는가"
어드미션은 "같은 GPU 위 세션들"의 ratio 합만 봅니다. 그럼 어떤 세션들이 "같은 GPU"일까요?

```python
def gpu_overlaps(a, b):
    if a is None or b is None:   # None = "모든 GPU" 이므로 무엇과도 겹침
        return True
    return a == b                # 같은 device 번호면 겹침
```
- [admission.py:62](backend/app/services/admission.py#L62)~[admission.py:69](backend/app/services/admission.py#L69)

규칙 정리:
- `None`(모든 GPU) vs `N` → **겹침** (None 세션은 모든 device 를 물고 있으니까)
- `None` vs `None` → 겹침
- `N` vs `N` → 겹침 (같은 device)
- `N` vs `M` (N≠M) → **분리** (다른 device, 서로 격리)

단일 GPU 호스트(RTX 4070)에서는 모든 세션이 항상 겹치므로, 정책은 단순히 `sum(active_ratios) ≤ 1.0` 으로 환원됩니다.

관련 함수:
- 활성 세션 ratio 합산 (`created`/`running` 만 계산, `exited`/`removed` 제외): `sum_used_ratio()` [admission.py:72](backend/app/services/admission.py#L72)
- 부동소수점 오차 허용 (`0.3+0.3+0.4=1.0000000000000002` 같은 경우): `_TOL = 1e-9` [admission.py:33](backend/app/services/admission.py#L33), 검사식 [admission.py:99](backend/app/services/admission.py#L99)
- UI capacity 표시용 스냅샷 (`GET /sessions/admission`): `usage_snapshot()` [admission.py:108](backend/app/services/admission.py#L108)

`admission.py` 는 순수 함수 모듈입니다 — docker 도 DB 도 안 만집니다. 호출자가 세션 목록을 넘겨줘요. 덕분에 GPU 없이 pytest 로 다 테스트됩니다(`backend/tests/test_admission.py`).

### 왜 `asyncio.Lock` 으로 감싸야 하나 (원자성)
검사(`check`)와 기록(`insert`) 사이가 원자적이지 않으면 이런 사고가 납니다:

```
시각  요청 A (ratio=0.6)        요청 B (ratio=0.6)
─────────────────────────────────────────────────
t0    check() → 현재 0.0, OK
t1                              check() → 현재 0.0, OK   ← A 가 아직 insert 안 함!
t2    insert (0.6)
t3                              insert (0.6)             ← 합 1.2, 초과했는데 통과!
```

두 요청이 서로의 insert 를 보기 전에 각자 검사를 통과해버립니다. **check-then-insert 사이의 틈(race window)** 때문이에요. `asyncio.Lock` 으로 `create()` 전체를 직렬화하면, A 가 검사→기록을 끝내고 락을 놓기 전까지 B 는 검사조차 시작 못 합니다. 그래서 B 는 A 의 0.6 을 반영한 상태에서 검사하고 → 409 로 거부됩니다.

- 락으로 감싸는 부분: [session_manager.py:94](backend/app/services/session_manager.py#L94)~[session_manager.py:100](backend/app/services/session_manager.py#L100)

Spring 비유로는 "DB 유니크 제약 + 트랜잭션"으로 중복 삽입을 막는 것과 목적이 같습니다. 여기선 단일 프로세스 이벤트 루프라 `asyncio.Lock` 하나로 충분해요.

> **주의**: 락은 `create()` 만 직렬화합니다. `docker.run` 자체는 여전히 `to_thread` 라 이벤트 루프는 안 막힙니다 — 다만 두 create 가 동시에 진행되진 않게 순서를 강제할 뿐입니다.

### 409 응답과 force 우회
검사 실패 시 `AdmissionDenied` 가 던져지고, 컨트롤러가 이를 구조화된 409 JSON 으로 번역합니다(요청 ratio, 현재 사용량, 잔여, GPU, 활성 세션 수 포함).
- 예외 정의: [admission.py:36](backend/app/services/admission.py#L36)
- 409 번역: [sessions.py:78](backend/app/api/sessions.py#L78)~[sessions.py:91](backend/app/api/sessions.py#L91)
- `force=true` 면 검사를 아예 건너뜁니다: [session_manager.py:114](backend/app/services/session_manager.py#L114) (oversubscription 데모용)

> 한 줄 요약: `gpu_overlaps` 로 "같은 GPU 세션"을 골라 ratio 합을 검사하고, `asyncio.Lock` 으로 check-then-insert 를 원자화해 동시 요청의 과할당을 막습니다.

---

## 8.7 설계 철학 — 백엔드와 훅은 "환경변수로만" 대화한다

마지막으로 이 시스템의 중요한 설계 원칙 하나를 짚고 갑니다.

**백엔드는 훅에게 쿼터를 오직 환경변수로만 전달합니다.** `create_container` 가 `FGPU_RATIO`(또는 `FGPU_QUOTA_BYTES`)를 컨테이너 env 로 넣고([docker_manager.py:80](backend/app/services/docker_manager.py#L80)~[docker_manager.py:89](backend/app/services/docker_manager.py#L89)), 컨테이너 안에서 `LD_PRELOAD` 로 뜬 `libfgpu.so` 가 그 env 를 읽어 자기 쿼터를 정합니다. 그게 전부예요.

- 백엔드는 GPU 를 만지지 않습니다 — 도커 소켓만 씁니다.
- 훅은 백엔드를 모릅니다 — 그냥 자기 프로세스의 env 만 봅니다.
- 둘 사이엔 공유 메모리도, RPC 도, 콜백도 없습니다. **환경변수라는 단방향, 무상태 인터페이스** 하나뿐입니다.

이 느슨한 결합 덕분에:
- 훅을 C 로 짜든 Rust 로 다시 짜든, env 계약(`FGPU_RATIO` 등)만 지키면 백엔드는 안 바뀝니다.
- 백엔드 없이 `docker run -e FGPU_RATIO=0.4 -e LD_PRELOAD=...` 로 손으로 띄워도 훅은 똑같이 동작합니다(앞 챕터 스크립트들이 그렇게 합니다).
- 테스트가 쉽습니다 — 백엔드는 "env 를 올바로 조립하는가"만 검증하면 되고, 훅은 "env 를 올바로 읽는가"만 검증하면 됩니다.

이게 7장에서 본 계층 분리 철학의 연장선입니다. 각 조각이 자기 책임만 알고, 최소한의 계약으로만 대화합니다.

> 한 줄 요약: 백엔드↔훅은 환경변수라는 단방향·무상태 계약 하나로만 연결됩니다. 서로를 몰라도 되도록 일부러 느슨하게 결합했습니다.

---

## ✍️ 스스로 점검

1. `POST /sessions` 요청 하나가 처리되는 동안 `to_thread` 로 별도 스레드에 넘겨지는 호출을 세 개 이상 짚어보세요. 왜 그렇게 해야 하나요?
2. `ratio=0.6` 요청 두 개가 동시에 들어왔을 때, `asyncio.Lock` 이 없다면 어떤 순서로 어떤 사고가 나나요? 락이 있으면 결과가 어떻게 달라지나요?
3. 어드미션(Layer A)과 훅(Layer B)은 각각 어떤 종류의 과할당을 잡나요? 하나만 있으면 못 막는 상황을 각각 하나씩 드세요.

## 🎯 다음 챕터

이 두 챕터로 백엔드의 뼈대(FastAPI 계층 + 비동기 모델)와 살(세션 생명주기 + 3계층 집행)을 모두 훑었습니다. 다음 챕터에서는 이 백엔드를 실제로 운용하는 부분 — `scripts/run_backend.sh` 로 띄우고 `scripts/smoke_test_api.sh` 로 검증하고, `scripts/eval/*.sh` 로 GPU 호스트에서 end-to-end 실험을 돌리는 흐름 — 을 다룹니다.

---

⟵ [이전: 7장. FastAPI 백엔드](07-backend-fastapi.md) ・ [📚 전체 목차](README.md) ・ [다음: 9장. 빌드·실행·검증](09-build-run-verify.md) ⟶
