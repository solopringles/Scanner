"""AMD phase detection helpers.

This is the first implementation pass aligned to:
- Section 1.1 (AMD phase definitions)
- Section 5 (FBoS requires manipulation characteristics)
"""

from __future__ import annotations

import pandas as pd

from .config import AMDConfig
from .types import AMDPhase


def _require_ohlc(df: pd.DataFrame) -> None:
    required = {"open", "high", "low", "close"}
    missing = required.difference(df.columns)
    if missing:
        raise ValueError(f"Missing OHLC columns: {sorted(missing)}")


def _window(df: pd.DataFrame, i: int, size: int) -> pd.DataFrame:
    if i < 0 or i >= len(df):
        raise IndexError(f"Index {i} out of bounds for len={len(df)}")
    start = max(0, i - size + 1)
    return df.iloc[start : i + 1]


def _atr_like(win: pd.DataFrame) -> float:
    # Lightweight ATR proxy for phase gating: average candle range.
    val = float((win["high"] - win["low"]).mean())
    return max(val, 1e-9)


def compute_manipulation_series(df: pd.DataFrame, cfg: AMDConfig) -> pd.Series:
    """
    Vectorized manipulation detector.
    Conditions mirror `is_manipulation` but run with rolling operations.
    """
    _require_ohlc(df)
    o = df["open"]
    h = df["high"]
    l = df["low"]
    c = df["close"]

    csz = cfg.manipulation_min_consecutive
    wsz = cfg.manipulation_window

    rng = (h - l).clip(lower=1e-9)
    body_ratio = (c - o).abs() / rng
    bull = c > o
    bear = c < o
    all_bull = bull.rolling(csz, min_periods=csz).sum() == csz
    all_bear = bear.rolling(csz, min_periods=csz).sum() == csz
    one_sided = all_bull | all_bear
    strong_bodies = body_ratio.rolling(csz, min_periods=csz).min() >= cfg.manipulation_min_body_ratio

    start_open = o.shift(wsz - 1)
    impulse_move = (c - start_open).abs()
    total_span = h.rolling(wsz, min_periods=wsz).max() - l.rolling(wsz, min_periods=wsz).min()
    pullback_ratio = (total_span - impulse_move).clip(lower=0) / total_span.clip(lower=1e-9)
    pullback_ok = pullback_ratio <= cfg.manipulation_max_pullback_ratio

    return (one_sided & strong_bodies & pullback_ok).fillna(False)


def compute_phase_series(df: pd.DataFrame, cfg: AMDConfig) -> pd.Series:
    """
    Vectorized phase classification for full DataFrame.
    Priority: accumulation -> manipulation -> distribution -> accumulation fallback.
    """
    _require_ohlc(df)
    o = df["open"]
    h = df["high"]
    l = df["low"]
    c = df["close"]

    # Accumulation features.
    aw = cfg.accumulation_window
    atr_like = (h - l).rolling(aw, min_periods=3).mean().clip(lower=1e-9)
    span = h.rolling(aw, min_periods=3).max() - l.rolling(aw, min_periods=3).min()
    net = (c - o.shift(aw - 1)).abs()
    acc = (span <= cfg.accumulation_max_span_atr * atr_like) & (net <= cfg.accumulation_max_net_atr * atr_like)

    # Manipulation features (already vectorized).
    manip = compute_manipulation_series(df, cfg)

    # Distribution features.
    dw = cfg.distribution_window
    bull = c > o
    bear = c < o
    net_d = c - o.shift(dw - 1)
    trend_up = net_d > 0
    trend_dn = net_d < 0
    trend_candles_up = bull.astype(int).rolling(dw, min_periods=5).sum() / float(dw)
    trend_candles_dn = bear.astype(int).rolling(dw, min_periods=5).sum() / float(dw)
    hh_up = (h.diff() > 0).astype(int).rolling(dw, min_periods=5).sum()
    hl_up = (l.diff() > 0).astype(int).rolling(dw, min_periods=5).sum()
    hh_dn = (h.diff() < 0).astype(int).rolling(dw, min_periods=5).sum()
    hl_dn = (l.diff() < 0).astype(int).rolling(dw, min_periods=5).sum()
    has_pullback = (
        (bull.astype(int).rolling(dw, min_periods=5).sum() > 0)
        & (bear.astype(int).rolling(dw, min_periods=5).sum() > 0)
    )
    dist_up = (
        trend_up
        & (trend_candles_up >= cfg.distribution_min_trend_candle_ratio)
        & (hh_up >= cfg.distribution_min_structure_swings)
        & (hl_up >= cfg.distribution_min_structure_swings)
        & has_pullback
    )
    dist_dn = (
        trend_dn
        & (trend_candles_dn >= cfg.distribution_min_trend_candle_ratio)
        & (hh_dn >= cfg.distribution_min_structure_swings)
        & (hl_dn >= cfg.distribution_min_structure_swings)
        & has_pullback
    )
    dist = dist_up | dist_dn

    phase = pd.Series(AMDPhase.ACCUMULATION, index=df.index, dtype="object")
    phase.loc[manip] = AMDPhase.MANIPULATION
    phase.loc[dist] = AMDPhase.DISTRIBUTION
    phase.loc[acc] = AMDPhase.ACCUMULATION
    return phase


