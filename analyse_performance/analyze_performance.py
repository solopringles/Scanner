from __future__ import annotations

import argparse
import math
import ast
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
from pandas.errors import EmptyDataError


@dataclass(slots=True)
class SymbolResult:
    symbol: str
    year: str
    trades: pd.DataFrame
    setups: pd.DataFrame
    events: pd.DataFrame


def _safe_div(a: float, b: float) -> float:
    if b == 0:
        return 0.0
    return a / b


def _max_drawdown_from_series(equity_curve: pd.Series) -> float:
    if equity_curve.empty:
        return 0.0
    peaks = equity_curve.cummax()
    dd = equity_curve - peaks
    return float(dd.min())


def _load_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path)
    except EmptyDataError:
        return pd.DataFrame()


def _normalize_model(value: str) -> str:
    if not isinstance(value, str) or not value:
        return "UNKNOWN"
    if "." in value:
        return value.split(".")[-1]
    return value


def _parse_trade_set(files: list[Path], output_dir: Path) -> list[SymbolResult]:
    results: list[SymbolResult] = []
    for trade_file in files:
        stem = trade_file.stem
        # Expected: SYMBOL_YYYY_trades
        parts = stem.split("_")
        if len(parts) < 3:
            continue
        symbol = parts[0]
        year = parts[1]
        setups_file = output_dir / f"{symbol}_{year}_setups.csv"
        events_file = output_dir / f"{symbol}_{year}_execution_events.csv"
        results.append(
            SymbolResult(
                symbol=symbol,
                year=year,
                trades=_load_csv(trade_file),
                setups=_load_csv(setups_file),
                events=_load_csv(events_file),
            )
        )
    return results


def _prepare_trades(trades: pd.DataFrame) -> pd.DataFrame:
    if trades.empty:
        return trades
    df = trades.copy()
    for col in ["timestamp", "fill_timestamp", "exit_timestamp"]:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], utc=True, errors="coerce")
    num_cols = ["entry_price", "sl_price", "tp_price", "qty", "exit_price", "commission_quote"]
    for col in num_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df["direction"] = df["direction"].astype(str).str.lower()
    long_mask = df["direction"].eq("long")
    risk = (df["entry_price"] - df["sl_price"]).abs()
    pnl_per_unit = np.where(long_mask, df["exit_price"] - df["entry_price"], df["entry_price"] - df["exit_price"])
    gross_pnl_quote = pnl_per_unit * df["qty"]
    commission = df["commission_quote"].fillna(0.0) if "commission_quote" in df.columns else 0.0
    net_pnl_quote = gross_pnl_quote - commission
    df["risk_per_unit"] = risk
    df["pnl_per_unit"] = pnl_per_unit
    df["r_multiple"] = np.where((risk > 0) & (df["qty"] > 0), net_pnl_quote / (risk * df["qty"]), np.nan)
    df["pnl_quote"] = net_pnl_quote
    df["is_win"] = df["r_multiple"] > 0
    df["is_loss"] = df["r_multiple"] < 0
    df["entry_key"] = df["entry_price"].round(6)
    return df


def _prepare_setups(setups: pd.DataFrame) -> pd.DataFrame:
    if setups.empty:
        return setups
    df = setups.copy()
    if "timestamp" in df.columns:
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
    if "entry_price" in df.columns:
        df["entry_price"] = pd.to_numeric(df["entry_price"], errors="coerce")
    if "direction" in df.columns:
        df["direction"] = df["direction"].astype(str).str.lower()
    if "model" in df.columns:
        df["model"] = df["model"].map(_normalize_model)
    if "tags" in df.columns:
        parsed = df["tags"].apply(lambda v: ast.literal_eval(v) if isinstance(v, str) and v.strip() else {})
        for col in [
            "session",
            "trigger",
            "rbos_logged",
            "rbos_confirmation_seen",
            "target_dol_price",
            "bias",
            "bias_source",
            "trade_aligned_with_bias",
            "trade_aligned_with_rbos_bias",
        ]:
            df[col] = parsed.apply(lambda d, key=col: d.get(key))
    df["entry_key"] = df["entry_price"].round(6)
    return df


