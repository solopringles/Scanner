"""Render annotated charts for all filled trades from monthly v2 outputs."""

from __future__ import annotations

import argparse
import ast
from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
import pandas as pd

from run_dukas_month import add_min_context, load_dukas_m1, resample_5m
from engine import EntryConfig, RiskConfig, run_pipeline_for_instrument


def _parse_tags(s: str) -> dict:
    if not isinstance(s, str) or not s.strip():
        return {}
    try:
        return ast.literal_eval(s)
    except Exception:
        return {}


def _candles(ax, df: pd.DataFrame) -> None:
    for i, (_, r) in enumerate(df.iterrows()):
        color = "#16a34a" if r["close"] >= r["open"] else "#dc2626"
        ax.vlines(i, r["low"], r["high"], color=color, linewidth=1.2)
        body_low = min(r["open"], r["close"])
        body_h = max(abs(r["close"] - r["open"]), 1e-7)
        ax.add_patch(Rectangle((i - 0.28, body_low), 0.56, body_h, facecolor=color, edgecolor=color, alpha=0.85))


def _nearest_ob_zone(artifacts, signal_ts: pd.Timestamp, direction: str):
    obs = [ob for ob in artifacts.order_blocks if ob.timestamp <= signal_ts and ob.direction == direction]
    if not obs:
        return None
    return obs[-1]


def _build_artifacts_for_month(data_path: Path, month: str):
    m1 = load_dukas_m1(data_path)
    m_start = pd.Timestamp(f"{month}-01", tz="UTC")
    m_end = (m_start + pd.offsets.MonthBegin(1)) - pd.Timedelta(minutes=1)
    m1 = m1.loc[m_start:m_end]
    df5 = add_min_context(resample_5m(m1), lookback=48).dropna()
    cfg = {
        "ohlc_1m": m1,
        "assume_htf_poi_tapped": True,
        "carry_bias": True,
        "entry_cfg": EntryConfig(
            fbos_mode="conservative",
            mitigation_mode="conservative",
            min_rr=1.5,
            require_prior_accumulation_for_fbos=False,
        ),
        "risk_cfg": RiskConfig(min_rr_threshold=1.0, fill_timeout_bars=24),
    }
    return m1, df5, run_pipeline_for_instrument(cfg, df5)


