"""The sip dedup window survives a restart, so buffered sips replayed on
reconnect are not double-counted into the persisted totals."""

import asyncio
import unittest

from ha_stub import HomeAssistant, load_state, local_ts, set_now
import datetime as _dt
from ha_stub import TZ

state = load_state()
Sip = state.Sip


def new_bottle(size=946):
    return state.BottleState(HomeAssistant(), "entry", size)


def _restart(b1):
    """Persist b1, then return a fresh state that loaded the same store."""

    async def go():
        await b1.async_save()
        b2 = new_bottle()
        b2._store._data = b1._store._data
        await b2.async_load()
        return b2

    return asyncio.run(go())


class DedupPersistenceTest(unittest.TestCase):
    def setUp(self):
        set_now(_dt.datetime(2026, 6, 18, 12, 0, tzinfo=TZ))

    def test_replayed_sip_after_restart_is_deduped(self):
        b1 = new_bottle()
        self.assertTrue(b1.add_sip(Sip(timestamp=local_ts(2026, 6, 18, 11, 59), volume_ml=100)))
        self.assertEqual(b1.lifetime_total_ml, 100)

        b2 = _restart(b1)
        accepted = b2.add_sip(Sip(timestamp=local_ts(2026, 6, 18, 11, 59), volume_ml=100))
        self.assertFalse(accepted, "replayed buffered sip should be deduped after restart")
        self.assertEqual(b2.lifetime_total_ml, 100, "totals must not double-count on replay")

    def test_distinct_sip_after_restart_still_counts(self):
        b1 = new_bottle()
        b1.add_sip(Sip(timestamp=local_ts(2026, 6, 18, 11, 0), volume_ml=100))

        b2 = _restart(b1)
        self.assertTrue(
            b2.add_sip(Sip(timestamp=local_ts(2026, 6, 18, 11, 30), volume_ml=120)),
            "a genuinely new sip must still be accepted after restart",
        )
        self.assertEqual(b2.lifetime_total_ml, 220)

    def test_rejected_sip_survives_a_restart_in_the_window(self):
        # Prod's phantom entry was deliberately left in recent_sips when the
        # totals were corrected by hand, precisely so the next drain wouldn't
        # replay it. Persisting the rejection is what makes that work.
        b1 = new_bottle()
        b1.add_sip(Sip(timestamp=local_ts(2026, 6, 18, 11, 0), volume_ml=100))
        b1.add_sip(Sip(timestamp=local_ts(2026, 6, 18, 11, 30), volume_ml=946), rejected=True)
        self.assertEqual(b1.lifetime_total_ml, 100)

        b2 = _restart(b1)
        self.assertEqual(b2.lifetime_total_ml, 100, "the phantom must not be restored")
        self.assertFalse(
            b2.add_sip(Sip(timestamp=local_ts(2026, 6, 18, 11, 30), volume_ml=946)),
            "and its replay must still be recognised",
        )
        self.assertEqual(b2.lifetime_total_ml, 100)
        self.assertEqual(
            b2.last_sip.volume_ml, 100,
            "the phantom must not be the last sip after a restart",
        )

    def test_dedup_still_works_with_arrival_time_stamps(self):
        # Sips are now stamped on arrival rather than backdated by a bogus fixed
        # 117 s, so the re-notify vector the dedup window exists for is a repeat
        # within a couple of seconds.
        b = new_bottle()
        ts = local_ts(2026, 6, 18, 11, 59)
        self.assertTrue(b.add_sip(Sip(timestamp=ts, volume_ml=100)))
        self.assertFalse(
            b.add_sip(Sip(timestamp=ts + 2, volume_ml=100)),
            "the same record re-notified moments later is a duplicate",
        )
        self.assertEqual(b.lifetime_total_ml, 100)


if __name__ == "__main__":
    unittest.main(verbosity=2)
