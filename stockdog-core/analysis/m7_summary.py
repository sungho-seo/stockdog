"""
M7 트래커 — daily report에 prepend/append할 H2 마크다운 블록 생성.

입력: 모든 ticker의 per-symbol latest JSON (raw/stockdog/m7/{ticker}/{category}_latest.json)
출력: 마크다운 H2 블록 (str). 시그널 없으면 단일 콜아웃.

시그널 계산:
  Insider: tx.value_usd >= effective.insider.min_value_usd (per-ticker override 후)
  Short delta: |today.short_ratio - yesterday.short_ratio| >= delta_pp
  Short MA: history 길이 >= ma_min_history_days 일 때만 →
            |today - MA30| / MA30 * 100 >= ma_relative_pct
"""
import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def _format_usd_short(value: float) -> str:
    """500000 → "$500K", 2300000 → "$2.3M". USD만, 양수만."""
    if value is None:
        return "N/A"
    av = float(value)
    if av >= 1_000_000_000:
        return f"${av / 1_000_000_000:.1f}B"
    if av >= 1_000_000:
        return f"${av / 1_000_000:.1f}M"
    if av >= 1_000:
        return f"${av / 1_000:.0f}K"
    return f"${av:.0f}"


def _compute_short_signals(
    today_rec: Dict[str, Any],
    history: List[Dict[str, Any]],
    thresholds: Dict[str, Any],
) -> Dict[str, Any]:
    """공매도 단일 ticker 시그널 계산.

    Returns:
      {
        "today_ratio": float | None,
        "yesterday_ratio": float | None,
        "delta_pp": float | None,
        "delta_breach": bool,
        "ma_value": float | None,
        "ma_relative_pct": float | None,    # (today - MA) / MA * 100
        "ma_breach": bool,
        "ma_history_count": int,
      }
    """
    out: Dict[str, Any] = {
        "today_ratio": None,
        "yesterday_ratio": None,
        "delta_pp": None,
        "delta_breach": False,
        "ma_value": None,
        "ma_relative_pct": None,
        "ma_breach": False,
        "ma_history_count": 0,
    }

    today_ratio = today_rec.get("short_ratio")
    today_date = today_rec.get("data_as_of") or today_rec.get("date")
    if today_ratio is None:
        return out
    out["today_ratio"] = today_ratio

    delta_pp_threshold = float(thresholds.get("delta_pp", 5.0))
    ma_window = int(thresholds.get("ma_window_days", 30))
    ma_relative_pct_th = float(thresholds.get("ma_relative_pct", 20.0))
    ma_min_hist = int(thresholds.get("ma_min_history_days", 10))

    # history is sorted desc by date. 첫 항목이 today일 수도 있으므로 today_date와 다른 직전 행을 yesterday로.
    yesterday_rec = None
    for r in history:
        rd = r.get("data_as_of") or r.get("date")
        if rd and rd != today_date:
            yesterday_rec = r
            break
    if yesterday_rec and yesterday_rec.get("short_ratio") is not None:
        y_ratio = yesterday_rec["short_ratio"]
        out["yesterday_ratio"] = y_ratio
        delta = today_ratio - y_ratio
        out["delta_pp"] = round(delta, 4)
        out["delta_breach"] = abs(delta) >= delta_pp_threshold

    # MA 계산 — today 제외한 직전 ma_window 일 (today 자신 포함은 self-referential 회피)
    prior = []
    for r in history:
        rd = r.get("data_as_of") or r.get("date")
        if rd == today_date:
            continue
        ratio = r.get("short_ratio")
        if ratio is None:
            continue
        prior.append(ratio)
        if len(prior) >= ma_window:
            break
    out["ma_history_count"] = len(prior)
    if len(prior) >= ma_min_hist and len(prior) > 0:
        ma_val = sum(prior) / len(prior)
        out["ma_value"] = round(ma_val, 4)
        if ma_val > 0:
            rel = (today_ratio - ma_val) / ma_val * 100.0
            out["ma_relative_pct"] = round(rel, 2)
            out["ma_breach"] = abs(rel) >= ma_relative_pct_th
    return out


