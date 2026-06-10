"""
Flask API server — serves bot state to the dashboard.
Starts/stops the bot engine in a background thread.
"""
import threading, json, sys
from pathlib import Path
from flask import Flask, jsonify, request
from flask_cors import CORS

sys.path.insert(0, str(Path(__file__).parent))
import engine

app = Flask(__name__)
CORS(app)

_stop_event = threading.Event()
_bot_thread  = None

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
    # Last 20 scans summary
    scans = s.get("scans", [])[-20:]
    recent_signals = []
    for sc in scans[-3:]:
        recent_signals.extend(sc.get("signals", []))
    # dedupe by ticker
    seen = set()
    deduped = []
    for sig in reversed(recent_signals):
        if sig["ticker"] not in seen:
            seen.add(sig["ticker"])
            deduped.append(sig)
    return jsonify({
        "bankroll":     s.get("bankroll", engine.START_BANKROLL),
        "total_pnl":    s.get("total_pnl", 0.0),
        "edges_found":  s.get("edges_found", 0),
        "positions":    s.get("positions", []),
        "settled":      settled[-30:],
        "win_rate":     round(wins/n, 4) if n else None,
        "trade_count":  n,
        "last_scan":    s.get("last_scan"),
        "status":       s.get("status", "stopped"),
        "started_at":   s.get("started_at"),
        "recent_signals": deduped[:15],
        "scan_count":   len(scans),
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
    global _bot_thread, _stop_event
    if _bot_thread and _bot_thread.is_alive():
        return jsonify({"ok": False, "msg": "already running"})
    _stop_event = threading.Event()
    _bot_thread = threading.Thread(target=engine.run_bot, args=(_stop_event,), daemon=True)
    _bot_thread.start()
    return jsonify({"ok": True, "msg": "started"})

@app.route("/api/stop", methods=["POST"])
def stop_bot():
    global _stop_event
    _stop_event.set()
    return jsonify({"ok": True, "msg": "stopping"})

@app.route("/api/reset", methods=["POST"])
def reset_bot():
    global _stop_event
    _stop_event.set()
    if engine.DATA_FILE.exists():
        engine.DATA_FILE.unlink()
    return jsonify({"ok": True, "msg": "reset"})

@app.route("/api/logs")
def get_logs():
    try:
        lines = engine.LOG_FILE.read_text().splitlines()
        return jsonify({"lines": lines[-80:]})
    except Exception:
        return jsonify({"lines": []})

if __name__ == "__main__":
    # Auto-start bot on server launch
    _stop_event = threading.Event()
    _bot_thread = threading.Thread(target=engine.run_bot, args=(_stop_event,), daemon=True)
    _bot_thread.start()
    app.run(host="0.0.0.0", port=5000, debug=False)
