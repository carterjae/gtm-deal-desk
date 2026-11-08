"""Notion API (v2022-06-28) client formatting Deal Desk synthesis into native properties, callouts, toggles, and bulleted lists."""

import copy
import os
import re
from typing import Any, Dict, List, Optional, Sequence
import requests
from dotenv import load_dotenv
from schema import ClayInputData, DealDeskSynthesis

load_dotenv()


DEFAULT_DEAL_DESK_SCHEMA: Dict[str, Dict[str, str]] = {
    "Company Name": {"type": "title"},
    "Selling For": {"type": "select"},
    "Domain": {"type": "url"},
    "Deal Size": {"type": "number"},
    "Stage": {"type": "select"},
    "MEDDICC Health": {"type": "select"},
    "AI Product Wedge": {"type": "rich_text"},
    "Annual Eval Tax (ROI)": {"type": "rich_text"},
    "Champion": {"type": "rich_text"},
    "Economic Buyer": {"type": "rich_text"},
    "OSS / Hiring Signal": {"type": "rich_text"},
    "Security / VPC Trigger": {"type": "select"},
}


def _rich_text(content: str, bold: bool = False) -> Dict[str, Any]:
    """Helper to construct a single Notion rich_text segment."""
    return {
        "type": "text",
        "text": {"content": str(content)[:2000]},
        "annotations": {"bold": bold},
    }


def _labeled_bullet(label: str, body: str) -> Dict[str, Any]:
    """Creates a native Notion bulleted_list_item block with a bold prefix label."""
    return {
        "object": "block",
        "type": "bulleted_list_item",
        "bulleted_list_item": {
            "rich_text": [
                _rich_text(f"{label}: ", bold=True),
                _rich_text(body, bold=False),
            ]
        },
    }


def _simple_bullet(body: str) -> Dict[str, Any]:
    """Creates a native Notion bulleted_list_item block."""
    return {
        "object": "block",
        "type": "bulleted_list_item",
        "bulleted_list_item": {
            "rich_text": [_rich_text(body, bold=False)]
        },
    }


def _callout_block(label: str, body: str, color: str = "gray_background") -> Dict[str, Any]:
    """Creates a native Notion callout block with bold title and background color."""
    return {
        "object": "block",
        "type": "callout",
        "callout": {
            "rich_text": [
                _rich_text(f"{label}: ", bold=True),
                _rich_text(body, bold=False),
            ],
            "color": color,
        },
    }


