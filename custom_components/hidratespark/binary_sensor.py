"""HidrateSpark binary sensors."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import HidrateSparkCoordinator
from .entity import HidrateSparkEntity


BINARY_SENSORS: tuple[BinarySensorEntityDescription, ...] = (
    BinarySensorEntityDescription(
        key="connected",
        translation_key="connected",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: HidrateSparkCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        HidrateSparkBinarySensor(coordinator, desc) for desc in BINARY_SENSORS
    )


class HidrateSparkBinarySensor(HidrateSparkEntity, BinarySensorEntity):
    """Connectivity sensor — also serves as the device's online indicator."""

    entity_description: BinarySensorEntityDescription

    def __init__(
        self,
        coordinator: HidrateSparkCoordinator,
        description: BinarySensorEntityDescription,
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool:
        return self._coordinator.connected

    @property
    def available(self) -> bool:
        # Always available so HA can show "Disconnected" when the bottle is gone.
        return True
