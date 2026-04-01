# V2 Strategy Dictionary + Pseudocode (Alignment Draft)

Purpose: define shared terms as code-friendly objects and outline first-pass pseudocode for each.

## 1) Dictionary (code-facing)

```python
AMDPhase = Enum("ACCUMULATION", "MANIPULATION", "DISTRIBUTION")

BreakType = Enum("FBOS", "RBOS", "NONE")

BiasDirection = Enum("BULLISH", "BEARISH", "NEUTRAL")

SessionLabel = Enum("ASIA", "LONDON", "NEW_YORK", "OFF_HOURS")

DOLType = Enum(
    "EQH_EQL",        # equal highs/lows
    "SESSION_HL",     # session highs/lows
    "PDH_PDL",        # previous day high/low
    "PWH_PWL",        # previous week high/low
    "SMT_OPEN",       # preferred TP target
    "STRUCT_SWING",   # fallback structural swing
    "INTERNAL_DOL",   # nearer in-leg liquidity
)

OBState = Enum("VALID_OB", "SMT_TRAP", "DEPLETED")

EntryModel = Enum(
    "FBOS",
    "MITIGATION",
    "SMT_REACTION",
    "INVERSE_REACTION",
    "INDUCEMENT_REACTION",
)

EntryTrigger = Enum(
    "SWEEP_CLOSE_AGGRESSIVE",
    "POST_SWEEP_CONFIRMATION",
    "OB_TAP_PLUS_M5_BOS",
    "LIMIT_ON_ZONE",
)

InvalidationReason = Enum(
    "DOL_ALREADY_HIT",
    "COUNTER_HTF_BIAS",
    "OB_ALREADY_TAPPED",
    "MOMENTUM_NOT_SHARP",
    "RETURNED_TO_RANGE",
    "PROTECTED_LEVEL_SWEEPED",
    "HTF_POI_INSIDE_SL",
    "NEWS_WINDOW_BLOCK",
    "OPPOSING_HTF_BOS",
)

TradeState = Enum(
    "PENDING",
    "ACTIVE",
    "BE_MOVED",
    "PARTIAL_TP_HIT",
    "CLOSED",
    "INVALIDATED",
)
```

## 2) Pseudocode per dictionary item

### AMDPhase

```text
function detect_amd_phase(df, i):
    if range_bound(df, lookback=i-window:i) and low_momentum(df, i):
        return ACCUMULATION
    if one_sided_impulse(df, i, min_body_ratio, min_consecutive) and little_pullback(df, i):
        return MANIPULATION
    if structured_leg(df, i) and pullback_continuation(df, i):
        return DISTRIBUTION
    return previous_phase_or_neutral
```

### BreakType (FBOS vs RBOS)

```text
function classify_break(df, i, direction, context):
    if not breaks_significant_swing(df, i, direction):
        return NONE
    if context.phase_before_break == ACCUMULATION and impulse_is_one_sided(df, i) and later_reverses(df, i, context):
        return FBOS
    if follows_manipulation_and_confirms_bias(df, i, context):
        return RBOS
    return NONE
```

### BiasDirection

```text
function derive_bias(htf_ctx):
    if tapped_valid_htf_poi(htf_ctx) and printed_rbos_up(htf_ctx) and dol_above(htf_ctx):
        return BULLISH
    if tapped_valid_htf_poi(htf_ctx) and printed_rbos_down(htf_ctx) and dol_below(htf_ctx):
        return BEARISH
    return NEUTRAL
```

```text
function invalidate_bias(bias_ctx):
    if bias_ob_broken(bias_ctx) or primary_dol_hit(bias_ctx) or htf_leg_taken_opposite(bias_ctx):
        return True
    return False
```

### SessionLabel

```text
function map_session(ts_london):
    if 00:00 <= time < 07:00: return ASIA
    if 07:00 <= time < 13:00: return LONDON
    if 13:00 <= time < 17:00: return NEW_YORK
    return OFF_HOURS
```

```text
function session_weight(session, pair):
    if session in [LONDON, NEW_YORK]: return HIGH
    if session == ASIA and pair_is_asia_active(pair): return MEDIUM
    return LOW
```

### DOLType

```text
function rank_dol_candidates(ctx):
    candidates = collect_eqh_eql + session_hl + pdh_pdl + pwh_pwl + smt_open + struct_swings
    aligned = filter_by_bias_direction(candidates, ctx.bias)
    valid = remove_invalid_dol(aligned, already_swept, protected_levels, session_induced)
    return sort_by_priority(valid, [EQH_EQL, SESSION_HL, PDH_PDL, PWH_PWL, SMT_OPEN, STRUCT_SWING])
```

