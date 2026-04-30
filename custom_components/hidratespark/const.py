"""Constants for the HidrateSpark integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "hidratespark"

# Configuration keys
CONF_ADDRESS: Final = "address"
CONF_SIZE_ML: Final = "size_ml"
CONF_NAME_PREFIX: Final = "name_prefix"

DEFAULT_SIZE_ML: Final = 591
DEFAULT_NAME_PREFIX: Final = "h2o"

# Reconnect tuning
RECONNECT_BACKOFF_INITIAL: Final = 1.0
RECONNECT_BACKOFF_MAX: Final = 60.0

# Refill detection tuning (matches upstream bridge)
REFILL_SETTLE_TIMEOUT_S: Final = 30.0
REFILL_STABLE_SAMPLES: Final = 3
REFILL_STABLE_TOLERANCE: Final = 2  # raw weight units
REFILL_MIN_DELTA: Final = 25  # raw units (~mL)

# BLE: services
SERVICE_USER: Final = "bf2d1ba0-c473-49f2-9571-0ce69036c642"
SERVICE_REF: Final = "45855422-6565-4cd7-a2a9-fe8af41b85e8"

# BLE: characteristics — modern (HydroSync) path
CHAR_USER_DATA: Final = "bf2d1ba1-c473-49f2-9571-0ce69036c642"
CHAR_SET_POINT: Final = "b44b03f0-b850-4090-86eb-72863fb3618d"
CHAR_DEBUG: Final = "e3578b0d-caa7-46d6-b7c2-7331c08de044"

# BLE: characteristics — legacy path
CHAR_DATA_POINT: Final = "016e11b1-6c8a-4074-9e5a-076053f93784"

# BLE: standard battery
CHAR_BATTERY_LEVEL: Final = "00002a19-0000-1000-8000-00805f9b34fb"

# BLE: discovered on firmware 80.18 (nRF52832)
CHAR_WEIGHT: Final = "1807a063-4e2d-4636-981a-35e93d1c7b94"
# Cap-state notifications share UUID with the DEBUG handshake characteristic
CHAR_CAP: Final = CHAR_DEBUG

# Drain command — single byte written to the data char to ack a sip record
DRAIN_BYTE: Final = bytes([0x57])

# Weight high-byte meaning
WEIGHT_HIGH_STABLE: Final = 0x8A  # bottle upright & settled — only reading we trust
WEIGHT_HIGH_TILTED: Final = 0x84
WEIGHT_HIGH_TRANSIENT: Final = 0x88

# 13-step handshake from HydroSync. Each tuple is (target_char, hex_payload).
# Writes are 50 ms apart.
HANDSHAKE_COMMANDS: Final[list[tuple[str, str]]] = [
    ("DEBUG", "2100d1"),
    ("SET_POINT", "92"),
    ("DEBUG", "2200f7"),
    ("SET_POINT", "7700000032d70000"),
    ("SET_POINT", "00341b00e0790000"),
    ("SET_POINT", "02345200c0a80000"),
    ("SET_POINT", "03346e0030c00000"),
    ("SET_POINT", "04348900a0d70000"),
    ("SET_POINT", "0534a50010ef0000"),
    ("SET_POINT", "0634c00080060100"),
    ("SET_POINT", "0734dc00f01d0100"),
    ("SET_POINT", "0834000000000000"),
    ("SET_POINT", "0934000000000000"),
]
HANDSHAKE_INTERVAL_S: Final = 0.05

# Sip dedup window
SIP_DEDUP_WINDOW: Final = 10  # check against last N sips
SIP_DEDUP_TIMESTAMP_TOLERANCE_S: Final = 2

# Persistence storage
STORAGE_VERSION: Final = 1
STORAGE_KEY_PREFIX: Final = "hidratespark"
