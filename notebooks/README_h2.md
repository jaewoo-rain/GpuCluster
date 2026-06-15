# H2 상보 워크로드 공유 이득 실험 — 실행 가이드

**실험 목표**: compute-heavy(LLM generate) + idle-heavy(텐서 상주 + 간헐 matmul + sleep) 워크로드를
동시 공유했을 때 **speedup ≥ 1.1** 또는 **A의 처리량 유지율 ≥ 0.80** 달성 여부 실측.

> 기존 `fgpu_infer.ipynb` / `fgpu_analysis.ipynb` 는 **수정하지 않는다.**  
> 이 실험은 **`fgpu_infer_h2.ipynb`** 를 사용한다. 분석은 `fgpu_analysis.ipynb` 를 재사용 가능  
> (epoch 타임스탬프 기반이라 SCENARIO만 다르면 동일 로직 적용, 단 `workload` 컬럼이 추가됨).

---

## H1과의 차이

| 항목 | H1 | H2 |
|---|---|---|
| 워크로드 | 양쪽 compute-heavy (LLM generate) | A=compute-heavy, B=idle-heavy |
| throttle | ON (`compute_ratio=0.4`) | **OFF** (`compute_ratio` 미포함) |
| 시나리오 이름 | `"concurrent"` | `"h2_sharing"` |
| 성공 기준 | speedup 관측 | overlap_tps_A/solo ≥ 0.80 (1순위) |

---

## 1. 사전 조건

- 백엔드 실행 중: `./scripts/run_backend.sh` (포트 8000)
- 이미지 빌드 완료: `fgpu-runtime-pytorch:stage4` (jupyterlab 포함)
- Stage 11 admission control 활성 (합=1.0 초과 409 반환)

---

## 2. throttle OFF 세션 생성법 (H2 핵심)

### throttle ON/OFF 메커니즘 (코드 근거)

`backend/app/services/docker_manager.py` 98-101행:

```python
if compute_ratio is not None:
    env["FGPU_THROTTLE_ENABLE"] = "1"
    env["FGPU_COMPUTE_RATIO"] = str(compute_ratio)
```

`backend/app/schemas/session.py` 51-55행:

```python
compute_ratio: Optional[float] = Field(
    default=None, gt=0.0, le=1.0,
    description="Stage 12: ... None 이면 throttle off.",
)
```

**결론**: `compute_ratio` 필드를 POST body에 **포함하지 않으면** 기본값 `None` →
`FGPU_THROTTLE_ENABLE` 환경변수가 컨테이너에 주입되지 않음 → hook 기본값 throttle OFF.

### curl로 세션 생성 (throttle OFF)

```bash
# 세션 A — compute-heavy (ratio=0.5, throttle OFF)
curl -X POST http://localhost:8000/sessions \
  -H 'Content-Type: application/json' \
  -d '{"ratio": 0.5, "mode": "jupyter", "image": "fgpu-runtime-pytorch:stage4"}'

# 응답: {"id": "<id_A>", "host_port": 49XXX, "jupyter_token": "...", ...}

# 세션 B — idle-heavy (ratio=0.5, throttle OFF)
curl -X POST http://localhost:8000/sessions \
  -H 'Content-Type: application/json' \
  -d '{"ratio": 0.5, "mode": "jupyter", "image": "fgpu-runtime-pytorch:stage4"}'

# admission 확인 (합=1.0 → 통과)
curl http://localhost:8000/sessions/admission
# 기대: {"by_gpu": {"all": {"ratio_used": 1.0, "ratio_available": 0.0, ...}}}
```

> **중요**: `compute_ratio` 필드를 JSON에 절대 포함하지 않는다.  
> `"compute_ratio": null` 도 명시 포함하면 안전하지만, 생략이 가장 명확하다.

### Web UI로 만들 때 주의점

1. Create 폼에서 `ratio=0.5`, `mode=jupyter`, `image=fgpu-runtime-pytorch:stage4` 입력
2. **compute_ratio 입력란은 비워둔다** (입력하면 throttle이 켜진다)
3. 세션 생성 후 노트북 환경 확인 셀(cell-3)에서 `FGPU_THROTTLE_ENABLE=<unset>` 반드시 검증

---

