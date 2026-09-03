"""Buffered sips replayed on reconnect carry old timestamps and must not roll
'today' backwards (zeroing the daily counters) or move 'last sip' backwards."""

import datetime as _dt
import unittest

from ha_stub import TZ, HomeAssistant, load_state, local_ts, set_now

state = load_state()
Sip = state.Sip


def new_bottle(size=946):
    return state.BottleState(HomeAssistant(), "entry", size)


class DayRolloverReplayTest(unittest.TestCase):
    def setUp(self):
        set_now(_dt.datetime(2026, 6, 18, 20, 0, tzinfo=TZ))  # evening

    def test_replayed_yesterday_sip_does_not_zero_today(self):
        b = new_bottle()
        b.add_sip(Sip(timestamp=local_ts(2026, 6, 18, 19, 0), volume_ml=100))  # live, today
        self.assertEqual(b.total_today_ml, 100)

        # A buffered sip from YESTERDAY is replayed on reconnect.
        b.add_sip(Sip(timestamp=local_ts(2026, 6, 17, 23, 0), volume_ml=80))
        self.assertEqual(b.total_today_ml, 100, "replayed old sip must not zero/alter today")
        self.assertEqual(b.sips_today, 1)
        self.assertEqual(b.lifetime_total_ml, 180, "old sip still adds to lifetime")

    def test_last_sip_only_advances_forward(self):
        b = new_bottle()
        live_ts = local_ts(2026, 6, 18, 19, 0)
        b.add_sip(Sip(timestamp=live_ts, volume_ml=100))
        b.add_sip(Sip(timestamp=local_ts(2026, 6, 18, 10, 0), volume_ml=90))  # older replay
        self.assertEqual(b.last_sip.timestamp, live_ts, "last sip must not jump backwards")


class RejectedSipCountersTest(unittest.TestCase):
    """The 2026-09-03 phantom landed just after midnight, so it poisoned the
    fresh day's counters as well as the lifetime total. A rejected sip must
    leave every counter exactly where it was."""

    def setUp(self):
        set_now(_dt.datetime(2026, 6, 18, 20, 0, tzinfo=TZ))

    def test_rejected_sip_touches_no_counter(self):
        b = new_bottle(621)
        b.add_sip(Sip(timestamp=local_ts(2026, 6, 18, 19, 0), volume_ml=199))
        before = (b.total_today_ml, b.sips_today, b.lifetime_total_ml, b.last_sip.timestamp)

        b.add_sip(Sip(timestamp=local_ts(2026, 6, 18, 19, 30), volume_ml=621), rejected=True)

        self.assertEqual(
            (b.total_today_ml, b.sips_today, b.lifetime_total_ml, b.last_sip.timestamp),
            before,
            "water_today, sips_today, lifetime and last-sip must all be untouched",
        )

    def test_rejected_sip_does_not_trigger_a_rollover(self):
        b = new_bottle(621)
        b.add_sip(Sip(timestamp=local_ts(2026, 6, 18, 19, 0), volume_ml=199))
        # A phantom carrying a stale timestamp must not be the thing that decides
        # what 'today' is.
        b.add_sip(Sip(timestamp=local_ts(2026, 6, 17, 23, 0), volume_ml=621), rejected=True)
        self.assertEqual(b.total_today_ml, 199)
        self.assertEqual(b.sips_today, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
