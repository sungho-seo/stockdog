from pipelines.base import MarketPipeline
from collectors.kr_stocks import get_kr_stock_data
from collectors.kr_indices import get_kr_index_data
from collectors.exchange_rates import get_exchange_rates
from analysis.llm_analyzer import analyze_kr_market
from utils.markdown_generator import save_report
from utils.vault_reader import read_watchlist_items
from utils.notifier import send_telegram_message


class KRPipeline(MarketPipeline):

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
        return analyze_kr_market(data)

    def save(self, report: str) -> None:
        save_report(report, self.config, region="KR")

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
