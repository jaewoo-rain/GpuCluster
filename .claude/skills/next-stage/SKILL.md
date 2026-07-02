---
name: next-stage
description: >
  Drive the fGPU prototype's staged workflow. Use when the user says "다음",
  "next stage", "다음 단계", or asks what to build next. Determines the current
  stage from the tree, proposes the next stage in prose, and — only after the
  user confirms — implements it end-to-end (hook/backend code + smoke test +
  verify script + doc updates) with buildable, independently verifiable output.
---

# next-stage — staged development driver

This project is built in numbered stages; each stage's deliverable must build and
be verified on its own before the next begins. The authoritative roadmap is
`description.md` §8 and the per-stage success criteria live in `CLAUDE.md`.

## Step 1 — Locate the current stage
Do NOT trust memory. Infer the current stage from what's actually present:
- Read `description.md` §8 roadmap table (stage list + status marks).
- Cross-check against the tree: which hooks exist in `hook/src/fgpu_hook.c`,
  which `hook/tests/*.cu`, which `backend/` features, which `scripts/`.
- The current stage = highest stage whose artifacts are fully present.

## Step 2 — Propose the next stage (and STOP)
Write, in prose (Korean to match the project voice):
- What the next stage adds and *why* (the design intent, per description.md).
- The concrete deliverables: which files change/appear.
- The acceptance criteria, phrased as observable checks (grep-able `[fgpu]`
  stderr lines, HTTP status codes, exit codes, pytest counts).
- Any alternatives considered / hard-constraint interactions.

Then **STOP and wait**. Do not write stage code until the user explicitly says
"다음" (or clearly confirms). This is the project's core workflow rule.

## Step 3 — Implement (only after confirmation)
Delegate by domain (or hand off to the `ceo` agent to coordinate):
- CUDA hook / `.cu` tests / run scripts → **hook-engineer** agent.
- FastAPI / Docker / SQLite / UI / pytest → **backend-engineer** agent.
- Sequence dependent pieces; parallelize independent ones.

Honor all conventions: reentrancy guard + `_locked` + `[fgpu]` stderr prefix in
the hook; `to_thread` + admission lock + whitelist env in the backend; no MIG /
no SM isolation / VMM = cuMemCreate/Release only.

## Step 4 — Make it verifiable
Every stage ships its own smoke path:
- A standalone test that exercises the new capability in isolation.
- A `scripts/run_*_in_container.sh` (or `scripts/eval/*.sh`) with baseline vs
  hooked/enabled runs, and/or pytest for backend-only stages.
- State the exact command + expected output. If you're on Windows and can't run
  the GPU path, hand the user the commands and the pass pattern — never claim a
  pass you didn't observe.

## Step 5 — Document (delegate to docs-sync)
Update CLAUDE.md (file map + stage criteria), description.md (§10.x rationale),
ARCHITECTURE.md (tree + stage table), README/study chapter as needed. Verify
every named file/flag exists.

## Step 6 — Close out
Report what landed, how to verify it, and the *next* proposed stage in prose —
then wait for "다음" again.