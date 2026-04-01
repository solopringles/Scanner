"""Entry model selection and triggers."""

from __future__ import annotations

from .config import EntryConfig
from .types import EntryModel, EntryTrigger


def choose_entry_model(trade_ctx: dict, cfg: EntryConfig | None = None) -> EntryModel:
    cfg = cfg or EntryConfig()
    if cfg.beginner_mode:
        return EntryModel.FBOS
    if trade_ctx.get("smt_reaction_setup", False):
        return EntryModel.SMT_REACTION
    if trade_ctx.get("aggressive_sweep_setup", False):
        return EntryModel.AGGRESSIVE_SWEEP
    if trade_ctx.get("external_range_edge_setup", False):
        return EntryModel.MITIGATION if trade_ctx.get("prefer_mitigation", False) else EntryModel.FBOS
    return EntryModel.FBOS


def validate_fbos_criteria(ctx: dict, cfg: EntryConfig | None = None) -> bool:
    """
    Minimal strict gate mirroring PDF Section 5.1:
    accumulation before move, one-sided manipulation, level sweep, bias align, DOL visible (>=2R).
    """
    cfg = cfg or EntryConfig()
    require_prior_acc = bool(ctx.get("require_prior_accumulation_for_fbos", False))
    prior_acc_ok = bool(ctx.get("prior_accumulation", False)) if require_prior_acc else True
    return bool(
        ((prior_acc_ok) if cfg.fbos_gate_prior_accumulation else True)
        and ((ctx.get("momentum_breakout", False)) if cfg.fbos_gate_momentum_breakout else True)
        and ((ctx.get("structural_level_taken", False)) if cfg.fbos_gate_structural_level_taken else True)
        and ((ctx.get("bias_aligned", False)) if cfg.fbos_gate_bias_alignment else True)
        and ((ctx.get("is_high_vol", False)) if cfg.fbos_gate_require_high_vol else True)
        and ((ctx.get("reclaim_strength_ok", False)) if cfg.fbos_gate_reclaim_strength else True)
        and ((ctx.get("exhaustion_confirmed", False)) if cfg.fbos_gate_exhaustion_confirmation else True)
        and ctx.get("dol_visible", False)
        and ((float(ctx.get("rr_to_dol", 0.0)) >= float(cfg.min_rr)) if cfg.fbos_gate_rr_to_dol else True)
    )


def validate_aggressive_sweep_criteria(ctx: dict, cfg: EntryConfig | None = None) -> bool:
    cfg = cfg or EntryConfig()
    hour = ctx.get("session_hour_london")
    within_window = (
        isinstance(hour, int)
        and cfg.aggressive_sweep_start_hour <= hour < cfg.aggressive_sweep_end_hour
    )
    return bool(
        within_window
        and (ctx.get("sweep_reject_high", False) or ctx.get("sweep_reject_low", False))
        and ctx.get("structural_level_taken", False)
        and ctx.get("bias_aligned", False)
        and ctx.get("dol_visible", False)
        and (
            float(ctx.get("sweep_penetration_pips", 0.0)) >= float(cfg.aggressive_sweep_min_penetration_pips)
        )
        and ((float(ctx.get("rr_to_dol", 0.0)) >= float(cfg.min_rr)) if cfg.min_rr > 0 else True)
    )


def trigger_aggressive_sweep(ctx: dict, cfg: EntryConfig | None = None) -> EntryTrigger | None:
    cfg = cfg or EntryConfig()
    if not validate_aggressive_sweep_criteria(ctx, cfg):
        return None
    return EntryTrigger.AGGRESSIVE_SWEEP


def trigger_fbos(ctx: dict, cfg: EntryConfig | None = None) -> EntryTrigger | None:
    cfg = cfg or EntryConfig()
    if not validate_fbos_criteria(ctx, cfg):
        return None
    if cfg.fbos_mode == "aggressive":
        return EntryTrigger.SWEEP_CLOSE_AGGRESSIVE
    if cfg.fbos_mode == "conservative" and (
        (not cfg.fbos_gate_post_confirmation) or ctx.get("post_sweep_confirmation", False)
    ):
        return EntryTrigger.POST_SWEEP_CONFIRMATION
    if cfg.fbos_mode == "mitigation_wait" and ctx.get("ob_tap_bos_after", False):
        return EntryTrigger.OB_TAP_PLUS_M5_BOS
    return None


def validate_mitigation_criteria(ctx: dict, cfg: EntryConfig | None = None) -> bool:
    cfg = cfg or EntryConfig()
    return bool(
        ctx.get("valid_inducement_ob", False)
        and not ctx.get("broken_through", False)
        and ctx.get("bias_aligned", False)
        and ctx.get("dol_visible", False)
        and (float(ctx.get("rr_to_dol", 0.0)) >= float(cfg.min_rr) if cfg.min_rr > 0 else True)
    )


def trigger_mitigation(ctx: dict, cfg: EntryConfig | None = None) -> EntryTrigger | None:
    cfg = cfg or EntryConfig()
    if not validate_mitigation_criteria(ctx, cfg):
        return None
    # PDF conservative flow: tap -> then M5 BOS -> enter next candle.
    # m5_bos_after_tap already implies a prior tap occurred.
    if cfg.mitigation_mode == "conservative" and ctx.get("m5_bos_after_tap", False):
        return EntryTrigger.OB_TAP_PLUS_M5_BOS
    if cfg.mitigation_mode == "aggressive" and ctx.get("extreme_confluence", False):
        return EntryTrigger.LIMIT_ON_ZONE
    return None


def trigger_smt_reaction(ctx: dict, cfg: EntryConfig | None = None) -> EntryTrigger | None:
    cfg = cfg or EntryConfig()
    if not bool(
        ctx.get("zone_role") == "SMT"
        and ctx.get("ob_tap_seen", False)
        and ctx.get("reaction_induced_structure", False)
        and ctx.get("m5_bos_after_tap", False)
        and ctx.get("dol_visible", False)
    ):
        return None
    if cfg.min_rr > 0 and float(ctx.get("rr_to_dol", 0.0)) < float(cfg.min_rr):
        return None
    return EntryTrigger.OB_TAP_PLUS_M5_BOS
