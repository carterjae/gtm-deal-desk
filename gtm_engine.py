#!/usr/bin/env python3
"""
gtm_engine.py — Enterprise Deal Desk Orchestrator (Clay -> Gemini 3.8 Structured JSON + Jinja2 -> Notion API)
Ingests staged JSON payloads from Clay, synthesizes executive deal intelligence via Google GenAI SDK
with strict Pydantic JSON Schema validation, renders Jinja2 block templates into native Notion blocks,
and pushes clean, emoji-free dossiers to Notion.
"""

import argparse
from enum import Enum
import json
import os
import re
import sys
from pathlib import Path
from google import genai
from google.genai import types
from jinja2 import Environment, FileSystemLoader
from pydantic import BaseModel, Field, ValidationError
import requests
import yaml

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.yaml"
TEMPLATES_DIR = BASE_DIR / "templates"
STAGING_DIR = BASE_DIR / "staging"


class MeddicScoreEnum(str, Enum):
    LOW_RISK = "Low Risk"
    MODERATE_RISK = "Moderate Risk"
    HIGH_RISK = "High Risk"


class DealIntelligenceSchema(BaseModel):
    """Strict Structured JSON Schema for Gemini Deal Desk Synthesis."""

    challenger_thesis: str = Field(
        ...,
        min_length=20,
        description="A sharp Challenger 'Why Now' Commercial Thesis (1 concise paragraph, no fluff).",
    )
    executive_insight: str = Field(
        ...,
        min_length=20,
        description="A distinct 1-2 sentence executive takeaway for the Economic Buyer quantifying strategic impact.",
    )
    meddic_red_flags: str = Field(
        ...,
        min_length=15,
        description="Primary MEDDICC Red Flags (Economic Buyer access, Metrics, or Decision Criteria hurdles).",
    )
    estimated_arr: int = Field(
        ...,
        ge=10000,
        le=1000000,
        description="Estimated ARR potential in USD based on tech footprint and headcount.",
    )
    meddic_score: MeddicScoreEnum = Field(
        ...,
        description="Overall MEDDICC risk classification: Low Risk, Moderate Risk, or High Risk.",
    )


def load_config() -> dict:
    for env_path in [BASE_DIR / ".env", BASE_DIR.parent / ".env"]:
        if env_path.exists():
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

    if "NOTION_API_KEY" not in os.environ and "NOTION_TOKEN" in os.environ:
        os.environ["NOTION_API_KEY"] = os.environ["NOTION_TOKEN"]

    if not CONFIG_PATH.exists():
        print(f"Error: {CONFIG_PATH} not found.")
        sys.exit(1)

    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        expanded = os.path.expandvars(f.read())
        return yaml.safe_load(expanded)


