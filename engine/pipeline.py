"""End-to-end v2 pipeline scaffold (no look-ahead)."""

from __future__ import annotations

import numpy as np
import pandas as pd

from .amd import compute_manipulation_series, compute_phase_series
from .bias import derive_bias, invalidate_bias
from .breaks import classify_break
from .config import AMDConfig, BreakConfig, DOLConfig, EntryConfig, OBConfig, RiskConfig, SessionConfig
from .dol import collect_dol_candidates, select_target_dol
from .entries import (
    aggressive_sweep_gate_report,
    choose_entry_model,
    fbos_gate_report,
    mitigation_gate_report,
    smt_reaction_gate_report,
    trigger_aggressive_sweep,
    trigger_fbos,
    trigger_mitigation,
    trigger_smt_reaction,
)
from .execution import simulate_limit_order_trade
from .mitigation import evaluate_mitigation_context
from .models import HTFBreakPoint, OBZone, PipelineArtifacts, SetupSignal, TradeRuntime
from .ob import detect_inducement_obs, finalize_ob_cycle, update_ob_lifecycle
from .risk import build_order, check_invalidation, compute_sl
from .sessions import map_session
from .types import AMDPhase, BiasDirection, BreakType, EntryModel, ZoneRole


def _require_ohlc(df: pd.DataFrame) -> pd.DataFrame:
    req = {"open", "high", "low", "close"}
    missing = req.difference(df.columns)
    if missing:
        raise ValueError(f"Missing OHLC columns: {sorted(missing)}")
    return df.sort_index()


def _london_hour(ts: pd.Timestamp) -> int:
    ts = pd.Timestamp(ts)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return int(ts.tz_convert("Europe/London").hour)


def _is_aggressive_sweep_window(ts: pd.Timestamp, entry_cfg: EntryConfig) -> bool:
    hour = _london_hour(ts)
    return entry_cfg.aggressive_sweep_start_hour <= hour < entry_cfg.aggressive_sweep_end_hour


def _break_matrix_no_lookahead(df: pd.DataFrame, lookback: int = 20) -> dict[str, np.ndarray]:
    """
    Fully vectorized break/sweep matrix, no future bars.
    """
    h = df["high"]
    l = df["low"]
    c = df["close"]
    prior_high = h.rolling(lookback, min_periods=lookback).max().shift(1)
    prior_low = l.rolling(lookback, min_periods=lookback).min().shift(1)

    close_break_high = (c > prior_high).fillna(False).to_numpy()
    close_break_low = (c < prior_low).fillna(False).to_numpy()
    sweep_reject_high = ((h > prior_high) & (c <= prior_high)).fillna(False).to_numpy()
    sweep_reject_low = ((l < prior_low) & (c >= prior_low)).fillna(False).to_numpy()
    breaks_level = (close_break_high | close_break_low | sweep_reject_high | sweep_reject_low)

    return {
        "close_break_high": close_break_high,
        "close_break_low": close_break_low,
        "sweep_reject_high": sweep_reject_high,
        "sweep_reject_low": sweep_reject_low,
        "breaks_level": breaks_level,
        # Structural levels used for explicit FBOS mapping/penetration.
        "prior_high": prior_high.to_numpy(),
        "prior_low": prior_low.to_numpy(),
        "close_high_penetration": (c - prior_high).clip(lower=0.0).fillna(0.0).to_numpy(),
        "close_low_penetration": (prior_low - c).clip(lower=0.0).fillna(0.0).to_numpy(),
        "sweep_high_penetration": (h - prior_high).fillna(0.0).to_numpy(),
        "sweep_low_penetration": (prior_low - l).fillna(0.0).to_numpy(),
    }


def _build_15m_fbos_context(
    df_5m: pd.DataFrame,
    *,
    lookback: int,
    pip_value: float,
    leadin_bars: int,
    min_body_ratio: float,
) -> dict[pd.Timestamp, dict]:
    """
    Build 15m inducement context and map it to the first actionable 5m bar
    after the 15m candle has fully closed.
    """
    agg_spec: dict[str, str] = {
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
    }
    for col in (
        "nearest_eqh_price",
        "nearest_eql_price",
        "session_high",
        "session_low",
        "pdh",
        "pdl",
        "pwh",
        "pwl",
        "smt_open",
    ):
        if col in df_5m.columns:
            agg_spec[col] = "last"

    df_15m = df_5m.resample("15min").agg(agg_spec).dropna(subset=["open", "high", "low", "close"])
    if df_15m.empty:
        return {}

    br_15m = _break_matrix_no_lookahead(df_15m, lookback=lookback)
    fbos_leadin_15m = _fbos_manipulation_leadin_matrix(
        df_15m,
        br_15m,
        leadin_bars=leadin_bars,
        min_body_ratio=min_body_ratio,
    )

    out: dict[pd.Timestamp, dict] = {}
    for i, ts in enumerate(df_15m.index):
        flags = {
            "close_break_high": bool(br_15m["close_break_high"][i]),
            "close_break_low": bool(br_15m["close_break_low"][i]),
            "sweep_reject_high": bool(br_15m["sweep_reject_high"][i]),
            "sweep_reject_low": bool(br_15m["sweep_reject_low"][i]),
            "breaks_level": bool(br_15m["breaks_level"][i]),
        }
        if not (flags["sweep_reject_high"] or flags["sweep_reject_low"]):
            continue

        actionable_ts = ts + pd.Timedelta(minutes=15)
        if actionable_ts not in df_5m.index:
            continue

        out[actionable_ts] = {
            "row": df_15m.iloc[i],
            "flags": flags,
            "prior_high": br_15m["prior_high"][i],
            "prior_low": br_15m["prior_low"][i],
            "sweep_high_pen_pips": float(br_15m["sweep_high_penetration"][i]) / max(pip_value, 1e-9),
            "sweep_low_pen_pips": float(br_15m["sweep_low_penetration"][i]) / max(pip_value, 1e-9),
            "momentum_breakout": bool(fbos_leadin_15m[i]),
            "source_tf": "15m",
        }
    return out


