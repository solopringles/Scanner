"""Mitigation feature helpers."""

from __future__ import annotations

import pandas as pd

from .config import OBConfig
from .models import OBZone
from .ob import classify_ob_state, update_ob_lifecycle
from .types import BiasDirection, OBState


def _bar_taps_zone(bar: pd.Series, ob: OBZone) -> bool:
    return float(bar["low"]) <= ob.high and float(bar["high"]) >= ob.low


def _local_bos_after_tap(df: pd.DataFrame, first_tap_idx: int, i: int, direction: str) -> bool:
    if first_tap_idx < 0 or i <= first_tap_idx:
        return False
    win = df.iloc[first_tap_idx:i]
    if win.empty:
        return False
    close = float(df.iloc[i]["close"])
    if direction == "long":
        return close > float(win["high"].max())
    return close < float(win["low"].min())


def _reaction_induced_structure(df: pd.DataFrame, first_tap_idx: int, i: int, direction: str, lookback: int = 3) -> bool:
    if first_tap_idx < 0 or i < first_tap_idx:
        return False
    start = max(first_tap_idx, i - lookback)
    win = df.iloc[start:i]
    if win.empty:
        return False
    bar = df.iloc[i]
    if direction == "long":
        return float(bar["low"]) < float(win["low"].min())
    return float(bar["high"]) > float(win["high"].max())


def evaluate_mitigation_context(
    *,
    df: pd.DataFrame,
    i: int,
    direction: str,
    order_blocks: list[OBZone],
    ob_cfg: OBConfig,
    htf_bias: BiasDirection = BiasDirection.NEUTRAL,
    htf_regime: str = "TREND",
    htf_dol_hit: bool = False,
) -> dict:
    """
    Evaluate mitigation rule context at bar i, no lookahead.
    Returns keys used by trigger_mitigation.
    """
    obs = [ob for ob in order_blocks if ob.direction == direction and ob.idx < i]
    if not obs:
        return {
            "valid_inducement_ob": False,
            "ob_previously_tapped": False,
            "ob_tap_seen": False,
            "m5_bos_after_tap": False,
            "active_ob_state": OBState.DEPLETED.value,
            "active_ob_low": None,
            "active_ob_high": None,
            "active_ob_role": None,
            "active_ob_role_reason": None,
            "active_ob_origin_bias": None,
        }

    ob = obs[-1]
    row = df.iloc[i]
    ob = update_ob_lifecycle(ob, htf_bias=htf_bias, htf_regime=htf_regime, htf_dol_hit=htf_dol_hit)
    role = ob.current_role
    role_reason = ob.role_reason

    # Tap timeline (no lookahead): count touches from OB creation up to current bar.
    history = df.iloc[ob.idx + 1 : i + 1]
    tap_mask = (history["low"] <= ob.high) & (history["high"] >= ob.low) if not history.empty else pd.Series(dtype=bool)
    tap_indices = history.index[tap_mask].tolist() if len(history) else []
    tap_count = len(tap_indices)
    first_tap_ts = tap_indices[0] if tap_count > 0 else None
    current_tap = _bar_taps_zone(row, ob)
    broken_through = bool(
        (direction == "long" and float(row["low"]) < ob.low)
        or (direction == "short" and float(row["high"]) > ob.high)
    )

    # OB/SMT gating.
    mid = (ob.low + ob.high) * 0.5
    dist = abs(float(row["close"]) - mid)
    state = classify_ob_state(
        ob,
        {
            "zone_role": role,
            "tap_count": tap_count,
            "broken_through": broken_through,
            "dol_hit": bool(row.get("primary_dol_hit", False)),
            "strong_bos_away": bool(row.get("strong_bos_away", False)),
            "range_too_extended": dist > (ob_cfg.range_extended_pips * 0.0001),
        },
    )

    first_tap_idx = int(df.index.get_loc(first_tap_ts)) if first_tap_ts is not None else None

    # Conservative confirmation: BOS after the first tap while the OB is still holding.
    m5_bos_after = bool(first_tap_idx is not None and not broken_through) and _local_bos_after_tap(
        df, first_tap_idx=first_tap_idx, i=i, direction=direction
    )
    reaction_induced_structure = bool(first_tap_idx is not None and not broken_through) and _reaction_induced_structure(
        df, first_tap_idx=first_tap_idx, i=i, direction=direction
    )

    return {
        "valid_inducement_ob": bool(ob.induced) and state == OBState.VALID_OB,
        "ob_previously_tapped": tap_count > 0,
        "ob_tap_seen": current_tap,
        "m5_bos_after_tap": m5_bos_after,
        "reaction_induced_structure": reaction_induced_structure,
        "broken_through": broken_through,
        "active_ob_state": state.value,
        "active_ob_low": float(ob.low),
        "active_ob_high": float(ob.high),
        "active_ob_role": role,
        "active_ob_role_reason": role_reason,
        "active_ob_origin_bias": ob.origin_bias,
        "tap_count": tap_count,
        "first_tap_ts": first_tap_ts,
        "first_tap_idx": first_tap_idx,
    }
