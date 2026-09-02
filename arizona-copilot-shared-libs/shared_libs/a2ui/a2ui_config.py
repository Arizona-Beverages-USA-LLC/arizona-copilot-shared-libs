"""
A2UI Configuration — Shared across all agents
=================================================
Sets up the A2uiSchemaManager with a single combined catalog that includes
every Basic v0.8 component PLUS two custom components (VegaChart and
WebFrameSrcdoc) and generates the A2UI system prompt addendum that teaches
the LLM how to output valid A2UI JSON alongside its text responses.

## CUSTOM CATALOG — confirmed working

As of April 18, 2026, Gemini Enterprise renders A2UI CUSTOM catalog
components inline, not just Basic. VegaChart (Vega-Lite spec) renders as
actual interactive charts in the GE chat window. Tested empirically with
bar, line, and time-series charts — all render correctly.

Our combined catalog (`custom_catalog.json`) defines:
  - VegaChart      — takes a Vega-Lite `spec` object, for interactive
                     charts (bar/line/scatter/etc.) — WORKING in GE.
  - WebFrameSrcdoc — takes an `html` string, rendered inside an iframe.
                     Under test as of April 18.

IMPORTANT — schema_modifiers=[remove_strict_validation]:
  The bundled v0.8 standard catalog defines every component with
  `"additionalProperties": false`, which rejects legitimate but not-
  enumerated properties like `weight` (documented as a per-component
  flex-grow property on https://a2ui.org/reference/components/ under
  "Common Properties") and `accessibility`. Google's own restaurant_finder
  sample uses `remove_strict_validation` to strip the strict flag from
  the schema before validation. We do the same so Gemini is allowed to
  use `weight` for column sizing, which prevents long customer names
  from word-wrapping.

Correct import paths for a2ui-agent-sdk 0.2.1:
  a2ui.schema.manager            -> A2uiSchemaManager
  a2ui.schema.catalog            -> CatalogConfig
  a2ui.schema.catalog_provider   -> FileSystemCatalogProvider
  a2ui.schema.constants          -> VERSION_0_8
  a2ui.schema.common_modifiers   -> remove_strict_validation
  a2ui.a2a.parts                 -> create_a2ui_part, parse_response_to_parts
  a2ui.a2a.extension             -> get_a2ui_agent_extension
"""

import os

# =============================================================================
# A2UI KILL SWITCH -- set A2UI_ENABLED=0 to force plain-markdown output
# =============================================================================
# A2UI parts are rendered by Gemini Enterprise. On the agent-to-agent (A2A)
# channel they are NOT delivered: the artifact carries text parts only, so any
# data that lives exclusively inside a VegaChart or a Card/Row table is dropped
# on the wire. The caller then receives a sentence like "the comparison is
# shown below" with nothing below it -- the numbers were fetched, formatted
# into a component, and thrown away.
#
# deploy_a2a.py sets this to "0" for the same reason it hardcodes
# SHOW_TELEMETRY_FOOTER="0": both are HUMAN affordances that misfire when the
# consumer is a program. deploy.py leaves it unset, so the human-facing
# Gemini Enterprise deployment keeps its native charts.
A2UI_ENABLED = os.environ.get("A2UI_ENABLED", "1").strip().lower() not in (
    "0", "false", "no", "off",
)

from a2ui.schema.constants import VERSION_0_8
from a2ui.schema.manager import A2uiSchemaManager
from a2ui.schema.catalog import CatalogConfig
from a2ui.schema.catalog_provider import FileSystemCatalogProvider
from a2ui.schema.common_modifiers import remove_strict_validation


# =============================================================================
# Path to the combined catalog JSON (colocated in this folder)
# =============================================================================
# This is a SINGLE catalog that includes every Basic v0.8 component (Text,
# Card, Column, Row, List, Tabs, Button, Divider, Icon, Image, Video,
# AudioPlayer, Modal, CheckBox, TextField, DateTimeInput, MultipleChoice,
# Slider) PLUS two custom components:
#
#   - VegaChart      — Vega-Lite chart specification
#   - WebFrameSrcdoc — HTML iframe widget
#
# We ship a single combined catalog (rather than Basic + a custom extension
# catalog) because A2uiSchemaManager._select_catalog() returns the FIRST
# supported catalog when no client capabilities are provided, which means
# the validator would only accept Basic components from a multi-catalog
# setup. Merging into one catalog guarantees the validator accepts
# everything we need.
#
# Trade-off: if the SDK's v0.8 Basic catalog is updated, we don't pick up
# the changes automatically. Since we're pinned to VERSION_0_8, this is
# acceptable — the v0.8 schema is frozen by the A2UI spec.
#
# WINDOWS PATH GOTCHA: We construct CatalogConfig() directly instead of
# using CatalogConfig.from_path() because the latter passes the path
# through urllib.parse.urlparse(), which on Windows interprets the drive
# letter (e.g. "C:") as a URL scheme and raises "Unsupported catalog URL
# scheme". FileSystemCatalogProvider takes the raw path. Works on both
# Windows and Linux (Agent Engine runtime).
# =============================================================================
_CUSTOM_CATALOG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "custom_catalog.json",
)


