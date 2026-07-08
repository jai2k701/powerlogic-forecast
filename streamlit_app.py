"""PowerLogic — DAM / RTM / G-DAM price forecasting (Streamlit version).

Data source, in order of preference:
  1. Local SQLite (data/iex_prices.db) kept fresh by scraper/iex_scraper.py
  2. CSV snapshots committed to the repo (data/*.csv) — used on Streamlit Cloud
Optionally tops up missing recent days by scraping IEX directly (cached).

Run:  streamlit run streamlit_app.py
"""
import math
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data" / "iex_prices.db"
DAILY_CSV = BASE_DIR / "data" / "daily_prices.csv"
BLOCKS_CSV = BASE_DIR / "data" / "blocks_recent.csv"
FUND_CSV = BASE_DIR / "data" / "daily_fundamentals.csv"
sys.path.insert(0, str(BASE_DIR / "scraper"))

MARKETS = {"DAM": "Day-Ahead Market", "RTM": "Real-Time Market",
           "GDAM": "Green Day-Ahead Market"}
NAVY, BLUE, GREEN, AMBER = "#1e3a5f", "#2563eb", "#10b981", "#f59e0b"

st.set_page_config(page_title="PowerLogic — Price Forecasting",
                   page_icon="⚡", layout="wide")


# ==================== DATA ====================
@st.cache_data(ttl=3600, show_spinner="Loading price data…")
def load_data():
    """Return (daily_df, blocks_df, source). Prices: daily in Rs/kWh, blocks in Rs/MWh."""
    if DB_PATH.exists():
        con = sqlite3.connect(DB_PATH)
        daily = pd.read_sql_query(
            """SELECT market, price_date AS date, AVG(mcp_rs_mwh)/1000.0 AS avg_mcp_rs_kwh
               FROM market_prices WHERE mcp_rs_mwh IS NOT NULL
               GROUP BY market, price_date HAVING COUNT(*) >= 90
               ORDER BY market, price_date""", con)
        blocks = pd.read_sql_query(
            """SELECT market, price_date AS date, time_block AS block, mcp_rs_mwh
               FROM market_prices
               WHERE mcp_rs_mwh IS NOT NULL AND price_date >= date('now','-60 day')""", con)
        con.close()
        source = "local SQLite database"
    elif DAILY_CSV.exists() and BLOCKS_CSV.exists():
        daily = pd.read_csv(DAILY_CSV)
        blocks = pd.read_csv(BLOCKS_CSV)
        source = "repo CSV snapshot"
    else:
        st.error("No data found: neither data/iex_prices.db nor data/*.csv exist. "
                 "Run `python scraper/iex_scraper.py --backfill 365` first.")
        st.stop()
    fund = pd.DataFrame()
    if DB_PATH.exists():
        con = sqlite3.connect(DB_PATH)
        try:
            fund = pd.read_sql_query(
                """SELECT fund_date AS date, energy_met_mu, peak_demand_mw,
                          hydro_mu, wind_mu, solar_mu
                   FROM daily_fundamentals WHERE energy_met_mu IS NOT NULL
                   ORDER BY fund_date""", con)
        except Exception:                              # noqa: BLE001
            pass
        con.close()
    elif FUND_CSV.exists():
        fund = pd.read_csv(FUND_CSV)
    daily["date"] = pd.to_datetime(daily["date"]).dt.date
    blocks["date"] = pd.to_datetime(blocks["date"]).dt.date
    if not fund.empty:
        fund["date"] = pd.to_datetime(fund["date"]).dt.date
    return daily, blocks, fund, source


@st.cache_data(ttl=6 * 3600, show_spinner="Fetching latest prices from IEX…")
def fetch_gap_from_iex(last_have: date, upto: date):
    """Scrape any days missing between the snapshot and today (cloud freshness)."""
    try:
        import iex_scraper as sc
        rows = []
        for mkt, slug in sc.MARKETS.items():
            url = sc.URL_TMPL.format(slug=slug,
                                     frm=(last_have + timedelta(days=1)).strftime("%d-%m-%Y"),
                                     to=upto.strftime("%d-%m-%Y"))
            html = sc.fetch(url)
            for r in sc.parse_records(html):
                rows.append({"market": mkt, "date": date.fromisoformat(r["price_date"]),
                             "block": r["time_block"], "mcp_rs_mwh": r["mcp"]})
        return pd.DataFrame(rows)
    except Exception:                                    # noqa: BLE001
        return pd.DataFrame()


