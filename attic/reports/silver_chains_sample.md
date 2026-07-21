# silver_chains_sample.md — 20 stratified subscription chains

10 Stripe + 10 Solidgate, stratified to include ≥2 voluntary deaths, ≥2 involuntary, ≥1 fraud, ≥2 alive renewers, ≥1 QA. Read all 20 by eye before silver is used.


### stripe `sub_1TfN0OA9qayReKqB9tbNBgk2` — app=atelier, geo=nan(unknown), media=fb/web_conversions, interval=week(plan), trial=free, qa=False

| event_datetime | event_name | churn_type | price | raw_event_type |
|---|---|---|---|---|
| 2026-06-06 16:23:29+00:00 | trial_started |  |  | customer.subscription.created |
| 2026-06-13 16:23:28+00:00 | subscription_expired | voluntary |  | customer.subscription.deleted |

### stripe `sub_1THd39JzVYkL7XCuhwKX5e8u` — app=invinci, geo=AU(resolved), media=facebook/web_conversions, interval=week(plan), trial=free, qa=False

| event_datetime | event_name | churn_type | price | raw_event_type |
|---|---|---|---|---|
| 2026-04-02 04:40:12+00:00 | trial_started |  |  | customer.subscription.created |
| 2026-04-05 05:41:21+00:00 | trial_converted |  | $9.99 | invoice.paid |
| 2026-04-12 05:41:40+00:00 | subscription_renewed |  | $9.99 | invoice.paid |
| 2026-04-13 08:58:56+00:00 | subscription_refunded |  | $9.99 | charge.refunded |
| 2026-04-19 04:40:11+00:00 | subscription_expired | voluntary |  | customer.subscription.deleted |

### stripe `sub_1THWtsJzVYkL7XCuMcrJG4go` — app=invinci, geo=US(resolved), media=facebook/web_conversions, interval=week(plan), trial=free, qa=False

| event_datetime | event_name | churn_type | price | raw_event_type |
|---|---|---|---|---|
| 2026-04-01 22:06:13+00:00 | trial_started |  |  | customer.subscription.created |
| 2026-04-04 23:12:56+00:00 | billing_issue_detected |  |  | invoice.payment_failed |
| 2026-04-09 15:13:08+00:00 | billing_issue_detected |  |  | invoice.payment_failed |
| 2026-04-11 23:07:21+00:00 | billing_issue_detected |  |  | invoice.payment_failed |
| 2026-04-13 03:13:11+00:00 | billing_issue_detected |  |  | invoice.payment_failed |
| 2026-04-14 14:13:17+00:00 | billing_issue_detected |  |  | invoice.payment_failed |
| 2026-04-17 03:07:32+00:00 | billing_issue_detected |  |  | invoice.payment_failed |
| 2026-04-18 15:07:36+00:00 | billing_issue_detected |  |  | invoice.payment_failed |
| 2026-04-18 23:08:24+00:00 | billing_issue_detected |  |  | invoice.payment_failed |
| 2026-04-18 23:13:18+00:00 | billing_issue_detected |  |  | invoice.payment_failed |
| 2026-04-25 22:06:12+00:00 | subscription_expired | involuntary |  | customer.subscription.deleted |

### stripe `sub_1TZy4gJzVYkL7XCuiSUhiahb` — app=invinci, geo=US(resolved), media=fbbohdan/subs_dim, interval=week(plan), trial=free, qa=False

