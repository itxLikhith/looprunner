"""LoopRunner tool handlers.

Autonomous QA loop runner for Hermes Agent.

State lives in the plugin data dir (``plugin_data_dir("looprunner")``) so it
survives ``hermes plugins update`` / ``remove``. Loops created by v0.7.x in
``~/.hermes/looprunner/loops.json`` are migrated on first load.

Design notes
------------
* A loop has two counters: ``round`` (passes) and ``attempts`` (verify runs).
  ``max_rounds`` bounds success, ``max_attempts`` bounds total work, so a loop
  that keeps failing cannot run forever.
* Every failure is reduced to a normalized *signature*. The same signature
  repeating ``stall_threshold`` times marks the loop ``stalled`` and pauses its
  cron -- the loop escalates instead of retrying blindly.
* All mutating entry points take a per-loop exclusive lock, so two cron ticks
  cannot interleave and corrupt state.
"""

import hashlib
import json
import os
import re
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

# Module-level ctx reference (set by __init__ at registration)
_ctx = None

STATE_VERSION = 2

# Statuses that mean "the loop is no longer doing work".
TERMINAL = ("stopped", "completed", "stalled")

# Generated artifacts that a verify run can leave behind. Committing these is
# noise at best and a broken build at worst, so both the diff gate and the
# ``git add`` step exclude them.
_ARTIFACT_PATTERNS = (
    r"__pycache__", r"\.py[co]$", r"\.pytest_cache", r"\.mypy_cache", r"\.ruff_cache",
    r"node_modules", r"\.venv", r"\.tox", r"\.coverage", r"htmlcov", r"\.DS_Store",
    r"(^|\\|/)(dist|build|target)(/|\\|$)", r"\.egg-info",
)
_ARTIFACT_RE = re.compile("|".join("(?:%s)" % p for p in _ARTIFACT_PATTERNS))

# git pathspec excludes matching the same set (used for ``git add``).
_ADD_EXCLUDES = [
    ":(exclude)*.pyc", ":(exclude)*.pyo", ":(exclude)*.coverage", ":(exclude)*.DS_Store",
    ":(exclude,glob)**/__pycache__/**", ":(exclude,glob)**/.pytest_cache/**",
    ":(exclude,glob)**/.mypy_cache/**", ":(exclude,glob)**/.ruff_cache/**",
    ":(exclude,glob)**/node_modules/**", ":(exclude,glob)**/.venv/**",
    ":(exclude,glob)**/.tox/**", ":(exclude,glob)**/dist/**",
    ":(exclude,glob)**/build/**", ":(exclude,glob)**/target/**",
    ":(exclude,glob)**/*.egg-info/**", ":(exclude,glob)**/htmlcov/**",
]


def _is_artifact(path):
    """True when a ``git status --porcelain`` path is a generated artifact."""
    normalized = (path or "").replace("\\", "/")
    # porcelain v1: "XY PATH" -- the path starts at column 3
    if len(normalized) > 3 and normalized[2] == " ":
        normalized = normalized[3:]
    if " -> " in normalized:  # rename: "OLD -> NEW"
        normalized = normalized.split(" -> ", 1)[1]
    return bool(_ARTIFACT_RE.search(normalized))


def set_ctx(ctx):
    global _ctx
    _ctx = ctx


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #

_STATE_DIR = None
_STATE_FILE = None


def _default_state_dir():
    try:
        from plugins.plugin_storage import plugin_data_dir
        return Path(plugin_data_dir("looprunner"))
    except Exception:
        return Path.home() / ".hermes" / "looprunner"


def _paths():
    global _STATE_DIR, _STATE_FILE
    if _STATE_DIR is None or _STATE_FILE is None:
        _STATE_DIR = _default_state_dir()
        _STATE_FILE = _STATE_DIR / "loops.json"
    assert _STATE_DIR is not None and _STATE_FILE is not None
    return _STATE_DIR, _STATE_FILE


def _migrate(raw):
    """Bring any loaded state up to STATE_VERSION."""
    if not isinstance(raw, dict):
        raw = {"loops": {}}
    loops = raw.get("loops") or {}
    for loop in loops.values():
        if not isinstance(loop, dict):
            continue
        loop.setdefault("round", 0)
        loop.setdefault("attempts", 0)
        loop.setdefault("max_attempts", 20)
        loop.setdefault("stall_threshold", 3)
        loop.setdefault("auto_fix", False)
        loop.setdefault("strict_git_guard", True)
        loop.setdefault("status", "running")
        loop.setdefault("prs", [])
        loop.setdefault("history", [])
        loop.setdefault("last_signature", None)
        loop.setdefault("signature_counts", {})
        loop.setdefault("created_at", _now())
        loop.setdefault("updated_at", _now())
        loop.setdefault("completed_at", None)
        loop.setdefault("interval_minutes", 60)
        loop.setdefault("branch_guard", True)
        loop.setdefault("verify_timeout", 300)
        loop.setdefault("cron_job_id", None)
        loop.setdefault("goal_cmd", "")
        loop.setdefault("checkpoint_on_stall", True)
        loop.setdefault("security_gate", False)
        loop.setdefault("mode", "fix")
        loop.setdefault("assignee", None)
        loop.setdefault("labels", [])
        loop.setdefault("goal_max_turns", 20)
        loop.setdefault("seen_issues", {})
        loop.setdefault("watch_cards", [])
        for entry in loop.get("history", []):
            entry.setdefault("attempt", entry.get("round", 0))
            entry.setdefault("duration_s", None)
            entry.setdefault("signature", None)
    raw["version"] = STATE_VERSION
    raw["loops"] = loops
    return raw


def _load_state():
    _dir, path = _paths()
    raw = None
    try:
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        raw = None
    if raw is None:
        # v0.7.x legacy location
        try:
            legacy = Path.home() / ".hermes" / "looprunner" / "loops.json"
            if legacy.exists():
                raw = json.loads(legacy.read_text(encoding="utf-8"))
        except Exception:
            raw = None
    if raw is None:
        raw = {"loops": {}}
    return _migrate(raw)


def _save_state(state):
    _dir, path = _paths()
    try:
        _dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        tmp.replace(path)
        return True
    except Exception as e:
        return str(e)


def _now():
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# Locking
# --------------------------------------------------------------------------- #

class _LoopLock:
    """Cross-process exclusive lock via an exclusive-create lock file.

    Cron ticks for the same loop must not interleave: two concurrent ticks would
    both read the same state, both run the verify command, and the second save
    would silently drop the first one's history entry.
    """

    def __init__(self, name="looprunner", timeout=60, stale_seconds=900):
        _dir, _ = _paths()
        self.path = _dir / (".%s.lock" % name)
        self.timeout = timeout
        self.stale_seconds = stale_seconds
        self.fd = None

    def __enter__(self):
        deadline = time.time() + self.timeout
        while True:
            try:
                if (time.time() - self.path.stat().st_mtime) > self.stale_seconds:
                    self.path.unlink()
            except Exception:
                pass
            try:
                self.fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self.fd, str(os.getpid()).encode())
                return self
            except FileExistsError:
                if time.time() > deadline:
                    raise TimeoutError(
                        "another loop operation is in progress (lock held > %ss)" % self.timeout
                    )
                time.sleep(0.25)

    def __exit__(self, *exc):
        try:
            if self.fd is not None:
                os.close(self.fd)
        except Exception:
            pass
        try:
            self.path.unlink()
        except Exception:
            pass
        return False


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _err(msg):
    return json.dumps({"error": msg})


def _most_recent(loops, active_only=False):
    cand = [l for l in loops.values() if (l.get("status") == "running" if active_only else True)]
    if not cand:
        return None
    return max(cand, key=lambda x: x.get("updated_at", ""))


def _resolve_loop(loops, loop_id, active_only=False):
    if loop_id:
        return loops.get(loop_id)
    return _most_recent(loops, active_only=active_only)


def _deadline_passed(loop):
    dl = loop.get("deadline")
    if not dl:
        return False
    try:
        d = datetime.fromisoformat(dl.replace("Z", "+00:00"))
        return datetime.now(timezone.utc) > d
    except Exception:
        return False


_NUMBER_RE = re.compile(r"\b\d+(?:\.\d+)?\b")
_HASH_RE = re.compile(r"\b[0-9a-f]{7,40}\b")
_PATH_RE = re.compile(r"(?:[A-Za-z]:)?[\\/][^\s:\"']+")


def _signature(output):
    """Reduce a failure to a stable fingerprint.

    Numbers, hashes and absolute paths are normalized away so that the same
    assertion failing with different values still counts as the *same* failure.
    That is what makes stall detection useful: retrying a fix that does not
    address the root cause produces the same signature every time.
    """
    s = (output or "").lower()
    s = _HASH_RE.sub("SHA", s)
    s = _PATH_RE.sub("PATH", s)
    s = _NUMBER_RE.sub("N", s)
    s = re.sub(r"\s+", " ", s).strip()
    return hashlib.sha256(s.encode("utf-8", "replace")).hexdigest()[:12]


