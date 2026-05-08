# Chapter 13 — Admission Control (capacity gate)

## 학습 목표

- 후크와 admission 이 *서로 다른 두 강제 layer* 임을 안다.
- "GPU overlap" 규칙을 직접 그려서 설명할 수 있다.
- TOCTOU race 가 이 시나리오에서 어떻게 발생하고, `asyncio.Lock` 으로 어떻게 막는지 안다.
- `force=True` 의 의미와 oversubscription 의 위험을 안다.

---

## 13.1 두 강제 layer — 한 그림

```
[POST /sessions {ratio: 0.4}]
        │
        ▼
   admission.check()      ← 이 챕터 (capacity gate)
        │  통과
        ▼
   docker.containers.run()
        │
        ▼
   [컨테이너 안 사용자 코드]
        │
        ▼
   cudaMalloc(...)         ← Chapter 03 의 hook (per-container quota)
        │
        ▼
   [성공 / DENY]
```

| 측면 | Admission | Hook |
|---|---|---|
| 어디서 | 백엔드 (호스트) | 컨테이너 안 |
| 언제 | 컨테이너 *spawn 시* | `cudaMalloc` *호출 시* |
| 무엇 검사 | 합 ratio ≤ 1.0 인가 | size ≤ quota_bytes 인가 |
| 거부의 의미 | "이 컨테이너를 못 띄움" — HTTP 409 | "이 alloc 만 거부" — `cudaErrorMemoryAllocation` |

이 둘이 *상보적* 으로 동작합니다. admission 만 있으면 컨테이너 안에서 사용자가 임의 alloc 을 다 할 수 있고, hook 만 있으면 두 컨테이너가 합쳐서 1.0 초과로 spawn 가능 (CUDA context overhead 등으로 실제 OOM 가능).

---

## 13.2 admission 의 정책 — 한 줄

```
같은 GPU(또는 None) 위에서 종료되지 않은 세션의 ratio 합 > 1.0 → 거부
```

[backend/app/services/admission.py](../../backend/app/services/admission.py) 의 `check()` 가 그대로 구현:

```python
def check(sessions, requested_ratio, gpu_index):
    used, n = sum_used_ratio(sessions, gpu_index)
    if used + requested_ratio > 1.0 + _TOL:
        raise AdmissionDenied(...)
```

`_TOL = 1e-9` 는 부동소수점 합산 오차 흡수 (예: `0.3 + 0.3 + 0.4` 가 `1.0000000000000002` 가 되는 케이스).

---

## 13.3 GPU overlap 규칙

