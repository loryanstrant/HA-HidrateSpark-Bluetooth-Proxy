"""Coordinator binding the BLE protocol to Home Assistant entities.

This is what makes ESPHome Bluetooth proxies work: we obtain the BLEDevice via
homeassistant.components.bluetooth.async_ble_device_from_address() — which
transparently returns a proxy-backed device when the bottle is in range of an
ESPHome proxy and not a local adapter — and feed it to bleak-retry-connector.
The same code path also drives a directly-attached USB/onboard adapter.

Address rotation: some bottles (seen on the PRO 2) advertise from a rotating
private address. HA never bonds with the bottle, so it can't resolve the
rotation, and a coordinator pinned to one address would sit "device not in
range" forever (issue #10). We therefore also watch for the bottle's stable
advertised name and follow it to whatever address it is currently using.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .ble import BottleClient
from .const import (
    CONF_ADDRESS,
    CONF_DEVICE_KEY,
    CONF_LOCAL_NAME,
    DEFAULT_NAME_PREFIX,
    DOMAIN,
)
from .state import BottleState, Sip

_LOGGER = logging.getLogger(__name__)

SIGNAL_UPDATE = f"{DOMAIN}_update_{{entry_id}}"


def _stable_name(*candidates: Optional[str]) -> Optional[str]:
    """First candidate that is a real advertised bottle name (h2o*)."""
    for name in candidates:
        if name and name.lower().startswith(DEFAULT_NAME_PREFIX):
            return name
    return None


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
        # Fixed key for the device and entity registries: the address the bottle
        # had when it was first added. It never follows a rotation.
        self.device_key: str = str(entry.data.get(CONF_DEVICE_KEY) or self.address)
        # Stable advertised name (e.g. h2o00003158), used to find the bottle
        # again after its address rotates. None if it was added by address only.
        self.local_name: Optional[str] = _stable_name(
            entry.data.get(CONF_LOCAL_NAME), entry.unique_id, entry.title
        )
        # Model / firmware read from the Device Information service.
        self.device_info: dict[str, str] = {}
        self._registry_info: dict[str, str] = {}
        self.state = BottleState(hass, entry.entry_id, size_ml)

        self._client: Optional[BottleClient] = None
        self._task: Optional[asyncio.Task] = None
        self._unsub_adverts: list[CALLBACK_TYPE] = []
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
            on_device_info=self._handle_device_info,
            ble_device_provider=self._get_ble_device,
            anchor_exists=lambda: self.state.weight_full_raw is not None,
        )

        # Wake the BLE loop whenever HA sees a fresh advertisement for the
        # bottle: by address, and by name so a rotated address is noticed too.
        matchers = [bluetooth.BluetoothCallbackMatcher(address=self.address)]
        if self.local_name:
            matchers.append(
                bluetooth.BluetoothCallbackMatcher(local_name=self.local_name)
            )
        for matcher in matchers:
            self._unsub_adverts.append(
                bluetooth.async_register_callback(
                    self.hass,
                    self._on_advertisement,
                    matcher,
                    bluetooth.BluetoothScanningMode.PASSIVE,
                )
            )

        self._task = self.hass.async_create_background_task(
            self._client.run(), name=f"hidratespark-{self.device_key}"
        )

    async def async_stop(self) -> None:
        for unsub in self._unsub_adverts:
            unsub()
        self._unsub_adverts = []
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
        """Return the most current BLEDevice (proxy or local) for the bottle.

        Falls back to searching by advertised name when nothing is heard at the
        last known address, which is what a rotated address looks like.
        """
        device = bluetooth.async_ble_device_from_address(
            self.hass, self.address, connectable=True
        )
        if device is not None or not self.local_name:
            return device
        candidates = [
            info
            for info in bluetooth.async_discovered_service_info(
                self.hass, connectable=True
            )
            if info.name == self.local_name
        ]
        if not candidates:
            return None
        best = max(candidates, key=lambda i: i.rssi if i.rssi is not None else -999)
        self._follow_address(best.address)
        return best.device

    @callback
    def _follow_address(self, new_address: str) -> None:
        """Retarget the bottle at a new BLE address and remember it."""
        new_address = new_address.upper()
        if new_address == self.address:
            return
        _LOGGER.info(
            "%s: bottle moved from %s to %s (rotating address)",
            self.name, self.address, new_address,
        )
        self.address = new_address
        if self._client is not None:
            self._client.address = new_address
        # Persist so a restart starts from the right place. The entry update
        # listener ignores this change; it only reloads on a size change.
        if self.entry.data.get(CONF_ADDRESS) != new_address:
            self.hass.config_entries.async_update_entry(
                self.entry, data={**self.entry.data, CONF_ADDRESS: new_address}
            )

    @callback
    def _on_advertisement(
        self,
        service_info: bluetooth.BluetoothServiceInfoBleak,
        change: bluetooth.BluetoothChange,
    ) -> None:
        # Never retarget mid-session: the current link is demonstrably fine,
        # and the advert may be a stale relay from a proxy.
        if self._connected:
            return
        if (
            self.local_name
            and service_info.name == self.local_name
            and service_info.connectable
        ):
            self._follow_address(service_info.address)
        # Nudge the run loop; the BLE client picks up the latest device on its
        # next iteration. We don't drive connect/disconnect from here.
        if self._client is not None:
            self._client.request_force_sync()

    # ---------------------------------------------------------- BLE callbacks

    async def _handle_sip(
        self,
        timestamp: float,
        volume_ml: int,
        _total_reported_pct: int,
        rejected: bool = False,
        weight_before: int = 0,
        weight_after: int = 0,
    ) -> None:
        sip = Sip(timestamp=timestamp, volume_ml=volume_ml)
        if self.state.add_sip(
            sip,
            rejected=rejected,
            weight_before=weight_before,
            weight_after=weight_after,
        ):
            await self.state.async_save()
            self._notify()
        elif sip.rejected:
            # Counted for nothing, but it went into the dedup window, so the
            # window has to be persisted or the bottle replays it as new.
            await self.state.async_save()

    async def _handle_device_info(self, info: dict[str, str]) -> None:
        if not info:
            return
        self.device_info = info
        if info == self._registry_info:
            return
        registry = dr.async_get(self.hass)
        device = registry.async_get_device(identifiers={(DOMAIN, self.device_key)})
        if device is None:
            # Not registered yet (the first connect can beat entity setup).
            # _registry_info stays unset, so the next connection tries again.
            return
        registry.async_update_device(
            device.id,
            model=info.get("model") or device.model,
            sw_version=info.get("sw_version") or device.sw_version,
            hw_version=info.get("hw_version") or device.hw_version,
        )
        self._registry_info = info

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
