"""Bottle identity: options flow (#9), name-based unique ID and following a
rotating BLE address (#10), and the 1.1 -> 1.2 config entry migration.

Run: python3 tests/test_identity.py
"""

import asyncio
import sys
import unittest

import ha_full_stub as stub

pkg = stub.load_package()
config_flow = sys.modules["hsp.config_flow"]
coordinator_mod = sys.modules["hsp.coordinator"]
const = sys.modules["hsp.const"]

NAME = "h2o00003158"
OLD = "AA:AA:AA:AA:AA:01"
NEW = "BB:BB:BB:BB:BB:02"


class OptionsFlowTest(unittest.TestCase):
    def test_options_flow_builds_without_passing_the_entry(self):
        # Issue #9: HA >= 2024.12's OptionsFlow takes no constructor args.
        flow = config_flow.HidrateSparkConfigFlow.async_get_options_flow(
            stub.ConfigEntry()
        )
        self.assertIsInstance(flow, config_flow.HidrateSparkOptionsFlow)


class StableIdTest(unittest.TestCase):
    def test_advertised_name_wins(self):
        self.assertEqual(config_flow.stable_id(NAME, OLD.lower()), NAME)

    def test_falls_back_to_upper_address(self):
        self.assertEqual(config_flow.stable_id(None, OLD.lower()), OLD)

    def test_address_masquerading_as_name_is_not_used(self):
        # HA reports the address as the name when none was advertised.
        self.assertEqual(config_flow.stable_id(OLD, OLD), OLD)


class BluetoothDiscoveryTest(unittest.TestCase):
    def test_rediscovery_under_new_address_updates_existing_entry(self):
        flow = config_flow.HidrateSparkConfigFlow()
        info = stub.ServiceInfo(NAME, NEW.lower(), service_uuids=[const.SERVICE_REF])
        asyncio.run(flow.async_step_bluetooth(info))
        self.assertEqual(flow.unique_id, NAME)
        self.assertEqual(flow.abort_updates, {"address": NEW})
        # The coordinator follows by name; HA must not reload a live session.
        self.assertFalse(flow.reload_on_update)

    def test_pro2_advertising_only_the_user_service_is_accepted(self):
        flow = config_flow.HidrateSparkConfigFlow()
        info = stub.ServiceInfo(NAME, NEW, service_uuids=[const.SERVICE_USER])
        result = asyncio.run(flow.async_step_bluetooth(info))
        self.assertNotIn(result.get("reason"), ("not_supported", "name_unavailable"))

    def test_nameless_advert_does_not_offer_a_new_bottle(self):
        # A known bottle's first advert at a rotated address may lack its name.
        flow = config_flow.HidrateSparkConfigFlow()
        info = stub.ServiceInfo(None, NEW, service_uuids=[const.SERVICE_USER])
        result = asyncio.run(flow.async_step_bluetooth(info))
        self.assertEqual(result["reason"], "name_unavailable")

    def test_unrelated_device_is_rejected(self):
        flow = config_flow.HidrateSparkConfigFlow()
        result = asyncio.run(
            flow.async_step_bluetooth(stub.ServiceInfo("Fridge", NEW))
        )
        self.assertEqual(result["reason"], "not_supported")

    def test_new_entry_records_a_fixed_device_key(self):
        flow = config_flow.HidrateSparkConfigFlow()
        info = stub.ServiceInfo(NAME, NEW.lower(), service_uuids=[const.SERVICE_REF])
        asyncio.run(flow.async_step_bluetooth(info))
        result = asyncio.run(flow.async_step_bluetooth_confirm({"size_ml": 621}))
        self.assertEqual(result["data"][const.CONF_DEVICE_KEY], NEW)
        self.assertEqual(result["data"]["address"], NEW)


