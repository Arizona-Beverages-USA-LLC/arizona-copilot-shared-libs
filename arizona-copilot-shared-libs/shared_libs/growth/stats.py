"""
stats.py — the pure-math core of the growth scan (no I/O; unit-tested offline).

Every function here implements a published method (see
shared_libs/growth/README.md):

  * growth / log-ratio z-test on count-like volumes, with an over-dispersion
    factor estimated from the sibling group (case volumes are lumpier than
    Poisson, so a raw Poisson test would call noise a win);
  * empirical-Bayes shrinkage of segment growth toward the parent (Efron &
    Morris 1975; partial pooling) so a +300% on a tiny base is pulled toward
    the parent rate before it can rank;
  * Benjamini–Hochberg false-discovery control across every segment tested;
  * LMDI-I additive decomposition (Ang) — the exact, residual-free split of a
    change into factor effects (doors × SKUs/door × cases/POD);
  * price-volume-mix at the lowest grain with NEW and DISCONTINUED items as
    their own bars (FTI Consulting; CPG organic-growth practice).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import median
from typing import Iterable, Sequence


# --------------------------------------------------------------------------- #
# Basic growth
# --------------------------------------------------------------------------- #
def growth(focal: float, comparator: float) -> float | None:
    """Fractional change focal vs comparator (None when the base is not positive)."""
    if comparator is None or focal is None or comparator <= 0:
        return None
    return focal / comparator - 1.0


def pct(x: float | None, nd: int = 1) -> str:
    return "n/a" if x is None else f"{x * 100:+.{nd}f}%"


# --------------------------------------------------------------------------- #
# Significance of a volume change (log-ratio test with over-dispersion)
# --------------------------------------------------------------------------- #
def _norm_sf(z: float) -> float:
    """Upper-tail probability of the standard normal."""
    return 0.5 * math.erfc(z / math.sqrt(2.0))


def log_ratio(focal: float, comparator: float) -> float:
    return math.log((max(focal, 0.0) + 0.5) / (max(comparator, 0.0) + 0.5))


def log_ratio_var(focal: float, comparator: float, dispersion: float = 1.0) -> float:
    """Variance of ln(focal/comparator) for Poisson-like counts, inflated by the
    over-dispersion factor (≥ 1)."""
    return dispersion * (1.0 / (max(focal, 0.0) + 0.5) + 1.0 / (max(comparator, 0.0) + 0.5))


def estimate_dispersion(pairs: Sequence[tuple[float, float]], parent_lr: float) -> float:
    """Over-dispersion φ for a sibling group: the median of the squared
    standardized deviation of each sibling's log-ratio from the parent's, under
    the Poisson variance. Median (not mean) so a few genuine outliers — the wins
    we are looking for — don't inflate φ. Floored at 1."""
    ratios = []
    for f, c in pairs:
        if f <= 0 and c <= 0:
            continue
        v = log_ratio_var(f, c)
        if v > 0:
            ratios.append((log_ratio(f, c) - parent_lr) ** 2 / v)
    if len(ratios) < 5:
        return 1.0
    # A chi-square(1) variable has median ≈ 0.455; rescale so φ=1 under Poisson.
    return max(1.0, median(ratios) / 0.455)


def one_sided_p(focal: float, comparator: float, parent_lr: float,
                dispersion: float) -> float:
    """p-value that this segment grew FASTER than its parent (H0: same rate)."""
    z = (log_ratio(focal, comparator) - parent_lr) / math.sqrt(
        log_ratio_var(focal, comparator, dispersion))
    return _norm_sf(z)


def one_sided_p_positive(focal: float, comparator: float, dispersion: float) -> float:
    """p-value that this segment grew at all (H0: no change)."""
    z = log_ratio(focal, comparator) / math.sqrt(log_ratio_var(focal, comparator, dispersion))
    return _norm_sf(z)


# --------------------------------------------------------------------------- #
# Empirical-Bayes shrinkage of segment growth toward the parent
# --------------------------------------------------------------------------- #
def eb_shrink(values: Sequence[float], variances: Sequence[float],
              prior_mean: float) -> list[float]:
    """Shrink each log-ratio toward the parent log-ratio (normal-normal model,
    method-of-moments τ²). Segments with little data move most."""
    if not values:
        return []
    n = len(values)
    mean_var = sum(variances) / n
    spread = sum((v - prior_mean) ** 2 for v in values) / n
    tau2 = max(spread - mean_var, 1e-6)
    return [
        (tau2 * v + var * prior_mean) / (tau2 + var)
        for v, var in zip(values, variances)
    ]


# --------------------------------------------------------------------------- #
# Benjamini–Hochberg false-discovery control
# --------------------------------------------------------------------------- #
def benjamini_hochberg(pvalues: Sequence[float], q: float) -> list[bool]:
    """Return which hypotheses are rejected at FDR q (step-up procedure)."""
    m = len(pvalues)
    if m == 0:
        return []
    order = sorted(range(m), key=lambda i: pvalues[i])
    cutoff_rank = 0
    for rank, i in enumerate(order, start=1):
        if pvalues[i] <= rank / m * q:
            cutoff_rank = rank
    keep = [False] * m
    for rank, i in enumerate(order, start=1):
        if rank <= cutoff_rank:
            keep[i] = True
    return keep


