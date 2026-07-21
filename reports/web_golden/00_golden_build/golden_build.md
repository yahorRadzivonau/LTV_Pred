# Golden build — progress log

Status: **STOP-POINT 1 reached, awaiting OK before Step 2.**

## Step 0 — BQ enrichment pull (one-off, read-only)

Pulled from `web-payment-orchestration` BigQuery project on 2026-07-09. After this, everything downstream is local-only (no further BQ access), per spec.

| Pull | Source | Columns | Rows | Note |
|---|---|---|---|---|
| Stripe product_id | `prod_silver_layer.stripe_subscriptions` | `sub_id`, `product_id` | 5,755 | `sub_id` verified unique (matches source's own distinct count) |
| Web conversions country | `prod_silver_layer.web_conversions` | `email`, `country` | 40,017 | Aggregated server-side (one row per distinct email, country = most recent `event_date`). Verified rows == distinct email, zero duplicates. |

Saved to `data/raw/bq_enrich_stripe_product_id_2026-07-09.parquet` and `data/raw/bq_enrich_web_conversions_country_2026-07-09.parquet`; pull recorded in `data/raw/SNAPSHOT_MANIFEST.json` under a new non-destructive `bq_enrich_pulls` key (original frozen `snapshot_ts`/`tables` untouched).

**Operational note (not a data-quality issue, worth recording):** the BQ MCP tool truncates any single result to ~3,000 rows / ~160KB regardless of query shape (confirmed empirically — happens identically whether using `LIMIT`/`OFFSET` or a `WHERE`-filtered bucket). Separately, plain `LIMIT n OFFSET m` pagination against `stripe_subscriptions` was **non-deterministic across separate query executions** — two supposedly-disjoint pages overlapped by 1,512 rows despite `sub_id` being a true unique key (verified `COUNT(*) == COUNT(DISTINCT sub_id) == 5755`). Fixed by partitioning with `MOD(ABS(FARM_FINGERPRINT(sub_id)), N)` instead of `OFFSET` — deterministic, non-overlapping, and verified complete by exact row-count sums (2879+2876=5755 for Stripe; 14 buckets summing to exactly 40,017 for web_conversions, matching an independent `COUNT(DISTINCT email)` check). Total BQ bytes processed: 70.9 MB — trivial cost, both source tables are MB-sized.

## Step 1 — app_id reclassification by product_id

Reclassified `app_id` in `data/silver/web_events_silver.parquet` using **product_id only**, not `app_name` (NULL before 2026-04-16 in the source dimension — using it would silently misclassify everything before that date).

Backup of the pre-change file kept at `data/silver/web_events_silver_pre_appid_v2_backup.parquet`. The old classification is preserved alongside the new one as `app_id_v1_silver`; the product_id actually used (BQ-refreshed for Stripe rows, silver's own for Solidgate) is kept as `product_id_used_for_app_id_v2`.

**Classification rules applied:**

- Stripe: `prod_SQNFtsL7NYSNdY` → `invinci`; `prod_UVk5DBWXTsemwf` → `atelier`; any other non-null product_id → `other_old`; null product_id → `unknown_old`.
- Solidgate: dominant Invinci-branded product_ids → `invinci`; other non-null → `other_old`; null → `unknown_old`.
- Stripe rows were rejoined to the **fresh BQ product_id** (Step 0 pull) rather than silver's own `product_id` column, since the BQ dimension table is live/authoritative — 43,340 of 43,499 Stripe rows got a BQ match; the 159 unmatched rows (silver's own product_id was already null — these are the `refund_orphan=TRUE` rows with no `subscription_id` to join on at all) fell through to `unknown_old`.

**⚠️ Flagged decision, not silently resolved — needs your confirmation:** the spec says Solidgate's "dominant" product_id → invinci (singular), but the raw `product.name` field shows **two** large, clearly Invinci-branded product_ids of comparable size:

| product_id | `product.name` (raw) | amount | raw events | 
|---|---|---|---|
| `a9730996-e563-42d7-8857-fe8ff20c4034` | `weekly` | $9.99 | 25,800 |
| `5a337f23-dbb3-4569-b7e5-04cd1008dadf` | **`Invinci Paid Trial`** | $9.99 | 21,494 |

A literal "top-1 by count → invinci, else other_old" reading would exclude the second one despite it being *explicitly* named "Invinci Paid Trial" — almost certainly the trial-entry SKU for the same subscription funnel as the first (generic "weekly" = recurring-charge SKU). I classified **both** as `invinci`, plus three small clearly-related "Invisi" (typo for Invinci) SKUs — `86870f81…` (monthly, 118 rows), `6bffb570…` (weekly, 103 rows), `ce467a85…` (6-month, 17 rows), all with "paid trial" in the name. Everything else (`TRIAL_UPSALE_COMBO`, `TRIAL_UPSALE_WEB`, `TRIAL_UPSALE_IDENTITY`, `Antivirus Test`) has no Invinci branding evidence → `other_old`.

**If this is wrong** (e.g. `5a337f23` should actually be `other_old`), say so now — `app_id_v1_silver` and `product_id_used_for_app_id_v2` are both in the file so this is a one-line fix, not a rebuild.

## Cross-tab: app_id × payment_provider × product_id (post-reclassification)

| app_id | provider | product_id | n rows |
|---|---|---|---|
| atelier | stripe | prod_UVk5DBWXTsemwf | 1,587 |
| invinci | stripe | prod_SQNFtsL7NYSNdY | 39,214 |
| invinci | solidgate | a9730996-e563-42d7-8857-fe8ff20c4034 | 16,089 |
| invinci | solidgate | 5a337f23-dbb3-4569-b7e5-04cd1008dadf | 15,717 |
| invinci | solidgate | 86870f81-884b-4a62-995b-e22ee8cf286b | 114 |
| invinci | solidgate | 6bffb570-3130-4081-a009-a59ec5cc207c | 46 |
| invinci | solidgate | ce467a85-8f94-4336-8ad9-98778bb24098 | 20 |
| other_old | solidgate | e5dc766f-32e1-4435-aa15-f53dba5c3b47 | 3,460 |
| other_old | solidgate | e357b6fb-05a1-4a1b-b6f0-cdbbfdc80567 (Antivirus Test) | 2,819 |
| other_old | stripe | prod_U7L8rAhwVnJLng | 1,525 |
| other_old | stripe | prod_U5p844CSrmmwTu | 534 |
| other_old | stripe | prod_TUjZNGpthSaFe5 | 385 |
| other_old | stripe | prod_TpdUoKIWgpQBQy | 70 |
| other_old | solidgate | fbc98c19-ba59-4601-b10b-f4f6cb49bbe5 | 55 |
| other_old | solidgate | d26782cc-b24c-4468-b4ef-0d0b3fa1f9c8 | 50 |
| other_old | stripe | prod_UYE73iJSodgpDz | 13 |
| other_old | stripe | prod_TCjnZXhcA0xjAT | 12 |
| unknown_old | — | (null, refund_orphan rows) | 159 |

**N per app_id (rows / distinct subscriptions):**

| app_id | rows | distinct subscriptions |
|---|---|---|
| invinci | 71,200 | 11,526 |
| other_old | 8,923 | 2,599 |
| atelier | 1,587 | 353 |
| unknown_old | 159 | 0 (all `refund_orphan=TRUE`, no `subscription_id`) |

**Old (v1) vs new (v2) app_id, row-level:**

| v1 → | atelier | invinci | other_old | unknown_old |
|---|---|---|---|---|
| **atelier** | 1,587 | 0 | 534 | 0 |
| **invinci** | 0 | 71,200 | 8,389 | 159 |

Two corrections from the old (`stripe_account`/`app_name`-based) classification: 534 rows previously bucketed as `atelier` (via the shared `A9qayReKqB` Stripe account) turn out to have a non-Atelier `product_id` → now `other_old`. 8,389 + 159 rows previously defaulted to `invinci` (the old fallback for anything unrecognized) are now correctly split into `other_old` (a real, different old product) and `unknown_old` (orphan refunds with no attachable product at all).

---

**Waiting for OK before Step 2 (golden filters).**
