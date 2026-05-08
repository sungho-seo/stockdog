#!/usr/bin/env python3
import os
import re
import json
import logging
import urllib.request
import requests
import tempfile
from utils.chart_generator import generate_fear_greed_gauge
import urllib.parse
import pytz
from datetime import datetime, timedelta
from functools import wraps

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from github import Github, GithubException

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN        = os.environ["BOT_TOKEN"]
ALLOWED_CHAT_ID  = int(os.environ["CHAT_ID"])
GITHUB_PAT       = os.environ["GITHUB_PAT"]
GEMINI_API_KEY   = os.environ.get("GEMINI_API_KEY", "")
REPO_NAME        = os.environ.get("REPO_NAME", "sungho-seo/skyler")
KST              = pytz.timezone("Asia/Seoul")

g    = Github(GITHUB_PAT)
repo = g.get_repo(REPO_NAME)

MAX_LEN = 4000

CATEGORY_FOLDER = {
    "AI":       "20_Notes/AI",
    "Security": "20_Notes/Security",
    "Finance":  "20_Notes/Finance",
    "Dev":      "20_Notes/AI",
    "Other":    "00_Inbox",
}

CRYPTO_IDS = {"ETH": "ethereum", "XRP": "ripple"}


# ── 유틸 ────────────────────────────────────────────────────────────────────

def kst_now() -> datetime:
    return datetime.now(KST)

def today_str() -> str:
    return kst_now().strftime("%Y-%m-%d")

def yesterday_str() -> str:
    return (kst_now() - timedelta(days=1)).strftime("%Y-%m-%d")

def get_file(path: str) -> str | None:
    try:
        return repo.get_contents(path).decoded_content.decode("utf-8")
    except GithubException:
        return None

async def send_long(update: Update, text: str):
    while len(text) > MAX_LEN:
        await update.message.reply_text(text[:MAX_LEN])
        text = text[MAX_LEN:]
    if text:
        await update.message.reply_text(text)

def extract_md_section(content: str, keyword: str) -> str:
    """마크다운에서 keyword 포함 섹션 추출 (다음 동급 헤딩 전까지)"""
    lines = content.splitlines()
    result, in_section = [], False
    for line in lines:
        if re.match(r'^#{1,3}\s', line):
            if keyword.lower() in line.lower():
                in_section = True
            elif in_section:
                break
        if in_section:
            result.append(line)
    return "\n".join(result)

def strip_frontmatter(content: str) -> str:
    """YAML frontmatter (---...---) 제거"""
    if content.startswith("---"):
        end = content.find("---", 3)
        if end != -1:
            return content[end + 3:].lstrip("\n")
    return content

def get_watchlist_tickers() -> list:
    content = get_file("_system/watchlist.md") or ""
    tickers = []
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("-") or "|" not in line:
            continue
        ticker = line.split("|")[0].strip()
        if ticker:
            tickers.append(ticker)
    return tickers

def get_watchlist_info() -> dict:
    """watchlist.md에서 티커/이름 → {name, type, code} 매핑 반환"""
    content = get_file("_system/watchlist.md") or ""
    mapping = {}
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("-") or "|" not in line:
            continue
        parts = line.split("|")
        if len(parts) >= 3:
            code = parts[0].strip()
            name = parts[1].strip()
            kind = parts[2].strip()
            if not code:
                continue
            entry = {"name": name, "type": kind, "code": code}
            mapping[code.upper()] = entry
            if "KR" in kind:
                mapping[name.upper()] = entry
    return mapping

def add_trade_row(content: str, new_row: str) -> str:
    """데일리 노트 매매 기록 테이블에 행 추가 (빈 플레이스홀더 교체 또는 구분선 다음 삽입)"""
    lines = content.splitlines()
    in_trade = False
    sep_idx  = -1
    for i, line in enumerate(lines):
        if "### 매매 기록" in line:
            in_trade = True
        if in_trade and re.match(r'^\|[-| ]+\|', line):
            sep_idx = i
            break
    if sep_idx == -1:
        return content
    next_idx = sep_idx + 1
    # 빈 플레이스홀더 행이면 교체, 아니면 다음에 삽입
    if next_idx < len(lines) and not re.search(r'[^\s|]', lines[next_idx]):
        lines[next_idx] = new_row
    else:
        lines.insert(next_idx, new_row)
    return "\n".join(lines)