```text
function select_target_dol(ctx):
    primary = first(rank_dol_candidates(ctx))
    if far_distance(primary) and has_reaccumulation_signals(ctx):
        return INTERNAL_DOL
    return primary
```

### OBState

```text
function classify_ob_state(ob, market_ctx):
    if ob_tapped_before(ob): return DEPLETED
    if dol_hit_for_ob_direction(ob, market_ctx): return SMT_TRAP
    if strong_rbos_away_from_ob(ob, market_ctx): return SMT_TRAP
    if ob_created_in_manipulation(ob): return SMT_TRAP
    return VALID_OB
```

### EntryModel

```text
function choose_entry_model(trade_ctx):
    if beginner_mode: return FBOS or MITIGATION
    if external_range_edge_setup(trade_ctx):
        return FBOS or MITIGATION
    if internal_continuation_setup(trade_ctx):
        return SMT_REACTION or INVERSE_REACTION or INDUCEMENT_REACTION
```

### EntryTrigger

```text
function trigger_fbos(ctx):
    assert fbos_criteria_all_true(ctx)
    if mode == aggressive: return SWEEP_CLOSE_AGGRESSIVE
    if mode == conservative and rejection_confirmed(ctx): return POST_SWEEP_CONFIRMATION
    if mode == mitigation_wait: return OB_TAP_PLUS_M5_BOS
    return NONE
```

```text
function trigger_mitigation(ctx):
    assert valid_inducement_ob(ctx) and ob_state == VALID_OB and rr_to_dol >= 2.0
    if conservative and ob_tap_seen(ctx) and m5_bos_after_tap(ctx):
        return OB_TAP_PLUS_M5_BOS
    if aggressive and extreme_confluence(ctx):
        return LIMIT_ON_ZONE
    return NONE
```

### InvalidationReason

```text
function check_invalidation(ctx):
    if primary_dol_hit(ctx): return DOL_ALREADY_HIT
    if counter_htf_bias(ctx): return COUNTER_HTF_BIAS
    if ob_already_tapped(ctx): return OB_ALREADY_TAPPED
    if weak_manipulation(ctx): return MOMENTUM_NOT_SHARP
    if reentered_accumulation(ctx): return RETURNED_TO_RANGE
    if level_is_protected(ctx): return PROTECTED_LEVEL_SWEEPED
    if htf_poi_inside_stop_distance(ctx): return HTF_POI_INSIDE_SL
    if high_impact_news_within_15m(ctx): return NEWS_WINDOW_BLOCK
    if opposing_bos_m15_plus(ctx): return OPPOSING_HTF_BOS
    return None
```

### TradeState

```text
function manage_trade_state(trade, bar):
    if trade.state == PENDING and entry_triggered(trade, bar):
        trade.state = ACTIVE
    if trade.state == ACTIVE and be_condition_met(trade, bar):
        move_stop_to_be(trade); trade.state = BE_MOVED
    if trade.state in [ACTIVE, BE_MOVED] and partial_tp_hit(trade, bar):
        close_partial(trade, 0.5); trade.state = PARTIAL_TP_HIT
    if sl_hit(trade, bar) or tp_hit(trade, bar) or invalidated(trade, bar):
        close_trade(trade); trade.state = CLOSED or INVALIDATED
```

## 3) End-to-end scanner/backtester flow (v2)

```text
for each instrument:
    load M1 -> resample M5
    annotate sessions, HTF levels, structure, equal levels, POIs
    phase_series = detect_amd_phase_per_bar
    break_labels = classify each break as FBOS/RBOS/NONE
    bias = derive from HTF POI + RBOS + DOL alignment

    dol_map = build and rank DOL candidates
    ob_map = detect inducement OBs and classify state (VALID_OB/SMT/DEPLETED)

    setup_candidates = []
    setup_candidates += generate_fbos_setups(phase, break_labels, bias, dol_map, invalidation_checks)
    setup_candidates += generate_mitigation_setups(ob_map, bias, dol_map, tap+bOS trigger, invalidation_checks)
    setup_candidates += optional_internal_models(...)

    apply timing/news filters and RR>=2 gate
    build orders (entry, sl at wick logic, tp at SMT open/next DOL)
    backtest with state transitions (BE, partials, invalidation exits)
    export setups, trades, diagnostics (why accepted/rejected)
```

## 4) Suggested implementation order

```text
1) Phase + break classifier (AMD + FBOS/RBOS)
2) Bias + DOL engine
3) OB state machine (OB -> SMT/depleted)
4) FBOS model v2 criteria + invalidations
5) Mitigation model v2 criteria + invalidations
6) Trade state manager (BE/partial protocol)
7) Internal models (SMT reaction/inverse/inducement) last
```

