# Web-data snapshot profile

Read-only profile of `data/raw/*.parquet` (Stripe + Solidgate webhook dumps +
dimension tables) via DuckDB. No BigQuery/network access used. All queries
run through `.venv/Scripts/python.exe` (duckdb only importable there — bare
`python` on PATH resolves to a different, duckdb-less install). DuckDB's
session `TimeZone` defaults to the machine-local zone (`Europe/Warsaw`), NOT
UTC — every query touching a `TIMESTAMPTZ` column explicitly wraps it in
`... AT TIME ZONE 'UTC'` before any comparison/bucketing; confirmed
empirically this changes the displayed wall-clock value, not just cosmetic.

**Status: all 8 sections done.**

## Section 1 — Snapshot header

Manifest (`data/raw/SNAPSHOT_MANIFEST.json`), verbatim:

```json
{
  "snapshot_ts": "2026-07-07T00:00:00Z",
  "pulled_at": "2026-07-07T10:11:02.848079+00:00",
  "tables": {
    "stripe_events": 361032,
    "solidgate_events": 57936,
    "stripe_subscriptions": 5746,
    "web_conversions": 125607,
    "stripe_payments": 11751,
    "solidgate_old_transactions": 1032
  }
}
```

Row counts per file — measured vs manifest:

```sql
SELECT COUNT(*) AS n FROM read_parquet('data/raw/stripe_events.parquet');
SELECT COUNT(*) AS n FROM read_parquet('data/raw/solidgate_events.parquet');
SELECT COUNT(*) AS n FROM read_parquet('data/raw/stripe_subscriptions.parquet');
SELECT COUNT(*) AS n FROM read_parquet('data/raw/web_conversions.parquet');
SELECT COUNT(*) AS n FROM read_parquet('data/raw/stripe_payments.parquet');
SELECT COUNT(*) AS n FROM read_parquet('data/raw/solidgate_old_transactions.parquet');
```

| table | manifest | measured | match |
|---|---|---|---|
| stripe_events | 361032 | 361032 | yes |
| solidgate_events | 57936 | 57936 | yes |
| stripe_subscriptions | 5746 | 5746 | yes |
| web_conversions | 125607 | 125607 | yes |
| stripe_payments | 11751 | 11751 | yes |
| solidgate_old_transactions | 1032 | 1032 | yes |

All 6 files match the manifest exactly — snapshot is internally consistent, no drift since the pull.

Min/max UTC timestamps:

```sql
SELECT MIN(created AT TIME ZONE 'UTC') AS min_created_utc,
       MAX(created AT TIME ZONE 'UTC') AS max_created_utc
FROM read_parquet('data/raw/stripe_events.parquet');
```
`stripe_events.created`: **2025-10-10 10:18:54 → 2026-07-06 23:57:15 UTC**

```sql
SELECT MIN(created AT TIME ZONE 'UTC') AS min_created_utc,
       MAX(created AT TIME ZONE 'UTC') AS max_created_utc
FROM read_parquet('data/raw/solidgate_events.parquet');
```
`solidgate_events.created`: **2025-10-02 00:58:26 → 2026-07-06 23:58:17 UTC**

```sql
SELECT MIN(event_date AT TIME ZONE 'UTC') AS min_event_date_utc,
       MAX(event_date AT TIME ZONE 'UTC') AS max_event_date_utc
FROM read_parquet('data/raw/web_conversions.parquet');
```
`web_conversions.event_date`: **2025-09-26 15:56:08.974663 → 2026-07-07 03:57:33.683 UTC**

Interpretation: stripe_events/solidgate_events both cut off cleanly by
`2026-07-06 23:5x:xx` (before manifest's `snapshot_ts=2026-07-07T00:00:00Z`,
as expected for an event stream). **`web_conversions.max_event_date`
(2026-07-07 03:57:33 UTC) is AFTER the manifest's `snapshot_ts` boundary** —
per owner confirmation, this is **expected, not an anomaly**: the event
tables (stripe_events, solidgate_events) were pulled with a query filtered
on `created < snapshot_ts`, while `web_conversions` was pulled as a full
`list_rows` with no time filter at all. So `web_conversions` simply contains
whatever was in the source table at pull time, unbounded by `snapshot_ts`.
**Recommendation for the dataset build** (see Open questions): explicitly
filter `web_conversions` by `event_date < snapshot_ts` to align it with the
other tables' cutoff — otherwise it's the only table that can "see the
future" relative to the snapshot boundary.

## Section 2 — Stripe events profile

`event_type` distribution (all 46 types):

```sql
SELECT event_type, COUNT(*) AS n
FROM read_parquet('data/raw/stripe_events.parquet')
GROUP BY 1 ORDER BY n DESC;
```

| event_type | n |
|---|---|
| invoice.updated | 62697 |
| customer.subscription.updated | 28136 |
| customer.updated | 26559 |
| invoice.payment_failed | 23709 |
| invoice.created | 22494 |
| invoice.finalized | 22162 |
| payment_intent.created | 21366 |
| payment_intent.payment_failed | 19503 |
| charge.failed | 18158 |
| invoice.paid | 15088 |
| invoice.payment_succeeded | 15088 |
| invoice.upcoming | 15063 |
| payment_intent.succeeded | 12981 |
| charge.succeeded | 11847 |
| invoice_payment.paid | 11838 |
| customer.subscription.created | 5627 |
| customer.created | 4911 |
| customer.subscription.trial_will_end | 4130 |
| customer.subscription.deleted | 3392 |
| customer.discount.created | 2835 |
| invoice.marked_uncollectible | 2747 |
| customer.discount.deleted | 2482 |
| subscription_schedule.updated | 988 |
| payment_intent.amount_capturable_updated | 822 |
| payment_intent.canceled | 741 |
| charge.captured | 731 |
| invoice.voided | 705 |
| subscription_schedule.created | 624 |
| charge.refunded | 476 |
| payment_intent.requires_action | 470 |
| charge.refund.updated | 425 |
| subscription_schedule.canceled | 376 |
| subscription_schedule.expiring | 326 |
| subscription_schedule.completed | 321 |
| subscription_schedule.aborted | 289 |
| radar.early_fraud_warning.created | 248 |
| charge.updated | 141 |
| charge.dispute.closed | 108 |
| charge.dispute.created | 106 |
| charge.dispute.funds_withdrawn | 101 |
| invoice.payment_action_required | 82 |
| radar.early_fraud_warning.updated | 46 |
| refund.created | 45 |
| refund.updated | 44 |
| coupon.created | 3 |
| charge.dispute.funds_reinstated | 1 |

`api_version` × month(created UTC):

