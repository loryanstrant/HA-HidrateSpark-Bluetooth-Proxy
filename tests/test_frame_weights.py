"""Fill, refills and scale driven by the sip frame's own weight bytes (#11).

Replays the 12 genuine frames from the 621 mL bottle corpus (see
test_sip_redrain.py) through BottleState. Their before/after pairs contain three
refills between sips (+831, +315 and +890 raw) and imply ~1.59 raw/mL, not the
1.305 measured on a 32oz bottle.

Run: python3 tests/test_frame_weights.py
"""

import asyncio
import unittest

from ha_stub import HomeAssistant, load_state

state = load_state()
Sip = state.Sip

GOOD_FRAMES = [
    "0119ea000400000075872a8bd789e78800000000",
    "011903010800000075872a8be788f28700000000",
    "01090c010100000075872a8bf2879b8700000000",
    "010d0d000400000075872a8bda8a5a8a00000000",  # refill before this one
    "010916000400000075872a8b5a8a018a00000000",
    "012f45000400000075872a8b018a428800000000",
    "01094e000400000075872a8b7d891f8900000000",  # top-up before this one
    "011361000100000075872a8b1f896a8800000000",
    "010465000400000075872a8b6a88408800000000",
    "010e73000100000075872a8b4088b38700000000",
    "010679000400000075872a8bb387568700000000",
    "0120f1000400000075872a8bd08a9d8900000000",  # refill before this one
]


def decode(hexframe, size_ml=621):
    b = bytes.fromhex(hexframe)
    return (
        round(size_ml * b[1] / 100),
        int.from_bytes(b[12:14], "little"),
        int.from_bytes(b[14:16], "little"),
    )


def replay(b, frames=GOOD_FRAMES, start_ts=1_000_000.0):
    counted = []
    for i, h in enumerate(frames):
        vol, before, after = decode(h, b.bottle_size_ml)
        sip = Sip(timestamp=start_ts + 600 * i, volume_ml=vol)
        counted.append(
            b.add_sip(sip, weight_before=before, weight_after=after)
        )
    return counted


def bottle(size=621):
    return state.BottleState(HomeAssistant(), "entry", size)


class LearnedScaleTest(unittest.TestCase):
    def test_default_until_enough_volume_seen(self):
        b = bottle()
        self.assertEqual(b.raw_per_ml, state.RAW_UNITS_PER_ML)
        replay(b, GOOD_FRAMES[:1])  # 155 mL < threshold
        self.assertEqual(b.raw_per_ml, state.RAW_UNITS_PER_ML)

    def test_corpus_learns_about_1_59(self):
        b = bottle()
        replay(b)
        self.assertAlmostEqual(b.raw_per_ml, 1.59, delta=0.03)

    def test_out_of_bounds_sample_is_ignored(self):
        b = bottle()
        b.add_sip(Sip(1.0, 100), weight_before=40000, weight_after=39000)  # 10/mL
        self.assertEqual(b.raw_per_ml, state.RAW_UNITS_PER_ML)


