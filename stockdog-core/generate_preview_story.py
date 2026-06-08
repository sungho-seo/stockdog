#!/usr/bin/env python3
"""IMPR-076 D1 — gated preview market story generator (Monday "미장 프리뷰").

Generates a structured preview narrative (content_type="preview") for the week ahead,
based on economic calendar + macro state + current positioning. Written to:

    <notes>/raw/stockdog/narrative/narrative.json (LIVE)
    <notes>/raw/stockdog/narrative/archive/<monday_date>.json (archive)

GATED — cost control. The LLM is called ONLY when:
  1. Required snapshots (macro_snapshot, watchlist_snapshot) exist
  2. EITHER calendar has ≥1 upcoming event OR positioning has ≥1 active flag
  3. preview.json does NOT already have status=="ok" with content_type=="preview" for monday_date (idempotent)

ALWAYS exits 0. Every failure (gate skip, insufficient data, LLM error, validation failure)
→ status:"skipped" written to output file and exit 0, so publish chain never breaks.

Usage:
    python generate_preview_story.py <notes_root> <monday_date> [--force]

    <notes_root>    path to vault root (container: /notes)
    <monday_date>   date as YYYY-MM-DD (should be a Monday in KST)
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
    check_idempotent,
    extract_macro_excerpt,
    get_llm, write_output, check_forbidden_words,
    set_wall_clock, cancel_wall_clock,
    extract_json_from_response,
    SCHEMA_VERSION,
)
from render_signals_tracker import compute_scored_flags, SCORE_WATCH
from collectors.economic_calendar import get_economic_calendar

LOG = "[generate_preview_story]"
LLM_WALL_CLOCK_SECONDS = 120

# Preview-specific constants
PREVIEW_TOP_N = 7
PREVIEW_SYSTEM_PROMPT = """당신은 한국 일반 대중 독자를 위한 주간 시장 프리뷰 이야기꾼입니다. 이번 주를 미리 살펴보는 관점에서 씁니다. 어려운 금융 용어는 비유로 풀고, 친근하지만 신뢰감 있는 톤으로 씁니다.

**절대 금지**:
① 투자 권유·단정 표현 — "사라", "팔아라", "매수", "매도", "목표가", "추천", "사세요", "파세요" 등 일절 사용 금지
② 미래 예측·예상 — 이번 주가 "오를 것", "상승할", "급등", "조정" 등 종목별 움직임 예상 금지. 데이터는 금요일 마감 기준.
③ 없는 정보 창조 — 실적 날짜, 컨센서스, 목표가, EPS 등 소스에 없는 이벤트/숫자 절대 금지
④ 현재 포지션은 "무엇이 있나" 관찰, 미래 "어떻게 될지" 아님 — 구분 엄격

**포지션 섹션 톤 (CRITICAL)**:
현재 상태를 기술하되, 미래 예측으로 흐르지 말 것. 예:
- ✓ "[TSLA] 금주 말 공매도 비중이 평균 대비 2%p 상향 — 약보 신호"
- ✗ "[TSLA] 공매도가 올라서 이번 주 하락할 것 같음"
- ✓ "FOMC 회의가 목요일 예정되어 있음"
- ✗ "FOMC가 긴축할 예상이므로 기술주 약세"

모든 내용은 정보·교육·참고용입니다.

**출력 형식**: 유효한 JSON 객체 하나만 출력 (코드 펜스·인사말·설명 텍스트 없이). 모든 텍스트 한국어.

**스키마**:
{{
  "hero_oneliner": "이번 주를 한 문장으로 — 가장 주목할 이벤트 또는 포지션 (1문장, 30자 내외)",
  "calendar": [
    {{"name": "FOMC Meeting", "date": "2026-06-17", "days_until": 3, "prev_actual": "N/A", "consensus": "예상치 없음"}}
  ],
  "macro_position": {{
    "story": "10Y 수익률이 4.2%로 지난주말 대비 +5bps 상향. 달러도 강세. 기술주 약보 환경 지속 예상",
    "kr_impact": "달러 강세 + 금리 상승은 원화 약세, 외국인 이탈 리스크 증대. 수출주 제약 환경"
  }},
  "positioning": [
    {{"ticker": "TSLA", "line": "공매도 비중 12.5% — 평균 대비 +2.1%p (약보 신호)", "tier": "C"}},
    {{"ticker": "NVDA", "line": "고위직 순매수 +$3.2M — 오너 신뢰 신호", "tier": "B"}}
  ],
  "positioning_overflow": 0,
  "themes": [
    {{"title": "테마명", "story": "이번 주 주목할 테마 설명 1~2문장"}}
  ],
  "data_as_of": "2026-06-13"
}}

