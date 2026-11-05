"""Tool handlers — the code that runs when the LLM calls each tool."""

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

_STATE_DIR = Path.home() / ".hermes" / "looprunner"
_STATE_FILE = _STATE_DIR / "loops.json"


def _load_state():
    try:
        if _STATE_FILE.exists():
            return json.loads(_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {"loops": {}}


def _save_state(state):
    try:
        _STATE_DIR.mkdir(parents=True, exist_ok=True)
        _STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
        return True
    except Exception as e:
        return str(e)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _most_recent(loops):
    if not loops:
        return None
    return max(loops.values(), key=lambda x: x.get("updated_at", ""))


def loop_start(args: dict, **kwargs) -> str:
    repo_path = (args.get("repo_path") or "").strip()
    if not repo_path:
        return json.dumps({"error": "repo_path is required"})
    if not Path(repo_path).exists():
        return json.dumps({"error": f"repo_path does not exist: {repo_path}"})

    cfg = kwargs.get("config") or {}
    verify_cmd = (args.get("verify_cmd") or cfg.get("verify_cmd") or "").strip()
    max_rounds = args.get("max_rounds") or cfg.get("max_rounds") or 10

    state = _load_state()
    loop_id = uuid.uuid4().hex[:12]
    state["loops"][loop_id] = {
        "id": loop_id,
        "repo_path": repo_path,
        "verify_cmd": verify_cmd,
        "max_rounds": max_rounds,
        "status": "running",
        "round": 0,
        "created_at": _now(),
        "updated_at": _now(),
        "history": [],
    }
    err = _save_state(state)
    if err is not True:
        return json.dumps({"error": f"state save failed: {err}"})
    return json.dumps({
        "loop_id": loop_id,
        "status": "running",
        "repo_path": repo_path,
        "verify_cmd": verify_cmd,
        "max_rounds": max_rounds,
        "message": "Loop started. Use loop_status to monitor.",
    })


def loop_status(args: dict, **kwargs) -> str:
    loop_id = (args.get("loop_id") or "").strip()
    state = _load_state()
    loops = state.get("loops", {})
    if loop_id:
        loop = loops.get(loop_id)
        if not loop:
            return json.dumps({"error": f"unknown loop_id: {loop_id}"})
    else:
        loop = _most_recent(loops)
        if not loop:
            return json.dumps({"error": "no loops found"})
    return json.dumps({
        "loop_id": loop["id"],
        "status": loop["status"],
        "repo_path": loop["repo_path"],
        "round": loop["round"],
        "max_rounds": loop["max_rounds"],
        "verify_cmd": loop["verify_cmd"],
        "last_result": loop["history"][-1] if loop["history"] else None,
        "rounds_completed": len(loop["history"]),
    })


def _set_status(args, new_status):
    loop_id = (args.get("loop_id") or "").strip()
    state = _load_state()
    loops = state.get("loops", {})
    if loop_id:
        loop = loops.get(loop_id)
    else:
        loop = _most_recent(loops)
    if not loop:
        return json.dumps({"error": "no loop found"})
    loop["status"] = new_status
    loop["updated_at"] = _now()
    err = _save_state(state)
    if err is not True:
        return json.dumps({"error": f"state save failed: {err}"})
    return json.dumps({"loop_id": loop["id"], "status": new_status})


def loop_pause(args: dict, **kwargs) -> str:
    return _set_status(args, "paused")


def loop_resume(args: dict, **kwargs) -> str:
    return _set_status(args, "running")


def loop_stop(args: dict, **kwargs) -> str:
    return _set_status(args, "stopped")