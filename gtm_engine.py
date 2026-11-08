import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional
import yaml
import json
import urllib.request
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(PROJECT_ROOT / ".env")

NOTION_TOKEN = os.environ.get("NOTION_TOKEN") or os.environ.get("NOTION_API_KEY") or ""
NOTION_DATABASE_ID = (
    os.environ.get("NOTION_DEAL_DESK_DB_ID")
    or os.environ.get("NOTION_DATABASE_ID")
    or ""
)


def load_config():
    """Loads whichever company config is currently active."""
    config_path = PROJECT_ROOT / "target_company.yml"
    if not config_path.exists():
        print(f"Error: {config_path} not found. Antigravity agent will generate one.")
        sys.exit(1)
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def compute_dynamic_roi(pain_type: str, headcount: int) -> dict:
    """Dynamically calculates the correct financial drain based on the company's product category."""
    if pain_type == "compliance_tax":  # e.g., Vanta
        hours = headcount * 50
        cost = hours * 150
        return {"metric_str": f"${cost:,} per audit cycle ({hours} engr hours)", "label": "Compliance & Audit Tax"}
    elif pain_type == "plumbing_tax":  # e.g., LangChain
        fte = round(headcount * 0.35, 1)
        cost = int(fte * 240000)
        return {"metric_str": f"${cost:,}/yr ({fte} Senior AI FTEs)", "label": "Agent Plumbing Tax"}
    elif pain_type == "eval_tax":  # e.g., Braintrust
        fte = round(headcount * 0.30, 1)
        cost = int(fte * 240000)
        return {"metric_str": f"${cost:,}/yr ({fte} Senior AI FTEs)", "label": "AI Eval & Regression Tax"}
    else:
        return {"metric_str": f"{headcount * 20} hours/mo in manual overhead", "label": "Operational Drag"}


