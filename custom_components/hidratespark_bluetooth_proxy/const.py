"""Constants for the HidrateSpark integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "hidratespark_bluetooth_proxy"

# Configuration keys
CONF_ADDRESS: Final = "address"
CONF_SIZE_ML: Final = "size_ml"
CONF_NAME_PREFIX: Final = "name_prefix"

DEFAULT_SIZE_ML: Final = 591
DEFAULT_NAME_PREFIX: Final = "h2o"

# Reconnect tuning
RECONNECT_BACKOFF_INITIAL: Final = 1.0
RECONNECT_BACKOFF_MAX: Final = 60.0

# Refill detection tuning
REFILL_SETTLE_TIMEOUT_S: Final = 30.0
REFILL_STABLE_SAMPLES: Final = 3  # consecutive steady samples = "settled"

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

# Safety bound on the inline re-drain: if the bottle keeps re-sending the same
# sip frame (firmware not popping the record), stop acking it after this many
# consecutive identical frames to avoid an unbounded write loop.
MAX_IDENTICAL_SIP_FRAMES: Final = 5

# Sip-frame plausibility guards. On 2026-09-03 a corrupt frame reported pct=100
# and was accepted as a 621 mL drink, poisoning the daily and lifetime totals
# even though no water had left the bottle. See _on_data_notify for the frame
# layout these guards are derived from.
#
# A single sip claiming this much of the bottle's capacity is not a drink, it is
# a bad byte. The largest genuine sip in the logged corpus is 47%.
MAX_SIP_PCT: Final = 100
# Each frame carries the bottle's own raw weight before and after the sip, so a
# frame can be checked against itself. Reject when the claimed volume exceeds
# this multiple of what the frame's own weight delta supports. Across the logged
# corpus every genuine frame sits at <= 0.85x, and the corrupt one at ~26x, so
# there is roughly 3.5x of margin before a real sip is ever at risk.
SIP_WEIGHT_CHECK_HEADROOM: Final = 3.0
# Allowance, as a fraction of bottle capacity, for `current_fill_ml` lagging an
# out-of-band refill before we call a sip "more water than the bottle held".
SIP_FILL_SLACK_FRACTION: Final = 0.1

# Weight encoding.
# The weight characteristic streams a 16-bit big-endian value (high<<8 | low).
# Earlier firmwares were assumed to put an orientation flag in the high byte and
# the weight in the low byte, but on legacy-firmware bottles (e.g. 32oz) the high
# byte rises *with* the weight (observed 0x8Exx at ~75% full, 0x90xx when full),
# so the whole u16 is the reading. We therefore treat the full u16 as the weight
# and detect a trustworthy "upright & settled" reading by stability: N consecutive
# samples within RAW_STABLE_TOLERANCE. Transient frames while the bottle is moved
# never form a streak, so they are filtered without needing a magic byte.
RAW_STABLE_TOLERANCE: Final = 4  # u16 units; settled jitter is ~±1-2
# Raw-units-per-mL scale for converting a weight delta into a volume. Measured
# from a full+empty calibration on a 946 mL bottle: full u16 = 37115, empty
# u16 = 35880, so 1235 raw units span 946 mL = ~1.305 raw/mL. This is a load-cell
# property, so it should hold across bottle sizes on the same puck.
RAW_UNITS_PER_ML: Final = 1.305
# A jump of this many u16 units across a cap open/close means the bottle was
# refilled (~30 mL at the scale above) rather than just opened to drink.
REFILL_MIN_DELTA_RAW: Final = 60

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

# Sip dedup window — wider than the upstream MQTT bridge because BLE relay
# via an ESPHome proxy can add a few seconds of timestamp jitter on replays.
SIP_DEDUP_WINDOW: Final = 50  # check against last N sips
SIP_DEDUP_TIMESTAMP_TOLERANCE_S: Final = 5

# Persistence storage
STORAGE_VERSION: Final = 1
STORAGE_KEY_PREFIX: Final = "hidratespark"
