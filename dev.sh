#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

bunx qvac serve openai &
SERVER_PID=$!

# Kill the server when the script exits (ctrl+c, normal exit, or error).
trap 'kill "$SERVER_PID" 2>/dev/null; wait "$SERVER_PID" 2>/dev/null; true' EXIT

# Give the server a moment to bind its port before the CLI tries to connect.
sleep 1

.venv/bin/python main.py "$@"
