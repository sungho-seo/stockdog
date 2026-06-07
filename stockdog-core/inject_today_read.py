#!/usr/bin/env python3
"""Inject analyst interpretation text into the TODAY_READ block of signals.md.

IMPR-065: the "오늘의 읽기" LLM layer. The signals.md tracker (rendered by
render_signals_tracker.py) carries a placeholder block:

    <!-- TODAY_READ:START YYYY-MM-DD -->
    <!-- TODAY_READ:END -->

This helper replaces that block (markers inclusive) with the analyst's
interpretation. It is idempotent: it replaces whatever block currently exists
(any START date, or none) with a freshly-dated one, so it can be re-run safely.

Usage:
    python3 inject_today_read.py <signals_md_path> <date> <text_file_path>

Exit codes:
    0  success
    1  markers not found (nothing written)
    2  interpretation text empty / whitespace-only (nothing written)

Host stdlib only.
"""

import json
import os
import re
import sys
from pathlib import Path

# Match the whole TODAY_READ block, markers inclusive. The START marker's date
# is optional/loose here (any date or none) so we can replace placeholder or a
# previously-injected block. DOTALL so the body spans newlines.
BLOCK_RE = re.compile(
    r"<!--\s*TODAY_READ:START(?:\s+\S+)?\s*-->.*?<!--\s*TODAY_READ:END\s*-->",
    re.DOTALL,
)

# Defensive: an analyst body must never contain a literal END marker (would
# break the block). Neutralize any START/END marker tokens in the text.
MARKER_TOKEN_RE = re.compile(r"TODAY_READ:(START|END)")


def build_block(date: str, text: str) -> str:
    return (
        f"<!-- TODAY_READ:START {date} -->\n"
        f"## 🧭 오늘의 읽기\n"
        f"\n"
        f"{text}\n"
        f"\n"
        f"*— analyst 해석, 비매매 참고. 생성: {date}.*\n"
        f"<!-- TODAY_READ:END -->"
    )


def atomic_write(path: Path, content: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def _write_signals_archive(signals_path: Path, date: str, today_read: str = None) -> None:
    """Write/update the signals archive entry with confirmed read text.

    IMPR-067 ongoing archive — called AFTER a successful inject to update
    the existing archive entry (written first by render_signals_tracker.py
    with counts + null read) with the confirmed LLM-generated read text.

    Reads the existing archive to preserve major/watch/notable, then overwrites
    with the new read text. Always idempotent: same date overwrites.

    Non-fatal: archive write failure must never break the inject flow.
    """
    try:
        # Derive archive dir from signals_path (signals_path is typically
        # .../10_Public/trackers/signals.md → archive should be
        # .../raw/stockdog/signals/archive/)
        vault_root = signals_path.parent.parent.parent
        archive_dir = vault_root / "raw" / "stockdog" / "signals" / "archive"
        archive_dir.mkdir(parents=True, exist_ok=True)

        archive_path = archive_dir / f"{date}.json"

        # Read existing entry to preserve counts
        major, watch, notable = 0, 0, False
        if archive_path.is_file():
            try:
                existing = json.loads(archive_path.read_text(encoding="utf-8"))
                major = existing.get("major", 0)
                watch = existing.get("watch", 0)
                notable = existing.get("notable", False)
            except (OSError, ValueError):
                pass

        # Update with confirmed read text
        archive_entry = {
            "date": date,
            "today_read": today_read,
            "major": major,
            "watch": watch,
            "notable": notable,
        }

        archive_json = json.dumps(archive_entry, ensure_ascii=False, indent=2)

        # Atomic write (temp + os.replace)
        tmp_path = archive_path.with_suffix(archive_path.suffix + ".tmp")
        tmp_path.write_text(archive_json, encoding="utf-8")
        os.replace(tmp_path, archive_path)

    except Exception as e:
        # Non-fatal: archive write failure must never break inject.
        # Just log and continue.
        print(f"warning: signals archive write failed ({e}) — continuing", file=sys.stderr)


def main() -> int:
    if len(sys.argv) != 4:
        print(
            "usage: inject_today_read.py <signals_md_path> <date> <text_file_path>",
            file=sys.stderr,
        )
        return 1

    signals_path = Path(sys.argv[1]).expanduser()
    date = sys.argv[2]
    text_path = Path(sys.argv[3]).expanduser()

    try:
        signals = signals_path.read_text(encoding="utf-8")
    except OSError as e:
        print(f"error: cannot read signals file: {e}", file=sys.stderr)
        return 1

    try:
        raw_text = text_path.read_text(encoding="utf-8")
    except OSError as e:
        print(f"error: cannot read text file: {e}", file=sys.stderr)
        return 1

    text = raw_text.strip()
    if not text:
        print("error: interpretation text is empty/whitespace; nothing to inject", file=sys.stderr)
        return 2

    # Defensive marker neutralization (analyst won't, but guard).
    text = MARKER_TOKEN_RE.sub(r"TODAY_READ_\1", text)

    if not BLOCK_RE.search(signals):
        print("error: TODAY_READ markers not found in signals file; not writing", file=sys.stderr)
        return 1

    new_block = build_block(date, text)
    # Use a function replacement to avoid re backreference interpretation of \  in text.
    updated = BLOCK_RE.sub(lambda _m: new_block, signals, count=1)

    atomic_write(signals_path, updated)

    print(f"injected {len(text)} chars into TODAY_READ block (date {date}) -> {signals_path}")

    # IMPR-067 ongoing — update archive entry with confirmed read text.
    # The entry was first written by render_signals_tracker.py with counts + null read;
    # this updates it with the confirmed LLM-generated text. Non-fatal failure.
    _write_signals_archive(signals_path, date, text)

    return 0


if __name__ == "__main__":
    sys.exit(main())