def _first_error_line(output):
    for line in (output or "").splitlines():
        if re.search(r"error|failed|assert|exception|traceback", line, re.I):
            return line.strip()[:200]
    lines = (output or "").strip().splitlines()
    return lines[0][:200] if lines else ""


def _hermes_bin():
    import shutil
    found = shutil.which("hermes")
    if found:
        return found
    for candidate in (
        os.path.join(os.path.expanduser("~"), ".hermes", "hermes-agent", ".hermes", "bin", "hermes"),
        os.path.join(os.path.expanduser("~"), "AppData", "Local", "hermes", "hermes-agent", ".hermes", "bin", "hermes"),
    ):
        if os.path.exists(candidate):
            return candidate
    return "hermes"


def _gh_path():
    """Resolve the real gh.exe, not an MSYS bash shim.

    ``subprocess.run([...])`` without a shell goes through ``CreateProcess``,
    which cannot execute an extensionless bash shim. When Git Bash's bin dir is
    on PATH, ``shutil.which("gh")`` returns exactly such a shim
    (``/c/Program Files/GitHub CLI/gh``) and the PR step dies with WinError 2.
    Prefer the known ``.exe`` install locations first.
    """
    import shutil
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    local_appdata = os.environ.get(
        "LOCALAPPDATA", os.path.join(os.path.expanduser("~"), "AppData", "Local")
    )
    for candidate in (
        os.path.join(program_files, "GitHub CLI", "gh.exe"),
        os.path.join(local_appdata, "GitHubCLI", "bin", "gh.exe"),
        os.path.join(local_appdata, "Programs", "GitHub CLI", "gh.exe"),
    ):
        if os.path.isfile(candidate):
            return candidate
    found = shutil.which("gh")
    if found and found.lower().endswith(".exe") and os.path.isfile(found):
        return found
    return "gh"


def _git_path():
    """Resolve the real git.exe, same MSYS-shim reasoning as ``_gh_path``."""
    import shutil
    for candidate in (
        os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Git", "cmd", "git.exe"),
        os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Git", "bin", "git.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Git", "cmd", "git.exe"),
    ):
        if os.path.isfile(candidate):
            return candidate
    found = shutil.which("git")
    if found and found.lower().endswith(".exe") and os.path.isfile(found):
        return found
    return "git"


def _run(argv, cwd=None, timeout=60):
    """Run a command without a shell; return (rc, combined_output)."""
    try:
        proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "command timed out after %ss" % timeout
    except Exception as e:
        return 127, "command error: %s" % e


def _normalize_cmd(cmd, cwd=None):
    """Make a verify command usable through cmd.exe on Windows.

    ``cmd.exe`` resolves ``.venv\\Scripts\\python.exe`` but not
    ``.venv/Scripts/python.exe`` -- the latter is parsed as the command ``.venv``
    followed by stray arguments, which surfaces as "'.venv' is not recognized".
    When the leading token is a relative path that exists with backslashes,
    rewrite it. ``cwd`` is the repository, not the Hermes process CWD.
    """
    if os.name != "nt":
        return cmd
    m = re.match(r"^\s*([^\s]+)\s*(.*)$", cmd, re.S)
    if not m:
        return cmd
    head, rest = m.group(1), m.group(2)
    if "/" in head and "\\" not in head:
        alt = head.replace("/", "\\")
        base = Path(cwd) if cwd else Path.cwd()
        if (base / alt).exists() or (base / head).exists():
            return alt + ((" " + rest) if rest else "")
    return cmd


def _current_branch(repo_path):
    rc, out = _run([_git_path(), "branch", "--show-current"], cwd=repo_path, timeout=10)
    return out.strip() if rc == 0 else None


def _is_loop_branch(branch):
    return bool(branch and (branch.startswith("looprunner/") or branch.startswith("auto/")))


def _cron_action(cron_id, action):
    """Pause or resume the loop's cron job. Returns True on success."""
    if not cron_id or cron_id == "created":
        return False
    try:
        proc = subprocess.run(
            [_hermes_bin(), "cron", action, cron_id],
            capture_output=True, text=True, timeout=15,
        )
        return proc.returncode == 0
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #

