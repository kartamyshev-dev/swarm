#!/usr/bin/env bash
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"
exec python3 "$(dirname "$0")/accept.py" "$@"
