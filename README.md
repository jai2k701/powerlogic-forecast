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

Open `index.html` in any browser — no build step, no backend. Chart.js is loaded from CDN.

> Prices are indicative, generated from embedded historical seasonal patterns (summer/October highs, monsoon lows, weekend dips). Not a substitute for exchange-published MCP.
