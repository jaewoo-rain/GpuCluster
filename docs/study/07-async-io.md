# Chapter 07 — asyncio.to_thread 와 동시성

## 학습 목표

- "이벤트 루프를 막는다(blocking)" 의 실제 의미를 안다.
- `asyncio.to_thread` 가 *언제* 필요하고 *언제* 무의미한지 안다.
- 우리 프로젝트의 모든 docker SDK / sqlite3 호출이 `to_thread` 로 감싸진 이유를 안다.
- `asyncio.gather` 로 병렬화한 list reconcile 의 효과를 안다.

---

## 7.1 동기 vs 비동기 — 한 그림

```
[동기 (sync)]
  요청1 ──── 처리(2초) ──── 응답
                           요청2 ──── 처리(2초) ──── 응답
                                                  요청3 ──── ...
  ⇒ 처리량 = 1 / 2 = 0.5 req/s, 100 동시 요청 = 200초

[비동기 (async, 단일 스레드)]
  요청1 ─ I/O 대기  ┐
  요청2 ─ I/O 대기  ├─ 동시에 진행
  요청3 ─ I/O 대기  ┘
  (한 요청이 I/O 기다리는 동안 다른 요청 처리)
  ⇒ I/O 가 병목이면 한 스레드로도 100 동시 요청 가능
```

핵심: **async 가 마법으로 빨라지는 게 아니라, *I/O 대기 시간 동안* 다른 코루틴을 진행시킬 뿐**입니다.

CPU 가 바쁜 작업 (행렬 계산, 압축 등) 은 async 로 안 빨라져요 — 단일 스레드는 한 번에 한 일만 함.

