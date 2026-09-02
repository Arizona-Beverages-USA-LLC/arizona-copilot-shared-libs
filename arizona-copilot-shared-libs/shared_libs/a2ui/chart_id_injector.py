"""
Chart ID injector — fresh chart_id per turn via before_model_callback.

WHY THIS EXISTS
===============
In the Google-standard A2UI pattern, the agent emits the full ChartArtifact
component tree (Card + Column + VegaChart + Modal + 5 format Buttons) from
its prompt template. Each format Button's action.context must carry a
`chart_id` so the download handler can later look up the chart's spec
and render it in the chosen format.

The question: where does `chart_id` come from?

Options considered:
  (a) Teach the model to generate uuids itself.
      Problem: uuids are hard for LLMs to produce correctly and consistently.
      Models may emit malformed uuids, reuse them across turns, or invent
      patterns that look like uuids but aren't unique.
  (b) Post-process the model's output to substitute a placeholder
      (e.g. CHART_ID_HERE) with a real uuid.
      Problem: Adds a string-surgery step after the LLM. Works but is a
      deviation from Google's canonical pattern where the model
      outputs final-form A2UI.
  (c) This approach: before the LLM runs, generate a fresh chart_id,
      stash it in session state, and inject a dynamic system
      instruction telling the model to use that specific id verbatim.
      The model sees a concrete value, emits it verbatim, and we never
      post-process its output.

Option (c) is more canonical: the model generates valid, final-form A2UI
on the first attempt. It also has a second benefit — the chart_id is
available in session state BEFORE the model runs, so the after_model
callback can look it up to cache the VegaChart spec under that same id.

MECHANISM
=========
This module is a `before_model_callback`. ADK only supports one
before_model_callback per agent, so this module also composes with the
existing chart_download_handler — if an inbound userAction is a
`download_chart` event, the download handler short-circuits the turn
and we never get to the injection step. If it's anything else (a user
query, a different userAction type), we proceed to inject the chart_id
and let the LLM run normally.

STATE KEY
=========
Writes to `ctx.state["arizona_current_chart_id"]`. The a2ui_bridge's
after_model callback reads this key when caching any VegaChart spec.

INJECTION MECHANICS
===================
ADK supports prompt placeholders via instructions_utils.inject_session_state,
but that only runs on the agent.instruction string — not on content the
model emits. Two possible injection strategies:

  Strategy A: append a system instruction to llm_request.contents that
  says "For this turn, if you emit a chart, use chart_id='<UUID>' in
  every format button's context." This is what we do here.

  Strategy B: use {placeholder} in the agent.instruction template and
  rely on ADK's built-in substitution. This would require restructuring
  the agent's instruction as an InstructionProvider function, which is
  a bigger refactor. Strategy A is lighter and works today.

We use Strategy A. It's straightforward, transparent, and easy to debug
by inspecting Cloud Logging.

TRIGGER
=======
We don't always need a chart_id. Most turns don't involve charts. We
inject a chart_id ONLY when the upcoming response is plausibly going to
include a VegaChart. The simplest heuristic is: generate a chart_id every
turn, put it in state, mention it to the model only if relevant. If the
model doesn't emit a chart, the id goes unused. There's no harm — state
is session-scoped and overwritten every turn anyway.

However we do pay a small prompt cost per turn (~80 tokens to announce
the chart_id to the model). For now, that's acceptable. If token cost
becomes an issue we can add a cheap heuristic (look at the user's
message for chart-trigger words) and skip the injection when unlikely.
"""

from __future__ import annotations

import logging
import uuid
from typing import Optional

from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types


logger = logging.getLogger(__name__)


# State key where the chart_id is stored per turn. The a2ui_bridge's
# after_model callback reads this same key when caching VegaChart specs.
_CHART_ID_STATE_KEY = "arizona_current_chart_id"

# The literal token the agent's prompt template uses as a placeholder.
# The injector tells the model to substitute this with a real chart_id
# at emission time. The bridge never does any string replacement;
# substitution happens inside the model itself, guided by this per-turn
# instruction.
_PLACEHOLDER_TOKEN = "CHART_ID_HERE"

# =============================================================================
# CHART-INTENT HEURISTIC
# =============================================================================
# The injector runs on every turn but ONLY emits its instruction when the
# user's most recent message contains a visualization keyword. This
# prevents the instruction from priming the model to emit unrequested
# charts on every turn.
#
# Observed failure mode: on Flash-tier models, the instruction
# "IMPORTANT FOR THIS TURN: If you emit a ChartArtifact ..." at the end
# of the conversation context was enough to steer the model toward
# emitting charts for queries like "list the top 10 customers" where
# the user never asked for one. Making the injection conditional fixes
# this without changing the prompt template.
#
# The keyword list mirrors the "When to use A2UI" section of the agent
# prompt. Keep these two lists in sync — if you add a chart trigger to
# the prompt, add it here too.
_CHART_TRIGGER_KEYWORDS: tuple[str, ...] = (
    "chart",
    "graph",
    "plot",
    "visualize",
    "visualization",
    "visualise",       # British spelling
    "visualisation",   # British spelling
    "bar chart",
    "line chart",
    "pie chart",
    "trend line",
    "trend chart",
    "distribution",    # as in "show the distribution of..."
    "histogram",
    "scatter",
    "diagram",
)