# =============================================================================
# Path to the WebFrameSrcdoc example HTML (kept as a separate file so the
# Python source isn't littered with escaped quotes and plus-concatenation
# operators that collide with Python string parsing).
# =============================================================================
_WEBFRAME_EXAMPLE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "webframe_example.html",
)


def _load_webframe_example_html() -> str:
    """Load the WebFrameSrcdoc example HTML as a single-line string for
    embedding in the system prompt. Falls back to an empty string if the
    file is missing so the config still imports."""
    try:
        with open(_WEBFRAME_EXAMPLE_PATH, "r", encoding="utf-8") as f:
            raw = f.read()
        # Flatten to one line by removing inter-tag whitespace; the browser
        # doesn't care about pretty-printing and this makes the embedded
        # example fit inside the system prompt without looking like a
        # multi-line blob.
        return " ".join(raw.split())
    except FileNotFoundError:
        return ""


_WEBFRAME_EXAMPLE_HTML = _load_webframe_example_html()


# =============================================================================
# SCHEMA MANAGER — single combined catalog
# =============================================================================
schema_manager = A2uiSchemaManager(
    version=VERSION_0_8,
    catalogs=[
        CatalogConfig(
            name="arizona_copilot_combined",
            provider=FileSystemCatalogProvider(_CUSTOM_CATALOG_PATH),
        ),
    ],
    schema_modifiers=[remove_strict_validation],
)


