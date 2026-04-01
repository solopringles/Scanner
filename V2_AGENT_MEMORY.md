# V2 Agent Memory (Project-Specific Operating Notes)

Purpose: keep a persistent, high-signal memory of what the user wants, what has already been built, and where mistakes commonly happen.

## User Intent and Working Style

- User is building a strategy engine from a detailed PDF spec.
- User wants strict alignment to strategy rules, not generic trading logic.
- User prefers direct action and iterative hardening over long planning.
- User asks for visual validation frequently (charts for all trades, model labels).
- User is sensitive to:
  - false logic assumptions,
  - lookahead bias,
  - execution realism mismatches.

## Non-Negotiable Strategy/Execution Rules

1. No lookforward/lookahead in signal generation.
2. Entries are limit orders where model says they should be.
3. Exit conflict policy:
   - if 5m bar hits both SL and TP, drop to M1;
   - if still ambiguous, log event and take SL (pessimistic).

These are implemented in:
- `engine/pipeline.py` (signal path)
- `engine/execution.py` (limit fills + conflict resolution)

## Canonical Strategy Document

- Source of truth: `Trading_Framework_Spec_Sheet.pdf`
- Extracted text used for code review:
  - `Trading_Framework_Spec_Sheet.txt`

Key PDF sections to continuously validate against:
- Section 5: FBOS entry/invalidations
- Section 6: Mitigation entry/invalidations
- Section 4: DOL hierarchy/validity
- Section 9: SL/BE policy

## Core Engine File Map

- `engine/types.py`:
  enums/states (`AMDPhase`, `BreakType`, `EntryModel`, etc.)
- `engine/config.py`:
  tunable configs (fast iteration)
- `engine/amd.py`:
  vectorized phase/manipulation features
- `engine/breaks.py`:
  FBOS/RBOS classification
- `engine/context.py`:
  no-lookahead liquidity context (EQH/EQL, PDH/PDL, PWH/PWL, session levels)
- `engine/dol.py`:
  DOL candidate/ranking/selection
- `engine/ob.py`:
  inducement OB detection/state helpers
- `engine/mitigation.py`:
  mitigation context checks (tap/BOS-after-tap/OB validity)
- `engine/entries.py`:
  model triggers
- `engine/risk.py`:
  SL/TP/RR/order build
- `engine/execution.py`:
  limit fill + M1 conflict resolution
- `engine/pipeline.py`:
  orchestrates full signal->order->trade flow
- `engine/io_utils.py`:
  CSV exports

Runner/utility:
- `run_dukas_month.py`: monthly Dukas run
- `plot_all_trades.py`: chart every filled trade

## Important Historical Fixes Already Made

1. Removed lookahead from pipeline.
2. Vectorized hot path (major speedup).
3. Fixed session timezone mapping to London.
4. Corrected RR directionality gate.
5. Fixed dataclass serialization issues for `slots=True`.
6. Enforced limit-only execution with M1 tie-break policy.
7. Added major-liquidity context (not just local rolling levels).
8. Corrected mitigation conservative trigger sequence:
   tap first, BOS after, then entry.
9. Fixed OB default that wrongly forced all OBs to SMT (`created_in_manipulation` default).
10. Added bias carry-forward (until invalidation) instead of pure bar-local neutral behavior.

## New Model Note

- `AGGRESSIVE_SWEEP` is a separate setup model from FBOS.
- It is currently allowed across all hours.
- It reuses the same downstream order, stop, target, and execution machinery, but it does not require FBOS-style manipulation lead-in.
- RBOS is treated as a diagnostic/logging signal for now, not a hard trade gate.

## Known Caution Areas

1. Mitigation vs FBOS balance can drift by pair/timeframe:
   - EURUSD had strong mitigation activity.
   - GBPJPY still showed mostly/only FBOS fills in tested windows.
2. Any change in OB validity rules can silently eliminate mitigation setups.
3. DOL source availability matters:
   if PDH/PDL/PWH/PWL/session levels are missing, setup quality drops.
