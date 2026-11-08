#!/usr/bin/env python3
"""Notion-first auto-poller that watches for new Deal Desk rows and enriches them via Gemini."""

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
import requests
import yaml
from dotenv import load_dotenv

load_dotenv()

from schema import ClayInputData
from synthesizer import load_seller_profile, synthesize_account
from notion_client import get_database_schema, update_existing_deal_desk_page


POLL_INTERVAL_SECONDS = 20
CONFIG_PATH = Path("config.yaml")
STAGING_DIR = Path("staging")


def load_config(config_path: Path = CONFIG_PATH) -> Dict[str, Any]:
    """Loads config.yaml and expands environment variables."""
    load_dotenv()
    if not config_path.exists():
        return {"notion": {"version": "2022-06-28"}, "llm": {"model": "gemini-2.5-flash", "temperature": 0.2}}
    raw = config_path.read_text(encoding="utf-8")
    return yaml.safe_load(os.path.expandvars(raw)) or {}


def _extract_title(props: Dict[str, Any]) -> str:
    """Extracts the plain_text title from a Notion page's properties."""
    for prop_meta in props.values():
        if prop_meta.get("type") == "title":
            title_items = prop_meta.get("title", [])
            return "".join(item.get("plain_text", "") for item in title_items).strip()
    return ""


def _extract_selling_for(props: Dict[str, Any]) -> Optional[str]:
    """Extracts the 'Selling For' select or rich_text value from a Notion page if present."""
    for col_name in ("Selling For", "Seller"):
        if col_name in props:
            col = props[col_name]
            col_type = col.get("type")
            if col_type == "select" and col.get("select"):
                return col["select"].get("name")
            if col_type == "rich_text" and col.get("rich_text"):
                return "".join(t.get("plain_text", "") for t in col["rich_text"]).strip()
    return None


def _extract_domain(props: Dict[str, Any]) -> Optional[str]:
    """Extracts the 'Domain' URL from a Notion page if already entered by the user."""
    if "Domain" in props:
        col = props["Domain"]
        if col.get("type") == "url" and col.get("url"):
            return col["url"]
    return None


def _build_clay_input_for_row(company_name: str, domain: Optional[str]) -> ClayInputData:
    """Builds a ClayInputData payload, enriching from local staging/<slug>.json if available."""
    slug = re.sub(r"[^a-z0-9]+", "_", company_name.strip().lower()).strip("_")
    staged_candidates = [
        STAGING_DIR / f"{slug}.json",
        STAGING_DIR / "sample_target.json",
    ]
    for candidate in staged_candidates:
        if candidate.exists():
            try:
                data = json.loads(candidate.read_text(encoding="utf-8"))
                if str(data.get("company_name", "")).strip().lower() == company_name.strip().lower():
                    if domain and not data.get("domain"):
                        data["domain"] = domain
                    return ClayInputData.model_validate(data)
            except Exception:
                pass

    return ClayInputData(
        company_name=company_name,
        domain=domain,
        github_org=slug if not domain else None,
        has_job_post=True,
        ai_engineer_count=12,
    )


def query_unprocessed_pages(database_id: str, headers: Dict[str, str], wedge_col_type: str = "rich_text") -> List[Dict[str, Any]]:
    """Queries the Notion database for pages where 'AI Product Wedge' is empty."""
    url = f"https://api.notion.com/v1/databases/{database_id}/query"
    filter_payload: Dict[str, Any] = {
        "filter": {
            "property": "AI Product Wedge",
            wedge_col_type: {"is_empty": True},
        }
    }
    resp = requests.post(url, headers=headers, json=filter_payload, timeout=20)
    if resp.status_code != 200:
        raise RuntimeError(f"Notion database query failed ({resp.status_code}): {resp.text}")
    return resp.json().get("results", [])


def poll_once(config: Dict[str, Any], force_offline: bool = False) -> int:
    """Executes a single polling pass across unprocessed Notion rows."""
    load_dotenv()
    api_key = os.environ.get("NOTION_API_KEY") or os.environ.get("NOTION_TOKEN") or ""
    database_id = os.environ.get("NOTION_DATABASE_ID") or ""

    if not api_key or not database_id:
        raise RuntimeError("NOTION_API_KEY and NOTION_DATABASE_ID must be set in .env")

    version = config.get("notion", {}).get("version", "2022-06-28")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Notion-Version": version,
    }

    db_schema = get_database_schema(database_id, headers)
    wedge_col_type = db_schema.get("AI Product Wedge", {}).get("type", "rich_text")
    pages = query_unprocessed_pages(database_id, headers, wedge_col_type=wedge_col_type)

    enriched_count = 0
    for page in pages:
        page_id = page.get("id")
        props = page.get("properties", {})
        company_name = _extract_title(props)

        # Skip blank rows where Company Name has not been filled in yet
        if not page_id or not company_name:
            continue

        selling_for_raw = _extract_selling_for(props)
        seller_profile = load_seller_profile(seller_override=selling_for_raw)
        seller_display_name = seller_profile.get("company_name", "Vanta")

        domain = _extract_domain(props)
        target = _build_clay_input_for_row(company_name, domain)

        synthesis = synthesize_account(
            target=target,
            config=config,
            force_offline=force_offline,
            seller_override=selling_for_raw,
        )

        update_existing_deal_desk_page(
            page_id=page_id,
            target=target,
            synthesis=synthesis,
            config=config,
            seller_name=seller_display_name,
            db_schema=db_schema,
        )

        print(f"[+] Enriched {company_name} for {seller_display_name} directly in Notion", flush=True)
        enriched_count += 1

    return enriched_count


def run_poller(interval: int = POLL_INTERVAL_SECONDS, once: bool = False, force_offline: bool = False) -> None:
    """Runs the continuous Notion auto-poller loop every 20 seconds with resilient error handling."""
    config = load_config()
    print(
        f"[*] Notion Auto-Poller started (Interval: {interval}s | Database: {os.environ.get('NOTION_DATABASE_ID')})",
        flush=True,
    )

    while True:
        try:
            poll_once(config=config, force_offline=force_offline)
        except KeyboardInterrupt:
            print("\n[*] Stopping Notion Auto-Poller...", flush=True)
            break
        except Exception as exc:
            print(f"[!] Transient error in Notion poller loop: {exc}", file=sys.stderr, flush=True)

        if once:
            break

        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            print("\n[*] Stopping Notion Auto-Poller...", flush=True)
            break


def main() -> None:
    parser = argparse.ArgumentParser(description="Notion-first Deal Desk Auto-Poller")
    parser.add_argument("--once", action="store_true", help="Run a single polling check and exit")
    parser.add_argument("--offline", action="store_true", help="Use deterministic fallback synthesis")
    parser.add_argument(
        "--interval",
        type=int,
        default=POLL_INTERVAL_SECONDS,
        help=f"Polling interval in seconds (default: {POLL_INTERVAL_SECONDS})",
    )
    args = parser.parse_args()
    run_poller(interval=args.interval, once=args.once, force_offline=args.offline)


if __name__ == "__main__":
    main()
