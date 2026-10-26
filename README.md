# Claude Code Overlay

A tiny always-on-top pill that shows what [Claude Code](https://claude.com/claude-code) is doing — so you can watch a video and still know the moment Claude needs you. When Claude asks for permission or asks you a question, you can answer right from the overlay.

Works on Linux, macOS and Windows.

![Overlay states](docs/preview.png)

| State | Meaning |
|---|---|
| **Working...** | Claude is busy. The logo spins and the text shimmers; elapsed time and current tool are shown. |
| **Needs you** | Claude is waiting for a permission or a question answer. The border and text pulse amber, and buttons to answer appear under the pill. |
| **Your turn** | Claude finished its turn and waits for your next prompt. |
| **Ready / No session** | Session is idle, or no Claude Code session is running. The logo slowly "breathes" so you can see the overlay is alive. |

Works with every Claude Code session — terminal or the VS Code extension. Show only the most urgent session (with a `+N` counter), or one pill per session.

## Requirements

- Python 3.9+. The installer creates its own virtualenv with [PySide6](https://pypi.org/project/PySide6-Essentials/) and [psutil](https://pypi.org/project/psutil/).
- Linux: X11, or GNOME/KDE on Wayland (the window runs through XWayland). Qt needs `libxcb-cursor0`, and Debian/Ubuntu need `python3-venv`:

```bash
sudo apt install python3-venv libxcb-cursor0
```

## Install

```bash
git clone https://github.com/WhiteBeef/claude-code-overlay.git
cd claude-code-overlay
./install.sh                 # Windows: py install.py
```

Options: `--no-autostart`, `--no-statusline`.

The installer:

1. copies the scripts to `~/.claude/overlay/` and creates a virtualenv there;
2. adds hooks to `~/.claude/settings.json` (a backup is saved as `settings.json.bak-overlay`);
3. points `statusLine` at the overlay's wrapper, which saves your plan limits and then runs your previous status line command, so your status line looks the same;
4. starts the overlay on login (`~/.config/autostart` on Linux, a LaunchAgent on macOS, the `Run` registry key on Windows) and right away.

Restart already open Claude Code sessions so they load the hooks.

## Usage

- **Drag** to move. The position is remembered.
- **Click** once to show the ⚙ settings and ✕ close buttons (or set `"controls": "hover"`). Click again to hide them. Tap-and-drag on a touchpad moves the overlay without toggling the buttons.
- **Right-click** for the menu: *Settings*, *Reset position*, *Quit*.
- **Answer prompts:** when Claude asks for permission, the overlay shows *Allow*, *Always allow* (when Claude Code offers a rule) and *Deny*. For a question, it shows the options. The terminal prompt stays open at the same time, so answer wherever is closer: the first answer wins. *Hide* / *Answer in terminal* removes the buttons and leaves the prompt to the terminal.
- Start manually: `~/.claude/overlay/venv/bin/python ~/.claude/overlay/overlay.py` (only one instance runs at a time).

## Configuration

The ⚙ panel covers the common options. Everything is in `~/.claude/overlay/config.json`, which the overlay rereads as soon as you save it. Missing keys fall back to the defaults, so you can keep only the ones you change.

```json
{
  "sessions": "top",
  "show": {
    "project": false,
    "elapsed": true,
    "tool": true,
    "five_hour": true,
    "seven_day": false,
    "reset_in": false
  },
  "interactive": { "enabled": true, "timeout": 600 },
  "controls": "click",
  "scale": 1.0,
  "opacity": 0.9,
  "font": "",
  "limit_warn": 70,
  "limit_alert": 90,
  "colors": {
    "accent": "#D97757", "background": "#1C1C1F", "text": "#EDEDED", "dim": "#9E9E9E",
    "waiting": "#FFA840", "done": "#73D985", "offline": "#8C8C8C", "alert": "#FF6B6B"
  },
  "labels": {
    "waiting": "Needs you", "working": "Working", "done": "Your turn",
    "idle": "Ready", "offline": "No session"
  }
}
```

| Key | Meaning |
|---|---|
| `sessions` | `"top"`: one pill for the most urgent session, `+N` for the rest. `"all"`: one pill per session, with its project name. |
| `show.project` | Project folder name in `"top"` mode. |
| `show.elapsed` | Turn duration while working, time since Claude stopped otherwise. |
| `show.tool` | The tool Claude is running. |
| `show.five_hour`, `show.seven_day` | Plan usage of the 5-hour and weekly windows, in percent. Amber from `limit_warn`, red from `limit_alert`. |
| `show.reset_in` | Time until the 5-hour window resets. |
| `interactive.enabled` | Show answer buttons for permission prompts and questions. |
| `interactive.timeout` | Seconds the overlay keeps the buttons for one prompt. |
| `controls` | `"click"` or `"hover"`: what reveals the ⚙ and ✕ buttons. |
| `scale`, `opacity`, `font` | Size multiplier, background opacity (0–1), font family (empty = system font). |
| `colors`, `labels` | Any colour Qt understands (`#RRGGBB`, `#AARRGGBB`, `red`), and the status texts. |

Plan limits come from Claude Code's status line data, so they appear only for claude.ai Pro and Max subscriptions, after the first response in a session, and only from sessions that show a status line (the terminal).

## How it works

`hook.py` runs on Claude Code hook events (`UserPromptSubmit`, `PreToolUse`, `PermissionRequest`, `Notification`, `Stop`, …) and writes one small JSON file per session to `~/.claude/overlay/state/`. `overlay.py` polls these files twice a second and draws the pill with Qt.

On `PermissionRequest` the hook adds the request to the session file and waits. A click in the overlay writes the answer to `~/.claude/overlay/answers/`, and the hook returns it to Claude Code as the permission decision (for a question, as `updatedInput.answers`). If the overlay isn't running, you answer in the terminal, or `interactive.timeout` passes, the hook exits without a decision and nothing changes.

`statusline.py` receives the status line JSON, saves `rate_limits` to `~/.claude/overlay/limits.json`, and passes the JSON to your original status line command (kept in `~/.claude/overlay/statusline.json`).

Some situations have no hook event, so the overlay fills the gaps itself:

- **Esc / rejected tool** — detected from the `[Request interrupted by user]` marker at the end of the session transcript.
- **Permission approved for a long command** — detected by a child process of Claude Code that has run for more than a second.
- **Closed or crashed session** — the state file is dropped once the Claude Code process is gone.

## Notes

- Linux: the window bypasses the window manager (override-redirect), so it stays above fullscreen video. Some Wayland compositors may still behave differently.
- macOS: the overlay hides its Dock icon and asks to stay visible on all Spaces and above fullscreen apps.
- Windows: hooks run through Git Bash, or PowerShell when Git Bash isn't installed; the installer writes the matching command.
- Hook commands print nothing except a permission decision you clicked, and always exit 0, so they can't block or otherwise change Claude Code's behavior.

## Uninstall

```bash
./uninstall.sh               # Windows: py uninstall.py
```

This also puts your original status line back.

## License

MIT. This is a community project and is not affiliated with or endorsed by Anthropic.
