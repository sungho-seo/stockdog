#!/usr/bin/env python3
"""Dependency-light runner for the pandas-based prior_from_history checks.

pytest is not installed anywhere (host or container), so this driver exercises
the pandas adapter paths directly. Run inside the stockdog container (has pandas).
"""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
from utils.prior_close import prior_from_history

failures = []

def check(name, cond, msg=""):
    if cond:
        print(f"OK  {name}")
    else:
        print(f"FAIL {name}: {msg}")
        failures.append(name)


# 1. Clean DataFrame — prior = second-to-last row
dates = pd.date_range('2026-06-27', periods=5, freq='D')
df = pd.DataFrame({'Close': [1535.00, 1535.24, 1551.00, 1545.97, 1553.22]}, index=dates)
r = prior_from_history(df)
check("clean n=1 value", r.value == 1545.97, f"got {r.value}")
check("clean n=1 date", r.prior_date == date(2026, 6, 30), f"got {r.prior_date}")
check("clean n=1 ref", r.ref_date == date(2026, 7, 1), f"got {r.ref_date}")
check("clean n=1 gap", r.gap_days == 1, f"got {r.gap_days}")

# 2. n=5 on a 10-row clean frame — dates 6/27..7/6, closes 100..109, ref 7/6
dates = pd.date_range('2026-06-27', periods=10, freq='D')
df = pd.DataFrame({'Close': list(range(100, 110))}, index=dates)
r = prior_from_history(df, n=5)
check("n=5 value", r.value == 104.0, f"got {r.value}")
check("n=5 date", r.prior_date == date(2026, 7, 1), f"got {r.prior_date}")
check("n=5 gap", r.gap_days == 5, f"got {r.gap_days}")
check("n=5 within_window", r.within_window is True, f"got {r.within_window}")

# 3. REGRESSION INVARIANT: clean series, n=1 == iloc[-2], n=5 == iloc[-6]
dates = pd.date_range('2026-06-20', periods=15, freq='D')
closes = [round(200 + i * 1.37, 2) for i in range(15)]
df = pd.DataFrame({'Close': closes}, index=dates)
r1 = prior_from_history(df, n=1)
check("regression n=1 == iloc[-2]", r1.value == closes[-2], f"got {r1.value} vs {closes[-2]}")
r5 = prior_from_history(df, n=5)
check("regression n=5 == iloc[-6]", r5.value == closes[-6], f"got {r5.value} vs {closes[-6]}")

# 4. Empty frame → None
r = prior_from_history(pd.DataFrame({'Close': []}))
check("empty value None", r.value is None, f"got {r.value}")

# 5. Gap: a frame whose only prior is 9 days before ref (n=1 gap guard)
idx = pd.DatetimeIndex([pd.Timestamp('2026-06-25'), pd.Timestamp('2026-07-04')])
df = pd.DataFrame({'Close': [100.0, 101.0]}, index=idx)
r = prior_from_history(df, max_gap_days=6)
check("gap9 within_window False", r.within_window is False, f"got {r.within_window}")
check("gap9 gap_days", r.gap_days == 9, f"got {r.gap_days}")

print()
if failures:
    print(f"FAILED: {len(failures)} checks -> {failures}")
    sys.exit(1)
print("ALL PANDAS CHECKS PASSED")
sys.exit(0)
