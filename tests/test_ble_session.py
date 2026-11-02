"""BLE session handling for newer bottles (PRO 2): the data path is picked from
the GATT table the bottle exposes, a session that stops answering or goes
silent is rebuilt, model/firmware are read, and sip weights reach the
coordinator.

Run: python3 tests/test_ble_session.py
"""

import asyncio
import sys
import types
import unittest
from unittest.mock import AsyncMock

import ha_full_stub as stub

stub.load_package()
ble = sys.modules["hsp.ble"]
const = sys.modules["hsp.const"]


class FakeServices:
    def __init__(self, uuids):
        self._uuids = {u.lower() for u in uuids}

    def get_characteristic(self, uuid):
        return object() if uuid.lower() in self._uuids else None

    def __iter__(self):
        return iter([])


class FakeGatt:
    def __init__(self, uuids, reads=None, fail_writes=False):
        self.services = FakeServices(uuids)
        self.reads = reads or {}
        self.fail_writes = fail_writes
        self.is_connected = True
        self.writes = []
        self.notifying = []
        self.cache_cleared = False

    async def read_gatt_char(self, uuid):
        if uuid not in self.reads:
            raise RuntimeError("not readable")
        return bytearray(self.reads[uuid])

    async def write_gatt_char(self, uuid, data, response=True):
        if self.fail_writes:
            raise RuntimeError("write failed")
        self.writes.append((uuid, bytes(data)))

    async def start_notify(self, uuid, cb):
        if self.services.get_characteristic(uuid) is None:
            raise RuntimeError(f"no such characteristic {uuid}")
        self.notifying.append(uuid)

    async def clear_cache(self):
        self.cache_cleared = True


def make_client(**extra):
    c = ble.BottleClient(
        "AA:BB:CC:DD:EE:FF",
        "h2o00003158",
        size_ml=621,
        on_sip=AsyncMock(),
        on_battery=AsyncMock(),
        on_status=AsyncMock(),
        on_refill=AsyncMock(),
        on_weight=AsyncMock(),
        ble_device_provider=lambda: None,
        anchor_exists=lambda: False,
        **extra,
    )
    c._connected = True
    return c


ALL_MODERN = [
    const.CHAR_USER_DATA, const.CHAR_SET_POINT, const.CHAR_DEBUG,
    const.CHAR_WEIGHT, const.CHAR_BATTERY_LEVEL,
]


class PathSelectionTest(unittest.TestCase):
    def test_modern_path_when_user_data_present(self):
        c, g = make_client(), FakeGatt(ALL_MODERN)
        asyncio.run(c._after_connect(g))
        self.assertEqual(c.handshake_path, "modern")
        self.assertEqual(len(g.writes), len(const.HANDSHAKE_COMMANDS) + 1)  # + drain

    def test_handshake_skips_characteristics_the_bottle_lacks(self):
        c = make_client()
        g = FakeGatt([const.CHAR_USER_DATA, const.CHAR_SET_POINT])  # no DEBUG
        asyncio.run(c._after_connect(g))
        self.assertEqual(c.handshake_path, "modern")
        self.assertNotIn(const.CHAR_DEBUG, {u for u, _ in g.writes})

    def test_user_data_without_handshake_chars_falls_back_to_legacy(self):
        # USER_DATA alone never streams; it must not be declared "modern".
        c = make_client()
        g = FakeGatt([const.CHAR_USER_DATA, const.CHAR_DATA_POINT])
        asyncio.run(c._after_connect(g))
        self.assertEqual(c.handshake_path, "legacy")

    def test_legacy_path_without_user_data(self):
        c, g = make_client(), FakeGatt([const.CHAR_DATA_POINT])
        asyncio.run(c._after_connect(g))
        self.assertEqual(c.handshake_path, "legacy")
        self.assertEqual(g.writes, [(const.CHAR_DATA_POINT, const.DRAIN_BYTE)])

    def test_no_data_char_clears_the_service_cache_and_raises(self):
        c, g = make_client(), FakeGatt([const.CHAR_BATTERY_LEVEL])
        with self.assertRaises(RuntimeError):
            asyncio.run(c._after_connect(g))
        self.assertTrue(g.cache_cleared)
        self.assertIsNone(c.handshake_path)