class RefillFromFramesTest(unittest.TestCase):
    def test_three_refills_between_sips_are_detected(self):
        b = bottle()
        self.assertTrue(all(replay(b)), "every genuine sip is still counted")
        self.assertEqual(b.refills_today, 3)

    def test_consecutive_frames_are_not_refills(self):
        b = bottle()
        replay(b, GOOD_FRAMES[:3])
        self.assertEqual(b.refills_today, 0)

    def test_cap_close_refill_is_not_counted_twice(self):
        b = bottle()
        replay(b, GOOD_FRAMES[:3])
        # The cap-close path saw the refill and anchored on the new weight.
        _, before, _ = decode(GOOD_FRAMES[3])
        b.refill("cap_close", before)
        replay(b, GOOD_FRAMES[3:4], start_ts=2_000_000.0)
        self.assertEqual(b.refills_today, 1)

    def test_cap_close_after_a_frame_refill_is_not_counted_twice(self):
        # The frame shows the refill first; the cap-close check settles later.
        b = bottle()
        replay(b, GOOD_FRAMES[:4])
        self.assertEqual(b.refills_today, 1)
        fill = b.current_fill_ml
        b.refill("cap_close", decode(GOOD_FRAMES[3])[1])
        self.assertEqual(b.refills_today, 1)
        self.assertEqual(b.current_fill_ml, fill, "the sip since stays drunk")

    def test_impossible_weight_jump_is_not_a_refill(self):
        b = bottle()
        replay(b, GOOD_FRAMES[:1])
        b.add_sip(Sip(9.0, 30), weight_before=65000, weight_after=64950)
        self.assertEqual(b.refills_today, 0)

    def test_rejected_frames_leave_weight_state_alone(self):
        b = bottle()
        replay(b, GOOD_FRAMES[:1])
        b.add_sip(Sip(5_000_000.0, 621), rejected=True,
                  weight_before=60000, weight_after=59000)
        self.assertEqual(b.refills_today, 0)
        self.assertEqual(b._last_frame_weight_after, decode(GOOD_FRAMES[0])[2])

    def test_frames_without_weights_change_nothing(self):
        b = bottle()
        b.add_sip(Sip(1.0, 50))
        self.assertIsNone(b._last_frame_weight_after)
        self.assertEqual(b.refills_today, 0)

    def test_first_sip_after_unseen_refill_is_not_rejected(self):
        # Drink it nearly dry, refill away from home (no cap event), then sip.
        b = bottle()
        b.update_fill_from_weight(35287)  # anchor
        b.add_sip(Sip(1.0, 600), weight_before=35287, weight_after=34330)
        self.assertLess(b.current_fill_ml, 100)
        counted = b.add_sip(Sip(2.0, 300), weight_before=35300, weight_after=34820)
        self.assertTrue(counted, "the refill is seen first, so the sip fits")
        self.assertEqual(b.refills_today, 1)


class FillFromFramesTest(unittest.TestCase):
    def test_fill_follows_each_frames_after_weight(self):
        b = bottle()
        b.update_fill_from_weight(decode(GOOD_FRAMES[0])[1])  # anchor
        replay(b, GOOD_FRAMES[:3])
        self.assertEqual(b.weight_raw, decode(GOOD_FRAMES[2])[2])

    def test_frames_never_bootstrap_the_full_anchor(self):
        # Without an anchor, a replayed old sip's weight must not be adopted as
        # "full" (which would also report the bottle as brim full).
        b = bottle()
        b.add_sip(Sip(1.0, 155), weight_before=35287, weight_after=35047)
        self.assertIsNone(b.weight_full_raw)
        self.assertEqual(b.current_fill_ml, 621 - 155)

    def test_fill_after_a_refill_is_full_minus_the_sip(self):
        b = bottle()
        replay(b, GOOD_FRAMES[:6])  # learns the scale
        # Frame 6 follows an unseen top-up: the refill re-anchors "full" at the
        # frame's before-weight, so fill must land at full minus this sip.
        vol, before, after = decode(GOOD_FRAMES[6])
        b.add_sip(Sip(9_000_000.0, vol), weight_before=before, weight_after=after)
        self.assertAlmostEqual(b.current_fill_ml, 621 - vol, delta=10)


class PersistenceTest(unittest.TestCase):
    def test_scale_and_last_weight_survive_restart(self):
        b = bottle()
        replay(b)
        asyncio.run(b.async_save())
        b2 = bottle()
        b2._store = b._store
        asyncio.run(b2.async_load())
        self.assertAlmostEqual(b2.raw_per_ml, b.raw_per_ml)
        self.assertEqual(b2._last_frame_weight_after, b._last_frame_weight_after)


if __name__ == "__main__":
    unittest.main(verbosity=2)