def _last_user_message_text(llm_request: LlmRequest) -> str:
    """Pull the text of the most recent user-role message from contents.

    Returns empty string if nothing matches. The check is case-
    insensitive at the caller.
    """
    if not llm_request.contents:
        return ""

    # Walk backwards; first user-role content with text wins.
    for content in reversed(llm_request.contents):
        role = getattr(content, "role", None)
        if role != "user":
            continue
        parts = getattr(content, "parts", None) or []
        for part in parts:
            text = getattr(part, "text", None)
            if isinstance(text, str) and text:
                return text
    return ""


def _user_message_mentions_chart(llm_request: LlmRequest) -> bool:
    """True if the most recent user message looks like a chart request.

    Implementation notes:
      - Case-insensitive substring match. Good enough — the prompt
        already tells the model what counts as a chart request, so we
        just need a rough filter for when to inject the per-turn id.
      - A false positive (inject when the user didn't want a chart) is
        cheap: ~80 extra tokens and the model may emit a chart it
        shouldn't. Still net-better than the current always-on state.
      - A false negative (skip injection when the user did want a chart)
        means the chart renders with `chart-fallback` id and downloads
        may fail. Acceptable tradeoff; the keyword list is conservative.
    """
    text = _last_user_message_text(llm_request).lower()
    if not text:
        return False
    return any(kw in text for kw in _CHART_TRIGGER_KEYWORDS)


def _make_chart_id() -> str:
    """Generate a short, URL-safe id for the current turn's chart(s).

    8 hex chars is plenty unique for a single chat session (collision
    chance of ~1 in 4 billion per id; we expect at most a few per
    session). Short ids keep the prompt clean and debugging readable.
    """
    return uuid.uuid4().hex[:8]


def _build_chart_id_instruction(chart_id: str) -> str:
    """Compose the per-turn system instruction that tells the model which
    id to use in the ChartArtifact template.

    This instruction is appended as a synthetic "system" message to the
    inbound LlmRequest. The model sees it alongside the main agent
    instruction and treats the placeholder substitution as a concrete
    rule for this single generation call.

    Kept deliberately short — verbose instructions here would eat into
    the comprehensive-analysis prompt budget.
    """
    return (
        f"IMPORTANT FOR THIS TURN: If you emit a ChartArtifact A2UI block, "
        f"replace every occurrence of the placeholder token `{_PLACEHOLDER_TOKEN}` "
        f"with the exact string `{chart_id}` (including inside surfaceId, "
        f"component ids, and every format button's action.context "
        f"`chart_id` value). Use this single chart_id for all format "
        f"buttons in the same chart. Do not invent your own chart_ids; "
        f"use `{chart_id}` verbatim."
    )


def inject_chart_id_for_turn(
    callback_context: CallbackContext,
    llm_request: LlmRequest,
) -> None:
    """Mutate the inbound request in place to inject a per-turn chart_id
    — but ONLY when the user's message looks like a chart request.

    1. Always generate a fresh chart_id and stash it in session state
       (so a2ui_bridge's VegaChart-caching path still works if the
       model decides to emit a chart despite our heuristic saying no).
    2. Look at the most recent user message. If it contains a
       visualization keyword ("chart", "graph", "plot", "visualize",
       etc.), append the "use this chart_id verbatim" instruction to
       llm_request.contents so the model substitutes the placeholder
       correctly.
    3. If no chart keywords are present, we DO NOT append the
       instruction. This avoids priming the model to emit charts on
       non-chart queries (the observed failure mode on Flash-tier
       models where the trailing "IMPORTANT FOR THIS TURN: If you emit
       a ChartArtifact..." message steered them toward emitting
       unrequested charts for top-N lists, comparisons, etc).
    """
    chart_id = _make_chart_id()

    # Persist to session state unconditionally so the after_model
    # callback (a2ui_bridge) can cache any VegaChart spec the model
    # emits. Defensive: if the model ignores our heuristic and emits a
    # chart anyway, at least it'll have a valid id available in state
    # to fall back to (a2ui_bridge reads this key).
    callback_context.state[_CHART_ID_STATE_KEY] = chart_id

    # Only inject the per-turn instruction if the user's message
    # suggests they actually want a chart. See comment block at the top
    # of this module for the rationale.
    if not _user_message_mentions_chart(llm_request):
        print(
            f"[CHART_ID] chart_id={chart_id!r} generated, stashed in state, "
            f"but NOT injected into prompt — no chart keywords in user "
            f"message",
            flush=True,
        )
        return

    # Build the synthetic user-turn content carrying the instruction.
    # We use role="user" because ADK's flows already treat user-role
    # content as trustworthy input for the generation; some models reject
    # additional role="system" parts when a system instruction is already
    # present on the request. A "user" content with a clear "IMPORTANT
    # FOR THIS TURN:" prefix reads as an in-context directive and is
    # reliably followed by Gemini 3.x.
    instruction_text = _build_chart_id_instruction(chart_id)
    instruction_content = types.Content(
        role="user",
        parts=[types.Part(text=instruction_text)],
    )

    # Append AT THE END so it's the most-recent instruction the model
    # sees before generating. ADK's LlmRequest.contents is a mutable list.
    if llm_request.contents is None:
        llm_request.contents = []
    llm_request.contents.append(instruction_content)

    print(
        f"[CHART_ID] injected chart_id={chart_id!r} into state + "
        f"per-turn instruction (user message mentions a chart)",
        flush=True,
    )


