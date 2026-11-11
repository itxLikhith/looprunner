"""LoopRunner plugin — registration."""

import logging

from . import schemas, tools

logger = logging.getLogger(__name__)

_call_log = []


def _on_post_tool_call(tool_name, args, result, task_id, **kwargs):
    _call_log.append({"tool": tool_name, "session": task_id})
    if len(_call_log) > 100:
        _call_log.pop(0)
    logger.debug("Tool called: %s (session %s)", tool_name, task_id)


def _on_pre_tool_call(tool_name, args, task_id, **kwargs):
    """Safety gate: veto unsafe tool calls while a loop is running.

    Two jobs, in order:

    1. Escalate a loop's verify/goal command to Hermes's own approval gate.
       A loop runs its command on every tick, including unattended cron runs,
       so the command must be approved once per (repo, command) by a human.
       Returning ``{"action": "approve", ...}`` hands the decision to Hermes --
       the plugin implements no approval logic of its own, and the user gets
       the same once/session/always/deny prompt, allowlist and /approve flow
       as any other dangerous command.
    2. Veto direct git writes while a loop is active on main/master.
    """
    try:
        verdict = tools.guard_tool_call(tool_name, args or {})
        if verdict:
            return verdict
    except Exception as e:
        logger.debug("pre_tool_call guard failed: %s", e)
    try:
        return tools.escalate_loop_command(tool_name, args or {})
    except Exception as e:
        # Hermes fails closed on hook exceptions, so a crash here would block
        # EVERY terminal call rather than the one being matched.
        logger.debug("pre_tool_call escalation failed: %s", e)
        return None


def _on_pre_llm_call(session_id, user_message, conversation_history,
                     is_first_turn, model, platform, **kwargs):
    state = tools._load_state()
    loops = state.get("loops", {})
    active = [l for l in loops.values() if l.get("status") == "running"]
    if not active:
        return None
    summary = "Active LoopRunner loops: " + ", ".join(
        f"{l['id']}({l['repo_path']}, round {l.get('round', 0)}, "
        f"attempts {l.get('attempts', 0)}, {l.get('status')})"
        for l in active
    )
    return {"context": summary}


def _get_config(ctx):
    try:
        return ctx.get_config() or {}
    except Exception:
        return {}


def register(ctx):
    cfg = _get_config(ctx)

    def _start(args, **kwargs):
        args = dict(args or {})
        args.setdefault("config", cfg)
        return tools.loop_start(args, **kwargs)

    def _wrap(name):
        def handler(args, **kwargs):
            return getattr(tools, name)(args or {}, **kwargs)
        return handler

    _STATUS = {
        "loop_start": _start,
        "loop_tick": _wrap("loop_tick"),
        "loop_verify": _wrap("loop_verify"),
        "loop_status": _wrap("loop_status"),
        "loop_history": _wrap("loop_history"),
        "loop_analyze": _wrap("loop_analyze"),
        "loop_pause": _wrap("loop_pause"),
        "loop_resume": _wrap("loop_resume"),
        "loop_stop": _wrap("loop_stop"),
        "loop_recommend": _wrap("loop_recommend"),
        "loop_commit": _wrap("loop_commit"),
        "loop_revert": _wrap("loop_revert"),
        "loop_watch": _wrap("loop_watch"),
        "loop_scan_issues": _wrap("loop_scan_issues"),
        "loop_security": _wrap("loop_security"),
        "loop_security_card": _wrap("loop_security_card"),
        "loop_checkpoint": _wrap("loop_checkpoint"),
        "loop_detect": _wrap("loop_detect"),
    }

    _SCHEMAS = {
        "loop_start": schemas.LOOP_START,
        "loop_tick": schemas.LOOP_TICK,
        "loop_verify": schemas.LOOP_VERIFY,
        "loop_status": schemas.LOOP_STATUS,
        "loop_history": schemas.LOOP_HISTORY,
        "loop_analyze": schemas.LOOP_ANALYZE,
        "loop_pause": schemas.LOOP_PAUSE,
        "loop_resume": schemas.LOOP_RESUME,
        "loop_stop": schemas.LOOP_STOP,
        "loop_recommend": schemas.LOOP_RECOMMEND,
        "loop_commit": schemas.LOOP_COMMIT,
        "loop_revert": schemas.LOOP_REVERT,
        "loop_watch": schemas.LOOP_WATCH,
        "loop_scan_issues": schemas.LOOP_SCAN_ISSUES,
        "loop_security": schemas.LOOP_SECURITY,
        "loop_security_card": schemas.LOOP_SECURITY_CARD,
        "loop_checkpoint": schemas.LOOP_CHECKPOINT,
        "loop_detect": schemas.LOOP_DETECT,
    }

    for name, schema in _SCHEMAS.items():
        ctx.register_tool(name=name, toolset="looprunner", schema=schema,
                          handler=_STATUS[name])

    # Register auxiliary task for LLM access (enables ctx.llm with task=)
    try:
        ctx.register_auxiliary_task(
            "looprunner_review",
            display_name="LoopRunner review",
            description="code review and improvement suggestions",
        )
    except Exception as e:
        logger.warning("Could not register auxiliary task: %s", e)

    # No slash command. A plugin slash command is a deterministic surface (the
    # bundled disk-cleanup and advanced-delegation plugins both parse a fixed
    # subcommand table), and routing free text through the model would need
    # ctx.inject_message -- a host API meant for external feeds (webhooks,
    # bridges), disabled by default and absent from the capability registry.
    #
    # Intent belongs in chat: the bundled skill plus the tool descriptions let
    # the model interpret "keep my tests green on X" directly, infer the repo,
    # detect the verify command and run the loop.

    # Store ctx reference for tools that need LLM access
    tools.set_ctx(ctx)

    ctx.register_hook("post_tool_call", _on_post_tool_call)
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)

    # Bundle the loop pattern skill
    try:
        import os
        skill_path = os.path.join(
            os.path.dirname(__file__), "skills", "looprunner-pattern", "SKILL.md"
        )
        ctx.register_skill("looprunner-pattern", skill_path)
    except Exception as e:
        logger.warning("Could not register bundled skill: %s", e)

    logger.info("LoopRunner plugin loaded (18 tools, 3 hooks)")
