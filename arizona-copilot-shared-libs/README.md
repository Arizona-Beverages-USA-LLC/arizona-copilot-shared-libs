# arizona-copilot-shared-libs

Canonical **shared libraries** for the Arizona Beverages copilots (VIP, IRI,
Sales & Merchandising, and future agents). One source of truth, **vendored**
into each agent repo — replacing the drifted per-agent copies that lived in
each agent's `shared/`.

The repo ships one tree, `shared_libs/`, with four sibling packages:

```
shared_libs/
├── a2ui/          A2UI charting (emit → render → download buttons)
├── data_export/   PNG/PDF/PPTX render + GCS sign + ADK export toolset
├── telemetry/     per-turn time / tokens / cache / cost instrumentation
└── caching/       ADK context-cache config + turn-1 cache pre-warming
```

Each agent vendors the **whole `shared_libs/` tree** at `<agent_repo>/shared_libs/`
and imports `from shared_libs.a2ui import ...` / `from shared_libs.data_export import ...`
/ `from shared_libs.telemetry import ...` / `from shared_libs.caching import ...`.

## Why they live here (and why as siblings)

- **`a2ui` hard-depends on `data_export`.** `a2ui_bridge.py` and
  `chart_download_handler.py` import `render_vega_to_html` (png),
  `upload_html_and_sign` (gcs_upload), and `save_as_adk_artifact` (_artifact)
  from `data_export`. Without it, a2ui's download buttons and interactive-chart
  link don't work. So the dependency must always travel with a2ui.
- **`data_export` is a shared library in its own right.** `create_export_toolset()`
  gives every agent the ADK tools for "download chart as PNG", "export analysis
  as PDF", "export as PowerPoint" — used independently of charting. It's exactly
  as drift-prone as a2ui and deserves one canonical home.
- **The coupling is one-way** (`a2ui → data_export`; `data_export` imports nothing
  from `a2ui`), so they're kept as **peers**, not nested. You may vendor
  `data_export` alone (an agent that only needs export tools, no charts); you must
  **never** vendor `a2ui` without `data_export`.
- **`telemetry` is independent of both.** It imports nothing from `a2ui`,
  `data_export`, or any agent module — the model-tier names and the optional
  BigQuery ledger are injected through its `configure_*` hooks. Every agent wants
  per-turn cost/latency instrumentation, so it earns a canonical home too, and it
  may be vendored entirely on its own.
- **`caching` is independent of all of them.** It imports nothing agent-specific
  — the App name is an argument, and pre-warm gating is a caller-supplied
  predicate. It replaces the near-identical per-agent `shared/app_builder.py`
  copies (one env-driven `build_app`) and adds a turn-1 cache pre-warm that was
  previously implemented in only one agent. May be vendored on its own.

## `a2ui/` — A2UI charting

Turns an agent's `<a2ui-json>` output (a Google-standard A2UI component tree;
charts via **VegaChart / Vega-Lite**) into the wire format Gemini Enterprise
renders natively, and adds per-turn **chart-id injection** and a **PNG / PDF /
CSV download** handler for the canonical ChartArtifact template.

It is a **renderer, not an inferer**: the agent writes the chart spec (guided by
`A2UI_GUIDELINES_CORE.md`); this package parses, validates, caches, and packages
it. The `chart_id_injector` does only a light keyword gate; the real "should this
be a chart" gating lives in the agent prompt.

The A2UI catalog is registered as **`arizona_copilot_combined`** (catalogId
`…/arizona_copilot_combined_v1`) — a domain-neutral name shared by every agent
that vendors this package.

> **Render surface:** A2UI renders on the **A2A path → Gemini Enterprise**, and
> does **NOT** render in local `adk web` on ADK 2.4.0. A correct chart shows as
> *nothing* in `adk web`; validate charts in Gemini Enterprise. See the project
> doc `a2ui_charting_consolidation_and_iri_enablement.md`.

```
a2ui/
├── __init__.py                 public API (the two factories + helpers)
├── a2ui_bridge.py              after_model_callback: parse/validate/cache/render
├── chart_id_injector.py        before_model_callback: download handler + per-turn chart_id
├── chart_download_handler.py   PNG/PDF/CSV download userAction handler
├── chart_state.py              per-turn VegaChart spec cache (session state)
├── a2ui_config.py              A2UI catalog / schema manager (loads the two data files)
├── custom_catalog.json         the A2UI component catalog (Card, VegaChart, Modal, …)
└── webframe_example.html       WebFrameSrcdoc example used by a2ui_config
```

## `data_export/` — downloadable files + export toolset

