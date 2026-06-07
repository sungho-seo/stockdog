#!/usr/bin/env python3
"""Render narrative detail pages (IMPR-071 D2).

Host-side, stdlib only (json, sys, datetime, pathlib). Reads each narrative from
raw/stockdog/narrative/archive/<date>.json and writes individual detail pages:
    <vault_root>/10_Public/daily-stories/<date>.md

raw/ is READ-ONLY — this script only reads from archive/*.json and writes under
10_Public/. Narratives with status != "ok" are skipped (never rendered).

Usage:
    render_stories_detail.py <vault_root> [<date>]
    - With <date>: render only that single narrative
    - Without <date>: render all narratives in archive (idempotent overwrite)
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


def render_detail_page(vault_root: Path, narrative: dict) -> bool:
    """Write a single detail page for one narrative.

    Returns True if successful, False otherwise.
    """
    report_date = narrative.get("report_date")
    if not report_date:
        return False

    if narrative.get("status") != "ok":
        return False

    nv = narrative.get("narrative", {})
    hero = nv.get("hero_oneliner", "")
    market_info = nv.get("market_narrative", {})
    market_story = market_info.get("story", "")
    market_keywords = market_info.get("keywords", [])

    macro_info = nv.get("macro_story", {})
    macro_story = macro_info.get("story", "")
    kr_impact = macro_info.get("kr_impact", "")

    indicator_captions = nv.get("indicator_captions", [])

    m7_status = narrative.get("m7_status")
    m7_stories = narrative.get("m7_stories", []) if m7_status == "ok" else []

    # Build markdown
    lines = []
    lines.append("---")
    lines.append(f'title: "시장 이야기 — {report_date}"')
    lines.append("public: true")
    lines.append("type: note")
    lines.append(f"date: {report_date}")
    lines.append("tags:")
    lines.append("  - ctx/public")
    lines.append("  - stockdog")
    lines.append("  - daily-story")
    lines.append("---")
    lines.append("")

    # H1 with date + hero oneliner
    lines.append(f"# {report_date}")
    lines.append("")
    if hero:
        lines.append(f"*{hero}*")
        lines.append("")

    # Cross-link to original daily reports — only link reports that actually
    # exist in the published Garden source (10_Public/daily-reports/<date>-<region>.md).
    # KR cron is Mon-Fri and US is Tue-Sat, so on some dates only one region's
    # report exists; unconditionally emitting both would bake a broken link
    # (e.g. a Saturday narrative has a US report but no KR report).
    reports_dir = vault_root / "10_Public" / "daily-reports"
    cross_links = []
    if (reports_dir / f"{report_date}-us.md").is_file():
        cross_links.append(f"[미국 시장 분석](/daily-reports/{report_date}-us)")
    if (reports_dir / f"{report_date}-kr.md").is_file():
        cross_links.append(f"[한국 시장 분석](/daily-reports/{report_date}-kr)")

    if cross_links:
        lines.append("---")
        lines.append("")
        lines.append("## 같은 날 시장 보고서")
        lines.append("")
        lines.append("📄 " + " · ".join(cross_links))
        lines.append("")
        lines.append("---")
        lines.append("")

    # Market narrative section
    if market_story:
        lines.append("## 오늘의 시장")
        lines.append("")
        lines.append(market_story)
        lines.append("")
        if market_keywords:
            keyword_str = " · ".join(market_keywords)
            lines.append(f"**주제**: {keyword_str}")
            lines.append("")
        lines.append("")

    # Macro story section
    if macro_story:
        lines.append("## 매크로 환경")
        lines.append("")
        lines.append(macro_story)
        lines.append("")
        if kr_impact:
            lines.append("### 한국 시장 영향")
            lines.append("")
            lines.append(kr_impact)
            lines.append("")
        lines.append("")

    # Indicator captions section
    if indicator_captions:
        lines.append("## 지표 읽기")
        lines.append("")
        for item in indicator_captions:
            label = item.get("label", "")
            caption = item.get("caption", "")
            if label:
                lines.append(f"### {label}")
                lines.append("")
                if caption:
                    lines.append(caption)
                    lines.append("")
        lines.append("")

    # M7 stories section (only if m7_status == "ok")
    if m7_stories:
        lines.append("## 시장 신호등 (M7)")
        lines.append("")
        lines.append("주요 기술주의 움직임을 관찰한 결과입니다.")
        lines.append("")
        for m7 in m7_stories:
            ticker = m7.get("ticker", "")
            story = m7.get("story", "")
            if ticker and story:
                lines.append(f"### {ticker}")
                lines.append("")
                lines.append(story)
                lines.append("")
        lines.append("")

    # Footer disclaimer
    lines.append("---")
    lines.append("")
    lines.append("## 주의")
    lines.append("")
    lines.append(
        "이 페이지의 내용은 시장 관찰을 바탕으로 한 정성적 해석입니다. "
        "투자 판단의 근거가 될 수 없으며, "
        "모든 투자 결정은 개별 투자자의 책임입니다."
    )
    lines.append("")

    # Write output
    out_dir = vault_root / "10_Public" / "daily-stories"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"{report_date}.md"

    with out_file.open("w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))

    return True


def main():
    if len(sys.argv) < 2:
        print("Usage: render_stories_detail.py <vault_root> [<date>]", file=sys.stderr)
        sys.exit(1)

    vault_root = Path(sys.argv[1])
    archive_dir = vault_root / "raw" / "stockdog" / "narrative" / "archive"

    if not archive_dir.is_dir():
        print(
            f"[render_stories_detail.py] archive dir not found: {archive_dir}",
            file=sys.stderr
        )
        sys.exit(1)

    want_date = sys.argv[2] if len(sys.argv) > 2 else None

    rendered_count = 0
    if want_date:
        # Single date mode
        json_file = archive_dir / f"{want_date}.json"
        narrative = load_json(json_file)
        if narrative and render_detail_page(vault_root, narrative):
            rendered_count += 1
            print(f"[render_stories_detail.py] rendered {want_date}")
        else:
            print(
                f"[render_stories_detail.py] failed to render {want_date}",
                file=sys.stderr
            )
            sys.exit(1)
    else:
        # All narratives mode (idempotent)
        for json_file in sorted(archive_dir.glob("*.json")):
            narrative = load_json(json_file)
            if narrative and render_detail_page(vault_root, narrative):
                rendered_count += 1

    if rendered_count == 0:
        print(
            f"[render_stories_detail.py] no narratives rendered from {archive_dir}",
            file=sys.stderr
        )
        sys.exit(1)

    print(f"[render_stories_detail.py] rendered {rendered_count} detail page(s)")
    sys.exit(0)


if __name__ == "__main__":
    main()
