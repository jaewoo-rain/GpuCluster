# Anti-phase throttle — 오버헤드 개선 (다음 세션 작업 플랜)

**작성**: 2026-06-16 / **상태**: 계획 (미구현) / **목표 세션**: 다음 세션에서 구현 + 측정
**선행 결과**: [docs/results/2026-06-16_fGPU공유-재실험-종합결과.md](../results/2026-06-16_fGPU공유-재실험-종합결과.md)

> 이 문서 하나로 다음 세션이 **(1) hook 수정 → (2) 빌드 → (3) before/after 오버헤드 측정**까지
> 끝낼 수 있게 자기완결로 적는다. 세미나 발표 스토리 = **오버헤드 측정 → 고침 → 오버헤드 재측정**.

---

## 0. 발표 스토리 (이게 목적)

1. **측정 ①** (현재 duty-cycle throttle): 4:6 공유 시 오버헤드 약 **+80%**, occupancy **0.67** (둘 다 쉬는 idle 낭비).
2. **고침** (anti-phase throttle): 두 컨테이너의 활성 구간을 **겹치지 않게 시간 슬롯으로 타일링** → "둘 다 쉬는 순간" 제거.
3. **측정 ②** (anti-phase): 같은 하니스로 재측정 → occupancy↑(→~1.0), 오버헤드↓ 를 정량화. **4:6 분배는 유지**됨을 함께 보인다.

핵심 주장: *"우리 duty-cycle throttle의 오버헤드 원인은 비동기 sleep이 겹쳐 만든 idle이고, 시간 슬롯을 동기화(anti-phase)하면 그 idle을 없애 오버헤드를 줄일 수 있다."*

---

## 1. 왜 오버헤드가 생기나 (복습)

현재 throttle(`hook/src/fgpu_hook.c`의 `cudaLaunchKernel` 경로):
- 100ms 윈도우 안에서 `compute_ratio × window` 만큼 launch 통과, 나머지는 `nanosleep`.
- **per-process** 상태(`g_window_start_ns`, `g_compute_ratio`)라 **상대 컨테이너를 모름** → A의 sleep과 B의 sleep 위상이 제각각.
- 둘 다 sleep 하는 순간 GPU idle = 오버헤드. 측정 증거: occupancy_sum 0.67~0.83 (<1).

---

## 2. Anti-phase 설계 (구현 스펙)

### 2.1 핵심 아이디어
모든 컨테이너가 **절대 시계(CLOCK_REALTIME) 기준으로 윈도우 경계를 자동 동기화**하고,
각 컨테이너는 윈도우 안에서 **자기 오프셋부터 자기 비율만큼의 슬롯에서만** launch 통과.
오프셋을 누적 비율로 배정하면 슬롯이 **겹치지 않게 타일링** → 매 순간 최대 1명만 활성, 둘 다 idle 없음.

예 (W=100ms, 합=1.0):
- A: ratio 0.4, offset 0.0 → 슬롯 `[0ms, 40ms)`
- B: ratio 0.6, offset 0.4 → 슬롯 `[40ms, 100ms)`

### 2.2 윈도우 동기화 = "조율 불필요"의 열쇠
- `phase = now_ns % W_ns` 를 **CLOCK_REALTIME(절대시각)** 으로 계산 → 컨테이너들이 별도 통신 없이 **같은 윈도우 경계**를 공유 (Docker time namespace 미사용 전제, 본 프로젝트 충족).
- 따라서 **공유해야 할 건 오프셋 하나뿐**. 윈도우 정렬은 공짜.

### 2.3 launch hook 의사코드 (anti-phase 분기)
```c
// 신규 env: FGPU_THROTTLE_ALGO = "dutycycle"(기본) | "antiphase"
//           FGPU_COMPUTE_OFFSET = double (기본 0.0)
double W   = g_window_ns;                 // 기존 윈도우(기본 100ms)
double s0  = g_compute_offset * W;        // 내 슬롯 시작
double s1  = (g_compute_offset + g_compute_ratio) * W;  // 내 슬롯 끝
uint64_t now = clock_gettime(CLOCK_REALTIME) -> ns;
double phase = (double)(now % (uint64_t)W);
if (phase >= s0 && phase < s1) {
    // 내 슬롯 → 통과
} else {
    // 내 슬롯 아님 → 내 다음 슬롯 시작까지 sleep
    double wait = (phase < s0) ? (s0 - phase) : ((W - phase) + s0);
    nanosleep(wait);
    // (깨어나면 통과)
}
```
- 기존 duty-cycle 경로는 그대로 두고 `FGPU_THROTTLE_ALGO` 로 분기 → **같은 .so로 before/after 비교 가능**.
- `g_window_ns`/`g_compute_ratio`는 기존 변수 재사용. `g_compute_offset` 신규.

### 2.4 합 < 1.0 인 경우 (주석으로 남길 것)
오프셋 타일링이 `[0, sum)` 만 덮고 `[sum, 1)` 구간은 **아무도 안 도는 의도된 idle**(예약했으나 안 씀).
work-conserving(2번 옵션)으로 이 틈을 메우는 건 **별도 후속**. 본 실험(합=1.0)에선 틈 없음.