def loop_start(args, **kwargs):
    repo_path = (args.get("repo_path") or "").strip()
    if not repo_path:
        return _err("repo_path is required")
    if not Path(repo_path).exists():
        return _err("repo_path does not exist: %s" % repo_path)

    cfg = kwargs.get("config") or {}
    verify_cmd = (args.get("verify_cmd") or cfg.get("verify_cmd") or "").strip()
    max_rounds = args.get("max_rounds") or cfg.get("max_rounds") or 10
    max_attempts = args.get("max_attempts") or cfg.get("max_attempts") or 20
    stall_threshold = args.get("stall_threshold") or cfg.get("stall_threshold") or 3
    goal = (args.get("goal") or "").strip()
    deadline = (args.get("deadline") or "").strip()
    interval_raw = args.get("interval_minutes") or cfg.get("interval_minutes") or 60
    try:
        interval = int(str(interval_raw).replace("m", "").replace("min", "").strip())
    except (ValueError, TypeError):
        interval = 60
    branch_guard = args.get("branch_guard", cfg.get("branch_guard", True))
    auto_recommend = args.get("auto_recommend", cfg.get("auto_recommend", False))
    auto_fix = args.get("auto_fix", cfg.get("auto_fix", False))
    strict_git_guard = args.get("strict_git_guard", cfg.get("strict_git_guard", True))
    verify_timeout = int(args.get("verify_timeout") or cfg.get("verify_timeout") or 300)
    goal_cmd = (args.get("goal_cmd") or cfg.get("goal_cmd") or "").strip()
    checkpoint_on_stall = args.get("checkpoint_on_stall", cfg.get("checkpoint_on_stall", True))
    security_gate = args.get("security_gate", cfg.get("security_gate", False))
    mode = (args.get("mode") or "fix").strip()
    assignee = (args.get("assignee") or cfg.get("assignee") or "").strip() or None
    labels = args.get("labels") or cfg.get("labels") or []
    if isinstance(labels, str):
        labels = [l.strip() for l in labels.split(",") if l.strip()]
    goal_max_turns = int(args.get("goal_max_turns") or cfg.get("goal_max_turns") or 20)

    with _LoopLock(name="looprunner-start"):
        state = _load_state()
        loop_id = uuid.uuid4().hex[:12]
        state["loops"][loop_id] = {
            "id": loop_id,
            "repo_path": repo_path,
            "verify_cmd": verify_cmd,
            "max_rounds": max_rounds,
            "max_attempts": max_attempts,
            "stall_threshold": stall_threshold,
            "goal": goal,
            "deadline": deadline,
            "interval_minutes": interval,
            "branch_guard": branch_guard,
            "auto_recommend": auto_recommend,
            "auto_fix": auto_fix,
            "strict_git_guard": strict_git_guard,
            "verify_timeout": verify_timeout,
            "goal_cmd": goal_cmd,
            "checkpoint_on_stall": checkpoint_on_stall,
            "security_gate": security_gate,
            "mode": mode,
            "assignee": assignee,
            "labels": labels,
            "goal_max_turns": goal_max_turns,
            "seen_issues": {},
            "watch_cards": [],
            "status": "running",
            "round": 0,
            "attempts": 0,
            "created_at": _now(),
            "updated_at": _now(),
            "completed_at": None,
            "cron_job_id": None,
            "prs": [],
            "history": [],
            "last_signature": None,
            "signature_counts": {},
        }
        err = _save_state(state)
        if err is not True:
            return _err("state save failed: %s" % err)

    # Auto-setup cron job for set-and-forget operation
    cron_job_id = None
    cron_error = None
    try:
        proc = subprocess.run(
            [_hermes_bin(), "cron", "create", "every %dm" % interval,
             "Run loop_tick on loop %s" % loop_id],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode == 0:
            m = re.search(r"Created job[:\s]+([a-f0-9]+)", proc.stdout + proc.stderr, re.I)
            cron_job_id = m.group(1) if m else "created"
        else:
            cron_error = (proc.stdout + proc.stderr).strip()[:500]
    except Exception as e:
        cron_error = "exception: %s" % e

    if cron_job_id or cron_error:
        with _LoopLock(name="loop-%s" % loop_id):
            state = _load_state()
            loop = state["loops"].get(loop_id)
            if loop:
                loop["cron_job_id"] = cron_job_id
                if cron_error:
                    loop["_cron_error"] = cron_error
                _save_state(state)

    msg = (
        "Loop started. Cron auto-scheduled every %dm." % interval
        if cron_job_id
        else "Loop started. Cron was NOT created -- run loop_tick manually."
    )
    if cron_error:
        msg += " Cron error: %s" % cron_error
    if goal:
        msg += " Goal: %s." % goal
    if deadline:
        msg += " Deadline: %s (auto-disables after)." % deadline

    return json.dumps({
        "loop_id": loop_id,
        "status": "running",
        "repo_path": repo_path,
        "verify_cmd": verify_cmd,
        "max_rounds": max_rounds,
        "max_attempts": max_attempts,
        "stall_threshold": stall_threshold,
        "goal": goal,
        "deadline": deadline,
        "interval_minutes": interval,
        "branch_guard": branch_guard,
        "auto_recommend": auto_recommend,
        "auto_fix": auto_fix,
        "strict_git_guard": strict_git_guard,
        "verify_timeout": verify_timeout,
        "cron_job_id": cron_job_id,
        "message": msg,
    })


def _finish_loop(loop, state, reason):
    """Mark the loop terminal and delete its cron jobs.

    The aim is achieved, so the schedule is no longer needed -- a paused job
    would keep showing up as orphaned state.
    """
    loop["status"] = "completed"
    loop["completed_at"] = _now()
    loop["updated_at"] = _now()
    loop["completion_reason"] = reason
    deleted = _cron_delete(loop.get("cron_job_id")) or _cron_delete(loop.get("retry_cron_id"))
    loop["cron_job_id"] = None
    loop["retry_cron_id"] = None
    _save_state(state)
    return deleted


def _run_verify(loop):
    """Run the loop's verify command. Returns (passed, output, duration_s)."""
    verify_cmd = (loop.get("verify_cmd") or "").strip()
    if not verify_cmd:
        return None, "no verify_cmd configured for this loop", 0.0
    timeout = int(loop.get("verify_timeout", 300))
    start = time.time()
    try:
        proc = subprocess.run(
            _normalize_cmd(verify_cmd, loop["repo_path"]), shell=True, cwd=loop["repo_path"],
            capture_output=True, text=True, timeout=timeout,
        )
        passed = proc.returncode == 0
        output = (proc.stdout + "\n" + proc.stderr).strip()
    except subprocess.TimeoutExpired:
        passed = False
        output = "verify command timed out (%ss)" % timeout
    except Exception as e:
        passed = False
        output = "verify command error: %s" % e
    return passed, output, round(time.time() - start, 2)


def loop_tick(args, **kwargs):
    loop_id = (args.get("loop_id") or "").strip()
    with _LoopLock(name=("loop-%s" % loop_id) if loop_id else "looprunner"):
        state = _load_state()
        loops = state.get("loops", {})
        loop = _resolve_loop(loops, loop_id, active_only=True)
        if not loop:
            return _err("no active loop found")
        lid = loop["id"]

        if _deadline_passed(loop):
            loop["history"].append({
                "attempt": loop.get("attempts", 0), "round": loop.get("round", 0),
                "type": "deadline", "at": _now(), "note": "deadline passed, auto-disabled",
            })
            _finish_loop(loop, state, "deadline passed")
            return json.dumps({"loop_id": lid, "status": "completed",
                               "reason": "deadline passed", "round": loop.get("round", 0)})

        if loop.get("attempts", 0) >= int(loop.get("max_attempts", 20)):
            _finish_loop(loop, state, "max_attempts reached")
            return json.dumps({
                "loop_id": lid, "status": "completed", "reason": "max_attempts reached",
                "round": loop.get("round", 0), "attempts": loop.get("attempts", 0),
                "message": "Attempt budget exhausted. The loop stopped rather than retrying forever.",
            })

        # Branch guard: never leave the loop sitting on main/master.
        current_branch = _current_branch(loop["repo_path"])
        if loop.get("branch_guard", True) and current_branch in ("main", "master"):
            work_branch = "looprunner/work-%d" % (loop.get("round", 0) + 1)
            rc, _out = _run([_git_path(), "checkout", "-b", work_branch],
                            cwd=loop["repo_path"], timeout=30)
            if rc == 0:
                current_branch = work_branch

        passed, output, duration = _run_verify(loop)
        if passed is None:
            return _err(output)

        loop["attempts"] = int(loop.get("attempts", 0)) + 1
        entry = {
            "attempt": loop["attempts"],
            "round": loop.get("round", 0),
            "type": "verify",
            "at": _now(),
            "duration_s": duration,
            "passed": passed,
            "output": output[:4000],
        }

        if passed:
            # A goal_cmd, when set, is the real acceptance test: the verify
            # command passing once is not the same as the aim being achieved.
            goal_met = True
            if loop.get("goal_cmd"):
                g_passed, g_output, _g_dur = _run_verify({
                    "verify_cmd": loop["goal_cmd"],
                    "repo_path": loop["repo_path"],
                    "verify_timeout": loop.get("verify_timeout", 300),
                })
                goal_met = bool(g_passed)
                entry["goal_cmd"] = loop["goal_cmd"]
                entry["goal_output"] = (g_output or "")[:2000]
                entry["goal_met"] = goal_met

            loop["round"] = int(loop.get("round", 0)) + 1
            loop["last_signature"] = None
            loop["signature_counts"] = {}
            entry["signature"] = None
            loop["history"].append(entry)
            loop["updated_at"] = _now()

            if goal_met and loop["round"] >= int(loop.get("max_rounds", 10)):
                _finish_loop(loop, state, "goal achieved")
                return json.dumps({
                    "loop_id": lid, "status": "completed", "round": loop["round"],
                    "attempts": loop["attempts"], "result": "PASS",
                    "reason": "goal achieved",
                    "duration_s": duration, "branch": current_branch,
                    "message": ("Goal achieved in %d round(s); the loop's cron job was "
                                "deleted." % loop["round"]),
                })

            _save_state(state)
            resp = {
                "loop_id": lid, "status": loop["status"], "round": loop["round"],
                "attempts": loop["attempts"], "result": "PASS",
                "duration_s": duration, "branch": current_branch,
                "message": "Round %d passed in %ss." % (loop["round"], duration),
            }
            if not goal_met:
                resp["goal_met"] = False
                resp["message"] += (" The goal command (%s) still fails, so the loop "
                                    "continues." % loop["goal_cmd"])
            if loop.get("auto_recommend"):
                resp["recommend_next"] = True
                resp["message"] += " Consider loop_recommend for the next improvement."
            return json.dumps(resp)

        # Failure: fingerprint it.
        sig = _signature(output)
        counts = loop.get("signature_counts") or {}
        counts[sig] = counts.get(sig, 0) + 1
        loop["signature_counts"] = counts
        loop["last_signature"] = sig
        entry["signature"] = sig
        entry["error_line"] = _first_error_line(output)
        loop["history"].append(entry)
        loop["updated_at"] = _now()
        repeat = counts[sig]

        if repeat >= int(loop.get("stall_threshold", 3)):
            loop["status"] = "stalled"
            loop["completed_at"] = _now()
            _save_state(state)
            _cron_action(loop.get("cron_job_id"), "pause")

            # The aim was not met. Snapshot the stage, dispatch a goal-mode card
            # to decide the best next step, and schedule a retry so the work is
            # not orphaned.
            checkpoint = None
            if loop.get("checkpoint_on_stall", True):
                checkpoint = _write_checkpoint(loop)
                if "error" not in checkpoint:
                    checkpoints = loop.get("checkpoints") or []
                    checkpoints.append(dict(checkpoint, at=_now()))
                    loop["checkpoints"] = checkpoints
            card_id = None
            try:
                card_id, _raw = _create_resume_card(
                    loop, "same failure signature recurred %d times" % repeat)
            except Exception:
                card_id = None
            retry_id = _ensure_retry_cron(loop, state)
            if checkpoint is not None and "error" not in checkpoint:
                loop["updated_at"] = _now()
            _save_state(state)

            resp = {
                "loop_id": lid, "status": "stalled", "round": loop.get("round", 0),
                "attempts": loop["attempts"], "result": "FAIL",
                "signature": sig, "signature_repeats": repeat,
                "verify_cmd": loop.get("verify_cmd"), "output": output[:2000],
                "error_line": entry.get("error_line"),
                "branch": current_branch,
                "checkpoint": checkpoint if checkpoint and "error" not in checkpoint else None,
                "resume_card": card_id,
                "retry_cron_id": retry_id,
                "message": (
                    "STALLED: the same failure signature has recurred %d times. "
                    "The loop paused its own cron instead of retrying blindly. "
                    "Escalate: run loop_analyze for a structured diagnosis, "
                    "loop_revert to roll back the last attempt, or loop_resume "
                    "after changing the approach." % repeat
                ),
            }
            if checkpoint and "error" not in checkpoint:
                resp["message"] += (" Stage snapshotted to %s."
                                    % checkpoint.get("branch"))
            if card_id:
                resp["message"] += (" A goal-mode card (%s) was dispatched to decide "
                                    "the best next step." % card_id)
            if retry_id:
                resp["message"] += " A retry cron (%s) will re-run the loop later." % retry_id
            return json.dumps(resp)

        _save_state(state)
        branch = "auto/round-%d" % (loop.get("round", 0) + 1)
        return json.dumps({
            "loop_id": lid, "status": loop["status"], "round": loop.get("round", 0),
            "attempts": loop["attempts"], "result": "FAIL",
            "signature": sig, "signature_repeats": repeat,
            "stall_threshold": loop.get("stall_threshold", 3),
            "verify_cmd": loop.get("verify_cmd"), "output": output,
            "error_line": entry.get("error_line"),
            "duration_s": duration, "branch": branch,
            "message": (
                "Verify failed (attempt %d, signature %s seen %dx). Analyze the "
                "output, apply a minimal fix, then use loop_commit with "
                "branch='%s'. Do NOT run git commit/push directly. Re-run "
                "loop_tick to verify the fix."
                % (loop["attempts"], sig, repeat, branch)
            ),
        })


def loop_verify(args, **kwargs):
    """Dry-run the verify command without recording a round."""
    loop_id = (args.get("loop_id") or "").strip()
    state = _load_state()
    loop = _resolve_loop(state.get("loops", {}), loop_id)
    if not loop:
        return _err("no loop found")
    passed, output, duration = _run_verify(loop)
    if passed is None:
        return _err(output)
    return json.dumps({
        "loop_id": loop["id"], "result": "PASS" if passed else "FAIL",
        "duration_s": duration, "output": output[:4000],
        "note": "Dry run -- no round recorded.",
    })


def loop_status(args, **kwargs):
    loop_id = (args.get("loop_id") or "").strip()
    state = _load_state()
    loops = state.get("loops", {})
    if loop_id:
        loop = loops.get(loop_id)
        if not loop:
            return _err("unknown loop_id: %s" % loop_id)
    else:
        loop = _most_recent(loops)
        if not loop:
            return _err("no loops found")

    history = loop.get("history", [])
    created = loop.get("created_at")
    elapsed = None
    try:
        start = datetime.fromisoformat(created.replace("Z", "+00:00"))
        elapsed = round((datetime.now(timezone.utc) - start).total_seconds(), 1)
    except Exception:
        pass

    last_sig = loop.get("last_signature")
    return json.dumps({
        "loop_id": loop["id"],
        "status": loop["status"],
        "repo_path": loop["repo_path"],
        "round": loop.get("round", 0),
        "attempts": loop.get("attempts", 0),
        "max_rounds": loop.get("max_rounds"),
        "max_attempts": loop.get("max_attempts", 20),
        "stall_threshold": loop.get("stall_threshold", 3),
        "verify_cmd": loop.get("verify_cmd"),
        "goal": loop.get("goal", ""),
        "deadline": loop.get("deadline", ""),
        "interval_minutes": loop.get("interval_minutes", 60),
        "branch_guard": loop.get("branch_guard", True),
        "auto_recommend": loop.get("auto_recommend", False),
        "auto_fix": loop.get("auto_fix", False),
        "strict_git_guard": loop.get("strict_git_guard", True),
        "verify_timeout": loop.get("verify_timeout", 300),
        "cron_job_id": loop.get("cron_job_id"),
        "last_signature": last_sig,
        "signature_repeats": (loop.get("signature_counts") or {}).get(last_sig, 0) if last_sig else 0,
        "prs": loop.get("prs", []),
        "last_result": history[-1] if history else None,
        "rounds_completed": len([h for h in history if h.get("type") == "verify" and h.get("passed")]),
        "passes": len([h for h in history if h.get("passed") is True]),
        "fails": len([h for h in history if h.get("passed") is False]),
        "elapsed_s": elapsed,
    })


def loop_history(args, **kwargs):
    """Full round history for a loop."""
    loop_id = (args.get("loop_id") or "").strip()
    state = _load_state()
    loop = _resolve_loop(state.get("loops", {}), loop_id)
    if not loop:
        return _err("no loop found")
    limit = args.get("limit")
    history = loop.get("history", [])
    if isinstance(limit, int) and limit > 0:
        history = history[-limit:]
    return json.dumps({
        "loop_id": loop["id"],
        "status": loop["status"],
        "round": loop.get("round", 0),
        "attempts": loop.get("attempts", 0),
        "count": len(history),
        "history": history,
    })


def loop_analyze(args, **kwargs):
    """Structured LLM diagnosis of the loop's most recent failure.

    Uses ``ctx.llm.complete_structured`` so the diagnosis is typed and can be
    acted on directly. Falls back to returning the prompt when no LLM is
    available (e.g. a bare CLI context).
    """
    loop_id = (args.get("loop_id") or "").strip()
    state = _load_state()
    loop = _resolve_loop(state.get("loops", {}), loop_id)
    if not loop:
        return _err("no loop found")

    failures = [h for h in loop.get("history", []) if h.get("passed") is False]
    if not failures:
        return json.dumps({
            "loop_id": loop["id"], "diagnosis": None,
            "note": "No failures recorded yet -- nothing to analyze.",
        })
    last = failures[-1]

    repo_path = loop["repo_path"]
    files = []
    for root, dirs, fnames in os.walk(repo_path):
        dirs[:] = [d for d in dirs if d not in (
            ".git", "__pycache__", "node_modules", ".venv", ".venv-real", "dist", "build",
        )]
        for fn in fnames:
            if fn.endswith((".py", ".js", ".ts", ".rs", ".go", ".java", ".yaml", ".yml", ".toml", ".md")):
                files.append(os.path.relpath(os.path.join(root, fn), repo_path))
        if len(files) > 40:
            break

    prompt_input = (
        "Repository: %s\n"
        "Verify command: %s\n"
        "Goal: %s\n"
        "Failure signature: %s\n"
        "First error line: %s\n\n"
        "Failure output (truncated):\n%s\n\n"
        "Relevant files:\n%s"
        % (
            repo_path,
            loop.get("verify_cmd"),
            loop.get("goal") or "make the verify command pass",
            last.get("signature"),
            last.get("error_line"),
            (last.get("output") or "")[:6000],
            "\n".join(files[:40]),
        )
    )

    schema = {
        "type": "object",
        "properties": {
            "root_cause": {"type": "string", "description": "One sentence: why the verify command fails."},
            "failing_tests": {"type": "array", "items": {"type": "string"}},
            "files_to_change": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}, "why": {"type": "string"}},
                    "required": ["path"],
                },
            },
            "proposed_fix": {"type": "string", "description": "Concrete fix sketch, 2-5 sentences."},
            "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
            "risk": {"type": "string", "enum": ["low", "medium", "high"]},
        },
        "required": ["root_cause", "proposed_fix", "confidence"],
    }

    if _ctx is None:
        return json.dumps({
            "loop_id": loop["id"], "diagnosis": None, "prompt": prompt_input,
            "note": "No plugin context available -- feed this prompt to the model by hand.",
        })

    try:
        result = _ctx.llm.complete_structured(
            instructions=(
                "You are diagnosing a failing automated check in a software repository. "
                "Identify the root cause, the specific failing tests, the exact files that "
                "must change, and a concrete minimal fix. Be specific to this repo."
            ),
            input=[{"type": "text", "text": prompt_input}],
            json_schema=schema,
            schema_name="looprunner.diagnosis",
            purpose="looprunner.analyze",
            temperature=0.0,
            max_tokens=1024,
        )
    except Exception as e:
        return json.dumps({
            "loop_id": loop["id"], "diagnosis": None, "prompt": prompt_input,
            "note": "ctx.llm failed (%s: %s); feed this prompt to the model by hand."
                    % (type(e).__name__, e),
        })

    parsed = getattr(result, "parsed", None)
    root = (parsed.get("root_cause") or "").strip() if isinstance(parsed, dict) else ""
    fix = (parsed.get("proposed_fix") or "").strip() if isinstance(parsed, dict) else ""
    usable = bool(root) and bool(fix) and not _PLACEHOLDER_RE.match(root) and not _PLACEHOLDER_RE.match(fix)
    if not usable:
        # Structured lane returned nothing actionable (some models emit empty
        # required fields). Fall back to a plain chat call before giving up.
        text = ""
        try:
            chat = _ctx.llm.complete(
                messages=[
                    {"role": "system", "content": "You are a pragmatic staff engineer diagnosing a failing check."},
                    {"role": "user", "content": prompt_input},
                ],
                max_tokens=900, temperature=0.2, purpose="looprunner.analyze-fallback",
            )
            text = (getattr(chat, "text", "") or "").strip()
        except Exception:
            pass
        if not text:
            text = (getattr(result, "text", "") or "").strip()
        return json.dumps({
            "loop_id": loop["id"], "diagnosis": None,
            "raw": text[:4000],
            "note": "Structured lane returned no usable fields; raw output attached.",
        })

    out = {"loop_id": loop["id"], "diagnosis": parsed}
    usage = getattr(result, "usage", None)
    if usage:
        out["usage"] = usage if isinstance(usage, dict) else str(usage)
    return json.dumps(out)


