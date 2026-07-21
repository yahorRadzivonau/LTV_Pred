# Solidgate geo hunt — exhaustive key map

READ-ONLY (data/raw solidgate only; silver untouched). Goal: find ANY direct or indirect country carrier to lift Solidgate geo above the current 24% (email→web_conversions).

**Prior expectation (pre-run):** direct country unlikely; currency probably all-USD (zero variance → useless proxy); BIN/masked-PAN would be precise bank-geo IF present; Channel domain not country-bearing. Verified below against the FULL file, not samples.

Parsed **57930** distinct events; **43** unique normalized key-paths found.

## 1. Full key-path map (fill % over all events)

_Note: paths under `invoices.*.orders.*` show fill >100% — each event's payload carries a CUMULATIVE set of many orders, so leaf-count exceeds event-count. Not a bug; it just means these are repeated/nested, not per-event singletons._

| key path | fill % | n |
|---|---|---|
| `invoices.*.orders.*.amount` | 201% | 116380 |
| `invoices.*.orders.*.created_at` | 201% | 116380 |
| `invoices.*.orders.*.id` | 201% | 116380 |
| `invoices.*.orders.*.status` | 201% | 116380 |
| `invoices.*.orders.*.updated_at` | 201% | 116380 |
| `invoices.*.orders.*.operation` | 181% | 104779 |
| `invoices.*.orders.*.processed_at` | 157% | 90935 |
| `invoices.*.orders.*.failed_reason` | 128% | 74044 |
| `invoices.*.orders.*.retry_attempt` | 102% | 59020 |
| `callback_type` | 100% | 57930 |
| `customer.customer_account_id` | 100% | 57930 |
| `customer.customer_email` | 100% | 57930 |
| `invoices.*.amount` | 100% | 57930 |
| `invoices.*.created_at` | 100% | 57930 |
| `invoices.*.id` | 100% | 57930 |
| `invoices.*.status` | 100% | 57930 |
| `invoices.*.subscription_term_number` | 100% | 57930 |
| `invoices.*.updated_at` | 100% | 57930 |
| `product.amount` | 100% | 57930 |
| `product.currency` | 100% | 57930 |
| `product.name` | 100% | 57930 |
| `product.payment_action` | 100% | 57930 |
| `product.product_id` | 100% | 57930 |
| `product.trial` | 100% | 57930 |
| `product.trial_amount` | 100% | 57930 |
| `product.trial_currency` | 100% | 57930 |
| `product.trial_period` | 100% | 57930 |
| `subscription.id` | 100% | 57930 |
| `subscription.status` | 100% | 57930 |
| `subscription.trial` | 100% | 57930 |
| `subscription.updated_at` | 100% | 57930 |
| `invoices.*.product_price_id` | 88% | 50947 |
| `subscription.expired_at` | 80% | 46329 |
| `subscription.payment_type` | 80% | 46329 |
| `subscription.started_at` | 80% | 46329 |
| `invoices.*.order_metadata.real_email` | 55% | 32065 |
| `subscription.next_charge_at` | 54% | 31316 |
| `invoices.*.billing_period_ended_at` | 29% | 16891 |
| `invoices.*.billing_period_started_at` | 29% | 16891 |
| `subscription.cancel_code` | 4% | 2276 |
| `subscription.cancel_message` | 4% | 2276 |
| `subscription.cancellation_requested_at` | 4% | 2276 |
| `subscription.cancelled_at` | 4% | 2276 |

## 2. Geo-candidate key-paths (name matches country/geo/ip/currency/card/bin/…)

| key path | fill % | n | sample values |
|---|---|---|---|
| `product.currency` | 100% | 57930 | USD |
| `product.trial_currency` | 100% | 57930 | USD |

## 3. Indirect carriers

**currency** (product.currency):
| currency | n |
|---|---|
| USD | 57930 |

Non-USD share: 0/57930 = 0.0% → ZERO variance, useless as geo proxy.

**Explicit checks (card BIN / masked PAN / phone / locale / timezone / IP):**

