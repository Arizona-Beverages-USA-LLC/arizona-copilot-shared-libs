"""
shared_libs — Repo-level shared libraries used across multiple agents.

This is a Level-2 shared package: source code that ships inside each agent's
deploy bundle via deploy.py's extra_packages list. It is NOT a separately
deployed service. Each agent imports from it like any other Python package
(e.g. `from shared_libs.a2ui import build_a2ui_after_model_callback`,
`from shared_libs.data_export import create_export_toolset`).

Current contents:
    a2ui/         — A2UI charting: turn an agent's <a2ui-json> component tree
                     (VegaChart / Vega-Lite) into the wire format Gemini
                     Enterprise renders, inject a per-turn chart_id, and serve
                     PNG/PDF/CSV downloads. Renderer, not inferer.
    data_export/  — turn agent output into downloadable files (PNG, XLSX,
                     HTML, PDF, DOCX, PPTX). Builder-pattern ADK tools via
                     `create_export_toolset()`.
    telemetry/    — per-turn time / tokens / cache-hit / cost instrumentation.
                     Four ADK callbacks (before/after model, before/after tool)
                     plus `configure_model_tiers()` and `configure_bq_ledger()`.
                     Logs to Cloud Logging and (opt-in) appends a footer +
                     machine-readable comment to the final response.
    caching/      — ADK context-cache config + turn-1 cache pre-warming.
                     `build_app(root_agent, app_name=...)` (the canonical App +
                     ContextCacheConfig builder, replacing per-agent app_builder
                     copies) and `build_prewarm_before_callback()` /
                     `inject_prewarm_metadata()` (create the Vertex cache on the
                     FIRST turn instead of the second — saves the cold-start).

Dependency note:
    a2ui depends on data_export (a2ui_bridge + chart_download_handler import
    `shared_libs.data_export.png / .gcs_upload / ._artifact`). data_export does
    NOT depend on a2ui. telemetry and caching depend on NEITHER — they import
    nothing agent-specific (their agent-specific inputs are injected via hooks /
    arguments), so each may be vendored entirely on its own. Vendor the WHOLE
    shared_libs/ tree so a2ui's dependency is always satisfied; you may vendor
    data_export, telemetry, or caching alone, but never a2ui alone.

Adding a new shared library:
    1. Create shared_libs/<name>/ with __init__.py
    2. Implement the library
    3. Import from consuming agents as `from shared_libs.<name> import ...`
    No deploy.py change needed — extra_packages already includes ./shared_libs.
"""