def _build_htf_regime_context(
    df_5m: pd.DataFrame,
    *,
    lookback: int,
    amd_cfg: AMDConfig,
    break_cfg: BreakConfig,
    ob_cfg: OBConfig,
    carry_bias: bool,
) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series, list[OBZone], list[HTFBreakPoint]]:
    """
    Build a coarse HTF bias/regime overlay from 1H structure and resolve HTF zones.
    """
    agg_spec: dict[str, str] = {
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
    }
    for col in (
        "nearest_eqh_price",
        "nearest_eql_price",
        "session_high",
        "session_low",
        "pdh",
        "pdl",
        "pwh",
        "pwl",
        "smt_open",
        "internal_dol",
        "internal_dol_direction",
    ):
        if col in df_5m.columns:
            agg_spec[col] = "last"

    df_htf = df_5m.resample("1h").agg(agg_spec).dropna(subset=["open", "high", "low", "close"])
    if df_htf.empty:
        empty = pd.Series(dtype="object")
        return empty, empty, empty, empty, [], []

    phase_series_obj = compute_phase_series(df_htf, amd_cfg)
    phase_vals = phase_series_obj.tolist()
    br_htf = _break_matrix_no_lookahead(df_htf, lookback=lookback)

    bias_vals: list[BiasDirection] = []
    regime_vals: list[str] = []
    break_type_vals: list[BreakType] = []
    break_ctx: list[dict] = []
    prev_bias = BiasDirection.NEUTRAL

    for i in range(len(df_htf)):
        flags = {
            "close_break_high": bool(br_htf["close_break_high"][i]),
            "close_break_low": bool(br_htf["close_break_low"][i]),
            "sweep_reject_high": bool(br_htf["sweep_reject_high"][i]),
            "sweep_reject_low": bool(br_htf["sweep_reject_low"][i]),
            "breaks_level": bool(br_htf["breaks_level"][i]),
        }
        phase_before = phase_vals[i - 1] if i > 0 else phase_vals[i]
        drake_candle = bool(
            (flags["close_break_high"] and flags["close_break_low"])
            or (flags["sweep_reject_high"] and flags["sweep_reject_low"])
        )
        hour_row = df_htf.iloc[i]
        post_gap_bootstrap = bool(
            i > 0 and (pd.Timestamp(df_htf.index[i]) - pd.Timestamp(df_htf.index[i - 1])) > pd.Timedelta(hours=1)
        )
        if drake_candle:
            direction, _, manipulation_side = _resolve_htf_drake_candle(
                df_5m,
                pd.Timestamp(df_htf.index[i]),
                prior_high=float(br_htf["prior_high"][i]),
                prior_low=float(br_htf["prior_low"][i]),
                hour_open=float(hour_row["open"]),
                hour_close=float(hour_row["close"]),
            )
            effective_flags = dict(flags)
            if direction == "long":
                effective_flags["close_break_low"] = False
                effective_flags["sweep_reject_low"] = False
            else:
                effective_flags["close_break_high"] = False
                effective_flags["sweep_reject_high"] = False
        else:
            direction = "long" if flags["close_break_high"] or flags["sweep_reject_high"] else "short"
            manipulation_side = "high" if direction == "short" else "low"
            effective_flags = flags
        break_ctx.append(effective_flags)
        if post_gap_bootstrap:
            break_type_vals.append(BreakType.NONE)
            bias_vals.append(prev_bias if carry_bias else BiasDirection.NEUTRAL)
            regime_vals.append("ACCUMULATION" if phase_vals[i] == AMDPhase.ACCUMULATION else "TREND")
            continue
        reversal_now = bool(effective_flags["sweep_reject_high"] or effective_flags["sweep_reject_low"])
        major_level_taken = _major_level_taken(hour_row, effective_flags, use_close_break=bool(effective_flags["close_break_high"] or effective_flags["close_break_low"]))
        if not major_level_taken:
            break_type_vals.append(BreakType.NONE)
            bias_vals.append(prev_bias if carry_bias else BiasDirection.NEUTRAL)
            regime_vals.append("ACCUMULATION" if phase_vals[i] == AMDPhase.ACCUMULATION else "TREND")
            continue
        rbos_distance = _recent_opposite_break_distance(
            break_ctx=break_ctx,
            i=i,
            direction="long" if effective_flags["close_break_low"] else "short",
            lookback_bars=4,
        )
        rbos_confirm = bool(
            (effective_flags["close_break_high"] or effective_flags["close_break_low"])
            and rbos_distance is not None
            and rbos_distance >= 2
        )
        btype = classify_break(
            phase_before_break=phase_before,
            breaks_significant_level=bool(effective_flags["breaks_level"]),
            manipulation_signature_present=bool(reversal_now or rbos_confirm),
            later_reversal_seen=reversal_now,
            rbos_confirmation_seen=rbos_confirm,
            cfg=break_cfg,
        )
        break_type_vals.append(btype)
        fresh_bias = derive_bias(
            {
                "rbos_up": btype == BreakType.RBOS and bool(effective_flags["close_break_high"]),
                "rbos_down": btype == BreakType.RBOS and bool(effective_flags["close_break_low"]),
            }
        )
        if fresh_bias != BiasDirection.NEUTRAL:
            prev_bias = fresh_bias
            bias = fresh_bias
        else:
            bias = prev_bias if carry_bias else BiasDirection.NEUTRAL
        bias_vals.append(bias)
        regime_vals.append("ACCUMULATION" if phase_vals[i] == AMDPhase.ACCUMULATION else "TREND")

    bias_series_raw = pd.Series([b.value for b in bias_vals], index=df_htf.index, name="htf_bias_raw")
    regime_series_raw = pd.Series(regime_vals, index=df_htf.index, name="htf_regime_raw")
    bias_enum = pd.Series(bias_vals, index=df_htf.index, name="htf_bias_enum")
    regime_series_htf = pd.Series(regime_vals, index=df_htf.index, name="htf_regime_htf")

    htf_order_blocks = detect_inducement_obs(df_htf, {"ob_cfg": ob_cfg})
    htf_break_points: list[HTFBreakPoint] = []
    for i, btype in enumerate(break_type_vals):
        if btype == BreakType.NONE:
            continue
        break_bias = bias_vals[i] if i < len(bias_vals) else BiasDirection.NEUTRAL
        break_regime = regime_vals[i] if i < len(regime_vals) else "ACCUMULATION"
        hour_row = df_htf.iloc[i]
        effective_flags = {
            "close_break_high": bool(br_htf["close_break_high"][i]),
            "close_break_low": bool(br_htf["close_break_low"][i]),
            "sweep_reject_high": bool(br_htf["sweep_reject_high"][i]),
            "sweep_reject_low": bool(br_htf["sweep_reject_low"][i]),
            "breaks_level": bool(br_htf["breaks_level"][i]),
        }
        drake_candle = bool(
            (effective_flags["close_break_high"] and effective_flags["close_break_low"])
            or (effective_flags["sweep_reject_high"] and effective_flags["sweep_reject_low"])
        )
        if drake_candle:
            direction, _, manipulation_side = _resolve_htf_drake_candle(
                df_5m,
                pd.Timestamp(df_htf.index[i]),
                prior_high=float(br_htf["prior_high"][i]),
                prior_low=float(br_htf["prior_low"][i]),
                hour_open=float(hour_row["open"]),
                hour_close=float(hour_row["close"]),
            )
        else:
            manipulation_side = "high" if bool(effective_flags["close_break_high"] or effective_flags["sweep_reject_high"]) else "low"
            direction = "long" if btype == BreakType.RBOS and effective_flags["close_break_high"] else "short"
        price = float(df_htf.iloc[i]["high"] if direction == "short" else df_htf.iloc[i]["low"])
        htf_break_points.append(
            HTFBreakPoint(
                idx=i,
                timestamp=pd.Timestamp(df_htf.index[i]),
                break_type=btype.value,
                bias=break_bias.value if isinstance(break_bias, BiasDirection) else str(break_bias),
                regime=break_regime,
                price=price,
                direction=direction,
                phase=phase_vals[i].value if isinstance(phase_vals[i], AMDPhase) else str(phase_vals[i]),
                is_major=bool(
                    _major_level_taken(
                        hour_row,
                        effective_flags,
                        use_close_break=bool(effective_flags["close_break_high"] or effective_flags["close_break_low"]),
                    )
                ),
                drake_candle=drake_candle,
                manipulation_side=manipulation_side,
                source_tf="1H",
            )
        )

    for ob in htf_order_blocks:
        if ob.idx < len(bias_enum):
            origin_bias = bias_enum.iloc[ob.idx]
            ob.origin_bias = origin_bias.value if isinstance(origin_bias, BiasDirection) else str(origin_bias)
        update_ob_lifecycle(
            ob,
            htf_bias=bias_enum.iloc[ob.idx] if ob.idx < len(bias_enum) else BiasDirection.NEUTRAL,
            htf_regime=regime_series_htf.iloc[ob.idx] if ob.idx < len(regime_series_htf) else "ACCUMULATION",
            htf_dol_hit=False,
        )

    bias_series = bias_series_raw.reindex(df_5m.index, method="ffill").fillna(BiasDirection.NEUTRAL.value)
    regime_series = regime_series_raw.reindex(df_5m.index, method="ffill").fillna("ACCUMULATION")
    return bias_series, regime_series, bias_series_raw, regime_series_raw, htf_order_blocks, htf_break_points


def _fbos_manipulation_leadin_matrix(
    df: pd.DataFrame,
    br: dict[str, np.ndarray],
    leadin_bars: int = 3,
    min_body_ratio: float = 0.55,
) -> np.ndarray:
    """
    FBOS lead-in:
    - bullish FBOS intent (close break below prior low) requires bearish one-sided momentum into the break
    - bearish FBOS intent (close break above prior high) requires bullish one-sided momentum into the break
    """
    o = df["open"]
    h = df["high"]
    l = df["low"]
    c = df["close"]
    body_ratio = (c - o).abs() / (h - l).clip(lower=1e-9)
    strong_bull = ((c > o) & (body_ratio >= min_body_ratio)).astype(int)
    strong_bear = ((c < o) & (body_ratio >= min_body_ratio)).astype(int)

    # Lead-in bars must be before the sweep bar => shift by 1.
    prior_bull_run = strong_bull.shift(1).rolling(leadin_bars, min_periods=leadin_bars).sum() == leadin_bars
    prior_bear_run = strong_bear.shift(1).rolling(leadin_bars, min_periods=leadin_bars).sum() == leadin_bars

    close_break_low = br["close_break_low"]
    close_break_high = br["close_break_high"]
    fbos_leadin = (close_break_low & prior_bear_run.fillna(False).to_numpy()) | (
        close_break_high & prior_bull_run.fillna(False).to_numpy()
    )
    return fbos_leadin


