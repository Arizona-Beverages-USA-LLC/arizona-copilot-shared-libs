"""
shared_libs.growth — the shared core of every "Where We're Winning" growth scan.

What is shared (identical in every agent):
    stats.py     pure math: growth, log-ratio test with over-dispersion,
                 empirical-Bayes shrinkage, Benjamini–Hochberg q-values, LMDI-I,
                 price-volume-mix with new/discontinued bars, SKU families
    model.py     Finding, coverage Ledger, the nine-class taxonomy, grade vocabulary
    grading.py   FDR filtering, qualitative GRADE-style grades, dedupe, ranking
    contract.py  PROMPT_CONTRACT (the shared prompt vocabulary) and build_result
                 (the shared tool-output shape)

What is NOT shared (each agent owns it): the source scanner — its SQL, data
definitions, slicings, floors — and the agent's own prompt section, written in
that agent's units and idiom. See README.md.
"""

from . import contract, grading, model, stats  # noqa: F401
from .contract import PROMPT_CONTRACT, build_result  # noqa: F401
from .model import (CLASSES, DEPTH, EMERGING, LIKE_FOR_LIKE, MODERATE,  # noqa: F401
                    MOMENTUM, NEW_BUSINESS, PORTFOLIO, PROFIT, RECOVERY,
                    RELATIVE, SELL_THROUGH, STRONG, Finding, Ledger)

__version__ = "1.0.0"
