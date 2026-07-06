"""PowerLogic forecasting backend: serves the app + real IEX data from SQLite.

Run:  python backend/server.py          (http://localhost:8123)
Data: data/iex_prices.db, populated by scraper/iex_scraper.py
"""
import sqlite3
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "data" / "iex_prices.db"
MARKETS = ("DAM", "RTM", "GDAM")

app = FastAPI(title="PowerLogic Price API")


def q(sql, params=()):
    con = sqlite3.connect(DB_PATH)
    try:
        return con.execute(sql, params).fetchall()
    finally:
        con.close()


def check_market(market):
    if market not in MARKETS:
        raise HTTPException(400, f"market must be one of {MARKETS}")


@app.get("/api/meta")
def meta():
    rows = q("""SELECT market, COUNT(*), MIN(price_date), MAX(price_date),
                       MAX(fetched_at)
                FROM market_prices GROUP BY market""")
    return {m: {"rows": n, "from": lo, "to": hi, "fetched_at": fa}
            for m, n, lo, hi, fa in rows}


@app.get("/api/daily")
def daily(market: str = Query("DAM"), days: int = Query(400, ge=7, le=2000)):
    """Daily average MCP in Rs/kWh, oldest first. Excludes incomplete days
    (days with under 90 blocks, e.g. today's still-trading RTM)."""
    check_market(market)
    rows = q("""SELECT price_date, AVG(mcp_rs_mwh)/1000.0
                FROM market_prices
                WHERE market=? AND mcp_rs_mwh IS NOT NULL
                GROUP BY price_date HAVING COUNT(*) >= 90
                ORDER BY price_date DESC LIMIT ?""", (market, days))
    return [{"date": d, "price": round(p, 4)} for d, p in reversed(rows)]


@app.get("/api/shape")
def shape(market: str = Query("DAM"), days: int = Query(45, ge=7, le=400)):
    """Average intraday profile: 96 multipliers (mean 1) from the most
    recent `days` days of block data."""
    check_market(market)
    cutoff = q("""SELECT MIN(price_date) FROM (
                    SELECT DISTINCT price_date FROM market_prices
                    WHERE market=? ORDER BY price_date DESC LIMIT ?)""",
               (market, days))[0][0]
    rows = q("""SELECT time_block, AVG(mcp_rs_mwh)
                FROM market_prices
                WHERE market=? AND price_date>=? AND mcp_rs_mwh IS NOT NULL
                GROUP BY time_block ORDER BY time_block""", (market, cutoff))
    if len(rows) < 96:
        raise HTTPException(404, "insufficient block data")
    vals = [v for _, v in rows]
    mean = sum(vals) / len(vals)
    return [round(v / mean, 5) for v in vals]


@app.get("/api/blocks")
def blocks(market: str = Query("DAM"), date: str = Query(...)):
    """Actual 96-block prices (Rs/kWh) for one date."""
    check_market(market)
    rows = q("""SELECT time_block, mcp_rs_mwh/1000.0, mcv_mwh
                FROM market_prices WHERE market=? AND price_date=?
                ORDER BY time_block""", (market, date))
    if not rows:
        raise HTTPException(404, f"no data for {market} {date}")
    return [{"block": b, "price": p and round(p, 4), "mcv": v} for b, p, v in rows]


@app.get("/")
def index():
    return FileResponse(BASE_DIR / "index.html")


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8123)
