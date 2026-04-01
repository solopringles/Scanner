"""Execution engine: limit fills + conflict-safe exits."""

from __future__ import annotations

import pandas as pd

from .config import RiskConfig
from .models import OrderSpec, TradeRuntime
from .lifecycle import manage_trade_state
from .types import TradeState


def _entry_touched(bar: pd.Series, entry: float, direction: str, risk_cfg: RiskConfig) -> bool:
    # Conservative limit-fill realism:
    # require price to trade through entry by spread + slippage buffer.
    buf = (risk_cfg.spread_pips + risk_cfg.entry_slippage_pips) * risk_cfg.pip_value
    if direction == "long":
        return float(bar["low"]) <= (entry - buf)
    return float(bar["high"]) >= (entry + buf)


def _long_sl_tp_hits(bar: pd.Series, sl: float, tp: float) -> tuple[bool, bool]:
    return float(bar["low"]) <= sl, float(bar["high"]) >= tp


def _short_sl_tp_hits(bar: pd.Series, sl: float, tp: float) -> tuple[bool, bool]:
    return float(bar["high"]) >= sl, float(bar["low"]) <= tp


def _resolve_same_bar_with_m1(
    *,
    direction: str,
    entry: float,
    sl: float,
    tp: float,
    bar_ts: pd.Timestamp,
    m1_df: pd.DataFrame | None,
    risk_cfg: RiskConfig,
    events: list[dict],
) -> tuple[float, str]:
    """
    Resolve SL/TP conflict inside a 5m bar:
    1) Use M1 path if available.
    2) If still ambiguous, pessimistically assign SL and log.
    """
    if m1_df is None or len(m1_df) == 0:
        events.append({"timestamp": bar_ts, "type": "ambiguous_no_m1", "resolution": "sl"})
        return sl, "sl_ambiguous_no_m1"

    bar_end = bar_ts + pd.Timedelta(minutes=5)
    win = m1_df[(m1_df.index >= bar_ts) & (m1_df.index < bar_end)]
    if win.empty:
        events.append({"timestamp": bar_ts, "type": "ambiguous_empty_m1", "resolution": "sl"})
        return sl, "sl_ambiguous_empty_m1"

    # Find fill minute first.
    fill_m1_ts = None
    for ts, b in win.iterrows():
        if _entry_touched(b, entry, direction, risk_cfg):
            fill_m1_ts = ts
            break
    if fill_m1_ts is None:
        events.append({"timestamp": bar_ts, "type": "ambiguous_no_fill_m1", "resolution": "sl"})
        return sl, "sl_ambiguous_no_fill_m1"

    post = win[win.index >= fill_m1_ts]
    for _, b in post.iterrows():
        if direction == "long":
            sl_hit, tp_hit = _long_sl_tp_hits(b, sl, tp)
        else:
            sl_hit, tp_hit = _short_sl_tp_hits(b, sl, tp)

        if sl_hit and tp_hit:
            events.append({"timestamp": bar_ts, "type": "ambiguous_same_m1", "resolution": "sl"})
            return sl, "sl_ambiguous_same_m1"
        if sl_hit:
            return sl, "sl_m1"
        if tp_hit:
            return tp, "tp_m1"

    events.append({"timestamp": bar_ts, "type": "ambiguous_unresolved_m1", "resolution": "sl"})
    return sl, "sl_ambiguous_unresolved_m1"


