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

### 6. skyler 볼트 자동 GitHub Push
- **배경**: 매일 생성된 리포트를 수동으로 skyler 레포에 옮기는 비효율 제거.
- **해결**: `sync_vault.sh` 신규 작성. 파이프라인 완료 후 자동 실행.
  - 로컬 `daily-market/YYYY-MM-DD/*.md`를 GitHub 경로 `raw/stockdog/daily-market/YYYY-MM-DD/`로 복사 후 push.
  - 이미지(`media/`) 제외.
  - push 성공/실패 여부 Telegram 알림.
  - `deploy.sh` cron에 `&& bash sync_vault.sh` 체이닝.
- **브랜치**: skyler 레포 `master` 브랜치 기준.
- **서버 로컬 구조는 기존 유지** (`daily-market/YYYY-MM-DD/`).

### 7. .env 통합
- **배경**: `stockdog-core/.env`와 `telebot/.env`가 대부분 동일한 키를 중복 관리.
- **해결**: 두 파일을 레포 루트 `.env` 하나로 통합.
  - `stockdog-core/docker-compose.yml`, `telebot/docker-compose.yml` 모두 `../.env` 참조로 변경.
  - `sync_vault.sh`도 `$DIR/../.env` 참조.
  - `GETXAPI_KEY`는 stockdog-core 전용, 나머지는 공유.

### 8. Shell 스크립트 실행 권한 git 반영
- `deploy.sh`, `sync_vault.sh` 모두 `git update-index --chmod=+x`로 실행 권한 커밋.
- 서버 pull 후 별도 `chmod +x` 불필요.

### 10. Obsidian 볼트를 설정 소스로 전환 (vault-driven config)
- **배경**: 인플루언서/포트폴리오 변경 시 매번 `config.yaml`을 코드로 수정하거나 직접 알려줘야 하는 비효율.
- **해결**: Obsidian의 `_system/influencers.md`, `_system/watchlist.md`를 단일 소스로 사용.
  - `utils/vault_reader.py` 신규 작성.
    - `influencers.md`: 테이블에서 `활성 = ✅`인 핸들만 파싱.
    - `watchlist.md`: `TICKER|Name|TYPE` 형식에서 `TYPE=STOCK`인 미국 종목만 파싱.
  - `config.yaml`: 하드코딩 리스트 제거, vault 파일 경로만 참조.
  - `docker-compose.yml`: `../skyler/daily-market:/notes/daily-market` → `../skyler:/notes` (볼트 전체 마운트).
  - `main.py`: `vault_reader`로 인플루언서·포트폴리오 로드.
  - `deploy.sh`: cron 시작 시 `git -C ../skyler pull` 선행 → Obsidian 수정이 다음 날 자동 반영.
- **최종 워크플로우**: Obsidian에서 파일 수정 → 자동 GitHub push → 서버 cron이 pull 후 반영. 코드 수정 불필요.

### 9. session_summaries/ 디렉터리 구조 도입
- **배경**: 여러 서비스의 세션 요약이 생길 수 있어 서비스별로 분리 관리.
- **해결**: `session_summaries/stockdog.md`로 이동. `sync_vault.sh`가 skyler 볼트에도 자동 복사.
- Obsidian Claude Code가 볼트 내 `raw/stockdog/session_summaries/stockdog.md`를 참조해 노트 작성 가능.

---

## 📈 Next Steps (Planned Improvements)
- **Prompt Engineering**: Gemini 프롬프트에 `📊 Earnings & Upgrades` 섹션 추가.
- **Engagement Filtering**: 좋아요/리트윗 수 기준으로 트윗 우선순위 필터링.
- **Attribution Accuracy**: LLM 기술적 분석 귀속 정확도 개선.

---
*All changes have been committed and pushed to the main branch.*  
**서버 반영:**
```bash
cd ~/service/stockdog && git pull
# .env 통합: 두 .env 내용을 루트로 병합 후 기존 파일 삭제
# telebot 재시작: cd telebot && docker compose up -d --build
# cron 업데이트: cd stockdog-core && ./deploy.sh
```

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

### 4. TeleBot Integration & Migration
- **Migration**: Moved the separate `Skyler-TeleBot` project into this repository for centralized management.
- **Dockerization**: Created a dedicated `Dockerfile` and `docker-compose.yml` for the bot with `restart: always` policy.
- **New Feature**: Added the `/fear` command. It fetches live data and generates a professional dark-theme gauge image using `matplotlib` to send via Telegram.

### 5. Fear & Greed Gauge Generator
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
