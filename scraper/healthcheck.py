"""Data freshness check — exit code 0 when every source is current, 1 when not.

Run last in run_update.cmd so the Scheduled Task's "Last Result" becomes a real
health signal: the 2026 Jul-Sep outage was invisible precisely because the task
reported success while fetching nothing.

  python scraper/healthcheck.py
"""
import sqlite3
import sys
from datetime import date
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "iex_prices.db"

# table, date column, max acceptable lag in days behind today
SOURCES = [
    ("market_prices", "price_date", 2),        # DAM clears D-1 for D
    ("daily_fundamentals", "fund_date", 4),    # PSP report publishes D+1
    ("vre_schedule", "vre_date", 5),           # VRE report publishes D+1, sometimes late
    ("daily_weather", "wx_date", 2),           # includes forecast days
]


def check_ml_forecast(today):
    """The ML output is a file, not a table, and went stale unnoticed once
    (Excel held the CSV open, so every rewrite failed). Gate it too."""
    path = DB_PATH.parent / "ml_forecast.csv"
    if not path.exists():
        print("FAIL  ml_forecast.csv: missing")
        return False
    try:
        rows = [ln.split(",") for ln in
                path.read_text(encoding="utf-8").splitlines()[1:] if ln]
        nd = [r[1] for r in rows if r and r[0] == "next_day"]
        if not nd:
            print("FAIL  ml_forecast.csv: no next_day row")
            return False
        lag = (today - date.fromisoformat(nd[0])).days
        ok = lag <= 1                       # forecast is for tomorrow
        print(f"{'ok  ' if ok else 'STALE'}  {'ml_forecast.csv':20} "
              f"next_day={nd[0]}  lag={lag}d (max 1d)")
        return ok
    except Exception as e:                                 # noqa: BLE001
        print(f"FAIL  ml_forecast.csv: {e}")
        return False


def main():
    if not DB_PATH.exists():
        print(f"FAIL  database missing: {DB_PATH}")
        return 1
    con = sqlite3.connect(DB_PATH)
    today = date.today()
    bad = []
    for table, col, max_lag in SOURCES:
        try:
            n, latest = con.execute(
                f"SELECT COUNT(*), MAX({col}) FROM {table}").fetchone()
        except sqlite3.OperationalError as e:
            print(f"FAIL  {table}: {e}")
            bad.append(table)
            continue
        if not latest:
            print(f"FAIL  {table}: empty")
            bad.append(table)
            continue
        lag = (today - date.fromisoformat(latest)).days
        status = "ok  " if lag <= max_lag else "STALE"
        print(f"{status}  {table:20} latest={latest}  lag={lag}d "
              f"(max {max_lag}d)  rows={n}")
        if lag > max_lag:
            bad.append(table)
    con.close()
    if not check_ml_forecast(today):
        bad.append("ml_forecast.csv")
    if bad:
        print(f"\nFAIL: stale sources: {', '.join(bad)}")
        return 1
    print("\nOK: all sources current")
    return 0


if __name__ == "__main__":
    sys.exit(main())