class MigrationTest(unittest.TestCase):
    def _migrate(self, entry, others=()):
        hass = stub.FakeHass()
        hass.config_entries.entries = [entry, *others]
        self.assertTrue(asyncio.run(pkg.async_migrate_entry(hass, entry)))
        return entry

    def test_address_keyed_entry_moves_to_name(self):
        e = self._migrate(
            stub.ConfigEntry(title=NAME, unique_id=OLD, data={"address": OLD})
        )
        self.assertEqual(e.unique_id, NAME)
        self.assertEqual(e.minor_version, 2)
        # Existing entities are keyed on the old address and must keep it.
        self.assertEqual(e.data[const.CONF_DEVICE_KEY], OLD)
        self.assertEqual(e.data[const.CONF_LOCAL_NAME], NAME)

    def test_renamed_entry_still_follows_its_stored_name(self):
        entry = stub.ConfigEntry(
            title="Kitchen bottle", unique_id=NAME,
            data={"address": OLD, const.CONF_LOCAL_NAME: NAME},
        )
        c = coordinator_mod.HidrateSparkCoordinator(
            stub.FakeHass(), entry, OLD, "Kitchen bottle", 621
        )
        self.assertEqual(c.local_name, NAME)

    def test_entry_without_a_name_keeps_its_address(self):
        e = self._migrate(
            stub.ConfigEntry(title=OLD, unique_id=OLD, data={"address": OLD})
        )
        self.assertEqual(e.unique_id, OLD)

    def test_clash_with_a_ghost_duplicate_does_not_steal_its_id(self):
        ghost = stub.ConfigEntry(entry_id="ghost", title=NAME, unique_id=NAME)
        e = self._migrate(
            stub.ConfigEntry(title=NAME, unique_id=OLD, data={"address": OLD}),
            others=[ghost],
        )
        self.assertEqual(e.unique_id, OLD)
        self.assertEqual(e.minor_version, 2)


class FollowAddressTest(unittest.TestCase):
    def setUp(self):
        stub.BT.by_address.clear()
        stub.BT.discovered.clear()
        self.hass = stub.FakeHass()
        self.entry = stub.ConfigEntry(
            title=NAME,
            unique_id=NAME,
            data={"address": OLD, const.CONF_DEVICE_KEY: OLD},
        )
        self.c = coordinator_mod.HidrateSparkCoordinator(
            self.hass, self.entry, OLD, NAME, 621
        )

    def test_lookup_by_name_after_rotation(self):
        stub.BT.discovered.append(stub.ServiceInfo(NAME, NEW, rssi=-70))
        stub.BT.discovered.append(stub.ServiceInfo("h2oSOMEONEELSE", "CC:CC", rssi=-30))
        device = self.c._get_ble_device()
        self.assertEqual(device.address, NEW)
        self.assertEqual(self.c.address, NEW)
        self.assertEqual(self.entry.data["address"], NEW, "new address persisted")
        self.assertEqual(self.c.device_key, OLD, "device key never follows")

    def test_known_address_is_used_directly(self):
        stub.BT.by_address[OLD] = "dev"
        self.assertEqual(self.c._get_ble_device(), "dev")
        self.assertEqual(self.hass.config_entries.updates, [])

    def test_advert_from_new_address_retargets_while_disconnected(self):
        self.c._on_advertisement(stub.ServiceInfo(NAME, NEW.lower()), None)
        self.assertEqual(self.c.address, NEW)

    def test_advert_never_retargets_a_live_session(self):
        self.c._connected = True
        self.c._on_advertisement(stub.ServiceInfo(NAME, NEW), None)
        self.assertEqual(self.c.address, OLD)

    def test_address_only_entry_has_no_name_to_follow(self):
        entry = stub.ConfigEntry(title=OLD, unique_id=OLD, data={"address": OLD})
        c = coordinator_mod.HidrateSparkCoordinator(self.hass, entry, OLD, OLD, 621)
        self.assertIsNone(c.local_name)
        stub.BT.discovered.append(stub.ServiceInfo(NAME, NEW))
        self.assertIsNone(c._get_ble_device())


class EntryUpdateListenerTest(unittest.TestCase):
    def _run(self, size_now, size_option):
        hass = stub.FakeHass()
        entry = stub.ConfigEntry(options={"size_ml": size_option})
        hass.data[const.DOMAIN] = {
            entry.entry_id: type(
                "C", (), {"state": type("S", (), {"bottle_size_ml": size_now})()}
            )()
        }
        asyncio.run(pkg._async_entry_updated(hass, entry))
        return hass.config_entries.reloads

    def test_address_update_does_not_reload(self):
        self.assertEqual(self._run(621, 621), [])

    def test_size_change_reloads(self):
        self.assertEqual(self._run(621, 946), ["e1"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