```sql
SELECT date_trunc('month', created AT TIME ZONE 'UTC') AS month, api_version, COUNT(*) AS n
FROM read_parquet('data/raw/stripe_events.parquet')
GROUP BY 1,2 ORDER BY 1,2;
```

| month | api_version | n |
|---|---|---|
| 2025-10 | 2025-08-27.basil | 873 |
| 2025-11 | 2025-08-27.basil | 233 |
| 2025-12 | 2025-04-30.basil | 10730 |
| 2026-01 | 2025-04-30.basil | 7010 |
| 2026-02 | 2025-04-30.basil | 12690 |
| 2026-03 | 2025-04-30.basil | 15019 |
| 2026-04 | 2025-04-30.basil | 59482 |
| 2026-05 | 2025-04-30.basil | 108963 |
| 2026-05 | 2025-12-15.clover | 293 |
| 2026-06 | 2025-04-30.basil | 111141 |
| 2026-06 | 2025-12-15.clover | 13563 |
| 2026-07 | 2025-04-30.basil | 19157 |
| 2026-07 | 2025-12-15.clover | 1878 |

Two version events, both worth flagging:
1. **2025-11 → 2025-12: hard cutover from `2025-08-27.basil` to `2025-04-30.basil`.** This is a version-string *downgrade* (an earlier-dated version replacing a later-dated one) — unusual, not a normal Stripe upgrade path. Not explainable from this data alone (see Open questions).
2. **2026-05 onward: gradual rollout of `2025-12-15.clover`** alongside `2025-04-30.basil` — 293 events in 2026-05 (0.3% of that month) growing to 1878/(19157+1878)=8.9% by 2026-07 (partial month). Not a clean cutover — both versions coexist through the end of the snapshot.

Invoice subscription-id path coverage per (api_version, event_type):

```sql
SELECT api_version, event_type, COUNT(*) AS n,
    COUNT(json_extract_string(data, '$.data.object.parent.subscription_details.subscription')) AS parent_path_nonnull,
    COUNT(json_extract_string(data, '$.data.object.subscription')) AS direct_path_nonnull
FROM read_parquet('data/raw/stripe_events.parquet')
WHERE event_type IN ('invoice.paid','invoice.payment_failed')
GROUP BY 1,2 ORDER BY 1,2;
```

| api_version | event_type | n | parent_path non-null | direct_path non-null |
|---|---|---|---|---|
| 2025-04-30.basil | invoice.paid | 14064 | 14063 | 0 |
| 2025-04-30.basil | invoice.payment_failed | 22894 | 22894 | 0 |
| 2025-12-15.clover | invoice.paid | 1024 | 1024 | 0 |
| 2025-12-15.clover | invoice.payment_failed | 815 | 815 | 0 |

`$.data.object.parent.subscription_details.subscription` covers essentially
100% of rows (14063/14064 = 99.99%, all others exact) **on both api_version
eras** — safe to standardize the dataset build on this single path.
`$.data.object.subscription` (the flat/legacy path) is **0 across the board**
— entirely unused in this snapshot regardless of version; don't bother
falling back to it.

`customer.subscription.deleted` → `cancellation_details.reason`:

```sql
SELECT json_extract_string(data, '$.data.object.cancellation_details.reason') AS reason, COUNT(*) AS n
FROM read_parquet('data/raw/stripe_events.parquet')
WHERE event_type = 'customer.subscription.deleted'
GROUP BY 1 ORDER BY n DESC;
```

| reason | n |
|---|---|
| payment_failed | 2718 |
| cancellation_requested | 674 |

Sums to 3392 = exact match with the `customer.subscription.deleted` count above — no NULL reasons, only 2 distinct values.

## Section 3 — Solidgate events profile

`event_type` distribution:

```sql
SELECT event_type, COUNT(*) AS n FROM read_parquet('data/raw/solidgate_events.parquet') GROUP BY 1 ORDER BY n DESC;
```

| event_type | n |
|---|---|
| retry | 9996 |
| create | 8605 |
| scheduled_for_retry | 7765 |
| recurring | 6849 |
| order_update | 6491 |
| active | 5672 |
| renew | 4301 |
| redemption | 3004 |
| expire | 2997 |
| cancel | 2065 |
| scheduled_for_cancellation | 191 |

`api_version` column sanity (odd for this provider):

```sql
SELECT DISTINCT api_version, COUNT(*) FROM read_parquet('data/raw/solidgate_events.parquet') GROUP BY 1;
```

Single constant value `solidgate_v1` across all 57936 rows — it's a static
ingestion tag, not a real evolving API version like Stripe's. Not a data
problem, just not comparable to the Stripe column of the same name.

True date range, overall and per event_type:

```sql
SELECT MIN(created AT TIME ZONE 'UTC') AS min_c, MAX(created AT TIME ZONE 'UTC') AS max_c
FROM read_parquet('data/raw/solidgate_events.parquet');
```
Overall: **2025-10-02 00:58:26 → 2026-07-06 23:58:17 UTC**

```sql
SELECT event_type, MIN(created AT TIME ZONE 'UTC') AS min_c, MAX(created AT TIME ZONE 'UTC') AS max_c, COUNT(*) AS n
FROM read_parquet('data/raw/solidgate_events.parquet') GROUP BY 1 ORDER BY min_c;
```

| event_type | min_c (UTC) | max_c (UTC) | n |
|---|---|---|---|
| order_update | 2025-10-02 00:58:26 | 2026-07-06 23:47:02 | 6491 |
| active | 2025-10-02 15:17:24 | 2026-07-06 23:43:03 | 5672 |
| retry | 2025-10-03 01:59:29 | 2026-07-06 23:58:16 | 9996 |
| cancel | 2025-10-03 01:59:31 | 2026-07-06 23:58:17 | 2065 |
| scheduled_for_retry | 2025-10-03 21:26:30 | 2026-07-06 23:55:56 | 7765 |
| renew | 2025-10-04 05:55:40 | 2026-07-06 23:46:14 | 4301 |
| recurring | 2025-10-05 05:42:12 | 2026-07-06 23:46:12 | 6849 |
| redemption | 2025-10-05 05:42:14 | 2026-07-06 23:43:08 | 3004 |
| create | 2025-10-08 11:30:53 | 2026-07-06 23:43:01 | 8605 |
| expire | 2025-10-08 11:30:59 | 2026-07-06 23:25:06 | 2997 |
| scheduled_for_cancellation | 2025-10-10 14:02:46 | 2026-07-06 12:01:04 | 191 |

```sql
SELECT COUNT(*) AS n_before_nov2025 FROM read_parquet('data/raw/solidgate_events.parquet')
WHERE (created AT TIME ZONE 'UTC') < TIMESTAMP '2025-11-01';
```
**2072 rows** before 2025-11-01.

