"""Offline tests for shared_libs.growth.  Run: python -m tests.test_growth"""

from __future__ import annotations

import math

from shared_libs.growth import build_result, grading, stats
from shared_libs.growth.model import EMERGING, MODERATE, STRONG, Finding, Ledger


def test_lmdi_exact():
    eff = stats.lmdi({"a": 100.0, "b": 3.0, "c": 6.0}, {"a": 80.0, "b": 2.7, "c": 5.0})
    assert abs(sum(eff.values()) - (80 * 2.7 * 5 - 100 * 3 * 6)) < 1e-9


def test_pvm_reconciles():
    lines = [stats.PvmLine("a", 100, 1000, 120, 1320), stats.PvmLine("c", 0, 0, 10, 200),
             stats.PvmLine("d", 30, 300, 0, 0), stats.PvmLine("e", 5, -10, 4, -5)]
    b = stats.pvm(lines)
    total = sum(b[k] for k in ("volume", "mix", "price", "new", "discontinued", "other"))
    assert abs(total - b["total_change"]) < 1e-9


def test_bh_matches_rejections():
    p = [0.001, 0.008, 0.039, 0.041, 0.042, 0.06, 0.074, 0.205]
    q = stats.bh_qvalues(p)
    assert [x <= 0.05 for x in q] == stats.benjamini_hochberg(p, 0.05)


def test_shrinkage_moves_small_most():
    s_big, s_small = stats.eb_shrink([math.log(1.2)] * 2, [0.0001, 0.5], math.log(0.6))
    assert abs(s_big - math.log(1.2)) < abs(s_small - math.log(1.2))


def test_sku_family():
    assert stats.sku_family("AZ HARD VARIETY 2-12PK 12OZ CAN 6/3/3_1581") == \
        stats.sku_family("AZ HARD VARIETY 2-12PK 12OZ CAN 3/3/3/3_1587")
    assert stats.sku_family("AZ HARD GREEN TEA UTAH 12PK 22OZ CAN_1597") == \
        stats.sku_family("AZ HARD GREEN TEA 12PK 22OZ CAN_1599")


def _f(**kw):
    base = dict(cls="relative_win", source="vip", title="t", dims={}, metric="m", unit="cases")
    base.update(kw)
    return Finding(**base)


def test_grades():
    assert grading.grade(_f(windows=["YTD", "L13W"], evidence=["X"], q_value=0.01)) == STRONG
    assert grading.grade(_f(windows=["YTD", "L13W"], q_value=0.01)) == MODERATE
    assert grading.grade(_f(windows=["YTD"])) == EMERGING
    assert grading.grade(_f(windows=["YTD", "L13W"], flags=["single reset period"])) == EMERGING


def test_apply_fdr_keeps_and_annotates():
    fs = [(_f(title=str(i)), p) for i, p in enumerate([0.0001, 0.3, 0.9])]
    kept = grading.apply_fdr(fs)
    assert len(kept) == 1 and kept[0].q_value is not None


def test_build_result_shape():
    L = Ledger()
    L.mark("vip", "relative_win", "state", "win", 1)
    r = build_result("vip", {"brand": "AZ Hard"}, {"focal": "x"},
                     [_f(windows=["YTD", "L13W"], impact=0.1)], L, {}, [])
    assert set(r) >= {"positives", "coverage_ledger", "summary_by_class", "headline", "how_to_use"}
    assert r["coverage_ledger"]["cells_with_wins"] == 1


if __name__ == "__main__":
    import sys
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    bad = 0
    for t in tests:
        try:
            t(); print("PASS", t.__name__)
        except Exception as e:  # noqa: BLE001
            bad += 1; print("FAIL", t.__name__, type(e).__name__, e)
    print(f"{len(tests) - bad}/{len(tests)} passed")
    sys.exit(1 if bad else 0)