Renders agent output to files and hands them back as ADK artifacts / GCS-signed
URLs. `create_export_toolset()` returns the agent-callable FunctionTools.

```
data_export/
├── __init__.py        public API: create_export_toolset()
├── tools.py           ADK FunctionTools: chart→PNG, analysis→PDF, analysis→PPTX
├── document.py        markdown → PDF (reportlab) / PPTX (python-pptx)
├── png/chart.py       Vega-Lite → PNG / HTML  (a2ui uses render_vega_to_html)
├── gcs_upload.py      upload + signed URLs    (a2ui uses upload_html_and_sign)
├── _gcs.py            GCS client / signing internals
├── _artifact.py       save_as_adk_artifact, safe_filename (a2ui uses these)
├── _sessions.py       session helpers
└── _types.py          shared types
```

## `telemetry/` — per-turn time / tokens / cache / cost

Instruments every user-facing turn: elapsed / LLM / tool / other **time**,
**tokens** per tier (triage / fast / deep, split input/output), **cache-hit**
tokens, and **cost** per tier. Numbers are always logged to Cloud Logging under
`[TELEMETRY]`; when `SHOW_TELEMETRY_FOOTER=1` it also appends a muted footer to
the final response plus a machine-readable `<!--agent_telemetry ...-->` comment
(the cross-A2A wire contract other agents parse).

It is **agent-agnostic** — it imports nothing from `a2ui`, `data_export`, or any
agent's own modules. Two `configure_*` hooks inject what it would otherwise have
to import:

- `configure_model_tiers(triage, fast, deep, agent_name_tiers=None)` — the model
  name each tier resolves to, so a call is bucketed to the right tier. Defaults
  to the `TRIAGE_MODEL` / `FAST_MODEL` / `DEEP_MODEL` env vars (with `AGENT_MODEL`
  fallback) until an agent overrides them exactly.
- `configure_bq_ledger(reset, last_drained)` — *optional*; agents that query
  BigQuery register their ledger callables so the per-tool footer shows query
  economics (queries / bytes billed / cost). Agents that don't query BQ never
  call it and the BQ line is simply omitted.

Tier resolution keys off the ADK agent name first (a `<agent>_fast` / `<agent>_deep`
sub-agent maps to that tier automatically; register other names via
`agent_name_tiers`), then falls back to matching the call's model against the
configured tier models.

```
telemetry/
├── __init__.py       public API: the four callbacks + configure_model_tiers /
│                     configure_bq_ledger + PRICING + compute_cost
└── turn_telemetry.py the callbacks, per-tier token/cost accounting, PRICING
                      table, footer + machine-comment builders
```

Runtime deps: only `google-adk` + `google-genai` (already required by any ADK
agent). No extra pip deps, no GCS.

### Wiring an agent (telemetry)

```python
from shared_libs.telemetry import (
    configure_model_tiers,
    telemetry_before_model_callback, telemetry_after_model_callback,
    telemetry_before_tool_callback, telemetry_after_tool_callback,
)
from .shared.model_config import TRIAGE_MODEL, FAST_MODEL, DEEP_MODEL

configure_model_tiers(TRIAGE_MODEL, FAST_MODEL, DEEP_MODEL)  # once, at import

# Optional — only if the agent queries BigQuery:
from shared_libs.telemetry import configure_bq_ledger
from .tools.bigquery.client import reset_drained, last_drained_ledger
configure_bq_ledger(reset=reset_drained, last_drained=last_drained_ledger)

root_agent = Agent(
    ...,
    before_tool_callback=telemetry_before_tool_callback,
    after_tool_callback=telemetry_after_tool_callback,
)
```

Chain the model-side callbacks around whatever the agent already runs in its
before/after-model callbacks — telemetry LAST in the after-model chain so its
footer lands beneath any correction notes. See sales-merch's `agent.py`
(`_chained_after_callback`) for the reference chain.

## `caching/` — context-cache config + turn-1 pre-warming

Two independent, agent-agnostic pieces every ADK data agent wants:

- **`build_app(root_agent, app_name=...)`** — the canonical ADK `App` wrapper +
  `ContextCacheConfig`, built from the `CONTEXT_CACHE_*` env vars. Replaces the
  near-byte-identical per-agent `shared/app_builder.py` copies; the only
  per-agent value (the App name) is an argument. Returns a bare `App` when
  `CONTEXT_CACHE_ENABLED=0`.