def top_up(daily, blocks):
    """Merge freshly scraped days into the loaded data (no-op when current)."""
    last_have = daily["date"].max()
    target = date.today() + timedelta(days=1)            # DAM publishes D+1
    if last_have >= target or (target - last_have).days > 40:
        return daily, blocks, False
    new = fetch_gap_from_iex(last_have, target)
    if new.empty:
        return daily, blocks, False
    new = new.dropna(subset=["mcp_rs_mwh"])
    blocks = pd.concat([blocks, new], ignore_index=True).drop_duplicates(
        subset=["market", "date", "block"], keep="last")
    nd = (new.groupby(["market", "date"])
             .agg(avg_mcp_rs_kwh=("mcp_rs_mwh", "mean"), n=("mcp_rs_mwh", "size"))
             .reset_index())
    nd = nd[nd["n"] >= 90].drop(columns="n")
    nd["avg_mcp_rs_kwh"] /= 1000.0
    daily = pd.concat([daily, nd], ignore_index=True).drop_duplicates(
        subset=["market", "date"], keep="last").sort_values(["market", "date"])
    return daily, blocks, True


# ==================== FORECAST MODELS ====================
def weekly_indices(prices, dows):
    s, c = [0.0] * 7, [0] * 7
    for p, d in zip(prices, dows):
        s[d] += p
        c[d] += 1
    overall = sum(prices) / len(prices)
    return [(s[i] / c[i]) / overall if c[i] else 1.0 for i in range(7)]


def stdev(a):
    if not a:
        return 0.0
    m = sum(a) / len(a)
    return math.sqrt(sum((x - m) ** 2 for x in a) / len(a))


def f_seasonal_naive(prices, dows, horizon, feats=None):
    res = [prices[i] - prices[i - 7] for i in range(7, len(prices))]
    fc = [prices[len(prices) - 7 + h % 7] for h in range(horizon)]
    return fc, stdev(res)


def f_ma7(prices, dows, horizon, feats=None):
    idx = weekly_indices(prices, dows)
    ma = sum(prices[-7:]) / 7
    res = [prices[i] - (sum(prices[i - 7:i]) / 7) * idx[dows[i]]
           for i in range(7, len(prices))]
    fc = [ma * idx[(dows[-1] + h) % 7] for h in range(1, horizon + 1)]
    return fc, stdev(res)


def hw_engine(prices, dows, horizon, alpha, beta, phi):
    """Holt's method with weekly indices and damped trend (phi<1 tames overshoot)."""
    idx = weekly_indices(prices, dows)
    de = [p / idx[d] for p, d in zip(prices, dows)]
    level, trend = de[0], de[1] - de[0]
    res = []
    for i in range(1, len(de)):
        res.append((de[i] - (level + phi * trend)) * idx[dows[i]])
        nl = alpha * de[i] + (1 - alpha) * (level + phi * trend)
        trend = beta * (nl - level) + (1 - beta) * phi * trend
        level = nl
    fc = []
    damp = 0.0
    for h in range(1, horizon + 1):
        damp += phi ** h
        fc.append(max(0.5, (level + trend * damp) * idx[(dows[-1] + h) % 7]))
    return fc, stdev(res)


def f_holt_winters(prices, dows, horizon, feats=None):
    return hw_engine(prices, dows, horizon, 0.3, 0.05, 1.0)


def f_adaptive_hw(prices, dows, horizon, feats=None):
    # Backtested on real DAM data: fast smoothing + damped trend cuts D+1 MAPE
    # from ~20% to ~17% and halves the error in fast-moving weeks.
    return hw_engine(prices, dows, horizon, 0.55, 0.15, 0.85)


def ols_solve(X, Y, ridge=1e-6):
    """Least squares via normal equations + Gaussian elimination (pure python)."""
    k = len(X[0])
    A = [[sum(x[i] * x[j] for x in X) + (ridge if i == j else 0)
          for j in range(k)] for i in range(k)]
    b = [sum(x[i] * y for x, y in zip(X, Y)) for i in range(k)]
    for col in range(k):
        piv = max(range(col, k), key=lambda r: abs(A[r][col]))
        if abs(A[piv][col]) < 1e-12:
            return None
        A[col], A[piv] = A[piv], A[col]
        b[col], b[piv] = b[piv], b[col]
        for r in range(col + 1, k):
            f = A[r][col] / A[col][col]
            for c in range(col, k):
                A[r][c] -= f * A[col][c]
            b[r] -= f * b[col]
    beta = [0.0] * k
    for r in range(k - 1, -1, -1):
        beta[r] = (b[r] - sum(A[r][c] * beta[c] for c in range(r + 1, k))) / A[r][r]
    return beta