def is_accumulation(df: pd.DataFrame, i: int, cfg: AMDConfig) -> bool:
    """
    Section 1.1: accumulation is range-bound and non-committal.
    We gate by:
    - bounded span vs ATR-like range
    - small net displacement through the window
    """
    _require_ohlc(df)
    win = _window(df, i, cfg.accumulation_window)
    if len(win) < 3:
        return False

    span = float(win["high"].max() - win["low"].min())
    atr = _atr_like(win)
    net = abs(float(win["close"].iloc[-1] - win["open"].iloc[0]))

    span_ok = span <= (cfg.accumulation_max_span_atr * atr)
    net_ok = net <= (cfg.accumulation_max_net_atr * atr)
    return span_ok and net_ok


def is_manipulation(df: pd.DataFrame, i: int, cfg: AMDConfig) -> bool:
    """
    Section 1.1 and Section 5.1:
    manipulation should be one-sided, momentum-driven, and have limited pullback.
    """
    _require_ohlc(df)
    win = _window(df, i, cfg.manipulation_window)
    if len(win) < cfg.manipulation_min_consecutive:
        return False

    tail = win.iloc[-cfg.manipulation_min_consecutive :]
    bodies = (tail["close"] - tail["open"]).abs()
    ranges = (tail["high"] - tail["low"]).clip(lower=1e-9)
    body_ratio = bodies / ranges

    all_bull = bool((tail["close"] > tail["open"]).all())
    all_bear = bool((tail["close"] < tail["open"]).all())
    one_sided = all_bull or all_bear
    strong_bodies = bool((body_ratio >= cfg.manipulation_min_body_ratio).all())

    impulse_move = abs(float(tail["close"].iloc[-1] - tail["open"].iloc[0]))
    total_span = float(tail["high"].max() - tail["low"].min())
    pullback_ratio = max(0.0, total_span - impulse_move) / max(total_span, 1e-9)
    pullback_ok = pullback_ratio <= cfg.manipulation_max_pullback_ratio

    return one_sided and strong_bodies and pullback_ok


def is_distribution(df: pd.DataFrame, i: int, cfg: AMDConfig) -> bool:
    """
    Section 1.1:
    distribution is the real move with directional continuation and pullbacks.
    """
    _require_ohlc(df)
    win = _window(df, i, cfg.distribution_window)
    if len(win) < 5:
        return False

    net = float(win["close"].iloc[-1] - win["open"].iloc[0])
    if net == 0:
        return False

    trend_up = net > 0
    if trend_up:
        trend_candles = (win["close"] > win["open"]).sum()
        hh = (win["high"].diff() > 0).sum()
        hl = (win["low"].diff() > 0).sum()
    else:
        trend_candles = (win["close"] < win["open"]).sum()
        hh = (win["high"].diff() < 0).sum()
        hl = (win["low"].diff() < 0).sum()

    trend_ratio = float(trend_candles) / float(len(win))
    has_pullback = bool((win["close"] > win["open"]).any() and (win["close"] < win["open"]).any())
    structure_ok = (hh >= cfg.distribution_min_structure_swings) and (hl >= cfg.distribution_min_structure_swings)

    return (
        trend_ratio >= cfg.distribution_min_trend_candle_ratio
        and has_pullback
        and structure_ok
    )


def detect_amd_phase(
    df: pd.DataFrame,
    i: int,
    cfg: AMDConfig,
    prev_phase: AMDPhase | None = None,
) -> AMDPhase:
    """
    Phase priority:
    1) accumulation
    2) manipulation
    3) distribution
    Fallback keeps prior phase to reduce noise around boundaries.
    """
    if is_accumulation(df, i, cfg):
        return AMDPhase.ACCUMULATION
    if is_manipulation(df, i, cfg):
        return AMDPhase.MANIPULATION
    if is_distribution(df, i, cfg):
        return AMDPhase.DISTRIBUTION
    return prev_phase if prev_phase is not None else AMDPhase.ACCUMULATION
