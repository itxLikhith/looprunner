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
| `loop_approve` | Approve a loop command out of band, so unattended runs may execute it |
| `loop_pending` | List loop commands still waiting for approval, with their rule keys |
| `loop_pause` / `loop_resume` / `loop_stop` | Control the loop and its cron |

## Talk to it, don't learn it

The plugin is built to be driven by intent, not by memorised arguments — in plain chat,
not through a slash command:

```
you: keep my tests green on colorama-test
     -> the model detects the repo and its verify command, starts a loop, ticks it

you: why did it stop?
     -> loop_status, then loop_analyze

you: undo that last change
     -> loop_revert
```

* `repo_path` accepts an **absolute path, a relative path, `~`, or a bare project
  name** (`loop_start(repo_path="colorama-test")`). A wrong name comes back with
  *"did you mean …?"*.
* `verify_cmd` **may be omitted** — `loop_start` reads the project files and picks
  one. `loop_detect(repo_path=...)` shows the ranked candidates if you want to choose.
* Errors carry the fix, and `loop_status` returns a `next_action` telling the agent
  what to do next.

There is deliberately **no `/looprunner` slash command**. A plugin slash command is a
deterministic surface (the bundled `disk-cleanup` and `advanced-delegation` plugins both
parse a fixed subcommand table), and routing free text through the model would require
`ctx.inject_message` — a host API intended for external feeds such as webhooks and
bridges, disabled by default, and absent from the capability registry. Chat is the right
place for intent: the bundled skill plus the tool descriptions let the model interpret a
request directly.

## Install

```
hermes plugins install /path/to/looprunner
hermes plugins enable looprunner
```

## Command approval

A loop runs its verify command on **every tick, including unattended cron runs**, so the
command is approved once per (repository, command) through Hermes's own approval gate —
the same prompt, allowlist and `/approve` flow as any other dangerous command. A command
from a tool argument is never trusted implicitly; auto-detection is only a suggestion until
approved.

A loop running where no human can answer (cron, `hermes chat -q`, webhook) **refuses
immediately** rather than blocking for the approval timeout — Hermes's
`approvals.cron_mode` / `single_query_mode` / `unattended_mode` default to `deny`, and
waiting would only delay the same refusal.

### How to approve

**1. Interactively** — start the loop from an interactive session and answer the prompt
with `[a]lways`. The approval is remembered per (repository, command), so cron ticks then
run it unattended.

**2. From chat** — `/loop_approve <repo-path> <command...>`, or the `loop_approve` tool.
`loop_pending` lists what is waiting, with rule keys.

**3. From the card** — when a loop cannot reach a human it dispatches an **approval card**
to the kanban board, so there is somewhere to answer it:

```
Task: Approve loop command: .venv/Scripts/python.exe -m pytest tests/ -q

Approving is a standing grant. It allows this command to run unattended in this
repository on every loop tick, including cron.

  hermes kanban comment <card> approve
  hermes kanban comment <card> deny
```

The next tick reads the decision before refusing, so the loop unblocks on its own. Only a
comment starting with `approve` or `deny` counts — discussion on the card cannot silently
grant a standing approval.

**4. Check a verdict without running anything:**

```
hermes approvals test -- <command>
```

### Configuration

```yaml
command_allowlist:
  - looprunner:<repo-hash>:<command-hash>   # a standing approval

approvals:
  mode: smart          # smart | manual | off
  cron_mode: deny      # deny | approve — what cron does when no human can answer
  single_query_mode: deny
  unattended_mode: deny
```

`approvals.mode: off` is equivalent to `--yolo` and bypasses every approval check.

> **Running a loop unattended and want prompts on your phone?** Connect a gateway platform
> (Telegram, Discord, …) and set `security.approval.transport: ntfy` — the `ntfy-approval`
> plugin delivers each prompt as a push notification with Approve once / session / always
> buttons. That is a user-level setup and needs no plugin configuration.

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
