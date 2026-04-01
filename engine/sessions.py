"""Session mapping helpers."""

from __future__ import annotations

from datetime import datetime, time

import pandas as pd

from .config import SessionConfig
from .types import SessionLabel


def _extract_hour_london(ts_london: pd.Timestamp | datetime | time | int) -> int:
    if isinstance(ts_london, int):
        return int(ts_london) % 24
    if isinstance(ts_london, time):
        return ts_london.hour
    ts = pd.Timestamp(ts_london)
    # Normalize all timestamps into Europe/London to keep session boundaries stable
    # across DST changes.
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return ts.tz_convert("Europe/London").hour


def map_session(ts_london: pd.Timestamp | datetime | time | int, cfg: SessionConfig | None = None) -> SessionLabel:
    cfg = cfg or SessionConfig()
    hour = _extract_hour_london(ts_london)

    if cfg.asia_start_hour <= hour < cfg.london_start_hour:
        return SessionLabel.ASIA
    if cfg.london_start_hour <= hour < cfg.london_end_hour:
        return SessionLabel.LONDON
    if cfg.new_york_start_hour <= hour < cfg.new_york_end_hour:
        return SessionLabel.NEW_YORK
    return SessionLabel.OFF_HOURS


def session_weight(session: SessionLabel, pair: str) -> float:
    pair_u = pair.upper()
    if session in (SessionLabel.LONDON, SessionLabel.NEW_YORK):
        return 1.0
    if session == SessionLabel.ASIA and any(k in pair_u for k in ("JPY", "CHF", "AUD", "NZD")):
        return 0.7
    if session == SessionLabel.ASIA:
        return 0.45
    return 0.3
