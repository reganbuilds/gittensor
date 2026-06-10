"""
Kalshi Weather Arbitrage Bot — Paper Trading Engine

Compares Open-Meteo daily-high forecasts against Kalshi temperature market
prices and paper-trades any gap above MIN_EDGE. Positions settle against the
actual observed high temperature once the market's day has passed. When live
APIs are unreachable the bot falls back to simulation, and every signal,
trade, and settlement is tagged with its data source so simulated results
are never mistaken for live ones.
"""
import time, json, math, random, logging, threading, os, re, requests
from datetime import datetime, timedelta, timezone, date
from pathlib import Path

SCAN_INTERVAL   = 300
MIN_EDGE        = 0.08
STAKE           = 50        # target outlay per trade, dollars
MAX_POSITIONS   = 6
MIN_PRICE       = 0.10      # skip contracts priced below this — thin books, huge sizing
START_BANKROLL  = 1000.0
FEE_RATE        = 0.07      # Kalshi trading fee ≈ 0.07 · price · (1 − price) per contract
FORECAST_STD_F  = 3.0       # assumed 1-day-ahead high-temp forecast error, °F
SETTLE_BUFFER_H = 9         # UTC hours past midnight before a US local day is fully over
BASE_DIR        = Path(__file__).parent.parent
DATA_FILE       = BASE_DIR / "data" / "state.json"
BACKUP_FILE     = BASE_DIR / "data" / "state.json.bak"
LOG_FILE        = BASE_DIR / "data" / "bot.log"

MARKETS = [
    {"city": "New York",    "prefix": "KXHIGHNY",  "lat": 40.7128,  "lon": -74.0060,  "base_temp": 68},
    {"city": "Chicago",     "prefix": "KXHIGHCHI", "lat": 41.8781,  "lon": -87.6298,  "base_temp": 62},
    {"city": "Miami",       "prefix": "KXHIGHMIA", "lat": 25.7617,  "lon": -80.1918,  "base_temp": 84},
    {"city": "Los Angeles", "prefix": "KXHIGHLAX", "lat": 34.0522,  "lon": -118.2437, "base_temp": 72},
    {"city": "Denver",      "prefix": "KXHIGHDEN", "lat": 39.7392,  "lon": -104.9903, "base_temp": 58},
]

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
OPEN_METEO  = "https://api.open-meteo.com/v1/forecast"

# Set by the API server to trigger a scan without waiting out SCAN_INTERVAL.
SCAN_NOW = threading.Event()

_state_lock = threading.Lock()

DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()],
)
log = logging.getLogger("kalshi_bot")

# ── State persistence ─────────────────────────────────────────────────────────

def _fresh_state():
    return {"bankroll": START_BANKROLL, "total_pnl": 0.0, "positions": [],
            "settled": [], "scans": [], "edges_found": 0,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "last_scan": None, "status": "running"}

def load_state():
    for path in (DATA_FILE, BACKUP_FILE):
        if path.exists():
            try:
                with open(path) as f:
                    return json.load(f)
            except Exception as e:
                log.error(f"Could not read {path.name}: {e}")
    return _fresh_state()

def save_state(s):
    """Atomic write: temp file + rename, keeping the previous copy as backup."""
    with _state_lock:
        tmp = DATA_FILE.with_suffix(".json.tmp")
        with open(tmp, "w") as f:
            json.dump(s, f, indent=2, default=str)
        if DATA_FILE.exists():
            os.replace(DATA_FILE, BACKUP_FILE)
        os.replace(tmp, DATA_FILE)

# ── Probability ───────────────────────────────────────────────────────────────

def ncdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))

def prob_in_range(low, high, mean, std):
    """P(low <= T <= high) under N(mean, std). Either bound may be None (open)."""
    p_hi = ncdf((high - mean) / std) if high is not None else 1.0
    p_lo = ncdf((low - mean) / std) if low is not None else 0.0
    return max(0.02, min(0.98, p_hi - p_lo))