class DeviceInfoTest(unittest.TestCase):
    def test_model_and_firmware_are_decoded_and_reported_once(self):
        on_info = AsyncMock()
        c = make_client(on_device_info=on_info)
        g = FakeGatt(
            ALL_MODERN + [const.CHAR_MODEL_NUMBER, const.CHAR_FIRMWARE_REV],
            reads={
                const.CHAR_MODEL_NUMBER: b"PRO2\x00\x00",
                const.CHAR_FIRMWARE_REV: b" 2.1.4 ",
                const.CHAR_BATTERY_LEVEL: b"\x50",
            },
        )
        asyncio.run(c._after_connect(g))
        asyncio.run(c._after_connect(g))
        on_info.assert_awaited_once_with({"model": "PRO2", "sw_version": "2.1.4"})

    def test_missing_device_info_service_is_harmless(self):
        on_info = AsyncMock()
        c = make_client(on_device_info=on_info)
        asyncio.run(c._after_connect(FakeGatt(ALL_MODERN)))
        on_info.assert_not_awaited()
        self.assertEqual(c.handshake_path, "modern")


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def monotonic(self):
        return self.t


class HoldSessionTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self._orig_time = ble.time
        ble.time = types.SimpleNamespace(monotonic=self.clock.monotonic, time=lambda: 0)

    def tearDown(self):
        ble.time = self._orig_time

    def _hold(self, c, g, step, max_iter=1000):
        """Run _hold_session, advancing the clock `step` s per loop tick."""
        ticks = {"n": 0}

        async def sleep(_s):
            ticks["n"] += 1
            self.clock.t += step
            if ticks["n"] > max_iter:
                g.is_connected = False

        c._sleep_or_wake = sleep
        asyncio.run(c._hold_session(g))
        return ticks["n"]

    def test_keepalive_failures_end_the_session(self):
        c = make_client()
        c._data_char = const.CHAR_USER_DATA
        g = FakeGatt(ALL_MODERN, fail_writes=True)
        ticks = self._hold(c, g, step=const.KEEPALIVE_INTERVAL_S)
        self.assertLess(ticks, 10, "must give up after a couple of failed probes")

    def test_silent_session_is_rebuilt(self):
        c = make_client()
        c._data_char = const.CHAR_USER_DATA
        g = FakeGatt(ALL_MODERN)
        ticks = self._hold(c, g, step=60.0)
        self.assertEqual(ticks, int(const.SESSION_STALE_S // 60))
        self.assertTrue(g.writes, "keepalive drains were sent meanwhile")

    def test_notifications_keep_the_session_alive(self):
        c = make_client()
        c._data_char = const.CHAR_USER_DATA
        g = FakeGatt(ALL_MODERN)

        async def sleep(_s, ticks={"n": 0}):
            ticks["n"] += 1
            self.clock.t += 60.0
            await c._on_battery_notify(None, bytearray([80]))
            if ticks["n"] > 100:
                g.is_connected = False

        c._sleep_or_wake = sleep
        asyncio.run(c._hold_session(g))
        self.assertGreater(self.clock.t, const.SESSION_STALE_S)


class SipWeightsTest(unittest.TestCase):
    def test_frame_weights_are_passed_to_the_coordinator(self):
        c = make_client()
        c._client = FakeGatt(ALL_MODERN)
        c._data_char = const.CHAR_USER_DATA
        frame = bytearray.fromhex("0119ea000400000075872a8bd789e78800000000")
        asyncio.run(c._on_data_notify(None, frame))
        args = c._on_sip.await_args.args
        self.assertEqual(args[1], 155)
        self.assertEqual((args[4], args[5]), (35287, 35047))


if __name__ == "__main__":
    unittest.main(verbosity=2)
