"""
Chart download handler — intercepts A2UI userAction events and serves
file downloads without spending an LLM turn.

PURPOSE
=======
The A2UI bridge wraps every VegaChart the agent emits in a Card with a
Download button + format-chooser Modal. When the user picks a format and
submits, GE sends a userAction DataPart back to the agent as a new "turn".

Without interception, that userAction would be handed to the LLM, which
would need to parse it, call an export tool, and respond. That's slow,
costs tokens, and is prone to inconsistency (the LLM might add commentary,
skip the tool call, or emit the wrong format).

This handler runs as a `before_model_callback`. It inspects every inbound
LlmRequest; when it finds a userAction with action.name == "download_chart"
plus a valid chart_id + format in context, it SHORT-CIRCUITS the LLM turn:
    1. Look up the cached spec (by chart_id)
    2. Render in the chosen format
    3. Save via tool_context.save_artifact
    4. Return a minimal LlmResponse with "Your <format> file is ready."
    5. LLM is never called this turn; token cost is ~zero.

If the userAction isn't one we handle, the handler returns None, letting
the normal LLM flow proceed.

DESIGN NOTES
============
- Returning an LlmResponse from before_model_callback skips the LLM AND
  the after_model_callback. So our response content must be plain text
  (not A2UI — it would arrive to GE unprocessed). Plain text acknowledging
  the download is fine; GE renders the native download attachment
  alongside.
- save_artifact on the CallbackContext works the same way as on
  ToolContext — the artifact gets attached to the event stream and GE
  renders the native download card. Same mechanism we use for
  export_chart_as_png today.
- This callback must coexist with the existing a2ui_bridge
  after_model_callback. The bridge runs on outbound responses; this
  handler runs on inbound requests. They don't interact.
- Defense in depth: every render/save failure returns a graceful text
  response; we never let an exception bubble and kill the turn.

FORMATS SUPPORTED (v1)
======================
    png  — render_vega_to_png
    svg  — render_vega_to_svg
    html — render_vega_to_html (interactive; preserves hover)
    pdf  — render_vega_to_pdf
    csv  — render_data_to_csv (raw data rows)
"""

from __future__ import annotations

import io
import json
import logging
import zipfile
from typing import Any, Optional

from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types

from .chart_state import get_cached_chart_spec


logger = logging.getLogger(__name__)


# The action names we handle. If the userAction name isn't in this set, we
# pass through so the LLM can handle it normally.
_HANDLED_ACTION = "download_chart"

# The action name fired by the Modal's entryPointChild Button when the user
# clicks "Download". This is a no-op from our perspective: the Modal
# opens the format-picker UI on the client side, we don't need to do
# anything. But if we let this action propagate to the LLM, GE shows a
# "User action triggered / Thinking..." bubble in chat, which is user-
# visible noise. So we short-circuit with a minimal empty-ish response.
_MODAL_OPEN_ACTION = "open_download_modal"

# Supported formats -> render function + file extension. Import lazily
# so the module loads even if some renderer deps aren't installed (e.g.
# reportlab for PDF in a minimal test).
#
# Note on HTML: GE's chat UI refuses to display .html downloads as an
# attachment card (security policy — HTML can contain scripts). So when
# the user picks "html" format we render the HTML, WRAP IT IN A ZIP
# containing chart.html, and deliver the zip. GE happily shows zip files
# as download cards; the user extracts the zip locally and opens the
# interactive chart in their browser. See _render_html_as_zip below.
# The user-facing format name stays "html" so the prompt template
# doesn't need changes; the transformation is handler-internal.
_FORMAT_EXT = {
    "png": "png",
    "svg": "svg",
    "html": "zip",  # delivered as chart.zip containing chart.html
    "pdf": "pdf",
    "csv": "csv",
}

# Name of the HTML file INSIDE the zip. When the user extracts the zip
# they'll see exactly this filename, then double-click to open it in
# their browser.
_HTML_INNER_FILENAME = "chart.html"


