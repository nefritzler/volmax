#!/usr/bin/env python3
"""
djay_bridge.py — djay Pro → WebSocket bridge

Reads deck A/B volume faders from djay Pro via macOS Accessibility API.
Broadcasts JSON state over ws://localhost:8765 at ~10Hz.

Broadcast format:
  {"status":"ok","deck_a":0.85,"deck_b":0.15,"crossfader":0.3,"timestamp":1234.5}

Status values:
  "ok"               djay found, faders read successfully
  "djay_not_running" djay not open (bridge retries every 2s)
  "no_ax_permission" Accessibility permission not granted
  "faders_not_found" djay open but faders not identified via AX
  "demo"             pyobjc not installed, animated demo data

Usage:
  python3 djay_bridge.py           # normal
  python3 djay_bridge.py --debug   # print all discovered AX sliders
"""

import asyncio
import json
import sys
import time
import math
import subprocess
import threading
import argparse

WS_PORT = 8765
POLL_INTERVAL = 0.1   # 100ms / 10Hz

# ── Accessibility API ──────────────────────────────────────────
try:
    from ApplicationServices import (
        AXUIElementCreateApplication,
        AXUIElementCopyAttributeValue,
        kAXErrorSuccess,
        AXIsProcessTrusted,
    )
    AX_AVAILABLE = True
except ImportError:
    AX_AVAILABLE = False

# ── WebSocket library ──────────────────────────────────────────
try:
    from websockets.server import serve as ws_serve
    import websockets
    WS_AVAILABLE = True
except ImportError:
    WS_AVAILABLE = False

# ── Shared state (written by poll thread, read by async loop) ──
_state = {
    "status": "starting",
    "deck_a": 1.0,
    "deck_b": 0.0,
    "crossfader": 0.0,
    "timestamp": time.time(),
}
_state_lock = threading.Lock()
_clients: set = set()


# ── Accessibility helpers ──────────────────────────────────────

def _ax_get(element, attr):
    """Read one AX attribute; return None on any error."""
    try:
        err, val = AXUIElementCopyAttributeValue(element, attr, None)
        return val if err == kAXErrorSuccess else None
    except Exception:
        return None


def _walk_sliders(element, depth=0, max_depth=12, out=None):
    """Recursively collect all AXSlider elements with their metadata."""
    if out is None:
        out = []
    if depth > max_depth:
        return out
    try:
        role = _ax_get(element, 'AXRole')
        if role == 'AXSlider':
            out.append({
                'el':    element,
                'val':   _ax_get(element, 'AXValue'),
                'desc':  (_ax_get(element, 'AXDescription') or '').lower(),
                'title': (_ax_get(element, 'AXTitle') or '').lower(),
                'pos':   _ax_get(element, 'AXPosition'),
                'size':  _ax_get(element, 'AXSize'),
            })
        for child in (_ax_get(element, 'AXChildren') or []):
            _walk_sliders(child, depth + 1, max_depth, out)
    except Exception:
        pass
    return out


