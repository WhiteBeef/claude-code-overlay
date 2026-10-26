#!/usr/bin/env bash
# Stops the overlay and removes its hooks, autostart entry and files.
set -euo pipefail

DEST="$HOME/.claude/overlay"
SETTINGS="$HOME/.claude/settings.json"

pkill -f "$DEST/overlay.py" || true

if [[ -f "$SETTINGS" ]]; then
    python3 - "$SETTINGS" "python3 $DEST/hook.py" <<'EOF'
import json, sys

path, command = sys.argv[1], sys.argv[2]
with open(path) as f:
    settings = json.load(f)
hooks = settings.get("hooks", {})
for event in list(hooks):
    groups = []
    for group in hooks[event]:
        group["hooks"] = [h for h in group.get("hooks", []) if h.get("command") != command]
        if group["hooks"]:
            groups.append(group)
    if groups:
        hooks[event] = groups
    else:
        del hooks[event]
if not hooks:
    settings.pop("hooks", None)
with open(path, "w") as f:
    json.dump(settings, f, indent=2, ensure_ascii=False)
EOF
    echo "Hooks removed from $SETTINGS"
fi

rm -f "$HOME/.config/autostart/claude-code-overlay.desktop"
rm -rf "$DEST"
echo "Claude Code Overlay uninstalled."
