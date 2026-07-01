# 실험 방법론 보강 계획 — overlap 구간 분석 + throttle 분해

**작성**: 2026-06-11 16:34 / **상태**: 계획 (코드·노트북 미수정) / **합의**: eval-benchmark-engineer · jupyter-session-engineer · gpu-throttle-perf-engineer

---

## 1. 문제 요약

경로 1 Jupyter 실험(세션 A `ratio 0.4`, B `ratio 0.6`, Qwen2-0.5B, `fgpu_infer.ipynb`)의
동시(concurrent) 시나리오에서 측정 오염 발견:

- A(0.4)가 B(0.6)보다 오래 걸려 **B가 먼저 끝남**.
- B가 끝나면 A가 갑자기 빨라짐(SM 경합 해소 = **drain 구간**).
- A의 `mean_tokens_per_s`는 **앞부분(경합·느림) + 뒷부분(단독·빠름)의 평균** → "공유 상태 A 처리량"이 오염됨.

**원인 2가지**
1. **throttle ON** — 세션 ratio가 메모리 quota뿐 아니라 duty-cycle `compute_ratio`로도 주입됨.
   A는 벽시계 40%만 커널 실행(설계상 느림). 화면 로그 `throttle=on compute_ratio=0.600` 확인.
2. **drain 구간** — 짧은 잡(B)이 끝나면 남은 잡(A)이 가속.

**실측 정량화(jupyter 전문가, 기존 CSV 분석)**: A overlap 구간(iter1-7) mean tps = **8.37**,
A drain 구간(iter9-20) mean tps = **17.76** → drain이 overlap 대비 **2.12배 빠름**.
오염된 overall mean(14.03)을 "concurrent A 성능"으로 보고하면 간섭 효과를 과소평가.
또한 B도 iter1-2가 A 시작 전(단독)이라 오염됨 → B overlap(iter3+) mean 16.67 vs B overall 17.72.

---

## 2. 확정 결정 (스코프 — 변경 불가)

1. **측정 대상 = 둘 다(메모리 fractional + 컴퓨트 throttle)** → **throttle는 켜둔다**.
   ratio가 메모리·컴퓨트 시간을 동시에 나누는 현재 동작 유지. 단, A가 느린 원인(throttle vs SM 경합)을
   **분해 해석**할 수 있어야 함.
2. **drain 해결 = overlap 구간만 분석** → 노트북 측정 루프는 그대로, **분석 단계에서 iter별 타임스탬프로
   overlap vs drain 구간을 잘라 throughput을 따로** 보고. 고정-시간 루프 전면 재작성은 **채택 안 함**.

---

## 3. 데이터 가용성 검증 결과 (file:line)

**결론: 재수집 불필요. 기존 CSV로 overlap 분석에 필요한 모든 데이터가 이미 저장됨.**

- `notebooks/fgpu_infer.ipynb` cell[8] 측정 루프: 각 iter마다
  `_t0_epoch = time.time()` / `_t1_epoch = time.time()`가 `model.generate()` 양끝을 감싸고
  `_iter_results.append((_t0_epoch, _t1_epoch, latency_s, gen_tokens, tps))`.
- `notebooks/fgpu_infer.ipynb` cell[10] CSV 저장: `/workspace/session_result_<SCENARIO>.csv`.
  컬럼(실제 확인) = `label, scenario, ratio, model, iter, **iter_start_epoch**, **iter_end_epoch**,
  latency_s, gen_tokens, tokens_per_s`.
  → 사용자가 말한 `start_epoch`/`end_epoch`의 **실제 컬럼명은 `iter_start_epoch`/`iter_end_epoch`**.
- `notebooks/fgpu_analysis.ipynb` cell[3]: `glob("**/session_result_*.csv")` + `read_csv` →
  `all_data` DataFrame(컬럼 보존, `session_id` 추가). **단 현재 분석은 메타 JSON의
  `mean_tokens_per_s`만 사용하고 iter별 타임스탬프는 활용 안 함** ← 여기가 보강 지점.

