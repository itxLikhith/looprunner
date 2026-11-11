"""Tool schemas — what the LLM sees."""

LOOP_START = {
    "name": "loop_start",
    "description": (
        "Start a generalized set-and-forget autonomous loop for a given repository. "
        "Runs verify command each round, analyzes failures with LLM, applies fixes, opens PRs. "
        "Supports goal + deadline for auto-prioritization and auto-disable. "
        "Two budgets bound the loop: max_rounds (passes) and max_attempts (total verify runs), "
        "so a loop that keeps failing stops instead of running forever."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "repo_path": {"type": "string", "description": "Absolute path to the repository"},
            "verify_cmd": {
                "type": "string",
                "description": (
                    "Shell command that exits 0 on pass and non-zero on fail. "
                    "Runs with the repository as cwd. On Windows, use "
                    "'.venv\\\\Scripts\\\\python -m pytest -q' (backslashes) or quote "
                    "the path; a bare forward-slash path is not resolved by cmd.exe."
                )
            },
            "max_rounds": {"type": "integer", "description": "Max PASSING rounds before auto-pause (default 10)"},
            "max_attempts": {"type": "integer", "description": "Max total verify runs before the loop gives up (default 20)"},
            "stall_threshold": {
                "type": "integer",
                "description": (
                    "How many times the SAME failure (normalized signature) may repeat "
                    "before the loop marks itself stalled and pauses its own cron (default 3)"
                ),
            },
            "goal": {"type": "string", "description": "What this loop should accomplish (e.g. 'ship v1.0')"},
            "goal_cmd": {
                "type": "string",
                "description": (
                    "Optional acceptance command for the goal. When set, the loop only "
                    "completes when this exits 0 -- the verify command passing once is "
                    "not the same as the aim being achieved."
                ),
            },
            "deadline": {"type": "string", "description": "ISO date by which goal must be met (auto-disables after)"},
            "interval_minutes": {"type": "integer", "description": "Cron interval in minutes (default 60)"},
            "branch_guard": {"type": "boolean", "description": "Auto-create a working branch when on main/master (default true)"},
            "auto_recommend": {"type": "boolean", "description": "Suggest next improvements after each pass"},
            "auto_fix": {"type": "boolean", "description": "On failure, ask the model to apply a minimal fix and commit it (default true)"},
            "autofix_card_after": {"type": "integer", "description": "Dispatch an escalation card after this many autofix attempts that changed nothing (default 2, 0 disables)"},
            "autofix_max_turns": {"type": "integer", "description": "Max agent turns per autofix attempt (default 8)"},
            "autofix_budget": {"type": "integer", "description": "Wall-clock seconds per autofix attempt (default 300)"},
            "strict_git_guard": {
                "type": "boolean",
                "description": (
                    "Block direct git commit/push while this loop is active on main/master "
                    "(default true). loop_commit stays allowed."
                ),
            },
            "checkpoint_on_stall": {
                "type": "boolean",
                "description": (
                    "On stall, snapshot the stage to a git branch + patch, dispatch a "
                    "goal-mode kanban card to decide the next step, and schedule a retry "
                    "cron (default true)."
                ),
            },
            "security_gate": {
                "type": "boolean",
                "description": (
                    "Run a security review of the pending diff before opening a PR and "
                    "refuse on critical/high findings (default false)."
                ),
            },
            "mode": {
                "type": "string",
                "description": "fix (default) runs verify_cmd; watch is for issue monitoring",
            },
            "assignee": {"type": "string", "description": "Kanban profile to assign dispatched cards to"},
            "labels": {"type": "array", "items": {"type": "string"}, "description": "Only watch issues carrying these labels"},
            "goal_max_turns": {"type": "integer", "description": "Turn budget for goal-mode cards (default 20)"},
            "verify_timeout": {"type": "integer", "description": "Seconds before the verify command is killed (default: derived from the command, 600-1800)"},
            "shard_index": {"type": "integer", "description": "Which shard this loop owns (1-based)"},
            "shard_total": {"type": "integer", "description": "Total shards; >1 adds sharding flags to the verify command"},
        },
        "required": ["repo_path"],
    },
}

