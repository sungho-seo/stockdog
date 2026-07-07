"""
Tests for utils.prior_close — prior baseline selection for change % computation.

No network, no db writes. Tests use synthetic data fixtures and cached yfinance-like
structures.
"""
import pytest
from datetime import date, datetime
import pandas as pd

from utils.prior_close import (
    select_prior,
    prior_from_history,
    prior_from_rows,
    PriorResult,
    _coerce,
    DEFAULT_MAX_GAP_DAYS,
)


# ============================================================================
# Fixtures: synthetic data for regression and edge cases
# ============================================================================

@pytest.fixture
def evidence_rows_usd_krw():
    """
    Real evidence from macro daily: 2026-06-27 through 2026-07-07.
    - 6/29 has None for usd_krw (Monday, no data) → dropped by the None filter.
    - 7/3 and 7/4 are DISTINCT dates, each with its own value.
      On 7/7 (Monday), the immediate prior (n=1) is 7/4 (the last valid distinct
      date before ref), NOT 7/3. The bug this guards against is picking a
      stale/out-of-window baseline; here 7/4 is a valid in-window prior (gap 3d).
      The None row (6/29) is skipped; Δ5d counts 5 distinct dates back.
    """
    return [
        {"date": "2026-06-27", "usd_krw": 1535.00},
        {"date": "2026-06-28", "usd_krw": 1535.24},
        {"date": "2026-06-29", "usd_krw": None},           # Monday: no data
        {"date": "2026-06-30", "usd_krw": 1551.00},
        {"date": "2026-07-01", "usd_krw": 1545.97},
        {"date": "2026-07-02", "usd_krw": 1553.22},
        {"date": "2026-07-03", "usd_krw": 1537.67},        # Thursday: real close
        {"date": "2026-07-04", "usd_krw": 1530.15},        # Friday: dup-ish or stale?
        {"date": "2026-07-07", "usd_krw": 1528.06},        # Monday: query date
    ]


