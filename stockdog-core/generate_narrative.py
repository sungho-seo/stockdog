#!/usr/bin/env python3
"""IMPR-064 P1+P2 — gated daily narrative generator.

Generates a structured JSON narrative ("hero_oneliner", "market_narrative",
"macro_story", "indicator_captions") from today's US daily-market report,
macro tracker, signals tracker, and Fear & Greed scalar.  Written to:

    <notes>/raw/stockdog/narrative/narrative.json

GATED — cost control. The LLM is called ONLY when:
  1. A US daily-market report exists for run_date (no report → "skipped", $0).
  2. narrative.json does NOT already have status=="ok" for run_date (idempotent).

Gate is checked BEFORE any LLM import, so quiet/holiday days incur $0.

ALWAYS exits 0. Every failure (gate skip, LLM error, wall-clock timeout,
validation failure, JSON parse error) → status:"skipped" written to the
output file and exit 0, so the publish chain is never broken.

Usage:
    python generate_narrative.py <notes_root> <date>

    <notes_root>  path to vault root (container: /notes)
    <date>        run date as YYYY-MM-DD
"""

import argparse
import json
import os
import re
import signal
import sys
from datetime import datetime
from pathlib import Path

LOG = "[generate_narrative]"
LLM_WALL_CLOCK_SECONDS = 120
SCHEMA_VERSION = 1

# M7 universe — canonical order, locked with config/m7.yaml
_M7_TICKERS = ["AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "TSLA"]

# Qualitative magnitude label from change_pct (None → unknown)
def _magnitude_label(change_pct):
    """Convert numeric change_pct to Korean qualitative label."""
    if change_pct is None:
        return "변화 미확인"
    pct = float(change_pct)
    if pct >= 3.0:
        return "큰 폭 상승"
    elif pct >= 1.0:
        return "소폭 상승"
    elif pct >= -1.0:
        return "보합"
    elif pct >= -3.0:
        return "소폭 하락"
    else:
        return "큰 폭 하락"

# Sparkline chars to strip from macro text (avoid token waste / rendering noise)
_SPARKLINE_RE = re.compile(r"[▁▂▃▄▅▆▇█]+")

# Forbidden words (투자 권유·단정 표현) — regex for post-generation scan.
#
# bare "매수"/"매도" 는 제외: "매수세", "매도세", "순매수", "순매도", "매수 우위" 같은
# 정상적인 시장 서술 어휘에서 false-positive 가 발생하기 때문.
# bare "추천" 도 제외: "추천주는 없다" 등 부정문에서 오매칭.
# 대신 명령·권유형(투자 조언) 패턴만 타깃으로 삼는다.
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
    print(f"{LOG} {msg}", flush=True)


class _WallClockTimeout(Exception):
    pass


def _alarm_handler(signum, frame):
    raise _WallClockTimeout()


# ---------------------------------------------------------------------------
# Gate helpers
# ---------------------------------------------------------------------------

def _check_report_exists(notes_root: Path, run_date: str) -> Path | None:
    """Return path to Market_Report_US_<date>.md if it exists, else None."""
    report_path = (
        notes_root / "raw" / "stockdog" / "daily-market"
        / run_date / f"Market_Report_US_{run_date}.md"
    )
    if report_path.is_file():
        return report_path
    log(f"US market report not found at {report_path} — skip (no LLM call)")
    return None


def _check_idempotent(notes_root: Path, run_date: str) -> bool:
    """Return True if we should SKIP (already have a good P1 narrative + M7 for today)."""
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
        and existing.get("m7_status") == "ok"
    ):
        log(f"narrative.json already ok+m7_ok for {run_date} — skip (idempotent, no LLM call)")
        return True
    return False


# ---------------------------------------------------------------------------
# Source excerpt builders
# ---------------------------------------------------------------------------

def _extract_report_sections(report_path: Path, run_date: str) -> tuple[str, str]:
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


