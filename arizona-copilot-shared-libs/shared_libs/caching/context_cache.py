"""
context_cache — the ADK App wrapper + ContextCacheConfig, one env-driven copy.

Every Arizona data agent had a near-byte-identical `shared/app_builder.py` that
read the same `CONTEXT_CACHE_*` env vars and built the same
`ContextCacheConfig`. This module is the single canonical implementation; the
only per-agent difference (the App name) is a plain argument.

Why an App wrapper at all
-------------------------
Two callers need the SAME App with the SAME cache config or production and
local drift apart:
  1. deploy.py — packages the App for Vertex AI Agent Engine.
  2. agent.py  — exports `root_app`, which `adk web` / `adk run` (ADK 1.4+)
                 discover and use directly instead of wrapping `root_agent`
                 in a bare default App.
If they build the App independently, Agent Engine gets ContextCacheConfig and
local `adk web` doesn't — so local turn-times stop resembling production
because every LLM call re-ingests the full (~70 KB) prompt on a cold cache.

Env vars (all optional; defaults match the historical per-agent copies):
    CONTEXT_CACHE_ENABLED       "1" = on (default), "0" = off
    CONTEXT_CACHE_MIN_TOKENS    default 4096  (prefix must reach this to cache)
    CONTEXT_CACHE_TTL_SECONDS   default 1800  (cache lifetime)
    CONTEXT_CACHE_INTERVALS     default 10    (recreate every N invocations)

Kept import-time-cheap: agent.py imports this at module load, so the
`google.adk` imports are deferred until the functions are actually called.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:  # for type hints only — never imported at module load
    from google.adk.apps import App
    from google.adk.agents.context_cache_config import ContextCacheConfig

logger = logging.getLogger(__name__)


def context_cache_enabled() -> bool:
    """True unless CONTEXT_CACHE_ENABLED is explicitly '0'."""
    return os.environ.get("CONTEXT_CACHE_ENABLED", "1") == "1"


def build_context_cache_config() -> "Optional[ContextCacheConfig]":
    """Build a ContextCacheConfig from the CONTEXT_CACHE_* env vars.

    Returns None when caching is disabled (CONTEXT_CACHE_ENABLED=0), so callers
    can pass the result straight through to `App(context_cache_config=...)`.
    """
    if not context_cache_enabled():
        logger.info("[caching] Context caching: DISABLED via env")
        return None

    # Lazy import — only needed when caching is on.
    from google.adk.agents.context_cache_config import ContextCacheConfig

    min_tokens = int(os.environ.get("CONTEXT_CACHE_MIN_TOKENS", "4096"))
    ttl_seconds = int(os.environ.get("CONTEXT_CACHE_TTL_SECONDS", "1800"))
    cache_intervals = int(os.environ.get("CONTEXT_CACHE_INTERVALS", "10"))

    logger.info(
        "[caching] Context caching: ENABLED  min_tokens=%d  ttl=%ds  "
        "refresh_every=%d invocations",
        min_tokens, ttl_seconds, cache_intervals,
    )
    return ContextCacheConfig(
        min_tokens=min_tokens,
        ttl_seconds=ttl_seconds,
        cache_intervals=cache_intervals,
    )


def build_app(root_agent: Any, *, app_name: str) -> "App":
    """Wrap `root_agent` in an ADK App, attaching ContextCacheConfig if enabled.

    `app_name` is the one per-agent value (e.g. "sales_copilot", "vip_copilot").
    Keep it stable and matching what deploy.py uses, so the local and deployed
    App — and therefore the cache — are keyed identically.
    """
    from google.adk.apps import App

    cache_cfg = build_context_cache_config()
    if cache_cfg is None:
        return App(root_agent=root_agent, name=app_name)
    return App(root_agent=root_agent, name=app_name, context_cache_config=cache_cfg)


__all__ = [
    "context_cache_enabled",
    "build_context_cache_config",
    "build_app",
]
