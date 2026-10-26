#!/usr/bin/env python3
"""Installs Claude Code Overlay on Linux, macOS and Windows.

1. copies the scripts to ~/.claude/overlay and creates a virtualenv there with PySide6 and psutil;
2. registers the hooks in ~/.claude/settings.json (backup: settings.json.bak-overlay);
3. points statusLine at statusline.py, which records the plan limits and then runs your
   previous status line command unchanged;
4. starts the overlay on login (unless --no-autostart) and right now.

Usage: python3 install.py [--no-autostart] [--no-statusline]
"""
import json
import os
import shutil
import subprocess
import sys

SRC = os.path.dirname(os.path.abspath(__file__))
DEST = os.path.join(os.path.expanduser("~"), ".claude", "overlay")
SETTINGS = os.path.join(os.path.expanduser("~"), ".claude", "settings.json")
FILES = ["common.py", "hook.py", "statusline.py", "overlay.py"]
PACKAGES = ["PySide6-Essentials", "psutil"]
EVENTS = ["SessionStart", "SessionEnd", "UserPromptSubmit", "PreToolUse", "PostToolUse",
          "PermissionRequest", "Notification", "Stop"]
WINDOWS = sys.platform == "win32"
MACOS = sys.platform == "darwin"

LAUNCH_AGENT = os.path.expanduser("~/Library/LaunchAgents/com.github.whitebeef.claude-code-overlay.plist")
DESKTOP_ENTRY = os.path.expanduser("~/.config/autostart/claude-code-overlay.desktop")
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "ClaudeCodeOverlay"


def venv_python(gui=False):
    if WINDOWS:
        return os.path.join(DEST, "venv", "Scripts", "pythonw.exe" if gui else "python.exe")
    return os.path.join(DEST, "venv", "bin", "python")


def git_bash_available():
    return bool(os.environ.get("CLAUDE_CODE_GIT_BASH_PATH") or shutil.which("bash")
                or os.path.exists(r"C:\Program Files\Git\bin\bash.exe"))


def shell_command(*args):
    """A command line for settings.json, plus the shell field it needs (or None)."""
    if not WINDOWS:
        return " ".join(f'"{a}"' if " " in a else a for a in args), None
    quoted = " ".join(f'"{a}"' for a in (a.replace("\\", "/") for a in args))
    if git_bash_available():
        return quoted, None
    return "& " + quoted, "powershell"


def is_ours(command):
    return isinstance(command, str) and any(
        f"overlay{sep}{name}" in command for sep in ("/", "\\") for name in ("hook.py", "statusline.py"))


# --- steps ---------------------------------------------------------------------


def stop_running():
    """Stop any overlay started from DEST, including the old GTK version."""
    script = os.path.join(DEST, "overlay.py")
    if os.path.exists(venv_python()):
        code = (
            "import psutil, sys\n"
            "for p in psutil.process_iter(['cmdline']):\n"
            "    if any(a.replace('\\\\', '/') == sys.argv[1] for a in p.info['cmdline'] or []):\n"
            "        p.terminate()\n"
        )
        subprocess.run([venv_python(), "-c", code, script.replace("\\", "/")], check=False)
    elif not WINDOWS:
        subprocess.run(["pkill", "-f", script], check=False)


def copy_files():
    os.makedirs(DEST, exist_ok=True)
    for name in FILES:
        shutil.copy2(os.path.join(SRC, name), os.path.join(DEST, name))


def make_venv():
    if not os.path.exists(venv_python()):
        print("Creating virtualenv…")
        try:
            subprocess.run([sys.executable, "-m", "venv", os.path.join(DEST, "venv")], check=True)
        except subprocess.CalledProcessError:
            sys.exit("Could not create a virtualenv. On Debian/Ubuntu: sudo apt install python3-venv")
    print("Installing " + ", ".join(PACKAGES) + "…")
    subprocess.run([venv_python(), "-m", "pip", "install", "--quiet", "--upgrade", "pip"], check=True)
    subprocess.run([venv_python(), "-m", "pip", "install", "--quiet", "--upgrade", *PACKAGES], check=True)


