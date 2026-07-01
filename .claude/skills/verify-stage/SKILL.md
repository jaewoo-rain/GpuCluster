---
name: verify-stage
description: >
  Enumerate and run the acceptance checks for a given fGPU stage. Use when the
  user asks to verify/test a stage, confirm a stage passes, or check the whole
  project (run-all). Maps the stage to its verification script and CLAUDE.md
  success criteria, runs what can run on this host, and reports PASS/FAIL against
  each criterion — handing over GPU-only commands when hardware is absent.
---

# verify-stage — acceptance-criteria checker

Given a stage number (or "all"), produce a concrete PASS/FAIL verdict against the
project's documented acceptance criteria, running whatever this host allows.

## Step 1 — Resolve stage → criteria + script
Read the stage's success-criteria section in `CLAUDE.md` (each stage has one) and
find its verification driver:

| Stage | Script |
|---|---|
| 1 | `scripts/run_test.sh` |
| 2 | `scripts/run_in_container.sh` |
| 3 | `scripts/smoke_test_api.sh` (needs backend running) |
| 4 | `scripts/run_pytorch_in_container.sh` |
| 5-A | `scripts/eval/run_isolation.sh` |
| 5-C | `scripts/run_driver_in_container.sh` |
| 5-D | `scripts/eval/run_overhead.sh` |
| 6 | `scripts/run_vmm_in_container.sh` |
| 7 | `scripts/run_launch_in_container.sh` |
| 8 | `cd backend && pytest tests/test_session_store.py` |
| 9 (min) | manual curl (auth on/off, `gpu_index`) |
| 10 | `scripts/eval/run_jupyter.sh` |
| 11 | `pytest backend/tests/test_admission.py` + `scripts/eval/run_admission.sh` |
| 12 | `scripts/run_throttle_in_container.sh` + `scripts/eval/run_throttle.sh` |
| all | `scripts/run_all_tests.sh` |

## Step 2 — Decide what can run here
- **Backend unit tests** (`pytest` for Stage 8 / 11) need no docker or GPU — run
  them on any host, including Windows (via the backend venv / WSL if configured).
- **Everything with a container or `nvidia-smi`** needs the Linux + NVIDIA host.
  On Windows, do NOT fake it: print the exact command block and the expected
  pass pattern, and ask the user to run it on the GPU box (or via WSL if the
  toolchain is there).

## Step 3 — Run and judge
For each acceptance criterion in CLAUDE.md, check the actual output:
- Hook stages: grep stderr for the exact `[fgpu]` tokens
  (`init` / `ALLOW` / `DENY` / `FREE` / `LAUNCH count=` / `THROTTLE` /
  `exit summary`) in the required order, plus the propagated error code
  (`err=2` / `result=2 (CUDA_ERROR_OUT_OF_MEMORY)`).
- API stages: check HTTP status codes, JSON shape, exit codes.
- eval stages: read the produced `experiments/<name>_<TS>/summary.txt` for the
  `VERDICT: PASS` line and the required artifacts.

## Step 4 — Report
One table: criterion → observed → PASS/FAIL, then an overall verdict. Attach the
per-step log path (`experiments/runall_<TS>/<step>.log` for run-all). If a step
couldn't run here, mark it "DEFERRED (needs GPU host)" with the command to run.
Never report PASS for a check you did not actually observe.