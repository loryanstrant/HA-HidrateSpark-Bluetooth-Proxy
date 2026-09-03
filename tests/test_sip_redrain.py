"""ble.py sip-notify resilience: the inline re-drain is bounded so a record the
firmware never pops can't trap us in a write loop, and implausible frames are
rejected rather than counted as real drinks.

The corpus replayed in RealFrameCorpusTest is the 13 frames actually logged by a
621 mL bottle between 2026-09-01 and 2026-09-03, one of which was corrupt and was
recorded as a 621 mL drink. Twelve must survive with their logged volumes intact;
the thirteenth must be dropped.

Self-contained: stubs the HA + bleak surface ble.py imports and drives
_on_data_notify directly. Run: python3 tests/test_sip_redrain.py
"""

import asyncio
import importlib.util
import inspect
import os
import sys
import time
import types
import unittest
from unittest.mock import AsyncMock


def _install_stubs():
    def mod(name):
        m = types.ModuleType(name)
        sys.modules[name] = m
        return m

    # Home Assistant surface (ble.py imports none directly, but const may be
    # pulled via the package; keep it minimal and safe).
    # bleak surface used only for type hints / establish_connection.
    bleak = mod("bleak")

    class BleakClient:  # stub
        ...

    bleak.BleakClient = BleakClient
    backends = mod("bleak.backends")
    device = mod("bleak.backends.device")

    class BLEDevice:  # stub
        ...

    device.BLEDevice = BLEDevice
    bleak.backends = backends
    backends.device = device

    brc = mod("bleak_retry_connector")

    class BleakClientWithServiceCache:  # stub
        ...

    async def establish_connection(*a, **k):  # stub
        raise RuntimeError("not used in unit tests")

    brc.BleakClientWithServiceCache = BleakClientWithServiceCache
    brc.establish_connection = establish_connection


def _load_ble():
    _install_stubs()
    base = os.path.join(
        os.path.dirname(__file__), "..", "custom_components", "hidratespark_bluetooth_proxy"
    )
    pkg = types.ModuleType("hsp")
    pkg.__path__ = [base]
    sys.modules["hsp"] = pkg

    def load(sub):
        spec = importlib.util.spec_from_file_location(f"hsp.{sub}", os.path.join(base, f"{sub}.py"))
        m = importlib.util.module_from_spec(spec)
        sys.modules[f"hsp.{sub}"] = m
        spec.loader.exec_module(m)
        return m

    load("const")
    return load("ble")


ble = _load_ble()
MAX = __import__("sys").modules["hsp.const"].MAX_IDENTICAL_SIP_FRAMES


class FakeClient:
    """Counts re-drain writes; always reports connected."""

    def __init__(self):
        self.writes = 0
        self.is_connected = True

    async def write_gatt_char(self, *a, **k):
        self.writes += 1


def make_client(size_ml=946):
    kwargs = dict(
        size_ml=size_ml,
        on_sip=AsyncMock(),
        on_battery=AsyncMock(),
        on_status=AsyncMock(),
        on_refill=AsyncMock(),
        on_weight=AsyncMock(),
        ble_device_provider=lambda: None,
    )
    # anchor_exists exists only once the 32oz weight branch is merged in.
    if "anchor_exists" in inspect.signature(ble.BottleClient.__init__).parameters:
        kwargs["anchor_exists"] = lambda: False
    c = ble.BottleClient("AA:BB:CC:DD:EE:FF", "bottle", **kwargs)
    c._connected = True
    c._client = FakeClient()
    c._data_char = "data-char-uuid"
    return c


class ReDrainBoundTest(unittest.TestCase):
    def test_redrain_stops_after_max_identical_frames(self):
        c = make_client()
        frame = bytearray([1, 0])  # remaining=1, short (no sip parsed)

        async def go():
            for _ in range(MAX + 3):
                await c._on_data_notify(None, frame)

        asyncio.run(go())
        self.assertEqual(
            c._client.writes, MAX,
            f"re-drain must stop after {MAX} identical frames, got {c._client.writes}",
        )
        self.assertEqual(c._on_sip.await_count, 0, "short frames must not emit sips")


def frame(pct, total_pct, weight_before=None, weight_after=None, remaining=1):
    """Build a sip frame. Omitting the weights yields a short (9-byte) frame,
    which is how a test opts out of the in-frame weight cross-check."""
    body = bytearray([remaining, pct]) + total_pct.to_bytes(2, "little")
    body += bytearray([4, 0, 0, 0, 0x75])
    if weight_before is None:
        return body
    body += bytearray([0x87, 0x2a, 0x8b])
    body += weight_before.to_bytes(2, "little") + weight_after.to_bytes(2, "little")
    return body + bytearray(4)


def drive(client, frames):
    """Feed frames in order; return the volumes emitted as genuine sips.

    Rejected frames are still handed to the sip callback, flagged, so they can
    reach the dedup window — they just aren't counted. `emitted_rejected` picks
    those out instead.
    """

    async def go():
        for f in frames:
            await client._on_data_notify(None, f)

    asyncio.run(go())
    return [c.args[1] for c in client._on_sip.await_args_list if not c.args[3]]


def emitted_rejected(client):
    return [c.args[1] for c in client._on_sip.await_args_list if c.args[3]]


