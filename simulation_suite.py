"""
V2 strategy simulation suite.

Goal:
- Build synthetic scenarios that exercise each dictionary concept from
  v2_strategy_dictionary_pseudocode.md.
- Provide deterministic PASS/FAIL checks to align implementation behavior
  before coding full engine logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable

class AMDPhase(Enum):
    ACCUMULATION = "ACCUMULATION"
    MANIPULATION = "MANIPULATION"
    DISTRIBUTION = "DISTRIBUTION"


class BreakType(Enum):
    FBOS = "FBOS"
    RBOS = "RBOS"
    NONE = "NONE"


class BiasDirection(Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"


class SessionLabel(Enum):
    ASIA = "ASIA"
    LONDON = "LONDON"
    NEW_YORK = "NEW_YORK"
    OFF_HOURS = "OFF_HOURS"


class DOLType(Enum):
    EQH_EQL = "EQH_EQL"
    SESSION_HL = "SESSION_HL"
    PDH_PDL = "PDH_PDL"
    PWH_PWL = "PWH_PWL"
    SMT_OPEN = "SMT_OPEN"
    STRUCT_SWING = "STRUCT_SWING"
    INTERNAL_DOL = "INTERNAL_DOL"


class OBState(Enum):
    VALID_OB = "VALID_OB"
    SMT_TRAP = "SMT_TRAP"
    DEPLETED = "DEPLETED"


class EntryTrigger(Enum):
    SWEEP_CLOSE_AGGRESSIVE = "SWEEP_CLOSE_AGGRESSIVE"
    POST_SWEEP_CONFIRMATION = "POST_SWEEP_CONFIRMATION"
    OB_TAP_PLUS_M5_BOS = "OB_TAP_PLUS_M5_BOS"
    LIMIT_ON_ZONE = "LIMIT_ON_ZONE"


class InvalidationReason(Enum):
    DOL_ALREADY_HIT = "DOL_ALREADY_HIT"
    COUNTER_HTF_BIAS = "COUNTER_HTF_BIAS"
    OB_ALREADY_TAPPED = "OB_ALREADY_TAPPED"
    MOMENTUM_NOT_SHARP = "MOMENTUM_NOT_SHARP"
    RETURNED_TO_RANGE = "RETURNED_TO_RANGE"
    PROTECTED_LEVEL_SWEEPED = "PROTECTED_LEVEL_SWEEPED"
    HTF_POI_INSIDE_SL = "HTF_POI_INSIDE_SL"
    NEWS_WINDOW_BLOCK = "NEWS_WINDOW_BLOCK"
    OPPOSING_HTF_BOS = "OPPOSING_HTF_BOS"


class TradeState(Enum):
    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    BE_MOVED = "BE_MOVED"
    PARTIAL_TP_HIT = "PARTIAL_TP_HIT"
    CLOSED = "CLOSED"
    INVALIDATED = "INVALIDATED"


@dataclass
class Candle:
    open: float
    high: float
    low: float
    close: float
    hour_london: int

    @property
    def body(self) -> float:
        return abs(self.close - self.open)

    @property
    def rng(self) -> float:
        return max(0.0, self.high - self.low)

    @property
    def body_ratio(self) -> float:
        if self.rng == 0:
            return 0.0
        return self.body / self.rng

    @property
    def bullish(self) -> bool:
        return self.close > self.open

    @property
    def bearish(self) -> bool:
        return self.close < self.open


def is_range_bound(candles: list[Candle], max_span: float = 1.2) -> bool:
    if not candles:
        return False
    hi = max(c.high for c in candles)
    lo = min(c.low for c in candles)
    span = hi - lo
    avg_rng = sum(c.rng for c in candles) / len(candles)
    return span <= max_span * max(avg_rng, 1e-9)


def is_one_sided_impulse(candles: list[Candle], min_body_ratio: float = 0.6) -> bool:
    if len(candles) < 2:
        return False
    same_dir = all(c.bullish for c in candles) or all(c.bearish for c in candles)
    strong = all(c.body_ratio >= min_body_ratio for c in candles)
    return same_dir and strong


def has_structured_pullbacks(candles: list[Candle]) -> bool:
    if len(candles) < 4:
        return False
    # Simplified continuation signature:
    # at least one opposite candle and net move preserved.
    has_opposite = any(c.bearish for c in candles) and any(c.bullish for c in candles)
    net_up = candles[-1].close > candles[0].open
    net_down = candles[-1].close < candles[0].open
    return has_opposite and (net_up or net_down)


def detect_amd_phase(window: list[Candle]) -> AMDPhase:
    if is_range_bound(window):
        return AMDPhase.ACCUMULATION
    if is_one_sided_impulse(window):
        return AMDPhase.MANIPULATION
    if has_structured_pullbacks(window):
        return AMDPhase.DISTRIBUTION
    return AMDPhase.ACCUMULATION


def classify_break(
    phase_before_break: AMDPhase,
    breaks_level: bool,
    one_sided_impulse: bool,
    later_reversal: bool,
    follows_manipulation_with_confirmation: bool,
) -> BreakType:
    if not breaks_level:
        return BreakType.NONE
    if phase_before_break == AMDPhase.ACCUMULATION and one_sided_impulse and later_reversal:
        return BreakType.FBOS
    if follows_manipulation_with_confirmation:
        return BreakType.RBOS
    return BreakType.NONE


def derive_bias(tapped_htf_poi: bool, rbos_up: bool, rbos_down: bool, dol_above: bool, dol_below: bool) -> BiasDirection:
    _ = (tapped_htf_poi, dol_above, dol_below)
    if rbos_up:
        return BiasDirection.BULLISH
    if rbos_down:
        return BiasDirection.BEARISH
    return BiasDirection.NEUTRAL


def map_session(hour_london: int) -> SessionLabel:
    if 0 <= hour_london < 7:
        return SessionLabel.ASIA
    if 7 <= hour_london < 13:
        return SessionLabel.LONDON
    if 13 <= hour_london < 17:
        return SessionLabel.NEW_YORK
    return SessionLabel.OFF_HOURS


def rank_dol_candidates(candidates: list[DOLType], bias: BiasDirection) -> list[DOLType]:
    # Bias is included for interface parity; direction filtering omitted in this synthetic suite.
    _ = bias
    priority = [
        DOLType.EQH_EQL,
        DOLType.SESSION_HL,
        DOLType.PDH_PDL,
        DOLType.PWH_PWL,
        DOLType.SMT_OPEN,
        DOLType.STRUCT_SWING,
        DOLType.INTERNAL_DOL,
    ]
    present = set(candidates)
    return [p for p in priority if p in present]


def classify_ob_state(
    tapped_before: bool,
    dol_hit: bool,
    rbos_away: bool,
    created_in_manipulation: bool,
) -> OBState:
    if tapped_before:
        return OBState.DEPLETED
    if dol_hit or rbos_away or created_in_manipulation:
        return OBState.SMT_TRAP
    return OBState.VALID_OB


def trigger_fbos(aggressive: bool, conservative_confirmation: bool, mitigation_wait: bool) -> EntryTrigger | None:
    if aggressive:
        return EntryTrigger.SWEEP_CLOSE_AGGRESSIVE
    if conservative_confirmation:
        return EntryTrigger.POST_SWEEP_CONFIRMATION
    if mitigation_wait:
        return EntryTrigger.OB_TAP_PLUS_M5_BOS
    return None


def trigger_mitigation(valid_ob: bool, rr_ok: bool, conservative_ok: bool, aggressive_ok: bool) -> EntryTrigger | None:
    if not valid_ob or not rr_ok:
        return None
    if conservative_ok:
        return EntryTrigger.OB_TAP_PLUS_M5_BOS
    if aggressive_ok:
        return EntryTrigger.LIMIT_ON_ZONE
    return None


def check_invalidation(
    dol_hit=False,
    counter_bias=False,
    ob_tapped=False,
    weak_momentum=False,
    returned_to_range=False,
    protected_level=False,
    htf_poi_inside_sl=False,
    news_window=False,
    opposing_bos=False,
) -> InvalidationReason | None:
    if dol_hit:
        return InvalidationReason.DOL_ALREADY_HIT
    if counter_bias:
        return InvalidationReason.COUNTER_HTF_BIAS
    if ob_tapped:
        return InvalidationReason.OB_ALREADY_TAPPED
    if weak_momentum:
        return InvalidationReason.MOMENTUM_NOT_SHARP
    if returned_to_range:
        return InvalidationReason.RETURNED_TO_RANGE
    if protected_level:
        return InvalidationReason.PROTECTED_LEVEL_SWEEPED
    if htf_poi_inside_sl:
        return InvalidationReason.HTF_POI_INSIDE_SL
    if news_window:
        return InvalidationReason.NEWS_WINDOW_BLOCK
    if opposing_bos:
        return InvalidationReason.OPPOSING_HTF_BOS
    return None


@dataclass
class SimTrade:
    state: TradeState = TradeState.PENDING
    entry: float = 100.0
    sl: float = 99.0
    tp: float = 102.0
    partial_tp: float = 101.0
    partial_taken: bool = False


def advance_trade(trade: SimTrade, bar: Candle, invalidation: bool = False) -> None:
    if trade.state == TradeState.PENDING:
        # Synthetic entry condition: any close above entry
        if bar.close >= trade.entry:
            trade.state = TradeState.ACTIVE

    if trade.state in (TradeState.ACTIVE, TradeState.BE_MOVED, TradeState.PARTIAL_TP_HIT):
        if invalidation:
            trade.state = TradeState.INVALIDATED
            return
        if bar.low <= trade.sl:
            trade.state = TradeState.CLOSED
            return
        if (not trade.partial_taken) and bar.high >= trade.partial_tp:
            trade.partial_taken = True
            trade.state = TradeState.PARTIAL_TP_HIT
        # BE move rule (simplified): once partial is taken.
        if trade.partial_taken and trade.state != TradeState.CLOSED:
            trade.sl = trade.entry
            trade.state = TradeState.BE_MOVED
        if bar.high >= trade.tp:
            trade.state = TradeState.CLOSED


@dataclass
class Scenario:
    name: str
    concept: str
    expected: str
    runner: Callable[[], str]
    candles: list[Candle] | None = None
    note: str = ""


def make_scenarios() -> list[Scenario]:
    s: list[Scenario] = []

    # 1) Perfect AMD trade path
    acc = [
        Candle(100, 101, 99.7, 100.2, 3),
        Candle(100.2, 100.8, 99.9, 100.1, 4),
        Candle(100.1, 100.7, 99.8, 100.2, 5),
    ]
    man = [
        Candle(100.2, 100.25, 99.3, 99.35, 8),
        Candle(99.35, 99.4, 98.6, 98.65, 8),
        Candle(98.65, 98.7, 97.9, 98.0, 9),
    ]
    dist = [
        Candle(98.0, 98.8, 97.95, 98.7, 13),
        Candle(98.7, 99.0, 98.4, 98.5, 13),
        Candle(98.5, 99.5, 98.45, 99.4, 14),
        Candle(99.4, 100.4, 99.3, 100.3, 14),
    ]

    def perfect_amd_trade() -> str:
        phases = [detect_amd_phase(acc), detect_amd_phase(man), detect_amd_phase(dist)]
        b = classify_break(AMDPhase.ACCUMULATION, True, True, True, False)
        r = classify_break(AMDPhase.MANIPULATION, True, False, False, True)
        ok = phases == [AMDPhase.ACCUMULATION, AMDPhase.MANIPULATION, AMDPhase.DISTRIBUTION] and b == BreakType.FBOS and r == BreakType.RBOS
        return "PASS" if ok else f"FAIL phases={phases} break={b.value}/{r.value}"

    s.append(
        Scenario(
            "perfect_amd_trade",
            "AMDPhase+BreakType",
            "PASS",
            perfect_amd_trade,
            candles=acc + man + dist,
            note="Range -> one-sided sweep/manipulation -> structured distribution reversal.",
        )
    )

    # 2) Manipulation not one-sided -> no FBOS
    def weak_manipulation() -> str:
        b = classify_break(AMDPhase.ACCUMULATION, True, False, True, False)
        return "PASS" if b == BreakType.NONE else f"FAIL got={b.value}"

    weak_seq = [
        Candle(100.0, 100.3, 99.6, 99.8, 8),
        Candle(99.8, 100.1, 99.5, 99.9, 8),
        Candle(99.9, 100.0, 99.4, 99.6, 9),
    ]
    s.append(
        Scenario(
            "weak_manipulation_no_fbos",
            "BreakType",
            "PASS",
            weak_manipulation,
            candles=weak_seq,
            note="Not one-sided momentum; should reject FBOS classification.",
        )
    )

    # 3) Bullish bias confirmation
    def bias_bull() -> str:
        bias = derive_bias(True, True, False, True, False)
        return "PASS" if bias == BiasDirection.BULLISH else f"FAIL got={bias.value}"

    bias_seq = [
        Candle(99.0, 99.6, 98.8, 99.5, 12),
        Candle(99.5, 100.1, 99.3, 100.0, 13),
        Candle(100.0, 100.8, 99.9, 100.7, 13),
    ]
    s.append(
        Scenario(
            "bias_bullish_confirmed",
            "BiasDirection",
            "PASS",
            bias_bull,
            candles=bias_seq,
            note="Synthetic bullish confirmation leg after HTF tap + RBoS up.",
        )
    )

    # 4) Session map checks
    def sessions() -> str:
        labels = [map_session(2), map_session(8), map_session(14), map_session(20)]
        expect = [SessionLabel.ASIA, SessionLabel.LONDON, SessionLabel.NEW_YORK, SessionLabel.OFF_HOURS]
        return "PASS" if labels == expect else f"FAIL got={[x.value for x in labels]}"

    session_seq = [
        Candle(100, 100.2, 99.9, 100.1, 2),
        Candle(100.1, 100.4, 100.0, 100.3, 8),
        Candle(100.3, 100.7, 100.2, 100.6, 14),
        Candle(100.6, 100.8, 100.4, 100.5, 20),
    ]
    s.append(
        Scenario(
            "session_mapping",
            "SessionLabel",
            "PASS",
            sessions,
            candles=session_seq,
            note="Bars tagged ASIA/LONDON/NEW_YORK/OFF_HOURS by London hour.",
        )
    )

    # 5) DOL priority checks
    def dol_priority() -> str:
        ranked = rank_dol_candidates(
            [DOLType.SMT_OPEN, DOLType.PDH_PDL, DOLType.EQH_EQL],
            BiasDirection.BULLISH,
        )
        expect = [DOLType.EQH_EQL, DOLType.PDH_PDL, DOLType.SMT_OPEN]
        return "PASS" if ranked == expect else f"FAIL got={[x.value for x in ranked]}"

    dol_seq = [
        Candle(100.0, 100.5, 99.8, 100.3, 9),
        Candle(100.3, 100.9, 100.2, 100.8, 10),
        Candle(100.8, 101.0, 100.4, 100.6, 11),
    ]
    s.append(
        Scenario(
            "dol_priority_order",
            "DOLType",
            "PASS",
            dol_priority,
            candles=dol_seq,
            note="DOL ranking check: EQH/EQL > PDH/PDL > SMT_OPEN in this test.",
        )
    )

    # 6) OB valid state
    def ob_valid() -> str:
        st = classify_ob_state(False, False, False, False)
        return "PASS" if st == OBState.VALID_OB else f"FAIL got={st.value}"

    ob_valid_seq = [
        Candle(100.0, 100.2, 99.6, 99.7, 8),
        Candle(99.7, 100.6, 99.6, 100.5, 8),
        Candle(100.5, 101.0, 100.4, 100.9, 9),
    ]
    s.append(
        Scenario(
            "ob_valid_state",
            "OBState",
            "PASS",
            ob_valid,
            candles=ob_valid_seq,
            note="Fresh inducement OB, not tapped yet, not converted to SMT.",
        )
    )

    # 7) OB -> SMT by DOL hit
    def ob_to_smt_dol() -> str:
        st = classify_ob_state(False, True, False, False)
        return "PASS" if st == OBState.SMT_TRAP else f"FAIL got={st.value}"

    ob_smt_seq = [
        Candle(100.0, 100.3, 99.7, 99.9, 8),
        Candle(99.9, 101.3, 99.8, 101.2, 13),
        Candle(101.2, 101.4, 100.9, 101.0, 14),
    ]
    s.append(
        Scenario(
            "ob_to_smt_on_dol_hit",
            "OBState",
            "PASS",
            ob_to_smt_dol,
            candles=ob_smt_seq,
            note="Price reaches target zone (DOL hit), OB should be reclassified to SMT.",
        )
    )

    # 8) OB depleted after prior tap
    def ob_depleted() -> str:
        st = classify_ob_state(True, False, False, False)
        return "PASS" if st == OBState.DEPLETED else f"FAIL got={st.value}"

    ob_depleted_seq = [
        Candle(100.0, 100.2, 99.7, 99.8, 8),
        Candle(99.8, 100.9, 99.8, 100.8, 9),
        Candle(100.8, 101.0, 99.75, 99.9, 13),
    ]
    s.append(
        Scenario(
            "ob_depleted_on_retap",
            "OBState",
            "PASS",
            ob_depleted,
            candles=ob_depleted_seq,
            note="Previously tapped OB becomes depleted.",
        )
    )

    # 9) FBOS conservative trigger
    def fbos_trigger() -> str:
        tr = trigger_fbos(False, True, False)
        return "PASS" if tr == EntryTrigger.POST_SWEEP_CONFIRMATION else f"FAIL got={tr}"

    fbos_trig_seq = [
        Candle(100.0, 100.2, 99.4, 99.5, 8),
        Candle(99.5, 99.8, 99.2, 99.7, 8),
        Candle(99.7, 100.4, 99.6, 100.3, 9),
    ]
    s.append(
        Scenario(
            "fbos_conservative_trigger",
            "EntryTrigger",
            "PASS",
            fbos_trigger,
            candles=fbos_trig_seq,
            note="Sweep then confirmation candle before entry.",
        )
    )

    # 10) Mitigation conservative trigger
    def mit_trigger() -> str:
        tr = trigger_mitigation(True, True, True, False)
        return "PASS" if tr == EntryTrigger.OB_TAP_PLUS_M5_BOS else f"FAIL got={tr}"

    mit_trig_seq = [
        Candle(100.0, 100.8, 99.9, 100.7, 9),
        Candle(100.7, 100.75, 100.1, 100.2, 10),
        Candle(100.2, 100.9, 100.15, 100.85, 10),
    ]
    s.append(
        Scenario(
            "mitigation_trigger",
            "EntryTrigger",
            "PASS",
            mit_trigger,
            candles=mit_trig_seq,
            note="OB tap then BOS confirmation entry.",
        )
    )

    # 11) Invalidation by news window
    def invalid_news() -> str:
        inv = check_invalidation(news_window=True)
        return "PASS" if inv == InvalidationReason.NEWS_WINDOW_BLOCK else f"FAIL got={inv}"

    news_seq = [
        Candle(100.0, 100.4, 99.9, 100.3, 12),
        Candle(100.3, 100.5, 99.7, 99.9, 12),
    ]
    s.append(
        Scenario(
            "invalidation_news",
            "InvalidationReason",
            "PASS",
            invalid_news,
            candles=news_seq,
            note="Setup blocked by high-impact news window.",
        )
    )

    # 12) Invalidation by counter bias
    def invalid_counter_bias() -> str:
        inv = check_invalidation(counter_bias=True)
        return "PASS" if inv == InvalidationReason.COUNTER_HTF_BIAS else f"FAIL got={inv}"

    counter_seq = [
        Candle(100.0, 100.2, 99.8, 99.9, 9),
        Candle(99.9, 100.0, 99.3, 99.4, 10),
    ]
    s.append(
        Scenario(
            "invalidation_counter_bias",
            "InvalidationReason",
            "PASS",
            invalid_counter_bias,
            candles=counter_seq,
            note="LTF setup conflicts with HTF bias.",
        )
    )

    # 13) Trade state from pending to active
    def trade_active() -> str:
        t = SimTrade()
        advance_trade(t, Candle(100, 100.5, 99.8, 100.1, 9))
        return "PASS" if t.state in (TradeState.ACTIVE, TradeState.BE_MOVED, TradeState.PARTIAL_TP_HIT, TradeState.CLOSED) else f"FAIL got={t.state.value}"

    active_seq = [
        Candle(99.8, 100.2, 99.7, 100.1, 9),
        Candle(100.1, 100.4, 99.9, 100.3, 9),
    ]
    s.append(
        Scenario(
            "trade_state_activation",
            "TradeState",
            "PASS",
            trade_active,
            candles=active_seq,
            note="Pending order transitions to active state.",
        )
    )

    # 14) Trade moves to BE after partial
    def trade_be() -> str:
        t = SimTrade()
        advance_trade(t, Candle(100, 101.1, 99.9, 100.8, 10))
        return "PASS" if t.sl == t.entry and t.state in (TradeState.BE_MOVED, TradeState.CLOSED) else f"FAIL state={t.state.value} sl={t.sl}"

    be_seq = [
        Candle(100.0, 101.15, 99.9, 100.9, 10),
        Candle(100.9, 101.3, 100.5, 101.2, 10),
    ]
    s.append(
        Scenario(
            "trade_state_be_move",
            "TradeState",
            "PASS",
            trade_be,
            candles=be_seq,
            note="Partial target touched; SL moved to breakeven.",
        )
    )

    # 15) Trade invalidation path
    def trade_invalidation() -> str:
        t = SimTrade(state=TradeState.ACTIVE)
        advance_trade(t, Candle(100.2, 100.6, 100.0, 100.4, 11), invalidation=True)
        return "PASS" if t.state == TradeState.INVALIDATED else f"FAIL got={t.state.value}"

    invalid_seq = [
        Candle(100.2, 100.6, 100.0, 100.4, 11),
        Candle(100.4, 100.5, 99.6, 99.8, 11),
    ]
    s.append(
        Scenario(
            "trade_state_invalidation",
            "TradeState",
            "PASS",
            trade_invalidation,
            candles=invalid_seq,
            note="Active trade invalidated by context rule.",
        )
    )

    return s


def run_suite(group: int | None = None) -> int:
    scenarios = make_scenarios()
    if group is not None:
        if group < 1:
            raise ValueError("group must be >= 1")
        start = (group - 1) * 5
        end = start + 5
        scenarios = scenarios[start:end]

    print("=" * 88)
    print("V2 STRATEGY SIMULATION SUITE")
    print("=" * 88)
    print(f"Total scenario count: {len(make_scenarios())}")
    print("Concept count: 10 (AMDPhase, BreakType, BiasDirection, SessionLabel, DOLType, "
          "OBState, EntryTrigger, InvalidationReason, TradeState, end-to-end AMD path)")
    if group is not None:
        print(f"Running group: {group} (5 scenarios per group)")
    print("-" * 88)

    passed = 0
    for i, sc in enumerate(scenarios, start=1):
        result = sc.runner()
        ok = result == sc.expected
        status = "PASS" if ok else "FAIL"
        if ok:
            passed += 1
        print(f"{i:02d}. [{status}] {sc.name:<30} concept={sc.concept}")
        if not ok:
            print(f"    expected={sc.expected} got={result}")

    total = len(scenarios)
    print("-" * 88)
    print(f"Passed: {passed}/{total}")

    return 0 if passed == total else 1


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run v2 strategy synthetic simulations.")
    parser.add_argument("--group", type=int, default=None, help="Run in groups of 5 (1-based).")
    args = parser.parse_args()
    raise SystemExit(run_suite(group=args.group))