def _set_status(args, new_status):
    loop_id = (args.get("loop_id") or "").strip()
    with _LoopLock(name=("loop-%s" % loop_id) if loop_id else "looprunner"):
        state = _load_state()
        loops = state.get("loops", {})
        loop = _resolve_loop(loops, loop_id)
        if not loop:
            return _err("no loop found")
        if new_status == "running":
            _cron_action(loop.get("cron_job_id"), "resume")
        elif new_status in TERMINAL:
            _cron_action(loop.get("cron_job_id"), "pause")

        loop["status"] = new_status
        loop["updated_at"] = _now()
        if new_status in TERMINAL:
            loop["completed_at"] = _now()
        err = _save_state(state)
        if err is not True:
            return _err("state save failed: %s" % err)
    return json.dumps({"loop_id": loop["id"], "status": new_status})


def loop_pause(args, **kwargs):
    return _set_status(args, "paused")


def loop_resume(args, **kwargs):
    return _set_status(args, "running")


def loop_stop(args, **kwargs):
    return _set_status(args, "stopped")


def _push_and_pr(repo_path, branch, pr_title, pr_body):
    """Push ``branch`` and open a PR. Returns (pr_url, steps).

    ``--head`` is passed explicitly: after a fresh ``git push -u`` gh sometimes
    cannot resolve the head branch and aborts with "you must first push the
    current branch to a remote", even though the push just succeeded.
    """
    git = _git_path()
    steps = []
    rc, out = _run([git, "push", "-u", "origin", branch], cwd=repo_path, timeout=90)
    steps.append({"step": "push", "ok": rc == 0, "out": out.strip()[:300]})
    if rc != 0:
        return "", steps
    pr_url = ""
    rc, out = _run([_gh_path(), "pr", "create", "--title", pr_title, "--body", pr_body,
                    "--head", branch], cwd=repo_path, timeout=60)
    steps.append({"step": "pr", "ok": rc == 0, "out": out.strip()[:300]})
    if rc == 0:
        pr_url = out.strip().splitlines()[-1].strip()
    return pr_url, steps


