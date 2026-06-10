#!/usr/bin/env python3
"""
Backtest for the Kalshi weather strategy.

For each city and historical day:
  forecast = the high-temp forecast as it stood 1 day ahead
             (Open-Meteo previous-runs API)
  actual   = the observed high temp (Open-Meteo ERA5 archive)

Because historical Kalshi order books aren't freely available, the market
side is modeled three ways, bracketing reality:

  efficient    market prices bins from the same forecast with a calibrated
               std — the bot should win nothing and lose fees (null model)
  climatology  market prices bins from a trailing 14-day average, ignoring
               the forecast — an inattentive market; upper bound on edge
  noisy        market = forecast probability ± 5-20% random mispricing —
               same assumption as the live engine's simulation mode

The backtest also calibrates FORECAST_STD_F against real forecast errors,
which the bot's probabilities depend on directly.

Usage:
  python3 backtest.py                  # real data, ~last 80 days
  python3 backtest.py --days 60
  python3 backtest.py --synthetic      # generated weather (harness test only)

Real Kalshi price history (the missing piece) would replace the market
models above; see the candlesticks endpoint in the Kalshi API docs.
"""
import argparse, json, math, random, statistics, sys
from datetime import date, timedelta
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent / "bot"))
import engine
from engine import (MARKETS, MIN_EDGE, STAKE, FEE_RATE, FORECAST_STD_F, MAX_POSITIONS,
                    MIN_PRICE, prob_in_range, temp_in_range)

PREV_RUNS_API = "https://previous-runs-api.open-meteo.com/v1/forecast"
ARCHIVE_API   = "https://archive-api.open-meteo.com/v1/archive"
ARCHIVE_DELAY_DAYS = 6      # ERA5 archive lags realtime by ~5 days
CLIM_WINDOW   = 14          # trailing days the "climatology" market averages
RESULTS_FILE  = engine.BASE_DIR / "data" / "backtest_results.json"

# ── Historical data ───────────────────────────────────────────────────────────

def fetch_forecast_history(mkt, days):
    """Daily high forecasts as issued 1 day ahead, keyed by ISO date.
    The previous-runs API exposes hourly temps from yesterday's model run;
    we take the daily max in local time."""
    r = requests.get(PREV_RUNS_API, params={
        "latitude": mkt["lat"], "longitude": mkt["lon"],
        "hourly": "temperature_2m_previous_day1",
        "temperature_unit": "fahrenheit",
        "past_days": min(days + ARCHIVE_DELAY_DAYS, 92),
        "forecast_days": 1,
        "timezone": "auto",
    }, timeout=20)
    r.raise_for_status()
    hourly = r.json().get("hourly", {})
    out = {}
    for t, v in zip(hourly.get("time") or [], hourly.get("temperature_2m_previous_day1") or []):
        if v is None:
            continue
        day = t[:10]
        out[day] = max(out.get(day, -999.0), float(v))
    return out

def fetch_actual_history(mkt, days):
    """Observed daily highs (ERA5 reanalysis), keyed by ISO date."""
    end = date.today() - timedelta(days=ARCHIVE_DELAY_DAYS)
    start = end - timedelta(days=days)
    r = requests.get(ARCHIVE_API, params={
        "latitude": mkt["lat"], "longitude": mkt["lon"],
        "start_date": start.isoformat(), "end_date": end.isoformat(),
        "daily": "temperature_2m_max",
        "temperature_unit": "fahrenheit",
        "timezone": "auto",
    }, timeout=20)
    r.raise_for_status()
    daily = r.json().get("daily", {})
    return {d: float(t) for d, t in zip(daily.get("time") or [], daily.get("temperature_2m_max") or [])
            if t is not None}

def synthetic_history(mkt, days, rng):
    """Seasonal weather with multi-day regimes; forecast = truth + 2.5°F error.
    Tests the harness, NOT real forecast skill."""
    forecasts, actuals = {}, {}
    start = date.today() - timedelta(days=days + ARCHIVE_DELAY_DAYS)
    regime = 0.0
    for i in range(days):
        d = start + timedelta(days=i)
        seasonal = mkt["base_temp"] + 15 * math.sin(2 * math.pi * (d.timetuple().tm_yday - 100) / 365)
        regime = 0.6 * regime + rng.gauss(0, 3)
        truth = seasonal + regime + rng.gauss(0, 2)
        forecasts[d.isoformat()] = round(truth + rng.gauss(0, 2.5), 1)
        actuals[d.isoformat()] = round(truth, 1)
    return forecasts, actuals

