# LoopRunner

Generalized set-and-forget autonomous QA loop runner for Hermes Agent.

## What it does

LoopRunner turns Hermes into a continuous autonomous QA agent for any repository: it runs your verify command each round, analyzes failures, applies minimal fixes on a branch, opens PRs, and repeats — all without manual intervention.

## Disclosure (rule 13)

- Reads/writes state under `~/.hermes/looprunner/` only.
- Spawns shell commands (your configured verify command) via the terminal tool.
- Creates git branches and opens PRs via `gh` CLI when configured.
- No network calls to third-party services beyond git/gh.
- No credentials stored; relies on Hermes's existing git/gh auth.
- Long-running background process per active loop.

## Install

```
hermes plugins install /path/to/looprunner-plugin
hermes plugins enable looprunner
```

## Usage

```
loop_start(repo_path="/path/to/repo", verify_cmd="cargo test")
loop_status()
loop_pause()
loop_resume()
loop_stop()
```

## License

MIT