# 📚 Skyler Bot Command Reference

이 문서는 `sskyler_bot`의 기능과 Gemini API 사용 여부를 관리합니다.

## 🤖 Gemini API Usage Summary
Gemini API는 주로 **분석, 요약, 추론**이 필요한 기능에서 사용됩니다.

| Command | Gemini API | Context | Cost Level |
| :--- | :---: | :--- | :--- |
| **StockDog Pipeline** | **YES** | Daily Market Analysis (All tweets/indicators) | High |
| `/scrap` | **YES** | URL analysis & Note generation | Medium |
| `/query` | **YES** | RAG (Retrieval-Augmented Generation) from Vault | Medium |
| `/week` | **YES** | Weekly work summarization | Low |

---

## 🛠️ Full Command List

### 1. Market & Finance (StockDog Integration)
- `/fear`: Fear & Greed gauge image (No API)
- `/price [ticker]`: Live price for US/KR stocks and Crypto (No API)
- `[Auto] Daily Report`: Sent every 10:00 AM KST (**Uses API**)

### 2. Obsidian Management
- `/memo [text]`: Save quick memo to Inbox (No API)
- `/daily [text]`: Append line to today's daily note (No API)
- `/trade [ticker] [side] [price] [qty]`: Log trade to table (No API)
- `/today` / `/yesterday`: View daily notes (No API)
- `/note [name]`: Fetch specific note (No API)
- `/inbox`: List files in Inbox (No API)
- `/search [query]`: Search files in vault (No API)
- `/portfolio`: View portfolio status (No API)

### 3. AI Intelligence
- `/scrap [url]`: AI summarizing of web/YouTube (**Uses API**)
- `/query [text]`: Ask AI about your vault content (**Uses API**)
- `/week`: AI summary of this week's work (**Uses API**)

---

## ⚙️ Maintenance
- **Last Updated**: 2026-05-08
- **Base Model**: Gemini 2.5 Flash (Fast & Low Cost)