# ── Market models ─────────────────────────────────────────────────────────────

def market_price(model, low, high, fc_mean, std_cal, clim_mean, clim_std, rng):
    if model == "efficient":
        p = prob_in_range(low, high, fc_mean, std_cal)
    elif model == "climatology":
        p = prob_in_range(low, high, clim_mean, clim_std)
    elif model == "noisy":
        p = prob_in_range(low, high, fc_mean, std_cal)
        p += rng.uniform(0.05, 0.20) * (1 if rng.random() > 0.5 else -1)
    else:
        raise ValueError(model)
    return max(0.03, min(0.97, p))

# ── Backtest core ─────────────────────────────────────────────────────────────

def make_bins(fc_mean):
    """Same 2°F bins around the forecast that the engine trades."""
    return [(round(fc_mean + off), round(fc_mean + off) + 2) for off in range(-8, 9, 2)]

def run_strategy(data, model, bot_std, std_cal, clim_stds, seed):
    """data: {city: (forecasts, actuals)}. Returns summary + daily pnl series."""
    rng = random.Random(seed)
    trades = []
    daily_pnl = {}
    all_days = sorted({d for fc, _ in data.values() for d in fc})
    for day in all_days:
        candidates = []
        for city, (forecasts, actuals) in data.items():
            if day not in forecasts or day not in actuals:
                continue
            past = [actuals[d] for d in forecasts if d < day and d in actuals]
            if len(past) < CLIM_WINDOW:
                continue  # warmup for the climatology market
            clim_mean = statistics.mean(past[-CLIM_WINDOW:])
            fc_mean = forecasts[day]
            for low, high in make_bins(fc_mean):
                p_bot = prob_in_range(low, high, fc_mean, bot_std)
                p_mkt = market_price(model, low, high, fc_mean, std_cal,
                                     clim_mean, clim_stds[city], rng)
                direction = "YES" if p_bot > p_mkt else "NO"
                edge = (p_bot - p_mkt) if direction == "YES" else (p_mkt - p_bot)
                if edge < MIN_EDGE:
                    continue
                price = p_mkt if direction == "YES" else 1 - p_mkt
                if price < MIN_PRICE:
                    continue  # thin books at extreme prices; sizing would be unrealistic
                contracts = max(1, int(STAKE // max(price, 0.01)))
                cost = contracts * price
                fee = FEE_RATE * contracts * p_mkt * (1 - p_mkt)
                yes_won = temp_in_range(actuals[day], low, high)
                won = yes_won if direction == "YES" else not yes_won
                pnl = (contracts if won else 0) - cost - fee
                candidates.append({"day": day, "city": city, "edge": round(edge, 4),
                                   "won": won, "pnl": round(pnl, 2),
                                   "outlay": round(cost + fee, 2)})
        # mirror the engine's position cap: best edges first, MAX_POSITIONS per day
        candidates.sort(key=lambda t: -t["edge"])
        taken = candidates[:MAX_POSITIONS]
        trades.extend(taken)
        daily_pnl[day] = sum(t["pnl"] for t in taken)

    total = sum(t["pnl"] for t in trades)
    outlay = sum(t["outlay"] for t in trades)
    wins = sum(1 for t in trades if t["won"])
    cum = mdd = peak = 0.0
    for day in sorted(daily_pnl):
        cum += daily_pnl[day]
        peak = max(peak, cum)
        mdd = max(mdd, peak - cum)
    return {"model": model, "bot_std": bot_std, "trades": len(trades),
            "win_rate": round(wins / len(trades), 3) if trades else None,
            "total_pnl": round(total, 2),
            "pnl_per_trade": round(total / len(trades), 2) if trades else None,
            "roi_per_trade": round(total / outlay, 4) if outlay else None,
            "max_drawdown": round(mdd, 2)}

def calibrate(data):
    """Forecast-error stats per city and pooled."""
    rows, pooled = [], []
    for city, (forecasts, actuals) in data.items():
        errs = [forecasts[d] - actuals[d] for d in forecasts if d in actuals]
        pooled.extend(errs)
        if len(errs) >= 2:
            rows.append({"city": city, "n": len(errs),
                         "bias": round(statistics.mean(errs), 2),
                         "std": round(statistics.stdev(errs), 2),
                         "within_3f": round(sum(1 for e in errs if abs(e) <= 3) / len(errs), 3)})
    std_cal = round(statistics.stdev(pooled), 2) if len(pooled) >= 2 else FORECAST_STD_F
    return rows, std_cal

def clim_std_per_city(data):
    """Std of (actual − trailing 14-day mean): the climatology market's uncertainty."""
    out = {}
    for city, (_, actuals) in data.items():
        days = sorted(actuals)
        resid = []
        for i in range(CLIM_WINDOW, len(days)):
            window = [actuals[d] for d in days[i - CLIM_WINDOW:i]]
            resid.append(actuals[days[i]] - statistics.mean(window))
        out[city] = max(3.0, round(statistics.stdev(resid), 2)) if len(resid) >= 2 else 6.0
    return out

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Backtest the Kalshi weather strategy")
    ap.add_argument("--days", type=int, default=80, help="history length (max ~85 real)")
    ap.add_argument("--synthetic", action="store_true", help="use generated weather")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    data, mode = {}, "real"
    if not args.synthetic:
        try:
            for mkt in MARKETS:
                fc = fetch_forecast_history(mkt, args.days)
                ac = fetch_actual_history(mkt, args.days)
                common = set(fc) & set(ac)
                data[mkt["city"]] = ({d: fc[d] for d in common}, {d: ac[d] for d in common})
                print(f"  {mkt['city']}: {len(common)} days of forecast+actual")
        except requests.RequestException as e:
            print(f"\n⚠  Historical APIs unreachable ({e.__class__.__name__}) — "
                  f"falling back to SYNTHETIC weather.\n"
                  f"   Synthetic results test the harness, not real forecast skill.\n")
            data, mode = {}, "synthetic"
    else:
        mode = "synthetic"
    if not data:
        for mkt in MARKETS:
            data[mkt["city"]] = synthetic_history(mkt, args.days, rng)

    cal_rows, std_cal = calibrate(data)
    clim_stds = clim_std_per_city(data)

    n_days = len({d for fc, _ in data.values() for d in fc})
    print(f"\n═══ BACKTEST — {mode.upper()} data, {n_days} days, {len(data)} cities ═══")
    print(f"\nForecast calibration (1-day-ahead forecast vs observed high):")
    print(f"  {'city':<14}{'n':>5}{'bias °F':>9}{'std °F':>8}{'|err|≤3°F':>11}")
    for r in cal_rows:
        print(f"  {r['city']:<14}{r['n']:>5}{r['bias']:>9.2f}{r['std']:>8.2f}{r['within_3f']:>10.0%}")
    print(f"  → pooled error std: {std_cal}°F  (bot FORECAST_STD_F = {FORECAST_STD_F})")

    print(f"\nStrategy results (stake ${STAKE}, min edge {MIN_EDGE:.0%}, fees on, "
          f"max {MAX_POSITIONS} trades/day):")
    print(f"  {'market model':<16}{'bot std':>8}{'trades':>8}{'win%':>7}{'total P&L':>11}"
          f"{'$/trade':>9}{'ROI/trade':>11}{'max DD':>9}")
    results = []
    for model in ("efficient", "climatology", "noisy"):
        for bot_std in (FORECAST_STD_F, std_cal):
            r = run_strategy(data, model, bot_std, std_cal, clim_stds, args.seed)
            results.append(r)
            wr = f"{r['win_rate']:.0%}" if r['win_rate'] is not None else "—"
            ppt = f"{r['pnl_per_trade']:+.2f}" if r['pnl_per_trade'] is not None else "—"
            roi = f"{r['roi_per_trade']:+.1%}" if r['roi_per_trade'] is not None else "—"
            print(f"  {model:<16}{bot_std:>8}{r['trades']:>8}{wr:>7}{r['total_pnl']:>+11.2f}"
                  f"{ppt:>9}{roi:>11}{r['max_drawdown']:>9.2f}")

    print(f"""
How to read this:
  efficient    market knows what the bot knows → P&L here is pure fee/
               miscalibration drag; should be ≈ 0 or negative
  climatology  market ignores forecasts → upper bound, only real if Kalshi
               traders are this lazy (they usually aren't)
  noisy        assumes 5-20% random mispricing — the live engine's sim
               assumption; treat as illustrative only
The strategy is only viable if real Kalshi quotes sit closer to climatology
than to the forecast. Verify with live paper trading before any real money.""")

    RESULTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    RESULTS_FILE.write_text(json.dumps(
        {"mode": mode, "days": n_days, "calibration": cal_rows,
         "pooled_std": std_cal, "results": results}, indent=2))
    print(f"Saved: {RESULTS_FILE}")

if __name__ == "__main__":
    main()