def generate_deal_intelligence(company_data: dict, config: dict) -> DealIntelligenceSchema:
    """
    Uses Gemini inside Antigravity via Google GenAI SDK to synthesize raw Clay signals
    into strictly validated DealIntelligenceSchema JSON.
    """
    api_key = os.environ.get("GEMINI_API_KEY") or config.get("llm", {}).get("api_key", "")
    model_name = config.get("llm", {}).get("model", "gemini-3.8-flash").removeprefix("models/")

    prompt = f"""
    You are an Enterprise Account Executive at {config.get('hiring_company', {}).get('name', 'Braintrust')} running a deal desk evaluation.
    Analyze the following raw signals extracted from Clay for {company_data.get('company_name')}:

    Firmographics: {company_data.get('firmographics', f"Domain: {company_data.get('domain')}, ~{company_data.get('ai_eng_count')} AI Engineers")}
    Tech Stack: {company_data.get('technographics', f"AI Product: {company_data.get('ai_product')}, Security: {company_data.get('security_trigger')}")}
    Hiring / Growth Signals: {company_data.get('growth_signals', company_data.get('oss_hiring_signal'))}
    Trigger Events: {company_data.get('recent_events', company_data.get('top_down_trigger'))}
    Quantified Waste: {company_data.get('annual_eval_tax')}

    Synthesize this into:
    1. challenger_thesis: A sharp Challenger 'Why Now' Commercial Thesis (1 concise paragraph, peer-to-peer executive tone, no fluff, no emojis).
    2. executive_insight: A distinct 1-2 sentence strategic takeaway for {company_data.get('economic_buyer')} explaining why automated CI/CD evaluation gates unlock enterprise deal velocity.
    3. meddic_red_flags: Primary MEDDICC Red Flags (Economic Buyer access, Metrics, or Status Quo DIY Decision Criteria hurdles).
    4. estimated_arr: Estimated ARR potential in USD based on tech footprint and headcount.
    5. meddic_score: MEDDICC risk classification ("Low Risk", "Moderate Risk", or "High Risk").
    """

    if api_key and not api_key.startswith("${"):
        try:
            client = genai.Client(api_key=api_key)
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.3,
                    response_mime_type="application/json",
                    response_schema={
                        "type": "OBJECT",
                        "properties": {
                            "challenger_thesis": {"type": "STRING"},
                            "executive_insight": {"type": "STRING"},
                            "meddic_red_flags": {"type": "STRING"},
                            "estimated_arr": {"type": "INTEGER"},
                            "meddic_score": {
                                "type": "STRING",
                                "enum": ["Low Risk", "Moderate Risk", "High Risk"],
                            },
                        },
                        "required": [
                            "challenger_thesis",
                            "executive_insight",
                            "meddic_red_flags",
                            "estimated_arr",
                            "meddic_score",
                        ],
                    },
                ),
            )
            validated = DealIntelligenceSchema.model_validate_json(response.text)
            print(f"[*] Gemini ({model_name}) structured JSON synthesis validated for '{company_data.get('company_name')}'.")
            return validated
        except ValidationError as ve:
            print(f"[!] Gemini response failed Pydantic schema validation: {ve}")
        except Exception as e:
            print(f"[!] Gemini API call fallback triggered ({e}); running deterministic schema validation.")

    raw_score = company_data.get("meddic_score", "Moderate Risk")
    normalized_score = (
        "Low Risk"
        if "Strong" in raw_score or "Low" in raw_score
        else ("High Risk" if "High" in raw_score else "Moderate Risk")
    )
    fallback_payload = {
        "challenger_thesis": (
            company_data.get("challenger_thesis")
            or (
                f"{company_data.get('company_name')} cannot scale {company_data.get('ai_product', 'production AI')} "
                f"safely while diverting {company_data.get('annual_eval_tax', '$1.44M/yr')} across "
                f"~{company_data.get('ai_eng_count', 15)} AI engineers to manual spreadsheet grading and bespoke "
                f"evaluation scripts."
            )
        ),
        "executive_insight": (
            f"For {company_data.get('economic_buyer')}, replacing ad-hoc manual testing with automated CI/CD evaluation "
            f"gates reclaims {company_data.get('wasted_ftes', 4.5)} Senior AI FTEs for core product roadmap execution "
            f"while eliminating silent production regressions."
        ),
        "meddic_red_flags": (
            company_data.get("meddic_red_flags")
            or (
                f"Internal DIY Status Quo: {company_data.get('company_name')} senior engineers maintaining ad-hoc "
                f"Python eval scripts internally without executive visibility into the {company_data.get('annual_eval_tax')} capacity drain."
            )
        ),
        "estimated_arr": int(company_data.get("estimated_arr", 75000)),
        "meddic_score": normalized_score,
    }
    return DealIntelligenceSchema.model_validate(fallback_payload)


def parse_rich_text(line: str) -> list[dict]:
    """Parses **bold** segments into structured Notion rich_text spans with zero raw markdown asterisks, chunking at 1,990c."""
    MAX_NOTION_CHUNK = 1990
    parts = re.split(r"(\*\*.*?\*\*)", line)
    rich_text = []
    for part in parts:
        if not part:
            continue
        if part.startswith("**") and part.endswith("**"):
            clean = part[2:-2]
            for i in range(0, len(clean), MAX_NOTION_CHUNK):
                rich_text.append({
                    "type": "text",
                    "text": {"content": clean[i : i + MAX_NOTION_CHUNK]},
                    "annotations": {"bold": True},
                })
        else:
            for i in range(0, len(part), MAX_NOTION_CHUNK):
                rich_text.append({
                    "type": "text",
                    "text": {"content": part[i : i + MAX_NOTION_CHUNK]},
                })
    return rich_text