def _dol_ctx_from_row(
    row: pd.Series,
    bias: BiasDirection,
    *,
    pip_value: float | None = None,
    current_timestamp: pd.Timestamp | None = None,
    htf_order_blocks: list[OBZone] | None = None,
) -> dict:
    current_price = row.get("close")
    return {
        "bias": bias,
        "eqh": row.get("nearest_eqh_price"),
        "eql": row.get("nearest_eql_price"),
        "session_high": row.get("session_high"),
        "session_low": row.get("session_low"),
        "pdh": row.get("pdh"),
        "pdl": row.get("pdl"),
        "pwh": row.get("pwh"),
        "pwl": row.get("pwl"),
        "smt_open": row.get("smt_open"),
        "internal_dol": row.get("internal_dol"),
        "internal_dol_direction": row.get(
            "internal_dol_direction",
            "long" if bias == BiasDirection.BULLISH else "short" if bias == BiasDirection.BEARISH else "long",
        ),
        "current_price": float(current_price) if current_price is not None and pd.notna(current_price) else None,
        "current_timestamp": pd.Timestamp(current_timestamp) if current_timestamp is not None else None,
        "pip_value": float(pip_value) if pip_value is not None else None,
        "htf_order_blocks": htf_order_blocks or [],
        "invalid_dol_sources": row.get("invalid_dol_sources", []),
    }


def _estimate_rr_to_dol(
    row: pd.Series,
    direction: str,
    entry_price: float,
    target_price: float | None,
    risk_cfg: RiskConfig,
    *,
    model: EntryModel,
) -> float:
    if target_price is None:
        return 0.0
    sl = compute_sl(
        {
            "direction": direction,
            "sweep_low": row.get("low"),
            "sweep_high": row.get("high"),
            "ob_low": row.get("ob_low"),
            "ob_high": row.get("ob_high"),
            "model": model.value,
        },
        entry_price=entry_price,
        cfg=risk_cfg,
    )
    if sl is None:
        return 0.0
    risk = abs(entry_price - float(sl))
    if risk <= 0:
        return 0.0
    reward = (float(target_price) - entry_price) if direction == "long" else (entry_price - float(target_price))
    if reward <= 0:
        return 0.0
    return reward / risk


def _limit_entry_price_from_row(
    row: pd.Series,
    model: EntryModel,
    direction: str,
    *,
    fbos_break_level: float | None = None,
) -> float | None:
    """
    Limit-entry placement by model:
    - FBOS: structural break level when available.
    - Mitigation/internal OB models: OB boundary.
    Falls back to close if explicit level is unavailable.
    """
    if model == EntryModel.FBOS:
        if fbos_break_level is not None and pd.notna(fbos_break_level):
            return float(fbos_break_level)
        if (v := row.get("structure_broken_price")) is not None and pd.notna(v):
            return float(v)
        if direction == "long" and (v := row.get("nearest_eql_price")) is not None and pd.notna(v):
            return float(v)
        if direction == "short" and (v := row.get("nearest_eqh_price")) is not None and pd.notna(v):
            return float(v)
    else:
        if direction == "long" and (v := row.get("ob_low")) is not None and pd.notna(v):
            return float(v)
        if direction == "short" and (v := row.get("ob_high")) is not None and pd.notna(v):
            return float(v)

    if (v := row.get("close")) is not None and pd.notna(v):
        return float(v)
    return None


def _resolve_fbos_break_direction(
    *,
    flags: dict,
    close_high_pen_pips: float,
    close_low_pen_pips: float,
    min_penetration_pips: float,
) -> tuple[str | None, float | None, str | None]:
    """
    FBOS structural break map from spec criterion 3:
    - close break below structural low -> long reversal intent
    - close break above structural high -> short reversal intent
    Competing breaks are resolved by greater close penetration if one side is dominant.
    Returns (direction, selected_penetration_pips, sweep_side).
    """
    bh = bool(flags.get("close_break_high", False))
    bl = bool(flags.get("close_break_low", False))
    if not bh and not bl:
        return None, None, None

    if bh and bl:
        # Competing breaks in same bar: select dominant side only if clear winner.
        if (close_high_pen_pips >= min_penetration_pips) and (close_high_pen_pips > close_low_pen_pips):
            # Break above high implies short reversal intent for FBOS.
            return "short", close_high_pen_pips, "high"
        if (close_low_pen_pips >= min_penetration_pips) and (close_low_pen_pips > close_high_pen_pips):
            # Break below low implies long reversal intent for FBOS.
            return "long", close_low_pen_pips, "low"
        return None, None, None

    if bh:
        if close_high_pen_pips >= min_penetration_pips:
            # Break above high implies short reversal intent for FBOS.
            return "short", close_high_pen_pips, "high"
        return None, None, None

    if close_low_pen_pips >= min_penetration_pips:
        # Break below low implies long reversal intent for FBOS.
        return "long", close_low_pen_pips, "low"
    return None, None, None


def _major_level_taken(row: pd.Series, flags: dict, *, use_close_break: bool = False) -> bool:
    """
    FBOS major structural liquidity check:
    requires sweep/break through at least one major level.
    """
    high = float(row["high"])
    low = float(row["low"])
    close = float(row["close"])
    hi_lvls = [row.get("nearest_eqh_price"), row.get("session_high"), row.get("pdh"), row.get("pwh"), row.get("smt_open")]
    lo_lvls = [row.get("nearest_eql_price"), row.get("session_low"), row.get("pdl"), row.get("pwl"), row.get("smt_open")]

    if use_close_break:
        if flags.get("close_break_high", False):
            return any((v is not None and pd.notna(v) and close > float(v)) for v in hi_lvls)
        if flags.get("close_break_low", False):
            return any((v is not None and pd.notna(v) and close < float(v)) for v in lo_lvls)
        return False

    if flags.get("sweep_reject_high", False):
        return any((v is not None and pd.notna(v) and high > float(v)) for v in hi_lvls)
    if flags.get("sweep_reject_low", False):
        return any((v is not None and pd.notna(v) and low < float(v)) for v in lo_lvls)
    return False


def _infer_fbos_level_source(row: pd.Series, sweep_side: str | None, *, use_close_break: bool = False) -> str:
    """
    Infer the dominant swept/broken line source for FBOS diagnostics and filtering.
    """
    if sweep_side not in {"high", "low"}:
        return "OTHER"

    px = float(row["close"]) if use_close_break else (float(row["high"]) if sweep_side == "high" else float(row["low"]))
    if sweep_side == "high":
        refs = [
            ("EQH", row.get("nearest_eqh_price")),
            ("SESSION_H", row.get("session_high")),
            ("PDH", row.get("pdh")),
            ("PWH", row.get("pwh")),
        ]
        passed = [(name, abs(px - float(v))) for name, v in refs if v is not None and pd.notna(v) and px > float(v)]
    else:
        refs = [
            ("EQL", row.get("nearest_eql_price")),
            ("SESSION_L", row.get("session_low")),
            ("PDL", row.get("pdl")),
            ("PWL", row.get("pwl")),
        ]
        passed = [(name, abs(float(v) - px)) for name, v in refs if v is not None and pd.notna(v) and px < float(v)]

    if not passed:
        return "OTHER"
    passed.sort(key=lambda x: x[1])
    return passed[0][0]


def _fbos_confirmation_pass(
    *,
    bar_open: float,
    bar_close: float,
    bar_high: float,
    bar_low: float,
    sweep_side: str,
    min_body_ratio: float,
) -> tuple[bool, float]:
    bar_range = max(bar_high - bar_low, 1e-9)
    body_ratio = abs(bar_close - bar_open) / bar_range
    passes = bool(
        (
            (sweep_side == "low")
            and (bar_close > bar_open)
            and (body_ratio >= min_body_ratio)
        )
        or (
            (sweep_side == "high")
            and (bar_close < bar_open)
            and (body_ratio >= min_body_ratio)
        )
    )
    return passes, body_ratio


