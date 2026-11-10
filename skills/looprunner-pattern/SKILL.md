---
name: looprunner-pattern
description: "Use when running an autonomous set-and-forget QA loop over a repo."
---

# LoopRunner Pattern

## When to use

When the user wants continuous autonomous QA on a repository: run verify command, find
failures, fix, open PR, repeat — with hard budgets so it cannot run forever.

## Steps

1. `loop_start(repo_path=..., verify_cmd=...)` to begin. A cron job is auto-created at
   `interval_minutes` (default 60m).
2. Each round: run `loop_tick`. It runs `verify_cmd` once and records the result.
   * PASS → `round` increments.
   * FAIL → the tool returns a failure **signature** and a `branch` name.
3. On FAIL: call `loop_analyze` for a structured diagnosis (root cause, failing tests,
   files to change, proposed fix, confidence, risk). Apply the fix, then:
   * `loop_verify` — dry-run the check without recording a round (confirm the fix).
   * `loop_commit` — branch + commit + push + PR. Refuses to run on a clean tree.
   * `loop_tick` again to confirm the pass.
4. `loop_status` for state, `loop_history` for the full round log.
5. Guard: never start a second round while one is still running.

## Budgets and stall detection

* `max_rounds` bounds **passes**; `max_attempts` bounds **total verify runs**.
  A loop that keeps failing stops at `max_attempts` instead of retrying forever.
* Every failure is reduced to a normalized signature (numbers, hashes and paths are
  masked). The same signature repeating `stall_threshold` times (default 3) marks the
  loop `stalled` and pauses its own cron. That is the escalation signal: change the
  approach, or `loop_revert` the last attempt.
* `loop_status` reports `last_signature` and `signature_repeats`.

## Verify command patterns

* Python (uv): `uv run pytest tests/ -q -x --timeout=120`
* Python (venv, Windows): `.venv\Scripts\python -m pytest tests/ -q` — **backslashes**.
  cmd.exe does not resolve `.venv/Scripts/python.exe`; the plugin rewrites a leading
  relative path to backslashes when it exists, but backslashes are the reliable form.
* Rust: `cargo test`
* Node: `npm test`
* Generic: any shell command that exits 0 on pass, non-zero on fail.

## Safety rules

* `loop_commit` is the only commit path — it creates a branch, commits, pushes, and opens
  a PR. Direct `git commit`/`git push` are blocked by the `pre_tool_call` gate while a
  loop is active on main/master or a loop branch.
* Never auto-push to main/master. Branch + PR only.
* Generated artifacts (`__pycache__`, `*.pyc`, `node_modules`, `dist`, `build`, `.venv`,
  …) are excluded from both the diff gate and the commit.
* `loop_commit` refuses to run when the work tree is clean — no empty commits.
* `loop_revert` closes the last PR, deletes its branch and returns to the previous branch.
* Never auto-approve unattended commands that need human approval.
* Fail cleanly under cron/unattended runs.
* Report bad news first.

## Recommendations

* `loop_recommend(loop_id=...)` analyzes the repo via the host LLM and returns numbered
  improvements with effort estimates.
* `loop_analyze` is the tool to reach for after a FAIL; it returns typed JSON.

## Auto-setup

* `loop_start` auto-creates a cron job that ticks every `interval_minutes`.
* Cron auto-pauses when the loop completes, stalls or stops.
* Stale cron jobs referencing deleted loops are common — clean up periodically.