def bh_qvalues(pvalues: Sequence[float]) -> list[float]:
    """Benjamini–Hochberg adjusted p-values (q-values): the smallest FDR at which
    each hypothesis would be rejected. Grading uses q ≤ 0.05 / 0.10 / 0.25."""
    m = len(pvalues)
    if m == 0:
        return []
    order = sorted(range(m), key=lambda i: pvalues[i])
    q = [0.0] * m
    running = 1.0
    for rank in range(m, 0, -1):
        i = order[rank - 1]
        running = min(running, pvalues[i] * m / rank)
        q[i] = running
    return q


# --------------------------------------------------------------------------- #
# SKU families — so a pack-configuration change or a state-specific variant is
# not mistaken for new business (research pitfall: SKU transitions).
# --------------------------------------------------------------------------- #
import re as _re

_CODE = _re.compile(r"_\d+$")
_PACK_CFG = _re.compile(r"\b\d+(?:/\d+)+\b")
_VARIANT = _re.compile(r"\b(UTAH|PROMO)\b")   # state-specific / promo variants of a core SKU


def sku_family(name: str) -> str:
    """'AZ HARD VARIETY 2-12PK 12OZ CAN 6/3/3_1581' and '…3/3/3/3_1587' → the same
    family; 'AZ HARD GREEN TEA UTAH 12PK 22OZ CAN_1597' → the 1599 family."""
    s = _CODE.sub("", (name or "").upper())
    s = _PACK_CFG.sub(" ", s)
    s = _VARIANT.sub(" ", s)
    return " ".join(s.split())


# --------------------------------------------------------------------------- #
# LMDI-I additive decomposition (Ang)
# --------------------------------------------------------------------------- #
def _logmean(a: float, b: float) -> float:
    if a == b:
        return a
    return (a - b) / (math.log(a) - math.log(b))


def lmdi(factors0: dict[str, float], factors1: dict[str, float]) -> dict[str, float] | None:
    """Split ΔV (V = Π factors) into additive factor effects with no residual:
    effect_k = L(V1, V0) · ln(x_k1 / x_k0). Returns None when any factor is
    non-positive (LMDI needs positive values; report the plain change instead)."""
    keys = list(factors0)
    if any(factors0[k] is None or factors1.get(k) is None for k in keys):
        return None
    if any(factors0[k] <= 0 or factors1[k] <= 0 for k in keys):
        return None
    v0 = math.prod(factors0[k] for k in keys)
    v1 = math.prod(factors1[k] for k in keys)
    L = _logmean(v1, v0)
    return {k: L * math.log(factors1[k] / factors0[k]) for k in keys}


# --------------------------------------------------------------------------- #
# Price-volume-mix with NEW / DISCONTINUED bars
# --------------------------------------------------------------------------- #
@dataclass
class PvmLine:
    key: str
    q0: float
    r0: float
    q1: float
    r1: float


def pvm(lines: Iterable[PvmLine]) -> dict[str, float]:
    """Organic-growth bridge at the lowest grain:
      continuing items (q0>0, q1>0, r0>0, r1>0) → volume + mix + price;
      new items (q0<=0, q1>0)                   → NEW bar (their full r1);
      discontinued (q0>0, q1<=0)                → DISCONTINUED bar (−r0);
      anything else (returns, zero/negative revenue) → OTHER, so the bars
      always sum to ΔR exactly."""
    cont = []
    new = disc = other = 0.0
    total0 = total1 = 0.0
    for ln in lines:
        total0 += ln.r0
        total1 += ln.r1
        if ln.q0 > 0 and ln.q1 > 0 and ln.r0 > 0 and ln.r1 > 0:
            cont.append(ln)
        elif ln.q0 <= 0 and ln.q1 > 0:
            new += ln.r1
            other += -ln.r0  # any stray base revenue on a "new" line
        elif ln.q0 > 0 and ln.q1 <= 0:
            disc -= ln.r0
            other += ln.r1
        else:
            other += ln.r1 - ln.r0
    q0 = sum(c.q0 for c in cont)
    q1 = sum(c.q1 for c in cont)
    r0 = sum(c.r0 for c in cont)
    if q0 > 0:
        p_bar0 = r0 / q0
        volume = (q1 - q0) * p_bar0
        mix = sum(c.q1 * (c.r0 / c.q0) for c in cont) - q1 * p_bar0
        price = sum((c.r1 / c.q1 - c.r0 / c.q0) * c.q1 for c in cont)
    else:
        volume = mix = price = 0.0
    return {
        "volume": volume, "mix": mix, "price": price,
        "new": new, "discontinued": disc, "other": other,
        "total_change": total1 - total0,
    }
