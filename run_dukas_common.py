"""Shared helpers for the v2 Dukas runners."""

from __future__ import annotations

from engine import BreakConfig, EntryConfig, RiskConfig


def infer_pair_params(pair: str) -> tuple[float, float, float]:
    """
    Return (pip_value, spread_pips, slip_pips) for the common runner pairs.
    """
    is_jpy = pair.upper().endswith("JPY")
    pip_value = 0.01 if is_jpy else 0.0001
    spread_pips = 1.2 if is_jpy else 0.2
    slip_pips = 0.3 if is_jpy else 0.1
    return pip_value, spread_pips, slip_pips


def build_default_runner_config(
    *,
    ohlc_1m,
    pip_value: float,
    spread_pips: float,
    slip_pips: float,
) -> dict:
    """
    Shared baseline config for the month/year runner scripts.
    """
    return {
        "ohlc_1m": ohlc_1m,
        "assume_htf_poi_tapped": True,
        "prefer_mitigation": True,
        "emit_both_models": True,
        "carry_bias": True,
        "break_cfg": BreakConfig(
            require_reversal_for_fbos=True,
            require_manipulation_context_for_rbos=True,
            require_accumulation_for_fbos=False,
        ),
        "entry_cfg": EntryConfig(
            beginner_mode=False,
            fbos_mode="aggressive",
            mitigation_mode="conservative",
            min_rr=2.0,
            require_prior_accumulation_for_fbos=False,
            fbos_min_penetration_pips=0.0,
            fbos_gate_prior_accumulation=False,
            fbos_gate_momentum_breakout=False,
            fbos_gate_structural_level_taken=False,
            fbos_gate_bias_alignment=True,
            fbos_gate_rr_to_dol=False,
            fbos_gate_post_confirmation=False,
            fbos_gate_require_high_vol=True,
            fbos_gate_reclaim_strength=True,
            fbos_min_reclaim_frac=0.20,
            fbos_gate_exhaustion_confirmation=True,
            fbos_exhaustion_stretch_min=1.0,
            fbos_exhaustion_min_impulse_body=0.60,
            fbos_exhaustion_max_stall_body=0.60,
        ),
        "risk_cfg": RiskConfig(
            account_balance=10000.0,
            risk_per_trade=0.01,
            pip_value=pip_value,
            min_stop_pips=3.0,
            min_rr_threshold=2.0,
            fill_timeout_bars=24,
            fail_fast_bars=3,
            fail_fast_rr=0.5,
            spread_pips=spread_pips,
            entry_slippage_pips=slip_pips,
            exit_slippage_pips=slip_pips,
            commission_per_lot_round_turn=7.0,
        ),
    }
