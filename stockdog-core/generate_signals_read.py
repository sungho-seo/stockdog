#!/usr/bin/env python3
"""IMPR-067 — automate the signals "오늘의 읽기" (gated).

Cron-safe automation of the manual /signals-read skill: read the rule-based
signals.md aggregation page, ask the LLM (Anthropic Sonnet 4.6, reused via
analysis.llm_analyzer.get_llm) for a 3-5문장 analyst interpretation, and inject
it into the TODAY_READ block via inject_today_read.py.

GATED — this is the cost control. The LLM is called ONLY on notable-signal days,
decided by the gate sidecar that render_signals_tracker.py writes:

    <notes>/raw/stockdog/signals/signal_count.json
    {"date": "...", "major": N, "watch": N, "notable": bool}

Quiet (no-signal) days → skip BEFORE any LLM call → $0. The gate is also dated:
a stale sidecar (date != run date) is treated as "skip" so we never read a quiet
LLM on a mismatched day.

Usage:
    python generate_signals_read.py <notes_root> <date>

ALWAYS exits 0. Every failure mode (gate missing/stale/quiet, no LLM key, API
error, wall-clock timeout, empty response, inject failure) logs and returns 0 so
the publish chain in sync_vault.sh is never broken; on any skip/failure the
renderer's placeholder block simply stays in place.
"""

import json
import os
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

LOG = "[generate_signals_read]"
LLM_WALL_CLOCK_SECONDS = 90
INJECT_TIMEOUT_SECONDS = 30

# In the container (COPY . . -> /app) this module and inject_today_read.py are
# siblings; resolve relative to this file so it works regardless of CWD.
HERE = Path(__file__).resolve().parent
INJECT_HELPER = HERE / "inject_today_read.py"


def log(msg: str) -> None:
    print(f"{LOG} {msg}", flush=True)


class _WallClockTimeout(Exception):
    pass


def _alarm_handler(signum, frame):
    raise _WallClockTimeout()


SYSTEM_INSTRUCTION = """당신은 시장 시그널 해석 analyst입니다. 아래 규칙 기반 "오늘의 시그널" 집계 페이지를 읽고, 사람이 읽을 **"오늘의 읽기"** 해석을 작성합니다.

작성 지침 (반드시 지킬 것):
1. 리스트 재진술 금지 — "MSFT breach, GOOGL 클러스터..." 같은 시그널 나열을 다시 하지 말 것. 페이지에 이미 있습니다.
2. analyst가 *유일하게* 더할 수 있는 것만 쓸 것:
   ① 점 잇기 — 서로 다른 트래커 신호의 연결.
   ② 레짐 한 줄 — 최근 흐름 속 오늘의 위치.
   ③ 상충 신호 정리 — 예: F&G는 Greed인데 내부자 매도 집중.
   ④ 부재의 의미 — 조용한 날이면 "이벤트 공백 구간"이라는 해석.
3. 3-5문장. 한국어. **시그널/관찰** 언어만. "매수/매도" 단정 표현 금지.
4. 추측·날조 금지. 페이지에 근거 없는 수치·종목을 만들지 말 것.
5. 영문 전문용어를 절대 사용하지 말 것. 본문 한국어 문장에 다음 단어들이 나타나면 안 됨: "building", "breach", "cluster", "drift", "backtest" 등. 반드시 자연스러운 한국어로 대체할 것.
   - 코드 제거: `(~Nd, building)`, `(~12d, building)`, 임계 코드 `CS-1` 등을 본문에 옮기지 말 것.
   - 필수 한글화 예시 (이 외 발견 시 즉시 한글화):
     • "building 구간" → "데이터가 아직 충분하지 않은" / "신뢰도가 낮은" / "표본이 부족한"
     • "breach" → "임계 초과" / "초과 달성"
     • "cluster" → "집중" / "군집"
     • "drift" → "변화" / "추이" / "움직임"
   - 예시: "공매도 데이터가 12일 분량이라 신뢰도가 낮다" (○), "12일 내외의 building 구간" (×).
6. 출력은 해석 본문만 — 마크다운 문단 텍스트, 헤더/마커/리스트 없이 문장만. 앞뒤 군더더기 없이 반환."""

HUMAN_TEMPLATE = """다음은 오늘({date})의 규칙 기반 "오늘의 시그널" 집계 페이지입니다.

<signals_page>
{signals}
</signals_page>
{brief_block}
위 지침에 따라 "오늘의 읽기" 해석 본문만 반환하세요."""


