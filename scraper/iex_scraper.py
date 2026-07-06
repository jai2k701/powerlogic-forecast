"""IEX market-snapshot scraper: DAM / RTM / G-DAM block-wise prices -> SQLite.

The iexindia.com market-snapshot pages server-render the full records array
(96 blocks/day) in the Next.js flight payload, so we fetch the page HTML with
fromDate/toDate params and parse the embedded JSON records.

Usage:
  python iex_scraper.py --update               # last 7 days + next day (daily cron run)
  python iex_scraper.py --backfill 365         # previous one year, all markets
  python iex_scraper.py --market DAM --from-date 01-05-2026 --to-date 31-05-2026
"""
import argparse
import json
import re
import sqlite3
import sys
import time
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "data" / "iex_prices.db"
LOG_PATH = Path(__file__).resolve().parent / "scraper.log"

MARKETS = {
    "DAM":  "day-ahead-market",
    "RTM":  "real-time-market",
    "GDAM": "green-day-ahead-market",
}
URL_TMPL = ("https://www.iexindia.com/market-data/{slug}/market-snapshot"
            "?interval=ONE_FOURTH_HOUR&dp=SELECT_RANGE&showGraph=false"
            "&fromDate={frm}&toDate={to}")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

CHUNK_DAYS = 30          # site renders ~31 days per request reliably
SLEEP_BETWEEN = 3        # polite delay between requests (seconds)
RETRIES = 3

PERIOD_RE = re.compile(r"^(\d{2}):(\d{2})\s*-\s*\d{2}:\d{2}$")
RECORDS_RE = re.compile(r'"records":\[(\{.*?\})\]', re.DOTALL)


