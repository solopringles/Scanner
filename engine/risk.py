"""Risk and order construction."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
import math
import pandas as pd

from .config import RiskConfig
from .models import OrderSpec, SetupSignal
from .types import InvalidationReason


def check_invalidation(ctx: dict) -> InvalidationReason | None:
    if ctx.get("dol_hit", False):
        return InvalidationReason.DOL_ALREADY_HIT
    if ctx.get("counter_htf_bias", False) and not ctx.get("relax_counter_htf_bias", False):
        return InvalidationReason.COUNTER_HTF_BIAS
    if ctx.get("ob_already_tapped", False):
        return InvalidationReason.OB_ALREADY_TAPPED
    if ctx.get("momentum_not_sharp", False):
        return InvalidationReason.MOMENTUM_NOT_SHARP
    if ctx.get("returned_to_range", False):
        return InvalidationReason.RETURNED_TO_RANGE
    if ctx.get("protected_level_swept", False):
        return InvalidationReason.PROTECTED_LEVEL_SWEEPED
    if ctx.get("htf_poi_inside_sl", False):
        return InvalidationReason.HTF_POI_INSIDE_SL
    if ctx.get("news_window_block", False):
        return InvalidationReason.NEWS_WINDOW_BLOCK
    if ctx.get("opposing_htf_bos", False):
        return InvalidationReason.OPPOSING_HTF_BOS
    return None


def compute_sl(setup: dict, entry_price: float, cfg: RiskConfig) -> float | None:
    direction = setup.get("direction")
    model = str(setup.get("model", ""))
    is_sweep_model = model in {"FBOS", "AGGRESSIVE_SWEEP"} or model.endswith("FBOS")
    buffer_pips = cfg.fbos_sl_buffer_pips if is_sweep_model else cfg.sl_buffer_pips
    buffer = buffer_pips * cfg.pip_value
    if direction == "long":
        # Sweep models default to the sweep extreme; enlightened mode tightens to OB boundary.
        if is_sweep_model and not cfg.enlightened_sl_mode:
            anchor = setup.get("sweep_low", setup.get("low_anchor"))
        else:
            anchor = setup.get("ob_low", setup.get("sweep_low", setup.get("low_anchor")))
        if anchor is None or pd.isna(anchor):
            return None
        sl = float(anchor) - buffer
        # Protective stop must be below entry for longs.
        if sl >= entry_price:
            return None
        if abs(entry_price - sl) < (cfg.min_stop_pips * cfg.pip_value):
            return None
        return sl
    if direction == "short":
        # Sweep models default to the sweep extreme; enlightened mode tightens to OB boundary.
        if is_sweep_model and not cfg.enlightened_sl_mode:
            anchor = setup.get("sweep_high", setup.get("high_anchor"))
        else:
            anchor = setup.get("ob_high", setup.get("sweep_high", setup.get("high_anchor")))
        if anchor is None or pd.isna(anchor):
            return None
        sl = float(anchor) + buffer
        # Protective stop must be above entry for shorts.
        if sl <= entry_price:
            return None
        if abs(entry_price - sl) < (cfg.min_stop_pips * cfg.pip_value):
            return None
        return sl
    return None


def compute_tp(setup: dict, entry_price: float, cfg: RiskConfig) -> float | None:
    direction = setup.get("direction")
    if (dol_price := setup.get("target_dol_price")) is not None:
        return float(dol_price)
    sl = setup.get("sl_price")
    if sl is None:
        return None
    risk = abs(entry_price - float(sl))
    if direction == "long":
        return entry_price + (risk * cfg.reward_multiplier)
    if direction == "short":
        return entry_price - (risk * cfg.reward_multiplier)
    return None


def min_rr_gate(direction: str, entry_price: float, sl_price: float, tp_price: float, min_rr: float) -> bool:
    # Enforce directional reward. Wrong-way targets are invalid.
    if direction == "long":
        reward = tp_price - entry_price
        risk = entry_price - sl_price
    else:
        reward = entry_price - tp_price
        risk = sl_price - entry_price
    if risk <= 0:
        return False
    return (reward > 0) and (reward / risk >= min_rr)


def build_order(setup: SetupSignal | dict, risk_cfg: RiskConfig) -> OrderSpec | None:
    # Supports both SetupSignal and plain dict for fast experimentation.
    if is_dataclass(setup):
        s = asdict(setup)
    else:
        s = dict(setup)
    ts = pd.Timestamp(s.get("timestamp"))
    direction = str(s.get("direction"))
    entry = float(s.get("entry_price"))
    sl = s.get("sl_price")
    if sl is None:
        sl = compute_sl(s, entry, risk_cfg)
    if sl is None:
        return None

    s["sl_price"] = float(sl)
    tp = s.get("tp_price")
    if tp is None:
        tp = compute_tp(s, entry, risk_cfg)
    if tp is None:
        return None
    if any(math.isnan(v) for v in (entry, float(sl), float(tp))):
        return None

    rr_ok = min_rr_gate(direction, entry, float(sl), float(tp), min_rr=risk_cfg.min_rr_threshold)
    if not rr_ok:
        return None

    risk_amount = risk_cfg.account_balance * risk_cfg.risk_per_trade
    risk_per_unit = abs(entry - float(sl))
    qty = risk_amount / max(risk_per_unit, 1e-9)
    rr = abs(float(tp) - entry) / max(risk_per_unit, 1e-9)

    return OrderSpec(
        timestamp=ts,
        direction=direction,
        entry_price=entry,
        sl_price=float(sl),
        tp_price=float(tp),
        qty=float(qty),
        rr=float(rr),
        meta={"model": str(s.get("model", ""))},
    )