| event_datetime | event_name | churn_type | price | raw_event_type |
|---|---|---|---|---|
| 2026-05-22 18:45:35+00:00 | trial_started |  |  | customer.subscription.created |
| 2026-05-25 19:47:04+00:00 | billing_issue_detected |  |  | invoice.payment_failed |
| 2026-05-29 07:47:14+00:00 | billing_issue_detected |  |  | invoice.payment_failed |
| 2026-06-01 19:47:16+00:00 | billing_issue_detected |  |  | invoice.payment_failed |
| 2026-06-01 19:47:34+00:00 | billing_issue_detected |  |  | invoice.payment_failed |
| 2026-06-05 07:47:23+00:00 | billing_issue_detected |  |  | invoice.payment_failed |
| 2026-06-05 07:47:47+00:00 | billing_issue_detected |  |  | invoice.payment_failed |
| 2026-06-07 15:43:14+00:00 | billing_issue_detected |  |  | invoice.payment_failed |
| 2026-06-08 02:36:50+00:00 | billing_issue_detected |  |  | invoice.payment_failed |
| 2026-06-08 19:46:48+00:00 | billing_issue_detected |  |  | invoice.payment_failed |
| 2026-06-08 19:47:20+00:00 | billing_issue_detected |  |  | invoice.payment_failed |
| 2026-06-08 19:47:45+00:00 | billing_issue_detected |  |  | invoice.payment_failed |
| 2026-06-15 18:45:34+00:00 | subscription_expired | involuntary |  | customer.subscription.deleted |

### stripe `sub_1TXDLFJzVYkL7XCul1LvjxzV` — app=invinci, geo=SE(resolved), media=fbbohdan/subs_dim, interval=week(plan), trial=free, qa=False

| event_datetime | event_name | churn_type | price | raw_event_type |
|---|---|---|---|---|
| 2026-05-15 04:27:18+00:00 | trial_started |  |  | customer.subscription.created |
| 2026-05-18 05:27:53+00:00 | billing_issue_detected |  |  | invoice.payment_failed |
| 2026-05-21 17:28:04+00:00 | billing_issue_detected |  |  | invoice.payment_failed |
| 2026-05-25 05:28:08+00:00 | trial_converted |  | $9.99 | invoice.paid |
| 2026-05-25 05:28:34+00:00 | subscription_renewed |  | $9.99 | invoice.paid |
| 2026-06-01 05:28:22+00:00 | subscription_renewed |  | $9.99 | invoice.paid |
| 2026-06-08 05:27:51+00:00 | billing_issue_detected |  |  | invoice.payment_failed |
| 2026-06-13 10:27:58+00:00 | subscription_renewed |  | $9.99 | invoice.paid |
| 2026-06-17 02:19:30+00:00 | subscription_renewed |  | $9.99 | invoice.paid |
| 2026-06-22 05:29:03+00:00 | subscription_renewed |  | $9.99 | invoice.paid |
| 2026-06-29 05:29:24+00:00 | subscription_renewed |  | $9.99 | invoice.paid |
| 2026-07-06 05:27:55+00:00 | billing_issue_detected |  |  | invoice.payment_failed |

### stripe `sub_1TO5z7JzVYkL7XCugMWgvNs2` — app=invinci, geo=nan(unknown), media=fbbohdan/subs_dim, interval=week(plan), trial=free, qa=False

| event_datetime | event_name | churn_type | price | raw_event_type |
|---|---|---|---|---|
| 2026-04-20 00:46:46+00:00 | trial_started |  |  | customer.subscription.created |
| 2026-04-23 01:47:53+00:00 | billing_issue_detected |  |  | invoice.payment_failed |
| 2026-04-28 00:48:04+00:00 | billing_issue_detected |  |  | invoice.payment_failed |
| 2026-04-30 01:48:17+00:00 | trial_converted |  | $9.99 | invoice.paid |
| 2026-05-02 06:48:07+00:00 | subscription_renewed |  | $9.99 | invoice.paid |
| 2026-05-07 01:50:32+00:00 | subscription_renewed |  | $9.99 | invoice.paid |
| 2026-05-14 01:47:38+00:00 | subscription_renewed |  | $9.99 | invoice.paid |
| 2026-05-21 01:48:19+00:00 | billing_issue_detected |  |  | invoice.payment_failed |
| 2026-05-25 15:48:26+00:00 | subscription_renewed |  | $9.99 | invoice.paid |
| 2026-05-28 01:48:35+00:00 | subscription_renewed |  | $9.99 | invoice.paid |
| 2026-06-04 01:48:41+00:00 | subscription_renewed |  | $9.99 | invoice.paid |
| 2026-06-14 00:47:59+00:00 | subscription_renewed |  | $9.99 | invoice.paid |
| 2026-06-18 01:47:58+00:00 | subscription_renewed |  | $9.99 | invoice.paid |
| 2026-06-25 01:48:05+00:00 | subscription_renewed |  | $9.99 | invoice.paid |
| 2026-07-02 01:48:17+00:00 | billing_issue_detected |  |  | invoice.payment_failed |
| 2026-07-06 16:48:21+00:00 | billing_issue_detected |  |  | invoice.payment_failed |

