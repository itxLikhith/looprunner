---
name: looprunner-pattern
description: "Use when running an autonomous set-and-forget QA loop over a repo."
---

# LoopRunner Pattern

## When to use

When the user wants continuous autonomous QA on a repository: run verify command, find failures, fix, open PR, repeat.

## Steps

1. `loop_start(repo_path=..., verify_cmd=...)` to begin.
2. Each round: run verify_cmd via terminal. If pass, record pass and continue.
3. If fail: analyze failure, apply minimal fix, commit to auto/round-<stamp> branch, open PR.
4. `loop_status` to check progress; `loop_pause`/`loop_resume` to control.
5. Guard: skip a new round if any card/worker is still running.

## Rules

- Never auto-push to main/master. Branch + PR only.
- Never auto-approve unattended commands that need human approval.
- Fail cleanly under cron/unattended runs.
- Report bad news first.