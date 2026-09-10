"""
Turn Telemetry — time, tokens, cache hits, and cost per turn
==========================================================================
Instruments every user-facing turn with:
  - **Elapsed time** from first LLM call to final response (wall clock)
  - **LLM time** (sum of time each LLM call took to generate)
  - **Tool time** (WALL-CLOCK window spanning all tool calls — not the
    sum of durations. ADK supports parallel tool dispatch, and summing
    durations would double-count concurrent calls. Per-tool breakdown
    below still shows summed duration per tool name, which is useful
    for seeing cumulative CPU time.)
  - **Other time** (elapsed − LLM − tool; orchestration, network, callbacks)
  - **Tokens** per tier (triage / fast / deep), split input/output
  - **Cache hits**: cached_content_token_count when context caching fires
  - **Cost** per tier: input + output + cache-discounted estimate
  - **Per-tool call counts** (Tableau vs dashboard builder vs export, etc.)

Output format (appended as a muted footer to the final response):

    ---
    ⏱ 45.2s total  ·  🧠 LLM 18.3s (3 calls)  ·  🔧 tools 24.1s (4 calls)  ·  ⚙ other 2.8s
    🧮 52,610 tokens  ·  💾 cached 48,200  ·  💰 $0.021 (save ~$0.094 vs no cache)
       deep (gemini-3.1-pro-preview)
         in 51,847   out 666   cached 48,200   ·  total $0.0210   (3 calls)
       🔧 tools
         query-datasource        20.4s  (3 calls)
         build_segment_dashboard  3.1s  (1 call)
         read-metadata            0.6s  (1 call)

WIRING
======
This is a shared, agent-agnostic library (vendored at shared_libs/telemetry/).
It exposes four callbacks plus one configuration hook:
  - `telemetry_before_model_callback`  — fires before each LLM call
  - `telemetry_after_model_callback`   — fires after each LLM call
  - `telemetry_before_tool_callback`   — fires before each tool call
  - `telemetry_after_tool_callback`    — fires after each tool call
  - `configure_model_tiers(...)`       — tell telemetry which model each of
    the triage/fast/deep tiers uses, so token usage buckets correctly.

Chain the model-side callbacks around whatever the agent already runs in its
before/after-model callbacks; register the tool-side callbacks directly as the
agent's `before_tool_callback` / `after_tool_callback`.

    from shared_libs.telemetry import (
        configure_model_tiers,
        telemetry_before_model_callback, telemetry_after_model_callback,
        telemetry_before_tool_callback, telemetry_after_tool_callback,
    )
    # once at import, pass your agent's real tier models (optional but exact):
    configure_model_tiers(TRIAGE_MODEL, FAST_MODEL, DEEP_MODEL)

Tier resolution: a call is bucketed by ADK agent name first (a "<agent>_fast" /
"<agent>_deep" sub-agent maps to that tier automatically; register other names
via configure_model_tiers(agent_name_tiers=...)), then by matching the call's
model against the configured tier models. This library imports nothing agent-
specific — the tier model names default to env vars (TRIAGE_MODEL / FAST_MODEL /
DEEP_MODEL, with AGENT_MODEL fallback) until configure_model_tiers() overrides.

ENABLING
========
Set SHOW_TELEMETRY_FOOTER=1 in .env to show the footer in responses.
Numbers are always logged to Cloud Logging under [TELEMETRY] prefixes.

PRICING
=======
Vertex AI on-demand rates captured 2026-04-20. Re-verify at
https://cloud.google.com/vertex-ai/generative-ai/pricing if stale.

CACHE DISCOUNT MATH
===================
When Vertex reports `cached_content_token_count` in the response, those
tokens were served from an explicit cache at ~10% of standard input
price. The footer computes:

    effective_input_cost = (in_tokens − cached_tokens) × normal_rate
                         + cached_tokens × normal_rate × 0.10

And shows the savings vs no-cache as:
    savings = cached_tokens × normal_rate × 0.90

When cached_content_token_count is absent or zero, the displayed numbers
are identical to pre-cache behavior (no math changes).
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, Optional

from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types


logger = logging.getLogger(__name__)

print("[TELEMETRY] module imported", flush=True)


# ---------------------------------------------------------------------------
# Model-tier configuration (decoupled — this is a shared, agent-agnostic lib)
# ---------------------------------------------------------------------------
# Telemetry buckets token usage into three tiers (triage / fast / deep). To do
# that it needs the model-name string each tier resolves to, so it can match a
# call's model back to its tier. This library must NOT import any agent's
# model_config, so the tier names come from env vars (with documented Gemini
# defaults) and may be overridden EXACTLY by the agent via configure_model_tiers()
# at import time — e.g. passing its own model_config's TRIAGE/FAST/DEEP values.
#
#   from shared_libs.telemetry import configure_model_tiers
#   from .shared.model_config import TRIAGE_MODEL, FAST_MODEL, DEEP_MODEL
#   configure_model_tiers(TRIAGE_MODEL, FAST_MODEL, DEEP_MODEL)
#
# Backward-compat: if AGENT_MODEL is set (old single-model mode) and a tiered
# var is not, AGENT_MODEL is the fallback — mirroring model_config's resolution.
_DEFAULT_TRIAGE_MODEL = "gemini-3.1-flash-lite"
_DEFAULT_FAST_MODEL = "gemini-3.6-flash"
_DEFAULT_DEEP_MODEL = "gemini-3.7-flash"

_AGENT_MODEL_FALLBACK = os.environ.get("AGENT_MODEL")

_TIER_MODELS: dict[str, str] = {
    "triage": os.environ.get(
        "TRIAGE_MODEL", _AGENT_MODEL_FALLBACK or _DEFAULT_TRIAGE_MODEL),
    "fast": os.environ.get(
        "FAST_MODEL", _AGENT_MODEL_FALLBACK or _DEFAULT_FAST_MODEL),
    "deep": os.environ.get(
        "DEEP_MODEL", _AGENT_MODEL_FALLBACK or _DEFAULT_DEEP_MODEL),
}


def configure_model_tiers(
    triage: Optional[str] = None,
    fast: Optional[str] = None,
    deep: Optional[str] = None,
    agent_name_tiers: Optional[dict[str, str]] = None,
) -> None:
    """Override the tier → model-name mapping used to bucket token usage.

    Call once at import time from the consuming agent so telemetry's tier
    resolution matches the agent's real model tiers exactly (instead of the
    env-var/default guess). Any argument left None keeps its current value.

    agent_name_tiers optionally extends the agent-name → tier map for agents
    whose sub-agent names don't follow the generic ``*_fast`` / ``*_deep``
    convention (e.g. ``{"my_router": "triage"}``).
    """
    if triage is not None:
        _TIER_MODELS["triage"] = triage
    if fast is not None:
        _TIER_MODELS["fast"] = fast
    if deep is not None:
        _TIER_MODELS["deep"] = deep
    if agent_name_tiers:
        _AGENT_NAME_TO_TIER.update(agent_name_tiers)
    logger.info(
        "Telemetry model tiers configured: triage=%s fast=%s deep=%s",
        _TIER_MODELS["triage"], _TIER_MODELS["fast"], _TIER_MODELS["deep"],
    )


# ---------------------------------------------------------------------------
# Optional BigQuery ledger hook (decoupled — agents that query BQ can enrich
# the per-tool footer with query economics: queries, bytes billed, cost).
# ---------------------------------------------------------------------------
# This library must not import any agent's BigQuery client, so an agent that
# tracks BQ usage registers two callables here at import time:
#
#   from shared_libs.telemetry import configure_bq_ledger
#   from .tools.bigquery.client import reset_drained, last_drained_ledger
#   configure_bq_ledger(reset=reset_drained, last_drained=last_drained_ledger)
#
# `reset()` is called at each tool start to zero the accumulator; the object
# returned by `last_drained()` is duck-typed for .total_queries,
# .total_bytes_billed_mb/_gb, .total_cost_usd, .cached_queries,
# .total_elapsed_s, .total_savings_usd, .summary(), .footer(). Agents that
# don't query BigQuery simply never call this, and the BQ line is omitted.
_BQ_RESET_FN: Optional[Any] = None
_BQ_LAST_DRAINED_FN: Optional[Any] = None


def configure_bq_ledger(reset: Optional[Any] = None,
                        last_drained: Optional[Any] = None) -> None:
    """Register the agent's BigQuery ledger callables (both optional).

    reset()         — called at tool start to reset the per-tool BQ accumulator.
    last_drained()  — called at tool end; returns the drained BQ ledger object
                      (or None) whose stats enrich the telemetry footer.
    """
    global _BQ_RESET_FN, _BQ_LAST_DRAINED_FN
    if reset is not None:
        _BQ_RESET_FN = reset
    if last_drained is not None:
        _BQ_LAST_DRAINED_FN = last_drained
    logger.info(
        "Telemetry BQ ledger hook configured (reset=%s last_drained=%s)",
        bool(_BQ_RESET_FN), bool(_BQ_LAST_DRAINED_FN),
    )


# ---------------------------------------------------------------------------
# Pricing — per 1 million tokens, Vertex AI on-demand.
# Captured 2026-04-20; 3.6 / 3.5-lite rates added 2026-07-24.
#
# The cache discount math below assumes cached input bills at 10% of the
# standard input rate. That holds for the 3.x family (e.g. 3.6 Flash lists
# $1.50/1M input and $0.15/1M cached), so no per-model cache rate is needed.
# ---------------------------------------------------------------------------
PRICING: dict[str, dict[str, Any]] = {
    # gemini-3.7-flash: DEEP tier as of 2026-09-04. Introductory on-demand
    # pricing through 2026-12-31 ($0.75 in / $3.75 out per 1M); reverts to
    # $1.50 / $7.50 on 2027-01-01 -- bump these two figures then. Cached input
    # bills ~10% of input (~$0.07/1M), consistent with the family cache math.
    "gemini-3.7-flash": {
        "input_per_1m":             0.75,
        "output_per_1m":            3.75,
        "long_context_threshold":   None,
    },
    "gemini-3.6-flash": {
        "input_per_1m":             1.50,
        "output_per_1m":            7.50,
        "long_context_threshold":   None,
    },
    "gemini-3.5-flash-lite": {
        "input_per_1m":             0.30,
        "output_per_1m":            2.50,
        "long_context_threshold":   None,
    },
    "gemini-3.5-flash": {
        "input_per_1m":             1.50,
        "output_per_1m":            9.00,
        "long_context_threshold":   None,
    },
    "gemini-3-flash-preview": {
        "input_per_1m":             0.50,
        "output_per_1m":            3.00,
        "long_context_threshold":   None,
    },
    "gemini-3.1-pro-preview": {
        "input_per_1m":             2.00,
        "output_per_1m":           12.00,
        "input_per_1m_long":        4.00,
        "output_per_1m_long":      18.00,
        "long_context_threshold": 200_000,
    },
    "gemini-2.5-pro": {
        "input_per_1m":             1.25,
        "output_per_1m":           10.00,
        "input_per_1m_long":        2.50,
        "output_per_1m_long":      15.00,
        "long_context_threshold": 200_000,
    },
    "gemini-2.5-flash": {
        "input_per_1m":             0.15,
        "output_per_1m":            0.60,
        "long_context_threshold":   None,
    },
    "gemini-2.5-flash-lite": {
        "input_per_1m":             0.10,
        "output_per_1m":            0.40,
        "long_context_threshold":   None,
    },
    "gemini-3.1-flash-lite-preview": {
        "input_per_1m":             0.10,
        "output_per_1m":            0.40,
        "long_context_threshold":   None,
    },
    "gemini-3-pro-preview": {
        "deprecated":       True,
        "deprecated_date":  "2026-03-09",
        "replacement":      "gemini-3.1-pro-preview",
    },
    # -- Anthropic Claude on Vertex AI (provider mux; captured 2026-08-22).
    # Vertex mirrors Anthropic API rates. Cache-hit = 10% of input, the SAME
    # factor as Gemini (_CACHE_DISCOUNT_FACTOR) -- one discount fits both.
    # Fable/Opus adaptive thinking bills thinking tokens AS OUTPUT tokens,
    # so output counts (and costs) already include reasoning depth.
    "claude-opus-4-8": {
        "input_per_1m":             5.00,
        "output_per_1m":           25.00,
        "long_context_threshold":   None,
    },
    "claude-opus-5": {
        "input_per_1m":             5.00,
        "output_per_1m":           25.00,
        "long_context_threshold":   None,
    },
    "claude-fable-5": {
        "input_per_1m":            10.00,
        "output_per_1m":           50.00,
        "long_context_threshold":   None,
    },
    "claude-sonnet-5": {
        # $2/$10 through 2026-08-31; rises to $3/$15 on 2026-09-01.
        "input_per_1m":             2.00,
        "output_per_1m":           10.00,
        "long_context_threshold":   None,
    },
}

# Fraction of normal input rate charged for cached tokens on Gemini 2.5+.
# Google announced a 90% discount (i.e., pay 10% of standard rate).
_CACHE_DISCOUNT_FACTOR = 0.10


# ---------------------------------------------------------------------------
# Session state keys
# ---------------------------------------------------------------------------
_TURN_USAGE_KEY = "telemetry_turn_usage"
_LAST_MODEL_KEY = "telemetry_last_model"
_LAST_THINKING_KEY = "telemetry_last_thinking"   # per-call thinking descriptor
_LAST_AGENT_NAME_KEY = "telemetry_last_agent_name"
_LAST_CALL_START_KEY = "telemetry_last_call_start"   # per-call LLM start time
_TOOL_STARTS_KEY = "telemetry_tool_starts"            # dict: call_id -> start
_LAST_RECORDED_FINGERPRINT_KEY = "telemetry_last_recorded_fingerprint"
_LAST_MACHINE_COMMENT_KEY = "telemetry_last_machine_comment"
# ^ fingerprint of the most-recently-recorded LLM call, used to dedupe:
# ADK re-invokes the after-model-callback chain when a callback returns a
# MODIFIED response (which the machine-comment append below now does on
# every final call). Without dedupe, token entries append twice.
# Fingerprint = (call_start_time, model_name, prompt_tokens, cand_tokens).

# ---------------------------------------------------------------------------
# Footer marker for stripping prior-turn footers from conversation history
# ---------------------------------------------------------------------------
# The after-model-callback appends a telemetry footer to the final assistant
# response as a separate Part(text=...) with a fixed leading marker. When
# that response goes back into conversation history and the model regenerates
# a follow-up turn (e.g., "same query asked twice in a row"), Flash sometimes
# copies the footer text into its NEW response body — because to the model,
# it's just text. The next turn then accumulates a SECOND footer on top, and
# you get visible duplicate footers in chat.
#
# Fix: on the way INTO the model (before_model_callback), strip any prior
# footer text from the conversation history we send to the model. The footer
# stays in the rendered chat for the user to see; the model just doesn't see
# it on subsequent turns and therefore can't echo it.
#
# The marker pattern matches everything from "\n\n---\n⏱ " through end of
# string (the footer is always at the end of a part's text, and once we hit
# the marker we know nothing useful follows). Using `⏱` (clock emoji) as part
# of the anchor makes false positives extremely unlikely — no normal user or
# model output starts with "---\n⏱ ".
_FOOTER_STRIP_RE = re.compile(r"\n\n---\n⏱[\s\S]*\Z")

# Machine-readable telemetry comment (the cross-A2A wire contract tea-talk
# parses). Appended to EVERY final response by _build_machine_comment below.
# Must be stripped from model-visible history for the same echo reason as
# the human footer above.
_MACHINE_STRIP_RE = re.compile(r"\n*<!--agent_telemetry\b[\s\S]*?-->\s*")

# ---------------------------------------------------------------------------
# Agent-name → tier mapping
# ---------------------------------------------------------------------------
# When FAST_MODEL and TRIAGE_MODEL are the same model (e.g., both Flash),
# the model name alone can't distinguish which tier a call belongs to.
# The agent name, which the callback_context exposes, is the reliable
# signal. This mapping takes precedence over model-based tier detection.
#
# Naming convention:
#   - Root triage agent is named "sales_copilot" (no suffix)
#   - Fast sub-agent is named "sales_copilot_fast"
#   - Deep sub-agent is named "sales_copilot_deep"
#
# If an agent name doesn't match any of these, we fall back to model-
# based tier detection.
_AGENT_NAME_TO_TIER: dict[str, str] = {
    "sales_copilot":      "triage",
    "sales_copilot_fast": "fast",
    "sales_copilot_deep": "deep",
    # VIP copilot agents (for cross-project compatibility if telemetry
    # module is ported):
    "vip_copilot":      "triage",
    "vip_copilot_fast": "fast",
    "vip_copilot_deep": "deep",
}


# ===========================================================================
# Model name helpers (unchanged from prior version)
# ===========================================================================

def _agent_name_to_tier(agent_name: Optional[str]) -> Optional[str]:
    """Resolve tier from the ADK agent name. Returns None if unknown.

    This is the PRIMARY tier signal — when FAST_MODEL and TRIAGE_MODEL
    share a model, model-based detection can't tell them apart but the
    agent name still can.
    """
    if not agent_name:
        return None
    explicit = _AGENT_NAME_TO_TIER.get(agent_name)
    if explicit is not None:
        return explicit
    # Generic convention so any agent works without registering names:
    # a "<agent>_fast" / "<agent>_deep" sub-agent maps to that tier. A bare
    # root name (no suffix) is left to model-name matching so we never
    # mis-bucket an unrecognized root as triage.
    lowered = agent_name.lower()
    if lowered.endswith("_fast"):
        return "fast"
    if lowered.endswith("_deep"):
        return "deep"
    return None


def _model_to_tier(model_name: str, agent_name: Optional[str] = None) -> str:
    """Which tier this call belongs to.

    Resolution order:
      1. Agent name (if recognizable) — wins every time.
      2. Model name match against TRIAGE_MODEL / FAST_MODEL / DEEP_MODEL
         — fallback for unknown agent names.
      3. Return the normalized model name as its own "tier" — last resort.

    Agent-name-based resolution is critical when two tiers share the
    same model (e.g., TRIAGE_MODEL == FAST_MODEL == gemini-3-flash-preview).
    Without it, every call would get bucketed under whichever tier's
    env var happens to be checked first below.
    """
    # 1. Try agent name first — most reliable.
    agent_tier = _agent_name_to_tier(agent_name)
    if agent_tier is not None:
        return agent_tier

    # 2. Fall back to model-name matching. Tier model names come from
    # _TIER_MODELS (env/default, or configure_model_tiers()) — no agent import.
    normalized = _normalize_model_name(model_name)
    if _names_match(normalized, _TIER_MODELS["triage"]):
        return "triage"
    if _names_match(normalized, _TIER_MODELS["fast"]):
        return "fast"
    if _names_match(normalized, _TIER_MODELS["deep"]):
        return "deep"
    return normalized or "unknown"


def _normalize_model_name(model_name: str) -> str:
    if not model_name:
        return ""
    import re
    name = model_name.strip()
    # Provider-mux encoding: 'claude/<vertex-id>/<effort>' -> '<vertex-id>'.
    # Tolerate a missing effort segment ('claude/<vertex-id>') too, so the
    # price lookup still resolves.
    if name.startswith("claude/"):
        parts = name.split("/")
        if len(parts) >= 2 and parts[1]:
            name = parts[1]
    for prefix in ("publishers/google/models/",
                   "publishers/anthropic/models/", "models/",
                   "vertex_ai/"):
        if name.startswith(prefix):
            name = name[len(prefix):]
    # Gemini dated/versioned aliases -> base id.
    m = re.match(r"^(gemini-[a-z0-9.\-]+?)(-\d{2}-\d{2}|-\d{6,})$", name)
    if m:
        name = m.group(1)
    # Claude Vertex ids carry a version suffix ('claude-opus-5@20260724' or
    # 'claude-opus-5-20260724') that must be stripped so pricing resolves to
    # the base row (e.g. 'claude-opus-5'). Strip ONLY an @-suffix or a DATE-
    # like (>=6-digit) tail — never a short model-version segment like the
    # '-8' in 'claude-opus-4-8', which is itself a PRICING key.
    if name.startswith("claude-"):
        name = name.split("@", 1)[0]
        name = re.sub(r"-\d{6,}$", "", name)
    return name


def _strip_footers_from_history(llm_request: LlmRequest) -> int:
    """Strip telemetry footers from prior assistant turns in conversation history.

    Walks `llm_request.contents`, finds any model-role Content, and on each
    of its Parts:
      - If the Part has text starting with the footer marker ("\\n\\n---\\n⏱"),
        the entire Part is dropped (it's a pure footer Part, our intended shape).
      - If the Part has text that CONTAINS the footer marker mid-string, the
        marker and everything after it is stripped. This handles the case
        where Flash copied a prior footer into its main response body.

    Returns the number of Parts modified (for logging). Mutates contents in place.

    This runs on every before_model_callback invocation. It is O(n) over the
    parts of the conversation, with a fast bail-out when the marker isn't
    present in a part's text — typical case is a no-op pass-through.
    """
    contents = getattr(llm_request, "contents", None)
    if not contents:
        return 0

    modified = 0

    for content in contents:
        if getattr(content, "role", None) != "model":
            continue
        parts = getattr(content, "parts", None)
        if not parts:
            continue

        # Walk parts in reverse so we can safely delete pure-footer Parts
        # without disturbing earlier indices.
        new_parts = []
        for part in parts:
            text = getattr(part, "text", None)
            if not isinstance(text, str) or not text:
                # Non-text parts (function_call, inlineData, etc.) pass through.
                new_parts.append(part)
                continue

            # Pure-footer Part: starts with the marker. Drop it entirely.
            if text.startswith("\n\n---\n⏱") or text.startswith("---\n⏱"):
                modified += 1
                continue

            # Pure machine-telemetry Part (<!--agent_telemetry ...-->).
            # Drop it from model-visible history entirely.
            if text.lstrip().startswith("<!--agent_telemetry"):
                modified += 1
                continue

            # Mixed Part: main text followed by a footer. Strip from marker on.
            if "\n\n---\n⏱" in text:
                stripped = _FOOTER_STRIP_RE.sub("", text)
                if stripped != text:
                    # Build a new Part rather than mutating the existing one
                    # to avoid issues if Part is a frozen pydantic model.
                    new_parts.append(types.Part(text=stripped))
                    modified += 1
                    continue

            # Mixed Part carrying a machine-telemetry comment: strip it.
            if "<!--agent_telemetry" in text:
                stripped = _MACHINE_STRIP_RE.sub("", text)
                if stripped != text:
                    new_parts.append(types.Part(text=stripped))
                    modified += 1
                    continue

            new_parts.append(part)

        if modified > 0:
            # Reassign the parts list. Pydantic v2 Content allows direct
            # attribute set; if not, we fall through silently.
            try:
                content.parts = new_parts
            except Exception:  # noqa: BLE001
                pass

    return modified


def _names_match(a: str, b: str) -> bool:
    return _normalize_model_name(a) == _normalize_model_name(b)


def _is_footer_enabled() -> bool:
    return os.environ.get("SHOW_TELEMETRY_FOOTER", "0") == "1"


# ===========================================================================
# Cost calculation — now cache-aware
# ===========================================================================

def compute_cost(
    model_name: str,
    input_tokens: int,
    output_tokens: int,
    cached_tokens: int = 0,
    cache_write_tokens: int = 0,
    thinking_tokens: int = 0,
) -> tuple[Optional[float], Optional[float], bool, Optional[float]]:
    """Return (input_cost, output_cost, is_long, savings_vs_no_cache).

    `input_tokens` is the TOTAL input tokens (cached + uncached) as
    reported by Vertex's prompt_token_count. `cached_tokens` is the
    portion that hit the explicit cache (cached_content_token_count).
    `thinking_tokens` is Vertex's thoughts_token_count — billed at the
    output rate but reported SEPARATELY from candidates_token_count.

    Cost math:
        uncached_portion  = input_tokens − cached_tokens
        input_cost        = uncached_portion × rate
                          + cached_tokens × rate × _CACHE_DISCOUNT_FACTOR
        output_cost       = (output_tokens + thinking_tokens) × output_rate
        savings           = cached_tokens × rate × (1 − _CACHE_DISCOUNT_FACTOR)

    savings_vs_no_cache is None when cached_tokens is 0 (nothing to save).
    All three cost values are None if the model isn't in PRICING.
    """
    normalized = _normalize_model_name(model_name)
    pricing = PRICING.get(normalized)

    if pricing is None or pricing.get("deprecated"):
        return (None, None, False, None)

    threshold = pricing.get("long_context_threshold")
    is_long = bool(threshold is not None and input_tokens > threshold)

    if is_long:
        input_rate = pricing["input_per_1m_long"]
        output_rate = pricing["output_per_1m_long"]
    else:
        input_rate = pricing["input_per_1m"]
        output_rate = pricing["output_per_1m"]

    # Clamp cached_tokens to the valid range [0, input_tokens]. Vertex has
    # been known to occasionally report cached > prompt on edge cases
    # (partial cache hits near threshold boundaries); clamping keeps the
    # math consistent.
    cached_tokens = max(0, min(cached_tokens, input_tokens))
    uncached_tokens = input_tokens - cached_tokens

    uncached_input_cost = (uncached_tokens / 1_000_000.0) * input_rate
    cached_input_cost = (cached_tokens / 1_000_000.0) * input_rate * _CACHE_DISCOUNT_FACTOR
    input_cost = uncached_input_cost + cached_input_cost
    if cache_write_tokens:
        # Anthropic prompt-cache WRITES bill at 1.25x the input rate and
        # are reported separately from input_tokens (captured by the
        # provider mux's client shim; Gemini never sets this field).
        input_cost += (cache_write_tokens / 1_000_000.0) * input_rate * 1.25

    # Thinking tokens (Gemini 2.5+/3.x with thinking enabled) are billed
    # at the output rate but NOT included in candidates_token_count.
    billable_output = output_tokens + thinking_tokens
    output_cost = (billable_output / 1_000_000.0) * output_rate

    savings = None
    if cached_tokens > 0:
        savings = (
            (cached_tokens / 1_000_000.0)
            * input_rate
            * (1 - _CACHE_DISCOUNT_FACTOR)
        )

    return (input_cost, output_cost, is_long, savings)


# ===========================================================================
# Accumulator helpers
# ===========================================================================

def _get_turn_usage(state: Any) -> dict[str, Any]:
    """Return the per-turn usage accumulator, creating it if absent.

    Fields:
      start_time    — turn start (first before-model)
      calls         — list of per-LLM-call entries
      tool_calls    — list of per-tool-call entries
    """
    existing = None
    try:
        if hasattr(state, "get"):
            existing = state.get(_TURN_USAGE_KEY)
        elif _TURN_USAGE_KEY in state:
            existing = state[_TURN_USAGE_KEY]
    except Exception:  # noqa: BLE001
        existing = None

    if isinstance(existing, dict):
        # Backfill any keys added in newer versions.
        existing.setdefault("calls", [])
        existing.setdefault("tool_calls", [])
        existing.setdefault("start_time", None)
        return existing

    fresh = {"calls": [], "tool_calls": [], "start_time": None}
    try:
        state[_TURN_USAGE_KEY] = fresh
    except Exception:  # noqa: BLE001
        pass
    return fresh


def _extract_usage(llm_response: LlmResponse) -> Optional[dict[str, int]]:
    """Pull input/output/cached/thinking token counts from LlmResponse.

    Returns dict with keys: input_tokens, output_tokens, cached_tokens,
    thinking_tokens.  cached_tokens and thinking_tokens default to 0
    when not reported.

    Vertex reports thinking tokens (thoughts_token_count) SEPARATELY
    from candidates_token_count — they are NOT included in the
    candidates count, but ARE billed at the output rate.
    """
    um = getattr(llm_response, "usage_metadata", None)
    if um is None:
        return None

    input_tokens = getattr(um, "prompt_token_count", None) or 0
    output_tokens = getattr(um, "candidates_token_count", None) or 0
    cached_tokens = getattr(um, "cached_content_token_count", None) or 0
    thinking_tokens = getattr(um, "thoughts_token_count", None) or 0

    if input_tokens == 0 and output_tokens == 0 and cached_tokens == 0:
        return None

    return {
        "input_tokens": int(input_tokens),
        "output_tokens": int(output_tokens),
        "cached_tokens": int(cached_tokens),
        "thinking_tokens": int(thinking_tokens),
    }


def _extract_thinking_from_request(llm_request: LlmRequest) -> Optional[str]:
    """Human-readable thinking descriptor for the CURRENT call.

    Claude turns encode effort in the mux model string
    ('claude/<id>/<effort>' -> 'high'). Gemini turns carry a
    thinking_config with either thinking_level or thinking_budget.
    Returns None when nothing is configured."""
    model = getattr(llm_request, "model", "") or ""
    if isinstance(model, str) and model.startswith("claude/"):
        parts = model.split("/")
        if len(parts) == 3 and parts[2]:
            return parts[2].lower()
    config = getattr(llm_request, "config", None)
    tc = getattr(config, "thinking_config", None) if config else None
    if tc is None:
        return None
    level = getattr(tc, "thinking_level", None)
    if level:
        return str(level).split(".")[-1].lower()
    budget = getattr(tc, "thinking_budget", None)
    if budget is not None:
        if budget == 0:
            return "off"
        if budget < 0:
            return "adaptive"
        return f"budget={budget}"
    return None


def _extract_model_from_request(llm_request: LlmRequest) -> Optional[str]:
    if llm_request is None:
        return None
    for attr in ("model", "model_name"):
        val = getattr(llm_request, attr, None)
        if isinstance(val, str) and val:
            return val
    config = getattr(llm_request, "config", None)
    if config is not None:
        for attr in ("model", "model_name"):
            val = getattr(config, attr, None)
            if isinstance(val, str) and val:
                return val
    return None


def _state_get(state: Any, key: str) -> Any:
    try:
        if hasattr(state, "get"):
            return state.get(key)
        return state[key] if key in state else None
    except Exception:  # noqa: BLE001
        return None


def _state_set(state: Any, key: str, value: Any) -> None:
    try:
        state[key] = value
    except Exception:  # noqa: BLE001
        pass


# ===========================================================================
# BEFORE / AFTER MODEL callbacks
# ===========================================================================

def telemetry_before_model_callback(
    callback_context: CallbackContext,
    llm_request: LlmRequest,
) -> Optional[LlmResponse]:
    """Record start time of THIS LLM call. On first call of the turn,
    also record the overall turn start. Stash model name and agent
    name for the after-callback. Strip prior-turn telemetry footers
    from conversation history before they reach the model. Always
    returns None (never short-circuits)."""
    try:
        # Strip footers from prior assistant turns BEFORE anything else —
        # this prevents the model from echoing them into new responses.
        # See _strip_footers_from_history docstring + _FOOTER_STRIP_RE
        # comment for the failure mode this fixes.
        try:
            stripped = _strip_footers_from_history(llm_request)
            if stripped > 0:
                print(
                    f"[TELEMETRY] before-model: stripped footers from "
                    f"{stripped} prior assistant part(s)",
                    flush=True,
                )
        except Exception as exc:  # noqa: BLE001
            print(
                f"[TELEMETRY] footer-strip error (continuing): "
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )

        state = callback_context.state
        usage = _get_turn_usage(state)

        now = time.monotonic()

        # Turn start: only set once per turn.
        if usage.get("start_time") is None:
            usage["start_time"] = now
            usage["calls"] = []
            usage["tool_calls"] = []
        elif os.environ.get("TELEMETRY_CALL_TRACE", "1") == "1":
            # A turn that never reached footer finalization (early return on a
            # partial / empty / function-call response, or a before-model
            # short-circuit) leaves start_time set. The NEXT turn then inherits
            # it and `elapsed` silently includes the inter-turn idle gap --
            # the second candidate explanation for a large residual.
            _age = now - usage["start_time"]
            _ncalls = len(usage.get("calls") or [])
            if _ncalls == 0 and _age > 5.0:
                print(
                    f"[TELEMETRY_TRACE] STALE start_time: age={_age:.1f}s "
                    f"with 0 counted calls -- elapsed for this turn will "
                    f"include the previous turn's idle gap",
                    flush=True,
                )

        # Per-call start for LLM wall-time measurement (overwritten each call).
        _state_set(state, _LAST_CALL_START_KEY, now)

        # Model name for the after-callback
        model_name = _extract_model_from_request(llm_request)
        if model_name:
            _state_set(state, _LAST_MODEL_KEY, model_name)

        # Thinking descriptor (Claude effort / Gemini level or budget) --
        # displayed per tier in the footer and machine comment.
        _state_set(state, _LAST_THINKING_KEY,
                   _extract_thinking_from_request(llm_request))

        # Agent name — used for tier resolution when multiple tiers
        # share a model (e.g., TRIAGE and FAST both on Flash). Stashed
        # via state so the after-callback can read it. CallbackContext
        # exposes `agent_name` in current ADK versions.
        agent_name = getattr(callback_context, "agent_name", None)
        if agent_name:
            _state_set(state, _LAST_AGENT_NAME_KEY, agent_name)

        _state_set(state, _TURN_USAGE_KEY, usage)
    except Exception as exc:  # noqa: BLE001
        print(
            f"[TELEMETRY] before-model error (continuing): "
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )

    return None


def telemetry_after_model_callback(
    callback_context: CallbackContext,
    llm_response: LlmResponse,
) -> Optional[LlmResponse]:
    """Record this call's token usage + wall time. On final call of the
    turn, build and append the footer."""
    try:
        state = callback_context.state
        usage = _get_turn_usage(state)

        # 1. LLM call wall time for THIS call
        call_end = time.monotonic()
        call_start = _state_get(state, _LAST_CALL_START_KEY)
        call_duration = (call_end - call_start) if call_start else 0.0

        # ---- RESIDUAL DIAGNOSTIC (added 2026-08-26) -------------------
        # `other` in the footer is a pure residual:
        #     other = elapsed - sum(counted LLM calls) - tool wall-clock
        # An LlmResponse with no usage_metadata is DISCARDED below, which means
        # its entire wall time silently becomes residual. That is the leading
        # explanation for a Claude turn measured at 59.1s total / 10.6s LLM /
        # 0.8s tools / 47.7s "other": ADK's litellm adapter consumes
        # usage_metadata on the first yielded response, so when Claude emits
        # narration text AND a tool_use block the second response carries none.
        #
        # This line makes every invocation visible. If one appears with
        # usage=False and a multi-second dur, the residual is an uncounted
        # call, not framework overhead. Gate with TELEMETRY_CALL_TRACE=0 once
        # the question is settled.
        if os.environ.get("TELEMETRY_CALL_TRACE", "1") == "1":
            try:
                _um = getattr(llm_response, "usage_metadata", None)
                _fc = False
                _content = getattr(llm_response, "content", None)
                for _part in (getattr(_content, "parts", None) or []):
                    if getattr(_part, "function_call", None):
                        _fc = True
                        break
                print(
                    f"[TELEMETRY_TRACE] after-model dur={call_duration:.2f}s "
                    f"partial={getattr(llm_response, 'partial', None)} "
                    f"usage={_um is not None} "
                    f"fn_call={_fc} "
                    f"model={_get_stashed_model(state)!r}",
                    flush=True,
                )
            except Exception:  # noqa: BLE001
                pass
        # ---------------------------------------------------------------

        # 2. Token / cache extraction
        call_usage = _extract_usage(llm_response)
        if call_usage is not None:
            model_name = (
                _get_stashed_model(state)
                or _get_model_name_from_response(llm_response)
                or "unknown"
            )

            # Provider normalization for Claude turns:
            # (a) Anthropic's input_tokens EXCLUDES cache reads, while
            #     Gemini's prompt_token_count INCLUDES them -- fold reads
            #     back into input so uncached = in - cached and the 10%
            #     discount stay correct across providers.
            # (b) Cache WRITES (cache_creation_input_tokens) never reach
            #     usage_metadata at all -- the provider mux sniffs them
            #     off the wire; pop and attach for billing at 1.25x.
            if isinstance(model_name, str) and model_name.startswith("claude"):
                if call_usage.get("cached_tokens"):
                    call_usage["input_tokens"] = (
                        call_usage["input_tokens"]
                        + call_usage["cached_tokens"])
                try:
                    from .provider_mux import pop_last_cache_write_tokens
                    _cw = pop_last_cache_write_tokens()
                except Exception:  # noqa: BLE001
                    _cw = 0
                if _cw:
                    call_usage["cache_write_tokens"] = _cw

            # Capture agent name too — this is the primary tier signal
            # when FAST_MODEL == TRIAGE_MODEL. Prefer the live callback
            # context's agent_name (always current); fall back to the
            # value stashed in before-callback.
            agent_name = (
                getattr(callback_context, "agent_name", None)
                or _state_get(state, _LAST_AGENT_NAME_KEY)
            )

            # Dedupe: skip if we've already recorded a call with this exact
            # fingerprint. ADK re-invokes the after-model-callback chain on
            # a response a callback MODIFIED (the machine-comment append
            # below does exactly that on every final call); without this,
            # tokens and call counts double.
            fingerprint = (
                call_start,
                model_name,
                call_usage["input_tokens"],
                call_usage["output_tokens"],
            )
            last_fingerprint = _state_get(state, _LAST_RECORDED_FINGERPRINT_KEY)
            if last_fingerprint == fingerprint:
                print(
                    f"[TELEMETRY] dedupe: skipped duplicate after-callback "
                    f"for fingerprint=({call_start}, {model_name!r}, "
                    f"in={call_usage['input_tokens']}, "
                    f"out={call_usage['output_tokens']})",
                    flush=True,
                )
                # Fall through to the footer-finalization checks below --
                # the first invocation already recorded this entry.
            else:
                entry = {
                    "model": model_name,
                    "agent_name": agent_name,
                    "thinking": _state_get(state, _LAST_THINKING_KEY),
                    "input_tokens": call_usage["input_tokens"],
                    "output_tokens": call_usage["output_tokens"],
                    "cached_tokens": call_usage["cached_tokens"],
                    "thinking_tokens": call_usage.get(
                        "thinking_tokens", 0),
                    "cache_write_tokens": call_usage.get(
                        "cache_write_tokens", 0),
                    "duration_sec": call_duration,
                }
                usage.setdefault("calls", []).append(entry)
                _state_set(state, _TURN_USAGE_KEY, usage)
                _state_set(state, _LAST_RECORDED_FINGERPRINT_KEY, fingerprint)

                cache_str = (
                    f" cached={call_usage['cached_tokens']}"
                    if call_usage["cached_tokens"] else ""
                )
                think_str = (
                    f" thinking={call_usage.get('thinking_tokens', 0)}"
                    if call_usage.get("thinking_tokens") else ""
                )
                agent_str = f" agent={agent_name!r}" if agent_name else ""
                print(
                    f"[TELEMETRY] llm call: model={model_name!r}"
                    f"{agent_str} "
                    f"in={call_usage['input_tokens']} "
                    f"out={call_usage['output_tokens']}"
                    f"{think_str}"
                    f"{cache_str} "
                    f"dur={call_duration:.2f}s",
                    flush=True,
                )

        # 3. Is this the FINAL call of the turn?
        if llm_response.partial:
            return None
        if llm_response.error_code:
            _reset_turn(state)
            return None
        if not llm_response.content or not llm_response.content.parts:
            return None
        if _has_function_call_part(llm_response):
            # Intermediate tool-use turn — keep accumulating.
            return None

        # 4. Final call — build the footer
        start_time = usage.get("start_time")
        elapsed = (time.monotonic() - start_time) if start_time else 0.0
        calls = usage.get("calls", [])
        tool_calls = usage.get("tool_calls", [])

        if not calls:
            _reset_turn(state)
            return None

        # Phantom-invocation guard: the after-model-callback can be re-run
        # AFTER a genuine final call already built its footer and reset the
        # turn (ADK re-runs the chain on modified responses). Signature:
        # start_time is None (reset cleared it) with at most the phantom's
        # own entry in calls. Skip footer emission and re-reset.
        if start_time is None and len(calls) <= 1:
            print(
                f"[TELEMETRY] phantom-invocation guard: skipping footer "
                f"(start_time=None, calls={len(calls)}).",
                flush=True,
            )
            cached_comment = _state_get(state, _LAST_MACHINE_COMMENT_KEY)
            _reset_turn(state)
            # Returning None tells ADK to keep the ORIGINAL (un-appended)
            # response -- which is how the machine comment was silently lost
            # on the A2A path. Return a response explicitly instead.
            if _response_has_machine_comment(llm_response):
                print(
                    "[TELEMETRY] phantom guard: telemetry already attached; "
                    "passing response through unchanged.",
                    flush=True,
                )
                return llm_response
            if isinstance(cached_comment, str) and cached_comment:
                print(
                    "[TELEMETRY] phantom guard: re-attaching cached machine "
                    "comment to re-invoked response.",
                    flush=True,
                )
                return _append_footer_to_response(llm_response, cached_comment)
            return None

        footer_text = _build_footer(elapsed, calls, tool_calls)
        machine_comment = _build_machine_comment(elapsed, calls, tool_calls)
        # Cached for the re-invocation; survives _reset_turn on purpose.
        _state_set(state, _LAST_MACHINE_COMMENT_KEY, machine_comment)

        print(f"[TELEMETRY] turn complete: {footer_text!r}", flush=True)
        try:
            logger.info("Turn telemetry: %s", footer_text.replace("\n", " | "))
        except UnicodeEncodeError:
            try:
                # Log an ASCII-safe representation if standard console encoding does not support emojis
                safe_log = footer_text.encode('ascii', errors='replace').decode('ascii')
                logger.info("Turn telemetry (ascii-safe): %s", safe_log.replace("\n", " | "))
            except Exception:
                pass

        _reset_turn(state)

        # The machine comment ships on EVERY final response -- it's the
        # cross-A2A wire contract tea-talk parses for cost/rows/splits.
        # The human footer stays behind SHOW_TELEMETRY_FOOTER as before.
        if not _is_footer_enabled():
            return _append_footer_to_response(llm_response, machine_comment)

        return _append_footer_to_response(
            llm_response, footer_text + machine_comment
        )

    except Exception as exc:  # noqa: BLE001
        print(
            f"[TELEMETRY] after-model error (continuing without footer): "
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )
        return None


# ===========================================================================
# BEFORE / AFTER TOOL callbacks — NEW in this version
# ===========================================================================

def _tool_call_id(tool: Any, tool_context: Any) -> str:
    """Derive a stable id for a tool call to correlate before/after.

    ToolContext has a `function_call_id` assigned by the framework per
    invocation. Falls back to tool name + timestamp if unavailable.
    """
    fcid = getattr(tool_context, "function_call_id", None)
    if fcid:
        return str(fcid)
    # Fallback: tool name + monotonic clock to the millisecond.
    tool_name = getattr(tool, "name", "unknown")
    return f"{tool_name}_{int(time.monotonic() * 1000)}"


def telemetry_before_tool_callback(
    tool: Any,
    args: dict,
    tool_context: Any,
) -> Optional[dict]:
    """Stamp the start time for this tool call. Returns None so the
    tool executes normally.

    We key by the call_id so before/after can correlate even if tools
    run concurrently (ADK supports parallel tool calls).
    """
    try:
        state = tool_context.state
        call_id = _tool_call_id(tool, tool_context)

        tool_starts = _state_get(state, _TOOL_STARTS_KEY)
        if not isinstance(tool_starts, dict):
            tool_starts = {}

        tool_starts[call_id] = time.monotonic()
        _state_set(state, _TOOL_STARTS_KEY, tool_starts)

        tool_name = getattr(tool, "name", "unknown")

        # Reset accumulated BQ telemetry so this tool's stats start fresh
        # (only if the agent registered a BQ ledger via configure_bq_ledger).
        if _BQ_RESET_FN is not None:
            try:
                _BQ_RESET_FN()
            except Exception:  # noqa: BLE001
                pass

        print(
            f"[TELEMETRY] tool start: {tool_name!r} id={call_id}",
            flush=True,
        )
    except Exception as exc:  # noqa: BLE001
        print(
            f"[TELEMETRY] before-tool error (continuing): "
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )

    return None


def _extract_row_count(tool_response: Any) -> Optional[int]:
    """Best-effort row-count extraction from a Tableau MCP tool response.

    Returns the number of rows in the response's `data` array, or None
    if the response shape doesn't match a recognized data-bearing
    Tableau MCP result. Designed to be cheap (no full JSON parse on
    multi-MB responses) and never raise.

    The MCP response shape is:
      tool_response["content"][0]["text"] = '{"data":[{...},{...},...]}'

    We count occurrences of the row-separator substring and add 1, which
    equals the row count for any non-empty data array -- avoiding a
    multi-MB JSON parse.
    """
    try:
        if not isinstance(tool_response, dict):
            return None
        # Batch-tool shape ({"results": [{"rows": N, ...}, ...]}) from
        # query_datasource_batch: sum the per-entry row counts.
        if isinstance(tool_response.get("results"), list):
            total = 0
            found = False
            for entry in tool_response["results"]:
                if isinstance(entry, dict) and isinstance(entry.get("rows"), int):
                    total += entry["rows"]
                    found = True
            if found:
                return total
        if tool_response.get("isError"):
            return None
        content = tool_response.get("content")
        if not isinstance(content, list) or not content:
            return None
        block = content[0]
        if not isinstance(block, dict) or block.get("type") != "text":
            return None
        text = block.get("text", "")
        if not isinstance(text, str) or not text:
            return None
        # Cheap shape check: must contain the data array marker.
        if '"data"' not in text:
            return None
        # Empty data array -- explicitly 0 rows.
        if '"data":[]' in text:
            return 0
        # Count row separators. For N rows, there are exactly N-1
        # separators of the form `},{` between adjacent objects.
        sep_count = text.count('},{')
        return sep_count + 1
    except Exception:  # noqa: BLE001
        return None


def telemetry_after_tool_callback(
    tool: Any,
    args: dict,
    tool_context: Any,
    tool_response: Any,
) -> Optional[dict]:
    """Record this tool call's wall time in the turn accumulator.
    Returns None so the original tool response passes through unchanged.
    """
    try:
        state = tool_context.state
        call_id = _tool_call_id(tool, tool_context)
        tool_name = getattr(tool, "name", "unknown")
        end = time.monotonic()

        tool_starts = _state_get(state, _TOOL_STARTS_KEY) or {}
        start = tool_starts.pop(call_id, None)
        duration = (end - start) if start else 0.0
        _state_set(state, _TOOL_STARTS_KEY, tool_starts)

        usage = _get_turn_usage(state)
        # Best-effort row-count extraction. None for non-Tableau tools or
        # error responses; an integer for successful data responses. Feeds
        # the machine-comment's `rows` field (the tea-talk wire contract).
        row_count = _extract_row_count(tool_response)
        # Record both the duration AND the wall-clock timestamps. The
        # timestamps are what `_build_footer` uses to compute aggregate
        # tool time correctly when tool calls run in parallel (summing
        # durations double-counts concurrent calls; using first-start
        # to last-end gives the real wall-clock elapsed).
        usage.setdefault("tool_calls", []).append(
            {
                "name": tool_name,
                "duration_sec": duration,
                "start_time": start,
                "end_time": end,
                "row_count": row_count,
            }
        )
        _state_set(state, _TURN_USAGE_KEY, usage)

        rc_str = f" rows={row_count}" if row_count is not None else ""

        # BigQuery telemetry: if the tool that just ran used BigQuery
        # (via the session ledger), capture the stats for the footer.
        # Uses last_drained_ledger() because the executor calls
        # drain_ledger() before this callback fires (clearing the ContextVar).
        bq_str = ""
        try:
            bq = _BQ_LAST_DRAINED_FN() if _BQ_LAST_DRAINED_FN is not None else None
            if bq is not None and bq.total_queries > 0:
                bq_str = (f" bq={bq.total_queries}q/{bq.total_bytes_billed_mb:.0f}MB"
                          f"/${bq.total_cost_usd:.4f}")
                if bq.cached_queries:
                    bq_str += f" ({bq.cached_queries} cached)"
                # Store on the tool_call entry so _build_footer can render it
                usage["tool_calls"][-1]["bq_summary"] = bq.summary()
                usage["tool_calls"][-1]["bq_footer"] = bq.footer()
                # Structured BQ stats for the machine comment (the A2A wire
                # contract tea-talk parses). The human footer above renders
                # bq_footer; without THIS, tea-talk never receives the BQ
                # economics and its footer/admin panel show $0 for BigQuery.
                usage["tool_calls"][-1]["bq"] = {
                    "queries": bq.total_queries,
                    "s": round(bq.total_elapsed_s, 1),
                    "gb_billed": round(bq.total_bytes_billed_gb, 4),
                    "cost_usd": round(bq.total_cost_usd, 4),
                    "cached": bq.cached_queries,
                    "saved_usd": round(bq.total_savings_usd, 4),
                }
                _state_set(state, _TURN_USAGE_KEY, usage)
        except Exception:  # noqa: BLE001
            pass

        print(
            f"[TELEMETRY] tool end: {tool_name!r} dur={duration:.2f}s{rc_str}{bq_str}",
            flush=True,
        )
    except Exception as exc:  # noqa: BLE001
        print(
            f"[TELEMETRY] after-tool error (continuing): "
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )

    return None


# ===========================================================================
# Utilities (unchanged)
# ===========================================================================

def _get_stashed_model(state: Any) -> Optional[str]:
    val = _state_get(state, _LAST_MODEL_KEY)
    return val if isinstance(val, str) and val else None


def _get_model_name_from_response(llm_response: LlmResponse) -> Optional[str]:
    for attr in ("model", "model_name", "model_version"):
        val = getattr(llm_response, attr, None)
        if isinstance(val, str) and val:
            return val
    um = getattr(llm_response, "usage_metadata", None)
    if um is not None:
        for attr in ("model", "model_version"):
            val = getattr(um, attr, None)
            if isinstance(val, str) and val:
                return val
    return None


def _has_function_call_part(llm_response: LlmResponse) -> bool:
    if not llm_response.content or not llm_response.content.parts:
        return False
    for part in llm_response.content.parts:
        if getattr(part, "function_call", None) is not None:
            return True
    return False


def _response_has_machine_comment(llm_response: LlmResponse) -> bool:
    """True if the machine telemetry comment is already attached."""
    content = getattr(llm_response, "content", None)
    parts = getattr(content, "parts", None) if content else None
    for part in (parts or []):
        text = getattr(part, "text", None)
        if isinstance(text, str) and "<!--agent_telemetry" in text:
            return True
    return False


def _reset_turn(state: Any) -> None:
    _state_set(state, _TURN_USAGE_KEY,
               {"calls": [], "tool_calls": [], "start_time": None})
    _state_set(state, _TOOL_STARTS_KEY, {})
    _state_set(state, _LAST_RECORDED_FINGERPRINT_KEY, None)


# ===========================================================================
# Footer formatting — expanded with LLM/tool/other breakdown + cache
# ===========================================================================

def _build_footer(
    elapsed_sec: float,
    calls: list[dict],
    tool_calls: list[dict],
) -> str:
    """Compose the multi-line footer string."""
    # ---- Aggregate LLM calls by tier ---------------------------------------
    by_tier: dict[str, dict[str, Any]] = {}
    total_tokens = 0
    total_cached = 0
    total_in_cost: Optional[float] = 0.0
    total_out_cost: Optional[float] = 0.0
    total_savings: Optional[float] = 0.0
    any_unknown_cost = False
    any_long_context = False
    total_llm_time = 0.0

    total_thinking = 0

    for call in calls:
        model_name = call["model"]
        agent_name = call.get("agent_name")
        normalized = _normalize_model_name(model_name)
        tier = _model_to_tier(model_name, agent_name)
        in_tok = call["input_tokens"]
        out_tok = call["output_tokens"]
        think_tok = call.get("thinking_tokens", 0)
        cached = call.get("cached_tokens", 0)
        dur = call.get("duration_sec", 0.0)

        in_cost, out_cost, is_long, savings = compute_cost(
            model_name, in_tok, out_tok, cached,
            call.get("cache_write_tokens", 0),
            think_tok,
        )

        bucket = by_tier.setdefault(
            tier,
            {
                "model": normalized or model_name,
                "in": 0,
                "out": 0,
                "thinking": None,
                "thinking_tokens": 0,
                "cached": 0,
                "written": 0,
                "in_cost": 0.0,
                "out_cost": 0.0,
                "savings": 0.0,
                "cost_known": True,
                "calls": 0,
                "long_ctx": False,
                "llm_time": 0.0,
            },
        )
        bucket["in"] += in_tok
        bucket["out"] += out_tok
        bucket["thinking_tokens"] += think_tok
        bucket["cached"] += cached
        bucket["written"] += call.get("cache_write_tokens", 0)
        bucket["calls"] += 1
        bucket["llm_time"] += dur
        if call.get("thinking"):
            bucket["thinking"] = call["thinking"]
        total_llm_time += dur

        if in_cost is None or out_cost is None:
            bucket["cost_known"] = False
            any_unknown_cost = True
        else:
            bucket["in_cost"] += in_cost
            bucket["out_cost"] += out_cost
            if total_in_cost is not None:
                total_in_cost += in_cost
            if total_out_cost is not None:
                total_out_cost += out_cost
            if savings is not None:
                bucket["savings"] += savings
                if total_savings is not None:
                    total_savings += savings

        if is_long:
            bucket["long_ctx"] = True
            any_long_context = True

        total_tokens += in_tok + out_tok + think_tok
        total_cached += cached
        total_thinking += think_tok

    # ---- Aggregate tool calls by name --------------------------------------
    #
    # Per-tool buckets keep summed duration (cumulative CPU time per tool
    # name — useful for seeing which tools dominate). The AGGREGATE tool
    # time at the top of the footer is wall-clock, because ADK supports
    # parallel tool dispatch (the 360° template fires 11 queries at once)
    # and summing durations would count that as ~30s when the real wall
    # clock is ~8s. Real wall-clock = max(end_time) − min(start_time).
    by_tool: dict[str, dict[str, Any]] = {}
    tool_starts_ts: list[float] = []
    tool_ends_ts: list[float] = []
    sum_of_durations = 0.0
    for tc in tool_calls:
        name = tc.get("name", "unknown")
        dur = tc.get("duration_sec", 0.0)
        st = tc.get("start_time")
        et = tc.get("end_time")
        bucket = by_tool.setdefault(name, {"count": 0, "duration": 0.0})
        bucket["count"] += 1
        bucket["duration"] += dur
        sum_of_durations += dur
        if isinstance(st, (int, float)):
            tool_starts_ts.append(float(st))
        if isinstance(et, (int, float)):
            tool_ends_ts.append(float(et))

    if tool_starts_ts and tool_ends_ts:
        # Wall-clock window spanning all tool calls. Parallel calls
        # collapse into one interval; serial calls span their sum.
        total_tool_time = max(tool_ends_ts) - min(tool_starts_ts)
        # Defensive floor — if timestamps are corrupt or cross-turn,
        # never go negative.
        total_tool_time = max(0.0, total_tool_time)
    else:
        # Fallback: no timestamps recorded (e.g. older session state
        # or a partial turn). Use the sum so the footer still has a
        # reasonable number rather than 0.
        total_tool_time = sum_of_durations

    other_time = max(0.0, elapsed_sec - total_llm_time - total_tool_time)

    # ---- Header line 1: time breakdown -------------------------------------
    tool_count = len(tool_calls)
    llm_count = len(calls)
    line1 = (
        f"⏱ {elapsed_sec:.1f}s total  ·  "
        f"🧠 LLM {total_llm_time:.1f}s ({llm_count} call{'s' if llm_count != 1 else ''})  ·  "
        f"🔧 tools {total_tool_time:.1f}s ({tool_count} call{'s' if tool_count != 1 else ''})  ·  "
        f"⚙ other {other_time:.1f}s"
    )

    # ---- Header line 2: tokens + cost + savings ----------------------------
    if any_unknown_cost:
        cost_summary = "💰 cost $? (pricing unavailable)"
    else:
        grand_total = (total_in_cost or 0.0) + (total_out_cost or 0.0)
        cost_summary = f"💰 {_format_cost(grand_total)}"
        if total_savings and total_savings > 0.0:
            cost_summary += (
                f"  (saved ~{_format_cost(total_savings)} "
                f"via cache vs no-cache)"
            )

    cached_str = f"  ·  💾 cached {total_cached:,}" if total_cached else ""
    thinking_str = f"  ·  💭 thinking {total_thinking:,}" if total_thinking else ""
    line2 = (
        f"🧮 {total_tokens:,} tokens{cached_str}{thinking_str}  ·  "
        f"{cost_summary}"
    )
    if any_long_context:
        line2 += "  ⚠ long-context tier"

    # ---- Per-tier lines ----------------------------------------------------
    tier_order = ["triage", "fast", "deep"]
    tier_lines = []
    for tier in tier_order:
        if tier in by_tier:
            tier_lines.append(_format_tier_lines(tier, by_tier[tier]))
    for tier in by_tier:
        if tier not in tier_order:
            tier_lines.append(_format_tier_lines(tier, by_tier[tier]))

    # ---- Per-tool lines ----------------------------------------------------
    tool_lines = []
    bq_footer_lines = []
    if by_tool:
        tool_lines.append("   🔧 tools")
        for name, bucket in sorted(
            by_tool.items(), key=lambda kv: kv[1]["duration"], reverse=True
        ):
            call_label = "call" if bucket["count"] == 1 else "calls"
            tool_lines.append(
                f"     {name:<28} {bucket['duration']:5.1f}s  "
                f"({bucket['count']} {call_label})"
            )

    # BigQuery telemetry from any tool call that captured it
    for tc in tool_calls:
        bq_footer = tc.get("bq_footer")
        if bq_footer and bq_footer not in bq_footer_lines:
            bq_footer_lines.append(bq_footer)

    body_parts = [line1, line2] + tier_lines + tool_lines + bq_footer_lines
    return "\n\n---\n" + "\n".join(body_parts)


def _format_tier_lines(tier: str, bucket: dict[str, Any]) -> str:
    """Two-line block per LLM tier."""
    in_tok = bucket["in"]
    out_tok = bucket["out"]
    think_tok = bucket.get("thinking_tokens", 0)
    cached = bucket["cached"]
    calls = bucket["calls"]
    model_name = bucket["model"]
    thinking = bucket.get("thinking")
    model_disp = (f"{model_name}, thinking={thinking}" if thinking
                  else model_name)
    long_flag = " ⚠>200K" if bucket["long_ctx"] else ""
    call_label = "call" if calls == 1 else "calls"

    if not bucket["cost_known"]:
        total_cost_str = "$?"
        savings_str = ""
    else:
        total_cost_str = _format_cost(bucket["in_cost"] + bucket["out_cost"])
        if bucket["savings"] and bucket["savings"] > 0:
            savings_str = f"  saved {_format_cost(bucket['savings'])}"
        else:
            savings_str = ""

    cached_str = f"   cached {cached:,}" if cached else ""
    written = bucket.get("written", 0)
    written_str = f"   wrote {written:,}" if written else ""
    think_str = f"   thinking {think_tok:,}" if think_tok else ""

    header_line = f"   {tier:<7} ({model_disp}){long_flag}"
    detail_line = (
        f"     in {in_tok:<9,} out {out_tok:<7,}{think_str}"
        f"{cached_str}{written_str}  ·  "
        f"total {total_cost_str}{savings_str}  ({calls} {call_label})"
    )
    return f"{header_line}\n{detail_line}"


def _format_cost(cost_usd: float) -> str:
    if cost_usd >= 1.0:
        return f"${cost_usd:,.2f}"
    if cost_usd >= 0.01:
        return f"${cost_usd:.4f}"
    if cost_usd >= 0.0001:
        return f"${cost_usd:.5f}"
    return f"${cost_usd:.7f}"


def _build_machine_comment(
    elapsed_sec: float,
    calls: list[dict],
    tool_calls: list[dict],
) -> str:
    """Compact machine-readable turn telemetry as an HTML comment.

    Appended to EVERY final response (independent of SHOW_TELEMETRY_FOOTER).
    Markdown renderers hide HTML comments, so humans never see it; the
    tea-talk orchestrator strips and parses it (_extract_agent_telemetry)
    into per-agent telemetry: cost, rows, LLM/tool splits. Treat the keys
    as a wire contract -- rename only in lockstep with tea-talk.
    """
    tokens_in = sum(c.get("input_tokens", 0) for c in calls)
    tokens_out = sum(c.get("output_tokens", 0) for c in calls)
    tokens_thinking = sum(c.get("thinking_tokens", 0) for c in calls)
    cached = sum(c.get("cached_tokens", 0) for c in calls)
    llm_s = sum(c.get("duration_sec", 0.0) for c in calls)

    cost = 0.0
    saved = 0.0
    cost_known = bool(calls)
    # Per-tier buckets, mirroring the human footer's tier lines so the
    # consumer can render "deep (gemini-3.1-pro-preview) in N out N".
    tiers: dict[str, dict[str, Any]] = {}
    for c in calls:
        model_name = c.get("model", "")
        ic, oc, _is_long, sav = compute_cost(
            model_name,
            c.get("input_tokens", 0),
            c.get("output_tokens", 0),
            c.get("cached_tokens", 0),
            c.get("cache_write_tokens", 0),
            c.get("thinking_tokens", 0),
        )
        if ic is None or oc is None:
            cost_known = False
        else:
            cost += ic + oc
            if sav:
                saved += sav
        tier_name = _model_to_tier(model_name, c.get("agent_name"))
        bucket = tiers.setdefault(tier_name, {
            "tier": tier_name,
            "model": _normalize_model_name(model_name) or model_name,
            "thinking": None,
            "in": 0, "out": 0, "thinking_tokens": 0,
            "cached": 0, "written": 0, "calls": 0,
            "cost_usd": 0.0, "saved_usd": 0.0,
        })
        if c.get("thinking"):
            bucket["thinking"] = c["thinking"]
        bucket["in"] += c.get("input_tokens", 0)
        bucket["out"] += c.get("output_tokens", 0)
        bucket["thinking_tokens"] += c.get("thinking_tokens", 0)
        bucket["cached"] += c.get("cached_tokens", 0)
        bucket["written"] += c.get("cache_write_tokens", 0)
        bucket["calls"] += 1
        if ic is not None and oc is not None:
            bucket["cost_usd"] += ic + oc
            if sav:
                bucket["saved_usd"] += sav

    # Per-tool buckets (summed duration per tool name, plus row counts) --
    # the same breakdown the human footer prints under its tools heading.
    by_tool: dict[str, dict[str, Any]] = {}
    for tc in tool_calls:
        tname = tc.get("name", "unknown")
        tb = by_tool.setdefault(tname, {
            "name": tname, "s": 0.0, "calls": 0,
            "rows": 0, "max_rows": 0, "have_rows": False,
            "bq": None, "bq_calls": [],
        })
        tb["s"] += tc.get("duration_sec", 0.0)
        tb["calls"] += 1
        rc = tc.get("row_count")
        if isinstance(rc, int):
            tb["have_rows"] = True
            tb["rows"] += rc
            if rc > tb["max_rows"]:
                tb["max_rows"] = rc
        # BigQuery economics per tool call: keep the aggregate (summed across
        # calls of this tool) AND each call's own stats (bq_calls), so the
        # consumer can render one summary line plus per-call lines.
        bqd = tc.get("bq")
        if isinstance(bqd, dict):
            if tb["bq"] is None:
                tb["bq"] = {"queries": 0, "s": 0.0, "gb_billed": 0.0,
                            "cost_usd": 0.0, "cached": 0, "saved_usd": 0.0}
            tb["bq"]["queries"] += bqd.get("queries", 0)
            tb["bq"]["s"] += bqd.get("s", 0.0)
            tb["bq"]["gb_billed"] += bqd.get("gb_billed", 0.0)
            tb["bq"]["cost_usd"] += bqd.get("cost_usd", 0.0)
            tb["bq"]["cached"] += bqd.get("cached", 0)
            tb["bq"]["saved_usd"] += bqd.get("saved_usd", 0.0)
            tb["bq_calls"].append(bqd)

    _tier_order = {"triage": 0, "fast": 1, "deep": 2}
    tier_list: list = []
    for tb2 in sorted(tiers.values(),
                      key=lambda b: _tier_order.get(b["tier"], 9)):
        entry = {k: tb2[k] for k in
                 ("tier", "model", "in", "out", "cached", "calls")}
        if tb2.get("thinking"):
            entry["thinking"] = tb2["thinking"]
        if tb2.get("thinking_tokens"):
            entry["thinking_tokens"] = tb2["thinking_tokens"]
        if tb2.get("written"):
            entry["cache_write"] = tb2["written"]
        entry["cost_usd"] = round(tb2["cost_usd"], 4)
        if tb2["saved_usd"] > 0:
            entry["saved_usd"] = round(tb2["saved_usd"], 4)
        tier_list.append(entry)

    tool_list: list = []
    for tb3 in sorted(by_tool.values(), key=lambda b: b["s"], reverse=True):
        entry = {"name": tb3["name"], "s": round(tb3["s"], 1),
                 "calls": tb3["calls"]}
        if tb3["have_rows"]:
            entry["rows"] = tb3["rows"]
            if tb3["max_rows"] != tb3["rows"]:
                entry["max_rows"] = tb3["max_rows"]
        if tb3.get("bq"):
            b = tb3["bq"]
            entry["bq"] = {
                "queries": b["queries"], "s": round(b["s"], 1),
                "gb_billed": round(b["gb_billed"], 4),
                "cost_usd": round(b["cost_usd"], 4),
                "cached": b["cached"], "saved_usd": round(b["saved_usd"], 4),
            }
            # Per-call breakdown (mirrors GE's individual `bigquery:` lines).
            if len(tb3.get("bq_calls") or []) > 1:
                entry["bq_calls"] = tb3["bq_calls"]
        tool_list.append(entry)

    starts = [tc["start_time"] for tc in tool_calls
              if isinstance(tc.get("start_time"), (int, float))]
    ends = [tc["end_time"] for tc in tool_calls
            if isinstance(tc.get("end_time"), (int, float))]
    if starts and ends:
        tools_s = max(0.0, max(ends) - min(starts))
    else:
        tools_s = sum(tc.get("duration_sec", 0.0) for tc in tool_calls)

    rows = sum(tc["row_count"] for tc in tool_calls
               if isinstance(tc.get("row_count"), int))

    payload: dict[str, Any] = {
        "elapsed_s": round(elapsed_sec, 1),
        "llm_calls": len(calls),
        "llm_s": round(llm_s, 1),
        "tool_calls": len(tool_calls),
        "tools_s": round(tools_s, 1),
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "tokens_thinking": tokens_thinking,
        "cached": cached,
        "rows": rows,
        "tiers": tier_list,
        "tools": tool_list,
    }
    if cost_known:
        payload["cost_usd"] = round(cost, 4)
        if saved > 0:
            payload["saved_usd"] = round(saved, 4)

    return ("\n\n<!--agent_telemetry "
            + json.dumps(payload, separators=(",", ":"))
            + "-->")


def _append_footer_to_response(
    llm_response: LlmResponse,
    footer_text: str,
) -> LlmResponse:
    existing_parts = list(llm_response.content.parts) if llm_response.content else []
    new_parts = existing_parts + [types.Part(text=footer_text)]

    new_response = llm_response.model_copy(
        update={
            "content": types.Content(
                role=(llm_response.content.role if llm_response.content else "model")
                or "model",
                parts=new_parts,
            ),
        }
    )
    return new_response


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