Precise answer to "does it reach back to 2025-09/10": **it reaches back to
2025-10-02, not 2025-09**. Real history starts in October 2025 — much
earlier than the "May-2026 start" seen in the previous extract, but the
month is October, not September; don't round this to "Q3 2025."

Distinct subscription count:

```sql
SELECT COUNT(DISTINCT json_extract_string(data, '$.subscription.id')) AS n_distinct_sub
FROM read_parquet('data/raw/solidgate_events.parquet');
```
**8732** distinct `$.subscription.id` values.

`event_type` column vs `$.callback_type` field — sanity check they're the same:

```sql
SELECT event_type, json_extract_string(data, '$.callback_type') AS callback_type, COUNT(*) AS n
FROM read_parquet('data/raw/solidgate_events.parquet') GROUP BY 1,2 ORDER BY n DESC LIMIT 20;
```
All 11 event_type values match `$.callback_type` 1:1, no rows where they diverge (11 rows returned, all `event_type = callback_type`) — `event_type` column is a direct passthrough of `$.callback_type`, safe to use either.

`$.subscription.cancel_message` distribution (non-null):

```sql
SELECT json_extract_string(data, '$.subscription.cancel_message') AS cancel_message, COUNT(*) AS n
FROM read_parquet('data/raw/solidgate_events.parquet')
WHERE json_extract_string(data, '$.subscription.cancel_message') IS NOT NULL
GROUP BY 1 ORDER BY n DESC;
```

| cancel_message | n |
|---|---|
| Cancellation after redemption period | 1408 |
| Cancellation by support | 325 |
| Fraud Alert received | 174 |
| Token revoked by customer | 145 |
| Fraud Decline received | 121 |
| Recurring payment is blocked by Antifraud | 88 |
| Bank antifraud system | 8 |
| Fraud Chargeback received | 5 |
| Card Token has expired | 2 |

Total 2276. Cross-tab against event_type (not asked explicitly, added for
precision since the total didn't cleanly match `cancel` + `scheduled_for_cancellation` alone):

```sql
SELECT event_type,
       json_extract_string(data, '$.subscription.cancel_message') IS NOT NULL AS has_cancel_message,
       COUNT(*) AS n
FROM read_parquet('data/raw/solidgate_events.parquet')
GROUP BY 1,2 ORDER BY 1,2;
```
`cancel_message` is populated on **all** `cancel` rows (2065/2065), **all**
`scheduled_for_cancellation` rows (191/191), **and** 20/6491 `order_update`
rows. 2065+191+20 = 2276 — exact match, no other event_type carries it.

Exact-duplicate rows:

```sql
SELECT (SELECT COUNT(*) FROM read_parquet('data/raw/solidgate_events.parquet')) -
       (SELECT COUNT(*) FROM (SELECT DISTINCT event_id, created, data FROM read_parquet('data/raw/solidgate_events.parquet'))) AS exact_dupe_rows;
```
**6** exact (event_id, created, data) duplicate rows out of 57936 — negligible.

```sql
SELECT event_id, COUNT(*) AS n FROM read_parquet('data/raw/solidgate_events.parquet')
GROUP BY 1 ORDER BY n DESC LIMIT 5;
```
Top repeated `event_id`: one value repeats **69** times (others 35, 27, 27, 27) — confirms the documented quirk (composite event_id without timestamp, legitimately repeats). Important distinction: 69 rows can share one `event_id` **without** being exact duplicates (different `created`/`data`) — only 6 rows total are true full-row duplicates. Don't dedup on `event_id` alone.

## Section 2b — investigating the basil→basil version downgrade (two/three Stripe identities?)

Owner's hypothesis: two Stripe accounts mixed in one table. Checked for an explicit account marker first:

```sql
SELECT json_extract_string(data,'$.account') AS account, json_extract_string(data,'$.context') AS context, COUNT(*)
FROM read_parquet('data/raw/stripe_events.parquet') GROUP BY 1,2;
```
Result: **both NULL for all 361032 rows** (top-level JSON keys are only
`['api_version','created','data','id','livemode','object','pending_webhooks','request','type']`)
— no explicit account/context field anywhere in the payload. Falling back
to the object-id-suffix method.

Stripe embeds a fixed 10-character token inside `sub_`/`in_`/`price_` object
ids, right after the id's own short prefix+counter segment (NOT at the
literal string end — `right(id,10)` does **not** isolate it, since a random
suffix follows the token; the token sits at a fixed offset that depends on
the id's prefix length: chars 10-19 for `in_` ids (3-char prefix), chars
11-20 for `sub_` ids (4-char prefix), chars 13-22 for `price_` ids (6-char
prefix)). Verified this offset empirically rather than assuming it:

```sql
SELECT substring(json_extract_string(data,'$.data.object.id'), 11, 10) AS frag, api_version, COUNT(*) AS n
FROM read_parquet('data/raw/stripe_events.parquet')
WHERE event_type='customer.subscription.created'
GROUP BY 1,2 ORDER BY api_version, n DESC LIMIT 15;
```

| id fragment | api_version | n |
|---|---|---|
| JzVYkL7XCu | 2025-04-30.basil | 5342 |
| IEMVDMVTXC | 2025-08-27.basil | 10 |
| A9qayReKqB | 2025-12-15.clover | 275 |

Same 3-way split, same exact fragment strings, confirmed independently on
`price_id` (offset 13):

```sql
SELECT substring(json_extract_string(data,'$.data.object.items.data[0].price.id'), 13, 10) AS frag, api_version, COUNT(*) AS n
FROM read_parquet('data/raw/stripe_events.parquet')
WHERE event_type='customer.subscription.created'
GROUP BY 1,2 ORDER BY api_version, n DESC LIMIT 15;
```
Identical mapping: `JzVYkL7XCu`↔basil-04-30, `IEMVDMVTXC`↔basil-08-27, `A9qayReKqB`↔clover-12-15.

Full grouped table, `invoice.paid` + `customer.subscription.created`, fragment × api_version × month:

```sql
SELECT date_trunc('month', created AT TIME ZONE 'UTC') AS month,
       substring(json_extract_string(data,'$.data.object.id'), 10, 10) AS id_fragment,
       api_version, COUNT(*) AS n
FROM read_parquet('data/raw/stripe_events.parquet')
WHERE event_type='invoice.paid'
GROUP BY 1,2,3 ORDER BY 1,2;

SELECT date_trunc('month', created AT TIME ZONE 'UTC') AS month,
       substring(json_extract_string(data,'$.data.object.id'), 11, 10) AS id_fragment,
       api_version, COUNT(*) AS n
FROM read_parquet('data/raw/stripe_events.parquet')
WHERE event_type='customer.subscription.created'
GROUP BY 1,2,3 ORDER BY 1,2;
```