## 3. 세션 생성 후 throttle OFF 검증

각 Jupyter 세션에서 `fgpu_infer_h2.ipynb` 를 열고 **cell-3(환경 출력 셀)** 만 먼저 실행:

```
FGPU_THROTTLE_ENABLE      : <unset>   ← 반드시 이 값이어야 함
FGPU_COMPUTE_RATIO        : <unset>   ← 반드시 이 값이어야 함
```

만약 `FGPU_THROTTLE_ENABLE=1` 이 보이면 세션을 삭제하고 `compute_ratio` 없이 재생성.

---

## 4. 실행 절차

### M0 — 사전 검증 (B idle 워크로드 solo 확인)

B solo를 먼저 돌려 `gpu_util_timeline` 에서 sleep 골(util≈0)이 충분히 깊은지 확인.

1. B 세션 노트북 config 셀:
   ```python
   WORKLOAD   = "memory_idle"
   SESSION_LABEL = "B"
   SCENARIO   = "solo"
   RATIO      = 0.5
   IDLE_SLEEP_S = 8.0
   ```
2. `Kernel → Restart & Run All`
3. 측정 완료 후 메타 JSON에서 `gpu_util_timeline` 확인:
   - sleep 구간(`IDLE_SLEEP_S=8s`) 동안 util≈0 이어야 함
   - util이 항상 높으면 `IDLE_SLEEP_S` 를 늘리거나 `MATMUL_DIM` 을 줄임

### M1 — solo 기준선 수집

**A(compute) solo**:

```python
WORKLOAD      = "compute"
SESSION_LABEL = "A"
SCENARIO      = "solo"
RATIO         = 0.5
```

`Kernel → Restart & Run All` → 완료 후 결과 파일 즉시 백업 (5단계 참고).

`solo_tps_A` = 요약 출력의 `mean tok/s` 값 기록.

**B(memory_idle) solo** (M0과 동일, 이미 수행했으면 재사용 가능):

```python
WORKLOAD      = "memory_idle"
SESSION_LABEL = "B"
SCENARIO      = "solo"
RATIO         = 0.5
```

`Kernel → Restart & Run All` → 완료 후 즉시 백업.

`B cycle_time(solo)` = 메타 JSON의 `cycle_time_s` 값 기록.

### M2 — h2_sharing 동시 수집 (수동 위상차)

> H2의 배리어는 파일 플래그 대신 **수동 타이밍**으로 대체한다.  
> 두 컨테이너의 `/workspace` 가 분리되어 있어 플래그 파일 공유 불가.

**순서**:

1. A 노트북 config 셀 변경:
   ```python
   WORKLOAD   = "compute"
   SESSION_LABEL = "A"
   SCENARIO   = "h2_sharing"
   ```
2. **A 먼저 `Kernel → Restart & Run All`**
3. A의 cell-6(모델 로드) 완료 + warmup 시작 로그 확인
   - 출력 예: `[h2-infer] 워밍업 1회 완료...`
   - 워밍업 3회 ≈ 30~45s 소요
4. **A warmup 완료 로그 `[h2-infer] 워밍업 3회 완료. ★ 이 시점 직후 B 세션 Run All`** 직후
   B 세션으로 즉시 전환 → `Kernel → Restart & Run All`
5. A 본 측정 20회 전체(≈120~300s) 동안 B가 상주 + burst/sleep 루프 수행
6. 둘 다 완료 후 결과 파일 즉시 백업

B config 셀:
```python
WORKLOAD      = "memory_idle"
SESSION_LABEL = "B"
SCENARIO      = "h2_sharing"
RATIO         = 0.5
N_ITERS_IDLE  = 20          # A 총 시간 / cycle_time 보다 크게 설정
IDLE_SLEEP_S  = 8.0
```

> B의 `N_ITERS_IDLE × (IDLE_SLEEP_S + burst)` ≥ A의 총 측정 시간 이어야 A 전체에서 B 상주.  
> A makespan ≈ 200s, cycle_time ≈ 8.05s → N_ITERS_IDLE=25 이상 권장.  
> 넉넉하게 `N_ITERS_IDLE=30` 설정 권장 (측정 후 epoch 기준으로 overlap 구간 자동 산출됨).

---