### stripe `sub_1TTgSXJzVYkL7XCuIW7TDvYG` — app=invinci, geo=nan(unknown), media=nan/subs_dim, interval=month(plan), trial=free, qa=True

| event_datetime | event_name | churn_type | price | raw_event_type |
|---|---|---|---|---|
| 2026-05-05 10:44:15+00:00 | trial_started |  |  | customer.subscription.created |
| 2026-05-19 10:44:13+00:00 | subscription_expired | voluntary |  | customer.subscription.deleted |

### solidgate `7ceb1ffe-435d-4284-81f4-36d8729d96b1` — app=invinci, geo=nan(unknown), media=facebook/web_conversions, interval=nan(nan), trial=paid, qa=False

| event_datetime | event_name | churn_type | price | raw_event_type |
|---|---|---|---|---|
| 2025-10-04 22:42:00+00:00 | trial_started |  |  | create/active |
| 2025-10-11 22:42:08+00:00 | trial_converted |  | $39.99 | order:settle_ok |
| 2025-11-08 22:42:00+00:00 | subscription_expired | fraud |  | cancel |
| 2025-11-08 22:42:06+00:00 | billing_issue_detected |  |  | order:auth_failed(retry=nan) |
| 2025-11-08 22:42:06+00:00 | entered_grace_period |  |  | redemption |
| 2025-11-09 22:42:09+00:00 | billing_issue_detected |  |  | order:auth_failed(retry=1.0) |

### solidgate `aa0efe14-e4db-4b7c-aeda-1931ca9ceb78` — app=invinci, geo=CA(resolved), media=fb-andrii/web_conversions, interval=nan(nan), trial=paid, qa=False

| event_datetime | event_name | churn_type | price | raw_event_type |
|---|---|---|---|---|
| 2026-06-29 03:33:08+00:00 | trial_started |  |  | create/active |
| 2026-06-29 03:33:10+00:00 | billing_issue_detected |  |  | order:auth_failed(retry=nan) |
| 2026-06-29 03:33:10+00:00 | subscription_expired | involuntary |  | expire |

### solidgate `273cd6e6-c7c2-4dfc-bd20-06535e509916` — app=invinci, geo=nan(unknown), media=ads-rs/web_conversions, interval=week(from_name), trial=free, qa=False

| event_datetime | event_name | churn_type | price | raw_event_type |
|---|---|---|---|---|
| 2026-06-26 18:54:40+00:00 | trial_started |  |  | create/active |
| 2026-06-29 18:54:42+00:00 | subscription_expired | involuntary |  | cancel |
| 2026-06-29 18:54:47+00:00 | billing_issue_detected |  |  | order:auth_failed(retry=nan) |
| 2026-06-29 18:54:47+00:00 | entered_grace_period |  |  | redemption |
| 2026-07-01 18:54:51+00:00 | billing_issue_detected |  |  | order:auth_failed(retry=1.0) |
| 2026-07-02 18:54:49+00:00 | billing_issue_detected |  |  | order:auth_failed(retry=2.0) |
| 2026-07-03 18:54:49+00:00 | billing_issue_detected |  |  | order:auth_failed(retry=3.0) |
| 2026-07-04 18:54:48+00:00 | billing_issue_detected |  |  | order:auth_failed(retry=4.0) |

### solidgate `5e11d0fc-93b4-4298-98ba-f2d69be12a3f` — app=invinci, geo=nan(unknown), media=ads-rs/web_conversions, interval=week(from_name), trial=free, qa=False

