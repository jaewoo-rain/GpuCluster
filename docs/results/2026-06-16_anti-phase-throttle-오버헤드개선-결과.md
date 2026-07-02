# Anti-phase throttle — 오버헤드 개선 결과 (측정 ②)

**측정일**: 2026-06-16 ~ 17 / **환경**: RTX 4070 12GB (WSL2 Ubuntu, Docker), caching ON
**주 측정 모델**: Qwen2-1.5B-Instruct (GPU를 충분히 채우는 현실적 큰 모델) / **regime 확인**: Qwen2-0.5B-Instruct
**계획 문서**: [docs/plan/2026-06-16_anti-phase-throttle-오버헤드개선.md](../plan/2026-06-16_anti-phase-throttle-오버헤드개선.md)
**선행 결과**: [docs/results/2026-06-16_fGPU공유-재실험-종합결과.md](./2026-06-16_fGPU공유-재실험-종합결과.md)

---

## 0. TL;DR

> **duty-cycle throttle은 두 컨테이너의 시간 슬롯 위상이 제각각이라, A·B가 동시에 GPU를 쓰려는 "겹침" 구간이 생긴다.
> 그 구간은 비효율적으로 돌며 GPU 노는 시간을 만든다. 윈도우 경계를 절대시각(CLOCK_REALTIME)으로 자동 동기화하고
> 각 컨테이너에 겹치지 않는 슬롯을 배정(anti-phase)하면 그 겹침을 없애 노는 시간과 전체 시간을 줄인다.**

**Qwen2-1.5B, ratio 0.4/0.6, before → after** (실측):
- **GPU 노는 시간(idle, nvidia-smi 실측): 47.2% → 41.0%**
- **전체 실행시간(공유): 109.5s → 80.8s (−26%)**, speedup 0.477 → 0.686
- **순차 대비 오버헤드: +57.3s → +25.4s (−56%)**
- **a_share 0.391 → 0.370 — 4:6 분배 유지** (공정성 안 깨짐)
- 요청당 지연: A ×3.82→×2.22, B ×2.41→×1.71
- occupancy_sum 0.641 → 0.961

핵심 주장: *throttle을 "더 잘 만든 것"* — duty-cycle도 anti-phase도 둘 다 4:6을 강제하지만,
anti-phase는 같은 분배를 **겹침으로 인한 낭비 없이** 더 짧은 시간에 달성한다.

> ⚠ 중요(§3.1): 이 이득은 **GPU가 충분히 채워지는 무거운 워크로드(큰 모델)** 에서 성립한다.
> GPU가 한가한 가벼운 워크로드(0.5B)에선 겹침이 오히려 빈틈을 메워, anti-phase의 idle 이득이 사라지거나 역전된다.

---

## 1. 무엇을 바꿨나 (구현)

`hook/src/fgpu_hook.c`의 `cudaLaunchKernel` throttle 경로에 **두 번째 알고리즘**을 추가.
기존 duty-cycle 경로는 그대로 두고 env 로 분기 → **같은 .so 하나로 before/after 비교**.

- 신규 env:
  - `FGPU_THROTTLE_ALGO = dutycycle`(기본) | `antiphase`
  - `FGPU_COMPUTE_OFFSET = double`(기본 0.0) — anti-phase 슬롯 시작 오프셋
- anti-phase 게이팅:
  ```
  W      = window (기본 100ms)
  s0,s1  = [offset·W, (offset+ratio)·W)         # 내 슬롯
  phase  = clock_gettime(CLOCK_REALTIME) % W    # 절대시각 → 컨테이너 공통 격자
  if s0 ≤ phase < s1:  통과
  else:                내 다음 슬롯 시작까지 nanosleep
  ```
- **윈도우 동기화 = 런타임 조율 불필요의 열쇠**: `phase`를 절대시각으로 계산하므로 컨테이너들이 **런타임 통신 없이**
  같은 윈도우 경계를 공유한다(Docker time namespace 미사용 전제, 본 프로젝트 충족). 매 윈도우 "지금 누구 차례?" 신호가 없다.
  - 필요한 조율은 **시작 시 offset 1회 배정뿐**(컨테이너끼리 대화 X). 그것도 admission(Stage 11)이 이미 아는
    "누가 ratio 얼마"를 누적합하면 되므로 새 인프라 0. 즉 "끊임없는 런타임 통신"을 "시작 시 1회 배정"으로 옮긴 것.
- 슬롯 타일링: A(ratio 0.4, offset 0.0)→`[0,40ms)`, B(ratio 0.6, offset 0.4)→`[40,100ms)` → 매 순간 최대 1명만 활성.

하니스(`run_sharing.sh` / `run_sweep.sh` / `run_gpuutil.sh`)에 `THROTTLE_ALGO` 전달 + concurrent 오프셋 주입(A=0, B=RATIO_A).

