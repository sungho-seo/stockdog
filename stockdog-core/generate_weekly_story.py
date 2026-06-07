#!/usr/bin/env python3
"""IMPR-073 D1 — gated weekly market story generator.

Generates a structured weekly narrative from daily-market reports collected
over the past week (Monday~Saturday KST = Tuesday~Saturday US ET). Written to:

    <notes>/raw/stockdog/narrative/narrative.json (LIVE)
    <notes>/raw/stockdog/narrative/archive/<sunday_date>.json (archive)

GATED — cost control. The LLM is called ONLY when:
  1. That week's US daily-market reports (≥3 required) exist
  2. narrative.json does NOT already have status=="ok" with content_type=="weekly" for sunday_date (idempotent)

ALWAYS exits 0. Every failure (gate skip, insufficient data, LLM error, validation failure)
→ status:"skipped" written to output file and exit 0, so publish chain never breaks.

Usage:
    python generate_weekly_story.py <notes_root> <sunday_date> [--force]

    <notes_root>    path to vault root (container: /notes)
    <sunday_date>   date as YYYY-MM-DD (should be a Sunday in KST)
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

from narrative_common import (
    log, _WallClockTimeout, _alarm_handler,
    check_report_exists, check_idempotent,
    extract_report_sections, extract_macro_excerpt, extract_fear_greed,
    get_llm, write_output, check_forbidden_words,
    set_wall_clock, cancel_wall_clock,
    _M7_TICKERS, SCHEMA_VERSION,
)

LOG = "[generate_weekly_story]"
LLM_WALL_CLOCK_SECONDS = 120


def _get_week_date_range(sunday_date: str) -> tuple[str, str, list[str]]:
    """Return (monday, saturday, list of dates) for the week containing sunday_date.

    Week is defined in KST: Monday~Saturday (corresponds to US ET Tue~Sat).
    Returns list of dates that should contain US reports.
    """
    try:
        dt_sunday = datetime.strptime(sunday_date, "%Y-%m-%d")
    except ValueError:
        log(f"invalid date format: {sunday_date}")
        return None, None, []

    # Find Monday of that week (Sunday - 6 days = previous Monday)
    dt_monday = dt_sunday - timedelta(days=6)
    dt_saturday = dt_sunday - timedelta(days=1)

    # Generate all dates Monday~Saturday
    dates = []
    current = dt_monday
    while current <= dt_saturday:
        dates.append(current.strftime("%Y-%m-%d"))
        current += timedelta(days=1)

    return (
        dt_monday.strftime("%Y-%m-%d"),
        dt_saturday.strftime("%Y-%m-%d"),
        dates,
    )


def _collect_week_reports(notes_root: Path, week_dates: list[str]) -> list[tuple[str, str]]:
    """Collect all US daily-market reports for the week.

    Returns list of (date, excerpt) tuples. Empty list if <3 reports found.
    """
    reports = []
    for date in week_dates:
        report_path = (
            notes_root / "raw" / "stockdog" / "daily-market"
            / date / f"Market_Report_US_{date}.md"
        )
        if report_path.is_file():
            excerpt, _ = extract_report_sections(report_path, date)
            reports.append((date, excerpt))

    log(f"collected {len(reports)} US reports for the week (need ≥3)")
    return reports if len(reports) >= 3 else []


def _extract_macro_weekly(notes_root: Path, week_dates: list[str]) -> str:
    """Extract macro deltas for the week.

    Reads macro_snapshot.json if available (stores daily snapshots).
    Returns summary of weekly macro trends.
    """
    snapshot_path = notes_root / "raw" / "stockdog" / "macro" / "macro_snapshot.json"
    if not snapshot_path.is_file():
        return "(이번 주 매크로 데이터가 없습니다)"

    try:
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "(이번 주 매크로 데이터가 없습니다)"

    # snapshot.daily[]: list of daily snapshots with timestamps
    daily_list = snapshot.get("daily", [])
    if len(daily_list) < 2:
        return "(충분한 주간 데이터가 없습니다)"

    # Extract first and last of the week for deltas
    first_day = daily_list[0]
    last_day = daily_list[-1]

    summary = f"주간 매크로 개요:\n"
    summary += f"- 주초: {first_day}\n"
    summary += f"- 주말: {last_day}\n"
    # Could extract specific fields like rate, inflation, etc. for detailed deltas
    summary += "(상세 분석은 매크로 트래커 참조)"
    return summary[:2000]


def _build_weekly_m7_context(notes_root: Path, week_dates: list[str]) -> str:
    """Build M7 context for the week (aggregated insider/short changes).

    Returns text with weekly M7 summary per ticker.
    """
    # For now, simpler version: read the final day's M7 state
    # In a full implementation, could track changes across the week
    last_date = week_dates[-1] if week_dates else None
    if not last_date:
        return "(M7 주간 데이터가 없습니다)"

    lines = [f"주간 M7 요약 (기준: {last_date}):"]
    for tk in _M7_TICKERS:
        lines.append(f"[{tk}] 주간 변동 추적 (상세는 M7 트래커 참조)")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Validation (weekly-specific)
# ---------------------------------------------------------------------------

def _validate_weekly_narrative(obj: dict) -> list[str]:
    """Return list of validation errors (empty = valid)."""
    errors = []

    # hero_oneliner
    if not isinstance(obj.get("hero_oneliner"), str) or not obj["hero_oneliner"].strip():
        errors.append("hero_oneliner is empty")

    # weekly_narrative
    wn = obj.get("weekly_narrative", {})
    if not isinstance(wn, dict):
        errors.append("weekly_narrative is not a dict")
    else:
        kw = wn.get("keywords", [])
        if not isinstance(kw, list) or not (2 <= len(kw) <= 4):
            errors.append(f"weekly_narrative.keywords len {len(kw) if isinstance(kw, list) else 'N/A'} not in 2..4")
        if not isinstance(wn.get("story"), str) or not wn["story"].strip():
            errors.append("weekly_narrative.story is empty")

    # themes (optional but if present, check structure)
    themes = obj.get("themes", [])
    if isinstance(themes, list):
        for i, theme in enumerate(themes):
            if not isinstance(theme, dict):
                errors.append(f"themes[{i}] is not a dict")
                continue
            if not isinstance(theme.get("title"), str) or not theme["title"].strip():
                errors.append(f"themes[{i}].title is empty")
            if not isinstance(theme.get("story"), str) or not theme["story"].strip():
                errors.append(f"themes[{i}].story is empty")

    # macro_flow (required)
    mf = obj.get("macro_flow", {})
    if not isinstance(mf, dict):
        errors.append("macro_flow is not a dict")
    else:
        if not isinstance(mf.get("story"), str) or not mf["story"].strip():
            errors.append("macro_flow.story is empty")
        if not isinstance(mf.get("kr_impact"), str) or not mf["kr_impact"].strip():
            errors.append("macro_flow.kr_impact is empty (required)")

    # m7_weekly (recommended 7 items)
    m7w = obj.get("m7_weekly", [])
    if not isinstance(m7w, list):
        errors.append("m7_weekly is not a list")
    else:
        if len(m7w) < 5:
            errors.append(f"m7_weekly has {len(m7w)} items (recommend ≥5, ideal 7)")
        for i, item in enumerate(m7w):
            if not isinstance(item, dict):
                errors.append(f"m7_weekly[{i}] is not a dict")
                continue
            if not isinstance(item.get("ticker"), str) or item["ticker"] not in _M7_TICKERS:
                errors.append(f"m7_weekly[{i}].ticker invalid")
            if not isinstance(item.get("story"), str) or not item["story"].strip():
                errors.append(f"m7_weekly[{i}].story is empty")

    return errors


# ---------------------------------------------------------------------------
# LLM prompt (weekly-specific)
# ---------------------------------------------------------------------------

WEEKLY_SYSTEM_PROMPT = """당신은 한국 일반 대중 독자를 위한 주간 시장 이야기꾼입니다. 어려운 금융 용어는 비유로 풀고, 친근하지만 신뢰감 있는 톤으로 씁니다.

