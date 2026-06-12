import os
from collections import Counter
from datetime import date, datetime, timedelta, timezone

from pipelines.base import MarketPipeline
# AUTHORITATIVE KR price/index source = Naver (same-day T-0, NXT-inclusive).
# data.go.kr 주식시세/지수 is T+1 published — it never returns same-day data, so
# it silently fell back to T-1 and mislabeled it "fresh" (the /kr page showed
# YESTERDAY's prices). Naver gives the T-0 close, the SAME source K7 수급/외인%
# already use. The data.go.kr collectors remain importable as a secondary
# fallback but are NO LONGER wired in (see collect()).
from collectors.kr_naver_quote import (
    get_kr_stock_data_naver as get_kr_stock_data,
    get_kr_index_data_naver as get_kr_index_data,
)
# Secondary/legacy data.go.kr collectors (T+1) — kept for reference/fallback only.
# from collectors.kr_stocks import get_kr_stock_data as get_kr_stock_data_datago
# from collectors.kr_indices import get_kr_index_data as get_kr_index_data_datago
from collectors.kr_investor_flow import (
    fetch_market_investor_flows,
    fetch_stock_investor_flows,
    fetch_foreign_streaks,
)
from collectors.kr_breadth import fetch_market_breadth
from collectors.kr_index_history import fetch_index_history
from collectors.exchange_rates import get_exchange_rates
from analysis.llm_analyzer import analyze_kr_market, build_report_header
from utils.markdown_generator import save_report
from utils.vault_reader import read_watchlist_items
from utils.notifier import send_telegram_message
from utils.kr_date import business_days_between
from utils.kr_snapshot import build_kr_snapshot, write_kr_snapshot


# KR public-garden snapshot dump target (P1 of the 국장/KR page).
# Mirrors the US pipeline convention (derive from config.obsidian.base_dir =
# the MOUNTED vault path /notes/...), NOT os.path.expanduser("~"). The old `~`
# path resolved to the CONTAINER home (/root/...) under the docker cron, so the
# snapshot was written to throwaway container storage and NEVER reached the host
# vault — only the host-run seed script (where ~=/home/ubuntu) ever updated it.
# We now derive it from base_dir so the docker cron run repopulates kr.json.
def _kr_snapshot_path(config: dict) -> str:
    base_dir = (config.get("obsidian", {}) or {}).get(
        "base_dir", "/notes/raw/stockdog/daily-market"
    )
    # base_dir = .../raw/stockdog/daily-market → sibling .../raw/stockdog/kr/
    return os.path.join(os.path.dirname(base_dir), "kr", "kr_snapshot.json")


def _yyyymmdd_to_iso(s: str) -> str:
    """'20260513' → '2026-05-13'. 형식 이상 시 원본 반환."""
    if not s or len(s) != 8 or not s.isdigit():
        return s
    return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"


def _kr_data_as_of(data: dict) -> str | None:
    """KR 데이터에서 실제 거래일(YYYY-MM-DD) 추출.

    우선순위:
      1) KOSPI base_date (대표 지수)
      2) KOSDAQ base_date
      3) indices/stocks 전체에서 가장 빈도 높은 base_date (mode)
    """
    indices = data.get('kr_indices', {}) or {}
    for primary in ('KOSPI', 'KOSDAQ'):
        bd = (indices.get(primary) or {}).get('base_date')
        if bd:
            return _yyyymmdd_to_iso(bd)

    bdates = []
    for d in indices.values():
        if d.get('base_date'):
            bdates.append(d['base_date'])
    for d in (data.get('kr_stocks', {}) or {}).values():
        if d.get('base_date'):
            bdates.append(d['base_date'])
    if not bdates:
        return None
    most_common, _ = Counter(bdates).most_common(1)[0]
    return _yyyymmdd_to_iso(most_common)


