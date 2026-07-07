"""Grid-India (NLDC) Daily PSP Report scraper: demand + RE generation -> SQLite.

Source: grid-india.in "Daily PSP Report" PDFs, listed via the CMS API
(webapi.grid-india.in) and served from webcdn.grid-india.in. Page 2 of each
report carries the all-India Power Supply Position: energy met, peak demand,
and source-wise hydro / wind / solar generation.

Usage:
  python gridindia_scraper.py --backfill 365     # previous one year
  python gridindia_scraper.py --update           # last 10 days (daily cron run)
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

API = "https://webapi.grid-india.in/api/v1/file"
CDN = "https://webcdn.grid-india.in/"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
SLEEP_BETWEEN = 1.5


def log(msg):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    print(line)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def init_db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS daily_fundamentals (
            fund_date        TEXT PRIMARY KEY,   -- YYYY-MM-DD (data day)
            energy_met_mu    REAL,               -- all-India energy met (MU)
            peak_demand_mw   REAL,               -- max demand met during day (MW)
            evening_peak_mw  REAL,               -- demand met at evening peak (MW)
            hydro_mu         REAL,
            wind_mu          REAL,
            solar_mu         REAL,
            shortage_mu      REAL,
            source_file      TEXT,
            fetched_at       TEXT DEFAULT (datetime('now','localtime'))
        )""")
    con.commit()
    return con


def api_post(payload):
    req = urllib.request.Request(
        API, data=json.dumps(payload).encode(),
        headers={"User-Agent": UA, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def fy_months(d_from, d_to):
    """Yield (PeriodYear like '2026-27', month '07') covering the range."""
    seen = []
    cur = date(d_from.year, d_from.month, 1)
    while cur <= d_to:
        fy_start = cur.year if cur.month >= 4 else cur.year - 1
        key = (f"{fy_start}-{str(fy_start + 1)[-2:]}", f"{cur.month:02d}")
        if key not in seen:
            seen.append(key)
        cur = (cur + timedelta(days=32)).replace(day=1)
    return seen


def list_reports(d_from, d_to):
    """Return [(data_date, cdn_url, title)] for PSP reports in range."""
    out = []
    for fy, month in fy_months(d_from, d_to):
        try:
            res = api_post({"_source": "grdw", "_type": "DAILY_PSP_REPORT",
                            "_fileDate": fy, "_month": month})
        except Exception as e:                        # noqa: BLE001
            log(f"  list {fy}/{month} failed: {e}")
            continue
        for item in res.get("retData", []):
            if item.get("MimeType") != "application/pdf":
                continue
            m = re.match(r"(\d{2})\.(\d{2})\.(\d{2})", item.get("Title_", ""))
            if not m:
                continue
            d = date(2000 + int(m.group(3)), int(m.group(2)), int(m.group(1)))
            if d_from <= d <= d_to:
                out.append((d, CDN + item["FilePath"], item["Title_"]))
        time.sleep(0.5)
    return sorted(set(out))


NUM = r"[-\d.,]+"


def last_num(line):
    nums = re.findall(r"[\d][\d,]*\.?\d*", line)
    return float(nums[-1].replace(",", "")) if nums else None


def parse_psp_pdf(path):
    """Extract all-India numbers from the Power Supply Position page."""
    import pdfplumber
    text = ""
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages[:4]:
            t = page.extract_text() or ""
            if "Power Supply Position" in t and "Energy Met" in t:
                text = t
                break
    if not text:
        return None
    flat = text.replace("\n", " ")
    out = {}

    def grab(pattern):
        m = re.search(pattern, flat)
        return last_num(m.group(1)) if m else None

    out["energy_met_mu"] = grab(r"Energy Met \(MU\)((?:\s+" + NUM + r"){6})")
    out["evening_peak_mw"] = grab(
        r"Evening Peak hrs\s*\(MW\).*?RLDCs\)((?:\s+" + NUM + r"){6})")
    out["peak_demand_mw"] = grab(
        r"Maximum Demand Met During the Day.*?\(From[^)]*\)((?:\s+" + NUM + r"){6})")
    out["hydro_mu"] = grab(r"Hydro Gen \(MU\)((?:\s+" + NUM + r"){6})")
    out["wind_mu"] = grab(r"Wind Gen \(MU\)((?:\s+" + NUM + r"){6})")
    out["solar_mu"] = grab(r"Solar Gen \(MU\)\*?((?:\s+" + NUM + r"){6})")
    out["shortage_mu"] = grab(r"Energy Shortage \(MU\)((?:\s+" + NUM + r"){6})")
    return out if out.get("energy_met_mu") else None


def fetch_pdf(url, dest):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=120) as r, open(dest, "wb") as f:
        f.write(r.read())


def main():
    ap = argparse.ArgumentParser(description="Grid-India PSP scraper")
    ap.add_argument("--backfill", type=int, metavar="DAYS")
    ap.add_argument("--update", action="store_true")
    args = ap.parse_args()

    today = date.today()
    days = args.backfill if args.backfill else 10
    d_from, d_to = today - timedelta(days=days), today

    con = init_db()
    have = {r[0] for r in con.execute(
        "SELECT fund_date FROM daily_fundamentals "
        "WHERE energy_met_mu IS NOT NULL AND peak_demand_mw IS NOT NULL")}
    log(f"=== PSP run start: {d_from}..{d_to} ({len(have)} days already stored)")
    reports = list_reports(d_from, d_to)
    todo = [r for r in reports if r[0].isoformat() not in have]
    log(f"PSP: {len(reports)} reports listed, {len(todo)} new to fetch")

    tmp = BASE_DIR / "data" / "_psp_tmp.pdf"
    ok = 0
    for d, url, title in todo:
        try:
            fetch_pdf(url, tmp)
            vals = parse_psp_pdf(tmp)
            if not vals:
                log(f"  {d}: parse failed ({title})")
                continue
            con.execute(
                """INSERT INTO daily_fundamentals
                   (fund_date, energy_met_mu, peak_demand_mw, evening_peak_mw,
                    hydro_mu, wind_mu, solar_mu, shortage_mu, source_file)
                   VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(fund_date) DO UPDATE SET
                     energy_met_mu=excluded.energy_met_mu,
                     peak_demand_mw=excluded.peak_demand_mw,
                     evening_peak_mw=excluded.evening_peak_mw,
                     hydro_mu=excluded.hydro_mu, wind_mu=excluded.wind_mu,
                     solar_mu=excluded.solar_mu, shortage_mu=excluded.shortage_mu,
                     source_file=excluded.source_file,
                     fetched_at=datetime('now','localtime')""",
                (d.isoformat(), vals["energy_met_mu"], vals["peak_demand_mw"],
                 vals["evening_peak_mw"], vals["hydro_mu"], vals["wind_mu"],
                 vals["solar_mu"], vals["shortage_mu"], title))
            con.commit()
            ok += 1
            if ok % 25 == 0:
                log(f"  progress: {ok}/{len(todo)}")
        except Exception as e:                        # noqa: BLE001
            log(f"  {d}: FAILED — {e}")
        time.sleep(SLEEP_BETWEEN)
    tmp.unlink(missing_ok=True)
    n = con.execute("SELECT COUNT(*), MIN(fund_date), MAX(fund_date) "
                    "FROM daily_fundamentals").fetchone()
    log(f"=== PSP run done: {ok} new days | table {n[0]} rows ({n[1]}..{n[2]})")
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
