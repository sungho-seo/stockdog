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
    extract_sector_snapshot,
    get_llm, write_output, check_forbidden_words,
    set_wall_clock, cancel_wall_clock,
    extract_json_from_response,
    compute_preview_positioning,
    alert_generation_failure,
    SCHEMA_VERSION,
)
from render_signals_tracker import compute_scored_flags, SCORE_WATCH, load_watchlist_snapshot
from collectors.economic_calendar import get_economic_calendar

LOG = "[generate_preview_story]"
LLM_WALL_CLOCK_SECONDS = 120

# Preview-specific constants
PREVIEW_TOP_N = 7
# M7 관전 포인트 — canonical order (fixed, not scored/ranked by LLM)
PREVIEW_M7_TICKERS = ["AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "TSLA"]
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

**섹터/테마 진입 모멘텀**:
[소스4] 섹터/테마 로테이션 데이터를 기반으로 진입 모멘텀(지난 1개월+1주 가중 흐름)만 서술 — 이번 주 방향성 예측 금지.
테마 주장은 반드시 데이터 근거, ETF 티커 노출 금지.

모든 내용은 정보·교육·참고용입니다. 지난 흐름의 기록이며 이번 주를 예단하지 않습니다.

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
  "m7_preview": [
    {{"ticker": "AAPL", "watch": "이번 주 무엇을 관찰할지 1~2문장 (제공된 팩트만 사용)"}}
  ],
  "data_as_of": "2026-06-13"
}}

calendar는 배열; consensus는 항상 "예상치 없음" (숫자 절대 금지). positioning_overflow는 N > PREVIEW_TOP_N일 때 카운트.
positioning은 이미 카운트된 데이터(LLM이 스코어/랭크하지 않음) — 그대로 서술하기.

**m7_preview 규칙 (M7 이번 주 관전 포인트)**:
[소스6 — M7 관전 포인트 팩트]를 그대로 활용해, M7 7개 종목 각각에 대해 "이번 주 무엇을 지켜볼지"를 관찰 관점으로 씁니다.
- 배열에 정확히 7개, 캐논 순서 그대로: AAPL, MSFT, GOOGL, AMZN, META, NVDA, TSLA.
- 각 watch: 1~2문장, **한국어 100자 이내(하드 상한 110자)** — 반드시 지킬 것. **관찰 서술만** — 이번 주 무엇을 지켜볼지.
- **길이·간결(중요)**: 팩트 줄에 신호가 여러 개인 종목(예: 내부자 클러스터 + 공매도)은 전부 나열하지 말 것. 가장 강한 신호 하나를 앞세우고(우선순위: 내부자 클러스터 > 극단·상승 공매도 > 큰 폭 이동) 나머지는 짧게 덧붙이거나 생략. 망라보다 압축.
- **금지**: 예측·결과(오를/내릴 것, 반등, 급등/급락, 저점/고점), 매매·스탠스(매수/매도/목표가/추천/비중), 그 종목 팩트 줄에 없는 숫자, 없는 이벤트 창조(실적/어닝/컨센서스/FOMC/출시). 소스6 팩트만 사용.
- **지난 세션 이동 강도(중요)**: 이동 강도는 반드시 팩트 줄에 파이썬이 제공한 라벨(보합 / 상승 / 큰 폭 상승 / 하락 / 큰 폭 하락)을 그대로 사용. LLM이 자체 강도 표현을 만들지 말 것. 특히 **급등·급락(및 어떤 활용형 — 급락세·급락했·급등한 등)은 과거 세션을 묘사할 때조차 사용 금지** (위 금지 목록과 동일). 예: -7.49% → "큰 폭 하락"(O), "급락"(X).
- **수급 숫자 정성 서술(매우 중요)**: watch에 넣을 수 있는 **유일한 숫자는 지난 세션 이동 %** 하나뿐(예: "(+4.84%)", "(-7.49%)") — 카드의 가격 등락과 일치하기 때문. 공매도 비중 %·평균 대비 ±%p·내부자 건수(×N, N건)·누적 일수(N일) 등 **수급 세부 숫자는 절대 쓰지 말 것**. (수급 상세 패널이 정확 숫자의 단일 출처이고, watch 팩트는 다른 스냅샷이라 같은 카드에서 값이 어긋난다.) 수급은 정성 표현으로: "공매도 비중이 평균을 웃도는(밑도는) 수준", "3거래일 연속 늘어나는(줄어드는) 흐름", "내부자 매도 클러스터가 포착", "고위직 내부자 매도 신호" 등.
- **최고/가장 높은 등 최상급 표현 금지** (제공되지 않은 순위 정보).
- 팩트 줄이 "특이 수급 신호 없음"인 조용한 종목: 정직하게 "특이 수급 신호 없음 — 지수 흐름과 동행하는지 관찰"처럼 씁니다.
- **문장 마무리·구조 다양화(중요)**: 7줄이 같은 리듬으로 끝나지 않게 할 것. 마무리 관찰어(관찰 / 주목 / 지켜볼 구간 / 짚어볼 대목 / 확인이 필요 등)와 문장 형태를 종목마다 바꿔, 이웃한 티커가 똑같이 읽히지 않게. "…3일 연속 드리프트 중 … 이어지는지 관찰합니다" 같은 동일 패턴 반복 금지. 자연스럽게, 억지스럽지 않게.
- 톤 예시 (실제 데이터 — 이동 %만 숫자, 수급은 정성):
  · TSLA "지난 세션 큰 폭 하락(-7.49%) 뒤 공매도 비중이 평균을 크게 웃도는 수준 — 이 부담이 이번 주에도 이어지는지 주목합니다."
  · AMZN "지난 세션 보합(+0.40%), 특이 수급 신호 없음 — 지수 흐름과 동행하는지 지켜볼 대목."