| month | id_fragment | api_version | n (invoice.paid) | n (sub.created) |
|---|---|---|---|---|
| 2025-10 | IEMVDMVTXC | 2025-08-27.basil | — | 8 |
| 2025-11 | IEMVDMVTXC | 2025-08-27.basil | — | 2 |
| 2025-12 | JzVYkL7XCu | 2025-04-30.basil | 167 | 296 |
| 2026-01 | JzVYkL7XCu | 2025-04-30.basil | 179 | 209 |
| 2026-02 | JzVYkL7XCu | 2025-04-30.basil | 239 | 352 |
| 2026-03 | JzVYkL7XCu | 2025-04-30.basil | 763 | 419 |
| 2026-04 | JzVYkL7XCu | 2025-04-30.basil | 2887 | 1448 |
| 2026-05 | JzVYkL7XCu | 2025-04-30.basil | 4435 | 1513 |
| 2026-05 | A9qayReKqB | 2025-12-15.clover | 23 | 10 |
| 2026-06 | JzVYkL7XCu | 2025-04-30.basil | 4646 | 1080 |
| 2026-06 | A9qayReKqB | 2025-12-15.clover | 895 | 264 |
| 2026-07 | JzVYkL7XCu | 2025-04-30.basil | 748 | 25 |
| 2026-07 | A9qayReKqB | 2025-12-15.clover | 106 | 1 |

Cross-check for any fragment ever appearing under more than one api_version (both event types pooled):

```sql
WITH u AS (
  SELECT substring(json_extract_string(data,'$.data.object.id'), 10, 10) AS frag, api_version
  FROM read_parquet('data/raw/stripe_events.parquet') WHERE event_type='invoice.paid'
  UNION ALL
  SELECT substring(json_extract_string(data,'$.data.object.id'), 11, 10) AS frag, api_version
  FROM read_parquet('data/raw/stripe_events.parquet') WHERE event_type='customer.subscription.created'
)
SELECT frag, COUNT(DISTINCT api_version) AS n_distinct_versions, COUNT(*) AS n FROM u GROUP BY 1;
```

| frag | n_distinct_versions | n |
|---|---|---|
| A9qayReKqB | 1 | 1299 |
| IEMVDMVTXC | 1 | 10 |
| JzVYkL7XCu | 1 | 19406 |

**Verdict: confirmed, not refuted.** Across 20715 checked events (invoice.paid
+ customer.subscription.created), every single one of the 3 id-fragments maps
to **exactly one** api_version — zero mixing, zero exceptions. This is
stronger than "two accounts": it's **three** distinct identities
(`IEMVDMVTXC`/2025-08-27.basil is real but tiny — only 10 subscription-created
events, Oct-Nov 2025), each tied 1:1 to one api_version. The `2025-04-30.basil`
→ `2025-12-15.clover` handoff (May 2026 onward) overlaps for ~2 months at the
event-type level (both versions active in the same calendar month) but an
**individual event is never internally inconsistent** — the id-fragment and
api_version always agree within one event, so whatever produced the switch,
it's a clean per-request/per-account distinction, not a data-corruption
artifact. Not explainable further from local data alone — added to Open
questions below (what real-world event this corresponds to: new account,
new API key, platform migration).

## Section 4 — Chain reconstruction sanity (per provider)

**Stripe.** First checked the actual distribution of invoice-event count per
subscription (via the confirmed `parent.subscription_details.subscription`
path) before blindly taking "top 3":

```sql
WITH c AS (
  SELECT json_extract_string(data,'$.data.object.parent.subscription_details.subscription') AS sub_id, COUNT(*) AS n
  FROM read_parquet('data/raw/stripe_events.parquet')
  WHERE json_extract_string(data,'$.data.object.parent.subscription_details.subscription') IS NOT NULL
  GROUP BY 1
)
SELECT COUNT(*) AS n_subs, MIN(n), MAX(n), approx_quantile(n,0.5) AS median, approx_quantile(n,0.9) AS p90, approx_quantile(n,0.99) AS p99 FROM c;
```
`n_subs=5724, min=1, max=639, median=24, p90=58, p99=103`. **The literal
top-3 by count (633-639 events each) are extreme outliers (>6x the p99)** —
turned out to be daily-billing-at-$0.99 subscriptions (see Section 5). Printing
those verbatim would misrepresent "typical" chain shape, so this section shows
**both**: a condensed view of the top-3 outliers, and 3 subscriptions near the
median (24-25 events) as the representative case.

Top-3 outliers (condensed — full chain is 633-639 rows each, all
`subscription_cycle` @ 99¢ firing roughly every 24h):
```sql
SELECT json_extract_string(data,'$.data.object.parent.subscription_details.subscription') AS sub_id, COUNT(*) AS n
FROM read_parquet('data/raw/stripe_events.parquet')
WHERE json_extract_string(data,'$.data.object.parent.subscription_details.subscription') IS NOT NULL
GROUP BY 1 HAVING n > 10 ORDER BY n DESC LIMIT 3;
```
`sub_1T91zgJzVYkL7XCuRKDMSxlk` (639), `sub_1T921aJzVYkL7XCuGl5p2fsw` (636), `sub_1T929gJzVYkL7XCuObwxb5k0` (633) — all three start ~2026-03-09 and cycle roughly daily at 99¢ through the end of the snapshot.

3 typical subscriptions (24-25 events, near-median), full chains:
```sql
SELECT created AT TIME ZONE 'UTC' AS created_utc, event_type,
       json_extract_string(data,'$.data.object.billing_reason') AS billing_reason,
       json_extract(data,'$.data.object.amount_paid') AS amount_paid,
       json_extract_string(data,'$.data.object.status') AS status
FROM read_parquet('data/raw/stripe_events.parquet')
WHERE json_extract_string(data,'$.data.object.parent.subscription_details.subscription') = '<sub_id>'
ORDER BY created_utc;
```