# =============================================================================
# userAction extraction
# =============================================================================

def _extract_useraction_from_request(llm_request: LlmRequest) -> Optional[dict]:
    """Scan the inbound request for an A2UI userAction DataPart.

    Returns the userAction dict (from the JSON envelope's data.userAction)
    if found, else None. We look at all contents — userActions typically
    arrive on the latest "user" role Content, but we scan all to be safe.
    """
    for content in reversed(llm_request.contents or []):
        parts = getattr(content, "parts", []) or []
        for part in parts:
            action = _useraction_from_part(part)
            if action is not None:
                return action
    return None


def _useraction_from_part(part) -> Optional[dict]:
    """If a Part contains an A2UI userAction, return it as a dict. Else None.

    userActions arrive inline as bytes wrapped in <a2a_datapart_json>
    envelope (confirmed via the probe at 2026-04-19). The wire format is:

        <a2a_datapart_json>
        {"data": {"userAction": {"name": "...", "context": {...}, ...}},
         "metadata": {"mimeType": "application/json+a2ui"}}
        </a2a_datapart_json>
    """
    inline_data = getattr(part, "inline_data", None)
    if inline_data is None:
        return None

    data = getattr(inline_data, "data", None)
    if not isinstance(data, (bytes, bytearray)):
        return None

    try:
        decoded = data.decode("utf-8")
    except UnicodeDecodeError:
        return None

    if "userAction" not in decoded:
        return None

    # Strip the envelope tags if present.
    inner = decoded.strip()
    if inner.startswith("<a2a_datapart_json>") and inner.endswith("</a2a_datapart_json>"):
        inner = inner[len("<a2a_datapart_json>"):-len("</a2a_datapart_json>")]

    try:
        payload = json.loads(inner)
    except json.JSONDecodeError:
        return None

    # payload shape: {"data": {"userAction": {...}}, "metadata": {...}}
    action = (
        payload.get("data", {}).get("userAction")
        if isinstance(payload, dict) else None
    )
    return action if isinstance(action, dict) else None


# =============================================================================
# Context extraction from the userAction payload
# =============================================================================

def _context_value(context: Any, key: str) -> Optional[str]:
    """Extract a string value for `key` from the A2UI action context.

    A2UI v0.8 context can come in TWO shapes depending on how the agent
    emitted the Button:

    Shape 1 — array form (matches our custom_catalog.json Button schema):
        "context": [
            {"key": "chart_id", "value": {"literalString": "abc12345"}},
            {"key": "format",   "value": {"literalString": "png"}}
        ]

    Shape 2 — map form (what GE sometimes normalizes to):
        "context": {
            "chart_id": "abc12345",
            "format": "png"
        }

    We handle both. If the value is a dict with literalString, unwrap it.
    """
    if isinstance(context, list):
        for entry in context:
            if not isinstance(entry, dict):
                continue
            if entry.get("key") == key:
                val = entry.get("value")
                if isinstance(val, dict):
                    return val.get("literalString") or val.get("path")
                if isinstance(val, str):
                    return val
        return None

    if isinstance(context, dict):
        val = context.get(key)
        if isinstance(val, dict):
            return val.get("literalString") or val.get("path")
        if isinstance(val, str):
            return val
        return None

    return None


# =============================================================================
# Rendering dispatch
# =============================================================================

