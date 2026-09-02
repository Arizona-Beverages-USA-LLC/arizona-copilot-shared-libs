"""
Vega-Lite spec -> PNG bytes via vl-convert-python, with production-quality
defaults applied automatically.

== Architecture: "Conventions in code, taste in prompt, noticing in the model" ==

This module is the Layer 1 of that architecture: deterministic, hardcoded
conventions that every chart the Copilot exports should have. We don't
argue with the model about these; we just apply them after the model has
chosen the chart shape and data.

What's hardcoded here (the conventions):
    * Data labels on single-series bar and line charts (auto-detected
      and auto-injected as a layered text mark). Skipped when the spec
      already has layers, when the chart type doesn't benefit, or when
      the model explicitly opts out.
    * Chronological sort on nominal/ordinal x-axes (preserves data
      order instead of alphabetical). Critical for Q1/Q2/Q3/Q4 style
      labels where alphabetical sort gives nonsense.
    * Arizona brand color palette as the default single-color and
      categorical range.
    * Consistent title/axis typography, weight, alignment.
    * Transparent view border, light horizontal gridlines only, no
      vertical gridlines (the standard dashboard look).
    * 800x450 canvas (16:9 fits slide content areas), 2x HiDPI scale.
    * Smart number formatting: $50M rather than $50,000,000 when
      values are in the millions.

What the model controls (taste, left to the prompt):
    * Which metric and data to chart
    * Mark type (bar vs line vs point)
    * Axis titles and chart title
    * What encoding to use for color grouping (if any)
    * What to highlight or annotate

What the model can opt out of (escape hatches):
    * `config.arizonaStyle.dataLabels: false` -> skip label injection
    * `config.arizonaStyle.palette: "none"` -> skip color defaults
    * Already-present layers -> we never touch them
    * Already-present mark.color -> kept as-is
    * Already-present x-encoding sort -> kept as-is

Install: pip install vl-convert-python
"""

from __future__ import annotations

import copy
import json
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Arizona-branded color palette
# ---------------------------------------------------------------------------
# Single-series default: a confident but not loud blue.
# Categorical palette: 5 colors that print legibly in grayscale too.

DEFAULT_SINGLE_COLOR = "#2E5C8A"   # deep professional blue
DEFAULT_CATEGORICAL = [
    "#2E5C8A",  # blue
    "#C0504D",  # rust red
    "#9BBB59",  # sage green
    "#8064A2",  # muted purple
    "#4BACC6",  # teal
]

# ---------------------------------------------------------------------------
# Canvas and typography defaults
# ---------------------------------------------------------------------------

DEFAULT_WIDTH = 800
DEFAULT_HEIGHT = 450
DEFAULT_SCALE = 2.0
DEFAULT_PPI = 144

TITLE_FONT_SIZE = 16
AXIS_TITLE_FONT_SIZE = 12
AXIS_LABEL_FONT_SIZE = 11
DATA_LABEL_FONT_SIZE = 11
DATA_LABEL_COLOR = "#1a1a1a"  # near-black for contrast against white background

# ---------------------------------------------------------------------------
# Chart type capability matrix
# ---------------------------------------------------------------------------
# Data labels make sense for discrete-x charts where each mark corresponds
# to a single labeled value. They don't make sense for:
#   * scatter plots (labels overlap and clutter)
#   * heatmaps (labels belong inside cells, different mechanism)
#   * pie/arc (already show percentages, different mechanism)
#   * area (filled region, label placement is ambiguous)
# Bar and line are the sweet spot for auto-labeling.

LABELABLE_MARK_TYPES = {"bar", "line"}
# "point" excluded on purpose: point labels tend to overlap unless carefully
# positioned. If the model wants scatter-plot labels, it should layer them
# explicitly.


