#!/usr/bin/env python3
"""
Entry point — starts Flask server and the bot engine.
Usage: python run.py [--port 5001]
Dashboard: open dashboard/index.html in your browser
API:       http://localhost:5000/api/state
"""
import argparse, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent / "bot"))
import engine
import server

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Kalshi Weather Arb Bot (paper trading)")
    parser.add_argument("--port", type=int, default=5000, help="API server port (default 5000)")
    args = parser.parse_args()

    print("═══ Kalshi Weather Arb Bot ═══")
    print(f"  Data: {engine.DATA_FILE}")
    print(f"  Logs: {engine.LOG_FILE}")
    print(f"  API:  http://localhost:{args.port}")
    print(f"  Open dashboard/index.html in your browser")
    if args.port != 5000:
        print(f"  NOTE: update the API URL in dashboard/index.html to localhost:{args.port}")
    print()

    server.start_bot_thread()
    try:
        server.app.run(host="0.0.0.0", port=args.port, debug=False, use_reloader=False)
    except KeyboardInterrupt:
        print("\nStopping...")
        server.stop_bot_thread()
