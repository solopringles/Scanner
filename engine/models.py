"""Lightweight data models used by the v2 engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from .types import DOLType, EntryModel, InvalidationReason, TradeState


@dataclass(slots=True)
class DOLCandidate:
    dol_type: DOLType
    price: float
    direction: str  # long|short
    source: str = ""
    strength: float = 1.0
    swept: bool = False
    protected: bool = False
    session_induced: bool = False


@dataclass(slots=True)
class OBZone:
    idx: int
    timestamp: pd.Timestamp
    direction: str  # long|short
    low: float
    high: float
    induced: bool = True
    tapped: bool = False
    created_in_manipulation: bool = False
    origin_bias: str = "NEUTRAL"
    prior_state: str = "VALID_OB"
    current_role: str = "OB"
    role_reason: str = ""
    protected: bool = True


@dataclass(slots=True)
class SetupSignal:
    timestamp: pd.Timestamp
    model: EntryModel
    direction: str  # long|short
    entry_price: float
    sl_price: float | None = None
    tp_price: float | None = None
    tags: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class OrderSpec:
    timestamp: pd.Timestamp
    direction: str
    entry_price: float
    sl_price: float
    tp_price: float
    qty: float
    rr: float
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class TradeRuntime:
    timestamp: pd.Timestamp
    direction: str
    state: TradeState
    entry_price: float
    sl_price: float
    tp_price: float
    qty: float
    fill_timestamp: pd.Timestamp | None = None
    exit_timestamp: pd.Timestamp | None = None
    partial_taken: bool = False
    be_trigger_price: float | None = None
    be_armed: bool = False
    exit_price: float | None = None
    exit_reason: str | None = None
    invalidation_reason: InvalidationReason | None = None
    commission_quote: float = 0.0


@dataclass(slots=True)
class PipelineArtifacts:
    df: pd.DataFrame
    phase_series: pd.Series
    break_series: pd.Series
    bias_series: pd.Series
    dol_candidates: list[DOLCandidate]
    order_blocks: list[OBZone]
    setup_candidates: list[SetupSignal]
    setups: list[SetupSignal]
    orders: list[OrderSpec]
    trades: list[TradeRuntime]
    execution_events: list[dict[str, Any]] = field(default_factory=list)
    htf_bias_series: pd.Series | None = None
    htf_regime_series: pd.Series | None = None
