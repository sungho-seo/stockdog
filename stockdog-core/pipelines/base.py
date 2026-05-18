from abc import ABC, abstractmethod
import logging

logger = logging.getLogger(__name__)


class MarketPipeline(ABC):
    REGION_LABEL = "?"
    FAILED_REASON_HINT = "core data"

    def __init__(self, config: dict, sample: bool = False):
        self.config = config
        self.sample = sample
        self._last_status: str | None = None

    @abstractmethod
    def collect(self) -> dict:
        """Collect raw market data. Returns a data dict."""
        pass

    @abstractmethod
    def analyze(self, data: dict) -> str:
        """Run LLM analysis on collected data. Returns Markdown string."""
        pass

    @abstractmethod
    def _compute_status(self, data: dict) -> str:
        """Return 'complete' | 'partial' | 'failed' based on collected data."""
        pass

    @abstractmethod
    def save(self, report: str) -> None:
        """Save the generated report to the vault."""
        pass

    def notify(self, data: dict, report: str) -> None:
        """Send Telegram notification. Override to customize."""
        pass

    def _failed_report_template(self) -> str:
        return (
            "> [!warning] 데이터 수집 실패 — 분석 생성 안 됨\n"
            f"> 본 리포트는 status=failed 상태에서 생성되었습니다. {self.REGION_LABEL} 시장 "
            f"핵심 데이터({self.FAILED_REASON_HINT}) 수집이 실패하여 LLM 분석을 건너뜁니다.\n"
            "> 원인 조사: stockdog 로그 확인 필요. (휴장일 / API 오류 / 네트워크 등)\n"
        )

    def run(self) -> None:
        name = self.__class__.__name__
        print(f"\n{'='*50}")
        print(f"▶ {name} starting...")
        print(f"{'='*50}")

        try:
            data = self.collect()
            self._last_status = self._compute_status(data)
            if self._last_status == "failed":
                logger.warning(f"{name}: status=failed — skipping LLM, using placeholder.")
                report = self._failed_report_template()
            else:
                report = self.analyze(data)

            if report and not report.startswith("> [!error]") and not report.startswith("Error"):
                self.save(report)
                self.notify(data, report)
            else:
                logger.error(f"{name}: analysis failed or returned error.")
                self.notify(data, report)
        except Exception as e:
            logger.error(f"{name} failed: {e}")
