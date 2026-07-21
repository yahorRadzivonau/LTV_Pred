# Involuntary-churn decline diagnostic

READ-ONLY over data/raw/* and frozen data/silver/web_events_silver.parquet (silver NOT modified). Goal: split involuntary terminals into **retryable** ("no money now") vs **hard_dead** (issuer/fraud/expired) vs **ambiguous**.

## 1. Stripe decline codes

Path check: `invoice.payment_failed` carries NO code; specific reason is on `charge.failed.$.data.object.outcome.reason` (+ generic `failure_code`) and `payment_intent.payment_failed.$.data.object.last_payment_error.decline_code`. Using charge.failed.outcome.reason (customer 100%, invoice 0% -> linked by customer_id+time).

### 1a. ALL charge.failed (n=18158) — dunning + terminal together

| outcome.reason | n | share | class |
|---|---|---|---|
| do_not_honor | 4971 | 27.4% | hard_dead |
| insufficient_funds | 4818 | 26.5% | retryable |
| transaction_not_allowed | 2260 | 12.4% | hard_dead |
| try_again_later | 1638 | 9.0% | retryable |
| highest_risk_level | 1379 | 7.6% | hard_dead |
| previously_declined_do_not_retry | 1008 | 5.6% | hard_dead |
| generic_decline | 726 | 4.0% | ambiguous |
| invalid_account | 519 | 2.9% | hard_dead |
| stolen_card | 151 | 0.8% | hard_dead |
| pickup_card | 144 | 0.8% | hard_dead |
| incorrect_number | 132 | 0.7% | hard_dead |
| lost_card | 112 | 0.6% | hard_dead |
| expired_card | 77 | 0.4% | hard_dead |
| card_velocity_exceeded | 77 | 0.4% | retryable |
| revocation_of_authorization | 59 | 0.3% | hard_dead |
| authentication_required | 39 | 0.2% | ambiguous |
| invalid_cvc | 17 | 0.1% | ambiguous |
| incorrect_cvc | 15 | 0.1% | ambiguous |
| restricted_card | 5 | 0.0% | ambiguous |
| call_issuer | 4 | 0.0% | ambiguous |
| processing_error | 4 | 0.0% | retryable |
| invalid_amount | 2 | 0.0% | ambiguous |
| stop_payment_order | 1 | 0.0% | ambiguous |

### 1b. TERMINAL involuntary (n=2718) — last decline before death (customer+time link)

> ⚠ LINKAGE CAVEAT: `charge.failed` has no invoice/subscription field (only customer_id); 1239 of 2718 terminals (46%) belong to customers with >1 involuntary sub, so their code may come from another sub's dunning. The unbiased split is on the clean subset (1c).

| last outcome.reason | n | share | class |
|---|---|---|---|
| do_not_honor | 479 | 17.6% | hard_dead |
| insufficient_funds | 444 | 16.3% | retryable |
| NULL | 430 | 15.8% | no_charge.failed_linked |
| previously_declined_do_not_retry | 287 | 10.6% | hard_dead |
| highest_risk_level | 284 | 10.4% | hard_dead |
| transaction_not_allowed | 278 | 10.2% | hard_dead |
| try_again_later | 196 | 7.2% | retryable |
| invalid_account | 102 | 3.8% | hard_dead |
| generic_decline | 68 | 2.5% | ambiguous |
| pickup_card | 40 | 1.5% | hard_dead |
| stolen_card | 36 | 1.3% | hard_dead |
| incorrect_number | 30 | 1.1% | hard_dead |
| lost_card | 30 | 1.1% | hard_dead |
| expired_card | 7 | 0.3% | hard_dead |
| card_velocity_exceeded | 4 | 0.1% | retryable |
| revocation_of_authorization | 2 | 0.1% | hard_dead |
| restricted_card | 1 | 0.0% | ambiguous |

### 1c. TERMINAL involuntary — CLEAN subset (unique-customer link, n=1057) — UNBIASED

| last outcome.reason | n | share | class |
|---|---|---|---|
| do_not_honor | 243 | 23.0% | hard_dead |
| insufficient_funds | 186 | 17.6% | retryable |
| transaction_not_allowed | 143 | 13.5% | hard_dead |
| previously_declined_do_not_retry | 130 | 12.3% | hard_dead |
| try_again_later | 92 | 8.7% | retryable |
| highest_risk_level | 89 | 8.4% | hard_dead |
| invalid_account | 52 | 4.9% | hard_dead |
| generic_decline | 40 | 3.8% | ambiguous |
| stolen_card | 22 | 2.1% | hard_dead |
| pickup_card | 21 | 2.0% | hard_dead |
| incorrect_number | 18 | 1.7% | hard_dead |
| lost_card | 12 | 1.1% | hard_dead |
| expired_card | 5 | 0.5% | hard_dead |
| card_velocity_exceeded | 2 | 0.2% | retryable |
| restricted_card | 1 | 0.1% | ambiguous |
| revocation_of_authorization | 1 | 0.1% | hard_dead |

## 2. Solidgate decline codes

Order keys: `['amount','created_at','failed_reason','id','operation','processed_at','status','updated_at']` — decline lives in numeric **`failed_reason`** (no textual field). Meanings below are a **PROVISIONAL external mapping** (Solidgate public code reference), NOT derived from the snapshot — **owner to confirm**.

### 2a. ALL auth_failed orders (n=14217)

| failed_reason | n | share | meaning (provisional) | class |
|---|---|---|---|---|
| 3.02 | 5970 | 42.0% | Insufficient funds | retryable |
| 3.10 | 2890 | 20.3% | Do not honor | hard_dead |
| 3.08 | 1957 | 13.8% | Card limit / insufficient | retryable |
| 3.04 | 929 | 6.5% | Stolen card / restricted | hard_dead |
| 4.03 | 673 | 4.7% | Fraud / security decline | hard_dead |
| 3.12 | 640 | 4.5% | Invalid card / do not honor | hard_dead |
| 3.07 | 212 | 1.5% | Expired card | hard_dead |
| 4.09 | 152 | 1.1% | Fraud / antifraud block | hard_dead |
| 4.02 | 119 | 0.8% | Fraud suspected | hard_dead |
| 4.04 | 76 | 0.5% | Fraud / lost-stolen | hard_dead |
| 5.04 | 74 | 0.5% | Technical / processing | retryable |
| 3.11 | 70 | 0.5% | Card restriction | hard_dead |
| 2.02 | 56 | 0.4% | Validation / invalid data | hard_dead |
| 2.08 | 55 | 0.4% | Invalid card data | hard_dead |
| 4.05 | 55 | 0.4% | Fraud / risk | hard_dead |
| 2.01 | 55 | 0.4% | Validation error | hard_dead |
| 3.03 | 52 | 0.4% | Insufficient / limit | retryable |
| 5.01 | 51 | 0.4% | Technical / gateway | retryable |
| 2.15 | 38 | 0.3% | (unmapped — confirm) | ambiguous |
| 5.08 | 31 | 0.2% | (unmapped — confirm) | ambiguous |
| 2.09 | 14 | 0.1% | (unmapped — confirm) | ambiguous |
| 2.12 | 10 | 0.1% | (unmapped — confirm) | ambiguous |
| 2.10 | 9 | 0.1% | (unmapped — confirm) | ambiguous |
| 4.01 | 7 | 0.0% | (unmapped — confirm) | ambiguous |
| 3.05 | 7 | 0.0% | (unmapped — confirm) | ambiguous |
| 0.03 | 6 | 0.0% | (unmapped — confirm) | ambiguous |
| 1.01 | 4 | 0.0% | (unmapped — confirm) | ambiguous |
| 2.06 | 2 | 0.0% | (unmapped — confirm) | ambiguous |
| 2.11 | 1 | 0.0% | (unmapped — confirm) | ambiguous |
| 5.06 | 1 | 0.0% | (unmapped — confirm) | ambiguous |
| 3.01 | 1 | 0.0% | (unmapped — confirm) | ambiguous |

### 2b. TERMINAL involuntary only (n=4379 subs with an auth_failed order) — last decline code

| last failed_reason | n | share | meaning (provisional) | class |
|---|---|---|---|---|
| 3.10 | 1222 | 27.9% | Do not honor | hard_dead |
| 3.02 | 939 | 21.4% | Insufficient funds | retryable |
| 3.08 | 697 | 15.9% | Card limit / insufficient | retryable |
| 3.04 | 325 | 7.4% | Stolen card / restricted | hard_dead |
| 4.03 | 298 | 6.8% | Fraud / security decline | hard_dead |
| 3.12 | 219 | 5.0% | Invalid card / do not honor | hard_dead |
| 3.07 | 189 | 4.3% | Expired card | hard_dead |
| 4.09 | 101 | 2.3% | Fraud / antifraud block | hard_dead |
| 4.02 | 58 | 1.3% | Fraud suspected | hard_dead |
| 2.02 | 56 | 1.3% | Validation / invalid data | hard_dead |
| 2.01 | 55 | 1.3% | Validation error | hard_dead |
| 5.01 | 51 | 1.2% | Technical / gateway | retryable |
| 2.08 | 39 | 0.9% | Invalid card data | hard_dead |
| 4.04 | 32 | 0.7% | Fraud / lost-stolen | hard_dead |
| 5.08 | 25 | 0.6% | (unmapped — confirm) | ambiguous |
| 3.03 | 19 | 0.4% | Insufficient / limit | retryable |
| 2.15 | 17 | 0.4% | (unmapped — confirm) | ambiguous |
| 2.09 | 12 | 0.3% | (unmapped — confirm) | ambiguous |
| 2.12 | 10 | 0.2% | (unmapped — confirm) | ambiguous |
| 1.01 | 3 | 0.1% | (unmapped — confirm) | ambiguous |
| 4.05 | 3 | 0.1% | Fraud / risk | hard_dead |
| 2.06 | 2 | 0.0% | (unmapped — confirm) | ambiguous |
| 0.03 | 1 | 0.0% | (unmapped — confirm) | ambiguous |
| 3.01 | 1 | 0.0% | (unmapped — confirm) | ambiguous |
| 3.05 | 1 | 0.0% | (unmapped — confirm) | ambiguous |
| 2.10 | 1 | 0.0% | (unmapped — confirm) | ambiguous |
| 2.11 | 1 | 0.0% | (unmapped — confirm) | ambiguous |
| 5.06 | 1 | 0.0% | (unmapped — confirm) | ambiguous |
| 5.04 | 1 | 0.0% | Technical / processing | retryable |

## 3. Series vs single-blip check

**Stripe** (linked terminals, n=2288): failed attempts before death — median=8, share with ≥3 fails=97%, with exactly 1=2%. Most Stripe involuntary deaths ARE a dunning series.

**Solidgate**: of 4379 terminals with an auth_failed order, **2970 (68%) show only ONE auth_failed order** in the snapshot (order_ids are unique — NOT a dedup artifact; these carry only the final expire-order, retry_attempt=NULL). So a multi-attempt dunning SERIES is NOT demonstrable for the majority of Solidgate involuntary deaths from this snapshot — the earlier retries were not captured.

Sample (8 stripe / 7 solidgate):
| provider | subscription_id | n_fail_orders | last_code | class |
|---|---|---|---|---|
| stripe | sub_1Rjl5HJzVYkL7XCuOIaRL61i | 10 | do_not_honor | hard_dead |
| stripe | sub_1Rjl5LJzVYkL7XCu0jIuWDbC | 10 | do_not_honor | hard_dead |
| stripe | sub_1RobJLJzVYkL7XCuGOAPTidt | 5 | do_not_honor | hard_dead |
| stripe | sub_1RrNqQJzVYkL7XCuLBWtYcfY | 5 | try_again_later | retryable |
| stripe | sub_1S2JCaJzVYkL7XCu4eOhfKDS | 4 | previously_declined_do_not_retry | hard_dead |
| stripe | sub_1S6EKyJzVYkL7XCutCfNAE3s | 5 | previously_declined_do_not_retry | hard_dead |
| stripe | sub_1S7OqCJzVYkL7XCuJBfygAiL | 6 | previously_declined_do_not_retry | hard_dead |
| stripe | sub_1S7mqjJzVYkL7XCu6BPiLXfB | 4 | stolen_card | hard_dead |
| solidgate | 0008f9df-5a15-4f31-b928-0d485d00d6db | 7 | 3.02 (Insufficient funds) | retryable |
| solidgate | 000d1331-2c65-4970-a5f9-630c7d5be68d | 1 | 3.02 (Insufficient funds) | retryable |
| solidgate | 00329f64-fee0-4fdd-ba6b-de11e7636824 | 1 | 3.12 (Invalid card / do not honor) | hard_dead |
| solidgate | 003a25c0-045a-405b-8a94-b858550e5d0d | 6 | 3.12 (Invalid card / do not honor) | hard_dead |
| solidgate | 003f8c62-94fb-47c4-baa3-e4bcc532160e | 1 | 3.08 (Card limit / insufficient) | retryable |
| solidgate | 0064b86c-aea6-4ea6-bea7-3477b4901eb1 | 1 | 3.10 (Do not honor) | hard_dead |
| solidgate | 009277e0-e6b0-4384-8c54-bac79f54d428 | 5 | 3.02 (Insufficient funds) | retryable |

## 4. Verdict — retryable vs hard-dead (with honest ranges)

Reported on the UNBIASED basis: Stripe = clean unique-customer subset (1c); Solidgate = provisional mapping. Each with a sensitivity row moving the single dominant borderline code (`do_not_honor` / `3.10`) to retryable — because that one code swings the whole conclusion.

| basis | retryable | hard_dead | ambiguous | n |
|---|---|---|---|---|
| Stripe clean, base (do_not_honor=hard) | 280 (26%) | 736 (70%) | 41 (4%) | 1057 |
| Stripe clean, do_not_honor→retryable | 523 (49%) | 493 (47%) | 41 (4%) | 1057 |
| Solidgate, base (3.10=hard) | 1707 (39%) | 2597 (59%) | 75 (2%) | 4379 |
| Solidgate, 3.10→retryable | 2929 (67%) | 1375 (31%) | 75 (2%) | 4379 |

## Bottom line (honest)

- **Hard-dead share is a RANGE, not a point:** Stripe **47%–70%**, Solidgate **31%–59%**. The width is driven almost entirely by ONE classification choice — whether `do_not_honor` (Stripe) / `3.10` (Solidgate) counts as hard or as retryable (it is partly recoverable in practice). Everything else is stable.
- **Linkage bias was CHECKED and is NOT material at the aggregate:** the clean unique-customer subset (1057 terminals) gives hard=70% (base) — essentially the same as the full linked set, so the earlier concern that customer+time mixes other subs' codes does not move the headline. (The earlier ~58% figure was LOWER only because its denominator included the 430 unlinked terminals as neither class; on a like-for-like linked basis it is ~70% base too.)
- **The genuine uncertainties are two, both about meaning not linkage:** (1) how to treat `do_not_honor`/`3.10` (the whole range above); (2) the Solidgate numeric code→meaning mapping is external/provisional, not from the snapshot — 42% of Solidgate auth_failed is code 3.02 (insufficient funds) which is unambiguous, but the hard-side codes lean on the provisional table.
- **Series-of-failures established for Stripe, NOT for ~2/3 of Solidgate:** Stripe linked terminals show a median of ~8 failed attempts before death (97% have ≥3) — clear slow dunning. Solidgate: 68% of involuntary terminals retain only the final expire-order in the snapshot (earlier retries not captured), so 'slow dunning death' cannot be confirmed there from this data.
- **430/2718 Stripe involuntary terminals (16%) have NO linked charge.failed at all** (only 65 even have a payment_intent.payment_failed) — a snapshot gap, left unclassified (NULL), not forced into a class.
- **What would settle it:** (a) the Solidgate numeric decline-code dictionary (owner); a full payment_intent→invoice→subscription link for FAILED Stripe charges is NOT possible in this snapshot (invoice objects carry no PI/charge field), so customer+time is the ceiling for Stripe.