# ---------------------------------------------------------------------------
# Default Vega-Lite "config" block — applied unless spec overrides it
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "background": "white",
    "view": {"stroke": "transparent"},  # drop the default border around plot area
    "title": {
        "fontSize": TITLE_FONT_SIZE,
        "fontWeight": "bold",
        "anchor": "start",       # left-aligned
        "color": "#1a1a1a",
        "offset": 12,
    },
    "axis": {
        "titleFontSize": AXIS_TITLE_FONT_SIZE,
        "titleFontWeight": "normal",
        "titleColor": "#444",
        "labelFontSize": AXIS_LABEL_FONT_SIZE,
        "labelColor": "#444",
        "gridColor": "#e0e0e0",
        "gridDash": [1, 0],
        "domainColor": "#b0b0b0",
        "tickColor": "#b0b0b0",
    },
    "axisX": {
        "grid": False,  # vertical gridlines off; only keep horizontal
        "labelAngle": 0,
    },
    "axisY": {
        "grid": True,   # horizontal gridlines on
    },
    "legend": {
        "titleFontSize": AXIS_TITLE_FONT_SIZE,
        "labelFontSize": AXIS_LABEL_FONT_SIZE,
        "orient": "top-right",
    },
    "range": {
        "category": DEFAULT_CATEGORICAL,
    },
}


# ---------------------------------------------------------------------------
# Opt-out hooks
# ---------------------------------------------------------------------------
# The model can disable specific conventions by including a config block:
#   {"config": {"arizonaStyle": {"dataLabels": false, "palette": "none"}}}
# Absence of the key means "apply the convention".

def _get_style_option(spec: dict, key: str, default: Any) -> Any:
    """Read a config.arizonaStyle.<key> opt-out value, falling back to default."""
    style = spec.get("config", {}).get("arizonaStyle", {})
    return style.get(key, default)


# ---------------------------------------------------------------------------
# Helpers for introspecting the spec
# ---------------------------------------------------------------------------

def _is_complex_spec(spec: dict) -> bool:
    """Detect specs we shouldn't auto-modify beyond config/sort.

    Faceted, repeated, concatenated, and already-layered specs are the
    model's explicit composition. We respect that and don't inject our own
    layers on top.
    """
    return any(key in spec for key in ("layer", "facet", "repeat", "hconcat", "vconcat", "concat", "spec"))


def _get_mark_type(spec: dict) -> Optional[str]:
    """Extract mark type whether mark is a string or a dict."""
    mark = spec.get("mark")
    if isinstance(mark, str):
        return mark
    if isinstance(mark, dict):
        return mark.get("type")
    return None


def _infer_numeric_format(y_encoding: dict, data_values: Optional[list]) -> Optional[str]:
    """Infer a d3-format string for data labels based on the y-encoding
    and actual data magnitudes.

    Returns formats like:
        "$,.1f"     — currency, one decimal      ($47.3)
        "$,.0f"     — currency, no decimals      ($47)
        ",.1f"      — plain, one decimal         (47.3)
        ",.0f"      — plain, no decimals         (47)
        ".1%"       — percentage, one decimal    (34.2%)

    Detection strategy:
        * Inspect y.axis.format if the model provided one — use it.
        * Otherwise check if the field name suggests currency (revenue,
          sales, cost, price, profit, margin amount) -> prefix $.
        * Check if the field name suggests percent (margin_%, _pct,
          rate, share) -> use percent format.
        * Determine decimal count from data magnitudes (small values
          need decimals, large values don't).
    """
    # The model's explicit format always wins
    axis_format = y_encoding.get("axis", {}).get("format")
    if axis_format:
        return axis_format

    # Inspect the field name for hints
    field_name = (y_encoding.get("field") or "").lower()
    is_currency = any(hint in field_name for hint in (
        "revenue", "sales", "cost", "price", "profit", "spend",
        "dollars", "amount", "value", "gmv", "arr", "mrr"
    ))
    is_percent_field = any(hint in field_name for hint in (
        "_pct", "_%", "percent", "rate", "share", "margin_pct"
    )) or "margin %" in field_name

    # Look at the data to choose decimal count
    decimals = 0
    if data_values:
        try:
            numeric_vals = []
            for row in data_values:
                if not isinstance(row, dict):
                    continue
                v = row.get(y_encoding.get("field"))
                if isinstance(v, (int, float)):
                    numeric_vals.append(float(v))
            if numeric_vals:
                max_abs = max(abs(v) for v in numeric_vals)
                # Rule of thumb: values < 100 often benefit from 1 decimal.
                # Values >= 100 rarely need decimals.
                decimals = 1 if max_abs < 100 else 0
        except Exception:
            decimals = 0

    if is_percent_field:
        # Note: percent format "%" assumes raw values are 0.xx fractions.
        # If the data is already in percent form (e.g. 34.2), use fixed decimals
        # with a trailing "%" via formatString isn't directly supported in axis
        # format strings. Simplest robust choice: treat as a plain number.
        return f",.{decimals}f"

    if is_currency:
        return f"$,.{decimals}f"

    return f",.{decimals}f"