---

## 3. 구현 단계 (파일별)

### 3.1 hook 수정 — `hook/src/fgpu_hook.c`
- throttle 섹션 찾기: `grep -n "THROTTLE\|window\|nanosleep\|compute_ratio\|g_window" hook/src/fgpu_hook.c`
- 추가:
  - 전역 `static double g_compute_offset = 0.0;` 와 `static int g_throttle_algo;` (0=dutycycle,1=antiphase)
  - init에서 `getenv("FGPU_COMPUTE_OFFSET")`, `getenv("FGPU_THROTTLE_ALGO")` 파싱 + init 로그에 출력
    (`[fgpu] init: throttle=... algo=antiphase offset=0.400 window_ms=100`)
  - `cudaLaunchKernel` 훅의 throttle 분기에 §2.3 의사코드 추가 (algo==antiphase일 때)
- 한국어 주석 유지(교육용 파일 규칙).

### 3.2 빌드 — **이미지 재빌드 불필요** ⚠️ 시간 절약 포인트
- 하니스(`run_sharing.sh`)는 `build/libfgpu.so` 를 **컨테이너에 마운트**한다 → **.so만 다시 빌드하면 끝**.
- 리눅스 클론에서: `cd ~/GpuCluster && ./scripts/build_hook.sh` → `build/libfgpu.so` 갱신.
- (윈도우 클론에서 코드 수정 시: 동기화 후 리눅스에서 빌드. 또는 리눅스 클론에서 직접 수정.)

### 3.3 하니스 수정 — `scripts/eval/run_sharing.sh`
- 신규 env 전달: `THROTTLE_ALGO="${THROTTLE_ALGO:-dutycycle}"`.
- `run_one()` 의 throttle 분기(`thr=(...)`)에 추가:
  - `-e FGPU_THROTTLE_ALGO="$THROTTLE_ALGO"`
  - **offset**: concurrent A = `0`, concurrent B = `$RATIO_A` (= A 비율). solo 는 offset 무의미(throttle off).
    → `run_one` 인자에 offset 추가하거나, label 로 분기: `[ "$1" = "A" ] && off=0 || off=$RATIO_A`.
  - `-e FGPU_COMPUTE_OFFSET="$off"`
- 이러면 기존 `THROTTLE_MODE=conc` 그대로 쓰되 `THROTTLE_ALGO=antiphase` 만 켜면 됨.

### 3.4 (선택) hook smoke — `hook/tests/test_throttle.cu` 활용 or 신규
- anti-phase 타이밍 검증용. 두 프로세스를 offset 0/0.5로 띄워 GPU util 타임라인이 번갈아 차는지 확인.
- 최소한, init 로그에 `algo=antiphase offset=...` 가 찍히는지 + 단일 컨테이너 throughput이 dutycycle과 유사한지(혼자면 둘 다 비율만큼 느림).

---

## 4. 측정 계획 (before / after) — 기존 하니스 재사용

### 4.1 베이스라인 ① (이미 측정됨 — 재측정 불필요, 참고값)
현재 duty-cycle, Qwen2-0.5B, ratio 0.4/0.6 (출처: 종합결과 문서):
- 단일 측정(NITERS=30): makespan_seq **105.6s**, makespan_conc **189.4s**, **overhead +83.7s (speedup 0.558)**, **occupancy 0.748**.
- 스윕 b1 conc(NITERS=12): speedup **0.465**, occupancy **0.666**, a_share **0.373**.

> 재현이 필요하면: `THROTTLE_MODE=conc THROTTLE_ALGO=dutycycle ... run_sharing.sh`

### 4.2 측정 ② (anti-phase, 신규)
```bash
# 리눅스 GPU 호스트, build/libfgpu.so 는 anti-phase 반영된 새 .so
IMAGE=fgpu-runtime-pytorch:stage4-infer \
  THROTTLE_MODE=conc THROTTLE_ALGO=antiphase NITERS=30 \
  bash scripts/eval/run_sharing.sh
# → experiments/sharing_<TS>/overlap_report.txt
```
직접 비교용 스윕(권장, n=3):
```bash
IMAGE=fgpu-runtime-pytorch:stage4-infer NITERS=12 REPS=3 \
  BATCHES='1' THROTTLES='conc' THROTTLE_ALGO=antiphase \
  bash scripts/eval/run_sweep.sh
```
(주의: `run_sweep.sh`/`run_sharing.sh` 에 `THROTTLE_ALGO` 전달 경로가 들어가야 함 — §3.3.)

### 4.3 기대 결과 & 판정
| 지표 | dutycycle (①) | anti-phase (②) 기대 |
|---|---|---|
| occupancy_sum | 0.67~0.75 | **→ ~1.0** (둘 다 idle 제거) |
| overhead | +80% | **감소** (이상적이면 0 근처) |
| a_share | 0.37 (≈4:6) | **유지 (≈4:6)** ← 반드시 확인 |
| 요청당 latency | A ×3.6 / B ×2.3 | 감소 |