def _record_pr(loop, pr_url, branch):
    prs = loop.get("prs") or []
    if pr_url and not any(p.get("url") == pr_url for p in prs):
        prs.append({
            "url": pr_url, "branch": branch,
            "round": loop.get("round", 0),
            "attempt": loop.get("attempts", 0),
            "opened_at": _now(), "state": "open",
        })
    loop["prs"] = prs
    loop["updated_at"] = _now()


def loop_commit(args, **kwargs):
    """Commit current changes to a loop branch and open a PR.

    Safety properties:
    * refuses to commit when the work tree is clean (no empty commits);
    * reports the diff stat instead of writing anything when ``dry_run`` is set;
    * reuses the loop's existing working branch instead of nesting a new one;
    * retrying after a failed push pushes the existing commits instead of
      refusing on a clean tree.
    """
    loop_id = (args.get("loop_id") or "").strip()
    dry_run = bool(args.get("dry_run", False))
    with _LoopLock(name=("loop-%s" % loop_id) if loop_id else "looprunner"):
        state = _load_state()
        loops = state.get("loops", {})
        loop = _resolve_loop(loops, loop_id)
        if not loop:
            return _err("no loop found")
        if loop.get("status") in TERMINAL:
            return _err("loop %s is %s -- resume it before committing" % (loop["id"], loop.get("status")))

        repo_path = loop["repo_path"]
        if not os.path.isdir(os.path.join(repo_path, ".git")):
            return _err("not a git repository: %s" % repo_path)

        # Pre-PR security gate: review the pending change set and refuse when a
        # critical or high finding is present.
        if bool(args.get("security_gate", loop.get("security_gate", False))):
            sec = json.loads(loop_security({"loop_id": loop["id"]}, **kwargs))
            if sec.get("blocking"):
                return json.dumps({
                    "loop_id": loop["id"], "committed": False,
                    "reason": "security gate: blocking findings",
                    "security": sec,
                    "message": (
                        "Refusing to open a PR: the security review found %d blocking "
                        "issue(s). Fix them and re-run, or pass security_gate=false."
                        % len(sec.get("blocking_findings", []))
                    ),
                })
            sec_note = "Security review passed (%d finding(s))." % len(sec.get("findings") or [])
        else:
            sec_note = ""

        commit_msg = (args.get("commit_message") or "looprunner: auto-fix").strip()
        pr_title = (args.get("pr_title") or commit_msg).strip()
        pr_body = (args.get("pr_body") or "Automated fix by LoopRunner.").strip()

        git = _git_path()

        # Diff gate: nothing to commit is an error, not an empty commit.
        rc, status_out = _run([git, "status", "--porcelain"], cwd=repo_path, timeout=20)
        if rc != 0:
            return _err("git status failed: %s" % status_out.strip()[:300])
        changed = [l for l in status_out.splitlines()
                   if l.strip() and not _is_artifact(l)]
        if not changed:
            # Clean tree. Two legitimate cases:
            #   * nothing was ever changed -> tell the caller;
            #   * an earlier attempt committed but the push failed -> retry the
            #     push, otherwise the loop is stuck with no way forward.
            current0 = _current_branch(repo_path)
            if _is_loop_branch(current0):
                rc, ahead = _run([git, "log", "@{u}..HEAD", "--oneline"],
                                 cwd=repo_path, timeout=20)
                if rc == 0 and ahead.strip():
                    prs0 = loop.get("prs") or []
                    if not any(p.get("branch") == current0 for p in prs0):
                        pr_url0, steps0 = _push_and_pr(repo_path, current0, pr_title, pr_body)
                        _record_pr(loop, pr_url0, current0)
                        _save_state(state)
                        return json.dumps({
                            "loop_id": loop["id"], "committed": False,
                            "pushed_retry": True, "branch": current0,
                            "pr_url": pr_url0, "steps": steps0,
                            "message": (
                                "Work tree was clean; pushed the existing commit(s) to "
                                "%s and opened %s." % (current0, pr_url0)
                                if pr_url0 else
                                "Work tree was clean; pushed existing commits to %s; "
                                "PR creation failed." % current0
                            ),
                        })
            return json.dumps({
                "loop_id": loop["id"], "committed": False,
                "reason": "nothing to commit -- work tree is clean",
                "message": "No changes to commit. Apply a fix first, or run loop_revert "
                           "if the last attempt should be rolled back.",
            })

        _rc, diff_stat = _run([git, "diff", "--stat"], cwd=repo_path, timeout=20)
        _rc, staged_stat = _run([git, "diff", "--cached", "--stat"], cwd=repo_path, timeout=20)
        stat = (staged_stat or diff_stat).strip()

        if dry_run:
            return json.dumps({
                "loop_id": loop["id"], "committed": False, "dry_run": True,
                "files_changed": len(changed),
                "diff_stat": stat,
                "message": "Dry run -- nothing was written. Re-run without dry_run to commit.",
            })

        # Branch: reuse the loop's working branch, else create auto/round-N.
        current = _current_branch(repo_path)
        branch = current if _is_loop_branch(current) else ("auto/round-%d" % (loop.get("round", 0) + 1))
        if branch != current:
            rc, out = _run([git, "checkout", "-b", branch], cwd=repo_path, timeout=30)
            if rc != 0:
                rc2, out2 = _run([git, "checkout", branch], cwd=repo_path, timeout=30)
                if rc2 != 0:
                    return _err("could not create branch %s: %s" % (branch, (out + out2).strip()[:300]))

        steps = []
        rc, _out = _run([git, "add", "-A", "--", "."] + _ADD_EXCLUDES,
                        cwd=repo_path, timeout=30)
        steps.append({"step": "add", "ok": rc == 0, "files": len(changed)})

        rc, out = _run([git, "commit", "-m", commit_msg], cwd=repo_path, timeout=30)
        steps.append({"step": "commit", "ok": rc == 0, "out": out.strip()[:300]})
        if rc != 0:
            return json.dumps({"loop_id": loop["id"], "committed": False,
                               "branch": branch, "steps": steps})

        pr_url, push_steps = _push_and_pr(repo_path, branch, pr_title, pr_body)
        steps.extend(push_steps)
        if not push_steps[0].get("ok"):
            return json.dumps({
                "loop_id": loop["id"], "committed": True, "branch": branch,
                "pushed": False, "steps": steps,
                "message": "Committed locally but the push failed -- check auth and re-run loop_commit.",
            })

        _record_pr(loop, pr_url, branch)
        _save_state(state)

        return json.dumps({
            "loop_id": loop["id"], "committed": True, "branch": branch,
            "pr_url": pr_url, "files_changed": len(changed),
            "diff_stat": stat, "steps": steps,
            "message": (
                ("Committed %d file(s) to %s and opened %s." % (len(changed), branch, pr_url)
                 if pr_url else
                 "Committed %d file(s) to %s; PR creation failed." % (len(changed), branch))
                + ((" " + sec_note) if sec_note else "")
            ),
        })


