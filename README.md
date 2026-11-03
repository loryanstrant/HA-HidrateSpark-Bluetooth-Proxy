# HidrateSpark — Home Assistant integration

[![HACS](https://img.shields.io/badge/HACS-Custom-41BDF5?style=flat-square)](https://github.com/hacs/integration)
[![Release](https://img.shields.io/github/v/release/loryanstrant/HA-HidrateSpark-Bluetooth-Proxy?style=flat-square)](https://github.com/loryanstrant/HA-HidrateSpark-Bluetooth-Proxy/releases)
[![Release date](https://img.shields.io/github/release-date/loryanstrant/HA-HidrateSpark-Bluetooth-Proxy?style=flat-square)](https://github.com/loryanstrant/HA-HidrateSpark-Bluetooth-Proxy/releases)
[![Downloads](https://img.shields.io/github/downloads/loryanstrant/HA-HidrateSpark-Bluetooth-Proxy/total?style=flat-square)](https://github.com/loryanstrant/HA-HidrateSpark-Bluetooth-Proxy/releases)
[![License](https://img.shields.io/github/license/loryanstrant/HA-HidrateSpark-Bluetooth-Proxy?style=flat-square)](LICENSE)
[![Last commit](https://img.shields.io/github/last-commit/loryanstrant/HA-HidrateSpark-Bluetooth-Proxy?style=flat-square)](https://github.com/loryanstrant/HA-HidrateSpark-Bluetooth-Proxy/commits)
[![Stars](https://img.shields.io/github/stars/loryanstrant/HA-HidrateSpark-Bluetooth-Proxy?style=flat-square)](https://github.com/loryanstrant/HA-HidrateSpark-Bluetooth-Proxy/stargazers)

[![Open in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=loryanstrant&repository=HA-HidrateSpark-Bluetooth-Proxy&category=integration)

A native Home Assistant integration for HidrateSpark smart water bottles.
Connects via Bluetooth — local adapter **or an ESPHome Bluetooth proxy** — and
exposes the bottle as a Home Assistant device. No MQTT broker, no Docker
container, no Linux box near the bottle.

This is a port of [HidrateSpark-MQTT-bridge][bridge] re-architected onto Home
Assistant's own Bluetooth stack.

[bridge]: https://github.com/loryanstrant/HidrateSpark-MQTT-bridge

## Why this exists

The MQTT bridge needs a Linux host with a working Bluetooth adapter near the
bottle. That's fine for a Raspberry Pi on the kitchen counter, less fine if
your Home Assistant install is in a closet on the other side of the house.

This integration uses HA's `bluetooth` integration for transport, which means
**any ESPHome Bluetooth proxy** in range of the bottle is enough — the proxy
relays GATT operations back to HA, and this integration runs the same
HydroSync handshake and frame parser end-to-end. The same code path also
works with a directly-attached USB or onboard adapter.

## Features

- 🔋 Battery percentage
- 💧 Per-sip events with timestamps and dedup (within ±2 s / same volume)
- 📊 Daily total (rolls at local midnight) and lifetime total
- 🚰 Auto-refill detection (cap-open/close + weight delta ≥ 25 mL), plus
  refills spotted from the sip frames themselves when the cap event was missed
- ⚖️ Live fill level from the on-bottle weight sensor (auto-calibrated, with a
  per-bottle weight scale learned from your sips)
- 🔀 Follows bottles that rotate their Bluetooth address (PRO 2) without
  creating duplicate devices
- 🛟 Sip-exceeds-fill fallback when no weight anchor exists yet
- 🔁 State persists across HA restarts via the Store API
- 📥 Buffered sips replay on reconnect (recorded at arrival time; the observed frame format has no sip timestamp)

## Supported bottles

| Bottle | Status |
|---|---|
| HidrateSpark PRO (v1) 21oz / 621 mL | Tested by the maintainer |
| HidrateSpark Steel / PRO 32oz (legacy firmware) | Tested by contributors |
| HidrateSpark PRO 2 | **◐ Experimental in v0.3.0.** Address-rotation handling, characteristic auto-detection and connection recovery are covered by simulated tests, not verified on PRO 2 hardware. Please report your model, firmware and results |

If your bottle connects but no sips arrive, enable debug logging for
`custom_components.hidratespark_bluetooth_proxy` and attach the `GATT service`
lines and the `bottle reports {...}` line to an issue. They show exactly which
characteristics and firmware your bottle has.

## Requirements

- Home Assistant 2024.12 or later
- The `bluetooth` integration set up (it's the default on HAOS)
- Either a local Bluetooth adapter on the HA host **or** an ESPHome
  Bluetooth proxy within range of the bottle
- The bottle paired once with the official HidrateSpark phone app — this puts
  it into "remembered" mode and registers its MAC

## Install

### HACS (recommended)

1. HACS → ⋮ → **Custom repositories**
2. Add `https://github.com/loryanstrant/HA-HidrateSpark-Bluetooth-Proxy` as an
   **Integration**
3. Install **HidrateSpark Bluetooth Proxy**, restart HA
4. **Settings → Devices & Services → Add Integration → HidrateSpark Bluetooth Proxy**

### Manual

Copy `custom_components/hidratespark_bluetooth_proxy/` into your HA `config/custom_components/`
folder, restart HA, then add the integration from **Devices & Services**.

## Setup

If your bottle is already advertising in range of HA (or a proxy), it appears
as a Bluetooth discovery and you only need to confirm the capacity. Otherwise
**+ Add Integration → HidrateSpark** lets you pick the bottle from a list of
discovered devices, or type its MAC address directly.

You can change the bottle's capacity later via the integration's **Configure**
button.

## Coexisting with the phone app

The bottle accepts only one BLE central at a time. If the official app is
actively connected, this integration won't be able to read the bottle until
the app disconnects. Recommended setups:

- **Best:** uninstall the phone app once you're set up
- **Acceptable:** force-quit it, or revoke its Bluetooth permission, when
  you're at home
- **Occasional firmware updates:** open the phone app briefly, then close it

## Entities

| Entity | Type |
|---|---|
| Battery | sensor (%, `battery`) |
| Water today | sensor (mL, `total_increasing`) |
| Water lifetime | sensor (mL, `total_increasing`) |
| Current fill | sensor (mL) |
| Current fill percent | sensor (%) |
| Last sip volume | sensor (mL) |
| Last sip time | sensor (timestamp) |
| Bottle weight (raw) | diagnostic, disabled by default |
| Connected | binary_sensor (connectivity) |

## What survives a disconnect

| Data | Buffered on bottle? | Recovered on reconnect? |
|---|---|---|
| Sips (volume) | ● Yes | ● Replayed; timestamped when received, not at the original drink time |
| Daily / lifetime totals | derived | ✅ Recomputed from replayed sips |
| Battery | ✅ | ✅ Re-read on connect |
| Current fill | ✅ | ✅ Snaps to true value within ~2 s |
| Cap open/close events | ❌ notify-only | ❌ Lost while away |
| Refill events while away | ◐ Weight changes in sip records on supported firmware | ◐ Inferred between consecutive usable sip records; refills without such records are not recovered |

## Acknowledgements

- Big thanks to **[kbaker827](https://github.com/kbaker827)** for
  [pull request #12](https://github.com/loryanstrant/HA-HidrateSpark-Bluetooth-Proxy/pull/12):
  experimental PRO 2 support, address-rotation recovery, options-flow fixes,
  sip-frame refill detection and the accompanying regression tests. The original
  contribution and commit authorship are preserved in the canonical project.
- BLE handshake sequence and frame layout originally from
  [HydroSync](https://github.com/maxperron/HydroSync) (GPL-3.0).
- Cap-state and weight-sensor characteristics reverse-engineered against
  HidrateSpark Steel/PRO firmware 80.18 on the nRF52832, documented in the
  upstream MQTT bridge.

## License

MIT
