"""Price indicators shared by the regime detector, scanner and strategies.

All functions take candles oldest-first and never look at data after the row
they describe (no peeking into the future).
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
import pandas as pd

from scout.data import Candle


def candle_frame(candles: Sequence[Candle]) -> pd.DataFrame:
    """Candles -> DataFrame indexed by UTC open time, oldest first."""
    frame = pd.DataFrame(
        {
            "open": [c.open for c in candles],
            "high": [c.high for c in candles],
            "low": [c.low for c in candles],
            "close": [c.close for c in candles],
            "dollar_volume": [c.volume * c.close for c in candles],
        },
        index=pd.to_datetime([c.open_time_ms for c in candles], unit="ms", utc=True),
    )
    return frame.sort_index()


def moving_average(values: pd.Series, periods: int) -> pd.Series:
    """Simple moving average: the average of the last `periods` values."""
    return values.rolling(periods, min_periods=periods).mean()


def percent_change(values: pd.Series, periods: int) -> float:
    return (values.iloc[-1] / values.iloc[-1 - periods] - 1) * 100


def average_true_range(frame: pd.DataFrame, periods: int) -> pd.Series:
    """ATR: the typical price range per candle, counting gaps from the previous close (Wilder smoothing)."""
    prev_close = frame["close"].shift(1)
    true_range = pd.concat(
        [frame["high"] - frame["low"], (frame["high"] - prev_close).abs(), (frame["low"] - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return true_range.ewm(alpha=1 / periods, adjust=False, min_periods=periods).mean()


def rsi(closes: pd.Series, periods: int = 14) -> pd.Series:
    """Relative Strength Index, 0-100: how one-sided recent moves were (Wilder smoothing).

    Above ~70 = mostly up moves lately (stretched); below ~30 = mostly down moves.
    """
    delta = closes.diff()
    gains = delta.clip(lower=0).ewm(alpha=1 / periods, adjust=False, min_periods=periods).mean()
    losses = (-delta.clip(upper=0)).ewm(alpha=1 / periods, adjust=False, min_periods=periods).mean()
    with np.errstate(divide="ignore"):
        strength = gains / losses
    return 100 - 100 / (1 + strength)


def percentile_rank(history: pd.Series, value: float) -> float:
    """% of past values below `value` (ties, within rounding error, count half), 0-100."""
    equal = np.isclose(history, value, rtol=1e-9, atol=0).sum()
    below = (history < value).sum() - np.isclose(history[history < value], value, rtol=1e-9, atol=0).sum()
    return float((below + 0.5 * equal) / len(history) * 100)
