"""Draw-on-liquidity candidate engine."""

from __future__ import annotations

from .config import DOLConfig
from .models import DOLCandidate, OBZone
from .types import BiasDirection, DOLType, ZoneRole


def collect_dol_candidates(ctx: dict) -> list[DOLCandidate]:
    """
    Create candidate DOL list from precomputed context values.
    Expects keys like: eqh, eql, session_high, session_low, pdh, pdl, pwh, pwl, smt_open.
    """
    out: list[DOLCandidate] = []
    push = out.append

    if (v := ctx.get("eqh")) is not None:
        push(DOLCandidate(DOLType.EQH_EQL, float(v), "long", source="eqh", strength=1.0))
    if (v := ctx.get("eql")) is not None:
        push(DOLCandidate(DOLType.EQH_EQL, float(v), "short", source="eql", strength=1.0))
    if (v := ctx.get("session_high")) is not None:
        push(DOLCandidate(DOLType.SESSION_HL, float(v), "long", source="session_high", strength=0.9))
    if (v := ctx.get("session_low")) is not None:
        push(DOLCandidate(DOLType.SESSION_HL, float(v), "short", source="session_low", strength=0.9))
    if (v := ctx.get("pdh")) is not None:
        push(DOLCandidate(DOLType.PDH_PDL, float(v), "long", source="pdh", strength=0.85))
    if (v := ctx.get("pdl")) is not None:
        push(DOLCandidate(DOLType.PDH_PDL, float(v), "short", source="pdl", strength=0.85))
    if (v := ctx.get("pwh")) is not None:
        push(DOLCandidate(DOLType.PWH_PWL, float(v), "long", source="pwh", strength=0.8))
    if (v := ctx.get("pwl")) is not None:
        push(DOLCandidate(DOLType.PWH_PWL, float(v), "short", source="pwl", strength=0.8))
    if (v := ctx.get("smt_open")) is not None:
        # Direction comes from current bias intent if provided.
        direction = str(ctx.get("smt_open_direction", "long"))
        push(DOLCandidate(DOLType.SMT_OPEN, float(v), direction, source="smt_open", strength=0.95))
    if (v := ctx.get("internal_dol")) is not None:
        direction = str(ctx.get("internal_dol_direction", "long"))
        current_price = ctx.get("current_price")
        strength = 0.6
        if current_price is not None:
            dist_pips = abs(float(v) - float(current_price)) / max(float(ctx.get("pip_value", 0.0001)), 1e-9)
            # Internal DOL becomes more important when it is the nearest practical target.
            strength = max(0.6, 1.8 - (dist_pips / 25.0))
        push(DOLCandidate(DOLType.INTERNAL_DOL, float(v), direction, source="internal_dol", strength=strength))

    current_ts = ctx.get("current_timestamp")
    htf_order_blocks = ctx.get("htf_order_blocks", [])
    if isinstance(htf_order_blocks, list) and htf_order_blocks:
        current_bias = ctx.get("bias", BiasDirection.NEUTRAL)
        if current_bias != BiasDirection.NEUTRAL:
            zone_direction = "long" if current_bias == BiasDirection.BULLISH else "short"
        else:
            zone_direction = "long"
        for i, ob in enumerate(htf_order_blocks):
            if not isinstance(ob, OBZone):
                continue
            if current_ts is not None and ob.timestamp > current_ts:
                continue
            if ob.current_role != ZoneRole.SMT.value:
                continue
            if ob.open_price is None:
                continue
            strength = 0.97 if ob.protected else 0.92
            push(
                DOLCandidate(
                    DOLType.SMT_OPEN,
                    float(ob.open_price),
                    zone_direction,
                    source=f"htf_smt_open_{i}",
                    strength=strength,
                    swept=ob.tapped,
                    protected=ob.protected,
                )
            )

    return out


def filter_dol_by_bias(candidates: list[DOLCandidate], bias: BiasDirection) -> list[DOLCandidate]:
    if bias == BiasDirection.NEUTRAL:
        return candidates
    direction = "long" if bias == BiasDirection.BULLISH else "short"
    return [c for c in candidates if c.direction == direction]


def remove_invalid_dol(candidates: list[DOLCandidate], ctx: dict, cfg: DOLConfig) -> list[DOLCandidate]:
    """
    Removes invalid levels based on spec invalidation rules.
    Fast path uses precomputed flags on candidates or level source lookup in ctx.
    """
    invalid_sources = set(ctx.get("invalid_dol_sources", []))
    out: list[DOLCandidate] = []
    for c in candidates:
        if c.source in invalid_sources:
            continue
        if cfg.drop_swept_levels and c.swept:
            continue
        if cfg.drop_protected_levels and c.protected:
            continue
        if cfg.drop_session_induced and c.session_induced:
            continue
        out.append(c)
    return out


def rank_dol_candidates(candidates: list[DOLCandidate]) -> list[DOLCandidate]:
    priority = {
        # Updated preference: session -> SMT open -> EQH/EQL -> PDH/PDL -> PWH/PWL.
        DOLType.SESSION_HL: 0,
        DOLType.SMT_OPEN: 1,
        DOLType.EQH_EQL: 2,
        DOLType.PDH_PDL: 3,
        DOLType.PWH_PWL: 4,
        DOLType.STRUCT_SWING: 5,
        DOLType.INTERNAL_DOL: 6,
    }
    def _rank_key(c: DOLCandidate) -> tuple[int, float]:
        eff_priority = priority.get(c.dol_type, 9)
        if c.dol_type == DOLType.INTERNAL_DOL and c.strength >= 1.0:
            eff_priority = 0
        return (eff_priority, -c.strength)

    return sorted(candidates, key=_rank_key)


def select_target_dol(ctx: dict, cfg: DOLConfig | None = None) -> DOLCandidate | None:
    cfg = cfg or DOLConfig()
    bias = ctx.get("bias", BiasDirection.NEUTRAL)
    candidates = collect_dol_candidates(ctx)
    if cfg.require_bias_alignment:
        candidates = filter_dol_by_bias(candidates, bias)
    candidates = remove_invalid_dol(candidates, ctx, cfg)
    ranked = rank_dol_candidates(candidates)
    return ranked[0] if ranked else None
