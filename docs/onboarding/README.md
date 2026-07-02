# fGPU 온보딩 교과서 📖

> 이 프로젝트(fGPU — LD_PRELOAD 기반 분수 GPU 프로토타입)에 **새로 합류한 개발자**를 위한 인수인계용 교과서입니다.
> **읽기만 해도 프로젝트 이해도가 80% 이상 오르도록** 처음부터 아주 친절하게 썼습니다.

## 이 교과서가 가정하는 독자 수준

- C 언어는 **거의 처음** (`malloc`이 뭔지 몰라도 됩니다)
- Python은 **조금** 읽을 줄 앎
- Java **Spring은 다뤄봤음** (백엔드는 Spring에 빗대어 설명)
- 터미널·쉘 스크립트(`.sh`)·Docker는 **잘 모름**

필요한 사전 지식은 전부 이 교과서 안에서 처음부터 설명합니다.

---

## 📚 목차 (반드시 이 순서로 읽으세요)

### 0부. 오리엔테이션
- **[0장. 시작하기 전에 — 큰 그림과 공부 순서](00-welcome.md)** ⭐ 여기부터!
  프로젝트가 뭘 하는 물건인지, 부품 지도, 용어집, 학습 로드맵.

### 1부. 사전 지식 (모르면 코드가 안 읽혀요)
- **[1장. C 언어 기초](01-c-language-basics.md)**
  변수·포인터·`malloc`/`free`·컴파일·`.so` 공유 라이브러리·구조체·연결 리스트.
- **[2장. 쉘 스크립트와 터미널](02-shell-scripts.md)**
  `.sh`가 뭔지, bash 문법, 환경변수 주입 관례, `build_hook.sh` 한 줄씩 해부.
- **[3장. Docker와 GPU 컨테이너](03-docker-and-gpu.md)**
  컨테이너 개념, Dockerfile, `--gpus`/`-v`/`-e` 옵션, "훅을 굽지 않고 마운트하는" 설계.

### 2부. 프로젝트의 심장 ⭐가장 중요
- **[4장. LD_PRELOAD와 dlsym](04-ld-preload-and-dlsym.md)**
  이 프로젝트가 작동하는 마법의 원리 — 함수 가로채기.
- **[5장. CUDA 계층과 훅 코드 완전 분해](05-cuda-layers-and-hook-walkthrough.md)**
  `fgpu_hook.c`를 한 줄씩. 쿼터 계산·ALLOW/DENY·Stage 12 스로틀.
- **[6장. 동시성과 스레드 안전성](06-thread-safety.md)**
  race condition, mutex 자물쇠, `__thread` 재진입 가드, lock-free atomic.

### 3부. 백엔드 (Spring 개발자라면 편안)
- **[7장. FastAPI 백엔드 아키텍처](07-backend-fastapi.md)**
  Spring↔FastAPI 대응표, 계층 구조, 인증, `async`/`asyncio.to_thread`.
- **[8장. 세션 생명주기와 3계층 집행](08-backend-lifecycle-and-admission.md)**
  세션이 태어나 죽기까지, DockerManager·SQLite, 세 문지기의 협력.

### 4부. 굴려보기 & 종합
- **[9장. 빌드·실행·검증](09-build-run-verify.md)**
  스테이지 워크플로우, 스크립트 실행 순서, eval 하네스가 증명하는 것.
- **[10장. 전체를 하나로](10-putting-it-together.md)**
  요청 하나가 전 시스템을 관통하는 여정, 어느 파일부터 볼지, 한계와 다음 스텝.

---

## ⏱️ 시간이 없다면 (최소 경로)

**0장 → 4장 → 5장 → 8장 → 10장** 만 읽어도 뼈대는 잡힙니다.
단, C가 처음이면 **1장은 건너뛰지 마세요.**

## 🔗 이 교과서를 뗀 다음엔

- [../study/](../study/) — 더 전문적인 심화 학습 챕터들
- [../../CLAUDE.md](../../CLAUDE.md) — 프로젝트 전체 지도 (파일별 역할, 스테이지 성공 기준)
- [../../description.md](../../description.md) — 설계의 "왜"를 담은 긴 한글 문서
- [../../ARCHITECTURE.md](../../ARCHITECTURE.md) — 파일 트리와 책임 매트릭스
