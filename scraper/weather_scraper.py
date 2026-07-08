"""Open-Meteo daily weather -> SQLite (demand driver for the ML forecaster).

Population-weighted average of 5 metro cities. Historical from the archive
API; the last ~6 days plus tomorrow from the forecast API (archive lags).
Free, no key. Usage: python weather_scraper.py [--backfill DAYS]
"""
import argparse
import json
import sqlite3
import sys
import time
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "iex_prices.db"

CITIES = {  # name: (lat, lon, population weight)
    "Delhi": (28.61, 77.21, 32),
    "Mumbai": (19.08, 72.88, 21),
    "Kolkata": (22.57, 88.36, 15),
    "Chennai": (13.08, 80.27, 12),
    "Hyderabad": (17.38, 78.49, 11),
}
ARCHIVE = ("https://archive-api.open-meteo.com/v1/archive?latitude={lat}"
           "&longitude={lon}&start_date={d1}&end_date={d2}"
           "&daily=temperature_2m_max,temperature_2m_min&timezone=Asia%2FKolkata")
FORECAST = ("https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
            "&daily=temperature_2m_max,temperature_2m_min&past_days=7"
            "&forecast_days=3&timezone=Asia%2FKolkata")


def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", type=int, default=None, metavar="DAYS")
    args = ap.parse_args()

    con = sqlite3.connect(DB_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS daily_weather (
        wx_date TEXT PRIMARY KEY, tmax_c REAL, tmin_c REAL,
        fetched_at TEXT DEFAULT (datetime('now','localtime')))""")

    today = date.today()
    days = args.backfill or 10
    d1 = today - timedelta(days=days)
    total_w = sum(w for _, _, w in CITIES.values())
    acc = {}   # date -> [wsum_tmax, wsum_tmin]

    for name, (lat, lon, w) in CITIES.items():
        urls = [FORECAST.format(lat=lat, lon=lon)]
        if days > 7:
            urls.append(ARCHIVE.format(lat=lat, lon=lon, d1=d1.isoformat(),
                                       d2=(today - timedelta(days=5)).isoformat()))
        for url in urls:
            try:
                j = get_json(url)["daily"]
            except Exception as e:                     # noqa: BLE001
                print(f"{name}: fetch failed — {e}")
                continue
            for d, tx, tn in zip(j["time"], j["temperature_2m_max"],
                                 j["temperature_2m_min"]):
                if tx is None or tn is None:
                    continue
                a = acc.setdefault(d, [0.0, 0.0, 0.0])
                a[0] += tx * w
                a[1] += tn * w
                a[2] += w
            time.sleep(0.4)

    rows = [(d, round(v[0] / v[2], 2), round(v[1] / v[2], 2))
            for d, v in sorted(acc.items()) if v[2] >= total_w * 0.6]
    con.executemany("""INSERT INTO daily_weather (wx_date, tmax_c, tmin_c)
                       VALUES (?,?,?)
                       ON CONFLICT(wx_date) DO UPDATE SET
                         tmax_c=excluded.tmax_c, tmin_c=excluded.tmin_c,
                         fetched_at=datetime('now','localtime')""", rows)
    con.commit()
    n = con.execute("SELECT COUNT(*), MIN(wx_date), MAX(wx_date) "
                    "FROM daily_weather").fetchone()
    print(f"{datetime.now():%H:%M:%S} weather: upserted {len(rows)} days | "
          f"table {n[0]} rows ({n[1]}..{n[2]})")
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
