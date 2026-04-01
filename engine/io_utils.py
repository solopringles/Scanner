"""Export helpers."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from pathlib import Path

import pandas as pd


def _rows_from_any(items: list) -> list[dict]:
    rows: list[dict] = []
    for item in items:
        if is_dataclass(item):
            rows.append(asdict(item))
        elif isinstance(item, dict):
            rows.append(dict(item))
        else:
            rows.append(item.__dict__)
    return rows


def export_setups(setups: list, path: str | Path) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(_rows_from_any(setups)).to_csv(p, index=False)


def export_trades(trades: list, path: str | Path) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(_rows_from_any(trades)).to_csv(p, index=False)