LOOP_DETECT = {
    "name": "loop_detect",
    "description": (
        "Inspect a repository and propose a verify command: reads package.json, "
        "pyproject.toml, Cargo.toml, Makefile, go.mod and friends, and returns ranked "
        "candidates with confidence. Use when the user names a repo but not how to "
        "test it. Also resolves relative paths and bare project names."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "repo_path": {
                "type": "string",
                "description": "Absolute path, relative path, '~' path, or a bare project name",
            },
        },
        "required": [],
    },
}

LOOP_WATCH = {
    "name": "loop_watch",
    "description": (
        "Watch a repository's open issues and dispatch a goal-mode kanban card for each "
        "new one. The card reproduces and diagnoses the issue, then spawns a follow-up "
        "fix card that applies the change and opens a PR."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "repo_path": {"type": "string", "description": "Absolute path to the repository"},
            "interval_minutes": {"type": "integer", "description": "How often to scan for new issues"},
            "assignee": {"type": "string", "description": "Kanban profile to assign cards to"},
            "goal": {"type": "string", "description": "Goal recorded on the watch loop"},
            "labels": {"type": "array", "items": {"type": "string"}, "description": "Only dispatch issues with these labels"},
            "goal_max_turns": {"type": "integer", "description": "Turn budget for goal-mode cards"},
        },
        "required": ["repo_path"],
    },
}

LOOP_SCAN_ISSUES = {
    "name": "loop_scan_issues",
    "description": (
        "Run one watch pass: dispatch a reproduction card for every new open issue. "
        "Called by the watch loop's cron job."
    ),
    "parameters": {
        "type": "object",
        "properties": {"loop_id": {"type": "string", "description": "Watch loop id"}},
        "required": [],
    },
}

LOOP_SECURITY = {
    "name": "loop_security",
    "description": (
        "Security review of the loop's pending change set (staged + unstaged + untracked). "
        "engine='llm' (default) is the self-contained review returning typed findings and "
        "a 'blocking' flag for critical/high. engine='plugin' uses the hermes-security "
        "multi-agent diff scan when that plugin is loaded; engine='auto' prefers it and "
        "falls back to the LLM review. Use before loop_commit, or let loop_commit's "
        "security_gate do it."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "loop_id": {"type": "string", "description": "Loop id (empty = most recent)"},
            "engine": {"type": "string", "enum": ["auto", "llm", "plugin"],
                       "description": "auto (default) prefers hermes-security when loaded"},
        },
        "required": [],
    },
}

LOOP_SECURITY_CARD = {
    "name": "loop_security_card",
    "description": (
        "Dispatch a goal-mode kanban card that runs the hermes-security multi-agent diff "
        "scan to completion. The heavy scan is a multi-turn workflow, so it cannot run "
        "inside a single tool call -- this hands it to a card carrying the "
        "security-diff-review skill. Requires the hermes-security plugin."
    ),
    "parameters": {
        "type": "object",
        "properties": {"loop_id": {"type": "string", "description": "Loop id (empty = most recent)"}},
        "required": [],
    },
}

LOOP_CHECKPOINT = {
    "name": "loop_checkpoint",
    "description": (
        "Snapshot the loop's current stage: create a git branch at the current HEAD and "
        "write any dirty work to a patch file. Use before a risky step or when the "
        "loop stops early."
    ),
    "parameters": {
        "type": "object",
        "properties": {"loop_id": {"type": "string", "description": "Loop id (empty = most recent)"}},
        "required": [],
    },
}

LOOP_TICK = {
    "name": "loop_tick",
    "description": (
        "Execute one autonomous round: run verify_cmd, if pass increment round, "
        "if fail analyze with LLM, apply fix, commit+PR. Checks deadline, the attempt "
        "budget and the stall threshold; auto-disables the cron when any of them trip."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "loop_id": {"type": "string", "description": "Loop id (empty = most recent active)"},
        },
        "required": [],
    },
}

