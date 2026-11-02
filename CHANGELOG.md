# Changelog

## What's new in v0.3.0
HidrateSpark PRO 2 support, and fixes for every open issue.

### Added
- **HidrateSpark PRO 2 support.** The PRO 2 changes its Bluetooth address every so often
  and drops quiet connections, which used to leave its device permanently "disconnected"
  ([#10](https://github.com/loryanstrant/HA-HidrateSpark-Bluetooth-Proxy/issues/10)). Bottles
  are now identified by their advertised name (e.g. `h2o00003158`), and the integration
  follows the bottle to its new address without a reload or a duplicate device. Bottles
  that only advertise the newer data service are discovered too.
- **Connection keepalive.** While connected the bottle is checked once a minute, and a
  connection that has stopped answering, or sent nothing for 30 minutes, is rebuilt with
  a fresh handshake. This should also help bottles that "just stop sending data".
- **Model and firmware on the device page**, read from the bottle's Device Information
  service where it has one.
- **Fill and refills from the sip records themselves**
  ([#11](https://github.com/loryanstrant/HA-HidrateSpark-Bluetooth-Proxy/issues/11)). Every
  sip record carries the bottle's own weight before and after the sip. Current fill now
  follows that, and a jump between one sip's "after" and the next sip's "before" counts as
  a refill. That catches refills the cap sensor missed, including ones made away from
  home and on bottles that don't report cap events reliably
  ([#5](https://github.com/loryanstrant/HA-HidrateSpark-Bluetooth-Proxy/issues/5)).
- **Per-bottle weight scale.** The raw-weight-per-mL figure differs between bottles (about
  1.31 on a 32oz, about 1.59 on a 21oz PRO), so it is now learned from your sips instead
  of assumed. Until about 250 mL of sips has been seen, the old figure is used.

### Fixed
- **Options ("Configure") no longer crashes** with `TypeError: HidrateSparkOptionsFlow()
  takes no arguments` on Home Assistant 2024.12 and later
  ([#9](https://github.com/loryanstrant/HA-HidrateSpark-Bluetooth-Proxy/issues/9)).
- **No more duplicate devices after an address change**
  ([#10](https://github.com/loryanstrant/HA-HidrateSpark-Bluetooth-Proxy/issues/10)).
- The integration no longer reloads every time its own stored data is updated; only a
  change of bottle size reloads it.

### Upgrade notes
- Requires Home Assistant 2024.12 or later (already the HACS minimum).
- Existing bottles are migrated automatically and keep their device, entities and
  history. If an address rotation already created a duplicate "ghost" bottle, delete the
  one that is permanently unavailable after upgrading.
- The daily-zeroing and "last sip 8 years ago" reports in
  [#4](https://github.com/loryanstrant/HA-HidrateSpark-Bluetooth-Proxy/issues/4) were fixed
  in v0.2.0 (rollover keyed on the wall clock) and v0.2.1 (sips stamped on arrival). The
  reconnect fixes in this release cover the "stops reconnecting" part.

## What's new in v0.2.1
A corrupt Bluetooth frame could be recorded as an enormous drink you never took — one bottle
logged a phantom 621 mL at one in the morning, which then sat in the daily and lifetime
totals. This release teaches the integration to spot frames that can't describe a real drink
and throw them away. Decoding the frames properly to do that also turned up the reason
"last sip" time had always looked about two minutes late.

### Fixed
- **Impossible drinks are no longer counted.** A sip is now rejected if it claims the whole
  bottle in one go, if the bottle's running sip counter jumps backwards in a way a daily
  reset can't explain, or if the frame's own before-and-after weight readings show that far
  less water actually left the bottle. Rejected frames are logged as a warning with their raw
  data and never reach your daily total, sip count or lifetime total.
- **"Last sip" time is no longer about two minutes late.** Every sip was being backdated by a
  fixed 1 minute 57 seconds, because a value that isn't a timestamp was being read as one.
  Sips are now recorded when they arrive.
- **The bottle's running total is decoded correctly**, so the diagnostic logs show sensible
  numbers instead of values in the tens of thousands.

### Upgrade notes
- No action needed.
- New sips will be timestamped up to about two minutes later than you're used to seeing. That
  is the fix, not a regression — the old times were wrong.
- Totals already recorded are not recalculated. If a phantom drink is sitting in your history,
  it stays there; this release stops new ones appearing.

## What's new in v0.2.0
Legacy-firmware (e.g. 32oz) bottles now report weight/fill, plus several sip-accuracy
and resilience fixes. Big thanks to **[Huw Davies (@hadavies)](https://github.com/hadavies)**,
who contributed all of the changes in this release and verified them on real HidrateSpark
hardware.

### Added
- **Weight & fill on legacy-firmware bottles (e.g. 32oz)** — the weight characteristic is
  now decoded as a full 16-bit value with stability-based "settled" detection, instead of
  relying on a fixed orientation byte that legacy firmware doesn't send. Fill is measured up
  from the bottle's learned empty weight, and the full-weight anchor auto-calibrates from the
  first settled reading (no cap open/close needed). _(contributed by @hadavies)_

### Fixed
- **No more double-counting after a Home Assistant restart** — the recent-sip dedup window is
  now persisted, so buffered sips replayed when the bottle reconnects are recognised as
  duplicates instead of re-added to your totals. _(contributed by @hadavies)_
- **'Today' counters no longer reset to zero on reconnect** — daily rollover is keyed on the
  current local time rather than a replayed sip's old timestamp. _(contributed by @hadavies)_
- **'Last sip' time no longer jumps backwards** when old buffered frames are replayed — it
  only ever advances forward. _(contributed by @hadavies)_
- **Corrupt sip frames are dropped** (bogus >10-year-old timestamps) rather than being
  recorded as a sip "now", and the re-drain loop is bounded so a stuck record can't cause an
  unbounded write loop. _(contributed by @hadavies)_

### Upgrade notes
- On first run after upgrading, weight-equipped bottles re-establish their full-weight
  calibration automatically from the next settled reading; the previous weight anchor is not
  carried over. No action needed.
