# Kalshi Weather Arb Bot (Paper Trading)

Paper-trading bot that compares weather forecasts (Open-Meteo, with a seasonal
simulation fallback) against Kalshi daily-high temperature market prices, and
paper-trades any edge above the configured threshold. No real money is involved.

## Quick start

```bash
pip3 install -r requirements.txt
python3 run.py
```

Then open `dashboard/index.html` in your browser. The dashboard polls the bot
API at `http://localhost:5000` every 5 seconds.

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
- `STAKE` — paper-trade size in dollars (default 50)
- `MAX_POSITIONS` — max simultaneous open positions (default 6)
- `START_BANKROLL` — starting paper bankroll (default $1,000)
