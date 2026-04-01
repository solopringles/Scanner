"""Bias derivation rules."""

from __future__ import annotations

from .types import BiasDirection


def derive_bias(htf_ctx: dict) -> BiasDirection:
    """
    RBOS-only bias trigger:
    - RBOS up sets bullish bias
    - RBOS down sets bearish bias
    - otherwise keep the current bias state unchanged upstream
    """
    rbos_up = bool(htf_ctx.get("rbos_up", False))
    rbos_down = bool(htf_ctx.get("rbos_down", False))

    if rbos_up:
        return BiasDirection.BULLISH
    if rbos_down:
        return BiasDirection.BEARISH
    return BiasDirection.NEUTRAL


def invalidate_bias(bias_ctx: dict) -> bool:
    """
    Bias invalidation from the spec:
    - bias OB broken
    - DOL already reached
    - opposing HTF leg taken out
    """
    return bool(
        bias_ctx.get("bias_ob_broken", False)
        or bias_ctx.get("primary_dol_hit", False)
        or bias_ctx.get("htf_leg_taken_opposite", False)
    )
