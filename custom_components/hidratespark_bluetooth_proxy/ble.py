"""BLE protocol layer for HidrateSpark bottles.

Implements the HydroSync 13-step handshake (DEBUG/SET_POINT writes) so newer
firmwares stream sip notifications, with a fallback to the legacy notify-only
characteristic for older firmwares (e.g. firmware 80.18 on nRF52832).

Connection establishment is delegated to bleak-retry-connector, which means the
same code path works for both local Bluetooth adapters and Home Assistant's
ESPHome Bluetooth proxy transport.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Optional

from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

from .const import (
    CHAR_BATTERY_LEVEL,
    CHAR_CAP,
    CHAR_DATA_POINT,
    CHAR_DEBUG,
    CHAR_SET_POINT,
    CHAR_USER_DATA,
    CHAR_WEIGHT,
    DRAIN_BYTE,
    HANDSHAKE_COMMANDS,
    HANDSHAKE_INTERVAL_S,
    MAX_IDENTICAL_SIP_FRAMES,
    MAX_SIP_PCT,
    REFILL_MIN_DELTA_RAW,
    REFILL_SETTLE_TIMEOUT_S,
    REFILL_STABLE_SAMPLES,
    RAW_STABLE_TOLERANCE,
    RAW_UNITS_PER_ML,
    SIP_WEIGHT_CHECK_HEADROOM,
)

_LOGGER = logging.getLogger(__name__)

# Callback signatures
SipCallback = Callable[[float, int, int, bool], Awaitable[None]]
"""await on_sip(timestamp_unix_s, volume_ml, total_reported_pct, rejected)."""

BatteryCallback = Callable[[int], Awaitable[None]]
StatusCallback = Callable[[bool, Optional[str]], Awaitable[None]]
RefillCallback = Callable[[str, Optional[int]], Awaitable[None]]
"""await on_refill(source, weight_full_raw_anchor)."""

WeightCallback = Callable[[int, int], Awaitable[None]]
"""await on_weight(raw_u16, low_byte_when_upright)."""


class BottleClient:
    """Maintains a single persistent connection to a HidrateSpark bottle."""

    def __init__(
        self,
        address: str,
        name: str,
        *,
        size_ml: int,
        on_sip: SipCallback,
        on_battery: BatteryCallback,
        on_status: StatusCallback,
        on_refill: RefillCallback,
        on_weight: WeightCallback,
        ble_device_provider: Callable[[], Optional[BLEDevice]],
        anchor_exists: Callable[[], bool],
    ) -> None:
        self.address = address.upper().strip()
        self.name = name
        self.size_ml = max(1, int(size_ml))
        self._on_sip = on_sip
        self._on_battery = on_battery
        self._on_status = on_status
        self._on_refill = on_refill
        self._on_weight = on_weight
        self._ble_device_provider = ble_device_provider
        self._anchor_exists = anchor_exists

        self._client: Optional[BleakClient] = None
        self._stop = asyncio.Event()
        self._connected = False
        self._handshake_path: Optional[str] = None
        self._data_char: Optional[str] = None

        # Inline re-drain loop guard: track the last sip frame and how many
        # times it has repeated, so we stop acking a record the firmware never
        # pops instead of spinning in a write loop.
        self._last_frame_hex: Optional[str] = None
        self._frame_repeat = 0
        # Highest running-total-percent seen so far, for the monotonic guard in
        # _on_data_notify. In-memory only: it is a corruption tripwire, not an
        # accounting figure, so starting fresh after a restart is fine.
        self._last_total_pct: Optional[int] = None

        # Refill detection state (16-bit raw weight values)
        self._weight_stable_raw: Optional[int] = None
        self._weight_stable_streak = 0
        self._weight_last_raw: Optional[int] = None
        self._pre_open_weight_raw: Optional[int] = None
        self._cap_open = False
        self._refill_check_task: Optional[asyncio.Task] = None

        # Wake/force-sync events for the run loop
        self._wake = asyncio.Event()
        self._force_sync = asyncio.Event()

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def handshake_path(self) -> Optional[str]:
        return self._handshake_path

    def request_force_sync(self) -> None:
        self._force_sync.set()
        self._wake.set()

    async def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._refill_check_task and not self._refill_check_task.done():
            self._refill_check_task.cancel()
        client = self._client
        if client is not None and client.is_connected:
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001 — best-effort teardown
                pass

    # ------------------------------------------------------------------ run loop

    async def run(self) -> None:
        """Hold a persistent connection, reconnecting with exponential backoff."""
        backoff = 1.0
        while not self._stop.is_set():
            device = self._ble_device_provider()
            if device is None:
                await self._on_status(False, "device not in range")
                await self._sleep_or_wake(backoff)
                backoff = min(backoff * 2, 60.0)
                continue

            try:
                client = await establish_connection(
                    BleakClientWithServiceCache,
                    device,
                    self.name,
                    disconnected_callback=self._on_disconnected,
                    use_services_cache=True,
                    max_attempts=3,
                )
            except Exception as err:  # noqa: BLE001 — log & retry
                _LOGGER.warning("connect to %s failed: %s", self.address, err)
                await self._on_status(False, str(err))
                await self._sleep_or_wake(backoff)
                backoff = min(backoff * 2, 60.0)
                continue

            self._client = client
            self._connected = True
            backoff = 1.0
            _LOGGER.info("connected to %s", self.address)
            await self._on_status(True, None)

            try:
                await self._after_connect(client)
                await self._on_status(True, None)
                while client.is_connected and not self._stop.is_set():
                    if self._force_sync.is_set():
                        self._force_sync.clear()
                        try:
                            await self._drain(client)
                        except Exception as err:  # noqa: BLE001
                            _LOGGER.warning("forced drain failed: %s", err)
                    await self._sleep_or_wake(5.0)
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                _LOGGER.exception("BLE session error: %s", err)
                await self._on_status(False, str(err))
            finally:
                self._connected = False
                self._client = None
                try:
                    if client.is_connected:
                        await client.disconnect()
                except Exception:  # noqa: BLE001
                    pass
                await self._on_status(False, None)

    def _on_disconnected(self, _client: BleakClient) -> None:
        self._connected = False
        self._wake.set()

    async def _sleep_or_wake(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass
        finally:
            self._wake.clear()

    # ------------------------------------------------------------ post-connect

    async def _after_connect(self, client: BleakClient) -> None:
        # Reset per-connection state so a previous connection's choices don't
        # leak in if the firmware behaviour changed (or the previous attempt
        # half-succeeded).
        self._handshake_path = None
        self._data_char = None
        # Battery: read once, then subscribe for change notifications.
        try:
            data = await client.read_gatt_char(CHAR_BATTERY_LEVEL)
            if data:
                pct = data[0]
                _LOGGER.debug("battery: %d%%", pct)
                await self._on_battery(pct)
            try:
                await client.start_notify(CHAR_BATTERY_LEVEL, self._on_battery_notify)
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("battery notify unavailable: %s", err)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("battery read failed: %s", err)

        # Try modern handshake + USER_DATA notifications. Fall back to legacy.
        modern_ok = False
        try:
            await self._handshake(client)
            await client.start_notify(CHAR_USER_DATA, self._on_data_notify)
            self._data_char = CHAR_USER_DATA
            self._handshake_path = "modern"
            modern_ok = True
            _LOGGER.info("modern path active (USER_DATA notifications)")
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("modern handshake failed (%s); falling back to legacy", err)

        if not modern_ok:
            try:
                await client.start_notify(CHAR_DATA_POINT, self._on_data_notify)
                self._data_char = CHAR_DATA_POINT
                self._handshake_path = "legacy"
                _LOGGER.info("legacy path active (DATA_POINT notifications)")
            except Exception:
                self._handshake_path = None
                raise

        # Cap state notifications (shared UUID with DEBUG)
        try:
            await client.start_notify(CHAR_CAP, self._on_cap_notify)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("cap notify unavailable: %s", err)

        # Weight notifications
        try:
            await client.start_notify(CHAR_WEIGHT, self._on_weight_notify)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("weight notify unavailable: %s", err)

        # Initial drain so any buffered sips replay.
        try:
            await self._drain(client)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("initial drain failed: %s", err)

    # ----------------------------------------------------------- handshake/drain

    async def _handshake(self, client: BleakClient) -> None:
        for target, payload_hex in HANDSHAKE_COMMANDS:
            char = CHAR_DEBUG if target == "DEBUG" else CHAR_SET_POINT
            await client.write_gatt_char(char, bytes.fromhex(payload_hex), response=True)
            await asyncio.sleep(HANDSHAKE_INTERVAL_S)

    async def _drain(self, client: BleakClient) -> None:
        if not self._data_char:
            return
        await client.write_gatt_char(self._data_char, DRAIN_BYTE, response=True)

    # ------------------------------------------------------------- notify hooks

    async def _on_battery_notify(self, _char, data: bytearray) -> None:
        if not data:
            return
        await self._on_battery(data[0])

    async def _on_data_notify(self, _char, data: bytearray) -> None:
        # HydroSync sip-record frame: 20 bytes. This layout is derived from the
        # 13 frames logged on a real bottle between 2026-09-01 and 2026-09-03,
        # not from vendor documentation:
        #
        #   [0]     N pending records remaining (0 = empty queue)
        #   [1]     sip volume as percent of bottle capacity
        #   [2:4]   running total, little-endian u16 — a cumulative SUM OF THE
        #           PERCENT FIELD, not mL. Resets daily, around midnight.
        #   [4]     unknown; observed values 1, 4 and 8
        #   [5:8]   always 00 00 00
        #   [8:12]  the constant 75 87 2a 8b, identical in every frame
        #   [12:14] bottle raw weight BEFORE the sip, little-endian u16
        #   [14:16] bottle raw weight AFTER the sip, little-endian u16
        #   [16:20] always zero
        #
        # Evidence for [2:4]: consecutive deltas equal the pct field exactly,
        # eight frames running. Evidence for the weight pair: every frame's
        # "before" equals the previous frame's "after", and the raw delta per
        # percent is 9.5-10.5 across all twelve good frames.
        #
        # There is NO per-sip timestamp anywhere in the frame. The code used to
        # read [5:9] big-endian as "seconds ago", which always yielded 117 (the
        # 0x75 that starts the constant at [8]), so every sip was silently
        # backdated by a fixed 1 min 57 s — the "last sip time trails by about
        # two minutes" that users kept reporting. Frames arrive immediately
        # after our own 0x57 drain, so arrival time is the best estimate we have.
        if not self._connected or self._stop.is_set():
            return
        if not data:
            return

        remaining = data[0]

        # An empty-queue announcement: nothing to parse, no need to re-drain.
        if remaining == 0:
            _LOGGER.debug("sip frame: queue empty")
            self._last_frame_hex = None
            self._frame_repeat = 0
            return

        # Track identical consecutive frames so a record the firmware never
        # pops can't trap us in an unbounded re-drain (write) loop.
        frame_hex = data.hex()
        if frame_hex == self._last_frame_hex:
            self._frame_repeat += 1
        else:
            self._frame_repeat = 0
        self._last_frame_hex = frame_hex

        # Re-drain immediately so the bottle pushes the next record. Some
        # firmwares first send a short "N pending" frame and only emit the real
        # record after the next 0x57. Skip the ack once the same frame has
        # repeated too many times — the bottle isn't advancing its queue.
        if self._frame_repeat >= MAX_IDENTICAL_SIP_FRAMES:
            _LOGGER.warning(
                "sip frame repeated %d×; pausing re-drain to avoid a write loop: %s",
                self._frame_repeat + 1,
                frame_hex,
            )
        elif self._client and self._client.is_connected:
            try:
                await self._drain(self._client)
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("inline drain failed: %s", err)

        if len(data) < 9:
            _LOGGER.debug("sip frame too short (%d): %s", len(data), data.hex())
            return

        pct = data[1]
        total_pct = int.from_bytes(data[2:4], "little")
        # The weight pair only exists on full-length frames. Anything shorter
        # simply skips the cross-check below rather than being rejected.
        if len(data) >= 16:
            weight_before = int.from_bytes(data[12:14], "little")
            weight_after = int.from_bytes(data[14:16], "little")
        else:
            weight_before = weight_after = 0

        ts = time.time()
        volume_ml = round(self.size_ml * pct / 100)

        if volume_ml <= 0 or pct == 0:
            _LOGGER.debug(
                "skipping zero-volume sip frame (pct=%d, total_pct=%d)",
                pct, total_pct,
            )
            return

        # Diagnostic: log the whole decoded frame at INFO, raw hex included, so
        # the corpus keeps growing and any future decode question can be settled
        # from real traffic rather than guesswork.
        _LOGGER.info(
            "sip: %dml (pct=%d, total_pct=%d, remaining=%d, weight %d->%d) raw=%s",
            volume_ml, pct, total_pct, remaining,
            weight_before, weight_after, data.hex(),
        )

        rejected = not self._sip_frame_is_plausible(
            data, pct, total_pct, volume_ml, weight_before, weight_after
        )

        # An implausible frame is still handed over, flagged. It contributes to
        # no total, but it has to reach the dedup window so the same record isn't
        # replayed as new on the next drain.
        try:
            await self._on_sip(float(ts), int(volume_ml), int(total_pct), rejected)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("sip handler error: %s", err)

    def _sip_frame_is_plausible(
        self,
        data: bytearray,
        pct: int,
        total_pct: int,
        volume_ml: int,
        weight_before: int,
        weight_after: int,
    ) -> bool:
        """Return False for a frame that cannot describe a real drink.

        The caller still passes the sip on, flagged, so it reaches the dedup
        window and isn't replayed as new later; it just contributes to no total.
        The frame has already been acked by the inline re-drain above, so the
        bottle's queue advances either way.
        """
        # 1. Implausible percent. A single sip does not empty a full bottle.
        if pct >= MAX_SIP_PCT:
            _LOGGER.warning(
                "rejecting sip frame: pct=%d claims %dml, at or above the %d%% "
                "ceiling for one sip. raw=%s",
                pct, volume_ml, MAX_SIP_PCT, data.hex(),
            )
            return False

        # 2. The running-total counter must not go backwards. It resets daily,
        # and the first record after a reset always carries total == pct, so a
        # decrease to at or below this frame's own pct is legitimate.
        #
        # The anchor is updated even when we reject, deliberately: if a record
        # is ever missed right after a reset the decrease would look corrupt,
        # and holding on to the old high-water mark would then reject every
        # remaining sip of the day. Re-anchoring costs at most a single frame.
        last_total = self._last_total_pct
        self._last_total_pct = total_pct
        if last_total is not None and total_pct < last_total and total_pct > pct:
            _LOGGER.warning(
                "rejecting sip frame: running total went backwards %d -> %d "
                "without looking like a counter reset (pct=%d). raw=%s",
                last_total, total_pct, pct, data.hex(),
            )
            return False

        # 3. Cross-check the claimed volume against the bottle's own weight
        # readings carried in this same frame. Fails open — an unusable pair is
        # skipped, never treated as a reason to reject — because these bytes are
        # undocumented and other firmwares may not populate them.
        weight_delta = weight_before - weight_after
        if weight_before > 0 and weight_after > 0 and weight_delta > 0:
            supported_ml = weight_delta / RAW_UNITS_PER_ML
            if volume_ml > SIP_WEIGHT_CHECK_HEADROOM * supported_ml:
                _LOGGER.warning(
                    "rejecting sip frame: claims %dml but the frame's own "
                    "weight only dropped %d raw units (~%dml). raw=%s",
                    volume_ml, weight_delta, round(supported_ml), data.hex(),
                )
                return False

        return True

    async def _on_cap_notify(self, _char, data: bytearray) -> None:
        _LOGGER.debug("cap frame raw=%s", data.hex() if data else "<empty>")
        if not self._connected or self._stop.is_set():
            return
        if not data:
            return
        # bit 0 of byte 0: 1 = open, 0 = closed
        is_open = bool(data[0] & 0x01)
        if is_open and not self._cap_open:
            self._cap_open = True
            self._pre_open_weight_raw = self._weight_stable_raw
            _LOGGER.debug("cap opened (pre=%s)", self._pre_open_weight_raw)
        elif not is_open and self._cap_open:
            self._cap_open = False
            _LOGGER.debug("cap closed; scheduling refill check")
            if self._refill_check_task and not self._refill_check_task.done():
                self._refill_check_task.cancel()
            self._refill_check_task = asyncio.create_task(self._check_refill_after_close())

    async def _on_weight_notify(self, _char, data: bytearray) -> None:
        if not self._connected or self._stop.is_set():
            return
        if len(data) < 2:
            return
        # The full 16-bit big-endian value is the weight; it rises as the bottle
        # fills. There is no reliable orientation flag, so a trustworthy "upright
        # & settled" reading is identified purely by stability: N consecutive
        # samples within tolerance. Frames emitted while the bottle is moved jump
        # around and never form a streak, so they are filtered out here.
        raw = (data[0] << 8) | data[1]
        _LOGGER.debug("weight frame raw=%s (u16=%d)", data.hex(), raw)

        if (
            self._weight_last_raw is not None
            and abs(raw - self._weight_last_raw) <= RAW_STABLE_TOLERANCE
        ):
            self._weight_stable_streak += 1
        else:
            self._weight_stable_streak = 1
        self._weight_last_raw = raw

        if self._weight_stable_streak >= REFILL_STABLE_SAMPLES:
            self._weight_stable_raw = raw
            try:
                await self._on_weight(raw, raw)
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("weight handler error: %s", err)

    # ------------------------------------------------------------- refill check

    async def _check_refill_after_close(self) -> None:
        """After cap closes, wait for a stable upright reading and compare."""
        if self._stop.is_set():
            return
        deadline = time.monotonic() + REFILL_SETTLE_TIMEOUT_S
        baseline = self._pre_open_weight_raw
        try:
            while time.monotonic() < deadline:
                await asyncio.sleep(1.0)
                if self._stop.is_set():
                    return
                post = self._weight_stable_raw
                if post is None:
                    continue
                if baseline is None or not self._anchor_exists():
                    # No usable baseline, or no full-weight anchor established
                    # yet, so adopt this settled reading as the calibration
                    # anchor (bottle assumed full) so subsequent fill maths work.
                    # Not counted as a refill.
                    _LOGGER.info(
                        "calibration: adopting %s as full-weight anchor", post
                    )
                    await self._on_refill("calibration", post)
                    return
                if post - baseline >= REFILL_MIN_DELTA_RAW:
                    _LOGGER.info(
                        "REFILL detected: pre=%s post=%s delta=%s",
                        baseline,
                        post,
                        post - baseline,
                    )
                    await self._on_refill("cap_close", post)
                    return
        except asyncio.CancelledError:
            return
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("refill check error: %s", err)
