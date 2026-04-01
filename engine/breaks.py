"""Break classifier (FBOS vs RBOS) for v2."""

from __future__ import annotations

from .config import BreakConfig
from .types import AMDPhase, BreakType


def classify_break(
    *,
    phase_before_break: AMDPhase,
    breaks_significant_level: bool,
    manipulation_signature_present: bool,
    later_reversal_seen: bool,
    rbos_confirmation_seen: bool,
    cfg: BreakConfig,
) -> BreakType:
    """
    Section 2.2 key rule:
    - FBoS alone does not confirm trend switch.
    - RBoS after manipulation confirms real direction.
    """
    if not breaks_significant_level:
        return BreakType.NONE

    is_fbos = (
        (phase_before_break == AMDPhase.ACCUMULATION or not cfg.require_accumulation_for_fbos)
        and manipulation_signature_present
        and (later_reversal_seen if cfg.require_reversal_for_fbos else True)
    )
    if is_fbos:
        return BreakType.FBOS

    is_rbos = rbos_confirmation_seen and (
        manipulation_signature_present if cfg.require_manipulation_context_for_rbos else True
    )
    if is_rbos:
        return BreakType.RBOS

    return BreakType.NONE


def is_fbos_break(
    *,
    phase_before_break: AMDPhase,
    breaks_significant_level: bool,
    manipulation_signature_present: bool,
    later_reversal_seen: bool,
    cfg: BreakConfig,
) -> bool:
    """Boolean wrapper for fast filters."""
    return (
        classify_break(
            phase_before_break=phase_before_break,
            breaks_significant_level=breaks_significant_level,
            manipulation_signature_present=manipulation_signature_present,
            later_reversal_seen=later_reversal_seen,
            rbos_confirmation_seen=False,
            cfg=cfg,
        )
        == BreakType.FBOS
    )


def is_rbos_break(
    *,
    breaks_significant_level: bool,
    manipulation_signature_present: bool,
    rbos_confirmation_seen: bool,
    cfg: BreakConfig,
) -> bool:
    """Boolean wrapper for fast filters."""
    return (
        classify_break(
            phase_before_break=AMDPhase.MANIPULATION,
            breaks_significant_level=breaks_significant_level,
            manipulation_signature_present=manipulation_signature_present,
            later_reversal_seen=False,
            rbos_confirmation_seen=rbos_confirmation_seen,
            cfg=cfg,
        )
        == BreakType.RBOS
    )