LOOP_VERIFY = {
    "name": "loop_verify",
    "description": (
        "Dry-run the loop's verify command without recording a round. "
        "Use before loop_commit to confirm a fix actually passes."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "loop_id": {"type": "string", "description": "Loop id (empty = most recent)"},
        },
        "required": [],
    },
}

LOOP_STATUS = {
    "name": "loop_status",
    "description": (
        "Report loop state: status, round (passes), attempts (verify runs), budgets, "
        "failure signature and its repeat count, recorded PRs, last result, elapsed time."
    ),
    "parameters": {
        "type": "object",
        "properties": {"loop_id": {"type": "string", "description": "Loop id (empty = most recent)"}},
        "required": [],
    },
}

LOOP_HISTORY = {
    "name": "loop_history",
    "description": "Full round history for a loop: every verify run with pass/fail, duration, signature and output excerpt.",
    "parameters": {
        "type": "object",
        "properties": {
            "loop_id": {"type": "string", "description": "Loop id (empty = most recent)"},
            "limit": {"type": "integer", "description": "Only the last N entries"},
        },
        "required": [],
    },
}

LOOP_ANALYZE = {
    "name": "loop_analyze",
    "description": (
        "Structured LLM diagnosis of the loop's most recent failure: root cause, failing "
        "tests, exact files to change, proposed fix, confidence and risk. "
        "Use after loop_tick returns FAIL."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "loop_id": {"type": "string", "description": "Loop id (empty = most recent)"},
        },
        "required": [],
    },
}

LOOP_PAUSE = {
    "name": "loop_pause",
    "description": "Pause an active loop. Workers finish the current round then stop; cron is paused.",
    "parameters": {
        "type": "object",
        "properties": {"loop_id": {"type": "string", "description": "Loop id (empty = most recent)"}},
        "required": [],
    },
}

LOOP_RESUME = {
    "name": "loop_resume",
    "description": "Resume a paused loop and its cron job.",
    "parameters": {
        "type": "object",
        "properties": {"loop_id": {"type": "string", "description": "Loop id (empty = most recent)"}},
        "required": [],
    },
}

LOOP_STOP = {
    "name": "loop_stop",
    "description": "Stop a loop permanently (state retained for audit). Disables associated cron.",
    "parameters": {
        "type": "object",
        "properties": {"loop_id": {"type": "string", "description": "Loop id (empty = most recent)"}},
        "required": [],
    },
}

LOOP_RECOMMEND = {
    "name": "loop_recommend",
    "description": (
        "Analyze the repository and suggest 3-5 concrete improvements using the host LLM. "
        "Returns title, why, effort (S/M/L), and a one-line fix sketch per suggestion."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "loop_id": {"type": "string", "description": "Loop id (empty = most recent)"},
        },
        "required": [],
    },
}

LOOP_COMMIT = {
    "name": "loop_commit",
    "description": (
        "Commit current changes to a loop branch and open a PR. Use after fixing a failure. "
        "Refuses to run when the work tree is clean, reuses the loop's existing working "
        "branch instead of nesting a new one, and records the PR in loop state."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "loop_id": {"type": "string", "description": "Loop id (empty = most recent)"},
            "commit_message": {"type": "string", "description": "Commit message"},
            "pr_title": {"type": "string", "description": "PR title"},
            "pr_body": {"type": "string", "description": "PR body/description"},
            "dry_run": {"type": "boolean", "description": "Report the diff stat without writing anything"},
            "security_gate": {
                "type": "boolean",
                "description": "Override the loop's security_gate setting for this commit",
            },
        },
        "required": [],
    },
}

LOOP_REVERT = {
    "name": "loop_revert",
    "description": (
        "Roll back the loop's most recent PR: close it, delete its branch, and return the "
        "work tree to the previous branch. Use when an automated fix made things worse."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "loop_id": {"type": "string", "description": "Loop id (empty = most recent)"},
        },
        "required": [],
    },
}
