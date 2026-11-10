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
            "deadline": {"type": "string", "description": "ISO date by which goal must be met (auto-disables after)"},
            "interval_minutes": {"type": "integer", "description": "Cron interval in minutes (default 60)"},
            "branch_guard": {"type": "boolean", "description": "Auto-create a working branch when on main/master (default true)"},
            "auto_recommend": {"type": "boolean", "description": "Suggest next improvements after each pass"},
            "auto_fix": {"type": "boolean", "description": "Reserved for future auto-fix rounds"},
            "strict_git_guard": {
                "type": "boolean",
                "description": (
                    "Block direct git commit/push while this loop is active on main/master "
                    "(default true). loop_commit stays allowed."
                ),
            },
            "verify_timeout": {"type": "integer", "description": "Seconds before the verify command is killed (default 300)"},
        },
        "required": ["repo_path"],
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