# =============================================================================
# A2UI INSTRUCTION FOR DATA AGENTS
# =============================================================================
# Note: the {WEBFRAME_EXAMPLE_HTML} placeholder is filled in at import time
# with the actual HTML from webframe_example.html, so the LLM sees a
# concrete worked example rather than a reference.
_DATA_AGENT_UI_DESCRIPTION_TEMPLATE = """
Use these patterns for different result types:

**Tabular results (multiple rows):**
- Wrap in a Card
- Use a Text component with variant h3 for the title
- Use a Column with Row children for table rows
- First Row is the header with bold Text (variant h5)
- Subsequent Rows are data rows with body Text
- Include a Divider between header and data
- Control column widths with the per-component `weight` property
  (flex-grow value; higher weight = more horizontal space). Use this
  to prevent long text like customer names from word-wrapping into
  multiple lines. Typical pattern: name column weight=3, numeric
  columns weight=1 each.

**Single KPI or summary:**
- Use a Card with a Column
- Text h2 for the metric value
- Text caption for the label

**P&L Waterfall:**
- Use a Card with Tabs (one tab per stage)
- Each tab has a Column of Rows showing line items

**Comparison (e.g. YoY):**
- Use a Card with a Column
- Text h3 for the title
- Side-by-side Rows for period comparison

**Charts (bar, line, scatter, etc.):**
- Use a VegaChart component.
- The `spec` property is a Vega-Lite JSON spec object. At minimum include:
    - `data`: {"values": [<array of data row objects>]}
    - `mark`: "bar" | "line" | "point" | "area"
    - `encoding`: mapping of visual channels (x, y, color, etc.) to data fields
    - `width`: integer pixel width. Use at least 600. For horizontal bar charts
      with long category labels, use 700.
    - `height`: integer pixel height. 300 for most charts; 400+ for horizontal
      bars with 10+ categories so each row has breathing room.
    - `title`: short title string (optional but nice to have)
- Use VegaChart ONLY when the user explicitly asks for a chart/graph/visualization.
  For plain data display, use the Card + Column + Row table pattern above.

**HORIZONTAL vs VERTICAL BAR CHART — CHOOSE DELIBERATELY.**
For categorical bar charts, choose orientation based on label length:
  - Short labels (<=5 chars like month abbreviations, years, brand codes) -> VERTICAL
    (category on x-axis, value on y-axis).
  - Long labels (customer names, SKU descriptions, product titles) -> HORIZONTAL
    (category on y-axis, value on x-axis). Horizontal avoids rotated or
    truncated x-axis labels.

**Example 1 — HORIZONTAL bar chart (use for top-N customers / SKUs):**
    {
      "title": "Top 10 Customers by Cases Sold (2025)",
      "data": {"values": [
        {"customer": "WAL-MART STORES", "cases": 25277436},
        {"customer": "AZ METRO DISTRIBUTORS LLC", "cases": 6625747}
      ]},
      "mark": "bar",
      "encoding": {
        "y": {
          "field": "customer",
          "type": "nominal",
          "sort": "-x",
          "axis": {"title": null, "labelLimit": 220, "labelPadding": 6}
        },
        "x": {
          "field": "cases",
          "type": "quantitative",
          "axis": {"title": "Cases Sold", "format": "~s"}
        },
        "tooltip": [
          {"field": "customer", "title": "Customer"},
          {"field": "cases", "title": "Cases Sold", "format": ","}
        ]
      },
      "width": 700,
      "height": 400
    }
  Key details:
    - `"sort": "-x"` on the y-channel sorts categories by the x-value, descending.
    - `"labelLimit": 220` widens the label column so long customer names don't
      truncate or overlap the bars. Default (150) is too narrow.
    - `"format": "~s"` on the quantitative axis shows "25M" instead of "25000000".
    - `"axis": {"title": null}` on the category channel hides a redundant
      axis title (the chart title already conveys it).
    - `"tooltip"` inside `encoding` is an ARRAY of explicit fields with
      pretty titles and per-field format strings. This gives a cleaner
      hover popup than `mark.tooltip = {content: "data"}` (which would
      expose internal fields like `cases_start` / `cases_end`).

**Example 2 — VERTICAL bar chart with data labels (use for month/quarter trends, short labels):**
    {
      "title": "Net Revenue by Quarter (2025)",
      "data": {"values": [
        {"quarter": "Q1", "revenue": 34200000},
        {"quarter": "Q2", "revenue": 41800000},
        {"quarter": "Q3", "revenue": 39100000},
        {"quarter": "Q4", "revenue": 38500000}
      ]},
      "encoding": {
        "x": {
          "field": "quarter",
          "type": "nominal",
          "axis": {"title": null, "labelAngle": 0}
        },
        "y": {
          "field": "revenue",
          "type": "quantitative",
          "axis": {"title": "Net Revenue ($)", "format": "$~s"}
        },
        "tooltip": [
          {"field": "quarter", "title": "Quarter"},
          {"field": "revenue", "title": "Net Revenue", "format": "$,"}
        ]
      },
      "layer": [
        {"mark": {"type": "bar"}},
        {
          "mark": {
            "type": "text",
            "align": "center",
            "baseline": "bottom",
            "dy": -4,
            "fontSize": 11,
            "fontWeight": 600
          },
          "encoding": {
            "text": {"field": "revenue", "type": "quantitative", "format": "$.2~s"}
          }
        }
      ],
      "width": 600,
      "height": 300
    }
  Key details:
    - The outer `mark` is REPLACED by a `layer` array. The first layer
      renders the bars, the second layer renders the data-label text
      above each bar.
    - `"mark": {"type": "text", "dy": -4, "baseline": "bottom"}` places
      the label just above the bar's top. Adjust `dy` by a pixel or two
      if labels overlap the bar edge.
    - `"format": "$.2~s"` on the text encoding formats values like
      `$41.8M` — two significant figures, SI suffix, dollar prefix.
      For non-currency values use `".2~s"` (no dollar sign) or `","`
      (comma-separated full integer).
    - The bar layer inherits x/y encoding from the outer `encoding`
      block; the text layer adds its own `text` encoding on top.
    - `"labelAngle": 0` keeps x-labels horizontal; Vega-Lite sometimes
      rotates them by default.
    - `"format": "$~s"` prefixes the dollar sign and uses SI units
      ("$41M") on the axis; in the tooltip, `"format": "$,"` shows
      comma-separated full dollars ("$41,800,000") for precision on
      hover.

**When to include data labels vs. when to skip them:**
  - INCLUDE labels on bar charts with <= 12 bars. The space above each
    bar is enough to read a short `$41.8M`-style label without overlap.
  - SKIP labels (use plain `"mark": "bar"` without the layer array) on
    bar charts with many bars (13+), monthly trends across multiple
    years, or any chart where labels would visually collide.
  - For HORIZONTAL bar charts, labels go to the RIGHT of the bars.
    Use `"align": "left", "baseline": "middle", "dx": 4` instead of
    the vertical settings above. Apply the same layer pattern.

**Example 3 — LINE chart (use for time series: monthly trends, YoY comparison):**
    {
      "title": "Monthly Cases Sold — 2024 vs 2025",
      "data": {"values": [
        {"month": "Jan", "year": "2024", "cases": 1820000},
        {"month": "Jan", "year": "2025", "cases": 1950000},
        {"month": "Feb", "year": "2024", "cases": 1740000},
        {"month": "Feb", "year": "2025", "cases": 1880000}
      ]},
      "mark": {"type": "line", "point": true},
      "encoding": {
        "x": {
          "field": "month",
          "type": "ordinal",
          "sort": ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"],
          "axis": {"title": null, "labelAngle": 0}
        },
        "y": {
          "field": "cases",
          "type": "quantitative",
          "axis": {"title": "Cases Sold", "format": "~s"}
        },
        "color": {"field": "year", "type": "nominal", "title": "Year"},
        "tooltip": [
          {"field": "month", "title": "Month"},
          {"field": "year", "title": "Year"},
          {"field": "cases", "title": "Cases Sold", "format": ","}
        ]
      },
      "width": 650,
      "height": 320
    }
  Key details:
    - `"mark": {"type": "line", "point": true}` draws a line WITH dots at
      each data point — easier to read than bare lines.
    - `"type": "ordinal"` + explicit `sort` array forces Jan–Dec order
      instead of alphabetical.
    - `color` channel produces one line per year, with an auto-legend.

**Axis & label best practices (apply to all charts):**
  - ALWAYS enable hover tooltips with an explicit `tooltip` array inside
    `encoding`. List each field you want to show, with a `title` (pretty
    display name) and optional `format` string. See the three examples
    above for the pattern. Tooltips make charts much more readable for
    dense data. Do NOT use `"tooltip": {"content": "data"}` shortcut at
    the mark level — that exposes internal Vega-Lite fields like
    `cases_start` / `cases_end` that confuse non-technical users.
  - Tooltip format strings:
      `","`   — comma-separated integer ("5,177,087")
      `"$,"`  — dollar with commas ("$142,300,000")
      `".1%"` — percent with 1 decimal ("34.2%")
      `",.2f"`— comma number with 2 decimals ("5,177,087.00")
  - Use `"format": "~s"` on quantitative axes to show SI-suffix numbers (25M, 1.4B).
    Add `"$"` prefix for money: `"$~s"`.
  - Use `"labelLimit": 220` on category axes with long labels.
  - Set `"title": null` on axes when the chart `title` already conveys it,
    to reduce visual clutter.
  - Use `"labelAngle": 0` on categorical x-axes to prevent rotated labels.

**Embedded HTML widgets (WebFrameSrcdoc):**
- Use a WebFrameSrcdoc component for interactive widgets that don't fit
  any other component type — calculators, what-if scenarios, embedded
  dashboards, or custom visualizations beyond what Vega-Lite supports.
- The `html` property takes a complete HTML document (can include `<style>`
  and `<script>` tags). The client renders it inside a sandboxed iframe.
- The `height` property sets the iframe's display height in pixels.
- Use WebFrameSrcdoc ONLY when the user explicitly asks for an "interactive
  widget," "embedded calculator," "what-if tool," or similar. For everything
  else, prefer native A2UI components (tables, cards, VegaChart).
- Keep the HTML self-contained and under ~4 KB. Do not include external
  resources (no <link>, no <img src>, no external <script>). Inline styles
  and scripts only.

**Example — WebFrameSrcdoc: interactive profit margin calculator**
The component shape is:
    {
      "component": {
        "WebFrameSrcdoc": {
          "html": {"literalString": "<complete self-contained HTML document>"},
          "height": 320
        }
      }
    }
Use HTML like the following as a template (two number inputs, live
recalculation, formatted output):

    {WEBFRAME_EXAMPLE_HTML}
"""


