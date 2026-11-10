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
| `loop_security_card` | Hand the heavy security scan to a goal-mode card |
| `loop_checkpoint` | Snapshot the current stage to a branch + patch |
| `loop_detect` | Inspect a repo and propose a verify command |
| `loop_pause` / `loop_resume` / `loop_stop` | Control the loop and its cron |

## Talk to it, don't learn it

The plugin is built to be driven by intent, not by memorised arguments:

* `repo_path` accepts an **absolute path, a relative path, `~`, or a bare project
  name** (`loop_start(repo_path="colorama-test")`). A wrong name comes back with
  *"did you mean …?"*.
* `verify_cmd` **may be omitted** — `loop_start` reads the project files and picks
  one. `loop_detect(repo_path=...)` shows the ranked candidates if you want to choose.
* Errors carry the fix, and `loop_status` returns a `next_action` telling the agent
  what to do next.

The `/looprunner` slash command does no parsing at all. Whatever the user typed is
injected into the session as a user turn (`ctx.inject_message`), so the model interprets
it against the full tool surface and acts — including calling several tools in order.
`/looprunner` with no arguments reports status.

```
/looprunner                                          status
/looprunner start a loop on colorama-test            -> the model does it
/looprunner keep my tests green on X and fix failures -> the model does it
/looprunner why did it stop / undo the last change    -> the model does it
```

Requires `plugins.entries.looprunner.allow_gateway_injection: true` in `config.yaml`;
without it the handler falls back to returning the handoff prompt as text.

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
and returns typed findings. Two engines:

* `engine="llm"` (default) — a self-contained LLM review. Always available.
* `engine="plugin"` — the **hermes-security** multi-agent diff scan, when that plugin is
  loaded. `engine="auto"` (the default) prefers the plugin and falls back to the LLM review.

Untracked files are read directly, so a secret committed as a *new* file is still reviewed.

`loop_commit(security_gate=True)` refuses to open a PR when a **critical** or **high**
finding is present.

### Optional: the heavy scan

The multi-agent scan is a multi-turn workflow (dispatch workers, wait, validate, finalize),
so it cannot run inside a single tool call. Two ways to use it:

```
loop_security(loop_id=..., engine="plugin")   # starts the scan, hands back the driver steps
loop_security_card(loop_id=...)               # hands the whole scan to a goal-mode card
```

The card carries the `security-diff-review` skill and drives the scan to completion, then
reports MERGE or BLOCK.

LoopRunner declares an **advisory** dependency:

```yaml
requires_plugins:
  - id: hermes-security
```

That edge sets load order (hermes-security's `register()` runs first) and a missing
dependency only logs a warning — the plugin probes with `ctx.has_plugin("hermes-security")`
and degrades to the built-in review. To enable the heavy scan:

```
hermes plugins install hermes-security
hermes plugins enable hermes-security
```

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
- Dispatches kanban cards via the `hermes kanban` CLI when watching issues or gating PRs.
- Optionally depends on the `hermes-security` plugin (advisory `requires_plugins` edge);
  when it is loaded, `loop_security` and `loop_security_card` call its tools through
  `ctx.dispatch_tool`. Without it, the built-in LLM review is used.
- No network calls to third-party services beyond git/gh.
- No credentials stored; relies on Hermes's existing git/gh auth.
- Long-running background process per active loop.

## License

MIT
