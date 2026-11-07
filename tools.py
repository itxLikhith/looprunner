"""Tool handlers."""

import json
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

# Module-level ctx reference (set by __init__ at registration)
_ctx = None

def set_ctx(ctx):
    global _ctx
    _ctx = ctx

_STATE_DIR = Path.home() / ".hermes" / "looprunner"
_STATE_FILE = _STATE_DIR / "loops.json"


def _load_state():
    try:
        if _STATE_FILE.exists():
            return json.loads(_STATE_FILE.read_text(encoding='utf-8'))
    except Exception:
        pass
    return {'loops': {}}


def _save_state(state):
    try:
        _STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        tmp.replace(_STATE_FILE)
        return True
    except Exception as e:
        return str(e)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _most_recent(loops, active_only=False):
    cand = [l for l in loops.values() if (l.get('status') == 'running' if active_only else True)]
    if not cand:
        return None
    return max(cand, key=lambda x: x.get('updated_at', ''))


def _deadline_passed(loop):
    dl = loop.get('deadline')
    if not dl:
        return False
    try:
        d = datetime.fromisoformat(dl.replace('Z', '+00:00'))
        return datetime.now(timezone.utc) > d
    except Exception:
        return False


def loop_start(args, **kwargs):
    repo_path = (args.get('repo_path') or '').strip()
    if not repo_path:
        return json.dumps({'error': 'repo_path is required'})
    if not Path(repo_path).exists():
        return json.dumps({'error': f'repo_path does not exist: {repo_path}'})

    cfg = kwargs.get('config') or {}
    verify_cmd = (args.get('verify_cmd') or cfg.get('verify_cmd') or '').strip()
    max_rounds = args.get('max_rounds') or cfg.get('max_rounds') or 10
    goal = (args.get('goal') or '').strip()
    deadline = (args.get('deadline') or '').strip()
    interval = int(args.get("interval_minutes") or 60)

    state = _load_state()
    loop_id = uuid.uuid4().hex[:12]
    state['loops'][loop_id] = {
        'id': loop_id,
        'repo_path': repo_path,
        'verify_cmd': verify_cmd,
        'max_rounds': max_rounds,
        'goal': goal,
        'deadline': deadline,
        'interval_minutes': interval,
        'status': 'running',
        'round': 0,
        'created_at': _now(),
        'updated_at': _now(),
        'history': [],
    }
    err = _save_state(state)
    if err is not True:
        return json.dumps({"error": f"state save failed: {err}"})

    # Auto-setup cron job for set-and-forget operation
    cron_job_id = None
    try:
        import subprocess, shutil, os
        hermes_bin = shutil.which("hermes")
        if not hermes_bin:
            # Fallback: common install locations
            for candidate in [
                os.path.join(os.path.expanduser("~"), ".hermes", "hermes-agent", ".hermes", "bin", "hermes"),
                os.path.join(os.path.expanduser("~"), "AppData", "Local", "hermes", "hermes-agent", ".hermes", "bin", "hermes"),
            ]:
                if os.path.exists(candidate):
                    hermes_bin = candidate
                    break
        if hermes_bin:
            cron_expr = f"every {interval}m"
            cron_prompt = f"Run loop_tick on loop {loop_id}"
            proc = subprocess.run(
                [hermes_bin, "cron", "create", cron_expr, cron_prompt],
                capture_output=True, text=True, timeout=30,
            )
            if proc.returncode == 0:
                import re
                m = re.search(r"Created job[:\s]+([a-f0-9]+)", proc.stdout + proc.stderr, re.I)
                cron_job_id = m.group(1) if m else "created"
            else:
                # Store error for debugging
                cron_job_id = None
                state["loops"][loop_id]["_cron_error"] = (proc.stdout + proc.stderr).strip()[:500]
    except Exception as e:
        state["loops"][loop_id]["_cron_error"] = f"exception: {e}"

    state["loops"][loop_id]["cron_job_id"] = cron_job_id
    _save_state(state)

    msg = "Loop started. Cron auto-scheduled every " + str(interval) + "m." if cron_job_id else "Loop started. Run loop_tick manually or set up cron."
    if goal:
        msg += f" Goal: {goal}."
    if deadline:
        msg += f" Deadline: {deadline} (auto-disables after)."
    return json.dumps({
        "loop_id": loop_id,
        "status": "running",
        "repo_path": repo_path,
        "verify_cmd": verify_cmd,
        "max_rounds": max_rounds,
        "goal": goal,
        "deadline": deadline,
        "interval_minutes": interval,
        "cron_job_id": cron_job_id,
        "message": msg,
    })


