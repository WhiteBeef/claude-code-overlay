#!/usr/bin/env bash
# Linux / macOS entry point; the real uninstaller is uninstall.py (on Windows: py uninstall.py).
exec python3 "$(dirname "${BASH_SOURCE[0]}")/uninstall.py" "$@"