**시계 동기화 검증(jupyter 전문가)**
- `time.time()`(`CLOCK_REALTIME` epoch)은 컨테이너 간 직접 비교 가능. Docker가 `--unshare time`을
  하지 않는 한 모든 컨테이너가 호스트 `CLOCK_REALTIME` 공유(`clock_namespaces(7)`). 실측 A/B epoch 범위가
  직접 숫자 비교 가능함을 확인. `latency_s`(perf_counter)와 `iter_end-iter_start`(epoch) 차이 < 1e-6s.
- `perf_counter`(`CLOCK_MONOTONIC`)는 컨테이너마다 부팅 기준이 달라 크로스-컨테이너 비교 **불가** →
  반드시 epoch을 써야 함. ✅ 노트북은 이미 epoch 저장.

**측정 시작 기준점**: `iter1`의 `iter_start_epoch`가 곧 측정 시작 기준(= meta `t_start_epoch`와
차이 21μs). 별도 `t_start_epoch`를 분석에 쓸 필요 없음.

**실측 skew**: 수동 동시 Shift+Enter라 측정 루프 진입 skew = **13.1초**. B iter1-2는 A 시작 전 단독,
B iter3은 A 시작을 걸침. 이 때문에 overlap_start 정의가 필수(§4).

**선택적(결론 불변) 노트북 변경 2가지** — 분석 완결성만 향상, overlap 판별엔 불필요:
- cell[7] `_model_ready_epoch`를 meta JSON에 저장(현재 기록만 하고 미저장).
- 측정 루프 직전 `_warmup_end_epoch = time.time()` 한 줄 추가 + meta 저장.
→ M0(선택)로 분류. 기본은 **노트북 무변경**, 분석 셀만 추가.

---

## 4. overlap-window 분석 설계 (`fgpu_analysis.ipynb` 신규 셀)

### 4.1 시간 경계 정의
- `overlap_start = max(A 첫 iter_start_epoch, B 첫 iter_start_epoch)` — 둘 다 활성이 된 시점.
  (skew로 한쪽이 먼저 시작한 단독 구간을 경합에서 제외)
- `overlap_end   = min(A 마지막 iter_end_epoch, B 마지막 iter_end_epoch)` — 먼저 끝난 잡의 종료.

### 4.2 iter 3분류 (각 잡 독립 적용)
경계에 **완전히 내포된** iter만 구간에 귀속, 걸친 iter는 별도 `boundary`로 **제외**(시간비율 배분 안 함):
- `overlap` : `iter_start_epoch >= overlap_start` AND `iter_end_epoch <= overlap_end`
- `drain`   : `iter_start_epoch >= overlap_end`
- `boundary`: 위 둘에 안 들어가는 경계 걸친 iter (분석 제외, 각주로만 언급)

**배분 대신 제외를 택한 이유**: tps가 iter 내 균일하지 않음(throttle sleep이 비균등 분포).
선형 배분은 근사치이고 독자에게 "보간됨" 주석 부담만 늘림. iter ≥ 3 확보 시 제외가 깔끔.
→ 구간 iter < 3이면 summary에 **신뢰도 경고** 출력.

### 4.3 구간별 throughput 계산
**"구간 총 gen_tokens / 구간 wall-clock"** 사용(iter별 tps 산술평균 아님).
- 산술평균은 짧은/긴 iter에 동일 가중 → 저속 outlier에 왜곡.
- 처리량 정의:
  - `overlap_tps_X = sum(overlap iter gen_tokens) / (overlap_end - overlap_start)`
  - `drain_tps_X   = sum(drain iter gen_tokens) / (X 마지막 iter_end - overlap_end)`
  - 분모(overlap wall-clock)가 A·B 동일 시간 창 → 직접 비교 가능.

### 4.4 엣지케이스
- **한쪽 OOM(iter 0개)**: `max(iter_end)` = NaN → 해당 세션 overlap 분석 통째 제외,
  `SKIP: session X has 0 iters (OOM)` 출력. OOM은 admission/hook 층 결과로 별도 표 보고.
- **start skew**: overlap_start 정의가 흡수(단독 웜업 구간 배제).
- **boundary iter**: §4.2대로 제외.

---

## 5. 지표 재정의