def f_fundamentals(prices, dows, horizon, feats=None):
    """Regress deseasonalized price on all-India energy met + solar + wind (MU).
    Future fundamentals use 3-day persistence. Falls back to adaptive HW when
    fundamentals coverage is thin."""
    if not feats or sum(1 for f in feats if f) < max(30, len(prices) * 0.6):
        return f_adaptive_hw(prices, dows, horizon)
    idx = weekly_indices(prices, dows)
    de = [p / idx[d] for p, d in zip(prices, dows)]
    pairs = [(f, y) for f, y in zip(feats, de) if f]
    X = [[1.0, f[0] / 1000, f[1] / 100, f[2] / 100] for f, _ in pairs]
    Y = [y for _, y in pairs]
    beta = ols_solve(X, Y)
    if beta is None:
        return f_adaptive_hw(prices, dows, horizon)
    dot = lambda x: sum(a * b for a, b in zip(x, beta))    # noqa: E731
    res = [y - dot(x) for x, y in zip(X, Y)]
    lastf = [f for f in feats if f][-3:]
    ff = [sum(v[i] for v in lastf) / len(lastf) for i in range(3)]
    base = dot([1.0, ff[0] / 1000, ff[1] / 100, ff[2] / 100])
    fc = [max(0.5, base * idx[(dows[-1] + h) % 7]) for h in range(1, horizon + 1)]
    return fc, stdev(res)


def f_hybrid(prices, dows, horizon, feats=None):
    """Mean of adaptive HW (captures momentum) and fundamentals regression
    (captures demand/RE level) — often steadier than either alone."""
    f1, s1 = f_adaptive_hw(prices, dows, horizon)
    f2, s2 = f_fundamentals(prices, dows, horizon, feats)
    return [(a + b) / 2 for a, b in zip(f1, f2)], (s1 + s2) / 2


def f_linreg(prices, dows, horizon, feats=None):
    idx = weekly_indices(prices, dows)
    de = [p / idx[d] for p, d in zip(prices, dows)]
    n = len(de)
    sx = sum(range(n)); sy = sum(de)
    sxy = sum(x * y for x, y in enumerate(de)); sxx = sum(x * x for x in range(n))
    slope = (n * sxy - sx * sy) / (n * sxx - sx * sx)
    inter = (sy - slope * sx) / n
    res = [(y - (inter + slope * x)) * idx[dows[x]] for x, y in enumerate(de)]
    fc = [max(0.5, (inter + slope * (n - 1 + h)) * idx[(dows[-1] + h) % 7])
          for h in range(1, horizon + 1)]
    return fc, stdev(res)


MODELS = {
    "Adaptive Holt-Winters (fast + damped)": f_adaptive_hw,
    "Hybrid — Adaptive HW + Fundamentals": f_hybrid,
    "Fundamentals Regression (demand + RE)": f_fundamentals,
    "Holt-Winters (trend + weekly seasonality)": f_holt_winters,
    "Seasonal Naive (same day last week)": f_seasonal_naive,
    "7-Day Moving Average": f_ma7,
    "Linear Trend + Weekly Regression": f_linreg,
}
AUTO_MODEL = "Auto — pick backtest winner"


def build_feats(dates, fund):
    """(energy_met, solar, wind) tuple per date, forward-filled; None when unknown."""
    if fund is None or fund.empty:
        return [None] * len(dates)
    lookup = {r.date: (r.energy_met_mu, r.solar_mu, r.wind_mu)
              for r in fund.itertuples() if pd.notna(r.energy_met_mu)}
    feats, last = [], None
    for d in dates:
        v = lookup.get(d)
        if v is not None and all(pd.notna(x) for x in v):
            last = tuple(float(x) for x in v)
        feats.append(last)
    return feats


