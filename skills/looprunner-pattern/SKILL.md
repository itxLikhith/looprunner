---
name: looprunner-pattern
description: "Use when running an autonomous set-and-forget QA loop over a repo."
---

# LoopRunner Pattern

## When to use

When the user wants continuous autonomous QA on a repository: run verify command, find
failures, fix, open PR, repeat — with hard budgets so it cannot run forever.

## Conversational use: infer, do not interrogate

Users do not know the tool names or the exact arguments. Map what they *mean* onto the
tools, and only ask when the answer genuinely cannot be inferred.

| The user says | Do |
| --- | --- |
| "keep my tests green on \<repo\>" / "keep fixing \<repo\>" | `loop_start` with that repo; let it auto-detect the verify command |
| "\<repo\>" (just a name, no command) | `loop_detect(repo_path=<repo>)`, then `loop_start` with the recommended command |
| "is it running?" / "how's it going?" | `loop_status` — it returns a `next_action` |
| "it's stuck" / "why did it stop" | `loop_status`, then `loop_analyze` |
| "undo that last change" | `loop_revert` |
| "pause/stop/resume it" | `loop_pause` / `loop_stop` / `loop_resume` |
| "check it for security issues" | `loop_security` (or `loop_security_card` for the heavy scan) |
| "watch the issues on \<repo\>" | `loop_watch` |

The rows above are **examples of intent**, not an exhaustive grammar — the user may
phrase any of these differently. Infer the goal, then pick the tool.

Rules:

* **Never ask for an absolute path.** `repo_path` accepts a project name, a relative
  path, or `~`. Only ask which repository when the user has not implied one.
* **Never ask for the verify command** when the project type is recognisable — omit it
  and let `loop_start` detect it, or call `loop_detect` first if the user wants to see
  the options.
* **One question at a time.** If something is genuinely ambiguous, ask the single most
  useful question and propose a default.
* **Report what you inferred.** "Started a loop on `colorama`, using `.venv/bin/python
  -m pytest -q`." — so the user can correct you.
* If a tool returns an error, read its message: LoopRunner errors carry the fix.
* There is no `/looprunner` slash command — intent is handled in plain chat, where the
  model can call several tools in order.

## Steps

1. `loop_start(repo_path=..., verify_cmd=...)` to begin. A cron job is auto-created at
   `interval_minutes` (default 60m). `repo_path` accepts a project name or relative path;
   `verify_cmd` may be omitted and is detected from the project files.
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

## Complete tool surface

Everything LoopRunner can do. Use this as the map when the user's request does not
obviously fit the flow above.

| Tool | What it does |
| --- | --- |
| `loop_start` | Start a loop; auto-creates the cron job. Resolves repo names, auto-detects `verify_cmd` |
| `loop_tick` | Run one verify round; records pass/fail, duration, failure signature |
| `loop_verify` | Dry-run the verify command without recording a round |
| `loop_status` | Round/attempt counts, budgets, signature, PRs, elapsed, and a `next_action` |
| `loop_history` | Full round history |
| `loop_analyze` | Structured LLM diagnosis: root cause, failing tests, files, fix, confidence, risk |
| `loop_commit` | Branch + commit + push + PR. Refuses clean trees; `dry_run`, `security_gate` |
| `loop_revert` | Close the last PR, delete its branch, return to the previous branch |
| `loop_detect` | Resolve a repo reference and propose a verify command |
| `loop_checkpoint` | Snapshot the current stage to a git branch + dirty-work patch |
| `loop_watch` | Watch open issues; dispatch a goal-mode reproduction card per new issue |
| `loop_scan_issues` | One watch pass (the watch cron calls this) |
| `loop_security` | Security review of the pending diff; `engine=auto|llm|plugin` |
| `loop_security_card` | Hand the heavy multi-agent security scan to a goal-mode card |
| `loop_recommend` | LLM suggestions for the repository |
| `loop_approve` | Approve a loop command out of band (writes `command_allowlist`) |
| `loop_pending` | List loop commands still waiting for approval, with rule keys |
| `loop_pause` / `loop_resume` / `loop_stop` | Control the loop and its cron |

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

## Goal resilience

* `goal_cmd` is the real acceptance test: the loop completes only when it exits 0, not
  merely when `verify_cmd` passes once.
* On completion the loop's cron job is **deleted**.
* On stall (`checkpoint_on_stall`, default true) the loop:
  1. snapshots the stage to `looprunner/checkpoint-<ts>` (git ref + dirty-work patch);
  2. dispatches a goal-mode kanban card to decide the best next step;
  3. schedules a retry cron at 4x the interval.
* `loop_checkpoint` does the snapshot on demand.

## Issue-driven maintenance

* `loop_watch(repo_path=..., interval_minutes=..., labels=[...])` scans open issues on a
  cron and dispatches a goal-mode reproduction card per new issue; the card spawns the
  follow-up fix card. Issue numbers are remembered, so rescans do not duplicate.
* `loop_scan_issues` runs one pass manually.

## Security gate

* `loop_security(loop_id=...)` reviews the pending diff (staged + unstaged + untracked) and
  returns typed findings with a `blocking` flag for critical/high.
  - `engine="llm"` is self-contained.
  - `engine="plugin"` uses the hermes-security multi-agent scan when that plugin is loaded;
    `engine="auto"` (the default) prefers it and falls back.
* `loop_commit(..., security_gate=True)` refuses to open a PR when blocking findings exist.
* `loop_security_card(loop_id=...)` dispatches a goal-mode card carrying the
  `security-diff-review` skill to drive the heavy scan to completion.
* LoopRunner declares an advisory `requires_plugins: [hermes-security]` edge; a missing
  dependency only logs a warning and the plugin probes with `ctx.has_plugin(...)`.
  Install with `hermes plugins install hermes-security`.

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
* When a command is refused on an unattended surface, say so plainly and point at the
  approval card or `/loop_approve` — do not silently retry, and do not suggest editing
  `approvals.mode` unless the user asks for the broader grant.

## Recommendations

* `loop_recommend(loop_id=...)` analyzes the repo via the host LLM and returns numbered
  improvements with effort estimates.
* `loop_analyze` is the tool to reach for after a FAIL; it returns typed JSON.

## Auto-setup

* `loop_start` auto-creates a cron job that ticks every `interval_minutes`.
* Cron auto-pauses when the loop completes, stalls or stops.
* Stale cron jobs referencing deleted loops are common — clean up periodically.
