# Kalshi Weather Arb Bot (Paper Trading)

Paper-trading bot that compares weather forecasts (Open-Meteo, with a seasonal
simulation fallback) against Kalshi daily-high temperature market prices, and
paper-trades any edge above the configured threshold. No real money is involved.

Positions settle against the **actual observed high temperature** (Open-Meteo)
once the market's day has passed, so win rate and P&L reflect real forecast
skill — not a simulated coin flip. When live data is unreachable the bot falls
back to simulation, and everything simulated is tagged `SIM` in the dashboard.
**Only trust results where both entry and settlement used live data.**

## Quick start

```bash
pip3 install -r requirements.txt
python3 run.py
```

Then open `dashboard/index.html` in your browser. The dashboard polls the bot
API at `http://localhost:5000` every 5 seconds.

## Backtest

```bash
python3 backtest.py              # ~last 80 days of real forecasts + actuals
python3 backtest.py --days 60
```

Pulls 1-day-ahead historical forecasts (Open-Meteo previous-runs API) and
observed highs (ERA5 archive), calibrates the forecast error std, and runs the
strategy against three market models — `efficient` (null: market knows the
forecast), `climatology` (market ignores forecasts; upper bound), and `noisy`
(random 5–20% mispricing). Historical Kalshi order books aren't freely
available, so the market side is modeled, not replayed: the backtest tells you
whether the *forecast* has skill and what fees cost, not what Kalshi would
actually have paid. Falls back to clearly-labeled synthetic weather when the
APIs are unreachable. Results are saved to `data/backtest_results.json`.

For the full walkthrough (including Windows/Mac/Linux specifics), open
`SETUP_GUIDE.html` in a browser.

## Layout

```
kalshi_bot/
├── run.py              # entry point — starts API server + bot engine
├── requirements.txt
├── SETUP_GUIDE.html    # step-by-step setup guide
├── bot/
│   ├── engine.py       # scan/trade/settle engine
│   └── server.py       # Flask API serving bot state
├── dashboard/
│   └── index.html      # live dashboard (no build step)
└── data/               # created at runtime: state.json, bot.log (gitignored)
```

## Configuration

Tunables live at the top of `bot/engine.py`:

- `SCAN_INTERVAL` — seconds between scans (default 300)
- `MIN_EDGE` — minimum forecast-vs-price gap to trade (default 0.08)
- `STAKE` — target outlay per trade in dollars (default 50)
- `MAX_POSITIONS` — max simultaneous open positions (default 6)
- `START_BANKROLL` — starting paper bankroll (default $1,000)
- `FEE_RATE` — Kalshi-style trading fee, ≈ 0.07 · price · (1 − price) per contract
- `FORECAST_STD_F` — assumed 1-day-ahead forecast error in °F (default 3.0);
  edge size is sensitive to this, so calibrate it against real forecast errors
  for your cities before trusting the results