def _fbos_trigger_ctx(
    *,
    require_prior_accumulation_for_fbos: bool,
    prior_accumulation: bool,
    momentum_breakout: bool,
    structural_level_taken: bool,
    bias_aligned: bool,
    is_high_vol: bool,
    reclaim_strength_ok: bool,
    exhaustion_confirmed: bool,
    dol_visible: bool,
    rr_to_dol: float,
    post_sweep_confirmation: bool,
    ob_tap_bos_after: bool,
) -> dict:
    return {
        "require_prior_accumulation_for_fbos": require_prior_accumulation_for_fbos,
        "prior_accumulation": prior_accumulation,
        "momentum_breakout": momentum_breakout,
        "structural_level_taken": structural_level_taken,
        "bias_aligned": bias_aligned,
        "is_high_vol": is_high_vol,
        "reclaim_strength_ok": reclaim_strength_ok,
        "exhaustion_confirmed": exhaustion_confirmed,
        "dol_visible": dol_visible,
        "rr_to_dol": rr_to_dol,
        "post_sweep_confirmation": post_sweep_confirmation,
        "ob_tap_bos_after": ob_tap_bos_after,
    }


def _recent_opposite_break_context(
    *,
    break_ctx: list[dict],
    i: int,
    direction: str,
    lookback_bars: int = 3,
) -> bool:
    """
    RBOS uses a recent inducement/manipulation leg, not necessarily the break bar itself.
    We treat inducement and manipulation as the same pre-break pressure concept here.
    """
    start = max(0, i - lookback_bars)
    for j in range(start, i):
        flags = break_ctx[j]
        # Short RBOS needs a prior high-side run; long RBOS needs a prior low-side run.
        if direction == "long" and (flags["close_break_low"] or flags["sweep_reject_low"]):
            return True
        if direction == "short" and (flags["close_break_high"] or flags["sweep_reject_high"]):
            return True
    return False


def _recent_opposite_break_distance(
    *,
    break_ctx: list[dict],
    i: int,
    direction: str,
    lookback_bars: int = 3,
) -> int | None:
    """
    Returns the distance in bars from the most recent opposite-side break.
    Used to keep HTF RBOS from confirming on the very first response candle
    after a manipulative sweep.
    """
    start = max(0, i - lookback_bars)
    for j in range(i - 1, start - 1, -1):
        flags = break_ctx[j]
        if direction == "long" and (flags["close_break_low"] or flags["sweep_reject_low"]):
            return i - j
        if direction == "short" and (flags["close_break_high"] or flags["sweep_reject_high"]):
            return i - j
    return None


def _resolve_htf_drake_candle(
    df_5m: pd.DataFrame,
    hour_ts: pd.Timestamp,
    *,
    prior_high: float,
    prior_low: float,
    hour_open: float,
    hour_close: float,
) -> tuple[str, bool, str]:
    """
    Resolve same-bar outside candles using the hourly displacement side and the
    5m sequence inside the hour.
    Returns (direction, drake_candle, manipulation_side).
    """
    hour_start = pd.Timestamp(hour_ts)
    hour_end = hour_start + pd.Timedelta(hours=1)
    window = df_5m.loc[(df_5m.index >= hour_start) & (df_5m.index < hour_end)]
    if window.empty:
        direction = "long" if hour_close >= hour_open else "short"
        return direction, False, ""

    first_high_ts = None
    first_low_ts = None
    for ts, row in window.iterrows():
        if first_high_ts is None and float(row["high"]) > prior_high:
            first_high_ts = ts
        if first_low_ts is None and float(row["low"]) < prior_low:
            first_low_ts = ts
        if first_high_ts is not None and first_low_ts is not None:
            break

    drake_candle = bool(first_high_ts is not None and first_low_ts is not None)
    if not drake_candle:
        if first_high_ts is not None:
            return "short", False, "high"
        if first_low_ts is not None:
            return "long", False, "low"
        direction = "long" if hour_close >= hour_open else "short"
        return direction, False, ""

    manipulation_side = "high" if first_high_ts <= first_low_ts else "low"
    direction = "long" if hour_close >= hour_open else "short"
    return direction, True, manipulation_side


