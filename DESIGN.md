# LoopRunner v0.8 — improvement plan

Derived from studying the plugin source, the hackathon catalog (~999 entries) and
the closest competitors.

## What the competitors do that LoopRunner does not

| Plugin | Idea worth stealing |
| --- | --- |
| `completion-proof` | **Revision-bound receipts** — a PASS is stale the moment source/tests change. LoopRunner counts a pass and moves on. |
| `escalation-governor` | **Stuck detection** on repeated identical calls / consecutive failures, then escalate instead of retrying blindly. |
| `degeneracy-guard` | Repetition-loop detection via `pre_tool_call` + approval gate. |
| `headway` | Proof-of-progress circuit breaker: exit-code 0 does not mean the task moved. |
| `modernisation-engine` | Record old/new evidence before any production change; separate candidate branch. |
| `diff-review` | Diff-level quality gate before committing. |
| `follow-through` | Never end a turn on a promise; nudge to actually do it. |

## Defects found in the current code

1. `loop_tick` only increments `round` on PASS — a loop can fail forever and never hit
   `max_rounds`. No attempt budget at all.
2. No convergence detection: the same failure retried N times looks identical to progress.
3. `loop_commit` runs `git add -A` + commit + push with no diff gate, no "nothing to
   commit" check, and no dry-run.
4. Branch guard creates `looprunner/work-N`, then `loop_commit` creates `auto/round-N`
   *from it* — nested branches, and the work branch is never cleaned up.
5. `loop_status` claims to report "open PRs" but never does.
6. State lives in `~/.hermes/looprunner/` (wiped by `plugins update`); the sanctioned
   `plugin_data_dir()` is available and works.
7. `loop_recommend` shells out to `hermes chat -q` — a whole extra agent per call.
   `ctx.llm` is available.
8. No lock: two cron ticks can interleave and corrupt `loops.json`.
9. `shell=True` on verify_cmd with no injection note.
10. No round timing, no history tool, no revert path.

## v0.8.0 changes

**New tools**
- `loop_history` — full round history with timings and signatures.
- `loop_verify` — dry-run the verify command without recording a round.
- `loop_analyze` — structured LLM diagnosis (`ctx.llm.complete_structured`):
  root cause, failing tests, files to change, proposed fix, confidence, risk.
- `loop_revert` — close the last PR, delete its branch, reset the work tree.

**Changed**
- `loop_start`: + `max_attempts`, `stall_threshold`, `auto_fix`, `strict_git_guard`.
- `loop_tick`: per-loop file lock, attempt counting, failure-signature stall detection,
  per-round timing, structured FAIL payload, `stalled` status.
- `loop_commit`: diff gate (refuses empty commits), `dry_run`, reuses the loop's working
  branch instead of nesting, records the PR in loop state.
- `loop_status`: attempts, signature repeats, recorded PRs, elapsed time.
- `loop_recommend`: `ctx.llm` first, subprocess fallback.
- State: `plugin_data_dir("looprunner")` with automatic v1 migration.

**New hook**
- `pre_tool_call` safety gate: blocks `loop_commit` on a non-running loop, and blocks
  direct `git commit`/`git push` while an active loop sits on main/master.

## Test plan

Run against real repos with real failures, not a synthetic fixture:
1. A Python repo with a genuinely failing test.
2. A repo where the fix requires a multi-file change.
3. A repo where the loop should stall (unfixable failure) — verify it stops.
