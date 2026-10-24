# Plan (from audit 2026-10-24)

- [x] Installable requirements.txt
- [x] README matches code (Binance only)
- [x] Tests for zones / buys / TP / SL (20 tests, offline)
- [x] Fail non-zero on empty data / network failure
- [x] Sanitize CSV filename
- [x] Remove dead CoinGecko/demo code
- [x] Recreate broken .venv
- [x] LICENSE (MIT) + README Python version
- [x] Fix stop-loss leaving negative remaining quantity
- [ ] Rename misleading "Realized profit"/ROI (sells minus all buys includes open position) - needs owner decision on metric
- [ ] Hash-pinned lock file (blocked: pip SSL error to PyPI in this environment)
- [ ] pip-audit CVE scan (same SSL block)