DATA_AGENT_A2UI_INSTRUCTION = schema_manager.generate_system_prompt(
    role_description=(
        "You are a data retrieval agent that queries Tableau data sources "
        "and returns results. When you have data results, present them using "
        "A2UI components so they render as rich UI in the chat."
    ),
    workflow_description=(
        "After querying data, format the results as A2UI JSON. "
        "Use a Card containing a Column with a Text heading and a "
        "Row/Column table layout for tabular results. "
        "For single KPI values, use a Card with large Text. "
        "When the user explicitly asks for a chart, graph, or visualization, "
        "emit a VegaChart component with a Vega-Lite specification. "
        "When the user asks for an interactive widget, calculator, or "
        "what-if tool, emit a WebFrameSrcdoc component with a complete "
        "self-contained HTML document."
    ),
    ui_description=_DATA_AGENT_UI_DESCRIPTION_TEMPLATE.replace(
        "{WEBFRAME_EXAMPLE_HTML}",
        _WEBFRAME_EXAMPLE_HTML or "(example HTML unavailable — the file webframe_example.html was not found at import time)",
    ),
    include_schema=True,
    include_examples=True,
    allowed_components=[
        "Text", "Card", "Column", "Row", "Divider",
        "Tabs", "Button", "List", "Image", "Icon",
        "VegaChart", "WebFrameSrcdoc",
    ],
)