def _merge_models(trades: pd.DataFrame, setups: pd.DataFrame) -> pd.DataFrame:
    if trades.empty:
        return trades
    if setups.empty:
        trades = trades.copy()
        trades["model"] = "UNKNOWN"
        return trades
    keep_cols = ["timestamp", "direction", "entry_key", "model"]
    for extra in [
        "session",
        "trigger",
        "rbos_logged",
        "rbos_confirmation_seen",
        "target_dol_price",
        "bias",
        "bias_source",
        "trade_aligned_with_bias",
        "trade_aligned_with_rbos_bias",
    ]:
        if extra in setups.columns:
            keep_cols.append(extra)
    merged = trades.merge(
        setups[keep_cols],
        left_on=["timestamp", "direction", "entry_key"],
        right_on=["timestamp", "direction", "entry_key"],
        how="left",
    )
    merged["model"] = merged["model"].fillna("UNKNOWN")
    return merged


def _build_symbol_summary(symbol: str, year: str, trades: pd.DataFrame, events: pd.DataFrame) -> dict[str, float | str]:
    if trades.empty:
        return {
            "symbol": symbol,
            "year": year,
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate_pct": 0.0,
            "net_r": 0.0,
            "avg_r": 0.0,
            "profit_factor_r": 0.0,
            "max_drawdown_r": 0.0,
            "net_quote_pnl": 0.0,
            "missed_limit_fills": int((events["type"] == "missed_limit_fill").sum()) if not events.empty else 0,
        }

    valid_r = trades["r_multiple"].replace([np.inf, -np.inf], np.nan).dropna()
    wins = int((valid_r > 0).sum())
    losses = int((valid_r < 0).sum())
    gross_win_r = float(valid_r[valid_r > 0].sum())
    gross_loss_r = float(valid_r[valid_r < 0].sum())
    net_r = float(valid_r.sum())
    equity_r = valid_r.cumsum()
    mdd_r = _max_drawdown_from_series(equity_r)

    return {
        "symbol": symbol,
        "year": year,
        "trades": int(len(trades)),
        "wins": wins,
        "losses": losses,
        "win_rate_pct": round(_safe_div(wins, max(len(valid_r), 1)) * 100.0, 2),
        "net_r": round(net_r, 3),
        "avg_r": round(float(valid_r.mean()) if len(valid_r) else 0.0, 4),
        "profit_factor_r": round(_safe_div(gross_win_r, abs(gross_loss_r)) if gross_loss_r < 0 else math.inf, 3)
        if gross_loss_r != 0
        else 0.0,
        "max_drawdown_r": round(mdd_r, 3),
        "net_quote_pnl": round(float(trades["pnl_quote"].sum()), 2),
        "missed_limit_fills": int((events["type"] == "missed_limit_fill").sum()) if not events.empty else 0,
    }


def _build_model_breakdown(trades: pd.DataFrame, symbol: str, year: str) -> pd.DataFrame:
    if trades.empty:
        return pd.DataFrame(columns=["symbol", "year", "model", "trades", "win_rate_pct", "net_r", "avg_r"])
    grouped = trades.groupby("model", dropna=False)["r_multiple"].agg(["count", "mean", "sum"])
    wins = trades.assign(win=(trades["r_multiple"] > 0).astype(int)).groupby("model")["win"].sum()
    out = grouped.rename(columns={"count": "trades", "mean": "avg_r", "sum": "net_r"}).reset_index()
    out["wins"] = out["model"].map(wins).fillna(0).astype(int)
    out["win_rate_pct"] = np.where(out["trades"] > 0, out["wins"] / out["trades"] * 100.0, 0.0)
    out.insert(0, "symbol", symbol)
    out.insert(1, "year", year)
    return out[["symbol", "year", "model", "trades", "wins", "win_rate_pct", "net_r", "avg_r"]]