def _extract_macro_excerpt(notes_root: Path) -> str:
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


def _extract_fear_greed(notes_root: Path, run_date: str) -> str:
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


def _extract_signals_excerpt(notes_root: Path) -> str:
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
# M7 source builder
# ---------------------------------------------------------------------------

def _build_m7_context(notes_root: Path, run_date: str) -> str:
    """Build per-ticker context block for the M7 story prompt.

    For each M7 ticker assembles:
      - Price movement label from watchlist_snapshot (qualitative only)
      - Insider flags from m7/<ticker>/insider_latest.json (breach/sell/buy)
      - Short data flag from m7/<ticker>/short_latest.json
      - US report mention excerpt (up to 200 chars per ticker)

    Returns a text block with one section per ticker. Never raises.
    """
    m7_dir = notes_root / "raw" / "stockdog" / "m7"
    wl_snapshot_path = notes_root / "raw" / "stockdog" / "watchlist" / "watchlist_snapshot.json"

    # Load watchlist snapshot once
    wl_snap_tickers = {}
    try:
        wl_snap = json.loads(wl_snapshot_path.read_text(encoding="utf-8"))
        wl_snap_tickers = wl_snap.get("tickers", {})
    except (OSError, ValueError):
        pass

    # Load US market report text once for mention search
    report_path = (
        notes_root / "raw" / "stockdog" / "daily-market"
        / run_date / f"Market_Report_US_{run_date}.md"
    )
    report_text = ""
    try:
        report_text = report_path.read_text(encoding="utf-8")
    except OSError:
        pass

    lines = []
    for tk in _M7_TICKERS:
        parts = [f"[{tk}]"]

        # Price movement
        tk_snap = wl_snap_tickers.get(tk, {})
        latest = tk_snap.get("latest") or {}
        change_pct = latest.get("change_pct")
        direction = _magnitude_label(change_pct)
        parts.append(f"가격움직임={direction}")

        # Insider flags
        insider_path = m7_dir / tk / "insider_latest.json"
        try:
            ins = json.loads(insider_path.read_text(encoding="utf-8"))
            txns = ins.get("transactions") or []
            breaches = [t for t in txns if t.get("breach")]
            sells = [t for t in txns if str(t.get("action", "")).lower() == "sell"]
            buys = [t for t in txns if str(t.get("action", "")).lower() == "buy"]
            if breaches:
                parts.append(f"인사이더=대규모매도({len(breaches)}건breach)")
            elif sells:
                parts.append(f"인사이더=매도({len(sells)}건)")
            elif buys:
                parts.append(f"인사이더=매수({len(buys)}건)")
            else:
                parts.append("인사이더=해당없음")
        except (OSError, ValueError):
            parts.append("인사이더=데이터없음")

        # Short interest flag
        short_path = m7_dir / tk / "short_latest.json"
        try:
            sh = json.loads(short_path.read_text(encoding="utf-8"))
            ratio = sh.get("short_ratio")
            freshness = sh.get("freshness", "")
            if ratio is not None and freshness == "fresh":
                # label high short interest (>40%) as notable
                if float(ratio) >= 40.0:
                    parts.append(f"공매도=높음({ratio:.1f}%)")
                else:
                    parts.append(f"공매도={ratio:.1f}%")
            else:
                parts.append("공매도=데이터없음")
        except (OSError, ValueError):
            parts.append("공매도=데이터없음")

        # US report mention (first 200 chars of first paragraph mentioning ticker)
        mention = ""
        if report_text and tk in report_text:
            # Find the sentence(s) mentioning the ticker
            for para in report_text.split("\n"):
                if tk in para and not para.startswith("|") and not para.startswith("#"):
                    mention = para.strip()[:200]
                    break
        if mention:
            parts.append(f"리포트발췌={mention}")
        else:
            parts.append("리포트발췌=없음")

        lines.append(" | ".join(parts))

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# M7 story prompts
# ---------------------------------------------------------------------------