init 로그로 동작 확인:
```
[fgpu] init: throttle=on algo=antiphase compute_ratio=0.400 offset=0.000 window_ms=100
```

---

## 2. 측정 — matched before/after (Qwen2-1.5B, NITERS=12, ratio 0.4/0.6)

같은 GPU·같은 모델에서 `THROTTLE_ALGO`만 바꿔 측정. **occupancy(throughput 프록시)뿐 아니라
nvidia-smi utilization.gpu 를 100ms 간격으로 캡처해 overlap 구간의 실측 GPU idle 도 함께 산출.**

| 지표 | duty-cycle (before) | anti-phase (after) | 변화 |
|---|---|---|---|
| **GPU idle (실측, overlap 구간)** | 47.2% | **41.0%** | **−6.2%p ↓** (노는 시간 줄임) |
| 공유 중 GPU util (실측) | 52.8% | **59.0%** | +6.2%p ↑ |
| **makespan_conc (공유 총시간)** | 109.5 s | **80.8 s** | **−26%** |
| makespan_seq (순차, 공유 안 함) | 52.2 s | 55.4 s | ≈ 기준선 |
| **순차 대비 오버헤드 (conc−seq)** | +57.3 s | **+25.4 s** | **−56%** |
| speedup (seq/conc) | 0.477 | **0.686** | ↑ |
| occupancy_sum | 0.641 | **0.961** | ↑ |
| **a_share (목표 0.40)** | 0.391 | **0.370** | **≈4:6 유지** ✓ |
| 요청 지연 A / B (vs solo) | ×3.82 / ×2.41 | **×2.22 / ×1.71** | 감소 |
| A 공유 p95 | 8.59 s | **5.54 s** | tail 축소 |

- solo(throttle OFF) 기준 GPU util ≈ 60% (1.5B는 GPU를 상당히 채운다 — 0.5B의 ~40%와 대비).
- 산출물: `experiments/gpuutil_q15_dutycycle/`, `experiments/gpuutil_q15_antiphase/`
  (각 `gpuutil_report.txt` = 실측 idle, `sharing/overlap_report.txt` = occupancy/makespan/a_share)

### 판정
1. **GPU idle 감소** (47.2%→41.0%, 실측) ✓
2. **오버헤드 감소** (makespan −26%, 오버헤드 −56%) ✓
3. **a_share 여전히 ratio 추종** (0.37~0.39 ≈ 4:6) ✓

→ 셋 다 충족. **PASS.** 큰 모델에선 occupancy·실측 util·makespan·idle 이 **모두 같은 방향**으로 개선.

---

## 3. 해석

- duty-cycle은 per-process 윈도우라 상대 컨테이너의 슬롯 위상을 모른다 → A·B가 **동시에 GPU를 쓰려는 겹침** 발생.
- GPU는 두 프로세스를 동시에 병렬로 못 돌리고(MIG/MPS 없음) **시분할로 번갈아** 처리한다.
  겹침 구간에선 두 컨테이너 사이를 오가는 **context switch + 캐시/대역폭 간섭**으로 비효율 → useful 처리량↓, 노는 시간↑.
- anti-phase는 절대시각 격자에서 슬롯을 타일링해 겹침을 제거 → 매 순간 한 작업만 GPU 사용 →
  큰 모델에선 그 한 작업이 GPU를 채우므로 **idle↓·util↑·makespan↓**, 같은 4:6 분배를 더 짧은 시간에 달성.
- **분배(a_share)는 두 방식이 동일** — anti-phase는 공정성을 깨지 않고 **겹침 낭비만 줄인** 개선이다.

### 3.1 ★ Regime 의존성 — 실측 GPU util 로 검증한 핵심 (정직성)

occupancy_sum 은 "혼자일 때 속도를 공유 중 몇 % 유지했나"의 **throughput 프록시**일 뿐, GPU 유휴율 자체가 아니다.
이를 확인하려 **nvidia-smi utilization.gpu 를 직접 측정**했더니, **워크로드 크기에 따라 anti-phase 의 idle 효과가 뒤집힌다:**

| 워크로드 | solo GPU util | duty idle | anti idle | **anti의 idle 페널티** |
|---|---|---|---|---|
| 0.5B (가벼움, GPU 미포화) | ~40% | 55.7% | 61.5% | **+5.8%p (anti 손해)** |
| 0.5B batch16 | ~41% | 58.3% | 59.8% | +1.5%p (거의 동률) |
| **1.5B (무거움, GPU 충분히 참)** | **~60%** | 47.2% | **41.0%** | **−6.2%p (anti 이득)** |

- **작은 모델**: GPU에 빈틈이 많아, duty-cycle 의 겹침이 그 빈틈을 **메워** util↑. anti-phase는 직렬화로 이를 포기 → idle↑.
  (이 경우 anti-phase 의 이득은 idle 이 아니라 "겹침의 전환 오버헤드 감소"에서 오며, occupancy↑·makespan↓로는 여전히 개선)
