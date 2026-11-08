#!/usr/bin/env python3
"""End-to-end CLI orchestrator for the Enterprise Deal Desk & Account Intelligence Hub."""

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List
import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent
if sys.path[0] != str(PROJECT_ROOT):
    sys.path.insert(0, str(PROJECT_ROOT))

load_dotenv(PROJECT_ROOT / ".env")

from schema import ClayInputData
from synthesizer import synthesize_account
from notion_client import push_deal_desk_page


RATE_LIMIT_SLEEP_SECONDS = 1.5


def load_config(config_path: Path) -> Dict[str, Any]:
    """Loads YAML configuration file and expands environment variables from os.environ."""
    load_dotenv()
    if not config_path.exists():
        raise FileNotFoundError(f"Configuration file not found: {config_path}")
    raw_yaml = config_path.read_text(encoding="utf-8")
    expanded_yaml = os.path.expandvars(raw_yaml)
    return yaml.safe_load(expanded_yaml)


def resolve_json_files(input_path: Path) -> List[Path]:
    """Resolves a single JSON file or all .json files inside a directory."""
    if not input_path.exists():
        raise FileNotFoundError(f"Staging path not found: {input_path}")

    if input_path.is_dir():
        files = sorted(p for p in input_path.glob("*.json") if p.is_file())
        if not files:
            raise ValueError(f"No .json files found in directory: {input_path}")
        return files

    return [input_path]


def process_single_file(
    file_path: Path,
    config: Dict[str, Any],
    index: int,
    total: int,
    dry_run: bool = False,
    force_offline: bool = False,
) -> Dict[str, Any]:
    """Validates a single Clay JSON payload, runs Gemini synthesis, and pushes to Notion."""
    raw_payload = json.loads(file_path.read_text(encoding="utf-8"))
    target = ClayInputData.model_validate(raw_payload)

    synthesis = synthesize_account(target, config, force_offline=force_offline)
    result = push_deal_desk_page(target, synthesis, config, dry_run=dry_run)

    if result["status"] == "created":
        print(f"[{index}/{total}] Synced: {target.company_name} -> Notion Page Created")
    else:
        print(f"[{index}/{total}] Synced: {target.company_name} -> Notion Page Created (Dry Run)")

    return result


def run_pipeline(
    input_path: Path,
    config_path: Path = Path("config.yaml"),
    dry_run: bool = False,
    force_offline: bool = False,
) -> List[Dict[str, Any]]:
    """Validates Clay input(s), runs Gemini (`gemini-2.5-flash`) synthesis, and pushes native blocks to Notion."""
    load_dotenv()
    config = load_config(config_path)
    json_files = resolve_json_files(input_path)
    total = len(json_files)
    results: List[Dict[str, Any]] = []

    for idx, file_path in enumerate(json_files, start=1):
        res = process_single_file(
            file_path=file_path,
            config=config,
            index=idx,
            total=total,
            dry_run=dry_run,
            force_offline=force_offline,
        )
        results.append(res)

        if idx < total:
            time.sleep(RATE_LIMIT_SLEEP_SECONDS)

    return results


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Enterprise Deal Desk GTM Engine (Clay JSON -> gemini-2.5-flash -> Notion API)"
    )
    parser.add_argument(
        "input",
        nargs="?",
        default="staging/sample_target.json",
        help="Path to a Clay JSON file or staging directory (e.g., staging/)",
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to config.yaml (default: config.yaml)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and synthesize without writing to the live Notion API",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Use deterministic schema-validated synthesis without calling external LLM API",
    )
    args = parser.parse_args()

    try:
        run_pipeline(
            input_path=Path(args.input),
            config_path=Path(args.config),
            dry_run=args.dry_run,
            force_offline=args.offline,
        )
    except Exception as exc:
        print(f"[!] Pipeline execution failed: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
