# V2 Simulation Suite

Run all scenarios:

```powershell
python simulation_suite.py
```

Run in groups of 5:

```powershell
python simulation_suite.py --group 1
python simulation_suite.py --group 2
python simulation_suite.py --group 3
```

Notes:
- This suite is synthetic and deterministic.
- It validates logic contracts from `v2_strategy_dictionary_pseudocode.md`.
- It is designed for alignment before implementing full production detectors.
