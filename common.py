"""Shared paths, config and process helpers for hook.py, statusline.py and overlay.py."""
import contextlib
import copy
import json
import os
import sys
import time
from datetime import datetime

try:
    import psutil
except ImportError:  # the overlay still works, it just can't tell when a session dies
    psutil = None

BASE_DIR = os.environ.get("CLAUDE_OVERLAY_DIR") or os.path.join(os.path.expanduser("~"), ".claude", "overlay")
STATE_DIR = os.path.join(BASE_DIR, "state")
ANSWER_DIR = os.path.join(BASE_DIR, "answers")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
POSITION_PATH = os.path.join(BASE_DIR, "position.json")
LIMITS_PATH = os.path.join(BASE_DIR, "limits.json")
HEARTBEAT_PATH = os.path.join(BASE_DIR, "overlay.alive")
LOCK_PATH = os.path.join(BASE_DIR, "overlay.lock")

DEFAULT_CONFIG = {
    # "top": one pill with the most urgent session; "all": one pill per session
    "sessions": "top",
    # what the pill shows besides the status
    "show": {
        "project": False,     # project folder name ("all" mode always shows it)
        "elapsed": True,      # turn duration / time since Claude stopped
        "tool": True,         # tool Claude is running right now
        "five_hour": True,    # 5-hour plan limit, percent used
        "seven_day": False,   # weekly plan limit, percent used
        "reset_in": False,    # time until the 5-hour window resets
    },
    # answer permission prompts and questions from the overlay
    "interactive": {
        "enabled": True,
        "timeout": 600,       # seconds a prompt waits for an overlay click
    },
    # "click": a single click shows the gear and close buttons; "hover": hovering does
    "controls": "click",
    "scale": 1.0,
    "opacity": 0.9,
    "font": "",               # font family, empty = system default
    "limit_warn": 70,         # limit percent shown amber from here
    "limit_alert": 90,        # ... and red from here
    "colors": {
        "accent": "#D97757",
        "background": "#1C1C1F",
        "text": "#EDEDED",
        "dim": "#9E9E9E",
        "waiting": "#FFA840",
        "done": "#73D985",
        "offline": "#8C8C8C",
        "alert": "#FF6B6B",
    },
    "labels": {
        "waiting": "Needs you",
        "working": "Working",
        "done": "Your turn",
        "idle": "Ready",
        "offline": "No session",
    },
}


def merge(base, override):
    """Deep-merge `override` into a copy of `base`, ignoring keys `base` doesn't know."""
    result = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(result.get(key), dict) and isinstance(value, dict):
            result[key] = merge(result[key], value)
        elif key in result:
            result[key] = value
    return result


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, data, indent=None):
    """Atomic write; retries because Windows refuses to replace a file someone is reading."""
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=indent, ensure_ascii=False)
    for attempt in range(20):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.02)


def load_config():
    return merge(DEFAULT_CONFIG, read_json(CONFIG_PATH, {}))


def overlay_running():
    try:
        return time.time() - os.path.getmtime(HEARTBEAT_PATH) < 6
    except OSError:
        return False


@contextlib.contextmanager
def file_lock(path):
    """Exclusive lock on `path`, held for the duration of the block."""
    with open(path, "a+") as f:
        if sys.platform == "win32":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)


def session_path(session_id):
    return os.path.join(STATE_DIR, f"{session_id}.json")


@contextlib.contextmanager
def update_session(session_id):
    """Read-modify-write a session's state file under its lock. Yields the state dict."""
    os.makedirs(STATE_DIR, exist_ok=True)
    path = session_path(session_id)
    with file_lock(path[:-5] + ".lock"):
        state = read_json(path, {})
        yield state
        if state.get("_delete"):
            for p in (path, path[:-5] + ".lock"):
                with contextlib.suppress(OSError):
                    os.remove(p)
        else:
            write_json(path, state)


# --- processes ---------------------------------------------------------------


def process_start(pid):
    """Creation time of a process, or None if it is gone."""
    if psutil is None:
        return None
    try:
        return psutil.Process(pid).create_time()
    except (psutil.Error, ValueError):
        return None


def is_alive(pid, started):
    if psutil is None:
        return True
    actual = process_start(pid)
    return actual is not None and isinstance(started, (int, float)) and abs(actual - started) < 1


def is_claude(proc):
    name = proc.name().lower()
    if name in ("claude", "claude.exe"):
        return True
    if name.startswith("node"):
        argv = proc.cmdline()
        return len(argv) > 1 and "claude" in argv[1].lower()
    return False


def find_claude_pid():
    """Walk up the process tree to the Claude Code process that ran this hook."""
    if psutil is None:
        return None, None
    try:
        proc = psutil.Process(os.getppid())
        for _ in range(8):
            if is_claude(proc):
                return proc.pid, proc.create_time()
            proc = proc.parent()
            if proc is None or proc.pid <= 1:
                break
    except psutil.Error:
        pass
    return None, None


def has_busy_child(pid, after):
    """True if the process has a child started after `after` that has run for over a second.

    Covers the gap between approving a permission prompt and the tool finishing
    (e.g. a long build), since Claude Code sends no hook event for an approval.
    """
    if psutil is None:
        return False
    now = time.time()
    try:
        children = psutil.Process(pid).children()
    except psutil.Error:
        return False
    for child in children:
        try:
            started = child.create_time()
        except psutil.Error:
            continue
        if started > after and now - started > 1.0:
            return True
    return False


_interrupt_cache = {}


def was_interrupted(transcript):
    """(interrupted, mtime): does the transcript end with the "interrupted by user" marker?

    Pressing Esc or rejecting a tool ends the turn without a Stop hook.
    """
    try:
        mtime = os.path.getmtime(transcript)
    except (OSError, TypeError):
        return False, 0
    cached = _interrupt_cache.get(transcript)
    if cached and cached[0] == mtime:
        return cached[1], mtime
    result = False
    try:
        with open(transcript, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 65536))
            tail = f.read()
        for line in reversed(tail.splitlines()):
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            kind = entry.get("type")
            if kind == "assistant":
                break
            if kind == "user":
                result = "[Request interrupted by user" in json.dumps(entry.get("message"))
                break
    except OSError:
        pass
    _interrupt_cache[transcript] = (mtime, result)
    return result, mtime


def last_response_time(transcript):
    """Epoch time of the last assistant message in the transcript, 0 if unknown."""
    try:
        with open(transcript, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 65536))
            tail = f.read()
    except (OSError, TypeError):
        return 0
    for line in reversed(tail.splitlines()):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and entry.get("type") == "assistant":
            try:
                return datetime.fromisoformat(entry["timestamp"].replace("Z", "+00:00")).timestamp()
            except (KeyError, AttributeError, ValueError):
                return 0
    return 0
