"""Run v2 pipeline/backtester on one month of Dukas M1 data."""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from engine import run_pipeline_for_instrument
from engine.context import enrich_liquidity_context
from engine.io_utils import export_setups, export_trades
from run_dukas_common import build_default_runner_config, infer_pair_params


def load_dukas_m1(path: str | Path) -> pd.DataFrame:
    df = pd.read_parquet(path)
    if "timestamp" not in df.columns:
        raise ValueError("Expected Dukas parquet with 'timestamp' column (epoch ms).")
    for col in ("open", "high", "low", "close"):
        if col not in df.columns:
            raise ValueError(f"Missing OHLC column: {col}")

    ts = pd.to_datetime(df["timestamp"].to_numpy(), unit="ms", utc=True)
    out = df[["open", "high", "low", "close"]].copy()
    out["open"] = pd.to_numeric(out["open"], errors="coerce")
    out["high"] = pd.to_numeric(out["high"], errors="coerce")
    out["low"] = pd.to_numeric(out["low"], errors="coerce")
    out["close"] = pd.to_numeric(out["close"], errors="coerce")
    out.index = pd.DatetimeIndex(ts, name="timestamp")
    out = out.dropna().sort_index()
    return out


def resample_5m(df_m1: pd.DataFrame) -> pd.DataFrame:
    df_5m = df_m1.resample("5min").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}
    )
    return df_5m.dropna()


def add_min_context(df_5m: pd.DataFrame, lookback: int = 48) -> pd.DataFrame:
    # Backward-compatible alias; now builds richer PDF-aligned liquidity context.
    return enrich_liquidity_context(df_5m, eq_lookback=lookback)


def run_month(data_path: str, month: str, output_dir: str, warmup_days: int = 21) -> None:
    month_start = pd.Timestamp(f"{month}-01", tz="UTC")
    month_end = (month_start + pd.offsets.MonthBegin(1)) - pd.Timedelta(minutes=1)
    warmup_start = month_start - pd.Timedelta(days=warmup_days)

    m1_all = load_dukas_m1(data_path)
    m1_ctx = m1_all.loc[warmup_start:month_end]
    m1 = m1_all.loc[month_start:month_end]
    if m1.empty:
        print(f"[!] No M1 data found in {month} for {data_path}")
        return

    df_5m_ctx = resample_5m(m1_ctx)
    df_5m_ctx = add_min_context(df_5m_ctx, lookback=48).dropna()
    df_5m = df_5m_ctx.loc[month_start:month_end]
    if df_5m.empty:
        print("[!] Not enough bars after context feature build.")
        return
    pair = Path(data_path).name.split("-")[0].upper()
    pip_value, spread_pips, slip_pips = infer_pair_params(pair)
    cfg = build_default_runner_config(
        ohlc_1m=m1_ctx,
        pip_value=pip_value,
        spread_pips=spread_pips,
        slip_pips=slip_pips,
    )

    artifacts = run_pipeline_for_instrument(cfg, df_5m)

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    setups_csv = out_dir / f"{pair}_{month}_setups.csv"
    candidate_setups_csv = out_dir / f"{pair}_{month}_candidate_setups.csv"
    trades_csv = out_dir / f"{pair}_{month}_trades.csv"
    events_csv = out_dir / f"{pair}_{month}_execution_events.csv"

    export_setups(artifacts.setup_candidates, candidate_setups_csv)
    export_setups(artifacts.setups, setups_csv)
    export_trades(artifacts.trades, trades_csv)
    pd.DataFrame(artifacts.execution_events).to_csv(events_csv, index=False)

    print("=" * 72)
    print(f"Pair: {pair}")
    print(f"Month: {month} ({month_start} -> {month_end})")
    print(f"Warmup start: {warmup_start}")
    print(f"M1 bars: {len(m1):,}")
    print(f"M1 context bars: {len(m1_ctx):,}")
    print(f"5m bars: {len(df_5m):,}")
    print(f"Breaks: {artifacts.break_series.value_counts().to_dict()}")
    print(f"Bias: {artifacts.bias_series.value_counts().to_dict()}")
    print(f"Setups: {len(artifacts.setups)}")
    print(f"Candidate setups: {len(artifacts.setup_candidates)}")
    print(f"Orders: {len(artifacts.orders)}")
    print(f"Trades (filled): {len(artifacts.trades)}")
    print(f"Execution events: {len(artifacts.execution_events)}")
    print(f"Saved candidate setups: {candidate_setups_csv}")
    print(f"Saved setups: {setups_csv}")
    print(f"Saved trades: {trades_csv}")
    print(f"Saved events: {events_csv}")
    print("=" * 72)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run v2 backtest on one month of Dukas data.")
    parser.add_argument("--data", required=True, help="Path to Dukas parquet (M1).")
    parser.add_argument("--month", required=True, help="YYYY-MM month to test, e.g. 2025-01")
    parser.add_argument("--output", default="output_v2", help="Output folder for CSV files.")
    parser.add_argument("--warmup-days", type=int, default=21)
    args = parser.parse_args()

    run_month(args.data, args.month, args.output, warmup_days=args.warmup_days)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
