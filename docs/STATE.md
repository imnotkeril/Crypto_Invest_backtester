# STATE

- 2026-10-24 audit done, report in docs/AUDIT_2026-10-24.md
- 2026-10-24 fix: requirements.txt, README data sources
- 2026-10-24 fix: DataUnavailableError + exit codes, safe_filename, SL quantity; dead code removed; 20 tests pass (commit on chore/audit)
- 2026-10-24 .venv recreated with --system-site-packages (old one pointed to missing Python 3.13; pip to PyPI fails with self-signed cert in chain, TLS not bypassed)
- 2026-10-24 LICENSE MIT (holder imnotkeril, owner may change), requirements-dev.txt, README tests/exit codes

## Needed from owner
- Confirm MIT license / holder name.
- Decide metric: "Realized profit" currently = all sells - all buys (counts open position as loss). Mark-to-market vs closed-only?
- Fix PyPI TLS (corporate proxy cert) to allow lock file + pip-audit.
- Push / PR of `chore/audit`.