def simulate_limit_order_trade(
    *,
    order: OrderSpec,
    ohlc_5m: pd.DataFrame,
    signal_ts: pd.Timestamp,
    risk_cfg: RiskConfig,
    m1_df: pd.DataFrame | None,
    events: list[dict],
) -> TradeRuntime | None:
    """
    Limit-order execution only.
    Fill window starts on next bar after signal and times out after fill_timeout_bars.
    Set fill_timeout_bars <= 0 to wait for the remainder of the dataset.
    """
    if signal_ts not in ohlc_5m.index:
        return None

    loc = int(ohlc_5m.index.get_loc(signal_ts))
    if int(risk_cfg.fill_timeout_bars) <= 0:
        fill_window = ohlc_5m.iloc[loc + 1 :]
    else:
        fill_window = ohlc_5m.iloc[loc + 1 : loc + 1 + risk_cfg.fill_timeout_bars]
    if fill_window.empty:
        return None

    fill_ts = None
    for ts, bar in fill_window.iterrows():
        if _entry_touched(bar, order.entry_price, order.direction, risk_cfg):
            fill_ts = ts
            break
    if fill_ts is None:
        events.append({"timestamp": signal_ts, "type": "missed_limit_fill", "entry": order.entry_price})
        return None

    trade = TradeRuntime(
        timestamp=signal_ts,
        direction=order.direction,
        state=TradeState.ACTIVE,
        entry_price=order.entry_price,
        sl_price=order.sl_price,
        tp_price=order.tp_price,
        qty=order.qty,
        fill_timestamp=fill_ts,
    )
    # Round-turn commission in quote currency.
    lots = float(order.qty) / max(float(risk_cfg.lot_size_units), 1e-9)
    trade.commission_quote = float(risk_cfg.commission_per_lot_round_turn) * lots

    # Exit simulation from fill bar onward.
    post_fill = ohlc_5m.loc[fill_ts:]
    max_hold_days = getattr(risk_cfg, "max_holding_days", None)
    max_exit_ts = (fill_ts + pd.Timedelta(days=float(max_hold_days))) if max_hold_days else None
    fail_fast_bars = int(getattr(risk_cfg, "fail_fast_bars", 0) or 0)
    fail_fast_rr = float(getattr(risk_cfg, "fail_fast_rr", 0.0) or 0.0)
    risk_per_unit = abs(float(trade.entry_price) - float(trade.sl_price))
    bars_elapsed = 0
    mfe = 0.0
    prev_bar: pd.Series | None = None
    pullback_active = False
    pullback_trigger_price: float | None = None
    for ts, bar in post_fill.iterrows():
        if max_exit_ts is not None and ts > max_exit_ts:
            trade.state = TradeState.CLOSED
            slip = risk_cfg.exit_slippage_pips * risk_cfg.pip_value
            trade.exit_price = float(bar["close"]) - slip if order.direction == "long" else float(bar["close"]) + slip
            trade.exit_timestamp = ts
            trade.exit_reason = "timeout_days"
            return trade

        if order.direction == "long":
            sl_hit, tp_hit = _long_sl_tp_hits(bar, trade.sl_price, trade.tp_price)
        else:
            sl_hit, tp_hit = _short_sl_tp_hits(bar, trade.sl_price, trade.tp_price)

        if sl_hit and tp_hit:
            px, reason = _resolve_same_bar_with_m1(
                direction=order.direction,
                entry=trade.entry_price,
                sl=trade.sl_price,
                tp=trade.tp_price,
                bar_ts=ts,
                m1_df=m1_df,
                risk_cfg=risk_cfg,
                events=events,
            )
            trade.state = TradeState.CLOSED
            slip = risk_cfg.exit_slippage_pips * risk_cfg.pip_value
            trade.exit_price = float(px) - slip if order.direction == "long" else float(px) + slip
            trade.exit_timestamp = ts
            trade.exit_reason = reason
            return trade
        if sl_hit:
            trade.state = TradeState.CLOSED
            slip = risk_cfg.exit_slippage_pips * risk_cfg.pip_value
            trade.exit_price = trade.sl_price - slip if order.direction == "long" else trade.sl_price + slip
            trade.exit_timestamp = ts
            trade.exit_reason = "sl"
            return trade
        if tp_hit:
            trade.state = TradeState.CLOSED
            slip = risk_cfg.exit_slippage_pips * risk_cfg.pip_value
            trade.exit_price = trade.tp_price - slip if order.direction == "long" else trade.tp_price + slip
            trade.exit_timestamp = ts
            trade.exit_reason = "tp"
            return trade

        bars_elapsed += 1
        if order.direction == "long":
            mfe = max(mfe, float(bar["high"]) - float(trade.entry_price))
        else:
            mfe = max(mfe, float(trade.entry_price) - float(bar["low"]))

        # Arm BE from the first post-entry pullback leg.
        if trade.be_trigger_price is None:
            if prev_bar is not None:
                if order.direction == "long":
                    if not pullback_active and (float(bar["low"]) < float(prev_bar["low"]) or float(bar["close"]) < float(prev_bar["close"])):
                        pullback_active = True
                        pullback_trigger_price = float(bar["high"])
                    elif pullback_active and pullback_trigger_price is not None:
                        if float(bar["high"]) > float(pullback_trigger_price):
                            trade.be_trigger_price = float(pullback_trigger_price)
                            trade.be_armed = True
                        else:
                            pullback_trigger_price = max(float(pullback_trigger_price), float(bar["high"]))
                else:
                    if not pullback_active and (float(bar["high"]) > float(prev_bar["high"]) or float(bar["close"]) > float(prev_bar["close"])):
                        pullback_active = True
                        pullback_trigger_price = float(bar["low"])
                    elif pullback_active and pullback_trigger_price is not None:
                        if float(bar["low"]) < float(pullback_trigger_price):
                            trade.be_trigger_price = float(pullback_trigger_price)
                            trade.be_armed = True
                        else:
                            pullback_trigger_price = min(float(pullback_trigger_price), float(bar["low"]))

        trade = manage_trade_state(trade, bar.to_dict(), risk_cfg)
        if trade.state in (TradeState.CLOSED, TradeState.INVALIDATED):
            if trade.exit_timestamp is None:
                trade.exit_timestamp = ts
            if trade.exit_price is None:
                trade.exit_price = float(bar["close"])
            return trade

        if fail_fast_bars > 0 and fail_fast_rr > 0 and bars_elapsed >= fail_fast_bars and risk_per_unit > 0:
            if mfe < (risk_per_unit * fail_fast_rr):
                trade.state = TradeState.CLOSED
                slip = risk_cfg.exit_slippage_pips * risk_cfg.pip_value
                trade.exit_price = float(bar["close"]) - slip if order.direction == "long" else float(bar["close"]) + slip
                trade.exit_timestamp = ts
                trade.exit_reason = "timeout_fail_fast"
                return trade

        prev_bar = bar

    # Timeout at last available close.
    last = post_fill.iloc[-1]
    trade.state = TradeState.CLOSED
    slip = risk_cfg.exit_slippage_pips * risk_cfg.pip_value
    trade.exit_price = float(last["close"]) - slip if order.direction == "long" else float(last["close"]) + slip
    trade.exit_timestamp = post_fill.index[-1]
    trade.exit_reason = "timeout"
    return trade
