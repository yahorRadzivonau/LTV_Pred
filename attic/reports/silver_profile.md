# web_events_silver — self-check profile

Built from data/raw snapshot (snapshot_ts=2026-07-07). Rows: **81869**, subscriptions: **14478**, customer_users: **11293**. Silver keeps everything with flags; golden derivation (Section 14) is separate.


## stripe: 43499 rows / 5746 subs / 4262 users

### event_name
| event_name | n |
|---|---|
| billing_issue_detected | 23348 |
| subscription_renewed | 8214 |
| trial_started | 5746 |
| subscription_expired | 3392 |
| trial_converted | 2278 |
| subscription_refunded | 521 |

### churn_type (terminals, n=3392)
| churn_type | n | share |
|---|---|---|
| involuntary | 2718 | 80.1% |
| voluntary | 674 | 19.9% |


## solidgate: 38370 rows / 8732 subs / 7031 users

### event_name
| event_name | n |
|---|---|
| billing_issue_detected | 14217 |
| trial_started | 8732 |
| subscription_expired | 5062 |
| entered_grace_period | 4173 |
| trial_converted | 3229 |
| subscription_renewed | 2917 |
| subscription_refunded | 40 |

### churn_type (terminals, n=5062)
| churn_type | n | share |
|---|---|---|
| involuntary | 4407 | 87.1% |
| voluntary | 329 | 6.5% |
| fraud | 326 | 6.4% |

## app_id × stripe_account (Stripe) — Atelier hypothesis

| app_id | A9qayReKqB | IEMVDMVTXC | JzVYkL7XCu |
|---|---|---|---|
| atelier | 2121 | 0 | 0 |
| invinci | 54 | 12 | 41312 |

**Verdict:** A9qayReKqB → app_id: {'atelier': 2121, 'invinci': 54} — Atelier hypothesis **CONFIRMED** (A9qayReKqB is the only account mapped to Atelier; the other two default to invinci absent a dim app_name).

## trial_type (per subscription)
| trial_type | n |
|---|---|
| free | 7689 |
| paid | 5638 |
| none | 1152 |

## attribution (media_source_step, per subscription)
| step | n |
|---|---|
| web_conversions | 10473 |
| subs_dim | 3088 |
| NULL | 918 |

## geo coverage & ambiguity (per subscription)

- **stripe**: country resolved 57.0%, geo_ambiguous 0.0% of subs
- **solidgate**: country resolved 24.2%, geo_ambiguous 0.0% of subs

| geo_status | n subs |
|---|---|
| unknown | 8757 |
| resolved | 5388 |
| no_match | 334 |

> **PRESENTATION LIMITATION:** geo-сегментация покрывает **57% Stripe / 24% Solidgate** подписок; остальные гео-агностик (country_code=NULL, geo_status='unknown'/'no_match'). Причина — 69% строк web_conversions имеют country='unknown' в источнике. NULL здесь = «страна неизвестна», НЕ «нет строки» (см. geo_status).

## interval_source (per subscription)
| interval_source | n |
|---|---|
| plan | 5746 |
| NULL | 4927 |
| from_name | 2663 |
| inferred_from_cadence | 1143 |

## flags

- is_qa rows: 2505 (110 subs)
- fraud churn rows: 326
- refund_orphan rows: 159
- non-USD invoices (audit): 28 gbp/eur rows, ALL on non-money invoice variants (updated/created/finalized); 0 money rows non-USD (assert 6 pass).

## Observed weekly survival (step-share retention) — the baseline curve

### stripe
| week | survival | n matured |
|---|---|---|
| 0 | 1.000 | 5746 |
| 1 | 0.978 | 5710 |
| 2 | 0.968 | 5618 |
| 4 | 0.715 | 4885 |
| 8 | 0.358 | 3307 |
| 12 | 0.301 | 1926 |
| 26 | 0.036 | 335 |
| 52 |  | 0 |

### solidgate
| week | survival | n matured |
|---|---|---|
| 0 | 1.000 | 8732 |
| 1 | 0.486 | 7261 |
| 2 | 0.443 | 3712 |
| 4 | 0.350 | 1413 |
| 8 | 0.179 | 543 |
| 12 | 0.131 | 543 |
| 26 | 0.087 | 543 |
| 52 |  | 0 |

## Terminal churn_type shares (input to one-map vs three-map decision)

| provider | voluntary | involuntary | fraud | unknown | n terminals |
|---|---|---|---|---|---|
| stripe | 19.9% | 80.1% | 0.0% | 0.0% | 3392 |
| solidgate | 6.5% | 87.1% | 6.4% | 0.0% | 5062 |