@st.cache_data(ttl=3600, show_spinner=False)
def rolling_backtest(daily, fund, market, window, n_days=30):
    """Walk-forward D+1 backtest of every model over the last n_days.
    Returns {model: {'dates', 'actual', 'forecast', 'mape', 'mae'}}."""
    ser = daily[daily["market"] == market].sort_values("date")
    prices = ser["avg_mcp_rs_kwh"].tolist()
    dts = ser["date"].tolist()
    dows = [d.weekday() for d in dts]
    feats = build_feats(dts, fund)
    out = {}
    start = max(window, len(prices) - n_days)
    for name, fn in MODELS.items():
        dates, actual, fcs = [], [], []
        for i in range(start, len(prices)):
            fc, _ = fn(prices[i - window:i], dows[i - window:i], 1,
                       feats[i - window:i])
            dates.append(dts[i])
            actual.append(prices[i])
            fcs.append(fc[0])
        apes = [abs(f - a) / a * 100 for f, a in zip(fcs, actual)]
        errs = [abs(f - a) for f, a in zip(fcs, actual)]
        out[name] = {"dates": dates, "actual": actual, "forecast": fcs,
                     "mape": sum(apes) / len(apes), "mae": sum(errs) / len(errs)}
    return out


# ==================== PIPELINE ====================
def intraday_shape(blocks, market, shape_days, solar_pct, peak_pct):
    b = blocks[blocks["market"] == market]
    recent_dates = sorted(b["date"].unique())[-shape_days:]
    prof = (b[b["date"].isin(recent_dates)]
            .groupby("block")["mcp_rs_mwh"].mean())
    prof = prof.reindex(range(1, 97)).interpolate().bfill().ffill()
    shape = (prof / prof.mean()).tolist()
    out = []
    for i, v in enumerate(shape):
        h = i / 4
        if 10 <= h < 17:
            v *= 1 - (solar_pct - 100) / 100 * 0.30
        if 18.5 <= h < 23:
            v *= 1 + (peak_pct - 100) / 100 * 0.50
        out.append(v)
    mean = sum(out) / len(out)
    return [v / mean for v in out]


def run_forecast(daily, blocks, fund, market, model_name, window, horizon, z,
                 demand, solar, fuel, peak_stress, cap, floor, shape_days):
    ser = daily[daily["market"] == market].sort_values("date").tail(window)
    prices = ser["avg_mcp_rs_kwh"].tolist()
    dows = [d.weekday() for d in ser["date"]]
    feats = build_feats(ser["date"].tolist(), fund)
    fc, sigma = MODELS[model_name](prices, dows, horizon, feats)
    adj = (1 + demand / 100) * (1 + (fuel - 100) / 100 * 0.35)
    shape = intraday_shape(blocks, market, shape_days, solar, peak_stress)
    clamp = lambda x: min(cap, max(floor, x))            # noqa: E731
    last_date = ser["date"].max()
    days = []
    for i, v in enumerate(fc):
        avg = v * adj
        band = sigma * math.sqrt(i + 1) * z
        days.append({
            "date": last_date + timedelta(days=i + 1),
            "avg": clamp(avg), "lo": clamp(avg - band), "hi": clamp(avg + band),
            "blocks": [clamp(avg * s) for s in shape],
            "b_lo": [clamp((avg - band) * s) for s in shape],
            "b_hi": [clamp((avg + band) * s) for s in shape],
        })
    trail30 = daily[daily["market"] == market].sort_values("date").tail(30)["avg_mcp_rs_kwh"].mean()
    return days, trail30, ser


def block_time(b):
    h1, m1 = divmod((b - 1) * 15, 60)
    h2, m2 = divmod(b * 15, 60)
    return f"{h1:02d}:{m1:02d} - {h2 % 24:02d}:{m2:02d}"


def slot_tag(b):
    h = (b - 1) / 4
    if 18 <= h < 23:
        return "Peak"
    if 10 <= h < 17:
        return "Solar"
    if 5 <= h < 10:
        return "Morning"
    if h >= 23 or h < 5:
        return "Night"
    return "Normal"


# ==================== UI ====================
st.markdown(
    f"""<div style="background:linear-gradient(135deg,{NAVY} 0%,#0f2744 100%);
    padding:16px 26px;border-radius:10px;margin-bottom:14px">
    <span style="color:#fff;font-size:24px;font-weight:700">⚡ <span
    style="color:#60a5fa">PowerLogic</span> — Market Price Forecasting</span><br>
    <span style="color:#94a3b8;font-size:13px">DAM / RTM / G-DAM · real IEX data ·
    96-block intraday analysis</span></div>""", unsafe_allow_html=True)

daily, blocks, fund, source = load_data()
daily, blocks, topped = top_up(daily, blocks)

