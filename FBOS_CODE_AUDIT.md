# Document 1: FBOS Code Audit (Detailed Analysis)

## Verdict

`70% correct` overall.

What was already solid:
- No-lookahead break/sweep matrix on 5m.
- Limit-order-only execution.
- 5m TP/SL collision fallback to 1m path with pessimistic SL on ambiguity.
- Major-level sweep gate exists.

Main issues found were rule-specific gaps in direction, SL precision, penetration strictness, confirmation strictness, DOL ordering, and dual-sweep handling.

## Section-by-section findings

### 1) Break/Sweep Detection

Status: `Mostly correct`

- Correct: breaks and sweeps are computed from shifted rolling levels (past-only).
- Gap: penetration depth was not explicitly enforced as a minimum threshold.

### 2) Direction Assignment for FBOS

Status: `Incorrect before fix`

- Prior behavior used derived bias direction for FBOS orders.
- That can conflict with actual sweep side (e.g., low sweep but short entry).
- Theory spec expects explicit mapping from sweep type to direction.

### 3) Stop-Loss Placement

Status: `Partially correct before fix`

- Prior behavior used generic SL buffer and mixed anchors.
- Theory spec requires explicit FBOS SL from sweep candle extreme with fixed pip buffer.

### 4) Confirmation Candle

Status: `Partially correct before fix`

- Prior behavior required close direction only.
- Missing body-strength filter (>= 50% body/range).

### 5) DOL Target Selection

Status: `Mismatch before fix`

- Previous ranking order favored non-requested precedence.
- Theory requested: session -> PDH/PDL -> EQH/EQL -> PWH/PWL.

### 6) Competing Sweeps (both sides in one bar)

Status: `Ambiguous before fix`

- No explicit deterministic policy for dual-sweep bars.
- Could allow noisy or inconsistent FBOS interpretation.

## 6 specific bugs/gaps (with impact)

1. `Directional coupling to bias instead of sweep side`
- Location: `v2/engine/pipeline.py` (candidate setup loop).
- Impact: wrong-side FBOS entries possible.
- Estimated impact: high; directly affects win-rate and distribution quality.

2. `No explicit minimum penetration gate`
- Location: `v2/engine/pipeline.py` (FBOS gating).
- Impact: shallow sweeps can pass as FBOS.
- Estimated impact: medium-high; increases false positives.

3. `FBOS SL not fixed to sweep extreme +/- 3 pips`
- Location: `v2/engine/risk.py` (`compute_sl`).
- Impact: inconsistent risk geometry, RR distortion.
- Estimated impact: medium; trade sizing and stop quality drift.

4. `Confirmation candle lacked body-size threshold`
- Location: `v2/engine/pipeline.py` (`post_sweep_confirmation`).
- Impact: weak closes could confirm FBOS.
- Estimated impact: medium; lower precision.

5. `DOL priority order not as requested`
- Location: `v2/engine/dol.py` (`rank_dol_candidates`).
- Impact: targets selected from lower-preference pools first.
- Estimated impact: medium; influences TP distance and pass/fail on RR gate.

6. `No explicit competing-sweep resolver`
- Location: `v2/engine/pipeline.py` (FBOS side selection).
- Impact: unstable interpretation when both sweep flags appear.
- Estimated impact: medium; edge-case instability and noisy setups.

## Code locations to verify

- Break matrix: `v2/engine/pipeline.py` (`_break_matrix_no_lookahead`)
- FBOS direction + penetration + competing sweeps:
  - `v2/engine/pipeline.py` (`_resolve_fbos_sweep_direction`)
  - candidate setup loop where FBOS is built
- Confirmation candle body rule: `v2/engine/pipeline.py` (`post_sweep_confirmation`)
- FBOS SL precision: `v2/engine/risk.py` (`compute_sl`)
- DOL ranking priority: `v2/engine/dol.py` (`rank_dol_candidates`)
- Config thresholds:
  - `v2/engine/config.py` (`EntryConfig`, `RiskConfig`)

## Quantitative impact estimates (directional, expected)

- FBOS setup count: likely decreases due to stricter penetration + confirmation.
- FBOS quality: expected increase (higher precision, lower noise).
- RR profile: should become more consistent from explicit sweep-based SL.
- Cross-model mix: mitigation share may rise if FBOS is filtered harder.
- Runtime impact: negligible to low (vectorized math; no heavy loops added).
