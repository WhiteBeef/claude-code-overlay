#!/usr/bin/env bash
# Linux / macOS entry point; the real installer is install.py (on Windows: py install.py).
exec python3 "$(dirname "${BASH_SOURCE[0]}")/install.py" "$@"
