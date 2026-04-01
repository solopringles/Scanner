"""Order block detection and state transitions."""

from __future__ import annotations

import numpy as np
import pandas as pd

from .config import OBConfig
from .models import OBZone
from .types import BiasDirection, OBState, ZoneRole


def is_ob_tapped_before(ob: OBZone, df: pd.DataFrame) -> bool:
    """
    True if price revisits the OB zone after creation.
    Overlap test: candle range intersects [ob.low, ob.high].
    """
    post = df.iloc[ob.idx + 1 :]
    if post.empty:
        return False
    low = post["low"].to_numpy()
    high = post["high"].to_numpy()
    return bool(np.any((low <= ob.high) & (high >= ob.low)))


def should_ob_be_smt(ob: OBZone, market_ctx: dict) -> bool:
    """
    OB -> SMT triggers from spec:
    - price breaks through the OB
    - DOL hit
    - strong BOS away
    - range too extended
    """
    return bool(
        (market_ctx.get("zone_role") == ZoneRole.SMT.value)
        or (int(market_ctx.get("tap_count", 0)) >= 3)
        or market_ctx.get("broken_through", False)
        or market_ctx.get("dol_hit", False)
        or market_ctx.get("strong_bos_away", False)
        or ob.created_in_manipulation
        or market_ctx.get("range_too_extended", False)
    )


def classify_ob_state(ob: OBZone, market_ctx: dict) -> OBState:
    if should_ob_be_smt(ob, market_ctx):
        return OBState.SMT_TRAP
    return OBState.VALID_OB


def resolve_ob_role(
    ob: OBZone,
    *,
    htf_bias: BiasDirection,
    htf_regime: str = "TREND",
    htf_dol_hit: bool = False,
    tap_count: int = 0,
) -> tuple[str, str, bool]:
    """
    Dynamic zone-role resolver:
    - Trend mode: bias-aligned zones are OB, counter-bias zones are SMT.
    - Accumulation or DOL-hit: zones are treated as SMT/liquidity.
    """
    if tap_count >= 3:
        return ZoneRole.SMT.value, "tap_degraded", False
    if htf_regime == "ACCUMULATION" or htf_bias == BiasDirection.NEUTRAL:
        return ZoneRole.SMT.value, "htf_accumulation_or_neutral", False
    if htf_dol_hit:
        return ZoneRole.SMT.value, "htf_dol_tapped", False

    aligned = (
        (htf_bias == BiasDirection.BULLISH and ob.direction == "long")
        or (htf_bias == BiasDirection.BEARISH and ob.direction == "short")
    )
    if aligned:
        return ZoneRole.OB.value, "bias_aligned", True
    return ZoneRole.SMT.value, "counter_bias_liquidity", False


def update_ob_lifecycle(
    ob: OBZone,
    *,
    htf_bias: BiasDirection,
    htf_regime: str = "TREND",
    htf_dol_hit: bool = False,
    tap_count: int = 0,
) -> OBZone:
    prior_state = ob.current_role if ob.current_role else ob.prior_state
    role, reason, protected = resolve_ob_role(
        ob,
        htf_bias=htf_bias,
        htf_regime=htf_regime,
        htf_dol_hit=htf_dol_hit,
        tap_count=tap_count,
    )
    ob.prior_state = prior_state
    ob.current_role = role
    ob.role_reason = reason
    ob.protected = protected
    ob.tapped = tap_count > 0
    return ob


def detect_inducement_obs(df: pd.DataFrame, ctx: dict) -> list[OBZone]:
    """
    Lightweight inducement OB finder:
    - Bullish OB candidate: last bearish candle before bullish displacement that takes prior high.
    - Bearish OB candidate: last bullish candle before bearish displacement that takes prior low.
    Uses numpy arrays for speed (single O(n) scan).
    """
    cfg: OBConfig = ctx.get("ob_cfg", OBConfig())
    required = {"open", "high", "low", "close"}
    missing = required.difference(df.columns)
    if missing:
        raise ValueError(f"Missing OHLC columns: {sorted(missing)}")
    if len(df) < cfg.structure_lookback + cfg.impulse_window + 2:
        return []

    o = df["open"].to_numpy()
    h = df["high"].to_numpy()
    l = df["low"].to_numpy()
    c = df["close"].to_numpy()
    ts = df.index
    n = len(df)

    out: list[OBZone] = []
    for i in range(cfg.structure_lookback, n - cfg.impulse_window - 1):
        # Candidate OB candle at i, impulse evaluated on i+1..i+impulse_window.
        ob_open, ob_close = o[i], c[i]
        ob_high, ob_low = h[i], l[i]
        prior_high = float(np.max(h[i - cfg.structure_lookback : i]))
        prior_low = float(np.min(l[i - cfg.structure_lookback : i]))
        swept_local_low = bool(ob_low < prior_low)
        swept_local_high = bool(ob_high > prior_high)
        future_open = o[i + 1 : i + 1 + cfg.impulse_window]
        future_close = c[i + 1 : i + 1 + cfg.impulse_window]
        future_high = h[i + 1 : i + 1 + cfg.impulse_window]
        future_low = l[i + 1 : i + 1 + cfg.impulse_window]
        future_range = np.maximum(future_high - future_low, 1e-9)
        future_body_ratio = np.abs(future_close - future_open) / future_range
        bullish_displacement = bool(np.any((future_close > future_open) & (future_body_ratio >= cfg.min_body_ratio)))
        bearish_displacement = bool(np.any((future_close < future_open) & (future_body_ratio >= cfg.min_body_ratio)))

        # Bullish inducement: bearish OB candle then bullish displacement above prior high.
        if (
            ob_close < ob_open
            and swept_local_low
            and float(np.max(future_high)) > prior_high
            and bullish_displacement
        ):
            out.append(
                OBZone(
                    idx=i,
                    timestamp=pd.Timestamp(ts[i]),
                    direction="long",
                    low=float(ob_low),
                    high=float(ob_high),
                    induced=True,
                    created_in_manipulation=True,
                )
            )
            continue

        # Bearish inducement: bullish OB candle then bearish displacement below prior low.
        if (
            ob_close > ob_open
            and swept_local_high
            and float(np.min(future_low)) < prior_low
            and bearish_displacement
        ):
            out.append(
                OBZone(
                    idx=i,
                    timestamp=pd.Timestamp(ts[i]),
                    direction="short",
                    low=float(ob_low),
                    high=float(ob_high),
                    induced=True,
                    created_in_manipulation=True,
                )
            )

    return out
