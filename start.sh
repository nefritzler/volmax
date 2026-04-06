#!/usr/bin/env bash
# VOLMAX — Start server + ngrok tunnel
# Usage: ./start.sh

set -e

PORT=8080
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Cleanup on exit ────────────────────────────────────────────
cleanup() {
  echo ""
  echo "Shutting down..."
  [[ -n "$SERVER_PID" ]] && kill "$SERVER_PID" 2>/dev/null
  [[ -n "$NGROK_PID"  ]] && kill "$NGROK_PID"  2>/dev/null
  exit 0
}
trap cleanup INT TERM

# ── Kill any existing processes on port ────────────────────────
lsof -ti tcp:$PORT | xargs kill -9 2>/dev/null || true

# ── Start Python server in background ─────────────────────────
cd "$SCRIPT_DIR"
python3 server.py &
SERVER_PID=$!

# ── Start ngrok in background ──────────────────────────────────
ngrok http $PORT --log=stdout > /tmp/ngrok-volmax.log 2>&1 &
NGROK_PID=$!

# ── Wait for ngrok to report a public URL ─────────────────────
echo ""
echo "Starting VOLMAX..."
PUBLIC_URL=""
for i in $(seq 1 20); do
  sleep 0.5
  PUBLIC_URL=$(curl -s http://localhost:4040/api/tunnels 2>/dev/null \
    | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    tunnels = d.get('tunnels', [])
    https = [t for t in tunnels if t.get('proto') == 'https']
    print(https[0]['public_url'] if https else '')
except:
    print('')
" 2>/dev/null)
  [[ -n "$PUBLIC_URL" ]] && break
done

# ── Print result ───────────────────────────────────────────────
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  VOLMAX Design Preview"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "  Local:   http://localhost:$PORT"
if [[ -n "$PUBLIC_URL" ]]; then
  echo "  Public:  $PUBLIC_URL"
  echo ""
  echo "  Screens:"
  echo "    $PUBLIC_URL/           → Overview"
  echo "    $PUBLIC_URL/performance → Live (main screen)"
  echo "    $PUBLIC_URL/setup       → Fixture Setup"
  echo "    $PUBLIC_URL/zones       → Zones"
  echo "    $PUBLIC_URL/timeline    → Tracks"
else
  echo "  Public:  (ngrok tunnel not established — check ngrok auth)"
  echo "           Run: ngrok config add-authtoken <your-token>"
fi
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Press Ctrl+C to stop both server and tunnel"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# ── Keep running until Ctrl+C ─────────────────────────────────
wait $SERVER_PID
