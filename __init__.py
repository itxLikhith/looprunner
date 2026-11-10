"""LoopRunner plugin — registration."""

import json
import logging
import os

from . import schemas, tools

logger = logging.getLogger(__name__)

_call_log = []


def _on_post_tool_call(tool_name, args, result, task_id, **kwargs):
    _call_log.append({"tool": tool_name, "session": task_id})
    if len(_call_log) > 100:
        _call_log.pop(0)
    logger.debug("Tool called: %s (session %s)", tool_name, task_id)


def _on_pre_tool_call(tool_name, args, task_id, **kwargs):
    """Safety gate: veto unsafe tool calls while a loop is running."""
    try:
        return tools.guard_tool_call(tool_name, args or {})
    except Exception as e:
        logger.debug("pre_tool_call guard failed: %s", e)
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

    # Register in-session slash command.
    #
    # The handler does NO parsing. Whatever the user typed is handed to the model
    # verbatim, with the tool list, and the model decides what to do -- including
    # running several tools in order. Hardcoding verbs or path heuristics here
    # just recreates the problem we are trying to avoid.
    def _slash_handler(raw_args: str, session_key: str = "", **ctx):
        raw = (raw_args or "").strip()
        if not raw:
            return tools.loop_status({}, **{})

        # Hand the user's words to the model as a real turn. The handler does no
        # parsing at all: the model knows the full tool surface and decides what
        # to do, including calling several tools in order.
        prompt = _handoff_prompt(raw)
        injected = False
        try:
            injected = bool(tools._ctx.inject_message(
                prompt, role="user", session_key=session_key or None))
        except Exception as e:
            logger.debug("inject_message failed: %s", e)
        if injected:
            # The model will act on the queued turn; nothing to print here.
            return "LoopRunner: handing that to the agent."
        # Injection unavailable (e.g. no live session): return the prompt so the
        # caller still sees what to do.
        return prompt


    def _handoff_prompt(raw):
        """Return a prompt the model can act on."""
        try:
            names = ", ".join(sorted(_SCHEMAS))
        except Exception:
            names = "loop_start, loop_tick, loop_status, ..."
        return json.dumps({
            "handoff": True,
            "user_input": raw,
            "available_tools": names,
            "instruction": (
                "The user typed '/looprunner %s'. Interpret it against the "
                "LoopRunner tool surface and act on it. You may call several "
                "tools in order (for example: loop_detect to identify the "
                "repository and its verify command, then loop_start, then "
                "loop_tick; or loop_status, then loop_analyze, then loop_commit). "
                "Infer the repository from the words rather than asking for an "
                "absolute path, and omit verify_cmd when the project type is "
                "recognisable. If you genuinely cannot tell what they want, ask "
                "one short question." % raw
            ),
        })

    try:
        ctx.register_command("looprunner", _slash_handler)
    except Exception as e:
        logger.warning("Could not register slash command: %s", e)

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
