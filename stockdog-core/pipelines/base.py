from abc import ABC, abstractmethod
import logging

logger = logging.getLogger(__name__)


class MarketPipeline(ABC):
    def __init__(self, config: dict, sample: bool = False):
        self.config = config
        self.sample = sample

    @abstractmethod
    def collect(self) -> dict:
        """Collect raw market data. Returns a data dict."""
        pass

    @abstractmethod
    def analyze(self, data: dict) -> str:
        """Run LLM analysis on collected data. Returns Markdown string."""
        pass

    @abstractmethod
    def save(self, report: str) -> None:
        """Save the generated report to the vault."""
        pass

    def notify(self, data: dict, report: str) -> None:
        """Send Telegram notification. Override to customize."""
        pass

    def run(self) -> None:
        name = self.__class__.__name__
        print(f"\n{'='*50}")
        print(f"▶ {name} starting...")
        print(f"{'='*50}")

        try:
            data = self.collect()
            report = self.analyze(data)

            if report and not report.startswith("> [!error]") and not report.startswith("Error"):
                self.save(report)
                self.notify(data, report)
            else:
                logger.error(f"{name}: analysis failed or returned error.")
                self.notify(data, report)
        except Exception as e:
            logger.error(f"{name} failed: {e}")
