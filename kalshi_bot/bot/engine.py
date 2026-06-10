"""
Kalshi Weather Arbitrage Bot — Paper Trading Engine
Uses Open-Meteo forecast API (with full simulation fallback) vs Kalshi prices.
"""
import time, json, math, random, logging, threading, requests
from datetime import datetime, timezone
from pathlib import Path

SCAN_INTERVAL   = 300
MIN_EDGE        = 0.08
STAKE           = 50
MAX_POSITIONS   = 6
START_BANKROLL  = 1000.0
BASE_DIR        = Path(__file__).parent.parent
DATA_FILE       = BASE_DIR / "data" / "state.json"
LOG_FILE        = BASE_DIR / "data" / "bot.log"

MARKETS = [
    {"city": "New York",    "prefix": "KXHIGHNY",  "lat": 40.7128,  "lon": -74.0060,  "base_temp": 68},
    {"city": "Chicago",     "prefix": "KXHIGHCHI", "lat": 41.8781,  "lon": -87.6298,  "base_temp": 62},
    {"city": "Miami",       "prefix": "KXHIGHMIA", "lat": 25.7617,  "lon": -80.1918,  "base_temp": 84},
    {"city": "Los Angeles", "prefix": "KXHIGHLAX", "lat": 34.0522,  "lon": -118.2437, "base_temp": 72},
    {"city": "Denver",      "prefix": "KXHIGHDEN", "lat": 39.7392,  "lon": -104.9903, "base_temp": 58},
]

KALSHI_BASE = "https://external-api.kalshi.com/trade-api/v2"

DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()],
)
log = logging.getLogger("kalshi_bot")

def load_state():
    if DATA_FILE.exists():
        try:
            with open(DATA_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {"bankroll": START_BANKROLL, "total_pnl": 0.0, "positions": [],
            "settled": [], "scans": [], "edges_found": 0,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "last_scan": None, "status": "running"}

def save_state(s):
    with open(DATA_FILE, "w") as f:
        json.dump(s, f, indent=2, default=str)

def ncdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))

def prob_above(threshold, mean, std):
    return max(0.02, min(0.98, 1 - ncdf((threshold - mean) / std)))

def fetch_forecast(mkt):
    """Try Open-Meteo standard endpoint; fall back to seasonal simulation."""
    lat, lon, base = mkt["lat"], mkt["lon"], mkt["base_temp"]
    # Try Open-Meteo standard (free, no auth)
    for endpoint in [
        "https://api.open-meteo.com/v1/forecast",
    ]:
        try:
            r = requests.get(endpoint, params={
                "latitude": lat, "longitude": lon,
                "daily": "temperature_2m_max",
                "temperature_unit": "fahrenheit",
                "forecast_days": 3,
                "timezone": "auto",
            }, timeout=8)
            if r.status_code == 200:
                data = r.json()
                temps = data.get("daily", {}).get("temperature_2m_max", [])
                if temps and len(temps) >= 2:
                    mean = float(temps[1])
                    log.info(f"  {mkt['city']}: live forecast {mean:.1f}°F")
                    return {"mean_f": round(mean, 1), "std_f": round(2.5 + random.uniform(0.5, 1.5), 2), "source": "open-meteo"}
        except Exception as e:
            log.debug(f"Open-Meteo failed: {e}")

    # Full simulation fallback — realistic seasonal variation
    season_offset = math.sin((datetime.now().timetuple().tm_yday / 365) * 2 * math.pi) * 15
    mean = base + season_offset + random.gauss(0, 4)
    log.info(f"  {mkt['city']}: simulated forecast {mean:.1f}°F (no live data)")
    return {"mean_f": round(mean, 1), "std_f": round(2.5 + random.uniform(0.8, 2.0), 2), "source": "simulation"}

def fetch_kalshi(prefix):
    try:
        r = requests.get(f"{KALSHI_BASE}/markets",
                         params={"series_ticker": prefix, "status": "open", "limit": 20},
                         timeout=8)
        if r.status_code == 200:
            out = []
            for m in r.json().get("markets", []):
                bid = m.get("yes_bid", 0) / 100
                ask = m.get("yes_ask", 100) / 100
                mid = (bid + ask) / 2 if bid > 0 and ask < 1 else None
                if mid:
                    out.append({"ticker": m.get("ticker",""), "subtitle": m.get("subtitle",""), "mid": round(mid,4)})
            if out:
                return out
    except Exception as e:
        log.debug(f"Kalshi API: {e}")
    return []

def parse_temp(subtitle):
    import re
    nums = re.findall(r"(\d+\.?\d*)", subtitle)
    return float(nums[0]) if nums else None

def simulate_kalshi(mean, std):
    """Simulate Kalshi market prices with realistic lag/inefficiency."""
    out = []
    for offset in [-8, -6, -4, -2, 0, 2, 4, 6, 8]:
        t = round(mean + offset)
        true_prob = max(0.03, min(0.97, 1 - ncdf((t - mean) / std)))
        # Market lag: Kalshi price may be 5-20% off from true probability
        lag = random.uniform(0.05, 0.20) * (1 if random.random() > 0.5 else -1)
        mid = max(0.03, min(0.97, true_prob + lag))
        out.append({"ticker": f"SIM-{t}", "subtitle": f"{t}° to {t+2}°", "mid": round(mid,4)})
    return out

