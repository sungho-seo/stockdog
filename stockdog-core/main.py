import os
import yaml
import logging
from dotenv import load_dotenv

# Import Phase 2 Collectors
from collectors.market_indicators import get_all_indicators
from collectors.holdings_13f import get_all_13f_data
from collectors.twitter_scraper import get_influencer_tweets

# Import Phase 3 Analyzers and Generators
from analysis.llm_analyzer import analyze_market_data
from utils.markdown_generator import save_to_obsidian

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

def load_config(config_path="config.yaml"):
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

def main():
    print("🐾 Starting StockDog Core Pipeline...")
    
    # Load environment variables (API keys)
    load_dotenv()
    
    # Load configuration
    config = load_config()
    influencers = config.get('twitter_influencers', [])
    portfolio = config.get('portfolio_13f', [])
    print(f"Loaded config: Monitoring {len(influencers)} influencers and {len(portfolio)} tickers.")
    
    # --- Phase 2: Data Collection ---
    print("\n--- Phase 2: Data Collection ---")
    indicators_data = get_all_indicators()
    f13_data = get_all_13f_data(portfolio)
    twitter_data = get_influencer_tweets(influencers)
    
    # --- Phase 3: LLM Analysis & Output ---
    print("\n--- Phase 3: LLM Analysis & Output ---")
    markdown_content = analyze_market_data(twitter_data, indicators_data, f13_data)
    
    if markdown_content.startswith("Error") or markdown_content.startswith("> [!error]"):
        print("Analysis failed. See logs.")
    else:
        save_to_obsidian(markdown_content, config)
    
    print("🐾 StockDog run complete.")

if __name__ == "__main__":
    main()
