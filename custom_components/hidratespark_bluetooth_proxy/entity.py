"""Base entity for HidrateSpark sensors and binary sensors."""

from __future__ import annotations

from homeassistant.core import callback
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity

from .const import DOMAIN
from .coordinator import HidrateSparkCoordinator


class HidrateSparkEntity(Entity):
    """Common bits for every entity belonging to a bottle."""

    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(self, coordinator: HidrateSparkCoordinator, key: str) -> None:
        self._coordinator = coordinator
        # Keyed on the fixed device key, never the live address, so a bottle
        # that rotates its BLE address keeps its device and entities (#10).
        key_base = coordinator.device_key
        self._attr_unique_id = f"{key_base}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, key_base)},
            connections={(CONNECTION_BLUETOOTH, key_base)},
            name=coordinator.name or "HidrateSpark",
            manufacturer="HidrateSpark",
            model="Smart Water Bottle",
        )

    async def async_added_to_hass(self) -> None:
        # A first connection can finish before the entity registers its device.
        # Apply cached metadata now; reconnects refresh it again.
        await self._coordinator._handle_device_info(self._coordinator.device_info)
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, self._coordinator.signal, self._handle_update
            )
        )

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        return self._coordinator.connected