# Note: {{ }} doubles for literal braces in ChatPromptTemplate
M7_SYSTEM_PROMPT = """당신은 한국 일반 대중 독자를 위한 시장 이야기꾼입니다. 어려운 금융 용어는 비유로 풀고, 친근하지만 신뢰감 있는 톤으로 씁니다.

**절대 금지**:
① 투자 권유·단정 표현 — "사라", "팔아라", "매수", "매도", "목표가", "추천", "사세요", "파세요" 등 일절 사용 금지
② 구체적 숫자(%, 가격, 금액) 출력 금지 — 주가 움직임은 '큰 폭 하락', '소폭 상승', '보합' 같은 정성적 표현만 사용
③ 소스에 없는 이벤트·실적·제품·인과 날조 절대 금지 — 제공된 소스에 명시된 사실만 사용
④ 과잉 인과 — 단정 대신 "~로 보입니다", "~와 맞물려" 등 관찰 표현 사용
⑤ 변화 미미하거나 소스 빈약하면 "오늘은 특별한 움직임이 관찰되지 않았습니다"로 정직하게 표기

모든 내용은 정보·교육·참고용입니다.

**출력 형식**: 유효한 JSON 배열 하나만 출력 (코드 펜스·인사말·설명 텍스트 없이). 모든 텍스트 한국어.

**스키마** (배열, 7개 항목):
[
  {{"ticker": "AAPL", "story": "2~3문장 팩트 스토리 — 어떤 일이 있었고 주가가 어떻게 움직였는지. 숫자 없이 정성적으로만."}}
]

각 종목당 2~3문장. ticker는 아래 7종 중 하나만 사용. story는 절대 비워두지 말 것."""

M7_HUMAN_TEMPLATE = """오늘은 {report_date} 기준입니다. 아래 소스 안의 사실만 사용하세요.

[소스 — M7 종목별 팩트 컨텍스트]
(형식: [TICKER] 가격움직임 | 인사이더 | 공매도비율 | 리포트발췌)
{m7_context}

위 스키마대로 7개 항목의 유효한 JSON 배열 하나만 출력하세요."""


# ---------------------------------------------------------------------------
# M7 validation
# ---------------------------------------------------------------------------

def _validate_m7_stories(stories) -> list[str]:
    """Validate M7 stories list. Returns list of errors (empty = valid)."""
    errors = []
    if not isinstance(stories, list):
        errors.append("m7_stories is not a list")
        return errors

    seen_tickers = set()
    for i, item in enumerate(stories):
        if not isinstance(item, dict):
            errors.append(f"m7_stories[{i}] is not a dict")
            continue
        tk = item.get("ticker")
        if tk not in _M7_TICKERS:
            errors.append(f"m7_stories[{i}].ticker '{tk}' not in M7 whitelist")
        if tk in seen_tickers:
            errors.append(f"m7_stories[{i}].ticker '{tk}' duplicate")
        seen_tickers.add(tk)
        story = item.get("story")
        if not isinstance(story, str) or not story.strip():
            errors.append(f"m7_stories[{i}].story is empty")

    # All 7 tickers should be present
    missing = [t for t in _M7_TICKERS if t not in seen_tickers]
    if missing:
        errors.append(f"m7_stories missing tickers: {missing}")

    return errors


# ---------------------------------------------------------------------------
# M7 story builder (main callable)
# ---------------------------------------------------------------------------

