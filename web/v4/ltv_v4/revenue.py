"""
Per-person revenue for the v3 web pipeline, keyed on LOWER(email).

Pure functions over the local pull (data/web_v4/events_<date>.parquet). No
BigQuery at runtime.

TWO AMOUNT COLUMNS, AND THE RULE THAT KEEPS THEM APART (v3: 2026-09-11)

  amt      GROSS (transaction_amount_usd). Identifies WHICH product a row is.
           Every classifier in this module keys on it: classify_trial (0.99
           real / 1.00 placeholder / NULL or 0.00 free), product_b_members
           (4.99 trial, >=20.00 plan), base_payment_ladder (drops rows priced
           at 0.99 that were logged under the base event name).
  amt_net  NET (net_revenue), = gross * rate - 0.057 after the acquirer's cut.
           The ONLY column that ever becomes money.

Never move a classifier onto amt_net. Its thresholds would then depend on the
acquirer's fee schedule and on which provider processed the row -- solidgate
and stripe take different cuts, so one $0.99 trial nets 0.897162 and another
nets 0.892212, and the same product would land in two different buckets.

Three money streams, kept SEPARATE all the way through -- v2 pooled trial into
the ups bucket, which made a one-time $0.99 inherit ups's monthly compounding
growth ratio:

  trial : one-time $0.99 prepayment that activates a 1-week trial. Never
          projected forward (config.TRIAL_IS_ONE_TIME). Placeholder ($1.00
          Solidgate card-validation hold) and free (NULL) trials carry NO money
          -- they enter the population only.
  base  : $9.99/week recurring (subscription_started).
  ups   : $11.99/month recurring (upsale_converted), a separate product on its
          own subscription id and its own ~30-day cadence.

State-signal events (billing_issue, sub_cancelled) are present in the pull but
are NEVER revenue here -- see config.STATE_EVENT_TYPES for why they exist.
"""
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

from ltv_v4.config import (
    BASE_EVENT_TYPE, UPS_EVENT_TYPE, TRIAL_EVENT_TYPE, CAPTURED_EVENT_TYPES,
    TRIAL_PLACEHOLDER_AMOUNT, REFUND_HAIRCUT, DATA_DIR, EVENTS_GLOB, JOIN_KEY,
    EXCLUDE_PRODUCT_B, PRODUCT_B_TRIAL_AMOUNT, PRODUCT_B_BASE_MIN_AMOUNT,
    NET_MISSING_MAX_SHARE, SESSION_KEY, SESSION_COL,
)

TRIAL_REAL = "real"
TRIAL_PLACEHOLDER = "placeholder"
TRIAL_FREE = "free"


def newest_events_path(data_dir: str = DATA_DIR) -> Path:
    """Newest data/web_v4/events_*.parquet. Explicit path always wins over this."""
    candidates = sorted(Path(data_dir).glob(EVENTS_GLOB))
    if not candidates:
        raise FileNotFoundError(
            f"No {EVENTS_GLOB} in {data_dir}. Run web/v4/pull_v4_events.py first."
        )
    return candidates[-1]


def classify_trial(events: pd.DataFrame) -> pd.Series:
    """real / placeholder / free for trial_started rows, NaN elsewhere.

    Keys on GROSS (amt), never on amt_net -- see the module docstring.

    Distinguisher is the raw amount, verified in ltv_v2 against raw provider
    data (14/14 Solidgate + 3/3 Stripe examples, zero exceptions):
      0.99 -> auth_settle / invoice.paid, real cents moved
      1.00 -> auth_0_amount, invoice.amount=0, Solidgate only (a hold)
      NULL -> no charge recorded at all
      0.00 -> also free. v3 2026-09-11: 205 Invinci/stripe rows carry a literal
              zero. They used to fall through to "real" (0 is neither NULL nor
              1.00), which cost nothing while money came from gross. Under
              net_revenue each would contribute -$0.057 -- the flat
              per-transaction fee applied to a zero charge -- so a free trial
              would have produced negative revenue. A zero charge is now called
              what it is.
    """
    amt = pd.to_numeric(events["amt"], errors="coerce").round(2)
    kind = pd.Series(np.nan, index=events.index, dtype="object")
    is_trial = events["event_type"].eq(TRIAL_EVENT_TYPE)
    kind[is_trial & amt.isna()] = TRIAL_FREE
    kind[is_trial & amt.notna() & amt.le(0.0)] = TRIAL_FREE
    kind[is_trial & amt.eq(TRIAL_PLACEHOLDER_AMOUNT)] = TRIAL_PLACEHOLDER
    kind[is_trial & amt.notna() & amt.gt(0.0) & amt.ne(TRIAL_PLACEHOLDER_AMOUNT)] = TRIAL_REAL
    return kind


