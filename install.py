#!/usr/bin/env python3
"""Installs the overlay for Claude Code, Kimi Code and Codex.

1. copies the scripts to ~/.claude/overlay and creates a virtualenv there with PySide6 and psutil;
2. registers hooks for each selected client, preserving existing settings and saving backups;
3. optionally wraps Claude Code's status line to record plan limits;
4. starts the overlay on login (unless --no-autostart) and right now.

Usage: python3 install.py [--clients claude,kimi,codex|all] [--no-autostart] [--no-statusline]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple

SRC = Path(__file__).resolve().parent
DEST = Path.home() / ".claude" / "overlay"
CLAUDE_HOME = Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))).expanduser()
KIMI_HOME = Path(os.environ.get("KIMI_CODE_HOME", str(Path.home() / ".kimi-code"))).expanduser()
CODEX_HOME = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()
SETTINGS = CLAUDE_HOME / "settings.json"
KIMI_SETTINGS = KIMI_HOME / "config.toml"
CODEX_HOOKS = CODEX_HOME / "hooks.json"
FILES = ["common.py", "hook.py", "statusline.py", "overlay.py"]
PACKAGES = ["PySide6-Essentials", "psutil"]
CLIENTS = ("claude", "kimi", "codex")
EVENTS = {
    "claude": [
        "SessionStart", "SessionEnd", "UserPromptSubmit", "PreToolUse", "PostToolUse",
        "PermissionRequest", "Notification", "Stop",
    ],
    "kimi": [
        "SessionStart", "SessionEnd", "UserPromptSubmit", "PreToolUse", "PostToolUse",
        "PostToolUseFailure", "PermissionRequest", "PermissionResult", "Stop", "StopFailure", "Interrupt",
    ],
    "codex": [
        "SessionStart", "SessionEnd", "UserPromptSubmit", "PreToolUse", "PostToolUse",
        "PermissionRequest", "Stop", "Interrupt",
    ],
}
WINDOWS = sys.platform == "win32"
MACOS = sys.platform == "darwin"

LAUNCH_AGENT = Path.home() / "Library" / "LaunchAgents" / "com.github.whitebeef.claude-code-overlay.plist"
DESKTOP_ENTRY = Path.home() / ".config" / "autostart" / "claude-code-overlay.desktop"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "ClaudeCodeOverlay"
KIMI_BEGIN = "# BEGIN claude-code-overlay"
KIMI_END = "# END claude-code-overlay"
KIMI_ORIGINAL_MODE = "# overlay-original-permission-mode:"


def venv_python(gui: bool = False) -> Path:
    if WINDOWS:
        return DEST / "venv" / "Scripts" / ("pythonw.exe" if gui else "python.exe")
    return DEST / "venv" / "bin" / "python"


def git_bash_available() -> bool:
    return bool(os.environ.get("CLAUDE_CODE_GIT_BASH_PATH") or shutil.which("bash")
                or Path(r"C:\Program Files\Git\bin\bash.exe").exists())


def shell_command(*args: object) -> Tuple[str, Optional[str]]:
    """A command line for client configuration, plus the shell field it needs (or None)."""
    parts = [str(arg) for arg in args]
    if not WINDOWS:
        return " ".join(f'"{part}"' if " " in part else part for part in parts), None
    quoted = " ".join(f'"{part.replace(chr(92), "/")}"' for part in parts)
    if git_bash_available():
        return quoted, None
    return "& " + quoted, "powershell"


def is_ours(command: object) -> bool:
    return isinstance(command, str) and any(
        f"overlay{sep}{name}" in command for sep in ("/", "\\") for name in ("hook.py", "statusline.py"))


# --- steps ---------------------------------------------------------------------


def stop_running() -> None:
    """Stop any overlay started from DEST, including the old GTK version."""
    script = DEST / "overlay.py"
    if venv_python().exists():
        code = (
            "import psutil, sys\n"
            "for p in psutil.process_iter(['cmdline']):\n"
            "    if any(a.replace('\\\\', '/') == sys.argv[1] for a in p.info['cmdline'] or []):\n"
            "        p.terminate()\n"
        )
        subprocess.run([venv_python(), "-c", code, str(script).replace("\\", "/")], check=False)
    elif not WINDOWS:
        subprocess.run(["pkill", "-f", str(script)], check=False)


def copy_files() -> None:
    DEST.mkdir(parents=True, exist_ok=True)
    for name in FILES:
        shutil.copy2(SRC / name, DEST / name)


def make_venv() -> None:
    if not venv_python().exists():
        print("Creating virtualenv…")
        try:
            subprocess.run([sys.executable, "-m", "venv", str(DEST / "venv")], check=True)
        except subprocess.CalledProcessError:
            sys.exit("Could not create a virtualenv. On Debian/Ubuntu: sudo apt install python3-venv")
    print("Installing " + ", ".join(PACKAGES) + "…")
    subprocess.run([venv_python(), "-m", "pip", "install", "--quiet", "--upgrade", "pip"], check=True)
    subprocess.run([venv_python(), "-m", "pip", "install", "--quiet", "--upgrade", *PACKAGES], check=True)


def check_linux_libs() -> None:
    if WINDOWS or MACOS:
        return
    import ctypes.util
    if not ctypes.util.find_library("xcb-cursor"):
        print("Warning: Qt needs libxcb-cursor. On Debian/Ubuntu: sudo apt install libxcb-cursor0")


def backup(path: Path) -> None:
    if path.exists():
        shutil.copy2(path, Path(str(path) + ".bak-overlay"))


def load_settings(path: Path = SETTINGS) -> dict:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as file:
        return json.load(file)


def save_settings(settings: dict, path: Path = SETTINGS) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        json.dump(settings, file, indent=2, ensure_ascii=False)
        file.write("\n")


def remove_hooks(settings: dict) -> None:
    hooks = settings.get("hooks", {})
    for event in list(hooks):
        groups = []
        for group in hooks[event]:
            group["hooks"] = [hook for hook in group.get("hooks", []) if not is_ours(hook.get("command"))]
            if group["hooks"]:
                groups.append(group)
        if groups:
            hooks[event] = groups
        else:
            del hooks[event]
    if not hooks:
        settings.pop("hooks", None)


def make_hook(client: str, event: str) -> dict:
    command, shell = shell_command(venv_python(), DEST / "hook.py", "--client", client)
    if client == "codex" and event in ("SessionEnd", "Interrupt"):
        timeout = 3
    else:
        timeout = 3600 if event == "PermissionRequest" and client != "kimi" else 10
    hook = {
        "type": "command",
        "command": command,
        # PermissionRequest waits for an overlay click; its own limit is in config.json
        "timeout": timeout,
    }
    if client == "claude" and shell:
        hook["shell"] = shell
    if client == "codex" and WINDOWS and shell:
        hook["commandWindows"] = command
        hook["command"] = command[2:]
    return hook


def add_hooks(settings: dict, client: str) -> None:
    hooks = settings.setdefault("hooks", {})
    for event in EVENTS[client]:
        entry = {"hooks": [make_hook(client, event)]}
        if event in ("PreToolUse", "PostToolUse", "PermissionRequest"):
            entry["matcher"] = "*"
        hooks.setdefault(event, []).append(entry)


def wrap_statusline(settings: dict) -> None:
    current = settings.get("statusLine") or {}
    if not is_ours(current.get("command")):
        with (DEST / "statusline.json").open("w", encoding="utf-8") as file:
            json.dump(current, file, indent=2)
    command, _shell = shell_command(venv_python(), DEST / "statusline.py")
    settings["statusLine"] = dict(current, type="command", command=command)


def install_json_hooks(client: str, with_statusline: bool) -> None:
    path = SETTINGS if client == "claude" else CODEX_HOOKS
    settings = load_settings(path)
    backup(path)
    remove_hooks(settings)
    add_hooks(settings, client)
    if client == "claude" and with_statusline:
        wrap_statusline(settings)
    save_settings(settings, path)
    print(f"Hooks added to {path}")


def pop_kimi_permission_mode(text: str) -> Tuple[str, Optional[str]]:
    first_table = re.search(r"(?m)^\s*\[", text)
    split_at = first_table.start() if first_table else len(text)
    top, rest = text[:split_at], text[split_at:]
    pattern = re.compile(r'(?m)^\s*default_permission_mode\s*=\s*("(manual|yolo|auto)")\s*(?:#.*)?$')
    match = pattern.search(top)
    if not match:
        return text, None
    mode = json.loads(match.group(1))
    top = top[:match.start()] + top[match.end():]
    return top + rest, mode


def prepend_kimi_permission_mode(text: str, mode: str) -> str:
    return f'default_permission_mode = "{mode}"\n\n' + text.lstrip("\n")


def remove_kimi_block(text: str) -> str:
    original_mode = None
    mode_recorded = False
    while KIMI_BEGIN in text:
        start = text.index(KIMI_BEGIN)
        end = text.find(KIMI_END, start)
        if end < 0:
            raise ValueError(f"Missing {KIMI_END} in Kimi Code config")
        end = text.find("\n", end + len(KIMI_END))
        end = len(text) if end < 0 else end + 1
        block = text[start:end]
        marker = re.search(rf"(?m)^{re.escape(KIMI_ORIGINAL_MODE)}\s*(\S+)\s*$", block)
        if marker:
            original_mode = json.loads(marker.group(1))
            mode_recorded = True
        text = text[:start] + text[end:]
    if mode_recorded:
        text, _current_mode = pop_kimi_permission_mode(text)
        if original_mode:
            text = prepend_kimi_permission_mode(text, original_mode)
    return text


def kimi_block(original_mode: Optional[str]) -> str:
    lines = [
        KIMI_BEGIN,
        f"{KIMI_ORIGINAL_MODE} {json.dumps(original_mode)}",
    ]
    for event in EVENTS["kimi"]:
        command, _shell = shell_command(venv_python(), DEST / "hook.py", "--client", "kimi")
        lines.extend([
            "[[hooks]]",
            f'event = "{event}"',
            f"command = {json.dumps(command)}",
            f"timeout = {600 if event == 'PreToolUse' else 10}",
            "",
        ])
    lines.append(KIMI_END)
    return "\n".join(lines) + "\n"


def install_kimi_hooks() -> None:
    text = KIMI_SETTINGS.read_text(encoding="utf-8") if KIMI_SETTINGS.exists() else ""
    backup(KIMI_SETTINGS)
    text = remove_kimi_block(text)
    text, original_mode = pop_kimi_permission_mode(text)
    existing = text.rstrip()
    text = prepend_kimi_permission_mode(existing, "auto") + "\n\n" + kimi_block(original_mode)
    KIMI_SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    KIMI_SETTINGS.write_text(text, encoding="utf-8")
    print(f"Hooks added to {KIMI_SETTINGS}")


def install_client(client: str, with_statusline: bool) -> None:
    if client == "kimi":
        install_kimi_hooks()
    else:
        install_json_hooks(client, with_statusline)


def autostart() -> str:
    python, script = venv_python(gui=True), DEST / "overlay.py"
    if WINDOWS:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, f'"{python}" "{script}"')
        return "Registry: HKCU\\" + RUN_KEY
    if MACOS:
        LAUNCH_AGENT.parent.mkdir(parents=True, exist_ok=True)
        with LAUNCH_AGENT.open("w") as file:
            file.write(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.github.whitebeef.claude-code-overlay</string>
  <key>ProgramArguments</key><array><string>{python}</string><string>{script}</string></array>
  <key>RunAtLoad</key><true/>
</dict>
</plist>
""")
        return str(LAUNCH_AGENT)
    DESKTOP_ENTRY.parent.mkdir(parents=True, exist_ok=True)
    with DESKTOP_ENTRY.open("w") as file:
        file.write(f"""[Desktop Entry]
Type=Application
Name=AI Code Overlay
Comment=Floating coding agent status indicator
Exec="{python}" "{script}"
X-GNOME-Autostart-enabled=true
NoDisplay=true
""")
    return str(DESKTOP_ENTRY)


