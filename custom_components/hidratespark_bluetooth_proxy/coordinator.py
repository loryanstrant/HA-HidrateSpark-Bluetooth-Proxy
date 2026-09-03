"""Coordinator binding the BLE protocol to Home Assistant entities.

This is what makes ESPHome Bluetooth proxies work: we obtain the BLEDevice via
homeassistant.components.bluetooth.async_ble_device_from_address() — which
transparently returns a proxy-backed device when the bottle is in range of an
ESPHome proxy and not a local adapter — and feed it to bleak-retry-connector.
The same code path also drives a directly-attached USB/onboard adapter.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .ble import BottleClient
from .const import DOMAIN
from .state import BottleState, Sip

_LOGGER = logging.getLogger(__name__)

SIGNAL_UPDATE = f"{DOMAIN}_update_{{entry_id}}"


class HidrateSparkCoordinator:
    """Owns the BLE client, the persisted state, and broadcasts updates."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        address: str,
        name: str,
        size_ml: int,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.address = address.upper()
        self.name = name
        self.state = BottleState(hass, entry.entry_id, size_ml)

        self._client: Optional[BottleClient] = None
        self._task: Optional[asyncio.Task] = None
        self._unsub_advert: Optional[CALLBACK_TYPE] = None
        self._battery_pct: Optional[int] = None
        self._connected = False
        self.last_error: Optional[str] = None

    @property
    def signal(self) -> str:
        return SIGNAL_UPDATE.format(entry_id=self.entry.entry_id)

    @property
    def battery_pct(self) -> Optional[int]:
        return self._battery_pct

    @property
    def connected(self) -> bool:
        return self._connected

    # ---------------------------------------------------------------- lifecycle

    async def async_start(self) -> None:
        await self.state.async_load()

        self._client = BottleClient(
            address=self.address,
            name=self.name,
            size_ml=self.state.bottle_size_ml,
            on_sip=self._handle_sip,
            on_battery=self._handle_battery,
            on_status=self._handle_status,
            on_refill=self._handle_refill,
            on_weight=self._handle_weight,
            ble_device_provider=self._get_ble_device,
            anchor_exists=lambda: self.state.weight_full_raw is not None,
        )

        # Wake the BLE loop whenever HA sees a fresh advertisement for the bottle.
        self._unsub_advert = bluetooth.async_register_callback(
            self.hass,
            self._on_advertisement,
            bluetooth.BluetoothCallbackMatcher(address=self.address),
            bluetooth.BluetoothScanningMode.PASSIVE,
        )

        self._task = self.hass.async_create_background_task(
            self._client.run(), name=f"hidratespark-{self.address}"
        )

    async def async_stop(self) -> None:
        if self._unsub_advert is not None:
            self._unsub_advert()
            self._unsub_advert = None
        if self._client is not None:
            await self._client.stop()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
        await self.state.async_save()

    def request_force_sync(self) -> None:
        if self._client is not None:
            self._client.request_force_sync()

    # ------------------------------------------------------- BLE device lookup

    def _get_ble_device(self):
        """Return the most current BLEDevice (proxy or local) for the bottle."""
        return bluetooth.async_ble_device_from_address(
            self.hass, self.address, connectable=True
        )

    @callback
    def _on_advertisement(
        self,
        service_info: bluetooth.BluetoothServiceInfoBleak,
        change: bluetooth.BluetoothChange,
    ) -> None:
        # Just nudge the run loop — the BLE client picks up the latest device on
        # its next iteration. We don't drive connect/disconnect from here.
        if self._client is not None and not self._connected:
            self._client.request_force_sync()

    # ---------------------------------------------------------- BLE callbacks

    async def _handle_sip(
        self,
        timestamp: float,
        volume_ml: int,
        _total_reported_pct: int,
        rejected: bool = False,
    ) -> None:
        sip = Sip(timestamp=timestamp, volume_ml=volume_ml)
        if self.state.add_sip(sip, rejected=rejected):
            await self.state.async_save()
            self._notify()
        elif sip.rejected:
            # Counted for nothing, but it went into the dedup window, so the
            # window has to be persisted or the bottle replays it as new.
            await self.state.async_save()

    async def _handle_battery(self, pct: int) -> None:
        if pct == self._battery_pct:
            return
        self._battery_pct = pct
        self._notify()

    async def _handle_status(self, connected: bool, error: Optional[str]) -> None:
        if connected != self._connected:
            self._connected = connected
        self.last_error = error
        self._notify()

    async def _handle_refill(self, source: str, weight_full_raw: Optional[int]) -> None:
        self.state.refill(source, weight_full_raw)
        await self.state.async_save()
        self._notify()

    async def _handle_weight(self, raw: int, _low_byte: int) -> None:
        fill_changed = self.state.update_fill_from_weight(raw)
        if fill_changed:
            # Persist sparingly — fill changes happen often. Only on real change.
            await self.state.async_save()
        # Always notify so the live raw-weight reading refreshes even before a
        # calibration anchor exists (update_fill_from_weight returns False once
        # an anchor is set and fill is unchanged, but it still records the
        # latest stable raw reading).
        self._notify()

    @callback
    def _notify(self) -> None:
        async_dispatcher_send(self.hass, self.signal)
