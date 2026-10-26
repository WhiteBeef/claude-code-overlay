#!/usr/bin/env bash
# Installs the overlay into ~/.claude/overlay, registers the Claude Code hooks
# and (unless --no-autostart) starts the overlay on login.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="$HOME/.claude/overlay"
SETTINGS="$HOME/.claude/settings.json"
AUTOSTART="$HOME/.config/autostart/claude-code-overlay.desktop"

autostart=1
[[ "${1:-}" == "--no-autostart" ]] && autostart=0

if ! python3 -c 'import gi; gi.require_version("Gtk", "3.0"); from gi.repository import Gtk; import cairo' 2>/dev/null; then
    echo "Missing GTK 3 bindings. On Debian/Ubuntu: sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-3.0" >&2
    exit 1
fi

mkdir -p "$DEST"
install -m 755 "$SRC/hook.py" "$SRC/overlay.py" "$DEST/"

mkdir -p "$(dirname "$SETTINGS")"
[[ -f "$SETTINGS" ]] && cp "$SETTINGS" "$SETTINGS.bak-overlay"
python3 - "$SETTINGS" "python3 $DEST/hook.py" <<'EOF'
import json, os, sys

path, command = sys.argv[1], sys.argv[2]
settings = {}
if os.path.exists(path):
    with open(path) as f:
        settings = json.load(f)
hooks = settings.setdefault("hooks", {})
for event in ["SessionStart", "SessionEnd", "UserPromptSubmit", "PreToolUse", "PostToolUse",
              "PermissionRequest", "Notification", "Stop"]:
    groups = hooks.setdefault(event, [])
    if any(h.get("command") == command for g in groups for h in g.get("hooks", [])):
        continue
    entry = {"hooks": [{"type": "command", "command": command, "timeout": 5}]}
    if event in ("PreToolUse", "PostToolUse", "PermissionRequest"):
        entry["matcher"] = "*"
    groups.append(entry)
with open(path, "w") as f:
    json.dump(settings, f, indent=2, ensure_ascii=False)
EOF
echo "Hooks added to $SETTINGS"

if (( autostart )); then
    mkdir -p "$(dirname "$AUTOSTART")"
    cat > "$AUTOSTART" <<EOF
[Desktop Entry]
Type=Application
Name=Claude Code Overlay
Comment=Floating Claude Code status indicator
Exec=python3 $DEST/overlay.py
X-GNOME-Autostart-enabled=true
NoDisplay=true
EOF
    echo "Autostart entry: $AUTOSTART"
fi

pgrep -f "$DEST/overlay.py" >/dev/null || nohup python3 "$DEST/overlay.py" >/dev/null 2>&1 &
echo "Overlay running. Restart open Claude Code sessions so they pick up the hooks."