- watch에서는 지난 세션 이동 % 외의 팩트 수급 숫자를 인용하지 말 것(정성 서술). 팩트에 없는 숫자·사실은 당연히 절대 금지.
"""

PREVIEW_HUMAN_TEMPLATE = """이번 주(월요일 시작) 미국 시장 전망을 담은 프리뷰입니다. 분석 기준: {week_start}.

[소스1 — 이번 주 경제 캘린더 (향후 7일)]
{calendar}

[소스2 — 현재 거시경제 환경]
{macro_position}

[소스3 — 현재 포지션 (개별 종목 + 매크로)]
{positioning_list}
{positioning_overflow_marker}

[소스4 — 섹터/테마 진입 모멘텀]
{sector_context}

[소스5 — 지켜볼 테마 (지난주 주간 분석에서 carry forward)]
{themes_context}

[소스6 — M7 관전 포인트 팩트]
{m7_preview_facts}

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


def _preview_magnitude_label(change_pct) -> str:
    """Python-assigned qualitative move label — the LLM never invents magnitude words.

    Label set (fixed): 보합 / 상승 / 큰 폭 상승 / 하락 / 큰 폭 하락.
    """
    if change_pct is None:
        return "변화 미확인"
    try:
        pct = float(change_pct)
    except (TypeError, ValueError):
        return "변화 미확인"
    if pct >= 3.0:
        return "큰 폭 상승"
    elif pct >= 1.0:
        return "상승"
    elif pct >= -1.0:
        return "보합"
    elif pct >= -3.0:
        return "하락"
    else:
        return "큰 폭 하락"


