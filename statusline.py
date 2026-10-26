#!/usr/bin/env python3
"""Claude Code status line wrapper: saves the plan rate limits for overlay.py.

Claude Code passes `rate_limits` (5-hour and weekly usage) only to the status line
command, so the installer points `statusLine` here. The JSON is then handed to the
user's original status line command (kept in ~/.claude/overlay/statusline.json)
and its output is printed unchanged.
"""
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import BASE_DIR, LIMITS_PATH, last_response_time, read_json, write_json  # noqa: E402

ORIGINAL_PATH = os.path.join(BASE_DIR, "statusline.json")


def save_limits(raw):
    try:
        data = json.loads(raw)
        limits = data.get("rate_limits")
    except (ValueError, AttributeError):
        return
    if not limits:
        return
    # Every session re-renders its status line with the rate_limits of its own last
    # API response, so an idle session (maybe on another account) would overwrite
    # fresher numbers with stale ones. The numbers that came from the latest API
    # response win, dated by the last assistant message in the session's transcript.
    at = last_response_time(data.get("transcript_path"))
    if at < (read_json(LIMITS_PATH) or {}).get("at", 0):
        return
    try:
        write_json(LIMITS_PATH, dict(limits, updated=time.time(), at=at))
    except OSError:
        pass


def main():
    raw = sys.stdin.buffer.read()
    save_limits(raw)
    command = (read_json(ORIGINAL_PATH) or {}).get("command")
    if not command:
        return
    try:
        result = subprocess.run(command, shell=True, input=raw, capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return
    sys.stdout.buffer.write(result.stdout)


if __name__ == "__main__":
    main()