- **큰 모델**: GPU에 빈틈이 거의 없어 겹침은 메울 게 없고 **순수 쟁탈(손해)뿐** → duty-cycle util이 solo(60%)보다 떨어짐(52.8%).
  anti-phase는 한 큰 작업이 슬롯을 채워 **idle↓·util↑·makespan↓** — 모든 축에서 이득.
- **결론**: anti-phase 의 진짜 무대는 **GPU가 포화에 가까운 무거운 워크로드**. 공정 분배가 실제로 필요한 상황이 대개 이쪽이다.
- 산출물: `experiments/gpuutil_{antiphase,dutycycle}/`(0.5B), `experiments/gpuutil_b16_*`(batch16), `experiments/gpuutil_q15_*`(1.5B).

### 3.2 (참고) 0.5B n=3 스윕 — 변동성 + 결정론성

작은 모델(0.5B, NITERS=12, n=3)에서도 occupancy·overhead·a_share 는 anti-phase 가 개선:
speedup 0.622±0.045 → 0.653±0.019, occupancy 0.797±0.071 → 0.882±0.034, a_share 0.383 → 0.375.
**부수 효과**: anti-phase 는 모든 지표에서 **편차가 더 작다**(슬롯 동기화가 sleep 위상 우연성 제거 → 더 결정론적·재현 가능).

---

## 4. 정직한 한계

- **워크로드 의존성(§3.1)**: 본 헤드라인 이득은 **GPU를 충분히 채우는 무거운 워크로드(큰 모델)** 기준.
  가벼운 워크로드(작은 모델/낮은 배치)에선 겹침이 빈틈을 메워 anti-phase 의 idle 이득이 사라지거나 역전될 수 있다.
- **시간 분할이지 공간 분할 아님**: 컴퓨트(SM)를 물리적으로 쪼개는 게 아니다(MIG/MPS 미사용).
  GPU 코어 자체의 유휴(작은 모델이 코어를 다 못 채움)는 이 방식으로 못 줄인다 — 그건 MPS(공간 공유) 영역.
  anti-phase 가 줄이는 건 "겹침으로 인한 시분할 비효율"이다.
- **직렬화의 대가**: anti-phase 는 컴퓨트를 번갈아 돌린다 → no-throttle(분배 보장 없음, 선행 결과 speedup 1.2~1.45)처럼
  겹침으로 빈틈을 메워 얻는 이득은 포기. 비교 축은 **duty-cycle vs anti-phase**(둘 다 4:6 강제) — "throttle을 더 잘 만든 것".
- **근사 한계**: 슬롯 경계를 걸친 커널(spillover), `nanosleep` 정밀도(~50μs), launch count ≠ device-time,
  정적 링크 바이너리 우회 가능 — 기존과 동일.
- 합 < 1.0이면 `[sum,1)` 구간은 의도된 idle(예약했으나 안 씀). work-conserving 은 별도 후속. 본 실험(합=1.0)에선 틈 없음.

---

## 5. 재현

```bash
# 리눅스 GPU 호스트 (WSL2 Ubuntu). build/libfgpu.so 는 anti-phase 반영된 새 .so
cd ~/GpuCluster && ./scripts/build_hook.sh          # 컨테이너 빌드(호스트 CUDA 없으면 자동)

# before (duty-cycle) / after (anti-phase) — 큰 모델
MODEL=Qwen/Qwen2-1.5B-Instruct IMAGE=fgpu-runtime-pytorch:stage4-infer \
  THROTTLE_MODE=conc THROTTLE_ALGO=dutycycle NITERS=12 bash scripts/eval/run_sharing.sh
MODEL=Qwen/Qwen2-1.5B-Instruct IMAGE=fgpu-runtime-pytorch:stage4-infer \
  THROTTLE_MODE=conc THROTTLE_ALGO=antiphase NITERS=12 bash scripts/eval/run_sharing.sh

# 실측 GPU idle 까지 함께 (occupancy 프록시 vs 실측 util 검증)
MODEL=Qwen/Qwen2-1.5B-Instruct THROTTLE_ALGO=antiphase NITERS=12 bash scripts/eval/run_gpuutil.sh
# → experiments/gpuutil_*/gpuutil_report.txt (실측 idle) + sharing/overlap_report.txt (occupancy/makespan)
```

> ⚠ 빌드 주의: 호스트(WSL)에 CUDA 툴킷이 있으면 `build_hook.sh`가 네이티브 빌드를 타서 호스트 glibc(2.39)에
> 링크된 `.so`가 나오고, 이는 runtime 이미지(ubuntu22.04, glibc 2.35)에서 `GLIBC_2.38 not found`로 실패한다.
> 호스트 CUDA가 없으면 자동으로 컨테이너 빌드(glibc 2.34 호환)를 탄다. 강제하려면 `FGPU_FORCE_CONTAINER_BUILD=1`.
