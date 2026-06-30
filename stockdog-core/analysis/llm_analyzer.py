import os
import json
import logging
from langchain_core.prompts import ChatPromptTemplate
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_anthropic import ChatAnthropic

logger = logging.getLogger(__name__)


def get_llm():
    if os.getenv("ANTHROPIC_API_KEY"):
        return ChatAnthropic(model="claude-sonnet-4-6", temperature=0.2, max_tokens=8192)
    elif os.getenv("GEMINI_API_KEY"):
        return ChatGoogleGenerativeAI(model="gemini-3-pro-preview", temperature=0.2, google_api_key=os.getenv("GEMINI_API_KEY"))
    else:
        logger.error("No LLM API keys found.")
        return None


def _format_korean_date(iso_date: str) -> str:
    """'2026-05-18' → '2026년 5월 18일'"""
    if not iso_date or iso_date == "unknown":
        return iso_date or "unknown"
    try:
        y, m, d = iso_date.split("-")
        return f"{int(y)}년 {int(m)}월 {int(d)}일"
    except (ValueError, AttributeError):
        return iso_date


def build_report_header(region: str, meta: dict) -> str:
    """본문 앞에 prepend할 H1 + 메타라인 (LLM 환각 방지를 위해 코드에서 결정).
    region: 'US' | 'KR'. meta: {'report_date','data_as_of','data_freshness'}.
    반환: '# … 마감 기준\\n\\n*발간일 … · 데이터 기준일 … · 신선도 …*\\n\\n'
    """
    label = "일일 미국 시장 분석" if region == "US" else "일일 한국 증시 마감 브리핑"
    data_as_of = meta.get("data_as_of") or "unknown"
    report_date = meta.get("report_date") or "unknown"
    freshness = meta.get("data_freshness") or "unknown"
    data_as_of_kr = _format_korean_date(data_as_of)
    report_date_kr = _format_korean_date(report_date)
    return (
        f"# {label} ({data_as_of_kr} 마감 기준)\n\n"
        f"*발간일 {report_date_kr} · 데이터 기준일 {data_as_of_kr} · 신선도 {freshness}*\n\n"
    )


def _invoke(llm, system_instruction, human_message, data_dict, meta=None):
    """meta(optional): {'report_date','data_as_of','data_freshness'} → prompts can {report_date} 등 참조."""
    if meta:
        # ChatPromptTemplate.from_messages가 {report_date} 등을 변수로 인식하려면 data_dict에 키가 있어야 한다.
        merged = dict(data_dict)
        merged.update({k: (v if v is not None else "unknown") for k, v in meta.items()})
        data_dict = merged
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


