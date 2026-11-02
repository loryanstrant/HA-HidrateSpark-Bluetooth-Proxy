"""HidrateSpark integration for Home Assistant."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .config_flow import stable_id
from .const import (
    CONF_ADDRESS,
    CONF_DEVICE_KEY,
    CONF_LOCAL_NAME,
    CONF_NAME_PREFIX,
    CONF_SIZE_ML,
    DEFAULT_NAME_PREFIX,
    DEFAULT_SIZE_ML,
    DOMAIN,
)
from .coordinator import HidrateSparkCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.SENSOR]


def _size_ml(entry: ConfigEntry) -> int:
    return entry.options.get(
        CONF_SIZE_ML, entry.data.get(CONF_SIZE_ML, DEFAULT_SIZE_ML)
    )


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate entries created before the bottle was identified by name."""
    if entry.version > 1:
        return False  # downgrade from a future version; refuse

    if entry.minor_version < 2:
        address = str(entry.data[CONF_ADDRESS]).upper()
        # Entities and the device were registered under the address, so that
        # address becomes the fixed device key and nothing gets orphaned.
        data = {**entry.data, CONF_ADDRESS: address}
        data.setdefault(CONF_DEVICE_KEY, address)
        # The entry title is the advertised name whenever one was seen at setup.
        new_unique_id = stable_id(entry.title, address)
        clash = any(
            other.entry_id != entry.entry_id and other.unique_id == new_unique_id
            for other in hass.config_entries.async_entries(DOMAIN)
        )
        if clash:
            # A duplicate already created by an address rotation owns the name.
            # Leave this one keyed on its address; the user can delete the ghost.
            _LOGGER.warning(
                "%s: another entry already uses unique ID %s; keeping %s",
                entry.title, new_unique_id, entry.unique_id,
            )
            new_unique_id = entry.unique_id
        if new_unique_id != address:
            data.setdefault(CONF_LOCAL_NAME, new_unique_id)
        hass.config_entries.async_update_entry(
            entry, data=data, unique_id=new_unique_id, minor_version=2
        )
        _LOGGER.info(
            "migrated %s to config entry 1.2 (unique_id=%s)",
            entry.title, new_unique_id,
        )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up HidrateSpark from a config entry."""
    address: str = entry.data[CONF_ADDRESS]
    name: str = entry.title or entry.data.get(CONF_NAME_PREFIX, DEFAULT_NAME_PREFIX)

    coordinator = HidrateSparkCoordinator(
        hass=hass,
        entry=entry,
        address=address,
        name=name,
        size_ml=_size_ml(entry),
    )
    await coordinator.async_start()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(_async_entry_updated))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator: HidrateSparkCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_stop()
    return unload_ok


async def _async_entry_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload when the bottle size changes.

    The coordinator also writes to the entry itself, to remember a bottle's new
    address after a rotation. That must not reload, or every rotation would
    tear down a connection that just succeeded.
    """
    coordinator: HidrateSparkCoordinator | None = hass.data.get(DOMAIN, {}).get(
        entry.entry_id
    )
    if coordinator is not None and coordinator.state.bottle_size_ml == _size_ml(entry):
        return
    await hass.config_entries.async_reload(entry.entry_id)