| 지표 | 정의 | 비고 |
|---|---|---|
| `solo_tps_X` | 단독 실행 tps (throttle ON) | baseline. **throttle penalty 포함**(하드웨어 ceiling 아님) |
| `overlap_tps_X` | overlap 구간 tps | **"공유 상태 X 처리량" = 오염 제거된 지표** |
| `drain_tps_X` | drain 구간 tps (상대 종료 후, throttle 여전 ON) | 이상적으로 `≈ solo_tps_X` |
| `makespan_conc` | `max(A,B 마지막 iter_end) - min(A,B 첫 iter_start)` | **변경 불필요, 그대로 유효** |
| `makespan_seq` | `A solo 총시간 + B solo 총시간` | 변경 불필요 |
| `speedup` | `makespan_seq / makespan_conc` | **재정의 불필요**. 단 해석문 병기(>1 시간효율, <1 경합패널티) |
| `fairness_index`(신규) | `min(overlap_tps_A, overlap_tps_B) / max(...)` | ratio 0.4:0.6 → 이상치 ≈ 0.667. throttle 정밀도 직접 지표 |

**핵심**: "공유 상태 A 처리량" = **overlap_tps**(오염 제거). drain_tps는 별도 표기.
solo / overlap-contended / drain **3자 비교**가 메인 결과 표.

---

## 6. throttle 분해 해석

### 6.1 주입 경로 확인 (throttle 전문가, file:line)
- `backend/app/services/docker_manager.py` L99-101: `compute_ratio is not None`일 때만
  `FGPU_THROTTLE_ENABLE=1` + `FGPU_COMPUTE_RATIO=str(compute_ratio)` 주입.
- `backend/app/services/session_manager.py` L219: `compute_ratio=compute_ratio` 전달.
- `hook/src/fgpu_hook.c` L349-357: `FGPU_COMPUTE_RATIO` 미설정 시 `g_compute_ratio = g_ratio` 폴백.
- ⇒ **세션 ratio 0.4는 메모리 quota 0.4 AND 컴퓨트 duty 0.4로 같이 들어감**(단일 경로, 독립 파라미터 아님).
  로그 `compute_ratio=0.600` = B 컨테이너.

### 6.2 분해 논리 (합의 — 단, "하한"으로 표현)
- duty-cycle throttle은 **per-process 상태**(`g_window_start_ns`, `g_compute_ratio`)로 상대 컨테이너를
  전혀 모름. solo·overlap 모두 동일 throttle penalty가 곱해짐 → 비율 `overlap_tps/solo_tps`에서
  **throttle 효과 상쇄**, 남는 건 SM 경합 효과.
- **함정(비선형 항)**: solo에서 A가 sleep하는 동안 GPU idle, concurrent에서는 그 구간을 B가 가져가
  A가 깨어날 때 SM 큐 포화 → throttle-경합 상호작용이 가산법으로 완전 분리 안 됨.
  ⇒ `overlap_tps/solo_tps`는 **"SM 경합 효과의 하한"**으로 표현(상한은 현 방법론으로 측정 불가).
  Qwen2 추론 커널이 100ms window보다 짧아 이 항은 작다고 가정 가능 → 주석 명시.

### 6.3 인수분해
- (a) throttle 효과: `solo_tps_A / tps_unthrottled_A` ≈ `compute_ratio_A = 0.4`(이론) 또는 실측.
- (b) 순수 경합 효과(하한): `overlap_tps_A / solo_tps_A`.
- (c) 전체 degradation: `overlap_tps_A / tps_unthrottled_A = (a) × (b)`.
- `drain_tps_A ≈ solo_tps_A` 여부: 일치 시 "throttle stateless 작동(상대 존재가 window 상태 미오염)" 증거.

### 6.4 throttle OFF solo 보조 측정 (선택 — 스코프 외 참고 데이터)
- 이론 예측 `tps_throttled ≈ tps_unthrottled × compute_ratio`는 `run_throttle.sh`가 noop 커널로
  ±0.15 검증 완료. **슬라이드 수준이면 이 PASS 결과 인용으로 충분**.
- Qwen2 실 워크로드(커널 길이 가변)에서 (a)를 직접 실측하려면 "throttle OFF solo" 1회 추가가
  논증 강화. **논문 figure에 분해 포함 시 권장, 발표만이면 생략**. → M3(선택)로 분류.

