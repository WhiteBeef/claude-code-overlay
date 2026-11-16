#!/usr/bin/env python3
"""Coding agent hook: records each session's status for overlay.py.

Reads the hook event JSON from stdin and writes one JSON file per client and session.

Claude Code and Codex answer native permission prompts through the overlay. Kimi Code
uses PreToolUse with its Auto mode for approvals and structured questions; native
PermissionRequest events remain terminal-only fallbacks.
The terminal prompt remains available where supported, and hook failures stay fail-open.

Never prints anything except a supported permission decision and always exits 0.
"""
import argparse
import json
import os
import sys
import time
import uuid
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (  # noqa: E402
    ANSWER_DIR,
    CLIENT_LABELS,
    find_agent_pid,
    find_kimi_transcript,
    is_alive,
    load_config,
    overlay_running,
    read_json,
    session_path,
    update_session,
    was_interrupted,
)

DENY_MESSAGE = "The user doesn't want to proceed with this tool use (denied from the overlay)."
KIMI_NO_APPROVAL_TOOLS = {
    "Read", "Grep", "Glob", "ReadMediaFile", "WebSearch", "FetchURL", "TodoList",
    "Agent", "AgentSwarm", "AskUserQuestion", "NotifyUser", "Skill", "TaskList",
    "TaskOutput", "WaitFor", "CronList", "ExitPlanMode",
}


def kimi_needs_approval(data: dict) -> bool:
    return data.get("tool_name") not in KIMI_NO_APPROVAL_TOOLS


def kimi_question(data: dict) -> bool:
    return data.get("tool_name") == "AskUserQuestion"


def new_status(event: str, data: dict, prev: Optional[str]) -> Optional[str]:
    """Map a hook event to a status, or None to leave the state untouched."""
    if event == "SessionStart":
        return prev if data.get("source") == "compact" and prev else "idle"
    if event in ("UserPromptSubmit", "PreToolUse", "PostToolUse", "PostToolUseFailure", "PermissionResult"):
        return "working"
    if event == "PermissionRequest":
        return "waiting"
    if event == "Notification":
        kind = data.get("notification_type") or ""
        message = (data.get("message") or "").lower()
        if kind in ("permission_prompt", "elicitation_dialog") or (not kind and "permission" in message):
            return "waiting"
        return None
    if event in ("Stop", "StopFailure", "Interrupt"):
        return "done"
    return None


