# StockDog Session Summary (2026-05-10) 🐾

## 🧠 개발 가이드라인 정립

### 1. CLAUDE.md 작성
- 프로젝트 아키텍처, 실행 명령어, 설정 구조를 문서화한 `CLAUDE.md` 신규 작성.
- **Karpathy 4원칙** 최우선 섹션으로 추가:
  - **Think Before Coding**: 가정을 명시하고, 불명확하면 묻기
  - **Simplicity First**: 요청된 것만 구현, 불필요한 추상화 금지
  - **Surgical Changes**: 요청과 무관한 코드는 건드리지 않기
  - **Goal-Driven Execution**: 성공 기준을 먼저 정의하고 검증

## 🛠️ 파이프라인 개선

### 2. `--sample` 개발 검증 모드 추가
- **배경**: 코드 변경 시 검증하려면 12명 인플루언서 전체를 호출해야 하는 구조적 문제.
- **해결**: `python main.py --sample` 플래그 추가. 인플루언서 1명 + 티커 1개만 실제 API로 호출해 전체 파이프라인을 빠르게 검증.
- **효과**: 로직/포맷/저장 경로 변경 시 full run 없이 빠른 루프 가능.

### 3. Twitter 스크랩 기준 개선
- **문제**: 시간 필터 없이 최신 5개 트윗만 가져와, 인플루언서가 오늘 안 올리면 며칠 전 글이 섞임.
- **해결**: GetXAPI 쿼리에 `since:YYYY-MM-DD` 필터 추가 (실행 시점 기준 24시간 전 날짜 동적 계산).

### 4. 파이프라인 실행 시각 변경
- **문제**: 기존 17:00 UTC(EDT 기준 오후 1시) 실행 — 미국 정규장 한복판이라 오후 장 트윗을 전혀 못 잡음.
- **해결**: `deploy.sh` cron을 `0 17 * * *` → `0 2 * * *` (UTC 02:00 = KST 11:00)으로 변경.
- **효과**: EDT 기준 오후 10시 실행 → 애프터마켓(20:00 ET) 마감 후 하루치 전체 포착.

### 5. 13F 데이터 30일 캐시 도입
- **문제**: 13F 기관 보유 데이터는 분기별로 갱신되는데 매일 yfinance를 호출하는 낭비 구조.
- **해결**: `cache/13f_cache.json`에 결과 저장, 30일 이내면 캐시 반환, 만료 시 재호출.
- **인프라**: `docker-compose.yml`에 `./cache:/app/cache` 볼륨 마운트 추가로 컨테이너 재시작 후에도 캐시 유지.
- **강제 갱신**: `cache/13f_cache.json` 삭제 시 다음 실행에서 재호출.

---

## 📈 Next Steps (Planned Improvements)
- **Prompt Engineering**: Gemini 프롬프트에 `📊 Earnings & Upgrades` 섹션 추가.
- **Engagement Filtering**: 좋아요/리트윗 수 기준으로 트윗 우선순위 필터링.
- **Attribution Accuracy**: LLM 기술적 분석 귀속 정확도 개선.

---
*All changes have been committed and pushed to the main branch.*  
**서버 반영:** `git pull origin main && ./deploy.sh && docker compose up -d --build`

---

# StockDog Session Summary (2026-05-08) 🐾

This session focused on transforming StockDog into a robust, professional-grade market monitoring system.

## 🚀 Major Features & Migrations

### 1. Twitter Scraper Migration (twscrape → GetXAPI)
- **Problem**: Legacy `twscrape` was unstable and blocked by Cloudflare on Oracle Cloud.
- **Solution**: Migrated to **GetXAPI**, a stable REST API. This fixed the `403 Forbidden` errors completely.
- **Upgrade**: Added **Media Extraction**. The system now identifies image/GIF URLs in tweets, downloads them locally, and embeds them in the report so you can see charts directly in Obsidian.

### 2. 13F Institutional Data Fix
- **Problem**: Direct HTML scraping of Yahoo Finance was failing with a `404 Not Found`.
- **Solution**: Integrated the **`yfinance`** library. This handles session management/crumbs automatically and reliably fetches top institutional holders for $TSLA and $ANET.

### 3. Gemini 3.0 Pro Implementation
- **Problem**: The previous `gemini-1.5-pro-latest` endpoint was deprecated.
- **Solution**: Upgraded to **Gemini 3.0 Pro** (`gemini-3-pro-preview`).
- **Fix**: Resolved authentication issues by explicitly passing the API key to the LLM constructor.

### 5. TeleBot Integration & Migration
- **Migration**: Moved the separate `Skyler-TeleBot` project into this repository for centralized management.
- **Dockerization**: Created a dedicated `Dockerfile` and `docker-compose.yml` for the bot with `restart: always` policy.
- **New Feature**: Added the `/fear` command. It fetches live data and generates a professional dark-theme gauge image using `matplotlib` to send via Telegram.

### 6. Fear & Greed Gauge Generator
- Implemented a custom gauge chart generator (`chart_generator.py`) to visualize market sentiment without relying on external image URLs.

## 🛠️ Technical Debt Cleared
- Removed legacy `accounts.db`.
- Cleaned up influencer list (`gefetrades` removed, `CathieWood` fixed).
- Verified all market indicators (F&G, VIX, 10Y Yield) are working with new code.

## 📈 Next Steps (Planned Improvements)
- **Prompt Engineering**: Add a dedicated `📊 Earnings & Upgrades` section to the Gemini prompt to prevent summarization loss of specific stock catalysts (e.g., `$ORCL`, `$HIMX`).
- **Engagement Filtering**: Implement a priority filter so the LLM focuses on tweets with the highest likes/retweets.
- **Attribution Accuracy**: Refine LLM instructions to ensure 100% correct attribution for technical analysis.

---
*All changes have been committed and pushed to the main branch.*  
**To activate on server:** `git pull origin main && docker compose up -d --build`
