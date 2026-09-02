# arizona-copilot-shared-libs

Canonical **shared libraries** for the Arizona Beverages copilots (VIP, IRI,
Sales & Merchandising, and future agents). One source of truth, **vendored**
into each agent repo — replacing the drifted per-agent copies that lived in
each agent's `shared/`.

The repo ships one tree, `shared_libs/`, with two sibling packages:

```
shared_libs/
├── a2ui/          A2UI charting (emit → render → download buttons)
└── data_export/   PNG/PDF/PPTX render + GCS sign + ADK export toolset
```

Each agent vendors the **whole `shared_libs/` tree** at `<agent_repo>/shared_libs/`
and imports `from shared_libs.a2ui import ...` / `from shared_libs.data_export import ...`.

## Why both live here (and why as siblings)

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

Telemetry is intentionally **decoupled** — a2ui never imports `turn_telemetry`.
If an agent wants telemetry, it chains its own telemetry callbacks around these
two (see IRI's `agent.py` for the pattern).

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
