from collections import Counter
from datetime import datetime, timedelta, timezone

from pipelines.base import MarketPipeline
from collectors.twitter_scraper import get_influencer_tweets
from collectors.market_indicators import get_all_indicators
from collectors.us_market import get_us_market_data
from collectors.holdings_13f import get_all_13f_data
from collectors.economic_calendar import get_economic_calendar
from analysis.llm_analyzer import analyze_us_market, build_report_header
from utils.markdown_generator import save_report, save_raw_twitter_data, _get_daily_dirs
from utils.metrics_history import save_indicators, generate_trend_chart, append_chart_to_report, stage_metrics_snapshot, save_macro, stage_macro_snapshot
from utils.vault_reader import read_influencers, read_watchlist_items
from utils.notifier import send_telegram_message
import os
import json


def _us_data_as_of(data: dict) -> str | None:
    """US 데이터에서 실제 거래일(YYYY-MM-DD) 추출.

    우선순위:
      1) SPY trade_date (대표 ETF)
      2) us_market 전체에서 가장 빈도 높은 trade_date (mode)
    """
    us_market = data.get('us_market', {}) or {}
    spy = us_market.get('SPY') or {}
    if spy.get('trade_date'):
        return spy['trade_date']

    dates = [d['trade_date'] for d in us_market.values() if d.get('trade_date')]
    if not dates:
        return None
    most_common, _ = Counter(dates).most_common(1)[0]
    return most_common