def _load_gate(notes_root: Path, run_date: str):
    """Return gate dict if notable for run_date, else None (skip)."""
    gate_path = notes_root / "raw" / "stockdog" / "signals" / "signal_count.json"
    if not gate_path.is_file():
        log(f"gate sidecar missing ({gate_path}) — skip (no LLM call)")
        return None
    try:
        gate = json.loads(gate_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        log(f"gate sidecar unreadable ({e}) — skip (no LLM call)")
        return None

    if gate.get("date") != run_date:
        log(f"gate date {gate.get('date')!r} != run date {run_date!r} (stale) — skip (no LLM call)")
        return None
    if not gate.get("notable"):
        log("no notable signals — skip (no LLM call)")
        return None

    log(f"notable day — proceeding (major={gate.get('major')}, watch={gate.get('watch')})")
    return gate


def _recent_brief_block(notes_root: Path) -> str:
    """Optional: newest daily-brief, truncated, as extra context. Best-effort."""
    try:
        briefs_dir = notes_root / "10_Daily" / "briefs"
        if not briefs_dir.is_dir():
            return ""
        files = sorted(briefs_dir.glob("*.md"))
        if not files:
            return ""
        text = files[-1].read_text(encoding="utf-8")[:2000].strip()
        if not text:
            return ""
        return f"\n<recent_daily_brief truncated>\n{text}\n</recent_daily_brief>\n"
    except OSError:
        return ""


def main() -> int:
    if len(sys.argv) != 3:
        log("usage: generate_signals_read.py <notes_root> <date> — skip")
        return 0

    notes_root = Path(sys.argv[1]).expanduser()
    run_date = sys.argv[2]

    # 1. Gate FIRST — no LLM import/call until we know it's a notable day.
    if _load_gate(notes_root, run_date) is None:
        return 0

    signals_path = notes_root / "10_Public" / "trackers" / "signals.md"
    try:
        signals_md = signals_path.read_text(encoding="utf-8")
    except OSError as e:
        log(f"cannot read signals.md ({e}) — skip")
        return 0

    # 2. Reuse the shared client (Anthropic Sonnet 4.6). Never new-client here.
    try:
        from analysis.llm_analyzer import get_llm
        llm = get_llm()
    except Exception as e:  # import/init failure is non-fatal
        log(f"get_llm import/init failed ({e}) — skip")
        return 0
    if llm is None:
        log("no LLM configured (no API key) — skip")
        return 0

    brief_block = _recent_brief_block(notes_root)

    # 3. Build + invoke with a wall-clock guard. SIGALRM is fine: main thread.
    try:
        from langchain_core.prompts import ChatPromptTemplate
        prompt = ChatPromptTemplate.from_messages([
            ("system", SYSTEM_INSTRUCTION),
            ("human", HUMAN_TEMPLATE),
        ])
        chain = prompt | llm.bind(max_tokens=1024)
    except Exception as e:
        log(f"prompt/chain build failed ({e}) — skip")
        return 0

    signal.signal(signal.SIGALRM, _alarm_handler)
    signal.alarm(LLM_WALL_CLOCK_SECONDS)
    try:
        resp = chain.invoke({
            "date": run_date,
            "signals": signals_md,
            "brief_block": brief_block,
        })
        text = (resp.content or "").strip()
    except _WallClockTimeout:
        log(f"LLM call exceeded {LLM_WALL_CLOCK_SECONDS}s wall clock — skip")
        return 0
    except Exception as e:
        log(f"LLM call failed ({e}) — skip")
        return 0
    finally:
        signal.alarm(0)

    if not text:
        log("LLM returned empty text — skip inject (placeholder stays)")
        return 0

    # 4. Inject via the existing helper (do NOT rewrite signals.md ourselves).
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", prefix="signals_read_", delete=False, encoding="utf-8"
        ) as tf:
            tf.write(text)
            tmp_path = tf.name

        result = subprocess.run(
            [sys.executable, str(INJECT_HELPER), str(signals_path), run_date, tmp_path],
            timeout=INJECT_TIMEOUT_SECONDS,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            log(f"injected {len(text)} chars into TODAY_READ block (date {run_date})")
        else:
            log(
                f"inject helper exit {result.returncode} — skip "
                f"(stderr: {result.stderr.strip()})"
            )
    except subprocess.TimeoutExpired:
        log(f"inject helper exceeded {INJECT_TIMEOUT_SECONDS}s — skip")
    except Exception as e:
        log(f"inject failed ({e}) — skip")
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # absolute last-resort guard — ALWAYS exit 0
        print(f"{LOG} unexpected error ({e}) — exit 0", flush=True)
        sys.exit(0)
