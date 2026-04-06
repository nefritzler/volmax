#!/usr/bin/env bash
# VOLMAX — Start preview server + djay bridge + ngrok tunnel
# Usage: ./start.sh [--no-bridge] [--debug-bridge]

set -e

PORT=8080
BRIDGE_PORT=8765
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

NO_BRIDGE=0
DEBUG_BRIDGE=0
for arg in "$@"; do
  [[ "$arg" == "--no-bridge"    ]] && NO_BRIDGE=1
  [[ "$arg" == "--debug-bridge" ]] && DEBUG_BRIDGE=1
done

# ── Cleanup on exit ────────────────────────────────────────────
SERVER_PID=""
BRIDGE_PID=""
NGROK_PID=""

cleanup() {
  echo ""
  echo "Shutting down..."
  [[ -n "$SERVER_PID" ]] && kill "$SERVER_PID" 2>/dev/null || true
  [[ -n "$BRIDGE_PID" ]] && kill "$BRIDGE_PID" 2>/dev/null || true
  [[ -n "$NGROK_PID"  ]] && kill "$NGROK_PID"  2>/dev/null || true
  exit 0
}
trap cleanup INT TERM

# ── Check/install Python dependencies ─────────────────────────
echo ""
echo "Checking Python dependencies..."
pip3 install -q -r "$SCRIPT_DIR/requirements.txt" 2>/dev/null && echo "  ✓ Dependencies OK" || \
  echo "  ⚠  pip install failed — bridge may not work"

# ── Kill any existing processes on relevant ports ──────────────
lsof -ti tcp:$PORT        | xargs kill -9 2>/dev/null || true
lsof -ti tcp:$BRIDGE_PORT | xargs kill -9 2>/dev/null || true

# ── Start Python preview server ────────────────────────────────
cd "$SCRIPT_DIR"
python3 server.py &
SERVER_PID=$!

# ── Start djay bridge ──────────────────────────────────────────
if [[ $NO_BRIDGE -eq 0 ]]; then
  BRIDGE_ARGS=""
  [[ $DEBUG_BRIDGE -eq 1 ]] && BRIDGE_ARGS="--debug"
  python3 "$SCRIPT_DIR/djay_bridge.py" $BRIDGE_ARGS > /tmp/djay-bridge.log 2>&1 &
  BRIDGE_PID=$!
  echo "  ✓ djay bridge started (ws://localhost:$BRIDGE_PORT)"
  echo "    Logs: tail -f /tmp/djay-bridge.log"
fi

# ── Start ngrok ────────────────────────────────────────────────
ngrok http $PORT --log=stdout > /tmp/ngrok-volmax.log 2>&1 &
NGROK_PID=$!

# ── Wait for ngrok URL ─────────────────────────────────────────
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
echo "  Preview server:  http://localhost:$PORT"
echo "  djay bridge:     ws://localhost:$BRIDGE_PORT"
if [[ -n "$PUBLIC_URL" ]]; then
  echo "  Public URL:      $PUBLIC_URL"
  echo ""
  echo "  Screens:"
  echo "    $PUBLIC_URL/performance  → Live (main screen)"
  echo "    $PUBLIC_URL/setup        → Fixture Setup"
  echo "    $PUBLIC_URL/zones        → Zones"
  echo "    $PUBLIC_URL/timeline     → Tracks"
else
  echo "  Public URL:  (ngrok not connected)"
fi
echo ""
if [[ $NO_BRIDGE -eq 0 ]]; then
  echo "  djay bridge: open djay Pro to start receiving deck data"
  echo "               tail -f /tmp/djay-bridge.log  to monitor"
fi
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Ctrl+C to stop everything"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

wait $SERVER_PID