calendar는 배열; consensus는 항상 "예상치 없음" (숫자 절대 금지). positioning_overflow는 N > PREVIEW_TOP_N일 때 카운트.
positioning은 이미 카운트된 데이터(LLM이 스코어/랭크하지 않음) — 그대로 서술하기.
"""

PREVIEW_HUMAN_TEMPLATE = """이번 주(월요일 시작) 미국 시장 전망을 담은 프리뷰입니다. 분석 기준: {week_start}.

[소스1 — 이번 주 경제 캘린더 (향후 7일)]
{calendar}

[소스2 — 현재 거시경제 환경]
{macro_position}

[소스3 — 현재 포지션 (개별 종목 + 매크로)]
{positioning_list}
{positioning_overflow_marker}

[소스4 — 지켜볼 테마 (지난주 주간 분석에서 carry forward)]
{themes_context}

위 스키마대로 유효한 JSON 객체 하나만 출력하세요."""


def _get_monday_date(monday_str: str) -> str:
    """Validate that the date is a Monday; return it."""
    try:
        dt = datetime.strptime(monday_str, "%Y-%m-%d")
        if dt.weekday() != 0:  # 0 = Monday
            log(f"warning: {monday_str} is not a Monday (weekday={dt.weekday()})")
        return monday_str
    except ValueError:
        log(f"invalid date format: {monday_str}")
        return None


def _get_prior_friday(monday_str: str) -> str:
    """Return the prior Friday (previous day of the Monday week)."""
    try:
        dt = datetime.strptime(monday_str, "%Y-%m-%d")
        prior_friday = dt - timedelta(days=3)  # Monday - 3 = Friday
        return prior_friday.strftime("%Y-%m-%d")
    except ValueError:
        return None


def _format_calendar_section(calendar_data: dict) -> str:
    """Format the calendar response for the LLM prompt."""
    lines = []
    upcoming = calendar_data.get("upcoming", [])

    if not upcoming:
        lines.append("(이번 주 주요 경제 이벤트 없음)")
    else:
        for event in upcoming:
            name = event.get("name", "")
            date = event.get("date", "")
            days = event.get("days_until", 0)
            prev = event.get("prev_actual", "N/A")
            lines.append(
                f"- [{name}] {date} (D-{days}) 이전발표: {prev}"
            )

    # Add static note about jobless claims
    lines.append("- [매주 목요일(미국)] 실직 급여청구(jobless claims) 정기 발표")

    return "\n".join(lines)


def _format_positioning_section(live_flags: list, cs_cards: list) -> tuple[str, int]:
    """Gate, rank, and cap the positioning list.

    Returns (formatted_text, overflow_count).
    """
    # Keep only flags with score >= SCORE_WATCH and domain in {short,insider,watchlist}
    filtered = [f for f in live_flags
                if f.get("score", 0) >= SCORE_WATCH
                and f.get("domain", "") in {"short", "insider", "watchlist"}]

    # Also keep CS cards (which may name tickers)
    # CS cards have cs field: "CS-1", "CS-2", "CS-3"
    cs_selected = [c for c in cs_cards if c.get("cs") in ("CS-1", "CS-2", "CS-3")]

    # Combine and rank by score DESC, then tier
    combined = filtered + cs_selected
    combined.sort(key=lambda x: (-x.get("score", 0), x.get("tier", "Z")))

    # Cap at PREVIEW_TOP_N
    capped = combined[:PREVIEW_TOP_N]
    overflow = max(0, len(combined) - PREVIEW_TOP_N)

    lines = []
    if not capped:
        lines.append("(특이 포지션 없음)")
    else:
        for item in capped:
            ticker = item.get("ticker", "—")
            text = item.get("text", "")
            tier = item.get("tier", "")
            cs = item.get("cs")

            if cs:
                # Cross-signal card
                lines.append(f"[{ticker} cross] {text} ({cs})")
            else:
                # Regular flag
                lines.append(f"[{ticker}] {text} ({tier})")

    return "\n".join(lines), overflow


def _format_themes_from_archive(notes_root: Path) -> str:
    """Read the most recent weekly archive and carry forward themes."""
    archive_dir = notes_root / "raw" / "stockdog" / "narrative" / "archive"
    if not archive_dir.is_dir():
        return "(지난주 테마 정보 없음)"

    # Find most recent file with content_type=="weekly"
    candidates = sorted(archive_dir.glob("*.json"), reverse=True)
    for candidate in candidates:
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
            if data.get("content_type") == "weekly" and data.get("status") == "ok":
                themes = data.get("themes", [])
                if themes:
                    lines = []
                    for theme in themes:
                        title = theme.get("title", "")
                        story = theme.get("story", "")
                        if title:
                            lines.append(f"- [{title}] {story}")
                    if lines:
                        return "\n".join(lines)
        except (OSError, ValueError):
            continue

    return "(지난주 테마 정보 없음)"


# ---------------------------------------------------------------------------
# Validation (preview-specific)
# ---------------------------------------------------------------------------

def _validate_preview_narrative(obj: dict) -> list[str]:
    """Return list of validation errors (empty = valid)."""
    errors = []

    # hero_oneliner
    if not isinstance(obj.get("hero_oneliner"), str) or not obj["hero_oneliner"].strip():
        errors.append("hero_oneliner is empty")

    # calendar
    cal = obj.get("calendar", [])
    if not isinstance(cal, list):
        errors.append("calendar is not a list")
    else:
        for i, event in enumerate(cal):
            if not isinstance(event, dict):
                errors.append(f"calendar[{i}] is not a dict")
                continue
            if "name" not in event or "date" not in event:
                errors.append(f"calendar[{i}] missing name or date")

    # macro_position
    mp = obj.get("macro_position", {})
    if not isinstance(mp, dict):
        errors.append("macro_position is not a dict")
    else:
        if not isinstance(mp.get("story"), str) or not mp["story"].strip():
            errors.append("macro_position.story is empty")
        if not isinstance(mp.get("kr_impact"), str) or not mp["kr_impact"].strip():
            errors.append("macro_position.kr_impact is empty")

    # positioning
    pos = obj.get("positioning", [])
    if not isinstance(pos, list):
        errors.append("positioning is not a list")
    else:
        for i, item in enumerate(pos):
            if not isinstance(item, dict):
                errors.append(f"positioning[{i}] is not a dict")
                continue
            if not item.get("ticker") or not item.get("line"):
                errors.append(f"positioning[{i}] missing ticker or line")

    # positioning_overflow
    if not isinstance(obj.get("positioning_overflow"), int):
        errors.append("positioning_overflow is not an int")

    # themes (optional)
    themes = obj.get("themes", [])
    if isinstance(themes, list):
        for i, theme in enumerate(themes):
            if not isinstance(theme, dict):
                errors.append(f"themes[{i}] is not a dict")
                continue
            if not theme.get("title") or not theme.get("story"):
                errors.append(f"themes[{i}] missing title or story")

    # data_as_of
    if not obj.get("data_as_of"):
        errors.append("data_as_of is missing")

    return errors


# ---------------------------------------------------------------------------
# Forbidden words (preview-specific + shared)
# ---------------------------------------------------------------------------

_PREVIEW_FORBIDDEN_RE = re.compile(
    # Recommendation/action forms (bound to active voice, never bare)
    r"매수 추천|매도 추천"                 # 명시적 투자 추천
    r"|매수하[세시]|매도하[세시]"          # 매수하세요 / 매도하세요 / 매수하시 / 매도하시
    r"|매수할|매도할"                     # 매수할 때 / 매도할 때
    r"|사세요|파세요|담으세요|정리하세요"  # ~세요 조언형 동사
    r"|사야|팔아야|사라|팔아라"           # 명령·당위형
    r"|지금 사|지금 팔"                   # "지금 사/팔"
    # Position adjustment verbs
    r"|비중 확대|비중확대|비중 축소|비중축소"  # 비중 조언
    r"|풀매수|손절|익절"                  # 포지션 조언
    # Future outcome/prediction forms
    r"|오를 것|오를 가능성"               # future rise
    r"|상승할 것|상승 예상|상승 전망"     # forecast rise
    r"|급등할|급등이 예상|급등(할|이 예상)"  # sharp rise
    r"|하락할 것|하락 예상|하락 전망"     # forecast fall
    # Target/forecast specifics
    r"|목표가|목표 주가|전망치|예상 수익"  # price targets
)


def _check_preview_forbidden(obj: dict) -> list[str]:
    """Check for preview-specific forbidden patterns (future-tense outcome phrasing)."""
    violations = []

    def _scan(v, path):
        if isinstance(v, str):
            m = _PREVIEW_FORBIDDEN_RE.search(v)
            if m:
                violations.append(f"{path}: found future-tense '{m.group()}'")
        elif isinstance(v, dict):
            for k, sub in v.items():
                _scan(sub, f"{path}.{k}")
        elif isinstance(v, list):
            for i, sub in enumerate(v):
                _scan(sub, f"{path}[{i}]")

    _scan(obj, "preview")
    return violations


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Gated preview market story generator.",
        add_help=False,
    )
    parser.add_argument("notes_root", nargs="?", default=None,
                        help="Path to vault root (container: /notes)")
    parser.add_argument("monday_date", nargs="?", default=None,
                        help="Monday date (YYYY-MM-DD) for the week to preview")
    parser.add_argument("--force", action="store_true", default=False,
                        help="Bypass idempotency gate only")
    args, _ = parser.parse_known_args()

    if not args.notes_root or not args.monday_date:
        log("usage: generate_preview_story.py <notes_root> <monday_date> [--force] — skip")
        return 0

    notes_root = Path(args.notes_root).expanduser()
    monday_date = _get_monday_date(args.monday_date)
    if not monday_date:
        write_output(notes_root, args.monday_date, args.monday_date, "skipped", None, content_type="preview")
        return 0

    force = args.force
    data_as_of = _get_prior_friday(monday_date)  # preview uses prior Friday as data_as_of

    log(f"analyzing preview for week starting {monday_date} (data as of {data_as_of})")

    # ── GATE 1: Snapshots exist? ───────────────────────────────────────────
    try:
        scored = compute_scored_flags(notes_root, asof=datetime.strptime(data_as_of, "%Y-%m-%d").date())
    except (ValueError, Exception) as e:
        log(f"snapshot check failed ({e}) — skip (no LLM call)")
        write_output(notes_root, monday_date, data_as_of, "skipped", None, content_type="preview")
        return 0

    live_flags = scored.get("live_flags", [])
    cs_cards = scored.get("cs_cards", [])

    # ── GATE 2: Worth-it? (calendar OR positioning) ────────────────────────
    # NOTE: get_economic_calendar() makes a FREE external HTTP call to FRED (no paid LLM).
    calendar_data = get_economic_calendar(sample=False)
    calendar_upcoming = calendar_data.get("upcoming", [])

    positioning_text, overflow = _format_positioning_section(live_flags, cs_cards)
    has_positioning = any(f.get("score", 0) >= SCORE_WATCH and f.get("domain") in {"short", "insider", "watchlist"}
                          for f in live_flags) or any(cs.get("cs") in ("CS-1", "CS-2", "CS-3") for cs in cs_cards)

    if not calendar_upcoming and not has_positioning:
        log(f"thin week: no calendar events AND no active positioning — skip (no LLM call, $0 gate)")
        write_output(notes_root, monday_date, data_as_of, "skipped", None, content_type="preview")
        return 0

    # ── GATE 3: idempotent ────────────────────────────────────────────────
    if not force and check_idempotent(notes_root, monday_date, content_type="preview"):
        return 0  # no write needed; existing file is already correct
    if force:
        log("--force: skipping idempotency gate (Gate 3)")

    # ── Source excerpts ─────────────────────────────────────────────────────
    calendar_text = _format_calendar_section(calendar_data)
    macro_excerpt = extract_macro_excerpt(notes_root)
    positioning_marker = f"\n(+{overflow}개 종목 추가 관찰 → /trackers/signals)" if overflow > 0 else ""
    themes_text = _format_themes_from_archive(notes_root)

    # ── LLM import (ONLY here — after all gates pass) ────────────────────────
    llm = get_llm()
    if llm is None:
        log("no LLM configured (no API key) — skip")
        write_output(notes_root, monday_date, data_as_of, "skipped", None, content_type="preview")
        return 0

    try:
        from langchain_core.prompts import ChatPromptTemplate
        prompt = ChatPromptTemplate.from_messages([
            ("system", PREVIEW_SYSTEM_PROMPT),
            ("human", PREVIEW_HUMAN_TEMPLATE),
        ])
        chain = prompt | llm.bind(max_tokens=4000, temperature=0.4)
    except Exception as e:
        log(f"prompt/chain build failed ({e}) — skip")
        write_output(notes_root, monday_date, data_as_of, "skipped", None, content_type="preview")
        return 0

    # ── LLM call with wall-clock guard ──────────────────────────────────────
    for attempt in range(1, 3):  # max 2 attempts
        set_wall_clock(LLM_WALL_CLOCK_SECONDS)
        raw_text = ""
        try:
            resp = chain.invoke({
                "week_start": monday_date,
                "calendar": calendar_text,
                "macro_position": macro_excerpt,
                "positioning_list": positioning_text,
                "positioning_overflow_marker": positioning_marker,
                "themes_context": themes_text,
            })
            raw_text = (resp.content or "").strip()
        except _WallClockTimeout:
            log(f"LLM call exceeded {LLM_WALL_CLOCK_SECONDS}s (attempt {attempt}) — skip")
            write_output(notes_root, monday_date, data_as_of, "skipped", None, content_type="preview")
            return 0
        except Exception as e:
            log(f"LLM call failed ({e}) (attempt {attempt}) — skip")
            write_output(notes_root, monday_date, data_as_of, "skipped", None, content_type="preview")
            return 0
        finally:
            cancel_wall_clock()

        if not raw_text:
            log(f"LLM returned empty text (attempt {attempt}) — skip")
            write_output(notes_root, monday_date, data_as_of, "skipped", None, content_type="preview")
            return 0

        # ── Parse JSON (robust extraction + debug capture) ──────────────────
        preview_obj = extract_json_from_response(raw_text)
        if preview_obj is None:
            log(f"JSON extraction failed (attempt {attempt})")
            # Debug capture: write raw LLM response to temp file for diagnosis
            try:
                debug_dir = notes_root / "raw" / "stockdog" / "narrative"
                debug_dir.mkdir(parents=True, exist_ok=True)
                debug_path = debug_dir / f"_preview_debug_{monday_date}.txt"
                debug_path.write_text(
                    f"# Preview parse failure debug — {monday_date} attempt {attempt}\n\n"
                    f"Raw LLM response (first 10000 chars):\n{raw_text[:10000]}",
                    encoding="utf-8"
                )
                log(f"wrote debug file {debug_path}")
            except OSError as e:
                log(f"failed to write debug file ({e})")
            if attempt < 2:
                log("retrying LLM call...")
                continue
            log("giving up after 2 attempts — skip")
            write_output(notes_root, monday_date, data_as_of, "skipped", None, content_type="preview")
            return 0

        # ── Schema validation ───────────────────────────────────────────────
        val_errors = _validate_preview_narrative(preview_obj)
        if val_errors:
            log(f"schema validation failed (attempt {attempt}): {val_errors}")
            if attempt < 2:
                log("retrying LLM call...")
                continue
            log("giving up after 2 attempts — skip")
            write_output(notes_root, monday_date, data_as_of, "skipped", None, content_type="preview")
            return 0

        # ── Shared forbidden word scan ──────────────────────────────────────
        fw_violations = check_forbidden_words(preview_obj)
        if fw_violations:
            log(f"shared forbidden word violation (attempt {attempt}): {fw_violations}")
            if attempt < 2:
                log("retrying LLM call...")
                continue
            log("giving up after 2 attempts — skip")
            write_output(notes_root, monday_date, data_as_of, "skipped", None, content_type="preview")
            return 0

        # ── Preview-specific forbidden word scan ────────────────────────────
        preview_fw = _check_preview_forbidden(preview_obj)
        if preview_fw:
            log(f"preview forbidden word violation (attempt {attempt}): {preview_fw}")
            if attempt < 2:
                log("retrying LLM call...")
                continue
            log("giving up after 2 attempts — skip")
            write_output(notes_root, monday_date, data_as_of, "skipped", None, content_type="preview")
            return 0

        # ── All checks passed ───────────────────────────────────────────────
        log(f"preview narrative validated OK (attempt {attempt})")

        # ── Write output ────────────────────────────────────────────────────
        write_output(
            notes_root, monday_date, data_as_of,
            "ok", None,
            content_type="preview",
            preview_fields={
                "hero_oneliner": preview_obj.get("hero_oneliner"),
                "calendar": preview_obj.get("calendar", []),
                "macro_position": preview_obj.get("macro_position"),
                "positioning": preview_obj.get("positioning", []),
                "positioning_overflow": preview_obj.get("positioning_overflow", 0),
                "themes": preview_obj.get("themes", []),
            }
        )
        return 0

    # Should not reach here, but guard
    write_output(notes_root, monday_date, data_as_of, "skipped", None, content_type="preview")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # absolute last-resort guard — ALWAYS exit 0
        print(f"{LOG} unexpected error ({e}) — exit 0", flush=True)
        sys.exit(0)
