# fGPU Fractional GPU 공유 실험 — 실행 가이드 (경로 1: 코드 0 변경)

이 가이드는 이미지 리빌드나 백엔드 코드 수정 **없이** Jupyter 세션 2개를 띄워  
실제 소형 LLM(Qwen2-0.5B) 추론으로 fractional 공유 이득을 측정하는 절차입니다.

---

## 사전 조건

- 백엔드 실행 중: `./scripts/run_backend.sh` (포트 8000)
- 이미지 빌드 완료: `fgpu-runtime-pytorch:stage4`
- (선택) Stage 11 admission control 활성: 합=1.0 초과 POST 시 HTTP 409 반환

---

## 1단계: 백엔드 기동

```bash
cd <repo>
./scripts/run_backend.sh
# 별도 터미널에서 헬스체크
curl http://localhost:8000/healthz
```

---

## 2단계: 세션 A, B 생성

### curl 방법

```bash
# 세션 A (ratio=0.4)
curl -X POST http://localhost:8000/sessions \
  -H 'Content-Type: application/json' \
  -d '{"ratio": 0.4, "mode": "jupyter", "image": "fgpu-runtime-pytorch:stage4"}'

# 응답에서 id, host_port 를 기록합니다.
# 예: {"id": "abc123", "host_port": 49152, ...}

# admission 상태 확인 (A만 있으면 ratio_used=0.4)
curl http://localhost:8000/sessions/admission

# 세션 B (ratio=0.6, 합=1.0 → admission 통과)
curl -X POST http://localhost:8000/sessions \
  -H 'Content-Type: application/json' \
  -d '{"ratio": 0.6, "mode": "jupyter", "image": "fgpu-runtime-pytorch:stage4"}'

# 합=1.0 이면 201. 합>1.0 이면 HTTP 409 (admission_denied).
curl http://localhost:8000/sessions/admission
# 예: {"by_gpu": {"all": {"ratio_used": 1.0, "ratio_available": 0.0, ...}}}
```

### Web UI 방법

1. `http://localhost:8000/` 열기
2. Create 폼: `mode=jupyter`, `ratio=0.4`, `image=fgpu-runtime-pytorch:stage4` → A 세션
3. Submit → 세션 행의 "open ↗" 버튼 클릭하면 Jupyter Lab 열림
4. B 세션도 동일하게 생성하되 `ratio=0.6` 으로

---

## 3단계: 노트북 업로드 및 실행