def _build_session_breakdown(trades: pd.DataFrame, symbol: str, year: str) -> pd.DataFrame:
    if trades.empty or "session" not in trades.columns:
        return pd.DataFrame(columns=["symbol", "year", "session", "trades", "wins", "win_rate_pct", "net_r", "avg_r"])
    grouped = trades.groupby("session", dropna=False)["r_multiple"].agg(["count", "mean", "sum"])
    wins = trades.assign(win=(trades["r_multiple"] > 0).astype(int)).groupby("session")["win"].sum()
    out = grouped.rename(columns={"count": "trades", "mean": "avg_r", "sum": "net_r"}).reset_index()
    out["wins"] = out["session"].map(wins).fillna(0).astype(int)
    out["win_rate_pct"] = np.where(out["trades"] > 0, out["wins"] / out["trades"] * 100.0, 0.0)
    out.insert(0, "symbol", symbol)
    out.insert(1, "year", year)
    return out[["symbol", "year", "session", "trades", "wins", "win_rate_pct", "net_r", "avg_r"]]


def _build_model_session_breakdown(trades: pd.DataFrame, symbol: str, year: str) -> pd.DataFrame:
    if trades.empty or "session" not in trades.columns:
        return pd.DataFrame(
            columns=["symbol", "year", "model", "session", "trades", "wins", "win_rate_pct", "net_r", "avg_r"]
        )
    grouped = trades.groupby(["model", "session"], dropna=False)["r_multiple"].agg(["count", "mean", "sum"])
    out = grouped.rename(columns={"count": "trades", "mean": "avg_r", "sum": "net_r"}).reset_index()
    wins = trades.assign(win=(trades["r_multiple"] > 0).astype(int)).groupby(["model", "session"])["win"].sum()
    out["wins"] = out.set_index(["model", "session"]).index.map(wins).fillna(0).astype(int).to_numpy()
    out["win_rate_pct"] = np.where(out["trades"] > 0, out["wins"] / out["trades"] * 100.0, 0.0)
    out.insert(0, "symbol", symbol)
    out.insert(1, "year", year)
    return out[["symbol", "year", "model", "session", "trades", "wins", "win_rate_pct", "net_r", "avg_r"]]


def _build_trigger_breakdown(trades: pd.DataFrame, symbol: str, year: str) -> pd.DataFrame:
    if trades.empty or "trigger" not in trades.columns:
        return pd.DataFrame(columns=["symbol", "year", "trigger", "trades", "wins", "win_rate_pct", "net_r", "avg_r"])
    grouped = trades.groupby("trigger", dropna=False)["r_multiple"].agg(["count", "mean", "sum"])
    wins = trades.assign(win=(trades["r_multiple"] > 0).astype(int)).groupby("trigger")["win"].sum()
    out = grouped.rename(columns={"count": "trades", "mean": "avg_r", "sum": "net_r"}).reset_index()
    out["wins"] = out["trigger"].map(wins).fillna(0).astype(int)
    out["win_rate_pct"] = np.where(out["trades"] > 0, out["wins"] / out["trades"] * 100.0, 0.0)
    out.insert(0, "symbol", symbol)
    out.insert(1, "year", year)
    return out[["symbol", "year", "trigger", "trades", "wins", "win_rate_pct", "net_r", "avg_r"]]


def _build_bias_source_breakdown(trades: pd.DataFrame, symbol: str, year: str) -> pd.DataFrame:
    if trades.empty or "bias_source" not in trades.columns:
        return pd.DataFrame(columns=["symbol", "year", "bias_source", "trades", "wins", "win_rate_pct", "net_r", "avg_r"])
    grouped = trades.groupby("bias_source", dropna=False)["r_multiple"].agg(["count", "mean", "sum"])
    wins = trades.assign(win=(trades["r_multiple"] > 0).astype(int)).groupby("bias_source")["win"].sum()
    out = grouped.rename(columns={"count": "trades", "mean": "avg_r", "sum": "net_r"}).reset_index()
    out["wins"] = out["bias_source"].map(wins).fillna(0).astype(int)
    out["win_rate_pct"] = np.where(out["trades"] > 0, out["wins"] / out["trades"] * 100.0, 0.0)
    out.insert(0, "symbol", symbol)
    out.insert(1, "year", year)
    return out[["symbol", "year", "bias_source", "trades", "wins", "win_rate_pct", "net_r", "avg_r"]]


