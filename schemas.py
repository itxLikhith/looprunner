"""Tool schemas — what the LLM sees."""

LOOP_START = {
    "name": "loop_start",
    "description": (
        "Start a generalized set-and-forget autonomous loop for a given repository. "
        "Spawns an isolated Hermes profile, runs the configured verify command each round, "
        "and opens PRs for fixes. Use when the user wants continuous autonomous QA on a repo."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "repo_path": {
                "type": "string",
                "description": "Absolute path to the repository to loop over",
            },
            "verify_cmd": {
                "type": "string",
                "description": "Verification command to run each round (overrides config)",
            },
            "max_rounds": {
                "type": "integer",
                "description": "Max rounds before auto-pause (overrides config)",
            },
        },
        "required": ["repo_path"],
    },
}

LOOP_STATUS = {
    "name": "loop_status",
    "description": "Report current loop state: round, status, last verify result, open PRs.",
    "parameters": {
        "type": "object",
        "properties": {
            "loop_id": {
                "type": "string",
                "description": "Loop id (empty = most recent)",
            },
        },
        "required": [],
    },
}

LOOP_PAUSE = {
    "name": "loop_pause",
    "description": "Pause an active loop. Workers finish current round then stop.",
    "parameters": {
        "type": "object",
        "properties": {
            "loop_id": {"type": "string", "description": "Loop id (empty = most recent)"},
        },
        "required": [],
    },
}

LOOP_RESUME = {
    "name": "loop_resume",
    "description": "Resume a paused loop.",
    "parameters": {
        "type": "object",
        "properties": {
            "loop_id": {"type": "string", "description": "Loop id (empty = most recent)"},
        },
        "required": [],
    },
}

LOOP_STOP = {
    "name": "loop_stop",
    "description": "Stop a loop permanently (state retained for audit).",
    "parameters": {
        "type": "object",
        "properties": {
            "loop_id": {"type": "string", "description": "Loop id (empty = most recent)"},
        },
        "required": [],
    },
}