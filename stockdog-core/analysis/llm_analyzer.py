import os
import json
import logging
from langchain_core.prompts import ChatPromptTemplate
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_anthropic import ChatAnthropic

logger = logging.getLogger(__name__)

def get_llm():
    """
    Initializes the LLM model based on available environment variables.
    Prefers Gemini, falls back to Claude.
    """
    if os.getenv("GEMINI_API_KEY"):
        # We use a relatively low temperature for analytical consistency
        return ChatGoogleGenerativeAI(model="gemini-1.5-pro-latest", temperature=0.2, google_api_key=os.getenv("GEMINI_API_KEY"))
    elif os.getenv("ANTHROPIC_API_KEY"):
        return ChatAnthropic(model="claude-3-opus-20240229", temperature=0.2)
    else:
        logger.error("No LLM API keys found. Please set GEMINI_API_KEY or ANTHROPIC_API_KEY in .env")
        return None

def analyze_market_data(twitter_data, indicators_data, f13_data):
    """
    Feeds the collected raw data to the LLM and asks it to generate a structured 
    market analysis report.
    """
    llm = get_llm()
    if not llm:
        return "Error: LLM not configured."
        
    system_instruction = """
    You are an expert financial analyst and a highly capable data synthesizer. 
    Your task is to analyze raw market data collected from Twitter influencers, market indicators (VIX, 10Y Yield, Fear & Greed), and 13F institutional holdings.
    
    CRITICAL REQUIREMENT: Output your analysis STRICTLY in Obsidian-flavored Markdown. 
    Use the following formatting elements to make it beautiful and readable:
    - Headers (##, ###)
    - Bullet points and numbered lists
    - Tables for comparing data (e.g., 13F holdings or indicator summaries)
    - Obsidian Callouts for key takeaways, e.g.:
      > [!summary] Executive Summary
      > [!warning] Conflicting Opinions
      > [!info] Market Indicators
      
    Your report should include:
    1. A brief executive summary of the current market mood.
    2. A summary of the Market Indicators (translate what they mean together).
    3. An analysis of the X (Twitter) influencers' sentiment. Specifically, highlight consensus and any *conflicting opinions* between influencers.
    4. An overview of the 13F institutional holding changes for the target portfolio.
    5. A short-term market flow prediction based on the aggregated data.
    """
    
    prompt = ChatPromptTemplate.from_messages([
        ("system", system_instruction),
        ("human", "Here is the raw data:\n\n<Indicators>\n{indicators}\n</Indicators>\n\n<13F>\n{f13}\n</13F>\n\n<Twitter>\n{twitter}\n</Twitter>\n\nPlease generate the Obsidian Markdown report.")
    ])
    
    chain = prompt | llm
    
    print("🧠 Analyzing data with LLM... (This may take a minute)")
    try:
        response = chain.invoke({
            "indicators": json.dumps(indicators_data, indent=2),
            "f13": json.dumps(f13_data, indent=2),
            "twitter": json.dumps(twitter_data, indent=2)
        })
        return response.content
    except Exception as e:
        logger.error(f"LLM Analysis failed: {e}")
        return f"> [!error] Analysis Failed\n> {str(e)}"
