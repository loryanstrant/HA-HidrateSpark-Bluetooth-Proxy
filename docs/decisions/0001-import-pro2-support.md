# ADR-0001: Import PRO 2 contribution without changing bottle history

Date: 2026-11-03
Status: Accepted for release preparation (merge/publication pending approval)

## Decision

Import [GitHub PR #12 by kbaker827](https://github.com/loryanstrant/HA-HidrateSpark-Bluetooth-Proxy/pull/12)
as the original commit `d30a893093ce08f3269223795225e149913ec718`.
Keep review fixes in a follow-up commit; explicitly thank the contributor.
Prepare a normal v0.3.0 release, but label PRO 2 **experimental** until verified on hardware.
Keep the existing setup/capacity screens; warnings belong in documentation and release notes.

## Identity and compatibility

The config-entry ID and storage key stay unchanged. Existing uppercase-address device/entity
keys are retained as a fixed `device_key`, while config-entry identity uses the advertised
`h2o*` name where known. Migration uses the stored advertised name before the display title.
Legacy renamed/address-only entries without a known advertised name remain address-keyed;
there is no reliable way to infer a missing name. Manual setup also checks stored addresses
so migration does not create duplicate entries. Address updates must not reload an active entry.
Duplicate pre-existing entries are not automatically merged or deleted.

## Weight safety

Validate frame weights before any refill/calibration mutation. Missing, equal, rising,
out-of-range or implausibly jumping weights are ignored for fill and scale, but the existing
sip guards still decide whether the sip itself counts. Defer refill and scale changes until
all sip guards pass. Persist empty fill and refill-source deduplication across restarts.
Do not expand undocumented protocol assumptions to PRO 2 based only on simulated tests.

## Verification limits

Standalone tests replay the existing recorded-frame corpus and exercise recovery paths.
Real Home Assistant contract tests separately check options injection, config-entry migration,
registry identifiers, address updates and callback lifecycle; do not combine them with global
HA stubs in one process. A prepared archive is not a published release; published-release
installation and real-bottle checks remain separate release/hardware gates.
No production Home Assistant changes are authorised by this work.