def loop_revert(args, **kwargs):
    """Roll back the loop's most recent PR: close it, delete the branch, reset."""
    loop_id = (args.get("loop_id") or "").strip()
    with _LoopLock(name=("loop-%s" % loop_id) if loop_id else "looprunner"):
        state = _load_state()
        loops = state.get("loops", {})
        loop = _resolve_loop(loops, loop_id)
        if not loop:
            return _err("no loop found")
        prs = loop.get("prs") or []
        if not prs:
            return json.dumps({
                "loop_id": loop["id"], "reverted": False,
                "reason": "no PRs recorded for this loop",
            })

        last = prs[-1]
        repo_path = loop["repo_path"]
        git = _git_path()
        steps = []

        rc, out = _run([_gh_path(), "pr", "close", last.get("url", ""), "--delete-branch"],
                       cwd=repo_path, timeout=60)
        if rc != 0:
            rc2, out2 = _run([_gh_path(), "pr", "close", last.get("url", "")],
                             cwd=repo_path, timeout=60)
            steps.append({"step": "pr_close", "ok": rc2 == 0,
                          "out": (out + out2).strip()[:300]})
        else:
            steps.append({"step": "pr_close", "ok": True, "out": out.strip()[:300]})

        # gh's --delete-branch may already have switched branch and removed it.
        rc, _ = _run([git, "rev-parse", "--verify", "--quiet", "refs/heads/%s" % last.get("branch", "")],
                     cwd=repo_path, timeout=20)
        if rc == 0:
            rc, out = _run([git, "checkout", "-"], cwd=repo_path, timeout=30)
            steps.append({"step": "checkout_back", "ok": rc == 0, "out": out.strip()[:200]})
            rc, out = _run([git, "branch", "-D", last.get("branch", "")],
                           cwd=repo_path, timeout=30)
            steps.append({"step": "branch_delete", "ok": rc == 0, "out": out.strip()[:200]})
        else:
            steps.append({"step": "branch_delete", "ok": True,
                          "out": "branch already removed"})

        last["state"] = "closed"
        last["closed_at"] = _now()
        loop["updated_at"] = _now()
        _save_state(state)

        return json.dumps({
            "loop_id": loop["id"], "reverted": True,
            "pr_url": last.get("url"), "branch": last.get("branch"),
            "steps": steps,
            "message": "Closed %s and deleted branch %s." % (last.get("url"), last.get("branch")),
        })


def loop_recommend(args, **kwargs):
    """Suggest concrete improvements for the loop's repository."""
    loop_id = (args.get("loop_id") or "").strip()
    state = _load_state()
    loop = _resolve_loop(state.get("loops", {}), loop_id)
    if not loop:
        return _err("no loop found")

    repo_path = loop["repo_path"]
    files = []
    for root, dirs, fnames in os.walk(repo_path):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", "node_modules", ".venv", ".venv-real")]
        for fn in fnames:
            if fn.endswith((".py", ".yaml", ".yml", ".md", ".toml")):
                files.append(os.path.relpath(os.path.join(root, fn), repo_path))
        if len(files) > 30:
            break

    readme = ""
    for name in ("README.md", "README.rst", "README.txt"):
        p = os.path.join(repo_path, name)
        if os.path.exists(p):
            try:
                readme = open(p, encoding="utf-8").read()[:1500]
            except Exception:
                pass
            break

    prompt = (
        "You are reviewing a software repository for improvements.\n"
        "Repo: %s\nFiles:\n%s\n\n"
        "README (truncated):\n%s\n\n"
        "Goal: %s\n\n"
        "Suggest 3-5 concrete, actionable improvements. For each: title, why it "
        "matters, estimated effort (S/M/L), and a one-line fix sketch. Be specific "
        "to THIS repo, not generic advice."
        % (repo_path, "\n".join(files[:30]), readme, loop.get("goal") or "improve quality")
    )

    if _ctx is not None:
        try:
            result = _ctx.llm.complete(
                messages=[
                    {"role": "system", "content": "You are a pragmatic staff engineer. Be specific and terse."},
                    {"role": "user", "content": prompt},
                ],
                max_tokens=900,
                temperature=0.3,
                purpose="looprunner.recommend",
            )
            text = (getattr(result, "text", "") or "").strip()
            if text:
                return json.dumps({"loop_id": loop["id"], "recommendations": {"raw": text}})
        except Exception:
            pass

    # Fallback: subprocess hermes chat
    try:
        proc = subprocess.run(
            [_hermes_bin(), "chat", "-q", prompt[:2000]],
            capture_output=True, text=True, timeout=300, cwd=repo_path,
        )
        text = (proc.stdout + proc.stderr).strip()
        if text:
            return json.dumps({"loop_id": loop["id"], "recommendations": {"raw": text[:4000]}})
    except Exception:
        pass

    return json.dumps({
        "loop_id": loop["id"], "prompt": prompt,
        "note": "No LLM available -- analyze this prompt to get improvement recommendations.",
    })


# --------------------------------------------------------------------------- #
# Kanban + goal resilience
# --------------------------------------------------------------------------- #

def _cron_delete(cron_id):
    """Delete the loop's cron job. Returns True on success."""
    if not cron_id or cron_id == "created":
        return False
    try:
        proc = subprocess.run(
            [_hermes_bin(), "cron", "remove", cron_id],
            capture_output=True, text=True, timeout=15,
        )
        return proc.returncode == 0
    except Exception:
        return False


def _kanban(args, timeout=90):
    """Run ``hermes kanban <args>``; return (rc, output)."""
    return _run([_hermes_bin(), "kanban"] + [str(a) for a in args], timeout=timeout)


def _kanban_create(title, body, assignee=None, parent=None, goal=False,
                   goal_max_turns=None, priority=None, skills=None, workspace=None):
    """Create a kanban card; return (card_id, raw_output)."""
    argv = ["create", title, "--body", body, "--json"]
    if assignee:
        argv += ["--assignee", str(assignee)]
    if parent:
        argv += ["--parent", str(parent)]
    if goal:
        argv += ["--goal"]
    if goal_max_turns:
        argv += ["--goal-max-turns", str(goal_max_turns)]
    if priority is not None:
        argv += ["--priority", str(priority)]
    for s in (skills or []):
        argv += ["--skill", str(s)]
    if workspace:
        argv += ["--workspace", str(workspace)]
    rc, out = _kanban(argv)
    card_id = None
    if rc == 0:
        try:
            card_id = json.loads(out).get("id")
        except Exception:
            m = re.search(r'"id"\s*:\s*"([^"]+)"', out)
            card_id = m.group(1) if m else None
    return card_id, out


def _checkpoint_name():
    return "looprunner/checkpoint-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _write_checkpoint(loop):
    """Snapshot the loop's current stage: a git ref plus a patch of any dirty work.

    Returns a dict describing what was captured, or an error string.
    """
    repo_path = loop["repo_path"]
    git = _git_path()
    branch = _checkpoint_name()

    rc, _ = _run([git, "branch", branch], cwd=repo_path, timeout=20)
    if rc != 0:
        return {"error": "could not create checkpoint branch %s" % branch}

    captured = {"branch": branch, "patch_file": None, "dirty": False}
    rc, patch = _run([git, "diff", "HEAD"], cwd=repo_path, timeout=30)
    _rc, untracked = _run([git, "ls-files", "--others", "--exclude-standard"],
                          cwd=repo_path, timeout=20)
    if patch.strip() or untracked.strip():
        _dir, _ = _paths()
        patch_path = _dir / ("%s.patch" % branch.replace("/", "-"))
        blob = patch
        for rel in untracked.splitlines():
            rel = rel.strip()
            if not rel or _is_artifact(rel):
                continue
            try:
                body = Path(repo_path, rel).read_text(encoding="utf-8", errors="replace")
            except Exception:
                blob += "\n--- untracked (unreadable): %s ---\n" % rel
                continue
            blob += "\n--- untracked file: %s ---\n%s\n" % (rel, body[:8000])
        try:
            patch_path.write_text(blob, encoding="utf-8")
            captured["patch_file"] = str(patch_path)
        except Exception as e:
            captured["patch_file"] = None
            captured["patch_error"] = str(e)
        captured["dirty"] = True
    return captured