def _build_m7_stories(notes_root: Path, run_date: str, llm):
    """Build M7 per-ticker stories via a separate LLM call.

    Returns (stories_list, status_str) where:
      - stories_list: list of {ticker, story} dicts (or None on failure)
      - status_str: "ok" | "skipped"

    Failures (LLM error, validation fail after 2 attempts) → (None, "skipped").
    Never raises — all exceptions caught internally.
    """
    try:
        from langchain_core.prompts import ChatPromptTemplate
        m7_prompt = ChatPromptTemplate.from_messages([
            ("system", M7_SYSTEM_PROMPT),
            ("human", M7_HUMAN_TEMPLATE),
        ])
        m7_chain = m7_prompt | llm.bind(max_tokens=1500, temperature=0.4)
    except Exception as e:
        log(f"[m7] prompt/chain build failed ({e}) — skip")
        return None, "skipped"

    m7_context = _build_m7_context(notes_root, run_date)

    for attempt in range(1, 3):
        signal.signal(signal.SIGALRM, _alarm_handler)
        signal.alarm(LLM_WALL_CLOCK_SECONDS)
        raw_text = ""
        try:
            resp = m7_chain.invoke({
                "report_date": run_date,
                "m7_context": m7_context,
            })
            raw_text = (resp.content or "").strip()
        except _WallClockTimeout:
            log(f"[m7] LLM call exceeded {LLM_WALL_CLOCK_SECONDS}s (attempt {attempt}) — skip")
            return None, "skipped"
        except Exception as e:
            log(f"[m7] LLM call failed ({e}) (attempt {attempt}) — skip")
            return None, "skipped"
        finally:
            signal.alarm(0)

        if not raw_text:
            log(f"[m7] LLM returned empty text (attempt {attempt}) — skip")
            return None, "skipped"

        # Strip code fences
        stripped = re.sub(r"^```(?:json)?\s*", "", raw_text, flags=re.IGNORECASE)
        stripped = re.sub(r"\s*```$", "", stripped).strip()

        try:
            stories = json.loads(stripped)
        except (ValueError, json.JSONDecodeError) as e:
            log(f"[m7] JSON parse failed ({e}) (attempt {attempt})")
            if attempt < 2:
                log("[m7] retrying LLM call...")
                continue
            log("[m7] giving up after 2 attempts — skip")
            return None, "skipped"

        # Schema validation
        val_errors = _validate_m7_stories(stories)
        if val_errors:
            log(f"[m7] schema validation failed (attempt {attempt}): {val_errors}")
            if attempt < 2:
                log("[m7] retrying LLM call...")
                continue
            log("[m7] giving up after 2 attempts — skip")
            return None, "skipped"

        # Forbidden word scan on each story
        fw_violations = []
        for item in stories:
            m = _FORBIDDEN_RE.search(item.get("story", ""))
            if m:
                fw_violations.append(f"{item.get('ticker')}: found '{m.group()}'")
        if fw_violations:
            log(f"[m7] forbidden word violation (attempt {attempt}): {fw_violations}")
            if attempt < 2:
                log("[m7] retrying LLM call...")
                continue
            log("[m7] giving up after 2 attempts — skip")
            return None, "skipped"

        log(f"[m7] stories validated OK (attempt {attempt}), {len(stories)} tickers")
        return stories, "ok"

    return None, "skipped"


# ---------------------------------------------------------------------------
# Output writer
# ---------------------------------------------------------------------------

