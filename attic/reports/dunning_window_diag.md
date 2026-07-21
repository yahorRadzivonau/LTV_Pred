# Dunning retry-window diagnostic

READ-ONLY (silver + tmp; silver not modified). Universe: **non-QA, non-Atelier**. Death moment = `churn_signal_datetime` (raw delete/cancel), not the period-end-shifted terminal.

Universe subs: stripe **5309**, solidgate **8707**.

## 1. Stripe dunning window (subs with ≥1 invoice.payment_failed)

Subs with ≥1 failed: **4012**; of them reaching terminal deleted: **2824**.

| window | p50 | p95 | p99 | max | n |
|---|---|---|---|---|---|
| first→last failed | 14.0 | 36.7 | 70.0 | 151.0 | 4012 |
| first failed→deleted | 14.0 | 32.4 | 51.9 | 106.0 | 2824 |

Histogram of first→last-failed window (days):
| bucket | n subs |
|---|---|
| 0-0d | 777 |
| 1-3d | 22 |
| 4-7d | 184 |
| 8-14d | 2544 |
| 15-30d | 222 |
| 31-60d | 200 |
| 61-90d | 54 |
| >=91d | 9 |

**">20d pause" — two readings:**
- (A) literal (a failed, then >20d silence, then failed/paid): **3** subs.
- (B) any >20d gap between consecutive FAILED events (dunning span): **188** subs — but 186 of them had a normal successful renewal INSIDE that gap (the sub was alive and paying, not paused; those are ordinary long-interval subs, not dunning pauses).

5 example chains for reading (A) (failed=F / paid=P, days from first failed):

- `sub_1T8yubJzVYkL7XCu2nC9NJKk`: F+0d F+0d F+0d F+92d F+97d F+102d P+103d
- `sub_1TD9yhJzVYkL7XCu67DRaJ2c`: F+0d F+92d F+97d F+101d F+105d F+106d
- `sub_1TI7u2JzVYkL7XCuvfxQeeck`: F+0d P+28d P+56d P+84d

## 2. Stripe resurrection — gap between successful paid > interval+14d

Subs with a resurrection gap (paid → [skip] → paid): **10**.

Resurrection gap days: p50=22.8, p95=45.9, max=46.8 (n gaps=11).

5 examples (sub, gap days, interval):

- `sub_1SqgYZJzVYkL7XCupG5Uz8o6`: max gap 45d, interval=month, n_paid=6
- `sub_1TBXp8JzVYkL7XCucoShELF9`: max gap 21d, interval=week, n_paid=5
- `sub_1TI7u2JzVYkL7XCuvfxQeeck`: max gap 28d, interval=week, n_paid=3
- `sub_1TIWUnJzVYkL7XCuIPKjquEj`: max gap 21d, interval=week, n_paid=13
- `sub_1TJqEEJzVYkL7XCuA78cFXTl`: max gap 21d, interval=week, n_paid=5

## 3. Solidgate dunning window + late resurrection (auth_failed / settle_ok)

Subs with ≥1 auth_failed: **5815**.

> ⚠ SNAPSHOT-CENSORING CAVEAT: **3562/5815 (61%) Solidgate auth_failed subs carry only ONE auth_failed order** in the snapshot — the payload keeps a cumulative order set but earlier retry orders were not captured for most terminated subs (same finding as decline_diag: ~68% keep only the final expire-order). So the p50=0 below is **"only the last attempt is visible", NOT "instant death"**; the Solidgate dunning window here is a LOWER BOUND, systematically under-measured. Stripe (full invoice.* stream) is not affected.

Days first auth_failed → last subscription event: p50=0.0, p95=12.0, p99=30.0, max=252.0 (n=5815) — under-measured for Solidgate, see caveat.

Histogram (days):
| bucket | n subs |
|---|---|
| 0-0d | 3797 |
| 1-3d | 750 |
| 4-7d | 871 |
| 8-14d | 212 |
| 15-30d | 130 |
| 31-60d | 31 |
| 61-90d | 17 |
| >=91d | 7 |

**Late resurrection (settle_ok >20d after a preceding auth_failed):** 30 subs.

5 examples (sub, gap days):

- `011063a7-5cf6-4350-a71f-789db258a644`: settle_ok 56d after last prior auth_failed
- `04f06fcc-f0e6-4f2e-a381-05f71bd53fcd`: settle_ok 24d after last prior auth_failed
- `06aaecc1-c7fd-4ad9-93dd-dc48eee9663f`: settle_ok 21d after last prior auth_failed
- `0c85499e-b0ed-4ac6-b6d6-8b7520488f4c`: settle_ok 28d after last prior auth_failed
- `0fbe658a-c9d9-4eb5-bb54-b720c4d8cf44`: settle_ok 27d after last prior auth_failed

## 4. KEY: life events AFTER the terminal (premature-burial check)

Death = churn_signal_datetime. A 'life event after' = a SUCCESSFUL payment (trial_converted/subscription_renewed for Stripe; settle_ok order for Solidgate) dated > death + 1 day. Also counted: ANY silver event after death.

| provider | terminal subs | successful-paid AFTER death | ANY event after death |
|---|---|---|---|
| stripe | 3220 | 0 (0.0%) | 17 (0.5%) |
| solidgate | 5043 | 3 (0.1%) | 9 (0.2%) |

Solidgate successful-paid-after-death — classification:

- `135f9bc8-080a-407c-9364-36e309dfa63b`: settle_ok +2d — SETTLE-LAG (same invoice_id as a pre-death auth_failed; late settle of an already-started attempt, NOT a new cycle)
- `6cf8cb5a-8462-4b8a-921c-704f871f1a4b`: settle_ok +2d — SETTLE-LAG (same invoice_id as a pre-death auth_failed; late settle of an already-started attempt, NOT a new cycle)
- `9e889c4c-d6a4-4226-a1a6-107bb5b38dca`: settle_ok +2d — SETTLE-LAG (same invoice_id as a pre-death auth_failed; late settle of an already-started attempt, NOT a new cycle)

**Read (corrected after verification):** TRUE resurrections (a NEW successful billing cycle after death) = **0 on both providers** — Stripe 0/3220; the 3 Solidgate cases are settle-lag (3/3 confirmed sharing an invoice_id with a pre-death auth_failed), i.e. a charge started before cancel that settled ~2 days later, not a revived subscription.

- **Stripe: robust.** Death anchored on the raw delete (churn_signal, ~9.5d EARLIER than the period-end terminal), so this is a conservative test, and 0/3220 pay afterwards.

- **Solidgate: same direction, weaker evidence.** No resurrection observed, but the snapshot censors early retries (61% of auth_failed subs keep one order) — so "nobody pays after death" is partly absence-of-evidence, not proof. The 9 "any event after" are audit/expire webhooks, not new charges.
