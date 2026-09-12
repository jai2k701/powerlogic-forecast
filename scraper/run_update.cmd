@echo off
REM Daily data refresh (Scheduled Task "IEX Price Scraper", 15:30).
REM Uses a 30-day window rather than --update so a multi-day outage (PC off,
REM task refused) closes itself on the next successful run. Upserts are
REM idempotent and the PSP/VRE scrapers skip days already stored, so a normal
REM day costs only a few extra HTTP requests.
setlocal
set PY=C:\Python314\python.exe
set APP=C:\Users\Jai_PC\Desktop\PriceForecastApp
set PYTHONIOENCODING=utf-8

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
endlocal