def _identify_faders(sliders, debug=False):
    """
    Match sliders to deck_a, deck_b, crossfader using keyword matching
    then positional heuristics as a fallback.
    Returns dict: {'deck_a': slider|None, 'deck_b': slider|None, 'crossfader': slider|None}
    """
    da = db = cf = None

    a_kw  = ['volume a', 'deck a', 'channel a', 'player 1', 'left volume', 'vol a', 'deck1']
    b_kw  = ['volume b', 'deck b', 'channel b', 'player 2', 'right volume', 'vol b', 'deck2']
    cf_kw = ['crossfader', 'cross fader', 'x-fader', 'xfader', 'cross fade']

    for s in sliders:
        text = s['desc'] + ' ' + s['title']
        if debug:
            print(f"    AXSlider  desc={s['desc']!r:30s}  title={s['title']!r:20s}  "
                  f"val={s['val']}  pos={s['pos']}")
        if da is None and any(k in text for k in a_kw):
            da = s
        elif db is None and any(k in text for k in b_kw):
            db = s
        elif cf is None and any(k in text for k in cf_kw):
            cf = s

    # Positional fallback: leftmost = deck A, rightmost = deck B,
    # middle of those = crossfader candidate.
    if da is None or db is None:
        positioned = [s for s in sliders if s['pos'] is not None]
        by_x = sorted(positioned, key=lambda s: s['pos'].x)
        if len(by_x) >= 2:
            if da is None:
                da = by_x[0]
            if db is None:
                db = by_x[-1]
        if cf is None and len(by_x) >= 3:
            cf = by_x[len(by_x) // 2]

    return {'deck_a': da, 'deck_b': db, 'crossfader': cf}


def _normalize(val):
    """Normalize an AX value to 0.0–1.0; handles 0-100 scale."""
    if val is None:
        return None
    try:
        f = float(val)
        return max(0.0, min(1.0, f / 100.0 if f > 1.0 else f))
    except (TypeError, ValueError):
        return None


def _find_djay_pid():
    """Return djay Pro's PID or None if not running."""
    for name in ['djay Pro', 'djay']:
        try:
            raw = subprocess.check_output(
                ['pgrep', '-x', name], text=True
            ).strip()
            if raw:
                return int(raw.split('\n')[0])
        except subprocess.CalledProcessError:
            pass
    return None


def _set_state(**kwargs):
    with _state_lock:
        _state.update(kwargs)
        _state['timestamp'] = time.time()


# ── Poll thread ────────────────────────────────────────────────

def _poll_loop(debug=False):
    """Runs in a background thread. Continuously reads djay Pro faders."""

    if not AX_AVAILABLE:
        print("[bridge] pyobjc not available — running in animated demo mode")
        print("[bridge] Install with:  pip3 install pyobjc-framework-ApplicationServices pyobjc-framework-Cocoa")
        _demo_loop()
        return

    # Accessibility permission check (non-blocking — warn and continue)
    if not AXIsProcessTrusted():
        print("[bridge] ⚠️  Accessibility permission required.")
        print("[bridge]    System Settings → Privacy & Security → Accessibility")
        print("[bridge]    Enable access for Terminal (or whichever app runs this script),")
        print("[bridge]    then restart djay_bridge.py.")
        _set_state(status='no_ax_permission')
        # Poll until granted
        while not AXIsProcessTrusted():
            time.sleep(3)
        print("[bridge] Accessibility permission granted — continuing.")

    faders   = {'deck_a': None, 'deck_b': None, 'crossfader': None}
    ax_app   = None
    last_pid = None
    discovery_attempts = 0

    while True:
        try:
            pid = _find_djay_pid()

            # djay not running
            if pid is None:
                if _state['status'] != 'djay_not_running':
                    print("[bridge] djay Pro not running — waiting...")
                    ax_app   = None
                    last_pid = None
                    faders   = {k: None for k in faders}
                    discovery_attempts = 0
                _set_state(status='djay_not_running')
                time.sleep(2)
                continue

            # djay (re)launched
            if pid != last_pid:
                print(f"[bridge] djay Pro found (PID {pid})")
                ax_app   = AXUIElementCreateApplication(pid)
                last_pid = pid
                faders   = {k: None for k in faders}
                discovery_attempts = 0

            # Discover faders if any are missing (retry up to 15 times)
            if not all(faders.values()) and discovery_attempts < 15:
                sliders = _walk_sliders(ax_app)
                if debug:
                    print(f"[bridge] Discovered {len(sliders)} AX sliders:")
                found = _identify_faders(sliders, debug=debug)
                for k, v in found.items():
                    if v is not None and faders[k] is None:
                        faders[k] = v
                matched = [k for k, v in faders.items() if v is not None]
                if matched:
                    print(f"[bridge] Matched faders: {matched}")
                else:
                    discovery_attempts += 1
                    if discovery_attempts == 15:
                        print("[bridge] ⚠️  Could not identify faders via Accessibility API.")
                        print("[bridge]    Make sure djay's window is open and visible.")
                        print("[bridge]    Run with --debug to inspect all discovered sliders.")

            if not any(faders.values()):
                _set_state(status='faders_not_found', deck_a=1.0, deck_b=0.0, crossfader=0.0)
                time.sleep(1)
                continue

            # Read current values
            def _read(key):
                s = faders.get(key)
                return _normalize(_ax_get(s['el'], 'AXValue')) if s else None

            da = _read('deck_a')
            db = _read('deck_b')
            cf = _read('crossfader')

            # Derive missing values from what we do have
            if da is None and db is None:
                da = 1.0 - (cf or 0.5)
                db = (cf or 0.5)
            da = da if da is not None else 1.0
            db = db if db is not None else 1.0
            if cf is None:
                total = da + db
                cf = (db / total) if total > 0 else 0.5

            _set_state(
                status='ok',
                deck_a=round(da, 3),
                deck_b=round(db, 3),
                crossfader=round(cf, 3),
            )

        except Exception as e:
            print(f"[bridge] Poll error: {e}")
            _set_state(status='error')

        time.sleep(POLL_INTERVAL)


def _demo_loop():
    """Animate a slow crossfade for demo/dev when pyobjc isn't available."""
    t = 0.0
    while True:
        cf = (math.sin(t * 0.25) + 1) / 2   # 0→1 over ~12s
        da = max(0.0, min(1.0, 1.0 - cf * 1.1))
        db = max(0.0, min(1.0, cf * 1.1))
        _set_state(status='demo', deck_a=round(da, 3),
                   deck_b=round(db, 3), crossfader=round(cf, 3))
        t += 1
        time.sleep(POLL_INTERVAL)


# ── WebSocket server ───────────────────────────────────────────

async def _ws_handler(websocket):
    _clients.add(websocket)
    try:
        # Send current state immediately on connect
        with _state_lock:
            await websocket.send(json.dumps(_state))
        async for _ in websocket:
            pass   # we don't expect inbound messages
    except Exception:
        pass
    finally:
        _clients.discard(websocket)


async def _broadcast_loop():
    """Push state to all connected clients whenever it changes."""
    last_sent = {}
    while True:
        with _state_lock:
            current = dict(_state)
        if current != last_sent and _clients:
            msg = json.dumps(current)
            dead = set()
            for ws in list(_clients):
                try:
                    await ws.send(msg)
                except Exception:
                    dead.add(ws)
            _clients -= dead
            last_sent = current
        await asyncio.sleep(POLL_INTERVAL)


async def _main(debug=False):
    poll_thread = threading.Thread(target=_poll_loop, args=(debug,), daemon=True)
    poll_thread.start()

    print(f"[bridge] WebSocket server → ws://localhost:{WS_PORT}")
    async with ws_serve(_ws_handler, 'localhost', WS_PORT):
        await _broadcast_loop()


if __name__ == '__main__':
    if not WS_AVAILABLE:
        print("Error: 'websockets' not installed.")
        print("       Run:  pip3 install websockets")
        sys.exit(1)

    parser = argparse.ArgumentParser(description='djay Pro → WebSocket bridge')
    parser.add_argument('--debug', action='store_true',
                        help='Print every discovered AX slider on discovery')
    args = parser.parse_args()

    try:
        asyncio.run(_main(debug=args.debug))
    except KeyboardInterrupt:
        print('\n[bridge] Stopped.')