def product_b_members(events: pd.DataFrame) -> set:
    """Emails belonging to product B ($4.99 trial -> $39.99/month), by PRICE.

    Price is known at payment time and does not depend on how the subscription
    ended, unlike cadence -- which is undefined for the 42.6% of payers who made
    exactly one payment, and leaks the outcome for everyone else. Same reasoning
    that keeps plan_interval out of the levers.
    """
    # GROSS on purpose: a list price is a product identifier, not money.
    amt = pd.to_numeric(events["amt"], errors="coerce").round(2)
    by_trial = events["event_type"].eq(TRIAL_EVENT_TYPE) & amt.eq(PRODUCT_B_TRIAL_AMOUNT)
    by_plan = events["event_type"].eq(BASE_EVENT_TYPE) & amt.ge(PRODUCT_B_BASE_MIN_AMOUNT)
    return set(events.loc[by_trial | by_plan, JOIN_KEY].dropna())


def load_events(path=None, exclude_product_b: bool = EXCLUDE_PRODUCT_B) -> pd.DataFrame:
    """Read the pull, normalise types, attach trial_kind, drop product B.

    Product B is removed at LOAD time so no downstream step can silently
    reintroduce it -- population, revenue and matrix all see the same scope.
    """
    path = Path(path) if path else newest_events_path()
    ev = pd.read_parquet(path)
    if "amt_net" not in ev.columns:
        raise ValueError(
            f"{path} predates the net_revenue switch (no amt_net column). "
            "Re-run web/v4/pull_v4_events.py. Do NOT patch around this by "
            "falling back to amt -- reporting net instead of gross is the "
            "entire point of the switch, and a silent fallback would restore "
            "the old numbers under the new column names."
        )
    ev[JOIN_KEY] = ev[JOIN_KEY].astype("string").str.lower()
    ev["ts"] = pd.to_datetime(ev["ts"], unit="s", utc=True)
    ev["amt"] = pd.to_numeric(ev["amt"], errors="coerce")
    # net_revenue = gross * rate - 0.057, so a zero or negative gross produces a
    # NEGATIVE net: the flat per-transaction fee charged against nothing. Floor
    # it here, once, rather than in each consumer, so no downstream sum can hold
    # a row that earned less than zero. When refunds are finally wired in they
    # must arrive as their own signed events -- not by letting this column go
    # negative, which would make a refund indistinguishable from a fee.
    ev["amt_net"] = pd.to_numeric(ev["amt_net"], errors="coerce").clip(lower=0.0)
    _assert_net_coverage(ev)
    ev[SESSION_COL] = _session_id(ev)
    ev["trial_kind"] = classify_trial(ev)

    if exclude_product_b:
        members = product_b_members(ev)
        ev = ev[~ev[JOIN_KEY].isin(members)].copy()
        ev.attrs["product_b_excluded"] = len(members)

    return ev.sort_values([JOIN_KEY, "ts"]).reset_index(drop=True)


def _session_id(ev: pd.DataFrame) -> pd.Series:
    """The grain every downstream groupby uses. See config.SESSION_KEY.

    In "person" mode this is a copy of the email, so the whole pipeline behaves
    exactly as v3 -- that is the point: the switch is only trustworthy if the
    changed code path is the one being exercised when it is off.
    """
    if SESSION_KEY == "person":
        return ev[JOIN_KEY]
    if SESSION_KEY != "subscription":
        raise ValueError(f"SESSION_KEY must be 'person' or 'subscription', got {SESSION_KEY!r}")
    if "subscription_key" not in ev.columns:
        raise ValueError(
            "SESSION_KEY='subscription' needs the subscription_key column, and this "
            "events file predates it. Re-run web/v4/pull_v4_events.py. Do NOT fall "
            "back to the email grain -- that silently restores every bug this mode "
            "exists to fix, under the new column names."
        )
    missing = int(ev["subscription_key"].isna().sum())
    if missing:
        raise ValueError(
            f"{missing} rows have no subscription_key. They cannot be assigned to a "
            "subscription, and defaulting them to one bucket would merge unrelated "
            "payments into a single chain. Measured at the time of writing: 0 such "
            "rows across all five pulled event types."
        )
    return ev[JOIN_KEY].astype("string") + "|" + ev["subscription_key"].astype("string")