def _extract_data_values(spec: dict) -> Optional[list]:
    """Pull inline data rows out of the spec, or None if data is not inline."""
    data = spec.get("data")
    if not isinstance(data, dict):
        return None
    values = data.get("values")
    if isinstance(values, list):
        return values
    return None


# ---------------------------------------------------------------------------
# Data label injection
# ---------------------------------------------------------------------------

def _inject_data_labels(spec: dict) -> dict:
    """If the spec is a single-mark bar or line chart without its own text
    layer, convert it to a layered spec with an auto-generated text label
    layer above each mark.

    Returns a new spec. Original is not mutated.

    No-ops when:
        * The spec is complex (already layered/faceted/repeated)
        * The mark type isn't in LABELABLE_MARK_TYPES
        * The model has set config.arizonaStyle.dataLabels = false
        * The encoding has no y field (we need something to label)
    """
    # Opt-out check first
    if not _get_style_option(spec, "dataLabels", True):
        return spec

    # Skip complex specs the model has already composed intentionally
    if _is_complex_spec(spec):
        return spec

    mark_type = _get_mark_type(spec)
    if mark_type not in LABELABLE_MARK_TYPES:
        return spec

    encoding = spec.get("encoding")
    if not isinstance(encoding, dict):
        return spec
    y_encoding = encoding.get("y")
    if not isinstance(y_encoding, dict) or not y_encoding.get("field"):
        return spec

    # Extract data rows (if inline) to inform format inference.
    data_values = _extract_data_values(spec)

    # Work on a deep copy so we don't mutate caller state
    polished = copy.deepcopy(spec)

    # The original mark (may be string or dict) — we preserve it as the first layer.
    original_mark = polished.pop("mark")

    # Figure out text label positioning + format
    text_format = _infer_numeric_format(y_encoding, data_values)
    text_encoding = {
        "text": {
            "field": y_encoding["field"],
            "type": y_encoding.get("type", "quantitative"),
            "format": text_format,
        }
    }

    # For bar charts, labels sit ABOVE the bar (dy negative)
    # For line charts, labels sit ABOVE the point (same dy)
    text_mark = {
        "type": "text",
        "align": "center",
        "baseline": "bottom",
        "dy": -6,
        "fontSize": DATA_LABEL_FONT_SIZE,
        "fontWeight": 500,
        "color": DATA_LABEL_COLOR,
    }

    # Build the layered spec: original mark first, labels on top
    # encoding is LIFTED to the outer spec so both layers inherit it.
    # Each layer may add/override its own encoding (labels add `text`).
    # The original mark is kept as-is (the outer encoding covers x/y/color).
    layer_original: dict = {"mark": original_mark}
    layer_labels: dict = {
        "mark": text_mark,
        "encoding": text_encoding,
    }

    # The outer spec loses `mark` and gets `layer`. Encoding stays at outer
    # level so all layers inherit x/y/color from it.
    polished["layer"] = [layer_original, layer_labels]
    # encoding is already present at outer level — layers inherit it.

    return polished