with st.sidebar:
    st.header("Forecast Parameters")
    market = st.radio("Market segment", list(MARKETS), horizontal=True,
                      format_func=lambda k: {"DAM": "DAM", "RTM": "RTM", "GDAM": "G-DAM"}[k])
    model_name = st.selectbox("Forecast model", [AUTO_MODEL, *MODELS])
    horizon = st.select_slider("Forecast horizon (days)", [1, 7, 15, 30], value=7)
    window = st.select_slider("History window (days)", [90, 180, 365], value=90)
    ci = st.select_slider("Confidence band", ["80%", "90%", "95%"], value="80%")
    z = {"80%": 1.28, "90%": 1.64, "95%": 1.96}[ci]
    shape_days = st.slider("Intraday shape window (days)", 5, 60, 7,
                           help="How many recent days build the 96-block profile. "
                                "Shorter adapts faster to season changes.")
    st.subheader("Scenario adjustments")
    demand = st.slider("Demand growth (%)", -10.0, 15.0, 0.0, 0.5)
    solar = st.slider("Solar / RE availability (%)", 60, 140, 100, 5)
    fuel = st.slider("Fuel / import price factor (%)", 80, 130, 100, 5)
    peak_stress = st.slider("Evening peak stress (%)", 80, 150, 100, 5)
    st.subheader("Price caps (CERC)")
    cap = st.number_input("Ceiling (Rs/kWh)", 1.0, 20.0, 10.0, 0.5)
    floor = st.number_input("Floor (Rs/kWh)", 0.0, 5.0, 0.0, 0.5)

bt = rolling_backtest(daily, fund, market, window)
if model_name == AUTO_MODEL:
    model_name = min(bt, key=lambda m: bt[m]["mape"])
    st.info(f"🏆 Auto-selected **{model_name}** — lowest MAPE "
            f"({bt[model_name]['mape']:.1f}%) in the 30-day walk-forward backtest.")

days, trail30, hist = run_forecast(daily, blocks, fund, market, model_name, window,
                                   horizon, z, demand, solar, fuel, peak_stress,
                                   cap, floor, shape_days)

last_data_date = daily[daily["market"] == market]["date"].max()
st.caption(f"Data source: **{source}**{' + live IEX top-up' if topped else ''} · "
           f"{MARKETS[market]} history to **{last_data_date:%d %b %Y}** · "
           f"{len(daily[daily['market'] == market]):,} days loaded")

# ---- summary metrics
avg_fc = sum(d["avg"] for d in days) / len(days)
flat = [(v, d, b) for d in days for b, v in enumerate(d["blocks"], 1)]
peak_val, peak_day, peak_b = max(flat, key=lambda t: t[0])
min_val, min_day, min_b = min(flat, key=lambda t: t[0])
chg = (avg_fc - trail30) / trail30 * 100
c1, c2, c3, c4 = st.columns(4)
c1.metric("Forecast avg (Rs/kWh)", f"{avg_fc:.2f}", f"{horizon} day(s)", delta_color="off")
c2.metric("Peak block price", f"{peak_val:.2f}",
          f"{peak_day['date']:%d %b} · {block_time(peak_b)}", delta_color="off")
c3.metric("Min block price", f"{min_val:.2f}",
          f"{min_day['date']:%d %b} · {block_time(min_b)}", delta_color="off")
c4.metric("vs trailing 30-day avg", f"{chg:+.1f}%", f"trailing {trail30:.2f} Rs/kWh",
          delta_color="inverse")

# ---- market comparison chips
cols = st.columns(3)
for col, mk in zip(cols, MARKETS):
    d2, _, _ = run_forecast(daily, blocks, fund, mk, model_name, window, horizon, z,
                            demand, solar, fuel, peak_stress, cap, floor, shape_days)
    a = sum(x["avg"] for x in d2) / len(d2)
    col.metric({"DAM": "DAM avg", "RTM": "RTM avg", "GDAM": "G-DAM avg"}[mk],
               f"₹{a:.2f}/kWh")

# ---- trend chart
show_hist = hist.tail(45)
fig = go.Figure()
fig.add_trace(go.Scatter(x=list(show_hist["date"]), y=list(show_hist["avg_mcp_rs_kwh"]),
                         name="Historical", line=dict(color=BLUE, width=2),
                         fill="tozeroy", fillcolor="rgba(37,99,235,0.07)"))
fx = [show_hist["date"].iloc[-1]] + [d["date"] for d in days]
fig.add_trace(go.Scatter(x=fx, y=[show_hist["avg_mcp_rs_kwh"].iloc[-1]] + [d["hi"] for d in days],
                         name="hi", line=dict(width=0), showlegend=False, hoverinfo="skip"))
