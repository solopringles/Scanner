# Mitigation Model: Theory Team Handoff

This document explains exactly how mitigation entries are modeled in `v2` today.

Core files:
- `v2/engine/mitigation.py`
- `v2/engine/entries.py`
- `v2/engine/pipeline.py`
- `v2/engine/execution.py`

## 1) High-level flow

1. Detect order blocks (OBs) from price action.
2. For each bar, evaluate mitigation context using only past/current bars.
3. Require OB validity and first-tap behavior.
4. Require post-tap local M5 BOS confirmation.
5. Build limit order from OB boundary.
6. Simulate fills and exits with the same execution engine as FBOS.

## 2) Exact mitigation context logic

At bar `i`, `evaluate_mitigation_context(...)`:

- Select latest OB with same direction and `ob.idx < i`.
- Build tap timeline from `ob.idx+1` through current bar:
  - `ob_tap_seen`: first tap is happening now.
  - `ob_previously_tapped`: more than one prior touch (depletion risk).
- Classify OB state (`VALID_OB`, `USED_UP`, `DEPLETED`) from tap/depletion signals.
- Conservative confirmation requires:
  - first tap occurred before current bar,
  - no second-tap depletion,
  - local BOS after tap (`_local_bos_after_tap`).

Returned fields:
- `valid_inducement_ob`
- `ob_previously_tapped`
- `ob_tap_seen`
- `m5_bos_after_tap`
- `active_ob_low`, `active_ob_high`

## 3) Mitigation trigger gate

`entries.validate_mitigation_criteria(...)` requires:
- valid inducement OB,
- OB not previously tapped,
- bias aligned,
- DOL visible,
- `rr_to_dol >= min_rr`.

`entries.trigger_mitigation(...)` in conservative mode requires:
- `m5_bos_after_tap == True`

Trigger emitted:
- `OB_TAP_PLUS_M5_BOS`

## 4) Entry and risk

For mitigation models:
- Entry is a limit order on OB boundary (`ob_low` for long, `ob_high` for short) when available.
- SL/TP and quantity are built through `risk.build_order(...)`.
- Minimum RR gate must pass before order creation.

## 5) Execution model

Shared with FBOS:
- Fill only when limit is touched within timeout bars.
- Exit on TP or SL using 5m bars.
- If both TP/SL are hit in same 5m bar, resolve with M1 path.
- If M1 is still ambiguous, log event and assign SL.

## 6) Practical summary

Mitigation in `v2` is not a blind OB touch entry.
It is modeled as:
- valid inducement OB -> tap sequence control -> post-tap BOS confirmation -> limit entry -> conservative execution.