`sub_1TjEs6JzVYkL7XCuJcNgBcWP` (25 events, weekly @ $9.99, 12 same-second collisions):
| created_utc | event_type | billing_reason | amount_paid | status |
|---|---|---|---|---|
| 2026-06-17 08:30:55 | invoice.paid | subscription_create | 0 | paid |
| 2026-06-17 08:30:55 | invoice.payment_succeeded | subscription_create | 0 | paid |
| 2026-06-17 08:30:55 | invoice.finalized | subscription_create | 0 | paid |
| 2026-06-17 08:30:55 | invoice.created | subscription_create | 0 | paid |
| 2026-06-20 08:31:52 | invoice.created | subscription_cycle | 0 | draft |
| 2026-06-20 08:32:35 | invoice.upcoming | upcoming | 0 | draft |
| 2026-06-20 09:33:03 | invoice.updated | subscription_cycle | 0 | open |
| 2026-06-20 09:33:03 | invoice.finalized | subscription_cycle | 0 | open |
| 2026-06-20 09:33:04 | invoice.updated | subscription_cycle | 0 | open |
| 2026-06-20 09:33:05 | invoice.payment_failed | subscription_cycle | 0 | open |
| 2026-06-25 07:33:11 | invoice.paid | subscription_cycle | 999 | paid |
| 2026-06-25 07:33:11 | invoice.payment_succeeded | subscription_cycle | 999 | paid |
| 2026-06-25 07:33:11 | invoice.updated | subscription_cycle | 999 | paid |
| 2026-06-27 08:31:41 | invoice.created | subscription_cycle | 0 | draft |
| 2026-06-27 08:31:51 | invoice.upcoming | upcoming | 0 | draft |
| 2026-06-27 09:32:34 | invoice.paid | subscription_cycle | 999 | paid |
| 2026-06-27 09:32:34 | invoice.payment_succeeded | subscription_cycle | 999 | paid |
| 2026-06-27 09:32:34 | invoice.finalized | subscription_cycle | 999 | paid |
| 2026-06-27 09:32:34 | invoice.updated | subscription_cycle | 999 | paid |
| 2026-07-04 08:31:41 | invoice.created | subscription_cycle | 0 | draft |
| 2026-07-04 08:32:33 | invoice.upcoming | upcoming | 0 | draft |
| 2026-07-04 09:32:49 | invoice.paid | subscription_cycle | 999 | paid |
| 2026-07-04 09:32:49 | invoice.payment_succeeded | subscription_cycle | 999 | paid |
| 2026-07-04 09:32:49 | invoice.finalized | subscription_cycle | 999 | paid |
| 2026-07-04 09:32:49 | invoice.updated | subscription_cycle | 999 | paid |

`sub_1TMhchJzVYkL7XCu7iOb6W5j` (25 events, monthly @ $11.99, then starts payment-failing from 2026-06-30 onward — a live churn-in-progress example) and `sub_1TVgDaJzVYkL7XCuypRd859n` (25 events, same shape, payment-failing from 2026-06-24 onward) — full chains in the working query log; both show the same structural pattern: `invoice.created`(draft)→`invoice.upcoming`→`invoice.finalized`+`invoice.paid`+`invoice.payment_succeeded`+`invoice.updated` firing within the same second on a successful cycle, vs. repeated `invoice.updated`/`invoice.payment_failed` pairs (no `finalized`/`paid`) on a failing one.

**Same-second collisions**: all 3 typical chains have same-second timestamp collisions (12, 12, 9 rows respectively sharing a `created_utc` with another row in the same chain) — a real, structural Stripe behavior (one billing action fans out to 3-5 webhook events in the same instant), not a data error. Dataset build must not assume 1 row = 1 unique timestamp per subscription.

**Solidgate**, top-3 `subscription.id` by row count (>10):
```sql
SELECT json_extract_string(data,'$.subscription.id') AS sub_id, COUNT(*) AS n
FROM read_parquet('data/raw/solidgate_events.parquet')
WHERE json_extract_string(data,'$.subscription.id') IS NOT NULL
GROUP BY 1 HAVING n > 10 ORDER BY n DESC LIMIT 3;
```
`5cc51131-7b6f-4ce7-9a0b-198813ca3536` (69), `87b8a4be-12b6-4838-b756-6205bc30ce1a` (54), `59aab603-fd67-4573-829d-90053247a0e3` (45) — row-count distribution overall: `median=4, p90=16, max=69` (via `approx_quantile` on the same per-subscription grouping), so these are outliers here too but far less extreme than Stripe's.

```sql
SELECT created AT TIME ZONE 'UTC' AS created_utc, event_type,
       json_extract_string(data,'$.subscription.status') AS sub_status,
       json_extract_string(data,'$.subscription.cancel_message') AS cancel_message
FROM read_parquet('data/raw/solidgate_events.parquet')
WHERE json_extract_string(data,'$.subscription.id') = '<sub_id>'
ORDER BY created_utc;
```

Chain 1 (`5cc51131...`, 69 rows, 2025-10-02 → 2026-01-24): clean weekly
`recurring`→`renew`→`order_update` triplets for months, interspersed with
`redemption`→`retry`→`scheduled_for_retry` runs (payment retry cycles) that
always resolve back to `renew`/`active` — subscription never actually
cancels in this window despite several retry episodes.

Chain 3 (`59aab603...`, 45 rows, 2026-05-23 → 2026-07-01): same
recurring/renew/order_update pattern, ends in an actual `cancel` event on
2026-07-01 with `cancel_message="Cancellation after redemption period"` —
and that final timestamp (14:00:03) has **7 other rows** sharing the exact
same second (`retry`, `scheduled_for_retry` ×5, `cancel`) — an even sharper
same-second fan-out than Stripe's. Solidgate chains need the same
"don't assume unique timestamps" handling.

**Stripe subscriptions with zero events in stripe_events (added check, per owner):**
```sql
CREATE OR REPLACE TEMP VIEW known_subs AS
SELECT DISTINCT json_extract_string(data, '$.data.object.parent.subscription_details.subscription') AS sub_id
FROM read_parquet('data/raw/stripe_events.parquet')
WHERE json_extract_string(data, '$.data.object.parent.subscription_details.subscription') IS NOT NULL
UNION
SELECT DISTINCT json_extract_string(data, '$.data.object.id') AS sub_id
FROM read_parquet('data/raw/stripe_events.parquet')
WHERE event_type LIKE 'customer.subscription.%';

SELECT COUNT(*) AS n_missing, MIN(sub_created_at AT TIME ZONE 'UTC') AS min_created, MAX(sub_created_at AT TIME ZONE 'UTC') AS max_created
FROM read_parquet('data/raw/stripe_subscriptions.parquet') s
WHERE s.sub_id NOT IN (SELECT sub_id FROM known_subs);
```
Result: **0 missing** — every one of the 5746 `stripe_subscriptions.sub_id`
values appears somewhere in `stripe_events`, via one of the two paths. This
contradicts the naive expectation of "a small group of pre-2025-10-10
subscriptions invisible to stripe_events." Checked why directly rather than
assuming a circular table-build:
```sql
SELECT COUNT(*) AS n_created_before FROM read_parquet('data/raw/stripe_subscriptions.parquet')
WHERE (sub_created_at AT TIME ZONE 'UTC') < TIMESTAMP '2025-10-10';

SELECT status, COUNT(*) AS n, MIN(sub_created_at AT TIME ZONE 'UTC') AS min_c, MAX(sub_created_at AT TIME ZONE 'UTC') AS max_c
FROM read_parquet('data/raw/stripe_subscriptions.parquet')
WHERE (sub_created_at AT TIME ZONE 'UTC') < TIMESTAMP '2025-10-10' GROUP BY status;

SELECT COUNT(*) AS n_created_and_ended_before
FROM read_parquet('data/raw/stripe_subscriptions.parquet')
WHERE (sub_created_at AT TIME ZONE 'UTC') < TIMESTAMP '2025-10-10'
  AND ended_at IS NOT NULL AND (ended_at AT TIME ZONE 'UTC') < TIMESTAMP '2025-10-10';
```
26 subscriptions were created before 2025-10-10 (min `sub_created_at` =
2025-07-11), of which 3 are still `active` and 23 are `canceled` — but **0**
of the 26 both started AND ended before 2025-10-10 (the `n_created_and_ended_before`
query returns an empty result set). So the 0-missing result isn't a table-
construction artifact — it's genuine: every pre-Oct-2025 subscription lived
long enough (renewed or was formally canceled) to leave a trace on/after
2025-10-10, when `stripe_events` coverage begins. There is no "silently
invisible" cohort in this snapshot.

