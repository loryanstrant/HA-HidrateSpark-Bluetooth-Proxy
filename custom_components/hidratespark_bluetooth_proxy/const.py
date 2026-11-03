"""Constants for the HidrateSpark integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "hidratespark_bluetooth_proxy"

# Configuration keys
CONF_ADDRESS: Final = "address"
CONF_SIZE_ML: Final = "size_ml"
CONF_NAME_PREFIX: Final = "name_prefix"
# Stable key the device and its entities are registered under. It is the address
# the bottle had when it was first added and never changes afterwards, so a
# bottle that rotates its BLE address (issue #10, seen on the PRO 2) keeps the
# same device and entity IDs.
CONF_DEVICE_KEY: Final = "device_key"
# The bottle's advertised name (e.g. h2o00003158), kept in entry data so it
# survives the user renaming the entry. Used to follow a rotating address.
CONF_LOCAL_NAME: Final = "local_name"

DEFAULT_SIZE_ML: Final = 591
DEFAULT_NAME_PREFIX: Final = "h2o"

# Keepalive while connected. Some bottles (the PRO 2 in particular) quietly drop
# an idle link or stop notifying while the GATT session still looks open, which
# used to leave the integration "connected" to a bottle that sent nothing. Every
# KEEPALIVE_INTERVAL_S we write a drain byte (a cheap request the bottle answers)
# and tear the session down after KEEPALIVE_MAX_FAILURES consecutive failures.
KEEPALIVE_INTERVAL_S: Final = 60.0
KEEPALIVE_MAX_FAILURES: Final = 2
# If no notification of any kind has arrived for this long, reconnect and redo
# the handshake. An idle bottle overnight costs one cheap reconnect per window;
# a wedged one recovers without anyone pressing the reset button.
SESSION_STALE_S: Final = 30 * 60.0

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

# BLE: standard Device Information service. Read once per connection, best
# effort, so the device page shows the real model and firmware (and so a PRO 2
# can be told apart from a PRO in bug reports).
CHAR_MODEL_NUMBER: Final = "00002a24-0000-1000-8000-00805f9b34fb"
CHAR_FIRMWARE_REV: Final = "00002a26-0000-1000-8000-00805f9b34fb"
CHAR_HARDWARE_REV: Final = "00002a27-0000-1000-8000-00805f9b34fb"
CHAR_MANUFACTURER: Final = "00002a29-0000-1000-8000-00805f9b34fb"
DEVICE_INFO_CHARS: Final[dict[str, str]] = {
    "model": CHAR_MODEL_NUMBER,
    "sw_version": CHAR_FIRMWARE_REV,
    "hw_version": CHAR_HARDWARE_REV,
    "manufacturer": CHAR_MANUFACTURER,
}

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
#
# It turns out NOT to hold across bottles: the 621 mL PRO's sip frames show a
# steady ~1.59 raw/mL (issue #11). So this is now only the starting value; the
# real per-bottle scale is learned from sip frames, see the next block.
RAW_UNITS_PER_ML: Final = 1.305
# Learned scale. Every sip frame carries the bottle's own before/after raw
# weight, and the sip volume, so each genuine frame is a (raw delta, mL) sample.
# Samples are pooled as running sums, which weights them by volume and keeps the
# 1%-quantisation of small sips from dominating. Sums decay so the scale can
# follow a different lid or a re-seated puck.
SCALE_MIN_ML_OBSERVED: Final = 250  # mL of sips needed before trusting it
SCALE_SUM_DECAY: Final = 0.98  # per sample
# Anything outside these bounds is not a load cell reading, it is a bad frame.
SCALE_SAMPLE_MIN: Final = 0.8
SCALE_SAMPLE_MAX: Final = 3.0
# A jump of this many u16 units across a cap open/close means the bottle was
# refilled (~30 mL at the scale above) rather than just opened to drink.
REFILL_MIN_DELTA_RAW: Final = 60
# The cap-close check and the sip-frame check can both see the same refill.
# A second report within this window re-anchors but is not counted again.
REFILL_DEDUP_WINDOW_S: Final = 120.0
# A refill can't add more than a bottle's worth (with margin for scale error);
# a bigger jump in a frame's 'before' weight is a corrupt byte, not water.
REFILL_MAX_BOTTLES: Final = 1.3

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