def start() -> None:
    args = [venv_python(gui=True), DEST / "overlay.py"]
    if WINDOWS:
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        subprocess.Popen(args, creationflags=flags, close_fds=True)
    else:
        subprocess.Popen(args, start_new_session=True, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def parse_clients(value: str) -> List[str]:
    if value == "all":
        return list(CLIENTS)
    clients = [client.strip().lower() for client in value.split(",") if client.strip()]
    if not clients or any(client not in CLIENTS for client in clients):
        raise argparse.ArgumentTypeError("use claude,kimi,codex or all")
    return list(dict.fromkeys(clients))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clients", type=parse_clients, default=["claude"],
                        help="comma-separated clients: claude,kimi,codex, or all (default: claude)")
    parser.add_argument("--no-autostart", action="store_true")
    parser.add_argument("--no-statusline", action="store_true", help="do not wrap Claude Code's status line")
    args = parser.parse_args()

    stop_running()
    copy_files()
    make_venv()
    check_linux_libs()
    for client in args.clients:
        install_client(client, not args.no_statusline)

    if not args.no_autostart:
        print(f"Autostart: {autostart()}")
    start()
    print("Overlay running. Restart open coding agent sessions so they pick up the hooks.")
    if "codex" in args.clients:
        print("Codex: run /hooks and trust the overlay hooks.")
    if "kimi" in args.clients:
        print("Kimi Code: Auto mode enabled for new sessions; run /auto in existing sessions.")
    print(f"Settings: click the overlay, then the gear, or edit {DEST / 'config.json'}")


if __name__ == "__main__":
    main()