def loop_checkpoint(args, **kwargs):
    """Snapshot the loop's current stage to a git branch (plus a dirty-work patch)."""
    loop_id = (args.get("loop_id") or "").strip()
    with _LoopLock(name=("loop-%s" % loop_id) if loop_id else "looprunner"):
        state = _load_state()
        loop = _resolve_loop(state.get("loops", {}), loop_id)
        if not loop:
            return _err("no loop found")
        captured = _write_checkpoint(loop)
        if "error" in captured:
            return _err(captured["error"])
        checkpoints = loop.get("checkpoints") or []
        checkpoints.append(dict(captured, at=_now()))
        loop["checkpoints"] = checkpoints
        loop["updated_at"] = _now()
        _save_state(state)
    return json.dumps({
        "loop_id": loop["id"], "checkpoint": captured,
        "message": ("Checkpoint %s created (%s)."
                    % (captured["branch"],
                       "dirty work captured in a patch" if captured["dirty"] else "clean tree")),
    })


def _create_resume_card(loop, reason):
    """Dispatch a goal-mode kanban card to decide the best next step."""
    goal = loop.get("goal") or "(no goal recorded)"
    body = (
        "A LoopRunner loop stopped before its goal was met.\n\n"
        "Loop: %s\nRepository: %s\nGoal: %s\nReason: %s\n"
        "Round (passes): %s\nAttempts (verify runs): %s\n"
        "Last failure signature: %s\n\n"
        "Your job (do NOT just retry blindly):\n"
        "1. Read the loop state and the failure output.\n"
        "2. Decide the single best next action: change the approach, roll back "
        "the last attempt with loop_revert, or conclude the goal is unachievable "
        "as written.\n"
        "3. Either apply that action, or leave a comment explaining the decision "
        "and block this card.\n\n"
        "Acceptance: a concrete decision has been made and either executed or "
        "recorded with a reason."
        % (
            loop["id"], loop["repo_path"], goal, reason,
            loop.get("round", 0), loop.get("attempts", 0),
            loop.get("last_signature"),
        )
    )
    card_id, raw = _kanban_create(
        "Resume loop %s: %s" % (loop["id"], goal[:70]),
        body,
        assignee=loop.get("assignee"),
        goal=True,
        goal_max_turns=loop.get("goal_max_turns", 20),
        skills=["looprunner-pattern"],
    )
    return card_id, raw


def _ensure_retry_cron(loop, state):
    """Give a stalled loop a fresh cron so it is retried later.

    The loop's own cron is paused on stall; without a retry the work would be
    orphaned. The retry fires at a longer interval than the original.
    """
    existing = loop.get("retry_cron_id")
    if existing:
        return existing
    interval = max(int(loop.get("interval_minutes", 60)), 30) * 4
    loop_id = loop["id"]
    try:
        proc = subprocess.run(
            [_hermes_bin(), "cron", "create",
             "every %dm" % interval,
             "Retry stalled LoopRunner loop %s (goal: %s)"
             % (loop_id, (loop.get("goal") or "not set")[:80])],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode == 0:
            m = re.search(r"Created job[:\s]+([a-f0-9]+)", proc.stdout + proc.stderr, re.I)
            retry_id = m.group(1) if m else None
            if retry_id:
                loop["retry_cron_id"] = retry_id
                loop["updated_at"] = _now()
                _save_state(state)
                return retry_id
    except Exception:
        pass
    return None


def loop_watch(args, **kwargs):
    """Start watching a repository's issues and dispatching reproduction cards.

    Each new open issue becomes a goal-mode kanban card that reproduces and
    diagnoses it; that card is responsible for spawning the follow-up fix card.
    """
    repo_path = (args.get("repo_path") or "").strip()
    if not repo_path:
        return _err("repo_path is required")
    if not Path(repo_path).exists():
        return _err("repo_path does not exist: %s" % repo_path)

    cfg = kwargs.get("config") or {}
    interval = int(args.get("interval_minutes") or cfg.get("interval_minutes") or 60)
    assignee = (args.get("assignee") or cfg.get("assignee") or "").strip() or None
    goal = (args.get("goal") or "triage incoming issues").strip()
    labels = args.get("labels") or []
    if isinstance(labels, str):
        labels = [l.strip() for l in labels.split(",") if l.strip()]
    goal_max_turns = int(args.get("goal_max_turns") or 20)

    with _LoopLock(name="looprunner-start"):
        state = _load_state()
        loop_id = uuid.uuid4().hex[:12]
        state["loops"][loop_id] = {
            "id": loop_id,
            "repo_path": repo_path,
            "verify_cmd": "",
            "max_rounds": 0,
            "max_attempts": 0,
            "stall_threshold": 3,
            "goal": goal,
            "deadline": "",
            "interval_minutes": interval,
            "branch_guard": False,
            "auto_recommend": False,
            "auto_fix": False,
            "strict_git_guard": False,
            "verify_timeout": 300,
            "goal_cmd": "",
            "checkpoint_on_stall": False,
            "security_gate": False,
            "mode": "watch",
            "assignee": assignee,
            "labels": labels,
            "goal_max_turns": goal_max_turns,
            "seen_issues": {},
            "watch_cards": [],
            "status": "running",
            "round": 0,
            "attempts": 0,
            "created_at": _now(),
            "updated_at": _now(),
            "completed_at": None,
            "cron_job_id": None,
            "prs": [],
            "history": [],
            "last_signature": None,
            "signature_counts": {},
            "checkpoints": [],
            "retry_cron_id": None,
        }
        _save_state(state)

    cron_job_id = None
    cron_error = None
    try:
        proc = subprocess.run(
            [_hermes_bin(), "cron", "create", "every %dm" % interval,
             "Run loop_scan_issues on loop %s" % loop_id],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode == 0:
            m = re.search(r"Created job[:\s]+([a-f0-9]+)", proc.stdout + proc.stderr, re.I)
            cron_job_id = m.group(1) if m else "created"
        else:
            cron_error = (proc.stdout + proc.stderr).strip()[:500]
    except Exception as e:
        cron_error = "exception: %s" % e

    if cron_job_id or cron_error:
        with _LoopLock(name="loop-%s" % loop_id):
            state = _load_state()
            loop = state["loops"].get(loop_id)
            if loop:
                loop["cron_job_id"] = cron_job_id
                if cron_error:
                    loop["_cron_error"] = cron_error
                _save_state(state)

    return json.dumps({
        "loop_id": loop_id, "mode": "watch", "repo_path": repo_path,
        "interval_minutes": interval, "assignee": assignee, "labels": labels,
        "cron_job_id": cron_job_id,
        "message": ("Watching %s every %dm." % (repo_path, interval))
                   + (" Cron error: %s" % cron_error if cron_error else ""),
    })


def loop_scan_issues(args, **kwargs):
    """One watch pass: dispatch a reproduction card for every new open issue."""
    loop_id = (args.get("loop_id") or "").strip()
    with _LoopLock(name=("loop-%s" % loop_id) if loop_id else "looprunner"):
        state = _load_state()
        loops = state.get("loops", {})
        loop = _resolve_loop(loops, loop_id, active_only=True)
        if not loop:
            return _err("no active loop found")
        if loop.get("mode") != "watch":
            return _err("loop %s is not a watch loop" % loop["id"])
        lid = loop["id"]
        repo_path = loop["repo_path"]

        gh_args = ["issue", "list", "--state", "open", "--limit", "100",
                   "--json", "number,title,body,labels,url"]
        labels = loop.get("labels") or []
        if labels:
            gh_args += ["--label", ",".join(labels)]
        rc, out = _run([_gh_path()] + gh_args, cwd=repo_path, timeout=90)
        if rc != 0:
            return _err("gh issue list failed: %s" % out.strip()[:300])
        try:
            issues = json.loads(out)
        except Exception:
            return _err("could not parse the issue list")

        seen = loop.get("seen_issues") or {}
        new_cards = []
        for iss in issues:
            num = str(iss.get("number"))
            if num in seen:
                continue
            title = (iss.get("title") or "").strip()
            body = (iss.get("body") or "").strip()
            url = iss.get("url", "")
            card_body = (
                "You are reproducing a reported issue in the repository %s.\n\n"
                "## Issue #%s: %s\n\n%s\n\n## URL\n%s\n\n"
                "## Your job\n"
                "1. Read the issue and identify the reported behaviour.\n"
                "2. Reproduce it: write a failing check or a small script that "
                "demonstrates the bug, and run it. Record the exact command.\n"
                "3. Find the root cause and the minimal fix.\n"
                "4. Report findings as a comment on this card:\n"
                "   REPRODUCED / STEPS / OBSERVED / EXPECTED / ROOT CAUSE "
                "(file:line) / SUGGESTED FIX.\n"
                "5. Create the follow-up fix card: call kanban_create (or run "
                "`hermes kanban create`) with title \"Fix issue #%s: %s\", the "
                "findings as the body, --parent set to THIS card, and --goal. "
                "Tell the fix card to apply the change and open a PR with "
                "loop_commit.\n"
                "6. Complete this card.\n\n"
                "Do NOT fix the issue yourself — reproduce and diagnose only."
                % (repo_path, num, title, body, url, num, title)
            )
            card_id, raw = _kanban_create(
                "Reproduce issue #%s: %s" % (num, title[:80]),
                card_body,
                assignee=loop.get("assignee"),
                goal=True,
                goal_max_turns=loop.get("goal_max_turns", 20),
                skills=["looprunner-pattern"],
            )
            seen[num] = {"card_id": card_id, "title": title, "url": url, "at": _now()}
            new_cards.append({"issue": num, "card_id": card_id, "title": title})

        loop["seen_issues"] = seen
        loop["updated_at"] = _now()
        _save_state(state)

    return json.dumps({
        "loop_id": lid, "issues_seen": len(seen), "new_cards": new_cards,
        "message": ("%d new issue(s) dispatched." % len(new_cards))
                   if new_cards else "No new issues.",
    })


_SECURITY_SCHEMA = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "severity": {"type": "string",
                                 "enum": ["critical", "high", "medium", "low", "info"]},
                    "path": {"type": "string"},
                    "line": {"type": "integer"},
                    "title": {"type": "string"},
                    "detail": {"type": "string"},
                    "evidence": {"type": "string"},
                },
                "required": ["severity", "title", "detail"],
            },
        },
        "summary": {"type": "string"},
    },
    "required": ["findings", "summary"],
}

