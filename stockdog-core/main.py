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
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample", action="store_true", help="Sample mode: minimal API calls for dev verification")
    args = parser.parse_args()

    load_dotenv()
    config = load_config()

    print("🐾 Starting StockDog...")

    USPipeline(config, sample=args.sample).run()
    KRPipeline(config, sample=args.sample).run()

    print("\n🐾 StockDog run complete.")


if __name__ == "__main__":
    main()
