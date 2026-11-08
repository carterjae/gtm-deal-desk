#!/usr/bin/env python3
"""
models/gtme_deliverable.py
Full-Stack GTM Engineering Blueprint & Work-Trial Deliverable schemas.
Restored 3-Section Executive Architecture with Mermaid diagram support.
"""

from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class CommercialSignal(BaseModel):
    trigger_type: str = Field(description="e.g., Tech Stack Migration, Leadership Hire, Store Expansion")
    evidence: str = Field(description="Concrete data point, filing, or job posting signal")
    why_now_impact: str = Field(description="Operational friction or revenue drag if unaddressed")


class EnrichmentWaterfallStep(BaseModel):
    step_number: int
    data_source: str = Field(description="e.g., Firecrawl, Apollo API, Claygent, BuiltWith")
    action_and_fallback: str = Field(description="Primary extraction logic and fallback if match rate < 70%")
    output_fields: List[str] = Field(description="Specific fields extracted")


class AccountGTMBlueprint(BaseModel):
    account_name: str
    scale_and_domain: str
    commercial_signals: List[CommercialSignal]
    waterfall_steps: List[EnrichmentWaterfallStep]
    sample_json_payload: str = Field(description="Clean, formatted JSON webhook payload ready for CRM ingestion")
    challenger_outreach: Dict[str, str] = Field(description="Keys: 'email_subject', 'email_body', 'linkedin_touch'")


class EnterpriseGovernanceSpec(BaseModel):
    deliberate_manual_restraint_gate: str = Field(description="Exact workflow boundary kept manual to protect brand reputation and CRM hygiene")
    failure_mode_and_rate_limit_mitigation: str = Field(description="How the pipeline handles 503s, API timeouts, and schema drift")
    unit_economics_breakdown: str = Field(description="Itemized API credit cost per account run (e.g., $0.07/run) vs. SDR manual research cost ($18.50/account)")
    attribution_kpis: List[str] = Field(description="3-4 concrete pipeline velocity and conversion metrics")


class FullStackGTMEDeliverable(BaseModel):
    hiring_company: str
    target_role: str
    executive_summary: str = Field(description="2-3 sentence strategic thesis connecting system architecture to revenue velocity")
    icp_matrix_and_disqualifiers: str = Field(description="Target ICP criteria plus explicit negative disqualifiers filtered out before enrichment")
    mermaid_architecture_diagram: str = Field(description="Valid Mermaid.js flowchart (graph TD or graph LR) showing Ingestion -> Enrichment Waterfall -> LLM Normalization -> Human Gate -> CRM Sync. Raw mermaid syntax only, no backticks.")
    target_accounts: List[AccountGTMBlueprint] = Field(description="3 real-world target accounts", min_length=3, max_length=3)
    enterprise_governance: EnterpriseGovernanceSpec


# --- Backwards Compatibility Aliases ---
class PipelineTechnicalSpec(BaseModel):
    ingestion_trigger: str = ""
    waterfall_architecture: List[EnrichmentWaterfallStep] = Field(default_factory=list)
    agent_reasoning_prompt: str = ""
    destination_routing: str = ""
    sample_json_payload: str = ""


class OperationalGovernance(BaseModel):
    deliberate_manual_gate: str = ""
    failure_mode_mitigation: str = ""
    unit_economics_estimate: str = ""


class ChallengerOutreach(BaseModel):
    email: str = ""
    linkedin_touch: str = ""
    asset_offered: str = ""
