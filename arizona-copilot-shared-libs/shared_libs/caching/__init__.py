"""
caching — ADK context-cache config + turn-1 cache pre-warming for Arizona
Beverages copilots.

CANONICAL SOURCE OF TRUTH. Maintained in the org's
`arizona-copilot-shared-libs` repo and **vendored** (copied) into each agent
repo at `<agent_repo>/shared_libs/caching/`. Do not edit the vendored copies
directly — change it there, then re-sync (see VENDORING.md).

WHAT IT DOES
============
Two independent pieces every data agent wants:

  1. context_cache — the ADK `App` wrapper + `ContextCacheConfig`, built from
     the `CONTEXT_CACHE_*` env vars. Replaces the near-identical per-agent
     `shared/app_builder.py` copies with one canonical, env-driven builder;
     the only per-agent value (the App name) is a plain argument.

  2. prewarm — creates the Vertex context cache on the FIRST turn instead of
     the second, by injecting synthetic `CacheMetadata`. ADK normally skips
     cache creation on turn 1, so the first (often deep) query of every fresh
     session pays the full uncached prompt (~$0.35 on a ~70 KB prompt). This is
     pure ADK mechanics — independent of an agent's prompt structure, so it
     drops into any agent unchanged.

WHY BOTH LIVE HERE
==================
The two are complementary: context_cache turns caching ON and keeps local ==
prod; prewarm makes the very first turn benefit instead of eating the
cold-start. Neither imports anything agent-specific.

HOW TO WIRE IT INTO AN AGENT
============================
App wrapper (replaces the per-agent app_builder.build_app):

    from shared_libs.caching import build_app
    root_app = build_app(root_agent, app_name="my_copilot")   # keep name stable

Pre-warm — chain the callback into the agent's before-model callback:

    from shared_libs.caching import build_prewarm_before_callback
    prewarm_cb = build_prewarm_before_callback(
        # optional agent-specific gate; omit to warm every cold turn:
        gate=lambda ctx, req: (ctx.agent_name or "").endswith("_deep"),
    )
    # inside the agent's chained before_model_callback:
    prewarm_cb(callback_context, llm_request)

…or inline, for agents that already compute tier/provider locally:

    from shared_libs.caching import should_prewarm, inject_prewarm_metadata
    if should_prewarm(llm_request) and tier == "DEEP" and not use_claude:
        inject_prewarm_metadata(llm_request)

MAXIMIZING THE BENEFIT (prompt-side, not this library)
======================================================
Caching value scales with cache HIT RATE, which the agent's prompt controls:
  * Keep all volatile content (the date, any dynamic value) at the TAIL of the
    system instruction — never in the head/body — so the prefix stays stable.
  * Minimize the number of distinct instruction shapes (cache prefixes): one
    per tier x render-mode; fold optional sections into the base where cheap.
  * Order layers by change-rate (static schema/KPI first, mode/report last) so
    the largest possible span is a stable, cacheable prefix.
This library provides the mechanism; those conventions provide the hit rate.

ENABLING / TUNING (env vars)
============================
    CONTEXT_CACHE_ENABLED       "1" on (default), "0" off
    CONTEXT_CACHE_MIN_TOKENS    default 4096
    CONTEXT_CACHE_TTL_SECONDS   default 1800
    CONTEXT_CACHE_INTERVALS     default 10
    CACHE_PRE_WARM_ENABLED      "1" on (default), "0" off

RUNTIME DEPENDENCIES
====================
Only `google-adk` and `google-genai` (already required by any ADK agent). No
extra pip deps.
"""

from __future__ import annotations

from .context_cache import (
    context_cache_enabled,
    build_context_cache_config,
    build_app,
)
from .prewarm import (
    prewarm_enabled,
    should_prewarm,
    inject_prewarm_metadata,
    build_prewarm_before_callback,
    PrewarmGate,
)

__all__ = [
    # context_cache
    "context_cache_enabled",
    "build_context_cache_config",
    "build_app",
    # prewarm
    "prewarm_enabled",
    "should_prewarm",
    "inject_prewarm_metadata",
    "build_prewarm_before_callback",
    "PrewarmGate",
]
