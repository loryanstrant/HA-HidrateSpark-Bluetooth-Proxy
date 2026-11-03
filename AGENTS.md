# Agent instructions

<!-- navgen:start-here:begin -->
## Start here (generated)

1. Read [NAVIGATION.md](NAVIGATION.md) - the repository map: docs, headings,
   entry points, public symbols, operations.
2. Use [`.agent/navigation.json`](.agent/navigation.json) when structured paths
   are easier to consume than prose.
3. Use the Gitea MCP server for live state: issues, pull requests, CI results.

Do not hand-edit `NAVIGATION.md` or `.agent/navigation.json`; they are generated
by navgen (`tools/navgen/navgen repo` in dev-standards) and refreshed nightly.
<!-- navgen:start-here:end -->

## Validation

<!-- Replace these with the real commands for this repository. -->

- Build: none (pure Python custom component)
- Test: `for f in tests/test_*.py; do python3 "$f" || exit 1; done` (no dependencies; HA, bleak and voluptuous are stubbed in `tests/ha_stub.py` and `tests/ha_full_stub.py`)
- HA contracts: `pip install -r requirements-test.txt && python3 -m pytest -q tests/ha` (real Home Assistant, separate from standalone stubs).
- Lint: `python3 -m py_compile custom_components/hidratespark_bluetooth_proxy/*.py`; CI also runs hassfest and HACS validation
