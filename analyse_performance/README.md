# v2 Performance Analysis

Quick analysis layer for `v2/output_v2`.

## Run

```powershell
cd v2
python analyse_performance\analyze_performance.py --output-dir output_v2
```

## Outputs

Written to `output_v2/analysis/`:

- `summary.csv`: per-symbol headline stats
- `model_breakdown.csv`: FBOS vs MITIGATION (and other models) stats
- `monthly_breakdown.csv`: monthly trade distribution and R performance

## Notes

- Metrics are computed in `R` using realized `entry/sl/exit`.
- `net_quote_pnl` is quote-currency PnL (`qty * price_move`), so cross-symbol totals should use `R`, not quote PnL.