class USPipeline(MarketPipeline):
    REGION_LABEL = "US"
    FAILED_REASON_HINT = "indicators/us_market"

    def _compute_status(self, data: dict) -> str:
        """
        US 리포트 status 판단.
        - failed: indicators 또는 us_market 둘 다 비어있음
        - partial: us_market 비어있거나 indicators 빈약
        - complete: 그 외 정상 (13F·influencer quiet 등은 정상으로 간주)
        """
        indicators = data.get('indicators', {}) or {}
        us_market = data.get('us_market', {}) or {}
        if not indicators and not us_market:
            return "failed"
        if not us_market or not indicators:
            return "partial"
        return "complete"

    def collect(self) -> dict:
        vault = self.config.get('vault', {})

        influencers = read_influencers(vault.get('influencers_file', ''))
        us_items = read_watchlist_items(
            vault.get('watchlist_file', ''),
            types=('STOCK', 'ETF', 'INDEX_US')
        )
        stock_items = [i for i in us_items if i['type'] == 'STOCK']

        if self.sample:
            influencers = influencers[:1]
            us_items = us_items[:1]
            stock_items = stock_items[:1]
            print(f"[SAMPLE] influencer={influencers}, us_items={[i['ticker'] for i in us_items]}")

        twitter_cfg = self.config.get('twitter', {})
        if twitter_cfg.get('enabled', True):  # default True for back-compat
            twitter_data = get_influencer_tweets(influencers)
            save_raw_twitter_data(twitter_data, self.config)
        else:
            print("[INFO] twitter.enabled=false — skipping influencer scraping")
            twitter_data = {}

        f13_cfg = self.config.get('f13', {})
        if f13_cfg.get('enabled', True):  # default True for back-compat
            f13_data = get_all_13f_data([i['ticker'] for i in stock_items])
        else:
            print("[INFO] f13.enabled=false — skipping 13F collection")
            f13_data = {}

        print("Fetching economic calendar (FRED)...")
        try:
            econ_calendar = get_economic_calendar(sample=self.sample)
        except Exception as e:
            print(f"[WARN] Economic calendar failed, skipping: {e}")
            econ_calendar = {"upcoming": [], "releasing_today": [], "error": str(e)}

        data = {
            'twitter': twitter_data,
            'indicators': get_all_indicators(),
            'us_market': get_us_market_data(us_items),
            '13f': f13_data,
            'econ_calendar': econ_calendar,
        }
        self._indicators = data['indicators']
        return data

    def _compute_freshness(self, data_as_of: str | None) -> str | None:
        """US freshness: 미국은 NYSE 휴장일 캘린더 없이 calendar-day delta로 보수적 계산.
        delta ≤ 3 days → fresh (주말 + 월요일 휴장 커버), ≥ 4 → stale.
        파싱 실패/None 시 None 반환 (frontmatter 라인 생략)."""
        if not data_as_of:
            return None
        try:
            data_date = datetime.strptime(data_as_of, "%Y-%m-%d").date()
            kst_today = (datetime.now(timezone.utc) + timedelta(hours=9)).date()
            delta = abs((kst_today - data_date).days)
            return "fresh" if delta <= 3 else "stale"
        except ValueError:
            return None

    def analyze(self, data: dict) -> str:
        self._last_data = data
        data_as_of = _us_data_as_of(data)
        freshness = self._compute_freshness(data_as_of)
        kst_today = (datetime.now(timezone.utc) + timedelta(hours=9)).date()
        meta = {
            "report_date": kst_today.isoformat(),
            "data_as_of": data_as_of or "unknown",
            "data_freshness": freshness or "unknown",
        }
        self._last_meta = meta
        body = analyze_us_market(data, meta=meta)
        # H1 + 메타라인은 LLM 환각 방지를 위해 코드에서 prepend (IMPR-033).
        if body and not body.startswith("> [!error]") and not body.startswith("Error"):
            return build_report_header("US", meta) + body
        return body

    def _append_m7_block(self, report: str) -> str:
        """IMPR-044 P1-S5: M7 트래커 H2 블록을 LLM 본문 뒤에 append.

        m7.enabled=false 또는 m7.yaml 부재 시 noop (원본 report 반환).
        Trend chart append와 동일 패턴 — 실패해도 본 리포트 흐름 막지 않음.
        """
        try:
            from utils.m7_config import load_m7_config
            from analysis.m7_summary import summarize_m7
            m7_cfg = load_m7_config()
            if not m7_cfg.get("enabled", True):
                return report
            date_str = (datetime.now(timezone.utc) + timedelta(hours=9)).date().isoformat()
            block = summarize_m7(date_str, m7_cfg)
            if not block:
                return report
            # 본문 뒤 append (회귀 안전 — Trend chart는 file-write 후 별도 append라 충돌 없음)
            return report.rstrip() + "\n\n" + block
        except FileNotFoundError:
            print("[INFO] m7.yaml not found — skipping M7 block.")
            return report
        except Exception as e:
            print(f"[WARN] M7 block append failed, ignoring: {e}")
            return report

    def save(self, report: str) -> None:
        data = getattr(self, '_last_data', {}) or {}
        status = self._compute_status(data)
        data_as_of = _us_data_as_of(data)
        data_freshness = self._compute_freshness(data_as_of) if status in ("complete", "partial") else None
        report = self._append_m7_block(report)
        report_path = save_report(report, self.config, region="US", status=status,
                                  data_as_of=data_as_of, data_freshness=data_freshness)
        try:
            _, media_dir, date_str = _get_daily_dirs(self.config)
            save_indicators(self._indicators)
            # Stage a vault-readable snapshot for the host-side M7 renderer
            # (which cannot read the root-owned, gitignored metrics_history.db).
            # base_dir is /notes/raw/stockdog/daily-market → derive m7 dir.
            base_dir = self.config.get('obsidian', {}).get('base_dir', '/notes/raw/stockdog/daily-market')
            m7_dir = os.path.join(os.path.dirname(base_dir), 'm7')
            stage_metrics_snapshot(os.path.join(m7_dir, 'metrics_snapshot.json'))
            # IMPR-062: capture watchlist price/volume from the IN-HAND us_market
            # dict (no re-fetch) + stage a snapshot for the host-side renderer.
            # Wrapped so a watchlist hiccup never breaks the US report.
            try:
                from utils.watchlist_store import save_watchlist_day, stage_watchlist_snapshot
                wl_dir = os.path.join(os.path.dirname(base_dir), 'watchlist')
                save_watchlist_day(wl_dir, date_str, data.get('us_market', {}) or {})
                stage_watchlist_snapshot(wl_dir)
            except Exception as we:
                print(f"[WARN] Watchlist step failed, ignoring: {we}")
            # IMPR-061: persist + stage macro (rates/inflation/policy/dollar).
            # Wrapped separately so a FRED/exchange hiccup never breaks the US run.
            try:
                from collectors.fred_macro import get_macro_latest
                macro_latest = get_macro_latest()
                # Reuse the run's USD/KRW if it was collected; else fetch fresh.
                usd_krw = None
                fx = (data.get('exchange_rates') or {}).get('USD_KRW') or {}
                if fx.get('rate') is not None:
                    usd_krw = fx['rate']
                else:
                    try:
                        from collectors.exchange_rates import get_exchange_rates
                        usd_krw = (get_exchange_rates().get('USD_KRW') or {}).get('rate')
                    except Exception as fxe:
                        print(f"[WARN] macro USD/KRW fetch failed, continuing: {fxe}")
                save_macro(macro_latest, usd_krw)
                macro_dir = os.path.join(os.path.dirname(base_dir), 'macro')
                stage_macro_snapshot(os.path.join(macro_dir, 'macro_snapshot.json'))
            except Exception as me:
                print(f"[WARN] Macro step failed, ignoring: {me}")
            chart_path = generate_trend_chart(media_dir, date_str)
            if chart_path and report_path:
                append_chart_to_report(report_path, os.path.basename(chart_path))
                print(f"📈 Trend chart appended to report.")
        except Exception as e:
            print(f"[WARN] Trend chart step failed, ignoring: {e}")

    def notify(self, data: dict, report: str) -> None:
        if self._last_status == "failed":
            send_telegram_message("⚠️ US 데이터 수집 실패 — vault에 placeholder 저장")
            return
        if report and not report.startswith("> [!error]") and not report.startswith("Error"):
            fgi = data.get('indicators', {}).get('fear_and_greed', {})
            score = int(round(fgi.get('score', 0))) if fgi.get('score') else 'N/A'
            rating = fgi.get('rating', 'N/A').upper()
            data_as_of = _us_data_as_of(data)
            msg = f"🇺🇸 *US Report Ready*\n\nFear & Greed: {score} ({rating})"
            if data_as_of:
                msg += f"\n🔗 https://blog.seosungho.com/daily-reports/{data_as_of}-us"
            else:
                msg += "\nDaily US report saved to vault."
            send_telegram_message(msg)
        else:
            send_telegram_message("❌ US Pipeline analysis failed. Check server logs.")
