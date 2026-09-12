@echo off
REM Daily data refresh (Scheduled Task "IEX Price Scraper", 15:30).
REM
REM Uses the project-local .venv, NOT C:\Python314. Under the Scheduled Task the
REM per-user site-packages tree (C:\Users\Jai_PC\AppData\Roaming\Python\...) is not
REM even visible - os.listdir on it raises FileNotFoundError for a path that plainly
REM exists in an interactive shell - so every pip-installed package silently vanished
REM and the pdfplumber/pandas steps became no-ops while the task still reported
REM success. Putting the interpreter inside the project folder (which the task reads
REM and writes fine) sidesteps that entirely. Recreate with:
REM   python -m venv .venv && .venv\Scripts\python -m pip install -r requirements-local.txt
REM
REM A 30-day window rather than --update means a multi-day outage (PC off, task
REM refused) closes itself on the next successful run. Upserts are idempotent and the
REM PSP/VRE scrapers skip days already stored, so a normal day costs a few requests.
setlocal
set APP=C:\Users\Jai_PC\Desktop\PriceForecastApp
set PY=%APP%\.venv\Scripts\python.exe
set PYTHONIOENCODING=utf-8

if not exist "%PY%" (
  echo ERROR: %PY% missing - recreate the venv, see header.
  exit /b 1
)

"%PY%" "%APP%\scraper\iex_scraper.py" --backfill 30
"%PY%" "%APP%\scraper\gridindia_scraper.py" --backfill 30
"%PY%" "%APP%\scraper\vre_scraper.py" --backfill 30
"%PY%" "%APP%\scraper\weather_scraper.py" --backfill 30
"%PY%" "%APP%\models\dam_ml.py" --emit
"%PY%" "%APP%\scraper\export_snapshot.py"

REM Publish the refreshed snapshots so the Streamlit Cloud dashboard updates.
cd /d "%APP%"
git add data/daily_prices.csv data/blocks_recent.csv data/daily_fundamentals.csv data/ml_forecast.csv
git diff --cached --quiet
if errorlevel 1 (
  git commit -q -m "Daily data refresh"
  git push -q
)

REM Freshness gate LAST: its exit code becomes the task's "Last Result", so a
REM non-zero result in Task Scheduler now means "data is stale" instead of
REM silently reporting success while fetching nothing.
"%PY%" "%APP%\scraper\healthcheck.py"
exit /b %errorlevel%
