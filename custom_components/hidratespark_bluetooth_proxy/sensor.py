"""HidrateSpark sensors."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import HidrateSparkCoordinator
from .entity import HidrateSparkEntity


SENSORS: tuple[SensorEntityDescription, ...] = (
    SensorEntityDescription(
        key="battery",
        translation_key="battery",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorEntityDescription(
        key="water_today",
        translation_key="water_today",
        native_unit_of_measurement=UnitOfVolume.MILLILITERS,
        device_class=SensorDeviceClass.VOLUME,
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    SensorEntityDescription(
        key="water_lifetime",
        translation_key="water_lifetime",
        native_unit_of_measurement=UnitOfVolume.MILLILITERS,
        device_class=SensorDeviceClass.VOLUME,
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    SensorEntityDescription(
        key="current_fill",
        translation_key="current_fill",
        native_unit_of_measurement=UnitOfVolume.MILLILITERS,
        device_class=SensorDeviceClass.VOLUME_STORAGE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="current_fill_pct",
        translation_key="current_fill_pct",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="last_sip_volume",
        translation_key="last_sip_volume",
        native_unit_of_measurement=UnitOfVolume.MILLILITERS,
        device_class=SensorDeviceClass.VOLUME_STORAGE,
    ),
    SensorEntityDescription(
        key="last_sip_time",
        translation_key="last_sip_time",
        device_class=SensorDeviceClass.TIMESTAMP,
    ),
    SensorEntityDescription(
        key="weight_raw",
        translation_key="weight_raw",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: HidrateSparkCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(HidrateSparkSensor(coordinator, desc) for desc in SENSORS)


class HidrateSparkSensor(HidrateSparkEntity, SensorEntity):
    """A single sensor reading from the bottle's coordinator state."""

    entity_description: SensorEntityDescription

    def __init__(
        self,
        coordinator: HidrateSparkCoordinator,
        description: SensorEntityDescription,
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        state = self._coordinator.state
        key = self.entity_description.key
        if key == "battery":
            return self._coordinator.battery_pct
        if key == "water_today":
            return state.total_today_ml
        if key == "water_lifetime":
            return state.lifetime_total_ml
        if key == "current_fill":
            return state.current_fill_ml
        if key == "current_fill_pct":
            return state.current_fill_pct
        if key == "last_sip_volume":
            return state.last_sip.volume_ml if state.last_sip else None
        if key == "last_sip_time":
            if state.last_sip is None:
                return None
            return datetime.fromtimestamp(state.last_sip.timestamp, tz=timezone.utc)
        if key == "weight_raw":
            return state.weight_low
        return None

    @property
    def available(self) -> bool:
        # Live-only readings (battery, raw weight) become unavailable when
        # the bottle is disconnected. Cached/accumulated values (totals,
        # current fill, last sip) remain meaningful across disconnects.
        if self.entity_description.key in ("battery", "weight_raw"):
            return self._coordinator.connected
        return True