def _assert_net_coverage(ev: pd.DataFrame) -> None:
    """Refuse to proceed if a material share of real money carries no net value.

    A row with amt > 0 and amt_net NULL is revenue that silently becomes $0.
    Known and tolerated at the time of writing: 76 paypal rows, $759.24 gross,
    2026-04/05, provider unused since -- 0.156% of captured gross. That is noise.
    A NEW hole is not, and the failure mode is invisible from the output alone
    (the LTV simply comes out lower), so it has to be caught here.
    """
    captured = ev["event_type"].isin(CAPTURED_EVENT_TYPES) & ev["amt"].gt(0)
    gross = float(ev.loc[captured, "amt"].sum())
    lost = ev[captured & ev["amt_net"].isna()]
    if lost.empty or gross <= 0:
        return

    lost_gross = float(lost["amt"].sum())
    share = lost_gross / gross
    where = ""
    if "payment_provider" in lost.columns:
        where = " by provider: " + ", ".join(
            f"{k}={v}" for k, v in lost["payment_provider"].value_counts().items()
        )
    span = f"{lost['ts'].min():%Y-%m-%d}..{lost['ts'].max():%Y-%m-%d}"
    msg = (f"{len(lost)} money rows have amt > 0 but no amt_net: ${lost_gross:,.2f} "
           f"gross, {share:.3%} of captured gross, {span}{where}")

    if share > NET_MISSING_MAX_SHARE:
        raise ValueError(
            msg + f" -- above NET_MISSING_MAX_SHARE ({NET_MISSING_MAX_SHARE:.1%}). "
            "This is revenue being dropped to zero, not a rounding issue. Find out "
            "which provider lost its net_revenue upstream before building anything."
        )
    warnings.warn(msg + " -- tolerated, see config.NET_MISSING_MAX_SHARE", stacklevel=2)


def money_events(events: pd.DataFrame) -> pd.DataFrame:
    """Only rows that carry real captured money.

    Excludes state-signal events entirely, and excludes placeholder/free trials
    (they are population members, not revenue).
    """
    keep = events["event_type"].isin(CAPTURED_EVENT_TYPES)
    not_a_dry_trial = ~(
        events["event_type"].eq(TRIAL_EVENT_TYPE)
        & events["trial_kind"].isin([TRIAL_PLACEHOLDER, TRIAL_FREE])
    )
    return events[keep & not_a_dry_trial].copy()


def per_person_revenue(events: pd.DataFrame, window_end=None) -> pd.DataFrame:
    """One row per email: trial/base/ups net revenue plus stream flags.

    window_end: cap revenue at this timestamp (used by the LOO/backtest harness
    to reconstruct "what was known as of T"). None = all time.

    Net = amt_net x (1 - REFUND_HAIRCUT): after the acquirer's cut, then after
    the refund stopgap. The haircut is still a flat portfolio rate rather than a
    per-person signal, even though refund_issued events now exist upstream --
    see config.REFUND_HAIRCUT_TODO.
    """
    money = money_events(events)
    if window_end is not None:
        money = money[money["ts"] <= window_end]

    # *_recv = received from the acquirer (amt_net summed), BEFORE the refund
    # haircut. These were named *_gross until 2026-09-11, when money moved to
    # net_revenue and the name stopped being true. They never leave this
    # function -- population.py picks up only the *_net columns and the flags.
    amt = money["amt_net"].fillna(0.0)
    by_type = {
        "trial_recv": money["event_type"].eq(TRIAL_EVENT_TYPE),
        "base_recv": money["event_type"].eq(BASE_EVENT_TYPE),
        "ups_recv": money["event_type"].eq(UPS_EVENT_TYPE),
    }
    frame = pd.DataFrame({name: amt.where(mask, 0.0) for name, mask in by_type.items()})
    frame[JOIN_KEY] = money[JOIN_KEY].values
    frame[SESSION_COL] = money[SESSION_COL].values

    # Grouped by BOTH: the session is the grain, the email rides along because
    # product B scope, reporting and any person-level question still need it.
    # In "person" mode the two columns are equal and this is a no-op.
    out = frame.groupby([JOIN_KEY, SESSION_COL], as_index=False).sum()
    for stream in ("trial", "base", "ups"):
        out[f"{stream}_net"] = out[f"{stream}_recv"] * (1.0 - REFUND_HAIRCUT)

    out["total_net"] = out["trial_net"] + out["base_net"] + out["ups_net"]
    out["is_payer"] = out["total_net"] > 0
    out["has_ups"] = out["ups_recv"] > 0
    out["has_paid_trial"] = out["trial_recv"] > 0
    return out


