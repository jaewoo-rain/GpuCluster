#!/usr/bin/env python3
"""
헤드리스 추론 측정 워크로드 (재실험 M3).

fgpu_infer.ipynb 의 측정 루프(cell 8~10)를 브라우저 없이 컨테이너에서 자동 실행하는 버전.
CSV 스키마는 scripts/eval/_overlap.py 및 fgpu_analysis.ipynb 와 호환된다.

환경변수 (run_sharing.sh 가 주입)
  FGPU_SCENARIO     : "solo" | "concurrent"            (기록용)
  FGPU_SESSION_LABEL: "A" | "B"
  FGPU_MEAS_RATIO   : 기록용 ratio (실제 quota 는 FGPU_RATIO 로 hook 에 주입됨)
  FGPU_MEAS_OUT     : 출력 디렉토리 (컨테이너 내, bind-mount 된 호스트 경로)
  FGPU_MEAS_NITERS  : 측정 반복 (기본 40)
  FGPU_MEAS_MAXTOK  : 생성 토큰 (기본 128)
  FGPU_MEAS_WARMUP  : 워밍업 (기본 3)
  FGPU_MEAS_MODEL   : HF 모델 ID (기본 Qwen/Qwen2-0.5B-Instruct)
  HF_HOME           : HF 캐시 (bind-mount 권장, 컨테이너 간 모델 재다운로드 방지)

출력 (FGPU_MEAS_OUT 안)
  result_<scenario>_<label>.csv : iter 단위 raw (label,scenario,ratio,model,iter,
                                  iter_start_epoch,iter_end_epoch,latency_s,gen_tokens,tokens_per_s)
  meta_<scenario>_<label>.json  : makespan/mean_tps/p50/p95/peak_mem + env snapshot
"""
import csv
import json
import os
import statistics
import time

SCENARIO = os.environ.get("FGPU_SCENARIO", "solo")
LABEL = os.environ.get("FGPU_SESSION_LABEL", "A")
RATIO = float(os.environ.get("FGPU_MEAS_RATIO", "0.5"))
OUT_DIR = os.environ.get("FGPU_MEAS_OUT", "/out")
N_ITERS = int(os.environ.get("FGPU_MEAS_NITERS", "40"))
MAX_NEW_TOKENS = int(os.environ.get("FGPU_MEAS_MAXTOK", "128"))
BATCH = int(os.environ.get("FGPU_MEAS_BATCH", "1"))   # 배치>1 = GPU 포화 ↑
WARMUP = int(os.environ.get("FGPU_MEAS_WARMUP", "3"))
MODEL_ID = os.environ.get("FGPU_MEAS_MODEL", "Qwen/Qwen2-0.5B-Instruct")
PROMPT = (
    "Explain the concept of fractional GPU resource sharing in cloud computing. "
    "Describe how multiple workloads can share a single GPU efficiently."
)

# caching 제어 (import torch 전에! 이미지에 PYTORCH_NO_CUDA_MEMORY_CACHING=1 이 박혀 있고
# 일부 torch 버전은 "값"이 아니라 "변수 존재"만 본다 → 진짜 켜려면 변수를 제거해야 함).
CACHING = os.environ.get("FGPU_MEAS_CACHING", "on")
if CACHING == "off":
    os.environ["PYTORCH_NO_CUDA_MEMORY_CACHING"] = "1"
else:
    os.environ.pop("PYTORCH_NO_CUDA_MEMORY_CACHING", None)

os.makedirs(OUT_DIR, exist_ok=True)
print(f"[measure] label={LABEL} scenario={SCENARIO} ratio={RATIO} "
      f"n_iters={N_ITERS} batch={BATCH} caching={CACHING} model={MODEL_ID}", flush=True)
for k in ("LD_PRELOAD", "FGPU_RATIO", "FGPU_THROTTLE_ENABLE", "FGPU_COMPUTE_RATIO",
          "PYTORCH_NO_CUDA_MEMORY_CACHING"):
    print(f"[measure]   {k}={os.environ.get(k, '<unset>')}", flush=True)

import torch
from transformers import AutoTokenizer, AutoModelForCausalLM

oom = False
iter_rows = []          # (t0_epoch, t1_epoch, latency_s, gen_tokens, tps)
peak_alloc_mib = 0.0
t_start = t_end = time.time()