def _format_m7_preview_facts(live_flags: list, wl_snap: dict) -> str:
    """Build the [소스6] M7 관전 포인트 fact block — one line per M7 ticker.

    Reuses ALREADY-COMPUTED data only (LLM must not add facts):
      - last-session move: wl_snap["tickers"][tkr]["latest"]["change_pct"]
        (nested `latest` key — same path the daily M7 context reads), with a
        Python-assigned magnitude label so the LLM never invents magnitude words.
      - short/insider signals: the pre-formatted flag TEXT for that ticker from
        live_flags (domain in {short, insider}) — fed verbatim so real short %/±%p
        and insider cluster counts are the only numbers cited.

    Quiet ticker (no short/insider flag) → "특이 수급 신호 없음".
    """
    tickers = (wl_snap or {}).get("tickers") or {}

    lines = []
    for tkr in PREVIEW_M7_TICKERS:
        # ── last-session move (nested latest.change_pct) ──
        info = tickers.get(tkr) or {}
        latest = info.get("latest") or {}
        change_pct = latest.get("change_pct")
        # fallback: some snapshot shapes carry change_pct at the ticker root
        if change_pct is None:
            change_pct = info.get("change_pct")
        label = _preview_magnitude_label(change_pct)
        if change_pct is None:
            move_part = f"지난 세션 {label}"
        else:
            try:
                move_part = f"지난 세션 {label}({float(change_pct):+.2f}%)"
            except (TypeError, ValueError):
                move_part = f"지난 세션 {label}"

        # ── short + insider signals (verbatim flag text) ──
        signal_texts = [
            f.get("text", "").strip()
            for f in live_flags
            if f.get("ticker") == tkr
            and f.get("domain") in {"short", "insider"}
            and f.get("text", "").strip()
        ]
        if signal_texts:
            signals_part = " · ".join(signal_texts)
        else:
            signals_part = "특이 수급 신호 없음"

        lines.append(f"{tkr} | {move_part} | {signals_part}")

    return "\n".join(lines)


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

    # positioning (IMPR-076: not required from LLM — Python-owned)
    # LLM's positioning is ignored; validation of this field is skipped
    # to allow the LLM to omit it or return garbage without failing validation.

    # positioning_overflow (not required from LLM — we compute it deterministically)
    # LLM's value is ignored; validation skipped

    # themes (optional)
    themes = obj.get("themes", [])
    if isinstance(themes, list):
        for i, theme in enumerate(themes):
            if not isinstance(theme, dict):
                errors.append(f"themes[{i}] is not a dict")
                continue
            if not theme.get("title") or not theme.get("story"):
                errors.append(f"themes[{i}] missing title or story")

    # m7_preview (optional/lenient — like themes; NEVER hard-fail when absent/short)
    m7p = obj.get("m7_preview", [])
    if isinstance(m7p, list):
        for i, entry in enumerate(m7p):
            if not isinstance(entry, dict):
                errors.append(f"m7_preview[{i}] is not a dict")
                continue
            if not entry.get("ticker") or not entry.get("watch"):
                errors.append(f"m7_preview[{i}] missing ticker or watch")

    # data_as_of
    if not obj.get("data_as_of"):
        errors.append("data_as_of is missing")

    return errors


# m7_preview magnitude guard — SCOPED to watch lines only (bare 급등/급락, any 활용형).
# Deliberately NOT added to the global _PREVIEW_FORBIDDEN_RE / check_forbidden_words,
# which scan the whole preview object and would wrongly reject a legitimate past-tense
# 급락 in macro_position/themes.
_M7_MAGNITUDE_RE = re.compile(r"급등|급락")


def _check_m7_preview_magnitude(obj: dict) -> list[str]:
    """Scan ONLY m7_preview[i].watch for LLM-invented magnitude words (급등/급락, any form).

    The last-session move must use the Python-supplied label verbatim
    (보합/상승/큰 폭 상승/하락/큰 폭 하락) — the LLM must not substitute 급등/급락 even when
    describing the past session. Confined to watch strings so a legitimate past-tense
    급락 elsewhere in the preview is untouched. Returns [] when m7_preview is absent
    or malformed (leniency preserved elsewhere).
    """
    violations = []
    m7 = obj.get("m7_preview")
    if not isinstance(m7, list):
        return violations
    for i, entry in enumerate(m7):
        if not isinstance(entry, dict):
            continue
        watch = entry.get("watch")
        if isinstance(watch, str):
            m = _M7_MAGNITUDE_RE.search(watch)
            if m:
                violations.append(f"m7_preview[{i}].watch: found magnitude word '{m.group()}'")
    return violations


