import os
import yaml
import logging
import argparse
from dotenv import load_dotenv

# Import Phase 2 Collectors
from collectors.market_indicators import get_all_indicators
from collectors.holdings_13f import get_all_13f_data
from collectors.twitter_scraper import get_influencer_tweets

# Import Phase 3 Analyzers and Generators
from analysis.llm_analyzer import analyze_market_data
from utils.markdown_generator import save_to_obsidian, save_raw_twitter_data
from utils.notifier import send_telegram_message
from utils.vault_reader import read_influencers, read_watchlist_tickers

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

def load_config(config_path="config.yaml"):
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample", action="store_true", help="Sample mode: 1 influencer + 1 ticker only")
    args = parser.parse_args()

    print("🐾 Starting StockDog Core Pipeline...")

    # Load environment variables (API keys)
    load_dotenv()

    # Load configuration
    config = load_config()
    vault = config.get('vault', {})

    influencers = read_influencers(vault.get('influencers_file', ''))
    portfolio = read_watchlist_tickers(vault.get('watchlist_file', ''), types=("STOCK",))

    if not influencers or not portfolio:
        print("❌ Failed to load influencers or portfolio from vault. Check vault files.")
        send_telegram_message("❌ StockDog 시작 실패: vault 파일을 읽을 수 없습니다.")
        return

    if args.sample:
        influencers = influencers[:1]
        portfolio = portfolio[:1]
        print(f"[SAMPLE MODE] 1 influencer ({influencers[0]}), 1 ticker ({portfolio[0]})")
    else:
        print(f"Loaded from vault: {len(influencers)} influencers, {len(portfolio)} tickers.")
    
    # --- Phase 2: Data Collection ---
    print("\n--- Phase 2: Data Collection ---")
    indicators_data = get_all_indicators()
    f13_data = get_all_13f_data(portfolio)
    twitter_data = get_influencer_tweets(influencers)
    
    # Save raw twitter data immediately
    save_raw_twitter_data(twitter_data, config)
    
    # --- Phase 3: LLM Analysis & Output ---
    print("\n--- Phase 3: LLM Analysis & Output ---")
    markdown_content = analyze_market_data(twitter_data, indicators_data, f13_data)
    
    if markdown_content.startswith("Error") or markdown_content.startswith("> [!error]"):
        print("Analysis failed. See logs.")
        send_telegram_message("❌ StockDog Analysis Failed. Check server logs.")
    else:
        save_to_obsidian(markdown_content, config)
        
        # --- Phase 4: Notification ---
        print("\n--- Phase 4: Notification ---")
        fgi = indicators_data.get("fear_and_greed", {})
        summary_msg = f"✅ *StockDog Report Ready*\n\nFear & Greed: {int(round(fgi.get('score', 0)))} ({fgi.get('rating', 'N/A').upper()})\n\nDaily report has been synced to Obsidian."
        send_telegram_message(summary_msg)
    
    print("🐾 StockDog run complete.")

if __name__ == "__main__":
    main()