def markdown_to_notion_blocks(md_text: str) -> list[dict]:
    """Converts rendered Jinja2 Markdown into clean, structured, emoji-free Notion API blocks."""
    blocks = []
    lines = md_text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        if not line.strip():
            i += 1
            continue

        if line.startswith("```"):
            lang_hint = line[3:].strip().lower()
            notion_lang = "mermaid" if lang_hint == "mermaid" else "plain text"
            code_lines = []
            i += 1
            while i < len(lines) and not lines[i].rstrip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            i += 1  # skip closing ```
            blocks.append({
                "object": "block",
                "type": "code",
                "code": {
                    "language": notion_lang,
                    "rich_text": [{"type": "text", "text": {"content": "\n".join(code_lines).strip()[:1950]}}],
                },
            })
            continue

        if line.startswith("### "):
            blocks.append({
                "object": "block",
                "type": "heading_3",
                "heading_3": {"rich_text": parse_rich_text(line[4:].strip())},
            })
        elif line.startswith("## "):
            blocks.append({
                "object": "block",
                "type": "heading_2",
                "heading_2": {"rich_text": parse_rich_text(line[3:].strip())},
            })
        elif line.startswith("> "):
            blocks.append({
                "object": "block",
                "type": "quote",
                "quote": {"rich_text": parse_rich_text(line[2:].strip())},
            })
        elif line.startswith("- ") or line.startswith("* "):
            blocks.append({
                "object": "block",
                "type": "bulleted_list_item",
                "bulleted_list_item": {"rich_text": parse_rich_text(line[2:].strip())},
            })
        else:
            blocks.append({
                "object": "block",
                "type": "paragraph",
                "paragraph": {"rich_text": parse_rich_text(line.strip())},
            })
        i += 1

    return blocks


def resolve_database_id(config: dict) -> str:
    """Ensures database_id resolves to a Notion database (auto-discovering inline child_database if a page_id was passed)."""
    db_id = config["notion"]["database_id"]
    headers = {
        "Authorization": f"Bearer {config['notion']['api_key']}",
        "Notion-Version": config["notion"]["version"],
    }
    resp = requests.get(f"https://api.notion.com/v1/databases/{db_id}", headers=headers, timeout=10)
    if resp.status_code == 200:
        return db_id

    blocks_resp = requests.get(f"https://api.notion.com/v1/blocks/{db_id}/children", headers=headers, timeout=10)
    if blocks_resp.status_code == 200:
        for block in blocks_resp.json().get("results", []):
            if block.get("type") == "child_database":
                return block["id"]
    return db_id


