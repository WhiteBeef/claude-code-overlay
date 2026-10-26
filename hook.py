#!/usr/bin/env python3
"""Claude Code hook: records each session's status for overlay.py.

Reads the hook event JSON from stdin and writes ~/.claude/overlay/state/<session_id>.json.

On PermissionRequest it also publishes the request so the overlay can show buttons
for it, then waits for a click. Claude Code shows its own prompt at the same time,
so whichever answers first wins; if the overlay isn't running or nobody clicks, the
hook exits without a decision and the terminal prompt works as usual.

Never prints anything except a permission decision and always exits 0, so it can't
otherwise interfere with Claude Code.
"""
import json
import os
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (  # noqa: E402
    ANSWER_DIR,
    find_claude_pid,
    is_alive,
    load_config,
    overlay_running,
    read_json,
    session_path,
    update_session,
    was_interrupted,
)

DENY_MESSAGE = "The user doesn't want to proceed with this tool use (denied from the overlay)."


def new_status(event, data, prev):
    """Map a hook event to a status, or None to leave the state untouched."""
    if event == "SessionStart":
        return prev if data.get("source") == "compact" and prev else "idle"
    if event in ("UserPromptSubmit", "PreToolUse", "PostToolUse", "PostToolUseFailure"):
        return "working"
    if event == "PermissionRequest":
        return "waiting"
    if event == "Notification":
        kind = data.get("notification_type") or ""
        message = (data.get("message") or "").lower()
        if kind in ("permission_prompt", "elicitation_dialog") or (not kind and "permission" in message):
            return "waiting"
        return None
    if event == "Stop":
        return "done"
    return None


def record(data, event):
    now = time.time()
    agent = data.get("agent_id")
    with update_session(data["session_id"]) as state:
        if event == "SessionEnd":
            state["_delete"] = True
            return
        prev = state.get("status")
        status = new_status(event, data, prev) or prev
        if status is None:
            state["_delete"] = not state  # nothing to record yet
            return

        # Once the agent that asked moves on, its prompt was answered in the terminal.
        if event in ("UserPromptSubmit", "PreToolUse", "PostToolUse", "PostToolUseFailure", "Stop"):
            requests = state.get("requests") or {}
            state["requests"] = {k: r for k, r in requests.items() if r.get("agent") != agent}

        if event == "UserPromptSubmit" or (status == "working" and "turn_start" not in state):
            state["turn_start"] = now
        if event == "PreToolUse" and not agent:
            state["tool"] = data.get("tool_name")
        elif event in ("UserPromptSubmit", "Stop", "SessionStart"):
            state["tool"] = None
        if status != prev:
            state["since"] = now
        if not state.get("pid"):
            state["pid"], state["pid_start"] = find_claude_pid()
        state.update(
            status=status,
            updated=now,
            cwd=data.get("cwd") or state.get("cwd"),
            transcript=data.get("transcript_path") or state.get("transcript"),
        )


# --- permission prompts ------------------------------------------------------


def summarize(tool, args):
    """One line describing what the tool wants to do."""
    for key in ("command", "file_path", "notebook_path", "url", "pattern", "query", "description"):
        if isinstance(args.get(key), str) and args[key]:
            return args[key]
    return json.dumps(args, ensure_ascii=False) if args else tool


def always_label(suggestions):
    """Button text for the "don't ask again" option (the first suggestion), or None."""
    if not suggestions:
        return None
    first = suggestions[0]
    if first.get("type") == "setMode":
        return {"acceptEdits": "Allow, accept edits", "auto": "Allow, auto mode"}.get(first.get("mode"), "Allow, switch mode")
    if first.get("type") == "addDirectories":
        return "Allow folder"
    return "Always allow"


def make_request(data, request_id):
    tool = data.get("tool_name") or "?"
    args = data.get("tool_input") or {}
    suggestions = data.get("permission_suggestions") or []
    request = {
        "id": request_id,
        "pid": os.getpid(),
        "agent": data.get("agent_id"),
        "created": time.time(),
        "tool": tool,
        "kind": "permission",
        "title": summarize(tool, args)[:400],
        "always": always_label(suggestions),
    }
    if tool == "AskUserQuestion" and args.get("questions"):
        request["kind"] = "question"
        request["questions"] = [
            {
                "question": q.get("question", ""),
                "header": q.get("header", ""),
                "multiSelect": bool(q.get("multiSelect")),
                "options": [o.get("label", "") for o in q.get("options") or []],
            }
            for q in args["questions"]
        ]
    elif tool == "ExitPlanMode":
        request["kind"] = "plan"
        request["title"] = "Approve the plan?"
    return request


def decision_for(answer, data):
    """Turn the overlay's answer into a PermissionRequest decision, or None to defer."""
    args = data.get("tool_input") or {}
    action = answer.get("action")
    if action == "allow":
        return {"behavior": "allow", "updatedInput": args}
    if action == "always":
        return {
            "behavior": "allow",
            "updatedInput": args,
            "updatedPermissions": (data.get("permission_suggestions") or [])[:1],
        }
    if action == "answer":
        return {"behavior": "allow", "updatedInput": dict(args, answers=answer.get("answers") or {})}
    if action == "deny":
        return {"behavior": "deny", "message": DENY_MESSAGE}
    return None


def still_pending(session_id, request_id, created):
    state = read_json(session_path(session_id), {})
    if request_id not in (state.get("requests") or {}):
        return False
    if state.get("pid") and not is_alive(state["pid"], state.get("pid_start")):
        return False
    interrupted, mtime = was_interrupted(state.get("transcript"))
    return not (interrupted and mtime > created)


def wait_for_answer(session_id, request_id, created, timeout):
    path = os.path.join(ANSWER_DIR, f"{request_id}.json")
    deadline = created + timeout
    next_check = 0
    while time.time() < deadline:
        answer = read_json(path)
        if answer:
            try:
                os.remove(path)
            except OSError:
                pass
            return answer
        now = time.time()
        if now >= next_check:
            if not overlay_running() or not still_pending(session_id, request_id, created):
                return None
            next_check = now + 1
        time.sleep(0.15)
    return None


def handle_permission(data):
    config = load_config()["interactive"]
    if not config.get("enabled") or not overlay_running():
        return
    os.makedirs(ANSWER_DIR, exist_ok=True)
    request = make_request(data, uuid.uuid4().hex)
    with update_session(data["session_id"]) as state:
        state.setdefault("requests", {})[request["id"]] = request

    try:
        answer = wait_for_answer(data["session_id"], request["id"], request["created"], config.get("timeout", 600))
    finally:
        with update_session(data["session_id"]) as state:
            (state.get("requests") or {}).pop(request["id"], None)
            if not state.get("status"):
                state["_delete"] = True

    decision = answer and decision_for(answer, data)
    if decision:
        with update_session(data["session_id"]) as state:
            state.update(status="working", since=time.time())
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": decision}}))


def main():
    data = json.load(sys.stdin)
    event = data.get("hook_event_name")
    if not data.get("session_id") or not event:
        return
    record(data, event)
    if event == "PermissionRequest":
        handle_permission(data)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
