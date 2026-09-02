"""
a2ui — shared A2UI charting package for the Arizona Beverages copilots.

CANONICAL SOURCE OF TRUTH. This package is maintained in the organization's
`a2ui-shared` GitHub repo and **vendored** (copied) into each agent repo at
`<agent_repo>/shared_libs/a2ui/`. Do not edit the vendored copies directly —
change it here, then re-sync (see VENDORING.md).

WHAT IT DOES
============
Turns an agent's `<a2ui-json>` output (a Google-standard A2UI component tree,
charts via VegaChart / Vega-Lite) into the wire format ADK Web / Gemini
Enterprise render natively, and provides per-turn chart-id injection + a
download handler (PNG / PDF / CSV) for the canonical ChartArtifact template.
It is a RENDERER, not an inferer: the agent writes the chart spec; this package
parses, validates, caches, and packages it. No chart inference happens here.

HOW TO WIRE IT INTO AN AGENT
============================
    from shared_libs.a2ui import (
        build_combined_before_model_callback,   # before_model_callback
        build_a2ui_after_model_callback,        # after_model_callback
    )

    root_agent = Agent(
        ...,
        before_model_callback=build_combined_before_model_callback(),  # download + chart_id
        after_model_callback=build_a2ui_after_model_callback(),        # parse/validate/render
    )

If the agent also wants telemetry (this package is intentionally telemetry-
DECOUPLED), chain the agent's own telemetry callbacks around these — this
package does not import turn_telemetry.

RUNTIME DEPENDENCIES (must be present in the target agent)
==========================================================
  * `a2ui-agent-sdk` (import name `a2ui`) — add to requirements.txt AND the
    inline deploy requirements in deploy.py / deploy_a2a.py.
  * `shared_libs.data_export` (png / gcs_upload / _artifact) — vendored beside
    this package in the same repo; needed for chart HTML render + GCS-signed
    download links.
  * The GCS bucket + service-account signing config used by data_export.

STATE KEYS (shared across agents; safe because sessions are per-agent)
=====================================================================
  * chart id : `arizona_current_chart_id`   (chart_id_injector <-> a2ui_bridge)
  * spec cache: `arizona.chart_cache`        (chart_state)
"""

from __future__ import annotations

# a2ui_config resolves the catalog at import (loads custom_catalog.json +
# webframe_example.html beside this file), so import it first.
from .a2ui_config import schema_manager
from .chart_state import (
    cache_chart_spec,
    cache_chart_spec_with_id,
    get_cached_chart_spec,
)
from .chart_download_handler import build_chart_download_callback
from .chart_id_injector import (
    build_chart_id_injector,
    build_combined_before_model_callback,
    inject_chart_id_for_turn,
)
from .a2ui_bridge import (
    build_a2ui_after_model_callback,
    A2UI_OPEN_TAG,
    A2UI_CLOSE_TAG,
)

__all__ = [
    # before_model_callback factories
    "build_combined_before_model_callback",
    "build_chart_id_injector",
    "inject_chart_id_for_turn",
    # after_model_callback factory
    "build_a2ui_after_model_callback",
    # download handler
    "build_chart_download_callback",
    # spec cache
    "cache_chart_spec",
    "cache_chart_spec_with_id",
    "get_cached_chart_spec",
    # catalog + tag constants
    "schema_manager",
    "A2UI_OPEN_TAG",
    "A2UI_CLOSE_TAG",
]
