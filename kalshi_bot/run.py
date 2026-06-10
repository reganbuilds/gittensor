#!/usr/bin/env python3
"""
Entry point — starts Flask server (which auto-starts the bot engine).
Usage: python run.py
Dashboard: open dashboard/index.html in your browser
API:       http://localhost:5000/api/state
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent / "bot"))
from server import app, _stop_event, _bot_thread
import threading
import engine

if __name__ == "__main__":
    print("═══ Kalshi Weather Arb Bot ═══")
    print(f"  Data: {engine.DATA_FILE}")
    print(f"  Logs: {engine.LOG_FILE}")
    print(f"  API:  http://localhost:5000")
    print(f"  Open dashboard/index.html in your browser")
    print()

    stop = threading.Event()
    bot = threading.Thread(target=engine.run_bot, args=(stop,), daemon=True)
    bot.start()

    try:
        app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)
    except KeyboardInterrupt:
        print("\nStopping...")
        stop.set()