각 세션의 Jupyter Lab (http://localhost:<host_port>?token=<token>) 에서:

1. `Upload` → `notebooks/fgpu_infer.ipynb` 업로드 (또는 복사)
2. 노트북 맨 위 **config 셀**에서:
   - A 세션: `SESSION_LABEL = "A"`, `RATIO = 0.4`
   - B 세션: `SESSION_LABEL = "B"`, `RATIO = 0.6`

---

## 4단계: 시나리오별 실행 절차

### 시나리오 1 — solo (기준선)

```
1. A 세션 노트북: SCENARIO = "solo"  → Kernel → Restart & Run All
2. 완료 후 A 세션 DELETE (workspace 보존):
   curl -X DELETE "http://localhost:8000/sessions/<id_A>?purge_workspace=false"
3. B 세션 노트북: SCENARIO = "solo"  → Kernel → Restart & Run All
4. 완료 후 B 세션 DELETE
5. 결과: data/sessions/<id_A>/session_result_solo.csv
         data/sessions/<id_B>/session_result_solo.csv
```

**중요**: solo 사이 GPU 메모리 해제 확인 필수.
```bash
nvidia-smi --query-gpu=memory.used --format=csv
```

### 시나리오 2 — seq (순차 A→B)

```
1. A 세션 노트북: SCENARIO = "seq"   → Run All
2. A 노트북 generate 셀 완료 확인
3. A 세션 DELETE
4. B 세션 노트북: SCENARIO = "seq"   → Run All
5. 결과: session_result_seq.csv (A, B 각각)
```

### 시나리오 3 — concurrent (동시, 이득 측정 핵심)

```
1. A, B 세션 모두 생성(running 상태)
2. A 노트북: SCENARIO = "concurrent"  → config 셀만 실행 완료
3. B 노트북: SCENARIO = "concurrent"  → config 셀만 실행 완료
4. A, B 노트북을 거의 동시에 "Run All" (양쪽 Shift+Enter)
   → 수 초 skew 허용 (makespan_conc 계산에 t_start_epoch 사용)
5. 완료 후 두 세션 모두 DELETE
6. 결과: session_result_concurrent.csv (A, B 각각)
```

**skew 최소화 팁**: 두 브라우저 탭을 나란히 두고 동시에 `Kernel → Restart & Run All`.

---

## 5단계: 결과 파일 위치

```
<repo>/data/sessions/
  <session_id_A>/
    session_result_solo.csv
    session_result_concurrent.csv
    session_meta_solo.json
    session_meta_concurrent.json
    hf_cache/              ← HF 모델 캐시 (첫 실행 시 ~1 GB 다운로드)
  <session_id_B>/
    session_result_solo.csv
    session_result_concurrent.csv
    ...
```

---

## 6단계: 종합 분석

호스트에서 (GPU 불필요):

```bash
# jupyter lab 또는 주피터 없이 nbconvert 로 실행
cd <repo>
jupyter nbconvert --to notebook --execute notebooks/fgpu_analysis.ipynb \
  --output notebooks/fgpu_analysis_executed.ipynb

# 또는 분석 노트북에서:
# SESSIONS_DIR = "<절대경로>/data/sessions"  # config 셀 수정
# → Run All
```

산출물:
- `notebooks/analysis_output/runs.csv` — 시나리오별 집계
- `notebooks/analysis_output/fgpu_sharing_results.png` — speedup/tokens/sec/makespan 그래프
- `notebooks/analysis_output/gpu_util_timeline.png` — GPU util 타임라인
- `notebooks/analysis_output/summary.txt` — VERDICT: PASS/FAIL

---

## 의도된 한계

| 한계 | 영향 |
|---|---|
| SM 격리 없음 | 두 컨테이너 동일 SM 자유 경쟁. speedup은 SM idle 시간 활용 여부에 의존. |
| `PYTORCH_NO_CUDA_MEMORY_CACHING=1` | KV cache 매 스텝 cudaMalloc/Free → 5~30x 속도 저하. 동일 조건이므로 speedup 상대 비교는 유효. |
| pynvml per-process 불가 | 컨테이너 내 PID ≠ 호스트 PID. GPU 전체 util만 보조 지표로 사용. |
| 수동 동시 시작 skew | 방법 B(수동 Shift+Enter) 사용 시 최대 수 초 skew. makespan_conc 오차 요인. |
| admission ≠ 물리 안전 | ratio 합=1.0 통과해도 CUDA context ~700MiB×2가 quota 밖 → 물리 OOM 가능(H4 시나리오). |

---

## HF 캐시 사전 다운로드 (선택, 첫 실행 시간 단축)

```bash
# 세션 A workspace 디렉토리에 캐시 미리 받기 (~1 GB, ~5분)
docker run --rm \
  -v <repo>/data/sessions/<id_A>:/workspace \
  -e HF_HOME=/workspace/hf_cache \
  fgpu-runtime-pytorch:stage4 \
  python3 -c "
from transformers import AutoModelForCausalLM, AutoTokenizer
m = AutoModelForCausalLM.from_pretrained('Qwen/Qwen2-0.5B-Instruct')
t = AutoTokenizer.from_pretrained('Qwen/Qwen2-0.5B-Instruct')
print('Download OK')
"
```

노트북의 `HF_HOME=/workspace/hf_cache` 설정 덕분에 이후 실행은 캐시를 사용합니다.
