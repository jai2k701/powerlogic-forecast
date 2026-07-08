"""DAM block-price ML forecaster: feature engineering + LightGBM + walk-forward eval.

Rows = (delivery_day, block). Target = log(MCP Rs/kWh). All features are
strictly D-1-available (price/fundamentals lags) or day-ahead-known (weather
forecast, calendar), so the backtest is an honest D+1 simulation.

Usage: python models/dam_ml.py [--eval-days 60] [--market DAM]
"""
import argparse
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import holidays
import lightgbm as lgb
import numpy as np
import pandas as pd

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "iex_prices.db"


def load_frames(market):
    con = sqlite3.connect(DB_PATH)
    px = pd.read_sql_query(
        """SELECT price_date AS d, time_block AS b, mcp_rs_mwh/1000.0 AS p
           FROM market_prices WHERE market=? AND mcp_rs_mwh IS NOT NULL
           ORDER BY price_date, time_block""", con, params=(market,))
    fund = pd.read_sql_query(
        """SELECT fund_date AS d, energy_met_mu, solar_mu, wind_mu, hydro_mu
           FROM daily_fundamentals WHERE energy_met_mu IS NOT NULL""", con)
    wx = pd.read_sql_query(
        "SELECT wx_date AS d, tmax_c, tmin_c FROM daily_weather", con)
    con.close()
    for f in (px, fund, wx):
        f["d"] = pd.to_datetime(f["d"])
    return px, fund, wx


def build_features(px, fund, wx):
    wide = px.pivot(index="d", columns="b", values="p").sort_index()
    wide = wide.asfreq("D")                       # explicit gaps
    davg = wide.mean(axis=1)

    frames = []
    for lag in (1, 2, 3, 7):
        f = wide.shift(lag).stack(future_stack=True).rename(f"lag{lag}").to_frame()
        frames.append(f)
    feat = pd.concat(frames, axis=1)

    roll7 = wide.shift(1).rolling(7, min_periods=4).mean()
    std7 = wide.shift(1).rolling(7, min_periods=4).std()
    feat["block_roll7"] = roll7.stack(future_stack=True)
    feat["block_std7"] = std7.stack(future_stack=True)

    feat = feat.reset_index().rename(columns={"d": "day", "b": "block"})
    day_frame = pd.DataFrame({"day": davg.index})
    day_frame["davg_lag1"] = davg.shift(1).values
    day_frame["davg_lag7"] = davg.shift(7).values
    day_frame["davg_roll7"] = davg.shift(1).rolling(7, min_periods=4).mean().values

    fund = fund.set_index("d").asfreq("D").ffill(limit=10)
    for c in ("energy_met_mu", "solar_mu", "wind_mu", "hydro_mu"):
        day_frame[f"{c}_lag1"] = fund[c].shift(1).reindex(day_frame["day"]).values
        day_frame[f"{c}_3d"] = (fund[c].shift(1).rolling(3, min_periods=2)
                                .mean().reindex(day_frame["day"]).values)

    wx = wx.set_index("d").asfreq("D")
    day_frame["tmax"] = wx["tmax_c"].reindex(day_frame["day"]).values   # D-ahead forecastable
    day_frame["tmin"] = wx["tmin_c"].reindex(day_frame["day"]).values
    day_frame["tmax_lag1"] = wx["tmax_c"].shift(1).reindex(day_frame["day"]).values
    day_frame["tmax_3d"] = (wx["tmax_c"].shift(1).rolling(3, min_periods=2)
                            .mean().reindex(day_frame["day"]).values)
    day_frame["cdd"] = np.maximum(0, day_frame["tmax"] - 24)

    ind_hols = holidays.country_holidays("IN")
    day_frame["dow"] = day_frame["day"].dt.dayofweek
    day_frame["month"] = day_frame["day"].dt.month
    day_frame["is_weekend"] = (day_frame["dow"] >= 5).astype(int)
    day_frame["is_holiday"] = day_frame["day"].dt.date.map(
        lambda x: int(x in ind_hols))
    day_frame["doy_sin"] = np.sin(2 * np.pi * day_frame["day"].dt.dayofyear / 365)
    day_frame["doy_cos"] = np.cos(2 * np.pi * day_frame["day"].dt.dayofyear / 365)

    feat = feat.merge(day_frame, on="day", how="left")
    feat["shape_lag1"] = feat["lag1"] / feat["davg_lag1"]
    feat["blk_sin"] = np.sin(2 * np.pi * (feat["block"] - 1) / 96)
    feat["blk_cos"] = np.cos(2 * np.pi * (feat["block"] - 1) / 96)

    target = wide.stack(future_stack=True).rename("y").reset_index()
    target.columns = ["day", "block", "y"]
    feat = feat.merge(target, on=["day", "block"], how="left")
    return feat