- **`build_prewarm_before_callback(gate=None)`** / **`inject_prewarm_metadata()`**
  — create the Vertex context cache on the **first** turn instead of the second.
  ADK's cache manager skips cache creation on turn 1 (no prior token count), so
  the first (often deep) query of every fresh session otherwise pays the full
  uncached prompt (~$0.35 on a ~70 KB prompt). Injecting a fingerprint-only
  `CacheMetadata` + a synthetic token count makes ADK create the cache
  immediately. This is pure ADK mechanics — independent of prompt structure, so
  it drops into any agent unchanged.

```
caching/
├── __init__.py       public API (build_app, build_context_cache_config,
│                     build_prewarm_before_callback, inject_prewarm_metadata,
│                     should_prewarm, prewarm_enabled)
├── context_cache.py  App wrapper + ContextCacheConfig from CONTEXT_CACHE_* env
└── prewarm.py        turn-1 CacheMetadata injection + before-model callback
```

Env: `CONTEXT_CACHE_ENABLED` (1), `CONTEXT_CACHE_MIN_TOKENS` (4096),
`CONTEXT_CACHE_TTL_SECONDS` (1800), `CONTEXT_CACHE_INTERVALS` (10),
`CACHE_PRE_WARM_ENABLED` (1). Runtime deps: only `google-adk` + `google-genai`.

### Wiring an agent (caching)

```python
from shared_libs.caching import build_app, build_prewarm_before_callback

root_app = build_app(root_agent, app_name="my_copilot")  # keep the name stable

# Pre-warm the cold turn. Chain this into the agent's before-model callback;
# the optional gate restricts warming (e.g. deep tier only, skip a provider
# whose prompt isn't cacheable):
prewarm_cb = build_prewarm_before_callback(
    gate=lambda ctx, req: (ctx.agent_name or "").endswith("_deep"),
)
# ... inside the chained before_model_callback: prewarm_cb(callback_context, llm_request)
```

**Getting the full benefit is prompt-side, not this library.** Cache value scales
with hit rate, which the agent's prompt controls: keep volatile content (the
date, any dynamic value) at the **tail** of the system instruction; minimize the
number of distinct instruction shapes (one per tier × render-mode); order layers
by change-rate (static schema/KPI first, mode/report last). This library is the
mechanism; those conventions are the hit rate.

## Canonical vs. vendored

- **Canonical source of truth:** this repo (`arizona-copilot-shared-libs`), org GitHub.
- **Vendored copy:** the whole `shared_libs/` tree is copied into each agent repo
  at `<agent_repo>/shared_libs/`. Agents import `from shared_libs.a2ui import ...`
  and `from shared_libs.data_export import ...`.
- **Never edit the vendored copies.** Change here, then re-sync — see
  [VENDORING.md](VENDORING.md).

This vendored model (vs. a cross-repo import) is deliberate: it matches each
agent's existing `extra_packages=["./shared_libs"]` deploy bundling and avoids
the cross-repo import fragility that broke an earlier approach.

## Wiring an agent (charting)

```python
from shared_libs.a2ui import (
    build_combined_before_model_callback,   # download handler + chart_id inject
    build_a2ui_after_model_callback,        # parse / validate / render A2UI
)

root_agent = Agent(
    ...,
    before_model_callback=build_combined_before_model_callback(),
    after_model_callback=build_a2ui_after_model_callback(),
)
```

Telemetry is intentionally **decoupled** — a2ui never imports the `telemetry`
package. If an agent wants telemetry, it vendors `shared_libs/telemetry` too and
chains its callbacks around these two (see "Wiring an agent (telemetry)" above).

Add `A2UI_GUIDELINES_CORE.md` into the agent's skill/prompt and replace the
domain examples with that agent's own. Prefer VIP's proven template (which names
no catalog) — it is the one verified to render in Gemini Enterprise.

## Runtime dependencies (must exist in the target agent)

See `requirements.txt`. Put the pip deps in the agent's `requirements.txt` **and**
the inline deploy requirements in `deploy.py` / `deploy_a2a.py`, and keep
`"./shared_libs"` in `extra_packages`. Downloads / interactive links also need a
GCS bucket + service-account signing config (the same config the existing
charting uses).

## Provenance

Built from the **sales-merch** agent's modules (the most-evolved, superset
implementation) — telemetry already decoupled, richer than VIP's fork. The
merged `A2UI_GUIDELINES_CORE.md` combines VIP's gating discipline + canonical
download template with sales-merch's worked chart-type examples. The A2UI catalog
was renamed from `sales_copilot_combined` to the domain-neutral
`arizona_copilot_combined`. See the project audit doc
`a2ui_charting_consolidation_and_iri_enablement.md` for the diff analysis.