def _toggle_block(title: str, children: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Creates a native Notion toggle block with nested child blocks."""
    return {
        "object": "block",
        "type": "toggle",
        "toggle": {
            "rich_text": [_rich_text(title, bold=True)],
            "children": children,
        },
    }


def _heading_2(title: str) -> Dict[str, Any]:
    """Creates a native Notion heading_2 block."""
    return {
        "object": "block",
        "type": "heading_2",
        "heading_2": {
            "rich_text": [_rich_text(title, bold=False)]
        },
    }


def _divider() -> Dict[str, Any]:
    """Creates a native Notion divider block."""
    return {"object": "block", "type": "divider", "divider": {}}


def _table_row(cells: Sequence[str], is_header: bool = False) -> Dict[str, Any]:
    """Creates a native Notion table_row block."""
    return {
        "object": "block",
        "type": "table_row",
        "table_row": {
            "cells": [[_rich_text(cell, bold=is_header)] for cell in cells]
        },
    }


def _table_block(rows: List[Dict[str, Any]], table_width: int = 4) -> Dict[str, Any]:
    """Creates a native Notion table block containing table_row children."""
    return {
        "object": "block",
        "type": "table",
        "table": {
            "table_width": table_width,
            "has_column_header": True,
            "has_row_header": False,
            "children": rows,
        },
    }


def _build_map_toggle(target: ClayInputData, synthesis: DealDeskSynthesis) -> Dict[str, Any]:
    """Builds the 'Mutual Action Plan (MAP) & Evaluation Milestones' toggle block containing a 4-column Notion table."""
    rows = [
        _table_row(
            [
                "Milestone / Stage",
                "Key Deliverable / Exit Criteria",
                "Joint Owner",
                "Target Date",
            ],
            is_header=True,
        ),
        _table_row(
            [
                "Scoping & Discovery",
                f"Baseline {synthesis.annual_evaluation_tax} friction across {target.ai_engineer_count} AI engineers and align on POC success criteria",
                f"AE / {synthesis.champion}",
                "T+7 Days",
            ]
        ),
        _table_row(
            [
                "Technical Validation & Security",
                f"Sandbox POC sign-off (CI/CD eval gates in {target.github_org or 'GitHub'}) & {synthesis.security_ypc} InfoSec/SOC2 audit",
                f"AE / {synthesis.champion} / InfoSec Lead",
                "T+21 Days",
            ]
        ),
        _table_row(
            [
                "Economic Alignment",
                f"Executive ROI sign-off on reclaiming {synthesis.annual_evaluation_tax} and commercial proposal review",
                f"AE / {synthesis.economic_buyer} / {synthesis.champion}",
                "T+35 Days",
            ]
        ),
        _table_row(
            [
                "Procurement & Legal",
                f"MSA, security addendum, order form execution, and production deployment for {target.company_name}",
                f"AE / {synthesis.economic_buyer} / Legal & Procurement",
                "T+45 Days",
            ]
        ),
    ]
    return _toggle_block(
        "Mutual Action Plan (MAP) & Evaluation Milestones",
        [_table_block(rows, table_width=4)],
    )


def _build_competitive_toggle(synthesis: DealDeskSynthesis) -> Dict[str, Any]:
    """Builds the 'Competitive Differentiation & Trap-Setting' toggle containing a callout box and trap questions."""
    bc = synthesis.competitive_battlecard
    questions_str = "\n".join(
        f"Trap Question {idx}: {q}" for idx, q in enumerate(bc.trap_setting_questions, start=1)
    )
    callout_body = (
        f"Primary Competitor / Alternative: {bc.competitor_name}\n\n"
        f"{questions_str}\n\n"
        f"Rebuttal Framework: {bc.objection_rebuttal}"
    )
    return _toggle_block(
        "Competitive Differentiation & Trap-Setting",
        [
            _callout_block(
                "Battlecard & Rebuttal Framework",
                f"\n{callout_body}",
                color="gray_background",
            )
        ],
    )


def _build_ramp_plan_toggle(synthesis: DealDeskSynthesis) -> Dict[str, Any]:
    """Builds the '30-60-90 Day Territory Ramp Plan' toggle containing a native Notion milestone table."""
    rm = synthesis.ramp_milestones
    rows = [
        _table_row(
            ["Ramp Horizon", "Territory & Account Execution Milestones"],
            is_header=True,
        ),
        _table_row(["First 30 Days (Foundation & Pipeline Ignition)", rm.days_30]),
        _table_row(["First 60 Days (POC Validation & Multi-Threading)", rm.days_60]),
        _table_row(["First 90 Days (Land Closure & Expansion Roadmap)", rm.days_90]),
    ]
    return _toggle_block(
        "30-60-90 Day Territory Ramp Plan",
        [_table_block(rows, table_width=2)],
    )


def get_database_schema(database_id: str, headers: Dict[str, str]) -> Dict[str, Any]:
    """Fetches the target Notion database property schema so properties map cleanly."""
    url = f"https://api.notion.com/v1/databases/{database_id}"
    resp = requests.get(url, headers=headers, timeout=15)
    if resp.status_code == 200:
        return resp.json().get("properties", {})
    return {}


def _format_property_value(col_type: str, value: Any) -> Dict[str, Any]:
    """Formats a value into the standard Notion API property structure based on column type."""
    if col_type == "title":
        return {"title": [{"text": {"content": str(value)[:2000]}}]}
    if col_type == "rich_text":
        return {"rich_text": [_rich_text(str(value))]}
    if col_type == "select":
        clean_option = str(value).replace(",", " /")[:100]
        return {"select": {"name": clean_option}}
    if col_type == "status":
        return {"status": {"name": str(value)[:100]}}
    if col_type == "number":
        if isinstance(value, (int, float)):
            return {"number": value}
        digits = re.sub(r"[^\d.]", "", str(value))
        return {"number": float(digits) if digits else 0}
    if col_type == "url":
        url_val = str(value)
        if not url_val.startswith("http"):
            url_val = f"https://{url_val}"
        return {"url": url_val}
    return {"rich_text": [_rich_text(str(value))]}


def _assign_by_aliases(
    props: Dict[str, Any],
    schema: Dict[str, Any],
    aliases: Sequence[str],
    value: Any,
) -> None:
    """Finds any matching column alias in the Notion database schema and sets its formatted value."""
    if value is None or value == "":
        return
    for col_name in aliases:
        if col_name in schema:
            col_type = schema[col_name].get("type", "rich_text")
            props[col_name] = _format_property_value(col_type, value)
            return


def build_database_properties(
    target: ClayInputData,
    synthesis: DealDeskSynthesis,
    db_properties: Dict[str, Any],
    seller_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Maps target ClayInputData signals and Deal Desk synthesis into native Notion Database Properties."""
    schema = db_properties if db_properties else DEFAULT_DEAL_DESK_SCHEMA
    props: Dict[str, Any] = {}

    # 1. Title property (automatically detects the title column name, e.g. 'Company Name' or 'Account')
    title_col = next(
        (name for name, meta in schema.items() if meta.get("type") == "title"),
        "Company Name",
    )
    props[title_col] = _format_property_value("title", target.company_name)

    # 1b. Selling For (select)
    if seller_name:
        _assign_by_aliases(props, schema, ("Selling For", "Seller"), seller_name)

    # 2. Domain / GitHub Org URL fallback
    domain_or_github = target.domain or (f"https://github.com/{target.github_org}" if target.github_org else None)
    _assign_by_aliases(props, schema, ("Domain", "Website", "GitHub Org"), domain_or_github)

    # 3. Deal Size / Land Deal Size & Expansion ARR
    _assign_by_aliases(
        props,
        schema,
        ("Deal Size", "Land Deal Size", "Estimated ARR", "ARR"),
        synthesis.land_deal_size or synthesis.estimated_arr,
    )
    _assign_by_aliases(
        props,
        schema,
        ("Expansion ARR", "Full Platform ARR", "ARR Potential"),
        synthesis.expansion_arr,
    )

    # 4. Stage (status or select, default 'Prospecting')
    _assign_by_aliases(props, schema, ("Stage", "Deal Stage", "Status"), synthesis.deal_stage)

    # 5. MEDDICC Health
    _assign_by_aliases(
        props,
        schema,
        ("MEDDICC Health", "MEDDicc Health", "MEDDICC Score"),
        synthesis.meddicc_health,
    )

    # 6. AI Product Wedge (text / select)
    _assign_by_aliases(
        props,
        schema,
        ("AI Product Wedge", "Product Wedge", "AI Wedge"),
        synthesis.ai_product_wedge,
    )

    # 7. Annual Evaluation Tax (text or number representing cost of inaction/friction + Land/Expansion ARR)
    eval_tax_display = synthesis.annual_evaluation_tax
    if "land:" not in eval_tax_display.lower() and "expansion" not in eval_tax_display.lower():
        eval_tax_display = (
            f"{eval_tax_display} | Land: ${synthesis.land_deal_size:,} -> Expansion ARR: ${synthesis.expansion_arr:,}"
        )
    _assign_by_aliases(
        props,
        schema,
        ("Annual Eval Tax (ROI)", "Annual Evaluation Tax", "Annual Eval Tax", "Annual Plumbing Tax"),
        eval_tax_display,
    )

    # 8. Champion (target persona/title)
    _assign_by_aliases(
        props,
        schema,
        ("Champion", "Technical Champion"),
        synthesis.champion,
    )

    # 9. Economic Buyer (target executive persona/title)
    _assign_by_aliases(
        props,
        schema,
        ("Economic Buyer", "EB"),
        synthesis.economic_buyer,
    )

    # 10. Boss / OSS / Hiring Signal (driven by has_job_post)
    _assign_by_aliases(
        props,
        schema,
        ("OSS / Hiring Signal", "Boss / Hiring Signal", "Hiring Signal", "Trigger Signal"),
        synthesis.boss_hiring_signal,
    )

    # 11. Security / VPC / YPC (informed by github_org)
    _assign_by_aliases(
        props,
        schema,
        ("Security / VPC Trigger", "Security / YPC", "Security / VPC", "Security"),
        synthesis.security_ypc,
    )

    return props


def build_notion_blocks(target: ClayInputData, synthesis: DealDeskSynthesis) -> List[Dict[str, Any]]:
    """Constructs modular Notion blocks (Callouts, Toggles, Bulleted Lists) with zero monolithic text dumps."""
    com = synthesis.command_of_message
    med = synthesis.meddicc

    signal_children = [
        _labeled_bullet(
            "AI Engineer Headcount (ai_engineer_count)",
            f"{target.ai_engineer_count} AI/ML Engineers ({'High infrastructure sprawl & eval tax (>5)' if target.ai_engineer_count > 5 else 'Lean velocity team (<=5)'})",
        ),
        _labeled_bullet(
            "Active Hiring Signal (has_job_post)",
            f"{target.has_job_post} -> {synthesis.boss_hiring_signal}",
        ),
        _labeled_bullet(
            "GitHub Org & Security Posture (github_org)",
            f"{target.github_org or 'Private / Non-Public'} -> {synthesis.security_ypc}",
        ),
    ]
    if target.technographics:
        signal_children.append(_labeled_bullet("Detected Tech Stack", ", ".join(target.technographics)))
    for ev in target.trigger_events:
        signal_children.append(_labeled_bullet("Trigger Event", ev))

    stakeholder_bullets = (
        [
            _labeled_bullet(
                f"{s.role_type} — {s.name} ({s.title})",
                f"{s.outreach_angle or 'Key stakeholder'} | {s.linkedin_url or 'Verified in Clay'}",
            )
            for s in target.stakeholders
        ]
        if target.stakeholders
        else [
            _labeled_bullet("Economic Buyer Target", synthesis.economic_buyer),
            _labeled_bullet("Technical Champion Target", synthesis.champion),
        ]
    )

    blocks: List[Dict[str, Any]] = [
        # Section 1: Executive Summary & Challenger "Why Now"
        _heading_2("1. Commercial Thesis & Challenger 'Why Now'"),
        _callout_block("Executive Summary", synthesis.executive_summary_callout, color="blue_background"),
        _labeled_bullet("AI Product Wedge", synthesis.ai_product_wedge),
        _labeled_bullet(
            "Calibrated Deal Sizing (Land -> Expand)",
            f"Initial Land Contract: ${synthesis.land_deal_size:,} | Full Platform Expansion ARR: ${synthesis.expansion_arr:,}",
        ),
        _labeled_bullet("Annual Evaluation Tax (Cost of Inaction)", synthesis.annual_evaluation_tax),
        _labeled_bullet("Why Now Thesis", synthesis.challenger_why_now),
        _labeled_bullet("Commercial Reframe", synthesis.reframe_insight),
        _toggle_block(
            "Verified Clay Signals (ai_engineer_count, has_job_post, github_org)",
            signal_children,
        ),
        _divider(),

        # Section 2: Command of the Message Value Matrix
        _heading_2("2. Command of the Message (CoM) Value Pillars"),
        _labeled_bullet("Current State", com.current_state),
        _labeled_bullet("Negative Consequences", com.negative_consequences),
        _labeled_bullet("Desired State", com.desired_state),
        _labeled_bullet("Positive Business Outcomes", com.positive_business_outcomes),
        _toggle_block(
            "Required Capabilities & Defensible Differentiators",
            [
                *[_labeled_bullet("Required Capability", cap) for cap in com.required_capabilities],
                *[_labeled_bullet("Differentiator", diff) for diff in com.differentiators],
            ],
        ),
        _divider(),

        # Section 3: MEDDICC Qualification & Deal Risk Matrix
        _heading_2("3. MEDDICC Qualification & Risk Assessment"),
        _callout_block(
            f"MEDDICC Health — {synthesis.meddicc_health}",
            " | ".join(med.red_flags),
            color="yellow_background" if synthesis.meddicc_health != "Low Risk" else "gray_background",
        ),
        _labeled_bullet("Metrics (Quantified Impact)", med.metrics),
        _labeled_bullet("Economic Buyer", med.economic_buyer),
        _labeled_bullet("Champion", med.champion),
        _labeled_bullet("Identified Pain", med.identified_pain),
        _labeled_bullet("Decision Process", med.decision_process),
        _labeled_bullet("Competition", med.competition),
        _toggle_block(
            "Decision Criteria & Red Flag Mitigation Plan",
            [
                *[_labeled_bullet("Decision Criterion", dc) for dc in med.decision_criteria],
                *[_labeled_bullet("Risk & Mitigation", rf) for rf in med.red_flags],
            ],
        ),
        _divider(),

        # Section 4: Buying Committee & Multi-Threaded Outbound Sequence
        _heading_2("4. Buying Committee & Multi-Threaded Outbound Execution"),
        *stakeholder_bullets,
        _toggle_block(
            "Signal-Grounded Outbound Sequence (Track A: Economic Buyer & Track B: Champion)",
            [_simple_bullet(step) for step in synthesis.executive_outreach_sequence],
        ),
        _toggle_block(
            "Outbound Challenger Sequence (3-Touch)",
            [
                _callout_block(
                    f"Touch 1 (Commercial Insight / 'Why Now') — Subject: {synthesis.outbound_sequence.email_1_subject}",
                    f"\n{synthesis.outbound_sequence.email_1_body}",
                    color="gray_background",
                ),
                _callout_block(
                    f"Touch 2 (Peer Provocation / Metric Benchmarking) — Subject: {synthesis.outbound_sequence.email_2_subject}",
                    f"\n{synthesis.outbound_sequence.email_2_body}",
                    color="gray_background",
                ),
                _callout_block(
                    f"Touch 3 (Technical Wedge / Build-vs-Buy Friction) — Subject: {synthesis.outbound_sequence.email_3_subject}",
                    f"\n{synthesis.outbound_sequence.email_3_body}",
                    color="gray_background",
                ),
            ],
        ),
        _divider(),

        # Section 5: Mutual Action Plan (MAP) & Evaluation Milestones
        _heading_2("5. Mutual Action Plan (MAP)"),
        _build_map_toggle(target, synthesis),
        _divider(),

        # Section 6: Executive Work-Trial & Territory Playbook
        _heading_2("6. Competitive Battlecard, 30-60-90 Ramp & Executive Pitch"),
        _build_competitive_toggle(synthesis),
        _build_ramp_plan_toggle(synthesis),
        _callout_block(
            "Executive Outreach Draft (To VP Sales / CRO)",
            f"\n{synthesis.hiring_manager_pitch}",
            color="blue_background",
        ),
    ]
    return blocks


def push_deal_desk_page(
    target: ClayInputData,
    synthesis: DealDeskSynthesis,
    config: Dict[str, Any],
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Creates a formatted Deal Desk page in the configured Notion database."""
    load_dotenv()
    notion_cfg = config.get("notion", {})

    raw_token = os.environ.get("NOTION_API_KEY") or os.environ.get("NOTION_TOKEN") or notion_cfg.get("api_key", "")
    api_key = "" if str(raw_token).startswith("${") else str(raw_token)

    raw_db = os.environ.get("NOTION_DATABASE_ID") or notion_cfg.get("database_id", "")
    database_id = "" if str(raw_db).startswith("${") else str(raw_db)

    version = notion_cfg.get("version", "2022-06-28")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Notion-Version": version,
    }

    print(f"[*] Target Notion Database ID: {database_id}")

    db_schema = get_database_schema(database_id, headers) if (api_key and database_id and not dry_run) else {}
    properties = build_database_properties(target, synthesis, db_schema)
    children = build_notion_blocks(target, synthesis)

    payload = {
        "parent": {"database_id": database_id},
        "properties": properties,
        "children": children,
    }

    if dry_run:
        return {"status": "dry_run", "payload": payload}

    if not api_key:
        raise RuntimeError("Missing Notion API key. Set NOTION_API_KEY in .env.")
    if not database_id:
        raise RuntimeError("Missing Notion Database ID. Set NOTION_DATABASE_ID in .env.")

    # Notion POST /v1/pages only permits 2 levels of block nesting in the initial request.
    # Extract any nested table blocks inside toggle blocks so we can append them right after page creation.
    api_children = copy.deepcopy(children)
    deferred_toggle_tables: List[List[Dict[str, Any]]] = []
    for block in api_children:
        if block.get("type") == "toggle":
            toggle_children = block["toggle"].get("children", [])
            if any(c.get("type") == "table" for c in toggle_children):
                deferred_toggle_tables.append(toggle_children)
                block["toggle"]["children"] = []
            else:
                deferred_toggle_tables.append([])

    payload["children"] = api_children

    response = requests.post(
        "https://api.notion.com/v1/pages",
        headers=headers,
        json=payload,
        timeout=25,
    )
    if response.status_code != 200:
        raise RuntimeError(f"Notion API error ({response.status_code}): {response.text}")

    response_json = response.json()
    page_id = response_json.get("id")

    # Append any 3rd-level table blocks into their parent toggle blocks
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

    print(f"[+] Notion Deal Desk page created: {response_json.get('url')}")
    return {
        "status": "created",
        "page_id": page_id,
        "url": response_json.get("url"),
    }


def update_existing_deal_desk_page(
    page_id: str,
    target: ClayInputData,
    synthesis: DealDeskSynthesis,
    config: Dict[str, Any],
    seller_name: Optional[str] = None,
    db_schema: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Updates an existing Notion Deal Desk page's properties and appends all Deal Desk body blocks."""
    load_dotenv()
    notion_cfg = config.get("notion", {})

    raw_token = os.environ.get("NOTION_API_KEY") or os.environ.get("NOTION_TOKEN") or notion_cfg.get("api_key", "")
    api_key = "" if str(raw_token).startswith("${") else str(raw_token)

    raw_db = os.environ.get("NOTION_DATABASE_ID") or notion_cfg.get("database_id", "")
    database_id = "" if str(raw_db).startswith("${") else str(raw_db)

    version = notion_cfg.get("version", "2022-06-28")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Notion-Version": version,
    }

    if db_schema is None and database_id:
        db_schema = get_database_schema(database_id, headers)

    properties = build_database_properties(target, synthesis, db_schema or {}, seller_name=seller_name)
    children = build_notion_blocks(target, synthesis)

    # 1. Update page properties via PATCH /v1/pages/{page_id}
    patch_resp = requests.patch(
        f"https://api.notion.com/v1/pages/{page_id}",
        headers=headers,
        json={"properties": properties},
        timeout=25,
    )
    if patch_resp.status_code != 200:
        raise RuntimeError(f"Notion page property update failed ({patch_resp.status_code}): {patch_resp.text}")

    # 2. Extract any 3rd-level table blocks inside toggle blocks to respect Notion's 2-level request limit
    api_children = copy.deepcopy(children)
    deferred_toggle_tables: List[List[Dict[str, Any]]] = []
    for block in api_children:
        if block.get("type") == "toggle":
            toggle_children = block["toggle"].get("children", [])
            if any(c.get("type") == "table" for c in toggle_children):
                deferred_toggle_tables.append(toggle_children)
                block["toggle"]["children"] = []
            else:
                deferred_toggle_tables.append([])

    # 3. Append top-level blocks to the page via PATCH /v1/blocks/{page_id}/children
    append_resp = requests.patch(
        f"https://api.notion.com/v1/blocks/{page_id}/children",
        headers=headers,
        json={"children": api_children},
        timeout=25,
    )
    if append_resp.status_code != 200:
        raise RuntimeError(f"Notion block append failed ({append_resp.status_code}): {append_resp.text}")

    # 4. Append deferred 4-column MAP table into its parent toggle block
    if any(deferred_toggle_tables):
        created_toggles = [
            b for b in append_resp.json().get("results", []) if b.get("type") == "toggle"
        ]
        for toggle_obj, deferred in zip(created_toggles, deferred_toggle_tables):
            if deferred:
                requests.patch(
                    f"https://api.notion.com/v1/blocks/{toggle_obj['id']}/children",
                    headers=headers,
                    json={"children": deferred},
                    timeout=15,
                )

    page_data = patch_resp.json()
    return {
        "status": "updated",
        "page_id": page_id,
        "url": page_data.get("url"),
    }


class Client:
    """Resilient Notion API Client compatible with 2022-06-28 specification."""

    def __init__(self, auth: str):
        self.auth = (auth or "").strip().strip('"').strip("'")
        self.headers = {
            "Authorization": f"Bearer {self.auth}",
            "Content-Type": "application/json",
            "Notion-Version": "2022-06-28",
        }
        self.databases = self._Databases(self.headers)
        self.blocks = self._Blocks(self.headers)
        self.pages = self._Pages(self.headers)

    class _Databases:
        def __init__(self, headers):
            self.headers = headers

        def query(self, database_id: str, filter: dict = None, page_size: int = 100):
            payload = {"page_size": page_size}
            if filter:
                payload["filter"] = filter
            resp = requests.post(
                f"https://api.notion.com/v1/databases/{database_id}/query",
                headers=self.headers,
                json=payload,
                timeout=25,
            )
            return resp.json() if resp.status_code == 200 else {"results": []}

    class _Blocks:
        def __init__(self, headers):
            self.headers = headers
            self.children = self._Children(headers)

        def update(self, block_id: str, **kwargs):
            resp = requests.patch(
                f"https://api.notion.com/v1/blocks/{block_id}",
                headers=self.headers,
                json=kwargs,
                timeout=25,
            )
            return resp.json()

        def delete(self, block_id: str):
            resp = requests.delete(
                f"https://api.notion.com/v1/blocks/{block_id}",
                headers=self.headers,
                timeout=25,
            )
            return resp.json()

        class _Children:
            def __init__(self, headers):
                self.headers = headers

            def list(self, block_id: str, page_size: int = 100):
                resp = requests.get(
                    f"https://api.notion.com/v1/blocks/{block_id}/children",
                    headers=self.headers,
                    params={"page_size": page_size},
                    timeout=25,
                )
                return resp.json() if resp.status_code == 200 else {"results": []}

            def append(self, block_id: str, children: list):
                resp = requests.patch(
                    f"https://api.notion.com/v1/blocks/{block_id}/children",
                    headers=self.headers,
                    json={"children": children},
                    timeout=25,
                )
                return resp.json()

    class _Pages:
        def __init__(self, headers):
            self.headers = headers

        def create(self, **kwargs):
            resp = requests.post(
                "https://api.notion.com/v1/pages",
                headers=self.headers,
                json=kwargs,
                timeout=25,
            )
            return resp.json()

        def retrieve(self, page_id: str):
            resp = requests.get(
                f"https://api.notion.com/v1/pages/{page_id}",
                headers=self.headers,
                timeout=25,
            )
            return resp.json()

        def update(self, page_id: str, **kwargs):
            resp = requests.patch(
                f"https://api.notion.com/v1/pages/{page_id}",
                headers=self.headers,
                json=kwargs,
                timeout=25,
            )
            return resp.json()



