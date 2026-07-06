# PowerLogic — Market Price Forecasting

Single-file web app for forecasting Indian power exchange prices across **DAM** (Day-Ahead Market), **RTM** (Real-Time Market) and **G-DAM** (Green Day-Ahead Market).

Companion app to [powerlogic](https://github.com/jai2k701/powerlogic) (Industrial Open Access cost analysis).

## Features

- **Market segments:** DAM / RTM / G-DAM tabs with IEX-calibrated price levels; exchange selector (IEX / PXIL / HPX)
- **Forecast models:** Holt-Winters (trend + weekly seasonality), Seasonal Naive, 7-Day Moving Average, Linear Trend + Weekly Regression
- **Horizons:** 1 / 7 / 15 / 30 days with 80/90/95% confidence bands
- **Scenario levers:** demand growth, solar/RE availability, fuel/import price factor, evening peak stress, CERC floor/ceiling caps
- **Outputs:** daily avg MCP trend chart (history + forecast band), 96-block (15-min) intraday profile, block-wise forecast table with slot tags (Peak / Solar / Morning / Night), CSV export

## Run

```
python backend/server.py        # http://localhost:8123 — app + real IEX data from SQLite
```

The frontend boots instantly on synthetic data, then swaps in real prices from the backend
(`LIVE DB · IEX` tag in the header). Opened as a plain file or on GitHub Pages (no backend),
it falls back to synthetic data (`SYNTHETIC DATA` tag).

API: `/api/meta`, `/api/daily?market=DAM&days=400`, `/api/shape?market=DAM&days=45`,
`/api/blocks?market=DAM&date=2026-06-01`. Markets: `DAM` `RTM` `GDAM`. Prices in Rs/kWh.

## Data scraper (`scraper/iex_scraper.py`)

Extracts real block-wise (15-min, 96 blocks/day) MCP + volume data for DAM, RTM and G-DAM
from the IEX market-snapshot pages into SQLite at `data/iex_prices.db`
(table `market_prices`, PK: date + market + exchange + block).

```
python scraper/iex_scraper.py --backfill 365                                # previous one year, all markets
python scraper/iex_scraper.py --update                                      # last 7 days + next day (daily job)
python scraper/iex_scraper.py --market DAM --from-date 01-05-2026 --to-date 31-05-2026
```

A Windows Scheduled Task ("IEX Price Scraper", daily 15:30, after DAM results publish)
runs `scraper/run_update.cmd` to keep the database current. Recreate it with:

```
schtasks /Create /TN "IEX Price Scraper" /TR "<path>\scraper\run_update.cmd" /SC DAILY /ST 15:30 /F
```

> Prices are indicative, generated from embedded historical seasonal patterns (summer/October highs, monsoon lows, weekend dips). Not a substitute for exchange-published MCP.
