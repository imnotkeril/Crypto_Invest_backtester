# Crypto_Invest_backtester

Single-file CLI backtester (`crypto_backtester.py`): zone accumulation, percent/ATH/trailing take-profits, breakeven/trailing stop. Data: Binance public klines (no key).

- Run: `python crypto_backtester.py --config config.json` (see `config.example.json`)
- Test: `.venv/Scripts/python.exe -m pytest tests` ; lint: `ruff check .` ; types: `mypy crypto_backtester.py --ignore-missing-imports`
- Never place real orders; tests must not touch the network.
- State: `docs/STATE.md`, plan: `docs/plan.md`, audit: `docs/AUDIT_2026-10-24.md`.
