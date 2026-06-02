#!/usr/bin/env bash
# Launch the Podcast Tracker dashboard and open it in your browser.
# Usage: ./run.sh        (defaults to port 9091)
set -e
cd "$(dirname "$0")"

PORT="${PORT:-9091}"

# Ensure the DB schema is up to date (safe/idempotent).
python3 dashboard_server.py --migrate >/dev/null 2>&1 || true

echo "Starting Podcast Tracker dashboard on http://localhost:${PORT}"
PORT="$PORT" python3 dashboard_server.py &
SERVER_PID=$!

# Give it a moment, then open the browser (macOS `open`).
sleep 1
command -v open >/dev/null 2>&1 && open "http://localhost:${PORT}" || true

# Keep the script attached to the server; Ctrl+C stops it.
trap "kill $SERVER_PID 2>/dev/null" INT TERM
wait $SERVER_PID
