import os
import json
import logging
from langchain_core.prompts import ChatPromptTemplate
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_anthropic import ChatAnthropic

logger = logging.getLogger(__name__)


def get_llm():
    if os.getenv("GEMINI_API_KEY"):
        return ChatGoogleGenerativeAI(model="gemini-3-pro-preview", temperature=0.2, google_api_key=os.getenv("GEMINI_API_KEY"))
    elif os.getenv("ANTHROPIC_API_KEY"):
        return ChatAnthropic(model="claude-3-opus-20240229", temperature=0.2)
    else:
        logger.error("No LLM API keys found.")
        return None


def _invoke(llm, system_instruction, human_message, data_dict):
    prompt = ChatPromptTemplate.from_messages([
        ("system", system_instruction),
        ("human", human_message),
    ])
    chain = prompt | llm
    print("🧠 Analyzing with LLM... (this may take a minute)")
    try:
        response = chain.invoke(data_dict)
        return response.content
    except Exception as e:
        logger.error(f"LLM analysis failed: {e}")
        return f"> [!error] Analysis Failed\n> {str(e)}"


def analyze_us_market(data):
    """
    Generates a US market report from Twitter, indicators, us_market prices, and 13F data.
    """
    llm = get_llm()
    if not llm:
        return "Error: LLM not configured."

    system_instruction = """
You are an expert US financial analyst. Analyze the provided market data and generate a structured daily report in Obsidian-flavored Markdown.

Use these formatting elements:
- Headers (##, ###)
- Tables for price data and 13F holdings
- Obsidian callouts: > [!summary], > [!warning], > [!info], > [!tip]

Structure the report as follows:
1. > [!summary] Executive Summary — overall market mood in 3-5 sentences
2. ## Market Indicators — F&G, VIX, 10Y Yield with interpretation
3. ## Portfolio Snapshot — table of all US stocks/ETFs/indices: name, price, change%, brief note
4. ## Influencer Sentiment — consensus and conflicting opinions from Twitter
5. ## Institutional Holdings (13F) — table of top holders for each stock
6. ## Short-term Outlook — data-driven 2-3 day forecast
7. ## Economic Calendar — upcoming releases table (next 7 days); if releasing_today is non-empty, add a > [!warning] callout noting the event and its potential market impact
"""

    human_message = """Here is today's raw data:

<Indicators>
{indicators}
</Indicators>

<US_Market>
{us_market}
</US_Market>

<13F>
{f13}
</13F>

<Twitter>
{twitter}
</Twitter>

<Economic_Calendar>
{econ_calendar}
</Economic_Calendar>

Generate the Obsidian Markdown report."""

    return _invoke(llm, system_instruction, human_message, {
        "indicators": json.dumps(data.get('indicators', {}), indent=2),
        "us_market": json.dumps(data.get('us_market', {}), indent=2),
        "f13": json.dumps(data.get('13f', {}), indent=2),
        "twitter": json.dumps(data.get('twitter', {}), indent=2),
        "econ_calendar": json.dumps(data.get('econ_calendar', {}), indent=2),
    })


def analyze_kr_market(data):
    """
    Generates a Korean market report from KR stocks, indices, and exchange rate data.
    """
    llm = get_llm()
    if not llm:
        return "Error: LLM not configured."

    system_instruction = """
You are an expert Korean financial analyst. Analyze the provided Korean market data and generate a structured daily report in Obsidian-flavored Markdown.

Use these formatting elements:
- Headers (##, ###)
- Tables for price data
- Obsidian callouts: > [!summary], > [!warning], > [!info]

Structure the report as follows:
1. > [!summary] 시장 요약 — 오늘 한국 시장 전반적 흐름 (3-5문장)
2. ## 주요 지수 — KOSPI, KOSDAQ 테이블 (종가, 등락률, 해석)
3. ## 환율 — USD/KRW 현황 및 시사점
4. ## 개별 종목 동향 — 종목별 테이블 (종목명, 종가, 등락률, 거래량, 분석)
5. ## 단기 전망 — 데이터 기반 1-2일 전망
"""

    human_message = """Here is today's Korean market data:

<KR_Indices>
{kr_indices}
</KR_Indices>

<Exchange_Rates>
{exchange}
</Exchange_Rates>

<KR_Stocks>
{kr_stocks}
</KR_Stocks>

Generate the Obsidian Markdown report in Korean."""

    return _invoke(llm, system_instruction, human_message, {
        "kr_indices": json.dumps(data.get('kr_indices', {}), indent=2),
        "exchange": json.dumps(data.get('exchange', {}), indent=2),
        "kr_stocks": json.dumps(data.get('kr_stocks', {}), indent=2),
    })