**절대 금지**:
① 투자 권유·단정 표현 — "사라", "팔아라", "매수", "매도", "목표가", "추천", "사세요", "파세요" 등 일절 사용 금지
② 지난 한 주의 큰 흐름에만 집중 — 세부 종목 개별 움직임보다는 섹터·심리·거시 회고
③ 과잉 인과 — 단정 대신 "~로 보입니다", "~와 맞물려" 등 관찰 표현 사용
④ 소스에 없는 이벤트·실적·제품 날조 절대 금지

모든 내용은 정보·교육·참고용입니다.

**출력 형식**: 유효한 JSON 객체 하나만 출력 (코드 펜스·인사말·설명 텍스트 없이). 모든 텍스트 한국어.

**스키마**:
{
  "hero_oneliner": "지난 한 주를 한 문장으로 — 가장 중요한 흐름 (1문장, 30자 내외)",
  "weekly_narrative": {
    "keywords": ["핵심 키워드1", "핵심 키워드2"],
    "story": "주간 시장의 큰 흐름 3~4문장 — 지수·섹터·심리의 연결고리"
  },
  "themes": [
    {"title": "테마명", "story": "테마의 주간 전개 1~2문장"}
  ],
  "macro_flow": {
    "story": "금리·인플레·달러·FOMC 등 매크로 주간 흐름 2~3문장",
    "kr_impact": "이 매크로 흐름이 한국장(코스피/원화/수출주)에 어떻게 연결되는지 1~2문장 (필수, 절대 비워두지 말 것)"
  },
  "m7_weekly": [
    {"ticker": "AAPL", "story": "주간 정성 스토리 1~2문장"}
  ]
}

