"""
contract.py — the thin contract every agent's "Where We're Winning" shares:
the prompt vocabulary (PROMPT_CONTRACT) and the tool-output shape
(build_result). Each agent adds its OWN section guidance in its own prompt, in
its own units and idiom; this file only fixes what must be identical so the
Master Sales Analyst can merge the agents' positives without translation.
"""

from __future__ import annotations

from typing import Any

from .grading import rank, summarize
from .model import CLASSES, GRADE_MEANING, Finding, Ledger

PROMPT_CONTRACT = """\
### Where We're Winning — shared contract (identical in every Arizona agent)
- **Classes of positive** (use these exact names): New business · Distribution
  breadth & depth · Like-for-like growth · Relative win / share · Portfolio
  momentum · Momentum · Profit & quality · Recovery · Clean sell-through. A class
  this data cannot measure is stated once as "not measurable in this data".
- **Grades** are qualitative and come from the scan — never upgrade one:
  Strong = confirmed in 2+ data layers and persistent (usually only the Master
  Sales Analyst can reach it); Moderate = one layer, statistically robust and
  persistent; Emerging = one window, small base or a caution flag → "early
  signals to watch", never a headline.
- **Table:** `Class | Positive (scope) | Evidence — figure, comparator, window | Grade`.
  Close with one coverage line from the ledger: "Angles checked: N; wins in M;
  classes with no qualifying win: …".
- **Honest labels:** quote the scan's numbers and derivations exactly; a relative
  win that is still a decline says so ("fell 18% while the book fell 44%"); a
  momentum positive that is a smaller decline says "decline narrowing"; show the
  base beside any large %.
- **Positives sit beside the candid diagnosis, never instead of it.**
"""


def build_result(source: str, scope: dict[str, Any], windows: dict[str, str],
                 findings: list[Finding], ledger: Ledger, context: dict[str, Any],
                 caveats: list[str], max_per_class: int = 12,
                 max_total: int = 40) -> dict[str, Any]:
    """The standard tool output — the same keys the Master's scan returns."""
    ranked = rank(findings, max_per_class=max_per_class, max_total=max_total)
    return {
        "source": source,
        "scope": scope,
        "windows": windows,
        "headline": [{"grade": f.grade, "class": CLASSES[f.cls], "title": f.title}
                     for f in ranked[:6]],
        "positives": [f.to_dict() for f in ranked],
        "summary_by_class": summarize(ranked),
        "context": context,
        "coverage_ledger": ledger.to_dict(),
        "grades": GRADE_MEANING,
        "caveats": caveats,
        "how_to_use": (
            "Build the 'Where We're Winning' section from `positives`: quote numbers and "
            "derivations exactly, keep each grade, lead with the highest grades, list "
            "Emerging only as early signals to watch, and close with the coverage line "
            "from `coverage_ledger`. Positives sit beside the diagnosis, never instead."),
    }