def _build_alignment_breakdown(trades: pd.DataFrame, symbol: str, year: str) -> pd.DataFrame:
    if trades.empty or "trade_aligned_with_rbos_bias" not in trades.columns:
        return pd.DataFrame(
            columns=["symbol", "year", "aligned_with_rbos_bias", "trades", "wins", "win_rate_pct", "net_r", "avg_r"]
        )
    df = trades.copy()
    df["aligned_with_rbos_bias"] = df["trade_aligned_with_rbos_bias"].fillna(False).astype(bool)
    grouped = df.groupby("aligned_with_rbos_bias", dropna=False)["r_multiple"].agg(["count", "mean", "sum"])
    wins = df.assign(win=(df["r_multiple"] > 0).astype(int)).groupby("aligned_with_rbos_bias")["win"].sum()
    out = grouped.rename(columns={"count": "trades", "mean": "avg_r", "sum": "net_r"}).reset_index()
    out["wins"] = out["aligned_with_rbos_bias"].map(wins).fillna(0).astype(int)
    out["win_rate_pct"] = np.where(out["trades"] > 0, out["wins"] / out["trades"] * 100.0, 0.0)
    out.insert(0, "symbol", symbol)
    out.insert(1, "year", year)
    return out[["symbol", "year", "aligned_with_rbos_bias", "trades", "wins", "win_rate_pct", "net_r", "avg_r"]]


def _build_exit_reason_breakdown(trades: pd.DataFrame, symbol: str, year: str) -> pd.DataFrame:
    if trades.empty or "exit_reason" not in trades.columns:
        return pd.DataFrame(columns=["symbol", "year", "exit_reason", "trades", "wins", "win_rate_pct", "net_r", "avg_r"])
    grouped = trades.groupby("exit_reason", dropna=False)["r_multiple"].agg(["count", "mean", "sum"])
    wins = trades.assign(win=(trades["r_multiple"] > 0).astype(int)).groupby("exit_reason")["win"].sum()
    out = grouped.rename(columns={"count": "trades", "mean": "avg_r", "sum": "net_r"}).reset_index()
    out["wins"] = out["exit_reason"].map(wins).fillna(0).astype(int)
    out["win_rate_pct"] = np.where(out["trades"] > 0, out["wins"] / out["trades"] * 100.0, 0.0)
    out.insert(0, "symbol", symbol)
    out.insert(1, "year", year)
    return out[["symbol", "year", "exit_reason", "trades", "wins", "win_rate_pct", "net_r", "avg_r"]]


def _build_monthly_breakdown(trades: pd.DataFrame, symbol: str, year: str) -> pd.DataFrame:
    if trades.empty:
        return pd.DataFrame(columns=["symbol", "year", "month", "trades", "net_r", "win_rate_pct"])
    df = trades.copy()
    ts_col = "fill_timestamp" if "fill_timestamp" in df.columns else "timestamp"
    # Drop timezone before period conversion to avoid pandas warnings.
    df["month"] = df[ts_col].dt.tz_convert(None).dt.to_period("M").astype(str)
    grouped = df.groupby("month")["r_multiple"].agg(["count", "sum", "mean"]).rename(
        columns={"count": "trades", "sum": "net_r", "mean": "avg_r"}
    )
    wins = df.assign(win=(df["r_multiple"] > 0).astype(int)).groupby("month")["win"].sum()
    out = grouped.reset_index()
    out["wins"] = out["month"].map(wins).fillna(0).astype(int)
    out["win_rate_pct"] = np.where(out["trades"] > 0, out["wins"] / out["trades"] * 100.0, 0.0)
    out.insert(0, "symbol", symbol)
    out.insert(1, "year", year)
    return out[["symbol", "year", "month", "trades", "wins", "win_rate_pct", "net_r", "avg_r"]]


