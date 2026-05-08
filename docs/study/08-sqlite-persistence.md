# Chapter 08 — SQLite 영속성

## 학습 목표

- 왜 *추가 의존성 없이* stdlib `sqlite3` 만으로 충분한지 안다.
- "connection per call" 패턴을 사용하는 이유를 안다.
- `isolation_level=None` (autocommit) 의 의미와 우리 시나리오에 맞는 이유를 안다.
- SQLite 의 한계 (single-host, write contention) 와 어디까지 가능한지 안다.

---

## 8.1 왜 SQLite 인가?

세 후보 비교:

| 옵션 | 설치 부담 | 동시성 | 영속성 | 확장성 |
|---|---|---|---|---|
| in-memory dict | 없음 | OK | **없음** (재시작 시 소실) | 단일 프로세스만 |
| **SQLite (stdlib)** | **없음** | 단일 writer (충분) | **있음** | 단일 호스트만 |
| Postgres / MySQL | 별도 서비스 | 좋음 | 있음 | 멀티 호스트 |

본 프로토타입의 트레이드오프:
- **단일 호스트** 시나리오 (캡스톤 / 논문 PoC) → SQLite 충분.
- **0 의존성** 이 가치 — 새 사용자가 `pip install` 할 게 없음.
- 인터페이스만 잘 분리해두면 [Stage 9 full](../../CLAUDE.md) 에서 Postgres/Redis 로 *교체* 가능 (SessionStore 인터페이스만 만족하면 SessionManager 그대로).

### SQLite 가 *진짜 작은가?*

- SQLite 는 라이브러리 (≠ 서버). 같은 프로세스 안에서 .db 파일을 직접 읽고 씀.
- 이 .db 파일이 **세계에서 가장 많이 배포된 데이터베이스 엔진**. 안드로이드/iOS 안에 다 들어 있음, 비행기 블랙박스에도.
- ACID 트랜잭션 완전 지원. WAL (Write-Ahead Log) 로 동시 read 도 OK.

