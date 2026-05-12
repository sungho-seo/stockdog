from pipelines.base import MarketPipeline
from collectors.twitter_scraper import get_influencer_tweets
from collectors.market_indicators import get_all_indicators
from collectors.us_market import get_us_market_data
from collectors.holdings_13f import get_all_13f_data
from collectors.economic_calendar import get_economic_calendar
from analysis.llm_analyzer import analyze_us_market
from utils.markdown_generator import save_report, save_raw_twitter_data
from utils.vault_reader import read_influencers, read_watchlist_items
from utils.notifier import send_telegram_message
import json


class USPipeline(MarketPipeline):

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

        twitter_data = get_influencer_tweets(influencers)
        save_raw_twitter_data(twitter_data, self.config)

        print("Fetching economic calendar (FRED)...")
        try:
            econ_calendar = get_economic_calendar(sample=self.sample)
        except Exception as e:
            print(f"[WARN] Economic calendar failed, skipping: {e}")
            econ_calendar = {"upcoming": [], "releasing_today": [], "error": str(e)}

        return {
            'twitter': twitter_data,
            'indicators': get_all_indicators(),
            'us_market': get_us_market_data(us_items),
            '13f': get_all_13f_data([i['ticker'] for i in stock_items]),
            'econ_calendar': econ_calendar,
        }

    def analyze(self, data: dict) -> str:
        return analyze_us_market(data)

    def save(self, report: str) -> None:
        save_report(report, self.config, region="US")

    def notify(self, data: dict, report: str) -> None:
        if report and not report.startswith("> [!error]") and not report.startswith("Error"):
            fgi = data.get('indicators', {}).get('fear_and_greed', {})
            score = int(round(fgi.get('score', 0))) if fgi.get('score') else 'N/A'
            rating = fgi.get('rating', 'N/A').upper()
            send_telegram_message(
                f"🇺🇸 *US Report Ready*\n\nFear & Greed: {score} ({rating})\nDaily US report saved to vault."
            )
        else:
            send_telegram_message("❌ US Pipeline analysis failed. Check server logs.")