def auth_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat.id != ALLOWED_CHAT_ID:
            await update.message.reply_text("⛔ 권한 없음")
            return
        await func(update, context)
    return wrapper


# ── 가격 조회 유틸 ────────────────────────────────────────────────────────────

def fetch_us_price(ticker: str) -> dict | None:
    """Yahoo Finance v8 API로 미국 주식/ETF/지수 가격 조회"""
    try:
        url = (
            f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
            f"?interval=1d&range=1d"
        )
        req  = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        data = json.loads(urllib.request.urlopen(req, timeout=10).read().decode())
        meta = data["chart"]["result"][0]["meta"]
        price  = meta.get("regularMarketPrice", 0)
        prev   = meta.get("chartPreviousClose", price)
        change = price - prev
        change_pct = (change / prev * 100) if prev else 0
        return {"price": price, "change": change, "change_pct": change_pct}
    except Exception:
        return None

def fetch_crypto_price(ticker: str) -> dict | None:
    """CoinGecko로 코인 USD 현재가 + 24h 변동 조회"""
    coin_id = CRYPTO_IDS.get(ticker.upper())
    if not coin_id:
        return None
    try:
        url = (
            f"https://api.coingecko.com/api/v3/simple/price"
            f"?ids={coin_id}&vs_currencies=usd&include_24hr_change=true"
        )
        req  = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        data = json.loads(urllib.request.urlopen(req, timeout=10).read().decode())
        info = data.get(coin_id, {})
        return {
            "price":      info.get("usd", 0),
            "change_pct": info.get("usd_24h_change", 0),
        }
    except Exception:
        return None

def fetch_kr_price(code: str) -> dict | None:
    """Yahoo Finance KS suffix로 한국 주식/ETF 가격 조회"""
    try:
        suffix = ".KS" if code.isdigit() else ".KQ"
        url = (
            f"https://query1.finance.yahoo.com/v8/finance/chart/{code}{suffix}"
            f"?interval=1d&range=1d"
        )
        req  = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        data = json.loads(urllib.request.urlopen(req, timeout=10).read().decode())
        meta = data["chart"]["result"][0]["meta"]
        price  = meta.get("regularMarketPrice", 0)
        prev   = meta.get("chartPreviousClose", price)
        change = price - prev
        change_pct = (change / prev * 100) if prev else 0
        return {"price": int(price), "change": int(change), "change_pct": change_pct}
    except Exception:
        return None


# ── Gemini / 스크랩 유틸 ──────────────────────────────────────────────────────

def is_youtube(url: str) -> bool:
    return "youtube.com" in url or "youtu.be/" in url

def get_youtube_info(url: str) -> dict:
    """YouTube oEmbed로 제목·채널명 조회 (API 키 불필요)"""
    try:
        oembed = f"https://www.youtube.com/oembed?url={urllib.parse.quote(url)}&format=json"
        resp = urllib.request.urlopen(oembed, timeout=5)
        data = json.loads(resp.read().decode())
        return {"title": data.get("title", ""), "author": data.get("author_name", "")}
    except Exception:
        return {"title": "", "author": ""}

def get_youtube_transcript(url: str) -> str:
    """YouTube 자막 추출 (한국어 우선, 없으면 영어, 없으면 자동생성)"""
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        import re
        video_id = ""
        m = re.search(r"(?:v=|youtu\.be/)([A-Za-z0-9_-]{11})", url)
        if m:
            video_id = m.group(1)
        if not video_id:
            return ""
        for lang in (["ko"], ["en"], None):
            try:
                if lang:
                    entries = YouTubeTranscriptApi.get_transcript(video_id, languages=lang)
                else:
                    entries = YouTubeTranscriptApi.get_transcript(video_id)
                text = " ".join(e["text"] for e in entries)
                return text[:8000]
            except Exception:
                continue
        return ""
    except Exception:
        return ""

def fetch_webpage_text(url: str) -> str:
    """웹 페이지 텍스트 추출 (스크립트·스타일 제거)"""
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })
        html = urllib.request.urlopen(req, timeout=10).read().decode("utf-8", errors="ignore")
        html = re.sub(r'<(script|style)[^>]*>.*?</(script|style)>', '', html, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<[^>]+>', ' ', html)
        text = re.sub(r'\s+', ' ', text).strip()
        return text[:6000]
    except Exception:
        return ""

def call_gemini(parts: list) -> str:
    """Gemini 2.5 Flash API 호출"""
    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY가 설정되지 않았습니다.")
    payload = {
        "contents": [{"parts": parts}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 4096}
    }
    req = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}
    )
    import time
    for attempt in range(2):
        try:
            resp   = urllib.request.urlopen(req, timeout=30)
            result = json.loads(resp.read().decode())
            return result["candidates"][0]["content"]["parts"][0]["text"]
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt == 0:
                time.sleep(60)
                continue
            raise

