"""
Backtest engine — runs the proprietary ribbon strategy against live Kraken OHLC.
All EMA periods loaded from env (SML_EMA_PERIODS). Never hardcoded.
"""
from __future__ import annotations
import os
import numpy as np
import pandas as pd


def _load_periods() -> list[int]:
    raw = os.getenv("SML_EMA_PERIODS", "")
    if raw:
        return [int(x.strip()) for x in raw.split(",")]
    raise RuntimeError("SML_EMA_PERIODS env var not set")


def _ema(s: pd.Series, p: int) -> pd.Series:
    return s.ewm(span=p, adjust=False).mean()


def _fetch_kraken(symbol: str, timeframe: str, limit: int) -> pd.DataFrame | None:
    try:
        import ccxt
    except ImportError:
        return None
    ex = ccxt.kraken({"enableRateLimit": True})
    try:
        ohlcv = ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    except Exception:
        return None
    if not ohlcv:
        return None
    df = pd.DataFrame(ohlcv, columns=["ts", "open", "high", "low", "close", "vol"])
    df["ret"] = df["close"].pct_change().fillna(0)
    return df


def _run_one(df: pd.DataFrame, symbol: str, periods: list[int]) -> dict:
    for p in periods:
        df[f"e{p}"] = _ema(df["close"], p)

    cols = [f"e{p}" for p in periods]
    stack_bull = pd.Series(True, index=df.index)
    stack_bear = pd.Series(True, index=df.index)
    for i in range(len(cols) - 1):
        stack_bull = stack_bull & (df[cols[i]] > df[cols[i + 1]])
        stack_bear = stack_bear & (df[cols[i]] < df[cols[i + 1]])

    df["position"] = 0
    df.loc[stack_bull, "position"] = 1
    df.loc[stack_bear, "position"] = -1

    warmup = max(periods)
    ev = df.iloc[warmup:].copy()
    ev["strat_ret"] = ev["position"].shift(1).fillna(0) * ev["ret"]

    eq = (1 + ev["strat_ret"]).cumprod()
    bh = (1 + ev["ret"]).cumprod()

    sharpe = (
        ev["strat_ret"].mean() / ev["strat_ret"].std() * np.sqrt(365)
        if ev["strat_ret"].std() > 0 else 0.0
    )
    max_dd = float((eq / eq.cummax() - 1).min())

    trades = []
    open_idx, open_pos = None, 0
    for i, row in ev.iterrows():
        if open_pos == 0 and row["position"] != 0:
            open_idx, open_pos = i, row["position"]
        elif open_pos != 0 and row["position"] != open_pos:
            entry = ev.loc[open_idx, "close"]
            exit_ = row["close"]
            trades.append((exit_ / entry - 1) * open_pos)
            open_idx, open_pos = (i, row["position"]) if row["position"] != 0 else (None, 0)

    tdf = pd.Series(trades)
    win_rate  = float((tdf > 0).mean()) if len(tdf) else 0.0
    avg_win   = float(tdf[tdf > 0].mean()) if (tdf > 0).any() else 0.0
    avg_loss  = float(tdf[tdf < 0].mean()) if (tdf < 0).any() else 0.0
    payoff    = round(abs(avg_win / avg_loss), 2) if avg_loss != 0 else None

    return {
        "symbol":        symbol,
        "bars":          len(ev),
        "n_trades":      len(tdf),
        "strat_return":  round(float(eq.iloc[-1] - 1) * 100, 2) if len(eq) else 0.0,
        "bh_return":     round(float(bh.iloc[-1] - 1) * 100, 2) if len(bh) else 0.0,
        "sharpe":        round(float(sharpe), 2),
        "max_dd":        round(max_dd * 100, 2),
        "win_rate":      round(win_rate * 100, 1),
        "payoff":        payoff,
        "beats_bh":      float(eq.iloc[-1] - 1) > float(bh.iloc[-1] - 1) if len(eq) else False,
    }


def run_backtest_symbols(symbols: list[str], timeframe: str = "1d", bars: int = 365) -> dict:
    periods = _load_periods()
    results = []
    errors  = []

    for sym in symbols:
        df = _fetch_kraken(sym, timeframe, bars)
        if df is None or len(df) < max(periods) + 10:
            errors.append({"symbol": sym, "error": "insufficient data or fetch failed"})
            continue
        try:
            r = _run_one(df, sym, periods)
            results.append(r)
        except Exception as e:
            errors.append({"symbol": sym, "error": str(e)})

    if not results:
        return {"error": "no results", "details": errors}

    n           = len(results)
    mean_strat  = round(np.mean([r["strat_return"] for r in results]), 2)
    mean_bh     = round(np.mean([r["bh_return"]    for r in results]), 2)
    mean_sharpe = round(np.mean([r["sharpe"]        for r in results]), 2)
    mean_dd     = round(np.mean([r["max_dd"]        for r in results]), 2)
    winners     = sum(1 for r in results if r["beats_bh"])

    return {
        "timeframe":     timeframe,
        "bars_per_symbol": bars,
        "symbol_count":  n,
        "results":       results,
        "aggregate": {
            "mean_strat_return": mean_strat,
            "mean_bh_return":    mean_bh,
            "mean_sharpe":       mean_sharpe,
            "mean_max_dd":       mean_dd,
            "symbols_beating_bh": f"{winners}/{n}",
            "alpha":             round(mean_strat - mean_bh, 2),
        },
        "errors": errors,
    }
