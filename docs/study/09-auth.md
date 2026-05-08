# Chapter 09 — Bearer 인증과 타이밍 공격

## 학습 목표

- HTTP Authorization 헤더의 Bearer 스킴이 어떻게 생겼는지 안다.
- `hmac.compare_digest` 가 *왜* 일반 `==` 보다 안전한지 안다.
- "타이밍 공격" 의 직관을 가진다.
- 우리 인증의 *의도된* 한계 (single static token, no RBAC) 와 그 이유를 안다.

---

## 9.1 Bearer 스킴 — 한 그림

```
클라이언트 ──── HTTP ──── 서버

요청 헤더:
  Authorization: Bearer <opaque-token>

서버:
  - "Bearer " 접두사 검사
  - 뒤의 토큰을 *상수 시간* 으로 기대값과 비교
  - 일치 → 통과, 불일치 → 401 + WWW-Authenticate: Bearer
```

"Bearer" 의 의미: *그 토큰을 들고 있는 사람* 은 누구든 권한 보유. 분실 시 즉시 무효화 필요. ([RFC 6750](https://datatracker.ietf.org/doc/html/rfc6750))

JWT 같은 *self-describing* 토큰도 Bearer 스킴 위에서 동작 — 우리 프로토타입은 그보다 한참 전 단계인 *opaque static token* 입니다.

---

## 9.2 우리 코드 — `_require_auth`

[backend/app/api/sessions.py](../../backend/app/api/sessions.py) 의 의존성:

```python
from fastapi import Header, HTTPException, Request
import hmac

def _require_auth(
    request: Request,
    authorization: str | None = Header(None),
):
    expected = request.app.state.api_token
    if not expected:
        return  # auth disabled — token unset

    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = authorization[len("Bearer "):]
    if not hmac.compare_digest(token, expected):
        raise HTTPException(status_code=401, detail="invalid bearer token")
```

라우터에 등록:
```python
router = APIRouter(prefix="/sessions",
                   dependencies=[Depends(_require_auth)])
```

읽을 포인트:

- **빈 토큰 = 인증 비활성**. 개발 친화 default. `FGPU_API_TOKEN` 안 주면 호환 모드.
- **`WWW-Authenticate: Bearer` 헤더**. RFC 7235 가 요구하는 401 응답의 표준 부속.
- **`/sessions` 라우터에만 적용**. `/healthz` 와 `/` (UI) 는 항상 public — health probe / 스크린샷 용도.
- **`hmac.compare_digest`**: 다음 절에서 설명.

### 더 공부하려면
- [RFC 6750 — Bearer Token Usage](https://datatracker.ietf.org/doc/html/rfc6750)
- [FastAPI — Security Tutorials](https://fastapi.tiangolo.com/tutorial/security/) — JWT 까지 가는 정석 학습

---

## 9.3 타이밍 공격 — 한 줄로

`a == b` 를 문자열 비교할 때, 보통 라이브러리는 **첫 다른 글자에서 즉시 false 반환**. 즉, 일치하는 prefix 가 길면 비교 시간이 *조금 더 길어짐*.

공격자가 토큰을 모르는 상태에서:
1. `Bearer aaaaaaaa...` 로 시도 → ~10ns
2. `Bearer baaaaaaa...` → ~10ns
3. ...
4. 진짜 토큰의 첫 글자가 `s` → `Bearer saaaaaa...` 시도 → ~12ns (한 글자 더 일치)

이 미묘한 차이를 수만 번 측정해 평균내면 한 글자씩 토큰을 *복원* 할 수 있습니다. 네트워크 jitter 가 크면 어렵지만, *원리적으로* 가능.

### `hmac.compare_digest` 의 보장

```python
hmac.compare_digest(a, b)
```

길이가 같으면 **모든 글자를 비교** 한 뒤 결과만 반환. 길이가 다르면 즉시 false 지만 그 자체가 길이 정보 누설(작은 양). 시간 차이는 사실상 없음.

같은 원리의 자매 함수: `secrets.compare_digest`, `hmac.HMAC.verify`.

### 더 공부하려면
- [Python hmac.compare_digest 공식](https://docs.python.org/3/library/hmac.html#hmac.compare_digest)
- [Coda Hale — A Lesson In Timing Attacks](https://codahale.com/a-lesson-in-timing-attacks/) — 친절한 입문
- [OWASP — Timing Attack](https://owasp.org/www-community/attacks/Side_Channel_Attack) — 광의로 사이드채널

---

## 9.4 `Header(None)` 이 하는 일

```python
authorization: str | None = Header(None)
```

FastAPI 의존성이 자동으로:
- HTTP `Authorization` 헤더 값을 추출 (snake_case → kebab-case 자동 변환).
- 없으면 `None`.
- OpenAPI 스키마에 헤더 파라미터로 등록 → `/docs` 의 try-it-out 에서 입력 가능.

매개변수 이름이 `authorization` 이라 `Authorization` 헤더와 매칭됩니다. 다른 이름이면 `Header(None, alias="X-API-Key")` 같이 alias 명시.

---

## 9.5 의도된 한계

본 프로토타입이 *하지 않는* 것 — 모두 의도된 trade-off:

| 미구현 | 이유 |
|---|---|
| 토큰 회전 (rotation) | 단일 static token + 환경변수가 prototype scope. 운영자가 재시작 시 갱신 |
| 다중 사용자 / RBAC | 캡스톤 시연용. 다중 사용자는 Stage 9 full |
| Refresh token / OAuth | JWT 도입 시점에 같이 — 현재는 더 단순한 모델 |
| Rate limiting | nginx / cloudflare 등 reverse proxy 영역 |
| 감사 로그 (audit) | uvicorn access log + docker daemon log 로 충분 |

이 한계를 *왜 의도된 것* 으로 적느냐면, 캡스톤 발표에서 "왜 RBAC 안 했어요?" 같은 질문에 *축소* 가 아니라 *범위 결정* 으로 답할 수 있어서입니다.

---

## 9.6 직접 해보기

```bash
# 인증 활성화로 백엔드 시작
FGPU_API_TOKEN=secret-dev-token ./scripts/run_backend.sh

# 다른 터미널:
# 1) 헬스체크는 항상 public
curl http://localhost:8000/healthz
# {"ok": true, "auth_enabled": true}

# 2) 인증 없이 sessions 호출 — 401
curl -i -X POST http://localhost:8000/sessions \
    -H 'Content-Type: application/json' \
    -d '{"ratio":0.3}'
# HTTP/1.1 401 Unauthorized
# www-authenticate: Bearer
# {"detail":"missing bearer token"}

# 3) 잘못된 토큰 — 401
curl -i -X POST http://localhost:8000/sessions \
    -H 'Authorization: Bearer wrong' \
    -H 'Content-Type: application/json' \
    -d '{"ratio":0.3}'
# {"detail":"invalid bearer token"}

# 4) 올바른 토큰 — 201
curl -i -X POST http://localhost:8000/sessions \
    -H 'Authorization: Bearer secret-dev-token' \
    -H 'Content-Type: application/json' \
    -d '{"ratio":0.3}'
# HTTP/1.1 201 Created
```

UI 도 동일하게 동작 — 좌상단 토큰 입력란에 토큰을 저장하면 `localStorage` 에 들어가고 모든 fetch 가 자동으로 `Authorization` 헤더 첨부.

---

## 9.7 보안 체크리스트 (간단)

본 프로토타입에서:

- ☑ 토큰을 stderr / 로그에 찍지 마라. (`fprintf` 도, FastAPI 미들웨어도)
- ☑ HTTPS 뒤에서 운영. HTTP 평문은 토큰 누출.
- ☑ 토큰은 환경변수 / secrets manager 에서 주입. 코드에 박지 마라.
- ☑ 401 응답은 *동일한 본문* 으로 (missing / invalid 구분이 약한 정보 누설). 우리 코드는 둘을 구분 — prototype scope 라 OK 지만 prod 에선 통일 권장.
- ☐ Rate limit (구현 안 됨, 운영 시 reverse proxy 로)
- ☐ 토큰 회전 (수동, 재시작 필요)

---

## 자가점검 질문

1. `Authorization: Bearer xxx` 의 "Bearer" 의미는?
2. `==` 대신 `hmac.compare_digest` 를 쓰는 이유 한 줄.
3. 우리 코드에서 `/healthz` 가 인증 없이도 응답하는 이유는?
4. `_require_auth` 가 *라우터* 가 아니라 각 엔드포인트의 `Depends` 였다면, 새 엔드포인트 추가 시 어떤 위험이 있나?
5. 401 응답에 `WWW-Authenticate: Bearer` 헤더를 넣는 이유는?

→ [Chapter 10: PyTorch caching allocator](10-pytorch-caching.md)

---

## 외부 자료 종합

- 📄 [RFC 6750 — Bearer Token Usage](https://datatracker.ietf.org/doc/html/rfc6750)
- 📄 [RFC 7235 — HTTP Authentication](https://datatracker.ietf.org/doc/html/rfc7235)
- 📚 [Python hmac](https://docs.python.org/3/library/hmac.html), [secrets](https://docs.python.org/3/library/secrets.html)
- 🛡 [OWASP Cheat Sheet — Authentication](https://cheatsheetseries.owasp.org/cheatsheets/Authentication_Cheat_Sheet.html)
- 📖 *Web Application Security* by Andrew Hoffman — 책
