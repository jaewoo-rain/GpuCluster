---
marp: true
theme: default
paginate: true
size: 16:9
---

<!--
발표용 소스 덱. 하나의 스토리로 단순화:
  "겹치는 시간 → GPU 노는 순간 생김 → anti-phase가 겹침 제거 → 노는 시간↓ → 전체 시간↓ (4:6 분배는 유지)"
모든 수치는 2026-06 실측, Qwen2-1.5B-Instruct (GPU를 충분히 채우는 현실적 큰 모델) 기준.
지표 정의는 scripts/eval/_overlap.py 와 1:1.
-->

<!-- _class: lead -->

# fGPU 코어 시분할 오버헤드 개선
## duty-cycle throttle → **anti-phase throttle**

두 작업의 GPU 사용 시간이 **겹치며 생기는 "노는 시간"**을 없애
4:6 분배는 유지하면서 전체 실행시간을 줄인다

2026-06 · RTX 4070 12GB · Qwen2-1.5B-Instruct

---

# 한 줄 요약

> **두 작업의 시간 슬롯을 겹치지 않게 맞추니, GPU 노는 시간이 줄고 전체 시간이 26% 빨라졌다 — 4:6 분배는 그대로.**

핵심 수치 (1.5B, ratio 0.4/0.6, before → after):

- **GPU 노는 시간(idle): 47% → 41%**
- **전체 실행시간: 109.5s → 80.8s (26%↓)**
- **순차 대비 오버헤드: +57s → +25s (56%↓)**
- **4:6 분배(a_share): 0.39 → 0.37 — 유지** (공정성 안 깨짐)
- 요청당 지연: A ×3.8→×2.2, B ×2.4→×1.7

---

# 1. 배경 — fGPU(GPU 한 장을 비율로 공유)

- **목표**: NVIDIA GPU **한 장**을 여러 컨테이너가 **비율(예 0.4 / 0.6)** 로 나눠 쓴다.
  (Backend.AI fGPU 모사 / 캡스톤 프로토타입)
- **방법**: `LD_PRELOAD`로 CUDA API를 후킹한 `libfgpu.so` 주입.
  - **메모리**: `cudaMalloc` 가로채 per-process 메모리 quota 강제.
  - **컴퓨트**: `cudaLaunchKernel` 가로채 **시분할(throttle)** — 비율만큼만 커널 통과. ← 오늘 개선 대상
- **제약**: MIG/SM 물리 분할 없음(4070 미지원). → 시간으로 나누는 **협조적 시분할**.

---

# 2. 기존 방식 — duty-cycle throttle

각 컨테이너가 **100ms 윈도우** 안에서 `compute_ratio × window` 만큼만 커널을 통과시키고
나머지는 `nanosleep`으로 대기 (커널을 버리진 않음).

```
A (ratio 0.4):  [■■■■····················]   40ms 통과 / 60ms 대기
B (ratio 0.6):  [■■■■■■■■■■■■············]   60ms 통과 / 40ms 대기
                 └ 각자 "자기 시계" 기준으로 윈도우 시작 → 타이밍이 어긋남(drift)
```

- 4:6 비율 분배는 잘 됨.
- **문제**: 상태가 per-process라 **상대 컨테이너의 타이밍을 모름** → 슬롯 위상이 제각각.

---

# 3. 문제 — 슬롯이 겹치면 GPU 노는 시간이 생긴다

타이밍이 어긋나면 A·B가 **동시에 GPU를 쓰려는 "겹침"** 구간이 생긴다.

```
이상적 (겹침 없이 번갈아)          어긋남 (duty-cycle 현실)
A: ■■■■········■■■■····        A: ■■■■········■■■■········
B: ····■■■■■■■■····■■■■        B: ··■■■■■■■■····■■■■■■■■··
   항상 한 작업이 GPU 사용          ↑↑          ↑↑   ← 겹침
   → GPU 거의 안 쉼               겹친 구간 = 두 작업이 GPU를 두고 충돌
                                 → 비효율 + GPU 노는 시간 발생
```

- 겹친 구간에선 두 작업이 같은 GPU를 두고 부딪혀 **비효율적으로 돌고, 노는 시간이 생긴다.**
- 그래서 "그냥 A 끝내고 B 하는 순차 실행"보다 **공유가 더 느려진다** = 오버헤드.

