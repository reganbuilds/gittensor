"""
Flask API server — serves bot state to the dashboard.
Owns the bot engine thread (start/stop/scan/reset).
"""
import threading, json, sys
from pathlib import Path
from flask import Flask, jsonify
from flask_cors import CORS

sys.path.insert(0, str(Path(__file__).parent))
import engine

app = Flask(__name__)
CORS(app)

_thread_lock = threading.Lock()
_stop_event  = threading.Event()
_bot_thread  = None

def bot_running():
    return _bot_thread is not None and _bot_thread.is_alive()

def start_bot_thread():
    """Start the engine thread. Returns False if already running."""
    global _bot_thread, _stop_event
    with _thread_lock:
        if bot_running():
            return False
        _stop_event = threading.Event()
        _bot_thread = threading.Thread(target=engine.run_bot, args=(_stop_event,), daemon=True)
        _bot_thread.start()
        return True

def stop_bot_thread(timeout=10):
    """Signal the engine thread to stop and wait for it to exit."""
    with _thread_lock:
        if not bot_running():
            return True
        _stop_event.set()
        _bot_thread.join(timeout)
        return not _bot_thread.is_alive()

def _get_state():
    try:
        if engine.DATA_FILE.exists():
            with open(engine.DATA_FILE) as f:
                return json.load(f)
    except Exception:
        pass
    return engine.load_state()

@app.route("/api/state")
def get_state():
    s = _get_state()
    settled = s.get("settled", [])
    wins = sum(1 for t in settled if t.get("status") == "won")
    n = len(settled)
    scans = s.get("scans", [])[-20:]
    recent_signals = []
    for sc in scans[-3:]:
        recent_signals.extend(sc.get("signals", []))
    # dedupe by ticker, newest first
    seen = set()
    deduped = []
    for sig in reversed(recent_signals):
        if sig["ticker"] not in seen:
            seen.add(sig["ticker"])
            deduped.append(sig)
    sources = {sig.get("source", "simulation") for sig in deduped[:15]}
    data_mode = ("live" if sources == {"live"} else
                 "mixed" if "live" in sources else "simulation") if sources else None
    return jsonify({
        "bankroll":     s.get("bankroll", engine.START_BANKROLL),
        "total_pnl":    s.get("total_pnl", 0.0),
        "edges_found":  s.get("edges_found", 0),
        "positions":    s.get("positions", []),
        "settled":      settled[-30:],
        "win_rate":     round(wins/n, 4) if n else None,
        "trade_count":  n,
        "last_scan":    s.get("last_scan"),
        "status":       "running" if bot_running() else "stopped",
        "started_at":   s.get("started_at"),
        "recent_signals": deduped[:15],
        "scan_count":   len(scans),
        "data_mode":    data_mode,
        "pnl_history":  _pnl_history(settled),
    })

def _pnl_history(settled):
    """Cumulative P&L over time from settled trades."""
    cum = 0.0
    out = [{"t": "Start", "pnl": 0.0}]
    for t in settled:
        cum += t.get("pnl", 0) or 0
        out.append({"t": t.get("settled_at","")[:16].replace("T"," "), "pnl": round(cum,2)})
    return out

@app.route("/api/start", methods=["POST"])
def start_bot():
    if start_bot_thread():
        return jsonify({"ok": True, "msg": "started"})
    return jsonify({"ok": False, "msg": "already running"})

@app.route("/api/stop", methods=["POST"])
def stop_bot():
    stopped = stop_bot_thread()
    return jsonify({"ok": stopped, "msg": "stopped" if stopped else "stop timed out"})

@app.route("/api/scan", methods=["POST"])
def scan_now():
    if bot_running():
        engine.SCAN_NOW.set()
        return jsonify({"ok": True, "msg": "scan queued"})
    def one_scan():
        s = engine.load_state()
        engine.scan(s)
        engine.save_state(s)
    threading.Thread(target=one_scan, daemon=True).start()
    return jsonify({"ok": True, "msg": "scan started"})

@app.route("/api/reset", methods=["POST"])
def reset_bot():
    if not stop_bot_thread():
        return jsonify({"ok": False, "msg": "could not stop bot; not resetting"})
    for f in (engine.DATA_FILE, engine.BACKUP_FILE):
        if f.exists():
            f.unlink()
    return jsonify({"ok": True, "msg": "reset"})

@app.route("/api/logs")
def get_logs():
    try:
        lines = engine.LOG_FILE.read_text().splitlines()
        return jsonify({"lines": lines[-80:]})
    except Exception:
        return jsonify({"lines": []})

if __name__ == "__main__":
    start_bot_thread()
    app.run(host="0.0.0.0", port=5000, debug=False)