| event_datetime | event_name | churn_type | price | raw_event_type |
|---|---|---|---|---|
| 2026-06-28 00:48:41+00:00 | trial_started |  |  | create/active |
| 2026-07-01 00:48:42+00:00 | subscription_expired | voluntary |  | cancel |
| 2026-07-01 00:48:46+00:00 | billing_issue_detected |  |  | order:auth_failed(retry=nan) |
| 2026-07-01 00:48:46+00:00 | entered_grace_period |  |  | redemption |
| 2026-07-03 00:48:49+00:00 | billing_issue_detected |  |  | order:auth_failed(retry=1.0) |
| 2026-07-04 00:48:48+00:00 | billing_issue_detected |  |  | order:auth_failed(retry=2.0) |
| 2026-07-04 01:18:50+00:00 | billing_issue_detected |  |  | order:auth_failed(retry=3.0) |
| 2026-07-05 00:48:46+00:00 | billing_issue_detected |  |  | order:auth_failed(retry=4.0) |

### solidgate `06bdafd9-7f63-41c9-8016-a45e7b7bb1c0` — app=invinci, geo=nan(unknown), media=fbbohdan/web_conversions, interval=week(inferred_from_cadence), trial=paid, qa=False

| event_datetime | event_name | churn_type | price | raw_event_type |
|---|---|---|---|---|
| 2026-06-26 14:56:24+00:00 | trial_started |  |  | create/active |
| 2026-06-26 14:56:26+00:00 | trial_converted |  | $0.99 | order:settle_ok |
| 2026-06-29 14:56:32+00:00 | billing_issue_detected |  |  | order:auth_failed(retry=nan) |
| 2026-06-29 14:56:32+00:00 | entered_grace_period |  |  | redemption |
| 2026-07-02 14:56:35+00:00 | billing_issue_detected |  |  | order:auth_failed(retry=1.0) |
| 2026-07-02 14:56:35+00:00 | entered_grace_period |  |  | scheduled_for_retry |
| 2026-07-03 14:56:34+00:00 | subscription_renewed |  | $9.99 | order:settle_ok |
| 2026-07-06 14:56:31+00:00 | subscription_renewed |  | $9.99 | order:settle_ok |

### solidgate `37ba949b-f422-47af-a5b1-e35ba93836fd` — app=invinci, geo=nan(unknown), media=fbbohdan/web_conversions, interval=week(inferred_from_cadence), trial=paid, qa=False

| event_datetime | event_name | churn_type | price | raw_event_type |
|---|---|---|---|---|
| 2026-06-24 08:21:53+00:00 | trial_started |  |  | create/active |
| 2026-06-24 08:21:56+00:00 | trial_converted |  | $0.99 | order:settle_ok |
| 2026-06-27 08:22:03+00:00 | subscription_renewed |  | $9.99 | order:settle_ok |
| 2026-07-04 08:22:02+00:00 | subscription_renewed |  | $9.99 | order:settle_ok |

### stripe `sub_1TGaWZJzVYkL7XCuWvazoIO5` — app=invinci, geo=AU(resolved), media=fbbohdan/web_conversions, interval=month(plan), trial=free, qa=False

| event_datetime | event_name | churn_type | price | raw_event_type |
|---|---|---|---|---|
| 2026-03-30 07:46:16+00:00 | trial_started |  |  | customer.subscription.created |
| 2026-04-13 08:47:01+00:00 | trial_converted |  | $11.99 | invoice.paid |
| 2026-05-13 08:47:30+00:00 | billing_issue_detected |  |  | invoice.payment_failed |
| 2026-05-18 07:47:38+00:00 | billing_issue_detected |  |  | invoice.payment_failed |
| 2026-05-22 17:47:46+00:00 | subscription_renewed |  | $11.99 | invoice.paid |
| 2026-06-16 07:47:28+00:00 | billing_issue_detected |  |  | invoice.payment_failed |
| 2026-06-21 02:47:32+00:00 | billing_issue_detected |  |  | invoice.payment_failed |
| 2026-06-25 02:47:34+00:00 | billing_issue_detected |  |  | invoice.payment_failed |
| 2026-06-28 19:47:37+00:00 | billing_issue_detected |  |  | invoice.payment_failed |
| 2026-06-30 07:47:36+00:00 | billing_issue_detected |  |  | invoice.payment_failed |
| 2026-07-13 07:46:15+00:00 | subscription_expired | involuntary |  | customer.subscription.deleted |

