"""Grid-India NLDC Daily VRE (REMC) report scraper: day-ahead RE schedule vs actual.

The report's "REMC Monitored Profile" section carries all-India totals of the
day-ahead SCHEDULE and the ACTUAL generation (MU) for wind and solar — i.e.
the renewable forecast the market bid against, archived historically.

Usage:
  python vre_scraper.py --backfill 365
  python vre_scraper.py --update          # last 10 days
"""
import argparse
import re
import sqlite3
import sys
import tempfile
import time
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gridindia_scraper import api_post, fetch_pdf, fy_months, log, CDN, DB_PATH  # noqa: E402

NUM_TOKEN = re.compile(r"^-?\d[\d,]*\.?\d*$")


def init_db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS vre_schedule (
            vre_date        TEXT PRIMARY KEY,   -- delivery day
            wind_sched_mu   REAL,               -- day-ahead schedule (all-India REMC)
            wind_actual_mu  REAL,
            solar_sched_mu  REAL,
            solar_actual_mu REAL,
            source_file     TEXT,
            fetched_at      TEXT DEFAULT (datetime('now','localtime'))
        )""")
    con.commit()
    return con


def list_reports(d_from, d_to):
    out = []
    for fy, month in fy_months(d_from, d_to):
        try:
            res = api_post({"_source": "grdw", "_type": "DAILY_VRE_REPORT",
                            "_fileDate": fy, "_month": month})
        except Exception as e:                        # noqa: BLE001
            log(f"  VRE list {fy}/{month} failed: {e}")
            continue
        for item in res.get("retData", []):
            if item.get("MimeType") != "application/pdf":
                continue
            m = re.match(r"(\d{2})\.(\d{2})\.(\d{2,4})", item.get("Title_", ""))
            if not m:
                continue
            y = int(m.group(3))
            d = date(y if y > 100 else 2000 + y, int(m.group(2)), int(m.group(1)))
            if d_from <= d <= d_to:
                out.append((d, CDN + item["FilePath"], item["Title_"]))
        time.sleep(0.5)
    return sorted(set(out))


def last_nums(line, n=4):
    toks = [t for t in line.split() if NUM_TOKEN.match(t)]
    if len(toks) < n:
        return None
    return [float(t.replace(",", "")) for t in toks[-n:]]


def parse_vre_pdf(path):
    """All-India Total wind/solar schedule & actual from 'REMC Monitored Profile'."""
    import pdfplumber
    with pdfplumber.open(path) as pdf:
        full = "\n".join((p.extract_text() or "") for p in pdf.pages[:3])
    i2 = full.find("REMC Monitored Profile")
    if i2 < 0:
        return None
    seg = full[i2:]
    end = re.search(r"\n3\.", seg)
    seg = seg[:end.start()] if end else seg[:4000]
    lines = seg.splitlines()
    wind = solar = None
    for i, ln in enumerate(lines):
        if "Total" in ln and "/ Solar" in ln:
            solar = last_nums(ln)
            for j in range(i - 1, max(0, i - 3), -1):
                if "/ Wind" in lines[j]:
                    wind = last_nums(lines[j])
                    break
            break
    def ok(v):
        # sched + deviation should equal actual (tolerance for rounding)
        return v and abs((v[0] + v[2]) - v[1]) < max(2.0, 0.02 * abs(v[1]))
    if not (ok(wind) and ok(solar)):
        return None
    return {"wind_sched": wind[0], "wind_actual": wind[1],
            "solar_sched": solar[0], "solar_actual": solar[1]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", type=int, metavar="DAYS")
    ap.add_argument("--update", action="store_true")
    args = ap.parse_args()

    today = date.today()
    days = args.backfill if args.backfill else 10
    d_from, d_to = today - timedelta(days=days), today

    con = init_db()
    have = {r[0] for r in con.execute("SELECT vre_date FROM vre_schedule "
                                      "WHERE wind_sched_mu IS NOT NULL")}
    log(f"=== VRE run start: {d_from}..{d_to} ({len(have)} days stored)")
    reports = list_reports(d_from, d_to)
    todo = [r for r in reports if r[0].isoformat() not in have]
    log(f"VRE: {len(reports)} reports listed, {len(todo)} new to fetch")

    tmp = Path(tempfile.gettempdir()) / "vre_report_tmp.pdf"
    ok_n = 0
    for d, url, title in todo:
        try:
            fetch_pdf(url, tmp)
            vals = parse_vre_pdf(tmp)
            if not vals:
                log(f"  {d}: VRE parse failed ({title})")
                continue
            con.execute(
                """INSERT INTO vre_schedule
                   (vre_date, wind_sched_mu, wind_actual_mu,
                    solar_sched_mu, solar_actual_mu, source_file)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(vre_date) DO UPDATE SET
                     wind_sched_mu=excluded.wind_sched_mu,
                     wind_actual_mu=excluded.wind_actual_mu,
                     solar_sched_mu=excluded.solar_sched_mu,
                     solar_actual_mu=excluded.solar_actual_mu,
                     source_file=excluded.source_file,
                     fetched_at=datetime('now','localtime')""",
                (d.isoformat(), vals["wind_sched"], vals["wind_actual"],
                 vals["solar_sched"], vals["solar_actual"], title))
            con.commit()
            ok_n += 1
            if ok_n % 25 == 0:
                log(f"  VRE progress: {ok_n}/{len(todo)}")
        except Exception as e:                        # noqa: BLE001
            log(f"  {d}: VRE FAILED — {e}")
        time.sleep(1.5)
    tmp.unlink(missing_ok=True)
    n = con.execute("SELECT COUNT(*), MIN(vre_date), MAX(vre_date) "
                    "FROM vre_schedule").fetchone()
    log(f"=== VRE run done: {ok_n} new days | table {n[0]} rows ({n[1]}..{n[2]})")
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