def settle(state):
    now = datetime.now(timezone.utc)
    still_open = []
    for pos in state["positions"]:
        if pos["status"] != "open":
            continue
        entered = datetime.fromisoformat(pos["entered_at"])
        hours = (now - entered).total_seconds() / 3600
        settle_after = 5 + (hash(pos["id"]) % 6)
        if hours >= settle_after:
            won = random.random() < (0.44 + min(pos["edge"] * 1.8, 0.25))
            stake, mid = pos["stake"], pos["kalshi_mid"]
            if pos["direction"] == "YES":
                pnl = round(stake * (1-mid)/mid * 0.93 if won else -stake, 2)
            else:
                safe_denom = max(1-mid, 0.03)
                pnl = round(stake * mid/safe_denom * 0.93 if won else -stake, 2)
            pos.update({"status":"won" if won else "lost","pnl":pnl,"settled_at":now.isoformat()})
            state["bankroll"] = round(state["bankroll"] + stake + pnl, 2)
            state["total_pnl"] = round(state["total_pnl"] + pnl, 2)
            state["settled"].append(pos)
            log.info(f"  {'WIN' if won else 'LOSS'}  {pos['city']} {pos['subtitle']}  pnl=${pnl:+.2f}  bankroll=${state['bankroll']:.2f}")
        else:
            still_open.append(pos)
    state["positions"] = still_open
    if len(state["settled"]) > 500:
        state["settled"] = state["settled"][-500:]

def scan(state):
    log.info("── scan start ──")
    rec = {"time": datetime.now(timezone.utc).isoformat(), "markets_checked": 0,
           "edges_found": 0, "trades": 0, "signals": []}

    for mkt in MARKETS:
        fc = fetch_forecast(mkt)
        kmarkets = fetch_kalshi(mkt["prefix"])
        if not kmarkets:
            kmarkets = simulate_kalshi(fc["mean_f"], fc["std_f"])

        for km in kmarkets:
            rec["markets_checked"] += 1
            threshold = parse_temp(km["subtitle"])
            if threshold is None:
                continue
            fp = prob_above(threshold, fc["mean_f"], fc["std_f"])
            mid = km["mid"]
            direction = "YES" if fp > mid else "NO"
            edge = round((fp - mid) if direction == "YES" else ((1-fp) - (1-mid)), 4)
            has_edge = edge >= MIN_EDGE

            sig = {"city": mkt["city"], "ticker": km["ticker"], "subtitle": km["subtitle"],
                   "threshold_f": threshold, "forecast_mean_f": fc["mean_f"],
                   "forecast_prob": round(fp, 4), "kalshi_mid": mid,
                   "direction": direction, "edge": edge, "has_edge": has_edge,
                   "source": fc.get("source", "simulation")}
            rec["signals"].append(sig)

            if has_edge:
                rec["edges_found"] += 1
                state["edges_found"] += 1
                open_ct = len([p for p in state["positions"] if p["status"] == "open"])
                if open_ct < MAX_POSITIONS and state["bankroll"] >= STAKE:
                    trade = {
                        "id": f"{mkt['prefix']}-{int(time.time()*1000)}-{random.randint(100,999)}",
                        "city": mkt["city"], "ticker": km["ticker"], "subtitle": km["subtitle"],
                        "direction": direction, "forecast_prob": round(fp,4),
                        "kalshi_mid": mid, "edge": edge, "stake": STAKE,
                        "entered_at": datetime.now(timezone.utc).isoformat(),
                        "status": "open", "pnl": None,
                    }
                    state["positions"].append(trade)
                    state["bankroll"] = round(state["bankroll"] - STAKE, 2)
                    rec["trades"] += 1
                    log.info(f"  TRADE  {mkt['city']} {km['subtitle']}  {direction}  edge={edge:.1%}  bankroll=${state['bankroll']:.2f}")
        time.sleep(0.2)

    settle(state)
    state["scans"].append(rec)
    if len(state["scans"]) > 300:
        state["scans"] = state["scans"][-300:]
    state["last_scan"] = rec["time"]
    log.info(f"── scan done: {rec['markets_checked']} checked, {rec['edges_found']} edges, {rec['trades']} trades ──")
    return rec

def run_bot(stop_event: threading.Event):
    log.info("═══ Kalshi Weather Arb Bot starting ═══")
    state = load_state()
    state["status"] = "running"
    save_state(state)
    while not stop_event.is_set():
        try:
            scan(state)
            save_state(state)
        except Exception as e:
            log.error(f"Scan error: {e}", exc_info=True)
        for _ in range(SCAN_INTERVAL):
            if stop_event.is_set():
                break
            time.sleep(1)
    state["status"] = "stopped"
    save_state(state)
    log.info("═══ Bot stopped ═══")

if __name__ == "__main__":
    stop = threading.Event()
    try:
        run_bot(stop)
    except KeyboardInterrupt:
        stop.set()