### stripe `sub_1TiWh8JzVYkL7XCu7KUHNnyM` — app=invinci, geo=US(resolved), media=fb-andrii/subs_dim, interval=week(plan), trial=none, qa=False

| event_datetime | event_name | churn_type | price | raw_event_type |
|---|---|---|---|---|
| 2026-06-15 09:20:39+00:00 | trial_started |  |  | customer.subscription.created |
| 2026-06-15 09:20:42+00:00 | billing_issue_detected |  |  | invoice.payment_failed |

### stripe `sub_1Sr7BoJzVYkL7XCuNVn4niMR` — app=invinci, geo=nan(unknown), media=nan/nan, interval=month(plan), trial=free, qa=False

| event_datetime | event_name | churn_type | price | raw_event_type |
|---|---|---|---|---|
| 2026-01-19 01:23:33+00:00 | trial_started |  |  | customer.subscription.created |
| 2026-01-26 02:24:29+00:00 | billing_issue_detected |  |  | invoice.payment_failed |
| 2026-01-29 14:24:36+00:00 | billing_issue_detected |  |  | invoice.payment_failed |
| 2026-02-02 02:24:42+00:00 | billing_issue_detected |  |  | invoice.payment_failed |
| 2026-02-05 14:24:43+00:00 | billing_issue_detected |  |  | invoice.payment_failed |
| 2026-02-09 02:24:42+00:00 | billing_issue_detected |  |  | invoice.payment_failed |
| 2026-02-26 01:23:32+00:00 | subscription_expired | involuntary |  | customer.subscription.deleted |

### solidgate `27e19c09-68a9-46e2-a050-ffa8f2753cb5` — app=invinci, geo=nan(unknown), media=fb-andrii/web_conversions, interval=nan(nan), trial=free, qa=False

| event_datetime | event_name | churn_type | price | raw_event_type |
|---|---|---|---|---|
| 2026-06-08 10:56:25+00:00 | trial_started |  |  | create/active |
| 2026-06-22 10:56:33+00:00 | trial_converted |  | $11.99 | order:settle_ok |

### solidgate `c836b11c-f634-4eb2-af23-b2e8030bb549` — app=invinci, geo=nan(unknown), media=ads-zk/web_conversions, interval=day(inferred_from_cadence), trial=paid, qa=False

| event_datetime | event_name | churn_type | price | raw_event_type |
|---|---|---|---|---|
| 2026-07-02 20:14:02+00:00 | trial_started |  |  | create/active |
| 2026-07-02 20:14:05+00:00 | trial_converted |  | $0.99 | order:settle_ok |
| 2026-07-05 20:14:12+00:00 | subscription_renewed |  | $9.99 | order:settle_ok |

### solidgate `02fd6e4d-b005-4355-bc36-ecc0208bbf7d` — app=invinci, geo=nan(unknown), media=ads-rs/web_conversions, interval=week(from_name), trial=free, qa=False

| event_datetime | event_name | churn_type | price | raw_event_type |
|---|---|---|---|---|
| 2026-06-26 02:25:47+00:00 | trial_started |  |  | create/active |
| 2026-06-29 02:25:51+00:00 | trial_converted |  | $9.99 | order:settle_ok |
| 2026-07-06 02:25:55+00:00 | subscription_renewed |  | $9.99 | order:settle_ok |

### solidgate `665f2cd8-f791-499b-82c2-2607db7d53f1` — app=invinci, geo=nan(unknown), media=fbbohdan/web_conversions, interval=nan(nan), trial=paid, qa=False

| event_datetime | event_name | churn_type | price | raw_event_type |
|---|---|---|---|---|
| 2026-07-04 20:38:56+00:00 | trial_started |  |  | create/active |
| 2026-07-04 20:38:57+00:00 | trial_converted |  | $0.99 | order:settle_ok |