import yaml
import logging
import argparse
from dotenv import load_dotenv

from pipelines.us_pipeline import USPipeline
from pipelines.kr_pipeline import KRPipeline

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')


def load_config(config_path="config.yaml"):
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def main():
    parser = argparse.ArgumentParser(description="StockDog daily pipeline runner")
    parser.add_argument(
        "--sample",
        action="store_true",
        help="Sample mode: minimal API calls for dev verification",
    )
    parser.add_argument(
        "--region",
        choices=["us", "kr", "both"],
        default="both",
        help="Which pipeline to run (default: both — backward compatible)",
    )
    args = parser.parse_args()

    load_dotenv()
    config = load_config()

    region = args.region.lower()
    print(f"🐾 Starting StockDog... (region={region}, sample={args.sample})")

    if region in ("us", "both"):
        USPipeline(config, sample=args.sample).run()
    if region in ("kr", "both"):
        KRPipeline(config, sample=args.sample).run()

    if region == "us":
        print("\n🐾 StockDog US-only run complete.")
    elif region == "kr":
        print("\n🐾 StockDog KR-only run complete.")
    else:
        print("\n🐾 StockDog run complete.")


if __name__ == "__main__":
    main()