def build_notion_payload(company_data: dict, config: dict) -> dict:
    """Transforms enriched Clay data + validated Gemini DealIntelligenceSchema + Jinja2 templates into Notion blocks."""
    database_id = resolve_database_id(config)

    intel: DealIntelligenceSchema = generate_deal_intelligence(company_data, config)
    model_name = config.get("llm", {}).get("model", "gemini-3.8-flash").removeprefix("models/")

    render_ctx = {
        **company_data,
        "challenger_thesis": intel.challenger_thesis,
        "executive_insight": intel.executive_insight,
        "meddic_red_flags": intel.meddic_red_flags,
        "estimated_arr": intel.estimated_arr,
        "meddic_score": intel.meddic_score.value,
    }

    jinja_env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)), autoescape=False)
    challenger_md = jinja_env.get_template("challenger_thesis.j2").render(**render_ctx).strip()
    meddicc_md = jinja_env.get_template("meddicc_risks.j2").render(**render_ctx).strip()
    com_matrix_md = jinja_env.get_template("com_matrix.j2").render(**render_ctx).strip()

    mermaid_arch = (
        "flowchart LR\n"
        f"    A[\"Clay Signal: {render_ctx.get('company_name')} (~{render_ctx.get('ai_eng_count', 15)} AI Engrs)\"] "
        f"--> B[\"0-Credit ROI Math: {render_ctx.get('annual_eval_tax', '$1.44M/yr')}\"]\n"
        f"    B --> C[\"Gemini ({model_name}) Structured JSON Schema\"]\n"
        "    C --> D[(\"Notion Deal Desk & 14-Day Braintrust POV\")]"
    )

    children_blocks = []

    # Section 1: Commercial Thesis ("Why Now?")
    children_blocks.append({
        "object": "block",
        "type": "heading_2",
        "heading_2": {"rich_text": [{"type": "text", "text": {"content": "Commercial Thesis (\"Why Now?\")"}}]},
    })
    children_blocks.extend(markdown_to_notion_blocks(challenger_md))
    children_blocks.append({"object": "block", "type": "divider", "divider": {}})

    # Section 2: MEDDICC Red Flags & Deal Risk
    children_blocks.append({
        "object": "block",
        "type": "heading_2",
        "heading_2": {"rich_text": [{"type": "text", "text": {"content": "MEDDICC Red Flags & Deal Risk"}}]},
    })
    children_blocks.extend(markdown_to_notion_blocks(meddicc_md))
    children_blocks.append({"object": "block", "type": "divider", "divider": {}})

    # Section 3: Command of the Message Matrix & Track A Outbound
    children_blocks.append({
        "object": "block",
        "type": "heading_2",
        "heading_2": {"rich_text": [{"type": "text", "text": {"content": "Command of the Message (CoM) Value Matrix"}}]},
    })
    children_blocks.extend(markdown_to_notion_blocks(com_matrix_md))
    children_blocks.append({"object": "block", "type": "divider", "divider": {}})

    # Section 4: Pipeline Architecture
    children_blocks.append({
        "object": "block",
        "type": "heading_3",
        "heading_3": {"rich_text": [{"type": "text", "text": {"content": f"Clay -> Gemini ({model_name}) -> Notion Architecture"}}]},
    })
    children_blocks.append({
        "object": "block",
        "type": "code",
        "code": {
            "language": "mermaid",
            "rich_text": [{"type": "text", "text": {"content": mermaid_arch}}],
        },
    })

    payload = {
        "parent": {"database_id": database_id},
        "properties": {
            "Company Name": {
                "title": [{"text": {"content": render_ctx.get("company_name", "Unknown")}}]
            },
            "Domain": {"url": render_ctx.get("domain", "https://example.com")},
            "Deal Size": {"number": intel.estimated_arr},
            "Stage": {"select": {"name": render_ctx.get("stage", "Prospecting")}},
            "MEDDICC Health": {"select": {"name": intel.meddic_score.value}},
            "Annual Eval Tax (ROI)": {
                "rich_text": [{"text": {"content": render_ctx.get("annual_eval_tax", "$1,440,000/yr")}}]
            },
            "AI Product Wedge": {
                "rich_text": [{"text": {"content": render_ctx.get("ai_product", "Production AI Agent")}}]
            },
            "Economic Buyer": {
                "rich_text": [{"text": {"content": render_ctx.get("economic_buyer", "VP of Engineering")}}]
            },
            "Champion": {
                "rich_text": [{"text": {"content": render_ctx.get("champion", "Lead AI Architect")}}]
            },
            "OSS / Hiring Signal": {
                "rich_text": [{"text": {"content": render_ctx.get("oss_hiring_signal", "Active AI Hiring Signal")}}]
            },
            "Security / VPC Trigger": {
                "select": {"name": render_ctx.get("security_trigger", "SOC2 + Regulated")}
            },
        },
        "children": children_blocks,
    }
    return payload


def push_to_notion(payload: dict, config: dict) -> str:
    headers = {
        "Authorization": f"Bearer {config['notion']['api_key']}",
        "Content-Type": "application/json",
        "Notion-Version": config["notion"]["version"],
    }
    url = "https://api.notion.com/v1/pages"
    response = requests.post(url, headers=headers, json=payload, timeout=15)
    if response.status_code == 200:
        page_url = response.json().get("url", "")
        title = payload["properties"]["Company Name"]["title"][0]["text"]["content"]
        print(f"[+] Successfully created Notion Deal Desk page for '{title}' -> {page_url}")
        return page_url
    else:
        print(f"[!] Failed to sync with Notion ({response.status_code}): {response.text}")
        return ""


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="GTM Deal Desk Orchestrator (Native Gemini 3.8 + Pydantic Schema Edition)")
    parser.add_argument("--all", action="store_true", help="Sync all staged territory accounts in staging/")
    parser.add_argument("--file", type=str, default="", help="Specific staged JSON file to sync")
    args = parser.parse_args()

    config = load_config()

    if args.all:
        staged_files = sorted(
            p for p in STAGING_DIR.glob("*.json") if p.name != "sample_target.json"
        )
        if not staged_files:
            print("No staged files found. Run `python3 webhook_receiver.py --simulate` first.")
            sys.exit(1)
        for p in staged_files:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            notion_payload = build_notion_payload(data, config)
            push_to_notion(notion_payload, config)
    else:
        target_path = Path(args.file) if args.file else (STAGING_DIR / "sample_target.json")
        if target_path.exists():
            with open(target_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            notion_payload = build_notion_payload(data, config)
            push_to_notion(notion_payload, config)
        else:
            print("No staging payload found. Run `python3 webhook_receiver.py --simulate` first.")