def _write_output(notes_root: Path, run_date: str, data_as_of: str,
                  status: str, narrative,
                  m7_status: str = "skipped", m7_stories=None) -> None:
    """Write narrative.json (always, even on skip/failure).

    m7_status: "ok" | "skipped"
    m7_stories: list of {ticker, story} or None
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
        "narrative": narrative,
        "m7_status": m7_status,
        "m7_stories": m7_stories if m7_status == "ok" else None,
    }
    try:
        out_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        log(f"wrote {out_path} (status={status}, m7_status={m7_status})")
    except OSError as e:
        log(f"failed to write output ({e})")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _validate_narrative(obj: dict) -> list[str]:
    """Return list of validation errors (empty = valid)."""
    errors = []

    if not isinstance(obj.get("hero_oneliner"), str) or not obj["hero_oneliner"].strip():
        errors.append("hero_oneliner is empty")

    mn = obj.get("market_narrative", {})
    if not isinstance(mn, dict):
        errors.append("market_narrative is not a dict")
    else:
        kw = mn.get("keywords", [])
        if not isinstance(kw, list) or not (2 <= len(kw) <= 4):
            errors.append(f"market_narrative.keywords len {len(kw) if isinstance(kw, list) else 'N/A'} not in 2..4")
        if not isinstance(mn.get("story"), str) or not mn["story"].strip():
            errors.append("market_narrative.story is empty")

    ms = obj.get("macro_story", {})
    if not isinstance(ms, dict):
        errors.append("macro_story is not a dict")
    else:
        if not isinstance(ms.get("story"), str) or not ms["story"].strip():
            errors.append("macro_story.story is empty")
        if not isinstance(ms.get("kr_impact"), str) or not ms["kr_impact"].strip():
            errors.append("macro_story.kr_impact is empty (required)")

    ic = obj.get("indicator_captions", [])
    if not isinstance(ic, list) or not (3 <= len(ic) <= 5):
        errors.append(f"indicator_captions len {len(ic) if isinstance(ic, list) else 'N/A'} not in 3..5")
    else:
        for i, item in enumerate(ic):
            if not isinstance(item, dict):
                errors.append(f"indicator_captions[{i}] is not a dict")
                continue
            if not isinstance(item.get("label"), str) or not item["label"].strip():
                errors.append(f"indicator_captions[{i}].label is empty")
            if not isinstance(item.get("caption"), str) or not item["caption"].strip():
                errors.append(f"indicator_captions[{i}].caption is empty")

    return errors


def _check_forbidden(obj: dict) -> list[str]:
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


# ---------------------------------------------------------------------------
# LLM prompt
# ---------------------------------------------------------------------------

# Note: literal curly braces in the JSON schema example are doubled {{ }} so
# that ChatPromptTemplate does NOT interpret them as template variables.
SYSTEM_PROMPT = """당신은 한국 일반 대중 독자를 위한 시장 이야기꾼입니다. 어려운 금융 용어는 비유로 풀고, 친근하지만 신뢰감 있는 톤으로 씁니다.

**절대 금지**:
① 투자 권유·단정 표현 — "사라", "팔아라", "매수", "매도", "목표가", "추천", "사세요", "파세요" 등 일절 사용 금지
② 소스에 없는 숫자·사건·인과 날조 — 제공된 데이터에 없는 내용을 지어내지 말 것
③ 과잉 인과 — 단정 대신 "~로 보입니다", "~와 맞물려" 등 관찰 표현 사용
모든 내용은 정보·교육·참고용입니다.

**출력 형식**: 유효한 JSON 객체 하나만 출력 (코드 펜스·인사말·설명 텍스트 없이). 모든 텍스트 한국어.

**스키마**:
{{
  "hero_oneliner": "시장 전체를 한 문장으로 — 오늘 가장 중요한 흐름 (1문장, 30자 내외)",
  "market_narrative": {{
    "keywords": ["핵심 키워드1", "핵심 키워드2"],
    "story": "오늘 시장의 큰 흐름을 2-3문장으로 — 지수·섹터·심리의 연결고리"
  }},
  "macro_story": {{
    "story": "금리·인플레·달러·FOMC 등 매크로 맥락 2-3문장",
    "kr_impact": "이 매크로 흐름이 한국장(코스피/원화/수출주)에 어떻게 연결되는지 1-2문장 (필수, 절대 비워두지 말 것)"
  }},
  "indicator_captions": [
    {{"label": "Fear & Greed", "caption": "지수 의미 + 미국장 또는 한국장에 어떻게·왜 연결되는지"}},
    {{"label": "VIX", "caption": "변동성 레벨 해석 + 미국장 또는 한국장 연결"}},
    {{"label": "美 10Y", "caption": "금리 수준 + 한국장(환율·외국인 수급)에 어떻게 연결되는지"}}
  ]
}}

