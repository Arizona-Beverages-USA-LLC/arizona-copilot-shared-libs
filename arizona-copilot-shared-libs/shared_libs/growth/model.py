"""
model.py — shared data model for every growth scan: a Finding (one positive), the coverage
Ledger, and the taxonomy / grading vocabulary shared by every source.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

# --------------------------------------------------------------------------- #
# Taxonomy of positives (docs/research/where-were-winning.md §2)
# --------------------------------------------------------------------------- #
NEW_BUSINESS = "new_business"
DEPTH = "distribution_depth"
LIKE_FOR_LIKE = "like_for_like"
RELATIVE = "relative_win"
PORTFOLIO = "portfolio_momentum"
MOMENTUM = "momentum"
PROFIT = "profit_quality"
RECOVERY = "recovery"
SELL_THROUGH = "clean_sell_through"

CLASSES: dict[str, str] = {
    NEW_BUSINESS: "New business",
    DEPTH: "Distribution breadth & depth",
    LIKE_FOR_LIKE: "Like-for-like growth",
    RELATIVE: "Relative win / share",
    PORTFOLIO: "Portfolio momentum",
    MOMENTUM: "Momentum",
    PROFIT: "Profit & quality",
    RECOVERY: "Recovery",
    SELL_THROUGH: "Clean sell-through",
}

# Evidence grades (IPCC evidence × agreement; GRADE up/downgrades).
STRONG, MODERATE, EMERGING = "Strong", "Moderate", "Emerging"
GRADE_WEIGHT = {STRONG: 1.0, MODERATE: 0.6, EMERGING: 0.3}
GRADE_MEANING = {
    STRONG: "confirmed in 2+ data layers (or 2+ independent measures) and "
            "persistent across windows",
    MODERATE: "one layer, statistically robust and persistent; other layers "
              "consistent or not measurable",
    EMERGING: "one layer and one window, a small base, or sell-in only — "
              "watch, don't headline",
}

SOURCE_LABEL = {
    "vip": "VIP distributor depletions (delivered cases)",
    "sap": "SAP shipments / customer P&L (sell-in)",
    "iri": "IRI / Circana consumer POS",
    "kroger": "Kroger shelf resets",
    "cross": "Cross-source (Master)",
}


@dataclass
class Finding:
    cls: str                      # taxonomy class
    source: str                   # vip | sap | iri | cross
    title: str                    # one-line plain-English statement
    dims: dict[str, str]          # scope coordinates, e.g. {"state": "UT"}
    metric: str                   # e.g. "delivered cases"
    unit: str                     # cases | $ | pts | doors | PODs | %
    focal: float | None = None
    comparator: float | None = None
    change: float | None = None   # focal − comparator (in unit)
    change_pct: float | None = None
    impact: float = 0.0           # |contribution| ÷ scope total (0..1+) for ranking
    benchmark: dict[str, Any] = field(default_factory=dict)
    windows: list[str] = field(default_factory=list)   # windows in which it held
    flags: list[str] = field(default_factory=list)
    derivation: str = ""          # how the number was built (shown to the model)
    grade: str = EMERGING
    evidence: list[str] = field(default_factory=list)  # cross-layer confirmations
    match_keys: dict[str, str] = field(default_factory=dict)  # for cross-layer joins
    q_value: float | None = None  # BH-adjusted p (None = not a statistical test)
    single_system: bool = False   # only one data layer can measure this (e.g. P&L, share)
    score: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        def r(x, nd=2):
            return None if x is None else round(x, nd)
        out = {
            "class": CLASSES.get(self.cls, self.cls),
            "grade": self.grade,
            "source": self.source,
            "title": self.title,
            "scope": self.dims,
            "metric": self.metric,
            "unit": self.unit,
            "focal": r(self.focal),
            "comparator": r(self.comparator),
            "change": r(self.change),
            "change_pct": None if self.change_pct is None else round(self.change_pct * 100, 1),
            "windows_held": self.windows,
            "derivation": self.derivation,
        }
        if self.benchmark:
            out["benchmark"] = self.benchmark
        if self.flags:
            out["flags"] = self.flags
        if self.evidence:
            out["confirmed_by"] = self.evidence
        return out


@dataclass
class Ledger:
    """Coverage ledger: every (class × dimension) cell must end in a status, so
    'all angles checked' is provable. Status: win | tested-none | not-in-data |
    via-agent | error."""
    cells: dict[tuple[str, str, str], dict[str, Any]] = field(default_factory=dict)
    _lock: Any = field(default_factory=threading.Lock, repr=False)

    def mark(self, source: str, cls: str, dimension: str, status: str,
             n: int = 0, note: str = "") -> None:
        with self._lock:
            self._mark(source, cls, dimension, status, n, note)

    def _mark(self, source, cls, dimension, status, n, note) -> None:
        key = (source, cls, dimension)
        cur = self.cells.get(key)
        if cur and cur["status"] == "win" and status != "win":
            return  # never downgrade a found win
        if cur and cur["status"] == "win" and status == "win":
            cur["n"] += n
            return
        self.cells[key] = {"status": status, "n": n, "note": note}

    def to_dict(self) -> dict[str, Any]:
        grid: dict[str, dict[str, dict[str, str]]] = {}
        for (src, cls, dim), v in sorted(self.cells.items()):
            label = v["status"] if v["status"] != "win" else f"win ({v['n']})"
            if v.get("note"):
                label += f" — {v['note']}"
            grid.setdefault(src, {}).setdefault(CLASSES.get(cls, cls), {})[dim] = label
        tested = sum(1 for v in self.cells.values() if v["status"] in ("win", "tested-none"))
        wins = sum(1 for v in self.cells.values() if v["status"] == "win")
        return {"cells_tested": tested, "cells_with_wins": wins, "grid": grid}