## Section 5 — Money sanity

`billing_reason` × `amount_paid` (cents), `invoice.paid`, top 15:

```sql
SELECT json_extract_string(data,'$.data.object.billing_reason') AS billing_reason,
       json_extract(data,'$.data.object.amount_paid') AS amount_paid,
       COUNT(*) AS n
FROM read_parquet('data/raw/stripe_events.parquet')
WHERE event_type='invoice.paid'
GROUP BY 1,2 ORDER BY n DESC LIMIT 15;
```

| billing_reason | amount_paid (¢) | n |
|---|---|---|
| subscription_cycle | 999 | 6427 |
| subscription_create | 0 | 3349 |
| subscription_cycle | 99 | 2188 |
| subscription_cycle | 1199 | 1136 |
| subscription_create | 99 | 764 |
| subscription_create | 3999 | 345 |
| subscription_cycle | 4999 | 229 |
| subscription_cycle | 499 | 207 |
| subscription_cycle | 849 | 78 |
| subscription_create | 499 | 72 |
| subscription_cycle | 3999 | 54 |
| subscription_cycle | 799 | 45 |
| subscription_cycle | 350 | 42 |
| subscription_cycle | 699 | 35 |
| subscription_create | 999 | 33 |

Paid-trial pattern existence check:
```sql
SELECT
  SUM(CASE WHEN json_extract_string(data,'$.data.object.billing_reason')='subscription_create' AND json_extract(data,'$.data.object.amount_paid')='99' THEN 1 ELSE 0 END) AS create_99_n,
  SUM(CASE WHEN json_extract_string(data,'$.data.object.billing_reason')='subscription_cycle' AND json_extract(data,'$.data.object.amount_paid')='999' THEN 1 ELSE 0 END) AS cycle_999_n,
  SUM(CASE WHEN json_extract_string(data,'$.data.object.billing_reason')='subscription_cycle' AND json_extract(data,'$.data.object.amount_paid')='3999' THEN 1 ELSE 0 END) AS cycle_3999_n
FROM read_parquet('data/raw/stripe_events.parquet') WHERE event_type='invoice.paid';
```
`subscription_create@99¢`: **764** rows — confirmed exists.
`subscription_cycle@999¢`: **6427** rows — confirmed exists (the dominant cycle price).
`subscription_cycle@3999¢`: **54** rows — confirmed exists (small volume).

**Not asked for, but found while checking**: `subscription_cycle@99¢` also
exists, at **2188** rows — larger volume than the 3999¢ cycle bucket. This is
the daily-cycling-at-trial-price pattern behind Section 4's 3 outlier chains:
```sql
SELECT COUNT(DISTINCT json_extract_string(data,'$.data.object.parent.subscription_details.subscription')) AS n_subs
FROM read_parquet('data/raw/stripe_events.parquet')
WHERE event_type='invoice.paid' AND json_extract_string(data,'$.data.object.billing_reason')='subscription_cycle'
  AND json_extract(data,'$.data.object.amount_paid')='99';
```
Only **27 distinct subscriptions** produce all 2188 of those rows (~81
invoices/sub average) — a small, extreme-frequency cohort, not a broad
pattern. Flagged again under Surprises.

Refund orphan rate — checked whether `charge.refunded`/`refund.created`
payloads carry ANY subscription or invoice reference directly:
```sql
SELECT event_type, COUNT(*) AS n,
    COUNT(json_extract_string(data,'$.data.object.invoice')) AS n_invoice,
    COUNT(json_extract_string(data,'$.data.object.subscription')) AS n_subscription_direct,
    COUNT(json_extract_string(data,'$.data.object.payment_intent')) AS n_payment_intent
FROM read_parquet('data/raw/stripe_events.parquet')
WHERE event_type IN ('charge.refunded','refund.created')
GROUP BY 1;
```

| event_type | n | invoice field | subscription field | payment_intent field |
|---|---|---|---|---|
| refund.created | 45 | 0 | 0 | 45 |
| charge.refunded | 476 | 0 | 0 | 476 |

**Orphan rate: 100% for both event types** (0/476 and 0/45 have a directly
resolvable invoice or subscription id in their own payload — verified this
is real by inspecting `data.object` keys for both event types, neither has
an `invoice` or `subscription` field at all in its schema). Both DO carry
`payment_intent` at 100% — that's an *indirect* resolution path (would
require joining against `payment_intent.*`/invoice events elsewhere in the
stream), not something resolvable from the refund event's own payload alone.
Noted in Open questions as a recommendation for the dataset build.

## Section 6 — Attribution & geo

Solidgate email path — printed raw JSON for one `create` and one `renew`
event before picking a path, rather than assuming:
```sql
SELECT data FROM read_parquet('data/raw/solidgate_events.parquet') WHERE event_type='create' LIMIT 1;
SELECT data FROM read_parquet('data/raw/solidgate_events.parquet') WHERE event_type='renew' LIMIT 1;
```
Both have identical top-level shape: `['callback_type','customer','invoices','product','subscription']`,
with `customer = {"customer_account_id": "...", "customer_email": "..."}`.
Confirmed the path is universal (not just create/renew) across all 11 event types:
```sql
SELECT event_type, COUNT(*) AS n, COUNT(json_extract_string(data, '$.customer.customer_email')) AS n_email_nonnull
FROM read_parquet('data/raw/solidgate_events.parquet') GROUP BY 1 ORDER BY 1;
```
**100% populated on every event_type (57936/57936)** — chosen path: **`$.customer.customer_email`**.

`stripe_subscriptions.utm_source`:
```sql
SELECT utm_source, COUNT(*) AS n FROM read_parquet('data/raw/stripe_subscriptions.parquet') GROUP BY 1 ORDER BY n DESC;
```
| utm_source | n |
|---|---|
| NULL | 2658 |
| fbbohdan | 2362 |
| fb-andrii | 450 |
| facebook | 209 |
| mobile | 62 |
| test | 5 |

