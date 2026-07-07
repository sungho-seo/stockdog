#!/usr/bin/env python3
"""Quick validation of prior_close module (no test framework dependency)."""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.prior_close import select_prior, prior_from_rows, _coerce

def assert_eq(actual, expected, msg=""):
    if actual != expected:
        raise AssertionError(f"Expected {expected}, got {actual}. {msg}")


def test_coerce():
    """Test _coerce function."""
    assert_eq(_coerce(date(2026, 7, 7)), date(2026, 7, 7), "_coerce date")
    assert_eq(_coerce("2026-07-07"), date(2026, 7, 7), "_coerce string")
    assert_eq(_coerce(None), None, "_coerce None")
    print("✓ _coerce tests passed")


def test_clean_regression():
    """Regression: clean 10-row series, n=1 should return vals[-2]."""
    pairs = [
        (date(2026, 6, 24), 100.0),
        (date(2026, 6, 25), 101.0),
        (date(2026, 6, 26), 102.0),
        (date(2026, 6, 27), 103.0),
        (date(2026, 6, 28), 104.0),
        (date(2026, 6, 29), 105.0),
        (date(2026, 6, 30), 106.0),
        (date(2026, 7, 1), 107.0),
        (date(2026, 7, 2), 108.0),
        (date(2026, 7, 3), 109.0),
    ]
    ref = date(2026, 7, 3)
    result = select_prior(pairs, ref, n=1)

    assert_eq(result.value, 108.0, "clean series n=1")
    assert_eq(result.prior_date, date(2026, 7, 2), "prior date n=1")
    assert_eq(result.gap_days, 1, "gap_days n=1")
    assert_eq(result.within_window, True, "within_window n=1")
    print("✓ Clean regression tests passed")


def test_clean_n5():
    """Regression: clean 10-row series, n=5 should return vals[-6]."""
    pairs = [
        (date(2026, 6, 24), 100.0),
        (date(2026, 6, 25), 101.0),
        (date(2026, 6, 26), 102.0),
        (date(2026, 6, 27), 103.0),
        (date(2026, 6, 28), 104.0),
        (date(2026, 6, 29), 105.0),
        (date(2026, 6, 30), 106.0),
        (date(2026, 7, 1), 107.0),
        (date(2026, 7, 2), 108.0),
        (date(2026, 7, 3), 109.0),
    ]
    ref = date(2026, 7, 3)
    result = select_prior(pairs, ref, n=5)

    # n=5 → 5th distinct date back from ref. sorted priors 6/24..7/2,
    # sorted_pairs[-5] = 6/28 = 104.0. gap = 7/3 - 6/28 = 5 days.
    assert_eq(result.value, 104.0, "clean series n=5")
    assert_eq(result.prior_date, date(2026, 6, 28), "prior date n=5")
    assert_eq(result.gap_days, 5, "gap_days n=5")
    print("✓ Clean n=5 tests passed")


def test_none_and_dedup():
    """Real case: skip None rows, select prior of 7/7, which is 7/4 (not 7/3 or earlier)."""
    pairs = [
        (date(2026, 6, 27), 1535.00),
        (date(2026, 6, 28), 1535.24),
        (date(2026, 6, 29), None),           # None → dropped
        (date(2026, 6, 30), 1551.00),
        (date(2026, 7, 1), 1545.97),
        (date(2026, 7, 2), 1553.22),
        (date(2026, 7, 3), 1537.67),         # Thursday
        (date(2026, 7, 4), 1530.15),         # Friday
        (date(2026, 7, 7), 1528.06),         # Monday: ref
    ]

    ref = date(2026, 7, 7)
    result = select_prior(pairs, ref, n=1)

    # After filtering out None and ref, prior dates are: 6/27, 6/28, 6/30, 7/1, 7/2, 7/3, 7/4
    # The 1st prior of 7/7 is 7/4
    assert_eq(result.value, 1530.15, "evidence case prior value")
    assert_eq(result.prior_date, date(2026, 7, 4), "evidence case prior date")
    assert_eq(result.gap_days, 3, "evidence case gap_days")
    print("✓ None and dedup tests passed")


def test_gap_guard():
    """Gap guard: 9-day gap should set within_window=False with max_gap_days=6."""
    pairs = [
        (date(2026, 6, 25), 100.0),  # Thursday
        (date(2026, 7, 4), 101.0),   # Saturday (9 days later)
    ]

    ref = date(2026, 7, 4)
    result = select_prior(pairs, ref, n=1, max_gap_days=6)

    assert_eq(result.gap_days, 9, "gap_days 9")
    assert_eq(result.within_window, False, "within_window with 9-day gap")
    print("✓ Gap guard tests passed")


def test_prior_from_rows():
    """prior_from_rows with auto ref_date detection."""
    rows = [
        {"date": "2026-06-27", "usd_krw": 1535.00},
        {"date": "2026-06-28", "usd_krw": 1535.24},
        {"date": "2026-06-29", "usd_krw": None},
        {"date": "2026-06-30", "usd_krw": 1551.00},
        {"date": "2026-07-01", "usd_krw": 1545.97},
        {"date": "2026-07-02", "usd_krw": 1553.22},
        {"date": "2026-07-03", "usd_krw": 1537.67},
        {"date": "2026-07-04", "usd_krw": 1530.15},
        {"date": "2026-07-07", "usd_krw": 1528.06},
    ]

    result = prior_from_rows(rows, "usd_krw")

    # Auto ref_date: last row with non-None usd_krw is 7/7
    # Prior of 7/7 is 7/4
    assert_eq(result.value, 1530.15, "prior_from_rows value")
    assert_eq(result.prior_date, date(2026, 7, 4), "prior_from_rows prior_date")
    assert_eq(result.ref_date, date(2026, 7, 7), "prior_from_rows ref_date")
    print("✓ prior_from_rows tests passed")


def test_friday_monday_gap():
    """Fri→Mon gap (3 cal days) is within window."""
    pairs = [
        (date(2026, 7, 3), 100.0),   # Friday
        (date(2026, 7, 6), 101.0),   # Monday
    ]

    ref = date(2026, 7, 6)
    result = select_prior(pairs, ref, n=1, max_gap_days=6)

    # 7/6 - 7/3 = 3 days
    assert_eq(result.gap_days, 3, "Fri-Mon gap")
    assert_eq(result.within_window, True, "Fri-Mon within_window")
    print("✓ Fri-Mon gap tests passed")


def main():
    try:
        test_coerce()
        test_clean_regression()
        test_clean_n5()
        test_none_and_dedup()
        test_gap_guard()
        test_prior_from_rows()
        test_friday_monday_gap()
        print("\n" + "="*60)
        print("✓ All validation tests passed!")
        return 0
    except Exception as e:
        print(f"\n✗ Test failed: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