def loop_tick(args, **kwargs):
    loop_id = (args.get('loop_id') or '').strip()
    state = _load_state()
    loops = state.get('loops', {})
    if loop_id:
        loop = loops.get(loop_id)
    else:
        loop = _most_recent(loops, active_only=True)
    if not loop:
        return json.dumps({'error': 'no active loop found'})
    if loop.get('status') != 'running':
        return json.dumps({'error': f"loop {loop['id']} is {loop['status']}, not running"})

    if _deadline_passed(loop):
        loop['status'] = 'completed'
        loop['updated_at'] = _now()
        loop['history'].append({'round': loop['round'], 'type': 'deadline', 'at': _now(),
                                'note': 'deadline passed, auto-disabled'})
        _save_state(state)
        return json.dumps({'loop_id': loop['id'], 'status': 'completed',
                           'reason': 'deadline passed', 'round': loop['round']})

    if loop['round'] >= loop['max_rounds']:
        loop['status'] = 'completed'
        loop['updated_at'] = _now()
        _save_state(state)
        return json.dumps({'loop_id': loop['id'], 'status': 'completed',
                           'reason': 'max_rounds reached', 'round': loop['round']})

    verify_cmd = (loop.get('verify_cmd') or '').strip()
    if not verify_cmd:
        return json.dumps({'error': 'no verify_cmd configured for this loop'})

    try:
        proc = subprocess.run(
            verify_cmd, shell=True, cwd=loop['repo_path'],
            capture_output=True, text=True, timeout=300,
        )
        passed = (proc.returncode == 0)
        output = (proc.stdout + chr(10) + proc.stderr).strip()
        if len(output) > 4000:
            output = output[:2000] + chr(10) + '...[truncated]...' + chr(10) + output[-2000:]
    except subprocess.TimeoutExpired:
        passed = False
        output = 'verify command timed out (300s)'
    except Exception as e:
        passed = False
        output = f'verify command error: {e}'

    entry = {
        'round': loop['round'] + 1,
        'type': 'verify',
        'at': _now(),
        'passed': passed,
        'output': output,
    }
    loop['history'].append(entry)
    loop['updated_at'] = _now()

    if passed:
        loop['round'] += 1
        _save_state(state)
        return json.dumps({
            'loop_id': loop['id'],
            'status': 'running',
            'round': loop['round'],
            'result': 'PASS',
            'message': f"Round {loop['round']} passed. Run loop_tick for next round.",
        })

    _save_state(state)
    branch = f"auto/round-{loop['round'] + 1}"
    return json.dumps({
        "loop_id": loop["id"],
        "status": "running",
        "round": loop["round"],
        "result": "FAIL",
        "verify_cmd": verify_cmd,
        "output": output,
        "branch": branch,
        "message": (
            "Verify failed. Analyze the output above, apply a minimal fix in the repo. "
            "IMPORTANT: Do NOT run 'git commit' or 'git push' directly. "
            "You MUST use the loop_commit tool with branch='" + branch + "' to commit+open a PR. "
            "loop_commit creates a branch, commits, pushes, and opens a PR via gh. "
            "After loop_commit returns a PR URL, run loop_tick again to verify the fix. "
            "Direct commits to master/main are NOT allowed — always use loop_commit."
        ),
    })


def loop_status(args, **kwargs):
    loop_id = (args.get('loop_id') or '').strip()
    state = _load_state()
    loops = state.get('loops', {})
    if loop_id:
        loop = loops.get(loop_id)
        if not loop:
            return json.dumps({'error': f'unknown loop_id: {loop_id}'})
    else:
        loop = _most_recent(loops)
        if not loop:
            return json.dumps({'error': 'no loops found'})
    return json.dumps({
        'loop_id': loop['id'],
        'status': loop['status'],
        'repo_path': loop['repo_path'],
        'round': loop['round'],
        'max_rounds': loop['max_rounds'],
        'verify_cmd': loop['verify_cmd'],
        'goal': loop.get('goal', ''),
        'deadline': loop.get('deadline', ''),
        'interval_minutes': loop.get('interval_minutes', 60),
        'last_result': loop['history'][-1] if loop['history'] else None,
        'rounds_completed': len([h for h in loop['history'] if h.get('type') == 'verify']),
        'passes': len([h for h in loop['history'] if h.get('passed')]),
        'fails': len([h for h in loop['history'] if h.get('passed') is False]),
    })


def _set_status(args, new_status):
    loop_id = (args.get('loop_id') or '').strip()
    state = _load_state()
    loops = state.get('loops', {})
    if loop_id:
        loop = loops.get(loop_id)
    else:
        loop = _most_recent(loops)
    if not loop:
        return json.dumps({'error': 'no loop found'})
    if new_status in ("stopped", "completed"):
        cron_id = loop.get("cron_job_id")
        if cron_id and cron_id != "created":
            try:
                import subprocess
                subprocess.run(["hermes", "cron", "pause", cron_id],
                               capture_output=True, text=True, timeout=15)
            except Exception:
                pass

    loop["status"] = new_status
    loop["updated_at"] = _now()
    err = _save_state(state)
    if err is not True:
        return json.dumps({'error': f'state save failed: {err}'})
    return json.dumps({'loop_id': loop['id'], 'status': new_status})


def loop_pause(args, **kwargs):
    return _set_status(args, "paused")


def loop_resume(args, **kwargs):
    return _set_status(args, "running")