def _build_portfolio_breakdown(all_trades: pd.DataFrame) -> dict[str, float]:
    if all_trades.empty:
        return {"portfolio_net_r": 0.0, "portfolio_win_rate_pct": 0.0, "portfolio_max_drawdown_r": 0.0, "daily_corr": 0.0}

    valid = all_trades["r_multiple"].replace([np.inf, -np.inf], np.nan).dropna()
    wins = float((valid > 0).sum())
    win_rate = _safe_div(wins, len(valid)) * 100.0 if len(valid) else 0.0
    mdd = _max_drawdown_from_series(valid.cumsum())

    daily = all_trades.copy()
    ts_col = "fill_timestamp" if "fill_timestamp" in daily.columns else "timestamp"
    daily["date"] = daily[ts_col].dt.date
    pivot = daily.pivot_table(index="date", columns="symbol", values="r_multiple", aggfunc="sum", fill_value=0.0)
    corr = float(pivot.corr().iloc[0, 1]) if pivot.shape[1] >= 2 else 0.0

    return {
        "portfolio_net_r": round(float(valid.sum()), 3),
        "portfolio_win_rate_pct": round(win_rate, 2),
        "portfolio_max_drawdown_r": round(mdd, 3),
        "daily_corr": round(corr, 4) if not np.isnan(corr) else 0.0,
    }


