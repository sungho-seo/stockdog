#!/usr/bin/env python3
"""
Manual-only remediation script for Form 4 insider-transaction parsing bug fix.

This script:
1. Fetches the Form 4 XML from SEC EDGAR for specified accessions
2. Re-parses using the fixed _parse_form4_xml (separating market vs non-market)
3. Surgically updates affected day-record JSON files in vault

Use case: After deploying a schema-breaking parser fix, re-fetch and re-parse
historically problematic records.

NOT part of regular cron. Run manually only.

Affected records (schema_version=1 bug):
  - TSLA 0001104659-26-075213 (Musk Elon, 2026-06-16, phantom $14.2B sell)
  - GOOGL 0001168404-26-000025 (GV 2019 GP, 2026-05-15, similar exercise/sale mix)
"""
import argparse
import json
import logging
import os
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from collectors.m7_insider import _parse_form4_xml, _fetch_form4_xml, _fetch_submissions, _list_recent_form4
from utils.m7_config import load_m7_config, merge_thresholds
from utils.m7_store import _atomic_write_json

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


REMEDIATION_SPECS = [
    {
        "symbol": "TSLA",
        "cik": "1318605",
        "accession": "0001104659-26-075213",
        "tx_date": "2026-06-16",
        "insider": "Musk, Elon",
        "description": "Exercise (M, 304M @ 44.13) + Sale (S, 17.5M @ 430) — phantom $14.2B bug",
    },
    {
        "symbol": "GOOGL",
        "cik": "1652044",
        "accession": "0001168404-26-000025",
        "tx_date": "2026-05-15",
        "insider": "GV 2019 GP, L.L.C.",
        "description": "Similar M/S mix from exercise+market transaction",
    },
]