def _render_html_as_zip(html_bytes: bytes, inner_filename: str = _HTML_INNER_FILENAME) -> bytes:
    """Wrap HTML bytes in a zip file containing a single chart.html member.

    Why: GE refuses to render a download card for .html attachments
    regardless of MIME type (security policy — HTML can contain scripts
    that run when opened). Zip files pass through GE cleanly. The user
    extracts locally, then opens chart.html in a browser to get the full
    interactive Vega chart with hover tooltips.

    Implementation notes:
    - Stdlib zipfile, no new dependencies.
    - ZIP_DEFLATED compression level 6 (default) — a 3KB interactive
      Vega HTML doesn't need fancy compression; the zip overhead itself
      dominates.
    - The inner filename is "chart.html" by default so the extracted
      file is unambiguous. Could parameterize per-chart in future if
      we want chart-specific names inside the zip.
    - Uses io.BytesIO so we never hit disk.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(inner_filename, html_bytes)
    return buf.getvalue()


def _render_by_format(fmt: str, spec: dict, title: str) -> bytes:
    """Render the spec in the requested format. Raises on error.

    Special case for fmt=='html': we render the HTML, then wrap it in a
    zip so GE will accept it as a download attachment. The returned bytes
    are the zip bytes; callers MUST save under a .zip filename for MIME
    consistency.
    """
    from shared_libs.data_export.png import (
        render_vega_to_png,
        render_vega_to_svg,
        render_vega_to_html,
        render_vega_to_pdf,
        render_data_to_csv,
    )

    if fmt == "png":
        return render_vega_to_png(spec)
    if fmt == "svg":
        return render_vega_to_svg(spec)
    if fmt == "html":
        # Render raw HTML first, then wrap in a zip. The zip is what
        # actually gets saved as the artifact.
        html_bytes = render_vega_to_html(spec, title=title)
        return _render_html_as_zip(html_bytes)
    if fmt == "pdf":
        return render_vega_to_pdf(spec, title=title)
    if fmt == "csv":
        return render_data_to_csv(spec)
    raise ValueError(f"Unsupported format: {fmt!r}")


# =============================================================================
# Response builder (short-circuit text-only response)
# =============================================================================

def _text_response(text: str) -> LlmResponse:
    """Build a minimal LlmResponse carrying a single text part.

    Used to short-circuit the LLM call when we've handled the userAction
    ourselves. The text is all the user sees in chat (alongside the native
    download attachment GE renders from the artifact we just saved).
    """
    return LlmResponse(
        content=types.Content(
            role="model",
            parts=[types.Part(text=text)],
        )
    )


# =============================================================================
# The callback
# =============================================================================

async def _chart_download_callback(
    callback_context: CallbackContext,
    llm_request: LlmRequest,
) -> Optional[LlmResponse]:
    """Before-model hook: intercept download_chart userActions and serve them.

    Returns an LlmResponse to short-circuit when we handle the action.
    Returns None to let the normal LLM flow proceed (not our action).
    """
    try:
        action = _extract_useraction_from_request(llm_request)
        if action is None:
            # No userAction in this request; pass through silently.
            return None

        name = action.get("name")

        # Short-circuit the entry-button click: the Modal opens client-
        # side, no agent work needed, but we must not propagate to the
        # LLM or the user sees a spurious "Thinking..." turn.
        if name == _MODAL_OPEN_ACTION:
            print(
                f"[CHART_DL] open_download_modal - silently short-circuiting "
                f"(no LLM call, no user-visible message)",
                flush=True,
            )
            # Empty single-space text is the minimum LlmResponse that
            # doesn't trigger a "model returned no content" error but
            # also doesn't render as a chat bubble in GE. Tested
            # empirically 2026-04-19.
            return _text_response(" ")

        if name != _HANDLED_ACTION:
            # Some other action (not a download); let the LLM handle it.
            print(
                f"[CHART_DL] userAction name={name!r} not handled here, "
                f"passing to LLM",
                flush=True,
            )
            return None

        # ---- It IS a download_chart action. Extract parameters. ----
        context_obj = action.get("context")
        chart_id = _context_value(context_obj, "chart_id")
        fmt = _context_value(context_obj, "format")

        print(
            f"[CHART_DL] download_chart userAction received: "
            f"chart_id={chart_id!r} format={fmt!r}",
            flush=True,
        )

        if not chart_id or not fmt:
            return _text_response(
                "Download request was malformed (missing chart_id or format). "
                "Please ask me to re-generate the chart and try again."
            )

        if fmt not in _FORMAT_EXT:
            return _text_response(
                f"Sorry, '{fmt}' isn't a supported download format. "
                f"Supported: PNG, SVG, HTML, PDF, CSV."
            )

        # ---- Look up cached spec ----
        entry = get_cached_chart_spec(callback_context, chart_id)
        if entry is None:
            print(
                f"[CHART_DL] cache miss for chart_id={chart_id!r}",
                flush=True,
            )
            return _text_response(
                "I couldn't find the chart to download. It may have been from "
                "an earlier conversation. Please ask me to re-generate it."
            )

        spec = entry.get("spec")
        title = entry.get("title") or "Chart"
        stem = entry.get("filename") or "chart"

        # ---- Render ----
        try:
            raw_bytes = _render_by_format(fmt, spec, title)
        except Exception as render_exc:  # noqa: BLE001
            logger.exception("Chart render failed for format=%s", fmt)
            print(
                f"[CHART_DL] render failed: {type(render_exc).__name__}: "
                f"{render_exc}",
                flush=True,
            )
            return _text_response(
                f"Sorry, I hit an error rendering the chart as {fmt.upper()}. "
                f"Please try a different format."
            )

        # ---- Save as ADK artifact ----
        from shared_libs.data_export._artifact import save_as_adk_artifact, safe_filename

        # The "save format" is what determines the file extension and MIME
        # type on the wire. For HTML, the user picked fmt="html" but we've
        # wrapped the bytes in a zip — so we save under format="zip"
        # (extension .zip, MIME application/zip) to match what GE actually
        # receives. The user-visible message still says "HTML" so they know
        # what's inside.
        save_fmt = "zip" if fmt == "html" else fmt
        save_stem = stem + "_html" if fmt == "html" else stem
        filename = safe_filename(save_stem, fallback_stem="chart", format=save_fmt)

        try:
            result = await save_as_adk_artifact(
                tool_context=callback_context,  # Context.save_artifact has same interface
                raw_bytes=raw_bytes,
                filename=filename,
                format=save_fmt,
            )
        except Exception as save_exc:  # noqa: BLE001
            logger.exception("save_as_adk_artifact failed")
            print(
                f"[CHART_DL] save failed: {type(save_exc).__name__}: "
                f"{save_exc}",
                flush=True,
            )
            return _text_response(
                f"Rendered the {fmt.upper()} but couldn't attach it. "
                f"Please try again."
            )

        print(
            f"[CHART_DL] SUCCESS: saved {filename} ({result.size_kb} KB) "
            f"as artifact v{result.artifact_version}",
            flush=True,
        )

        # Short, factual response. GE renders the download card from the
        # artifact. For HTML-in-zip, tell the user they need to extract.
        if fmt == "html":
            return _text_response(
                f"Your interactive HTML chart is ready ({result.size_kb} KB, "
                f"zipped). Download it, extract the zip, and open "
                f"`{_HTML_INNER_FILENAME}` in your browser for the full "
                f"interactive chart with hover tooltips."
            )
        return _text_response(
            f"Your {fmt.upper()} download is ready ({result.size_kb} KB)."
        )

    except Exception as outer_exc:  # noqa: BLE001
        # Absolute last resort: a bug in the handler must NEVER kill the turn.
        # Fall through to LLM processing and log the failure.
        logger.exception("chart_download_callback outer error")
        print(
            f"[CHART_DL] outer error - falling back to LLM: "
            f"{type(outer_exc).__name__}: {outer_exc}",
            flush=True,
        )
        return None


def build_chart_download_callback():
    """Factory matching the pattern used by a2ui_bridge and useraction_probe."""
    print(
        "[CHART_DL] build_chart_download_callback() called - "
        "handler registered as before_model_callback",
        flush=True,
    )
    return _chart_download_callback


__all__ = ["build_chart_download_callback"]