def run_analysis(output_dir: Path) -> None:
    trade_files = sorted(output_dir.glob("*_trades.csv"))
    if not trade_files:
        print(f"No trade files found in {output_dir}")
        return

    symbols = _parse_trade_set(trade_files, output_dir)
    summary_rows: list[dict[str, float | str]] = []
    model_rows: list[pd.DataFrame] = []
    session_rows: list[pd.DataFrame] = []
    model_session_rows: list[pd.DataFrame] = []
    trigger_rows: list[pd.DataFrame] = []
    bias_source_rows: list[pd.DataFrame] = []
    alignment_rows: list[pd.DataFrame] = []
    exit_reason_rows: list[pd.DataFrame] = []
    monthly_rows: list[pd.DataFrame] = []
    all_trade_frames: list[pd.DataFrame] = []

    for item in symbols:
        trades = _prepare_trades(item.trades)
        setups = _prepare_setups(item.setups)
        merged = _merge_models(trades, setups)
        merged["symbol"] = item.symbol
        merged["year"] = item.year

        summary_rows.append(_build_symbol_summary(item.symbol, item.year, merged, item.events))
        model_rows.append(_build_model_breakdown(merged, item.symbol, item.year))
        session_rows.append(_build_session_breakdown(merged, item.symbol, item.year))
        model_session_rows.append(_build_model_session_breakdown(merged, item.symbol, item.year))
        trigger_rows.append(_build_trigger_breakdown(merged, item.symbol, item.year))
        bias_source_rows.append(_build_bias_source_breakdown(merged, item.symbol, item.year))
        alignment_rows.append(_build_alignment_breakdown(merged, item.symbol, item.year))
        exit_reason_rows.append(_build_exit_reason_breakdown(merged, item.symbol, item.year))
        monthly_rows.append(_build_monthly_breakdown(merged, item.symbol, item.year))
        all_trade_frames.append(merged)

    summary_df = pd.DataFrame(summary_rows)
    model_df = pd.concat(model_rows, ignore_index=True) if model_rows else pd.DataFrame()
    session_df = pd.concat(session_rows, ignore_index=True) if session_rows else pd.DataFrame()
    model_session_df = pd.concat(model_session_rows, ignore_index=True) if model_session_rows else pd.DataFrame()
    trigger_df = pd.concat(trigger_rows, ignore_index=True) if trigger_rows else pd.DataFrame()
    bias_source_df = pd.concat(bias_source_rows, ignore_index=True) if bias_source_rows else pd.DataFrame()
    alignment_df = pd.concat(alignment_rows, ignore_index=True) if alignment_rows else pd.DataFrame()
    exit_reason_df = pd.concat(exit_reason_rows, ignore_index=True) if exit_reason_rows else pd.DataFrame()
    monthly_df = pd.concat(monthly_rows, ignore_index=True) if monthly_rows else pd.DataFrame()
    all_trades = pd.concat(all_trade_frames, ignore_index=True) if all_trade_frames else pd.DataFrame()
    portfolio_stats = _build_portfolio_breakdown(all_trades)

    analysis_dir = output_dir / "analysis"
    analysis_dir.mkdir(parents=True, exist_ok=True)
    summary_df.to_csv(analysis_dir / "summary.csv", index=False)
    model_df.to_csv(analysis_dir / "model_breakdown.csv", index=False)
    session_df.to_csv(analysis_dir / "session_breakdown.csv", index=False)
    model_session_df.to_csv(analysis_dir / "model_session_breakdown.csv", index=False)
    trigger_df.to_csv(analysis_dir / "trigger_breakdown.csv", index=False)
    bias_source_df.to_csv(analysis_dir / "bias_source_breakdown.csv", index=False)
    alignment_df.to_csv(analysis_dir / "rbos_alignment_breakdown.csv", index=False)
    exit_reason_df.to_csv(analysis_dir / "exit_reason_breakdown.csv", index=False)
    monthly_df.to_csv(analysis_dir / "monthly_breakdown.csv", index=False)

    print("\n=== V2 PERFORMANCE ANALYSIS ===")
    print(summary_df.to_string(index=False))
    print("\n=== MODEL BREAKDOWN ===")
    if model_df.empty:
        print("No model data.")
    else:
        print(model_df.sort_values(["symbol", "model"]).to_string(index=False))
    print("\n=== SESSION BREAKDOWN ===")
    if session_df.empty:
        print("No session data.")
    else:
        print(session_df.sort_values(["symbol", "session"]).to_string(index=False))
    print("\n=== MODEL x SESSION BREAKDOWN ===")
    if model_session_df.empty:
        print("No model/session data.")
    else:
        print(model_session_df.sort_values(["symbol", "model", "session"]).to_string(index=False))
    print("\n=== TRIGGER BREAKDOWN ===")
    if trigger_df.empty:
        print("No trigger data.")
    else:
        print(trigger_df.sort_values(["symbol", "trigger"]).to_string(index=False))
    print("\n=== BIAS SOURCE BREAKDOWN ===")
    if bias_source_df.empty:
        print("No bias source data.")
    else:
        print(bias_source_df.sort_values(["symbol", "bias_source"]).to_string(index=False))
    print("\n=== RBOS ALIGNMENT BREAKDOWN ===")
    if alignment_df.empty:
        print("No RBOS alignment data.")
    else:
        print(alignment_df.sort_values(["symbol", "aligned_with_rbos_bias"]).to_string(index=False))
    print("\n=== EXIT REASON BREAKDOWN ===")
    if exit_reason_df.empty:
        print("No exit reason data.")
    else:
        print(exit_reason_df.sort_values(["symbol", "exit_reason"]).to_string(index=False))
    print("\n=== PORTFOLIO ===")
    for k, v in portfolio_stats.items():
        print(f"{k}: {v}")
    print(f"\nSaved CSV outputs to: {analysis_dir}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze v2 backtest outputs.")
    parser.add_argument("--output-dir", type=Path, default=Path("output_v2"), help="Folder containing *_trades.csv files.")
    args = parser.parse_args()
    run_analysis(args.output_dir)


if __name__ == "__main__":
    main()
