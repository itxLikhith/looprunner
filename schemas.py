"""Tool schemas — what the LLM sees."""

LOOP_START = {
    "name": "loop_start",
    "description": (
        "Start a generalized set-and-forget autonomous loop for a given repository. "
        "Runs verify command each round, analyzes failures with LLM, applies fixes, opens PRs. "
        "Supports goal + deadline for auto-prioritization and auto-disable."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "repo_path": {"type": "string", "description": "Absolute path to the repository"},
            "verify_cmd": {"type": "string", "description": "Verification command (overrides config)"},
            "max_rounds": {"type": "integer", "description": "Max rounds before auto-pause"},
            "goal": {"type": "string", "description": "What this loop should accomplish (e.g. 'ship v1.0')"},
            "deadline": {"type": "string", "description": "ISO date by which goal must be met (auto-disables after)"},
            "interval_minutes": {"type": "integer", "description": "Cron interval in minutes (default 60)"},
        },
        "required": ["repo_path"],
    },
}

LOOP_TICK = {
    "name": "loop_tick",
    "description": (
        "Execute one autonomous round: run verify_cmd, if pass increment round, "
        "if fail analyze with LLM, apply fix, commit+PR. Checks deadline and auto-disables if passed."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "loop_id": {"type": "string", "description": "Loop id (empty = most recent active)"},
        },
        "required": [],
    },
}

LOOP_STATUS = {
    "name": "loop_status",
    "description": "Report current loop state: round, status, goal, deadline, last verify result, open PRs.",
    "parameters": {
        "type": "object",
        "properties": {"loop_id": {"type": "string", "description": "Loop id (empty = most recent)"}},
        "required": [],
    },
}

LOOP_PAUSE = {
    "name": "loop_pause",
    "description": "Pause an active loop. Workers finish current round then stop.",
    "parameters": {
        "type": "object",
        "properties": {"loop_id": {"type": "string", "description": "Loop id (empty = most recent)"}},
        "required": [],
    },
}

LOOP_RESUME = {
    "name": "loop_resume",
    "description": "Resume a paused loop.",
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
        "Commit current changes to a branch and open a PR. Use after fixing a failure. "
        "Creates branch auto/round-N, commits all changes, pushes, opens PR via gh."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "loop_id": {"type": "string", "description": "Loop id (empty = most recent)"},
            "commit_message": {"type": "string", "description": "Commit message"},
            "pr_title": {"type": "string", "description": "PR title"},
            "pr_body": {"type": "string", "description": "PR body/description"},
        },
        "required": ["commit_message"],
    },
}

LOOP_COMMIT = {
    "name": "loop_commit",
    "description": (
        "Commit current changes to a branch and open a PR. Use after fixing a failure. "
        "Creates branch auto/round-N, commits all changes, pushes, opens PR via gh."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "loop_id": {"type": "string", "description": "Loop id (empty = most recent)"},
            "commit_message": {"type": "string", "description": "Commit message"},
            "pr_title": {"type": "string", "description": "PR title"},
            "pr_body": {"type": "string", "description": "PR body/description"},
        },
        "required": ["commit_message"],
    },
}
