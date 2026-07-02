# 밑바닥부터 직접 만드는 fGPU 🛠️

> [온보딩 교과서](../onboarding/README.md)로 이 프로젝트를 **이해한** 사람이,
> 이제 **빈 디렉토리에서 시작해 전체를 자기 손으로 다시 구현**하려 할 때 보는 실전 개발 가이드입니다.

## 온보딩 교과서와 무엇이 다른가요?

| | 온보딩 교과서 | 이 교과서 (밑바닥부터) |
|---|---------------|------------------------|
| 질문 | "이게 **무엇이고 왜** 이런가?" | "그럼 나는 **무엇을·어떤 순서로·어떻게** 짜나?" |
| 초점 | 개념·아키텍처 이해 | 개발 순서·점진적 구현·검증 게이트 |
| 방식 | 비유와 설명 | 빈 파일 → 스텁 → 검증 → 살 붙이기 |
| 대상 | 신입 (C 초보) | 온보딩을 뗀 사람 |

---

## 핵심 개발 철학 (0장 요약)

1. **가장 위험한 것부터** — 훅(LD_PRELOAD)이 되는지부터 증명. 백엔드·UI는 나중.
2. **스텁 → 검증 → 살 붙이기** — 한 번에 다 짜지 말고, 각 단계를 실행해 눈으로 확인하며 쌓기.
3. **baseline vs hooked** — 항상 훅 없을 때와 있을 때를 나란히 비교해 검증.

---

## 📚 목차 (프로젝트 Stage 로드맵을 개발 여정으로 재구성)

- **[0장. 개발 로드맵과 환경 준비](00-roadmap-and-setup.md)** ⭐ 여기부터!
  전체 개발 순서·의존성·환경 셋업·저장소 골격.

### 훅 라인 (C — 프로젝트의 심장)
- **[1장. Stage 1 — 최소 훅을 밑바닥부터](01-stage1-minimal-hook.md)**
  빈 `.c` → 로드 확인 → dlsym 위임 → quota → ALLOW/DENY → tracking.
- **[5장. Stage 5-C·6·7 — 훅을 여러 계층으로 확장](05-stage5c-6-7-hook-expansion.md)**
  Driver·VMM·launch 추가. **이중 카운트가 터지고 재진입 가드가 등장하는 장.**
- **[7장. Stage 12 — Duty-cycle 컴퓨트 스로틀](07-stage12-throttle.md)**
  시간 윈도우·nanosleep·throughput ∝ ratio.

### 컨테이너 & 평가 라인
- **[2장. Stage 2·4 — 컨테이너화와 PyTorch 통합](02-stage2-4-container-pytorch.md)**
  Dockerfile·마운트 패턴·PyTorch OOM 전파.
- **[4장. Stage 5 — 평가 인프라와 웹 UI](04-stage5-eval-and-ui.md)**
  isolation·overhead·correlation 하네스, vanilla JS UI.

### 백엔드 라인 (Python)
- **[3장. Stage 3·8 — 백엔드를 밑바닥부터](03-stage3-8-backend.md)**
  FastAPI 골격 → docker_manager → SQLite 영속 → async 리팩터.
- **[6장. Stage 9·10·11 — 운영 기능](06-stage9-11-ops.md)**
  인증·멀티GPU·Jupyter·어드미션(순수함수 먼저, 통합 나중).

### 종합
- **[8장. 종합 — 순서·의존성·함정 체크리스트](08-synthesis-checklist.md)**
  전체 의존성 그래프, 순서표, 검증 게이트 모음, 함정 체크리스트, 확장 아이디어.

---

## 권장 진행 순서

챕터 번호 순(0→1→2→…→8)이 기본 권장 경로입니다. 다만 8장의 의존성 그래프에서 보듯 **훅 라인(1→5→7)** 과 **백엔드 라인(3→6)** 은 Stage 2가 끝난 뒤엔 어느 정도 **병렬로** 진행할 수 있습니다.

## 🔗 함께 보기

- [../onboarding/](../onboarding/) — 개념 이해용 온보딩 교과서 (선행 필수)
- [../../CLAUDE.md](../../CLAUDE.md) — 완성된 저장소의 파일별 역할·스테이지 성공 기준 (정답지)
- [../../description.md](../../description.md) — 설계의 "왜"
- [../../LINUX_SETUP.md](../../LINUX_SETUP.md) — 환경 설치 런북
