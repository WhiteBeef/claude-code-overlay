#!/usr/bin/env python3
"""Removes selected client hooks; full uninstall also removes the shared overlay files."""
from __future__ import annotations

import argparse
import json
import shutil
from typing import List

from install import (
    CLIENTS,
    CODEX_HOOKS,
    DEST,
    DESKTOP_ENTRY,
    KIMI_SETTINGS,
    LAUNCH_AGENT,
    MACOS,
    RUN_KEY,
    RUN_VALUE,
    SETTINGS,
    WINDOWS,
    is_ours,
    load_settings,
    parse_clients,
    remove_hooks,
    remove_kimi_block,
    save_settings,
    stop_running,
)


def restore_statusline(settings: dict) -> None:
    if not is_ours((settings.get("statusLine") or {}).get("command")):
        return
    try:
        with (DEST / "statusline.json").open(encoding="utf-8") as file:
            original = json.load(file)
    except (OSError, ValueError):
        original = {}
    if original.get("command"):
        settings["statusLine"] = original
    else:
        settings.pop("statusLine", None)


def remove_autostart() -> None:
    if WINDOWS:
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, RUN_VALUE)
        except OSError:
            pass
        return
    path = LAUNCH_AGENT if MACOS else DESKTOP_ENTRY
    if path.exists():
        path.unlink()


def remove_json_client(client: str) -> None:
    path = SETTINGS if client == "claude" else CODEX_HOOKS
    if not path.exists():
        return
    settings = load_settings(path)
    remove_hooks(settings)
    if client == "claude":
        restore_statusline(settings)
    save_settings(settings, path)
    print(f"Hooks removed from {path}")


def remove_kimi_client() -> None:
    if not KIMI_SETTINGS.exists():
        return
    text = KIMI_SETTINGS.read_text(encoding="utf-8")
    updated = remove_kimi_block(text)
    if updated != text:
        KIMI_SETTINGS.write_text(updated, encoding="utf-8")
        print(f"Hooks removed from {KIMI_SETTINGS}")


def remove_client(client: str) -> None:
    if client == "kimi":
        remove_kimi_client()
    else:
        remove_json_client(client)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clients", type=parse_clients, default=list(CLIENTS),
                        help="clients to disconnect, or all (default: all)")
    args = parser.parse_args()
    clients: List[str] = args.clients
    full_uninstall = set(clients) == set(CLIENTS)
    if full_uninstall:
        stop_running()
    for client in clients:
        remove_client(client)
    if full_uninstall:
        remove_autostart()
        shutil.rmtree(DEST, ignore_errors=True)
        print("AI Code Overlay uninstalled.")
    else:
        print("Selected hooks removed; shared overlay files kept.")


if __name__ == "__main__":
    main()