### 더 공부하려면
- [Python — asyncio 공식 문서 — Coroutines](https://docs.python.org/3/library/asyncio-task.html)
- [Real Python — Async IO in Python](https://realpython.com/async-io-python/)

---

## 7.2 "블로킹(blocking)" 이 뭔가?

이벤트 루프를 막는 함수 = `await` 로 양보(yield) 하지 않고 *제어 흐름을 점유* 하는 함수.

| 호출 | 양보? | 막힘? |
|---|---|---|
| `await asyncio.sleep(1)` | 양보 | 안 막힘 (1초 동안 다른 코루틴 진행) |
| `time.sleep(1)` | 양보 X | **막힘** (1초간 이벤트 루프 정지) |
| `requests.get(url)` (sync HTTP) | 양보 X | **막힘** (네트워크 대기 동안 정지) |
| `await httpx_client.get(url)` (async HTTP) | 양보 | 안 막힘 |
| `docker.containers.run(...)` (docker SDK) | 양보 X | **막힘** |
| `sqlite3.connect(...).execute(...)` | 양보 X | **막힘** |

이벤트 루프가 막히는 동안 *모든 다른 요청* 도 응답을 못 받아요. healthz 같은 가벼운 endpoint 도 멈춤.

---

## 7.3 우리 문제 — docker SDK 와 sqlite3 는 sync only

[`docker-py`](https://docker-py.readthedocs.io/) 는 동기 라이브러리입니다. `containers.run(...)` 안에서 docker daemon 과 HTTP/socket 통신을 하는데 그동안 이벤트 루프가 멈춰요.

`sqlite3` 도 마찬가지. `INSERT` 한 줄도 sync.

만약 그대로 `async def create_session` 안에서 부르면:

```python
async def create(self, ratio):
    c = self.docker.create_container(...)   # 1~3초 동안 이벤트 루프 멈춤
    self.store.insert(rec)                  # 추가로 수십 ms 멈춤
    return rec
```

→ 두 사용자가 동시에 POST 하면 한 명이 끝날 때까지 다른 한 명은 응답을 못 받음. *동시성의 의미 자체가 사라짐*.

---

## 7.4 해결 — `asyncio.to_thread`

Python 3.9+ 의 표준 도구. sync 함수를 *별도 스레드에 던져* 거기서 실행하고, 본 코루틴은 *그 스레드의 결과를 await* 합니다.

[session_manager.py:142-153](../../backend/app/services/session_manager.py#L142-L153):

```python
c = await asyncio.to_thread(
    self.docker.create_container,
    name=name, ratio=ratio, command=cmd, ...
)
```

벌어지는 일:
1. `asyncio.to_thread` 가 `self.docker.create_container(...)` 호출을 thread pool 의 워커 스레드에 제출.
2. 본 코루틴은 그 결과를 *await* 함 → 이벤트 루프는 자유로워짐.
3. 다른 요청의 코루틴이 그 사이에 진행 가능.
4. docker daemon 응답이 오면 워커가 깨어나 결과를 반환 → await 가 풀림.

### 비유

> 사장이 손님 두 명에게 동시에 주문을 받음. 첫 주문은 *주방* (= 워커 스레드)에 넘기고, 사장은 두 번째 손님 주문을 받으러 감. 음식이 나오면 사장이 받아서 손님에게 전달.

이게 우리 프로젝트의 `to_thread` 패턴 그대로입니다.

### 더 공부하려면
- [Python — asyncio.to_thread](https://docs.python.org/3/library/asyncio-task.html#asyncio.to_thread)
- [Trio 의 비슷한 개념: trio.to_thread.run_sync](https://trio.readthedocs.io/en/stable/reference-core.html#putting-blocking-i-o-into-worker-threads) — 다른 라이브러리도 같은 패턴

---

## 7.5 SQLite + 스레드 — 함정

`sqlite3.Connection` 객체는 **스레드 안전을 보장하지 않습니다**. 한 connection 을 여러 스레드가 쓰면 깨질 수 있어요. `to_thread` 가 임의 워커에서 도므로 연결을 미리 만들어두면 위험합니다.

해결: **호출마다 새 connection** ([Chapter 08](08-sqlite-persistence.md) 참조).

[session_store.py](../../backend/app/services/session_store.py) 의 패턴:
```python
def insert(self, rec):
    with closing(sqlite3.connect(self.path)) as conn:
        conn.execute("INSERT INTO sessions ... VALUES (...)", (...))
        conn.commit()
```

연결 만드는 비용이 우려스러울 수 있지만, SQLite 는 **파일 열기/닫기가 매우 빠름** — 보통 마이크로초 단위. 우리 트래픽 수준에선 무시 가능.

---

## 7.6 `asyncio.gather` 로 reconcile 병렬화

[session_manager.py:221-227](../../backend/app/services/session_manager.py#L221-L227):

```python
async def list_all(self) -> list[Session]:
    recs = await asyncio.to_thread(self.store.list_all)
    results = await asyncio.gather(
        *(self.get(r.id) for r in recs), return_exceptions=False
    )
    return [r for r in results if r is not None]
```

`list_all` 은 N 개 세션 record 각각에 대해 docker daemon 에 status 를 묻습니다. 직렬로 하면 N × (round-trip latency). `gather` 로 *동시에* await 하면 ≈ 한 번의 latency 안에 끝남.

### `gather` vs `as_completed`

- `gather`: 모든 결과를 *기다려서* 한 번에 받음. 결과 순서 보장.
- `as_completed`: 끝나는 순서대로 결과를 yield. streaming 처리에 유용.

우리는 list 응답을 한 번에 만들어 반환하므로 `gather` 가 자연.

### 더 공부하려면
- [Python — asyncio.gather](https://docs.python.org/3/library/asyncio-task.html#asyncio.gather)

---

## 7.7 `asyncio.Lock` — admission 의 경쟁 방지

[session_manager.py:73](../../backend/app/services/session_manager.py#L73):

```python
self._create_lock = asyncio.Lock()
```

이건 `pthread_mutex_t` 와 다른 *코루틴* 락이에요. `await lock.acquire()` 로 잡고, 안에서는 다른 코루틴이 못 들어옴.

용도: admission 검사와 컨테이너 spawn 사이의 race ([Chapter 13](13-admission-control.md)). 검사 끝나고 spawn 사이에 다른 요청이 끼면 두 요청이 같은 capacity 로 둘 다 통과해버릴 수 있어요. → 검사+spawn 을 통째로 lock 안에 넣음.

```python
async def create(self, ratio, ...):
    async with self._create_lock:
        return await self._create_locked(...)
```

여전히 `_create_locked` 안의 `to_thread` 호출은 worker 로 넘어가므로 **이벤트 루프 자체는 안 막힘**. 직렬화되는 건 *create 흐름끼리* 만.

### 더 공부하려면
- [Python — asyncio.Lock](https://docs.python.org/3/library/asyncio-sync.html#asyncio.Lock)

---

## 7.8 직접 해보기 — 동시 POST 가 진짜 병렬인지

```bash
./scripts/run_backend.sh

# 다른 터미널에서 동시에 두 번 POST:
( time curl -s -X POST http://localhost:8000/sessions \
       -H 'Content-Type: application/json' \
       -d '{"ratio":0.3}' >/dev/null ) &
( time curl -s -X POST http://localhost:8000/sessions \
       -H 'Content-Type: application/json' \
       -d '{"ratio":0.3}' >/dev/null ) &
wait
```

- `to_thread` 적용된 현재 코드: 둘 다 거의 *동시에* 끝남 (각각 ~1-2초).
- 만약 `to_thread` 없이 sync 라면: 둘이 합쳐 ~3-4초 (직렬).

실제로 측정해보면 비동기화의 효과를 체감할 수 있어요.

---

## 7.9 함정 정리

| 함정 | 이유 | 회피 |
|---|---|---|
| `time.sleep(N)` 을 `async` 함수 안에서 부름 | 이벤트 루프 멈춤 | `await asyncio.sleep(N)` |
| `requests.get(url)` 을 `async` 함수에서 부름 | 동기 HTTP, 막힘 | `httpx.AsyncClient` 또는 `await asyncio.to_thread(requests.get, url)` |
| 한 sqlite3 connection 을 여러 to_thread 호출에서 공유 | thread-unsafe | 호출마다 새 connection |
| CPU heavy 작업을 `to_thread` 로 던짐 | thread pool 포화 + GIL 때문에 별로 안 빨라짐 | `concurrent.futures.ProcessPoolExecutor` 또는 외부 워커 |
| `asyncio.Lock` 과 `threading.Lock` 혼용 | 다른 도구. 코루틴 vs 스레드 | 상황에 맞게 한 쪽만 |

---

## 자가점검 질문

1. `await asyncio.sleep(1)` 와 `time.sleep(1)` 의 차이를 이벤트 루프 관점에서 설명하라.
2. `asyncio.to_thread` 가 *해결하지 못하는* 종류의 병목 한 가지를 말하라. (정답: CPU bound — GIL 때문)
3. 우리 `list_all` 이 `gather` 없이 직렬로 `await self.get(r.id)` 했다면, 100개 세션 reconcile 의 latency 는 어떻게 변하나?
4. `asyncio.Lock` 안에서 `await asyncio.to_thread(...)` 를 부르면 이벤트 루프는 막히는가?
5. SQLite 연결을 `__init__` 에 한 번 만들어두지 않고 매 호출마다 새로 만드는 이유는?

→ [Chapter 08: SQLite 영속성](08-sqlite-persistence.md)

---

## 외부 자료 종합

- 📚 [Python asyncio 공식](https://docs.python.org/3/library/asyncio.html)
- 📖 [David Beazley — Curious Course on Coroutines and Concurrency](https://www.dabeaz.com/coroutines/) — 옛날 자료지만 코루틴의 본질
- 🎥 *Concurrency from the Ground Up* — Beazley PyCon 발표 (YouTube 검색)
- 📖 [Caleb Hattingh — Using Asyncio in Python](https://www.oreilly.com/library/view/using-asyncio-in/9781492075325/) — 책
