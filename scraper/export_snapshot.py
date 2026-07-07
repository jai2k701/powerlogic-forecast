"""Export compact CSV snapshots from iex_prices.db for the Streamlit app.

Committed to the repo so the cloud-hosted app has real data without a server:
  data/daily_prices.csv  - full daily avg MCP history per market (Rs/kWh)
  data/blocks_recent.csv - last 60 days of block-wise MCP (Rs/MWh)
"""
import csv
import sqlite3
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "data" / "iex_prices.db"
OUT_DAILY = BASE_DIR / "data" / "daily_prices.csv"
OUT_BLOCKS = BASE_DIR / "data" / "blocks_recent.csv"


def main():
    con = sqlite3.connect(DB_PATH)
    daily = con.execute(
        """SELECT market, price_date, ROUND(AVG(mcp_rs_mwh)/1000.0, 4)
           FROM market_prices WHERE mcp_rs_mwh IS NOT NULL
           GROUP BY market, price_date HAVING COUNT(*) >= 90
           ORDER BY market, price_date""").fetchall()
    with open(OUT_DAILY, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["market", "date", "avg_mcp_rs_kwh"])
        w.writerows(daily)

    blocks = con.execute(
        """SELECT market, price_date, time_block, mcp_rs_mwh
           FROM market_prices
           WHERE mcp_rs_mwh IS NOT NULL
             AND price_date >= date('now', '-60 day')
           ORDER BY market, price_date, time_block""").fetchall()
    with open(OUT_BLOCKS, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["market", "date", "block", "mcp_rs_mwh"])
        w.writerows(blocks)
    con.close()
    print(f"exported {len(daily)} daily rows -> {OUT_DAILY.name}, "
          f"{len(blocks)} block rows -> {OUT_BLOCKS.name}")


if __name__ == "__main__":
    main()
