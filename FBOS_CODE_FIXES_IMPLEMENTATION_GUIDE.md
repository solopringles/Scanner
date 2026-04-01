# Document 2: FBOS Code Fixes (Implementation Guide)

This guide maps requested fixes to concrete code changes.

## Critical Fixes (implemented now)

### 1) Explicit directional mapping (sweep type -> direction)

Requested:
- Direction must come from sweep side, not inferred bias alone.

Implemented:
- Added deterministic resolver:
  - `sweep_reject_low` -> `long`
  - `sweep_reject_high` -> `short`
- In competing sweeps, select dominant side by penetration (else reject).

Code:
- `v2/engine/pipeline.py`
  - `_resolve_fbos_sweep_direction(...)`
  - FBOS branch in candidate loop uses resolved FBOS direction.

### 2) Explicit SL calculation (sweep extreme +/- 3 pips)

Requested:
- FBOS SL anchored to sweep candle extreme with 3-pip buffer.

Implemented:
- Added `RiskConfig.fbos_sl_buffer_pips = 3.0`.
- `compute_sl(...)` now detects FBOS model and uses sweep anchors + FBOS buffer explicitly.

Code:
- `v2/engine/config.py` (`RiskConfig`)
- `v2/engine/risk.py` (`compute_sl`)

### 3) Penetration minimum (5+ pips)

Requested:
- Require minimum break/sweep penetration.

Implemented:
- Added `EntryConfig.fbos_min_penetration_pips = 5.0`.
- Break matrix now carries prior levels and penetration deltas.
- FBOS setup only allowed when selected sweep penetration >= configured minimum.

Code:
- `v2/engine/config.py` (`EntryConfig`)
- `v2/engine/pipeline.py` (`_break_matrix_no_lookahead`, FBOS branch)

## Medium Fixes (implemented now)

### 4) DOL selection priority

Requested:
- session -> PDH/PDL -> EQH/EQL -> PWH/PWL.

Implemented:
- Updated DOL ranking map to follow requested order.

Code:
- `v2/engine/dol.py` (`rank_dol_candidates`)

### 5) Confirmation candle rules

Requested:
- Close-direction confirmation + body size >= 50%.

Implemented:
- FBOS confirmation now requires directional close and `body_ratio >= 0.50`.
- Threshold exposed as config: `fbos_confirmation_min_body_ratio`.

Code:
- `v2/engine/config.py` (`EntryConfig`)
- `v2/engine/pipeline.py` (`post_sweep_confirmation` logic)

### 6) Competing sweeps resolution

Requested:
- Explicit logic when both sweep directions occur.

Implemented:
- Competing sweeps resolved by dominant penetration if clear winner and above threshold.
- If tie/unclear/no side passes threshold: reject FBOS on that bar.

Code:
- `v2/engine/pipeline.py` (`_resolve_fbos_sweep_direction`)

## Validation run

Quick smoke check executed:
- `python run_dukas_month.py --data ..\\DukasData\\eurusd-m1-bid-2020-01-01-2025-12-31.parquet --month 2025-01 --output output_v2_tmp_fixcheck`
- Run completed successfully (no runtime errors).

## Notes for rapid iteration

Fast knobs to tune in config:
- `EntryConfig.fbos_min_penetration_pips`
- `EntryConfig.fbos_confirmation_min_body_ratio`
- `RiskConfig.fbos_sl_buffer_pips`
- `RiskConfig.min_rr_threshold`

These are safe first-order controls for balancing frequency vs quality.
