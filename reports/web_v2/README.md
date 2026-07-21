# Table A / Table B — column legend

Source: appsflyer BigQuery (`web_conversions`), v2 pipeline (`ltv_v2/`, `web_appsflyer_v2/`), isolated from the golden Stripe/Solidgate pipeline. Population: 5,406 people (appsflyer-native attribution, `first_date >= 2026-04-13`). Refund haircut applied to all net figures (`REFUND_HAIRCUT=0.00913`, temporary stopgap — see `ltv_v2/config.py`).

## The three LTV variants, per horizon (4/12/26/52/104 weeks)

| Column suffix | Meaning | Reliability |
|---|---|---|
| `ltv_per_*_N` | **base only** — the $9.99/week `subscription_started` stream. | Reliable — weekly-ratio projection is the correct cadence for a weekly charge. |
| `ltv_per_*_N_ups_factonly` | base + upsell, but upsell added **only from real observed data**. Beyond the cohort's own age, upsell is frozen at today's value (assumed flat, not growing). | Conservative floor — never overstates, may understate. |
| `ltv_per_*_N_ups_projected` | base + upsell, upsell **projected on a monthly cadence** (the $11.99/month `upsale_converted` stream fires ~every 4 weeks, not every week). | **Projected, ~3 months of upsell history — accuracy is low. Do not use for decisions without the `low_n` and `ups_projection_quality` flags in view.** |

`ltv_per_attributed_*` divides by `n_attributed` (everyone acquired, including non-payers — the CAC/LTV unit). `ltv_per_payer_*` divides by `n_payers` (payers only — unit economics).

`current` = revenue realized as of **the cohort's own current age today**, not week 0. For a cohort already older than a given horizon N, `current` can legitimately exceed `ltv_N` — that is not a bug, it just means `current` sits further along the curve than the fixed N-week checkpoint.

## Quality flags

- `ltv_{N}_source`: `fact` (real data, cohort already reached age N) or `model` (projected).
- `ltv_{N}_ups_projection_quality`: `fact` (real data) or `rough_low_data` (projected — upsell has only ~3 months of history behind it; treat as directional, not precise).
- `low_n`: `True` when `n_payers < 40` for that cell. Small-N cells can show large `_ups_projected` values purely from the multiplier hitting a single data point — check `n_payers` before trusting any low_n cell.

## Why the monthly-cadence fix exists

The upsell stream was previously projected using the same weekly ratio as base — implicitly assuming ~4x more charging events than a monthly product actually has. That inflated `_ups` projections at long horizons (the "+21%"-class bug from the earlier audit). `ltv_per_*_N_ups_projected` now uses a separate ratio built from the same retention curve, sampled every 4 weeks instead of every week, matching upsell's real cadence.