# ---------------------------------------------------------------------------
# Spec transformation: main pipeline
# ---------------------------------------------------------------------------

def _apply_production_defaults(spec: dict) -> dict:
    """Layer production-quality defaults onto an agent-provided spec.

    Returns a NEW dict. Does not mutate the input. The agent's explicit
    choices always win; we only fill in absent fields.

    Pipeline:
        1. Canvas dimensions (width/height)
        2. Config block merge (typography, gridlines, colors)
        3. X-axis sort preservation
        4. Default single-series color
        5. Data label injection (transforms single-mark to layered spec)
    """
    polished = copy.deepcopy(spec)

    # --- Step 1: Canvas size ---
    polished.setdefault("width", DEFAULT_WIDTH)
    polished.setdefault("height", DEFAULT_HEIGHT)

    # --- Step 2: Config merge ---
    agent_config = polished.get("config", {})
    merged_config = _deep_merge(DEFAULT_CONFIG, agent_config)
    polished["config"] = merged_config

    # --- Step 3: Preserve input order on nominal/ordinal x-axis ---
    # Unless model explicitly set a sort. Prevents "Q1 2024, Q1 2025, Q1 2026,
    # Q2 2024..." alphabetical chaos.
    encoding = polished.get("encoding", {})
    x_enc = encoding.get("x")
    if isinstance(x_enc, dict):
        x_type = x_enc.get("type")
        if x_type in ("nominal", "ordinal") and "sort" not in x_enc:
            x_enc["sort"] = None  # null in JSON = preserve data order

    # --- Step 4: Default single-series color ---
    # Only for simple bar/line/area/point without an explicit color encoding.
    mark = polished.get("mark")
    if isinstance(mark, dict):
        mark_type = mark.get("type")
    else:
        mark_type = mark
    has_color_encoding = isinstance(encoding.get("color"), dict)
    apply_palette = _get_style_option(polished, "palette", "default") != "none"
    if apply_palette and mark_type in ("bar", "line", "area", "point") and not has_color_encoding:
        if isinstance(mark, str):
            polished["mark"] = {"type": mark, "color": DEFAULT_SINGLE_COLOR}
        elif isinstance(mark, dict) and "color" not in mark:
            polished["mark"]["color"] = DEFAULT_SINGLE_COLOR

    # --- Step 5: Data label injection ---
    # This is the last step because it may restructure the spec from
    # single-mark to layered. After this, further spec edits become harder.
    polished = _inject_data_labels(polished)

    return polished


def _deep_merge(base: dict, overlay: dict) -> dict:
    """Recursive merge: overlay values win at leaves; both dicts merge at nodes.

    Used to layer agent config over our defaults so agent can override any
    specific setting without losing all the others."""
    if not isinstance(overlay, dict):
        return overlay  # non-dict overlay replaces base entirely
    out = dict(base)
    for key, overlay_val in overlay.items():
        base_val = out.get(key)
        if isinstance(base_val, dict) and isinstance(overlay_val, dict):
            out[key] = _deep_merge(base_val, overlay_val)
        else:
            out[key] = overlay_val
    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def render_vega_to_png(
    vega_lite_spec: dict | str,
    scale: float = DEFAULT_SCALE,
    ppi: int = DEFAULT_PPI,
    apply_defaults: bool = True,
) -> bytes:
    """Render a Vega-Lite spec to PNG bytes with production-quality defaults.

    Args:
        vega_lite_spec: Either a dict (preferred) or a JSON string. If dict,
            it's JSON-serialized after defaults are applied.
        scale: Rendering scale factor. Default 2.0 gives HiDPI crispness
            when embedded in slides and documents at typical sizes.
        ppi: Pixels-per-inch for print contexts. Default 144 is "high DPI".
        apply_defaults: If True (default), layer our production defaults onto
            the spec before rendering — sorted axes, gridlines, colors,
            typography, data labels. Set False only for debugging or if you
            genuinely want the agent's raw spec with no styling help.

    Returns:
        Raw PNG bytes.

    Raises:
        ImportError: If vl-convert-python isn't installed.
        ValueError: If the spec is malformed JSON (when passed as string).
        TypeError: If the spec is neither dict nor string.
    """
    try:
        import vl_convert as vlc
    except ImportError as exc:
        raise ImportError(
            "vl-convert-python is required to render PNG charts. "
            "Add it to the deploy requirements and redeploy. "
            "(pip install vl-convert-python)"
        ) from exc

    # Normalize input -> dict (so we can apply defaults)
    if isinstance(vega_lite_spec, str):
        try:
            spec_dict = json.loads(vega_lite_spec)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"vega_lite_spec is not valid JSON: {exc.msg} at "
                f"line {exc.lineno}, col {exc.colno}"
            ) from exc
    elif isinstance(vega_lite_spec, dict):
        spec_dict = vega_lite_spec
    else:
        raise TypeError(
            f"vega_lite_spec must be dict or str, got {type(vega_lite_spec).__name__}"
        )

    if apply_defaults:
        spec_dict = _apply_production_defaults(spec_dict)

    spec_str = json.dumps(spec_dict)
    return vlc.vegalite_to_png(vl_spec=spec_str, scale=scale, ppi=ppi)


