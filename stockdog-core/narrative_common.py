#!/usr/bin/env python3
"""IMPR-073 D0 — Common helpers shared by daily and weekly narrative generators.

Extracted from generate_narrative.py:
- Forbidden word regex + validation
- Wall-clock timeout handler
- Log helper
- Source extractors (daily-market report, macro, F&G, signals)
- LLM getter with Sonnet 4.6 binding
- Output writer with content_type support
- Schema validation helpers

Ensures narrative path (daily narrative generation) remains unchanged.
"""

import json
import os
import re
import signal
import sys
from datetime import datetime
from pathlib import Path

LOG_PREFIX = "[narrative]"
LLM_WALL_CLOCK_SECONDS = 120
SCHEMA_VERSION = 1

# M7 universe — canonical order, locked with config/m7.yaml
_M7_TICKERS = ["AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "TSLA"]

# Sparkline chars to strip from macro text (avoid token waste / rendering noise)
_SPARKLINE_RE = re.compile(r"[▁▂▃▄▅▆▇█]+")

# Forbidden words (투자 권유·단정 표현) — regex for post-generation scan.
_FORBIDDEN_RE = re.compile(
    r"매수하[세시]|매도하[세시]"          # 매수하세요 / 매도하세요 / 매수하시 / 매도하시
    r"|매수할|매도할"                     # 매수할 때 / 매도할 때 (활용형)
    r"|매수 추천|매도 추천"               # 명시적 투자 추천
    r"|추천합니다|추천드|강력 추천|강추"  # 추천 동사·단어 (bare 추천 아님)
    r"|목표가|목표 주가"                  # 목표가류
    r"|비중 확대|비중확대|비중 축소|비중축소"  # 비중 조언
    r"|풀매수|손절|익절"                  # 포지션 조언
    r"|사세요|파세요|담으세요|정리하세요"  # ~세요 조언형 동사
    r"|사야|팔아야|사라|팔아라"           # 명령·당위형 (기존 패턴 유지)
    r"|지금 사|지금 팔"                   # "지금 사/팔" (기존 패턴 유지)
)

# Whitelist of H2 sections to include from the US market report
_REPORT_SECTION_WHITELIST = {"시장 지표", "단기 전망"}


def log(msg: str) -> None:
    """Log message with prefix."""
    print(f"{LOG_PREFIX} {msg}", flush=True)


def alert_generation_failure(generator: str, run_date: str, error_class: str, detail: str = "") -> None:
    """Telegram alert + greppable log marker on a REAL generation failure.
    Call ONLY on post-gate errors (after get_llm()): LLM exception/timeout, empty output,
    json_parse/schema_validation/forbidden_word after retries, write_failure, or last-resort except.
    NEVER on legit gate skips (no report / idempotent / no-LLM-key / thin-week). Always swallows its own errors."""
    marker = f"!!ALERT!! {generator} narrative generation FAILED for {run_date} ({error_class})"
    log(marker + (f" — {detail}" if detail else ""))
    try:
        from utils.notifier import send_telegram_message
        msg = f"⚠️ {generator} narrative 실패\n날짜: {run_date}\n원인: {error_class}"
        if detail:
            msg += f"\n{detail[:200]}"
        send_telegram_message(msg, parse_mode=None)
    except Exception as e:
        log(f"alert_generation_failure: notify failed ({e}) — continuing")


class _WallClockTimeout(Exception):
    """Wall-clock timeout exception."""
    pass


def _alarm_handler(signum, frame):
    raise _WallClockTimeout()


# ---------------------------------------------------------------------------
# Gate helpers
# ---------------------------------------------------------------------------

def check_report_exists(notes_root: Path, run_date: str) -> Path | None:
    """Return path to Market_Report_US_<date>.md if it exists, else None."""
    report_path = (
        notes_root / "raw" / "stockdog" / "daily-market"
        / run_date / f"Market_Report_US_{run_date}.md"
    )
    if report_path.is_file():
        return report_path
    log(f"US market report not found at {report_path} — skip (no LLM call)")
    return None


def check_idempotent(notes_root: Path, run_date: str, content_type: str = "daily", require_m7_ok: bool = False) -> bool:
    """Return True if we should SKIP (already have a good narrative for today).

    Args:
        notes_root: Path to vault root
        run_date: Date as YYYY-MM-DD
        content_type: "daily" | "weekly" — only skip if matching content_type
        require_m7_ok: If True (daily only), also require m7_status=="ok" for skip (idempotent retry on M7 failure)

    Returns:
        True if narrative.json already has status=="ok" for run_date with matching content_type
        and (for daily with require_m7_ok=True) also m7_status=="ok"
    """
    out_path = notes_root / "raw" / "stockdog" / "narrative" / "narrative.json"
    if not out_path.is_file():
        return False
    try:
        existing = json.loads(out_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False

    # Base condition: report_date, status, schema_version, content_type match
    if not (
        existing.get("report_date") == run_date
        and existing.get("status") == "ok"
        and existing.get("schema_version") == SCHEMA_VERSION
        and existing.get("content_type") == content_type
    ):
        return False

    # For daily with require_m7_ok=True, also check m7_status=="ok"
    if content_type == "daily" and require_m7_ok:
        if existing.get("m7_status") != "ok":
            return False
        log(f"narrative.json already ok+m7_ok for {run_date} — skip (idempotent, no LLM call)")
        return True

    # For weekly (or daily with require_m7_ok=False), skip if status==ok and content_type matches
    log(f"narrative.json already ok for {run_date} (content_type={content_type}) — skip (idempotent, no LLM call)")
    return True


# ---------------------------------------------------------------------------
# Source excerpt builders
# ---------------------------------------------------------------------------

def extract_report_sections(report_path: Path, run_date: str) -> tuple[str, str]:
    """Return (excerpt_text, data_as_of) from the US market report.

    Includes: frontmatter + [!summary] callout + whitelisted H2 sections.
    Excludes: portfolio tables, leverage ETF rows, M7 insider tables.
    """
    try:
        text = report_path.read_text(encoding="utf-8")
    except OSError as e:
        log(f"cannot read report ({e})")
        return "(오늘은 이 데이터가 없습니다)", run_date

    # Extract data_as_of from frontmatter
    data_as_of = run_date
    fm_match = re.search(r"^data_as_of:\s*(.+)$", text, re.MULTILINE)
    if fm_match:
        data_as_of = fm_match.group(1).strip()

    # Split on H2 boundaries ("## …")
    parts = re.split(r"(?=^## )", text, flags=re.MULTILINE)

    # Always include frontmatter + [!summary] block (everything before first ##)
    preamble = parts[0] if parts else ""
    # Trim preamble to frontmatter + summary callout only (first ~50 lines max)
    preamble_lines = preamble.splitlines()
    # Find [!summary] end — keep up to first blank line after it, cap at 60 lines
    summary_end = len(preamble_lines)
    in_summary = False
    for i, line in enumerate(preamble_lines):
        if "[!summary]" in line:
            in_summary = True
        if in_summary and i > 10 and line.strip() == "":
            summary_end = i + 1
            break
    preamble = "\n".join(preamble_lines[:min(summary_end, 60)])

    # Extract whitelisted sections
    selected = [preamble.strip()]
    for part in parts[1:]:
        header_match = re.match(r"^## (.+)", part)
        if not header_match:
            continue
        section_name = header_match.group(1).strip()
        if section_name in _REPORT_SECTION_WHITELIST:
            selected.append(part.strip())

    excerpt = "\n\n".join(selected)
    # Cap at ~3000 chars to stay within token budget
    return excerpt[:3000], data_as_of


def extract_macro_excerpt(notes_root: Path) -> str:
    """Return macro tracker text with sparkline chars stripped."""
    macro_path = notes_root / "10_Public" / "trackers" / "macro.md"
    if not macro_path.is_file():
        return "(오늘은 이 데이터가 없습니다)"
    try:
        text = macro_path.read_text(encoding="utf-8")
    except OSError:
        return "(오늘은 이 데이터가 없습니다)"

    # Strip sparkline chars
    text = _SPARKLINE_RE.sub("", text)

    # Keep only the relevant sections: 시장 컨텍스트, 금리 곡선, 인플레이션,
    # 정책, 환율, 심리 컨텍스트 (and the leading context/실질10Y)
    # Strategy: split on H2, keep named sections, skip frontmatter header block
    parts = re.split(r"(?=^## )", text, flags=re.MULTILINE)
    keep_sections = {
        "시장 컨텍스트", "금리 곡선 (UST)", "인플레이션",
        "정책 (Fed)", "환율 / 달러", "심리 컨텍스트 (참조)",
    }
    selected = []
    for part in parts:
        header_match = re.match(r"^## (.+)", part)
        if header_match and header_match.group(1).strip() in keep_sections:
            selected.append(part.strip())

    result = "\n\n".join(selected) if selected else text[:2000]
    return result[:2000]


def extract_fear_greed(notes_root: Path, run_date: str) -> str:
    """Return scalar Fear & Greed object (score/rating/previous_*). No history."""
    fg_path = (
        notes_root / "raw" / "stockdog" / "daily-market"
        / run_date / "media" / "fear_greed.json"
    )
    if not fg_path.is_file():
        return "(오늘은 이 데이터가 없습니다)"
    try:
        raw = json.loads(fg_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "(오늘은 이 데이터가 없습니다)"

    fg = raw.get("fear_and_greed", {})
    scalar = {
        "score": round(float(fg.get("score", 0))),
        "rating": fg.get("rating", ""),
        "previous_close": fg.get("previous_close"),
        "previous_1_week": fg.get("previous_1_week"),
        "previous_1_month": fg.get("previous_1_month"),
    }
    return json.dumps(scalar, ensure_ascii=False, indent=2)


def extract_sector_snapshot(notes_root: Path, ref_date: str, *, mode: str, stale_days: int) -> str:
    """Extract sector/theme rotation context for daily/weekly/preview narratives.

    Reads sectors_snapshot.json and builds a Korean-language grounding string.
    Returns placeholder "(섹터 데이터 없음)" on any failure.

    Args:
        notes_root: Path to vault root
        ref_date: Reference date (YYYY-MM-DD) to check staleness against asof
        mode: "daily", "weekly", or "preview" — determines emphasis and composition
        stale_days: Threshold in calendar days; if snapshot older, add staleness label

    Returns:
        Korean grounding string listing sectors + theme(s),
        with staleness marker if applicable.
    """
    # English → Korean GICS sector name map
    _GICS_TO_KR = {
        "Technology": "기술",
        "Communication": "커뮤니케이션",
        "Consumer Discretionary": "경기소비재",
        "Consumer Staples": "필수소비재",
        "Energy": "에너지",
        "Financials": "금융",
        "Healthcare": "헬스케어",
        "Industrials": "산업재",
        "Materials": "소재",
        "Utilities": "유틸리티",
        "Real Estate": "리츠",
    }

    snapshot_path = notes_root / "raw" / "stockdog" / "sectors" / "sectors_snapshot.json"
    if not snapshot_path.is_file():
        return "(섹터 데이터 없음)"

    try:
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "(섹터 데이터 없음)"

    asof = snapshot.get("asof")
    if not asof:
        return "(섹터 데이터 없음)"

    sectors = snapshot.get("sectors", [])
    themes = snapshot.get("themes", [])

    if not sectors:
        return "(섹터 데이터 없음)"

    # Check staleness
    staleness_prefix = ""
    try:
        asof_date = datetime.strptime(asof, "%Y-%m-%d").date()
        ref_date_obj = datetime.strptime(ref_date, "%Y-%m-%d").date()
        days_old = (ref_date_obj - asof_date).days
        if days_old > stale_days:
            staleness_prefix = f"※ 기준일 {asof} (다소 지연) — "
    except (ValueError, Exception):
        pass  # If date parsing fails, skip staleness marker

    if mode == "daily":
        return _extract_sector_snapshot_daily(
            staleness_prefix, sectors, themes, _GICS_TO_KR
        )
    elif mode == "weekly":
        return _extract_sector_snapshot_weekly(
            staleness_prefix, sectors, themes, _GICS_TO_KR
        )
    else:  # preview
        return _extract_sector_snapshot_preview(
            staleness_prefix, sectors, themes, _GICS_TO_KR
        )


def _extract_sector_snapshot_daily(staleness_prefix: str, sectors: list, themes: list, gics_map: dict) -> str:
    """Build daily sector/theme grounding string.

    Composition order:
    1. Header: "섹터·테마 (당일·5일·1개월):"
    2. "선도(당일):": top-3 sectors by d1 desc
    3. "부진(당일):": bottom-1 sector by d1 asc
    4. "모멘텀 상위:": top-2 sectors by momentum desc (names only)
    5. "테마:": themes with momentum >= 0 (max 3), sorted by momentum desc
    """
    lines = [staleness_prefix + "섹터·테마 (당일·5일·1개월):"]

    # Compute rankings for tagging
    # For each metric (d1, d5, d1mo), rank sectors
    sectors_with_metrics = [s for s in sectors if s.get("d1") is not None]

    # Rank by d1 desc
    d1_ranked = sorted(sectors_with_metrics, key=lambda x: -x.get("d1", 0))
    d1_top_third = set(s.get("name") for s in d1_ranked[:max(1, len(d1_ranked) // 3)])

    # Rank by d5 desc
    d5_ranked = sorted(sectors_with_metrics, key=lambda x: -x.get("d5", 0))
    d5_top_third = set(s.get("name") for s in d5_ranked[:max(1, len(d5_ranked) // 3)])

    # Rank by d1mo desc
    d1mo_ranked = sorted(sectors_with_metrics, key=lambda x: -x.get("d1mo", 0))
    d1mo_top_third = set(s.get("name") for s in d1mo_ranked[:max(1, len(d1mo_ranked) // 3)])

    # Top-3 sectors by d1 desc
    top_3_d1 = d1_ranked[:3]

    lines.append("선도(당일):")
    for s in top_3_d1:
        kr_name = gics_map.get(s.get("name", ""), s.get("name", ""))
        d1 = s.get("d1")
        d5 = s.get("d5")
        d1mo = s.get("d1mo")
        sector_name = s.get("name", "")

        # Compute tag
        tag = _compute_daily_tag(sector_name, d1_top_third, d5_top_third, d1mo_top_third)

        line = f"· {kr_name} (당일 {d1:+.1f}% / 5일 {d5:+.1f}% / 1개월 {d1mo:+.1f}%){tag}"
        lines.append(line)

    # Bottom-1 sector by d1 asc (weakest)
    bottom_1_d1 = [d1_ranked[-1]] if d1_ranked else []

    lines.append("부진(당일):")
    for s in bottom_1_d1:
        kr_name = gics_map.get(s.get("name", ""), s.get("name", ""))
        d1 = s.get("d1")
        d5 = s.get("d5")
        d1mo = s.get("d1mo")

        line = f"· {kr_name} (당일 {d1:+.1f}% / 5일 {d5:+.1f}% / 1개월 {d1mo:+.1f}%)"
        lines.append(line)

    # Top-2 sectors by momentum desc (leadership-by-trend)
    sectors_with_momentum = [s for s in sectors if s.get("momentum") is not None]
    sectors_by_momentum = sorted(sectors_with_momentum, key=lambda x: -x.get("momentum", 0))
    top_2_momentum = sectors_by_momentum[:2]

    if top_2_momentum:
        momentum_line = "모멘텀 상위: "
        momentum_names = []
        for s in top_2_momentum:
            kr_name = gics_map.get(s.get("name", ""), s.get("name", ""))
            momentum_names.append(kr_name)
        momentum_line += ", ".join(momentum_names)
        lines.append(momentum_line)

    # Themes with momentum >= 0 (max 3), sorted by momentum desc
    themes_with_momentum = [t for t in themes if t.get("momentum") is not None and t.get("momentum", -999) >= 0]
    themes_sorted = sorted(themes_with_momentum, key=lambda x: -x.get("momentum", 0))[:3]

    if themes_sorted:
        lines.append("테마:")
        for t in themes_sorted:
            kr_name = t.get("name", "")
            d5 = t.get("d5")
            d1mo = t.get("d1mo")
            line = f"· {kr_name} (5일 {d5:+.1f}% / 1개월 {d1mo:+.1f}%)"
            lines.append(line)

    return "\n".join(lines)


def _compute_daily_tag(sector_name: str, d1_top_third: set, d5_top_third: set, d1mo_top_third: set) -> str:
    """Compute persistence tag for daily sector.

    Returns:
    - " — 지속(당일·5일·1개월 모두 상위)" if in top-third for ALL three horizons
    - " — 당일 한정(되돌림 가능)" if in top-third for d1 but NOT both d5 AND d1mo
    - "" otherwise
    """
    in_d1 = sector_name in d1_top_third
    in_d5 = sector_name in d5_top_third
    in_d1mo = sector_name in d1mo_top_third

    if in_d1 and in_d5 and in_d1mo:
        return " — 지속(당일·5일·1개월 모두 상위)"
    elif in_d1 and not (in_d5 and in_d1mo):
        return " — 당일 한정(되돌림 가능)"
    else:
        return ""


def _extract_sector_snapshot_weekly(staleness_prefix: str, sectors: list, themes: list, gics_map: dict) -> str:
    """Build weekly sector/theme grounding string (existing logic)."""
    lines = [staleness_prefix + "섹터·테마 로테이션:"]

    # Sort sectors by momentum (desc), filter out null momentum
    sectors_sorted = [s for s in sectors if s.get("momentum") is not None]
    sectors_sorted.sort(key=lambda x: -x.get("momentum", 0))

    # Extract top-3 and weakest-1 (last in sorted DESC list)
    top_3 = sectors_sorted[:3]
    weakest_1 = [sectors_sorted[-1]] if sectors_sorted else []

    # Add top-3 sectors
    if top_3:
        lines.append("선도:")
        for s in top_3:
            kr_name = gics_map.get(s.get("name", ""), s.get("name", ""))
            d5 = s.get("d5")
            d1mo = s.get("d1mo")
            # Weekly: emphasize d5 (지난 1주)
            lines.append(f"  · {kr_name} ({d5:+.1f}% 1주, {d1mo:+.1f}% 월)")

    # Add weakest-1 sector
    if weakest_1:
        lines.append("부진:")
        for s in weakest_1:
            kr_name = gics_map.get(s.get("name", ""), s.get("name", ""))
            d5 = s.get("d5")
            d1mo = s.get("d1mo")
            lines.append(f"  · {kr_name} ({d5:+.1f}% 1주, {d1mo:+.1f}% 월)")

    # Sort themes by momentum (desc), filter out null momentum
    themes_sorted = [t for t in themes if t.get("momentum") is not None]
    themes_sorted.sort(key=lambda x: -x.get("momentum", 0))

    # Add top theme(s) — keep themes that are already Korean and have momentum >= 0
    top_themes = [t for t in themes_sorted if t.get("momentum", -999) >= 0][:1]
    if top_themes:
        lines.append("테마:")
        for t in top_themes:
            # Theme names are already Korean (반도체, 소프트웨어, etc.)
            kr_name = t.get("name", "")
            d5 = t.get("d5")
            d1mo = t.get("d1mo")
            lines.append(f"  · {kr_name} ({d5:+.1f}% 1주, {d1mo:+.1f}% 월)")

    return "\n".join(lines)


def _extract_sector_snapshot_preview(staleness_prefix: str, sectors: list, themes: list, gics_map: dict) -> str:
    """Build preview sector/theme grounding string (existing logic)."""
    lines = [staleness_prefix + "섹터·테마 로테이션:"]

    # Sort sectors by momentum (desc), filter out null momentum
    sectors_sorted = [s for s in sectors if s.get("momentum") is not None]
    sectors_sorted.sort(key=lambda x: -x.get("momentum", 0))

    # Extract top-3 and weakest-1 (last in sorted DESC list)
    top_3 = sectors_sorted[:3]
    weakest_1 = [sectors_sorted[-1]] if sectors_sorted else []

    # Add top-3 sectors
    if top_3:
        lines.append("선도:")
        for s in top_3:
            kr_name = gics_map.get(s.get("name", ""), s.get("name", ""))
            d5 = s.get("d5")
            d1mo = s.get("d1mo")
            # Preview: emphasize d1mo + d5 as entering momentum
            lines.append(f"  · {kr_name} (진입모멘텀 {d1mo:+.1f}% 월 / {d5:+.1f}% 1주)")

    # Add weakest-1 sector
    if weakest_1:
        lines.append("부진:")
        for s in weakest_1:
            kr_name = gics_map.get(s.get("name", ""), s.get("name", ""))
            d5 = s.get("d5")
            d1mo = s.get("d1mo")
            lines.append(f"  · {kr_name} ({d5:+.1f}% 1주, {d1mo:+.1f}% 월)")

    # Sort themes by momentum (desc), filter out null momentum
    themes_sorted = [t for t in themes if t.get("momentum") is not None]
    themes_sorted.sort(key=lambda x: -x.get("momentum", 0))

    # Add top theme(s) — keep themes that are already Korean and have momentum >= 0
    top_themes = [t for t in themes_sorted if t.get("momentum", -999) >= 0][:1]
    if top_themes:
        lines.append("테마:")
        for t in top_themes:
            # Theme names are already Korean (반도체, 소프트웨어, etc.)
            kr_name = t.get("name", "")
            d5 = t.get("d5")
            d1mo = t.get("d1mo")
            lines.append(f"  · {kr_name} (진입모멘텀 {d1mo:+.1f}% 월 / {d5:+.1f}% 1주)")

    return "\n".join(lines)


def extract_signals_excerpt(notes_root: Path) -> str:
    """Return signals tracker excerpt: context line + 주요시그널 + 관찰 top~5 + 요약표.

    Explicitly EXCLUDES the TODAY_READ block and the 방법론 section.
    """
    signals_path = notes_root / "10_Public" / "trackers" / "signals.md"
    if not signals_path.is_file():
        return "(오늘은 이 데이터가 없습니다)"
    try:
        text = signals_path.read_text(encoding="utf-8")
    except OSError:
        return "(오늘은 이 데이터가 없습니다)"

    # Strip TODAY_READ block
    text = re.sub(
        r"<!-- TODAY_READ:START.*?-->.*?<!-- TODAY_READ:END -->",
        "",
        text,
        flags=re.DOTALL,
    )

    # Split on H2
    parts = re.split(r"(?=^## )", text, flags=re.MULTILINE)

    selected = []

    # Include the preamble up to first H2 (컨텍스트 한 줄 + static banner)
    if parts:
        preamble = parts[0].strip()
        # Keep only up to ~20 lines (avoid frontmatter bloat)
        preamble_lines = preamble.splitlines()
        # Find the static context line (starts with **컨텍스트**)
        ctx_idx = 0
        for i, line in enumerate(preamble_lines):
            if "컨텍스트" in line or "F&G" in line:
                ctx_idx = i
                break
        # Keep from ctx line onward, max 10 lines
        selected.append(
            "\n".join(preamble_lines[ctx_idx: ctx_idx + 10]).strip()
        )

    keep_sections = {"🔴 주요 시그널", "📊 트래커별 요약"}
    observe_section = None

    for part in parts[1:]:
        header_match = re.match(r"^## (.+)", part)
        if not header_match:
            continue
        section_name = header_match.group(1).strip()

        if section_name in keep_sections:
            selected.append(part.strip())
        elif section_name == "🟡 관찰":
            observe_section = part  # truncate to top ~5 bullets below

    # Truncate 관찰 section to 5 bullets
    if observe_section:
        lines = observe_section.splitlines()
        bullets = [l for l in lines if l.strip().startswith("-")]
        header_line = lines[0] if lines else "## 🟡 관찰"
        truncated = [header_line] + bullets[:5]
        if len(bullets) > 5:
            truncated.append(f"- +{len(bullets) - 5}건 (요약 참조)")
        selected.append("\n".join(truncated))

    result = "\n\n".join(selected)
    return result[:2000]


# ---------------------------------------------------------------------------
# LLM helper
# ---------------------------------------------------------------------------

def get_llm():
    """Get LLM instance configured for narrative tasks.

    Delegates to analysis.llm_analyzer.get_llm() which returns:
    - ChatAnthropic (claude-sonnet-4-6) if ANTHROPIC_API_KEY set
    - ChatGoogleGenerativeAI (gemini-3-pro-preview) if GEMINI_API_KEY set
    - None if neither API key configured

    This ensures narrative generation uses the same LLM provider fallback
    as the rest of the stockdog pipeline.
    """
    try:
        from analysis.llm_analyzer import get_llm as shared_get_llm
        return shared_get_llm()
    except Exception as e:
        log(f"get_llm import failed ({e})")
        return None


# ---------------------------------------------------------------------------
# Output writer
# ---------------------------------------------------------------------------

def write_output(notes_root: Path, run_date: str, data_as_of: str,
                 status: str, narrative,
                 content_type: str = "daily",
                 m7_status: str = "skipped", m7_stories=None,
                 weekly_fields: dict = None,
                 preview_fields: dict = None,
                 generator: str = "narrative") -> None:
    """Write narrative.json (always, even on skip/failure).

    Args:
        notes_root: Path to vault root
        run_date: Date as YYYY-MM-DD
        data_as_of: Data cutoff date (frontmatter from report)
        status: "ok" | "skipped"
        narrative: Narrative object (dict) or None
        content_type: "daily" | "weekly" | "preview" (default "daily")
        m7_status: "ok" | "skipped" (daily only)
        m7_stories: List of {ticker, story} dicts (daily only, or None)
        weekly_fields: Extra fields for weekly (e.g., hero_oneliner, themes, macro_flow, m7_weekly)
        preview_fields: Extra fields for preview (e.g., hero_oneliner, calendar, macro_position, positioning)

    IMPR-071: If status=="ok", also archive the full payload to
    archive/<run_date>.json for permanent data retention.
    """
    out_dir = notes_root / "raw" / "stockdog" / "narrative"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "narrative.json"

    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "report_date": run_date,
        "data_as_of": data_as_of,
        "status": status,
        "content_type": content_type,
    }

    # Daily-specific fields
    if content_type == "daily":
        payload["narrative"] = narrative
        payload["m7_status"] = m7_status
        payload["m7_stories"] = m7_stories if m7_status == "ok" else None
    # Weekly-specific fields
    elif content_type == "weekly":
        if weekly_fields:
            payload.update(weekly_fields)
    # Preview-specific fields
    elif content_type == "preview":
        if preview_fields:
            payload.update(preview_fields)

    try:
        out_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        log(f"wrote {out_path} (status={status}, content_type={content_type})")

        # IMPR-071 D0: Archive to date-stamped file only on success
        if status == "ok":
            archive_dir = out_dir / "archive"
            archive_dir.mkdir(parents=True, exist_ok=True)
            archive_path = archive_dir / f"{run_date}.json"
            try:
                archive_path.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                log(f"archived {archive_path}")
            except OSError as e:
                log(f"failed to archive ({e})")
    except OSError as e:
        log(f"failed to write output ({e})")
        alert_generation_failure(generator, run_date, "write_failure", str(e))


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def check_forbidden_words(obj: dict) -> list[str]:
    """Return list of forbidden-word violations found in any string value."""
    violations = []

    def _scan(v, path):
        if isinstance(v, str):
            m = _FORBIDDEN_RE.search(v)
            if m:
                violations.append(f"{path}: found '{m.group()}'")
        elif isinstance(v, dict):
            for k, sub in v.items():
                _scan(sub, f"{path}.{k}")
        elif isinstance(v, list):
            for i, sub in enumerate(v):
                _scan(sub, f"{path}[{i}]")

    _scan(obj, "narrative")
    return violations


def set_wall_clock(seconds: int = LLM_WALL_CLOCK_SECONDS) -> None:
    """Set wall-clock alarm for LLM call timeout."""
    signal.signal(signal.SIGALRM, _alarm_handler)
    signal.alarm(seconds)


def cancel_wall_clock() -> None:
    """Cancel wall-clock alarm."""
    signal.alarm(0)


# ---------------------------------------------------------------------------
# Positioning computation (Python-owned, deterministic)
# ---------------------------------------------------------------------------

def _scrub_positioning_text(text: str) -> str:
    """Scrub internal/English-jargon codes from positioning line.

    Transformations (IMPR-076):
    - (~Nd, building) → (표본 누적 중)   [regex: \(~\d+d,\s*building\)]
    - standalone (building) → (표본 누적 중)
    - (breach) → remove entirely (just drop the "(breach)" token)
    - (~Nd) → (최근 N일)   [regex: \(~(\d+)d\) → (최근 \1일)]
    - Keep Korean forms like ~5일 내 alone.
    """
    # 1. (~Nd, building) → (표본 누적 중)
    text = re.sub(r'\(~\d+d,\s*building\)', '(표본 누적 중)', text)

    # 2. standalone (building) → (표본 누적 중)
    text = re.sub(r'\(building\)', '(표본 누적 중)', text)

    # 3. (breach) → remove entirely
    text = re.sub(r'\(breach\)', '', text)

    # 4. (~Nd) → (최근 N일)  [only English form ~Nd, not Korean ~5일 내]
    text = re.sub(r'\(~(\d+)d\)', r'(최근 \1일)', text)

    return text


def compute_preview_positioning(live_flags: list, cs_cards: list, preview_top_n: int = 7) -> tuple[list, int]:
    """Compute deterministic positioning list for preview narrative.

    Returns (positioning_list, overflow) where:
    - positioning_list: list of {ticker, line, tier} dicts (capped at preview_top_n)
    - overflow: count of distinct tickers BEYOND the cap

    IMPR-076: Positioning is Python-owned — the LLM's positioning JSON is ignored.
    The line text comes from the scored flag's own text (the human-readable text
    from compute_scored_flags, NOT LLM-generated).

    DEDUP: Each ticker appears AT MOST ONCE. For each ticker with multiple candidates
    (from both live_flags and cs_cards), we keep the single best entry (highest score,
    tie-break preferring CS cards because they're richer/more informative).
    """
    from render_signals_tracker import SCORE_WATCH

    # Keep only flags with score >= SCORE_WATCH and domain in {short,insider,watchlist}
    filtered = [f for f in live_flags
                if f.get("score", 0) >= SCORE_WATCH
                and f.get("domain", "") in {"short", "insider", "watchlist"}]

    # Also keep CS cards (cross-signals)
    cs_selected = [c for c in cs_cards if c.get("cs") in ("CS-1", "CS-2", "CS-3")]

    # Combine all candidates
    combined = filtered + cs_selected

    # Group by ticker and keep best entry per ticker
    ticker_map = {}
    for item in combined:
        ticker = item.get("ticker", "—")
        score = item.get("score", 0)
        is_cs = "cs" in item  # True if this is a CS card

        if ticker not in ticker_map:
            ticker_map[ticker] = (score, is_cs, item)
        else:
            existing_score, existing_is_cs, existing_item = ticker_map[ticker]
            # Keep the item with higher score; if tied, prefer CS card
            if score > existing_score or (score == existing_score and is_cs and not existing_is_cs):
                ticker_map[ticker] = (score, is_cs, item)

    # Extract deduplicated items and rank by score DESC
    deduped = [item for _, _, item in ticker_map.values()]
    deduped.sort(key=lambda x: -x.get("score", 0))

    # Cap at preview_top_n
    capped = deduped[:preview_top_n]
    # Overflow = distinct tickers beyond the cap
    overflow = max(0, len(deduped) - preview_top_n)

    # Build output list: each item is {ticker, line, tier}
    positioning_list = []
    for item in capped:
        raw_line = item.get("text", "")
        # Scrub internal codes from the line (IMPR-076)
        scrubbed_line = _scrub_positioning_text(raw_line)
        positioning_list.append({
            "ticker": item.get("ticker", "—"),
            "line": scrubbed_line,
            "tier": item.get("tier", "C"),  # tier from the flag (or "C" for CS cards)
        })

    return positioning_list, overflow


# ---------------------------------------------------------------------------
# Robust JSON extraction
# ---------------------------------------------------------------------------

def extract_json_from_response(raw_text: str) -> dict | None:
    """Extract JSON object from LLM response, handling truncation and prose.

    Attempts to parse JSON by:
    1. Stripping markdown code fences (```json ... ``` or ``` ... ```)
    2. Extracting the outermost {...} if there's leading/trailing prose
    3. Calling json.loads on the extracted string

    Returns:
        Parsed dict on success, None on failure (logs reason).
    """
    if not raw_text or not raw_text.strip():
        log("extract_json_from_response: empty input")
        return None

    # Step 1: Strip markdown code fences
    stripped = re.sub(r"^```(?:json)?\s*", "", raw_text, flags=re.IGNORECASE)
    stripped = re.sub(r"\s*```$", "", stripped).strip()

    # Step 2: Try direct parse first (most common case)
    try:
        return json.loads(stripped)
    except (ValueError, json.JSONDecodeError):
        pass

    # Step 3: Extract outermost {...} to handle leading/trailing prose
    # Find first { and last }
    first_brace = stripped.find("{")
    last_brace = stripped.rfind("}")

    if first_brace == -1 or last_brace == -1 or last_brace <= first_brace:
        log(f"extract_json_from_response: no valid {...} found in response")
        return None

    extracted = stripped[first_brace : last_brace + 1]
    try:
        return json.loads(extracted)
    except (ValueError, json.JSONDecodeError) as e:
        log(f"extract_json_from_response: JSON still invalid after brace extraction ({e})")
        return None
