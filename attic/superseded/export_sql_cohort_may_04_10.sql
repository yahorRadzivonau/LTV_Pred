-- Row-level export for an apples-to-apples comparison with the Python MAP model.
-- Cohort: Stripe $9.99/week subscriptions created from 2026-05-04 through 2026-05-10.
-- Snapshot is frozen to the same date used by golden_all_se_training: 2026-07-07.
--
-- Run in BigQuery and save the result as:
--   data/sql/sql_cohort_may_04_10_rows.csv
--
-- The LEFT JOIN intentionally keeps subscriptions that never made a payment,
-- so all_subs and first-payer conversion can both be reconstructed in Python.

WITH
params AS (
  SELECT
    DATE '2026-05-04' AS cohort_start,
    DATE '2026-05-10' AS cohort_end,
    DATE '2026-07-07' AS snapshot_date
),

stripe_all_created AS (
  SELECT
    JSON_VALUE(data, '$.data.object.id')       AS sub_id,
    JSON_VALUE(data, '$.data.object.customer') AS customer_id,
    DATE(created)                              AS subscription_start_date,
    DATE_TRUNC(DATE(created), WEEK(MONDAY))    AS cohort_week,
    COALESCE(JSON_VALUE(data, '$.data.object.status'), '') AS initial_status,
    created AS created_ts
  FROM `web-payment-orchestration.prod_web_events.stripe_events`, params
  WHERE event_type = 'customer.subscription.created'
    AND COALESCE(
      JSON_VALUE(data, '$.data.object.items.data[0].price.id'),
      JSON_VALUE(data, '$.data.object.plan.id'),
      JSON_VALUE(data, '$.data.object.metadata.price_id')
    ) = 'price_1RVWUuJzVYkL7XCuWyUpC9bH'
    AND DATE(created) BETWEEN cohort_start AND cohort_end
    AND COALESCE(JSON_VALUE(data, '$.data.object.status'), '') != 'incomplete_expired'
),

paid_trial_conversions AS (
  SELECT DISTINCT
    JSON_VALUE(data, '$.data.object.id') AS sub_id
  FROM `web-payment-orchestration.prod_web_events.stripe_events`, params
  WHERE event_type = 'customer.subscription.updated'
    AND JSON_VALUE(data, '$.data.object.status') = 'trialing'
    AND JSON_VALUE(data, '$.data.previous_attributes.status') = 'incomplete'
    AND DATE(created) <= snapshot_date
),

stripe_main_starts_raw AS (
  SELECT
    sub_id, customer_id, subscription_start_date, cohort_week, created_ts
  FROM stripe_all_created
  WHERE initial_status != 'incomplete'

  UNION ALL

  SELECT
    c.sub_id, c.customer_id, c.subscription_start_date, c.cohort_week, c.created_ts
  FROM stripe_all_created c
  INNER JOIN paid_trial_conversions p USING (sub_id)
  WHERE c.initial_status = 'incomplete'
),

-- One canonical start per subscription. This prevents duplicated webhooks from
-- multiplying payment rows after the join.
main_starts AS (
  SELECT
    sub_id,
    ARRAY_AGG(customer_id IGNORE NULLS ORDER BY created_ts LIMIT 1)[SAFE_OFFSET(0)] AS customer_id,
    MIN(subscription_start_date) AS subscription_start_date,
    MIN(cohort_week) AS cohort_week
  FROM stripe_main_starts_raw
  WHERE sub_id IS NOT NULL
  GROUP BY sub_id
),

stripe_payments_raw AS (
  SELECT
    COALESCE(
      JSON_VALUE(data, '$.data.object.parent.subscription_details.subscription'),
      JSON_VALUE(data, '$.data.object.subscription')
    ) AS sub_id,
    JSON_VALUE(data, '$.data.object.id') AS invoice_id,
    event_type,
    created AS pay_ts,
    SAFE_CAST(JSON_VALUE(data, '$.data.object.amount_paid') AS FLOAT64) / 100 AS amount
  FROM `web-payment-orchestration.prod_web_events.stripe_events`, params
  WHERE event_type IN ('invoice.paid', 'invoice.payment_succeeded')
    AND SAFE_CAST(JSON_VALUE(data, '$.data.object.amount_paid') AS FLOAT64) > 0
    AND DATE(created) <= snapshot_date
),

stripe_payments AS (
  SELECT
    sub_id,
    invoice_id,
    pay_ts,
    DATE(pay_ts) AS pay_date,
    amount
  FROM (
    SELECT
      *,
      ROW_NUMBER() OVER (
        PARTITION BY invoice_id
        ORDER BY
          CASE event_type WHEN 'invoice.payment_succeeded' THEN 1 ELSE 2 END,
          pay_ts
      ) AS event_rn
    FROM stripe_payments_raw
    WHERE invoice_id IS NOT NULL
      AND sub_id IS NOT NULL
  )
  WHERE event_rn = 1
),

numbered AS (
  SELECT
    p.sub_id,
    p.invoice_id,
    p.pay_ts,
    p.pay_date,
    p.amount,
    ROW_NUMBER() OVER (
      PARTITION BY p.sub_id
      ORDER BY p.pay_ts, p.invoice_id
    ) - 1 AS rebill_number
  FROM stripe_payments p
  INNER JOIN main_starts s USING (sub_id)
)

SELECT
  s.sub_id,
  s.customer_id,
  s.subscription_start_date,
  s.cohort_week,
  n.invoice_id,
  n.pay_ts,
  n.pay_date,
  n.amount,
  n.rebill_number,
  DATE_DIFF((SELECT snapshot_date FROM params), s.cohort_week, WEEK(MONDAY)) AS cohort_age_weeks_at_snapshot
FROM main_starts s
LEFT JOIN numbered n USING (sub_id)
ORDER BY s.sub_id, n.rebill_number;