### 6.5 한 줄 표현(슬라이드/논문)
> "A(ratio 0.4)의 throughput 저하 = duty-cycle throttle 설계상 40% 제한(`solo_A/baseline ≈ 0.40`)
> × SM 경합 추가 저하(`overlap_A/solo_A ≈ X`)로 인수분해. (throttle-경합 비선형 항은 미분리 — 측정 한계)"

---

## 7. 시각화 (`fgpu_analysis.ipynb` 신규/확장 셀)

1. **(1순위) overlap/drain 음영 타임라인** — 기존 `gpu_util_timeline`(cell[8]) 확장.
   x=wall-clock(s, 공통 t0), y=롤링 tps(1~2 iter 윈도우), A·B 선.
   `axvspan(overlap_start, overlap_end, alpha=.15, color='red', label='overlap')`,
   `axvspan(overlap_end, max_end, alpha=.1, color='blue', label='drain')`.
   → 오염 문제 + 해결법을 한 그림에 설명. gpu_util 오버레이 시 SM util↔tps 상관도 함께.
2. **(2순위) 구간별 tps 그룹 막대** — x={A,B}, hue={solo, overlap, drain}, y=tps.
   solo→overlap 하락, overlap→drain 회복을 한눈에. 슬라이드 메인.
3. **(3순위, 선택) fairness scatter** — x=ratio, y=overlap_tps, 점 2개 + 이상 선형 기준선.
   `run_throttle.sh` 결과와 연결 → "메모리 quota 실험 + throttle 정밀도 실험" 통합.
- **피할 것**: 전체 iter tps 단일 boxplot(overlap/drain 섞여 bimodal → 중앙값 무의미).

---

## 8. 슬라이드·문서 영향 (`docs/presentation/build/build_deck.js`)

| 슬라이드 | 현재 서술(line) | 보강 |
|---|---|---|
| ④ 방법 (L78-96) | "perf_counter → latency·makespan / tokens/sec / 동일조건 상대비교"(L92-93,96) | **"동시 측정은 overlap 구간만(drain 분리)"** 1줄. throttle ON 명시(메모리+컴퓨트 동시 분할) 1줄 |
| ⑤ 시나리오 (L101-105) | conc "자원을 공간(메모리)으로 나눔"(L105) | conc 설명에 **"throttle로 컴퓨트도 분할 → tps는 overlap 구간 기준 보고"** 추가 |
| ⑥ 결과① (L124-131) | speedup/tokens/sec/makespan 빈칸(L131) | tokens/sec 칸 캡션을 **"overlap tps(공유 상태)"**로, 별도 drain 표기. 3자 비교 표 슬롯 |
| ⑦ 결과② util 타임라인 (L139-146) | "util 겹치는 구간 = SM 경합/idle 채움"(L143) | **overlap/drain 음영 + throttle 분해(throttle 40% × 경합 X)** 캡션 1줄 |
| ⑧ 결론/향후 (L155-159) | "compute throttle 적용"(L159, 향후로 표기) | throttle는 **이미 적용됨**으로 정정, 한계에 **"throttle-경합 비선형 항 미분리"** 추가 |

문서 반영: `description.md`/`CLAUDE.md`에 overlap 분석 방법론 1단락(docs-paper-writer).

---

## 9. 에이전트 역할 배분

| 작업 | 담당 |
|---|---|
| overlap 분석 셀 설계·구현(`fgpu_analysis.ipynb`), 구간 분류·지표·CSV·그래프 | **eval-benchmark-engineer** |
| (선택 M0) `fgpu_infer.ipynb` 보조 타임스탬프 저장, CSV 영속 검증 | **jupyter-session-engineer** |
| throttle 분해 해석 검수, (선택 M3) throttle OFF solo 보조 측정 정의 | **gpu-throttle-perf-engineer** |
| overlap/drain 분류 로직 단위 테스트(합성 타임스탬프) | **test-qa-engineer** |
| deck(④⑤⑥⑦⑧) 서술 + description.md 반영 | **docs-paper-writer** |

---

## 10. 단계 (마일스톤)

