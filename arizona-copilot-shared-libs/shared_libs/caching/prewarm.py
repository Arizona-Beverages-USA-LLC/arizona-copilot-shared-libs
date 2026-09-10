"""
prewarm — create the Vertex context cache on the FIRST turn, not the second.

ADK's cache manager skips cache creation on the first turn of a session because
it has no previous token count to validate against — so the first (often the
most expensive, deep) query of every fresh session pays the full uncached
prompt (~$0.35 on a ~70 KB deep prompt). This module injects a fingerprint-only
`CacheMetadata` plus a synthetic cacheable-token count, which makes the manager
treat turn 1 as "a second turn whose fingerprint matched" and create the cache
immediately.

This is pure ADK mechanics — it operates on whatever `system_instruction` /
tools / contents the turn already carries, so it is completely independent of
an agent's prompt structure and drops into any agent unchanged.

Wiring (two styles):

  1. Drop-in callback — chain it into the agent's before-model callback:

        from shared_libs.caching import build_prewarm_before_callback
        prewarm_cb = build_prewarm_before_callback(
            gate=lambda ctx, req: ctx.agent_name.endswith("_deep"),  # optional
        )
        # call prewarm_cb(callback_context, llm_request) inside the chain

  2. Inline — for agents that already compute tier/provider locally:

        from shared_libs.caching import should_prewarm, inject_prewarm_metadata
        if should_prewarm(llm_request) and tier == "DEEP" and not use_claude:
            inject_prewarm_metadata(llm_request)

Enable/disable with CACHE_PRE_WARM_ENABLED (default "1"). Everything here is
best-effort: any failure is swallowed so a turn is never broken by warming.
"""

from __future__ import annotations

import hashlib
import logging
import os
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# Synthetic token count injected so ADK's manager believes the (as-yet
# uncounted) prefix is worth caching. Must clear CONTEXT_CACHE_MIN_TOKENS
# (default 4096); 50k comfortably does for the large data-agent prompts.
_DEFAULT_SYNTHETIC_TOKEN_COUNT = 50_000

PrewarmGate = Callable[[Any, Any], bool]


def prewarm_enabled(enabled_env: str = "CACHE_PRE_WARM_ENABLED") -> bool:
    """True unless the enable env var is explicitly set to something != '1'."""
    return os.environ.get(enabled_env, "1") == "1"


def should_prewarm(llm_request: Any) -> bool:
    """The ADK-level preconditions for warming this turn.

    True only when context caching is actually configured for the request and
    no cache has been created/attached yet — i.e. this really is the cold first
    turn. Agent-specific gating (tier, provider, block-turns) is the caller's
    job; layer it on top of this.
    """
    if getattr(llm_request, "cache_config", None) is None:
        return False
    cm = getattr(llm_request, "cache_metadata", None)
    if cm is not None and getattr(cm, "cache_name", None) is not None:
        return False
    return True


def inject_prewarm_metadata(
    llm_request: Any,
    *,
    synthetic_token_count: int = _DEFAULT_SYNTHETIC_TOKEN_COUNT,
) -> str:
    """Inject synthetic cache metadata so ADK creates the cache on turn 1.

    Computes a fingerprint over the cacheable surface (system_instruction,
    tools, tool_config, and any leading non-final user contents) exactly as
    ADK's own cache manager would, sets `llm_request.cache_metadata` to a
    fingerprint-only CacheMetadata, and seeds `cacheable_contents_token_count`
    if it is unset. Returns the fingerprint (for logging).
    """
    from google.adk.models.cache_metadata import CacheMetadata
    from google.genai import types

    contents = llm_request.contents or []

    # The cacheable prefix is everything up to the trailing run of user turns
    # (the current question). Walk back over trailing user messages.
    last_user_start = len(contents)
    for i in range(len(contents) - 1, -1, -1):
        if contents[i].role == "user":
            last_user_start = i
        else:
            break
    contents_count = last_user_start

    fp_data: dict = {}
    cfg = getattr(llm_request, "config", None)
    if cfg and getattr(cfg, "system_instruction", None):
        fp_data["system_instruction"] = cfg.system_instruction
    if cfg and getattr(cfg, "tools", None):
        tools_data = []
        for tool in cfg.tools:
            if isinstance(tool, types.Tool):
                tools_data.append(tool.model_dump())
        fp_data["tools"] = tools_data
    if cfg and getattr(cfg, "tool_config", None):
        fp_data["tool_config"] = cfg.tool_config.model_dump()
    if contents_count > 0 and contents:
        for i in range(min(contents_count, len(contents))):
            fp_data.setdefault("cached_contents", []).append(
                contents[i].model_dump()
            )
    fingerprint = hashlib.sha256(str(fp_data).encode()).hexdigest()[:16]

    llm_request.cache_metadata = CacheMetadata(
        fingerprint=fingerprint,
        contents_count=contents_count,
    )
    if getattr(llm_request, "cacheable_contents_token_count", None) is None:
        llm_request.cacheable_contents_token_count = synthetic_token_count

    logger.info(
        "[caching] pre-warm: injected cache metadata (fp=%s, contents_count=%d)",
        fingerprint, contents_count,
    )
    return fingerprint


def build_prewarm_before_callback(
    *,
    gate: Optional[PrewarmGate] = None,
    enabled_env: str = "CACHE_PRE_WARM_ENABLED",
    synthetic_token_count: int = _DEFAULT_SYNTHETIC_TOKEN_COUNT,
):
    """Build a before-model callback that pre-warms the cache on the cold turn.

    gate(callback_context, llm_request) -> bool is an optional agent-specific
    predicate (e.g. only warm the deep tier, skip a non-cacheable provider).
    When omitted, every turn that satisfies should_prewarm() is warmed.

    Returns a callable with the ADK before_model_callback signature; it always
    returns None (it mutates llm_request in place, never replaces the response)
    and never raises — chain it alongside the agent's other before callbacks.
    """
    def _prewarm_before_model_callback(callback_context: Any, llm_request: Any):
        try:
            if not prewarm_enabled(enabled_env):
                return None
            if not should_prewarm(llm_request):
                return None
            if gate is not None and not gate(callback_context, llm_request):
                return None
            inject_prewarm_metadata(
                llm_request, synthetic_token_count=synthetic_token_count
            )
        except Exception:  # noqa: BLE001 — warming must never break a turn
            logger.warning("[caching] pre-warm injection failed (non-fatal)",
                           exc_info=True)
        return None

    return _prewarm_before_model_callback


__all__ = [
    "prewarm_enabled",
    "should_prewarm",
    "inject_prewarm_metadata",
    "build_prewarm_before_callback",
    "PrewarmGate",
]
