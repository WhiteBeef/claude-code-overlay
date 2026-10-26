#!/usr/bin/env python3
"""Stops the overlay and removes its hooks, status line wrapper, autostart entry and files."""
import json
import os
import shutil

from install import (
    DEST,
    DESKTOP_ENTRY,
    LAUNCH_AGENT,
    MACOS,
    RUN_KEY,
    RUN_VALUE,
    SETTINGS,
    WINDOWS,
    is_ours,
    load_settings,
    remove_hooks,
    save_settings,
    stop_running,
)


def restore_statusline(settings):
    if not is_ours((settings.get("statusLine") or {}).get("command")):
        return
    try:
        with open(os.path.join(DEST, "statusline.json"), encoding="utf-8") as f:
            original = json.load(f)
    except (OSError, ValueError):
        original = {}
    if original.get("command"):
        settings["statusLine"] = original
    else:
        settings.pop("statusLine", None)


def remove_autostart():
    if WINDOWS:
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, RUN_VALUE)
        except OSError:
            pass
        return
    path = LAUNCH_AGENT if MACOS else DESKTOP_ENTRY
    if os.path.exists(path):
        os.remove(path)


def main():
    stop_running()
    if os.path.exists(SETTINGS):
        settings = load_settings()
        remove_hooks(settings)
        restore_statusline(settings)
        save_settings(settings)
        print(f"Hooks removed from {SETTINGS}")
    remove_autostart()
    shutil.rmtree(DEST, ignore_errors=True)
    print("Claude Code Overlay uninstalled.")


if __name__ == "__main__":
    main()