- **M1 (핵심)**: `fgpu_analysis.ipynb`에 overlap-window 분석 셀 추가
  — 경계 계산 → 3분류 → overlap/drain/solo tps → runs.csv 컬럼 확장 → 신뢰도 경고.
  (노트북 무변경 + 기존 CSV로 검증 가능)
- **M2**: 시각화 셀 — 음영 타임라인 + 구간별 그룹 막대(+ fairness scatter).
- **M3 (선택)**: throttle OFF solo 1회 보조 측정으로 (a) throttle penalty 실측 — 논문 figure용.
- **M0 (선택)**: `fgpu_infer.ipynb`에 `_warmup_end_epoch`/`_model_ready_epoch` meta 저장(완결성).
- **M4**: deck/문서 서술 반영.
- 각 마일스톤은 기존 저장 CSV(A=`bfb376ebe348`, B=`4ed5ec3597bb`)로 단독 검증 가능.

---

## 11. 변경 예정 파일

| 파일 | 변경 | 마일스톤 |
|---|---|---|
| `notebooks/fgpu_analysis.ipynb` | overlap 분석 셀 + 시각화 셀 신규. cell[8] 타임라인 음영 확장. runs.csv 컬럼 추가(`overlap_tps_a/b`, `drain_tps_a/b`, `solo_tps_a/b`, `fairness_index`, `n_overlap_iters`, `n_drain_iters`, `n_boundary_iters`) | M1, M2 |
| `notebooks/fgpu_infer.ipynb` | (선택) `_warmup_end_epoch` 한 줄 + meta JSON 키 2개 | M0 |
| `backend/tests/test_overlap_classify.py` (신규) | overlap/drain/boundary 분류 순수함수 단위 테스트 | M1 |
| `docs/presentation/build/build_deck.js` | ④⑤⑥⑦⑧ 슬라이드 서술 | M4 |
| `description.md` / `CLAUDE.md` | 방법론 1단락 | M4 |
| `scripts/eval/run_throttle.sh` 또는 신규 보조 스크립트 | (선택) throttle OFF solo penalty 측정 | M3 |

**주의**: overlap 분류 로직은 **노트북 셀 인라인이 아니라 import 가능한 순수함수**(예:
`scripts/eval/_overlap.py` 또는 노트북이 `%run`/`import`하는 헬퍼)로 두어야 test-qa가 단위 테스트 가능.
→ M1에서 헬퍼 모듈 위치 확정 필요(권장: `scripts/eval/_overlap.py`, stdlib만, `_correlate.py` 패턴 따름).

---

## 12. 테스트 계획 (test-qa-engineer)

**`backend/tests/test_overlap_classify.py`** — 합성 타임스탬프로 경계 검증(docker/GPU 불필요):
1. **기본 분류**: A·B iter 리스트 합성 → overlap/drain/boundary 카운트가 기대치와 일치.
2. **overlap_start skew**: B가 A보다 먼저 시작(단독 iter 2개) → 그 2개가 overlap에서 제외됨.
3. **overlap_end 경계**: A의 마지막 iter가 overlap_end 걸침 → boundary로 분류(overlap/drain 둘 다 아님).
4. **boundary 제외**: 경계 걸친 iter가 overlap/drain 어느 합산에도 미포함(시간비율 배분 안 함 확인).
5. **OOM(iter 0개)**: 한쪽 iter 0개 → SKIP 반환, NaN 미전파.
6. **throughput 정의**: `sum(gen_tokens)/wall-clock`이 기대 tps와 일치(산술평균과 다름을 보이는 케이스).
7. **신뢰도 경고**: overlap iter < 3 → 경고 플래그 set.
8. **fairness_index**: overlap_tps_A/B 주입 → `min/max` 계산 정확성, ratio 0.4:0.6 이상치 0.667 근접.
9. **회귀**: 기존 `mean_tokens_per_s`(오염) vs `overlap_tps`(정제)가 합성 데이터에서 다른 값임을 단언.

**검증 데이터셋**: 실제 A=`data/sessions/bfb376ebe348`, B=`data/sessions/4ed5ec3597bb`의
`session_result_concurrent.csv`로 분석 셀 실행 시 A overlap≈8.37 / drain≈17.76 재현 확인(스모크).
