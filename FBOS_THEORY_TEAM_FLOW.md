# FBOS Model: Theory Team Handoff

This document explains exactly how FBOS is modeled in `v2` right now.

## 1) What FBOS means in this codebase

In our implementation, an FBOS setup is only created when a sweep/rejection event happens and strict context gates pass.

Core files:
- `v2/engine/pipeline.py`
- `v2/engine/entries.py`
- `v2/engine/execution.py`

---

## 2) High-level flow

1. Build 5m structure/sweep signals with **no lookahead**.
2. Detect a valid manipulation lead-in before the sweep.
3. Check FBOS entry criteria (bias, major level taken, DOL visibility, RR >= 2).
4. Place a **limit order only** (no market entries).
5. Simulate fill and exit on 5m bars.
6. If one 5m bar can hit both TP and SL, resolve with 1m path.
7. If still ambiguous on 1m, log ambiguity and take SL.

---

## 3) Exact logic (simple pseudocode)

```python
for each 5m bar i:
    flags = break_matrix_no_lookahead(i)
    # flags include:
    # close_break_high, close_break_low, sweep_reject_high, sweep_reject_low

    fbos_leadin = prior_3_bars_one_sided_momentum_into_sweep(i)
    bias = current_bias_state(i)  # carried until invalidation
    direction = "long" if bias is bullish else "short"

    target_dol = select_target_dol(i, bias)
    entry_price = fbos_limit_entry_level(i)
    rr_to_dol = estimate_rr(entry_price, sl_from_sweep, target_dol)

    fbos_ok = (
        fbos_leadin
        and major_level_taken_by_sweep(i)
        and bias is not neutral
        and target_dol exists
        and rr_to_dol >= 2.0
        and post_sweep_confirmation_candle(i)
    )

    if fbos_ok:
        create_setup(model="FBOS", direction=direction, entry=entry_price)
        order = build_limit_order(setup)
        trade = simulate_limit_fill_and_exit(order)
```

---

## 4) Gate-by-gate definition

### A) Break/sweep detection (no lookahead)

From `pipeline._break_matrix_no_lookahead`:
- `close_break_high`: close > rolling prior high
- `close_break_low`: close < rolling prior low
- `sweep_reject_high`: high breaches prior high but close rejects below/at prior high
- `sweep_reject_low`: low breaches prior low but close rejects above/at prior low

These are computed with rolling highs/lows shifted by 1 bar (past-only data).

### B) FBOS lead-in manipulation

From `pipeline._fbos_manipulation_leadin_matrix`:
- Requires a run of strong candles **before** sweep bar.
- Uses body ratio threshold.
- For low sweep reject: expects prior bearish run.
- For high sweep reject: expects prior bullish run.

### C) Major structural level taken

From `pipeline._major_level_taken`:
- Sweep must run through at least one major level:
- High-side set: `EQH/session_high/PDH/PWH`
- Low-side set: `EQL/session_low/PDL/PWL`

### D) FBOS validation

From `entries.validate_fbos_criteria`:
- Optional prior accumulation (configurable)
- Momentum breakout (from lead-in)
- Structural level taken
- Bias aligned
- DOL visible
- `rr_to_dol >= 2.0`

From `entries.trigger_fbos` in conservative mode:
- Also needs `post_sweep_confirmation == True`

### E) Entry type

From `pipeline._limit_entry_price_from_row`:
- FBOS entry is a limit at:
1. `structure_broken_price` if available
2. fallback structural liquidity level
3. fallback close (last resort)

### F) Execution and exit

From `execution.simulate_limit_order_trade`:
- Fill only if future bars touch entry limit inside timeout window.
- Exit rules:
1. TP-only hit => TP exit
2. SL-only hit => SL exit
3. Both in same 5m bar => call M1 resolver

From `execution._resolve_same_bar_with_m1`:
- Replay minute bars in that 5m window
- If still ambiguous, log event and assign SL

---

## 5) Current config defaults that matter

Used in year/month runners:
- `entry_cfg.fbos_mode = "conservative"`
- `entry_cfg.min_rr = 1.5` (mitigation gate), but FBOS hard gate is still `rr_to_dol >= 2.0`
- `entry_cfg.require_prior_accumulation_for_fbos = False`
- `emit_both_models = True` (FBOS and mitigation can both be generated)

---

## 6) Practical summary

Our FBOS is currently modeled as:
- Sweep/reject + prior one-sided momentum + major liquidity taken + bias + visible DOL + minimum structural RR + confirmation candle,
- followed by strict limit-order execution and conservative conflict handling on exits.

If theory requires tighter directional mapping between sweep side and trade direction, that is a separate rule layer we can enforce explicitly.
