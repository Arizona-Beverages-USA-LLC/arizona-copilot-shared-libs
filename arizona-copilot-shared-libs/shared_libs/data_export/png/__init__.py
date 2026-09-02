"""
Chart renderers for downloadable export.

Despite the subpackage name `png/`, this module now contains renderers
for every format the ChartArtifact supports: PNG, SVG, HTML, PDF, CSV.
The naming is historical (PNG was the first one shipped); keeping the
path stable avoids a big refactor. All renderers share the same
production-defaults pipeline in chart.py so inline/downloaded charts
look identical.

Uses vl-convert-python for PNG/SVG rendering (bundled Rust Vega-Lite
renderer, no browser needed). PDF uses reportlab to embed PNG bytes
in a PDF document. HTML is a hand-built template that loads Vega
libs from CDN for interactivity. CSV extracts rows from the spec.
"""

from .chart import (
    render_vega_to_png,
    render_vega_to_svg,
    render_vega_to_html,
    render_vega_to_pdf,
    render_data_to_csv,
)

__all__ = [
    "render_vega_to_png",
    "render_vega_to_svg",
    "render_vega_to_html",
    "render_vega_to_pdf",
    "render_data_to_csv",
]
