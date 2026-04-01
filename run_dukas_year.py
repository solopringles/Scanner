"""Run v2 pipeline on full-year Dukas M1 data for both EURUSD and GBPJPY."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from pathlib import Path

import pandas as pd

from engine import run_pipeline_for_instrument
from run_dukas_common import build_default_runner_config, infer_pair_params
from run_dukas_month import add_min_context, load_dukas_m1, resample_5m


def discover_pair_path(data_dir: Path, pair: str) -> Path:
    candidates = sorted(data_dir.glob(f"{pair.lower()}-m1-bid-*.parquet"))
    if not candidates:
        raise FileNotFoundError(f"No Dukas parquet found for {pair} in {data_dir}")
    return candidates[-1]


def run_year(year: int, output_dir: str, warmup_days: int = 21, data_dir: str | Path | None = None) -> None:
    base = Path(__file__).resolve().parent.parent
    data_dir = Path(data_dir) if data_dir is not None else base / "DukasData"
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    pairs = ("EURUSD", "GBPJPY")

    start = pd.Timestamp(f"{year}-01-01 00:00:00+00:00")
    end = pd.Timestamp(f"{year}-12-31 23:59:00+00:00")

    warmup_start = start - pd.Timedelta(days=warmup_days)

    for pair in pairs:
        path = discover_pair_path(data_dir, pair)
        m1_all = load_dukas_m1(path)
        m1_ctx = m1_all.loc[warmup_start:end]
        m1 = m1_all.loc[start:end]
        df_5m_ctx = add_min_context(resample_5m(m1_ctx), lookback=48).dropna()
        df_5m = df_5m_ctx.loc[start:end]

        pip_value, spread_pips, slip_pips = infer_pair_params(pair)
        cfg = build_default_runner_config(
            ohlc_1m=m1_ctx,
            pip_value=pip_value,
            spread_pips=spread_pips,
            slip_pips=slip_pips,
        )

        artifacts = run_pipeline_for_instrument(cfg, df_5m)

        setups_csv = out / f"{pair}_{year}_setups.csv"
        trades_csv = out / f"{pair}_{year}_trades.csv"
        events_csv = out / f"{pair}_{year}_execution_events.csv"

        pd.DataFrame([asdict(s) for s in artifacts.setups]).to_csv(setups_csv, index=False)
        pd.DataFrame([asdict(t) for t in artifacts.trades]).to_csv(trades_csv, index=False)
        pd.DataFrame(artifacts.execution_events).to_csv(events_csv, index=False)

        print("=" * 72)
        print(f"Pair: {pair}")
        print(f"Year: {year} ({start} -> {end})")
        print(f"Warmup start: {warmup_start}")
        print(f"M1 bars: {len(m1):,}")
        print(f"M1 context bars: {len(m1_ctx):,}")
        print(f"5m bars: {len(df_5m):,}")
        print(f"Breaks: {artifacts.break_series.value_counts().to_dict()}")
        print(f"Bias: {artifacts.bias_series.value_counts().to_dict()}")
        print(f"Setups: {len(artifacts.setups)}")
        print(f"Orders: {len(artifacts.orders)}")
        print(f"Trades (filled): {len(artifacts.trades)}")
        print(f"Execution events: {len(artifacts.execution_events)}")
        print(f"Saved setups: {setups_csv}")
        print(f"Saved trades: {trades_csv}")
        print(f"Saved events: {events_csv}")
        print("=" * 72)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run v2 backtest for full year on Dukas data.")
    parser.add_argument("--year", type=int, default=2025)
    parser.add_argument("--output", default="output_v2")
    parser.add_argument("--warmup-days", type=int, default=21)
    parser.add_argument("--data-dir", default=None, help="Directory containing Dukas parquet files.")
    args = parser.parse_args()
    run_year(args.year, args.output, warmup_days=args.warmup_days, data_dir=args.data_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
