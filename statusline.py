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
from common import BASE_DIR, LIMITS_PATH, read_json, write_json  # noqa: E402

ORIGINAL_PATH = os.path.join(BASE_DIR, "statusline.json")
SEEN_TTL = 24 * 3600  # forget sessions that have not rendered for a day


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
    # fresher numbers with stale ones. Only a session whose API time moved since its
    # previous render has just heard from the API, so only that one gets to write.
    now = time.time()
    saved = read_json(LIMITS_PATH) or {}
    seen = {sid: v for sid, v in (saved.get("seen") or {}).items() if now - v[1] < SEEN_TTL}
    sid = data.get("session_id")
    api_ms = (data.get("cost") or {}).get("total_api_duration_ms")
    if sid and api_ms is not None:
        fresh = sid not in seen or seen[sid][0] != api_ms
        seen[sid] = [api_ms, now]
        if not fresh:
            limits = {k: saved[k] for k in ("five_hour", "seven_day") if k in saved}
    try:
        write_json(LIMITS_PATH, dict(limits, updated=now, seen=seen))
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