FEATURES = ["lag1", "lag2", "lag3", "lag7", "block_roll7", "block_std7",
            "davg_lag1", "davg_lag7", "davg_roll7",
            "energy_met_mu_lag1", "energy_met_mu_3d", "solar_mu_lag1",
            "solar_mu_3d", "wind_mu_lag1", "wind_mu_3d", "hydro_mu_lag1",
            "hydro_mu_3d", "tmax", "tmin", "tmax_lag1", "tmax_3d", "cdd",
            "dow", "month", "is_weekend", "is_holiday", "doy_sin", "doy_cos",
            "block", "blk_sin", "blk_cos", "shape_lag1"]

PARAMS = dict(objective="l1", n_estimators=700, learning_rate=0.045,
              num_leaves=63, min_child_samples=40, subsample=0.9,
              colsample_bytree=0.9, reg_lambda=1.0, verbose=-1)


def metrics(actual, pred, label):
    actual, pred = np.asarray(actual), np.asarray(pred)
    mask = actual > 0
    ape = np.abs(pred[mask] - actual[mask]) / actual[mask]
    mape = ape.mean() * 100
    wmape = np.abs(pred - actual).sum() / actual.sum() * 100
    mae = np.abs(pred - actual).mean()
    print(f"  {label:<34} MAPE {mape:6.1f}%  WMAPE {wmape:5.1f}%  MAE {mae:.3f} Rs/kWh")
    return mape, wmape, mae


def walk_forward(feat, eval_days, retrain_every=7):
    days = sorted(feat.loc[feat["y"].notna(), "day"].unique())
    eval_set = days[-eval_days:]
    preds = []
    model = None
    for i, d in enumerate(eval_set):
        if model is None or i % retrain_every == 0:
            tr = feat[(feat["day"] < d) & feat["y"].notna() & feat["lag1"].notna()]
            model = lgb.LGBMRegressor(**PARAMS)
            model.fit(tr[FEATURES], np.log(tr["y"].clip(lower=0.05)))
        te = feat[(feat["day"] == d) & feat["y"].notna() & feat["lag1"].notna()]
        if te.empty:
            continue
        p = np.exp(model.predict(te[FEATURES])).clip(0.05, 10.0)
        out = te[["day", "block", "y", "lag1"]].copy()
        out["pred"] = p
        preds.append(out)
    return pd.concat(preds), model


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", default="DAM")
    ap.add_argument("--eval-days", type=int, default=60)
    args = ap.parse_args()

    px, fund, wx = load_frames(args.market)
    print(f"{args.market}: {px['d'].nunique()} days of block prices, "
          f"{len(fund)} fundamentals days, {len(wx)} weather days")
    feat = build_features(px, fund, wx)
    res, model = walk_forward(feat, args.eval_days)
    print(f"\nWalk-forward D+1, last {args.eval_days} days "
          f"({res['day'].min():%d-%b} .. {res['day'].max():%d-%b}), "
          f"{len(res)} block predictions:")
    print("Block level:")
    metrics(res["y"], res["pred"], "LightGBM")
    metrics(res["y"], res["lag1"], "Naive (same block yesterday)")
    print("Daily average level:")
    dres = res.groupby("day")[["y", "pred", "lag1"]].mean()
    metrics(dres["y"], dres["pred"], "LightGBM")
    metrics(dres["y"], dres["lag1"], "Naive (yesterday's daily avg)")

    imp = sorted(zip(FEATURES, model.feature_importances_),
                 key=lambda t: -t[1])[:12]
    print("\nTop features:", ", ".join(f"{k}({v})" for k, v in imp))


if __name__ == "__main__":
    main()