---

# 4. 해결 — anti-phase (시간 슬롯을 겹치지 않게)

**모든 컨테이너가 "절대 시계(벽시계)"로 윈도우 격자를 자동 동기화**하고,
각자 **겹치지 않는 시간 슬롯**에서만 커널을 통과시킨다.

```
W = 100ms 격자 (절대시각 기준 → 모든 컨테이너가 같은 격자 공유)
A (offset 0.0, ratio 0.4):  [■■■■····················]   슬롯 [0,   40ms)
B (offset 0.4, ratio 0.6):  [····■■■■■■■■■■■■········]   슬롯 [40, 100ms)
                             └ 겹침 0 → 항상 한 작업만 GPU 사용 → 충돌·노는시간 제거
```

- 벽시계로 자동 정렬되니 **런타임 통신 불필요** (매 윈도우 "지금 누구 차례?" 신호가 없다).
  - 필요한 조율은 **시작 시 offset 1회 배정뿐** — 그것도 admission이 이미 아는 정보(누가 ratio 얼마)로 누적합. 컨테이너끼리 대화 X.
- 합 = 1.0이면 빈틈없이 타일링.

---

# 5. 구현 — 같은 .so로 on/off, 이미지 재빌드 불필요

`hook/src/fgpu_hook.c`의 `cudaLaunchKernel` 경로에 알고리즘 분기 추가:

```c
phase = clock_gettime(CLOCK_REALTIME) % W;     // 현재 윈도우의 시작점부터 몇 ms 지났는지
s0 = offset*W;  s1 = (offset + ratio)*W;       // 내 슬롯 [s0, s1)
if (s0 <= phase && phase < s1)  pass;          // 내 슬롯 → 통과
else  nanosleep(다음 내 슬롯 시작까지);          // 아니면 대기
```

- 신규 env: `FGPU_THROTTLE_ALGO=antiphase`, `FGPU_COMPUTE_OFFSET` (A=0, B=0.4)
- 기존 duty-cycle 경로 보존 → **빌드 하나로 before/after 비교**.
- `.so`만 재빌드(컨테이너에 마운트되므로 이미지 재빌드 불필요).

---

# 6. 측정 — 핵심 결과 (Qwen2-1.5B, ratio 0.4/0.6)

| 지표 | duty-cycle (before) | **anti-phase (after)** |
|---|---|---|
| **GPU 노는 시간(idle)** | 47.2% | **41.0%** |
| **전체 실행시간(공유)** | 109.5 s | **80.8 s** (−26%) |
| 순차 실행(공유 안 함) | 52.2 s (기준) | 55.4 s (기준) |
| **순차 대비 오버헤드** | +57.3 s | **+25.4 s** (−56%) |
| a_share (목표 0.40) | 0.391 | **0.370** (4:6 유지) |
| 요청 지연 A / B | ×3.82 / ×2.41 | **×2.22 / ×1.71** |

→ 겹침을 없애니 **노는 시간↓, 전체 시간↓, 지연↓ — 분배는 그대로.**

<!--
순차 baseline이 before/after에서 52.2 vs 55.4로 약간 다른 건 매 측정마다 solo를 새로 재기 때문(동일 수준).
오버헤드는 각 측정 자신의 순차값 기준.
-->

---

# 7. 총시간 한눈에 (절대값)

| 시나리오 | 전체 시간 | 순차 대비 오버헤드 (초 / %) |
|---|---|---|
| **그냥 순차** (공유 없음, A 끝내고 B) | ≈52 s (기준) | — |
| 변경 전 (duty-cycle 공유) | 109.5 s | **+57.3 s / +110%** |
| 변경 후 (anti-phase 공유) | 80.8 s | **+25.4 s / +46%** |

- 오버헤드 **+57s → +25s**, 절대 **−32초**, 오버헤드 자체 기준 **−56%**.
- 순차 대비 손해율 **+110% → +46%**.

---

# 8. 4:6 분배는 안 깨졌다 (공정성 확인)

throttle 개선이 **분배를 희생하지 않았는가**가 핵심 검증.