def trigger_mode_a_deal_desk_blitz(
    seller_config: Dict[str, Any],
    dry_run: bool = False,
    force_offline: bool = False,
) -> Dict[str, Any]:
    """
    Upgrade #3: Mode B -> Mode A Cross-Trigger.
    Accepts a dynamic seller_config representing the hiring startup:
      - hiring_company (str)
      - product_summary (str)
      - pricing_tier (int, optional)
      - target_account (str: sample_icp_target_company)
      - target_domain (str: sample_icp_target_domain)
    Synthesizes a full MEDDICC + Command of the Message + Challenger Account Plan
    and writes it to NOTION_DEAL_DESK_DB_ID, returning the created Notion page URL.
    """
    from schema import ClayInputData
    from synthesizer import synthesize_account
    from notion_client import build_database_properties, build_notion_blocks, get_database_schema
    import requests
    import copy

    load_dotenv(PROJECT_ROOT / ".env")
    hiring_company = seller_config.get("hiring_company", "Target Seller")
    target_account = seller_config.get("target_account", "Stripe")
    target_domain = seller_config.get("target_domain", "stripe.com")
    product_summary = seller_config.get("product_summary", f"{hiring_company} Enterprise Platform")

    print(
        f"[*] [Upgrade #3 Blitz] Cross-triggering Mode A Deal Desk for Seller '{hiring_company}' "
        f"-> Target ICP '{target_account}' ({target_domain})..."
    )

    target_input = ClayInputData(
        company_name=target_account,
        domain=target_domain,
        github_org=target_account.lower().replace(" ", ""),
        has_job_post=True,
        ai_engineer_count=45,
        ai_product_wedge=product_summary[:200],
    )

    cfg = {
        "notion": {
            "version": "2022-06-28",
            "database_id": os.environ.get("NOTION_DEAL_DESK_DB_ID") or os.environ.get("NOTION_DATABASE_ID") or "",
            "api_key": os.environ.get("NOTION_TOKEN") or os.environ.get("NOTION_API_KEY") or "",
        },
        "llm": {
            "model": "gemini-2.5-flash",
            "temperature": 0.2,
            "api_key": os.environ.get("GEMINI_API_KEY", ""),
        },
    }

    try:
        synthesis = synthesize_account(
            target=target_input,
            config=cfg,
            force_offline=force_offline,
            seller_override=hiring_company,
        )
    except Exception as synth_err:
        print(f"[!] Mode A Blitz LLM spike ({synth_err}); using deterministic enterprise synthesis fallback...")
        synthesis = synthesize_account(
            target=target_input,
            config=cfg,
            force_offline=True,
            seller_override=hiring_company,
        )

    if dry_run:
        return {
            "status": "dry_run",
            "hiring_company": hiring_company,
            "target_account": target_account,
            "url": f"https://www.notion.so/dry-run-{target_account.lower()}",
        }

    api_key = cfg["notion"]["api_key"].strip().strip('"').strip("'")
    database_id = cfg["notion"]["database_id"].strip().strip('"').strip("'")
    if not api_key or not database_id:
        raise RuntimeError("Missing NOTION_TOKEN or NOTION_DEAL_DESK_DB_ID in .env")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28",
    }

    db_schema = get_database_schema(database_id, headers)
    properties = build_database_properties(
        target_input,
        synthesis,
        db_schema,
        seller_name=hiring_company,
    )
    children = build_notion_blocks(target_input, synthesis)

    api_children = copy.deepcopy(children)
    deferred_toggle_tables = []
    for block in api_children:
        if block.get("type") == "toggle":
            toggle_children = block["toggle"].get("children", [])
            if any(c.get("type") == "table" for c in toggle_children):
                deferred_toggle_tables.append(toggle_children)
                block["toggle"]["children"] = []
            else:
                deferred_toggle_tables.append([])

    payload = {
        "parent": {"database_id": database_id},
        "properties": properties,
        "children": api_children,
    }

    resp = requests.post(
        "https://api.notion.com/v1/pages",
        headers=headers,
        json=payload,
        timeout=25,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Mode A Deal Desk Notion creation failed ({resp.status_code}): {resp.text}")

    resp_json = resp.json()
    page_id = resp_json.get("id")
    page_url = resp_json.get("url", "")

    if page_id and any(deferred_toggle_tables):
        blocks_resp = requests.get(
            f"https://api.notion.com/v1/blocks/{page_id}/children?page_size=100",
            headers=headers,
            timeout=15,
        )
        if blocks_resp.status_code == 200:
            created_toggles = [
                b for b in blocks_resp.json().get("results", []) if b.get("type") == "toggle"
            ]
            for toggle_obj, deferred in zip(created_toggles, deferred_toggle_tables):
                if deferred:
                    requests.patch(
                        f"https://api.notion.com/v1/blocks/{toggle_obj['id']}/children",
                        headers=headers,
                        json={"children": deferred},
                        timeout=15,
                    )

    print(f"[+] [Upgrade #3 Blitz] Created Mode A Deal Desk Account Plan -> {page_url}")
    return {
        "status": "created",
        "page_id": page_id,
        "url": page_url,
        "hiring_company": hiring_company,
        "target_account": target_account,
    }


def run_universal_sync():
    config = load_config()
    roi = compute_dynamic_roi(config["pain_point_type"], config["icp_headcount"])

    print(f"[*] Running GTM Engine for Hiring Company: {config['hiring_company']}")
    print(f"[*] Target Account: {config['target_account']} | Computed ROI: {roi['metric_str']}")

    if config["pain_point_type"] == "plumbing_tax":
        ai_prod = config.get("ai_product", f"{config['target_account']} Intelligence")
        eb_name = config.get("eb_first_name", "Team")
        track_a_email = (
            f"Subject: {ai_prod} velocity / the 35% agent plumbing tax\n\n"
            f"{eb_name} — Scaling {ai_prod} into autonomous execution is a massive wedge for {config['target_account']}. "
            f"However, with ~{config['icp_headcount']} AI engineers building on LangGraph, roughly {roi['metric_str']} in senior engineering capacity "
            f"typically gets swallowed maintaining custom Postgres state checkpointers and manual eval pipelines.\n\n"
            f"Klarna and Moody's deploy LangSmith + LangGraph Enterprise inside their VPC to cut agent release cycles by 3x. "
            f"Open to a 12-minute brief on how they structured their production eval gates?"
        )
        commercial_thesis = f"Commercial Thesis: {config['target_account']} cannot scale {ai_prod} safely while diverting {roi['metric_str']} of senior AI engineering capacity to DIY state checkpointers and manual trace debugging."
    elif config["pain_point_type"] == "eval_tax":
        ai_prod = config.get("ai_product", f"{config['target_account']} AI")
        eb_name = config.get("eb_first_name", "Team")
        track_a_email = (
            f"Subject: {ai_prod} velocity / the 30% vibes-based eval tax\n\n"
            f"{eb_name} — Scaling {ai_prod} into autonomous workflows is a massive revenue driver for {config['target_account']}. "
            f"However, across ~{config['icp_headcount']} AI engineers, roughly {roi['metric_str']} in senior engineering capacity "
            f"typically gets swallowed firefighting prompt regressions, grading outputs in spreadsheets, and building custom eval scripts.\n\n"
            f"Notion and Dropbox standardized on Braintrust to run 10,000+ automated regression evals in CI/CD and deploy prompt updates 10x faster with zero vibes. "
            f"Open to a 12-minute brief on how they automated their production eval gates?"
        )
        commercial_thesis = f"Commercial Thesis: {config['target_account']} cannot scale {ai_prod} safely while diverting {roi['metric_str']} of senior AI engineering capacity to manual spreadsheet grading and bespoke prompt evaluation scaffolding."
    elif config["pain_point_type"] == "compliance_tax":
        track_a_email = (
            f"Subject: Security reviews / {config['target_account']} enterprise expansion\n\n"
            f"Scaling {config['target_account']}'s enterprise sales pipeline is tough when prospects stall deals over 300-item security questionnaires and compliance reviews.\n\n"
            f"Most engineering leaders we speak with find their team burns {roi['metric_str']} manually collecting evidence across AWS and GitHub for auditors instead of shipping product.\n\n"
            f"{config['hiring_company']} automates 90% of that evidence gathering so your engineers get their sprints back and deals close 3x faster. Open to a 12-minute brief?"
        )
        commercial_thesis = f"Commercial Thesis: {config['target_account']} cannot scale enterprise revenue while relying on manual compliance processes that cost them {roi['metric_str']}."
    else:
        track_a_email = (
            f"Subject: Operational drag / {config['target_account']} efficiency\n\n"
            f"Engineering teams at {config['target_account']} currently absorb {roi['metric_str']} in manual operational overhead. "
            f"{config['hiring_company']} automates this workflow directly. Open to a 12-minute brief?"
        )
        commercial_thesis = f"Commercial Thesis: {config['target_account']} is burning {roi['metric_str']} on manual processes."

    mermaid_code = (
        "flowchart LR\n"
        '    n0(["Trigger: OSS LangGraph / Job Post Detection"]) --> n1[["0-Credit Python ROI Engine: '
        + roi["metric_str"]
        + '"]]\n'
        '    n1 --> n2[["Challenger Reframe: The 35% Plumbing Tax"]]\n'
        '    n2 --> n3[("Notion Deal Desk & Outbound Sequence")]'
    )

    payload = {
        "parent": {"database_id": NOTION_DATABASE_ID},
        "properties": {
            "Company Name": {"title": [{"text": {"content": config["target_account"]}}]},
            "Economic Buyer": {"rich_text": [{"text": {"content": config["economic_buyer"]}}]},
            "Champion": {"rich_text": [{"text": {"content": config["champion"]}}]},
        },
        "children": [
            {
                "object": "block",
                "type": "heading_2",
                "heading_2": {
                    "rich_text": [
                        {
                            "text": {
                                "content": f"1-Page Deal Desk & Outbound Dossier: {config['hiring_company']} -> {config['target_account']}"
                            }
                        }
                    ]
                },
            },
            {
                "object": "block",
                "type": "callout",
                "callout": {"rich_text": [{"text": {"content": commercial_thesis}}]},
            },
            {
                "object": "block",
                "type": "heading_3",
                "heading_3": {"rich_text": [{"text": {"content": "Track A: Economic Buyer Cold Email"}}]},
            },
            {
                "object": "block",
                "type": "code",
                "code": {
                    "language": "plain text",
                    "rich_text": [{"text": {"content": track_a_email}}],
                },
            },
            {
                "object": "block",
                "type": "heading_3",
                "heading_3": {"rich_text": [{"text": {"content": "Clay Signal & Enrichment Architecture"}}]},
            },
            {
                "object": "block",
                "type": "code",
                "code": {
                    "language": "mermaid",
                    "rich_text": [{"text": {"content": mermaid_code}}],
                },
            },
        ],
    }

    if not NOTION_TOKEN or not NOTION_DATABASE_ID:
        print("[!] Notion credentials missing. Skipping cloud sync (Dry run successful).")
        return

    req = urllib.request.Request(
        "https://api.notion.com/v1/pages",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {NOTION_TOKEN}",
            "Content-Type": "application/json",
            "Notion-Version": "2022-06-28",
        },
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        res = json.loads(resp.read().decode())
        print(f"[+] Successfully synced {config['target_account']} to Notion -> {res.get('url')}")


if __name__ == "__main__":
    run_universal_sync()
