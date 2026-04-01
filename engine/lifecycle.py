"""Trade lifecycle state transitions."""

from __future__ import annotations

from .config import RiskConfig
from .models import TradeRuntime
from .types import InvalidationReason, TradeState


def should_move_to_be(trade: TradeRuntime, bar: dict, cfg: RiskConfig) -> bool:
    """
    Structural break-even:
    move SL to entry only after the pullback leg trigger is taken out.
    The trigger should be set from the post-entry pullback structure, not RR.
    """
    _ = cfg
    if trade.be_trigger_price is None or not trade.be_armed:
        return False
    if trade.direction == "long":
        return float(bar["high"]) >= float(trade.be_trigger_price)
    return float(bar["low"]) <= float(trade.be_trigger_price)


def should_take_partial(trade: TradeRuntime, bar: dict, cfg: RiskConfig) -> bool:
    if trade.partial_taken:
        return False
    entry = trade.entry_price
    risk = abs(entry - trade.sl_price)
    partial_target = entry + risk * cfg.partial_rr if trade.direction == "long" else entry - risk * cfg.partial_rr
    if trade.direction == "long":
        return float(bar["high"]) >= partial_target
    return float(bar["low"]) <= partial_target


def exit_on_invalidation(trade: TradeRuntime, reason: InvalidationReason, bar: dict | None = None) -> None:
    trade.state = TradeState.INVALIDATED
    trade.invalidation_reason = reason
    trade.exit_reason = reason.value
    if bar is not None:
        trade.exit_price = float(bar["close"])


def manage_trade_state(
    trade: TradeRuntime,
    bar: dict,
    cfg: RiskConfig,
    invalidation_reason: InvalidationReason | None = None,
) -> TradeRuntime:
    """
    Conservative intra-bar ordering:
    invalidation -> SL -> TP -> partial -> BE.
    """
    if trade.state in (TradeState.CLOSED, TradeState.INVALIDATED):
        return trade

    if invalidation_reason is not None:
        exit_on_invalidation(trade, invalidation_reason, bar)
        return trade

    low = float(bar["low"])
    high = float(bar["high"])
    close = float(bar["close"])

    if trade.direction == "long":
        if low <= trade.sl_price:
            trade.state = TradeState.CLOSED
            trade.exit_price = trade.sl_price
            trade.exit_reason = "sl"
            return trade
        if high >= trade.tp_price:
            trade.state = TradeState.CLOSED
            trade.exit_price = trade.tp_price
            trade.exit_reason = "tp"
            return trade
    else:
        if high >= trade.sl_price:
            trade.state = TradeState.CLOSED
            trade.exit_price = trade.sl_price
            trade.exit_reason = "sl"
            return trade
        if low <= trade.tp_price:
            trade.state = TradeState.CLOSED
            trade.exit_price = trade.tp_price
            trade.exit_reason = "tp"
            return trade

    if should_take_partial(trade, bar, cfg):
        trade.partial_taken = True
        trade.state = TradeState.PARTIAL_TP_HIT

    if should_move_to_be(trade, bar, cfg):
        trade.sl_price = trade.entry_price
        if trade.state != TradeState.PARTIAL_TP_HIT:
            trade.state = TradeState.BE_MOVED

    if trade.state == TradeState.ACTIVE and close:
        # Keeps state alive without churn.
        trade.state = TradeState.ACTIVE

    return trade
