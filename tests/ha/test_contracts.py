"""Migration, registry, options and discovery contracts with real HA objects."""

from unittest.mock import AsyncMock, patch

from bleak.backends.device import BLEDevice
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hidratespark_bluetooth_proxy import async_migrate_entry
from custom_components.hidratespark_bluetooth_proxy.const import DOMAIN
from custom_components.hidratespark_bluetooth_proxy.coordinator import HidrateSparkCoordinator
from custom_components.hidratespark_bluetooth_proxy.entity import HidrateSparkEntity

NAME = "h2o00003158"
OLD = "AA:AA:AA:AA:AA:01"
NEW = "BB:BB:BB:BB:BB:02"


def entry(hass, **kwargs):
    defaults = dict(domain=DOMAIN, title=NAME, unique_id=OLD,
                    data={"address": OLD}, options={"size_ml": 621},
                    version=1, minor_version=1)
    config = MockConfigEntry(**(defaults | kwargs))
    config.add_to_hass(hass)
    return config


def advert(address=NEW, name=NAME):
    return BluetoothServiceInfoBleak(
        name=name, address=address, rssi=-60, manufacturer_data={},
        service_data={}, service_uuids=[], source="local", connectable=True,
        time=1, tx_power=None, device=BLEDevice(address, name, {}), advertisement=None,
    )


async def test_migration_preserves_device_entity_and_storage_key(hass):
    config = entry(hass)
    registry = dr.async_get(hass)
    device = registry.async_get_or_create(config_entry_id=config.entry_id,
                                         identifiers={(DOMAIN, OLD)})
    entities = er.async_get(hass)
    sensor = entities.async_get_or_create("sensor", DOMAIN, f"{OLD}_water_today",
                                         config_entry=config, device_id=device.id)
    assert await async_migrate_entry(hass, config)
    coordinator = HidrateSparkCoordinator(hass, config, config.data["address"], NAME, 621)
    coordinator._follow_address(NEW)
    entity = HidrateSparkEntity(coordinator, "water_today")
    assert config.unique_id == NAME
    assert config.minor_version == 2
    assert entity.unique_id == sensor.unique_id
    assert entity.device_info["identifiers"] == {(DOMAIN, OLD)}
    assert entities.async_get(sensor.entity_id).device_id == device.id
    assert coordinator.state._store.key == f"hidratespark_{config.entry_id}"
    assert config.data["address"] == NEW
    assert coordinator.device_key == OLD
    before = dict(config.data)
    assert await async_migrate_entry(hass, config)
    assert config.data == before


async def test_renamed_entry_migration_uses_stored_advertised_name(hass):
    config = entry(hass, title="Kitchen bottle", data={"address": OLD, "local_name": NAME})
    assert await async_migrate_entry(hass, config)
    assert config.unique_id == NAME
    assert config.data["device_key"] == OLD


async def test_metadata_received_before_device_registration_is_applied(hass):
    config = entry(hass)
    coordinator = HidrateSparkCoordinator(hass, config, OLD, NAME, 621)
    info = {"model": "PRO", "sw_version": "80.18"}
    await coordinator._handle_device_info(info)
    registry = dr.async_get(hass)
    device = registry.async_get_or_create(config_entry_id=config.entry_id,
                                         identifiers={(DOMAIN, OLD)})
    entity = HidrateSparkEntity(coordinator, "water_today")
    entity.hass = hass
    with patch.object(entity, "async_on_remove"):
        await entity.async_added_to_hass()
    assert registry.async_get(device.id).model == "PRO"
    assert registry.async_get(device.id).sw_version == "80.18"


async def test_options_flow_uses_real_injected_entry(hass):
    config = entry(hass)
    result = await hass.config_entries.options.async_init(config.entry_id)
    assert result["type"] == "form"
    assert result["step_id"] == "init"
    done = await hass.config_entries.options.async_configure(result["flow_id"],
                                                           {"size_ml": 946})
    assert done["type"] == "create_entry"
    assert config.options["size_ml"] == 946


async def test_options_setup_does_not_use_runner_hardware(hass):
    """Keep real HA flows working on a Linux runner with no USB/BLE access."""
    config = entry(hass)
    with (
        patch("homeassistant.components.usb.system_info.async_get_system_info",
              new=AsyncMock(return_value={"docker": False})),
        patch("btsocket.btmgmt_socket.open",
              side_effect=AssertionError("real Bluetooth socket attempted")) as socket,
    ):
        result = await hass.config_entries.options.async_init(config.entry_id)
    assert result["type"] == "form"
    assert result["step_id"] == "init"
    socket.assert_not_called()


async def test_rediscovery_updates_address_without_reload(hass):
    config = entry(hass)
    assert await async_migrate_entry(hass, config)
    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "bluetooth"}, data=advert())
    assert result["type"] == "abort"
    assert result["reason"] == "already_configured"
    assert config.data["address"] == NEW
    reload.assert_not_called()


async def test_manual_address_does_not_duplicate_migrated_entry(hass):
    config = entry(hass)
    assert await async_migrate_entry(hass, config)
    with patch("custom_components.hidratespark_bluetooth_proxy.config_flow.async_discovered_service_info",
               return_value=[]):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
        done = await hass.config_entries.flow.async_configure(result["flow_id"],
                                                            {"address": OLD, "size_ml": 621})
    assert done["type"] == "abort"
    assert done["reason"] == "already_configured"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_coordinator_registers_and_unsubscribes_matchers(hass):
    config = entry(hass)
    assert await async_migrate_entry(hass, config)
    coordinator = HidrateSparkCoordinator(hass, config, OLD, NAME, 621)
    with patch("custom_components.hidratespark_bluetooth_proxy.coordinator.bluetooth.async_register_callback") as register, \
         patch("custom_components.hidratespark_bluetooth_proxy.coordinator.BottleClient.run", new_callable=AsyncMock):
        await coordinator.async_start()
        assert register.call_count == 2
        await coordinator.async_stop()
        assert register.return_value.call_count == 2