# m7_preview supply-number guard — SCOPED to watch lines only. The 수급 상세 panel is
# the single source of exact supply numbers; watch facts come from a DIFFERENT snapshot
# and citing them causes a same-card mismatch (e.g. watch "38.1%" vs panel "46.5%").
# So a watch line may carry AT MOST one numeric token — the last-session move % — and
# must describe short/insider signals qualitatively. Confined to watch strings; the
# global forbidden regex is NOT broadened (macro_position/themes legitimately use these).
_M7_NUM_NBUILDING_RE = re.compile(r"\d+\s*일")   # building-days: "13일", "13 일"
_M7_NUM_NCOUNT_RE = re.compile(r"\d+\s*건")      # insider counts: "3건", "1 건"


def _check_m7_preview_numbers(obj: dict) -> list[str]:
    """Scan ONLY m7_preview[i].watch for disallowed supply-number tokens.

    Rejects a watch line containing any of: '%p', '×', a digit-then-'건', a digit-then-'일',
    OR more than one '%' token (the single allowed '%' is the last-session move). Returns []
    when m7_preview is absent/malformed (leniency preserved).
    """
    violations = []
    m7 = obj.get("m7_preview")
    if not isinstance(m7, list):
        return violations
    for i, entry in enumerate(m7):
        if not isinstance(entry, dict):
            continue
        watch = entry.get("watch")
        if not isinstance(watch, str):
            continue
        hits = []
        if "%p" in watch:
            hits.append("%p")
        if "×" in watch:
            hits.append("×")
        if _M7_NUM_NCOUNT_RE.search(watch):
            hits.append("N건")
        if _M7_NUM_NBUILDING_RE.search(watch):
            hits.append("N일")
        if watch.count("%") > 1:
            hits.append("multiple %")
        if hits:
            violations.append(f"m7_preview[{i}].watch: disallowed supply-number token(s) {hits}")
    return violations


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
    sector_context = extract_sector_snapshot(notes_root, data_as_of, mode="preview", stale_days=4)
    # M7 관전 포인트 팩트 — reuse the watchlist snapshot loader + already-scored live_flags
    wl_snap = load_watchlist_snapshot(notes_root)
    m7_facts = _format_m7_preview_facts(live_flags, wl_snap)

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
                "sector_context": sector_context,
                "themes_context": themes_text,
                "m7_preview_facts": m7_facts,
            })
            raw_text = (resp.content or "").strip()
        except _WallClockTimeout:
            log(f"LLM call exceeded {LLM_WALL_CLOCK_SECONDS}s (attempt {attempt}) — skip")
            alert_generation_failure("preview", monday_date, "llm_timeout", f"wall-clock {LLM_WALL_CLOCK_SECONDS}s exceeded attempt {attempt}")
            write_output(notes_root, monday_date, data_as_of, "skipped", None, content_type="preview", generator="preview")
            return 0
        except Exception as e:
            log(f"LLM call failed ({e}) (attempt {attempt}) — skip")
            alert_generation_failure("preview", monday_date, "llm_exception", str(e)[:120])
            write_output(notes_root, monday_date, data_as_of, "skipped", None, content_type="preview", generator="preview")
            return 0
        finally:
            cancel_wall_clock()

        if not raw_text:
            log(f"LLM returned empty text (attempt {attempt}) — skip")
            alert_generation_failure("preview", monday_date, "empty_output", f"attempt {attempt}")
            write_output(notes_root, monday_date, data_as_of, "skipped", None, content_type="preview", generator="preview")
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
            alert_generation_failure("preview", monday_date, "json_parse_fail", "extract_json_from_response returned None after 2 attempts")
            write_output(notes_root, monday_date, data_as_of, "skipped", None, content_type="preview", generator="preview")
            return 0

        # ── Schema validation ───────────────────────────────────────────────
        val_errors = _validate_preview_narrative(preview_obj)
        if val_errors:
            log(f"schema validation failed (attempt {attempt}): {val_errors}")
            if attempt < 2:
                log("retrying LLM call...")
                continue
            log("giving up after 2 attempts — skip")
            alert_generation_failure("preview", monday_date, "schema_validation_fail", str(val_errors)[:120])
            write_output(notes_root, monday_date, data_as_of, "skipped", None, content_type="preview", generator="preview")
            return 0

        # ── Shared forbidden word scan ──────────────────────────────────────
        fw_violations = check_forbidden_words(preview_obj)
        if fw_violations:
            log(f"shared forbidden word violation (attempt {attempt}): {fw_violations}")
            if attempt < 2:
                log("retrying LLM call...")
                continue
            log("giving up after 2 attempts — skip")
            alert_generation_failure("preview", monday_date, "forbidden_word_fail", str(fw_violations)[:120])
            write_output(notes_root, monday_date, data_as_of, "skipped", None, content_type="preview", generator="preview")
            return 0

        # ── Preview-specific forbidden word scan ────────────────────────────
        preview_fw = _check_preview_forbidden(preview_obj)
        if preview_fw:
            log(f"preview forbidden word violation (attempt {attempt}): {preview_fw}")
            if attempt < 2:
                log("retrying LLM call...")
                continue
            log("giving up after 2 attempts — skip")
            alert_generation_failure("preview", monday_date, "forbidden_word_fail", str(preview_fw)[:120])
            write_output(notes_root, monday_date, data_as_of, "skipped", None, content_type="preview", generator="preview")
            return 0

        # ── m7_preview watch-line guards (SCOPED to watch lines only) ────────
        # (1) magnitude words (급등/급락) and (2) disallowed supply-number tokens
        # (공매도 %/±%p/×N/N건/N일 — cross-snapshot mismatch with 수급 상세 panel).
        # On any hit → retry (cap 2). On the final attempt, do NOT hard-fail the whole
        # preview: drop m7_preview leniently and ship the rest (optional field).
        m7_mag = _check_m7_preview_magnitude(preview_obj)
        m7_num = _check_m7_preview_numbers(preview_obj)
        if m7_mag or m7_num:
            log(f"m7_preview watch-line violation (attempt {attempt}): "
                f"magnitude={m7_mag} numbers={m7_num}")
            if attempt < 2:
                log("retrying LLM call...")
                continue
            log("m7_preview still violates watch-line guards after 2 attempts — dropping "
                "m7_preview (lenient), keeping rest of preview")
            preview_obj["m7_preview"] = []

        # ── All checks passed ───────────────────────────────────────────────
        log(f"preview narrative validated OK (attempt {attempt})")

        # ── IMPR-076: Overwrite positioning with Python-deterministic values ────
        # LLM's positioning is ignored; we recompute it deterministically
        py_positioning, py_overflow = compute_preview_positioning(live_flags, cs_cards, PREVIEW_TOP_N)
        log(f"positioning: Python {len(py_positioning)} items + {py_overflow} overflow (replacing LLM's {len(preview_obj.get('positioning', []))} + {preview_obj.get('positioning_overflow', 0)})")

        # ── Write output ────────────────────────────────────────────────────
        write_output(
            notes_root, monday_date, data_as_of,
            "ok", None,
            content_type="preview",
            preview_fields={
                "hero_oneliner": preview_obj.get("hero_oneliner"),
                "calendar": preview_obj.get("calendar", []),
                "macro_position": preview_obj.get("macro_position"),
                "positioning": py_positioning,
                "positioning_overflow": py_overflow,
                "themes": preview_obj.get("themes", []),
                "m7_preview": preview_obj.get("m7_preview", []),
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
        try:
            import sys as _sys
            _argv = _sys.argv
            _run_date = _argv[2] if len(_argv) >= 3 else "?"
        except Exception:
            _run_date = "?"
        alert_generation_failure("preview", _run_date, "unexpected", str(e)[:120])
        sys.exit(0)
