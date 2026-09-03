"""16-bit weight decoding, auto-calibration, and weight-based fill tracking."""

import unittest

from ha_stub import HomeAssistant, load_state

state = load_state()
Sip = state.Sip


def new_bottle(size=946):
    return state.BottleState(HomeAssistant(), "entry", size)


class WeightCalibrationTest(unittest.TestCase):
    def test_first_reading_calibrates_full_anchor(self):
        b = new_bottle(946)
        changed = b.update_fill_from_weight(37000)
        self.assertTrue(changed)
        self.assertEqual(b.weight_full_raw, 37000)
        self.assertEqual(b.current_fill_ml, 946)

    def test_empty_bottle_reads_zero(self):
        # Measured full+empty calibration on a 946 mL bottle.
        b = new_bottle(946)
        b.update_fill_from_weight(37115)  # full anchor
        self.assertEqual(b.current_fill_ml, 946)
        b.update_fill_from_weight(35880)  # truly empty
        self.assertAlmostEqual(b.current_fill_ml, 0, delta=20)

    def test_fill_tracks_proportionally(self):
        b = new_bottle(946)
        b.update_fill_from_weight(37115)  # full
        # halfway down the raw span (1235 units) -> ~half the bottle drunk
        b.update_fill_from_weight(37115 - 1235 // 2)
        self.assertAlmostEqual(b.current_fill_ml, 473, delta=15)

    def test_fill_never_exceeds_full_or_goes_negative(self):
        b = new_bottle(946)
        b.update_fill_from_weight(37000)
        b.update_fill_from_weight(99000)  # implausibly heavy
        self.assertEqual(b.current_fill_ml, 946)
        b.update_fill_from_weight(0)  # implausibly light
        self.assertEqual(b.current_fill_ml, 0)

    def test_empty_reads_zero_after_a_partial_refill(self):
        # The key tare-anchoring win: once a real drain establishes the empty
        # floor, empty reads 0 even when a later fill doesn't reach the brim.
        b = new_bottle(946)
        b.update_fill_from_weight(37115)  # full anchor
        b.update_fill_from_weight(35880)  # drained to empty -> learns the tare
        self.assertAlmostEqual(b.current_fill_ml, 0, delta=20)
        # Refill only part-way (anchor moves, tare must not).
        b.refill("cap_close", 36800)
        b.update_fill_from_weight(36800)  # ~partial fill
        self.assertGreater(b.current_fill_ml, 600)
        self.assertLess(b.current_fill_ml, 800)
        # Drink it all again: still reads ~0, no phantom residual.
        b.update_fill_from_weight(35882)
        self.assertAlmostEqual(b.current_fill_ml, 0, delta=20)

    def test_calibration_is_not_a_refill_but_cap_close_is(self):
        b = new_bottle(946)
        b.refill("calibration", 37000)
        self.assertEqual(b.refills_today, 0, "calibration must not count as a refill")
        b.refill("cap_close", 37100)
        self.assertEqual(b.refills_today, 1, "a real refill increments the counter")


class SipExceedsFillTest(unittest.TestCase):
    """A sip bigger than the water in the bottle is not a drink.

    add_sip already treated that as an out-of-band refill, but only while there
    was no weight anchor. With an anchor the fill is known directly, so the same
    condition means the frame was corrupt — which is how a 621 mL phantom drink
    reached the totals on 2026-09-03.
    """

    def _anchored(self, fill_ml, size=621):
        """A bottle with a weight anchor set and a known fill — the exact state
        the guard reads, without going through tare learning."""
        b = new_bottle(size)
        b.weight_full_raw = 35540
        b.current_fill_ml = fill_ml
        return b

    def test_impossible_sip_is_rejected_when_anchored(self):
        b = self._anchored(506)  # what prod actually read at 00:55
        self.assertLess(b.current_fill_ml, 621)
        before = (b.lifetime_total_ml, b.total_today_ml, b.sips_today)

        accepted = b.add_sip(Sip(timestamp=1788000000.0, volume_ml=621))

        self.assertFalse(accepted, "a 621ml sip from a part-full bottle is impossible")
        self.assertEqual(
            (b.lifetime_total_ml, b.total_today_ml, b.sips_today), before,
            "a rejected sip must not touch lifetime, today's total or the count",
        )
        self.assertIsNone(b.last_sip, "a rejected sip must not become the last sip")

    def test_plausible_sip_still_counts_when_anchored(self):
        b = self._anchored(506)
        self.assertTrue(b.add_sip(Sip(timestamp=1788000000.0, volume_ml=200)))
        self.assertEqual(b.lifetime_total_ml, 200)

    def test_slack_allows_a_sip_taken_just_after_an_unseen_refill(self):
        # current_fill_ml lags an out-of-band refill by a reading or two, so a
        # sip slightly over the known fill must still be accepted.
        b = self._anchored(50)
        self.assertTrue(
            b.add_sip(Sip(timestamp=1788000000.0, volume_ml=100)),
            "100ml against a 50ml reading is inside the 10% (62ml) slack",
        )
        self.assertFalse(
            self._anchored(50).add_sip(Sip(timestamp=1788000000.0, volume_ml=200)),
            "200ml against a 50ml reading is well outside the slack",
        )

    def test_unanchored_path_still_infers_a_refill(self):
        b = new_bottle(621)
        b.current_fill_ml = 100
        self.assertTrue(b.add_sip(Sip(timestamp=1788000000.0, volume_ml=300)))
        self.assertEqual(b.current_fill_ml, 321, "bottle was refilled out of band")


class RejectedSipDedupTest(unittest.TestCase):
    """A rejected sip must stay in the dedup window.

    If it were simply dropped, the bottle's next drain would replay the same
    record, find no match, and count it as a brand-new drink — silently undoing
    the rejection. This is the same reason the phantom 621 mL entry was left in
    prod's `recent_sips` when its totals were corrected by hand on 2026-09-03.
    """

    def test_replayed_rejected_frame_is_not_counted_twice(self):
        b = new_bottle(621)
        b.update_fill_from_weight(35540)
        sip = Sip(timestamp=1788360910.15, volume_ml=621)

        self.assertFalse(b.add_sip(sip, rejected=True))
        self.assertEqual(b.lifetime_total_ml, 0)
        self.assertTrue(sip.rejected, "caller needs this to know it must persist")

        # The bottle re-sends the identical record moments later.
        replay = Sip(timestamp=1788360910.15 + 1, volume_ml=621)
        self.assertFalse(
            b.add_sip(replay),
            "the replay must be recognised as a duplicate, not counted as new",
        )
        self.assertEqual(b.lifetime_total_ml, 0)
        self.assertEqual(b.total_today_ml, 0)
        self.assertEqual(b.sips_today, 0)

    def test_rejected_sip_never_becomes_last_sip(self):
        b = new_bottle(621)
        b.add_sip(Sip(timestamp=1788000000.0, volume_ml=100))
        b.add_sip(Sip(timestamp=1788360910.15, volume_ml=621), rejected=True)
        self.assertEqual(b.last_sip.volume_ml, 100)

    def test_rejection_survives_a_restart(self):
        import asyncio

        async def go():
            b1 = new_bottle(621)
            b1.add_sip(Sip(timestamp=1788360910.15, volume_ml=621), rejected=True)
            await b1.async_save()

            b2 = new_bottle(621)
            b2._store._data = b1._store._data
            await b2.async_load()
            return b2

        b2 = asyncio.run(go())
        self.assertIsNone(b2.last_sip, "a phantom must not resurface as the last sip")
        self.assertFalse(
            b2.add_sip(Sip(timestamp=1788360910.15, volume_ml=621)),
            "the persisted window must still recognise the replay after a restart",
        )
        self.assertEqual(b2.lifetime_total_ml, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