indicator_captions는 항상 Fear & Greed·VIX·美 10Y 3개를 포함하고, 스프레드나 달러 움직임이 두드러지면 최대 5개까지 추가 가능. 각 caption은 '미국장 또는 한국장에 어떻게·왜'를 반드시 포함. 데이터가 없으면 지어내지 말고 "(데이터 없음)"으로 정직하게 표기.

keywords는 2개 이상 4개 이하. story, kr_impact는 절대 빈 문자열 사용 금지."""

HUMAN_TEMPLATE = """오늘은 {report_date} 기준입니다. 아래 소스 안의 사실만 사용하세요.

[소스1 — 오늘의 US 데일리 마켓 리포트 발췌]
{daily_market}

[소스2 — 매크로 트래커 발췌 (스파크라인 제거됨)]
{macro}

[소스3 — Fear & Greed 스칼라]
{fear_greed}

[소스4 — 시그널 집계 (참고용, 개별 종목 포지셔닝 반복 말고 시장 전체 흐름을 이야기)]
{signals}

위 스키마대로 유효한 JSON 객체 하나만 출력하세요."""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Gated daily narrative generator.",
        add_help=False,
    )
    parser.add_argument("notes_root", nargs="?", default=None,
                        help="Path to vault root (container: /notes)")
    parser.add_argument("date", nargs="?", default=None,
                        help="Run date as YYYY-MM-DD")
    parser.add_argument("--force", action="store_true", default=False,
                        help="Bypass idempotency gate (Gate 2) only — "
                             "US report gate (Gate 1) is never bypassed")
    args, _ = parser.parse_known_args()

    if not args.notes_root or not args.date:
        log("usage: generate_narrative.py <notes_root> <date> [--force] — skip")
        return 0

    notes_root = Path(args.notes_root).expanduser()
    run_date = args.date
    force = args.force
    data_as_of = run_date  # will be updated once report is read

    # ── GATE 1: US report exists? (NO LLM import before this) ──────────────
    # --force does NOT bypass this gate: no report → $0, always skipped.
    report_path = _check_report_exists(notes_root, run_date)
    if report_path is None:
        _write_output(notes_root, run_date, data_as_of, "skipped", None)
        return 0

    # ── GATE 2: idempotent — already have status:ok for today? ─────────────
    # --force bypasses this gate only.
    if not force and _check_idempotent(notes_root, run_date):
        return 0  # no write needed; existing file is already correct
    if force:
        log("--force: skipping idempotency gate (Gate 2)")

    # ── Source excerpts (all fallback to placeholder on failure) ───────────
    daily_market, data_as_of = _extract_report_sections(report_path, run_date)
    macro = _extract_macro_excerpt(notes_root)
    fear_greed = _extract_fear_greed(notes_root, run_date)
    signals = _extract_signals_excerpt(notes_root)

    # ── LLM import (ONLY here — after both gates pass) ─────────────────────
    try:
        from analysis.llm_analyzer import get_llm
        llm = get_llm()
    except Exception as e:
        log(f"get_llm import/init failed ({e}) — skip")
        _write_output(notes_root, run_date, data_as_of, "skipped", None)
        return 0
    if llm is None:
        log("no LLM configured (no API key) — skip")
        _write_output(notes_root, run_date, data_as_of, "skipped", None)
        return 0

    # get_llm() returns ChatAnthropic(temperature=0.2). We want 0.4 for this
    # narrative task. Use .bind() to override on the chain level — ChatAnthropic
    # accepts temperature as a bind param (passed through to the API call).
    # Note: get_llm() signature has no temperature arg, so we cannot pass it
    # there; .bind(temperature=0.4) is the correct override path.
    try:
        from langchain_core.prompts import ChatPromptTemplate
        prompt = ChatPromptTemplate.from_messages([
            ("system", SYSTEM_PROMPT),
            ("human", HUMAN_TEMPLATE),
        ])
        chain = prompt | llm.bind(max_tokens=2000, temperature=0.4)
    except Exception as e:
        log(f"prompt/chain build failed ({e}) — skip")
        _write_output(notes_root, run_date, data_as_of, "skipped", None)
        return 0

    # ── LLM call with wall-clock guard ─────────────────────────────────────
    for attempt in range(1, 3):  # max 2 attempts (1 retry on validation fail)
        signal.signal(signal.SIGALRM, _alarm_handler)
        signal.alarm(LLM_WALL_CLOCK_SECONDS)
        raw_text = ""
        try:
            resp = chain.invoke({
                "report_date": run_date,
                "daily_market": daily_market,
                "macro": macro,
                "fear_greed": fear_greed,
                "signals": signals,
            })
            raw_text = (resp.content or "").strip()
        except _WallClockTimeout:
            log(f"LLM call exceeded {LLM_WALL_CLOCK_SECONDS}s (attempt {attempt}) — skip")
            _write_output(notes_root, run_date, data_as_of, "skipped", None)
            return 0
        except Exception as e:
            log(f"LLM call failed ({e}) (attempt {attempt}) — skip")
            _write_output(notes_root, run_date, data_as_of, "skipped", None)
            return 0
        finally:
            signal.alarm(0)

        if not raw_text:
            log(f"LLM returned empty text (attempt {attempt}) — skip")
            _write_output(notes_root, run_date, data_as_of, "skipped", None)
            return 0

        # ── Parse JSON ─────────────────────────────────────────────────────
        # Strip code fences if the model wrapped the output despite instructions
        stripped = re.sub(r"^```(?:json)?\s*", "", raw_text, flags=re.IGNORECASE)
        stripped = re.sub(r"\s*```$", "", stripped).strip()
        try:
            narrative_obj = json.loads(stripped)
        except (ValueError, json.JSONDecodeError) as e:
            log(f"JSON parse failed ({e}) (attempt {attempt})")
            if attempt < 2:
                log("retrying LLM call...")
                continue
            log("giving up after 2 attempts — skip")
            _write_output(notes_root, run_date, data_as_of, "skipped", None)
            return 0

        # ── Schema validation ───────────────────────────────────────────────
        val_errors = _validate_narrative(narrative_obj)
        if val_errors:
            log(f"schema validation failed (attempt {attempt}): {val_errors}")
            if attempt < 2:
                log("retrying LLM call...")
                continue
            log("giving up after 2 attempts — skip")
            _write_output(notes_root, run_date, data_as_of, "skipped", None)
            return 0

        # ── Forbidden word scan ─────────────────────────────────────────────
        fw_violations = _check_forbidden(narrative_obj)
        if fw_violations:
            log(f"forbidden word violation (attempt {attempt}): {fw_violations}")
            if attempt < 2:
                log("retrying LLM call...")
                continue
            log("giving up after 2 attempts — skip")
            _write_output(notes_root, run_date, data_as_of, "skipped", None)
            return 0

        # ── All checks passed ───────────────────────────────────────────────
        log(f"narrative validated OK (attempt {attempt})")

        # ── M7 stories (P2 — independent LLM call, P1 preserved on failure) ─
        log("starting M7 stories generation (P2)...")
        m7_stories, m7_status = _build_m7_stories(notes_root, run_date, llm)

        _write_output(
            notes_root, run_date, data_as_of,
            "ok", narrative_obj,
            m7_status=m7_status, m7_stories=m7_stories,
        )
        return 0

    # Should not reach here, but guard
    _write_output(notes_root, run_date, data_as_of, "skipped", None)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # absolute last-resort guard — ALWAYS exit 0
        print(f"{LOG} unexpected error ({e}) — exit 0", flush=True)
        sys.exit(0)
