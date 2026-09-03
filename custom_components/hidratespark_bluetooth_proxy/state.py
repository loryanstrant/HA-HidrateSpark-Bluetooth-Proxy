"""Persistent state for a HidrateSpark bottle.

Tracks sip dedup, daily/lifetime totals with day rollover, weight-anchored
fill level with auto-calibration on refill, and the sip-exceeds-fill auto
refill heuristic. Persisted via Home Assistant's Store API so values survive
restarts.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    RAW_UNITS_PER_ML,
    SIP_DEDUP_TIMESTAMP_TOLERANCE_S,
    SIP_DEDUP_WINDOW,
    SIP_FILL_SLACK_FRACTION,
    STORAGE_KEY_PREFIX,
    STORAGE_VERSION,
)

_LOGGER = logging.getLogger(__name__)


@dataclass
class Sip:
    """A single sip event."""

    timestamp: float  # unix seconds
    volume_ml: int
    # A sip that failed a plausibility check. It is still kept in the dedup
    # window — and persisted there — so that when the bottle re-sends the same
    # record we recognise it instead of counting it as new. It contributes
    # nothing to any total and never becomes `last_sip`.
    rejected: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "iso": datetime.fromtimestamp(self.timestamp, tz=timezone.utc).isoformat(),
            "timestamp": self.timestamp,
            "volume_ml": self.volume_ml,
            "rejected": self.rejected,
        }


class BottleState:
    """In-memory state with HA-Store-backed persistence."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        bottle_size_ml: int,
    ) -> None:
        self._hass = hass
        self._store: Store = Store(
            hass, STORAGE_VERSION, f"{STORAGE_KEY_PREFIX}_{entry_id}"
        )

        self.bottle_size_ml = bottle_size_ml
        self.current_fill_ml: int = bottle_size_ml
        self.lifetime_total_ml: int = 0
        self.last_refill_ts: Optional[float] = None
        self.last_seen: Optional[float] = None

        # Sip history (in-memory only, dedup window).
        self.sips: deque[Sip] = deque(maxlen=200)
        self.last_sip: Optional[Sip] = None

        # Daily total with day rollover.
        self._today_date: str = ""
        self._total_today_ml: int = 0
        self._sips_today: int = 0
        self._refills_today: int = 0

        # Weight calibration (16-bit raw values). "full" is the bootstrap/refill
        # anchor; "empty" is the learned tare (lightest settled reading), which
        # is the stable zero reference fill is measured up from.
        self.weight_full_raw: Optional[int] = None
        self.weight_empty_raw: Optional[int] = None
        self.weight_raw: Optional[int] = None  # most recent stable u16 reading

    # ----------------------------------------------------------- persistence

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        self.current_fill_ml = int(data.get("current_fill_ml") or self.bottle_size_ml)
        self.lifetime_total_ml = int(data.get("lifetime_total_ml") or 0)
        self.last_refill_ts = data.get("last_refill_ts")
        self._today_date = str(data.get("today_date") or "")
        self._total_today_ml = int(data.get("total_today_ml") or 0)
        self._sips_today = int(data.get("sips_today") or 0)
        self._refills_today = int(data.get("refills_today") or 0)
        self.weight_full_raw = data.get("weight_full_raw")
        self.weight_empty_raw = data.get("weight_empty_raw")

        # Restore the recent-sip dedup window. Without this, a HA restart
        # empties the dedup history, and the bottle's initial drain replays its
        # buffered sips as brand-new ones — double-counting into the persisted
        # lifetime/today totals.
        for raw in data.get("recent_sips") or []:
            try:
                self.sips.append(
                    Sip(
                        timestamp=float(raw["timestamp"]),
                        volume_ml=int(raw["volume_ml"]),
                        rejected=bool(raw.get("rejected", False)),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        # Rejected sips are in the window purely to be recognised on replay, so
        # a phantom must not resurface as 'last sip' after a restart.
        counted = [s for s in self.sips if not s.rejected]
        if counted:
            self.last_sip = max(counted, key=lambda s: s.timestamp)
            self.last_seen = self.last_sip.timestamp

    async def async_save(self) -> None:
        await self._store.async_save(
            {
                "current_fill_ml": self.current_fill_ml,
                "lifetime_total_ml": self.lifetime_total_ml,
                "last_refill_ts": self.last_refill_ts,
                "today_date": self._today_date,
                "total_today_ml": self._total_today_ml,
                "sips_today": self._sips_today,
                "refills_today": self._refills_today,
                "weight_full_raw": self.weight_full_raw,
                "weight_empty_raw": self.weight_empty_raw,
                # Persist the dedup window so replays after a restart are
                # recognised as duplicates rather than re-counted.
                "recent_sips": [
                    {
                        "timestamp": s.timestamp,
                        "volume_ml": s.volume_ml,
                        "rejected": s.rejected,
                    }
                    for s in list(self.sips)[-SIP_DEDUP_WINDOW:]
                ],
            }
        )

    # --------------------------------------------------------------- mutations

    def set_bottle_size(self, size_ml: int) -> None:
        self.bottle_size_ml = size_ml
        if self.current_fill_ml > size_ml:
            self.current_fill_ml = size_ml

    def _local_date_str(self, ts: Optional[float] = None) -> str:
        """Return YYYY-MM-DD in HA's configured local timezone.

        Using HA's configured zone (Settings -> System -> General) keeps the
        'water today' counter and the new sip/refill counters in sync with
        what the user sees on the wall clock, including DST transitions.
        """
        if ts is None:
            return dt_util.now().strftime("%Y-%m-%d")
        return dt_util.as_local(
            datetime.fromtimestamp(ts, tz=timezone.utc)
        ).strftime("%Y-%m-%d")

    def _maybe_rollover(self, ts: Optional[float] = None) -> None:
        """Reset daily counters if `ts` (or now) falls on a new local date."""
        date_str = self._local_date_str(ts)
        if date_str != self._today_date:
            self._today_date = date_str
            self._total_today_ml = 0
            self._sips_today = 0
            self._refills_today = 0

    def refill(self, source: str, weight_full_raw: Optional[int]) -> None:
        self._maybe_rollover()
        self.current_fill_ml = self.bottle_size_ml
        self.last_refill_ts = time.time()
        # A "calibration" is the one-time bootstrap that establishes the full-
        # weight anchor (bottle assumed full); it isn't a user refill, so it
        # doesn't bump the daily refill counter.
        if source != "calibration":
            self._refills_today += 1
        if weight_full_raw is not None:
            self.weight_full_raw = weight_full_raw
        _LOGGER.info(
            "REFILL (%s): fill=%dml anchor=%s refills_today=%d",
            source,
            self.current_fill_ml,
            self.weight_full_raw,
            self._refills_today,
        )

    def update_fill_from_weight(self, raw: int) -> bool:
        """Recompute current fill from a stable upright 16-bit weight reading.

        Fill is measured up from the bottle's empty weight (tare), not down from
        the "full" anchor: the tare is stable per bottle, whereas "full" varies
        with how full it was actually filled, so anchoring on it leaves phantom
        volume at empty. The tare is learned as the lightest settled reading
        seen; until a real drain has been observed we fall back to estimating
        down from the full anchor so a freshly-set-up (full) bottle still reads
        sensibly. Returns True if current_fill_ml changed.
        """
        self.weight_raw = raw
        if self.weight_full_raw is None:
            # Bootstrap: the first settled reading establishes the full-weight
            # anchor (bottle assumed full at calibration). A real refill
            # (cap open/close + weight jump) re-anchors at the true full later.
            self.weight_full_raw = raw
            self.current_fill_ml = self.bottle_size_ml
            _LOGGER.info("weight calibration: adopted %s as full anchor", raw)
            return True

        full_span = RAW_UNITS_PER_ML * self.bottle_size_ml
        # Learn the empty floor (tare) as the lightest settled reading. Only
        # accept candidates that are plausibly below the full anchor but not more
        # than a bottle's worth below it (which would be the bottle lifted off
        # the puck rather than genuinely empty).
        if (
            self.weight_full_raw - 1.3 * full_span <= raw < self.weight_full_raw
            and (self.weight_empty_raw is None or raw < self.weight_empty_raw)
        ):
            self.weight_empty_raw = raw

        if (
            self.weight_empty_raw is not None
            and self.weight_full_raw - self.weight_empty_raw >= 0.6 * full_span
        ):
            # Enough range observed: measure up from the learned empty floor, so
            # empty reads 0 regardless of how full the last fill actually was.
            new_fill = round((raw - self.weight_empty_raw) / RAW_UNITS_PER_ML)
        else:
            # Not drained enough yet to trust the floor: estimate down from full.
            new_fill = self.bottle_size_ml - round(
                (self.weight_full_raw - raw) / RAW_UNITS_PER_ML
            )
        new_fill = max(0, min(self.bottle_size_ml, new_fill))
        if new_fill != self.current_fill_ml:
            self.current_fill_ml = new_fill
            return True
        return False

    def add_sip(self, sip: Sip, rejected: bool = False) -> bool:
        """Append a sip if it isn't a duplicate. Returns True if it was counted.

        `rejected` marks a frame the BLE layer already found implausible. A
        rejected sip is still recorded in the dedup window, so the caller should
        persist whenever `sip.rejected` ends up True even though this returns
        False — see the note on the rejection branch below.
        """
        # Dedup against last N sips: same volume within ±2 s timestamp.
        for existing in list(self.sips)[-SIP_DEDUP_WINDOW:]:
            if (
                abs(existing.timestamp - sip.timestamp)
                < SIP_DEDUP_TIMESTAMP_TOLERANCE_S
                and existing.volume_ml == sip.volume_ml
            ):
                return False

        # A sip cannot be bigger than the water that was in the bottle. Without a
        # weight anchor that condition means "refilled out of band" and is handled
        # below; with an anchor we know the fill directly, so it means the frame
        # was corrupt and must not reach any counter.
        #
        # The slack is there because `current_fill_ml` lags an out-of-band refill
        # by a weight reading or two, and dropping a genuine post-refill sip would
        # be worse than letting a marginal one through. This guard is the weakest
        # of the set — the frame-level checks in ble.py are what actually catch a
        # corrupt frame — so widen the slack rather than tighten it if field
        # reports show real sips being dropped.
        if not rejected and self.weight_full_raw is not None:
            slack_ml = self.bottle_size_ml * SIP_FILL_SLACK_FRACTION
            if sip.volume_ml > self.current_fill_ml + slack_ml:
                _LOGGER.warning(
                    "rejecting %dml sip: bottle only held %dml (+%dml slack)",
                    sip.volume_ml,
                    self.current_fill_ml,
                    round(slack_ml),
                )
                rejected = True

        # A rejected sip is still appended to the dedup window, and persisted
        # there, so the bottle's next drain recognises the record instead of
        # replaying it as new. Backing a phantom out of the totals while dropping
        # it from the window would leave the correction silently undone on the
        # next reconnect. It contributes to nothing else: no total, no count, and
        # it never becomes `last_sip`.
        if rejected:
            sip.rejected = True
            self.sips.append(sip)
            return False

        # Day rollover is keyed on wall-clock *now* (in HA's configured local
        # timezone), never on the sip's own timestamp. Buffered sips replayed
        # on reconnect carry old timestamps; keying rollover off them would roll
        # 'today' backwards and zero the daily counters (issue #4).
        self._maybe_rollover()

        self.sips.append(sip)
        self.lifetime_total_ml += sip.volume_ml

        # Only count toward 'today' if the sip actually falls on today's local
        # date. Historical/replayed sips still contribute to the lifetime total.
        if self._local_date_str(sip.timestamp) == self._today_date:
            self._total_today_ml += sip.volume_ml
            self._sips_today += 1

        # 'Last sip' / 'last seen' advance forward only, so a replayed old frame
        # can't make the last-sip sensor jump backwards (issue #4).
        if self.last_sip is None or sip.timestamp >= self.last_sip.timestamp:
            self.last_sip = sip
            self.last_seen = sip.timestamp

        # Sip-exceeds-fill: bottle was clearly refilled out-of-band. Only used as
        # a fallback while we have no weight anchor to track fill directly.
        if self.weight_full_raw is None and sip.volume_ml > self.current_fill_ml:
            self.current_fill_ml = max(0, self.bottle_size_ml - sip.volume_ml)
            self.last_refill_ts = sip.timestamp
            _LOGGER.info(
                "REFILL (auto: sip exceeded fill) -> %dml after %dml sip",
                self.current_fill_ml,
                sip.volume_ml,
            )
        elif self.weight_full_raw is None:
            # Sip-decrement fallback while we have no weight anchor.
            self.current_fill_ml = max(0, self.current_fill_ml - sip.volume_ml)

        return True

    @property
    def total_today_ml(self) -> int:
        # Late-day rollover: if no sip has come in yet today, we still want
        # the sensor to read 0 once midnight has passed (in HA's local zone).
        if self._local_date_str() != self._today_date:
            return 0
        return self._total_today_ml

    @property
    def sips_today(self) -> int:
        if self._local_date_str() != self._today_date:
            return 0
        return self._sips_today

    @property
    def refills_today(self) -> int:
        if self._local_date_str() != self._today_date:
            return 0
        return self._refills_today

    @property
    def current_fill_pct(self) -> int:
        if self.bottle_size_ml <= 0:
            return 0
        return round(100 * self.current_fill_ml / self.bottle_size_ml)