def record(data: dict, event: str, client: str) -> None:
    now = time.time()
    agent = data.get("agent_id")
    with update_session(data["session_id"], client) as state:
        if event == "SessionEnd":
            state["_delete"] = True
            return
        prev = state.get("status")
        status = new_status(event, data, prev) or prev
        if client == "kimi" and event == "PreToolUse" and kimi_question(data):
            status = "waiting"
        if status is None:
            state["_delete"] = not state  # nothing to record yet
            return

        # Once the agent that asked moves on, its prompt was answered in the terminal.
        if event in ("UserPromptSubmit", "PreToolUse", "PostToolUse", "PostToolUseFailure",
                     "PermissionResult", "Stop", "StopFailure", "Interrupt"):
            requests = state.get("requests") or {}
            state["requests"] = {k: r for k, r in requests.items() if r.get("agent") != agent}

        if event == "UserPromptSubmit" or (status == "working" and "turn_start" not in state):
            state["turn_start"] = now
        if event == "PreToolUse" and not agent:
            state["tool"] = data.get("tool_name")
        elif event in ("UserPromptSubmit", "Stop", "StopFailure", "Interrupt", "SessionStart"):
            state["tool"] = None
        if status != prev:
            state["since"] = now
        if not state.get("pid"):
            state["pid"], state["pid_start"] = find_agent_pid(client)
        transcript = data.get("transcript_path") or state.get("transcript")
        if client == "kimi" and not transcript:
            transcript = find_kimi_transcript(data["session_id"])
        state.update(
            session_id=data["session_id"],
            client=client,
            status=status,
            updated=now,
            cwd=data.get("cwd") or state.get("cwd"),
            transcript=transcript,
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


def make_request(data: dict, request_id: str, client: str) -> dict:
    tool = data.get("tool_name") or "?"
    args = data.get("tool_input") or {}
    suggestions = data.get("permission_suggestions") or []
    request = {
        "id": request_id,
        "pid": os.getpid(),
        "client": client,
        "agent": data.get("agent_id"),
        "created": time.time(),
        "tool": tool,
        "kind": "permission",
        "title": summarize(tool, args)[:400],
        "always": always_label(suggestions),
        "terminal_only": client == "kimi" and data.get("hook_event_name") == "PermissionRequest",
    }
    if client == "kimi" and data.get("hook_event_name") == "PreToolUse":
        request["always"] = "Auto mode"
    if tool == "AskUserQuestion" and args.get("questions"):
        request["kind"] = "question"
        request["questions"] = [
            {
                "question": q.get("question", ""),
                "header": q.get("header", ""),
                "multiSelect": bool(q.get("multiSelect") or q.get("multi_select")),
                "options": [o.get("label", "") for o in q.get("options") or []],
            }
            for q in args["questions"]
        ]
    elif tool == "ExitPlanMode":
        request["kind"] = "plan"
        request["title"] = "Approve the plan?"
    return request


def decision_for(answer: dict, data: dict, client: str) -> Optional[dict]:
    """Turn the overlay's answer into a PermissionRequest decision, or None to defer."""
    args = data.get("tool_input") or {}
    action = answer.get("action")
    if client == "codex":
        if action in ("allow", "always", "answer"):
            return {"behavior": "allow"}
        if action == "deny":
            return {"behavior": "deny", "message": DENY_MESSAGE}
        return None
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


def still_pending(session_id: str, request_id: str, created: float, client: str) -> bool:
    state = read_json(session_path(session_id, client), {})
    if request_id not in (state.get("requests") or {}):
        return False
    if state.get("pid") and not is_alive(state["pid"], state.get("pid_start")):
        return False
    if client != "claude":
        return True
    interrupted, mtime = was_interrupted(state.get("transcript"))
    return not (interrupted and mtime > created)


def wait_for_answer(session_id: str, request_id: str, created: float, timeout: float,
                    client: str) -> Optional[dict]:
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
            if not overlay_running() or not still_pending(session_id, request_id, created, client):
                return None
            next_check = now + 1
        time.sleep(0.15)
    return None


def handle_permission(data: dict, client: str) -> None:
    config = load_config()["interactive"]
    if not config.get("enabled") or not overlay_running():
        return
    os.makedirs(ANSWER_DIR, exist_ok=True)
    request = make_request(data, uuid.uuid4().hex, client)
    with update_session(data["session_id"], client) as state:
        state.setdefault("requests", {})[request["id"]] = request
    if client == "kimi":
        return

    try:
        answer = wait_for_answer(
            data["session_id"], request["id"], request["created"], config.get("timeout", 600), client
        )
    finally:
        with update_session(data["session_id"], client) as state:
            (state.get("requests") or {}).pop(request["id"], None)
            if not state.get("status"):
                state["_delete"] = True

    decision = answer and decision_for(answer, data, client)
    if decision:
        with update_session(data["session_id"], client) as state:
            state.update(status="working", since=time.time())
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": decision}}))


def kimi_decision(answer: Optional[dict], question: bool) -> Optional[dict]:
    if not answer:
        return None
    action = answer.get("action")
    reason = None
    if question and action == "answer":
        answers = json.dumps({"answers": answer.get("answers") or {}}, ensure_ascii=False)
        reason = (
            "The user answered the question in the overlay. Do not ask it again; "
            f"use these answers and continue: {answers}"
        )
    elif not question and action == "deny":
        reason = DENY_MESSAGE
    if reason is None:
        return None
    return {
        "hookSpecificOutput": {
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def handle_kimi_pretool(data: dict) -> None:
    question = kimi_question(data)
    if not question and not kimi_needs_approval(data):
        return
    config = load_config()["interactive"]
    if not config.get("enabled") or not overlay_running():
        return
    session_id = data["session_id"]
    state = read_json(session_path(session_id, "kimi"), {})
    if not question and state.get("overlay_auto"):
        return

    os.makedirs(ANSWER_DIR, exist_ok=True)
    request = make_request(data, uuid.uuid4().hex, "kimi")
    with update_session(session_id, "kimi") as session:
        session.setdefault("requests", {})[request["id"]] = request
        session.update(status="waiting", since=time.time())

    answer = None
    try:
        timeout = min(float(config.get("timeout", 600)), 570)
        answer = wait_for_answer(session_id, request["id"], request["created"], timeout, "kimi")
    finally:
        with update_session(session_id, "kimi") as session:
            (session.get("requests") or {}).pop(request["id"], None)
            if answer and answer.get("action") == "always":
                session["overlay_auto"] = True
            if question and (not answer or answer.get("action") == "defer"):
                request["terminal_only"] = True
                session.setdefault("requests", {})[request["id"]] = request
                session.update(status="waiting", since=time.time())
            else:
                session.update(status="working", since=time.time())

    decision = kimi_decision(answer, question)
    if decision:
        print(json.dumps(decision))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--client", choices=sorted(CLIENT_LABELS), default="claude")
    args = parser.parse_args()
    data = json.load(sys.stdin)
    event = data.get("hook_event_name")
    if not data.get("session_id") or not event:
        return
    record(data, event, args.client)
    if event == "PermissionRequest":
        handle_permission(data, args.client)
    elif args.client == "kimi" and event == "PreToolUse":
        handle_kimi_pretool(data)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
