"""
Prior close / prior session baseline selection for change-% computation.

Handles gaps (weekends/holidays), None values, and duplicate-date deduplication.
"""
from datetime import date, datetime
from typing import NamedTuple, Iterable, Optional, Any

DEFAULT_MAX_GAP_DAYS = 6


class PriorResult(NamedTuple):
    value: Optional[float]
    prior_date: Optional[date]
    ref_date: Optional[date]
    gap_days: Optional[int]       # calendar days: ref_date - prior_date
    stale: bool                    # gap_days > 3 (crossed more than a weekend)
    within_window: bool            # gap_days <= max_gap_days


def _coerce(d) -> Optional[date]:
    """Coerce to date: 'YYYY-MM-DD' | datetime | pandas Timestamp | date -> date | None.

    Bad input returns None (row will be dropped); never raises.
    """
    if d is None:
        return None
    if isinstance(d, date) and not isinstance(d, datetime):
        return d
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, str):
        try:
            return datetime.strptime(d, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            return None
    # pandas Timestamp
    try:
        return d.date()
    except (AttributeError, TypeError):
        return None


def select_prior(
    pairs: Iterable[tuple[Any, Optional[float]]],  # (date-coercible, value)
    ref: Any,  # date-coercible
    *,
    n: int = 1,
    max_gap_days: int = DEFAULT_MAX_GAP_DAYS
) -> PriorResult:
    """Select the n-th valid distinct prior date before ref.

    Args:
        pairs: iterable of (date, value) tuples; dates are coerced via _coerce().
        ref: reference date to measure against; coerced via _coerce().
        n: which prior to select (1 = immediate prior, 5 = 5 distinct dates back).
               n=1 applies gap guard; n>1 skips it.
        max_gap_days: calendar days threshold; gap > this sets within_window=False.

    Returns:
        PriorResult with value/prior_date/ref_date/gap_days/stale/within_window.
        On "no valid prior" → value=None, prior_date=None, gap_days=None, within_window=False.

    Process:
        1. Coerce all dates; drop (coerce failures, None values, ref or after).
        2. Deduplicate by date, keeping LAST occurrence per date.
        3. Sort ascending by date.
        4. Pick the n-th distinct date strictly < ref.
        5. If n=1: apply gap guard (within_window = gap_days <= max_gap_days).
           If n>1: no guard, just return None if < n distinct priors available.
    """
    ref_date = _coerce(ref)
    if ref_date is None:
        return PriorResult(
            value=None, prior_date=None, ref_date=None,
            gap_days=None, stale=False, within_window=False
        )

    # Coerce, filter None/invalid, drop ref-and-after.
    clean_pairs = []
    for dt, val in pairs:
        d = _coerce(dt)
        if d is None:
            continue
        if d >= ref_date:
            continue
        clean_pairs.append((d, val))

    # Deduplicate by date, keeping LAST.
    by_date = {}
    for d, val in clean_pairs:
        by_date[d] = val

    # Sort ascending.
    sorted_pairs = sorted(by_date.items())

    # Select the n-th prior (n-th from the end).
    if len(sorted_pairs) < n:
        return PriorResult(
            value=None, prior_date=None, ref_date=ref_date,
            gap_days=None, stale=False, within_window=False
        )

    prior_date, value = sorted_pairs[-n]
    gap = (ref_date - prior_date).days
    stale = gap > 3
    within_window = gap <= max_gap_days if n == 1 else True

    return PriorResult(
        value=value, prior_date=prior_date, ref_date=ref_date,
        gap_days=gap, stale=stale, within_window=within_window
    )


def prior_from_history(
    hist,
    col: str = "Close",
    *,
    n: int = 1,
    max_gap_days: int = DEFAULT_MAX_GAP_DAYS
) -> PriorResult:
    """Select prior from a pandas DataFrame (DatetimeIndex, no duplicates expected).

    Args:
        hist: pandas DataFrame with DatetimeIndex (assumed oldest->newest, no dup dates).
        col: column name to extract values (default "Close").
        n: which prior to select (1 = prior session, etc.).
        max_gap_days: calendar days threshold for gap guard.

    Returns:
        PriorResult; ref_date = last index date.
    """
    if hist is None or hist.empty or len(hist) < 1:
        return PriorResult(
            value=None, prior_date=None, ref_date=None,
            gap_days=None, stale=False, within_window=False
        )

    # Extract pairs: (date, value) from index and column.
    pairs = []
    for idx, row in hist.iterrows():
        d = _coerce(idx)
        val = row.get(col) if hasattr(row, 'get') else row[col]
        try:
            val = float(val) if val is not None else None
        except (TypeError, ValueError):
            val = None
        pairs.append((d, val))

    ref_date = _coerce(hist.index[-1])
    return select_prior(pairs, ref_date, n=n, max_gap_days=max_gap_days)


def prior_from_rows(
    rows: list,
    key: str,
    *,
    ref_date: Optional[Any] = None,
    n: int = 1,
    max_gap_days: int = DEFAULT_MAX_GAP_DAYS
) -> PriorResult:
    """Select prior from a list of dicts with 'date' and a value key.

    Args:
        rows: list of dicts, each with 'date' and `key` fields.
        key: dict key for the value field (e.g. 'Close', 'short_ratio').
        ref_date: reference date; defaults to latest date whose key value is non-None.
                  Coerced via _coerce().
        n: which prior to select (1 = prior session, etc.).
        max_gap_days: calendar days threshold for gap guard.

    Returns:
        PriorResult; ref_date auto-detected if not provided.
    """
    if not rows:
        return PriorResult(
            value=None, prior_date=None, ref_date=None,
            gap_days=None, stale=False, within_window=False
        )

    # Auto-detect ref_date if not provided: latest date with non-None key value.
    if ref_date is None:
        for row in reversed(rows):
            val = row.get(key)
            if val is not None:
                ref_date = row.get("date")
                break

    if ref_date is None:
        return PriorResult(
            value=None, prior_date=None, ref_date=None,
            gap_days=None, stale=False, within_window=False
        )

    # Build pairs: (date, value).
    pairs = []
    for row in rows:
        d = row.get("date")
        val = row.get(key)
        try:
            val = float(val) if val is not None else None
        except (TypeError, ValueError):
            val = None
        pairs.append((d, val))

    return select_prior(pairs, ref_date, n=n, max_gap_days=max_gap_days)
