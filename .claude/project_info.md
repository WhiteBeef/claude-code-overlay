# Project info

## Overview

Always-on-top Qt overlay for Claude Code, Kimi Code and Codex. One shared runtime combines sessions from all installed clients.

## Build & config

- Install selected clients with `python3 install.py --clients claude,kimi,codex` or `--clients all`; default is `claude` for backward compatibility.
- Client settings: Claude Code `settings.json`, Kimi Code `config.toml` (`[[hooks]]` entries), Codex `hooks.json`.
- The shared runtime stays in `~/.claude/overlay` for compatibility with existing installations. `CLAUDE_CONFIG_DIR`, `KIMI_CODE_HOME` and `CODEX_HOME` relocate client settings, not the runtime.
- No project test suite. Validate with `py_compile`, isolated installer/hook integration scripts and Qt offscreen startup.

## Decisions & gotchas

- State files are prefixed with the client so identical session IDs cannot overwrite each other.
- Kimi Code `PermissionRequest` is observation-only. Interactive approvals use `PreToolUse` with `default_permission_mode = "auto"`; Reject returns a supported denial, and question answers travel in the denial reason so the model can continue. Native plan review remains terminal-only.
- Kimi Auto mode skips overlay approvals for the rest of one session. The installer restores the previous global permission mode on uninstall. Without the overlay, Kimi's built-in Auto mode runs tools without asking.
- Kimi Code 2.6.2 may skip `Stop` after normal TUI responses. Use the session's `agents/main/wire.jsonl` `turn.ended` as a completion fallback, but ignore it when newer prompt/model/tool records show another active turn.
- Codex permission decisions support `behavior` and `message`, not Claude's `updatedInput` or `updatedPermissions`. Users must trust installed hooks through `/hooks`.
- Codex `SessionEnd` and `Interrupt` hook timeouts must not exceed 3 seconds.
- Only Claude Code transcripts use the interruption-marker parser; Kimi Code and Codex provide native `Interrupt` events.
