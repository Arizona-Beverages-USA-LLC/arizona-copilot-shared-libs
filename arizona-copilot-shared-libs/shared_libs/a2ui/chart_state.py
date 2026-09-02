"""
Chart state cache — session-scoped storage of Vega-Lite specs.

PURPOSE
=======
When the agent emits a VegaChart component in an A2UI block, the A2UI bridge
wraps it with a Download Button + format Modal. The Button carries a chart_id
in its userAction context. When the user later clicks the Download button and
picks a format, the userAction arrives at the agent — but the Vega-Lite spec
isn't in that message (GE only forwards the action name + context values).

So we need to have stashed the spec somewhere keyed by chart_id at the time
the chart was FIRST emitted, and retrieve it when the userAction arrives.
That's what this module provides.

STORAGE
=======
Uses ADK's session state (Context.state / ToolContext.state), which is a
dict-like object backed by the invocation's session. State is session-scoped,
survives across turns within the same chat, and is isolated per-user.
Importantly for Agent Engine: the InMemoryArtifactService backing this state
is provisioned automatically, no extra config needed.

We store a single top-level dict under key "arizona.chart_cache" to avoid
namespace collisions with other state keys. Inside that dict each chart_id
maps to a dict with the spec + metadata needed to re-render on demand.

LIFECYCLE
=========
We don't explicitly evict entries. Each session is ephemeral (a chat
conversation); when the session ends the state goes with it. If a single
conversation accumulates hundreds of charts, memory could grow; in practice
that's unlikely. If it becomes a problem later, we can add an LRU cap here.

API
===
    cache_chart_spec(ctx, spec, filename=None, title=None) -> chart_id
        -> auto-generates a fresh chart_id and returns it (legacy path,
           retained for backwards compatibility with any caller that
           wants the cache to pick the id).

    cache_chart_spec_with_id(ctx, chart_id, spec, filename=None, title=None)
        -> stores under a caller-supplied chart_id (Option 2 path:
           the chart_id is set by before_model_callback BEFORE the
           model runs, so the bridge needs to cache under that same
           id rather than mint a new one).

    get_cached_chart_spec(ctx, chart_id) -> dict or None

All three are synchronous because the underlying state access is in-memory.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Optional


logger = logging.getLogger(__name__)


# Top-level state key. Namespaced with "arizona." prefix so we don't
# collide with ADK's own state keys or future user-controlled ones.
_STATE_KEY = "arizona.chart_cache"


def _get_cache(ctx) -> dict:
    """Return (and lazily create) the chart cache dict on session state."""
    state = ctx.state
    try:
        existing = state.get(_STATE_KEY)
    except AttributeError:
        # If state doesn't support .get, try dict-style access
        existing = state[_STATE_KEY] if _STATE_KEY in state else None

    if existing is None:
        cache: dict = {}
        state[_STATE_KEY] = cache
        return cache

    if not isinstance(existing, dict):
        # Corrupt state — start fresh rather than raise
        logger.warning(
            "chart_cache state key has unexpected type %s; resetting",
            type(existing).__name__,
        )
        cache = {}
        state[_STATE_KEY] = cache
        return cache

    return existing


def cache_chart_spec(
    ctx,
    spec: dict,
    filename: Optional[str] = None,
    title: Optional[str] = None,
) -> str:
    """Store a Vega-Lite spec in session state and return a unique chart_id.

    Called by the A2UI bridge when it detects a VegaChart component in an
    outbound A2UI message. The returned chart_id is then embedded in the
    download Button's userAction context so a later click can look up this
    spec.

    Args:
        ctx: The ADK Context (from before_model_callback) or ToolContext.
            Must expose .state.
        spec: The Vega-Lite specification dict.
        filename: Optional user-friendly filename stem (extension added per
            format at export time). If None, defaults to "chart".
        title: Optional chart title (used for PDF page title, HTML page
            title, etc.). If None, attempts to read from spec.title.

    Returns:
        A chart_id string (short uuid prefix, 8 chars) keyed into the cache.
    """
    cache = _get_cache(ctx)

    # Short uuid prefix (8 chars of hex) is plenty unique inside one session
    # and keeps component ids readable.
    chart_id = uuid.uuid4().hex[:8]

    # Resolve title if not explicit
    if title is None:
        spec_title = spec.get("title") if isinstance(spec, dict) else None
        if isinstance(spec_title, str):
            title = spec_title
        elif isinstance(spec_title, dict):
            title = spec_title.get("text") or "Chart"
        else:
            title = "Chart"

    cache[chart_id] = {
        "spec": spec,
        "filename": filename or "chart",
        "title": title,
    }

    # Reassign so state delta is recorded. ADK State objects track mutations,
    # but to be safe with the delta mechanism we reassign the top-level key.
    ctx.state[_STATE_KEY] = cache

    logger.info(
        "chart_cache: stored chart_id=%s title=%r (cache size now %d)",
        chart_id, title, len(cache),
    )
    return chart_id


def cache_chart_spec_with_id(
    ctx,
    chart_id: str,
    spec: dict,
    filename: Optional[str] = None,
    title: Optional[str] = None,
) -> None:
    """Store a spec under a CALLER-SUPPLIED chart_id.

    This is the Option 2 path. In the canonical Google-standard flow,
    the chart_id is chosen by `chart_id_injector.inject_chart_id_for_turn`
    BEFORE the LLM runs, stashed in session state, and woven into the
    agent's per-turn instruction. When the LLM emits A2UI that references
    that chart_id in its format buttons, the bridge needs to cache the
    VegaChart spec under that SAME id — not mint a fresh one.

    Overwrites any existing entry at this chart_id. If the agent emits
    multiple VegaCharts in one turn, the last one wins. That's acceptable
    for v1; see a2ui_bridge._cache_vegacharts comments.

    Args:
        ctx: ADK Context or ToolContext exposing .state.
        chart_id: the caller-supplied id. Must match what the agent put
            in the format buttons' action.context.
        spec: Vega-Lite specification dict.
        filename: optional filename stem (extension added by exporter).
        title: optional chart title (for PDF/HTML page titles).
    """
    cache = _get_cache(ctx)

    # Resolve title if not explicit.
    if title is None:
        spec_title = spec.get("title") if isinstance(spec, dict) else None
        if isinstance(spec_title, str):
            title = spec_title
        elif isinstance(spec_title, dict):
            title = spec_title.get("text") or "Chart"
        else:
            title = "Chart"

    cache[chart_id] = {
        "spec": spec,
        "filename": filename or "chart",
        "title": title,
    }

    # Reassign so state delta is recorded.
    ctx.state[_STATE_KEY] = cache

    logger.info(
        "chart_cache: stored chart_id=%s (caller-supplied) title=%r (cache size now %d)",
        chart_id, title, len(cache),
    )


def get_cached_chart_spec(ctx, chart_id: str) -> Optional[dict]:
    """Retrieve a previously cached chart spec by id.

    Returns a dict with keys {spec, filename, title} or None if not found.
    """
    cache = _get_cache(ctx)
    entry = cache.get(chart_id)
    if entry is None:
        logger.warning(
            "chart_cache: miss for chart_id=%s (cache has %d entries: %s)",
            chart_id, len(cache), list(cache.keys())[:10],
        )
        return None
    return entry


__all__ = ["cache_chart_spec", "cache_chart_spec_with_id", "get_cached_chart_spec"]