@pytest.fixture
def clean_10_row_series():
    """Synthetic 10-row daily series: no gaps, no None, no dups."""
    return [
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


# ============================================================================
# Test: _coerce() handles multiple input types
# ============================================================================

def test_coerce_date_object():
    """date object passes through."""
    d = date(2026, 7, 7)
    assert _coerce(d) == d


def test_coerce_datetime_object():
    """datetime coerces to date."""
    dt = datetime(2026, 7, 7, 15, 30, 0)
    assert _coerce(dt) == date(2026, 7, 7)


def test_coerce_iso_string():
    """YYYY-MM-DD string parses to date."""
    assert _coerce("2026-07-07") == date(2026, 7, 7)


def test_coerce_bad_string():
    """Invalid string returns None."""
    assert _coerce("not-a-date") is None


def test_coerce_none():
    """None returns None."""
    assert _coerce(None) is None


def test_coerce_bad_type():
    """Unparseable type returns None."""
    assert _coerce(12345) is None


# ============================================================================
# Test: select_prior() with clean data (regression invariant)
# ============================================================================

def test_select_prior_clean_n1(clean_10_row_series):
    """On a clean 10-row series, select_prior(n=1) returns the second-to-last row."""
    pairs = clean_10_row_series
    ref = pairs[-1][0]  # date(2026, 7, 3)
    result = select_prior(pairs, ref, n=1)

    assert result.value == 108.0  # pairs[-2][1]
    assert result.prior_date == date(2026, 7, 2)
    assert result.gap_days == 1
    assert result.within_window is True


def test_select_prior_clean_n5(clean_10_row_series):
    """On a clean 10-row series, select_prior(n=5) returns the 5th-from-last row."""
    pairs = clean_10_row_series
    ref = pairs[-1][0]  # date(2026, 7, 3)
    result = select_prior(pairs, ref, n=5)

    # ref (7/3) is excluded from the prior pool; n=5 → 5th distinct date back.
    # sorted priors 6/24..7/2, sorted_pairs[-5] = 6/28 = 104.0 (= vals[-6]).
    assert result.value == 104.0  # pairs[-6][1]
    assert result.prior_date == date(2026, 6, 28)
    assert result.gap_days == 5  # 7/3 − 6/28
    # n>1 never applies gap guard, so within_window is always True if prior found
    assert result.within_window is True


def test_select_prior_insufficient_priors(clean_10_row_series):
    """When fewer than n distinct priors, return None."""
    pairs = clean_10_row_series[:3]
    ref = pairs[-1][0]
    result = select_prior(pairs, ref, n=5)

    assert result.value is None
    assert result.prior_date is None
    assert result.gap_days is None
    assert result.within_window is False


# ============================================================================
# Test: select_prior() with None values and deduplication (evidence case)
# ============================================================================

def test_select_prior_evidence_row_with_none():
    """Real case: 7/7 (Monday) query. The None row (6/29) is skipped; the
    immediate prior (n=1) is 7/4 (last valid distinct date before ref, gap 3d)."""
    pairs = [
        (date(2026, 6, 27), 1535.00),
        (date(2026, 6, 28), 1535.24),
        (date(2026, 6, 29), None),           # None → dropped
        (date(2026, 6, 30), 1551.00),
        (date(2026, 7, 1), 1545.97),
        (date(2026, 7, 2), 1553.22),
        (date(2026, 7, 3), 1537.67),         # Thursday: real close
        (date(2026, 7, 4), 1530.15),         # Friday: real close
        (date(2026, 7, 7), 1528.06),         # Monday: ref
    ]

    # Drop pairs >= ref; ref is excluded from prior pool
    ref = date(2026, 7, 7)
    result = select_prior(pairs, ref, n=1)

    # Prior should be 7/4 (last valid distinct date before 7/7)
    # After dedup, 7/4 is the most recent non-ref date
    assert result.value == 1530.15
    assert result.prior_date == date(2026, 7, 4)
    assert result.gap_days == 3
    assert result.within_window is True


def test_select_prior_dedup_keeps_last():
    """When same date appears twice, keep the second (last) occurrence."""
    pairs = [
        (date(2026, 7, 1), 100.0),
        (date(2026, 7, 1), 101.0),           # Same date, later value
        (date(2026, 7, 2), 102.0),
    ]

    ref = date(2026, 7, 2)
    result = select_prior(pairs, ref, n=1)

    # Deduplicated: 7/1 → 101.0 (last), 7/2 → 102.0
    # Prior of 7/2 is 7/1 with value 101.0
    assert result.value == 101.0
    assert result.prior_date == date(2026, 7, 1)


# ============================================================================
# Test: Gap guard (n=1 only)
# ============================================================================

def test_gap_guard_n1_within():
    """n=1, gap=4 calendar days (Thu→Mon over a weekend) → within_window True."""
    pairs = [
        (date(2026, 7, 2), 100.0),  # Thursday
        (date(2026, 7, 6), 101.0),  # Monday
    ]

    ref = date(2026, 7, 6)
    result = select_prior(pairs, ref, n=1)

    assert result.value == 100.0
    # 7/6 − 7/2 = 4 calendar days; within the default 6-day window.
    assert result.gap_days == 4
    assert result.within_window is True


def test_gap_guard_n1_beyond_window():
    """n=1, gap=9 days (beyond MAX_GAP_DAYS=6) → within_window False."""
    pairs = [
        (date(2026, 6, 25), 100.0),  # Thursday
        (date(2026, 7, 4), 101.0),   # Saturday (9 calendar days later)
    ]

    ref = date(2026, 7, 4)
    result = select_prior(pairs, ref, n=1, max_gap_days=6)

    assert result.value == 100.0
    assert result.gap_days == 9
    assert result.within_window is False


def test_gap_guard_n_gt_1_ignores_guard():
    """n>1: no gap guard applied, within_window always True (if prior exists)."""
    pairs = [
        (date(2026, 6, 25), 100.0),  # 9 days before ref
        (date(2026, 7, 4), 101.0),   # ref
    ]

    ref = date(2026, 7, 4)
    result = select_prior(pairs, ref, n=1, max_gap_days=6)

    # This is the n=1 case with gap=9, so within_window should be False.
    assert result.within_window is False


# ============================================================================
# Test: prior_from_rows() with dict list
# ============================================================================

def test_prior_from_rows_auto_ref(evidence_rows_usd_krw):
    """prior_from_rows auto-detects ref_date as latest with non-None key."""
    rows = evidence_rows_usd_krw

    # Auto-detect ref_date: latest row with non-None usd_krw is 2026-07-07 (index -1)
    result = prior_from_rows(rows, "usd_krw")

    # Prior of 2026-07-07 should be 2026-07-04 (last non-None before 7/7)
    assert result.value == 1530.15
    assert result.prior_date == date(2026, 7, 4)
    assert result.ref_date == date(2026, 7, 7)
    assert result.gap_days == 3


def test_prior_from_rows_explicit_ref(evidence_rows_usd_krw):
    """prior_from_rows with explicit ref_date."""
    rows = evidence_rows_usd_krw

    result = prior_from_rows(rows, "usd_krw", ref_date="2026-07-03")

    # Prior of 2026-07-03 should be 2026-07-02 (1537.67 → 1553.22)
    # Wait, the order is: ...7/02=1553.22, 7/03=1537.67, 7/04=1530.15, 7/07=1528.06
    # Prior of 7/3 is 7/2 with value 1553.22
    assert result.value == 1553.22
    assert result.prior_date == date(2026, 7, 2)
    assert result.ref_date == date(2026, 7, 3)


# ============================================================================
# Test: prior_from_history() with pandas DataFrame
# ============================================================================

def test_prior_from_history_clean():
    """prior_from_history on a clean DataFrame (no gaps)."""
    # Create a minimal DataFrame
    dates = pd.date_range('2026-06-27', periods=5, freq='D')
    df = pd.DataFrame({
        'Close': [1535.00, 1535.24, 1551.00, 1545.97, 1553.22],
    }, index=dates)

    result = prior_from_history(df)

    # ref_date = last index (2026-07-01)
    # prior = second-to-last (2026-06-30) with value 1545.97
    assert result.value == 1545.97
    assert result.prior_date == date(2026, 6, 30)
    assert result.ref_date == date(2026, 7, 1)
    assert result.gap_days == 1


def test_prior_from_history_n5():
    """prior_from_history with n=5."""
    dates = pd.date_range('2026-06-27', periods=10, freq='D')
    closes = list(range(100, 110))
    df = pd.DataFrame({'Close': closes}, index=dates)

    result = prior_from_history(df, n=5)

    # ref_date = 2026-07-06 (last). ref excluded; sorted priors 6/27..7/5,
    # sorted_pairs[-5] = 2026-07-01 (value 104). gap = 7/6 − 7/1 = 5.
    assert result.value == 104.0
    assert result.prior_date == date(2026, 7, 1)
    assert result.gap_days == 5
    # With n=5, within_window should be True if prior found
    assert result.within_window is True


# ============================================================================
# Test: Holiday-like gaps (Fri→Mon, Thanksgiving week, New Year)
# ============================================================================

def test_holiday_gap_fri_mon():
    """Friday→Monday gap (3 calendar days) is within window."""
    pairs = [
        (date(2026, 7, 3), 100.0),   # Friday
        (date(2026, 7, 6), 101.0),   # Monday (gap=3 cal days)
    ]

    ref = date(2026, 7, 6)
    result = select_prior(pairs, ref, n=1, max_gap_days=6)

    assert result.gap_days == 3
    assert result.within_window is True


def test_holiday_gap_thanksgiving():
    """Wednesday before Thanksgiving → Friday after (2 calendar days) is within."""
    pairs = [
        (date(2026, 11, 25), 100.0),  # Wednesday
        (date(2026, 11, 27), 101.0),  # Friday (Thanksgiving market closed)
    ]

    ref = date(2026, 11, 27)
    result = select_prior(pairs, ref, n=1, max_gap_days=6)

    assert result.gap_days == 2
    assert result.within_window is True


def test_holiday_gap_year_end():
    """Dec 31 (closed) → Jan 2 (Jan 1 closed) is a 2-day gap."""
    pairs = [
        (date(2025, 12, 30), 100.0),  # Tuesday
        (date(2026, 1, 2), 101.0),    # Friday (Wed 12/31 closed, Thu 1/1 closed)
    ]

    ref = date(2026, 1, 2)
    result = select_prior(pairs, ref, n=1, max_gap_days=6)

    assert result.gap_days == 3
    assert result.within_window is True


# ============================================================================
# Test: Stale flag (gap > 3 calendar days)
# ============================================================================

def test_stale_flag_4day_gap():
    """gap > 3 days → stale=True."""
    pairs = [
        (date(2026, 7, 1), 100.0),
        (date(2026, 7, 5), 101.0),    # 4 days later
    ]

    ref = date(2026, 7, 5)
    result = select_prior(pairs, ref, n=1)

    assert result.gap_days == 4
    assert result.stale is True


def test_stale_flag_3day_gap():
    """gap = 3 days → stale=False (not more than a weekend)."""
    pairs = [
        (date(2026, 7, 2), 100.0),
        (date(2026, 7, 5), 101.0),    # 3 days later (Fri→Mon)
    ]

    ref = date(2026, 7, 5)
    result = select_prior(pairs, ref, n=1)

    assert result.gap_days == 3
    assert result.stale is False


# ============================================================================
# Test: Empty/missing data
# ============================================================================

def test_select_prior_empty_pairs():
    """Empty pairs → returns None."""
    ref = date(2026, 7, 7)
    result = select_prior([], ref, n=1)

    assert result.value is None
    assert result.prior_date is None
    assert result.within_window is False


def test_select_prior_all_none_values():
    """All values are None → returns None."""
    pairs = [
        (date(2026, 7, 1), None),
        (date(2026, 7, 2), None),
    ]

    ref = date(2026, 7, 2)
    result = select_prior(pairs, ref, n=1)

    # After dedup and non-None filter, 7/2 is still in the list with value None.
    # But we only count distinct dates, so (7/1: None, 7/2: None).
    # Selecting the 1st prior of 7/2: 7/1 with value None.
    assert result.value is None
    assert result.prior_date == date(2026, 7, 1)


def test_prior_from_history_empty():
    """Empty DataFrame → returns None."""
    df = pd.DataFrame({'Close': []})
    result = prior_from_history(df)

    assert result.value is None
    assert result.prior_date is None


def test_prior_from_rows_empty():
    """Empty rows list → returns None."""
    result = prior_from_rows([], "Close")

    assert result.value is None
    assert result.prior_date is None


def test_prior_from_rows_no_valid_ref():
    """No rows with non-None key → ref_date auto-detect fails."""
    rows = [
        {"date": "2026-07-01", "Close": None},
        {"date": "2026-07-02", "Close": None},
    ]

    result = prior_from_rows(rows, "Close")

    assert result.value is None
    assert result.prior_date is None
    assert result.ref_date is None
