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


def check_idempotent(notes_root: Path, run_date: str, content_type: str = "daily") -> bool:
    """Return True if we should SKIP (already have a good narrative for today).

    Args:
        notes_root: Path to vault root
        run_date: Date as YYYY-MM-DD
        content_type: "daily" | "weekly" — only skip if matching content_type

    Returns:
        True if narrative.json already has status=="ok" for run_date with matching content_type
    """
    out_path = notes_root / "raw" / "stockdog" / "narrative" / "narrative.json"
    if not out_path.is_file():
        return False
    try:
        existing = json.loads(out_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if (
        existing.get("report_date") == run_date
        and existing.get("status") == "ok"
        and existing.get("schema_version") == SCHEMA_VERSION
        and existing.get("content_type") == content_type
    ):
        log(f"narrative.json already ok for {run_date} (content_type={content_type}) — skip (idempotent, no LLM call)")
        return True
    return False


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

    Returns ChatAnthropic with claude-sonnet-4-6 model.
    """
    try:
        from langchain_anthropic import ChatAnthropic
        if not os.getenv("ANTHROPIC_API_KEY"):
            log("no ANTHROPIC_API_KEY configured")
            return None
        return ChatAnthropic(model="claude-sonnet-4-6", temperature=0.2, max_tokens=8192)
    except Exception as e:
        log(f"get_llm failed ({e})")
        return None


# ---------------------------------------------------------------------------
# Output writer
# ---------------------------------------------------------------------------

def write_output(notes_root: Path, run_date: str, data_as_of: str,
                 status: str, narrative,
                 content_type: str = "daily",
                 m7_status: str = "skipped", m7_stories=None,
                 weekly_fields: dict = None) -> None:
    """Write narrative.json (always, even on skip/failure).

    Args:
        notes_root: Path to vault root
        run_date: Date as YYYY-MM-DD
        data_as_of: Data cutoff date (frontmatter from report)
        status: "ok" | "skipped"
        narrative: Narrative object (dict) or None
        content_type: "daily" | "weekly" (default "daily")
        m7_status: "ok" | "skipped" (daily only)
        m7_stories: List of {ticker, story} dicts (daily only, or None)
        weekly_fields: Extra fields for weekly (e.g., hero_oneliner, themes, macro_flow, m7_weekly)

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