| | a_share (목표 0.40 = 4:6의 A몫) |
|---|---|
| duty-cycle | 0.391 |
| anti-phase | 0.370 |

→ 둘 다 **≈0.4로 4:6 분배 유지.** anti-phase는 같은 분배를 **노는 시간 없이** 달성한 것이지
분배를 바꾼 게 아니다. (요청 지연도 A ×3.8→×2.2, B ×2.4→×1.7로 함께 감소)

---

# 9. 결론

1. **노는 시간 ↓** — 겹침을 없애 GPU idle **47% → 41%**.
2. **전체 시간 ↓** — 공유 실행시간 **109.5s → 80.8s (26% 빠름)**, 순차 대비 오버헤드 **−56%**.
3. **공정성 유지** — a_share ≈ 0.4, **4:6 분배 안 깨짐**.
4. **지연 ↓** — 요청당 지연도 함께 감소.

> **메시지**: 두 작업의 시간 슬롯을 **겹치지 않게 정렬**하기만 하면,
> 같은 4:6 분배를 **더 적은 낭비·더 짧은 시간**으로 달성한다.
> 메모리 quota + 컴퓨트 시분할 두 축을 **후킹 하나**로 구현.

---

# 10. 정직한 한계

- **워크로드 의존성**: 본 결과는 **GPU를 충분히 채우는 무거운 워크로드(큰 모델)** 기준.
  GPU가 한가한 **가벼운 워크로드**에선 겹침이 오히려 빈틈을 메워, 이득이 작거나 줄 수 있음
  (→ 공정 분배가 실제로 필요한 **포화 상황**의 개선으로 프레이밍).
- **물리 분할 아님**: 컴퓨트(SM)를 쪼개는 게 아니라 **시간으로 나누는** 협조적 방식.
  SM 격리는 MIG/MPS 영역(범위 밖).
- **근사 한계**: 슬롯 경계를 걸친 커널, `nanosleep` 정밀도(~50μs), 정적 링크 바이너리 우회 가능.

---

# 11. 재현

```bash
# 리눅스 GPU 호스트(WSL2 Ubuntu). .so만 재빌드
cd ~/GpuCluster && ./scripts/build_hook.sh

# before (duty-cycle) / after (anti-phase)
MODEL=Qwen/Qwen2-1.5B-Instruct IMAGE=fgpu-runtime-pytorch:stage4-infer \
  THROTTLE_MODE=conc THROTTLE_ALGO=dutycycle NITERS=12 bash scripts/eval/run_sharing.sh
MODEL=Qwen/Qwen2-1.5B-Instruct IMAGE=fgpu-runtime-pytorch:stage4-infer \
  THROTTLE_MODE=conc THROTTLE_ALGO=antiphase NITERS=12 bash scripts/eval/run_sharing.sh

# GPU idle 실측까지 같이 보려면:
MODEL=Qwen/Qwen2-1.5B-Instruct THROTTLE_ALGO=antiphase bash scripts/eval/run_gpuutil.sh
```

상세: `docs/results/2026-06-16_anti-phase-throttle-오버헤드개선-결과.md`

---

# 부록 — 원시 측정값 (Qwen2-1.5B, NITERS=12)

- **duty-cycle**: solo util ~60% / 공유 util 52.8%(idle 47.2%) / occupancy 0.641 / speedup 0.477
  / makespan seq 52.22s conc 109.53s / a_share 0.391
  - A: solo p50 2.18s → 공유 8.34s (×3.82, p95 8.59s) / B: 2.19s → 5.27s (×2.41)
- **anti-phase**: solo util ~55% / 공유 util 59.0%(idle 41.0%) / occupancy 0.961 / speedup 0.686
  / makespan seq 55.44s conc 80.83s / a_share 0.370
  - A: solo p50 2.46s → 공유 5.45s (×2.22, p95 5.54s) / B: 2.10s → 3.59s (×1.71)

> occupancy_sum = (공유 중 각 작업 처리량 ÷ 혼자일 때 처리량)의 합. useful 처리량 유지율 지표(1.0=낭비 0).
> idle = overlap 구간 nvidia-smi utilization.gpu 평균의 여집합(실측).
