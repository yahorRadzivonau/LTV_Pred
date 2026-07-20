"""
ltv_v2: EXPERIMENTAL pipeline built directly on the appsflyer BQ source
(appsflyer-data-411716.silver_layer.web_conversions), instead of the golden
Stripe/Solidgate pipeline (ltv/, web_person_level_*.py).

Isolation contract: nothing under ltv_v2/ imports from or writes into ltv/,
models/, or any of the golden-pipeline scripts/reports. The working
reconcile.py (golden pipeline) is untouched and must stay green -- see
reconcile_v2.py for this version's own gate.

Status: STEP 1-3 (isolation, revenue extraction, validation gate) complete.
Tables (STEP 4+) not yet built -- pending gate review.
"""
