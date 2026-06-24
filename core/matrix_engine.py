"""
SqueezeOS Core Mathematical Engine
Proprietary EMA Ribbon Matrix — configuration loaded from environment.
Zero simulation. All data from live CCXT feed.
"""
import os
import numpy as np
import pandas as pd
import ccxt


def _load_periods() -> list[int]:
    raw = os.getenv("SML_EMA_PERIODS", "")
    if raw:
        return [int(x.strip()) for x in raw.split(",")]
    raise RuntimeError("SML_EMA_PERIODS env var not set")

EMA_PERIODS = _load_periods()
_EXCHANGE: ccxt.Exchange | None = None


def _get_exchange() -> ccxt.Exchange:
    global _EXCHANGE
    if _EXCHANGE is None:
        exchange_id = os.getenv("CCXT_EXCHANGE", "binance")
        _EXCHANGE = getattr(ccxt, exchange_id)({"enableRateLimit": True})
    return _EXCHANGE


def fetch_ohlcv(symbol: str, timeframe: str = "15m", limit: int = 500) -> pd.DataFrame:
    ex = _get_exchange()
    raw = ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    df = pd.DataFrame(raw, columns=["Timestamp", "Open", "High", "Low", "Close", "Volume"])
    df["Timestamp"] = pd.to_datetime(df["Timestamp"], unit="ms")
    df.set_index("Timestamp", inplace=True)
    return df


def compute_sml_matrix(df: pd.DataFrame) -> pd.DataFrame:
    """
    Computes the proprietary 5-EMA ribbon grid.
    Failsafe: No drifting, no smoothing approximations, no lag offsets.
    """
    for p in EMA_PERIODS:
        df[f"EMA_{p}"] = df["Close"].ewm(span=p, adjust=False).mean()
    return df


def evaluate_execution_intent(
    df_row: pd.Series,
    current_position: float,
    average_entry_price: float,
    drawdown_tier: int,
    max_tiers: int = 3,
) -> tuple[str, float | None]:
    """
    Core execution logic governing non-custodial capital protection.
    Returns (intent_signal, execution_price | None)
    """
    close       = df_row["Close"]
    ema_55      = df_row["EMA_55"]
    ema_89      = df_row["EMA_89"]
    ema_144     = df_row["EMA_144"]
    ema_233     = df_row["EMA_233"]
    ema_365     = df_row["EMA_365"]

    bullish_ribbon = ema_55 > ema_89 > ema_144 > ema_233
    above_anchor   = close > ema_365

    if current_position == 0:
        if bullish_ribbon and above_anchor and close > ema_55:
            return "EXECUTE_INITIAL_ENTRY", close

    else:
        pnl = (close - average_entry_price) / average_entry_price

        dd_step = float(os.getenv("SML_DRAWDOWN_STEP", "0"))
        if not dd_step:
            raise RuntimeError("SML_DRAWDOWN_STEP env var not set")
        target_drawdown = -dd_step * (drawdown_tier + 1)

        if (
            pnl < target_drawdown
            and drawdown_tier < max_tiers
            and bullish_ribbon
            and above_anchor
        ):
            return "EXECUTE_TRANCHE_AVG_DOWN", close

        profit_target = float(os.getenv("SML_PROFIT_TARGET", "0"))
        if not profit_target:
            raise RuntimeError("SML_PROFIT_TARGET env var not set")
        elif close > average_entry_price * profit_target:
            return "EXECUTE_TAKE_PROFIT_EXIT", close

        elif not above_anchor:
            return "EXECUTE_STRUCTURAL_STOP_OUT", close

    return "MAINTAIN_STATE", None


def get_live_intent(
    symbol: str = "ETH/USDT",
    timeframe: str = "15m",
    current_position: float = 0.0,
    average_entry_price: float = 0.0,
    drawdown_tier: int = 0,
) -> dict:
    df = fetch_ohlcv(symbol, timeframe=timeframe, limit=500)
    df = compute_sml_matrix(df)
    latest = df.iloc[-1]
    intent, price = evaluate_execution_intent(
        latest, current_position, average_entry_price, drawdown_tier
    )
    ribbon = {f"ribbon_{i+1}": float(latest[f"EMA_{p}"]) for i, p in enumerate(EMA_PERIODS)}
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "close": float(latest["Close"]),
        **ribbon,
        "intent": intent,
        "execution_price": float(price) if price is not None else None,
    }


if __name__ == "__main__":
    result = get_live_intent("ETH/USDT", "15m")
    print(result)