# ---------------------------------------------------------------------------
# Internal helper: normalize+default the spec once. All renderers below share
# this pipeline so inline A2UI / PNG / SVG / HTML / PDF all render from the
# same styled Vega-Lite spec.
# ---------------------------------------------------------------------------

def _prepare_spec(
    vega_lite_spec,
    apply_defaults: bool = True,
) -> tuple[dict, str]:
    """Normalize input to a dict, apply production defaults, return
    (dict, json_string).

    Returns both the dict form and the serialized JSON string because
    some renderers want one and some want the other.
    """
    if isinstance(vega_lite_spec, str):
        try:
            spec_dict = json.loads(vega_lite_spec)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"vega_lite_spec is not valid JSON: {exc.msg} at "
                f"line {exc.lineno}, col {exc.colno}"
            ) from exc
    elif isinstance(vega_lite_spec, dict):
        spec_dict = vega_lite_spec
    else:
        raise TypeError(
            f"vega_lite_spec must be dict or str, got {type(vega_lite_spec).__name__}"
        )

    if apply_defaults:
        spec_dict = _apply_production_defaults(spec_dict)

    return spec_dict, json.dumps(spec_dict)


# ---------------------------------------------------------------------------
# SVG renderer
# ---------------------------------------------------------------------------

def render_vega_to_svg(
    vega_lite_spec,
    apply_defaults: bool = True,
) -> bytes:
    """Render a Vega-Lite spec to SVG bytes.

    SVG is vector output — scales cleanly to any size, small file size,
    editable in Illustrator/Figma. Same production-quality styling as PNG.
    NOTE: SVG output is STATIC — hover tooltips and interactive features
    are Vega runtime behaviors that don't exist in a plain SVG file. For
    interactivity, use render_vega_to_html instead.

    Returns UTF-8 bytes of the SVG document (so callers can pass directly
    to save_as_adk_artifact which expects bytes).
    """
    try:
        import vl_convert as vlc
    except ImportError as exc:
        raise ImportError(
            "vl-convert-python is required to render SVG charts. "
            "(pip install vl-convert-python)"
        ) from exc

    _, spec_str = _prepare_spec(vega_lite_spec, apply_defaults)
    svg_text = vlc.vegalite_to_svg(vl_spec=spec_str)
    return svg_text.encode("utf-8")


# ---------------------------------------------------------------------------
# HTML renderer (interactive)
# ---------------------------------------------------------------------------