def check_linux_libs():
    if WINDOWS or MACOS:
        return
    import ctypes.util
    if not ctypes.util.find_library("xcb-cursor"):
        print("Warning: Qt needs libxcb-cursor. On Debian/Ubuntu: sudo apt install libxcb-cursor0")


def load_settings():
    if not os.path.exists(SETTINGS):
        return {}
    with open(SETTINGS, encoding="utf-8") as f:
        return json.load(f)


def save_settings(settings):
    os.makedirs(os.path.dirname(SETTINGS), exist_ok=True)
    with open(SETTINGS, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2, ensure_ascii=False)
        f.write("\n")


def remove_hooks(settings):
    hooks = settings.get("hooks", {})
    for event in list(hooks):
        groups = []
        for group in hooks[event]:
            group["hooks"] = [h for h in group.get("hooks", []) if not is_ours(h.get("command"))]
            if group["hooks"]:
                groups.append(group)
        if groups:
            hooks[event] = groups
        else:
            del hooks[event]
    if not hooks:
        settings.pop("hooks", None)


def add_hooks(settings):
    command, shell = shell_command(venv_python(), os.path.join(DEST, "hook.py"))
    hooks = settings.setdefault("hooks", {})
    for event in EVENTS:
        hook = {"type": "command", "command": command,
                # PermissionRequest waits for an overlay click; its own limit is in config.json
                "timeout": 3600 if event == "PermissionRequest" else 10}
        if shell:
            hook["shell"] = shell
        entry = {"hooks": [hook]}
        if event in ("PreToolUse", "PostToolUse", "PermissionRequest"):
            entry["matcher"] = "*"
        hooks.setdefault(event, []).append(entry)


def wrap_statusline(settings):
    current = settings.get("statusLine") or {}
    if not is_ours(current.get("command")):
        with open(os.path.join(DEST, "statusline.json"), "w", encoding="utf-8") as f:
            json.dump(current, f, indent=2)
    command, _shell = shell_command(venv_python(), os.path.join(DEST, "statusline.py"))
    settings["statusLine"] = dict(current, type="command", command=command)


def autostart():
    python, script = venv_python(gui=True), os.path.join(DEST, "overlay.py")
    if WINDOWS:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, f'"{python}" "{script}"')
        return "Registry: HKCU\\" + RUN_KEY
    if MACOS:
        os.makedirs(os.path.dirname(LAUNCH_AGENT), exist_ok=True)
        with open(LAUNCH_AGENT, "w") as f:
            f.write(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.github.whitebeef.claude-code-overlay</string>
  <key>ProgramArguments</key><array><string>{python}</string><string>{script}</string></array>
  <key>RunAtLoad</key><true/>
</dict>
</plist>
""")
        return LAUNCH_AGENT
    os.makedirs(os.path.dirname(DESKTOP_ENTRY), exist_ok=True)
    with open(DESKTOP_ENTRY, "w") as f:
        f.write(f"""[Desktop Entry]
Type=Application
Name=Claude Code Overlay
Comment=Floating Claude Code status indicator
Exec="{python}" "{script}"
X-GNOME-Autostart-enabled=true
NoDisplay=true
""")
    return DESKTOP_ENTRY


def start():
    args = [venv_python(gui=True), os.path.join(DEST, "overlay.py")]
    if WINDOWS:
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        subprocess.Popen(args, creationflags=flags, close_fds=True)
    else:
        subprocess.Popen(args, start_new_session=True, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main():
    flags = set(sys.argv[1:])
    stop_running()
    copy_files()
    make_venv()
    check_linux_libs()

    settings = load_settings()
    if os.path.exists(SETTINGS):
        shutil.copy2(SETTINGS, SETTINGS + ".bak-overlay")
    remove_hooks(settings)
    add_hooks(settings)
    if "--no-statusline" not in flags:
        wrap_statusline(settings)
    save_settings(settings)
    print(f"Hooks added to {SETTINGS}")

    if "--no-autostart" not in flags:
        print(f"Autostart: {autostart()}")
    start()
    print("Overlay running. Restart open Claude Code sessions so they pick up the hooks.")
    print(f"Settings: click the overlay, then the gear, or edit {os.path.join(DEST, 'config.json')}")


if __name__ == "__main__":
    main()