# =============================================================================
# A2UI INSTRUCTION FOR ROLE AGENTS
# =============================================================================
ROLE_AGENT_A2UI_INSTRUCTION = schema_manager.generate_system_prompt(
    role_description=(
        "You are a business analyst that provides insights and recommendations. "
        "Present your analysis using A2UI components for rich UI rendering."
    ),
    workflow_description=(
        "After analyzing data, format key insights as A2UI Cards. "
        "Use Cards for insight summaries, Tabs for multi-section analysis, "
        "and Text with appropriate variants for hierarchy."
    ),
    ui_description="""
Use these patterns:

**Insight summary:**
- Card containing a Column with:
  - Text h3 for insight title
  - Divider
  - Text body for the analysis narrative
  - Row of metric Cards for key numbers

**Recommendations:**
- Card with Column containing:
  - Text h3 "Recommendations"
  - Numbered Text items for each recommendation

**Multi-section analysis (e.g. full P&L review):**
- Card with Tabs, one tab per analysis section
""",
    include_schema=True,
    include_examples=True,
    allowed_components=[
        "Text", "Card", "Column", "Row", "Divider",
        "Tabs", "Button", "List", "Icon",
        "VegaChart", "WebFrameSrcdoc",
    ],
)


# =============================================================================
# MARKDOWN-ONLY OVERRIDE (A2UI_ENABLED=0)
# =============================================================================
# Replaces the A2UI system-prompt addendum entirely. The model is never taught
# the component schema, so it never emits A2UI JSON, so the a2ui_bridge
# after-model callback finds nothing to convert and passes the response
# through untouched. No change to agent.py is required.
_MARKDOWN_ONLY_INSTRUCTION = """
OUTPUT FORMAT: MARKDOWN ONLY. Do not emit A2UI JSON of any kind.

You are answering another PROGRAM over an agent-to-agent channel, not a human
in a chat window. Rich UI components are not delivered on this channel. Any
number that exists only inside a chart component is lost in transit, and the
caller receives prose describing data it cannot see.

Therefore:

  * ALWAYS return the data itself as a GitHub-flavored MARKDOWN TABLE:
    a header row, a separator row, then one row per record. This table is the
    ONLY channel the caller can read. If you fetched rows, they belong here.
  * If the user asks for a chart, graph, plot, trend line, or visualization,
    you STILL return a markdown table. The CALLER renders the chart from your
    table. Do not attempt to draw it, do not apologize for not drawing it,
    and do not claim that you have drawn it.
  * NEVER write "shown below", "see the chart above", "as visualized", or any
    similar pointer unless the thing you are pointing at is the markdown table
    you just wrote.
  * Keep prose short: one or two sentences of context before the table, and
    any genuine data caveat after it.
  * If a period has NO DATA YET -- the current year is complete only through
    last month, say -- leave that cell EMPTY. Do NOT write 0. A zero is a real
    measurement meaning "none"; an empty cell means "not yet known". The
    caller charts them very differently: a zero draws a cliff to the axis and
    makes the year look like a collapse, while an empty cell leaves an honest
    gap in the line.
  * One column per dimension and one per measure. Put the period in the column
    header (for example "2025 Gross Profit") rather than describing it in
    prose. Keep the column count to the minimum the question requires.
"""

if not A2UI_ENABLED:
    DATA_AGENT_A2UI_INSTRUCTION = _MARKDOWN_ONLY_INSTRUCTION
    ROLE_AGENT_A2UI_INSTRUCTION = _MARKDOWN_ONLY_INSTRUCTION
    print(
        "[A2UI] DISABLED via A2UI_ENABLED=0 -- markdown-table output enforced",
        flush=True,
    )
else:
    print("[A2UI] enabled -- component output active", flush=True)
