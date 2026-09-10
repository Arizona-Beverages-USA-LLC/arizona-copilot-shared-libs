"""
telemetry — per-turn time / tokens / cache / cost instrumentation for Arizona
Beverages copilots.

CANONICAL SOURCE OF TRUTH. Maintained in the org's
`arizona-copilot-shared-libs` repo and **vendored** (copied) into each agent
repo at `<agent_repo>/shared_libs/telemetry/`. Do not edit the vendored copies
directly — change it there, then re-sync (see VENDORING.md).

WHAT IT DOES
============
Instruments every user-facing turn with elapsed / LLM / tool / other time,
tokens per tier (triage / fast / deep) split input/output, cache-hit tokens,
and cost per tier. It logs the numbers to Cloud Logging under `[TELEMETRY]`
and — when `SHOW_TELEMETRY_FOOTER=1` — appends a muted footer to the final
response plus a machine-readable `<!--agent_telemetry ...-->` comment (the
cross-A2A wire contract other agents parse).

It is **agent-agnostic and decoupled**: it imports nothing from any agent's
own modules. It never imports `model_config`; instead the consuming agent tells
it which model each tier uses via `configure_model_tiers()`.

HOW TO WIRE IT INTO AN AGENT
============================
    from shared_libs.telemetry import (
        configure_model_tiers,
        telemetry_before_model_callback,
        telemetry_after_model_callback,
        telemetry_before_tool_callback,
        telemetry_after_tool_callback,
    )

    # Once at import — pass the agent's real tier models so token usage buckets
    # correctly. Any tier left None keeps its env-var / default value.
    configure_model_tiers(TRIAGE_MODEL, FAST_MODEL, DEEP_MODEL)

    # Optional: if the agent queries BigQuery, register its ledger so the
    # per-tool footer shows query economics (queries / bytes billed / cost).
    from shared_libs.telemetry import configure_bq_ledger
    from .tools.bigquery.client import reset_drained, last_drained_ledger
    configure_bq_ledger(reset=reset_drained, last_drained=last_drained_ledger)

    root_agent = Agent(
        ...,
        # chain the model-side callbacks around the agent's existing ones;
        before_tool_callback=telemetry_before_tool_callback,
        after_tool_callback=telemetry_after_tool_callback,
    )

The model-side callbacks return a possibly-modified LlmResponse (footer +
machine comment on the final call), so chain them like any other
before/after-model callback (telemetry LAST in the after-model chain so its
footer lands beneath any correction notes).

ENABLING
========
Set `SHOW_TELEMETRY_FOOTER=1` to show the footer in responses. Numbers are
always logged regardless.

RUNTIME DEPENDENCIES
====================
Only `google-adk` and `google-genai` (already required by any ADK agent). No
extra pip deps, no GCS, no agent-specific imports.
"""

from __future__ import annotations

from .turn_telemetry import (
    PRICING,
    compute_cost,
    configure_model_tiers,
    configure_bq_ledger,
    telemetry_before_model_callback,
    telemetry_after_model_callback,
    telemetry_before_tool_callback,
    telemetry_after_tool_callback,
)

__all__ = [
    "PRICING",
    "compute_cost",
    "configure_model_tiers",
    "configure_bq_ledger",
    "telemetry_before_model_callback",
    "telemetry_after_model_callback",
    "telemetry_before_tool_callback",
    "telemetry_after_tool_callback",
]