def render_vega_to_html(
    vega_lite_spec,
    title: str | None = None,
    apply_defaults: bool = True,
) -> bytes:
    """Render a Vega-Lite spec as a standalone interactive HTML file.

    Produces a fully self-contained HTML document (Vega-Lite library + spec
    + data inline). The user can double-click to open in any browser and
    get the chart WITH full hover tooltips, zoom, pan — all the Vega
    runtime interactivity. Same styling as PNG/SVG variants.

    This is the download format that best preserves the "interactive chart"
    experience for charts saved from chat.

    Args:
        vega_lite_spec: Vega-Lite spec dict or JSON string.
        title: Optional browser tab title / H1 heading above the chart.
            Defaults to "Chart" if not provided.
        apply_defaults: Whether to apply the production-defaults pipeline.

    Returns:
        UTF-8 bytes of the complete HTML document.
    """
    _, spec_str = _prepare_spec(vega_lite_spec, apply_defaults)
    page_title = title or "Chart"

    # Build the standalone HTML. We load Vega, Vega-Lite, and Vega-Embed
    # from the official jsDelivr CDN (standard Vega recommendation). The
    # spec is embedded inline so the file is self-contained apart from
    # the JS libraries.
    #
    # If the user opens this file offline with no internet, the Vega
    # libraries won't load and the chart won't render. That's a tradeoff
    # of file size vs. offline support. If we need truly offline HTML in
    # future we'd inline the minified Vega bundles (adds ~300KB to each file).
    html_doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>{_html_escape(page_title)}</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <script src="https://cdn.jsdelivr.net/npm/vega@5"></script>
  <script src="https://cdn.jsdelivr.net/npm/vega-lite@5"></script>
  <script src="https://cdn.jsdelivr.net/npm/vega-embed@6"></script>
  <style>
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      max-width: 1000px;
      margin: 20px auto;
      padding: 0 20px;
      color: #1a1a1a;
    }}
    h1 {{
      font-size: 20px;
      font-weight: 600;
      margin-bottom: 16px;
    }}
    #chart-container {{
      background: white;
      padding: 16px;
      border-radius: 8px;
      border: 1px solid #e0e0e0;
    }}
    footer {{
      margin-top: 24px;
      font-size: 12px;
      color: #888;
    }}
  </style>
</head>
<body>
  <h1>{_html_escape(page_title)}</h1>
  <div id="chart-container">
    <div id="vis"></div>
  </div>
  <footer>Generated by Arizona Sales Copilot. Hover over chart elements to see details.</footer>
  <script>
    const spec = {spec_str};
    vegaEmbed('#vis', spec, {{ actions: true, renderer: 'svg' }}).catch(console.error);
  </script>
</body>
</html>
"""
    return html_doc.encode("utf-8")


def _html_escape(text: str) -> str:
    """Minimal HTML escape for interpolated values in templates."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# ---------------------------------------------------------------------------
# PDF renderer (wraps PNG in a minimal PDF)
# ---------------------------------------------------------------------------