fig.add_trace(go.Scatter(x=fx, y=[show_hist["avg_mcp_rs_kwh"].iloc[-1]] + [d["lo"] for d in days],
                         name=f"{ci} band", line=dict(width=0), fill="tonexty",
                         fillcolor="rgba(16,185,129,0.15)"))
fig.add_trace(go.Scatter(x=fx, y=[show_hist["avg_mcp_rs_kwh"].iloc[-1]] + [d["avg"] for d in days],
                         name="Forecast", line=dict(color=GREEN, width=2.5, dash="dash")))
fig.update_layout(title=f"Daily Avg MCP — {MARKETS[market]} (IEX)",
                  yaxis_title="Rs/kWh", height=380, margin=dict(t=50, b=10),
                  legend=dict(orientation="h", y=1.12), hovermode="x unified")
st.plotly_chart(fig, width="stretch")

# ---- intraday profile
sel = st.selectbox("Intraday profile — forecast day",
                   range(len(days)), format_func=lambda i: f"{days[i]['date']:%a, %d %b %Y}")
d = days[sel]
xt = [block_time(b) for b in range(1, 97)]
fig2 = go.Figure()
fig2.add_trace(go.Scatter(x=xt, y=d["b_hi"], line=dict(width=0), showlegend=False, hoverinfo="skip"))
fig2.add_trace(go.Scatter(x=xt, y=d["b_lo"], line=dict(width=0), fill="tonexty",
                          fillcolor="rgba(245,158,11,0.15)", name=f"{ci} band"))
fig2.add_trace(go.Scatter(x=xt, y=d["blocks"], name="Forecast",
                          line=dict(color=NAVY, width=2),
                          fill="tozeroy", fillcolor="rgba(30,58,95,0.08)"))
fig2.update_layout(title=f"96 Time Blocks (15-min) — {d['date']:%d %b %Y}",
                   yaxis_title="Rs/kWh", height=340, margin=dict(t=50, b=10),
                   xaxis=dict(tickmode="array", tickvals=xt[::8],
                              ticktext=[t[:5] for t in xt[::8]]),
                   legend=dict(orientation="h", y=1.14), hovermode="x unified")
st.plotly_chart(fig2, width="stretch")

# ---- forecast accuracy (backtest)
st.markdown("### 📊 Forecast Accuracy — 30-day walk-forward backtest")
st.caption("Every point is a true D+1 forecast made using only data available "
           "before that day, compared against the actual daily average MCP.")

sel_bt = bt[model_name]
best = min(bt, key=lambda m: bt[m]["mape"])
a1, a2, a3 = st.columns(3)
a1.metric("MAPE — " + model_name.split(" (")[0].split(" —")[0],
          f"{sel_bt['mape']:.1f}%", "mean abs % error", delta_color="off")
a2.metric("MAE", f"{sel_bt['mae']:.3f} Rs/kWh", "mean abs error", delta_color="off")
a3.metric("Best model (30d)", best.split(" (")[0].split(" —")[0],
          f"MAPE {bt[best]['mape']:.1f}%", delta_color="off")

ACTUAL_C, FC_C, ML_C = "#2563eb", "#d97706", "#0d9488"

# ML (LightGBM) results precomputed locally by models/dam_ml.py --emit
ml = None
ML_CSV = BASE_DIR / "data" / "ml_forecast.csv"
if market == "DAM" and ML_CSV.exists():
    mldf = pd.read_csv(ML_CSV)
    mldf["date"] = pd.to_datetime(mldf["date"]).dt.date
    ml_bt = mldf[mldf["kind"] == "backtest"].dropna(subset=["actual"])
    nd = mldf[mldf["kind"] == "next_day"]
    if len(ml_bt) >= 10:
        ape = (ml_bt["pred"] - ml_bt["actual"]).abs() / ml_bt["actual"]
        hyb = bt["Hybrid — Adaptive HW + Fundamentals"]
        hyb_by_date = dict(zip(hyb["dates"], hyb["forecast"]))
        ens = ml_bt[ml_bt["date"].isin(hyb_by_date)].copy()
        ens["pred"] = (ens["pred"] + ens["date"].map(hyb_by_date)) / 2
        eape = (ens["pred"] - ens["actual"]).abs() / ens["actual"]
        ml = {"bt": ml_bt, "mape": ape.mean() * 100,
              "mae": (ml_bt["pred"] - ml_bt["actual"]).abs().mean(),
              "ens_mape": eape.mean() * 100,
              "ens_mae": (ens["pred"] - ens["actual"]).abs().mean(),
              "next": nd.iloc[0] if not nd.empty else None}

