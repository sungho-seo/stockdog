from pipelines.base import MarketPipeline
from collectors.kr_stocks import get_kr_stock_data
from collectors.kr_indices import get_kr_index_data
from collectors.exchange_rates import get_exchange_rates
from analysis.llm_analyzer import analyze_kr_market
from utils.markdown_generator import save_report
from utils.vault_reader import read_watchlist_items
from utils.notifier import send_telegram_message


class KRPipeline(MarketPipeline):

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

        if self.sample:
            kr_stock_items = kr_stock_items[:1]
            kr_index_items = kr_index_items[:1]
            print(f"[SAMPLE] kr_stocks={[i['ticker'] for i in kr_stock_items]}")

        return {
            'kr_stocks': get_kr_stock_data(kr_stock_items),
            'kr_indices': get_kr_index_data(kr_index_items),
            'exchange': get_exchange_rates(),
        }

    def analyze(self, data: dict) -> str:
        self._last_data = data
        return analyze_kr_market(data)

    def save(self, report: str) -> None:
        status = self._compute_status(getattr(self, '_last_data', {}) or {})
        save_report(report, self.config, region="KR", status=status)

    def notify(self, data: dict, report: str) -> None:
        if report and not report.startswith("> [!error]") and not report.startswith("Error"):
            usd_krw = data.get('exchange', {}).get('USD_KRW', {})
            rate = usd_krw.get('rate', 'N/A')
            change = usd_krw.get('change_pct', 0)
            send_telegram_message(
                f"🇰🇷 *KR Report Ready*\n\nUSD/KRW: {rate} ({change:+.2f}%)\nDaily KR report saved to vault."
            )
        else:
            send_telegram_message("❌ KR Pipeline analysis failed. Check server logs.")
