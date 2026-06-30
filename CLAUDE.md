# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Behavioral Guidelines

These take priority over everything else.

### 1. Think Before Coding

Before implementing anything:
- State assumptions explicitly. If uncertain, ask — don't guess and run.
- If multiple interpretations exist, present them. Don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing and ask.

### 2. Simplicity First

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

### 3. Surgical Changes

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it — don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

Every changed line should trace directly to the user's request.

### 4. Goal-Driven Execution

For multi-step tasks, state a brief plan before starting:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
```

There is no test suite. Use `--sample` mode for fast verification during development:

```bash
python main.py --sample
```

`--sample`은 인플루언서 1명, 티커 1개만 실제 API로 호출해 전체 파이프라인을 검증한다. 실제 API를 쓰므로 환경 문제도 그대로 잡힌다. 로직·포맷·저장 경로 변경 시 이걸로 먼저 검증하고 full run은 최후에만 돌린다.

---

## Project Purpose

**Skyler-StockDog** is a Python backend market intelligence system that collects data from X/Twitter influencers, market indicators (F&G, VIX, 10Y Yield), and institutional holdings (13F), analyzes everything with LLMs (Gemini primary, Claude fallback), and generates Obsidian-formatted Markdown reports. A companion Telegram bot provides on-the-go access to the vault and live market data.

Deployed on Oracle Cloud Free Tier (Ubuntu 22.04). The output Markdown files are stored in a private Obsidian vault repo (`github.com/sungho-seo/skyler`), which the user manages via manual git pull/push.

## Repository Layout

```
stockdog-core/   # Daily pipeline: collect → analyze → report
telebot/         # Telegram bot (24/7, restart: always)
skyler/          # Obsidian vault clone (gitignored, user-managed)
AI_RULES.md      # Architectural decisions and project rules
BOT_COMMANDS.md  # Telegram bot command reference
```

## Running the Code

### Local / Manual Triggers

```bash
cd stockdog-core
python main.py              # Full daily pipeline
python fear_greed_job.py    # F&G index only (US market open)

cd ../telebot
python bot.py               # Start Telegram bot
```

### Docker

```bash
# Core pipeline (one-shot run)
cd stockdog-core
docker compose run --rm stockdog python main.py
docker compose run --rm stockdog python fear_greed_job.py

# Telegram bot (persistent)
cd telebot
docker compose up -d --build
```

### Cron Deployment (Ubuntu Server)

```bash
cd stockdog-core
./deploy.sh     # Idempotent — installs/updates both cron jobs
```

Two cron jobs are installed:
- **Main pipeline**: daily `0 2 * * *` UTC (KST 11:00) → `main.py` → `cron_stockdog.log`
- **F&G index**: weekdays `30 13 * * 1-5` UTC (09:30 ET) → `fear_greed_job.py` → `cron_fear_greed.log`

There is no test suite; validate changes by running the scripts manually.

## Architecture

### Core Pipeline (`stockdog-core/main.py`)

Four sequential phases:

1. **Config load** — `config.yaml` (influencers, tickers, output path) + `.env` (API keys via `python-dotenv`)
2. **Data collection** — three independent collectors run in sequence:
   - `collectors/twitter_scraper.py` — fetches 5 latest tweets per influencer via GetXAPI REST API; downloads tweet images locally
   - `collectors/market_indicators.py` — CNN internal API (F&G), Yahoo Finance Chart API (VIX, 10Y Yield)
   - `collectors/holdings_13f.py` — yfinance institutional holdings for configured tickers
3. **LLM analysis** — `analysis/llm_analyzer.py` sends JSON payload (indicators + 13F + tweets) to Gemini 3.0 Pro; falls back to Claude Anthropic if needed; returns Obsidian-formatted Markdown
4. **Output + notification** — `utils/markdown_generator.py` saves `Market_Report_YYYY-MM-DD.md`, `Raw_Twitter_YYYY-MM-DD.md`, and `media/` to `../skyler/daily-market/YYYY-MM-DD/`; `utils/notifier.py` sends Telegram summary

### Standalone F&G Job (`stockdog-core/fear_greed_job.py`)

Runs independently at US market open. Fetches F&G index, generates a gauge chart image via `utils/chart_generator.py` (matplotlib), and sends it to Telegram. Deliberately separated from `main.py` to allow real-time open-market updates.

### Telegram Bot (`telebot/bot.py`)

Runs with `restart: always` via Docker. Uses Gemini API for analysis. Reads/writes to the Obsidian vault via GitHub API (`PyGithub`). Key commands include `/fear` (live F&G gauge), `/price`, `/scrap` (webpage/YouTube summary), `/query` (vault RAG search), `/trade` (log to portfolio table), `/memo`, `/daily`. Full reference in `BOT_COMMANDS.md`.

## Configuration

**`stockdog-core/config.yaml`** — edit to add/remove influencers or portfolio tickers without code changes. The `obsidian.base_dir` path (`/notes/daily-market`) is the Docker volume mount target.

**`.env` (repo root)** — shared by both services. Required keys:
- `GEMINI_API_KEY` — primary LLM
- `ANTHROPIC_API_KEY` — fallback LLM
- `GETXAPI_KEY` — Twitter/X data (REST API; **do not revert to `twscrape`/Selenium** — blocked by Cloudflare on server)
- `BOT_TOKEN`, `CHAT_ID` — Telegram
- `GITHUB_PAT` — telebot vault access (PyGithub API); vault push now uses SSH
- `DATA_GO_KR_API_KEY` — 금융위 공공데이터포털 API (한국 주식/지수 시세)

**Docker volume** (core): `../skyler/daily-market` → `/notes/daily-market`. The `config.yaml` is also bind-mounted so it can be updated without rebuilding the image.

## Key Architectural Decisions (from AI_RULES.md)

- **GetXAPI only** for Twitter — stable REST API; headless scrapers are blocked on Oracle Cloud.
- **Gemini first, Claude fallback** — Gemini 3.0 Pro is preferred for cost and speed.
- **Config-driven influencers/tickers** — changes via `config.yaml`, not code edits.
- **Obsidian Markdown output** — use tables, `> [!info]` callouts, and tags; analysis over raw data dumps.
- **No UI** — output is purely Markdown files in Obsidian; Telegram bot is the mobile interface.
- **Core pipeline cron, not `restart: always`** — prevents redundant pipeline runs; only the bot needs persistent uptime.
