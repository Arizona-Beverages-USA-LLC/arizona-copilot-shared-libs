"""
ADK tool wrappers for the data_export library.

Each function in this module becomes an ADK FunctionTool when returned
from create_export_toolset(). ADK auto-wraps plain functions using type
hints + docstrings. The docstrings are seen by the model.

The tool functions declare `tool_context: ToolContext` as the FIRST
parameter. ADK recognizes this type hint and auto-injects the context at
call time (the model does NOT supply it — ADK strips it from the function
signature the model sees). This is how the tool gets access to
save_artifact(), which is the mechanism that delivers file bytes to the
Gemini Enterprise UI as a native download attachment.

Phase 1 (this commit):
    export_chart_as_png — Vega-Lite spec -> PNG -> ADK artifact

Phases 2-6 (coming):
    XLSX, HTML, PDF, DOCX, PPTX export tools — same pattern.
"""

from __future__ import annotations

from google.adk.tools.tool_context import ToolContext

from ._artifact import save_as_adk_artifact, safe_filename
from .png import render_vega_to_png


# The app caches every emitted Vega-Lite chart on session state under this key
# (see sales_copilot_agent/shared/chart_state.py). We read it by key - rather
# than importing the app module - so this library stays app-agnostic, and embed
# all of the conversation's charts into the exported PDF / PPTX.
_CHART_CACHE_STATE_KEY = "arizona.chart_cache"


def _gather_session_charts(tool_context) -> list:
    """Render every Vega-Lite chart cached in this session to PNG.

    Returns [{"title": str, "png": bytes}] for embedding in a document. Never
    raises: a chart that fails to render is skipped so the export still
    succeeds - with whatever charts do render, or with none at all.
    """
    try:
        state = getattr(tool_context, "state", None)
        cache = None
        if state is not None:
            try:
                cache = state.get(_CHART_CACHE_STATE_KEY)
            except Exception:  # noqa: BLE001
                cache = state[_CHART_CACHE_STATE_KEY] if _CHART_CACHE_STATE_KEY in state else None
        if not isinstance(cache, dict) or not cache:
            return []
    except Exception:  # noqa: BLE001
        return []

    charts = []
    for cid, entry in cache.items():
        if not isinstance(entry, dict):
            continue
        spec = entry.get("spec")
        if not spec:
            continue
        try:
            png = render_vega_to_png(spec)
        except Exception:  # noqa: BLE001
            continue
        charts.append({"id": cid, "title": entry.get("title") or "Chart", "png": png})
    return charts


# =============================================================================
# PNG — one-shot chart export
# =============================================================================

async def export_chart_as_png(
    tool_context: ToolContext,
    vega_lite_spec: dict,
    filename: str = "chart.png",
) -> dict:
    """Render a Vega-Lite chart specification as a PNG file the user can
    download from the chat as an attachment.

    Use this when the user asks for a chart as a PNG file to download (not
    for inline viewing). For inline viewing, emit a VegaChart A2UI component
    instead — this tool is for producing a downloadable image file.

    After this tool runs successfully, the PNG appears in the chat as a
    native download attachment (filename + file-type icon + download arrow).
    You do NOT need to emit any A2UI component, link, or URL — the
    attachment renders automatically.

    Args:
        vega_lite_spec: A valid Vega-Lite v5 specification as a dict. Must
            include at minimum: data, mark, and encoding. Example:
            {
                "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
                "data": {"values": [{"q": "Q1", "rev": 44}, {"q": "Q2", "rev": 47}]},
                "mark": "bar",
                "encoding": {
                    "x": {"field": "q", "type": "nominal"},
                    "y": {"field": "rev", "type": "quantitative"}
                }
            }
            Production-quality defaults are applied automatically (sorted
            axes, gridlines, colors, HiDPI rendering).
        filename: Suggested download filename. Will be sanitized and given
            a .png extension if not present. Default "chart.png".

    Returns:
        dict with keys:
            status (str): "success" when the file was saved
            filename (str): Final sanitized filename of the attachment
            mime_type (str): "image/png"
            size_kb (int): File size in KB
            format (str): "png"
            artifact_version (int): Version number assigned by ADK

        When the tool returns successfully, the user's chat UI will show
        the PNG as a downloadable attachment. Just acknowledge briefly that
        the file is ready — DO NOT re-describe the chart or emit any
        custom Link / URL / HTML / A2UI component for the download.
    """
    # 1) Render the spec to PNG bytes in memory
    png_bytes = render_vega_to_png(vega_lite_spec)

    # 2) Sanitize the filename
    clean_name = safe_filename(filename, fallback_stem="chart", format="png")

    # 3) Hand the bytes to ADK; GE will render it as a native download
    result = await save_as_adk_artifact(
        tool_context=tool_context,
        raw_bytes=png_bytes,
        filename=clean_name,
        format="png",
    )

    # 4) Return a tiny metadata dict (no bytes, no URL — the model never
    #    sees the file contents, only what was saved)
    return result.to_dict()


_REPORT_LOG_STATE_KEY = "arizona.report_log"


def _load_report_text(tool_context, scope: str, content: str) -> str:
    """Resolve the text to export.

    scope="last"    -> the most recent captured analysis (verbatim).
    scope="all"     -> every captured analysis this session, concatenated.
    scope="content" -> the caller-supplied `content` (explicit override).

    The log is written by sales_copilot_agent/shared/report_capture.py as the
    raw model output, so the export matches EXACTLY what the user saw. Falls
    back to `content` if nothing was captured.
    """
    sc = (scope or "last").strip().lower()
    if sc == "content":
        return content or ""
    log = None
    try:
        state = getattr(tool_context, "state", None)
        if state is not None:
            try:
                log = state.get(_REPORT_LOG_STATE_KEY)
            except Exception:  # noqa: BLE001
                log = state[_REPORT_LOG_STATE_KEY] if _REPORT_LOG_STATE_KEY in state else None
    except Exception:  # noqa: BLE001
        log = None
    if not isinstance(log, list) or not log:
        return content or ""
    if sc == "all":
        return "\n\n---\n\n".join(str(x) for x in log if x)
    return str(log[-1])


