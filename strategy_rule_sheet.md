# V2 Rule Sheet

Purpose: keep the discretionary structure readable while making the code boundaries explicit.

This document is not a new strategy.
It is a shared vocabulary for audit work, so we can say:
- what is a raw market fact,
- what is a strict rule,
- what is a soft/discretionary label,
- and where the code should not pretend certainty.

## 1) Core Principle

Price action is mechanical.
Our labels are not always mechanical.

So we separate:
- `fact`: what the candle actually did
- `strict`: what the engine may trade
- `soft`: what the mentor may still call valid, but the engine should only log

If a candle fits `soft` but not `strict`, we log it and do not force a trade.

## 2) Shared Definitions

### Sweep

Raw fact:
- price wicks through a known level
- then reacts away from it

Strict sweep:
- wick penetration is meaningful, not just a 0.1 pip touch
- there is a visible rejection
- the sweep is on the correct side for the intended direction

Soft sweep:
- the level is clearly run
- reaction is there, but the wick or displacement is not clean enough to be a trade on its own

### Order Block

Raw fact:
- the last opposite candle before displacement

Strict OB:
- it is the last opposite candle before the move
- the move away is strong enough to matter
- the zone remains unbroken until mitigation

Soft OB:
- the candle looks like the OB by eye
- but the displacement or invalidation state is not clean enough yet for a strict engine entry

### Mitigation

Raw fact:
- price returns to the OB zone after displacement

Strict mitigation:
- first clean tap into the OB
- OB is still valid
- entry is allowed only when the trigger sequence is complete

Soft mitigation:
- price revisits the zone
- but the touch is messy, late, or already too far into invalidation territory

### RBOS

Raw fact:
- structure breaks in the opposite direction after manipulation / inducement

Strict RBOS:
- a real structural break
- on the correct side
- used only to set bias

Soft RBOS:
- the move looks like a reversal confirmation to a human
- but the code has not met the full break criteria yet

## 3) Rules We Keep Separate

### A. Sweep is not automatically FBOS

A sweep can exist without becoming FBOS.

Use this when:
- level was run,
- reaction happened,
- but there was no full FBOS chain.

### B. OB is not automatically mitigation

An OB can exist without a valid entry.

Use this when:
- the candle is the right OB candle,
- but the tap / BOS / validity state is not complete.

### C. RBOS is bias, not an entry by itself

RBOS should:
- flip bias up or down,
- remain visible as a logged state,
- not rewrite the entry model on its own.

## 4) Audit Tiers

### Strict Tier

Used by the engine.

Requirements:
- clean structure
- no lookahead
- explicit trigger sequence
- entries only when the rule path is complete

### Soft Tier

Used for mentor comparison and logging.

Requirements:
- visually plausible
- useful for discussion
- not necessarily tradable

### Ignore Tier

Used when:
- the move is just continuation,
- the level is too weak,
- or the structure is too compressed to trust.

## 5) Practical Tagging Rules

When reviewing a candle block, tag each feature separately:

- `fact_sweep`
- `strict_sweep`
- `soft_sweep`
- `fact_ob`
- `strict_ob`
- `soft_ob`
- `fact_mitigation`
- `strict_mitigation`
- `fact_rbos`
- `strict_rbos`

This prevents one label from doing too much work.

## 6) Decision Flow

1. Record the raw candle facts.
2. Decide whether the move is a strict sweep or only a soft sweep.
3. Decide whether the candle is a strict OB or only a soft OB.
4. Decide whether the retest is strict mitigation or only a soft revisit.
5. Decide whether the break is strict RBOS.
6. Only then compare against the code.

## 7) How We Use This In February Reviews

For each candidate block:
- if the code says no and the mentor says yes, check whether the label belongs in `soft` rather than `strict`
- if the code and mentor agree, keep it in `strict`
- if neither side is clear, mark it `ignore`

This is the part that keeps us honest:
- we do not weaken the engine just because a candle looks plausible,
- and we do not dismiss a useful read just because the strict rule is too narrow.

## 8) Working Default

Until we explicitly change it:
- `strict` is what the engine trades
- `soft` is what we log for review
- `ignore` is noise