class KRPipeline(MarketPipeline):
    REGION_LABEL = "KR"
    FAILED_REASON_HINT = "KOSPI/KOSDAQ"

    def _compute_status(self, data: dict) -> str:
        """
        KR 리포트 status 판단.
        - failed: KOSPI/KOSDAQ 둘 다 N/A (핵심 지수 부재)
        - partial: 일부 지수 N/A 또는 stock dict 비어있음
        - complete: 위 조건 모두 아님
        """
        indices = data.get('kr_indices', {}) or {}
        core_present = [t for t in ('KOSPI', 'KOSDAQ') if t in indices and indices[t].get('close')]
        if not core_present:
            return "failed"
        stocks = data.get('kr_stocks', {}) or {}
        # 일부 지수 누락 또는 stock 응답 비어있으면 partial
        if len(core_present) < 2 or not stocks:
            return "partial"
        return "complete"

    def collect(self) -> dict:
        vault = self.config.get('vault', {})

        kr_stock_items = read_watchlist_items(
            vault.get('watchlist_file', ''),
            types=('STOCK_KR', 'ETF_KR')
        )
        kr_index_items = read_watchlist_items(
            vault.get('watchlist_file', ''),
            types=('INDEX_KR',)
        )

        # K7 대형주 basket (curated/static, config-driven). Mapped to the
        # get_kr_stock_data item shape ({ticker, name, type}). Order preserved.
        # Best-effort: a missing/malformed config.kr.k7 degrades to []; the
        # snapshot then ships k7:[] and the emitter hides the block.
        k7_cfg = ((self.config.get('kr', {}) or {}).get('k7', []) or [])
        k7_items = [
            {'ticker': str(e.get('code')), 'name': e.get('name'),
             'type': 'STOCK_KR'}
            for e in k7_cfg
            if isinstance(e, dict) and e.get('code')
        ]
        k7_codes = [it['ticker'] for it in k7_items]

        if self.sample:
            kr_stock_items = kr_stock_items[:1]
            kr_index_items = kr_index_items[:1]
            k7_items = k7_items[:1]
            k7_codes = k7_codes[:1]
            print(f"[SAMPLE] kr_stocks={[i['ticker'] for i in kr_stock_items]}")

        # investor_flows is a best-effort 4th key (P2 of the 국장/KR page).
        # fetch_market_investor_flows() is fully tolerant (returns None, never
        # raises) so it can NEVER abort the pipeline.
        #
        # kr_k7_prices / kr_k7_flows (K7 대형주 트래커, P3-A) are ALSO best-effort:
        # get_kr_stock_data returns {} on failure (per-stock keyed) and
        # fetch_stock_investor_flows returns {} and never raises — neither can
        # abort the pipeline. They reuse the SAME free external sources already
        # in use (data.go.kr prices + Naver per-ticker trend).
        # Phase A/B (등락 종목수 breadth + 지수 30일 추세 + 종목 외국인 연속).
        # All three are best-effort, tolerant (return None/{}/never raise) so
        # none can abort the pipeline. They reuse the SAME free Naver hosts
        # already in use. The 투심 게이지(Phase B) is computed downstream in
        # utils/kr_snapshot.py from breadth + investor_flows (no extra fetch).
        return {
            'kr_stocks': get_kr_stock_data(kr_stock_items),
            'kr_indices': get_kr_index_data(kr_index_items),
            'exchange': get_exchange_rates(),
            'investor_flows': fetch_market_investor_flows(),
            'kr_k7_prices': get_kr_stock_data(k7_items) if k7_items else {},
            'kr_k7_flows': fetch_stock_investor_flows(k7_codes) if k7_codes else {},
            'kr_breadth': fetch_market_breadth(),
            'kr_index_history': fetch_index_history(),
            'kr_k7_foreign_streaks': fetch_foreign_streaks(k7_codes) if k7_codes else {},
        }

    def _compute_freshness(self, data_as_of: str | None) -> tuple[str | None, int | None]:
        """data_as_of(ISO) → ('fresh'|'stale'|None, business_days). 파싱 실패/None 시 (None, None).

        Naver is a same-day (T-0) source — a correct run stamps TODAY's trading
        date, so fresh ⇔ bdays == 0. The OLD rule (`bdays <= 1`) was a data.go.kr
        T+1 workaround that SWALLOWED the staleness (T-1 data labeled "fresh").
        With the Naver switch we tighten it: any non-today bizdate (weekend /
        holiday / stale fetch) is surfaced HONESTLY as 'stale' so the save()
        stale-callout ("N영업일 전 데이터") fires instead of pretending it is fresh.
        """
        if not data_as_of:
            return None, None
        try:
            data_date = datetime.strptime(data_as_of, "%Y-%m-%d").date()
            kst_today = (datetime.now(timezone.utc) + timedelta(hours=9)).date()
            bdays = business_days_between(kst_today, data_date)
            return ("fresh" if bdays == 0 else "stale"), bdays
        except ValueError:
            return None, None

    def analyze(self, data: dict) -> str:
        self._last_data = data
        data_as_of = _kr_data_as_of(data)
        freshness, _bdays = self._compute_freshness(data_as_of)
        kst_today = (datetime.now(timezone.utc) + timedelta(hours=9)).date()
        meta = {
            "report_date": kst_today.isoformat(),
            "data_as_of": data_as_of or "unknown",
            "data_freshness": freshness or "unknown",
        }
        self._last_meta = meta
        body = analyze_kr_market(data, meta=meta)
        # H1 + 메타라인은 LLM 환각 방지를 위해 코드에서 prepend (IMPR-033).
        # 단, 에러 콜아웃 응답은 그대로 두어 디버깅 흐름 유지.
        if body and not body.startswith("> [!error]") and not body.startswith("Error"):
            return build_report_header("KR", meta) + body
        return body

    def save(self, report: str) -> None:
        data = getattr(self, '_last_data', {}) or {}
        status = self._compute_status(data)
        data_as_of = _kr_data_as_of(data)

        # freshness 계산 + stale callout prepend (IMPR-031)
        data_freshness = None
        final_report = report
        if status in ("complete", "partial") and data_as_of:
            data_freshness, bdays = self._compute_freshness(data_as_of)
            if data_freshness == "stale":
                callout = (
                    f"> [!info] data_as_of {data_as_of} ({bdays}영업일 전 데이터 사용) — "
                    f"최신 거래일 데이터 미수신, fallback 사용.\n\n"
                )
                final_report = callout + report

        save_report(final_report, self.config, region="KR", status=status,
                    data_as_of=data_as_of, data_freshness=data_freshness)

        # KR public-garden snapshot dump (P1 of the 국장/KR page). Built from the
        # dicts we ALREADY collected — no new collector, no extra external API.
        # Tolerant: never aborts the pipeline (build_kr_snapshot/write are
        # exception-safe; we wrap once more for total safety).
        try:
            meta = getattr(self, "_last_meta", {}) or {}
            report_date = meta.get("report_date")
            report_slug = (f"/daily-reports/{report_date}-kr"
                           if report_date else None)
            snap = build_kr_snapshot(
                data,
                updated=report_date or (
                    datetime.now(timezone.utc) + timedelta(hours=9)
                ).date().isoformat(),
                data_date=data_as_of,
                report_slug=report_slug,
                hero=self._kr_hero_oneliner(data),
                story=None,   # full story lives in the linked report (P1)
            )
            write_kr_snapshot(_kr_snapshot_path(self.config), snap)
        except Exception as e:
            # Snapshot dump is best-effort — log and continue.
            print(f"[KRPipeline] kr_snapshot dump skipped: {e}")

    @staticmethod
    def _kr_hero_oneliner(data):
        """Deterministic KR hero one-liner from KOSPI/KOSDAQ change (no LLM).

        e.g. "코스피 +0.43%·코스닥 +4.76% 동반 상승". Null-safe → None when
        neither index is available (emitter then falls back to a default tagline).
        """
        idx = (data or {}).get("kr_indices", {}) or {}
        parts = []
        for key, label in (("KOSPI", "코스피"), ("KOSDAQ", "코스닥")):
            d = idx.get(key) or {}
            cp = d.get("change_pct")
            if cp is None:
                continue
            sign = "+" if cp > 0 else ""
            parts.append(f"{label} {sign}{cp:.2f}%")
        if not parts:
            return None
        return "·".join(parts)

    def notify(self, data: dict, report: str) -> None:
        if self._last_status == "failed":
            send_telegram_message("⚠️ KR 데이터 수집 실패 — vault에 placeholder 저장")
            return
        if report and not report.startswith("> [!error]") and not report.startswith("Error"):
            usd_krw = data.get('exchange', {}).get('USD_KRW', {})
            rate = usd_krw.get('rate', 'N/A')
            change = usd_krw.get('change_pct', 0)
            send_telegram_message(
                f"🇰🇷 *KR Report Ready*\n\nUSD/KRW: {rate} ({change:+.2f}%)\nDaily KR report saved to vault."
            )
        else:
            send_telegram_message("❌ KR Pipeline analysis failed. Check server logs.")
