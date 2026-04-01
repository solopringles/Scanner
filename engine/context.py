"""Context feature builder aligned to PDF liquidity hierarchy."""

from __future__ import annotations

import pandas as pd


def enrich_liquidity_context(df_5m: pd.DataFrame, eq_lookback: int = 48) -> pd.DataFrame:
    """
    Add no-lookahead context columns:
    - nearest_eqh_price / nearest_eql_price
    - pdh / pdl
    - pwh / pwl
    - session_high / session_low (current London day session running levels, shifted by 1 bar)
    """
    out = df_5m.copy().sort_index()
    if out.empty:
        return out

    # Local EQH/EQL proxies.
    out["nearest_eqh_price"] = out["high"].rolling(eq_lookback, min_periods=eq_lookback).max().shift(1)
    out["nearest_eql_price"] = out["low"].rolling(eq_lookback, min_periods=eq_lookback).min().shift(1)

    # Previous day/week levels.
    daily = out.resample("1D").agg({"high": "max", "low": "min"})
    weekly = out.resample("1W-MON").agg({"high": "max", "low": "min"})
    out["pdh"] = daily["high"].shift(1).reindex(out.index, method="ffill")
    out["pdl"] = daily["low"].shift(1).reindex(out.index, method="ffill")
    out["pwh"] = weekly["high"].shift(1).reindex(out.index, method="ffill")
    out["pwl"] = weekly["low"].shift(1).reindex(out.index, method="ffill")

    # Session running highs/lows in Europe/London; shifted to avoid lookahead.
    lond = out.tz_convert("Europe/London")
    hours = lond.index.hour
    session = pd.Series("OFF", index=lond.index)
    session[(hours >= 0) & (hours < 7)] = "ASIA"
    session[(hours >= 7) & (hours < 13)] = "LONDON"
    session[(hours >= 13) & (hours < 17)] = "NEW_YORK"
    lond["session"] = session.values
    lond["london_date"] = lond.index.date

    # Running session extrema, then shift(1) for strict no-lookahead.
    grp = lond.groupby(["london_date", "session"], sort=False)
    lond["session_high"] = grp["high"].cummax().shift(1)
    lond["session_low"] = grp["low"].cummin().shift(1)

    out["session_high"] = lond["session_high"].tz_convert("UTC")
    out["session_low"] = lond["session_low"].tz_convert("UTC")
    out["session"] = lond["session"].tz_convert("UTC")
    return out

