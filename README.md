# LoopRunner

Set-and-forget autonomous QA loop runner for Hermes Agent.

## What it does

LoopRunner turns Hermes into a continuous autonomous QA agent for any repository: it runs
your verify command each round, diagnoses failures with the host LLM, applies a minimal fix
on a branch, opens a PR, and repeats — all without manual intervention.

It is built to **stop itself**. Two budgets bound a loop, and a failure fingerprint detects
when retrying is pointless:

- `max_rounds` bounds **passes**.
- `max_attempts` bounds **total verify runs** — a loop that keeps failing terminates instead
  of retrying forever.
- Every failure is reduced to a normalized *signature* (numbers, hashes and absolute paths
  masked). The same signature recurring `stall_threshold` times marks the loop `stalled` and
  pauses its own cron, so the agent escalates instead of spinning.

## Tools

| Tool | Purpose |
| --- | --- |
| `loop_start` | Start a loop; auto-creates the cron job |
| `loop_tick` | Run one verify round; records pass/fail, duration, signature |
| `loop_verify` | Dry-run the verify command without recording a round |
| `loop_analyze` | Structured LLM diagnosis: root cause, failing tests, files, fix, confidence |
| `loop_commit` | Branch + commit + push + PR (refuses clean trees; `dry_run`, `security_gate`) |
| `loop_revert` | Close the last PR, delete its branch, return to the previous branch |
| `loop_status` | Round/attempt counts, budgets, signature, PRs, elapsed time |
| `loop_history` | Full round history |
| `loop_recommend` | LLM suggestions for the repository |
| `loop_watch` | Watch open issues and dispatch reproduction cards |
| `loop_scan_issues` | One watch pass (called by the watch cron) |
| `loop_security` | Security review of the pending diff |
| `loop_checkpoint` | Snapshot the current stage to a branch + patch |
| `loop_pause` / `loop_resume` / `loop_stop` | Control the loop and its cron |

## Install

```
hermes plugins install /path/to/looprunner
hermes plugins enable looprunner
```

## Issue-driven maintenance

`loop_watch(repo_path=..., interval_minutes=60)` scans the repository's open issues on a
cron and dispatches a **goal-mode kanban card** for each new one. That card reproduces
and diagnoses the issue, then spawns a follow-up fix card (as its child) that applies the
change and opens a PR with `loop_commit`. Issue numbers are remembered, so a rescan never
dispatches the same issue twice.

## Goal resilience

A loop with a `goal` stops early for three reasons: the deadline passes, the attempt budget
is exhausted, or it **stalls** (the same failure signature recurring `stall_threshold` times).

* When the aim is achieved, the loop's cron job is **deleted** — no orphaned schedule.
* When it stalls, the loop:
  1. snapshots the stage to `looprunner/checkpoint-<timestamp>` (a git ref plus a patch of
     any dirty work) — `loop_checkpoint` does the same on demand;
  2. dispatches a goal-mode kanban card that decides the best next step (change approach,
     `loop_revert`, or conclude the goal is unachievable) rather than retrying blindly;
  3. schedules a **retry cron** at a longer interval so the work is picked up again.

Set `checkpoint_on_stall: false` to opt out. An optional `goal_cmd` is the real acceptance
test: the loop only completes when it exits 0, not merely when the verify command passes.

## Security gate

`loop_security(loop_id=...)` reviews the pending change set (staged, unstaged and untracked)
with the host LLM and returns typed findings. `loop_commit(security_gate=True)` — or
`security_gate: true` on the loop — refuses to open a PR when a **critical** or **high**
finding is present. Untracked files are read directly, so a secret committed as a *new* file
is still reviewed.

## Usage

```
loop_start(repo_path="/path/to/repo", verify_cmd="cargo test",
           max_rounds=5, max_attempts=10, stall_threshold=3,
           goal="ship v1.0", goal_cmd="make release-check",
           security_gate=true)
loop_tick()
loop_analyze()      # after a FAIL
loop_verify()       # confirm the fix without recording a round
loop_commit()       # branch + PR
loop_tick()         # confirm the pass
```

```
loop_watch(repo_path="/path/to/repo", interval_minutes=60, labels=["bug"])
```

## Safety

- `loop_commit` is the only commit path. A `pre_tool_call` gate blocks direct
  `git commit`/`git push` while a loop is active on `main`/`master` or a loop branch.
- Generated artifacts (`__pycache__`, `*.pyc`, `node_modules`, `dist`, `build`, `.venv`, …)
  are excluded from both the diff gate and the commit.
- `loop_commit` refuses to run on a clean work tree — no empty commits.
- State lives in the plugin data directory, so it survives `hermes plugins update`.
- Per-loop file locking prevents concurrent cron ticks from corrupting state.

## Disclosure (rule 13)

- Reads/writes state under the plugin data directory (`plugin-data/looprunner/`) only.
- Spawns shell commands (your configured verify command) with the repository as cwd.
- Creates git branches and opens PRs via the `gh` CLI.
- No network calls to third-party services beyond git/gh.
- No credentials stored; relies on Hermes's existing git/gh auth.
- Long-running background process per active loop.

## License

MIT