def parse_gemini_json(text: str) -> dict:
    """Gemini 응답에서 JSON 블록 추출"""
    text = re.sub(r'```json\s*|\s*```', '', text)
    match = re.search(r'\{.*\}', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except Exception:
            pass
    return {}


# ── 명령어 핸들러 ─────────────────────────────────────────────────────────────

@auth_only
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📚 *Skyler Bot 명령어*\n\n"
        "*조회*\n"
        "/today — 오늘 데일리 노트\n"
        "/yesterday — 어제 데일리 노트\n"
        "/note 파일명 — 특정 노트 조회\n"
        "/search 키워드 — 볼트 전체 검색\n"
        "/inbox — 00\\_Inbox 목록\n"
        "/portfolio — 포트폴리오 현황\n"
        "/price 티커 — 실시간 가격 조회\n"
        "/fear — 공포탐욕지수 게이지 이미지\n\n"
        "*입력*\n"
        "/memo 내용 — Inbox에 빠른 메모 저장\n"
        "/daily 내용 — 오늘 데일리 노트에 한 줄 추가\n"
        "/trade 티커 매수/매도 가격 수량 — 매매 기록\n"
        "/scrap URL — URL 스크랩 → 볼트 자동 저장\n\n"
        "*분석*\n"
        "/query 질문 — 볼트 기반 AI 질의응답\n"
        "/week — 이번 주 업무 요약",
        parse_mode="Markdown"
    )

@auth_only
async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    date = today_str()
    content = get_file(f"10_Daily/{date}.md")
    if content:
        await send_long(update, content)
    else:
        await update.message.reply_text(f"❌ {date} 데일리 노트 없음")

@auth_only
async def cmd_yesterday(update: Update, context: ContextTypes.DEFAULT_TYPE):
    date = yesterday_str()
    content = get_file(f"10_Daily/{date}.md")
    if content:
        await send_long(update, content)
    else:
        await update.message.reply_text(f"❌ {date} 데일리 노트 없음")

@auth_only
async def cmd_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("사용법: /note 파일명  예) /note TSLA")
        return
    name = " ".join(context.args)
    candidates = [
        f"20_Notes/Finance/Stocks/{name}.md",
        f"20_Notes/AI/{name}.md",
        f"20_Notes/Security/{name}.md",
        f"40_Reference/{name}.md",
        f"00_Inbox/{name}.md",
        f"30_Projects/Personal/{name}.md",
        f"30_Projects/Work/{name}.md",
    ]
    for path in candidates:
        content = get_file(path)
        if content:
            await update.message.reply_text(f"📄 `{path}`", parse_mode="Markdown")
            await send_long(update, content)
            return
    await update.message.reply_text(f"❌ '{name}' 노트를 찾을 수 없습니다.")

@auth_only
async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("사용법: /search 키워드")
        return
    keyword = " ".join(context.args)
    try:
        results = g.search_code(f"{keyword} repo:{REPO_NAME}")
        total = results.totalCount
        items = list(results[:10])
        if not items:
            await update.message.reply_text(f"🔍 '{keyword}' 검색 결과 없음")
            return
        lines = [f"🔍 '{keyword}' 검색 결과 (총 {total}건, 상위 10개):"]
        for item in items:
            lines.append(f"• {item.path}")
        await update.message.reply_text("\n".join(lines))
    except Exception as e:
        await update.message.reply_text(f"❌ 검색 오류: {e}")