def log(msg):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    print(line)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS market_prices (
            price_date       TEXT    NOT NULL,   -- YYYY-MM-DD
            market           TEXT    NOT NULL,   -- DAM / RTM / GDAM
            exchange         TEXT    NOT NULL DEFAULT 'IEX',
            time_block       INTEGER NOT NULL,   -- 1..96 (block 1 = 00:00-00:15)
            mcp_rs_mwh       REAL,
            mcv_mwh          REAL,
            purchase_bid_mwh REAL,
            sell_bid_mwh     REAL,
            final_sched_mwh  REAL,
            fetched_at       TEXT DEFAULT (datetime('now','localtime')),
            PRIMARY KEY (price_date, market, exchange, time_block)
        )""")
    con.execute("CREATE INDEX IF NOT EXISTS idx_mp_market_date "
                "ON market_prices(market, price_date)")
    con.commit()
    return con


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Accept": "text/html"})
    last_err = None
    for attempt in range(1, RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.read().decode("utf-8", errors="replace")
        except Exception as e:                        # noqa: BLE001
            last_err = e
            log(f"  attempt {attempt}/{RETRIES} failed: {e}")
            time.sleep(10 * attempt)
    raise RuntimeError(f"fetch failed after {RETRIES} attempts: {last_err}")


def num(v):
    if v in (None, "", "-"):
        return None
    try:
        return float(str(v).replace(",", ""))
    except ValueError:
        return None


def parse_records(html):
    """Yield dict rows from every embedded records array (block rows only)."""
    text = html.replace('\\"', '"')
    for m in RECORDS_RE.finditer(text):
        try:
            records = json.loads("[" + m.group(1) + "]")
        except json.JSONDecodeError:
            continue
        for rec in records:
            pm = PERIOD_RE.match(str(rec.get("period", "")).strip())
            if not pm:
                continue                       # skip Total/summary rows
            hh, mm = int(pm.group(1)), int(pm.group(2))
            try:
                d = datetime.strptime(rec["date"], "%d-%m-%Y").date()
            except (KeyError, ValueError):
                continue
            # G-DAM uses unconstrained_m_c_p / total_cleared_volume / total_sell_bid
            mcp = rec.get("mcp")
            if mcp is None:
                mcp = rec.get("unconstrained_m_c_p")
            mcv = rec.get("mcv")
            if mcv is None:
                mcv = rec.get("total_cleared_volume")
            sell = rec.get("sell_bid")
            if sell is None:
                sell = rec.get("total_sell_bid")
            yield {
                "price_date": d.isoformat(),
                "time_block": hh * 4 + mm // 15 + 1,
                "mcp": num(mcp),
                "mcv": num(mcv),
                "purchase_bid": num(rec.get("purchase_bid")),
                "sell_bid": num(sell),
                "fsv": num(rec.get("final_scheduled_volume")),
            }


def scrape_range(con, market, d_from, d_to):
    slug = MARKETS[market]
    total = 0
    cur = d_from
    while cur <= d_to:
        chunk_end = min(cur + timedelta(days=CHUNK_DAYS - 1), d_to)
        url = URL_TMPL.format(slug=slug, frm=cur.strftime("%d-%m-%Y"),
                              to=chunk_end.strftime("%d-%m-%Y"))
        log(f"{market}: fetching {cur} .. {chunk_end}")
        try:
            html = fetch(url)
        except RuntimeError as e:
            log(f"{market}: SKIPPED {cur}..{chunk_end} — {e}")
            cur = chunk_end + timedelta(days=1)
            continue
        rows = list(parse_records(html))
        con.executemany(
            """INSERT INTO market_prices
               (price_date, market, exchange, time_block, mcp_rs_mwh, mcv_mwh,
                purchase_bid_mwh, sell_bid_mwh, final_sched_mwh, fetched_at)
               VALUES (?,?,?,?,?,?,?,?,?,datetime('now','localtime'))
               ON CONFLICT(price_date, market, exchange, time_block)
               DO UPDATE SET mcp_rs_mwh=excluded.mcp_rs_mwh,
                             mcv_mwh=excluded.mcv_mwh,
                             purchase_bid_mwh=excluded.purchase_bid_mwh,
                             sell_bid_mwh=excluded.sell_bid_mwh,
                             final_sched_mwh=excluded.final_sched_mwh,
                             fetched_at=excluded.fetched_at""",
            [(r["price_date"], market, "IEX", r["time_block"], r["mcp"],
              r["mcv"], r["purchase_bid"], r["sell_bid"], r["fsv"])
             for r in rows])
        con.commit()
        log(f"{market}: stored {len(rows)} block rows")
        total += len(rows)
        cur = chunk_end + timedelta(days=1)
        if cur <= d_to:
            time.sleep(SLEEP_BETWEEN)
    return total


def main():
    ap = argparse.ArgumentParser(description="IEX price scraper")
    ap.add_argument("--market", choices=[*MARKETS, "ALL"], default="ALL")
    ap.add_argument("--backfill", type=int, metavar="DAYS",
                    help="scrape the previous DAYS days")
    ap.add_argument("--update", action="store_true",
                    help="scrape last 7 days + next day (daily run)")
    ap.add_argument("--from-date", metavar="DD-MM-YYYY")
    ap.add_argument("--to-date", metavar="DD-MM-YYYY")
    args = ap.parse_args()

    today = date.today()
    if args.backfill:
        d_from, d_to = today - timedelta(days=args.backfill), today
    elif args.from_date and args.to_date:
        d_from = datetime.strptime(args.from_date, "%d-%m-%Y").date()
        d_to = datetime.strptime(args.to_date, "%d-%m-%Y").date()
    else:  # --update (default): re-fetch trailing week + next-day DAM/GDAM results
        d_from, d_to = today - timedelta(days=7), today + timedelta(days=1)

    markets = list(MARKETS) if args.market == "ALL" else [args.market]
    con = init_db()
    log(f"=== run start: markets={markets} range={d_from}..{d_to} db={DB_PATH}")
    grand = 0
    for mkt in markets:
        grand += scrape_range(con, mkt, d_from, d_to)
        time.sleep(SLEEP_BETWEEN)
    n = con.execute("SELECT COUNT(*), MIN(price_date), MAX(price_date) "
                    "FROM market_prices").fetchone()
    log(f"=== run done: {grand} rows upserted | table now {n[0]} rows "
        f"({n[1]} .. {n[2]})")
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