def run_pipeline_for_instrument(instrument_cfg: dict, data: pd.DataFrame) -> PipelineArtifacts:
    """
    Compact pipeline:
    vectorized phase/manipulation -> no-lookahead breaks -> bias -> DOL -> setup -> order -> lifecycle.
    """
    df = _require_ohlc(data).copy()

    amd_cfg: AMDConfig = instrument_cfg.get("amd_cfg", AMDConfig())
    break_cfg: BreakConfig = instrument_cfg.get("break_cfg", BreakConfig())
    _session_cfg: SessionConfig = instrument_cfg.get("session_cfg", SessionConfig())
    dol_cfg: DOLConfig = instrument_cfg.get("dol_cfg", DOLConfig())
    ob_cfg: OBConfig = instrument_cfg.get("ob_cfg", OBConfig())
    entry_cfg: EntryConfig = instrument_cfg.get("entry_cfg", EntryConfig())
    risk_cfg: RiskConfig = instrument_cfg.get("risk_cfg", RiskConfig())

    n = len(df)
    if n == 0:
        empty = pd.Series(dtype="object")
        return PipelineArtifacts(
            df=df,
            phase_series=empty,
            break_series=empty,
            bias_series=empty,
            dol_candidates=[],
            order_blocks=[],
            setup_candidates=[],
            setups=[],
            orders=[],
            trades=[],
            execution_events=[],
            htf_bias_series=empty,
            htf_regime_series=empty,
            htf_order_blocks=[],
            htf_break_points=[],
        )

    o = df["open"].to_numpy()
    h = df["high"].to_numpy()
    l = df["low"].to_numpy()
    c = df["close"].to_numpy()
    idx = df.index

    phase_series_obj = compute_phase_series(df, amd_cfg)
    phase_vals: list[AMDPhase] = phase_series_obj.tolist()
    phase_acc_arr = (phase_series_obj == AMDPhase.ACCUMULATION).astype(int).to_numpy()
    accum_recent = (
        pd.Series(phase_acc_arr, index=df.index)
        .shift(1)
        .rolling(12, min_periods=1)
        .sum()
        .fillna(0)
        .to_numpy()
        >= 6
    )
    manip_flags = compute_manipulation_series(df, amd_cfg).to_numpy()
    tr_series = (df["high"] - df["low"]).astype(float)
    atr20 = tr_series.rolling(20, min_periods=20).mean()
    ema20 = df["close"].ewm(span=20, adjust=False).mean()
    vol_q2 = float(atr20.quantile(0.66)) if not atr20.dropna().empty else float("inf")
    high_vol_arr = (atr20 > vol_q2).fillna(False).to_numpy()
    br = _break_matrix_no_lookahead(df, lookback=getattr(ob_cfg, "structure_lookback", 20))
    fbos_leadin_flags = _fbos_manipulation_leadin_matrix(
        df,
        br,
        leadin_bars=max(3, int(getattr(amd_cfg, "manipulation_min_consecutive", 3))),
        min_body_ratio=max(0.5, float(getattr(amd_cfg, "manipulation_min_body_ratio", 0.6))),
    )
    htf_fbos_ctx = _build_15m_fbos_context(
        df,
        lookback=getattr(ob_cfg, "structure_lookback", 20),
        pip_value=risk_cfg.pip_value,
        leadin_bars=max(2, int(getattr(amd_cfg, "manipulation_min_consecutive", 3))),
        min_body_ratio=max(0.5, float(getattr(amd_cfg, "manipulation_min_body_ratio", 0.6))),
    )
    htf_bias_series, htf_regime_series, htf_bias_series_raw, htf_regime_series_raw, htf_order_blocks, htf_break_points = _build_htf_regime_context(
        df,
        lookback=getattr(ob_cfg, "structure_lookback", 20),
        amd_cfg=amd_cfg,
        break_cfg=break_cfg,
        ob_cfg=ob_cfg,
        carry_bias=bool(instrument_cfg.get("carry_bias", True)),
    )
    htf_bias_enum = htf_bias_series.map(lambda v: BiasDirection(v) if v in BiasDirection._value2member_map_ else BiasDirection.NEUTRAL)

    break_vals: list[BreakType] = []
    bias_vals: list[BiasDirection] = []
    bias_source_vals: list[str] = []
    break_ctx: list[dict] = []

    prev_bias = BiasDirection.NEUTRAL
    prev_bias_source = "NEUTRAL"
    carry_bias = bool(instrument_cfg.get("carry_bias", True))
    active_cycle_bias = BiasDirection.NEUTRAL
    active_cycle_dol_hit = False
    active_cycle_dol_price: float | None = None
    cycle_dol_hit_vals: list[bool] = []
    cycle_dol_price_vals: list[float | None] = []
    for i in range(n):
        flags = {
            "close_break_high": bool(br["close_break_high"][i]),
            "close_break_low": bool(br["close_break_low"][i]),
            "sweep_reject_high": bool(br["sweep_reject_high"][i]),
            "sweep_reject_low": bool(br["sweep_reject_low"][i]),
            "breaks_level": bool(br["breaks_level"][i]),
        }
        break_ctx.append(flags)
        phase_before = phase_vals[i - 1] if i > 0 else phase_vals[i]
        reversal_now = bool(flags["sweep_reject_high"] or flags["sweep_reject_low"])
        # FBOS uses prior lead-in momentum into sweep (old scanner behavior).
        # RBOS uses current momentum break continuation.
        manip_sig = bool(fbos_leadin_flags[i]) if reversal_now else bool(manip_flags[i])
        rbos_confirm = bool(
            (flags["close_break_high"] or flags["close_break_low"])
            and _recent_opposite_break_context(
                break_ctx=break_ctx,
                i=i,
                direction="long" if flags["close_break_low"] else "short",
                lookback_bars=3,
            )
        )

        btype = classify_break(
            phase_before_break=phase_before,
            breaks_significant_level=bool(flags["breaks_level"]),
            manipulation_signature_present=bool(manip_sig or rbos_confirm),
            later_reversal_seen=reversal_now,
            rbos_confirmation_seen=rbos_confirm,
            cfg=break_cfg,
        )
        break_vals.append(btype)

        # Optional invalidation reset before deriving fresh signal.
        if invalidate_bias(
            {
                "bias_ob_broken": bool(df.iloc[i].get("bias_ob_broken", False)),
                "primary_dol_hit": active_cycle_dol_hit,
                "htf_leg_taken_opposite": bool(df.iloc[i].get("htf_leg_taken_opposite", False)),
            }
        ):
            prev_bias = BiasDirection.NEUTRAL
            prev_bias_source = "NEUTRAL"
            active_cycle_bias = BiasDirection.NEUTRAL
            active_cycle_dol_hit = False
            active_cycle_dol_price = None

        fresh_bias = derive_bias(
            {
                "rbos_up": btype == BreakType.RBOS and bool(flags["close_break_high"]),
                "rbos_down": btype == BreakType.RBOS and bool(flags["close_break_low"]),
            }
        )
        if fresh_bias != BiasDirection.NEUTRAL:
            prev_bias = fresh_bias
            bias = fresh_bias
            prev_bias_source = "RBOS_UP" if fresh_bias == BiasDirection.BULLISH else "RBOS_DOWN"
            bias_source = prev_bias_source
        else:
            bias = prev_bias if carry_bias else BiasDirection.NEUTRAL
            bias_source = prev_bias_source if carry_bias else "NEUTRAL"
        bias_vals.append(bias)
        bias_source_vals.append(bias_source)

        if bias != active_cycle_bias:
            active_cycle_bias = bias
            active_cycle_dol_hit = False
            active_cycle_dol_price = None

        row = df.iloc[i]
        target_dol = (
            select_target_dol(
                _dol_ctx_from_row(
                    row,
                    bias,
                    pip_value=risk_cfg.pip_value,
                    current_timestamp=idx[i],
                    htf_order_blocks=htf_order_blocks,
                ),
                cfg=dol_cfg,
            )
            if bias != BiasDirection.NEUTRAL
            else None
        )
        target_price = None if target_dol is None else float(target_dol.price)
        if target_price is not None:
            active_cycle_dol_price = target_price

        current_bar_hit_dol = bool(
            target_price is not None
            and (
                (bias == BiasDirection.BULLISH and float(row["high"]) >= float(target_price))
                or (bias == BiasDirection.BEARISH and float(row["low"]) <= float(target_price))
            )
        )
        if current_bar_hit_dol:
            active_cycle_dol_hit = True

        cycle_dol_hit_vals.append(active_cycle_dol_hit)
        cycle_dol_price_vals.append(active_cycle_dol_price)

    htf_dol_hit_series_raw = (
        pd.Series(cycle_dol_hit_vals, index=df.index, name="cycle_dol_hit")
        .resample("1h")
        .max()
        .reindex(htf_bias_series_raw.index, method="ffill")
        .fillna(False)
    )

    phase_series = pd.Series([p.value for p in phase_vals], index=df.index, name="amd_phase")
    break_series = pd.Series([b.value for b in break_vals], index=df.index, name="break_type")
    bias_series = pd.Series([b.value for b in bias_vals], index=df.index, name="bias")

    # Candidate pool from most recent context for diagnostics only.
    last = df.iloc[-1] if n else pd.Series(dtype=float)
    dol_candidates = collect_dol_candidates(
        _dol_ctx_from_row(
            last,
            bias_vals[-1] if bias_vals else BiasDirection.NEUTRAL,
            pip_value=risk_cfg.pip_value,
            current_timestamp=df.index[-1] if len(df) else None,
            htf_order_blocks=htf_order_blocks,
        )
    )

    order_blocks: list[OBZone] = detect_inducement_obs(df, {"ob_cfg": ob_cfg})
    final_htf_bias = htf_bias_enum.iloc[-1] if len(htf_bias_enum) else BiasDirection.NEUTRAL
    final_htf_regime = str(htf_regime_series.iloc[-1]) if len(htf_regime_series) else "ACCUMULATION"
    for ob in order_blocks:
        if ob.idx < len(htf_bias_enum):
            origin_bias = htf_bias_enum.iloc[ob.idx]
            ob.origin_bias = origin_bias.value if isinstance(origin_bias, BiasDirection) else str(origin_bias)
        update_ob_lifecycle(
            ob,
            htf_bias=final_htf_bias if isinstance(final_htf_bias, BiasDirection) else BiasDirection.NEUTRAL,
            htf_regime=final_htf_regime,
            htf_dol_hit=bool(cycle_dol_hit_vals[-1]) if cycle_dol_hit_vals else False,
        )
    for ob in htf_order_blocks:
        opened_at = pd.Timestamp(ob.timestamp)
        closed_at = opened_at + pd.Timedelta(hours=1)
        update_ob_lifecycle(
            ob,
            htf_bias=BiasDirection.NEUTRAL,
            htf_regime="ACCUMULATION",
            htf_dol_hit=False,
            opened_at=opened_at,
            closed_at=closed_at,
            visible_at=closed_at,
            eligible_at=closed_at,
            origin_candle=opened_at,
            tap_count=ob.tap_count,
        )
    finalize_ob_cycle(
        htf_order_blocks,
        htf_bias_series=htf_bias_series_raw,
        htf_regime_series=htf_regime_series_raw,
        htf_dol_hit_series=htf_dol_hit_series_raw,
    )
    setup_candidates: list[SetupSignal] = []
    setups: list[SetupSignal] = []
    break_candidate_idx = [i for i, b in enumerate(break_vals) if b != BreakType.NONE]
    # FBOS conservative sequencing: break bar -> confirmation bar -> entry on next bar open.
    fbos_candidate_idx = [
        i + 2
        for i in range(n - 2)
        if bool(br["close_break_high"][i]) or bool(br["close_break_low"][i])
    ]
    htf_fbos_candidate_idx = [int(df.index.get_loc(ts)) for ts in htf_fbos_ctx if ts in df.index]

    # Mitigation can trigger after OB tap on bars that are not necessarily break bars.
    mitigation_ctx_cache: dict[int, dict] = {}
    mitigation_candidate_idx: list[int] = []
    if bool(instrument_cfg.get("emit_both_models", True)) or bool(instrument_cfg.get("prefer_mitigation", False)):
        for i in range(n):
            bias = bias_vals[i]
            if bias == BiasDirection.NEUTRAL:
                continue
            direction = "long" if bias == BiasDirection.BULLISH else "short"
            htf_bias = htf_bias_enum.iloc[i] if i < len(htf_bias_enum) else BiasDirection.NEUTRAL
            htf_regime = str(htf_regime_series.iloc[i]) if i < len(htf_regime_series) else "ACCUMULATION"
            mctx = evaluate_mitigation_context(
                df=df,
                i=i,
                direction=direction,
                order_blocks=order_blocks,
                ob_cfg=ob_cfg,
                htf_bias=htf_bias,
                htf_regime=htf_regime,
                htf_dol_hit=cycle_dol_hit_vals[i] if i < len(cycle_dol_hit_vals) else False,
            )
            mitigation_ctx_cache[i] = mctx
            if mctx["valid_inducement_ob"] and mctx["m5_bos_after_tap"]:
                mitigation_candidate_idx.append(i)

    candidate_idx = sorted(
        set(break_candidate_idx).union(mitigation_candidate_idx).union(fbos_candidate_idx).union(htf_fbos_candidate_idx)
    )
    for i in candidate_idx:
        ts = pd.Timestamp(idx[i])
        row = df.iloc[i]  # slower path only for candidate bars
        flags = break_ctx[i]
        bias = bias_vals[i]
        bias_source = bias_source_vals[i]
        htf_bias = htf_bias_enum.iloc[i] if i < len(htf_bias_enum) else BiasDirection.NEUTRAL
        htf_regime = str(htf_regime_series.iloc[i]) if i < len(htf_regime_series) else "ACCUMULATION"
        active_cycle_dol_hit_now = cycle_dol_hit_vals[i] if i < len(cycle_dol_hit_vals) else False
        active_cycle_dol_price_now = cycle_dol_price_vals[i] if i < len(cycle_dol_price_vals) else None
        if bias == BiasDirection.NEUTRAL:
            continue
        direction = "long" if bias == BiasDirection.BULLISH else "short"
        session = map_session(ts)
        external_edge = bool(flags["sweep_reject_high"] or flags["sweep_reject_low"])
        aggressive_sweep_now = bool(external_edge and _is_aggressive_sweep_window(ts, entry_cfg))
        internal_continuation = bool(row.get("internal_continuation_setup", False))
        default_model = choose_entry_model(
            {
                "external_range_edge_setup": external_edge,
                "aggressive_sweep_setup": aggressive_sweep_now,
                "prefer_mitigation": bool(instrument_cfg.get("prefer_mitigation", False)),
                "internal_continuation_setup": internal_continuation,
                "session": session.value,
            },
            cfg=entry_cfg,
        )
        emit_both = bool(instrument_cfg.get("emit_both_models", True))
        if aggressive_sweep_now:
            model_candidates = [EntryModel.AGGRESSIVE_SWEEP]
            if emit_both:
                model_candidates.append(EntryModel.MITIGATION)
        else:
            model_candidates = [EntryModel.FBOS, EntryModel.MITIGATION] if emit_both else [default_model]
        if emit_both and htf_regime != "ACCUMULATION":
            model_candidates.append(EntryModel.SMT_REACTION)

        target_dol = select_target_dol(
            _dol_ctx_from_row(
                row,
                bias,
                pip_value=risk_cfg.pip_value,
                current_timestamp=ts,
                htf_order_blocks=htf_order_blocks,
            ),
            cfg=dol_cfg,
        )
        target_price = None if target_dol is None else float(target_dol.price)
        for model in model_candidates:
            trig = None
            entry_price = None
            setup_direction = direction
            setup_sweep_low = float(l[i])
            setup_sweep_high = float(h[i])
            ob_low = row.get("ob_low")
            ob_high = row.get("ob_high")
            fbos_penetration = None
            sweep_side = None
            fbos_level_source = None
            aggressive_level_source = None
            sweep_penetration = None
            gate_report: dict[str, bool] = {}

            if model == EntryModel.AGGRESSIVE_SWEEP:
                if not external_edge:
                    continue
                if flags["sweep_reject_high"]:
                    sweep_side = "high"
                    sweep_penetration = float(br["sweep_high_penetration"][i]) / max(risk_cfg.pip_value, 1e-9)
                    setup_direction = "short"
                    break_level = br["prior_high"][i]
                elif flags["sweep_reject_low"]:
                    sweep_side = "low"
                    sweep_penetration = float(br["sweep_low_penetration"][i]) / max(risk_cfg.pip_value, 1e-9)
                    setup_direction = "long"
                    break_level = br["prior_low"][i]
                else:
                    continue
                if pd.isna(break_level) or sweep_penetration < float(entry_cfg.aggressive_sweep_min_penetration_pips):
                    continue
                entry_price = float(break_level)
                rr_to_dol = _estimate_rr_to_dol(
                    row,
                    setup_direction,
                    entry_price,
                    target_price,
                    risk_cfg,
                    model=model,
                )
                aggressive_ctx = {
                    "session_hour_london": _london_hour(ts),
                    "sweep_reject_high": bool(flags["sweep_reject_high"]),
                    "sweep_reject_low": bool(flags["sweep_reject_low"]),
                    "sweep_penetration_pips": sweep_penetration,
                    "structural_level_taken": _major_level_taken(row, flags, use_close_break=False),
                    "bias_aligned": (bias != BiasDirection.NEUTRAL) and (setup_direction == direction),
                    "dol_visible": target_price is not None,
                    "rr_to_dol": rr_to_dol,
                }
                gate_report = aggressive_sweep_gate_report(aggressive_ctx, cfg=entry_cfg)
                trig = trigger_aggressive_sweep(aggressive_ctx, cfg=entry_cfg)
                if trig is None:
                    continue
                aggressive_level_source = _infer_fbos_level_source(row, sweep_side, use_close_break=False)

            elif model == EntryModel.FBOS:
                htf_ctx = htf_fbos_ctx.get(ts)
                if htf_ctx is not None and session.value == "LONDON":
                    htf_row = htf_ctx["row"]
                    htf_flags = htf_ctx["flags"]
                    sweep_high_pen_pips = float(htf_ctx["sweep_high_pen_pips"])
                    sweep_low_pen_pips = float(htf_ctx["sweep_low_pen_pips"])

                    if htf_flags.get("sweep_reject_high", False):
                        fbos_direction = "short"
                        fbos_penetration = sweep_high_pen_pips
                        sweep_side = "high"
                        break_level = htf_ctx["prior_high"]
                    elif htf_flags.get("sweep_reject_low", False):
                        fbos_direction = "long"
                        fbos_penetration = sweep_low_pen_pips
                        sweep_side = "low"
                        break_level = htf_ctx["prior_low"]
                    else:
                        continue

                    if fbos_penetration < float(entry_cfg.fbos_min_penetration_pips):
                        continue

                    entry_price = float(o[i])
                    rr_to_dol = _estimate_rr_to_dol(
                        htf_row,
                        fbos_direction,
                        entry_price,
                        target_price,
                        risk_cfg,
                        model=model,
                    )
                    post_sweep_confirmation, _ = _fbos_confirmation_pass(
                        bar_open=float(htf_row["open"]),
                        bar_close=float(htf_row["close"]),
                        bar_high=float(htf_row["high"]),
                        bar_low=float(htf_row["low"]),
                        sweep_side=sweep_side,
                        min_body_ratio=float(entry_cfg.fbos_confirmation_min_body_ratio),
                    )

                    fbos_level_source = _infer_fbos_level_source(htf_row, sweep_side, use_close_break=False)
                    fbos_ctx = _fbos_trigger_ctx(
                        require_prior_accumulation_for_fbos=entry_cfg.require_prior_accumulation_for_fbos,
                        prior_accumulation=bool(accum_recent[i - 1]) if i > 0 else False,
                        momentum_breakout=bool(htf_ctx["momentum_breakout"]),
                        structural_level_taken=_major_level_taken(htf_row, htf_flags, use_close_break=False),
                        bias_aligned=(bias != BiasDirection.NEUTRAL) and (fbos_direction == direction),
                        is_high_vol=bool(high_vol_arr[i - 1]) if i > 0 else False,
                        reclaim_strength_ok=True,
                        exhaustion_confirmed=True,
                        dol_visible=target_price is not None,
                        rr_to_dol=rr_to_dol,
                        post_sweep_confirmation=post_sweep_confirmation,
                        ob_tap_bos_after=bool(row.get("ob_tap_bos_after", False)),
                    )
                    gate_report = fbos_gate_report(fbos_ctx, cfg=entry_cfg)
                    trig = trigger_fbos(fbos_ctx, cfg=entry_cfg)
                    setup_sweep_low = float(htf_row["low"])
                    setup_sweep_high = float(htf_row["high"])
                    setup_direction = fbos_direction
                else:
                    # Enforce spec sequencing:
                    # t0: structural close-break, t1: opposite-direction confirmation, t2: entry.
                    if i < 2:
                        continue
                    break_i = i - 2
                    conf_i = i - 1
                    break_flags = break_ctx[break_i]
                    close_high_pen_pips = float(br["close_high_penetration"][break_i]) / max(risk_cfg.pip_value, 1e-9)
                    close_low_pen_pips = float(br["close_low_penetration"][break_i]) / max(risk_cfg.pip_value, 1e-9)
                    fbos_direction, fbos_penetration, sweep_side = _resolve_fbos_break_direction(
                        flags=break_flags,
                        close_high_pen_pips=close_high_pen_pips,
                        close_low_pen_pips=close_low_pen_pips,
                        min_penetration_pips=entry_cfg.fbos_min_penetration_pips,
                    )
                    if fbos_direction is None:
                        continue

                    break_level = None
                    if sweep_side == "high":
                        break_level = br["prior_high"][break_i]
                    elif sweep_side == "low":
                        break_level = br["prior_low"][break_i]

                    # Entry at next candle open after confirmation (t2 open).
                    entry_price = float(o[i])
                    if entry_price is None:
                        continue
                    rr_to_dol = _estimate_rr_to_dol(
                        row,
                        fbos_direction,
                        entry_price,
                        target_price,
                        risk_cfg,
                        model=model,
                    )

                    conf_open = float(o[conf_i])
                    conf_close = float(c[conf_i])
                    conf_high = float(h[conf_i])
                    conf_low = float(l[conf_i])
                    post_sweep_confirmation, conf_body_ratio = _fbos_confirmation_pass(
                        bar_open=conf_open,
                        bar_close=conf_close,
                        bar_high=conf_high,
                        bar_low=conf_low,
                        sweep_side=sweep_side,
                        min_body_ratio=float(entry_cfg.fbos_confirmation_min_body_ratio),
                    )
                    break_range = max(float(h[break_i]) - float(l[break_i]), 1e-9)
                    reclaim_strength_ok = True
                    if float(entry_cfg.fbos_min_reclaim_frac) > 0.0:
                        if break_level is None or pd.isna(break_level):
                            reclaim_strength_ok = False
                        elif sweep_side == "low":
                            sweep_extreme = float(l[break_i])
                            reclaim_strength_ok = ((conf_close - sweep_extreme) / break_range) >= float(
                                entry_cfg.fbos_min_reclaim_frac
                            )
                        else:
                            sweep_extreme = float(h[break_i])
                            reclaim_strength_ok = ((sweep_extreme - conf_close) / break_range) >= float(
                                entry_cfg.fbos_min_reclaim_frac
                            )
                    break_open = float(o[break_i])
                    break_close = float(c[break_i])
                    break_high = float(h[break_i])
                    break_low = float(l[break_i])
                    break_body_ratio = abs(break_close - break_open) / max(break_high - break_low, 1e-9)
                    ema20_break = float(ema20.iloc[break_i])
                    atr20_break = float(atr20.iloc[break_i])
                    stretch = abs(break_close - ema20_break) / max(atr20_break, 1e-9)
                    entry_low = float(l[i])
                    entry_high = float(h[i])
                    no_rebreak = (entry_low > break_low) if sweep_side == "low" else (entry_high < break_high)
                    break_mid = (break_high + break_low) / 2.0
                    micro_flip = (conf_close > break_mid) if sweep_side == "low" else (conf_close < break_mid)
                    exhaustion_confirmed = bool(
                        (break_body_ratio >= float(entry_cfg.fbos_exhaustion_min_impulse_body))
                        and (conf_body_ratio <= float(entry_cfg.fbos_exhaustion_max_stall_body))
                        and (stretch >= float(entry_cfg.fbos_exhaustion_stretch_min))
                        and bool(reclaim_strength_ok)
                        and bool(micro_flip)
                        and bool(no_rebreak)
                    )
                    fbos_level_source = _infer_fbos_level_source(
                        df.iloc[break_i],
                        sweep_side,
                        use_close_break=True,
                    )
                    fbos_ctx = _fbos_trigger_ctx(
                        require_prior_accumulation_for_fbos=entry_cfg.require_prior_accumulation_for_fbos,
                        prior_accumulation=bool(accum_recent[break_i]),
                        momentum_breakout=bool(fbos_leadin_flags[break_i]),
                        structural_level_taken=_major_level_taken(
                            df.iloc[break_i], break_flags, use_close_break=True
                        ),
                        bias_aligned=(bias != BiasDirection.NEUTRAL) and (fbos_direction == direction),
                        is_high_vol=bool(high_vol_arr[break_i]),
                        reclaim_strength_ok=bool(reclaim_strength_ok),
                        exhaustion_confirmed=bool(exhaustion_confirmed),
                        dol_visible=target_price is not None,
                        rr_to_dol=rr_to_dol,
                        post_sweep_confirmation=post_sweep_confirmation,
                        ob_tap_bos_after=bool(row.get("ob_tap_bos_after", False)),
                    )
                    gate_report = fbos_gate_report(fbos_ctx, cfg=entry_cfg)
                    trig = trigger_fbos(fbos_ctx, cfg=entry_cfg)
                    setup_sweep_low = float(l[break_i])
                    setup_sweep_high = float(h[break_i])
                    setup_direction = fbos_direction

            elif model == EntryModel.SMT_REACTION:
                if htf_bias == BiasDirection.NEUTRAL or htf_regime == "ACCUMULATION":
                    continue
                smt_direction = "short" if htf_bias == BiasDirection.BULLISH else "long"
                smt_mctx = evaluate_mitigation_context(
                    df=df,
                    i=i,
                    direction=smt_direction,
                    order_blocks=order_blocks,
                    ob_cfg=ob_cfg,
                    htf_bias=htf_bias,
                    htf_regime=htf_regime,
                    htf_dol_hit=active_cycle_dol_hit_now,
                )
                if smt_mctx["active_ob_role"] != ZoneRole.SMT.value:
                    continue
                entry_price = _limit_entry_price_from_row(row, EntryModel.MITIGATION, smt_direction)
                if entry_price is None:
                    continue
                rr_to_dol = _estimate_rr_to_dol(
                    row,
                    smt_direction,
                    entry_price,
                    target_price,
                    risk_cfg,
                    model=model,
                )
                smt_ctx = {
                    "zone_role": smt_mctx["active_ob_role"],
                    "ob_tap_seen": smt_mctx["ob_tap_seen"],
                    "reaction_induced_structure": smt_mctx.get("reaction_induced_structure", False),
                    "m5_bos_after_tap": smt_mctx["m5_bos_after_tap"],
                    "dol_visible": target_price is not None,
                    "rr_to_dol": rr_to_dol,
                }
                gate_report = smt_reaction_gate_report(smt_ctx, cfg=entry_cfg)
                trig = trigger_smt_reaction(smt_ctx, cfg=entry_cfg)
                if trig is None:
                    continue
                mctx = smt_mctx
                ob_low = smt_mctx.get("active_ob_low")
                ob_high = smt_mctx.get("active_ob_high")
                setup_direction = smt_direction

            elif model == EntryModel.MITIGATION:
                entry_price = _limit_entry_price_from_row(row, model, direction)
                if entry_price is None:
                    continue
                rr_to_dol = _estimate_rr_to_dol(
                    row,
                    direction,
                    entry_price,
                    target_price,
                    risk_cfg,
                    model=model,
                )
                mctx = mitigation_ctx_cache.get(i)
                if mctx is None or active_cycle_dol_hit_now:
                    mctx = evaluate_mitigation_context(
                        df=df,
                        i=i,
                        direction=direction,
                        order_blocks=order_blocks,
                        ob_cfg=ob_cfg,
                        htf_bias=htf_bias,
                        htf_regime=htf_regime,
                        htf_dol_hit=active_cycle_dol_hit_now,
                    )
                mitigation_sl = compute_sl(
                    {
                        "direction": direction,
                        "ob_low": mctx.get("active_ob_low"),
                        "ob_high": mctx.get("active_ob_high"),
                        "model": model.value,
                    },
                    entry_price=float(row["open"]),
                    cfg=risk_cfg,
                )
                mitigation_rr = 0.0
                if mitigation_sl is not None and target_price is not None:
                    mitigation_risk = abs(float(row["open"]) - float(mitigation_sl))
                    mitigation_reward = (
                        float(target_price) - float(row["open"])
                        if direction == "long"
                        else float(row["open"]) - float(target_price)
                    )
                    if mitigation_risk > 0 and mitigation_reward > 0:
                        mitigation_rr = mitigation_reward / mitigation_risk
                mitigation_ctx = {
                    "valid_inducement_ob": mctx["valid_inducement_ob"],
                    "ob_previously_tapped": mctx["ob_previously_tapped"],
                    "bias_aligned": bias != BiasDirection.NEUTRAL,
                    "dol_visible": target_price is not None,
                    "rr_to_dol": mitigation_rr,
                    "ob_tap_seen": mctx["ob_tap_seen"],
                    "m5_bos_after_tap": mctx["m5_bos_after_tap"],
                    "extreme_confluence": bool(row.get("extreme_confluence", False)),
                    "zone_role": mctx.get("active_ob_role"),
                }
                gate_report = mitigation_gate_report(mitigation_ctx, cfg=entry_cfg)
                trig = trigger_mitigation(mitigation_ctx, cfg=entry_cfg)
                ob_low = mctx.get("active_ob_low")
                ob_high = mctx.get("active_ob_high")
                setup_direction = direction
                setup_sweep_low = float(l[i])
                setup_sweep_high = float(h[i])

            else:
                continue

            if model in {EntryModel.MITIGATION, EntryModel.SMT_REACTION}:
                ob_role = mctx.get("active_ob_role")
                ob_role_reason = mctx.get("active_ob_role_reason")
                ob_origin_bias = mctx.get("active_ob_origin_bias")
            else:
                aligned_htf = (
                    (htf_bias == BiasDirection.BULLISH and setup_direction == "long")
                    or (htf_bias == BiasDirection.BEARISH and setup_direction == "short")
                )
                ob_role = (
                    ZoneRole.OB.value
                    if aligned_htf and htf_regime != "ACCUMULATION" and not active_cycle_dol_hit_now
                    else ZoneRole.SMT.value
                )
                ob_role_reason = "bias_aligned" if ob_role == ZoneRole.OB.value else "counter_bias_liquidity"
                ob_origin_bias = htf_bias.value

            signal_tags = {
                "break_type": btype.value,
                "rbos_event": bool(btype == BreakType.RBOS),
                "trigger": trig.value if trig is not None else None,
                "emitted": bool(trig is not None),
                "session": session.value,
                "htf_bias": htf_bias.value if isinstance(htf_bias, BiasDirection) else str(htf_bias),
                "htf_regime": htf_regime,
                "htf_dol_hit": bool(active_cycle_dol_hit_now),
                "bias": bias.value,
                "bias_source": bias_source,
                "ob_role": ob_role,
                "ob_role_reason": ob_role_reason,
                "ob_origin_bias": ob_origin_bias,
                "trade_aligned_with_bias": bool(
                    (bias == BiasDirection.BULLISH and setup_direction == "long")
                    or (bias == BiasDirection.BEARISH and setup_direction == "short")
                ),
                "trade_aligned_with_rbos_bias": bool(
                    bias_source in {"RBOS_UP", "RBOS_DOWN"}
                    and (
                        (bias == BiasDirection.BULLISH and setup_direction == "long")
                        or (bias == BiasDirection.BEARISH and setup_direction == "short")
                    )
                ),
                "target_dol_price": target_price,
                "sweep_low": setup_sweep_low,
                "sweep_high": setup_sweep_high,
                "fbos_penetration_pips": fbos_penetration if model == EntryModel.FBOS else None,
                "fbos_sweep_side": sweep_side if model == EntryModel.FBOS else None,
                "fbos_level_source": fbos_level_source if model == EntryModel.FBOS else None,
                "fbos_source_tf": (htf_ctx["source_tf"] if model == EntryModel.FBOS and htf_ctx is not None and session.value == "LONDON" else "5m") if model == EntryModel.FBOS else None,
                "aggressive_sweep_penetration_pips": sweep_penetration if model == EntryModel.AGGRESSIVE_SWEEP else None,
                "aggressive_sweep_side": sweep_side if model == EntryModel.AGGRESSIVE_SWEEP else None,
                "aggressive_sweep_level_source": aggressive_level_source if model == EntryModel.AGGRESSIVE_SWEEP else None,
                "aggressive_sweep_source_tf": "5m" if model == EntryModel.AGGRESSIVE_SWEEP else None,
                "smt_reaction_source_tf": "5m" if model == EntryModel.SMT_REACTION else None,
                "cycle_dol_price": active_cycle_dol_price_now,
                "aggressive_sweep_ob_candidate": bool(
                    model == EntryModel.AGGRESSIVE_SWEEP
                    and (
                        (setup_direction == "short" and float(row["close"]) > float(row["open"]))
                        or (setup_direction == "long" and float(row["close"]) < float(row["open"]))
                    )
                ),
                "aggressive_sweep_ob_low": float(row["low"]) if model == EntryModel.AGGRESSIVE_SWEEP else None,
                "aggressive_sweep_ob_high": float(row["high"]) if model == EntryModel.AGGRESSIVE_SWEEP else None,
                "rbos_logged": bool(btype == BreakType.RBOS),
                "rbos_confirmation_seen": bool(rbos_confirm),
                "gate_report": gate_report,
                "ob_low": ob_low,
                "ob_high": ob_high,
            }
            candidate_signal = SetupSignal(
                timestamp=ts,
                model=model,
                direction=setup_direction,
                entry_price=entry_price if entry_price is not None else float("nan"),
                tags=signal_tags,
            )
            setup_candidates.append(candidate_signal)
            if trig is None:
                continue
            setups.append(candidate_signal)

    orders = []
    for s in setups:
        inv = check_invalidation(
            {
                "counter_htf_bias": bool(
                    (s.direction == "short")
                    and (s.model == EntryModel.FBOS)
                    and bool(instrument_cfg.get("block_short_fbos", False))
                ),
                "news_window_block": bool(s.tags.get("news_window_block", False)),
            }
        )
        if s.model in {EntryModel.FBOS, EntryModel.AGGRESSIVE_SWEEP}:
            # User rule: avoid FBOS during Asia session.
            if s.model == EntryModel.FBOS and str(s.tags.get("session", "")) == "ASIA":
                continue
        if inv is not None:
            continue
        order = build_order(
            {
                "timestamp": s.timestamp,
                "direction": s.direction,
                "entry_price": s.entry_price,
                "sweep_low": s.tags.get("sweep_low"),
                "sweep_high": s.tags.get("sweep_high"),
                "ob_low": s.tags.get("ob_low"),
                "ob_high": s.tags.get("ob_high"),
                "target_dol_price": s.tags.get("target_dol_price"),
                "model": s.model.value,
            },
            risk_cfg=risk_cfg,
        )
        if order is not None:
            orders.append(order)

    trades: list[TradeRuntime] = []
    execution_events: list[dict] = []
    m1_df = instrument_cfg.get("ohlc_1m")
    if m1_df is not None and len(m1_df) > 0:
        # M1 must be sorted and share timezone with the 5m frame.
        m1_df = m1_df.sort_index()

    for order in orders:
        trade = simulate_limit_order_trade(
            order=order,
            ohlc_5m=df,
            signal_ts=order.timestamp,
            risk_cfg=risk_cfg,
            m1_df=m1_df,
            events=execution_events,
        )
        if trade is not None:
            trades.append(trade)

    return PipelineArtifacts(
        df=df,
        phase_series=phase_series,
        break_series=break_series,
        bias_series=bias_series,
        dol_candidates=dol_candidates,
        order_blocks=order_blocks,
        setup_candidates=setup_candidates,
        setups=setups,
        orders=orders,
        trades=trades,
        execution_events=execution_events,
        htf_bias_series=htf_bias_series,
        htf_regime_series=htf_regime_series,
        htf_order_blocks=htf_order_blocks,
        htf_break_points=htf_break_points,
    )