def summarize_m7(date_str: str, cfg: Dict[str, Any]) -> str:
    """M7 트래커 H2 블록 생성.

    cfg: load_m7_config() 결과.
    date_str: 리포트 날짜 (보통 KST today). 출력 헤더에 직접 노출 안 함.

    cfg.enabled == False면 빈 문자열 반환 (호출부에서 noop 처리).
    """
    if not cfg.get("enabled", True):
        return ""

    from utils.m7_config import get_tickers, merge_thresholds
    from utils.m7_store import read_per_symbol_latest, read_per_symbol_history

    storage = cfg.get("storage", {}) or {}
    raw_base_dir = storage.get("raw_base_dir", "/notes/raw/stockdog/m7")

    insider_rows: List[Dict[str, Any]] = []
    short_rows: List[Dict[str, Any]] = []
    short_stale_warning = False
    insider_collected_any = False
    short_collected_any = False

    for t in get_tickers(cfg):
        symbol = t["symbol"]
        eff = merge_thresholds(symbol, cfg)
        insider_th = eff.get("insider", {}) or {}
        short_th = eff.get("short", {}) or {}
        min_value_usd = float(insider_th.get("min_value_usd", 500000))

        # Insider latest
        insider_latest = read_per_symbol_latest(raw_base_dir, "insider", symbol)
        if insider_latest is not None:
            insider_collected_any = True
            txs = insider_latest.get("transactions", []) or []
            for tx in txs:
                value_usd = tx.get("value_usd") or 0.0
                if value_usd >= min_value_usd:
                    insider_rows.append({
                        "ticker": symbol,
                        "insider_name": tx.get("insider_name") or "Unknown",
                        "role": tx.get("role") or "",
                        "action": tx.get("action") or "",
                        "value_usd": value_usd,
                        "date": tx.get("date") or "",
                    })

        # Short latest + history
        short_latest = read_per_symbol_latest(raw_base_dir, "short", symbol)
        short_history = read_per_symbol_history(raw_base_dir, "short", symbol)
        if short_latest is not None:
            short_collected_any = True
            if short_latest.get("freshness") == "stale":
                short_stale_warning = True
            sig = _compute_short_signals(short_latest, short_history, short_th)
            if sig["delta_breach"] or sig["ma_breach"]:
                short_rows.append({
                    "ticker": symbol,
                    "today_ratio": sig["today_ratio"],
                    "delta_pp": sig["delta_pp"],
                    "delta_breach": sig["delta_breach"],
                    "ma_relative_pct": sig["ma_relative_pct"],
                    "ma_breach": sig["ma_breach"],
                    "ma_history_count": sig["ma_history_count"],
                    "data_as_of": short_latest.get("data_as_of"),
                    "freshness": short_latest.get("freshness"),
                })

    parts: List[str] = []
    parts.append("## M7 트래커")
    parts.append("")

    # 시그널 없음 케이스
    if not insider_rows and not short_rows:
        if not insider_collected_any and not short_collected_any:
            parts.append("> [!warning] M7 트래커 — 데이터 수집 실패 (insider/short 모두 latest 미존재).")
        elif short_stale_warning:
            parts.append("> [!warning] M7 공매도 데이터 stale (T-N, FINRA T-1 미공개).")
            parts.append("")
            parts.append("> [!info] M7 트래커 — 오늘 임계값 돌파 종목 없음.")
        else:
            parts.append("> [!info] M7 트래커 — 오늘 임계값 돌파 종목 없음.")
        parts.append("")
        return "\n".join(parts) + "\n"

    # Insider 표
    if insider_rows:
        parts.append("### Insider 매매 (Form 4)")
        parts.append("")
        parts.append("| 종목 | 인사이더 | 직책 | 행위 | 금액 | 날짜 |")
        parts.append("|---|---|---|---|---|---|")
        # 금액 큰 순 정렬
        for r in sorted(insider_rows, key=lambda x: x["value_usd"], reverse=True):
            parts.append(
                f"| {r['ticker']} | {r['insider_name']} | {r['role']} | {r['action']} | "
                f"{_format_usd_short(r['value_usd'])} | {r['date']} |"
            )
        parts.append("")

    # Short 표
    if short_rows:
        header_suffix = " (T-1 기준)" if not short_stale_warning else " (T-N 기준, stale)"
        parts.append(f"### 공매도 동향 (FINRA RegSHO,{header_suffix})")
        parts.append("")
        parts.append("| 종목 | 단기 비율 | 전일 대비 | 30d MA 대비 |")
        parts.append("|---|---|---|---|")
        for r in short_rows:
            ratio_s = f"{r['today_ratio']:.1f}%" if r['today_ratio'] is not None else "N/A"
            delta_s = "—"
            if r['delta_pp'] is not None:
                arrow = "⚠" if r['delta_breach'] else ""
                sign = "+" if r['delta_pp'] >= 0 else ""
                delta_s = f"{sign}{r['delta_pp']:.1f}%p {arrow}".strip()
            ma_s = "—"
            if r['ma_relative_pct'] is not None:
                arrow = "⚠" if r['ma_breach'] else ""
                sign = "+" if r['ma_relative_pct'] >= 0 else ""
                ma_s = f"{sign}{r['ma_relative_pct']:.1f}% {arrow}".strip()
            elif r['ma_history_count'] > 0:
                ma_s = f"N/A (history={r['ma_history_count']}d)"
            parts.append(f"| {r['ticker']} | {ratio_s} | {delta_s} | {ma_s} |")
        parts.append("")

    # 요약 콜아웃
    summary_bits: List[str] = []
    if insider_rows:
        biggest = max(insider_rows, key=lambda x: x["value_usd"])
        summary_bits.append(
            f"{biggest['ticker']} {biggest['action']} {_format_usd_short(biggest['value_usd'])}"
        )
    if short_rows:
        biggest_short = max(
            short_rows,
            key=lambda x: abs(x["delta_pp"] or 0) + abs((x.get("ma_relative_pct") or 0)),
        )
        delta_pp = biggest_short.get("delta_pp")
        if delta_pp is not None:
            sign = "+" if delta_pp >= 0 else ""
            summary_bits.append(f"{biggest_short['ticker']} 공매도 {sign}{delta_pp:.1f}%p")
        else:
            summary_bits.append(f"{biggest_short['ticker']} 공매도 시그널")

    if summary_bits:
        parts.append(f"> [!summary] 오늘의 M7 시그널: {' + '.join(summary_bits)}.")
        parts.append("")

    if short_stale_warning:
        parts.append("> [!warning] M7 공매도 데이터 일부 stale (FINRA T-1 미공개).")
        parts.append("")

    return "\n".join(parts) + "\n"
