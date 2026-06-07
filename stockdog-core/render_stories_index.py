#!/usr/bin/env python3
"""Render the narrative timeline index (IMPR-071 D1).

Host-side, stdlib only (json, sys, datetime, pathlib). Reads the archived narrative
JSON files from raw/stockdog/narrative/archive/ and writes a single public index page:
    <vault_root>/10_Public/daily-stories/index.md

raw/ is READ-ONLY — this script only reads from archive/*.json and writes under
10_Public/. Narratives with status != "ok" are skipped (never rendered).

Cadence note: archive grows 1 file per day when daily-market report exists and
narrative generation succeeds (gated in generate_narrative.py).

Usage:
    render_stories_index.py <vault_root> [<date>]   # date arg ignored (full index)
"""

import json
import sys
from datetime import datetime
from pathlib import Path


def load_json(path: Path):
    """Read a JSON file; None on absence / parse error."""
    if not path.is_file():
        return None
    try:
        with path.open(encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def main():
    if len(sys.argv) < 2:
        print("Usage: render_stories_index.py <vault_root> [<date>]", file=sys.stderr)
        sys.exit(1)

    vault_root = Path(sys.argv[1])
    archive_dir = vault_root / "raw" / "stockdog" / "narrative" / "archive"

    # Collect all ok narratives, newest first
    narratives = []
    if archive_dir.is_dir():
        for json_file in sorted(archive_dir.glob("*.json"), reverse=True):
            data = load_json(json_file)
            if data and data.get("status") == "ok":
                narratives.append(data)

    # If no narratives, exit non-zero to skip (match tracker pattern)
    if not narratives:
        print(f"[render_stories_index.py] no narratives found in {archive_dir}", file=sys.stderr)
        sys.exit(1)

    # Build index markdown
    lines = []
    lines.append("---")
    lines.append('title: "지난 이야기"')
    lines.append("public: true")
    lines.append("type: note")
    lines.append(f"date: {datetime.now().strftime('%Y-%m-%d')}")
    lines.append("tags:")
    lines.append("  - ctx/public")
    lines.append("  - stockdog")
    lines.append("  - daily-story")
    lines.append("---")
    lines.append("")
    lines.append("# 지난 이야기")
    lines.append("")
    lines.append("시장의 이야기를 모은 타임라인입니다. 최신부터 역순으로 정렬됩니다.")
    lines.append("")

    # Cards for each narrative
    for narrative in narratives:
        report_date = narrative.get("report_date", "Unknown")
        hero = narrative.get("narrative", {}).get("hero_oneliner", "")
        keywords = narrative.get("narrative", {}).get("market_narrative", {}).get("keywords", [])

        # Date + hero_oneliner as header with link to detail page
        lines.append(f"## [{report_date}](/daily-stories/{report_date})")
        lines.append("")

        if hero:
            lines.append(f"*{hero}*")
            lines.append("")

        # Keywords as comma-separated chips
        if keywords:
            keyword_str = " · ".join(keywords)
            lines.append(f"**주제**: {keyword_str}")
            lines.append("")

        lines.append("---")
        lines.append("")

    # Write output
    out_dir = vault_root / "10_Public" / "daily-stories"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "index.md"

    with out_file.open("w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))

    print(f"[render_stories_index.py] wrote {out_file}")
    sys.exit(0)


if __name__ == "__main__":
    main()
