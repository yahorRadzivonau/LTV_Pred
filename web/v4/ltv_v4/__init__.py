"""
ltv_v4: per-person web LTV pipeline on the appsflyer BQ source.

What makes it v3 (vs ltv_v2): the forecast is built PER PERSON and aggregated
per cell, instead of scaling a cell's observed fact by one global growth ratio.
In v2 the web matrix carries a single app_id ('invinci'), so map_model computes
one hr for the whole product and the lever multipliers average away into a
single pooled curve -- two cells of the same age get an identical multiplier no
matter their funnel/source/geo. Here app_id = cohort_date, hr is computed per
cohort, and the personal multipliers enter through the cell's own people.

Isolation contract (same spirit as ltv_v2, stricter on writes):
  - nothing under web/v4/ WRITES outside data/web_v4/ and reports/web_v4/
  - core/map_model.py is NOT modified: its lever list is a module global shared
    with the iOS model, so v3 carries its own copy in ltv_v4/map_web.py and
    only reads pure helpers (common.empirical_hbase, HMAX, K_SHRINK) from core
  - web/v2/, web/golden/, reports/web_v2/ and the BigQuery predictions table
    stay untouched -- v2 remains the reproducible baseline for comparison

Spec: docs/WEB_V3_SPEC.md
"""
