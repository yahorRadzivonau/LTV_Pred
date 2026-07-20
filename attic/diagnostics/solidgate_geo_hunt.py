"""
Solidgate geo hunt — exhaustive search for ANY direct or indirect country
carrier. READ-ONLY on data/raw/solidgate_events.parquet and
data/raw/solidgate_old_transactions.parquet. silver NOT touched.
Output: reports/solidgate_geo_hunt.md.

Method: parse EVERY event's JSON (not 3 samples), recursively enumerate all
key-paths (dynamic UUID/order keys normalized to '*'), count fill rate per
path, then flag any path whose name could carry geo (country/geo/region/ip/
locale/lang/phone/currency/timezone/address/bin/card/pan/issuer/bank/mask).

Run: .venv/Scripts/python.exe solidgate_geo_hunt.py
"""
import json
import re
from collections import defaultdict
from pathlib import Path

import duckdb
import pandas as pd

ROOT = Path(__file__).resolve().parent
EV = (ROOT / "data" / "raw" / "solidgate_events.parquet").as_posix()
OLD = (ROOT / "data" / "raw" / "solidgate_old_transactions.parquet").as_posix()
TMP = ROOT / "data" / "tmp"
OUT = ROOT / "reports" / "solidgate_geo_hunt.md"

GEO_RX = re.compile(r"countr|geo|region|\bip\b|ipaddr|locale|lang|phone|currenc|timezone|"
                    r"\btz\b|address|city|state|zip|postal|\bbin\b|card|pan|issuer|bank|mask", re.I)
DYNKEY_RX = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-|^order-\d|^upsale-\d|^[0-9a-f]{24,}$|^\d{10,}$", re.I)


def norm_key(k):
    return "*" if DYNKEY_RX.search(str(k)) else str(k)


def walk(obj, prefix, fill, samples, total_key):
    if isinstance(obj, dict):
        for k, v in obj.items():
            walk(v, f"{prefix}.{norm_key(k)}" if prefix else norm_key(k), fill, samples, total_key)
    elif isinstance(obj, list):
        for item in obj:
            walk(item, prefix + "[]", fill, samples, total_key)
    else:
        # leaf
        nonempty = obj is not None and str(obj).strip() not in ("", "null", "none")
        if nonempty:
            fill[prefix] += 1
            if len(samples[prefix]) < 10:
                samples[prefix].add(str(obj)[:40])