def plot_pair_month(pair: str, data_path: Path, out_dir: Path, month: str) -> int:
    setups_csv = out_dir.parent / f"{pair}_{month}_setups.csv"
    trades_csv = out_dir.parent / f"{pair}_{month}_trades.csv"
    if not setups_csv.exists() or not trades_csv.exists():
        print(f"[!] Missing CSVs for {pair}: {setups_csv.name} / {trades_csv.name}")
        return 0

    setups = pd.read_csv(setups_csv)
    trades = pd.read_csv(trades_csv)
    if trades.empty:
        print(f"[!] No filled trades for {pair} {month}")
        return 0

    setups["timestamp"] = pd.to_datetime(setups["timestamp"], utc=True)
    trades["timestamp"] = pd.to_datetime(trades["timestamp"], utc=True)
    trades["fill_timestamp"] = pd.to_datetime(trades["fill_timestamp"], utc=True)
    trades["exit_timestamp"] = pd.to_datetime(trades["exit_timestamp"], utc=True)
    setups["tags_parsed"] = setups["tags"].apply(_parse_tags)

    _, df5, artifacts = _build_artifacts_for_month(data_path, month)

    written = 0
    pair_dir = out_dir / pair
    pair_dir.mkdir(parents=True, exist_ok=True)

    for i, tr in trades.iterrows():
        signal_ts = tr["timestamp"]
        fill_ts = tr["fill_timestamp"]
        exit_ts = tr["exit_timestamp"]
        direction = str(tr["direction"]).lower()

        srow = setups[setups["timestamp"] == signal_ts]
        if srow.empty:
            continue
        s = srow.iloc[0]
        tags = s["tags_parsed"] if isinstance(s["tags_parsed"], dict) else {}
        model = str(s["model"])
        break_type = artifacts.break_series.get(signal_ts, "UNKNOWN")
        dol = tags.get("target_dol_price")

        # window around trade
        try:
            sig_loc = df5.index.get_loc(signal_ts)
        except KeyError:
            continue
        start = max(0, sig_loc - 60)
        end = min(len(df5), sig_loc + 120)
        w = df5.iloc[start:end]
        if w.empty:
            continue

        fig, ax = plt.subplots(figsize=(13, 6))
        _candles(ax, w)

        # map ts to x
        x_map = {ts: idx for idx, ts in enumerate(w.index)}
        xs = x_map.get(signal_ts)
        xf = x_map.get(fill_ts)
        xe = x_map.get(exit_ts)

        if xs is not None:
            ax.axvline(xs, color="#0ea5e9", linestyle="--", linewidth=1.3, label="Signal")
        if xf is not None:
            ax.axvline(xf, color="#22c55e", linestyle="--", linewidth=1.3, label="Fill")
        if xe is not None:
            ax.axvline(xe, color="#f97316", linestyle="--", linewidth=1.3, label="Exit")

        entry = float(tr["entry_price"])
        sl = float(tr["sl_price"])
        tp = float(tr["tp_price"])
        ax.axhline(entry, color="#22c55e", linewidth=1.2, label="Entry (Limit)")
        ax.axhline(sl, color="#ef4444", linewidth=1.2, label="SL")
        ax.axhline(tp, color="#a855f7", linewidth=1.2, label="TP")
        if dol is not None:
            try:
                ax.axhline(float(dol), color="#111827", linestyle=":", linewidth=1.2, label="DOL")
            except Exception:
                pass

        ob = _nearest_ob_zone(artifacts, signal_ts, direction)
        ob_text = "None"
        if ob is not None:
            ob_text = f"{ob.low:.5f} - {ob.high:.5f}"
            ax.axhspan(ob.low, ob.high, color="#facc15", alpha=0.18, label="OB/SMT used")

        title = (
            f"{pair} {signal_ts} | Break={break_type} | EntryModel={model} | Dir={direction.upper()} | Exit={tr['exit_reason']}"
        )
        ax.set_title(title)
        ax.set_xlabel("5m bars")
        ax.set_ylabel("Price")
        ax.grid(alpha=0.2)

        info = (
            f"FBOS/RBOS: {break_type}\n"
            f"Entry Type: {model}\n"
            f"DOL: {dol}\n"
            f"OB/SMT used: {ob_text}\n"
            f"Fill: {fill_ts}\nExit: {exit_ts}\nReason: {tr['exit_reason']}"
        )
        ax.text(0.01, 0.99, info, transform=ax.transAxes, va="top", ha="left", fontsize=9,
                bbox=dict(boxstyle="round", fc="white", ec="#d1d5db", alpha=0.9))

        # legend dedupe
        handles, labels = ax.get_legend_handles_labels()
        seen = set()
        uniq_h, uniq_l = [], []
        for h, l in zip(handles, labels):
            if l not in seen:
                uniq_h.append(h)
                uniq_l.append(l)
                seen.add(l)
        ax.legend(uniq_h, uniq_l, loc="lower right", fontsize=8)

        out = pair_dir / f"{pair}_{signal_ts.strftime('%Y%m%d_%H%M')}_{i+1}.png"
        fig.tight_layout()
        fig.savefig(out, dpi=140)
        plt.close(fig)
        written += 1

    return written


def main() -> int:
    parser = argparse.ArgumentParser(description="Plot all filled trades with FBOS/RBOS + DOL + OB/SMT overlays.")
    parser.add_argument("--month", default="2025-01")
    parser.add_argument("--output-dir", default="output_v2/trade_charts")
    args = parser.parse_args()

    base = Path(__file__).resolve().parent
    out = (base / args.output_dir).resolve()
    data_dir = base.parent / "DukasData"

    total = 0
    total += plot_pair_month("EURUSD", data_dir / "eurusd-m1-bid-2020-01-01-2025-12-31.parquet", out, args.month)
    total += plot_pair_month("GBPJPY", data_dir / "gbpjpy-m1-bid-2020-01-01-2025-12-31.parquet", out, args.month)
    print(f"[OK] Wrote {total} trade charts to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