# =============================================================================
# PDF / PPTX - analysis document export (markdown -> formatted file)
# =============================================================================

async def export_report_as_pdf(
    tool_context: ToolContext,
    content: str = "",
    title: str = "Analysis",
    filename: str = "analysis.pdf",
    include_charts: bool = True,
    scope: str = "last",
) -> dict:
    """Export an analysis you already produced as a formatted PDF the user can
    download from the chat.

    IMPORTANT - do NOT re-type, paste, or summarize the analysis. This tool
    exports the EXACT text already shown in the chat, pulled verbatim from the
    session. Just choose the scope:
      scope="last" (default) -> the most recent analysis.
      scope="all"            -> the ENTIRE chat: every analysis produced this
        session, in full (use for "export the whole chat / everything").
    Only set scope="content" and pass `content` to export custom/edited text you
    supply yourself - and then it must be the COMPLETE analysis verbatim, never
    an abridged summary.

    Charts you showed are embedded INLINE at their [[chart: ...]] markers (already
    present in the captured text); any leftover charts go at the end. Set
    include_charts=false to omit charts.

    After this runs, the PDF appears as a native download attachment. Do NOT emit
    any link, URL, or A2UI component - just briefly say the PDF is ready.

    Args:
        content: Optional override markdown. Leave empty to export the captured
            analysis per `scope`. If provided (scope="content"), it must be the
            full analysis verbatim, not a summary.
        title: Document title on the cover (e.g. "Kroger & Walmart 360").
        filename: Suggested download filename; sanitized, given a .pdf extension.
        include_charts: When true (default), embed the session's charts.
        scope: "last" (most recent analysis), "all" (entire chat), or "content".

    Returns:
        dict with status, filename, mime_type, size_kb, format, artifact_version.
        Just acknowledge the PDF is ready; do not re-describe the report.
    """
    from .document import render_markdown_to_pdf

    text = _load_report_text(tool_context, scope, content)
    charts = _gather_session_charts(tool_context) if include_charts else []
    pdf_bytes = render_markdown_to_pdf(text, title=title, charts=charts)
    clean_name = safe_filename(filename or title, fallback_stem="analysis", format="pdf")
    result = await save_as_adk_artifact(
        tool_context=tool_context, raw_bytes=pdf_bytes, filename=clean_name, format="pdf",
    )
    return result.to_dict()


async def export_report_as_pptx(
    tool_context: ToolContext,
    content: str = "",
    title: str = "Analysis",
    filename: str = "analysis.pptx",
    include_charts: bool = True,
    scope: str = "last",
) -> dict:
    """Export an analysis you already produced as a formatted PowerPoint the user
    can download from the chat.

    IMPORTANT - do NOT re-type, paste, or summarize the analysis. This tool
    exports the EXACT text already shown in the chat, pulled verbatim from the
    session. Just choose the scope:
      scope="last" (default) -> the most recent analysis.
      scope="all"            -> the ENTIRE chat: every analysis produced this
        session, in full (use for "export the whole chat / everything").
    Only set scope="content" and pass `content` to export custom/edited text you
    supply yourself - and then it must be the COMPLETE analysis verbatim.

    A branded title slide is created, then each '##' section becomes slides (its
    bullets, then each supporting table). Charts you showed become slides INLINE
    at their [[chart: ...]] markers; leftover charts go at the end. Set
    include_charts=false to omit charts.

    After this runs, the .pptx appears as a native download attachment. Do NOT
    emit any link, URL, or A2UI component - just briefly say the deck is ready.

    Args:
        content: Optional override markdown. Leave empty to export the captured
            analysis per `scope`. If provided (scope="content"), it must be the
            full analysis verbatim, not a summary.
        title: Deck title on the title slide.
        filename: Suggested download filename; sanitized, given a .pptx extension.
        include_charts: When true (default), embed the session's charts as slides.
        scope: "last" (most recent analysis), "all" (entire chat), or "content".

    Returns:
        dict with status, filename, mime_type, size_kb, format, artifact_version.
        Just acknowledge the deck is ready; do not re-describe the report.
    """
    from .document import render_markdown_to_pptx

    text = _load_report_text(tool_context, scope, content)
    charts = _gather_session_charts(tool_context) if include_charts else []
    pptx_bytes = render_markdown_to_pptx(text, title=title, charts=charts)
    clean_name = safe_filename(filename or title, fallback_stem="analysis", format="pptx")
    result = await save_as_adk_artifact(
        tool_context=tool_context, raw_bytes=pptx_bytes, filename=clean_name, format="pptx",
    )
    return result.to_dict()


# =============================================================================
# Toolset factory
# =============================================================================

def create_export_toolset() -> list:
    """Return the list of ADK tools for data export.

    Usage in an agent module:

        from shared_libs.data_export import create_export_toolset
        root_agent = Agent(
            ...,
            tools=[create_tableau_toolset(), *create_export_toolset()],
        )

    Note: ADK auto-wraps these plain async functions as FunctionTools using
    their type hints and docstrings. No explicit FunctionTool construction
    needed. The `tool_context: ToolContext` first parameter tells ADK to
    inject the context automatically — the model does not see or supply it.

    Phase 1: PNG only. Future phases will add XLSX, HTML, PDF, DOCX, PPTX.
    """
    return [
        export_chart_as_png,
        export_report_as_pdf,
        export_report_as_pptx,
    ]