def render_vega_to_pdf(
    vega_lite_spec,
    title: str | None = None,
    apply_defaults: bool = True,
) -> bytes:
    """Render a Vega-Lite spec as a single-page PDF containing the chart.

    Implementation: renders to PNG, then wraps the PNG bytes in a PDF
    document via reportlab. PDF is static (no hover), but is the most
    universal format for sharing charts in reports, emails, or print.

    Args:
        vega_lite_spec: Vega-Lite spec.
        title: Optional title printed at top of PDF page. Defaults to
            "Chart" or is extracted from the spec's own title if present.
        apply_defaults: Whether to apply production defaults.

    Returns:
        PDF file bytes.
    """
    # Render the chart as PNG first. All styling (labels, sort, palette)
    # is already applied inside render_vega_to_png -> _apply_production_defaults.
    png_bytes = render_vega_to_png(vega_lite_spec, apply_defaults=apply_defaults)

    # Resolve title: explicit argument wins, otherwise check the spec for
    # a top-level title, otherwise default to "Chart".
    if title is None:
        spec_dict, _ = _prepare_spec(vega_lite_spec, apply_defaults)
        spec_title = spec_dict.get("title")
        if isinstance(spec_title, str):
            title = spec_title
        elif isinstance(spec_title, dict):
            title = spec_title.get("text", "Chart")
        else:
            title = "Chart"

    try:
        from reportlab.lib.pagesizes import letter
        from reportlab.lib.units import inch
        from reportlab.pdfgen import canvas as pdf_canvas
        from reportlab.lib.utils import ImageReader
    except ImportError as exc:
        raise ImportError(
            "reportlab is required to render PDF charts. "
            "Add 'reportlab' to the deploy requirements and redeploy."
        ) from exc

    import io

    buf = io.BytesIO()
    c = pdf_canvas.Canvas(buf, pagesize=letter)
    page_width, page_height = letter  # 612 x 792 points

    # Title at top
    c.setFont("Helvetica-Bold", 14)
    c.drawString(0.75 * inch, page_height - 0.9 * inch, title)

    # Chart image below. The PNG is 800x450 at 2x scale = 1600x900 pixels;
    # we size it at ~7 inches wide (= 504 points) so it fits nicely on
    # letter with margins and scales down from the HiDPI original, keeping
    # the labels readable.
    img = ImageReader(io.BytesIO(png_bytes))
    img_width = 6.5 * inch
    img_height = img_width * (450 / 800)  # preserve 16:9 aspect
    x = (page_width - img_width) / 2
    y = page_height - 1.4 * inch - img_height
    c.drawImage(img, x, y, width=img_width, height=img_height)

    # Footer
    c.setFont("Helvetica", 8)
    c.setFillGray(0.5)
    c.drawString(0.75 * inch, 0.6 * inch, "Generated by Arizona Sales Copilot")

    c.showPage()
    c.save()
    return buf.getvalue()


# ---------------------------------------------------------------------------
# CSV renderer (extracts data rows from spec)
# ---------------------------------------------------------------------------

def render_data_to_csv(
    vega_lite_spec,
) -> bytes:
    """Extract the data rows from a Vega-Lite spec and emit as CSV.

    Looks at spec.data.values (the standard inline-data location) and emits
    a CSV with a header row + one row per data entry. Column order follows
    the first row's keys.

    Does NOT apply production defaults — CSV is raw data, not styled output.

    Args:
        vega_lite_spec: Vega-Lite spec dict or JSON string.

    Returns:
        CSV bytes (UTF-8 with BOM for Excel compatibility).

    Raises:
        ValueError: if the spec has no inline data rows to export.
    """
    # Parse input
    if isinstance(vega_lite_spec, str):
        spec_dict = json.loads(vega_lite_spec)
    elif isinstance(vega_lite_spec, dict):
        spec_dict = vega_lite_spec
    else:
        raise TypeError(
            f"vega_lite_spec must be dict or str, got {type(vega_lite_spec).__name__}"
        )

    data = spec_dict.get("data", {})
    values = data.get("values") if isinstance(data, dict) else None

    if not values or not isinstance(values, list):
        raise ValueError(
            "Cannot export CSV: Vega-Lite spec has no inline data.values array. "
            "CSV export requires spec.data.values to be a list of row dicts."
        )

    # Determine column order: union of keys across rows, preserving first-seen order
    columns: list[str] = []
    seen: set[str] = set()
    for row in values:
        if not isinstance(row, dict):
            continue
        for key in row.keys():
            if key not in seen:
                seen.add(key)
                columns.append(key)

    if not columns:
        raise ValueError(
            "Cannot export CSV: data rows are not dicts, don't know column order."
        )

    # Build CSV using stdlib csv writer. BOM prefix makes Excel interpret
    # UTF-8 correctly when the user opens the file directly.
    import csv
    import io

    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(columns)
    for row in values:
        if not isinstance(row, dict):
            continue
        writer.writerow([row.get(col, "") for col in columns])

    # UTF-8 BOM + body, encoded to bytes
    return b"\xef\xbb\xbf" + buf.getvalue().encode("utf-8")