def fetch_and_reparse(
    symbol: str,
    cik_int: str,
    accession: str,
    cfg: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Fetch Form 4 XML from SEC and re-parse with fixed parser.

    Returns: transaction dict (with new schema_version=2 fields)
             or None if fetch fails.
    """
    fetch = cfg.get("fetch", {}) or {}
    headers = {
        "User-Agent": fetch.get("sec_user_agent", "Skyler M7 Tracker nosy.seo@gmail.com"),
        "Accept": "application/json",
        "Accept-Encoding": "gzip, deflate",
        "Host": "www.sec.gov",
    }
    max_retries = int(fetch.get("edgar_max_retries", 3))
    backoff_sec = float(fetch.get("edgar_retry_backoff_sec", 2.0))
    pause_sec = float(fetch.get("edgar_request_pause_sec", 0.15))

    # Fetch submissions to find primary_doc
    cik10 = str(int(cik_int)).zfill(10)
    submissions_headers = dict(headers)
    submissions_headers["Host"] = "data.sec.gov"
    submissions_headers["Accept"] = "application/json"

    submissions_url = f"https://data.sec.gov/submissions/CIK{cik10}.json"
    logger.info(f"[{symbol}] Fetching submissions for CIK {cik10}...")
    try:
        import requests
        resp = requests.get(submissions_url, headers=submissions_headers, timeout=15)
        resp.raise_for_status()
        submissions = resp.json()
    except Exception as e:
        logger.error(f"[{symbol}] Failed to fetch submissions: {e}")
        return None

    # Find primary_doc for this accession
    recent = (submissions.get("filings") or {}).get("recent") or {}
    accs = recent.get("accessionNumber") or []
    primary_docs = recent.get("primaryDocument") or []
    primary_doc = None
    for acc, doc in zip(accs, primary_docs):
        if acc == accession:
            primary_doc = doc
            break

    if not primary_doc:
        logger.error(f"[{symbol}] Could not find primary_doc for accession {accession}")
        return None

    logger.info(f"[{symbol}] Found primary_doc={primary_doc}, fetching XML...")
    headers["Host"] = "www.sec.gov"
    xml_text = _fetch_form4_xml(
        cik_int, accession, primary_doc, headers,
        max_retries, backoff_sec, pause_sec
    )
    if not xml_text:
        logger.error(f"[{symbol}] Failed to fetch Form 4 XML for {accession}")
        return None

    logger.info(f"[{symbol}] Parsing XML with fixed parser...")
    tx = _parse_form4_xml(xml_text, accession)
    return tx


def remediate_record(
    symbol: str,
    cik_int: str,
    accession: str,
    tx_date: str,
    cfg: Dict[str, Any],
    skip_fetch: bool = False,
) -> dict:
    """Remediate a single bad record across all affected JSON files.

    Returns: {
        "symbol": str,
        "accession": str,
        "status": "success" | "fetch_failed" | "neutralized",
        "before": dict,
        "after": dict,
        "files_updated": [path, ...],
    }
    """
    raw_base_dir = cfg.get("storage", {}).get("raw_base_dir", "/notes/raw/stockdog/m7")
    eff = merge_thresholds(symbol, cfg)
    min_value_usd = float(eff.get("insider", {}).get("min_value_usd", 500000))
    schema_version = cfg.get("storage", {}).get("schema_version", 2)

    result = {
        "symbol": symbol,
        "accession": accession,
        "status": "unknown",
        "before": None,
        "after": None,
        "files_updated": [],
    }

    # Try to fetch and re-parse from SEC
    if not skip_fetch:
        logger.info(f"[{symbol}] Attempting SEC fetch for {accession}...")
        tx = fetch_and_reparse(symbol, cik_int, accession, cfg)
        if tx:
            logger.info(f"[{symbol}] SEC fetch succeeded. Parsed: action={tx.get('action')}, "
                        f"value_usd={tx.get('value_usd')}, market_shares={tx.get('market_shares')}")
            result["status"] = "success"
            result["after"] = tx
        else:
            logger.warning(f"[{symbol}] SEC fetch failed, falling back to neutralization...")
            result["status"] = "neutralized"
            # Neutralization: move headline to nonmarket, set headline to 0
            tx = {
                "accession": accession,
                "action": "Exercise",
                "shares": 0.0,
                "price_usd": 0.0,
                "value_usd": 0.0,
                "market_shares": 0.0,
                "market_value_usd": 0.0,
                "nonmarket_shares": 0.0,
                "nonmarket_value_usd": 0.0,  # Unknown exact value, conservatively 0
                "tx_codes": [],
                "price_footnoted": False,
            }
            result["after"] = tx
    else:
        logger.info(f"[{symbol}] --skip-fetch, using neutralization...")
        result["status"] = "neutralized"
        tx = {
            "accession": accession,
            "action": "Exercise",
            "shares": 0.0,
            "price_usd": 0.0,
            "value_usd": 0.0,
            "market_shares": 0.0,
            "market_value_usd": 0.0,
            "nonmarket_shares": 0.0,
            "nonmarket_value_usd": 0.0,
            "tx_codes": [],
            "price_footnoted": False,
        }
        result["after"] = tx

    # Now update all affected files
    # Files to update (per spec):
    #   - ~/{symbol}/insider_history.json (all day-records matching this accession)
    #   - ~/insider/{date_range}.json (all day-records matching this accession)

    def merge_transaction(old_tx: dict, new_tx: dict) -> None:
        """Merge new_tx into old_tx, preserving existing non-empty values for insider_name/role."""
        for key, val in new_tx.items():
            # Preserve existing insider_name/role if the new value is empty string
            if key in ("insider_name", "role") and val == "" and key in old_tx and old_tx[key]:
                logger.debug(f"  Preserving existing {key}='{old_tx[key]}' (new value was empty)")
                continue
            old_tx[key] = val

    updated_files = []

    # 1. Per-symbol history file
    history_path = os.path.join(raw_base_dir, symbol, "insider_history.json")
    if os.path.exists(history_path):
        logger.info(f"[{symbol}] Updating per-symbol history {history_path}...")
        try:
            with open(history_path, "r", encoding="utf-8") as f:
                records = json.load(f)
            if not isinstance(records, list):
                records = [records]

            before_count = len(records)
            for record in records:
                if isinstance(record, dict):
                    txs = record.get("transactions", [])
                    if isinstance(txs, list):
                        for t in txs:
                            if isinstance(t, dict) and t.get("accession") == accession:
                                if not result["before"]:
                                    result["before"] = t.copy()
                                # Update this transaction (preserving non-empty insider_name/role)
                                merge_transaction(t, tx)
                                t["breach"] = t.get("value_usd", 0.0) >= min_value_usd

            _atomic_write_json(history_path, records)
            logger.info(f"[{symbol}] Updated {history_path}, {before_count} records")
            updated_files.append(history_path)
        except Exception as e:
            logger.error(f"[{symbol}] Failed to update {history_path}: {e}")
    else:
        logger.warning(f"[{symbol}] File not found: {history_path}")

    # 2. Per-day dump files (2026-06-19, 2026-06-20 for TSLA; broader range for GOOGL)
    # For simplicity, scan all insider/*.json files and update matching records
    insider_dir = os.path.join(raw_base_dir, "insider")
    if os.path.isdir(insider_dir):
        logger.info(f"Scanning per-day dumps in {insider_dir}...")
        for fname in sorted(os.listdir(insider_dir)):
            if not fname.endswith(".json"):
                continue
            fpath = os.path.join(insider_dir, fname)
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    dump = json.load(f)

                by_ticker = dump.get("by_ticker") or {}
                sym_data = by_ticker.get(symbol)
                if not sym_data:
                    continue

                txs = sym_data.get("transactions", [])
                for t in txs:
                    if isinstance(t, dict) and t.get("accession") == accession:
                        if not result["before"]:
                            result["before"] = t.copy()
                        merge_transaction(t, tx)
                        t["breach"] = t.get("value_usd", 0.0) >= min_value_usd

                _atomic_write_json(fpath, dump)
                logger.info(f"Updated per-day dump {fname}")
                updated_files.append(fpath)
            except Exception as e:
                logger.error(f"Failed to update {fpath}: {e}")

    result["files_updated"] = updated_files
    return result


def main():
    parser = argparse.ArgumentParser(
        description="Remediate Form 4 parsing bug by re-fetching and re-parsing from SEC EDGAR."
    )
    parser.add_argument("--config", default=None, help="Path to m7.yaml")
    parser.add_argument(
        "--skip-fetch",
        action="store_true",
        help="Don't fetch from SEC; use neutralization (fallback path) for all records",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Don't write — just report what would change",
    )
    args = parser.parse_args()

    cfg = load_m7_config(args.config)

    print("\n" + "="*70)
    print("FORM 4 INSIDER TRANSACTION BUG REMEDIATION")
    print("="*70)
    print("This script re-fetches Form 4 XML from SEC EDGAR and re-parses using")
    print("the fixed parser (separating market vs. non-market transactions).")
    print("="*70 + "\n")

    all_results = []

    for spec in REMEDIATION_SPECS:
        symbol = spec["symbol"]
        cik_int = spec["cik"]
        accession = spec["accession"]
        tx_date = spec["tx_date"]
        description = spec["description"]

        print(f"\n{'='*70}")
        print(f"Processing {symbol} {accession}")
        print(f"Date: {tx_date}")
        print(f"Description: {description}")
        print('='*70)

        if args.dry_run:
            logger.info(f"[{symbol}] DRY-RUN mode: will not write files")

        result = remediate_record(symbol, cik_int, accession, tx_date, cfg, skip_fetch=args.skip_fetch)
        all_results.append(result)

        print(f"\nStatus: {result['status'].upper()}")
        if result["before"]:
            print(f"BEFORE:")
            print(f"  action: {result['before'].get('action')}")
            print(f"  shares: {result['before'].get('shares')}")
            print(f"  price_usd: {result['before'].get('price_usd')}")
            print(f"  value_usd: {result['before'].get('value_usd')}")
            print(f"  breach: {result['before'].get('breach')}")
        if result["after"]:
            print(f"AFTER:")
            print(f"  action: {result['after'].get('action')}")
            print(f"  shares: {result['after'].get('shares')}")
            print(f"  price_usd: {result['after'].get('price_usd')}")
            print(f"  value_usd: {result['after'].get('value_usd')}")
            print(f"  market_shares: {result['after'].get('market_shares')}")
            print(f"  nonmarket_shares: {result['after'].get('nonmarket_shares')}")
            print(f"  tx_codes: {result['after'].get('tx_codes')}")

        if args.dry_run:
            print("DRY-RUN: Files NOT written")
        else:
            print(f"Files updated: {len(result['files_updated'])}")
            for fpath in result['files_updated']:
                print(f"  - {fpath}")

    print(f"\n{'='*70}")
    print("REMEDIATION SUMMARY")
    print('='*70)
    for result in all_results:
        status = result["status"]
        symbol = result["symbol"]
        accession = result["accession"]
        files = len(result["files_updated"])
        print(f"{symbol:6s} {accession} → {status:12s} ({files} files)")

    if args.dry_run:
        print("\nDRY-RUN mode: No files were actually written.")
    else:
        print("\nRemediation complete. Run rebuild_m7_history.py if you want to regenerate per-symbol history files.")


if __name__ == "__main__":
    main()
