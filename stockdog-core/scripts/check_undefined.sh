#!/usr/bin/env bash
# Standing F821/F822 gate. Fails (exit 1) ONLY on undefined-name / undefined-export.
# Benign codes (F401 unused-import, F541, unused-local) are intentionally NOT enforced.
# $0, local, stdlib pyflakes. Would have caught the _FORBIDDEN_RE NameError before shipping.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 2
PYFLAKES="python3 -m pyflakes"
if ! python3 -c "import pyflakes" 2>/dev/null; then
    if command -v pyflakes >/dev/null 2>&1; then PYFLAKES="pyflakes"
    else echo "check_undefined: pyflakes not available (pip install --user pyflakes)" >&2; exit 2; fi
fi
mapfile -t FILES < <(git ls-files '*.py' 2>/dev/null)
if [ "${#FILES[@]}" -eq 0 ]; then mapfile -t FILES < <(find . -name '*.py' -not -path './__pycache__/*'); fi
[ "${#FILES[@]}" -eq 0 ] && { echo "check_undefined: no .py files"; exit 0; }
HITS="$($PYFLAKES "${FILES[@]}" 2>&1 | grep 'undefined name' || true)"
if [ -n "$HITS" ]; then
    echo "❌ check_undefined: undefined name(s) detected (F821/F822) — commit/build BLOCKED:" >&2
    echo "$HITS" >&2
    echo "Fix the missing import/symbol (file:line:col: undefined name 'X')." >&2
    exit 1
fi
echo "✅ check_undefined: 0 undefined names across ${#FILES[@]} tracked .py files."
exit 0
