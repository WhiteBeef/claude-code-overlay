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


def save_limits(raw):
    try:
        limits = json.loads(raw).get("rate_limits")
    except (ValueError, AttributeError):
        return
    if not limits:
        return
    try:
        write_json(LIMITS_PATH, dict(limits, updated=time.time()))
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