fig3 = go.Figure()
fig3.add_trace(go.Scatter(x=sel_bt["dates"], y=sel_bt["actual"], name="Actual",
                          line=dict(color=ACTUAL_C, width=2)))
fig3.add_trace(go.Scatter(x=sel_bt["dates"], y=sel_bt["forecast"],
                          name="D+1 forecast", line=dict(color=FC_C, width=2, dash="dash")))
if ml:
    fig3.add_trace(go.Scatter(x=list(ml["bt"]["date"]), y=list(ml["bt"]["pred"]),
                              name="ML — LightGBM", line=dict(color=ML_C, width=2, dash="dot")))
fig3.update_layout(title=f"Actual vs D+1 Forecast — {MARKETS[market]}",
                   yaxis_title="Rs/kWh", height=340, margin=dict(t=50, b=10),
                   legend=dict(orientation="h", y=1.14), hovermode="x unified")
st.plotly_chart(fig3, width="stretch")

lb_rows = [{"Model": m.split(" —")[0], "MAPE (%)": round(v["mape"], 1),
            "MAE (Rs/kWh)": round(v["mae"], 3)} for m, v in bt.items()]
if ml:
    lb_rows.append({"Model": "ML — LightGBM (demand+RE+weather)",
                    "MAPE (%)": round(ml["mape"], 1),
                    "MAE (Rs/kWh)": round(ml["mae"], 3)})
    lb_rows.append({"Model": "Ensemble — ML + Hybrid",
                    "MAPE (%)": round(ml["ens_mape"], 1),
                    "MAE (Rs/kWh)": round(ml["ens_mae"], 3)})
lb = pd.DataFrame(lb_rows).sort_values("MAPE (%)").reset_index(drop=True)
st.dataframe(lb, width="stretch", hide_index=True)

if ml and ml["next"] is not None:
    nd = ml["next"]
    m1, m2 = st.columns(2)
    m1.metric(f"ML next-day forecast ({nd['date']:%d %b})",
              f"₹{nd['pred']:.2f}/kWh", "LightGBM, trained locally", delta_color="off")
    hyb_next = next((d["avg"] for d in days if d["date"] == nd["date"]), None)
    if hyb_next:
        m2.metric("Ensemble (ML + selected model)",
                  f"₹{(nd['pred'] + hyb_next) / 2:.2f}/kWh",
                  f"mean with {model_name.split(' (')[0].split(' —')[0]}",
                  delta_color="off")
    st.caption("ML rows come from data/ml_forecast.csv, regenerated by the daily "
               "scheduled task (models/dam_ml.py --emit). DAM only for now.")

# block-level check on the latest fully-traded day
bmk = blocks[blocks["market"] == market]
last_day = bmk.groupby("date")["block"].count()
last_day = last_day[last_day >= 90].index.max()
prior_daily = daily[(daily["market"] == market) & (daily["date"] < last_day)]
if len(prior_daily) >= window:
    ser_p = prior_daily.sort_values("date").tail(window)
    fc1, _ = MODELS[model_name](ser_p["avg_mcp_rs_kwh"].tolist(),
                                [d.weekday() for d in ser_p["date"]], 1,
                                build_feats(ser_p["date"].tolist(), fund))
    shp = intraday_shape(blocks[blocks["date"] < last_day], market,
                         shape_days, 100, 100)
    pred_b = [fc1[0] * s for s in shp]
    act_b = (bmk[bmk["date"] == last_day].sort_values("block")["mcp_rs_mwh"] / 1000).tolist()
    n = min(len(pred_b), len(act_b))
    b_mape = sum(abs(p - a) / a for p, a in zip(pred_b[:n], act_b[:n])) / n * 100
    b_mae = sum(abs(p - a) for p, a in zip(pred_b[:n], act_b[:n])) / n
    st.markdown(f"**Block-level check — {last_day:%a, %d %b %Y}** "
                f"(forecast made from data before that day)")
    b1, b2 = st.columns(2)
    b1.metric("Block MAPE", f"{b_mape:.1f}%",
              "inflated by near-zero solar-hour prices", delta_color="off")
    b2.metric("Block MAE", f"{b_mae:.3f} Rs/kWh", "96 blocks", delta_color="off")
    fig4 = go.Figure()
    fig4.add_trace(go.Scatter(x=xt[:n], y=act_b[:n], name="Actual",
                              line=dict(color=ACTUAL_C, width=2)))
    fig4.add_trace(go.Scatter(x=xt[:n], y=pred_b[:n], name="Forecast",
                              line=dict(color=FC_C, width=2, dash="dash")))
    fig4.update_layout(title=f"96-Block Forecast vs Actual — {last_day:%d %b %Y}",
                       yaxis_title="Rs/kWh", height=340, margin=dict(t=50, b=10),
                       xaxis=dict(tickmode="array", tickvals=xt[:n:8],
                                  ticktext=[t[:5] for t in xt[:n:8]]),
                       legend=dict(orientation="h", y=1.14), hovermode="x unified")
    st.plotly_chart(fig4, width="stretch")