@auth_only
async def cmd_inbox(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        contents = repo.get_contents("00_Inbox")
        files = [c.name for c in contents if c.name.endswith(".md") and c.name != "README.md"]
        if not files:
            await update.message.reply_text("📥 Inbox 비어있음")
        else:
            text = f"📥 Inbox ({len(files)}개):\n" + "\n".join(f"• {f}" for f in sorted(files))
            await update.message.reply_text(text)
    except Exception as e:
        await update.message.reply_text(f"❌ 오류: {e}")

@auth_only
async def cmd_memo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("사용법: /memo 내용")
        return
    text = " ".join(context.args)
    now  = kst_now()
    date = now.strftime("%Y-%m-%d")
    time = now.strftime("%H%M")
    path = f"00_Inbox/{date}-memo-{time}.md"
    content = (
        f"---\n"
        f"date: {date}\n"
        f"tags:\n"
        f"  - \"#status/inbox\"\n"
        f"  - \"#ctx/personal\"\n"
        f"---\n\n"
        f"# 메모 ({date} {now.strftime('%H:%M')})\n\n"
        f"{text}\n"
    )
    try:
        repo.create_file(path, f"memo: telegram {date} {now.strftime('%H:%M')}", content)
        await update.message.reply_text(f"✅ 저장 완료\n`{path}`", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ 저장 실패: {e}")

@auth_only
async def cmd_daily(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("사용법: /daily 내용")
        return
    text = " ".join(context.args)
    now  = kst_now()
    date = now.strftime("%Y-%m-%d")
    path = f"10_Daily/{date}.md"
    try:
        file_obj = repo.get_contents(path)
        existing = file_obj.decoded_content.decode("utf-8")
        new_content = existing.rstrip() + f"\n\n> {now.strftime('%H:%M')} (telegram) {text}\n"
        repo.update_file(
            path,
            f"daily: telegram 메모 {date} {now.strftime('%H:%M')}",
            new_content,
            file_obj.sha
        )
        await update.message.reply_text("✅ 데일리 노트에 추가 완료")
    except GithubException:
        await update.message.reply_text(f"❌ {date} 데일리 노트 없음. /memo 로 Inbox에 저장하세요.")
    except Exception as e:
        await update.message.reply_text(f"❌ 오류: {e}")

@auth_only
async def cmd_portfolio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    content = get_file("20_Notes/Finance/Portfolio.md")
    if not content:
        await update.message.reply_text("❌ Portfolio.md 없음")
        return
    await update.message.reply_text("📊 *포트폴리오 현황*", parse_mode="Markdown")
    await send_long(update, strip_frontmatter(content))

@auth_only
async def cmd_trade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 4:
        await update.message.reply_text(
            "사용법: /trade 티커 매수/매도 가격 수량 [이유]\n"
            "예) /trade TSLA 매수 350.50 10 반등 기대\n"
            "예) /trade NAVER 매도 245000 5"
        )
        return
    ticker    = context.args[0].upper()
    direction = context.args[1]
    price     = context.args[2]
    qty       = context.args[3]
    reason    = " ".join(context.args[4:]) if len(context.args) > 4 else "-"
    now  = kst_now()
    date = now.strftime("%Y-%m-%d")
    path = f"10_Daily/{date}.md"
    try:
        file_obj = repo.get_contents(path)
        content  = file_obj.decoded_content.decode("utf-8")
        new_row  = f"| {ticker} | {ticker} | {direction} | {price} | {qty} | {reason} |"
        updated  = add_trade_row(content, new_row)
        repo.update_file(
            path,
            f"trade: {ticker} {direction} {date}",
            updated,
            file_obj.sha
        )
        await update.message.reply_text(
            f"✅ 매매 기록 추가\n"
            f"*{ticker}* {direction} {price} × {qty}\n"
            f"이유: {reason}",
            parse_mode="Markdown"
        )
    except GithubException:
        await update.message.reply_text(f"❌ {date} 데일리 노트 없음")
    except Exception as e:
        await update.message.reply_text(f"❌ 오류: {e}")

@auth_only
async def cmd_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "사용법: /price 티커\n"
            "예) /price TSLA  /price ETH  /price NAVER"
        )
        return
    ticker   = context.args[0].upper()
    watchlist = get_watchlist_info()
    info     = watchlist.get(ticker)

    def fmt_arrow(val: float) -> str:
        return "🔺" if val > 0 else "🔻" if val < 0 else "➖"

    def fmt_sign(val: float) -> str:
        return "+" if val > 0 else ""

    if info is None:
        # watchlist에 없으면 미국 주식으로 시도
        result = fetch_us_price(ticker)
        if result:
            arrow = fmt_arrow(result["change"])
            sign  = fmt_sign(result["change"])
            await update.message.reply_text(
                f"{arrow} *{ticker}*\n"
                f"${result['price']:,.2f}  {sign}${result['change']:.2f} ({sign}{result['change_pct']:.2f}%)",
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text(f"❌ '{ticker}' 조회 실패 (watchlist 미등록 종목)")
        return

    kind = info["type"]
    name = info["name"]
    code = info["code"]

    if "CRYPTO" in kind:
        result = fetch_crypto_price(code)
        if result:
            arrow = fmt_arrow(result["change_pct"])
            sign  = fmt_sign(result["change_pct"])
            await update.message.reply_text(
                f"{arrow} *{code}* ({name})\n"
                f"${result['price']:,.2f}\n"
                f"24h: {sign}{result['change_pct']:.2f}%",
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text(f"❌ {code} 가격 조회 실패")

    elif "KR" in kind:
        result = fetch_kr_price(code)
        if result:
            arrow = fmt_arrow(result["change"])
            sign  = fmt_sign(result["change"])
            await update.message.reply_text(
                f"{arrow} *{name}* ({code})\n"
                f"{result['price']:,}원\n"
                f"{sign}{result['change']:,}원 ({sign}{result['change_pct']:.2f}%)",
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text(f"❌ {name} 가격 조회 실패")

    else:
        result = fetch_us_price(code)
        if result:
            arrow = fmt_arrow(result["change"])
            sign  = fmt_sign(result["change"])
            await update.message.reply_text(
                f"{arrow} *{code}* ({name})\n"
                f"${result['price']:,.2f}\n"
                f"{sign}${result['change']:.2f} ({sign}{result['change_pct']:.2f}%)",
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text(f"❌ {code} 가격 조회 실패")

@auth_only
async def cmd_fear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fear & Greed Index를 게이지 이미지로 전송"""
    await update.message.reply_text("⏳ Fear & Greed 조회 중...")
    try:
        url = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "application/json",
            "Referer": "https://www.cnn.com/markets/fear-and-greed"
        }
        
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        
        fgi  = data.get("fear_and_greed", {})
        score  = fgi.get("score", 0)
        rating = fgi.get("rating", "unknown")

        with tempfile.TemporaryDirectory() as tmpdir:
            img_path = generate_fear_greed_gauge(
                score=score,
                rating=rating,
                output_dir=tmpdir
            )
            if img_path:
                with open(img_path, "rb") as f:
                    await update.message.reply_photo(
                        photo=f,
                        caption=f"📊 Fear & Greed: {int(round(score))} / {rating.upper()}"
                    )
            else:
                await update.message.reply_text(
                    f"📊 Fear & Greed: {int(round(score))} ({rating.upper()})"
                )
    except Exception as e:
        logger.error(f"fear cmd error: {e}")
        await update.message.reply_text(f"❌ 오류: {e}")


@auth_only
async def cmd_week(update: Update, context: ContextTypes.DEFAULT_TYPE):
    now     = kst_now()
    weekday = now.weekday()  # 0=월 … 6=일

    sections = []
    for i in range(weekday + 1):
        date  = (now - timedelta(days=weekday - i)).strftime("%Y-%m-%d")
        daily = get_file(f"10_Daily/{date}.md")
        if not daily:
            continue
        section = extract_md_section(daily, "Work")
        if section.strip() and "**주말**" not in section and "**공휴일**" not in section:
            sections.append(f"=== {date} ===\n{section[:1000]}")

    if not sections:
        await update.message.reply_text("이번 주 업무 데이터 없음 (주말이거나 데일리 노트 미작성)")
        return

    await update.message.reply_text("⏳ 주간 업무 요약 중...")

    prompt = (
        "다음은 이번 주 데일리 노트의 Work 섹션입니다.\n\n"
        + "\n\n".join(sections)
        + "\n\n---\n"
        "위 내용 기반으로 아래 3가지를 한국어로 간결하게 정리해줘:\n"
        "1. ✅ 완료된 주요 업무 (3줄 이내)\n"
        "2. 📌 미완료 / 후속조치 항목\n"
        "3. 💡 인사이트 (있으면)\n"
    )
    try:
        answer = call_gemini([{"text": prompt}])
        await send_long(update, f"📋 이번 주 업무 요약\n\n{answer}")
    except Exception as e:
        logger.error(f"week error: {e}")
        await send_long(update, "📋 이번 주 Work 섹션:\n\n" + "\n\n".join(sections))

@auth_only
async def cmd_scrap(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "사용법: /scrap [URL]\n"
            "예) /scrap https://youtube.com/watch?v=...\n"
            "예) /scrap https://blog.example.com/article"
        )
        return

    url = context.args[0]
    await update.message.reply_text(f"⏳ Gemini 분석 중...")

    try:
        now  = kst_now()
        date = now.strftime("%Y-%m-%d")
        time = now.strftime("%H%M")

        BODY_INSTRUCTION = (
            '"body":"## 섹션1 제목\\n내용\\n\\n## 섹션2 제목\\n내용 '
            '(한국어 마크다운, 구체적 내용·개념·방법·예시 포함, 섹션 수는 내용에 맞게 자유롭게)"'
        )

        if is_youtube(url):
            yt = get_youtube_info(url)
            title_hint = f'제목: {yt["title"]}, 채널: {yt["author"]}' if yt["title"] else ""
            prompt = (
                "이 유튜브 영상을 분석하고 옵시디언 스크랩 노트용 JSON만 반환해줘 (다른 텍스트 없이).\n\n"
                f"{title_hint}\n\n"
                "반환 형식:\n"
                '{"title":"노트 제목(한국어 30자 이내)",'
                '"category":"AI 또는 Security 또는 Finance 또는 Dev 또는 Other",'
                '"source_type":"youtube","channel":"채널명",'
                f'{BODY_INSTRUCTION},'
                '"insight":"핵심 인사이트 한 줄"}'
            )
            try:
                parts = [{"text": prompt}, {"file_data": {"file_uri": url}}]
                raw = call_gemini(parts)
            except Exception:
                transcript = get_youtube_transcript(url)
                content = f"\n\n[자막]\n{transcript}" if transcript else "\n\n(자막 없음)"
                parts = [{"text": prompt + f"\n\nURL: {url}{content}"}]
                raw = call_gemini(parts)
        else:
            page_text = fetch_webpage_text(url)
            prompt = (
                "다음 웹 페이지를 분석하고 옵시디언 스크랩 노트용 JSON만 반환해줘 (다른 텍스트 없이).\n\n"
                f"URL: {url}\n내용: {page_text}\n\n"
                "반환 형식:\n"
                '{"title":"노트 제목(한국어 30자 이내)",'
                '"category":"AI 또는 Security 또는 Finance 또는 Dev 또는 Other",'
                '"source_type":"news 또는 blog 또는 github 또는 other","author":"작성자 또는 사이트명",'
                f'{BODY_INSTRUCTION},'
                '"insight":"핵심 인사이트 한 줄"}'
            )
            parts = [{"text": prompt}]
            raw = call_gemini(parts)

        info = parse_gemini_json(raw)
        if not info:
            await update.message.reply_text(f"❌ Gemini 응답 파싱 실패\n원문:\n{raw[:500]}")
            return

        title       = info.get("title", f"스크랩 {date} {time}")
        category    = info.get("category", "Other")
        source_type = info.get("source_type", "other")
        author      = info.get("author", info.get("channel", ""))
        body        = info.get("body", "")
        insight     = info.get("insight", "")

        folder     = CATEGORY_FOLDER.get(category, "00_Inbox")
        source_tag = "source/youtube" if source_type == "youtube" else "source/news"
        safe_title = re.sub(r'[^\w가-힣\-]', '-', title)[:30]
        filename   = f"{date}-{safe_title}.md"
        filepath   = f"{folder}/{filename}"

        note = (
            f"---\n"
            f"date: {date}\n"
            f"source: {author}\n"
            f"url: {url}\n"
            f"tags:\n"
            f"  - scrap\n"
            f"  - {source_tag}\n"
            f"  - ctx/personal\n"
            f"---\n\n"
            f"# {title}\n\n"
            f"| 항목 | 내용 |\n"
            f"|------|------|\n"
            f"| 출처 | {author} |\n"
            f"| URL | {url} |\n"
            f"| 날짜 | {date} |\n\n"
            f"---\n\n"
            f"{body}\n\n"
            f"---\n\n"
            f"## 내 생각 / 인사이트\n\n"
            f"{insight}\n\n"
            f"---\n\n"
            f"## 관련 노트\n- \n"
        )

        try:
            repo.create_file(filepath, f"scrap: {title} ({date})", note)
        except GithubException:
            filepath = f"{folder}/{date}-{safe_title}-{time}.md"
            filename = filepath.split("/")[-1]
            repo.create_file(filepath, f"scrap: {title} ({date})", note)

        daily_path = f"10_Daily/{date}.md"
        try:
            daily_obj     = repo.get_contents(daily_path)
            daily_content = daily_obj.decoded_content.decode("utf-8")
            note_id       = filename.replace(".md", "")
            link_line     = f"- [[{note_id}]]"
            if "## Scrap" in daily_content:
                daily_content = daily_content.replace(
                    "## Scrap", f"## Scrap\n{link_line}", 1
                )
            else:
                daily_content = daily_content.rstrip() + f"\n\n## Scrap\n{link_line}\n"
            repo.update_file(
                daily_path,
                f"scrap: {title} 링크 추가",
                daily_content,
                daily_obj.sha
            )
        except Exception:
            pass

        headers = [line.lstrip("#").strip() for line in body.splitlines() if line.startswith("##")]
        toc     = "\n".join(f"• {h}" for h in headers) if headers else ""
        preview = f"\n\n📋 *목차*\n{toc}" if toc else ""
        await update.message.reply_text(
            f"✅ 스크랩 완료\n"
            f"📁 `{filepath}`\n\n"
            f"*{title}*{preview}\n\n"
            f"💡 {insight}",
            parse_mode="Markdown"
        )

    except Exception as e:
        logger.error(f"scrap error: {e}")
        await update.message.reply_text(f"❌ 오류: {e}")


@auth_only
async def cmd_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "사용법: /query 질문\n"
            "예) /query TSLA 최근 동향\n"
            "예) /query 코인 포트폴리오 어때"
        )
        return

    query = " ".join(context.args)
    await update.message.reply_text("⏳ 볼트 분석 중...")

    context_blocks = []

    known_tickers = get_watchlist_tickers()
    query_upper   = query.upper()
    caps_words    = re.findall(r'\b[A-Z]{2,6}\b', query)
    matched       = list({t for t in known_tickers if t in query_upper} | set(caps_words))

    for ticker in matched[:5]:
        note = get_file(f"20_Notes/Finance/Stocks/{ticker}.md")
        if note:
            context_blocks.append(f"=== {ticker}.md ===\n{note[:2000]}")

    for i in range(5):
        date  = (kst_now() - timedelta(days=i)).strftime("%Y-%m-%d")
        daily = get_file(f"10_Daily/{date}.md")
        if daily:
            section = extract_md_section(daily, "Finance")
            if section.strip():
                context_blocks.append(f"=== Daily {date} Finance ===\n{section[:1500]}")

    if not context_blocks:
        try:
            results = g.search_code(f"{query} repo:{REPO_NAME}")
            for item in list(results[:3]):
                content = get_file(item.path)
                if content:
                    context_blocks.append(f"=== {item.path} ===\n{content[:1500]}")
        except Exception:
            pass

    if not context_blocks:
        await update.message.reply_text("❌ 관련 볼트 내용을 찾지 못했습니다.")
        return

    vault_context = "\n\n".join(context_blocks)
    prompt = (
        f"다음은 사용자의 옵시디언 볼트(세컨드 브레인) 내용입니다.\n\n"
        f"{vault_context}\n\n"
        f"---\n"
        f"질문: {query}\n\n"
        f"위 볼트 내용만 근거로 답변해줘. 볼트에 없는 내용은 언급하지 말고, "
        f"있는 내용 기반으로 핵심만 간결하게 한국어로 답변해줘."
    )

    try:
        answer = call_gemini([{"text": prompt}])
        await send_long(update, f"🔍 {query}\n\n{answer}")
    except Exception as e:
        logger.error(f"query error: {e}")
        await update.message.reply_text(f"❌ 오류: {e}")


# ── 메인 ─────────────────────────────────────────────────────────────────────

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    for name, handler in [
        ("start",     cmd_help),
        ("help",      cmd_help),
        ("today",     cmd_today),
        ("yesterday", cmd_yesterday),
        ("note",      cmd_note),
        ("search",    cmd_search),
        ("inbox",     cmd_inbox),
        ("memo",      cmd_memo),
        ("daily",     cmd_daily),
        ("portfolio", cmd_portfolio),
        ("trade",     cmd_trade),
        ("price",     cmd_price),
        ("fear",      cmd_fear),
        ("week",      cmd_week),
        ("scrap",     cmd_scrap),
        ("query",     cmd_query),
    ]:
        app.add_handler(CommandHandler(name, handler))
    logger.info("Skyler Bot 시작 (polling)...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