- card/pan/bin: none
- phone: none
- locale/lang: none
- timezone: none
- ip: none
- address/city/zip: none

(No card/phone/locale/tz/ip/address carrier exists anywhere in solidgate_events.)

**Second email `invoices.*.order_metadata.real_email` (55% fill) as a geo-JOIN lever:** NOT a geo carrier, but tested whether joining web_conversions on it too matches more subs to a real country. Result: customer_email already resolves 2109 subs; real_email differs from customer_email in only 5 subs and adds just **+1 subs** with a country → no meaningful lift (real_email ≈ customer_email).


## 4. solidgate_old_transactions — columns, fill %, geo-bearing

Rows: 1032.

| column | fill % | geo-bearing? | sample |
|---|---|---|---|
| Order ID | 100% | no | order-1760078774090, order-1759797055349, order-175952965406 |
| Amount | 100% | no | 4.99, 29.99, 19.99, 9.99 |
| Currency | 100% | yes (proxy) | USD |
| Order status | 100% | no | refunded, declined, settled, voided |
| Payment method | 100% | no | card, apple-pay |
| Email | 100% | no | lynnvirshup@gmail.com, thk25@icloud.com, mmcnally87@comcast. |
| Created at | 100% | no | 2025-10-11 13:35:50+00:00, 2025-10-11 08:44:36+00:00, 2025-1 |
| Channel | 100% | yes (proxy) | vpn-solution.com |
| Payment type | 100% | no | first, recurring |
| Card brand | 100% | yes (proxy) | DISCOVER, MASTERCARD, AMEX, VISA |
| Cardholder name | 2% | yes (proxy) | jason holcomb, myrna v relampago, dawn phillips, simon garci |
| Auth code | 33% | no | 01129P, 061242, 009750, 105504 |
| Decline code | 67% | no | 5.01, 2.11, 3.1, 4.09 |
| Secured | 100% | no | non-3D, 3D |
| Alert ID | 0% | no | 3dea2ca8-8393-4944-9cad-10c54c56aa71 |
| Dispute ID | 0% | no |  |
| Dispute status | 0% | no |  |

**Channel domain distribution** (only geo-namish field):
| Channel | n |
|---|---|
| vpn-solution.com | 1032 |

**Overlap:** old_transactions distinct emails=388, of which also in current solidgate subs=223. But old_transactions has NO country column either — overlap adds no geo, only Channel/currency.

## 5. Verdict — coverage × precision per carrier

| carrier | present? | coverage of SG subs | precision | usable to lift geo? |
|---|---|---|---|---|
| direct country/region/city | NO | — | — | no |
| IP address | NO | — | precise if present | no |
| card BIN / masked PAN | NO | — | precise bank-geo if present | no |
| phone country code | NO | — | precise if present | no |
| currency | yes | 100% | coarse proxy | NO (all USD, zero variance) |
| Channel domain (old_txns only) | yes | old-txn subset only | not country | no |
| 2nd email (real_email) join | yes | +1 subs | — | no (≈ customer_email) |

**Bottom line:** Solidgate geo CANNOT be lifted above the current ~24% from local data. The full-file key map (43 paths, all 57,930 events) contains **no direct country/region/city, no IP, no card/BIN/masked-PAN, no phone, no locale, no timezone** — the payload is purely billing/lifecycle. The only geo-named fields are `product.currency`/`trial_currency`, both **100% USD (zero variance → no proxy signal)**. `old_transactions` has no country column either (only a single Channel domain `vpn-solution.com` and USD currency), and its email overlap with current subs (223) adds no geo. The second email `real_email` is effectively a duplicate of customer_email (+2 subs). **To raise Solidgate geo above 24% requires an EXTERNAL source** (IP-geolocation at checkout, a BIN table joined to a captured PAN prefix, or Solidgate's own geo API) — none of which is in this snapshot. Prior expectation confirmed: no direct country, and currency/BIN did NOT yield a usable proxy.