### 더 공부하려면
- [SQLite 공식 — Appropriate Uses For SQLite](https://www.sqlite.org/whentouse.html)
- [SQLite 공식 — How SQLite Is Tested](https://www.sqlite.org/testing.html) — 테스트 라인이 코드보다 100배 많음

---

## 8.2 우리 store 의 모양

[backend/app/services/session_store.py](../../backend/app/services/session_store.py) 의 핵심 패턴:

```python
class SessionStore:
    def __init__(self, path: str):
        self.path = path
        self._init_schema()                 # CREATE TABLE IF NOT EXISTS

    def _connect(self):
        return sqlite3.connect(
            self.path,
            isolation_level=None,           # autocommit
            detect_types=sqlite3.PARSE_DECLTYPES,
        )

    def insert(self, rec: Session):
        with closing(self._connect()) as conn:
            conn.execute(
                "INSERT INTO sessions(id, container_id, ratio, ...) "
                "VALUES (?, ?, ?, ...)",
                (rec.id, rec.container_id, rec.ratio, ...),
            )

    def get(self, sid: str) -> Optional[Session]:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE id = ?", (sid,)
            ).fetchone()
            return _row_to_session(row) if row else None
```

세 가지 디자인 결정:

### (1) Connection per call

`Connection` 객체는 thread-safe 가 *기본적으로 보장 안 됨*. [Chapter 07](07-async-io.md) 에서 본 `to_thread` 가 임의 워커에서 도므로, `__init__` 에서 만든 connection 을 공유하면 위험.

대안: 매 메서드 호출마다 새 connection 만들기. SQLite 는 파일 열기/닫기가 매우 빠름 (수 μs) 이라 이 비용은 무시 가능.

### (2) `with closing(...)`

`with sqlite3.connect(...) as conn:` 만 쓰면 *transaction* 만 관리하고 connection 은 GC 때까지 안 닫힘. 우리는 명시적으로 `closing` 으로 감싸 connection 도 즉시 닫음.

```python
from contextlib import closing
with closing(sqlite3.connect(path)) as conn:
    ...
# 여기서 conn.close() 보장
```

### (3) `isolation_level=None` (autocommit)

SQLite Python binding 의 기본은 *암묵 트랜잭션* — `BEGIN` 을 자동으로 깔고 `commit()` 부를 때까지 묶음. `None` 으로 두면 **각 statement 가 독립 트랜잭션**.

우리는 단일 statement 만 실행하므로 명시 트랜잭션 불필요. `None` 이 더 단순.

### 더 공부하려면
- [Python sqlite3 공식](https://docs.python.org/3/library/sqlite3.html)
- [sqlite3 — Transaction control](https://docs.python.org/3/library/sqlite3.html#transaction-control)

---

## 8.3 스키마 evolution — 단순한 ALTER TABLE

[session_store.py](../../backend/app/services/session_store.py) 의 `_init_schema` 는 보통 다음과 같은 모양:

```python
def _init_schema(self):
    with closing(self._connect()) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                container_id TEXT,
                ratio REAL,
                ...
                created_at TIMESTAMP
            )
        """)
        # idempotent migration — 컬럼이 없을 때만 추가
        cols = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
        if "gpu_index" not in cols:
            conn.execute("ALTER TABLE sessions ADD COLUMN gpu_index INTEGER")
        if "host_port" not in cols:
            conn.execute("ALTER TABLE sessions ADD COLUMN host_port INTEGER")
        # ... 등 ...
```

핵심: **idempotent**. 여러 번 실행해도 결과 동일. `IF NOT EXISTS` + `PRAGMA table_info` 점검 + 조건부 ALTER.

> **프로덕션이라면**: alembic 같은 본격 마이그레이션 도구를 쓰세요. 본 프로토타입 범위에선 ALTER + 게이트가 충분하고, 호환 깨질 만큼 변경되면 README 에 "rm -rf data/" 로 안내 (CLAUDE.md 참고).

### 더 공부하려면
- [SQLite — ALTER TABLE](https://www.sqlite.org/lang_altertable.html)
- [Alembic 공식](https://alembic.sqlalchemy.org/) — 본격 마이그레이션이 필요할 때

---

## 8.4 datetime 보존 — `detect_types`

```python
sqlite3.connect(path, detect_types=sqlite3.PARSE_DECLTYPES)
```

`PARSE_DECLTYPES` 옵션은 컬럼 선언 타입(`TIMESTAMP`)을 보고 자동으로 Python `datetime` 으로 변환. 없으면 문자열로 저장 후 읽을 때 수동 파싱 필요.

`backend/tests/test_session_store.py` 가 datetime round-trip 보존을 검증합니다 ([CLAUDE.md](../../CLAUDE.md) 의 테스트 설명 참조).

### 함정 — timezone

기본 `TIMESTAMP` 어댑터는 *naive datetime* 을 가정합니다. timezone-aware (UTC 등) 를 그대로 저장하려면 어댑터 등록이 필요해요.

[session_manager.py:183](../../backend/app/services/session_manager.py#L183) 가 `datetime.now(timezone.utc)` 로 만드는데, 호환성을 위해 store 에서 변환을 명시적으로 다루는 게 안전. 자세한 처리는 코드 직접 참조.

---

## 8.5 SQLite 의 한계와 우리 영역

| 한계 | 본 프로토타입에서 문제? |
|---|---|
| **단일 writer** (write 시 DB 전체 락) | 작은 트래픽이라 무관 |
| **네트워크 마운트(NFS) 위험** | 로컬 디스크 사용 가정 — 무관 |
| **멀티 호스트 공유 불가** | 본 프로토타입은 single-host — 무관 |
| **64-bit 정수 / 1GB row 한계** | 우리 record 는 KB 단위 — 무관 |

→ Stage 9 full 에서 멀티 호스트로 갈 때만 문제. 그때 Postgres / Redis 로 교체.

### WAL 모드는 켜야 하나?

기본은 `journal` 모드. WAL (`PRAGMA journal_mode=WAL`) 은 동시 read 성능을 크게 올려주고, 우리 `list_all` 같은 read-heavy 패턴에 유리. 다만 WAL 에는 별도 파일(`-wal`, `-shm`) 이 생기고 NFS 호환성이 약해지는 등 trade-off 존재.

본 프로토타입 트래픽 수준에선 차이 미미해 *기본* 모드 유지. 필요해지면 한 줄로 켤 수 있음.

### 더 공부하려면
- [SQLite — WAL mode](https://www.sqlite.org/wal.html)

---

## 8.6 직접 해보기 — DB 직접 들여다보기

```bash
./scripts/run_backend.sh
# 다른 터미널:
curl -X POST http://localhost:8000/sessions \
    -H 'Content-Type: application/json' \
    -d '{"ratio":0.3}'

# DB 파일 직접 열어보기
sqlite3 data/sessions.db
sqlite> .schema sessions
sqlite> SELECT id, ratio, status, gpu_index FROM sessions;
sqlite> .exit
```

백엔드를 Ctrl+C 로 죽인 뒤 다시 띄우고:
```bash
curl http://localhost:8000/sessions
# 같은 record 가 그대로 — Stage 8 의 가치
```

### 단위 테스트

[backend/tests/test_session_store.py](../../backend/tests/test_session_store.py) 가 docker / GPU 없이 store 를 단독 검증합니다:

```bash
cd backend && pip install -e ".[dev]" && pytest
```

이 분리가 가능한 이유는 store 가 *순수 영속성 레이어* 라 docker 와 무관해서예요. 좋은 설계의 부산물.

---

## 자가점검 질문

1. SQLite Python binding 에서 `Connection` 을 여러 스레드가 공유할 때 안전한가?
2. `isolation_level=None` 의 효과는?
3. `with sqlite3.connect(...) as conn:` 만 쓰면 connection 이 *언제* 닫히는가?
4. SQLite 가 PostgreSQL 로 교체될 때 `SessionManager` 의 코드 변경이 필요한가? (정답: 인터페이스만 같으면 0 변경 — 그게 분리의 가치)
5. 백엔드 재시작 후 `GET /sessions/<id>` 가 같은 record 를 돌려주려면 *최소* 어떤 두 가지가 충족돼야 하나?

→ [Chapter 09: Bearer 인증](09-auth.md)

---

## 외부 자료 종합

- 📚 [Python sqlite3 docs](https://docs.python.org/3/library/sqlite3.html)
- 📚 [SQLite 공식 사이트](https://www.sqlite.org/)
- 📖 *Use The Index, Luke!* — SQL 인덱스 본질에 대한 명저. [무료 온라인](https://use-the-index-luke.com/)
- 🎥 [Richard Hipp — How SQLite Is Tested](https://www.youtube.com/results?search_query=richard+hipp+sqlite+testing)
