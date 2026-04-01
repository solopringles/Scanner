"""Tunable thresholds for phase and break classification."""

from dataclasses import dataclass


@dataclass
class AMDConfig:
    # Section 1.1 (PDF): accumulation = range-bound and low momentum.
    accumulation_window: int = 20
    accumulation_max_span_atr: float = 2.8
    accumulation_max_net_atr: float = 1.0

    # Section 1.1 (PDF): manipulation = one-sided, full-body momentum candles.
    manipulation_window: int = 4
    manipulation_min_consecutive: int = 3
    manipulation_min_body_ratio: float = 0.60
    manipulation_max_pullback_ratio: float = 0.30

    # Section 1.1 (PDF): distribution = directional continuation with pullbacks.
    distribution_window: int = 8
    distribution_min_trend_candle_ratio: float = 0.60
    distribution_min_structure_swings: int = 2


@dataclass
class BreakConfig:
    # Section 2.2 (PDF): bias switch is confirmed by RBoS, not FBoS alone.
    require_reversal_for_fbos: bool = True
    require_manipulation_context_for_rbos: bool = True
    require_accumulation_for_fbos: bool = True


@dataclass
class SessionConfig:
    # London timezone hour buckets from the PDF session map.
    asia_start_hour: int = 0
    london_start_hour: int = 7
    new_york_start_hour: int = 13
    new_york_end_hour: int = 17
    london_end_hour: int = 13


@dataclass
class DOLConfig:
    # Priority follows PDF Section 4.1.
    internal_dol_distance_mult: float = 3.0
    require_bias_alignment: bool = True
    drop_swept_levels: bool = True
    drop_protected_levels: bool = True
    drop_session_induced: bool = True
    relax_all_filters: bool = False


@dataclass
class OBConfig:
    # Lightweight inducement OB finder settings.
    structure_lookback: int = 20
    impulse_window: int = 3
    min_body_ratio: float = 0.55
    range_extended_pips: float = 100.0


@dataclass
class EntryConfig:
    beginner_mode: bool = False
    fbos_mode: str = "conservative"  # aggressive|conservative|mitigation_wait
    mitigation_mode: str = "conservative"  # conservative|aggressive
    min_rr: float = 0.0
    relax_all_gates: bool = False
    aggressive_sweep_start_hour: int = 0
    aggressive_sweep_end_hour: int = 24
    aggressive_sweep_min_penetration_pips: float = 0.0
    require_prior_accumulation_for_fbos: bool = True
    # FBOS-only quality gates.
    fbos_min_penetration_pips: float = 5.0
    fbos_confirmation_min_body_ratio: float = 0.50
    fbos_gate_prior_accumulation: bool = True
    fbos_gate_momentum_breakout: bool = True
    fbos_gate_structural_level_taken: bool = True
    fbos_gate_bias_alignment: bool = True
    fbos_gate_rr_to_dol: bool = False
    fbos_gate_post_confirmation: bool = True
    fbos_gate_require_high_vol: bool = False
    fbos_gate_reclaim_strength: bool = False
    fbos_min_reclaim_frac: float = 0.0
    fbos_gate_exhaustion_confirmation: bool = False
    fbos_exhaustion_stretch_min: float = 1.0
    fbos_exhaustion_min_impulse_body: float = 0.60
    fbos_exhaustion_max_stall_body: float = 0.45


@dataclass
class RiskConfig:
    # Tunable risk model for rapid config sweeps.
    account_balance: float = 10000.0
    risk_per_trade: float = 0.01
    pip_value: float = 0.0001
    min_stop_pips: float = 3.0
    sl_buffer_pips: float = 2.0
    # FBOS explicit SL spec: sweep extreme +/- 3 pips.
    fbos_sl_buffer_pips: float = 3.0
    # "Enlightened" mode uses OB boundary instead of sweep extreme for sweep models.
    enlightened_sl_mode: bool = False
    reward_multiplier: float = 2.0
    partial_rr: float = 1.0
    min_rr_threshold: float = 0.0
    fill_timeout_bars: int = 288
    max_holding_days: float = 3.0
    fail_fast_bars: int = 0
    fail_fast_rr: float = 0.0
    # Execution realism.
    spread_pips: float = 0.0
    entry_slippage_pips: float = 0.0
    exit_slippage_pips: float = 0.0
    # Costs.
    lot_size_units: float = 100000.0
    commission_per_lot_round_turn: float = 7.0