def loop_recommend(args, **kwargs):
    """Use ctx.llm to analyze the repo and suggest improvements."""
    loop_id = (args.get("loop_id") or "").strip()
    state = _load_state()
    loops = state.get("loops", {})
    if loop_id:
        loop = loops.get(loop_id)
    else:
        loop = _most_recent(loops)
    if not loop:
        return json.dumps({"error": "no loop found"})

    repo_path = loop["repo_path"]
    import os
    files = []
    for root, dirs, fnames in os.walk(repo_path):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", "node_modules", ".venv")]
        for fn in fnames:
            if fn.endswith((".py", ".yaml", ".yml", ".md", ".toml", ".json")):
                rel = os.path.relpath(os.path.join(root, fn), repo_path)
                files.append(rel)
        if len(files) > 60:
            break
    file_list = chr(10).join(files[:60])

    readme = ""
    for name in ("README.md", "README.rst", "README.txt"):
        p = os.path.join(repo_path, name)
        if os.path.exists(p):
            try:
                readme = open(p, encoding="utf-8").read()[:3000]
            except Exception:
                pass
            break

    prompt = (
        "You are reviewing a software repository for improvements. "
        "Repo: " + repo_path + chr(10) +
        "Files:" + chr(10) + file_list + chr(10) + chr(10) +
        "README (truncated):" + chr(10) + readme + chr(10) + chr(10) +
        "Goal: " + (loop.get("goal", "improve quality")) + chr(10) + chr(10) +
        "Suggest 3-5 concrete, actionable improvements. "
        "For each: title, why it matters, estimated effort (S/M/L), and a one-line fix sketch. "
        "Return as a JSON array of objects with keys: title, why, effort, fix. "
        "Be specific to THIS repo, not generic advice."
    )

    # Try ctx.llm first
    if _ctx is not None and hasattr(_ctx, "llm"):
        try:
            resp = _ctx.llm.complete(messages=[{"role": "user", "content": prompt}], task="looprunner_review")
            text = resp.text if hasattr(resp, "text") else str(resp)
            if text and text.strip():
                try:
                    parsed = json.loads(text)
                except Exception:
                    parsed = {"raw": text}
                return json.dumps({
                    "loop_id": loop["id"],
                    "recommendations": parsed,
                })
        except Exception:
            pass

    # Fallback: use subprocess to call hermes chat
    try:
        import subprocess
        proc = subprocess.run(
            ["hermes", "chat", "-q", prompt[:2000]],
            capture_output=True, text=True, timeout=120,
        )
        text = (proc.stdout + proc.stderr).strip()
        if text:
            return json.dumps({
                "loop_id": loop["id"],
                "recommendations": {"raw": text[:4000]},
            })
    except Exception:
        pass

    return json.dumps({
        "loop_id": loop["id"],
        "prompt": prompt,
        "note": "Analyze this prompt to get improvement recommendations.",
    })


def loop_commit(args, **kwargs):
    loop_id = (args.get("loop_id") or "").strip()
    state = _load_state()
    loops = state.get("loops", {})
    if loop_id:
        loop = loops.get(loop_id)
    else:
        loop = _most_recent(loops)
    if not loop:
        return json.dumps({"error": "no loop found"})

    repo_path = loop["repo_path"]
    commit_msg = (args.get("commit_message") or "looprunner: auto-fix").strip()
    pr_title = (args.get("pr_title") or commit_msg).strip()
    pr_body = (args.get("pr_body") or "Automated fix by LoopRunner.").strip()
    round_n = loop["round"] + 1
    branch = f"auto/round-{round_n}"

    import subprocess
    steps = []
    try:
        r = subprocess.run(f"git checkout -b {branch}", shell=True, cwd=repo_path,
                           capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            r = subprocess.run(f"git checkout {branch}", shell=True, cwd=repo_path,
                               capture_output=True, text=True, timeout=30)
        steps.append({"step": "checkout", "ok": r.returncode == 0, "out": (r.stderr or r.stdout).strip()})

        r = subprocess.run("git add -A", shell=True, cwd=repo_path,
                           capture_output=True, text=True, timeout=30)
        steps.append({"step": "add", "ok": r.returncode == 0})

        r = subprocess.run(f'git commit -m "{commit_msg}"', shell=True, cwd=repo_path,
                           capture_output=True, text=True, timeout=30)
        steps.append({"step": "commit", "ok": r.returncode == 0, "out": (r.stdout + r.stderr).strip()})

        r = subprocess.run(f"git push -u origin {branch}", shell=True, cwd=repo_path,
                           capture_output=True, text=True, timeout=60)
        steps.append({"step": "push", "ok": r.returncode == 0, "out": (r.stdout + r.stderr).strip()})

        r = subprocess.run(f'gh pr create --title "{pr_title}" --body "{pr_body}"',
                           shell=True, cwd=repo_path, capture_output=True, text=True, timeout=60)
        pr_url = r.stdout.strip() if r.returncode == 0 else ""
        steps.append({"step": "pr", "ok": r.returncode == 0, "out": (r.stdout + r.stderr).strip()})

        return json.dumps({
            "loop_id": loop["id"],
            "branch": branch,
            "pr_url": pr_url,
            "steps": steps,
        })
    except Exception as e:
        return json.dumps({"loop_id": loop["id"], "error": str(e), "steps": steps})


def loop_stop(args, **kwargs):
    return _set_status(args, "stopped")