## 5. 데이터 안전 — 즉시 호스트 백업 (필수)

> `.gitignore` 가 `experiments/` 와 `data/` 를 **모두 제외**한다.  
> git 이 지켜주지 않으므로 **호스트 복사가 유일한 안전장치**.  
> 세션 workspace purge 절대 금지.

### 백업 명령

```bash
TS=$(date +%Y-%m-%d_%H-%M-%S)
DEST=/home/jaewoo/Desktop/backend_ai/GpuCluster/experiments/sharing_results/h2_${TS}
mkdir -p "$DEST"

# 각 세션 id 확인: curl http://localhost:8000/sessions | python3 -m json.tool
ID_A=<세션_A_id>
ID_B=<세션_B_id>

# solo 결과
cp data/sessions/${ID_A}/session_result_solo.csv    "$DEST/A_solo.csv"
cp data/sessions/${ID_A}/session_meta_solo.json      "$DEST/A_solo_meta.json"
cp data/sessions/${ID_B}/session_result_solo.csv    "$DEST/B_solo.csv"
cp data/sessions/${ID_B}/session_meta_solo.json      "$DEST/B_solo_meta.json"

# h2_sharing 결과
cp data/sessions/${ID_A}/session_result_h2_sharing.csv  "$DEST/A_h2.csv"
cp data/sessions/${ID_A}/session_meta_h2_sharing.json   "$DEST/A_h2_meta.json"
cp data/sessions/${ID_B}/session_result_h2_sharing.csv  "$DEST/B_h2.csv"
cp data/sessions/${ID_B}/session_meta_h2_sharing.json   "$DEST/B_h2_meta.json"

echo "백업 완료: $DEST"
ls "$DEST"
```

### 세션 삭제 시 주의

```bash
# 안전 (workspace 보존, 기본값)
curl -X DELETE "http://localhost:8000/sessions/${ID_A}"

# 절대 금지 — workspace 영구 삭제
# curl -X DELETE "http://localhost:8000/sessions/${ID_A}?purge_workspace=true"
```

---

## 6. 분석

### fgpu_analysis.ipynb 재사용

`fgpu_analysis.ipynb` 는 `iter_start_epoch`/`iter_end_epoch` 기반이라  
SCENARIO만 `"h2_sharing"` 으로 다르면 동일 로직으로 speedup/makespan 산출 가능.

단, H2 CSV에는 `workload` 컬럼이 추가되어 있다. 기존 분석 노트북이 이 컬럼을 모르면
무시하고 처리하므로 호환성 파괴 없음. overlap 3구간(solo/overlap/drain) 분리 로직도 동일하게 적용.

### 성공 기준 (H2 판정)

| 순위 | 지표 | 기준 | 비고 |
|---|---|---|---|
| 1순위 (필수) | `overlap_tps_A / solo_tps_A` | ≥ 0.80 | A의 처리량 유지율 |
| 2순위 (강한 주장) | speedup = 순차 makespan / 동시 makespan | ≥ 1.1 (stretch: 1.2) | overlap 구간 기준만 |
| 3순위 (증거) | overlap 평균 gpu_util 합산 | < 95% | B sleep 구간 여유 용량 |

1순위 충족 필수. 1+2 또는 1+3 충족 시 "H2 공유 이득 관측" 보고.

**판정 주의**: speedup 단독 판정 금지 (B iter 수 조정으로 임의 변동 가능).

---

## 7. 한계 (논문 정직성)

- SM 격리 없음: 두 컨테이너 동일 SM 자유 경쟁. speedup은 SM idle 시간 활용 여부에만 의존.
- matmul burst ≠ LLM SM 패턴: 실제 LLM 추론과 SM 경합 패턴이 다름. 한계로 명시.
- throttle OFF이므로 "메모리 quota만 강제" 상태. compute duty-cycle 제어는 없음.
- `PYTORCH_NO_CUDA_MEMORY_CACHING=1`: 절대 tok/s는 실배포 대비 낮음. 상대 비교는 유효.
- 수동 위상차: A warmup 완료 직후 B 시작까지 수 초 skew 허용. epoch 기준 사후 보정.
- consumer RTX 4070: MPS 없음. context switch 오버헤드로 speedup 상한 제약.
