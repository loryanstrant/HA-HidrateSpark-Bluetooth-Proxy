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
    REFILL_MIN_DELTA,
    REFILL_SETTLE_TIMEOUT_S,
    REFILL_STABLE_SAMPLES,
    REFILL_STABLE_TOLERANCE,
    WEIGHT_HIGH_STABLE,
)

_LOGGER = logging.getLogger(__name__)

# Callback signatures
SipCallback = Callable[[float, int, int], Awaitable[None]]
"""await on_sip(timestamp_unix_s, volume_ml, total_reported_ml)."""

BatteryCallback = Callable[[int], Awaitable[None]]
StatusCallback = Callable[[bool, Optional[str]], Awaitable[None]]
RefillCallback = Callable[[str, Optional[int]], Awaitable[None]]
"""await on_refill(source, weight_full_low_anchor)."""

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

        self._client: Optional[BleakClient] = None
        self._stop = asyncio.Event()
        self._connected = False
        self._handshake_path: Optional[str] = None
        self._data_char: Optional[str] = None

        # Refill detection state
        self._weight_stable_low: Optional[int] = None
        self._weight_stable_streak = 0
        self._weight_last_low: Optional[int] = None
        self._pre_open_weight_low: Optional[int] = None
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
        # HydroSync sip-record frame layout (>= 9 bytes):
        #   [0]   N pending records remaining (0 = empty queue)
        #   [1]   sip volume as percent of bottle capacity
        #   [2:4] total reported volume so far, big-endian u16 (mL)
        #   [4]   reserved / record type
        #   [5:9] seconds-ago for this sip, big-endian u32
        # Volume in mL is derived from the percent * configured bottle size.
        if not self._connected or self._stop.is_set():
            return
        if not data:
            return

        remaining = data[0]

        # An empty-queue announcement: nothing to parse, no need to re-drain.
        if remaining == 0:
            _LOGGER.debug("sip frame: queue empty")
            return

        # Re-drain immediately so the bottle pushes the next record. Some
        # firmwares first send a short "N pending" frame and only emit the
        # real record after the next 0x57.
        if self._client and self._client.is_connected:
            try:
                await self._drain(self._client)
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("inline drain failed: %s", err)

        if len(data) < 9:
            _LOGGER.debug("sip frame too short (%d): %s", len(data), data.hex())
            return

        try:
            pct = data[1]
            total_reported = int.from_bytes(data[2:4], "big")
            seconds_ago = int.from_bytes(data[5:9], "big")
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("malformed sip frame %s: %s", data.hex(), err)
            return

        # Sanity clamp: ignore obviously bogus seconds-ago (> 10 years).
        if seconds_ago > 10 * 365 * 24 * 3600:
            seconds_ago = 0
        ts = time.time() - seconds_ago
        volume_ml = round(self.size_ml * pct / 100)

        if volume_ml <= 0 or pct == 0:
            _LOGGER.debug(
                "skipping zero-volume sip frame (pct=%d, total=%d)",
                pct, total_reported,
            )
            return

        # Diagnostic: log the raw frame at INFO so the seconds-ago byte
        # range can be cross-checked when 'last sip time' looks off (e.g.
        # users reporting it always trails by ~2 minutes).
        _LOGGER.info(
            "sip: %dml (pct=%d, total_reported=%d, %ds ago, remaining=%d) raw=%s",
            volume_ml, pct, total_reported, seconds_ago, remaining, data.hex(),
        )

        try:
            await self._on_sip(float(ts), int(volume_ml), int(total_reported))
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("sip handler error: %s", err)

    async def _on_cap_notify(self, _char, data: bytearray) -> None:
        if not self._connected or self._stop.is_set():
            return
        if not data:
            return
        # bit 0 of byte 0: 1 = open, 0 = closed
        is_open = bool(data[0] & 0x01)
        if is_open and not self._cap_open:
            self._cap_open = True
            self._pre_open_weight_low = self._weight_stable_low
            _LOGGER.debug("cap opened (pre=%s)", self._pre_open_weight_low)
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
        high = data[0]
        low = data[1]
        raw = (high << 8) | low

        if high != WEIGHT_HIGH_STABLE:
            # Tilted/transient — useful only to invalidate stability streak.
            self._weight_last_low = None
            self._weight_stable_streak = 0
            return

        # Track stability: 3 consecutive samples within ±tolerance.
        if (
            self._weight_last_low is not None
            and abs(low - self._weight_last_low) <= REFILL_STABLE_TOLERANCE
        ):
            self._weight_stable_streak += 1
        else:
            self._weight_stable_streak = 1
        self._weight_last_low = low

        if self._weight_stable_streak >= REFILL_STABLE_SAMPLES:
            self._weight_stable_low = low
            try:
                await self._on_weight(raw, low)
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("weight handler error: %s", err)

    # ------------------------------------------------------------- refill check

    async def _check_refill_after_close(self) -> None:
        """After cap closes, wait for a stable upright reading and compare."""
        if self._stop.is_set():
            return
        deadline = time.monotonic() + REFILL_SETTLE_TIMEOUT_S
        baseline = self._pre_open_weight_low
        try:
            while time.monotonic() < deadline:
                await asyncio.sleep(1.0)
                if self._stop.is_set():
                    return
                post = self._weight_stable_low
                if post is None:
                    continue
                if baseline is None:
                    # No pre-open snapshot — don't declare a refill, just
                    # adopt this reading as the calibration anchor so that
                    # subsequent fill calculations work. (Avoids a spurious
                    # refill on the very first cap-close after install.)
                    _LOGGER.debug(
                        "refill check: no pre-open baseline; adopting %s as anchor",
                        post,
                    )
                    await self._on_refill("calibration", post)
                    return
                if post - baseline >= REFILL_MIN_DELTA:
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
