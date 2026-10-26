#!/usr/bin/env python3
"""Claude Code hook: records each session's status for overlay.py.

Reads the hook event JSON from stdin and writes ~/.claude/overlay/state/<session_id>.json.
Never prints anything and always exits 0, so it can't interfere with Claude Code.
"""
import json
import os
import sys
import time

STATE_DIR = os.path.expanduser("~/.claude/overlay/state")


def read_stat(pid):
    """Return (ppid, starttime) from /proc/<pid>/stat."""
    with open(f"/proc/{pid}/stat") as f:
        fields = f.read().rsplit(")", 1)[1].split()
    return int(fields[1]), fields[19]


def is_claude(pid):
    with open(f"/proc/{pid}/cmdline", "rb") as f:
        argv = [a.decode(errors="replace") for a in f.read().split(b"\0") if a]
    if not argv:
        return False
    exe = os.path.basename(argv[0])
    return exe == "claude" or (exe.startswith("node") and len(argv) > 1 and "claude" in argv[1])


def find_claude_pid():
    """Walk up the process tree to the Claude Code process that ran this hook."""
    pid = os.getppid()
    for _ in range(8):
        try:
            if is_claude(pid):
                return pid, read_stat(pid)[1]
            pid = read_stat(pid)[0]
        except (OSError, ValueError, IndexError):
            break
        if pid <= 1:
            break
    return None, None


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


def main():
    data = json.load(sys.stdin)
    session_id = data.get("session_id")
    event = data.get("hook_event_name")
    if not session_id or not event:
        return
    os.makedirs(STATE_DIR, exist_ok=True)
    path = os.path.join(STATE_DIR, f"{session_id}.json")

    if event == "SessionEnd":
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        return

    try:
        with open(path) as f:
            state = json.load(f)
    except (OSError, ValueError):
        state = {}

    prev = state.get("status")
    status = new_status(event, data, prev)
    if status is None:
        return

    now = time.time()
    if event == "UserPromptSubmit" or (status == "working" and "turn_start" not in state):
        state["turn_start"] = now
    if event == "PreToolUse":
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
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, path)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