_BLOCKING_SEVERITIES = ("critical", "high")


def _collect_diff(repo_path):
    """Return (patch_text, untracked_files, changed_files).

    Untracked files never appear in ``git diff``, so their contents are read
    and appended -- otherwise a secret committed as a *new* file would pass the
    pre-PR review unexamined.
    """
    git = _git_path()
    _rc, staged = _run([git, "diff", "--cached"], cwd=repo_path, timeout=30)
    _rc, unstaged = _run([git, "diff"], cwd=repo_path, timeout=30)
    _rc, untracked = _run([git, "ls-files", "--others", "--exclude-standard"],
                          cwd=repo_path, timeout=20)
    _rc, changed = _run([git, "status", "--porcelain"], cwd=repo_path, timeout=20)
    files = [l for l in changed.splitlines() if l.strip() and not _is_artifact(l)]

    new_content = ""
    for rel in untracked.splitlines():
        rel = rel.strip()
        if not rel or _is_artifact(rel):
            continue
        try:
            body = Path(repo_path, rel).read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        new_content += "\n--- new file: %s ---\n%s\n" % (rel, body[:4000])
    return (staged + "\n" + unstaged + new_content), untracked.strip(), files


def loop_security(args, **kwargs):
    """Security review of the loop's pending change set.

    This is the pre-PR gate: it reviews the diff (staged + unstaged + untracked)
    and returns typed findings. ``loop_commit(security_gate=True)`` calls it and
    refuses to open a PR when a critical or high finding is present.
    """
    loop_id = (args.get("loop_id") or "").strip()
    state = _load_state()
    loop = _resolve_loop(state.get("loops", {}), loop_id)
    if not loop:
        return _err("no loop found")
    repo_path = loop["repo_path"]
    patch, untracked, files = _collect_diff(repo_path)
    if not patch.strip() and not untracked:
        return json.dumps({
            "loop_id": loop["id"], "findings": [], "summary": "No pending changes.",
            "blocking": False,
        })

    if _ctx is None:
        return json.dumps({
            "loop_id": loop["id"], "findings": None,
            "note": "No plugin context available; cannot run the security review.",
            "blocking": False,
        })

    prompt = (
        "Review this change set for security flaws. Repository: %s\n\n"
        "Changed files:\n%s\n\n"
        "Untracked files:\n%s\n\n"
        "Diff:\n%s\n\n"
        "Report only real, demonstrable issues introduced by this change: "
        "credentials or secrets in code, injection sinks (SQL/shell/command), "
        "path traversal, unsafe deserialization, CSRF/auth bypasses, weak "
        "crypto, SSRF, and similar. For each: severity, path, line, title, "
        "detail, and the exact offending excerpt. Do not report style issues."
        % (repo_path, "\n".join(files[:40]), untracked or "(none)", patch[:20000])
    )

    try:
        result = _ctx.llm.complete_structured(
            instructions=(
                "You are a security reviewer. Review the supplied diff for real, "
                "demonstrable security flaws introduced by this change. Be precise; "
                "do not invent findings."
            ),
            input=[{"type": "text", "text": prompt}],
            json_schema=_SECURITY_SCHEMA,
            schema_name="looprunner.security",
            purpose="looprunner.security",
            temperature=0.0,
            max_tokens=1200,
        )
    except Exception as e:
        return json.dumps({
            "loop_id": loop["id"], "findings": None,
            "note": "Security review failed (%s: %s)." % (type(e).__name__, e),
            "blocking": False,
        })

    parsed = getattr(result, "parsed", None)
    if not isinstance(parsed, dict):
        return json.dumps({
            "loop_id": loop["id"], "findings": None,
            "raw": (getattr(result, "text", "") or "")[:4000],
            "note": "Security review returned no parseable findings; raw output attached.",
            "blocking": False,
        })

    findings = parsed.get("findings") or []
    blocking = [f for f in findings
                if str(f.get("severity", "")).lower() in _BLOCKING_SEVERITIES]
    return json.dumps({
        "loop_id": loop["id"],
        "findings": findings,
        "summary": parsed.get("summary", ""),
        "blocking": bool(blocking),
        "blocking_findings": blocking,
        "message": (
            "%d finding(s), %d blocking."
            % (len(findings), len(blocking))
        ),
    })


# --------------------------------------------------------------------------- #
# Safety gate (pre_tool_call hook)
# --------------------------------------------------------------------------- #

_GIT_WRITE_RE = re.compile(
    r"(?:^|&&|;|\|\|)\s*(?:sudo\s+)?git\s+"
    r"(commit|push|reset\s+--hard|clean\s+-[fdx]+|checkout\s+-B)\b"
)

# Values that mean "the model did not actually answer".
_PLACEHOLDER_RE = re.compile(r"^(?:placeholder|n/?a|todo|tbd|none|unknown|\.{3}|-)$", re.I)


def _cmd_targets_repo(cmd, repo_path):
    """True when a shell command plausibly operates on ``repo_path``.

    The gate exists to stop an agent from bypassing ``loop_commit`` *in the loop's
    own repository*; a commit in some unrelated checkout is none of its business.
    """
    if not cmd or not repo_path:
        return False
    if repo_path.replace("\\", "/") in cmd.replace("\\", "/"):
        return True
    try:
        Path.cwd().resolve().relative_to(Path(repo_path).resolve())
        return True
    except Exception:
        return False


def guard_tool_call(tool_name, args):
    """Veto unsafe tool calls while a loop is running.

    Returns ``{"action": "block", "message": ...}`` or ``None``.
    """
    try:
        state = _load_state()
    except Exception:
        return None
    loops = state.get("loops", {})
    active = [l for l in loops.values()
              if l.get("status") == "running" and l.get("strict_git_guard", True)]
    if not active:
        return None

    if tool_name == "loop_commit":
        loop_id = (args.get("loop_id") or "").strip()
        loop = loops.get(loop_id) if loop_id else _most_recent(loops, active_only=True)
        if loop and loop.get("status") in TERMINAL:
            return {
                "action": "block",
                "message": ("loop %s is %s. Resume it with loop_resume before committing."
                            % (loop["id"], loop.get("status"))),
            }
        return None

    if tool_name in ("terminal", "execute_code", "shell", "bash", "command"):
        cmd = args.get("command") or args.get("cmd") or args.get("code") or ""
        if not isinstance(cmd, str) or not _GIT_WRITE_RE.search(cmd):
            return None
        for loop in active:
            branch = _current_branch(loop["repo_path"])
            if not (branch in ("main", "master") or _is_loop_branch(branch)):
                continue
            if _cmd_targets_repo(cmd, loop["repo_path"]):
                return {
                    "action": "block",
                    "message": ("Direct git write blocked: loop %s is active on %s. "
                                "Use loop_commit (branch + PR) instead of running git "
                                "commit/push by hand." % (loop["id"], branch)),
                }
    return None