def per_person_week_cumulative(events: pd.DataFrame, pop: pd.DataFrame, max_week: int,
                               streams: tuple = None) -> pd.DataFrame:
    """Observed cumulative net revenue per person, by week since their entry.

    Rows = people (in `pop` order), columns = weeks 0..max_week. This is the
    FACT the forecast is scored against, so it uses the ACTUAL per-row NET
    amounts, not the modelled NET_BASE_PRICE/NET_UPS_PRICE constants. 97.9% of
    in-window base rows are exactly $9.99 gross and the remainder are real
    discounts; on top of that the acquirer's cut differs by provider, so even
    two identically-priced rows can net different amounts.

    streams: restrict to specific event types, e.g. (BASE_EVENT_TYPE,) for a
    base-only series. None = every money stream. The base-only variant is what
    fills the legacy "without upsell" columns downstream, so those never have to
    be zero-filled.

    Week is measured from the person's own first_date, so week 0 means "their
    first week", not a calendar week.
    """
    money = money_events(events)
    if streams is not None:
        money = money[money["event_type"].isin(streams)]
    first = pop.set_index(SESSION_COL)["first_date"]
    money = money[money[SESSION_COL].isin(first.index)].copy()
    # Week 0 is the SESSION's own first week, not the person's. For a second
    # subscription started months later, measuring from the person's first date
    # would put its first payment at week 30 and drop it past max_week.
    money["week"] = (
        (money["ts"] - money[SESSION_COL].map(first)).dt.days // 7
    ).clip(lower=0)
    money = money[money["week"] <= max_week]

    net = money["amt_net"].fillna(0.0) * (1.0 - REFUND_HAIRCUT)
    grid = (
        pd.DataFrame({SESSION_COL: money[SESSION_COL].values, "week": money["week"].values, "net": net.values})
        .pivot_table(index=SESSION_COL, columns="week", values="net", aggfunc="sum", fill_value=0.0)
        .reindex(index=pop[SESSION_COL], columns=range(max_week + 1), fill_value=0.0)
        .fillna(0.0)
    )
    return grid.cumsum(axis=1)


def base_payment_ladder(events: pd.DataFrame, dedup_days: float = 3.0) -> pd.DataFrame:
    """Ordered base payments per person -- the rungs the survival curve counts.

    Mirrors ios/pipeline/se_training.py: same ~3-day dedup of duplicated
    normalised rows, same step_k = cumcount + 1, same days_to_next.

    IMPORTANT (v3 fix): subscription_started rows priced at the trial amount are
    excluded. On the 2026-07-28 pull 736 such rows exist; they are trial
    activations logged under the base event name (verified NOT to be duplicates
    of trial_started -- only 1 of 736 coincides in time with one). Counting them
    as base payments would hand those people a phantom first rung and shift
    their whole curve.
    """
    base = events[events["event_type"].eq(BASE_EVENT_TYPE)].copy()
    # GROSS on purpose: 0.99 is the trial's list price, i.e. a product tag. The
    # net equivalents are 0.897162 (solidgate) and 0.892212 (stripe) -- two
    # thresholds for one product. See the module docstring.
    amt = base["amt"].round(2)
    mislabelled_trial = amt.eq(round(0.99, 2))
    base = base[~mislabelled_trial].copy()

    # Every one of these four is per SESSION, not per person. Keyed by email,
    # two parallel subscriptions interleave: the dedup below then compares a
    # payment against the OTHER subscription's payment 10-30 hours earlier and
    # deletes it (4,672 rows, $46,190), step_k counts across both chains, and
    # days_to_next measures the gap to the wrong subscription.
    base = base.sort_values([SESSION_COL, "ts"])
    gap_days = base.groupby(SESSION_COL)["ts"].diff().dt.total_seconds() / 86400
    base = base[gap_days.isna() | (gap_days > dedup_days)].copy()

    base["step_k"] = base.groupby(SESSION_COL).cumcount() + 1
    base["next_pay_ts"] = base.groupby(SESSION_COL)["ts"].shift(-1)
    base["days_to_next"] = (base["next_pay_ts"] - base["ts"]).dt.total_seconds() / 86400
    return base
