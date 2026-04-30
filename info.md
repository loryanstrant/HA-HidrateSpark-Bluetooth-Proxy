# HidrateSpark for Home Assistant

A native Home Assistant integration for HidrateSpark smart water bottles.

Connects via Bluetooth — local adapter **or ESPHome Bluetooth proxy** — and
exposes the bottle as a Home Assistant device with sips, totals, current fill,
battery, and refill detection. No MQTT broker, no cloud, no Docker container.

This is a port of [HidrateSpark-MQTT-bridge][bridge] re-architected to use HA's
own Bluetooth stack.

[bridge]: https://github.com/loryanstrant/HidrateSpark-MQTT-bridge

## Features

- 🔋 Battery percentage
- 💧 Per-sip events with timestamps and dedup
- 📊 Daily total (rolls at midnight) and lifetime total
- 🚰 Auto-refill detection (cap-state + weight delta)
- ⚖️ Live fill level from the bottle's weight sensor (auto-calibrated)
- 📡 Works through ESPHome Bluetooth proxies — no Linux box near the bottle
- 🔁 State persists across restarts via HA's Store API