def parse_range(subtitle):
    """Parse a market subtitle into (low, high) °F bounds, None = unbounded.
    Handles '67° to 69°', '69° or above', '53° or below', '>69°', '<53°'."""
    s = subtitle.replace("°", "").replace("F", "").strip()
    m = re.search(r"(-?\d+\.?\d*)\s*(?:to|-)\s*(-?\d+\.?\d*)", s)
    if m:
        return float(m.group(1)), float(m.group(2))
    m = re.search(r"(-?\d+\.?\d*)\s*or\s*(?:above|higher)", s, re.I)
    if m:
        return float(m.group(1)), None
    m = re.search(r">\s*(-?\d+\.?\d*)", s)
    if m:
        return float(m.group(1)), None
    m = re.search(r"(-?\d+\.?\d*)\s*or\s*(?:below|lower)", s, re.I)
    if m:
        return None, float(m.group(1))
    m = re.search(r"<\s*(-?\d+\.?\d*)", s)
    if m:
        return None, float(m.group(1))
    return None

def temp_in_range(t, low, high):
    if low is not None and t < low:
        return False
    if high is not None and t > high:
        return False
    return True

# ── Data fetchers ─────────────────────────────────────────────────────────────

def fetch_forecast(mkt):
    """Tomorrow's forecast high. Returns mean, std, target date, and source."""
    try:
        r = requests.get(OPEN_METEO, params={
            "latitude": mkt["lat"], "longitude": mkt["lon"],
            "daily": "temperature_2m_max",
            "temperature_unit": "fahrenheit",
            "forecast_days": 2,
            "timezone": "auto",
        }, timeout=8)
        if r.status_code == 200:
            daily = r.json().get("daily", {})
            temps = daily.get("temperature_2m_max") or []
            days  = daily.get("time") or []
            if len(temps) >= 2 and len(days) >= 2 and temps[1] is not None:
                mean = float(temps[1])
                log.info(f"  {mkt['city']}: live forecast {mean:.1f}°F for {days[1]}")
                return {"mean_f": round(mean, 1), "std_f": FORECAST_STD_F,
                        "date": days[1], "source": "open-meteo"}
        else:
            log.warning(f"  {mkt['city']}: forecast HTTP {r.status_code}, using simulation")
    except requests.RequestException as e:
        log.warning(f"  {mkt['city']}: forecast unreachable ({e.__class__.__name__}), using simulation")

    season_offset = math.sin((datetime.now().timetuple().tm_yday / 365) * 2 * math.pi) * 15
    mean = mkt["base_temp"] + season_offset + random.gauss(0, 4)
    target = (datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat()
    log.info(f"  {mkt['city']}: simulated forecast {mean:.1f}°F for {target}")
    return {"mean_f": round(mean, 1), "std_f": FORECAST_STD_F,
            "date": target, "source": "simulation"}

def fetch_actual_high(lat, lon, target_date):
    """Observed high temperature for a recent local date, or None if unavailable."""
    try:
        r = requests.get(OPEN_METEO, params={
            "latitude": lat, "longitude": lon,
            "daily": "temperature_2m_max",
            "temperature_unit": "fahrenheit",
            "past_days": 7, "forecast_days": 1,
            "timezone": "auto",
        }, timeout=8)
        if r.status_code == 200:
            daily = r.json().get("daily", {})
            for day, t in zip(daily.get("time") or [], daily.get("temperature_2m_max") or []):
                if day == target_date and t is not None:
                    return float(t)
    except requests.RequestException as e:
        log.warning(f"Actuals unreachable ({e.__class__.__name__}) for {target_date}")
    return None

def _date_token(target_date):
    """Kalshi daily tickers embed the event date as e.g. 26JUN11."""
    d = date.fromisoformat(target_date)
    return d.strftime("%y%b%d").upper()

def fetch_kalshi(prefix, target_date):
    """Open Kalshi markets for this series settling on target_date."""
    try:
        r = requests.get(f"{KALSHI_BASE}/markets",
                         params={"series_ticker": prefix, "status": "open", "limit": 100},
                         timeout=8)
        if r.status_code != 200:
            log.warning(f"  Kalshi HTTP {r.status_code} for {prefix}, using simulation")
            return []
        out = []
        for m in r.json().get("markets", []):
            bid = m.get("yes_bid", 0) / 100
            ask = m.get("yes_ask", 100) / 100
            mid = (bid + ask) / 2 if bid > 0 and ask < 1 else None
            if mid:
                out.append({"ticker": m.get("ticker", ""), "subtitle": m.get("subtitle", ""),
                            "mid": round(mid, 4)})
        token = _date_token(target_date)
        matching = [m for m in out if token in m["ticker"].upper()]
        if matching:
            return matching
        if out:
            log.warning(f"  {prefix}: {len(out)} open markets but none match date {target_date} "
                        f"(token {token}) — check ticker format; using simulation")
        return []
    except requests.RequestException as e:
        log.warning(f"  Kalshi unreachable ({e.__class__.__name__}) for {prefix}, using simulation")
        return []

def simulate_kalshi(mean, std):
    """Simulated 2°F bin markets priced at true probability ± random mispricing."""
    out = []
    for offset in [-8, -6, -4, -2, 0, 2, 4, 6, 8]:
        lo = round(mean + offset)
        hi = lo + 2
        true_prob = prob_in_range(lo, hi, mean, std)
        lag = random.uniform(0.05, 0.20) * (1 if random.random() > 0.5 else -1)
        mid = max(0.03, min(0.97, true_prob + lag))
        out.append({"ticker": f"SIM-{lo}", "subtitle": f"{lo}° to {hi}°", "mid": round(mid, 4)})
    return out

# ── Settlement ────────────────────────────────────────────────────────────────

def settle(state):
    """Settle positions whose market day has fully passed, against observed temps.
    Falls back to drawing an outcome from the forecast distribution (tagged
    'simulation') when observed data is unavailable."""
    now = datetime.now(timezone.utc)
    actual_cache = {}
    still_open = []
    for pos in state["positions"]:
        if pos["status"] != "open":
            continue
        target = date.fromisoformat(pos["target_date"])
        ready_at = datetime(target.year, target.month, target.day, tzinfo=timezone.utc) \
                   + timedelta(days=1, hours=SETTLE_BUFFER_H)
        if now < ready_at:
            still_open.append(pos)
            continue

        key = (pos["lat"], pos["lon"], pos["target_date"])
        if key not in actual_cache:
            actual_cache[key] = fetch_actual_high(*key)
        actual = actual_cache[key]

        if actual is not None:
            settle_source = "open-meteo"
        else:
            actual = random.gauss(pos["forecast_mean_f"], pos["forecast_std_f"])
            settle_source = "simulation"

        yes_won = temp_in_range(actual, pos["range_low"], pos["range_high"])
        won = yes_won if pos["direction"] == "YES" else not yes_won
        payout = pos["contracts"] if won else 0
        pnl = round(payout - pos["cost"] - pos["fee"], 2)
        pos.update({"status": "won" if won else "lost", "pnl": pnl,
                    "settled_at": now.isoformat(), "settle_source": settle_source,
                    "actual_high_f": round(actual, 1)})
        state["bankroll"] = round(state["bankroll"] + payout, 2)
        state["total_pnl"] = round(state["total_pnl"] + pnl, 2)
        state["settled"].append(pos)
        log.info(f"  {'WIN' if won else 'LOSS'}  {pos['city']} {pos['subtitle']}  "
                 f"actual={actual:.1f}°F ({settle_source})  pnl=${pnl:+.2f}  "
                 f"bankroll=${state['bankroll']:.2f}")
    state["positions"] = still_open
    if len(state["settled"]) > 500:
        state["settled"] = state["settled"][-500:]

# ── Scanning / trading ────────────────────────────────────────────────────────

def scan(state):
    log.info("── scan start ──")
    rec = {"time": datetime.now(timezone.utc).isoformat(), "markets_checked": 0,
           "edges_found": 0, "trades": 0, "signals": []}

    for mkt in MARKETS:
        fc = fetch_forecast(mkt)
        kmarkets = fetch_kalshi(mkt["prefix"], fc["date"])
        live_markets = bool(kmarkets)
        if not kmarkets:
            kmarkets = simulate_kalshi(fc["mean_f"], fc["std_f"])
        source = "live" if (fc["source"] == "open-meteo" and live_markets) else "simulation"

        open_tickers = {p["ticker"] for p in state["positions"] if p["status"] == "open"}
        for km in kmarkets:
            rec["markets_checked"] += 1
            rng = parse_range(km["subtitle"])
            if rng is None:
                continue
            low, high = rng
            fp = prob_in_range(low, high, fc["mean_f"], fc["std_f"])
            mid = km["mid"]
            direction = "YES" if fp > mid else "NO"
            edge = round((fp - mid) if direction == "YES" else (mid - fp), 4)
            has_edge = edge >= MIN_EDGE

            sig = {"city": mkt["city"], "ticker": km["ticker"], "subtitle": km["subtitle"],
                   "range_low": low, "range_high": high, "forecast_mean_f": fc["mean_f"],
                   "forecast_prob": round(fp, 4), "kalshi_mid": mid,
                   "direction": direction, "edge": edge, "has_edge": has_edge,
                   "source": source}
            rec["signals"].append(sig)

            if not has_edge:
                continue
            rec["edges_found"] += 1
            state["edges_found"] += 1
            if km["ticker"] in open_tickers:
                continue  # already holding this market
            open_ct = len([p for p in state["positions"] if p["status"] == "open"])
            if open_ct >= MAX_POSITIONS:
                continue

            price = mid if direction == "YES" else round(1 - mid, 4)
            if price < MIN_PRICE:
                continue
            contracts = max(1, int(STAKE // max(price, 0.01)))
            cost = round(contracts * price, 2)
            fee = round(FEE_RATE * contracts * mid * (1 - mid), 2)
            if state["bankroll"] < cost + fee:
                continue
            trade = {
                "id": f"{mkt['prefix']}-{int(time.time()*1000)}-{random.randint(100,999)}",
                "city": mkt["city"], "ticker": km["ticker"], "subtitle": km["subtitle"],
                "direction": direction, "range_low": low, "range_high": high,
                "target_date": fc["date"], "lat": mkt["lat"], "lon": mkt["lon"],
                "forecast_prob": round(fp, 4), "forecast_mean_f": fc["mean_f"],
                "forecast_std_f": fc["std_f"], "kalshi_mid": mid, "edge": edge,
                "contracts": contracts, "price": price, "cost": cost, "fee": fee,
                "stake": round(cost + fee, 2), "source": source,
                "entered_at": datetime.now(timezone.utc).isoformat(),
                "status": "open", "pnl": None,
            }
            state["positions"].append(trade)
            open_tickers.add(km["ticker"])
            state["bankroll"] = round(state["bankroll"] - cost - fee, 2)
            rec["trades"] += 1
            log.info(f"  TRADE  {mkt['city']} {km['subtitle']}  {direction}  "
                     f"{contracts}x @ {price:.2f}  edge={edge:.1%}  ({source})  "
                     f"bankroll=${state['bankroll']:.2f}")
        time.sleep(0.2)

    settle(state)
    state["scans"].append(rec)
    if len(state["scans"]) > 300:
        state["scans"] = state["scans"][-300:]
    state["last_scan"] = rec["time"]
    log.info(f"── scan done: {rec['markets_checked']} checked, "
             f"{rec['edges_found']} edges, {rec['trades']} trades ──")
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
            if stop_event.is_set() or SCAN_NOW.is_set():
                break
            time.sleep(1)
        SCAN_NOW.clear()
    state["status"] = "stopped"
    save_state(state)
    log.info("═══ Bot stopped ═══")

if __name__ == "__main__":
    stop = threading.Event()
    try:
        run_bot(stop)
    except KeyboardInterrupt:
        stop.set()
