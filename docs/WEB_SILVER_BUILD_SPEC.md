> Build spec for `web_events_silver` — the full-fidelity event table for web subscriptions (Stripe + Solidgate). Claude Code executes the steps in order, locally, from the frozen parquet snapshot. Reader is a model: read literally, act section by section.
> Silver keeps EVERYTHING with flags; the golden (training) dataset is derived later by separate rules (Section 14) and is NOT built in this task.

# Web Silver Layer Build Spec

Last updated: 2026-07-07
Source repo: LTV_Pred (local, C:\Users\yahor\PycharmProjects\LTV)
Snapshot: data/raw/*.parquet, frozen at snapshot_ts=2026-07-07T00:00:00Z per data/raw/SNAPSHOT_MANIFEST.json.
Profiling evidence: reports/SNAPSHOT_SUMMARY.md (read it before building).

## 0. Scope and non-goals

- Build ONE table: `web_events_silver`, written to `data/silver/web_events_silver.parquet`. Grain: one row = one lifecycle event of one web subscription.
- Providers: Stripe and Solidgate. PayPal is OUT entirely.
- Silver philosophy: do not drop questionable rows — FLAG them (QA subs, test utm, fraud churn, ambiguous geo, orphan refunds). Dropping happens only in the golden derivation (Section 14), which is out of scope here.
- Non-goals: no model fitting, no golden dataset, no BigQuery access, no network access.

## 1. Inputs — local parquet only

| File | Rows (manifest) | Role |
|---|---|---|
| `data/raw/stripe_events.parquet` | 361,032 | RAW Stripe webhooks; `data` column is a JSON string |
| `data/raw/solidgate_events.parquet` | 57,936 | RAW Solidgate webhooks; `data` column is a JSON string |
| `data/raw/stripe_subscriptions.parquet` | 5,746 | Stripe dimension: utm, campaign, app_name, email, interval, status |
| `data/raw/web_conversions.parquet` | 125,607 | Geo + funnel-level attribution by email (both providers) |
| `data/raw/stripe_payments.parquet` | 11,751 | Money cross-check only, not a primary source |

- `data/raw/solidgate_old_transactions.parquet` is NOT an input: settled-only partial duplicate (42% order-id overlap with solidgate_events); events cover the lifecycle.
- Verify row counts against `SNAPSHOT_MANIFEST.json` before building; on mismatch STOP.

## 2. Forbidden inputs and approaches

- Do NOT query BigQuery or any network resource. Local parquet only.
- Do NOT use the BQ `*_parsed` tables' logic as reference — it drops voluntary cancels, multiplies renewals ~4x, and loses Solidgate subscription ids.
- Do NOT modify, rewrite, or delete anything in `data/raw/`. Outputs go to `data/silver/` and `reports/` only.
- Do NOT dedup Solidgate rows by `event_id` (Section 6).
- Do NOT use bare `python` — run everything via `.venv/Scripts/python.exe` (bare python on PATH lacks duckdb).

## 3. Global rules — apply everywhere

- All timestamp logic in UTC. **Single rule (owner-accepted 2026-07-07):** set duckdb session `TimeZone='UTC'` at connect, so bare `created` reads out already UTC; then immediately after any timestamp column leaves duckdb into pandas, normalize it with a single helper `to_utc(df, cols)` — tz-naive → `tz_localize('UTC')`, tz-aware → `tz_convert('UTC')` — BEFORE any concat/sort/compare. Do NOT sprinkle `AT TIME ZONE 'UTC'` per-query (with a UTC session it re-derives a tz-NAIVE value and reintroduces the naive-vs-aware mix that this rule exists to prevent). Never compare timestamps as strings.
- Raw JSON money is integer CENTS: divide by 100 exactly once, at extraction. Verified: 3999=$39.99, 999=$9.99, 99=$0.99.
- Normalize NULL encodings on ingest: treat `''`, `'null'`, `'none'`, `'unknown'` string variants as NULL for utm/campaign/country/app fields (tables encode missing inconsistently).
- Lowercase emails on BOTH sides of every email join (231 subscriptions have mixed-case emails; unlowered join loses them).
- When filtering `web_conversions` by time, apply `event_date < snapshot_ts` (the dimension pull was not gated by the snapshot cutoff; event tables were).

## 4. Output schema for `web_events_silver`

| Column | Type | Meaning |
|---|---|---|
| `subscription_id` | STRING | Stripe `sub_...`; Solidgate subscription UUID. Never `sub_sched_...`. NULL allowed only on unattachable refunds. |
| `customer_user_id` | STRING | Provider customer id. Per-provider; no cross-provider merge. |
| `payment_provider` | STRING | `stripe` / `solidgate` |
| `stripe_account` | STRING | Stripe identity fragment: `JzVYkL7XCu` / `IEMVDMVTXC` / `A9qayReKqB`; NULL for Solidgate. Extract from object ids at fixed offsets (in_: 10-19, sub_: 11-20, price_: 13-22 — see SNAPSHOT_SUMMARY.md 2b). |
| `app_id` | STRING | Section 8.6 |
| `event_name` | STRING | iOS vocabulary; Sections 5-6 |
| `event_datetime` | TIMESTAMP (UTC) | Section 8.1 ordering |
| `install_date` | DATE | Section 8.2 |
| `country_code` | STRING | Section 8.4 |
| `geo_ambiguous` | BOOL | TRUE if the email maps to >1 country (8.14% of emails) |
| `geo_status` | STRING | `resolved` (country_code set) / `unknown` (email matched web_conversions but country was `unknown`/missing there) / `no_match` (email absent from web_conversions or no email). Disambiguates why `country_code` is NULL — owner requirement 2026-07-07. |
| `media_source` | STRING | Section 8.5; NULL = truly unattributed |
| `media_source_step` | STRING | `subs_dim` / `web_conversions` / NULL — which step resolved it |
| `campaign` | STRING | Section 8.5 |
| `product_id`, `price_id` | STRING | Section 8.7 |
| `interval_unit` | STRING | `day` / `week` / `month` / `year` (day = QA, kept and flagged); NULL if neither name nor cadence yields one |
| `interval_source` | STRING | `plan` (Stripe) / `from_name` / `inferred_from_cadence` / NULL — how `interval_unit` was derived (Section 8.10) |
| `trial_type` | STRING | `free` / `paid` / `none` — Section 8.3; DECISION #1: paid = actually captured money only |
| `price_amount` | FLOAT | USD dollars for money events; NULL otherwise |
| `churn_type` | STRING | `voluntary` / `involuntary` / `fraud` / `unknown`; only on terminal events, else NULL |
| `is_qa` | BOOL | TRUE for daily-interval 99¢ QA subs and `utm_source='test'` |
| `refund_orphan` | BOOL | TRUE if a refund could not be attached via the payment_intent path |
| `raw_event_type` | STRING | original provider event type, for audit |

## 5. Stripe mapping

Subscription-id extraction:

| Event family | Path |
|---|---|
| `customer.subscription.*` | `$.data.object.id` |
| `invoice.*` | `$.data.object.parent.subscription_details.subscription` — VERIFIED ~100% on BOTH basil and clover eras; the flat `$.data.object.subscription` is 0% everywhere, do not implement a fallback to it |
| `charge.*` / `payment_intent.*` / refunds | resolve via payment_intent → its invoice → subscription |

State mapping:

| Raw `event_type` (+condition) | `event_name` |
|---|---|
| `customer.subscription.created` | `trial_started` |
| `invoice.paid`, `billing_reason='subscription_create'` | trial-entry charge: sets `trial_type`; emit as `trial_started`-money marker, not a renewal |
| `invoice.paid`, `billing_reason='subscription_create'`, `amount_paid>0` | trial-entry money: ENRICHES the existing `trial_started` row (sets `trial_type='paid'`, `price_amount`); emits NO second row (owner clarification #1, 2026-07-07) |
| first `invoice.paid`, `billing_reason='subscription_cycle'` | `trial_converted` |
| later `invoice.paid`, `billing_reason='subscription_cycle'` | `subscription_renewed` |
| `invoice.payment_failed` | `billing_issue_detected` |
| `customer.subscription.deleted`, reason=`cancellation_requested` | `subscription_expired`, `churn_type='voluntary'` |
| `customer.subscription.deleted`, reason=`payment_failed` | `subscription_expired`, `churn_type='involuntary'` |
| `customer.subscription.deleted`, any other reason | `subscription_expired`, `churn_type='unknown'` + report the reason text (full-table check found ONLY the two values above, 2,718 + 674 = 3,392, zero NULL — this branch is a guard) |
| `charge.refunded` / `refund.created` | `subscription_refunded` (attach per Section 8.8) |
| `invoice.upcoming` | DROP (preview, amount 0) |
| `invoice.marked_uncollectible` | keep as audit-only row, `event_name='billing_issue_detected'`, flag in `raw_event_type` |
| all other Stripe types (`invoice.updated`, `payment_intent.*`, `customer.updated`, `charge.succeeded`, ...) | do not emit rows; they are duplicates/plumbing of the above |

Mandatory dedup rules:
- One payment emits ~4 events (`invoice.finalized`/`paid`/`payment_succeeded`/`updated`) with one invoice id. Collapse to ONE money event keyed by invoice id. This is the root cause of the parsed layer's 4x renewal inflation.
- Duplicate `event_id` rows: keep one; duplicates are delivery retries differing only in `pending_webhooks`. Guard-assert: within one `event_id`, JSON with `pending_webhooks` stripped must be identical; else STOP.

## 6. Solidgate mapping

- `event_id` is a synthetic composite `{event_type}__{uuid}__{uuid}__{status}` WITHOUT time — it legitimately repeats across weeks (max 69 rows). NEVER dedup by event_id; drop only exact `(event_id, created, data)` triple duplicates (6 rows known).
- Subscription id: `$.subscription.id`. Email: `$.customer.customer_email` (verified 100% coverage on all 11 event types).
- Order records live under `$.invoices.<invoice_id>.orders.<order_id>` with DYNAMIC keys: unnest both maps to read each order's `status`, `amount`, `processed_at`, `retry_attempt`.

| Raw `event_type` (+condition) | `event_name` |
|---|---|
| `create` / `active` | `trial_started` (dedup the pair into one entry) |
| `recurring` or `renew` with order `status='success'` | first → `trial_converted`, later → `subscription_renewed` |
| order `status='auth_failed'` | `billing_issue_detected` — ORDER-driven (owner rule 2026-07-07): ONE row per real failed charge = one `auth_failed` order, deduped by `order_id`, dated at the order's `processed_at`, `retry_attempt` kept in the row. Rationale: Stage 3 needs dunning *intensity* = real state events (failed charges), not scheduler webhook noise. |
| `retry`, `scheduled_for_retry` webhooks | audit-only (recorded in `raw_event_type`); NO state row — scheduler chatter, not a life fact. The failed-charge signal comes from the `auth_failed` order above, not from these events. |
| order `status='settle_ok'` with `amount>0` | successful CAPTURE (Section 7: success lives in order status, never event type) → first → `trial_converted`, later → `subscription_renewed`, dated `processed_at` |
| order `status='refunded'` | `subscription_refunded` |
| order `status IN ('auth_ok','processing','void_ok')` | audit-only; no state row (`auth_ok`=hold not capture, `processing`=in-flight/censored, `void_ok`=auth reversal) |
| subscription status `redemption` (on any event) | `entered_grace_period` (one per distinct entry into redemption) |
| `expire` | `subscription_expired`, `churn_type='involuntary'` |
| `cancel` | `subscription_expired`, `churn_type` from the lookup below |
| `order_update`, `scheduled_for_cancellation` | audit-only; no state row (but note `cancel_message` appears on them too) |

`cancel_message` → `churn_type` lookup (full observed vocabulary):

| `cancel_message` | `churn_type` |
|---|---|
| `Cancellation after redemption period` | `involuntary` |
| `Card Token has expired` | `involuntary` |
| `Cancellation by support` | `voluntary` |
| `Token revoked by customer` | `voluntary` |
| `Fraud Alert received`, `Fraud Decline received`, `Recurring payment is blocked by Antifraud`, `Bank antifraud system`, `Fraud Chargeback received` | `fraud` |
| any unseen value | `unknown` + report text and count |

## 7. Money-success semantics

- Success/failure of a Solidgate charge is in the ORDER status, never in the event type. A `recurring` event with `auth_failed` orders is a dunning row, not a renewal. Observed order-status vocabulary (post order-id dedup): `auth_failed` (14,217 = failed charge → `billing_issue_detected`), `settle_ok` (6,146 = captured money → `trial_converted`/`subscription_renewed`), `auth_ok` (3,783 = hold, mostly $0 trial validation → audit), `processing` (1,358 = in-flight/censored → audit), `refunded` (40 → `subscription_refunded`), `void_ok` (1 = auth reversal → audit).
- Stripe `payment_intent.*`/`charge.*` do not create money rows; `invoice.paid` (deduped) is the single money source. Cross-check totals against `stripe_payments.parquet` in the self-check.

## 8. Derived fields

### 8.1 Ordering
Sort within a subscription by `event_datetime` UTC, then invoice id/billing period (Stripe) or order `processed_at` + `retry_attempt` (Solidgate). Raw arrival order and same-second collisions are NOT a valid order.

### 8.2 install_date
Stripe: subscription `start_date`/`created` from `customer.subscription.created`. Solidgate: `$.subscription.started_at`. UTC date.

### 8.3 trial_type (DECISION #1 accepted)
`paid` only if trial-entry money was actually CAPTURED (>0). Stripe: first `subscription_create` invoice with `amount_paid > 0`. Solidgate: `payment_action='auth_0_amount'` is a hold, NOT a capture → `free` even when `trial_amount > 0`. `none` = no trial phase. Never derive from product names.

### 8.4 country_code + geo_ambiguous (DECISION #2 accepted)
Join lowercased email → `web_conversions` (filtered `event_date < snapshot_ts`). If one country → use it, `geo_ambiguous=FALSE`. If several → pick the country whose `event_date` is at-or-before `install_date` and closest to it (none before → earliest), set `geo_ambiguous=TRUE`. NEVER `ANY_VALUE`.

### 8.5 media_source / campaign, two-step (DECISION #5 accepted)
Step 1 (Stripe rows): `stripe_subscriptions.utm_source`/`campaign_name` by sub_id. Step 2 (NULL after step 1, and ALL Solidgate rows): `web_conversions.utm_source`/`campaign_name` by lowercased email, same tie-break as geo. Record which step won in `media_source_step`. Treat `''`/`unknown`/`none`/`test` as NULL (but `test` also sets `is_qa=TRUE`). NULL stays NULL — never relabel as `organic`, never fill with a provider name.

### 8.6 app_id
Priority: (1) `stripe_subscriptions.app_name` when non-NULL (only ~2.8% of rows — `Atelier` mostly); (2) mapping `stripe_account` → app, built from the observed `app_name × stripe_account` cross-tab — REPORT this cross-tab in the self-check; hypothesis to test: `A9qayReKqB` (live since 2026-05) corresponds to Atelier (launched 2026-06-10); (3) fallback `invinci`. Solidgate: `web_conversions.app_name` via email if available, else `invinci`. Do not hardcode a single constant for all rows.

### 8.7 product_id / price_id
Stripe: `prod_...` and `price_...` are different fields — keep both. Solidgate: one UUID → put into both, don't treat as distinct.

### 8.8 Refund attachment
Attach via `payment_intent` → its invoice → subscription (direct subscription field on refunds is 100% absent). Unattachable → keep row, `subscription_id=NULL`, `refund_orphan=TRUE`. Never drop silently, never guess an attachment.

### 8.9 Death shift to end of paid period
Terminal `subscription_expired` gets `event_datetime` = period end, not the cancel/delete moment. Stripe: `current_period_end` / `ended_at`/`cancel_at` from the deleted object. Solidgate: `$.subscription.expired_at` (populated 100% on `cancel`); on `expire` events it is 0% populated → derive as last successful charge datetime + one `interval_unit`. Keep the raw click/delete moment in an extra column `churn_signal_datetime` for later 3-map work.

### 8.10 interval_unit
Stripe: `plan.interval`. Solidgate: `$.product.name` when it encodes cadence (`weekly`→week, `monthly`→month, `6 months`→month, etc.), `interval_source='from_name'`. Most Solidgate names do NOT encode it (`Invinci Paid Trial`, `TRIAL_UPSALE_COMBO`, `Antivirus Test` → 1,143 renewing subs with no parseable interval); for those, INFER from the median gap between consecutive `settle_ok` orders (owner-accepted 2026-07-07 deviation from name-only parse): gap≈7d→week, ≈30d→month, ≈365d→year, ≤2d→day, `interval_source='inferred_from_cadence'` (needs ≥2 settle_ok orders). Neither available → NULL, `interval_source=NULL`. Report the full observed name set, the parse mapping, and the from_name/inferred/NULL split.

## 9. Flags, not filters

Silver DROPS nothing except: `invoice.upcoming`, non-emitting plumbing event types (Section 5 last row), exact Solidgate triple-duplicates, and PayPal (absent anyway). Everything else stays with flags: QA daily subs → `is_qa=TRUE` (27 subs known, 633-639 events each — they are the extreme outliers), `utm_source='test'` → `is_qa=TRUE`, fraud churn → `churn_type='fraud'`, ambiguous geo → `geo_ambiguous=TRUE`, orphan refunds → `refund_orphan=TRUE`.

## 10. DO NOT — known traps

- DO NOT dedup Solidgate by `event_id` — you will delete real weekly rebill events.
- DO NOT count each Stripe invoice event variant as a payment — collapse by invoice id.
- DO NOT map Solidgate `cancel` by event type — only via the `cancel_message` lookup.
- DO NOT order chains by raw timestamp alone; same-second collisions are the norm.
- DO NOT use `ANY_VALUE` for geo; 8% of emails are multi-country.
- DO NOT relabel NULL attribution as `organic`; DO NOT fill `media_source` with a provider name.
- DO NOT read amounts as dollars (raw is cents) or capture-vs-hold as the same thing (`auth_0_amount` is a hold).
- DO NOT join emails without lowercasing both sides.
- DO NOT let a `sub_sched_...` id into `subscription_id`.
- DO NOT place death at click time — shift to period end (raw moment goes to `churn_signal_datetime`).
- DO NOT touch `data/raw/*` files; DO NOT access the network.

## 11. Preflight asserts — hard-fail before writing silver

1. Row counts of all 5 input files match `SNAPSHOT_MANIFEST.json`.
2. Stripe benign-dup guard: per duplicated `event_id`, payloads identical after stripping `pending_webhooks`; else STOP.
3. `subscription_id` format: `^sub_` or UUID; zero `sub_sched_`; NULL only where `refund_orphan=TRUE`.
4. Per subscription: `subscription_renewed` count ≤ physically possible periods since `install_date` given `interval_unit` (catches 4x inflation). QA daily subs included in the check with day granularity.
5. `price_amount` in dollars: known ladder 0.99 / 9.99 / 39.99 present; max ≤ 200.
6. 100% of money rows USD; else STOP.
7. `churn_type` non-NULL only on terminal rows; distribution reported per provider; `unknown` > 5% of terminals → STOP and print the unmapped reasons/messages.
8. Stripe terminal totals reconcile with raw: voluntary ≈ 674, involuntary ≈ 2,718 (±2% tolerance for edge dedup); large deviation → STOP.
9. Geo coverage ≥ 90% on both providers (raw joins measured 97.3% Stripe / 96.1% Solidgate); far below → the email join broke.
10. `media_source` never equals `solidgate`/`paypal`; `media_source_step` populated wherever `media_source` is.
11. Every input `subscription_id` seen in raw events appears in silver at least once (nothing silently lost end-to-end).
12. `stripe_account` non-NULL on 100% of Stripe rows; exactly the 3 known identities; any 4th value → STOP and report.
13. Exactly ONE `trial_started` row per `subscription_id` (owner clarification #1: the `create`/`active` pair on Solidgate and the `customer.subscription.created` + `subscription_create` invoice on Stripe each collapse to a single `trial_started`); any subscription with 0 or ≥2 → STOP and report.

## 12. Self-check artifacts (write to reports/)

- `silver_profile.md`: row/sub/user counts per provider; `event_name` distribution per provider; `churn_type` distribution per provider; `app_name × stripe_account` cross-tab with verdict on the Atelier hypothesis; trial_type distribution; attribution coverage by step; geo coverage + ambiguity rate; QA/fraud/orphan flag counts.
- `silver_chains_sample.md`: 20 random subscriptions (10 Stripe / 10 Solidgate, stratified: include ≥2 voluntary deaths, ≥2 involuntary, ≥1 fraud, ≥2 alive renewers, ≥1 QA) rendered as full ordered chains. The owner reads all 20 by eye before silver is used.
- Observed weekly survival curve (simple KM or step-share by weeks-since-install) per provider, as a table in `silver_profile.md` — the "real graph" baseline the owner asked for.

DONE when: `data/silver/web_events_silver.parquet` exists, all 12 asserts pass, both report files exist, and `data/raw/*` mtimes are unchanged (state them).

## 13. Stop-points

1. After implementing Sections 5-7 mapping code, BEFORE running on full data: show the mapping code summary + one hand-traced chain per provider. Wait for owner OK.
2. After asserts run: report pass/fail per assert. Any fail → stop.
3. After self-check artifacts: owner reads the 20 chains and the account×app cross-tab. Silver is not "accepted" until then.

## 14. Golden derivation (recorded for later; NOT built now)

Accepted owner decisions to apply when deriving the training set from silver:
1. `trial_type`: captured-money definition (already in silver).
2. Geo: tie-break rule in silver; whether `geo_ambiguous` rows enter segment-level fitting — decide after looking at silver profile.
3. Fraud churn: excluded from survival fitting (kept in silver); may become a lever category instead — decide on silver numbers.
4. Churn stays ONE event with `churn_type` attribute; one-map vs three-map decision comes AFTER the silver diagnostic (voluntary/involuntary/fraud shares + observed curve shape). Note: raw Stripe terminals are already known to be ~80% involuntary.
5. Solidgate included; provider vs media_source as levers decided by LOO at model stage, both fields present in silver.
Golden will additionally drop: `is_qa=TRUE`, and possibly `refund_orphan` money rows — decided then.
