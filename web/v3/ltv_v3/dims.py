"""
Normalisation for the campaign_name / ad_name reporting dimensions.

These two fields arrive from the ad platforms via redirect URLs, so the raw
values are a mix of three populations (measured on the live pull scope,
event_date >= 2026-04-13, app_name Invinci/Unknown, money event types):

  1. REAL names, sometimes URL-ENCODED. Encoding splits one campaign into two
     categories: `AB_EN_P2W_WP%3Aredirect_O%3Ainvinci_sub` (1,460 rows) and
     `AB_EN_P2W_WP:redirect_O:invinci_sub` (1,244 rows) are the SAME campaign.
     unquote() merges them. Numeric FB campaign ids (`120250...`) are real
     campaigns named by id -- kept as-is.

  2. MACRO LEFTOVERS -- the ad platform never substituted the template:
     `{{ad.name}}` (115), `%7Bad_name%7D` = `{ad_name}` (2,556), bare `%7B` =
     `{` (148), and the literal field name `ad_name` (2,606) / `campaign_name`.

  3. EMPTY/UNKNOWN: NULL, '' (ad_name: 3,035), 'unknown' (ad_name: 5,788).

Groups 2+3 are collapsed into one explicit bucket, '(missing)' -- together
they are ~30% of ad_name rows (matches the "31%" measured for the terraform
request in docs/BQ_TABLES_WEEKLY_CURVE_V2.md). One big honest bucket instead of
six flavours of garbage smeared across the table. The bucket name matches the
utm_source fallback already used by ltv_v3/population.py.
"""
from urllib.parse import unquote

import pandas as pd

UNSET = "(missing)"

# Values that mean "the platform never told us", checked AFTER url-decoding and
# lowercasing. The literal field name shows up when the redirect template used
# the parameter name itself as the fallback value.
_GARBAGE = {"", "unknown", "none", "null", "{", "nan"}


def normalize_dim(series: pd.Series, field_name: str) -> pd.Series:
    """URL-decode, trim, and collapse every no-signal variant into UNSET.

    field_name: the source column's own name ('campaign_name' / 'ad_name') --
    its literal appearance as a value is a macro fallback, not a name.
    """
    s = series.astype("string")
    decoded = s.map(lambda v: unquote(v).strip() if pd.notna(v) else v)

    low = decoded.str.lower()
    is_garbage = (
        decoded.isna()
        | low.isin(_GARBAGE)
        | low.eq(field_name.lower())
        # unresolved macros in either template style: {ad_name}, {{ad.name}}, ...
        | (decoded.str.startswith("{") & decoded.str.endswith("}"))
    )
    return decoded.where(~is_garbage, UNSET)