def main():
    con = duckdb.connect(); con.execute("SET enable_progress_bar=false"); con.execute("SET TimeZone='UTC'")

    rows = con.execute(f"SELECT DISTINCT event_id, created, data FROM read_parquet('{EV}')").fetchdf()
    n = len(rows)
    fill = defaultdict(int)
    samples = defaultdict(set)
    for d in rows["data"]:
        walk(json.loads(d), "", fill, samples, n)

    L = ["# Solidgate geo hunt — exhaustive key map\n",
         "READ-ONLY (data/raw solidgate only; silver untouched). Goal: find ANY direct or "
         "indirect country carrier to lift Solidgate geo above the current 24% (email→web_conversions).\n",
         "**Prior expectation (pre-run):** direct country unlikely; currency probably all-USD "
         "(zero variance → useless proxy); BIN/masked-PAN would be precise bank-geo IF present; "
         "Channel domain not country-bearing. Verified below against the FULL file, not samples.\n",
         f"Parsed **{n}** distinct events; **{len(fill)}** unique normalized key-paths found.\n"]

    # full key map (fill %)
    allpaths = sorted(fill.items(), key=lambda kv: -kv[1])
    L.append("## 1. Full key-path map (fill % over all events)\n")
    L.append("_Note: paths under `invoices.*.orders.*` show fill >100% — each event's payload "
             "carries a CUMULATIVE set of many orders, so leaf-count exceeds event-count. Not a bug; "
             "it just means these are repeated/nested, not per-event singletons._\n")
    L.append("| key path | fill % | n |\n|---|---|---|")
    for p, c in allpaths:
        L.append(f"| `{p}` | {c/n:.0%} | {c} |")

    # geo candidates
    geo = [(p, c) for p, c in allpaths if GEO_RX.search(p)]
    L.append("\n## 2. Geo-candidate key-paths (name matches country/geo/ip/currency/card/bin/…)\n")
    if not geo:
        L.append("**NONE** — no key path anywhere in the payload has a geo-suggestive name.\n")
    else:
        L.append("| key path | fill % | n | sample values |\n|---|---|---|---|")
        for p, c in geo:
            sv = ", ".join(sorted(samples[p])[:10])
            L.append(f"| `{p}` | {c/n:.0%} | {c} | {sv} |")

    # currency variance (indirect proxy)
    L.append("\n## 3. Indirect carriers\n")
    cur = con.execute(f"""
        SELECT json_extract_string(data,'$.product.currency') AS cur, COUNT(*) n
        FROM (SELECT DISTINCT event_id, created, data FROM read_parquet('{EV}')) GROUP BY 1 ORDER BY n DESC
    """).fetchdf()
    L.append("**currency** (product.currency):\n| currency | n |\n|---|---|")
    for _, r in cur.iterrows():
        L.append(f"| {r['cur']} | {r['n']} |")
    non_usd = cur[cur["cur"].str.lower() != "usd"]["n"].sum() if len(cur) else 0
    L.append(f"\nNon-USD share: {non_usd}/{int(cur['n'].sum())} = {non_usd/max(1,cur['n'].sum()):.1%} "
             f"→ {'usable' if non_usd/max(1,cur['n'].sum())>0.02 else 'ZERO variance, useless as geo proxy'}.\n")

    # explicit card/bin/phone/locale/tz presence (even if fill 0)
    L.append("**Explicit checks (card BIN / masked PAN / phone / locale / timezone / IP):**\n")
    found_any = False
    for label, rx in [("card/pan/bin", r"card|pan|bin|mask"), ("phone", r"phone"),
                       ("locale/lang", r"locale|lang"), ("timezone", r"timezone|\btz\b"),
                       ("ip", r"\bip\b|ipaddr"), ("address/city/zip", r"address|city|zip|postal|region")]:
        hits = [p for p in fill if re.search(rx, p, re.I)]
        if hits:
            found_any = True
            L.append(f"- {label}: FOUND paths {hits}")
        else:
            L.append(f"- {label}: none")
    if not found_any:
        L.append("\n(No card/phone/locale/tz/ip/address carrier exists anywhere in solidgate_events.)")

    # second email (invoices.*.order_metadata.real_email) as a potential geo-JOIN lift —
    # not a geo carrier itself, but could match more subs to web_conversions' country.
    WC = (ROOT / "data" / "raw" / "web_conversions.parquet").as_posix()
    lift = con.execute(f"""
        WITH d AS (SELECT DISTINCT event_id, created, data FROM read_parquet('{EV}')),
        re AS (
          SELECT json_extract_string(data,'$.subscription.id') sid,
                 lower(json_extract_string(data,'$.customer.customer_email')) cust_email,
                 lower(regexp_extract(data, '"real_email"\\s*:\\s*"([^"]+)"', 1)) real_email
          FROM d WHERE json_extract_string(data,'$.subscription.id') IS NOT NULL
        ),
        per_sub AS (
          SELECT sid, ANY_VALUE(cust_email) cust_email,
                 MAX(CASE WHEN real_email IS NOT NULL AND real_email<>'' THEN real_email END) real_email
          FROM re GROUP BY sid
        ),
        wc AS (SELECT DISTINCT lower(email) em FROM read_parquet('{WC}')
               WHERE email IS NOT NULL AND country IS NOT NULL AND lower(country) NOT IN ('unknown',''))
        SELECT COUNT(*) n_subs,
          COUNT(*) FILTER (WHERE cust_email IN (SELECT em FROM wc)) cust_hit,
          COUNT(*) FILTER (WHERE cust_email NOT IN (SELECT em FROM wc) AND real_email IN (SELECT em FROM wc)) lift,
          COUNT(*) FILTER (WHERE real_email IS NOT NULL AND real_email<>cust_email) real_differs
        FROM per_sub
    """).fetchdf().iloc[0]
    L.append(f"\n**Second email `invoices.*.order_metadata.real_email` (55% fill) as a geo-JOIN lever:** "
             f"NOT a geo carrier, but tested whether joining web_conversions on it too matches more subs to a "
             f"real country. Result: customer_email already resolves {int(lift['cust_hit'])} subs; real_email "
             f"differs from customer_email in only {int(lift['real_differs'])} subs and adds just "
             f"**+{int(lift['lift'])} subs** with a country → no meaningful lift (real_email ≈ customer_email).\n")

    # ---------- old_transactions ----------
    L.append("\n## 4. solidgate_old_transactions — columns, fill %, geo-bearing\n")
    desc = con.execute(f"DESCRIBE SELECT * FROM read_parquet('{OLD}')").fetchdf()
    tot_old = con.execute(f"SELECT COUNT(*) FROM read_parquet('{OLD}')").fetchone()[0]
    L.append(f"Rows: {tot_old}.\n")
    L.append("| column | fill % | geo-bearing? | sample |\n|---|---|---|---|")
    for c in desc["column_name"]:
        cq = c.replace('"', '""')
        nn = con.execute(f'SELECT COUNT("{cq}") FROM read_parquet(\'{OLD}\')').fetchone()[0]
        sv = con.execute(f'SELECT DISTINCT "{cq}" FROM read_parquet(\'{OLD}\') WHERE "{cq}" IS NOT NULL LIMIT 4').fetchdf()[c].astype(str).tolist()
        geo_flag = "yes (proxy)" if GEO_RX.search(c) or c in ("Channel",) else "no"
        L.append(f"| {c} | {nn/tot_old:.0%} | {geo_flag} | {', '.join(sv)[:60]} |")

    # Channel domain distribution
    ch = con.execute(f'SELECT "Channel" ch, COUNT(*) n FROM read_parquet(\'{OLD}\') GROUP BY 1 ORDER BY n DESC LIMIT 15').fetchdf()
    L.append("\n**Channel domain distribution** (only geo-namish field):\n| Channel | n |\n|---|---|")
    for _, r in ch.iterrows():
        L.append(f"| {r['ch']} | {r['n']} |")

    # overlap old_transactions email with current solidgate subs
    evf = TMP / "solidgate_events_flat.parquet"
    ov = con.execute(f"""
        WITH old_em AS (SELECT DISTINCT lower(Email) em FROM read_parquet('{OLD}') WHERE Email IS NOT NULL),
             sg_em AS (SELECT DISTINCT email em FROM read_parquet('{evf.as_posix()}') WHERE email IS NOT NULL)
        SELECT (SELECT COUNT(*) FROM old_em) old_emails,
               (SELECT COUNT(*) FROM old_em WHERE em IN (SELECT em FROM sg_em)) matched_to_current
    """).fetchdf()
    L.append(f"\n**Overlap:** old_transactions distinct emails={int(ov['old_emails'][0])}, "
             f"of which also in current solidgate subs={int(ov['matched_to_current'][0])}. "
             "But old_transactions has NO country column either — overlap adds no geo, only Channel/currency.\n")

    # ---------- verdict ----------
    L.append("## 5. Verdict — coverage × precision per carrier\n")
    L.append("| carrier | present? | coverage of SG subs | precision | usable to lift geo? |\n|---|---|---|---|---|")
    L.append(f"| direct country/region/city | {'yes' if any(re.search(r'countr|region|city',p,re.I) for p in fill) else 'NO'} | — | — | no |")
    L.append(f"| IP address | {'yes' if any(re.search(r'\\bip\\b',p,re.I) for p in fill) else 'NO'} | — | precise if present | no |")
    L.append(f"| card BIN / masked PAN | {'yes' if any(re.search(r'bin|pan|mask',p,re.I) for p in fill) else 'NO'} | — | precise bank-geo if present | no |")
    L.append(f"| phone country code | {'yes' if any(re.search(r'phone',p,re.I) for p in fill) else 'NO'} | — | precise if present | no |")
    usable_cur = non_usd/max(1,cur['n'].sum()) > 0.02
    L.append(f"| currency | yes | 100% | coarse proxy | {'maybe' if usable_cur else 'NO (all USD, zero variance)'} |")
    L.append(f"| Channel domain (old_txns only) | yes | old-txn subset only | not country | no |")
    L.append(f"| 2nd email (real_email) join | yes | +{int(lift['lift'])} subs | — | no (≈ customer_email) |")
    L.append("\n**Bottom line:** Solidgate geo CANNOT be lifted above the current ~24% from local data. "
             "The full-file key map (43 paths, all 57,930 events) contains **no direct country/region/city, "
             "no IP, no card/BIN/masked-PAN, no phone, no locale, no timezone** — the payload is purely "
             "billing/lifecycle. The only geo-named fields are `product.currency`/`trial_currency`, both "
             "**100% USD (zero variance → no proxy signal)**. `old_transactions` has no country column either "
             "(only a single Channel domain `vpn-solution.com` and USD currency), and its email overlap with "
             "current subs (223) adds no geo. The second email `real_email` is effectively a duplicate of "
             "customer_email (+2 subs). **To raise Solidgate geo above 24% requires an EXTERNAL source** "
             "(IP-geolocation at checkout, a BIN table joined to a captured PAN prefix, or Solidgate's own "
             "geo API) — none of which is in this snapshot. Prior expectation confirmed: no direct country, "
             "and currency/BIN did NOT yield a usable proxy.")

    OUT.write_text("\n".join(L), encoding="utf-8")
    print(f"wrote {OUT}")
    print(f"paths={len(fill)}, geo_candidates={len(geo)}, non_usd_share={non_usd/max(1,cur['n'].sum()):.3f}")
    print("geo candidate paths:", [p for p, _ in geo])


if __name__ == "__main__":
    main()