# The real corpus, oldest first: (raw hex, expected volume or None if it must be
# rejected). Volumes are exactly what the bottle's own logs recorded.
CORPUS = [
    ("0119ea000400000075872a8bd789e78800000000", 155),
    ("011903010800000075872a8be788f28700000000", 155),
    ("01090c010100000075872a8bf2879b8700000000", 56),
    ("010d0d000400000075872a8bda8a5a8a00000000", 81),
    ("010916000400000075872a8b5a8a018a00000000", 56),
    ("012f45000400000075872a8b018a428800000000", 292),
    ("01094e000400000075872a8b7d891f8900000000", 56),
    ("011361000100000075872a8b1f896a8800000000", 118),
    ("010465000400000075872a8b6a88408800000000", 25),
    ("010e73000100000075872a8b4088b38700000000", 87),
    ("010679000400000075872a8bb387568700000000", 37),
    # 2026-09-03 00:57 — the corrupt one. pct byte carries 0x64, claiming a
    # 621 mL drink, while the frame's own weight bytes fell by only 31 raw units.
    ("016464000400000075872a8b5687378700000000", None),
    ("0120f1000400000075872a8bd08a9d8900000000", 199),
]

BAD_FRAME_HEX = "016464000400000075872a8b5687378700000000"


class RealFrameCorpusTest(unittest.TestCase):
    def test_corpus_replays_with_only_the_corrupt_frame_dropped(self):
        c = make_client(size_ml=621)
        emitted = drive(c, [bytearray.fromhex(h) for h, _ in CORPUS])
        self.assertEqual(
            emitted,
            [v for _, v in CORPUS if v is not None],
            "every genuine frame must survive with its logged volume, and only "
            "the corrupt one may be dropped",
        )

    def test_the_621ml_regression_frame_is_rejected_on_its_own(self):
        c = make_client(size_ml=621)
        self.assertEqual(drive(c, [bytearray.fromhex(BAD_FRAME_HEX)]), [])

    def test_rejected_frame_still_reaches_the_dedup_window(self):
        # It must be handed over flagged, not dropped on the floor: the dedup
        # window is what stops the next drain replaying it as a fresh drink.
        c = make_client(size_ml=621)
        drive(c, [bytearray.fromhex(BAD_FRAME_HEX)])
        self.assertEqual(emitted_rejected(c), [621])


class PctGuardTest(unittest.TestCase):
    def test_full_bottle_in_one_sip_is_rejected(self):
        self.assertEqual(drive(make_client(), [frame(pct=100, total_pct=100)]), [])

    def test_large_but_possible_sip_is_accepted(self):
        self.assertEqual(drive(make_client(), [frame(pct=99, total_pct=99)]), [937])


class MonotonicTotalTest(unittest.TestCase):
    def test_backwards_total_is_rejected(self):
        c = make_client()
        self.assertEqual(
            drive(c, [frame(pct=10, total_pct=200), frame(pct=10, total_pct=50)]),
            [95],
            "a total that drops without looking like a reset must be rejected",
        )

    def test_daily_reset_is_accepted(self):
        c = make_client()
        # After a reset the first record carries total == pct.
        self.assertEqual(
            drive(c, [frame(pct=47, total_pct=268), frame(pct=47, total_pct=47)]),
            [445, 445],
        )

    def test_anchor_is_reset_after_a_rejection(self):
        c = make_client()
        # Without re-anchoring, the stale 200 would reject every later frame
        # for the rest of the day.
        self.assertEqual(
            drive(
                c,
                [
                    frame(pct=10, total_pct=200),
                    frame(pct=10, total_pct=50),  # rejected
                    frame(pct=10, total_pct=60),  # must recover
                ],
            ),
            [95, 95],
        )


class WeightCrossCheckTest(unittest.TestCase):
    def test_volume_far_beyond_the_weight_drop_is_rejected(self):
        # The real corrupt frame's weights, with pct dropped to 99 so the
        # percent ceiling can't be what rejects it.
        c = make_client(size_ml=621)
        f = frame(pct=99, total_pct=99, weight_before=34646, weight_after=34615)
        self.assertEqual(drive(c, [f]), [])

    def test_weight_consistent_sip_is_accepted(self):
        c = make_client(size_ml=621)
        f = frame(pct=47, total_pct=47, weight_before=35329, weight_after=34882)
        self.assertEqual(drive(c, [f]), [292])

    def test_check_fails_open_on_unusable_weights(self):
        for label, before, after in (
            ("zero pair", 0, 0),
            ("zero after", 34646, 0),
            ("weight rose", 34615, 34646),
            ("no change", 34646, 34646),
        ):
            with self.subTest(label):
                c = make_client(size_ml=621)
                f = frame(pct=99, total_pct=99, weight_before=before, weight_after=after)
                self.assertEqual(
                    drive(c, [f]), [615],
                    "undocumented weight bytes must never reject a frame when "
                    "they are unusable",
                )


class DecodeTest(unittest.TestCase):
    def test_running_total_is_little_endian(self):
        c = make_client()
        asyncio.run(c._on_data_notify(None, bytearray.fromhex(CORPUS[3][0])))
        self.assertEqual(c._on_sip.await_args.args[2], 13, "0x000d LE, not 0x0d00 BE")

    def test_sip_is_stamped_at_arrival_not_backdated(self):
        c = make_client(size_ml=621)
        before = time.time()
        asyncio.run(c._on_data_notify(None, bytearray.fromhex(CORPUS[0][0])))
        ts = c._on_sip.await_args.args[0]
        self.assertGreaterEqual(
            ts, before,
            "sips must be stamped on arrival; the old code subtracted a bogus "
            "fixed 117 s, which is the ~2 minute lag users reported",
        )
        self.assertLessEqual(ts, time.time())


if __name__ == "__main__":
    unittest.main(verbosity=2)