def build_chart_id_injector():
    """Factory for the standalone injector callback.

    Returns a coroutine that can be used directly as a before_model_callback
    if you want the injector WITHOUT the download handler composition.
    In normal operation you'll want `build_combined_before_model_callback`
    instead since only ONE before_model_callback is allowed per agent.
    """
    def _injector_only(
        callback_context: CallbackContext,
        llm_request: LlmRequest,
    ) -> Optional[LlmResponse]:
        try:
            inject_chart_id_for_turn(callback_context, llm_request)
        except Exception as exc:  # noqa: BLE001
            # Never kill the turn just because injection failed — worst
            # case the download button won't work for this turn's chart.
            logger.exception("chart_id injection failed")
            print(
                f"[CHART_ID] injection failed: {type(exc).__name__}: {exc} "
                f"- proceeding without chart_id (downloads will fall back)",
                flush=True,
            )
        return None  # Always proceed to the LLM

    print(
        "[CHART_ID] build_chart_id_injector() called - injector-only mode",
        flush=True,
    )
    return _injector_only


# =============================================================================
# Composition helper: chain this with the download handler
# =============================================================================

def build_combined_before_model_callback():
    """Return a single before_model_callback that composes two behaviors:

      1. If the inbound request is a `download_chart` userAction, let the
         download handler short-circuit the LLM and return the file.
      2. Otherwise, inject a fresh chart_id into state + per-turn
         instruction, then let the LLM run.

    This is the callback to register on the agent. ADK only allows one
    before_model_callback per agent; this composition is how we get
    both behaviors into that one slot.

    We import the download handler's inner function (not the factory)
    so we can call it directly and inspect its return value.
    """
    # Import here (not at module top) to avoid circular-import concerns
    # between injector ↔ chart_download_handler. Also makes the coupling
    # explicit at build time.
    from .chart_download_handler import _chart_download_callback

    def _run_async_in_sync(coro):
        import asyncio
        import concurrent.futures
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop and loop.is_running():
            with concurrent.futures.ThreadPoolExecutor() as executor:
                future = executor.submit(asyncio.run, coro)
                return future.result()
        else:
            return asyncio.run(coro)

    def _combined(
        callback_context: CallbackContext,
        llm_request: LlmRequest,
    ) -> Optional[LlmResponse]:
        # Step 1: try the download handler. If it returns an LlmResponse,
        # we short-circuit the LLM (the user clicked a download button;
        # the file is already saved as an artifact).
        try:
            download_response = _run_async_in_sync(
                _chart_download_callback(callback_context, llm_request)
            )
        except Exception as exc:  # noqa: BLE001
            # The download handler has its own defensive try/except, but
            # extra safety never hurts. Log and continue.
            logger.exception("download handler raised in combined callback")
            print(
                f"[CHART_ID] download handler error (continuing): "
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )
            download_response = None

        if download_response is not None:
            # The turn is handled (a file was saved). Don't inject a
            # chart_id — no LLM generation is happening this turn.
            return download_response

        # Step 2: no download to serve. Inject a fresh chart_id for the
        # upcoming LLM turn in case the model emits a chart.
        try:
            inject_chart_id_for_turn(callback_context, llm_request)
        except Exception as exc:  # noqa: BLE001
            logger.exception("chart_id injection failed in combined callback")
            print(
                f"[CHART_ID] injection failed: {type(exc).__name__}: {exc} "
                f"- proceeding without chart_id",
                flush=True,
            )

        return None  # Let the LLM run

    print(
        "[CHART_ID] build_combined_before_model_callback() called - "
        "download handler + chart_id injection chained",
        flush=True,
    )
    return _combined


__all__ = [
    "build_chart_id_injector",
    "build_combined_before_model_callback",
    "inject_chart_id_for_turn",
    "_CHART_ID_STATE_KEY",
    "_PLACEHOLDER_TOKEN",
]
