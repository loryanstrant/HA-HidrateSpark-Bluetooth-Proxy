# Copilot instructions — HA-HidrateSpark-Bluetooth-Proxy

> Canonical standards live in the `dev-standards` repo on SOUNDWAVE/Gitea.
> Read by Copilot chat **and** inline suggestions. For full HA build conventions,
> see the `build-ha-component` skill in dev-standards.

## What this repo is

A **Home Assistant custom component** — a Bluetooth (BLE) proxy/integration for
HidrateSpark smart water bottles. Domain: `hidratespark_bluetooth_proxy`.
Coordinator-based, exposes sensor + binary_sensor entities.

## Repo shape

- `custom_components/hidratespark_bluetooth_proxy/` — `manifest.json`,
  `__init__.py`, `config_flow.py`, `const.py`, `coordinator.py`, `entity.py`,
  `ble.py`, `state.py`, `sensor.py`, `binary_sensor.py`, `strings.json`,
  `translations/`, `brand/icon.png`.
- `hacs.json`, `info.md`, `.github/workflows/` (validate + release).

## Conventions

- Bump `manifest.json` **version** every release (semver); `domain` matches the
  folder name. CI cuts the release.
- BLE deps belong in `manifest.json` `requirements`; declare `bluetooth` usage
  per HA's Bluetooth integration rules.
- Test: standalone `for f in tests/test_*.py; do python3 "$f" || exit 1; done`,
  then `pip install -r requirements-test.txt && python3 -m pytest -q tests/ha`
  (real HA contracts, isolated from global stubs), plus hassfest and HACS validation.
- Deploy/test via the published release artifact into TEST1/TEST2, not host
  file-copy. Backup + auto-rollback.

## Never

- Don't commit HA long-lived tokens or deploy keys — Gitea Actions secrets only.