# ---- demand & RE fundamentals (Grid-India NLDC PSP reports)
if fund is not None and not fund.empty:
    with st.expander("🇮🇳 All-India Demand & RE Generation (Grid-India daily PSP reports)",
                     expanded=False):
        fshow = fund.sort_values("date").tail(90)
        fc1, fc2 = st.columns(2)
        last = fshow.iloc[-1]
        fc1.metric("Energy met (latest day)", f"{last['energy_met_mu']:,.0f} MU",
                   f"{last['date']:%d %b %Y}", delta_color="off")
        pk = last.get("peak_demand_mw")
        fc2.metric("Peak demand met", f"{pk:,.0f} MW" if pd.notna(pk) else "—",
                   f"{last['date']:%d %b %Y}", delta_color="off")
        figd = go.Figure()
        figd.add_trace(go.Scatter(x=list(fshow["date"]), y=list(fshow["energy_met_mu"]),
                                  name="Energy met", line=dict(color=ACTUAL_C, width=2),
                                  fill="tozeroy", fillcolor="rgba(37,99,235,0.07)"))
        figd.update_layout(title="All-India Energy Met (MU/day)", yaxis_title="MU",
                           height=280, margin=dict(t=45, b=10), hovermode="x unified")
        st.plotly_chart(figd, width="stretch")
        figr = go.Figure()
        figr.add_trace(go.Scatter(x=list(fshow["date"]), y=list(fshow["solar_mu"]),
                                  name="Solar", line=dict(color="#d97706", width=2)))
        figr.add_trace(go.Scatter(x=list(fshow["date"]), y=list(fshow["wind_mu"]),
                                  name="Wind", line=dict(color="#0d9488", width=2)))
        figr.update_layout(title="RE Generation (MU/day)", yaxis_title="MU",
                           height=280, margin=dict(t=45, b=10),
                           legend=dict(orientation="h", y=1.16), hovermode="x unified")
        st.plotly_chart(figr, width="stretch")
        st.caption("Source: Grid-India (NLDC) Daily PSP Reports, scraped into "
                   "daily_fundamentals. These series feed the Fundamentals "
                   "Regression and Hybrid forecast models.")

# ---- block table + CSV
tbl = pd.DataFrame({
    "Block": range(1, 97),
    "Time": xt,
    "Price (Rs/kWh)": [round(v, 3) for v in d["blocks"]],
    "Low": [round(v, 3) for v in d["b_lo"]],
    "High": [round(v, 3) for v in d["b_hi"]],
    "Slot": [slot_tag(b) for b in range(1, 97)],
})
with st.expander(f"Block-wise forecast table — {d['date']:%a, %d %b %Y}", expanded=False):
    st.dataframe(tbl, width="stretch", height=380, hide_index=True)

full = pd.DataFrame([{"Market": market, "Date": dd["date"], "Block": b,
                      "Time": block_time(b), "Forecast (Rs/kWh)": round(v, 3),
                      "Low": round(dd["b_lo"][b - 1], 3), "High": round(dd["b_hi"][b - 1], 3),
                      "Slot": slot_tag(b)}
                     for dd in days for b, v in enumerate(dd["blocks"], 1)])
st.download_button("⬇ Download full forecast CSV",
                   full.to_csv(index=False).encode(),
                   f"{market}_forecast_{days[0]['date']}.csv", "text/csv")

st.caption("** Forecast computed on real IEX block-wise prices "
           "(scraped market-snapshot data). Block prices = forecast daily avg × "
           "recent actual intraday profile, clamped to CERC floor/ceiling. "
           "Indicative only — not a substitute for exchange-published MCP.")