def analyze_us_market(data, meta=None, notes_root=None):
    """
    Generates a US market report from Twitter, indicators, us_market prices, and 13F data.

    Args:
        data: Dict with 'indicators', 'us_market', '13f', 'twitter', 'econ_calendar'
        meta: Optional dict with 'report_date', 'data_as_of', 'data_freshness'
        notes_root: Optional path to vault root (for sector snapshot extraction)
    """
    llm = get_llm()
    if not llm:
        return "Error: LLM not configured."

    # Extract sector/theme grounding for daily report.
    # Resolve notes_root from env when the caller doesn't pass it (matches
    # sector_job's hard-coded /notes mount) so the block can never silently no-op.
    notes_root = notes_root or os.environ.get("NOTES_ROOT", "/notes")
    sector_theme_context = "(섹터 데이터 없음)"
    if meta:
        try:
            from pathlib import Path
            from narrative_common import extract_sector_snapshot
            notes_root_path = Path(notes_root).expanduser()
            data_as_of = meta.get("data_as_of", "")
            sector_theme_context = extract_sector_snapshot(
                notes_root_path, data_as_of, mode="daily", stale_days=2
            )
        except Exception as e:
            logger.warning(f"Failed to extract sector snapshot: {e}")
            # sector_theme_context stays as placeholder on any error

    system_instruction = """
당신은 전문 미국 시장 분석가입니다. 제공된 데이터를 분석하여 한국어로 작성된 Obsidian Markdown 리포트를 생성하세요.

단정적 권유 표현('매수', '매도')은 사용하지 말고 '시그널', '관찰 포인트', '리스크' 등 관찰자 톤으로 작성하세요.
티커·가격·종목명·기관명은 영문 원문을 그대로 유지하세요 (예: SPY, NVIDIA, BlackRock).

사용 형식 요소:
- 헤더 (##, ###)
- 가격 데이터 및 13F 보유 현황 테이블
- Obsidian 콜아웃: > [!summary], > [!warning], > [!info], > [!tip]

중요 — 날짜 표기 규칙:
- 본 리포트의 분석 대상은 {data_as_of} 마감 데이터입니다 (보고서 발간일은 {report_date}, 데이터 기준일과 다를 수 있음). H1 제목과 메타 라인은 호출 측에서 자동 prepend되니 본문에는 작성하지 말 것.
- 모든 본문 날짜 표기는 한국어 형식 `YYYY년 M월 D일` 사용 (frontmatter는 ISO 유지).
- > [!summary] 첫 문장은 한국어 날짜 형식으로 시작 (예: `2026년 5월 18일 미국 증시는 …`).

§ 섹터·테마 작성 규칙:
- [소스3] 섹터·테마 데이터는 코드가 붙인 '지속'/'당일 한정' 태그만 근거로 서술하세요. 다른 persistence 주장 금지.
- 당일 1일 등락만으로 '로테이션', '추세 전환' 표현 금지.
- 추세 표현은 5일과 1개월 부호가 일치할 때만 허용.
- 길이: 3~6 bullets 또는 짧은 1단락, 6줄 이내, 90자 이내 (한국어).
- ETF 티커 노출 금지.

리포트 구조 (이 순서와 한글 헤더를 정확히 따를 것):
1. > [!summary] 시장 요약 — 오늘 미국 시장 전반적 흐름 (3-5문장). 첫 문장은 한국어 날짜 형식으로 시작.
2. ## 시장 지표 — F&G, VIX, 10Y Yield 해석 포함
3. ## 섹터·테마 — 제공된 [소스3] 섹터·테마 데이터를 해석. 코드가 제공한 태그가 근거.
   단, <Sector_Theme> 블록이 `(섹터 데이터 없음)`이면 이 섹션 본문은 생성하지 말고 헤더 아래에 다음 콜아웃 한 줄만 출력하세요:
   `> [!info] 섹터 데이터 없음 — 이번 리포트에는 섹터·테마 스냅샷이 포함되지 않습니다.`
   추정·예상·일반론으로 빈 자리를 채우지 마세요.
4. ## 포트폴리오 스냅샷 — 미국 주식/ETF/지수 테이블 (컬럼: 종목 | 이름 | 가격 | 등락률 | 메모). 종목·이름은 영문 원문 유지.
5. ## 인플루언서 심리 — Twitter 합의된 견해와 충돌된 견해.
   단, <Twitter> 블록이 비어있거나 `{{}}`이면 이 섹션 본문은 생성하지 말고 헤더 아래에 다음 콜아웃 한 줄만 출력하세요:
   `> [!info] Twitter 수집 비활성화 — 이번 리포트에는 인플루언서 시그널이 포함되지 않습니다.`
   추정·예상·일반론으로 빈 자리를 채우지 마세요.
6. ## 기관 보유 (13F) — 종목별 상위 보유 기관 테이블. 기관명은 영문 원문 유지.
   단, <13F> 블록이 비어있거나 `{{}}`이면 이 섹션 본문은 생성하지 말고 헤더 아래에 다음 콜아웃 한 줄만 출력하세요:
   `> [!info] 13F 수집 비활성화 — 이번 리포트에는 기관 보유 데이터가 포함되지 않습니다.`
   추정·예상·일반론으로 빈 자리를 채우지 마세요.
7. ## 단기 전망 — 데이터 기반 2-3일 관찰 포인트
8. ## 경제 캘린더 — 향후 7일 발표 일정 테이블 (컬럼: 날짜 | 이벤트 | D-Day | 이전값 | 컨센서스). releasing_today가 비어있지 않으면 > [!warning] 콜아웃으로 해당 이벤트와 잠재적 시장 영향을 명시.
"""

    human_message = """Here is today's raw data:

<Report_Meta>
report_date: {report_date}
data_as_of: {data_as_of}
data_freshness: {data_freshness}
</Report_Meta>

<Indicators>
{indicators}
</Indicators>

<Sector_Theme>
[소스3 — 섹터·테마]
{sector_theme}
</Sector_Theme>

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
        "sector_theme": sector_theme_context,
        "us_market": json.dumps(data.get('us_market', {}), indent=2),
        "f13": json.dumps(data.get('13f', {}), indent=2),
        "twitter": json.dumps(data.get('twitter', {}), indent=2),
        "econ_calendar": json.dumps(data.get('econ_calendar', {}), indent=2),
    }, meta=meta)


def analyze_kr_market(data, meta=None):
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

중요 — 날짜 표기 규칙:
- 본 리포트의 분석 대상은 {data_as_of} 마감 데이터입니다 (보고서 발간일은 {report_date}, 데이터 기준일과 다를 수 있음). H1 제목과 메타 라인은 호출 측에서 자동 prepend되니 본문에는 작성하지 말 것.
- 모든 본문 날짜 표기는 한국어 형식 `YYYY년 M월 D일` 사용 (frontmatter는 ISO 유지).
- > [!summary] 첫 문장은 한국어 날짜 형식으로 시작 (예: `2026년 5월 18일 한국 증시는 …`).

Structure the report as follows:
1. > [!summary] 시장 요약 — 오늘 한국 시장 전반적 흐름 (3-5문장). 첫 문장은 한국어 날짜 형식으로 시작.
2. ## 주요 지수 — KOSPI, KOSDAQ 테이블 (종가, 등락률, 해석)
3. ## 환율 — USD/KRW 현황 및 시사점
4. ## 개별 종목 동향 — 종목별 테이블 (종목명, 종가, 등락률, 거래량, 분석)
5. ## 단기 전망 — 데이터 기반 1-2일 전망
"""

    human_message = """Here is today's Korean market data:

<Report_Meta>
report_date: {report_date}
data_as_of: {data_as_of}
data_freshness: {data_freshness}
</Report_Meta>

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
    }, meta=meta)