**PASS 기준**: (1) occupancy_sum 이 dutycycle 대비 유의하게 상승, (2) overhead 유의하게 감소,
(3) a_share 가 여전히 ratio 추종(분배 안 깨짐). 셋 다 충족 시 "anti-phase로 오버헤드 개선" 입증.

### 4.4 정직한 한계 (발표에 명시)
- anti-phase는 **컴퓨트를 직렬화**(번갈아) → 동시 SM 공유로 여유 회수하는 이득(occupancy>1)은 **포기**.
  따라서 **가벼운 부하**에선 no-throttle보다 느릴 수 있음. **공정성 보장이 필요한 포화 상황의 개선**으로 프레이밍.
- 비교는 **dutycycle-throttle vs anti-phase-throttle**(둘 다 4:6 강제) — "throttle을 더 잘 만든 것".
  no-throttle(speedup 1.4)은 별개 축(분배 보장 없음)으로 같이 제시.
- 커널 실행 시간 spillover(슬롯 경계 걸친 커널), nanosleep 정밀도(~50μs)는 기존과 동일한 근사 한계.

---

## 5. 환경 메모 (다음 세션이 헤맬 부분)

- **GPU 호스트 = WSL2 Ubuntu** (`wsl -d Ubuntu`). GPU(RTX 4070)+Docker 동작. 리눅스 클론 `/home/ubuntu/GpuCluster`.
- 윈도우 클론(`/mnt/c/Users/박도현/Desktop/GpuCluster`)에서 코드 수정 시 리눅스로 동기화 필요.
  (지난 세션은 임시 sync 스크립트 사용 — 정리됨. 필요하면 `cp` 로 복사 후 `sed -i 's/\r$//'` CRLF 제거.)
- **PowerShell→wsl 따옴표 주의**: `$(...)` 를 PowerShell이 가로챔 → 명령은 **.sh 파일로 만들어 `tr -d '\r' < file | bash`** 로 실행하는 패턴이 안전.
- 측정 이미지: `fgpu-runtime-pytorch:stage4-infer` (= stage4 + `pip install numpy transformers accelerate sentencepiece`).
  - **TODO**: 이 transformers 설치를 `runtime-image-pytorch/Dockerfile` 에 내재화(지금은 수동 파생 이미지).
- **caching**: 측정 스크립트가 `import torch` 전에 `PYTORCH_NO_CUDA_MEMORY_CACHING` 제거(기본 caching ON). `FGPU_MEAS_CACHING=off` 로 끔.
- 모델 캐시: `experiments/hf_cache` (재다운로드 방지).

---

## 6. 변경 예정 파일

| 파일 | 변경 |
|---|---|
| `hook/src/fgpu_hook.c` | anti-phase 분기 + `FGPU_THROTTLE_ALGO`/`FGPU_COMPUTE_OFFSET` 파싱 + init 로그 |
| `scripts/eval/run_sharing.sh` | `THROTTLE_ALGO` 전달, concurrent offset(A=0, B=RATIO_A) 주입 |
| `scripts/eval/run_sweep.sh` | `THROTTLE_ALGO` 패스스루(env export) |
| (선택) `hook/tests/test_throttle.cu` | anti-phase 타이밍 smoke |
| (선택) `backend/app/services/docker_manager.py` | 백엔드 경로로도 쓰려면 offset 주입(실험엔 불필요, 하니스로 충분) |
| `docs/results/...` (신규) | 측정 ② 결과 + before/after 비교표 |

---

## 7. 마일스톤 (다음 세션)

- **M1** — hook anti-phase 구현 + `build_hook.sh` 빌드. init 로그로 algo/offset 확인. (cuda-hook / gpu-throttle)
- **M2** — `run_sharing.sh` offset/algo 전달. 단일 `THROTTLE_ALGO=antiphase` 측정 1회 → occupancy 상승 확인(스모크). (eval)
- **M3** — before/after 스윕(n=3) → occupancy·overhead·a_share 비교표. PASS 판정. (eval)
- **M4** — 결과 문서 + 발표 덱("측정→고침→측정") 반영. (docs)

각 단계 자체검증: M1=로그, M2=occupancy↑ 스모크, M3=판정표, M4=문서.

---

## 8. 한 줄 요약 (다음 세션 시작점)

> `hook/src/fgpu_hook.c` 의 launch-throttle에 **절대시각 윈도우 기반 슬롯 게이팅(anti-phase)** 를 `FGPU_THROTTLE_ALGO=antiphase`로 추가하고(offset: A=0, B=ratioA), `.so`만 재빌드해 `run_sharing.sh THROTTLE_MODE=conc THROTTLE_ALGO=antiphase` 로 재측정. **occupancy 0.67→~1.0, overhead↓, a_share≈4:6 유지**면 성공. dutycycle 대비 before/after로 발표.
