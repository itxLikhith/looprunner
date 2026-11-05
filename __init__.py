"""LoopRunner plugin — registration."""

import json
import logging

from . import schemas, tools

logger = logging.getLogger(__name__)

_call_log = []


def _on_post_tool_call(tool_name, args, result, task_id, **kwargs):
    _call_log.append({"tool": tool_name, "session": task_id})
    if len(_call_log) > 100:
        _call_log.pop(0)
    logger.debug("Tool called: %s (session %s)", tool_name, task_id)


def _on_pre_llm_call(session_id, user_message, conversation_history,
                     is_first_turn, model, platform, **kwargs):
    state = tools._load_state()
    loops = state.get("loops", {})
    active = [l for l in loops.values() if l.get("status") == "running"]
    if not active:
        return None
    summary = "Active LoopRunner loops: " + ", ".join(
        f"{l['id']}({l['repo_path']}, round {l['round']})" for l in active
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

    def _tick(args, **kwargs):
        return tools.loop_tick(args or {}, **kwargs)

    def _status(args, **kwargs):
        return tools.loop_status(args or {}, **kwargs)

    def _pause(args, **kwargs):
        return tools.loop_pause(args or {}, **kwargs)

    def _resume(args, **kwargs):
        return tools.loop_resume(args or {}, **kwargs)

    def _stop(args, **kwargs):
        return tools.loop_stop(args or {}, **kwargs)

    ctx.register_tool(name="loop_start", toolset="looprunner",
                      schema=schemas.LOOP_START, handler=_start)
    ctx.register_tool(name="loop_tick", toolset="looprunner",
                      schema=schemas.LOOP_TICK, handler=_tick)
    ctx.register_tool(name="loop_status", toolset="looprunner",
                      schema=schemas.LOOP_STATUS, handler=_status)
    ctx.register_tool(name="loop_pause", toolset="looprunner",
                      schema=schemas.LOOP_PAUSE, handler=_pause)
    ctx.register_tool(name="loop_resume", toolset="looprunner",
                      schema=schemas.LOOP_RESUME, handler=_resume)
    ctx.register_tool(name="loop_stop", toolset="looprunner",
                      schema=schemas.LOOP_STOP, handler=_stop)

    ctx.register_hook("post_tool_call", _on_post_tool_call)
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)

    # Bundle the loop pattern skill
    try:
        import os
        skill_path = os.path.join(os.path.dirname(__file__), "skills", "looprunner-pattern")
        ctx.register_skill("looprunner-pattern", skill_path)
    except Exception as e:
        logger.warning("Could not register bundled skill: %s", e)

    logger.info("LoopRunner plugin loaded (5 tools, 2 hooks)")