4. Strong filtering + strict limit fills can create many missed fills (not necessarily bad).
5. Always verify counts at each stage:
   `breaks -> bias -> setups -> orders -> filled -> events`.

## Debug Checklist (Use Before Claiming “No Setups”)

1. Break counts: are FBOS/RBOS detected?
2. Bias distribution: mostly neutral or carried?
3. DOL availability at candidate bars: major levels populated?
4. FBOS gate breakdown:
   - accumulation context,
   - lead-in manipulation,
   - major level taken,
   - DOL + RR.
5. Mitigation gate breakdown:
   - valid inducement OB,
   - OB not depleted,
   - tap sequence,
   - M5 BOS after tap.
6. Order conversion:
   - SL/TP valid side of entry?
   - RR gate pass?
7. Fill conversion:
   - missed limit fill count.

## Prompt Pattern Memory

Common user prompt types:
- “Do deeper dive against PDF and old scanner logic.”
- “Show me visually all trades with labels.”
- “No lookforward. Limit entries only. Realistic exits.”
- “Aggressively debug edge cases.”

Response behavior that works best:
- run tools immediately,
- provide concrete counts,
- show exact file paths changed,
- keep strategy-alignment explicit to PDF criteria.

## Operational Rule for Future Changes

Before modifying detection/entry logic:
1. map change to PDF criterion number,
2. confirm no lookahead,
3. confirm execution realism unaffected,
4. rerun at least one month (EURUSD + GBPJPY),
5. report stage counts and model mix.

## Current Working Focus

- Prioritize entry logic, structure mapping, and setup validity first.
- Keep TP / exit optimization separate until the entry flow is stable.
- When asked to compare targets, treat it as a later grid-test problem, not a blocker for entry validation.
- Current trade review should focus on whether the setup was formed, order created, and fill occurred cleanly.
- Use `v2/strategy_rule_sheet.md` as the shared strict-vs-soft audit layer when a candle looks plausible by eye but does not fit the current code path cleanly.

## Output Hygiene Rule

- Do not create new analysis CSV files unless the user explicitly asks for a file artifact.
- Default behavior for exploratory sweeps/ablations: print ranked results in-chat only.

## Recent FBOS Gate Ablation Findings (2025 Full Year, FBOS-only test mode)

- Test pattern used:
  - all gates off baseline, then one gate on at a time.
  - Runner configured to FBOS-only path to isolate FBOS gating effects.
- Ranked by net R:
  1. bias_only: +3041.121R
  2. all_off: +3025.892R
  3. penetration_only: +2996.433R
  4. rr_only: +2980.272R
  5. structural_only: +1589.766R
  6. confirmation_only: +1451.204R
  7. momentum_only: +0.339R
  8. prior_acc_only: -172.918R
- Biggest trade-killers vs all-off:
  - momentum gate: strongest choke point.
  - confirmation gate: second strongest choke point.
  - prior accumulation gate: third strongest choke point and only single gate that turned net R negative.
- Practical takeaway:
  - Refine first: momentum, confirmation, prior accumulation.
  - Keep as lighter filters: bias and RR.

## Latest Gate Stability Update (Dec 2025 + Jan 2025 + 2025 Full-Year Cross-Pair)

- Dec-2025 EURUSD 128-combo sweep:
  - Top ROI combos were `none` and `bias`; `structural` and `rr` were second-tier.
- Jan-2025 EURUSD 128-combo sweep:
  - Top ROI combos shifted toward `prior_acc` + `structural` families.
  - `penetration` combos were consistently weak/negative.
- Full-year 2025 (EURUSD + GBPJPY) shortlist retest:
  - Strong positives: `bias`, `none`.
  - `prior_acc` family combos turned negative on full-year cross-pair robustness.
- Working decision:
  - Keep the runner configs and notes in sync.
  - Treat the month/year runners as the practical baseline for current sweeps unless explicitly testing an alternate gate stack.