**NULL share: 2658/5746 = 46.3%.**

`campaign_name` NULL share:
```sql
SELECT COUNT(*) AS n_total, COUNT(campaign_name) AS n_nonnull, COUNT(*)-COUNT(campaign_name) AS n_null
FROM read_parquet('data/raw/stripe_subscriptions.parquet');
```
`n_total=5746, n_nonnull=3024, n_null=2722` → **NULL share: 47.4%**.

`web_conversions.utm_source`:
```sql
SELECT utm_source, COUNT(*) AS n FROM read_parquet('data/raw/web_conversions.parquet') GROUP BY 1 ORDER BY n DESC;
```
19 distinct values, top ones: `fbbohdan` (57532), `facebook` (22323),
`unknown` (22076), `fb-andrii` (13248), empty string `""` (3529), `ads-rs`
(3452), `fb` (999), `adsterra` (708), `clickadu` (590), `mobile` (397), `ig`
(362), `test` (190), `ads-zk` (170), `an` (18), `none` (7), `fbtetsviola` (3),
`th` (1), `fbtestviola` (1), `facebook-SiteLink` (1). **No true SQL NULL** in
this column — missingness is encoded as `"unknown"`/`""`/`"none"` instead
(3 different sentinels, none of them NULL) — a real inconsistency vs.
`stripe_subscriptions.utm_source`, which uses actual NULL. Flagged under Surprises.

Email multi-country share:
```sql
WITH c AS (SELECT email, COUNT(DISTINCT country) AS n_country FROM read_parquet('data/raw/web_conversions.parquet') WHERE email IS NOT NULL GROUP BY 1)
SELECT COUNT(*) AS n_emails, SUM(CASE WHEN n_country>1 THEN 1 ELSE 0 END) AS n_multi, SUM(CASE WHEN n_country>1 THEN 1 ELSE 0 END)*1.0/COUNT(*) AS share_multi FROM c;
```
`n_emails=38902, n_multi=3177` → **8.17%** of emails see more than one distinct `country` value.

Email match rate — first confirmed case sensitivity (needed to decide whether to `lower()`):
```sql
SELECT COUNT(*) FILTER (WHERE email != lower(email)) AS n_mixed_case, COUNT(*) AS n_total
FROM read_parquet('data/raw/web_conversions.parquet') WHERE email IS NOT NULL;
-- n_mixed_case=0, n_total=125607 (web_conversions already all-lowercase)

SELECT COUNT(*) FILTER (WHERE json_extract_string(data,'$.customer.customer_email') != lower(json_extract_string(data,'$.customer.customer_email'))) AS n_mixed_case, COUNT(*) AS n_total
FROM read_parquet('data/raw/solidgate_events.parquet');
-- n_mixed_case=231, n_total=57936 (solidgate has real mixed-case emails)
```
Since Solidgate has 231 mixed-case rows and web_conversions is already
all-lowercase, matching without `lower()` would silently under-count — used
`lower()` on both sides:
```sql
WITH sg AS (SELECT DISTINCT lower(json_extract_string(data,'$.customer.customer_email')) AS email FROM read_parquet('data/raw/solidgate_events.parquet')),
     wc AS (SELECT DISTINCT lower(email) AS email FROM read_parquet('data/raw/web_conversions.parquet') WHERE email IS NOT NULL)
SELECT (SELECT COUNT(*) FROM sg) AS n_sg_distinct,
       (SELECT COUNT(*) FROM sg WHERE email IN (SELECT email FROM wc)) AS n_matched,
       (SELECT COUNT(*) FROM sg WHERE email IN (SELECT email FROM wc))*1.0/(SELECT COUNT(*) FROM sg) AS match_rate;
```
`n_sg_distinct=5558, n_matched=5339` → **match rate: 96.06%**.

`app_name` in `web_conversions`:
```sql
SELECT app_name, COUNT(*) AS n FROM read_parquet('data/raw/web_conversions.parquet') GROUP BY 1 ORDER BY n DESC;
```
| app_name | n |
|---|---|
| Invinci | 100544 |
| Unknown | 20747 |
| Atelier | 4312 |
| Yesim | 3 |
| NULL | 1 |

`app_name` in `stripe_subscriptions`:
```sql
SELECT app_name, COUNT(*) AS n FROM read_parquet('data/raw/stripe_subscriptions.parquet') GROUP BY 1 ORDER BY n DESC;
```
| app_name | n |
|---|---|
| NULL | 5587 |
| Atelier | 159 |

**`stripe_subscriptions.app_name` is 97.2% NULL (5587/5746)** and the one
populated value (`Atelier`, 159 rows) doesn't even include `Invinci` — the
dominant app in `web_conversions` (100544/125607 = 80.1% of rows). Flagged
under Surprises — this field is close to useless for joining Stripe
subscriptions to a specific app by name; `funnel_name`/`utm_source` or the
customer email→web_conversions join are the more usable app-identification
paths for Stripe rows.

## Section 7 — solidgate_old_transactions

```sql
SELECT COUNT(*) AS n, MIN("Created at" AT TIME ZONE 'UTC') AS min_c, MAX("Created at" AT TIME ZONE 'UTC') AS max_c
FROM read_parquet('data/raw/solidgate_old_transactions.parquet');
```
**1032 rows, 2025-07-21 12:38:26 → 2025-10-13 10:50:44 UTC.** 17 columns,
human-readable headers requiring double-quoting (`"Order ID"`, `"Created at"`,
etc.) — a per-transaction (not per-event) table: `Order ID, Amount, Currency,
Order status, Payment method, Email, Created at, Channel, Payment type, Card
brand, Cardholder name, Auth code, Decline code, Secured, Alert ID, Dispute
ID, Dispute status`.

```sql
SELECT "Order status", COUNT(*) AS n FROM read_parquet('data/raw/solidgate_old_transactions.parquet') GROUP BY 1 ORDER BY n DESC;
```
| Order status | n |
|---|---|
| declined | 692 |
| settled | 327 |
| refunded | 9 |
| voided | 4 |

**67.1% of rows are `declined`** (failed payment attempts), not successful
transactions.

