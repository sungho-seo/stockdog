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
당신은 전문 미국 시장 분석가입니다. 제공된 데이터를 분석하여 한국어로 작성된 Obsidian Markdown 리포트를 생성하세요.

단정적 권유 표현('매수', '매도')은 사용하지 말고 '시그널', '관찰 포인트', '리스크' 등 관찰자 톤으로 작성하세요.
티커·가격·종목명·기관명은 영문 원문을 그대로 유지하세요 (예: SPY, NVIDIA, BlackRock).

사용 형식 요소:
- 헤더 (##, ###)
- 가격 데이터 및 13F 보유 현황 테이블
- Obsidian 콜아웃: > [!summary], > [!warning], > [!info], > [!tip]

리포트 구조 (이 순서와 한글 헤더를 정확히 따를 것):
1. > [!summary] 시장 요약 — 오늘 미국 시장 전반적 흐름 (3-5문장)
2. ## 시장 지표 — F&G, VIX, 10Y Yield 해석 포함
3. ## 포트폴리오 스냅샷 — 미국 주식/ETF/지수 테이블 (컬럼: 종목 | 이름 | 가격 | 등락률 | 메모). 종목·이름은 영문 원문 유지.
4. ## 인플루언서 심리 — Twitter 합의된 견해와 충돌된 견해.
   단, <Twitter> 블록이 비어있거나 `{{}}`이면 이 섹션 본문은 생성하지 말고 헤더 아래에 다음 콜아웃 한 줄만 출력하세요:
   `> [!info] Twitter 수집 비활성화 — 이번 리포트에는 인플루언서 시그널이 포함되지 않습니다.`
   추정·예상·일반론으로 빈 자리를 채우지 마세요.
5. ## 기관 보유 (13F) — 종목별 상위 보유 기관 테이블. 기관명은 영문 원문 유지.
6. ## 단기 전망 — 데이터 기반 2-3일 관찰 포인트
7. ## 경제 캘린더 — 향후 7일 발표 일정 테이블. releasing_today가 비어있지 않으면 > [!warning] 콜아웃으로 해당 이벤트와 잠재적 시장 영향을 명시.
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

한국어로 Obsidian Markdown 리포트를 생성하세요. 단, 티커·가격·종목명·기관명은 영문 원문을 유지하세요."""

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
