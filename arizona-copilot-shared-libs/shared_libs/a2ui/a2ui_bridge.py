"""
A2UI Bridge — after_model_callback for native ADK Web rendering
================================================================

This module turns an LLM's `<a2ui-json>...</a2ui-json>` output into the
wire format that ADK Web's Angular frontend (and Gemini Enterprise)
recognizes and renders as native A2UI components (Cards, Columns, Rows,
Text, Tables, VegaCharts, Modals, etc.).

## Design after Option 2 refactor (2026-04-19)

Previously this module included a `_wrap_vegacharts_with_download_ui`
pass that rewrote every emitted VegaChart into a Card+Modal+Buttons
tree on the server, injecting a fresh chart_id and caching the spec.
That was our one genuine deviation from Google's canonical A2UI pattern.

After the switch to Google-standard architecture:

  - The agent's prompt now includes a full CHART_ARTIFACT_EXAMPLE
    template. The model emits the complete Card + Modal + Button tree
    itself, with the final-form chart_id already substituted from a
    per-turn system instruction set by chart_id_injector.
  - This bridge no longer modifies A2UI JSON structurally.
  - What it DOES still do for charts: after validation passes, scan
    for VegaChart specs and cache each one under the current turn's
    chart_id (read from session state key `arizona_current_chart_id`,
    set by the chart_id_injector before the LLM ran).
  - That cache is consulted by chart_download_handler when the user
    later clicks a download-format button.

So the bridge's role is now:
  1. Parse `<a2ui-json>` blocks from the model's text output
  2. Validate each A2UI message against the catalog
  3. Cache VegaChart specs under the per-turn chart_id (no JSON edits)
  4. Rebuild the response as one inline_data Part per A2UI message
  5. Set the `a2a:response` metadata flag

## Why this module exists

ADK Web's Angular bundle (`main-*.js`) already contains a full A2UI
renderer and parsing pipeline, but the renderer only activates when an
Event meets ALL FOUR of these wire conditions:

  1. Event.customMetadata contains {"a2a:response": true}
     -> gates frontend's isEventA2aResponse()
  2. Each A2UI payload is its own inline_data Part with
     mime_type == "text/plain"
  3. The Part's data bytes start with "<a2a_datapart_json>" and end
     with "</a2a_datapart_json>"
  4. The JSON between those tags is an A2A DataPart containing one
     A2UI message (beginRendering, surfaceUpdate, or dataModelUpdate)

By default `adk web` does NOT produce this wire format — it just
streams whatever the LLM said. So we rewrite the LlmResponse inside
this `after_model_callback`.

## Wire format verification

The wire format produced here has been verified end-to-end against
ADK Web v1.28.1 and Gemini Enterprise (April 2026). A styled Card with
Text, VegaChart, Modal, and Button components renders correctly when
all four conditions above are met.

## Diagnostic build

Every key step is logged via `print(..., flush=True)` because on Agent
Engine, Python `logging.*` calls don't always reach Cloud Logging, but
stdout prints are captured. All lines are prefixed `[A2UI_BRIDGE]`.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Awaitable, Callable, Optional

from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_response import LlmResponse
from google.genai import types

# a2ui-agent-sdk is a required dependency for this bridge. We import at
# module load time so a missing dep surfaces immediately rather than
# silently degrading to text-only mode.
from a2ui.schema.constants import A2UI_OPEN_TAG, A2UI_CLOSE_TAG  # tag constants for prompts
from a2ui.parser.parser import parse_response, has_a2ui_parts

# Reuse the shared schema_manager defined in a2ui_config.py. That module
# applies `remove_strict_validation` to the catalog, matching Google's
# own restaurant_finder ADK sample and allowing documented common
# properties like `weight` and `accessibility`.
from .a2ui_config import schema_manager as _SCHEMA_MANAGER

# Cache chart specs under a per-turn chart_id so the download handler
# can look them up when a format button is clicked.
from .chart_state import cache_chart_spec_with_id


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Diagnostic: module-import-time print. Seeing this in Cloud Logging after
# redeploy confirms the module at least loads.
# ---------------------------------------------------------------------------
print("[A2UI_BRIDGE] module imported — callback factory is about to be defined", flush=True)


# ---------------------------------------------------------------------------
# Resolve the catalog once at import. `.validator` is a jsonschema-backed
# validator that accepts/rejects A2UI messages against the combined
# Basic + custom catalog.
# ---------------------------------------------------------------------------
_CATALOG = _SCHEMA_MANAGER.get_selected_catalog()

# Wire-format constants. MUST match ADK Web / GE client expectations.
_A2A_DATA_PART_START_TAG = "<a2a_datapart_json>"
_A2A_DATA_PART_END_TAG = "</a2a_datapart_json>"
_A2UI_MIME_TYPE = "application/json+a2ui"

# Frontend gate. Must be truthy on Event.customMetadata for the client's
# isEventA2aResponse() to return true.
_A2A_RESPONSE_FLAG_KEY = "a2a:response"

# Session state key where chart_id_injector stashes the per-turn chart_id.
# Must match `_CHART_ID_STATE_KEY` in chart_id_injector.py.
_CHART_ID_STATE_KEY = "arizona_current_chart_id"

# Placeholder token the agent's ChartArtifact template uses for the
# interactive-chart GCS signed URL. After the LLM emits A2UI with this
# placeholder, the bridge uploads the chart's HTML to GCS, gets a signed
# URL, and does a targeted string replace on this token across all A2UI
# JSON messages before shipping to the client.
#
# Chosen to be distinctive enough that a collision with legitimate user
# text is essentially impossible. If substitution fails (upload error,
# permissions, etc.) the token remains and the user sees a broken link;
# downside is cosmetic and the logs capture the root cause.
#
# Historical note: we previously attempted to render the interactive
# link inside a WebFrameSrcdoc iframe to force new-tab behavior via
# <a target="_blank">. Gemini Enterprise blocks WebFrameSrcdoc content
# at the security-policy layer ("This UI element was blocked for
# security reasons"), so we reverted to Text + markdown link. GE
# intercepts such links through its own tracking redirect that forces
# same-tab opening; no known workaround in A2UI v0.8 + GE as of
# April 2026.
_HTML_URL_PLACEHOLDER = "HTML_URL_HERE"


# ---------------------------------------------------------------------------
# Wire-format helper.
# ---------------------------------------------------------------------------
def _make_a2ui_inline_data_part(a2ui_message: dict[str, Any]) -> types.Part:
    """Build the inline_data Part for a single A2UI message."""
    datapart_dict = {
        "data": a2ui_message,
        "kind": "data",
        "metadata": {"mimeType": _A2UI_MIME_TYPE},
    }
    wrapped = (
        _A2A_DATA_PART_START_TAG
        + json.dumps(datapart_dict, separators=(",", ":"))
        + _A2A_DATA_PART_END_TAG
    )
    return types.Part(
        inline_data=types.Blob(
            mime_type="text/plain",
            data=wrapped.encode("utf-8"),
        )
    )


# ---------------------------------------------------------------------------
# Collect all text parts from the model's LlmResponse into a single string.
# ---------------------------------------------------------------------------
def _collect_text(response: LlmResponse) -> str:
    if not response.content or not response.content.parts:
        return ""
    pieces: list[str] = []
    for part in response.content.parts:
        if part.text:
            pieces.append(part.text)
    return "".join(pieces)


# ---------------------------------------------------------------------------
# Model-drift normalization: unwrap wire-format envelopes emitted by the LLM.
#
# Gemini 2.5 Pro sometimes emits A2UI output in the bridge's OWN wire
# format instead of the agent-facing `<a2ui-json>[ ... ]</a2ui-json>`
# format. The leaked pattern looks like this, one block per A2UI
# message:
#
#   <a2a_datapart_json>
#   {"data": {"surfaceUpdate": {...}},
#    "kind": "data",
#    "metadata": {"mimeType": "application/json+a2ui"}}
#   </a2a_datapart_json>
#   <a2a_datapart_json>
#   {"data": {"beginRendering": {...}}, "kind": "data", ...}
#   </a2a_datapart_json>
#
# The content inside the `data` field IS valid A2UI. Rather than fight
# the model through prompt engineering, we unwrap the envelopes and
# rewrite as a single proper `<a2ui-json>[...]</a2ui-json>` block so
# the SDK parser handles it unchanged.
#
# Only runs when the wrong open tag is present. A response that uses
# the right tag passes through untouched.
# ---------------------------------------------------------------------------
_WIRE_OPEN_TAG = "<a2a_datapart_json>"
_WIRE_CLOSE_TAG = "</a2a_datapart_json>"
_WIRE_BLOCK_RE = re.compile(
    re.escape(_WIRE_OPEN_TAG) + r"\s*(.*?)\s*" + re.escape(_WIRE_CLOSE_TAG),
    re.DOTALL,
)


def _unwrap_wire_format_envelopes(raw_text: str) -> tuple[str, int, int]:
    """Detect and unwrap any `<a2a_datapart_json>{...}</a2a_datapart_json>`
    blocks in raw_text, replacing the full sequence of such blocks with
    one `<a2ui-json>[...]</a2ui-json>` block containing the unwrapped
    A2UI messages.

    Returns (rewritten_text, blocks_unwrapped, blocks_skipped). A block
    is skipped when its content isn't valid JSON or doesn't match the
    expected `{"data": <a2ui message>, "kind": "data", ...}` shape.
    Skipped blocks are left in the output (still wrapped in the wrong
    tag) so nothing is silently dropped; the downstream parser will then
    fail to detect A2UI and the bridge will log + pass through as text,
    which is the existing safe fallback.

    If no wire-format blocks are found, returns (raw_text, 0, 0).
    """
    if _WIRE_OPEN_TAG not in raw_text:
        return raw_text, 0, 0

    messages: list[dict] = []
    skipped = 0
    matches = list(_WIRE_BLOCK_RE.finditer(raw_text))

    for match in matches:
        payload = match.group(1)
        try:
            envelope = json.loads(payload)
        except (ValueError, TypeError) as exc:
            print(
                f"[A2UI_BRIDGE] wire-format block JSON parse failed: "
                f"{type(exc).__name__}: {exc} — skipping this block",
                flush=True,
            )
            skipped += 1
            continue

        if not isinstance(envelope, dict):
            print(
                "[A2UI_BRIDGE] wire-format envelope not a dict — skipping",
                flush=True,
            )
            skipped += 1
            continue

        data_field = envelope.get("data")
        if data_field is None:
            print(
                "[A2UI_BRIDGE] wire-format envelope missing `data` field — skipping",
                flush=True,
            )
            skipped += 1
            continue

        # The data field can either be a single A2UI message dict or
        # (rare) a list of messages. Handle both.
        if isinstance(data_field, dict):
            messages.append(data_field)
        elif isinstance(data_field, list):
            for item in data_field:
                if isinstance(item, dict):
                    messages.append(item)
                else:
                    skipped += 1
        else:
            print(
                f"[A2UI_BRIDGE] wire-format `data` field unexpected type "
                f"{type(data_field).__name__} — skipping",
                flush=True,
            )
            skipped += 1

    unwrapped = len(messages)
    if unwrapped == 0:
        # Every block was malformed. Leave raw_text as-is so the bridge's
        # no-tags-found code path trips and the text passes through.
        return raw_text, 0, skipped

    # Build replacement. Replace the entire span from the first wire-block
    # match to the last one with ONE <a2ui-json>[...]</a2ui-json> block,
    # preserving any text that came before the first block or after the
    # last block.
    first_start = matches[0].start()
    last_end = matches[-1].end()
    prefix = raw_text[:first_start]
    suffix = raw_text[last_end:]
    replacement = (
        "<a2ui-json>"
        + json.dumps(messages, separators=(",", ":"))
        + "</a2ui-json>"
    )
    # Also strip out any wire-format blocks that might appear in prefix
    # or suffix (defensive against weird interleaving, though the regex
    # above would have caught contiguous blocks).
    prefix = _WIRE_BLOCK_RE.sub("", prefix)
    suffix = _WIRE_BLOCK_RE.sub("", suffix)

    rewritten = prefix + replacement + suffix
    return rewritten, unwrapped, skipped


# ---------------------------------------------------------------------------
# Spec caching: walk all validated A2UI messages, find every VegaChart,
# cache its spec under the current turn's chart_id.
#
# The agent's ChartArtifact template uses a SINGLE chart_id per turn for
# all format buttons under a given chart. That means:
#   - If the agent emits one VegaChart, we cache one spec under the id.
#   - If the agent emits multiple VegaCharts in one turn (rare but legal)
#     they all share the same chart_id. Calling cache_chart_spec_with_id
#     with the same id overwrites the previous entry — the LAST VegaChart
#     in the response "wins" and is the one served when the user clicks
#     a download button. This is acceptable for Option 2: emitting
#     multiple downloadable charts per turn would require multiple
#     chart_ids which isn't part of the canonical template. If we need
#     that later, we'll extend the injector to stash a list of ids per
#     turn instead of a single id.
# ---------------------------------------------------------------------------

def _cache_vegacharts(
    a2ui_messages: list[dict],
    callback_context: CallbackContext,
) -> int:
    """Scan validated A2UI messages for VegaChart components and cache
    each spec under the per-turn chart_id.

    Returns the number of charts cached. Non-structural — the messages
    are not modified.
    """
    state = callback_context.state
    try:
        chart_id = state.get(_CHART_ID_STATE_KEY) if hasattr(state, "get") \
            else (state[_CHART_ID_STATE_KEY] if _CHART_ID_STATE_KEY in state else None)
    except Exception:  # noqa: BLE001
        chart_id = None

    if not chart_id:
        # No chart_id in state — the injector either didn't run or cleared
        # it. Charts will still render, but downloads will fall back to
        # "cache miss; please regenerate."
        return 0

    count = 0
    for msg in a2ui_messages:
        if not isinstance(msg, dict):
            continue
        surface_update = msg.get("surfaceUpdate")
        if not isinstance(surface_update, dict):
            continue
        components = surface_update.get("components")
        if not isinstance(components, list):
            continue

        for entry in components:
            if not isinstance(entry, dict):
                continue
            comp = entry.get("component")
            if not isinstance(comp, dict):
                continue
            vc = comp.get("VegaChart")
            if not isinstance(vc, dict):
                continue
            spec = vc.get("spec")
            if not isinstance(spec, dict):
                continue

            # Deep copy via json round-trip so later mutations don't
            # affect the cached version.
            try:
                spec_copy = json.loads(json.dumps(spec))
            except Exception:  # noqa: BLE001
                continue

            try:
                cache_chart_spec_with_id(
                    callback_context,
                    chart_id=chart_id,
                    spec=spec_copy,
                )
                count += 1
            except Exception as cache_exc:  # noqa: BLE001
                print(
                    f"[A2UI_BRIDGE] cache failed for chart_id={chart_id!r}: "
                    f"{type(cache_exc).__name__}: {cache_exc}",
                    flush=True,
                )

    return count


# ---------------------------------------------------------------------------
# Interactive HTML link: upload chart's HTML to GCS, get a signed URL,
# and substitute HTML_URL_HERE placeholder across all A2UI messages.
#
# This runs AFTER caching so we know we have at least one VegaChart and
# a chart_id in state. Designed to be best-effort — any failure leaves
# the placeholder untouched and the rest of the response ships normally.
# A broken link in chat is less bad than a missing chart altogether.
# ---------------------------------------------------------------------------

def _find_first_vegachart_spec(a2ui_messages: list[dict]) -> Optional[dict]:
    """Return the first VegaChart spec found across all messages, or None.

    We only upload once per turn (one HTML file per chart_id). If the turn
    emitted multiple VegaCharts, the first one wins for the interactive-
    link URL. Same tradeoff we accepted for the download cache.
    """
    for msg in a2ui_messages:
        if not isinstance(msg, dict):
            continue
        surface_update = msg.get("surfaceUpdate")
        if not isinstance(surface_update, dict):
            continue
        components = surface_update.get("components")
        if not isinstance(components, list):
            continue
        for entry in components:
            if not isinstance(entry, dict):
                continue
            comp = entry.get("component")
            if not isinstance(comp, dict):
                continue
            vc = comp.get("VegaChart")
            if isinstance(vc, dict):
                spec = vc.get("spec")
                if isinstance(spec, dict):
                    return spec
    return None


def _substitute_html_url_placeholder(
    a2ui_messages: list[dict],
    signed_url: str,
) -> int:
    """Find every occurrence of HTML_URL_HERE in Text component literalStrings
    across all A2UI messages and replace with the signed URL.

    Why scoped to Text literalStrings: the only legitimate place the
    placeholder should appear is in the interactive-link Text component
    the agent emits per the prompt template. We avoid touching component
    ids, action names, or schema structural fields by only walking Text
    literalStrings.

    Returns the number of substitutions made (for diagnostic logging).
    Mutates messages in place.
    """
    count = 0
    for msg in a2ui_messages:
        if not isinstance(msg, dict):
            continue
        surface_update = msg.get("surfaceUpdate")
        if not isinstance(surface_update, dict):
            continue
        components = surface_update.get("components")
        if not isinstance(components, list):
            continue
        for entry in components:
            if not isinstance(entry, dict):
                continue
            comp = entry.get("component")
            if not isinstance(comp, dict):
                continue
            text_comp = comp.get("Text")
            if not isinstance(text_comp, dict):
                continue
            text_wrapper = text_comp.get("text")
            if not isinstance(text_wrapper, dict):
                continue
            literal = text_wrapper.get("literalString")
            if not isinstance(literal, str):
                continue
            if _HTML_URL_PLACEHOLDER in literal:
                text_wrapper["literalString"] = literal.replace(
                    _HTML_URL_PLACEHOLDER, signed_url
                )
                count += 1
    return count


def _upload_and_substitute_html_url(
    a2ui_messages: list[dict],
    callback_context: CallbackContext,
) -> None:
    """If the response contains a VegaChart and an HTML_URL_HERE placeholder,
    render the chart's HTML, upload to GCS, and substitute the placeholder
    with the signed URL across all A2UI messages.

    Best-effort: any failure leaves the placeholder unchanged and logs the
    cause. The A2UI JSON remains valid; the user sees a broken link in
    that one Text component but everything else renders normally.

    Guarded for performance: only runs when a placeholder is actually present
    (check BEFORE rendering/uploading), so turns where the model didn't emit
    the template don't pay the cost.
    """
    # Cheap precheck: if no placeholder appears anywhere, skip entirely.
    # Serialize to a single string once and scan. Much cheaper than rendering
    # HTML + uploading just to discover there's nothing to replace.
    try:
        quick_scan = json.dumps(a2ui_messages)
    except Exception:  # noqa: BLE001
        return
    if _HTML_URL_PLACEHOLDER not in quick_scan:
        return

    # Get the chart_id (already set by the injector before the LLM ran).
    state = callback_context.state
    try:
        chart_id = state.get(_CHART_ID_STATE_KEY) if hasattr(state, "get") \
            else (state[_CHART_ID_STATE_KEY] if _CHART_ID_STATE_KEY in state else None)
    except Exception:  # noqa: BLE001
        chart_id = None

    if not chart_id:
        print(
            "[A2UI_BRIDGE] HTML_URL_HERE found but no chart_id in state — "
            "leaving placeholder (broken link in UI)",
            flush=True,
        )
        return

    # Find the VegaChart spec (first one wins).
    spec = _find_first_vegachart_spec(a2ui_messages)
    if spec is None:
        print(
            "[A2UI_BRIDGE] HTML_URL_HERE found but no VegaChart in response — "
            "leaving placeholder",
            flush=True,
        )
        return

    # Render the interactive HTML. Reuse the same renderer the download
    # button uses so users see identical output whether they click the
    # link or download the zip.
    try:
        from shared_libs.data_export.png import render_vega_to_html
        # Chart title: pull from spec if present, else default.
        title = "Chart"
        spec_title = spec.get("title") if isinstance(spec, dict) else None
        if isinstance(spec_title, str):
            title = spec_title
        elif isinstance(spec_title, dict):
            title = spec_title.get("text") or "Chart"
        html_bytes = render_vega_to_html(spec, title=title)
    except Exception as exc:  # noqa: BLE001
        print(
            f"[A2UI_BRIDGE] HTML render failed: {type(exc).__name__}: {exc} "
            f"— leaving placeholder",
            flush=True,
        )
        return

    # Upload to GCS and get a signed URL.
    try:
        from shared_libs.data_export.gcs_upload import upload_html_and_sign
        signed_url = upload_html_and_sign(html_bytes, chart_id=chart_id)
    except Exception as exc:  # noqa: BLE001
        print(
            f"[A2UI_BRIDGE] HTML upload failed: {type(exc).__name__}: {exc} "
            f"— leaving placeholder",
            flush=True,
        )
        return

    if not signed_url:
        # upload_html_and_sign returned None — it already logged the
        # specific cause. Leave placeholder, continue.
        print(
            "[A2UI_BRIDGE] HTML upload returned None URL — leaving placeholder",
            flush=True,
        )
        return

    # Substitute the placeholder everywhere it appears.
    sub_count = _substitute_html_url_placeholder(a2ui_messages, signed_url)
    print(
        f"[A2UI_BRIDGE] substituted HTML_URL_HERE in {sub_count} Text "
        f"component(s) with signed URL",
        flush=True,
    )


# ---------------------------------------------------------------------------
# The after_model_callback. Returning None uses the original response;
# any returned LlmResponse replaces it.
# ---------------------------------------------------------------------------
def _a2ui_after_model_callback(
    callback_context: CallbackContext,
    llm_response: LlmResponse,
) -> Optional[LlmResponse]:
    print("[A2UI_BRIDGE] callback fired", flush=True)

    # Fast paths: skip streaming chunks, errors, empty responses.
    if llm_response.error_code:
        print(
            f"[A2UI_BRIDGE] skipping — error_code set: {llm_response.error_code}",
            flush=True,
        )
        return None
    if llm_response.partial:
        print("[A2UI_BRIDGE] skipping — partial streaming chunk", flush=True)
        return None
    if not llm_response.content or not llm_response.content.parts:
        print("[A2UI_BRIDGE] skipping — no content or parts", flush=True)
        return None

    raw_text = _collect_text(llm_response)
    print(
        f"[A2UI_BRIDGE] collected text: {len(raw_text)} chars, "
        f"{len(llm_response.content.parts)} original parts",
        flush=True,
    )

    if not raw_text:
        print("[A2UI_BRIDGE] skipping — no text in response", flush=True)
        return None

    # Normalize model-drift wire-format output. When the LLM emits
    # `<a2a_datapart_json>{...}</a2a_datapart_json>` instead of the
    # expected `<a2ui-json>[...]</a2ui-json>`, unwrap the envelopes
    # and rewrite so the SDK parser finds valid A2UI.
    raw_text, unwrapped, skipped = _unwrap_wire_format_envelopes(raw_text)
    if unwrapped or skipped:
        print(
            f"[A2UI_BRIDGE] wire-format normalization: "
            f"{unwrapped} envelope(s) unwrapped, {skipped} skipped",
            flush=True,
        )

    if not has_a2ui_parts(raw_text):
        print(
            "[A2UI_BRIDGE] skipping — no <a2ui-json> tags found in text "
            f"(first 120 chars: {raw_text[:120]!r})",
            flush=True,
        )
        return None

    print("[A2UI_BRIDGE] <a2ui-json> tags FOUND — proceeding to parse", flush=True)

    # Parse the text into narrative + A2UI JSON using the SDK's parser,
    # which auto-fixes common LLM JSON mistakes (trailing commas, etc).
    try:
        response_parts = parse_response(raw_text)
        a2ui_count = sum(
            len(rp.a2ui_json) for rp in response_parts if rp.a2ui_json is not None
        )
        print(
            f"[A2UI_BRIDGE] parse_response succeeded: "
            f"{len(response_parts)} response_parts, {a2ui_count} A2UI messages",
            flush=True,
        )
    except ValueError as exc:
        print(
            f"[A2UI_BRIDGE] parse_response FAILED: {exc} — passing response through as text",
            flush=True,
        )
        logger.warning(
            "A2UI parse failed; passing response through as text. Error: %s",
            exc,
        )
        return None

    # Validate every A2UI message against the catalog. On failure, let
    # the response pass through as raw text so the user sees SOMETHING
    # rather than nothing at all.
    validator = _CATALOG.validator
    try:
        for rp in response_parts:
            if rp.a2ui_json is not None:
                for message in rp.a2ui_json:
                    validator.validate(message)
        print("[A2UI_BRIDGE] validation PASSED for all A2UI messages", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(
            f"[A2UI_BRIDGE] validation FAILED: {type(exc).__name__}: {exc} — "
            f"passing response through as text",
            flush=True,
        )
        logger.warning(
            "A2UI validation failed; passing response through as text. "
            "Error: %s",
            exc,
        )
        return None

    # Cache any VegaChart specs so downloads can find them later.
    # This is non-mutating — the messages themselves are unchanged.
    cached_count = 0
    for rp in response_parts:
        if rp.a2ui_json is not None:
            cached_count += _cache_vegacharts(rp.a2ui_json, callback_context)
    if cached_count:
        print(
            f"[A2UI_BRIDGE] cached {cached_count} VegaChart spec(s) for downloads",
            flush=True,
        )

    # Upload interactive HTML to GCS and substitute HTML_URL_HERE placeholder.
    # Best-effort: failures leave the placeholder untouched. Guarded by
    # a cheap string precheck so turns without the template don't pay the
    # upload/render cost.
    for rp in response_parts:
        if rp.a2ui_json is not None:
            try:
                _upload_and_substitute_html_url(rp.a2ui_json, callback_context)
            except Exception as exc:  # noqa: BLE001
                # Extra safety net on top of the function's own try/except.
                print(
                    f"[A2UI_BRIDGE] HTML_URL substitution outer error: "
                    f"{type(exc).__name__}: {exc} — continuing",
                    flush=True,
                )

    # Build the new parts list: text parts interleaved with one
    # inline_data Part per A2UI message.
    new_parts: list[types.Part] = []
    for rp in response_parts:
        if rp.text:
            new_parts.append(types.Part(text=rp.text))
        if rp.a2ui_json is not None:
            for message in rp.a2ui_json:
                new_parts.append(_make_a2ui_inline_data_part(message))

    if not new_parts:
        print("[A2UI_BRIDGE] skipping — nothing extractable after parse", flush=True)
        return None

    # Preserve existing custom_metadata and layer in our flag.
    merged_metadata: dict[str, Any] = {}
    if llm_response.custom_metadata:
        merged_metadata.update(llm_response.custom_metadata)
    merged_metadata[_A2A_RESPONSE_FLAG_KEY] = True

    # Construct a fresh LlmResponse.
    new_response = llm_response.model_copy(
        update={
            "content": types.Content(
                role=llm_response.content.role or "model",
                parts=new_parts,
            ),
            "custom_metadata": merged_metadata,
        }
    )

    print(
        f"[A2UI_BRIDGE] SUCCESS — rewrote response: "
        f"{len(llm_response.content.parts)} original parts -> "
        f"{len(new_parts)} new parts, a2a:response flag set",
        flush=True,
    )
    logger.info(
        "A2UI callback rewrote response: %d original part(s) -> %d new part(s) "
        "(%d A2UI message(s))",
        len(llm_response.content.parts),
        len(new_parts),
        sum(
            len(rp.a2ui_json) for rp in response_parts if rp.a2ui_json is not None
        ),
    )
    return new_response


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
A2uiAfterModelCallback = Callable[
    [CallbackContext, LlmResponse],
    Optional[LlmResponse] | Awaitable[Optional[LlmResponse]],
]


def build_a2ui_after_model_callback() -> A2uiAfterModelCallback:
    """Return an after_model_callback that renders A2UI for ADK Web / GE."""
    print(
        "[A2UI_BRIDGE] build_a2ui_after_model_callback() called — "
        "callback is being registered with the Agent",
        flush=True,
    )
    return _a2ui_after_model_callback


# ---------------------------------------------------------------------------
# Exported tag constants.
# ---------------------------------------------------------------------------
__all__ = [
    "build_a2ui_after_model_callback",
    "A2UI_OPEN_TAG",
    "A2UI_CLOSE_TAG",
]
