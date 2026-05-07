# Skyler-StockDog Project Context & Rules

## 🎯 Project Goal
Build **stockdog**, a backend service that monitors the stock market by collecting data from social media and reliable web services, analyzes it using LLMs, and generates highly readable Markdown notes for Obsidian to assist in making effective investment decisions.

## 🏗️ Architecture Overview
- **Server Environment**: Oracle Cloud Free Tier (Ubuntu 22.04, 24GB RAM, Public IP)
- **Tech Stack**: Python (Scraping, LLM APIs, Markdown Generation)
- **Code Repository (Public)**: [https://github.com/sungho-seo/stockdog](https://github.com/sungho-seo/stockdog)
- **Vault Repository (Private)**: [https://github.com/sungho-seo/skyler](https://github.com/sungho-seo/skyler) (Obsidian Vault, user handles pull/push)
- **User Interface**: No separate web/app UI needed. The output is purely Markdown files viewed in Obsidian.
- **LLM Integration**: 
  - Claude / Gemini API for analyzing collected data, predicting trends, and formatting Markdown.
  - Telegram Bot (Gemini API) for reading/writing notes on the go.

## 🔍 Core Data Sources
1. **X (Twitter) Influencers**:
   - Read dynamically from configuration (`config.yaml`).
   - Starts with: Gary Black, wallstengine, gefetrades, kobeissiletter, jc_paretsx, stocksavvyshay, trendspider, deitaone, ryandetrick, barchart, bluekurtic, Cathie Wood, cryptorover.
   - **Important Architectural Decision**: We use **GetXAPI** (via standard HTTP requests) for Twitter data collection. DO NOT revert to using headless scrapers (like `twscrape` or `selenium`) as they are unstable and frequently blocked by Cloudflare in server environments.
2. **Market Indicators**:
   - Fear & Greed Index
   - VIX (Volatility Index)
   - US 10-Year Treasury Yield
   - Major Macro Events (CPI, PPI, FOMC)
3. **Institutional Holdings**:
   - 13F Filings tracked dynamically from configuration (`config.yaml`).
   - Starts with: TSLA, ANET.

## 🧠 AI Assistant Guidelines
1. **Markdown First**: All output must be beautifully formatted Markdown for optimal viewing in Obsidian. Use tables, Obsidian callouts (`> [!info]`), headers, and tags for high readability.
2. **Analysis over Raw Data**: Don't just dump raw tweets or data. Use LLMs to summarize the general sentiment, highlight conflicting opinions, and provide a unified market prediction/analysis.
3. **Dynamic Configuration**: Always read target influencers and stock tickers from a config file (e.g. `config.yaml`), so the user can change them via Git without modifying the code.
4. **Git Workflow**: The user will manually handle Git pull/push. We can assume writing to daily/weekly master markdown files is acceptable.