keywords는 2개 이상 4개 이하. themes는 0~3개 권장. m7_weekly는 모든 7개 또는 주목할 5개 이상 권장. story 필드는 절대 빈 문자열 금지. kr_impact는 반드시 포함."""

WEEKLY_HUMAN_TEMPLATE = """지난 한 주(주간 기준: 월요~토요)의 미국 시장 리포트를 종합합니다. 분석 기준: {week_start} ~ {week_end}.

[소스1 — 주간 미국 데일리 마켓 리포트 발췌들]
{daily_reports}

[소스2 — 주간 매크로 흐름 (금리·인플레·환율)]
{macro_weekly}

[소스3 — 주간 M7 (Apple, Microsoft, Google, Amazon, Meta, NVIDIA, Tesla) 회고]
{m7_weekly_context}

위 스키마대로 유효한 JSON 객체 하나만 출력하세요."""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Gated weekly market story generator.",
        add_help=False,
    )
    parser.add_argument("notes_root", nargs="?", default=None,
                        help="Path to vault root (container: /notes)")
    parser.add_argument("sunday_date", nargs="?", default=None,
                        help="Sunday date (YYYY-MM-DD) for the week to analyze")
    parser.add_argument("--force", action="store_true", default=False,
                        help="Bypass idempotency gate only")
    args, _ = parser.parse_known_args()

    if not args.notes_root or not args.sunday_date:
        log("usage: generate_weekly_story.py <notes_root> <sunday_date> [--force] — skip")
        return 0

    notes_root = Path(args.notes_root).expanduser()
    sunday_date = args.sunday_date
    force = args.force
    data_as_of = sunday_date  # weekly uses sunday as data_as_of

    # ── Get week date range ────────────────────────────────────────────────
    monday, saturday, week_dates = _get_week_date_range(sunday_date)
    if not week_dates:
        log(f"failed to parse week range for {sunday_date} — skip")
        write_output(notes_root, sunday_date, data_as_of, "skipped", None, content_type="weekly")
        return 0

    log(f"analyzing week {monday} ~ {saturday}")

    # ── GATE 1: Sufficient reports exist? ──────────────────────────────────
    reports = _collect_week_reports(notes_root, week_dates)
    if not reports:
        log(f"insufficient US reports (<3) for week — skip (no LLM call)")
        write_output(notes_root, sunday_date, data_as_of, "skipped", None, content_type="weekly")
        return 0

    # ── GATE 2: idempotent — already have status:ok for this sunday? ───────
    if not force and check_idempotent(notes_root, sunday_date, content_type="weekly"):
        return 0  # no write needed; existing file is already correct
    if force:
        log("--force: skipping idempotency gate (Gate 2)")

    # ── Source excerpts (all fallback to placeholder on failure) ───────────
    daily_reports_text = "\n\n---\n\n".join([f"[{date}]\n{excerpt}" for date, excerpt in reports])
    macro_weekly = _extract_macro_weekly(notes_root, week_dates)
    m7_weekly_context = _build_weekly_m7_context(notes_root, week_dates)

    # ── LLM import (ONLY here — after both gates pass) ─────────────────────
    llm = get_llm()
    if llm is None:
        log("no LLM configured (no API key) — skip")
        write_output(notes_root, sunday_date, data_as_of, "skipped", None, content_type="weekly")
        return 0

    try:
        from langchain_core.prompts import ChatPromptTemplate
        prompt = ChatPromptTemplate.from_messages([
            ("system", WEEKLY_SYSTEM_PROMPT),
            ("human", WEEKLY_HUMAN_TEMPLATE),
        ])
        chain = prompt | llm.bind(max_tokens=2500, temperature=0.4)
    except Exception as e:
        log(f"prompt/chain build failed ({e}) — skip")
        write_output(notes_root, sunday_date, data_as_of, "skipped", None, content_type="weekly")
        return 0

    # ── LLM call with wall-clock guard ─────────────────────────────────────
    for attempt in range(1, 3):  # max 2 attempts
        set_wall_clock(LLM_WALL_CLOCK_SECONDS)
        raw_text = ""
        try:
            resp = chain.invoke({
                "week_start": monday,
                "week_end": saturday,
                "daily_reports": daily_reports_text,
                "macro_weekly": macro_weekly,
                "m7_weekly_context": m7_weekly_context,
            })
            raw_text = (resp.content or "").strip()
        except _WallClockTimeout:
            log(f"LLM call exceeded {LLM_WALL_CLOCK_SECONDS}s (attempt {attempt}) — skip")
            write_output(notes_root, sunday_date, data_as_of, "skipped", None, content_type="weekly")
            return 0
        except Exception as e:
            log(f"LLM call failed ({e}) (attempt {attempt}) — skip")
            write_output(notes_root, sunday_date, data_as_of, "skipped", None, content_type="weekly")
            return 0
        finally:
            cancel_wall_clock()

        if not raw_text:
            log(f"LLM returned empty text (attempt {attempt}) — skip")
            write_output(notes_root, sunday_date, data_as_of, "skipped", None, content_type="weekly")
            return 0

        # ── Parse JSON ─────────────────────────────────────────────────────
        stripped = re.sub(r"^```(?:json)?\s*", "", raw_text, flags=re.IGNORECASE)
        stripped = re.sub(r"\s*```$", "", stripped).strip()
        try:
            weekly_obj = json.loads(stripped)
        except (ValueError, json.JSONDecodeError) as e:
            log(f"JSON parse failed ({e}) (attempt {attempt})")
            if attempt < 2:
                log("retrying LLM call...")
                continue
            log("giving up after 2 attempts — skip")
            write_output(notes_root, sunday_date, data_as_of, "skipped", None, content_type="weekly")
            return 0

        # ── Schema validation ───────────────────────────────────────────────
        val_errors = _validate_weekly_narrative(weekly_obj)
        if val_errors:
            log(f"schema validation failed (attempt {attempt}): {val_errors}")
            if attempt < 2:
                log("retrying LLM call...")
                continue
            log("giving up after 2 attempts — skip")
            write_output(notes_root, sunday_date, data_as_of, "skipped", None, content_type="weekly")
            return 0

        # ── Forbidden word scan ─────────────────────────────────────────────
        fw_violations = check_forbidden_words(weekly_obj)
        if fw_violations:
            log(f"forbidden word violation (attempt {attempt}): {fw_violations}")
            if attempt < 2:
                log("retrying LLM call...")
                continue
            log("giving up after 2 attempts — skip")
            write_output(notes_root, sunday_date, data_as_of, "skipped", None, content_type="weekly")
            return 0

        # ── All checks passed ───────────────────────────────────────────────
        log(f"weekly narrative validated OK (attempt {attempt})")

        # ── Write output ────────────────────────────────────────────────────
        write_output(
            notes_root, sunday_date, data_as_of,
            "ok", None,
            content_type="weekly",
            weekly_fields={
                "hero_oneliner": weekly_obj.get("hero_oneliner"),
                "weekly_narrative": weekly_obj.get("weekly_narrative"),
                "themes": weekly_obj.get("themes", []),
                "macro_flow": weekly_obj.get("macro_flow"),
                "m7_weekly": weekly_obj.get("m7_weekly", []),
            }
        )
        return 0

    # Should not reach here, but guard
    write_output(notes_root, sunday_date, data_as_of, "skipped", None, content_type="weekly")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # absolute last-resort guard — ALWAYS exit 0
        print(f"{LOG} unexpected error ({e}) — exit 0", flush=True)
        sys.exit(0)
