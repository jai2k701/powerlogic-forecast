@echo off
"C:\Python314\python.exe" "C:\Users\Jai_PC\Desktop\PriceForecastApp\scraper\iex_scraper.py" --update
"C:\Python314\python.exe" "C:\Users\Jai_PC\Desktop\PriceForecastApp\scraper\gridindia_scraper.py" --update
"C:\Python314\python.exe" "C:\Users\Jai_PC\Desktop\PriceForecastApp\scraper\vre_scraper.py" --update
"C:\Python314\python.exe" "C:\Users\Jai_PC\Desktop\PriceForecastApp\scraper\weather_scraper.py"
"C:\Python314\python.exe" "C:\Users\Jai_PC\Desktop\PriceForecastApp\scraper\export_snapshot.py"
