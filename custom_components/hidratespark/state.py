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

from .const import (
    SIP_DEDUP_TIMESTAMP_TOLERANCE_S,
    SIP_DEDUP_WINDOW,
    STORAGE_KEY_PREFIX,
    STORAGE_VERSION,
)

_LOGGER = logging.getLogger(__name__)


@dataclass
class Sip:
    """A single sip event."""

    timestamp: float  # unix seconds
    volume_ml: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "iso": datetime.fromtimestamp(self.timestamp, tz=timezone.utc).isoformat(),
            "timestamp": self.timestamp,
            "volume_ml": self.volume_ml,
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

        # Weight calibration: low-byte value at "full".
        self.weight_full_low: Optional[int] = None
        self.weight_low: Optional[int] = None  # most recent stable low byte

    # ----------------------------------------------------------- persistence

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        self.current_fill_ml = int(data.get("current_fill_ml") or self.bottle_size_ml)
        self.lifetime_total_ml = int(data.get("lifetime_total_ml") or 0)
        self.last_refill_ts = data.get("last_refill_ts")
        self._today_date = str(data.get("today_date") or "")
        self._total_today_ml = int(data.get("total_today_ml") or 0)
        self.weight_full_low = data.get("weight_full_low")

    async def async_save(self) -> None:
        await self._store.async_save(
            {
                "current_fill_ml": self.current_fill_ml,
                "lifetime_total_ml": self.lifetime_total_ml,
                "last_refill_ts": self.last_refill_ts,
                "today_date": self._today_date,
                "total_today_ml": self._total_today_ml,
                "weight_full_low": self.weight_full_low,
            }
        )

    # --------------------------------------------------------------- mutations

    def set_bottle_size(self, size_ml: int) -> None:
        self.bottle_size_ml = size_ml
        if self.current_fill_ml > size_ml:
            self.current_fill_ml = size_ml

    def refill(self, source: str, weight_full_low: Optional[int]) -> None:
        self.current_fill_ml = self.bottle_size_ml
        self.last_refill_ts = time.time()
        if weight_full_low is not None:
            self.weight_full_low = weight_full_low
        _LOGGER.info(
            "REFILL (%s): fill=%dml anchor=%s",
            source,
            self.current_fill_ml,
            self.weight_full_low,
        )

    def update_fill_from_weight(self, low_byte: int) -> bool:
        """Recompute current fill from a stable upright weight reading.

        Returns True if current_fill_ml changed.
        """
        self.weight_low = low_byte
        if self.weight_full_low is None:
            # No anchor yet — sip-decrement estimate stays in effect.
            return False
        delta = self.weight_full_low - low_byte  # positive when drunk
        new_fill = max(0, min(self.bottle_size_ml, self.bottle_size_ml - delta))
        if new_fill != self.current_fill_ml:
            self.current_fill_ml = new_fill
            return True
        return False

    def add_sip(self, sip: Sip) -> bool:
        """Append a sip if it isn't a duplicate. Returns True if accepted."""
        # Dedup against last N sips: same volume within ±2 s timestamp.
        for existing in list(self.sips)[-SIP_DEDUP_WINDOW:]:
            if (
                abs(existing.timestamp - sip.timestamp)
                < SIP_DEDUP_TIMESTAMP_TOLERANCE_S
                and existing.volume_ml == sip.volume_ml
            ):
                return False

        # Day rollover keyed by the sip's local date.
        sip_date = datetime.fromtimestamp(sip.timestamp).strftime("%Y-%m-%d")
        if sip_date != self._today_date:
            self._today_date = sip_date
            self._total_today_ml = 0

        self.sips.append(sip)
        self.last_sip = sip
        self.lifetime_total_ml += sip.volume_ml
        self._total_today_ml += sip.volume_ml
        self.last_seen = sip.timestamp

        # Sip-exceeds-fill: bottle was clearly refilled out-of-band.
        if self.weight_full_low is None and sip.volume_ml > self.current_fill_ml:
            self.current_fill_ml = max(0, self.bottle_size_ml - sip.volume_ml)
            self.last_refill_ts = sip.timestamp
            _LOGGER.info(
                "REFILL (auto: sip exceeded fill) -> %dml after %dml sip",
                self.current_fill_ml,
                sip.volume_ml,
            )
        elif self.weight_full_low is None:
            # Sip-decrement fallback while we have no weight anchor.
            self.current_fill_ml = max(0, self.current_fill_ml - sip.volume_ml)

        return True

    @property
    def total_today_ml(self) -> int:
        # Late-day rollover: if no sip has come in yet today, we still want the
        # sensor to read 0 once midnight has passed.
        today = datetime.now().strftime("%Y-%m-%d")
        if today != self._today_date:
            return 0
        return self._total_today_ml

    @property
    def current_fill_pct(self) -> int:
        if self.bottle_size_ml <= 0:
            return 0
        return round(100 * self.current_fill_ml / self.bottle_size_ml)
