---
name: looprunner-pattern
description: "Use when running an autonomous set-and-forget QA loop over a repo."
---

# LoopRunner Pattern

## When to use

When the user wants continuous autonomous QA on a repository: run verify command, find failures, fix, open PR, repeat.

## Steps

1. `loop_start(repo_path=..., verify_cmd=...)` to begin. Cron job auto-created at interval_minutes (default 60m).
2. Each round: run verify_cmd via terminal. If pass, record pass and continue.
3. If fail: analyze failure, apply minimal fix, commit to auto/round-<stamp> branch, open PR.
4. `loop_status` to check progress; `loop_pause`/`loop_resume` to control.
5. Guard: skip a new round if any card/worker is still running.

## Verify command patterns

- Python (uv): `uv run pytest tests/ -q -x --timeout=120`
- Python (venv): `.venv/Scripts/python -m pytest tests/ -q`
- Rust: `cargo test`
- Node: `npm test`
- Generic: any shell command that exits 0 on pass, non-zero on fail

## Recommendations

- `loop_recommend(loop_id=...)` analyzes the repo via LLM and returns numbered improvements with effort estimates.
- Uses subprocess `hermes chat` fallback because `ctx.llm` may return empty (provider routing issue with task=None).
- Recommendations are raw text — the worker must parse and implement them.

## Auto-setup

- loop_start auto-creates a cron job that ticks every interval_minutes.
- Cron auto-pauses when loop completes or stops.
- Stale cron jobs referencing deleted loops are common — clean up periodically.

## Rules

- Never auto-push to main/master. Branch + PR only.
- The subprocess worker (hermes chat) may commit directly to master instead of calling loop_commit. Enforce at two levels: (1) explicit "DO NOT run git commit directly" in the loop_tick FAIL message, (2) a git pre-commit hook that rejects commits on master/main. The hook is the hard gate; the message is the instruction.
- loop_commit is the only allowed commit path — it creates a branch, commits, pushes, and opens a PR via gh. If the worker bypasses it, the pre-commit hook blocks the commit.
- Never auto-approve unattended commands that need human approval.
- Fail cleanly under cron/unattended runs.
- Report bad news first.