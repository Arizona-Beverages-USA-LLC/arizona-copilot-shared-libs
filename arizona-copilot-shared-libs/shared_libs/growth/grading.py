"""
grading.py — shared evidence grading, false-discovery filtering, de-duplication
and ranking for every growth scan.

Grades are GRADE / IPCC style and QUALITATIVE: a positive starts at Strong and
drops one level per failed check, floored at Emerging. The numeric weights are
used ONLY to rank — never shown as probabilities.

A single-source agent can rarely reach Strong: "not confirmed by a second data
layer" is one of the checks, and only the Master Sales Analyst sees several
layers. That is intentional — cross-layer confirmation is what Strong means.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable

from . import stats
from .model import CLASSES, EMERGING, GRADE_WEIGHT, MODERATE, STRONG, Finding

GRADES = [STRONG, MODERATE, EMERGING]

# Caution flags that each cost one grade level (lower-case substrings).
DOWNGRADE_FLAGS = ("easy comparison", "pipeline fill", "may not be repeating",
                   "sell-in only", "territory hand-off", "net pods at retained doors fell",
                   "forecast, not actual", "single reset period")

Q_STRONG = 0.05       # BH q required to stay Strong
Q_MODERATE = 0.10     # BH q above this costs two levels
Q_HYPOTHESIS = 0.25   # BH q kept at all (labelled hypothesis); above → dropped


def apply_fdr(candidates: Iterable[tuple[Finding, float]],
              q_keep: float = Q_HYPOTHESIS) -> list[Finding]:
    """Benjamini–Hochberg q-values across every statistically-tested candidate;
    keep q ≤ q_keep and record p / q on the finding."""
    cands = list(candidates)
    if not cands:
        return []
    qs = stats.bh_qvalues([p for _, p in cands])
    kept = []
    for (fnd, p), q in zip(cands, qs):
        if q <= q_keep:
            fnd.benchmark = {**fnd.benchmark, "p_value": round(p, 4), "q_value": round(q, 4)}
            fnd.q_value = q
            kept.append(fnd)
    return kept


def grade(f: Finding, q_strong: float = Q_STRONG, q_moderate: float = Q_MODERATE) -> str:
    down = 0
    confirmed = bool(f.evidence)
    if not confirmed:
        down += 1
    if len(set(f.windows)) < 2:
        down += 1
    if f.q_value is not None:
        if f.q_value > q_moderate:
            down += 2
        elif f.q_value > q_strong:
            down += 1
    for flag in f.flags:
        fl = flag.lower()
        if any(k in fl for k in DOWNGRADE_FLAGS):
            if "sell-in only" in fl and confirmed:
                continue
            down += 1
    return GRADES[min(down, 2)]


def _close(a, b, tol=0.03) -> bool:
    if a is None or b is None:
        return a == b
    if a == b:
        return True
    return abs(a - b) <= tol * max(abs(a), abs(b))


def dedupe(findings: list[Finding]) -> list[Finding]:
    """Drop near-duplicates: same source & class with focal and comparator within
    3% (e.g. an owner and its only banner). Evidence is merged into the survivor."""
    kept: list[Finding] = []
    for f in sorted(findings, key=lambda x: -abs(x.impact)):
        dup = next((g for g in kept if g.source == f.source and g.cls == f.cls
                    and g.focal not in (None, 0) and _close(g.focal, f.focal)
                    and _close(g.comparator, f.comparator)), None)
        if dup:
            dup.evidence += [e for e in f.evidence if e not in dup.evidence]
        else:
            kept.append(f)
    return kept


def rank(findings: list[Finding], max_per_class: int = 12, max_total: int = 45,
         q_strong: float = Q_STRONG, q_moderate: float = Q_MODERATE) -> list[Finding]:
    """Grade, score (|impact| × grade weight × persistence) and cap per class."""
    for f in findings:
        f.grade = grade(f, q_strong, q_moderate)
        persist = 1.0 if len(set(f.windows)) >= 2 else 0.8
        f.score = abs(f.impact) * GRADE_WEIGHT[f.grade] * persist
    out, per_class = [], defaultdict(int)
    for f in sorted(findings, key=lambda x: -x.score):
        if per_class[f.cls] >= max_per_class:
            continue
        per_class[f.cls] += 1
        out.append(f)
        if len(out) >= max_total:
            break
    return out


def summarize(findings: list[Finding]) -> dict[str, dict[str, int]]:
    by_class: dict[str, dict[str, int]] = {}
    for f in findings:
        c = by_class.setdefault(CLASSES.get(f.cls, f.cls), {STRONG: 0, MODERATE: 0, EMERGING: 0})
        c[f.grade] += 1
    return by_class