[admission.py:62-69](../../backend/app/services/admission.py#L62-L69):

```python
def gpu_overlaps(a: Optional[int], b: Optional[int]) -> bool:
    if a is None or b is None:
        return True
    return a == b
```

세 케이스:

| 세션 1 | 세션 2 | overlap? | 의미 |
|---|---|---|---|
| `gpu_index=None` | `gpu_index=None` | YES | 둘 다 *전체 GPU* — 같은 풀 |
| `gpu_index=None` | `gpu_index=0` | YES | None 은 *모든* device 와 겹침 |
| `gpu_index=0` | `gpu_index=0` | YES | 같은 device |
| `gpu_index=0` | `gpu_index=1` | NO | 다른 device — 격리됨 |

### 단일 GPU 호스트 (RTX 4070 등)

모든 세션이 항상 overlap → 정책이 사실상 `sum(active_ratios) ≤ 1.0`.

### 멀티 GPU 호스트

- 두 device 에 각자 ratio 0.7 + 0.7 가능 (서로 격리).
- 단, `gpu_index=None` 인 세션은 *모든* device 의 capacity 를 잡아먹음 — 큰 영향. UI 설계 시 명시 필요.

### 더 공부하려면
- [Backend.AI 의 fGPU 정책 문서](https://docs.backend.ai/) — 비교 대상

---

## 13.4 TOCTOU race — `asyncio.Lock` 이 푸는 문제

[Chapter 07](07-async-io.md) 에서 본 비동기 동시성. 두 사용자가 *동시에* `POST /sessions {ratio: 0.6}` 보내면:

```
[요청 A]  list_all() → used=0
[요청 B]                                   list_all() → used=0
[요청 A]  check(0.6) — 0+0.6=0.6 ≤ 1.0 OK
[요청 B]                                   check(0.6) — 0+0.6=0.6 ≤ 1.0 OK
[요청 A]  docker.run(...)
[요청 B]                                   docker.run(...)
                                            → 둘 다 spawn — used=1.2 ✗
```

이게 전형적 TOCTOU (Time-of-check to time-of-use). 검사–실행 사이에 다른 흐름이 끼어 *둘 다* 검사를 통과.

해결: [session_manager.py:73](../../backend/app/services/session_manager.py#L73) 의 `asyncio.Lock`:

```python
async def create(self, ratio, ...):
    async with self._create_lock:
        return await self._create_locked(...)
```

`_create_locked` 안에서 `list_all` + `check` + `docker.run` 이 *원자적으로* 직렬화. 다른 요청은 이 lock 이 풀릴 때까지 대기.

### 직렬화의 비용?

- lock 안 작업 = list_all (수십 ms) + admission check (μs) + docker.run (1~2초).
- 두 동시 요청 → 합쳐서 ~3~4초. 직렬보다 빠르진 않지만 *correctness* 가 우선.
- 더 비싼 동시성을 원하면 락을 좀 더 잘게 (예: list_all 만 lock 밖, check+run 만 lock 안) — 본 프로토타입은 가독성 우선.

### 더 공부하려면
- [Wikipedia — TOCTOU](https://en.wikipedia.org/wiki/Time-of-check_to_time-of-use)
- [Python asyncio.Lock](https://docs.python.org/3/library/asyncio-sync.html#asyncio.Lock)

---

## 13.5 stale session 처리

[admission.py:79-89](../../backend/app/services/admission.py#L79-L89) 의 `sum_used_ratio`:

```python
for r in sessions:
    if r.status not in ("created", "running"):
        continue  # exited / removed 는 제외
    if not gpu_overlaps(r.gpu_index, gpu_index):
        continue
    used += r.ratio
```

핵심: `status` 를 보고 *현재* 점유 중인 세션만 합산. 종료된 세션은 quota 풀어준 것으로 간주.

여기서 우리가 보는 sessions 는 [session_manager.list_all()](../../backend/app/services/session_manager.py#L221) 로 *docker daemon 과 reconcile* 된 결과. 컨테이너가 daemon 에서 사라졌거나 종료됐으면 `status` 가 자동으로 갱신 → admission 이 stale 한 quota 안 잡음.

> 예외 케이스: 컨테이너가 *정말로* 영원히 안 끝나는 jupyter 노트북. 사용자가 명시적으로 stop/delete 안 해주면 quota 는 영구 점유. → [Chapter 15 한계](15-limitations.md) 의 "idle 사용자".

---

## 13.6 `force=True` — oversubscription escape hatch

```python
async def create(..., force: bool = False):
    async with self._create_lock:
        return await self._create_locked(..., force=force)

async def _create_locked(..., force):
    if not force:
        sessions = await self.list_all()
        admission.check(sessions, requested_ratio=ratio, gpu_index=gpu_index)
    ... docker run ...
```

`force=True` 면 admission 검사 skip. 의도적 oversubscription.

용도:
- 실험 / 디버그 — "정책 위반 시 어떤 일이 벌어지는지" 관찰.
- 강제 다중 사용자 테스트.

UI 에서 체크박스로 노출 — 의식적으로 켜야 동작.

위험: 두 컨테이너가 각자 quota 안에선 OK 지만 *물리* GPU 가 부족해 실제 OOM 가능. CUDA context overhead (~700 MiB / process) 도 추가로 잡아먹음.

---

## 13.7 `usage_snapshot` — UI 의 capacity 라인

[admission.py:108-135](../../backend/app/services/admission.py#L108-L135):

```python
def usage_snapshot(sessions) -> dict:
    return {
        "by_gpu": {
            "all": {"ratio_used": 0.7, "ratio_available": 0.3, "active_sessions": 1},
            "0":   {"ratio_used": 0.4, "ratio_available": 0.6, "active_sessions": 1},
        }
    }
```

`GET /sessions/admission` 가 이걸 반환 → UI 가 3초마다 polling 해 capacity 라인 업데이트:

```
capacity: GPU all: 0.700/1.000 (1 session)
          GPU 0:   0.400/1.000 (1 session)
```

폼 옆에 보이니 사용자가 *제출 전에* 거절될지 알 수 있음.

---

## 13.8 단위 테스트 — docker / GPU 없이

[backend/tests/test_admission.py](../../backend/tests/test_admission.py) 가 17개 테스트로 admission 의 모든 분기를 검증합니다.

```bash
cd backend && pip install -e ".[dev]" && pytest tests/test_admission.py -v
```

이게 가능한 이유: `admission.py` 가 *순수 함수 모듈* — docker / GPU / 네트워크 의존성 0. `sessions` 는 그냥 객체 리스트. CI 가 GPU 없는 머신에서도 검증 가능.

이게 [Chapter 06](06-fastapi-backend.md) 에서 본 *layer 분리* 의 가치예요. 비즈니스 로직을 인프라에서 떼어내면 테스트가 빨라지고 정확해집니다.

---

## 13.9 직접 해보기

```bash
./scripts/run_backend.sh

# 1) 0.7 세션 spawn
curl -s -X POST http://localhost:8000/sessions \
    -H 'Content-Type: application/json' \
    -d '{"ratio":0.7}'

# 2) 현재 capacity 확인
curl -s http://localhost:8000/sessions/admission | jq
# {"by_gpu":{"all":{"ratio_used":0.7,"ratio_available":0.3,"active_sessions":1}}}

# 3) 0.4 추가 시도 — 0.7+0.4 > 1.0 → 409
curl -i -X POST http://localhost:8000/sessions \
    -H 'Content-Type: application/json' \
    -d '{"ratio":0.4}'
# HTTP/1.1 409 Conflict
# {"detail":{"error":"admission_denied","requested_ratio":0.4,
#  "currently_used":0.7,"available":0.3,...}}

# 4) force=true 로 우회
curl -i -X POST http://localhost:8000/sessions \
    -H 'Content-Type: application/json' \
    -d '{"ratio":0.4, "force":true}'
# HTTP/1.1 201 Created — oversubscribed
```

### TOCTOU race 직접 시연

```bash
# 두 0.6 세션을 *동시* 요청 → 정확히 하나만 201, 다른 건 409
( curl -s -o /tmp/r1 -w "%{http_code}\n" -X POST http://localhost:8000/sessions \
    -H 'Content-Type: application/json' -d '{"ratio":0.6}' ) &
( curl -s -o /tmp/r2 -w "%{http_code}\n" -X POST http://localhost:8000/sessions \
    -H 'Content-Type: application/json' -d '{"ratio":0.6}' ) &
wait
# 둘 다 201 이면 lock 이 안 걸린 것 — 버그
# 정확히 하나가 201, 하나가 409 면 정상
```

---

## 자가점검 질문

1. admission 과 hook quota 가 *모두* 필요한 이유를 한 가지 시나리오로 설명하라.
2. `gpu_index=None` 인 세션이 멀티 GPU 호스트에서 미치는 영향은?
3. TOCTOU race 가 *이 시나리오에서* 발생하는 정확한 순간은?
4. `force=True` 가 *어떤 검사를* 건너뛰는가? hook 의 quota 검사도 건너뛰는가? (정답: 아니요. hook 은 컨테이너 안에 있어 백엔드 force 무관)
5. exited 된 세션이 admission 합산에서 제외되는 이유는?

→ [Chapter 14: Jupyter Lab 통합](14-jupyter.md)

---

## 외부 자료 종합

- 📚 [Wikipedia — Admission control](https://en.wikipedia.org/wiki/Admission_control)
- 📚 [Wikipedia — TOCTOU](https://en.wikipedia.org/wiki/Time-of-check_to_time-of-use)
- 📖 *Designing Data-Intensive Applications* by Martin Kleppmann — 7장 (Transactions) 의 race condition 분류가 매우 좋음