try:
    torch.cuda.reset_peak_memory_stats()
    tok = AutoTokenizer.from_pretrained(MODEL_ID)
    # transformers 5.x 는 dtype=, 4.x 는 torch_dtype= — 둘 다 호환되게 폴백.
    try:
        model = AutoModelForCausalLM.from_pretrained(
            MODEL_ID, dtype=torch.bfloat16, device_map="cuda:0"
        )
    except TypeError:
        model = AutoModelForCausalLM.from_pretrained(
            MODEL_ID, torch_dtype=torch.bfloat16, device_map="cuda:0"
        )
    model.eval()
    inputs = tok([PROMPT] * BATCH, return_tensors="pt").to("cuda:0")

    # 워밍업 (결과 제외)
    with torch.no_grad():
        for _ in range(WARMUP):
            model.generate(**inputs, max_new_tokens=MAX_NEW_TOKENS,
                           do_sample=False, pad_token_id=tok.eos_token_id)
    torch.cuda.synchronize()

    torch.cuda.reset_peak_memory_stats()
    t_start = time.time()
    with torch.no_grad():
        for i in range(N_ITERS):
            _p0 = time.perf_counter()
            _t0 = time.time()
            out = model.generate(**inputs, max_new_tokens=MAX_NEW_TOKENS,
                                 do_sample=False, pad_token_id=tok.eos_token_id)
            torch.cuda.synchronize()
            _t1 = time.time()
            lat = time.perf_counter() - _p0
            gen = (out.shape[-1] - inputs["input_ids"].shape[-1]) * BATCH
            tps = gen / lat if lat > 0 else 0.0
            iter_rows.append((_t0, _t1, lat, gen, tps))
            if (i + 1) % 5 == 0 or i == 0:
                print(f"[measure] iter {i+1}/{N_ITERS}: {lat:.2f}s {gen}tok {tps:.1f}tok/s",
                      flush=True)
    t_end = time.time()
    peak_alloc_mib = torch.cuda.max_memory_allocated() / 1024**2

except torch.cuda.OutOfMemoryError as e:
    oom = True
    print(f"[measure] OOM (hook DENY 또는 물리 한계): {e}", flush=True)
except Exception as e:
    oom = True
    print(f"[measure] 오류: {type(e).__name__}: {e}", flush=True)


def _pct(data, p):
    if not data:
        return 0.0
    s = sorted(data)
    if len(s) == 1:
        return s[0]
    idx = (len(s) - 1) * p / 100.0
    lo = int(idx)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (idx - lo)


lats = [r[2] for r in iter_rows]
tpss = [r[4] for r in iter_rows]
gens = [r[3] for r in iter_rows]

csv_path = os.path.join(OUT_DIR, f"result_{SCENARIO}_{LABEL}.csv")
with open(csv_path, "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["label", "scenario", "ratio", "model", "iter",
                "iter_start_epoch", "iter_end_epoch", "latency_s",
                "gen_tokens", "tokens_per_s"])
    for idx, (t0, t1, lat, gen, tps) in enumerate(iter_rows):
        w.writerow([LABEL, SCENARIO, RATIO, MODEL_ID, idx + 1,
                    f"{t0:.6f}", f"{t1:.6f}", f"{lat:.6f}", gen, f"{tps:.4f}"])
    if not iter_rows:   # OOM placeholder
        w.writerow([LABEL, SCENARIO, RATIO, MODEL_ID, 0,
                    f"{t_start:.6f}", f"{t_end:.6f}", 0.0, 0, 0.0])

meta = {
    "label": LABEL, "scenario": SCENARIO, "ratio": RATIO, "model": MODEL_ID,
    "oom": oom, "n_iters": len(iter_rows),
    "makespan_s": (t_end - t_start) if iter_rows else 0.0,
    "t_start_epoch": t_start, "t_end_epoch": t_end,
    "mean_latency_s": statistics.mean(lats) if lats else 0.0,
    "p50_latency_s": _pct(lats, 50), "p95_latency_s": _pct(lats, 95),
    "mean_tokens_per_s": statistics.mean(tpss) if tpss else 0.0,
    "total_gen_tokens": sum(gens),
    "peak_mem_alloc_mib": peak_alloc_mib,
    "env_snapshot": {k: os.environ.get(k) for k in (
        "LD_PRELOAD", "FGPU_RATIO", "FGPU_QUOTA_BYTES", "FGPU_THROTTLE_ENABLE",
        "FGPU_COMPUTE_RATIO", "PYTORCH_NO_CUDA_MEMORY_CACHING")},
    "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
}
meta_path = os.path.join(OUT_DIR, f"meta_{SCENARIO}_{LABEL}.json")
with open(meta_path, "w") as f:
    json.dump(meta, f, indent=2)

print(f"[measure] saved {csv_path}", flush=True)
print(f"[measure] saved {meta_path}", flush=True)
print(f"[measure] makespan={meta['makespan_s']:.2f}s "
      f"mean_tps={meta['mean_tokens_per_s']:.2f} "
      f"p50_lat={meta['p50_latency_s']:.3f}s peak_mem={peak_alloc_mib:.0f}MiB", flush=True)
if oom:
    raise SystemExit(1)