Id overlap with `solidgate_events`:
```sql
WITH old_ids AS (SELECT DISTINCT "Order ID" AS oid FROM read_parquet('data/raw/solidgate_old_transactions.parquet')),
     ev_subs AS (SELECT DISTINCT json_extract_string(data,'$.subscription.id') AS sid FROM read_parquet('data/raw/solidgate_events.parquet'))
SELECT (SELECT COUNT(*) FROM old_ids) AS n_old_ids,
       (SELECT COUNT(*) FROM old_ids WHERE oid IN (SELECT sid FROM ev_subs)) AS n_matched_as_sub;
```
`n_old_ids=1032, n_matched_as_sub=0` — `Order ID` (`order-<epoch>` format)
never matches `$.subscription.id` (UUID format) directly, as expected —
they're different id spaces. Checked whether `Order ID` appears **nested**
inside any `solidgate_events` payload instead (the sample JSON shows orders
nested at `invoices.*.orders.*.id`, a dynamic key path not expressible as a
fixed `json_extract` — used a substring search instead):
```sql
WITH old_ids AS (SELECT DISTINCT "Order ID" AS oid FROM read_parquet('data/raw/solidgate_old_transactions.parquet'))
SELECT COUNT(*) AS n_old_ids_found_as_substring_in_any_event
FROM old_ids o
WHERE EXISTS (SELECT 1 FROM read_parquet('data/raw/solidgate_events.parquet') e WHERE e.data LIKE '%' || o.oid || '%');
```
**435/1032 (42.2%)** of old `Order ID`s do appear later inside a
`solidgate_events` payload.

**Verdict**: `solidgate_old_transactions` is a legacy transaction ledger
covering 2025-07-21 → 2025-10-13, i.e. mostly (all but ~11 days) **before**
`solidgate_events` coverage begins (2025-10-02) — it plugs the pre-history
gap. Two-thirds of it is declined attempts with no downstream subscription
(nothing to link, genuinely one-off noise for a churn/LTV dataset). The
remaining third (327 settled + a few refunded/voided) is worth keeping:
42% of all its Order IDs resurface inside `solidgate_events`, meaning a real
chunk of these early transactions belong to subscriptions that continued
into the main event stream. **Relevant, not ignorable** — but only the
`settled` subset, and only for filling the pre-Oct-2025 lifecycle gap, not
as a general-purpose event source (it has no lifecycle/event_type dimension
of its own, just point-in-time transaction attempts).

## Section 8 — Surprises

- **Stripe `api_version` corresponds to 3 embedded account/context identities, not the expected 2** (Section 2b) — `2025-08-27.basil` (tiny, 10 subs, Oct-Nov 2025), `2025-04-30.basil` (main body), `2025-12-15.clover` (new, ramping since May 2026). The 08-27→04-30 handoff is a version-string *downgrade*, unexplained from data alone.
- **A 27-subscription cohort bills daily at 99¢** (`subscription_cycle` billing_reason, `amount_paid=99`) — 2188 invoice.paid rows from just 27 subs, avg ~81 invoices/sub. Not a data error (verified as consistent, repeating chains in Section 4), but structurally unlike the rest of the (weekly/monthly) portfolio — worth a decision on whether to keep, exclude, or model separately.
- **`stripe_subscriptions.app_name` is 97.2% NULL** (5587/5746) and doesn't cover `Invinci` (the dominant app elsewhere) at all — only `Atelier` (159 rows) is populated. >20% NULL threshold from the task's own criteria, by a wide margin.
- **`stripe_subscriptions.funnel_name` is 39.5% NULL** (2267/5746) — also over the 20% threshold.
- **Missingness encoding is inconsistent across tables**: `stripe_subscriptions.utm_source` uses real SQL NULL for missing (46.3%); `web_conversions.utm_source` never uses NULL, instead spreading missing/junk across `"unknown"` (17.6%), `""` empty string (2.8%), and `"none"` (7 rows) — three different sentinels, zero true NULLs.
- **Refund events (`charge.refunded`, `refund.created`) carry 0% direct subscription/invoice linkage** in their own payload (Section 5) — 100% "orphan" by the strict "resolvable in payload" definition, though a `payment_intent` field is present 100% of the time as an indirect join key.
- Currency: **100% USD** across stripe_events invoices, stripe_subscriptions, and solidgate_events — no multi-currency handling needed for this snapshot.
- Negative amounts: **none found** outside the (not separately checked, but structurally expected) refund event types — `amount_paid` on invoices and `amount` on `stripe_payments` are both non-negative everywhere.
- Solidgate `api_version` column is a static `"solidgate_v1"` tag (100% of rows) — not a real evolving version like Stripe's; harmless but not comparable across providers.
- Same-second multi-event collisions are common and sometimes extreme on **both** providers (Stripe: up to 12/25 rows in one chain sharing a timestamp; Solidgate: up to 9 events in the same second in one observed chain) — any per-timestamp uniqueness assumption in the dataset build will silently drop rows.

## Open questions for the spec

- **`web_conversions` needs an explicit `event_date < snapshot_ts` filter in the build** — confirmed (not guessed) that the event tables were pulled with `created < snapshot_ts` while `web_conversions` was a full unfiltered `list_rows`, so it's the only table that can contain rows "from the future" relative to the manifest's snapshot boundary.
- What real-world event caused the 3-way Stripe identity split in Section 2b (`IEMVDMVTXC`→`JzVYkL7XCu`→`A9qayReKqB`, each tied to its own api_version)? New Stripe account per migration stage? Key rotation? Not answerable from local data — needs whoever owns the Stripe dashboard/account history.
- Should the 27-subscription daily-99¢-cycle cohort (Section 5/8) be included, excluded, or modeled as a separate segment in the dataset build? It's a real, consistent pattern, not noise, but structurally unlike the rest of the portfolio's billing cadence.
- For refund orphan resolution (Section 5): is it worth joining `charge.refunded`/`refund.created` events to `payment_intent.*` events (via the always-present `payment_intent` field) to indirectly recover a subscription id, or is a 100% direct-orphan rate acceptable for the spec's purposes?
- `stripe_subscriptions.app_name` (97.2% NULL) is not usable for app identification — confirm whether `funnel_name`, `utm_source`, or an email-based join to `web_conversions.app_name` should be the standardized path instead.

## Read-only verification

Baseline mtimes (recorded before any query in this session):
```
2026-07-07 11:46:12.120922000 +0200  data/raw/solidgate_events.parquet
2026-07-07 11:46:12.957587800 +0200  data/raw/solidgate_old_transactions.parquet
2026-07-07 11:46:05.993990400 +0200  data/raw/stripe_events.parquet
2026-07-07 11:46:25.589760400 +0200  data/raw/stripe_payments.parquet
2026-07-07 11:46:14.720993400 +0200  data/raw/stripe_subscriptions.parquet
2026-07-07 11:46:23.934513800 +0200  data/raw/web_conversions.parquet
```
Re-checked at the end of this task (see closing message for the final `ls -la` re-run and comparison) — no `data/raw/*.parquet` file was modified, rewritten, or deduplicated at any point; every query in this report is a pure `SELECT` over `read_parquet(...)`.
