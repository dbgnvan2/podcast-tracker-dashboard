#!/usr/bin/env bash
# Launch the Podcast Tracker dashboard and open it in your browser.
# Usage: ./run.sh           (defaults to port 9091; set PORT to override)
set -e
cd "$(dirname "$0")"

PORT="${PORT:-9091}"
URL="http://localhost:${PORT}"

# If a dashboard is already serving on this port, just open it.
if curl -s -m 2 "${URL}/api/stats" >/dev/null 2>&1; then
    echo "Dashboard already running at ${URL} — opening browser."
    command -v open >/dev/null 2>&1 && open "${URL}" || true
    exit 0
fi

# If the port is taken by something else, find the next free port.
port_busy() { lsof -nP -i ":$1" -sTCP:LISTEN >/dev/null 2>&1; }
if port_busy "$PORT"; then
    base="$PORT"
    for p in $(seq $((base+1)) $((base+10))); do
        if ! port_busy "$p"; then PORT="$p"; break; fi
    done
    URL="http://localhost:${PORT}"
    echo "Port ${base} busy — using ${PORT} instead."
fi

# Ensure the DB schema is up to date (safe/idempotent).
python3 dashboard_server.py --migrate >/dev/null 2>&1 || true

echo "Starting Podcast Tracker dashboard on ${URL}"
PORT="$PORT" python3 dashboard_server.py &
SERVER_PID=$!

# Give it a moment, then open the browser (macOS `open`).
sleep 1.5
command -v open >/dev/null 2>&1 && open "${URL}" || true

# Keep the script attached to the server; Ctrl+C stops it.
trap "kill $SERVER_PID 2>/dev/null" INT TERM
wait $SERVER_PID
