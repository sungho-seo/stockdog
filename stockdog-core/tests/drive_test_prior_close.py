#!/usr/bin/env python3
"""Minimal pytest-free driver for tests/test_prior_close.py.

pytest is not installed on host or in the container. This driver imports the
test module, resolves @pytest.fixture functions by parameter name, and runs
every test_* function, reporting pass/fail. Run in the container (has pandas).
"""
import sys
import inspect
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

# Provide a no-op `pytest.fixture` so the module imports without real pytest.
import types
if "pytest" not in sys.modules:
    fake = types.ModuleType("pytest")
    def fixture(func=None, **kw):
        def wrap(f):
            f.__is_fixture__ = True
            return f
        return wrap(func) if func else wrap
    fake.fixture = fixture
    sys.modules["pytest"] = fake

import importlib.util
spec = importlib.util.spec_from_file_location(
    "test_prior_close", Path(__file__).parent / "test_prior_close.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

# Collect fixtures (functions decorated so they still exist as callables).
fixtures = {}
for name, obj in inspect.getmembers(mod, inspect.isfunction):
    if getattr(obj, "__is_fixture__", False):
        fixtures[name] = obj

passed, failed = 0, 0
failures = []
for name, obj in sorted(inspect.getmembers(mod, inspect.isfunction)):
    if not name.startswith("test_"):
        continue
    sig = inspect.signature(obj)
    kwargs = {}
    ok = True
    for pname in sig.parameters:
        if pname in fixtures:
            kwargs[pname] = fixtures[pname]()
        else:
            ok = False
            break
    if not ok:
        continue
    try:
        obj(**kwargs)
        passed += 1
        print(f"PASS {name}")
    except AssertionError as e:
        failed += 1
        failures.append((name, f"AssertionError: {e}"))
        print(f"FAIL {name}: AssertionError: {e}")
    except Exception as e:
        failed += 1
        failures.append((name, f"{type(e).__name__}: {e}"))
        print(f"ERROR {name}: {type(e).__name__}: {e}")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
