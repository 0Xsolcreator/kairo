#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

# Load .env if it exists
if [ -f .env ]; then
    set -o allexport
    # shellcheck source=.env
    source .env
    set +o allexport
fi

LOG=qvac-server.log

# Start the model server, redirecting all output to a log file.
bunx qvac serve openai >"$LOG" 2>&1 &
SERVER_PID=$!

trap 'kill "$SERVER_PID" 2>/dev/null; wait "$SERVER_PID" 2>/dev/null; true' EXIT

# Wait until the server is actually accepting connections (up to 30s).
printf "  \033[90m·\033[0m  \033[2mStarting model server\033[0m  (logs → %s)\n" "$LOG"
for i in $(seq 1 60); do
    if curl -sf http://127.0.0.1:11434/v1/models >/dev/null 2>&1; then
        printf "  \033[91m✓\033[0m  Server ready.\n"
        break
    fi
    if ! kill -0 "$SERVER_PID" 2>/dev/null; then
        printf "  \033[31m✗\033[0m  Server process exited unexpectedly. Check %s for details.\n" "$LOG" >&2
        exit 1
    fi
    sleep 0.5
done

if [ -n "${QVAC_DEBUG:-}" ]; then
    printf "  \033[33m⚠\033[0m  \033[2mQVAC_DEBUG=%s\033[0m — verbose logging enabled for all agent modules\n" "$QVAC_DEBUG"
fi

.venv/bin/python main.py "$@"
