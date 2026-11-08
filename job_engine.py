#!/usr/bin/env python3
"""
Autonomous Job Search & Work-Trial Deal Room Engine (job_engine.py).

Capabilities:
  - Scores role fit against master_career_profile.json & extracts jd_hard_skill_keywords (Upgrade #4)
  - Enforces the Deterministic ATS Keyword Gap Guardrail and compiles Markdown + WeasyPrint PDF resumes
  - Cross-triggers Mode A Deal Desk Blitz (gtm_engine.py) when fit_score >= 90 (Upgrade #3)
  - Writes or updates in-place the Notion Deal Room in NOTION_JOB_DB_ID
  - Stages a ready-to-send MIME .eml draft with the compiled PDF resume attached (Upgrade #2)
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional
import requests
from dotenv import load_dotenv
from google import genai
from google.genai import types
from pydantic import BaseModel, Field, field_validator

PROJECT_ROOT = Path(__file__).resolve().parent
ENV_PATH = PROJECT_ROOT / ".env"
load_dotenv(ENV_PATH)

from gtm_engine import trigger_mode_a_deal_desk_blitz
from scripts.compile_resume import clean_slug, compile_tailored_resume, load_master_profile

from scripts.stage_email_draft import stage_outbound_email_draft, split_subject_and_body, sanitize_cold_email
from scripts.outreach_dispatcher import build_mailto_url, push_draft_to_imap, stage_committee_outreach
from scripts.contact_verifier import generate_hiring_committee_search_url, clean_domain
from scripts.signal_harvester import harvest_company_dossier
from scripts.cache_manager import get_or_create_career_cache
from models.intelligence import CompanyDossier

NOTION_VERSION = "2022-06-28"

RoleArchetypeLiteral = Literal[
    "GTM Engineer",
    "AI Revenue Architect / RevOps",
    "AI Operations / Technical RevOps",
    "Founding / Outbound AE",
    "Growth Engineer",
]

DEAL_ROOM_PLACEHOLDER = "{{NOTION_DEAL_ROOM_URL}}"


class ClayJobInput(BaseModel):
    """Validated schema for incoming Clay or Notion Poller job-enrichment payloads."""

    company_name: str = Field(..., description="Hiring company name")
    company_url: Optional[str] = Field(default=None, description="Company website URL")
    funding_stage: Optional[str] = Field(default=None, description="Funding amount, valuation, and stage")
    product_summary: Optional[str] = Field(default=None, description="Summary of what the hiring company builds and sells")
    role_title: str = Field(..., description="Target job title from the job posting")
    job_url: Optional[str] = Field(default=None, description="Direct URL to the job posting")
    job_description: str = Field(..., description="Full or summarized job description text")
    buyer_name_title: Optional[str] = Field(
        default=None, description="Hiring manager or Economic Buyer name and title (e.g., 'Sarah Jenkins, VP of Revenue')"
    )
    buyer_email: Optional[str] = Field(default=None, description="Economic Buyer email address")
    buyer_linkedin: Optional[str] = Field(default=None, description="Economic Buyer LinkedIn URL")
    salary_range: Optional[str] = Field(default=None, description="Posted or benchmarked salary / OTE range")

    @field_validator(
        "company_url",
        "funding_stage",
        "product_summary",
        "job_url",
        "buyer_name_title",
        "buyer_email",
        "buyer_linkedin",
        "salary_range",
        mode="before",
    )
    @classmethod
    def sanitize_optional_str(cls, v: Any) -> Optional[str]:
        if v is None:
            return None
        s = str(v).strip()
        if not s or s.lower() in ("null", "none", "n/a", "false", "undefined"):
            return None
        return s


class TailoredExperienceItem(BaseModel):
    """Tailored experience entry strictly grounded in master_career_profile.json."""

    company: str = Field(..., description="Exact employer name from master_career_profile.json (never fabricate)")
    role: str = Field(..., description="Exact role title from master_career_profile.json")
    dates: str = Field(..., description="Exact dates from master_career_profile.json (never fabricate)")
    location: str = Field(default="Chicago, IL", description="Location from master_career_profile.json")
    xyz_bullets: List[str] = Field(
        ...,
        min_length=3,
        max_length=4,
        description=(
            "3–4 tailored bullet points using the Google XYZ formula "
            "('Accomplished [X] as measured by [Y], by doing [Z]') mirroring the target JD's exact terminology."
        ),
    )


class LinkedInOutboundBundle(BaseModel):
    """Multi-channel LinkedIn assets adhering to strict platform character limits."""

    connection_note: str = Field(
        description="Strictly <= 300 characters. Executive peer-to-peer hook citing recent company signal/news. No generic fluff."
    )
    inmail_message: str = Field(
        description="Strictly <= 600 characters. Compressed Challenger reframe referencing the Cost-of-Inaction (COI) and Jae's automated pipeline architecture."
    )

    @field_validator("connection_note")
    @classmethod
    def truncate_connection_note(cls, v: str) -> str:
        s = v.strip() if v else ""
        if len(s) > 300:
            return s[:297] + "..."
        return s

    @field_validator("inmail_message")
    @classmethod
    def truncate_inmail(cls, v: str) -> str:
        s = v.strip() if v else ""
        if len(s) > 600:
            return s[:597] + "..."
        return s


def sanitize_linkedin_bundle(bundle: LinkedInOutboundBundle) -> LinkedInOutboundBundle:
    """Guarantees LinkedIn limits are never exceeded."""
    if len(bundle.connection_note) > 300:
        bundle.connection_note = bundle.connection_note[:297] + "..."
    if len(bundle.inmail_message) > 600:
        bundle.inmail_message = bundle.inmail_message[:597] + "..."
    return bundle


challenger_email_instructions = """
### CHALLENGER COLD EMAIL RULES:
1. HARD RULE: NEVER include the internal Notion card link, internal database URL, or candidate tracking notes in the email draft.
2. Option A must have ZERO links. It should offer the live company-specific Deal Desk & Pipeline Architecture walkthrough on request.
3. Option B must include a direct link to the live interactive portfolio workbench pre-configured for the company using the placeholder: {{BLUEPRINT_URL}} (which resolves to https://jae-carter.vercel.app/?company=<Company>).
"""

resume_prompt_constraints = """
RESUME FORMATTING & VOICE CONSTRAINTS:
1. Executive Summary MUST use standard implied first-person syntax (e.g., "AI Systems & GTM Engineer with experience...", NOT "Jae Carter is a...").
2. NEVER use third-person pronouns ("he", "his", "him") or refer to the candidate by name in bullet points.
3. NEVER explicitly write "Google XYZ format" or "XYZzy format" anywhere in the text.
4. CRITICAL VERB VARIETY RULE: Do NOT start every bullet with the literal word "Accomplished". Use strong, varied engineering and commercial action verbs (e.g., "Architected", "Engineered", "Co-developed", "Built", "Orchestrated", "Generated", "Reduced", "Scaled") while preserving the XYZ structure (Impact/Outcome + Quantified Metric + Technical/Commercial Mechanism).
"""

RESUME_PROMPT_GUARDRAILS = resume_prompt_constraints



def sanitize_email_text(body_text: str, blueprint_url: str = "") -> str:
    """Removes internal notion URLs and swaps blueprint placeholders."""
    # Strip any accidental notion.so links to private pages
    cleaned = re.sub(r"https?://(?:www\.)?notion\.so/\S+", blueprint_url or "[Architecture Blueprint Available on Request]", body_text)
    cleaned = cleaned.replace("{{notion_link}}", blueprint_url or "the architecture teardown")
    if blueprint_url and "{{BLUEPRINT_URL}}" in cleaned:
        cleaned = cleaned.replace("{{BLUEPRINT_URL}}", blueprint_url)
    return cleaned.strip()


def sanitize_resume_markdown(resume_text: str) -> str:
    """
    Deterministically strips any meta-commentary mentioning Google XYZ,
    XYZ formula, or prompt formatting instructions.
    """
    if not resume_text:
        return ""
    patterns = [
        r"\(tailored for google xyz format\)",
        r"\(google xyz format\)",
        r"\(xyz format\)",
        r"\(formatted to google xyz\)",
        r"\[google xyz formula\]",
        r"google xyz format",
        r"google xyz formula",
        r"\(tailored for xyz\)",
        r"\(xyz formula\)",
        r"tailored for xyz",
        r"xyz formula",
        r"google xyz",
    ]
    cleaned = resume_text
    # Ensure Professional Experience heading is strictly clean without swallowing next line
    cleaned = re.sub(
        r"##[ \t]+Professional[ \t]+Experience[^\n]*",
        "## Professional Experience",
        cleaned,
        flags=re.IGNORECASE,
    )
    for p in patterns:
        cleaned = re.sub(p, "", cleaned, flags=re.IGNORECASE)

    # Strip third-person phrasing from resume summary and bullets
    third_person_patterns = [
        (r"\bJae Carter is an?\s+", ""),
        (r"\bJae is an?\s+", ""),
        (r"\bJae Carter\s+brings\b", "Brings"),
        (r"\bJae\s+brings\b", "Brings"),
        (r"\bHe demonstrates\b", "Demonstrates"),
        (r"\bThe candidate demonstrates\b", "Demonstrates"),
        (r"\bThe candidate\b", "I"),
    ]
    for p, repl in third_person_patterns:
        cleaned = re.sub(p, repl, cleaned, flags=re.IGNORECASE)

    # Clean up double parentheses or trailing spaces left behind
    cleaned = re.sub(r"\(\s*\)", "", cleaned)
    cleaned = re.sub(r"  +", " ", cleaned)
    return cleaned.strip()


def parse_inline_markdown(text: str) -> list:
    """
    Parses inline markdown (**bold**, *italic*, `code`) into Notion rich_text objects.
    Prevents raw markdown asterisks and backticks from displaying in Notion.
    """
    if not text:
        return []

    pattern = re.compile(r"(\*\*.*?\*\*|`.*?`|\*.*?\*)")
    tokens = pattern.split(text)
    rich_text = []

    for token in tokens:
        if not token:
            continue
        if token.startswith("**") and token.endswith("**") and len(token) > 4:
            rich_text.append({
                "type": "text",
                "text": {"content": token[2:-2][:1900]},
                "annotations": {"bold": True}
            })
        elif token.startswith("`") and token.endswith("`") and len(token) > 2:
            rich_text.append({
                "type": "text",
                "text": {"content": token[1:-1][:1900]},
                "annotations": {"code": True}
            })
        elif token.startswith("*") and token.endswith("*") and len(token) > 2:
            rich_text.append({
                "type": "text",
                "text": {"content": token[1:-1][:1900]},
                "annotations": {"italic": True}
            })
        else:
            rich_text.append({
                "type": "text",
                "text": {"content": token[:1900]}
            })

    return rich_text if rich_text else [{"type": "text", "text": {"content": text[:1900]}}]


def markdown_resume_to_notion_blocks(markdown_text: str) -> List[Dict[str, Any]]:
    """
    Converts full tailored resume markdown into complete, native Notion blocks
    (headings, bold paragraphs, and bullet points) inside the accordion without truncation.
    """
    if not markdown_text:
        return []

    clean_text = sanitize_resume_markdown(markdown_text)
    blocks: List[Dict[str, Any]] = []
    lines = clean_text.split("\n")

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        # Document Header (# Candidate Name)
        if stripped.startswith("# "):
            candidate_name = stripped[2:].strip()
            blocks.append({
                "object": "block",
                "type": "heading_3",
                "heading_3": {
                    "rich_text": parse_inline_markdown(f"**{candidate_name}**")
                }
            })
        # Major Section Headings (## Executive Summary, ## Professional Experience, etc.)
        elif stripped.startswith("## "):
            section_title = stripped[3:].strip()
            blocks.append({
                "object": "block",
                "type": "heading_3",
                "heading_3": {
                    "rich_text": parse_inline_markdown(section_title)
                }
            })
        # Company & Role Headings (### Role — Company (Dates | Loc))
        elif stripped.startswith("### "):
            role_header = stripped[4:].strip()
            blocks.append({
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": parse_inline_markdown(f"**{role_header}**")
                }
            })
        # Bullet Points
        elif stripped.startswith("- ") or stripped.startswith("* "):
            bullet_body = stripped[2:].strip()
            blocks.append({
                "object": "block",
                "type": "bulleted_list_item",
                "bulleted_list_item": {
                    "rich_text": parse_inline_markdown(bullet_body)
                }
            })
        # Narrative Paragraphs
        else:
            blocks.append({
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": parse_inline_markdown(stripped)
                }
            })

    # Notion allows up to 100 children per parent block; cap safely at 90
    return blocks[:90]





class DualChallengerEmailBundle(BaseModel):
    """Dual cold outbound email bundle supporting both permission-first (zero links) and asset-linked variations."""

    subject: str = Field(
        ...,
        description="Provocative, low-friction subject line referencing pipeline drag or company trigger.",
    )
    option_a_permission_first: str = Field(
        ...,
        description=(
            "120-word Challenger email without links (strict zero-link deliverability). "
            "Cites trigger and quantified COI. Closes with a soft permission hook offering the architecture blueprint upon request."
        ),
    )
    option_b_asset_linked: str = Field(
        ...,
        description=(
            "120-word Challenger email with a dedicated anchor link pointing to the public 3-Account Waterfall & JSON Blueprint. "
            "Frames the link as proof of work rather than a generic pitch."
        ),
    )
    blueprint_public_url: str = Field(
        default="",
        description="Public URL placeholder for the shared 3-Account Waterfall & CRM JSON deliverable.",
    )


class JobSynthesisResult(BaseModel):
    """Strict Structured Output schema returned by Gemini for the Job Pipeline Deal Room."""

    role_archetype: RoleArchetypeLiteral = Field(
        ...,
        description=(
            "Best match among the 5 target tracks: "
            "'GTM Engineer', 'AI Revenue Architect / RevOps', 'AI Operations / Technical RevOps', "
            "'Founding / Outbound AE', or 'Growth Engineer'."
        ),
    )
    fit_score: int = Field(
        ...,
        ge=0,
        le=100,
        description=(
            "Integer (0-100) scoring alignment with Jae Carter's stack: "
            "Clay, Gemini/LLMs, Local Daemons/APIs, Notion Deal Desk automation, and Challenger/CoM/MEDDICC execution."
        ),
    )
    fit_justification: str = Field(
        ...,
        description=(
            "1-2 punchy sentences explaining why this role received this fit score based on specific stack and commercial "
            "overlap with Jae Carter's background (e.g., Clay waterfalls, Python webhook daemons, Workday Federal MEDDICC rigor)."
        ),
    )
    salary_range: str = Field(
        ...,
        description=(
            "Exact salary/OTE range extracted from the JD if listed, or market-benchmarked compensation range "
            "for this company stage & role archetype (e.g., '$170,000 – $215,000 + Equity' or '$150k Base / $300k OTE + Equity')."
        ),
    )
    jd_hard_skill_keywords: List[str] = Field(
        ...,
        min_length=5,
        max_length=12,
        description="Top 10 exact hard-skill nouns, tools, or methodologies extracted verbatim from the JD.",
    )
    sample_icp_target_company: str = Field(
        ...,
        description=(
            "Name of 1 real enterprise prospect company that the hiring startup wants to sell to "
            "(e.g., 'Stripe', 'Ramp', 'Databricks', 'Cloudflare', 'Rippling')."
        ),
    )
    sample_icp_target_domain: str = Field(
        ...,
        description="Clean domain of sample_icp_target_company (e.g., 'stripe.com', 'ramp.com', 'databricks.com').",
    )
    top_down_trigger: str = Field(
        ...,
        description="Executive 'Why Now' business trigger synthesized from the JD, product wedge, and funding/growth stage.",
    )
    bottom_up_trigger: str = Field(
        ...,
        description="Specific technical or operational GTM workflow pain point this role is being hired to solve.",
    )
    com_summary: List[str] = Field(
        ...,
        min_length=3,
        max_length=3,
        description=(
            "Exactly 3 Command of the Message bullets showing how Jae Carter will drive revenue/efficiency in this role: "
            "Bullet 1 MUST start with 'Current State: ', "
            "Bullet 2 MUST start with 'Negative Consequences: ', "
            "Bullet 3 MUST start with 'Positive Business Outcomes & Required Capabilities: '."
        ),
    )
    tailored_summary: str = Field(
        ...,
        description=(
            "Tailored 2–3 sentence executive summary positioning Jae Carter specifically for this company and role, "
            "grounded in master_career_profile.json."
        ),
    )
    tailored_experience: List[TailoredExperienceItem] = Field(
        ...,
        min_length=3,
        max_length=4,
        description=(
            "Array of company experience objects from master_career_profile.json (Independent / Systems Architecture, "
            "AlphaSense, Workday, Enterprise Mobility), each with 3–4 Google XYZ bullet points tailored to the JD."
        ),
    )
    sample_prospect_email: str = Field(
        ...,
        description=(
            "A complete Day-1 Challenger cold email (including 'Subject: ...') written ON BEHALF OF the hiring company "
            "targeting sample_icp_target_company, acting as a mini Work Trial proof-of-work."
        ),
    )
    challenger_email: DualChallengerEmailBundle = Field(
        ...,
        description=(
            "Dual cold outbound email bundle from Jae Carter to the Economic Buyer: "
            "Option A (zero links, permission-first) and Option B (asset-linked to public deliverable)."
        ),
    )
    linkedin_bundle: Optional[LinkedInOutboundBundle] = Field(
        default=None,
        description="Multi-channel LinkedIn connection note (<=300 chars) and InMail (<=600 chars) for the Economic Buyer.",
    )

    @field_validator("challenger_email", mode="before")
    @classmethod
    def coerce_challenger_email(cls, v: Any) -> Any:
        """Seamlessly coerces legacy string/dict outputs into DualChallengerEmailBundle."""
        if isinstance(v, DualChallengerEmailBundle):
            return v
        if isinstance(v, dict):
            if "option_a_permission_first" in v:
                return v
            subj = v.get("subject", "Enterprise pipeline drag")
            body = v.get("body", v.get("text", ""))
            return {
                "subject": subj,
                "option_a_permission_first": body,
                "option_b_asset_linked": body,
                "blueprint_public_url": v.get("blueprint_public_url", ""),
            }
        if isinstance(v, str):
            lines = v.strip().split("\n", 1)
            subj = "Enterprise pipeline drag"
            body = v.strip()
            if lines and lines[0].lower().startswith("subject:"):
                subj = lines[0][len("subject:"):].strip()
                body = lines[1].strip() if len(lines) > 1 else ""
            return {
                "subject": subj,
                "option_a_permission_first": body,
                "option_b_asset_linked": body,
                "blueprint_public_url": "",
            }
        return v


# Type alias matching user request specification
JobDealRoomSynthesis = JobSynthesisResult


def synthesize_deal_room_with_caching(company_name: str, role_title: str, dossier_dict: dict, coi_dict: dict):
    """
    Synthesizes a Deal Room using Gemini context caching. 
    Locks master career profile and Clay/Lindahl rubrics into server KV memory.
    """
    client = genai.Client()

    # 1. Retrieve active cache (provisions or fetches server cache if >= 32k tokens)
    cached_content_name = get_or_create_career_cache(client, ttl_hours=12)

    dynamic_payload = f"""
    TARGET COMPANY OPPORTUNITY:
    - Company: {company_name}
    - Role: {role_title}
    - Company Dossier: {json.dumps(dossier_dict, indent=2)}
    - Cost of Inaction (COI) Metrics: {json.dumps(coi_dict, indent=2)}

    Execute full Deal Room synthesis matching the required Pydantic output schema.
    """

    # 2. Configure request with KV cache if explicitly available, else fallback to implicit prefix
    config_args = {
        "response_mime_type": "application/json",
        "temperature": 0.2
    }

    if cached_content_name:
        print(f"[KV_CACHE] Utilizing active explicit KV cache: {cached_content_name}")
        config_args["cached_content"] = cached_content_name
        prompt_contents = dynamic_payload
    else:
        # Fallback: Prepend static corpus for automatic prefix caching
        corpus_path = Path("context/static_career_corpus.md")
        static_context = corpus_path.read_text(encoding="utf-8") if corpus_path.exists() else ""
        prompt_contents = f"{static_context}\n\n---\n\n{dynamic_payload}"

    # 3. Execute generation
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt_contents,
        config=types.GenerateContentConfig(**config_args)
    )

    # 4. Telemetry check for cache reads
    if hasattr(response, "usage_metadata"):
        usage = response.usage_metadata
        prompt_tokens = getattr(usage, "prompt_token_count", 0)
        cached_tokens = getattr(usage, "cached_content_token_count", 0)
        print(f"[TELEMETRY] Token Telemetry -> Prompt Tokens: {prompt_tokens} | Cached Tokens Read: {cached_tokens}")

    return response.text


def synthesize_deal_room_with_cache(
    company_name: str,
    role_title: str,
    dossier: dict,
    coi: dict,
    model_name: str = "gemini-2.5-flash",
) -> JobDealRoomSynthesis:
    """
    Synthesizes the structured Deal Room output utilizing explicit KV cache
    when >=32k tokens, or implicit prefix caching with the static career corpus.
    """
    api_key = (os.environ.get("GEMINI_API_KEY") or "").strip().strip('"').strip("'")
    client = genai.Client(api_key=api_key) if api_key else genai.Client()

    # Retrieve active cache (if token threshold is met)
    cached_content_name = ""
    try:
        cached_content_name = get_or_create_career_cache(client, ttl_hours=12)
    except Exception as e:
        print(f"[!] Notice checking career cache: {e}")

    dynamic_target_prompt = f"""
    TARGET COMPANY OPPORTUNITY:
    - Company: {company_name}
    - Role: {role_title}
    - Dossier: {json.dumps(dossier, indent=2)}
    - Cost of Inaction: {json.dumps(coi, indent=2)}
    
    Generate the structured Deal Room output matching schema.
    """

    if cached_content_name:
        # 1. EXPLICIT CACHE PATH: Static career corpus is already cached in KV memory
        print(f"[KV_CACHE] Utilizing active explicit KV cache: {cached_content_name}")
        response = client.models.generate_content(
            model=model_name,
            contents=dynamic_target_prompt,
            config=types.GenerateContentConfig(
                cached_content=cached_content_name,
                response_mime_type="application/json",
                response_schema=JobDealRoomSynthesis,
                temperature=0.2,
            ),
        )
    else:
        # 2. IMPLICIT PREFIX CACHE PATH: Static corpus prepended to prompt
        corpus_path = PROJECT_ROOT / "context" / "static_career_corpus.md"
        static_context = corpus_path.read_text(encoding="utf-8") if corpus_path.exists() else ""

        full_prompt = f"{static_context}\n\n---\n\n{dynamic_target_prompt}"
        response = client.models.generate_content(
            model=model_name,
            contents=full_prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=JobDealRoomSynthesis,
                temperature=0.2,
            ),
        )

    # Verify cache utilization in usage metadata
    if hasattr(response, "usage_metadata"):
        usage = response.usage_metadata
        prompt_tokens = getattr(usage, "prompt_token_count", 0)
        cached_tokens = getattr(usage, "cached_content_token_count", 0)
        print(f"[TELEMETRY] Token Telemetry -> Prompt Tokens: {prompt_tokens} | Cached Tokens Read: {cached_tokens}")

    return JobDealRoomSynthesis.model_validate_json(response.text)


def _normalize_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    u = url.strip()
    if not u:
        return None
    if not u.startswith(("http://", "https://")):
        u = f"https://{u}"
    return u


def _normalize_email(email: Optional[str]) -> Optional[str]:
    if not email:
        return None
    e = email.strip()
    if "@" not in e or " " in e:
        return None
    return e


def _sanitize_select_option(option_name: str) -> str:
    """Notion select property options cannot contain commas."""
    return str(option_name).replace(",", "").strip()[:100]


def _infer_archetype_fallback(job: ClayJobInput) -> RoleArchetypeLiteral:
    text = f"{job.role_title} {job.job_description}".lower()
    if any(k in text for k in ("gtm engineer", "clay", "go-to-market engineer", "outbound automation")):
        return "GTM Engineer"
    if any(k in text for k in ("ai operations", "ai ops", "technical revops", "revops engineer", "business systems", "gtm systems engineer")):
        return "AI Operations / Technical RevOps"
    if any(k in text for k in ("revops", "revenue operations", "revenue architect", "systems")):
        return "AI Revenue Architect / RevOps"
    if any(k in text for k in ("growth engineer", "plg", "product-led", "funnel", "experimentation", "lifecycle")):
        return "Growth Engineer"
    if any(k in text for k in ("account executive", "founding ae", "outbound ae", "enterprise ae", "sales", "meddicc")):
        return "Founding / Outbound AE"
    return "GTM Engineer"


def calculate_coi_metrics(dossier: Any) -> Dict[str, Any]:
    """
    Calculates realistic operational latency and engineering/GTM capacity drag
    based on CompanyDossier headcount bracket and funding stage (loaded operator rate $85-$115/hr).
    """
    headcount = getattr(dossier, "headcount_bracket", "51-200") if dossier else "51-200"
    if isinstance(dossier, dict):
        headcount = dossier.get("headcount_bracket", "51-200")

    funding = (
        getattr(getattr(dossier, "capital", None), "funding_stage", "Series B")
        if dossier and hasattr(dossier, "capital")
        else (dossier.get("capital", {}).get("funding_stage", "Series B") if isinstance(dossier, dict) else "Series B")
    )

    # Realistic loaded GTM / RevOps / AI Ops operator rates ($85-$115/hr)
    tier_map = {
        "1-10":    {"reps": 2,  "hrs_per_week": 8,  "hourly_rate": 85},
        "11-50":   {"reps": 4,  "hrs_per_week": 9,  "hourly_rate": 90},
        "51-200":  {"reps": 8,  "hrs_per_week": 10, "hourly_rate": 95},
        "201-500": {"reps": 16, "hrs_per_week": 10, "hourly_rate": 105},
        "500+":    {"reps": 30, "hrs_per_week": 9,  "hourly_rate": 115},
    }

    tier = tier_map.get(headcount, tier_map["51-200"])
    reps = tier["reps"]
    hrs = tier["hrs_per_week"]
    rate = tier["hourly_rate"]

    annual_tax = reps * hrs * rate * 52
    monthly_drag = annual_tax / 12
    quarterly_risk = annual_tax / 4

    return {
        "headcount_bracket": headcount,
        "funding_stage": funding,
        "estimated_reps": reps,
        "hours_wasted_per_rep_week": hrs,
        "fully_loaded_hourly_rate": rate,
        "annual_sdr_tax_str": f"${annual_tax:,.0f}",
        "monthly_drag_str": f"${monthly_drag:,.0f}",
        "quarterly_risk_str": f"${quarterly_risk:,.0f}",
        "pipeline_latency_compression": "35m manual research -> 14.2s deterministic synthesis",
    }


def _deterministic_job_fallback(
    job: ClayJobInput,
    coi_metrics: Optional[Dict[str, Any]] = None,
) -> JobSynthesisResult:
    """High-precision deterministic fallback grounded in master_career_profile.json."""
    t_lower = f"{job.role_title} {job.job_description}".lower()
    disqualified_words = (
        "janitor", "clerk", "transit", "operator", "driver", "technician", "union",
        "police", "nurse", "paralegal", "custodian", "warehouse", "cashier", "ptc",
        "trainset", "conductor", "locomotive", "mechanic", "carpenter", "electrician",
    )
    is_disqualified = any(w in t_lower for w in disqualified_words)

    archetype = _infer_archetype_fallback(job)
    buyer = job.buyer_name_title or f"VP of Revenue / Head of GTM at {job.company_name}"
    buyer_first = buyer.split(",")[0].split()[0] if buyer else "there"
    funding = job.funding_stage or "recent growth milestone"
    product = job.product_summary or f"{job.company_name}'s enterprise platform"
    coi = coi_metrics or calculate_coi_metrics(None)
    clean_company = job.company_name.split()[0].replace(",", "").strip()

    if is_disqualified:
        score = 25
        justification = (
            f"Scores {score}/100 [DISQUALIFIED]: Role '{job.role_title}' contains non-tech or non-GTM keywords "
            f"and does not qualify for enterprise GTM Engineering or RevOps execution."
        )
    else:
        score_map = {
            "GTM Engineer": 95,
            "AI Operations / Technical RevOps": 93,
            "AI Revenue Architect / RevOps": 92,
            "Growth Engineer": 92,
            "Founding / Outbound AE": 88,
        }
        score = score_map.get(archetype, 85)
        justification = (
            f"Scores {score}/100: Matches Jae Carter's {archetype} track combining enterprise outbound rigor "
            f"(Workday Federal $18M+ pipeline / AlphaSense AI Voice Agents) with production GTM systems engineering (Python, TypeScript, Gemini SDK, SQLite WAL daemons)."
        )

    import urllib.parse
    portfolio_deep_link = f"https://jae-carter.vercel.app/?company={urllib.parse.quote(job.company_name)}"

    return JobSynthesisResult(
        role_archetype=archetype,
        fit_score=score,
        fit_justification=justification,
        salary_range=job.salary_range or "$170,000 – $215,000 + Equity (Est.)",
        jd_hard_skill_keywords=[
            "Python",
            "TypeScript",
            "Clay",
            "Webhooks",
            "REST APIs",
            "Salesforce",
            "HubSpot",
            "SQL",
            "LLM Orchestration",
            "MEDDICC",
        ],
        sample_icp_target_company="Stripe",
        sample_icp_target_domain="stripe.com",
        top_down_trigger=(
            f"Following {job.company_name}'s {funding}, leadership is scaling {product} into enterprise accounts "
            f"while keeping GTM and operations headcount lean through systems automation."
        ),
        bottom_up_trigger=(
            f"The {job.role_title} mandate requires eliminating manual webhook/CRM handoff latency, unverified enrichment spend, "
            f"and brittle point-to-point scripts across the revenue stack."
        ),
        com_summary=[
            (
                f"Current State: {job.company_name}'s revenue and operations teams manage fragmented enrichment signals, manual account research, "
                f"and non-idempotent CRM syncs while scaling {product}."
            ),
            (
                "Negative Consequences: High-intent enterprise signals stall in manual triage queues, operators burn 30%+ of sprint capacity "
                "maintaining brittle glue scripts, and outbound sequences lack signal-verified technical framing."
            ),
            (
                "Positive Business Outcomes & Required Capabilities: By deploying an event-driven Clay -> Python/SQLite WAL -> Gemini 2.5 -> CRM/Notion "
                f"pipeline on Day 1, {job.company_name} compresses account brief synthesis from 35 minutes to 14.2 seconds with 100% Pydantic schema enforcement."
            ),
        ],
        tailored_summary=(
            f"AI Systems & {archetype} combining enterprise revenue execution ($18M+ federal pipeline at Workday, institutional TMT research at AlphaSense) "
            f"with production Python and TypeScript engineering. Experienced in building multi-model AI voice agents (Gemini, OpenAI, ElevenLabs), "
            f"SQLite WAL durable webhook queues, and Pydantic-validated LLM synthesis pipelines tailored to {job.company_name}'s GTM stack."
        ),
        tailored_experience=[
            TailoredExperienceItem(
                company="Independent Systems Architecture",
                role="AI Systems & GTM Engineer",
                dates="June 2026 – Present",
                location="Chicago, IL",
                xyz_bullets=[
                    f"Architected an event-driven GTM Deal Desk and account synthesis engine (Python, Next.js, SQLite WAL, Notion API) that indexes 65+ enterprise ATS/EDGAR sources, executes 420+ multi-pass DNS/enrichment probes, and compiles 45+ 9-section MEDDICC + Command of the Message blueprints in under 15 seconds for targets like {job.company_name}.",
                    "Engineered an LLM evaluation and reliability harness across Gemini 2.5 Pro and Flash tiers with 100% Pydantic schema enforcement, KV-cached static corpus grounding, and exponential-jitter circuit breakers to eliminate malformed JSON in production.",
                    "Built a durable SQLite WAL task queue and asynchronous worker daemon with SHA-256 payload idempotency keys and dead-letter recovery, preventing duplicate CRM/Notion writes under burst webhook concurrency.",
                ],
            ),
            TailoredExperienceItem(
                company="AlphaSense",
                role="Channel & Customer Research Analyst",
                dates="August 2025 – June 2026",
                location="Chicago, IL",
                xyz_bullets=[
                    "Co-developed an autonomous AI Call Taker orchestrating Google Gemini, OpenAI, and ElevenLabs voice synthesis to conduct and transcribe primary expert research calls, scaling discovery capacity 10x across 40+ institutional TMT accounts and reducing manual prep latency by 60%.",
                    "Engineered automated Notion workflows and two-stage triage filters (lexical keyword screening followed by semantic conviction scoring) to qualify expert survey leads, validate source quality, and accelerate institutional research fulfillment.",
                    "Synthesized high-signal market intelligence, earnings call transcripts, and competitive vendor displacement triggers for enterprise technology, media, and telecom (TMT) clients in partnership with commercial and product teams.",
                ],
            ),
            TailoredExperienceItem(
                company="Workday",
                role="Sales Development Representative (Workday Federal)",
                dates="March 2022 – February 2025",
                location="Chicago, IL",
                xyz_bullets=[
                    "Generated $18M+ in qualified enterprise federal pipeline and drove $3.2M in closed-won ARR (122% of quota) across DoD and Civilian agencies by engineering signal-based procurement and appropriation tracking workflows.",
                    "Applied Challenger Sale commercial teaching, Command of the Message value matrices, and MEDDICC qualification to uncover ERP/HCM modernization pain points across multi-year FedRAMP procurement cycles, lifting cold-to-qualified conversion by 35%.",
                    "Multi-threaded across agency C-suite leadership, CIO/CISO modernization directors, and contracting gatekeepers to compress enterprise federal discovery cycles.",
                ],
            ),
            TailoredExperienceItem(
                company="Enterprise Mobility",
                role="Fleet Logistics Supervisor & Operations Coordinator",
                dates="2017 – February 2022",
                location="Chicago, IL",
                xyz_bullets=[
                    "Directed high-volume fleet logistics, inventory allocation, and multi-branch supply chain operations across the Chicago metro market, achieving 15%+ utilization gains and sustaining peak fleet utilization above 94%.",
                    "Analyzed regional demand telemetry, turnaround staging bottlenecks, and return surges to implement proactive midday inter-branch inventory rebalancing.",
                    "Promoted from Customer Service Representative to Supervisor; trained and led frontline coordinators on consultative customer engagement, account retention, and operational execution.",
                ],
            ),
        ],
        sample_prospect_email=(
            f"Subject: Eliminating manual operational drag at Stripe with {job.company_name}\n\n"
            f"Hi [VP of Engineering / Operations at Stripe],\n\n"
            f"As Stripe's platform and revenue teams scale this quarter, the hidden bottleneck usually isn't headcount—it's "
            f"the 25–35% capacity drag spent maintaining manual triage workflows and fragmented internal tooling.\n\n"
            f"{job.company_name} ({product}) eliminates that manual overhead so your team ships faster with deterministic enterprise governance.\n\n"
            f"Open to a 12-minute brief this week on how peer enterprise teams cut cycle latency by 3x with {job.company_name}?\n\n"
            f"Best,\nJae Carter"
        ),
        challenger_email=DualChallengerEmailBundle(
            subject=f"{clean_company}'s {job.role_title} — built a live {clean_company} systems prototype",
            option_a_permission_first=(
                f"{buyer_first} — saw {job.company_name} is scaling the {job.role_title} function alongside your {funding}.\n\n"
                f"Most teams scaling at this stage hit a wall between multi-source signal enrichment (Clay/webhooks/ATS triggers) and deterministic CRM or agent execution—burning ~{coi['hours_wasted_per_rep_week']} hrs/wk per operator on manual data plumbing.\n\n"
                f"At AlphaSense (where I co-built our Gemini/OpenAI/ElevenLabs voice research agent) and Workday Federal ($18M+ outbound pipeline), I engineered Python and TypeScript systems to eliminate that exact handoff latency.\n\n"
                f"I pre-configured a live Deal Desk & Pipeline Architecture instance mapped to {job.company_name}'s enterprise motion. Open to me sending over the link or walking through it for 15 minutes this week?\n\n"
                f"Best,\nJae Carter"
            ),
            option_b_asset_linked=(
                f"{buyer_first} — saw {job.company_name} is scaling the {job.role_title} function alongside your {funding}.\n\n"
                f"Most teams scaling at this stage hit a wall between multi-source signal enrichment (Clay/webhooks/ATS triggers) and deterministic CRM or agent execution—burning ~{coi['hours_wasted_per_rep_week']} hrs/wk per operator on manual data plumbing.\n\n"
                f"At AlphaSense (where I co-built our Gemini/OpenAI/ElevenLabs voice research agent) and Workday Federal ($18M+ outbound pipeline), I engineered Python and TypeScript systems to eliminate that exact handoff latency.\n\n"
                f"Rather than just sending a PDF resume, I pre-loaded a live Deal Desk & Pipeline Architecture workbench mapped to {job.company_name}'s enterprise motion:\n"
                f"[Live {job.company_name} Deal Desk & Architecture Workbench]({{{{BLUEPRINT_URL}}}})\n\n"
                f"Worth a 15-minute walkthrough this week to discuss how I can own this pipeline on Day 1?\n\n"
                f"Best,\nJae Carter"
            ),
            blueprint_public_url=portfolio_deep_link,
        ),
        linkedin_bundle=LinkedInOutboundBundle(
            connection_note=(
                f"Saw {clean_company} is hiring for {job.role_title}. "
                f"Co-built AI voice/research pipelines at AlphaSense & generated $18M+ pipeline at Workday Federal—built a live {clean_company} Deal Desk prototype. Would love to connect."
            )[:300].rstrip(),
            inmail_message=(
                f"Hi {buyer_first},\n\n"
                f"Saw {job.company_name} is scaling the {job.role_title} role. At AlphaSense (co-built our Gemini/OpenAI/ElevenLabs AI Call Taker) and Workday Federal ($18M+ pipeline), I engineered Python/TS systems that compress account synthesis from 35m to 14.2s.\n\n"
                f"Pre-loaded a live Deal Desk workbench for {job.company_name}: {portfolio_deep_link}\n\n"
                f"Open to a 10-minute walkthrough this week?"
            )[:600].rstrip(),
        ),
    )


def build_job_system_instruction() -> str:
    profile = load_master_profile()
    profile_json = json.dumps(profile, indent=2) if profile else "{}"

    return (
        "You are an elite GTM Systems Engineer, Enterprise Revenue Architect, and Challenger Outbound Strategist "
        "representing candidate **Jae Carter**.\n\n"
        "### Master Career Profile (Single Source of Truth — NEVER Fabricate Employers, Roles, or Dates):\n"
        f"{profile_json}\n\n"
        f"{challenger_email_instructions}\n\n"
        f"{RESUME_PROMPT_GUARDRAILS}\n\n"
        "### Positioning Rules Across the 5 Target Tracks:\n"
        "1. `GTM Engineer`: Emphasize Clay waterfalls, SQLite WAL local daemons, webhook orchestration, Gemini API structured outputs, Notion/CRM API.\n"
        "2. `AI Revenue Architect / RevOps`: Emphasize automated Deal Rooms, MEDDICC governance, CRM/Notion schema design, MCP/API integrations.\n"
        "3. `AI Operations / Technical RevOps`: Emphasize multi-model AI voice agents (AlphaSense Gemini+OpenAI+ElevenLabs), LLM evaluation harnesses, Pydantic schema enforcement, and idempotent operational pipelines.\n"
        "4. `Founding / Outbound AE`: Emphasize Workday Federal outbound rigor ($18M+ pipeline, $3.2M ARR), Challenger Sale, Command of the Message, self-sourced pipeline automation.\n"
        "5. `Growth Engineer`: Emphasize programmatic signal detection, multi-source enrichment, automated outbound conversion.\n\n"
        "### Mandatory Output Rules:\n"
        "1. `role_archetype` MUST be one of: 'GTM Engineer', 'AI Revenue Architect / RevOps', 'AI Operations / Technical RevOps', 'Founding / Outbound AE', 'Growth Engineer'.\n"
        "2. `fit_score` MUST be an integer 0-100 scoring alignment with Jae's stack and enterprise methodology.\n"
        "3. `fit_justification` MUST be 1–2 punchy sentences explaining why this role received this fit score based on specific stack and commercial overlap with Jae's background.\n"
        "4. `jd_hard_skill_keywords` MUST extract the Top 10 exact hard-skill nouns/tools/methodologies verbatim from the JD.\n"
        "5. `sample_icp_target_company` and `sample_icp_target_domain` MUST identify 1 real enterprise prospect company (and its domain) that the hiring startup wants to sell to.\n"
        "6. `top_down_trigger` (funding, product launch, enterprise expansion) and `bottom_up_trigger` (stack gap, manual SDR bottleneck, tech debt in JD) must be concrete.\n"
        "7. `com_summary` MUST be a 3-item list: item 1 starting with 'Current State: ', item 2 starting with 'Negative Consequences: ', and item 3 starting with 'Positive Business Outcomes & Required Capabilities: '.\n"
        "8. `tailored_summary` MUST be a sharp 2–3 sentence executive summary tailored to the target company and role. MUST use standard implied first-person syntax (e.g., 'AI Systems & GTM Engineer with experience...', NOT 'Jae Carter is a...'). NEVER use third-person pronouns ('he', 'his', 'him') or refer to the candidate by name in the summary or experience bullets.\n"
        "9. `tailored_experience` MUST include the verified companies from `master_career_profile.json` (`Independent Systems Architecture`, `AlphaSense`, `Workday`, `Enterprise Mobility`) with their exact `role`, `dates`, and `location`, and 3–4 bullets per company structured around measurable impact (Outcome + Quantified Metric + Mechanism) mirroring the target JD's exact terminology. VARY your starting action verbs (Architected, Engineered, Co-developed, Built, Generated, Orchestrated)—do NOT start every bullet with 'Accomplished'. Always highlight the AlphaSense AI Call Taker (Gemini + OpenAI + ElevenLabs) and the SQLite WAL + Pydantic LLM evaluation harness.\n"
        "10. `sample_prospect_email` MUST be a Challenger cold email (with Subject line) written ON BEHALF OF the hiring startup targeting `sample_icp_target_company`.\n"
        "11. `challenger_email` MUST follow the CHALLENGER COLD EMAIL RULES above. It is a `DualChallengerEmailBundle` containing:\n"
        "    - `subject`: Specific, peer-to-peer subject line referencing the company's role and live systems prototype.\n"
        "    - `option_a_permission_first`: ~95-115 words with ZERO LINKS (strict deliverability / spam protection). Cites company trigger, specific JD stack bottleneck, and Jae's AlphaSense AI voice agent + Workday Federal ($18M+ pipeline) background. Concludes with a soft permission CTA offering the live company-configured Deal Desk & Pipeline Workbench link.\n"
        "    - `option_b_asset_linked`: ~95-115 words with a direct link to the live interactive portfolio workbench pre-loaded for the company using `{{BLUEPRINT_URL}}` (e.g. `[Live Company Deal Desk & Architecture Workbench]({{BLUEPRINT_URL}})`).\n"
        "    - `blueprint_public_url`: Public URL `https://jae-carter.vercel.app/?company=<URL-encoded Company Name>`.\n"
        "12. In both email options of `challenger_email`, ground the operational bottleneck in realistic engineering/RevOps terms (e.g., webhook-to-CRM handoff latency, unverified enrichment drag, or ~10 hrs/wk per operator lost to manual data plumbing—avoid sounding like a generic SDR blast).\n"
        "13. `linkedin_bundle` MUST include `connection_note` (HARD LIMIT: Under 300 characters including spaces; peer-to-peer tone anchored in company news/stack) and `inmail_message` (HARD LIMIT: Under 600 characters including spaces; referencing AlphaSense AI Call Taker, Workday Federal $18M+ pipeline, and the live portfolio workbench).\n"
        "14. NEGATIVE CONSTRAINT: Under NO circumstances should any resume bullet, header, summary, or output field contain internal meta-labels or framework names such as 'Google XYZ', 'XYZ formula', 'XYZzy format', 'tailored for XYZ', 'Google XYZ Format', or prompt formatting instructions."

    )


def build_job_prompt(
    job: ClayJobInput,
    coi_metrics: Optional[Dict[str, Any]] = None,
    dossier: Optional[Any] = None,
) -> str:
    prompt = (
        "Synthesize a Day-1 GTM Execution Brief, ATS Hard-Skill Keywords, Sample Enterprise ICP Target, "
        "Tailored Executive Resume Experience, & Work Trial Deal Room for the following role:\n\n"

        f"- **Hiring Company**: {job.company_name}\n"
        f"- **Company URL**: {job.company_url or 'Not specified'}\n"
        f"- **Funding & Stage**: {job.funding_stage or 'Not specified'}\n"
        f"- **Product Summary**: {job.product_summary or 'Infer from company name and job description'}\n"
        f"- **Role Title**: {job.role_title}\n"
        f"- **Job Posting URL**: {job.job_url or 'Not specified'}\n"
        f"- **Economic Buyer / Hiring Manager**: {job.buyer_name_title or 'VP of Sales / Head of GTM'}\n"
        f"- **Buyer Email**: {job.buyer_email or 'Not specified'}\n"
        f"- **Buyer LinkedIn**: {job.buyer_linkedin or 'Not specified'}\n\n"
    )

    if dossier:
        headcount = getattr(dossier, "headcount_bracket", None) or (dossier.get("headcount_bracket") if isinstance(dossier, dict) else None)
        capital_stage = getattr(getattr(dossier, "capital", None), "funding_stage", None)
        total_funding = getattr(getattr(dossier, "capital", None), "total_funding_amount", None)
        headline = getattr(getattr(dossier, "news", None), "recent_headline", None)
        why_now = getattr(getattr(dossier, "news", None), "why_now_trigger", None)
        tech_stack = getattr(dossier, "verified_tech_stack", None)

        prompt += (
            "### Verified Company Dossier Signals:\n"
            f"- Headcount Bracket: {headcount or 'Growth Stage'}\n"
            f"- Capital: {capital_stage or 'Venture-backed'} ({total_funding or 'Undisclosed'})\n"
            f"- Strategic Trigger: {headline or 'Enterprise Scaling'}\n"
            f"- Why Now: {why_now or 'Accelerating GTM execution'}\n"
            f"- Technographics: {', '.join(tech_stack) if tech_stack else 'Salesforce, Modern Cloud Stack'}\n\n"
        )

    coi = coi_metrics or calculate_coi_metrics(dossier)
    clean_company = job.company_name.split()[0].replace(",", "").strip()

    lead_investors_str = "leading venture investors"
    recent_headline_str = "recent enterprise expansion"
    if dossier:
        lead_inv = getattr(getattr(dossier, "capital", None), "lead_investors", None) or (
            dossier.get("capital", {}).get("lead_investors") if isinstance(dossier, dict) else None
        )
        if lead_inv:
            lead_investors_str = ", ".join(lead_inv)
        headline = getattr(getattr(dossier, "news", None), "recent_headline", None) or (
            dossier.get("news", {}).get("recent_headline") if isinstance(dossier, dict) else None
        )
        if headline:
            recent_headline_str = headline

    import urllib.parse
    portfolio_deep_link = f"https://jae-carter.vercel.app/?company={urllib.parse.quote(job.company_name)}"

    challenger_prompt_section = f"""### OPERATIONAL LATENCY & CAPACITY CONTEXT:
- Company Scale: {coi['headcount_bracket']} ({coi['funding_stage']})
- Operator Capacity Drag: ~{coi['hours_wasted_per_rep_week']} hrs/week per GTM/RevOps operator spent on manual enrichment hygiene, webhook debugging, and non-idempotent CRM syncs (~{coi['annual_sdr_tax_str']}/yr loaded engineering/operator capacity)
- Deterministic Pipeline Benchmark: Compresses account brief synthesis from 35 minutes of manual research to 14.2 seconds with 100% Pydantic schema enforcement.

### PEER-TO-PEER OUTBOUND EMAIL GUIDELINES:
1. Subject line: "{clean_company}'s {job.role_title} — built a live {clean_company} systems prototype"
2. Hook: Reference {job.company_name} scaling the {job.role_title} function alongside their recent signal ({lead_investors_str} / "{recent_headline_str}").
3. Operational Bottleneck: Highlight the handoff wall between multi-source signal enrichment (Clay/webhooks/ATS triggers) and deterministic CRM or agent execution (~{coi['hours_wasted_per_rep_week']} hrs/wk per operator lost to manual data plumbing).
4. Technical Proof: Cite Jae's work at AlphaSense (co-developing the Gemini/OpenAI/ElevenLabs AI Call Taker that scaled research 10x) and Workday Federal ($18M+ outbound pipeline), plus his event-driven Python/TypeScript Deal Desk architecture.
5. Call to Action:
   - Option A (Permission-First, Zero Links): Offer to send over the live {job.company_name}-configured Deal Desk & Pipeline Architecture link or walk through it for 15 minutes.
   - Option B (Asset-Linked): Link directly to `{{{{BLUEPRINT_URL}}}}` (set `blueprint_public_url` to `{portfolio_deep_link}`).
"""
    linkedin_instructions = f"""### MULTI-CHANNEL LINKEDIN ASSETS:
1. LinkedIn Connection Request Note:
   - HARD LIMIT: Under 300 characters (including spaces).
   - Tone: Executive peer-to-peer, not an applicant pitch.
   - Anchor: Cite recent signal from dossier ({recent_headline_str}) or common stack.
   - Example style: "Noticed Toast's push into enterprise POS APIs. Scaled similar enrichment waterfalls at Workday & AlphaSense to eliminate manual rep research. Would love to connect."

2. LinkedIn InMail:
   - HARD LIMIT: Under 600 characters (including spaces).
   - Hook: Reference the ~{coi['headcount_bracket']} team scale and ~{coi['annual_sdr_tax_str']} manual research drag.
   - Value: Mention automating Clay waterfalls, local LLM daemons, and CRM routing.
   - CTA: Open to a 10-minute peer walkthrough of the architecture.
"""
    prompt += challenger_prompt_section
    prompt += "\n" + linkedin_instructions
    prompt += f"\n### Job Description:\n{job.job_description}\n"
    prompt += "\n" + RESUME_PROMPT_GUARDRAILS + "\n"
    return prompt



def synthesize_job_opportunity(
    job: ClayJobInput,
    force_offline: bool = False,
    preferred_model: Optional[str] = None,
    dossier: Optional[Any] = None,
    coi_metrics: Optional[Dict[str, Any]] = None,
) -> JobSynthesisResult:
    """Calls Gemini with Structured Outputs enabled and exponential backoff / model cascade."""
    load_dotenv(ENV_PATH)
    api_key = (os.environ.get("GEMINI_API_KEY") or "").strip().strip('"').strip("'")
    if force_offline or not api_key or api_key.startswith("${"):
        print("[*] Running deterministic fallback synthesis (offline/no API key).")
        return _deterministic_job_fallback(job, coi_metrics=coi_metrics)

    client = genai.Client(api_key=api_key)
    system_instruction = build_job_system_instruction()
    prompt = build_job_prompt(job, coi_metrics=coi_metrics, dossier=dossier)

    # Context Caching: Retrieve active cache (if token threshold is met)
    cached_content_name = ""
    try:
        cached_content_name = get_or_create_career_cache(client, ttl_hours=12)
    except Exception as cache_err:
        print(f"[!] Notice checking career cache: {cache_err}")

    env_model = os.environ.get("JOB_ENGINE_MODEL", "").strip()
    candidate_models: List[str] = []
    for m in [
        preferred_model,
        env_model,
        "gemini-2.5-pro",
        "gemini-3-flash-preview",
        "gemini-2.5-flash",
    ]:
        if m and m not in candidate_models:
            candidate_models.append(m.removeprefix("models/"))

    last_error: Optional[Exception] = None
    for model_name in candidate_models:
        for attempt in range(2):
            try:
                print(f"[*] Synthesizing Job Deal Room for '{job.company_name} — {job.role_title}' via {model_name}...")

                if cached_content_name and model_name == "gemini-2.5-flash":
                    # 1. EXPLICIT CACHE PATH: Static career corpus is cached in KV memory for gemini-2.5-flash
                    response = client.models.generate_content(
                        model=model_name,
                        contents=prompt,
                        config=types.GenerateContentConfig(
                            cached_content=cached_content_name,
                            temperature=0.2,
                            response_mime_type="application/json",
                            response_schema=JobSynthesisResult,
                        ),
                    )
                else:
                    # 2. DIRECT SYSTEM-INSTRUCTION PATH (used for gemini-2.5-pro & fallback models):
                    # system_instruction already embeds master_career_profile.json; avoid prepending the 236k-token
                    # padded cache file which exceeds the 200k-token standard context tier on gemini-2.5-pro.
                    response = client.models.generate_content(
                        model=model_name,
                        contents=prompt,
                        config=types.GenerateContentConfig(
                            system_instruction=system_instruction,
                            temperature=0.2,
                            response_mime_type="application/json",
                            response_schema=JobSynthesisResult,
                        ),
                    )

                # Verify cache utilization in usage metadata
                if hasattr(response, "usage_metadata"):
                    usage = response.usage_metadata
                    prompt_tokens = getattr(usage, "prompt_token_count", 0)
                    cached_tokens = getattr(usage, "cached_content_token_count", 0)
                    print(f"[TELEMETRY] Token Telemetry -> Prompt Tokens: {prompt_tokens} | Cached Tokens Read: {cached_tokens}")

                result = JobSynthesisResult.model_validate_json(response.text)
                if not result.linkedin_bundle:
                    fb = _deterministic_job_fallback(job, coi_metrics=coi_metrics)
                    result.linkedin_bundle = fb.linkedin_bundle
                if result.linkedin_bundle:
                    result.linkedin_bundle = sanitize_linkedin_bundle(result.linkedin_bundle)
                print(
                    f"[+] Synthesis complete | Archetype: {result.role_archetype} | Fit Score: {result.fit_score}/100"
                )
                return result
            except Exception as exc:
                last_error = exc
                err_str = str(exc).upper()
                if "404" in err_str or "NOT_FOUND" in err_str or "LIMIT: 0" in err_str:
                    print(f"[!] Model {model_name} unavailable on current key tier; cascading to fallback model...")
                    break
                if attempt == 0 and ("429" in err_str or "503" in err_str or "RESOURCE_EXHAUSTED" in err_str):
                    backoff = 2 ** (attempt + 1)
                    print(f"[!] Rate limit/spike on {model_name}; retrying in {backoff}s...")
                    time.sleep(backoff)
                    continue
                print(f"[!] Model {model_name} error ({exc}); cascading to next model...")
                break

    print(f"[!] All Gemini models failed ({last_error}); falling back to deterministic synthesis.")
    return _deterministic_job_fallback(job, coi_metrics=coi_metrics)


# ---------------------------------------------------------------------------
# Notion Block Helpers & Deal Room Writer (Strict [:1990] Truncation / Chunking)
# ---------------------------------------------------------------------------


def _rt(content: str, bold: bool = False, link_url: Optional[str] = None) -> Dict[str, Any]:
    """Enforces Notion's 2,000-character limit per text object by truncating at [:1990]."""
    text_obj: Dict[str, Any] = {"content": str(content)[:1990]}
    if link_url and link_url.startswith(("http://", "https://")):
        text_obj["link"] = {"url": link_url}
    return {
        "type": "text",
        "text": text_obj,
        "annotations": {"bold": bold},
    }


def _chunked_rich_text(content: str, chunk_size: int = 1900) -> List[Dict[str, Any]]:
    """Splits long strings (such as a full Markdown resume) into <=1900-char rich_text objects so nothing is lost."""
    text = str(content)
    if not text:
        return [_rt("")]
    return [
        {
            "type": "text",
            "text": {"content": text[i : i + chunk_size]},
        }
        for i in range(0, len(text), chunk_size)
    ]


def _heading_2(title: str) -> Dict[str, Any]:
    return {
        "object": "block",
        "type": "heading_2",
        "heading_2": {"rich_text": [_rt(title)]},
    }


def _heading_3(title: str) -> Dict[str, Any]:
    return {
        "object": "block",
        "type": "heading_3",
        "heading_3": {"rich_text": [_rt(title)]},
    }


def _company_logo_icon(company_or_domain: str = "") -> Dict[str, Any]:
    raw = (company_or_domain or "").strip()
    dom = raw if ("." in raw) else f"{re.sub(r'[^a-zA-Z0-9]', '', raw.lower())}.com"
    clean_dom = clean_domain(dom) if dom else "vercel.com"
    logo_url = f"https://unavatar.io/{clean_dom}?fallback=https://www.google.com/s2/favicons?domain={clean_dom}&sz=128"
    return {"type": "external", "external": {"url": logo_url}}


def _callout(label: str, body: str, color: str = "gray_background", link_url: Optional[str] = None, icon: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    callout_dict: Dict[str, Any] = {
        "rich_text": [
            _rt(f"{label}: ", bold=True),
            _rt(body, bold=False, link_url=link_url),
        ],
        "color": color,
    }
    if icon:
        callout_dict["icon"] = icon
    return {
        "object": "block",
        "type": "callout",
        "callout": callout_dict,
    }


def _labeled_bullet(label: str, body: str) -> Dict[str, Any]:
    return {
        "object": "block",
        "type": "bulleted_list_item",
        "bulleted_list_item": {
            "rich_text": [
                _rt(f"{label}: ", bold=True),
                _rt(body, bold=False),
            ]
        },
    }


def _simple_bullet(body: str) -> Dict[str, Any]:
    if ":" in body:
        prefix, rest = body.split(":", 1)
        if len(prefix.strip()) <= 55:
            return _labeled_bullet(prefix.strip(), rest.strip())
    return {
        "object": "block",
        "type": "bulleted_list_item",
        "bulleted_list_item": {"rich_text": [_rt(body)]},
    }


def _toggle(title: str, children: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "object": "block",
        "type": "toggle",
        "toggle": {
            "rich_text": [_rt(title, bold=True)],
            "children": children,
        },
    }


def _toggle_heading_2(
    title: str,
    children: List[Dict[str, Any]],
    color: str = "default",
) -> Dict[str, Any]:
    """Creates a collapsible heading_2 block in Notion with nested children."""
    return {
        "object": "block",
        "type": "heading_2",
        "heading_2": {
            "rich_text": [_rt(title, bold=True)],
            "is_toggleable": True,
            "color": color,
            "children": children,
        },
    }


def _toggle_heading_3(
    title: str,
    children: List[Dict[str, Any]],
    color: str = "default",
) -> Dict[str, Any]:
    """Creates a collapsible heading_3 block in Notion with nested children."""
    return {
        "object": "block",
        "type": "heading_3",
        "heading_3": {
            "rich_text": [_rt(title, bold=True)],
            "is_toggleable": True,
            "color": color,
            "children": children,
        },
    }


def _code_block(content: str, language: str = "plain text") -> Dict[str, Any]:
    return {
        "object": "block",
        "type": "code",
        "code": {
            "language": language,
            "rich_text": _chunked_rich_text(content, chunk_size=1900),
        },
    }


def _divider() -> Dict[str, Any]:
    return {"object": "block", "type": "divider", "divider": {}}


def upload_pdf_to_notion(pdf_path: str, token: str) -> Optional[str]:
    """
    Uploads a local PDF binary directly to Notion via /v1/file_uploads and /v1/file_uploads/{id}/send.
    Returns the Notion file_upload ID on success.
    """
    if not pdf_path or not os.path.isfile(pdf_path) or not token:
        return None
    filename = os.path.basename(pdf_path)
    try:
        create_resp = requests.post(
            "https://api.notion.com/v1/file_uploads",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Notion-Version": NOTION_VERSION,
            },
            json={"filename": filename, "content_type": "application/pdf"},
            timeout=20,
        )
        if create_resp.status_code != 200:
            print(f"[!] Notion file_uploads init warning ({create_resp.status_code}): {create_resp.text}")
            return None
        upload_id = create_resp.json().get("id")
        if not upload_id:
            return None

        with open(pdf_path, "rb") as f:
            send_resp = requests.post(
                f"https://api.notion.com/v1/file_uploads/{upload_id}/send",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Notion-Version": NOTION_VERSION,
                },
                files={"file": (filename, f, "application/pdf")},
                timeout=35,
            )
        if send_resp.status_code != 200:
            print(f"[!] Notion file_uploads send warning ({send_resp.status_code}): {send_resp.text}")
            return None

        print(f"[+] Uploaded PDF binary to Notion (/v1/file_uploads) -> ID: {upload_id}")
        return upload_id
    except Exception as exc:
        print(f"[!] Warning: Notion PDF file upload failed: {exc}")
        return None


def _coi_snapshot_callout(coi: Dict[str, Any], company_name: str = "") -> Dict[str, Any]:
    """
    Renders the dedicated Quantified Cost of Inaction (COI) Snapshot callout block right above the email draft:
    > Quantified Cost of Inaction (COI) Snapshot
    > • Annual Manual Research Tax: {coi['annual_sdr_tax_str']}/year
    > • Monthly Capacity Drain: {coi['monthly_drag_str']}/month (~{coi['estimated_reps']} reps × {coi['hours_wasted_per_rep_week']} hrs/wk)
    > • 90-Day Cost of Status Quo: {coi['quarterly_risk_str']} in delayed pipeline velocity
    """
    return {
        "object": "block",
        "type": "callout",
        "callout": {
            "icon": _company_logo_icon(company_name),
            "rich_text": [
                _rt("Quantified Cost of Inaction (COI) Snapshot\n", bold=True),
                _rt("• "),
                _rt("Annual Manual Research Tax: ", bold=True),
                _rt(f"{coi.get('annual_sdr_tax_str', '$224,640')}/year\n"),
                _rt("• "),
                _rt("Monthly Capacity Drain: ", bold=True),
                _rt(f"{coi.get('monthly_drag_str', '$18,720')}/month (~{coi.get('estimated_reps', 8)} reps × {coi.get('hours_wasted_per_rep_week', 12)} hrs/wk)\n"),
                _rt("• "),
                _rt("90-Day Cost of Status Quo: ", bold=True),
                _rt(f"{coi.get('quarterly_risk_str', '$56,160')} in delayed pipeline velocity"),
            ],
            "color": "red_background",
        },
    }


def build_linkedin_notion_blocks(bundle: LinkedInOutboundBundle, buyer_name: str, company_name: str = "") -> list:
    """Builds clean Notion blocks for LinkedIn multi-channel touches."""
    icon_obj = _company_logo_icon(company_name)
    return [
        {
            "object": "block",
            "type": "heading_3",
            "heading_3": {
                "rich_text": [{"type": "text", "text": {"content": "Multi-Channel Touches (LinkedIn)"}}]
            }
        },
        {
            "object": "block",
            "type": "callout",
            "callout": {
                "icon": icon_obj,
                "rich_text": [
                    {"type": "text", "text": {"content": "Connection Request (Max 300 chars):\n"}, "annotations": {"bold": True}},
                    {"type": "text", "text": {"content": f'"{bundle.connection_note}"\n'}},
                    {"type": "text", "text": {"content": f"Length: {len(bundle.connection_note)}/300 chars | Recipient: {buyer_name}"}, "annotations": {"italic": True, "color": "gray"}}
                ]
            }
        },
        {
            "object": "block",
            "type": "callout",
            "callout": {
                "icon": icon_obj,
                "rich_text": [
                    {"type": "text", "text": {"content": "Executive InMail (Max 600 chars):\n"}, "annotations": {"bold": True}},
                    {"type": "text", "text": {"content": f'"{bundle.inmail_message}"\n'}},
                    {"type": "text", "text": {"content": f"Length: {len(bundle.inmail_message)}/600 chars | Recipient: {buyer_name}"}, "annotations": {"italic": True, "color": "gray"}}
                ]
            }
        }
    ]


def build_outbound_suite_blocks(
    challenger_email_subject: str,
    challenger_email_body: str,
    linkedin_bundle: Any,
    buyer_name: str,
    challenger_email_option_b: Optional[str] = None,
    company_name: str = "",
) -> list:
    """Builds collapsible outbound assets with 1-click copy callouts for dual email drafts."""
    icon_obj = _company_logo_icon(company_name)
    conn_note = getattr(linkedin_bundle, "connection_note", "") if linkedin_bundle else ""
    inmail_msg = getattr(linkedin_bundle, "inmail_message", "") if linkedin_bundle else ""
    if not conn_note:
        conn_note = f"Noticed {buyer_name}'s GTM scaling work. Built automated Deal Desk pipelines at Workday & AlphaSense. Would love to connect."[:300]
    if not inmail_msg:
        inmail_msg = f"Noticed {buyer_name}'s team scale. Architected automated Clay waterfalls and local webhook daemons eliminating manual research drag. Open to a 10-minute peer walkthrough?"[:600]

    email_callouts = [
        {
            "object": "block",
            "type": "callout",
            "callout": {
                "icon": icon_obj,
                "color": "gray_background",
                "rich_text": [
                    {"type": "text", "text": {"content": "Option A (Permission-First, Zero Links):\n"}, "annotations": {"bold": True}},
                    {"type": "text", "text": {"content": f"Subject: {challenger_email_subject}\n\n"}, "annotations": {"bold": True}},
                    {"type": "text", "text": {"content": sanitize_cold_email(challenger_email_body)[:1800]}}
                ]
            }
        }
    ]
    if challenger_email_option_b:
        email_callouts.append(
            {
                "object": "block",
                "type": "callout",
                "callout": {
                    "icon": icon_obj,
                    "color": "purple_background",
                    "rich_text": [
                        {"type": "text", "text": {"content": "Option B (Asset-Linked, Proof-of-Work):\n"}, "annotations": {"bold": True}},
                        {"type": "text", "text": {"content": f"Subject: {challenger_email_subject}\n\n"}, "annotations": {"bold": True}},
                        {"type": "text", "text": {"content": sanitize_cold_email(challenger_email_option_b)[:1800]}}
                    ]
                }
            }
        )

    return [
        {
            "object": "block",
            "type": "divider",
            "divider": {}
        },
        # Collapsible Email Section
        {
            "object": "block",
            "type": "heading_2",
            "heading_2": {
                "is_toggleable": True,
                "rich_text": [{"type": "text", "text": {"content": f"Challenger Cold Email Drafts — \"{challenger_email_subject[:60]}\""}}],
                "children": email_callouts
            }
        }
    ]


def build_collapsible_resume_block(
    resume_markdown: str,
    pdf_filename: str,
    notion_file_upload_id: Optional[str] = None,
    company_name: str = "",
) -> list:
    """Collapses raw resume text into an accordion while highlighting the PDF artifact."""
    icon_obj = _company_logo_icon(company_name)
    children = [
        {
            "object": "block",
            "type": "callout",
            "callout": {
                "icon": icon_obj,
                "color": "green_background",
                "rich_text": [
                    {"type": "text", "text": {"content": "ATS Optimization Verified\n"}, "annotations": {"bold": True}},
                    {"type": "text", "text": {"content": f"Compiled PDF stored in output/resumes/{pdf_filename} and synced to page properties."}}
                ]
            }
        }
    ]
    if notion_file_upload_id:
        children.append(
            {
                "object": "block",
                "type": "pdf",
                "pdf": {
                    "type": "file_upload",
                    "file_upload": {"id": notion_file_upload_id},
                },
            }
        )
    resume_native_blocks = markdown_resume_to_notion_blocks(resume_markdown)
    children.extend(resume_native_blocks)

    return [
        {
            "object": "block",
            "type": "divider",
            "divider": {}
        },
        {
            "object": "block",
            "type": "heading_2",
            "heading_2": {
                "is_toggleable": True,
                "rich_text": [{"type": "text", "text": {"content": f"Tailored ATS Resume ({pdf_filename})"}}],
                "children": children
            }
        }
    ]


def build_top_dashboard_columns(
    dossier: Optional[Any] = None,
    coi: Optional[Dict[str, Any]] = None,
    job: Optional[ClayJobInput] = None,
) -> Dict[str, Any]:
    """Creates a side-by-side executive dashboard at the top of the Deal Room."""
    def _val(obj, *keys, default=None):
        curr = obj
        for k in keys:
            if curr is None:
                return default
            if isinstance(curr, dict):
                curr = curr.get(k)
            else:
                curr = getattr(curr, k, None)
        return curr if curr is not None else default

    funding_str = _val(dossier, "capital", "total_funding_amount") or "Undisclosed"
    stage_str = (
        _val(dossier, "capital", "funding_stage")
        or (job.funding_stage if job and getattr(job, "funding_stage", None) else None)
        or "Growth"
    )
    investors_raw = _val(dossier, "capital", "lead_investors", default=[])
    investors = ", ".join(investors_raw) if (isinstance(investors_raw, list) and investors_raw) else "N/A"

    buyer_name = (
        _val(dossier, "leadership", "economic_buyer_name")
        or (job.buyer_name_title.split(",")[0].strip() if job and getattr(job, "buyer_name_title", None) else None)
        or "Revenue Leadership"
    )
    buyer_title = (
        _val(dossier, "leadership", "economic_buyer_title")
        or (job.buyer_name_title.split(",")[1].strip() if job and getattr(job, "buyer_name_title", None) and "," in job.buyer_name_title else None)
        or "VP of Sales / CRO"
    )
    headline = _val(dossier, "news", "recent_headline") or "Active expansion"

    coi_dict = coi or {}
    annual_tax = coi_dict.get("annual_sdr_tax_str", "$0")
    monthly_drag = coi_dict.get("monthly_drag_str", "$0")
    estimated_reps = coi_dict.get("estimated_reps", 0)
    hours_wasted = coi_dict.get("hours_wasted_per_rep_week", 0)
    quarterly_risk = coi_dict.get("quarterly_risk_str", "$0")

    co_name = _val(dossier, "company_name") or (job.company_name if job else "")
    icon_obj = _company_logo_icon(co_name)

    return {
        "object": "block",
        "type": "column_list",
        "column_list": {
            "children": [
                # COLUMN 1: Company Intelligence & Signals (Left)
                {
                    "object": "block",
                    "type": "column",
                    "column": {
                        "children": [
                            {
                                "object": "block",
                                "type": "callout",
                                "callout": {
                                    "icon": icon_obj,
                                    "color": "blue_background",
                                    "rich_text": [
                                        {"type": "text", "text": {"content": "Company Signal Radar\n"}, "annotations": {"bold": True}},
                                        {"type": "text", "text": {"content": f"• Stage: {stage_str} ({funding_str})\n"}},
                                        {"type": "text", "text": {"content": f"• Investors: {investors}\n"}},
                                        {"type": "text", "text": {"content": f"• Buyer: {buyer_name} ({buyer_title})\n"}},
                                        {"type": "text", "text": {"content": f"• Signal: {headline[:250]}"}}
                                    ]
                                }
                            }
                        ]
                    }
                },
                # COLUMN 2: Quantified COI Financial Drag (Right)
                {
                    "object": "block",
                    "type": "column",
                    "column": {
                        "children": [
                            {
                                "object": "block",
                                "type": "callout",
                                "callout": {
                                    "icon": icon_obj,
                                    "color": "red_background",
                                    "rich_text": [
                                        {"type": "text", "text": {"content": "Cost of Inaction (COI)\n"}, "annotations": {"bold": True}},
                                        {"type": "text", "text": {"content": f"• Annual Tax: {annual_tax}/yr\n"}},
                                        {"type": "text", "text": {"content": f"• Monthly Drag: {monthly_drag}/mo\n"}},
                                        {"type": "text", "text": {"content": f"• Team Impact: ~{estimated_reps} reps × {hours_wasted}h/wk\n"}},
                                        {"type": "text", "text": {"content": f"• 90-Day Delay: {quarterly_risk}"}}
                                    ]
                                }
                            }
                        ]
                    }
                }
            ]
        }
    }


# Backwards compatibility alias
build_top_intelligence_callout = build_top_dashboard_columns


def build_dual_outbound_accordion(
    email_bundle: DualChallengerEmailBundle,
    company_name: str,
    blueprint_url: str = "",
) -> dict:
    """Builds a toggle accordion containing both Option A and Option B cold email drafts."""
    import urllib.parse
    default_portfolio_link = f"https://jae-carter.vercel.app/?company={urllib.parse.quote(company_name or 'Target')}"
    target_link = blueprint_url if blueprint_url else default_portfolio_link
    if "github.com/jaecarter" in target_link:
        target_link = default_portfolio_link

    clean_option_a = sanitize_email_text(email_bundle.option_a_permission_first, target_link)
    clean_option_b = sanitize_email_text(email_bundle.option_b_asset_linked, target_link)

    if target_link not in clean_option_b:
        clean_option_b += f"\n\n[Live {company_name} Deal Desk & Architecture Workbench]({target_link})"

    icon_obj = _company_logo_icon(company_name)

    return {
        "object": "block",
        "type": "heading_2",
        "heading_2": {
            "is_toggleable": True,
            "rich_text": [{"type": "text", "text": {"content": f"Challenger Cold Email Drafts (Dual-Track) — \"{email_bundle.subject[:60]}\""}}],
            "children": [
                # Option A: Permission-First (Deliverability)
                {
                    "object": "block",
                    "type": "callout",
                    "callout": {
                        "icon": icon_obj,
                        "color": "gray_background",
                        "rich_text": [
                            {"type": "text", "text": {"content": "Option A: Permission-First (Zero Links / Deliverability Optimized)\n"}, "annotations": {"bold": True}},
                            {"type": "text", "text": {"content": "Best for cold domain outreach and avoiding spam filters. Offers the teardown as an opt-in.\n\n"}, "annotations": {"italic": True, "color": "gray"}},
                            {"type": "text", "text": {"content": f"Subject: {email_bundle.subject}\n\n"}, "annotations": {"bold": True}},
                            {"type": "text", "text": {"content": clean_option_a[:1900]}}
                        ]
                    }
                },
                # Option B: Asset-Linked (High-Conviction)
                {
                    "object": "block",
                    "type": "callout",
                    "callout": {
                        "icon": icon_obj,
                        "color": "blue_background",
                        "rich_text": [
                            {"type": "text", "text": {"content": "Option B: Asset-Linked (High-Conviction / Work-Trial Attached)\n"}, "annotations": {"bold": True}},
                            {"type": "text", "text": {"content": "Includes direct link to the 3-Account Waterfall Architecture & CRM JSON Webhook payload.\n\n"}, "annotations": {"italic": True, "color": "gray"}},
                            {"type": "text", "text": {"content": f"Subject: {email_bundle.subject}\n\n"}, "annotations": {"bold": True}},
                            {"type": "text", "text": {"content": clean_option_b[:1900]}}
                        ]
                    }
                }
            ]
        }
    }


def render_full_resume_as_notion_blocks(resume_markdown: str, pdf_filename: str, company_name: str = "") -> list:
    """
    Parses the complete resume markdown into native, styled Notion blocks
    inside the toggle accordion so the full resume is embedded on canvas.
    """
    icon_obj = _company_logo_icon(company_name)
    resume_children = [
        {
            "object": "block",
            "type": "callout",
            "callout": {
                "icon": icon_obj,
                "color": "green_background",
                "rich_text": [
                    {"type": "text", "text": {"content": f"Tailored ATS Resume — {pdf_filename}\n"}, "annotations": {"bold": True}},
                    {"type": "text", "text": {"content": "Full text rendered below. Standalone PDF compiled in output/resumes/."}}
                ]
            }
        }
    ]

    clean_md = sanitize_resume_markdown(resume_markdown.strip()) if resume_markdown else ""

    for line in clean_md.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue

        if stripped.startswith("# "):
            resume_children.append({
                "object": "block",
                "type": "heading_2",
                "heading_2": {
                    "rich_text": [{"type": "text", "text": {"content": stripped[2:].strip()}, "annotations": {"bold": True}}]
                }
            })
        elif stripped.startswith("## "):
            resume_children.append({
                "object": "block",
                "type": "heading_3",
                "heading_3": {
                    "rich_text": [{"type": "text", "text": {"content": stripped[3:].strip()}, "annotations": {"bold": True}}]
                }
            })
        elif stripped.startswith("### "):
            resume_children.append({
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [{"type": "text", "text": {"content": stripped[4:].strip()}, "annotations": {"bold": True}}]
                }
            })
        elif stripped.startswith("- ") or stripped.startswith("* "):
            bullet_text = stripped[2:].strip()
            resume_children.append({
                "object": "block",
                "type": "bulleted_list_item",
                "bulleted_list_item": {
                    "rich_text": [{"type": "text", "text": {"content": bullet_text[:1900]}}]
                }
            })
        else:
            resume_children.append({
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [{"type": "text", "text": {"content": stripped[:1900]}}]
                }
            })

    return [
        {
            "object": "block",
            "type": "heading_2",
            "heading_2": {
                "is_toggleable": True,
                "rich_text": [{"type": "text", "text": {"content": f"Tailored ATS Resume ({pdf_filename})"}}],
                "children": resume_children[:95]  # Notion block child limit
            }
        }
    ]


def build_full_depth_deal_room_blocks(
    dossier: Any,
    coi: dict,
    role_mandate: str,
    experience_transfer: list,  # List of strings or tuples (Pillar, Detail)
    why_now_top_down: str,
    why_now_bottom_up: str,
    challenger_subject: str,
    challenger_body: str,
    linkedin_bundle: Any,
    resume_markdown: str,
    pdf_filename: str,
    work_trial_target_company: Optional[str] = None,
    work_trial_email: Optional[str] = None,
    blitz_deal_desk_url: Optional[str] = None,
    company_name: Optional[str] = None,
    challenger_option_b: Optional[str] = None,
    email_bundle: Optional[DualChallengerEmailBundle] = None,
    blueprint_url: str = "",
    buyer_email: Optional[str] = None,
    buyer_linkedin_url: Optional[str] = None,
    job_url: Optional[str] = None,
    imap_synced: bool = False,
) -> list:
    """Builds a full-context Notion Deal Room with zero information loss."""

    def _val(obj, *keys, default=None):
        curr = obj
        for k in keys:
            if curr is None:
                return default
            if isinstance(curr, dict):
                curr = curr.get(k)
            else:
                curr = getattr(curr, k, None)
        return curr if curr is not None else default

    buyer_name = _val(dossier, "leadership", "economic_buyer_name") or "Revenue Leadership"
    buyer_title = _val(dossier, "leadership", "economic_buyer_title") or "VP of Sales / CRO"
    funding_str = _val(dossier, "capital", "total_funding_amount") or "Undisclosed"
    stage_str = _val(dossier, "capital", "funding_stage") or "Growth"
    investors_raw = _val(dossier, "capital", "lead_investors", default=[])
    investors = ", ".join(investors_raw) if (isinstance(investors_raw, list) and investors_raw) else str(investors_raw or "N/A")
    headline = _val(dossier, "news", "recent_headline") or "Active expansion"

    coi_dict = coi or {}
    annual_tax = coi_dict.get("annual_sdr_tax_str", "$0")
    monthly_drag = coi_dict.get("monthly_drag_str", "$0")
    estimated_reps = coi_dict.get("estimated_reps", 0)
    hours_wasted = coi_dict.get("hours_wasted_per_rep_week", 0)
    quarterly_risk = coi_dict.get("quarterly_risk_str", "$0")

    conn_note = getattr(linkedin_bundle, "connection_note", "") or (linkedin_bundle.get("connection_note", "") if isinstance(linkedin_bundle, dict) else "")
    inmail = getattr(linkedin_bundle, "inmail_message", "") or (linkedin_bundle.get("inmail_message", "") if isinstance(linkedin_bundle, dict) else "")

    # Format Experience Transfer bullets
    transfer_children = []
    for item in experience_transfer:
        if isinstance(item, (tuple, list)) and len(item) == 2:
            title, desc = item
            transfer_children.append({
                "object": "block",
                "type": "bulleted_list_item",
                "bulleted_list_item": {
                    "rich_text": [
                        {"type": "text", "text": {"content": f"{title}: "}, "annotations": {"bold": True}},
                        {"type": "text", "text": {"content": str(desc)[:1900]}}
                    ]
                }
            })
        else:
            transfer_children.append({
                "object": "block",
                "type": "bulleted_list_item",
                "bulleted_list_item": {
                    "rich_text": [{"type": "text", "text": {"content": str(item)[:1900]}}]
                }
            })

    # Clean and sanitize tailored resume markdown
    clean_resume = sanitize_resume_markdown(resume_markdown.strip()) if resume_markdown else "No resume markdown provided."
    clean_resume = clean_resume.strip()

    co_name = company_name or "Target"
    icon_obj = _company_logo_icon(co_name)

    # 0. 1-CLICK EXECUTIVE ACTION LAUNCHPAD
    action_launchpad_rich_text = [
        {"type": "text", "text": {"content": "⚡ 1-Click Executive Action Launchpad\n"}, "annotations": {"bold": True}},
    ]

    target_email = buyer_email or _val(dossier, "leadership", "economic_buyer_email")
    if target_email and challenger_body:
        mailto_url = build_mailto_url(target_email, challenger_subject, challenger_body)
        action_launchpad_rich_text.extend([
            {"type": "text", "text": {"content": "• Touch 1 Dispatch: "}},
            {
                "type": "text",
                "text": {
                    "content": "🚀 [Launch Pre-Filled Draft in Apple Mail]",
                    "link": {"url": mailto_url}
                },
                "annotations": {"bold": True, "underline": True, "color": "blue"}
            },
            {"type": "text", "text": {"content": f" (Recipient: {target_email})\n"}}
        ])
    else:
        action_launchpad_rich_text.extend([
            {"type": "text", "text": {"content": f"• Touch 1 Dispatch: Review Draft Below (Buyer: {buyer_name})\n"}}
        ])

    action_launchpad_rich_text.append({"type": "text", "text": {"content": "• Surround Sound: "}})
    target_linkedin = buyer_linkedin_url or _val(dossier, "leadership", "economic_buyer_linkedin")
    if target_linkedin:
        action_launchpad_rich_text.append({
            "type": "text",
            "text": {
                "content": f"👤 [View {buyer_name} on LinkedIn]",
                "link": {"url": target_linkedin}
            },
            "annotations": {"bold": True, "underline": True}
        })
    else:
        action_launchpad_rich_text.append({"type": "text", "text": {"content": f"👤 Buyer: {buyer_name}"}})

    if job_url:
        action_launchpad_rich_text.extend([
            {"type": "text", "text": {"content": "  |  "}},
            {
                "type": "text",
                "text": {
                    "content": "🌐 [Open Live Job Posting]",
                    "link": {"url": job_url}
                },
                "annotations": {"bold": True, "underline": True}
            }
        ])

    spar_encoded_co = urllib.parse.quote(co_name)
    action_launchpad_rich_text.extend([
        {"type": "text", "text": {"content": "  |  "}},
        {
            "type": "text",
            "text": {
                "content": "🥊 [Launch Live Voice Sparring Room]",
                "link": {"url": f"http://localhost:8000/spar?company={spar_encoded_co}"}
            },
            "annotations": {"bold": True, "underline": True, "color": "orange"}
        }
    ])

    draft_status_note = (
        "\n\nDraft Status: [SYNCED TO APPLE MAIL DRAFTS]\n"
        "Workflow: Open Mail.app -> Drafts -> Review & Hit Send (Cmd+Shift+D).\n"
        "Auto-Advance: Native listener detects sent email and automatically flips this card to '3. Outbound Sent'."
        if imap_synced else
        "\n\nDraft Status: [CLICK LAUNCH LINK ABOVE]\n"
        "Workflow: Click 'Launch Pre-Filled Draft' -> Apple Mail opens with copy -> Hit Send.\n"
        "Auto-Advance: Native listener detects sent email and automatically flips this card to '3. Outbound Sent'."
    )
    action_launchpad_rich_text.append({"type": "text", "text": {"content": draft_status_note}})

    launchpad_callout = {
        "object": "block",
        "type": "callout",
        "callout": {
            "icon": icon_obj,
            "color": "blue_background",
            "rich_text": action_launchpad_rich_text
        }
    }

    blocks = [
        launchpad_callout,
        {"object": "block", "type": "divider", "divider": {}},
        # 1. SIDE-BY-SIDE EXECUTIVE DASHBOARD
        {
            "object": "block",
            "type": "column_list",
            "column_list": {
                "children": [
                    {
                        "object": "block",
                        "type": "column",
                        "column": {
                            "children": [
                                {
                                    "object": "block",
                                    "type": "callout",
                                    "callout": {
                                        "icon": icon_obj,
                                        "color": "blue_background",
                                        "rich_text": [
                                            {"type": "text", "text": {"content": "Company Signal Radar\n"}, "annotations": {"bold": True}},
                                            {"type": "text", "text": {"content": f"• Stage: {stage_str} ({funding_str})\n"}},
                                            {"type": "text", "text": {"content": f"• Investors: {investors}\n"}},
                                            {"type": "text", "text": {"content": f"• Buyer: {buyer_name} ({buyer_title})\n"}},
                                            {"type": "text", "text": {"content": f"• Signal: {headline[:250]}"}}
                                        ]
                                    }
                                }
                            ]
                        }
                    },
                    {
                        "object": "block",
                        "type": "column",
                        "column": {
                            "children": [
                                {
                                    "object": "block",
                                    "type": "callout",
                                    "callout": {
                                        "icon": icon_obj,
                                        "color": "red_background",
                                        "rich_text": [
                                            {"type": "text", "text": {"content": "Cost of Inaction (COI)\n"}, "annotations": {"bold": True}},
                                            {"type": "text", "text": {"content": f"• Annual Tax: {annual_tax}/yr\n"}},
                                            {"type": "text", "text": {"content": f"• Monthly Drag: {monthly_drag}/mo\n"}},
                                            {"type": "text", "text": {"content": f"• Impact: ~{estimated_reps} reps × {hours_wasted}h/wk\n"}},
                                            {"type": "text", "text": {"content": f"• 90-Day Delay: {quarterly_risk}"}}
                                        ]
                                    }
                                }
                            ]
                        }
                    }
                ]
            }
        },
        {"object": "block", "type": "divider", "divider": {}},

        # 2. ACCORDION 1: Role Mandate & Experience Transfer
        {
            "object": "block",
            "type": "heading_2",
            "heading_2": {
                "is_toggleable": True,
                "rich_text": [{"type": "text", "text": {"content": "Role Mandate & Candidate Fit Alignment"}}],
                "children": [
                    {
                        "object": "block",
                        "type": "callout",
                        "callout": {
                            "icon": icon_obj,
                            "color": "gray_background",
                            "rich_text": [
                                {"type": "text", "text": {"content": "Core Mandate:\n"}, "annotations": {"bold": True}},
                                {"type": "text", "text": {"content": role_mandate[:1900]}}
                            ]
                        }
                    },
                    {
                        "object": "block",
                        "type": "heading_3",
                        "heading_3": {
                            "rich_text": [{"type": "text", "text": {"content": "How Your Background Transfers"}}]
                        }
                    }
                ] + transfer_children
            }
        },

        # 3. ACCORDION 2: Dual Why-Now Triggers (Top-Down + Bottom-Up)
        {
            "object": "block",
            "type": "heading_2",
            "heading_2": {
                "is_toggleable": True,
                "rich_text": [{"type": "text", "text": {"content": "Executive & Operational Why-Now Triggers"}}],
                "children": [
                    {
                        "object": "block",
                        "type": "callout",
                        "callout": {
                            "icon": icon_obj,
                            "color": "blue_background",
                            "rich_text": [
                                {"type": "text", "text": {"content": "Top-Down Strategic Shift:\n"}, "annotations": {"bold": True}},
                                {"type": "text", "text": {"content": why_now_top_down[:1900]}}
                            ]
                        }
                    },
                    {
                        "object": "block",
                        "type": "callout",
                        "callout": {
                            "icon": icon_obj,
                            "color": "orange_background",
                            "rich_text": [
                                {"type": "text", "text": {"content": "Bottom-Up Operational Friction / Scale Landmines:\n"}, "annotations": {"bold": True}},
                                {"type": "text", "text": {"content": why_now_bottom_up[:1900]}}
                            ]
                        }
                    }
                ]
            }
        },
    ]

    # Optional Accordion: Day-1 Proof-of-Work (Mini Work Trial Outbound)
    if work_trial_email:
        co_disp = company_name or "Target"
        target_disp = work_trial_target_company or "ICP Enterprise"
        wt_children = [
            {
                "object": "block",
                "type": "callout",
                "callout": {
                    "icon": icon_obj,
                    "color": "green_background",
                    "rich_text": [
                        {"type": "text", "text": {"content": "Day-1 Challenger Outbound Execution:\n"}, "annotations": {"bold": True}},
                        {"type": "text", "text": {"content": f"Below is a live Day-1 Challenger cold email drafted on behalf of {co_disp} targeting {target_disp}, ready to deploy via Clay + automated sequencing."[:1900]}}
                    ]
                }
            },
            {
                "object": "block",
                "type": "code",
                "code": {
                    "language": "plain text",
                    "rich_text": [{"type": "text", "text": {"content": work_trial_email[:1900]}}]
                }
            }
        ]
        blocks.append(
            {
                "object": "block",
                "type": "heading_2",
                "heading_2": {
                    "is_toggleable": True,
                    "rich_text": [{"type": "text", "text": {"content": f"Day-1 Work Trial: Sample Outbound ({co_disp} -> {target_disp})"[:100]}}],
                    "children": wt_children
                }
            }
        )

    # 4. ACCORDION 3: Challenger Cold Email Drafts (Dual-Track)
    if email_bundle is None:
        email_bundle = DualChallengerEmailBundle(
            subject=challenger_subject,
            option_a_permission_first=challenger_body,
            option_b_asset_linked=challenger_option_b or challenger_body,
            blueprint_public_url=blueprint_url or "",
        )
    blocks.append(
        build_dual_outbound_accordion(
            email_bundle=email_bundle,
            company_name=company_name or "Target",
            blueprint_url=blueprint_url or getattr(email_bundle, "blueprint_public_url", ""),
        )
    )

    # 4b. ACCORDION 3b: Multi-Channel Surround Sound (LinkedIn Connection Note, InMail & 30s Voice Note)
    first_buyer_name = (buyer_name.split()[0] if buyer_name else "there").capitalize()
    voice_note_script = (
        f"Hey {first_buyer_name} — saw your team expanding {co_name}'s GTM systems. "
        f"Rather than a standard pitch, I mapped out a Tier-0 pre-filter and idempotent CRM sync "
        f"tackling the manual research drag across your pipeline. "
        f"Dropped a quick linkless breakdown to your inbox earlier—would love to send the live Notion architecture over if you're open. Jae."
    )
    linkedin_children = [
        {
            "object": "block",
            "type": "callout",
            "callout": {
                "icon": icon_obj,
                "color": "blue_background",
                "rich_text": [
                    {"type": "text", "text": {"content": "300-Char Connection Note (Send with invite):\n"}, "annotations": {"bold": True}},
                    {"type": "text", "text": {"content": (conn_note or f"Hi {first_buyer_name}, saw your expansion at {co_name}—built an automated pipeline architecture for your team.")[:295]}}
                ]
            }
        },
        {
            "object": "block",
            "type": "callout",
            "callout": {
                "icon": icon_obj,
                "color": "purple_background",
                "rich_text": [
                    {"type": "text", "text": {"content": "InMail Message (Direct outreach):\n"}, "annotations": {"bold": True}},
                    {"type": "text", "text": {"content": (inmail or f"Hi {first_buyer_name}, noticed {co_name}'s recent growth. Built a Day-1 Deal Room eliminating manual rep research. Worth a 5-min look?")[:595]}}
                ]
            }
        },
        {
            "object": "block",
            "type": "callout",
            "callout": {
                "icon": icon_obj,
                "color": "orange_background",
                "rich_text": [
                    {"type": "text", "text": {"content": "30-Second LinkedIn Voice Note Script (Mobile Audio):\n"}, "annotations": {"bold": True}},
                    {"type": "text", "text": {"content": voice_note_script}}
                ]
            }
        }
    ]
    blocks.append({
        "object": "block",
        "type": "heading_2",
        "heading_2": {
            "is_toggleable": True,
            "rich_text": [{"type": "text", "text": {"content": "Multi-Channel Outbound (LinkedIn Connection, InMail & Voice Note)"}}],
            "children": linkedin_children
        }
    })

    # 5. ACCORDION 4: Full Tailored ATS Resume (Native Notion Blocks)
    blocks.extend(
        render_full_resume_as_notion_blocks(
            resume_markdown=clean_resume,
            pdf_filename=pdf_filename,
        )
    )

    # Audit & Deduplication Filter:
    # 1. Ensure the only place Company Signal Radar and COI appear is in the top 2-column column_list block.
    # 2. Strip any legacy heading_2 accordions named 'Company Intel & Capital Signals' or 'Cost of Inaction Breakdown'.
    disallowed_h2 = {
        "company intel & capital signals",
        "company intel",
        "capital signals",
        "cost of inaction breakdown",
    }
    filtered_blocks = []
    for blk in blocks:
        if blk.get("type") == "heading_2":
            rich_objs = blk.get("heading_2", {}).get("rich_text", [])
            h2_text = "".join([t.get("text", {}).get("content", "") or t.get("plain_text", "") for t in rich_objs]).strip().lower()
            if any(dh in h2_text for dh in disallowed_h2):
                continue
        filtered_blocks.append(blk)

    return filtered_blocks


def build_clean_notion_deal_room_blocks(*args, **kwargs) -> list:
    """Backward-compatible adapter that routes calls into build_full_depth_deal_room_blocks."""
    if "why_now_top_down" in kwargs or len(args) >= 6:
        return build_full_depth_deal_room_blocks(*args, **kwargs)

    dossier = kwargs.get("dossier") if "dossier" in kwargs else (args[0] if len(args) > 0 else None)
    coi = kwargs.get("coi") if "coi" in kwargs else (args[1] if len(args) > 1 else {})
    why_now_narrative = kwargs.get("why_now_narrative") if "why_now_narrative" in kwargs else (args[2] if len(args) > 2 else "")
    challenger_subject = kwargs.get("challenger_subject") if "challenger_subject" in kwargs else (args[3] if len(args) > 3 else "")
    challenger_body = kwargs.get("challenger_body") if "challenger_body" in kwargs else (args[4] if len(args) > 4 else "")
    linkedin_bundle = kwargs.get("linkedin_bundle") if "linkedin_bundle" in kwargs else (args[5] if len(args) > 5 else None)
    resume_markdown = kwargs.get("resume_markdown") if "resume_markdown" in kwargs else (args[6] if len(args) > 6 else "")
    pdf_filename = kwargs.get("pdf_filename") if "pdf_filename" in kwargs else (args[7] if len(args) > 7 else "")

    role_mandate = kwargs.get("role_mandate") or "Drive automated outbound pipeline generation and technical revenue workflow orchestration across the enterprise account territory."

    experience_transfer = kwargs.get("experience_transfer")
    if not experience_transfer:
        com_bullets = kwargs.get("com_bullets")
        if com_bullets:
            experience_transfer = [(b.split(":")[0], b.split(":")[1].strip()) if ":" in b else ("Methodology", b) for b in com_bullets]
        else:
            experience_transfer = [
                ("GTM Waterfall Architecture (Clay & Webhooks)", "Engineered programmatic signal detection, multi-vendor waterfall cascades, and CRM deduplication reducing rep research time by 80%."),
                ("Enterprise Challenger Outbound (Workday Federal)", "Executed high-acuity outbound prospecting targeting tier-1 accounts using Command of the Message and MEDDICC qualification."),
                ("LLM Reasoning & Deal Desk Synthesis (Gemini API)", "Deployed autonomous Python daemons orchestrating structured AI synthesis into living Notion Deal Rooms.")
            ]

    why_now_top_down = kwargs.get("why_now_top_down")
    why_now_bottom_up = kwargs.get("why_now_bottom_up")
    if not why_now_top_down or not why_now_bottom_up:
        parts = why_now_narrative.split("• Bottom-Up Operational Trigger") if "• Bottom-Up Operational Trigger" in why_now_narrative else [why_now_narrative, ""]
        why_now_top_down = why_now_top_down or parts[0].replace("• Top-Down Strategic Trigger (Macro 'Why Now'):", "").strip() or why_now_narrative
        why_now_bottom_up = why_now_bottom_up or (parts[1].strip() if len(parts) > 1 else "Manual rep research and fragmented CRM handoffs create operational drag and quota tax.")

    return build_full_depth_deal_room_blocks(
        dossier=dossier,
        coi=coi,
        role_mandate=role_mandate,
        experience_transfer=experience_transfer,
        why_now_top_down=why_now_top_down,
        why_now_bottom_up=why_now_bottom_up,
        challenger_subject=challenger_subject,
        challenger_body=challenger_body,
        linkedin_bundle=linkedin_bundle,
        resume_markdown=resume_markdown,
        pdf_filename=pdf_filename,
        work_trial_target_company=kwargs.get("work_trial_target_company"),
        work_trial_email=kwargs.get("work_trial_email"),
        blitz_deal_desk_url=kwargs.get("blitz_deal_desk_url"),
        company_name=kwargs.get("company_name"),
        challenger_option_b=kwargs.get("challenger_option_b"),
        email_bundle=kwargs.get("email_bundle"),
        blueprint_url=kwargs.get("blueprint_url", ""),
    )


def build_day1_execution_brief_blocks(
    job: ClayJobInput,
    synthesis: JobSynthesisResult,
    abs_resume_path: str,
    resume_markdown: str,
    blitz_deal_desk_url: Optional[str] = None,
    notion_file_upload_id: Optional[str] = None,
    coi_metrics: Optional[Dict[str, Any]] = None,
    dossier: Optional[Any] = None,
) -> List[Dict[str, Any]]:
    """Constructs the full-depth, strictly toggle-nested Notion block hierarchy for the Deal Room page body."""
    buyer_display = job.buyer_name_title or "Hiring Manager / Economic Buyer"

    # Extract Option A & Option B
    email_bundle: Optional[DualChallengerEmailBundle] = None
    blueprint_url = ""
    if isinstance(synthesis.challenger_email, DualChallengerEmailBundle):
        email_bundle = synthesis.challenger_email
        email_subj = synthesis.challenger_email.subject
        email_body_a = sanitize_email_text(synthesis.challenger_email.option_a_permission_first, "")
        email_body_b = sanitize_email_text(synthesis.challenger_email.option_b_asset_linked, synthesis.challenger_email.blueprint_public_url)
        blueprint_url = synthesis.challenger_email.blueprint_public_url
    elif isinstance(synthesis.challenger_email, dict):
        email_subj = synthesis.challenger_email.get("subject", f"{job.company_name} pipeline architecture — Jae Carter")
        email_body_a = sanitize_email_text(synthesis.challenger_email.get("option_a_permission_first", synthesis.challenger_email.get("body", "")), "")
        email_body_b = sanitize_email_text(synthesis.challenger_email.get("option_b_asset_linked", ""), synthesis.challenger_email.get("blueprint_public_url", ""))
        blueprint_url = synthesis.challenger_email.get("blueprint_public_url", "")
    else:
        email_subj, raw_body = split_subject_and_body(
            str(synthesis.challenger_email),
            default_subject=f"{job.company_name} pipeline architecture — Jae Carter",
        )
        email_body_a = sanitize_email_text(raw_body, "")
        email_body_b = None

    clean_bundle = sanitize_linkedin_bundle(synthesis.linkedin_bundle) if synthesis.linkedin_bundle else None
    pdf_filename = os.path.basename(abs_resume_path)

    role_mandate = f"{synthesis.role_archetype} mandate at {job.company_name}: {synthesis.fit_justification}"
    experience_transfer = [
        (b.split(":")[0], b.split(":")[1].strip()) if ":" in b else ("Methodology", b)
        for b in synthesis.com_summary
    ]

    return build_full_depth_deal_room_blocks(
        dossier=dossier,
        coi=coi_metrics or {},
        role_mandate=role_mandate,
        experience_transfer=experience_transfer,
        why_now_top_down=synthesis.top_down_trigger,
        why_now_bottom_up=synthesis.bottom_up_trigger,
        challenger_subject=email_subj,
        challenger_body=email_body_a,
        challenger_option_b=email_body_b,
        linkedin_bundle=clean_bundle or LinkedInOutboundBundle(
            connection_note=f"Hi {buyer_display.split(',')[0]}, saw your expansion at {job.company_name}—built an automated pipeline architecture for your team.",
            inmail_message=f"Hi {buyer_display.split(',')[0]},\n\nNoticed {job.company_name}'s recent growth. Built a Day-1 Deal Room eliminating manual rep research. Worth a 5-min look?",
        ),
        resume_markdown=resume_markdown,
        pdf_filename=pdf_filename,
        work_trial_target_company=synthesis.sample_icp_target_company,
        work_trial_email=synthesis.sample_prospect_email,
        blitz_deal_desk_url=None,  # Decoupled: Standalone Stage 5 GTME Blueprint generated in output/blitzes/
        company_name=job.company_name,
        email_bundle=email_bundle,
        blueprint_url=blueprint_url,
        buyer_email=job.buyer_email,
        buyer_linkedin_url=job.buyer_linkedin,
        job_url=job.job_url,
        imap_synced=bool(os.environ.get("IMAP_USER") and os.environ.get("IMAP_PASSWORD")),
    )


def build_job_database_properties(
    job: ClayJobInput,
    synthesis: JobSynthesisResult,
    abs_resume_path: str,
    challenger_email_text: Optional[str] = None,
    notion_file_upload_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Maps job payload + synthesis to the properties of 'GTM & AI Revenue Job Pipeline' (including ATS Resume PDF files)."""
    if challenger_email_text is not None:
        email_text = sanitize_cold_email(challenger_email_text)
    elif isinstance(synthesis.challenger_email, DualChallengerEmailBundle):
        email_text = f"Subject: {synthesis.challenger_email.subject}\n\n{sanitize_cold_email(synthesis.challenger_email.option_a_permission_first)}"
    elif isinstance(synthesis.challenger_email, dict):
        subj = synthesis.challenger_email.get("subject", "")
        body = sanitize_cold_email(synthesis.challenger_email.get("option_a_permission_first", synthesis.challenger_email.get("body", "")))
        email_text = f"Subject: {subj}\n\n{body}" if subj else body
    else:
        email_text = sanitize_cold_email(str(synthesis.challenger_email))

    # Pre-Write Executive Persona & Role-Change Verification Gate
    buyer_display = job.buyer_name_title or "Hiring Team"
    if job.buyer_name_title and job.buyer_name_title.strip().lower() not in ("hiring team", "hiring manager", "vp of sales", "revenue leadership"):
        try:
            from scripts.persona_verifier import verify_and_route_persona
            parts = [p.strip() for p in job.buyer_name_title.split(",") if p.strip()]
            name_part = parts[0]
            title_part = parts[1] if len(parts) > 1 else "Economic Buyer"
            routed = verify_and_route_persona(
                person_name=name_part,
                company_name=job.company_name,
                claimed_title=title_part,
                fallback_title=title_part
            )
            buyer_display = routed["card_display_name"]
            if routed.get("leadership_transition_trigger"):
                synthesis.top_down_trigger = f"{routed['leadership_transition_trigger']} | {synthesis.top_down_trigger}"
        except Exception:
            pass

    # Automated Committee Resolution: resolve exact executive contact & verified work email
    resolved_contact = None
    try:
        from scripts.contact_verifier import resolve_hiring_committee_contact
        resolved_contact = resolve_hiring_committee_contact(
            company_name=job.company_name,
            role_archetype=synthesis.role_archetype,
            domain=job.company_url,
            existing_buyer=buyer_display,
        )
        if buyer_display in ("Hiring Team", "Hiring Manager", "VP of Sales", "Revenue Leadership", "Hiring Team"):
            buyer_display = resolved_contact["buyer_display"]
    except Exception as e:
        print(f"[*] Note resolving committee contact: {e}")

    buyer_email = job.buyer_email
    if not buyer_email and resolved_contact:
        buyer_email = resolved_contact["buyer_email"]

    props: Dict[str, Any] = {
        "Company & Role": {
            "title": [_rt(f"{job.company_name} — {job.role_title}")]
        },
        "Role Archetype": {
            "select": {"name": _sanitize_select_option(synthesis.role_archetype)}
        },
        "Stage": {
            "select": {"name": _sanitize_select_option("2. Deal Room Generated")}
        },
        "Fit Score": {
            "number": int(synthesis.fit_score)
        },
        "Company URL": {
            "url": _normalize_url(job.company_url)
        },
        "Job Posting URL": {
            "url": _normalize_url(job.job_url)
        },
        "Funding & Stage": {
            "rich_text": [_rt(job.funding_stage or "Not specified")]
        },
        "Salary Range": {
            "rich_text": [_rt(job.salary_range or synthesis.salary_range or "$165,000 – $210,000 + Equity (Est.)")]
        },
        "Fit Justification": {
            "rich_text": [_rt(synthesis.fit_justification)]
        },
        "Economic Buyer": {
            "rich_text": [_rt(buyer_display)]
        },
        "Buyer Email": {
            "email": _normalize_email(buyer_email)
        },
        "Buyer LinkedIn": {
            "url": _normalize_url(job.buyer_linkedin) or (resolved_contact["buyer_linkedin"] if resolved_contact else generate_hiring_committee_search_url(job.company_name, synthesis.role_archetype))
        },
        "Top-Down Trigger": {
            "rich_text": [_rt(synthesis.top_down_trigger)]
        },
        "Bottom-Up Trigger": {
            "rich_text": [_rt(synthesis.bottom_up_trigger)]
        },
        "Challenger Cold Email": {
            "rich_text": [_rt(email_text)]
        },
        "Local Resume Path": {
            "rich_text": [_rt(abs_resume_path)]
        },
    }
    if notion_file_upload_id:
        props["ATS Resume PDF"] = {
            "files": [
                {
                    "type": "file_upload",
                    "file_upload": {"id": notion_file_upload_id},
                    "name": os.path.basename(abs_resume_path),
                }
            ]
        }
    return props


def write_job_deal_room_to_notion(
    job: ClayJobInput,
    synthesis: JobSynthesisResult,
    abs_resume_path: str,
    resume_markdown: str,
    blitz_deal_desk_url: Optional[str] = None,
    existing_page_id: Optional[str] = None,
    dry_run: bool = False,
    coi_metrics: Optional[Dict[str, Any]] = None,
    dossier: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    Creates or updates in-place the Deal Room row & page body in NOTION_JOB_DB_ID,
    sanitizes all cold email copy (guaranteeing zero internal Notion links leak),
    stages MIME .eml drafts for both Option A and Option B, and appends a copy-paste callout block.
    """
    load_dotenv(ENV_PATH)
    token = (
        os.environ.get("NOTION_TOKEN")
        or os.environ.get("NOTION_API_KEY")
        or ""
    ).strip().strip('"').strip("'")
    job_db_id = (os.environ.get("NOTION_JOB_DB_ID") or "").strip().strip('"').strip("'")

    notion_file_upload_id: Optional[str] = None
    if not dry_run and token:
        notion_file_upload_id = upload_pdf_to_notion(abs_resume_path, token)

    resume_markdown = sanitize_resume_markdown(resume_markdown)
    clean_co = re.sub(r"[^\w\-]", "_", job.company_name).lower().strip("_")

    import urllib.parse
    default_portfolio_link = f"https://jae-carter.vercel.app/?company={urllib.parse.quote(job.company_name)}"
    raw_bp_url = getattr(synthesis.challenger_email, "blueprint_public_url", "") or ""
    if not raw_bp_url or "github.com/jaecarter" in raw_bp_url:
        blueprint_url = default_portfolio_link
    else:
        blueprint_url = raw_bp_url

    # Resolve sanitized Option A and Option B email copy
    if isinstance(synthesis.challenger_email, DualChallengerEmailBundle):
        email_subj = synthesis.challenger_email.subject
        opt_a_body = sanitize_email_text(synthesis.challenger_email.option_a_permission_first, blueprint_url)
        opt_b_body = sanitize_email_text(synthesis.challenger_email.option_b_asset_linked, blueprint_url)
    elif isinstance(synthesis.challenger_email, dict):
        email_subj = synthesis.challenger_email.get("subject", f"{job.company_name} pipeline drag")
        opt_a_body = sanitize_email_text(synthesis.challenger_email.get("option_a_permission_first", synthesis.challenger_email.get("body", "")), blueprint_url)
        opt_b_body = sanitize_email_text(synthesis.challenger_email.get("option_b_asset_linked", ""), blueprint_url)
    else:
        email_subj, raw_body = split_subject_and_body(
            str(synthesis.challenger_email),
            default_subject=f"{job.company_name} pipeline drag",
        )
        opt_a_body = sanitize_email_text(raw_body, blueprint_url)
        opt_b_body = ""

    clean_email_a = f"Subject: {email_subj}\n\n{opt_a_body}"
    clean_email_b = f"Subject: {email_subj}\n\n{opt_b_body}" if opt_b_body else ""

    properties = build_job_database_properties(
        job,
        synthesis,
        abs_resume_path=abs_resume_path,
        challenger_email_text=clean_email_a,
        notion_file_upload_id=notion_file_upload_id,
    )
    children = build_day1_execution_brief_blocks(
        job,
        synthesis,
        abs_resume_path=abs_resume_path,
        resume_markdown=resume_markdown,
        blitz_deal_desk_url=None,  # Decoupled from Stage 2 Notion blocks
        notion_file_upload_id=notion_file_upload_id,
        coi_metrics=coi_metrics,
        dossier=dossier,
    )

    if dry_run:
        draft_info_a = stage_outbound_email_draft(
            company_name=job.company_name,
            role_title=job.role_title,
            challenger_email=clean_email_a,
            pdf_path=abs_resume_path,
            buyer_email=job.buyer_email,
            buyer_name_title=job.buyer_name_title,
            output_filename=f"{clean_co}_outbound_draft_option_a.eml",
        )
        stage_outbound_email_draft(
            company_name=job.company_name,
            role_title=job.role_title,
            challenger_email=clean_email_a,
            pdf_path=abs_resume_path,
            buyer_email=job.buyer_email,
            buyer_name_title=job.buyer_name_title,
            output_filename=f"{clean_co}_outbound_draft.eml",
        )
        draft_info_b = None
        if clean_email_b:
            draft_info_b = stage_outbound_email_draft(
                company_name=job.company_name,
                role_title=job.role_title,
                challenger_email=clean_email_b,
                pdf_path=abs_resume_path,
                buyer_email=job.buyer_email,
                buyer_name_title=job.buyer_name_title,
                output_filename=f"{clean_co}_outbound_draft_option_b.eml",
            )
        return {
            "status": "dry_run",
            "database_id": job_db_id,
            "role_archetype": synthesis.role_archetype,
            "fit_score": synthesis.fit_score,
            "local_resume_path": abs_resume_path,
            "eml_draft_path": draft_info_a["eml_path"],
            "eml_draft_option_b_path": draft_info_b["eml_path"] if draft_info_b else None,
            "blitz_deal_desk_url": blitz_deal_desk_url,
        }

    if not token:
        raise RuntimeError("Missing NOTION_TOKEN / NOTION_API_KEY in .env")
    if not job_db_id:
        raise RuntimeError("Missing NOTION_JOB_DB_ID in .env. Run scripts/init_job_db.py first.")

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Notion-Version": NOTION_VERSION,
    }

    logo_icon = _company_logo_icon(job.company_name)

    if existing_page_id:
        # Update existing triage row in-place and attach brand favicon icon
        patch_resp = requests.patch(
            f"https://api.notion.com/v1/pages/{existing_page_id}",
            headers=headers,
            json={"properties": properties, "icon": logo_icon},
            timeout=25,
        )
        if patch_resp.status_code != 200:
            raise RuntimeError(f"Notion in-place update failed ({patch_resp.status_code}): {patch_resp.text}")
        data = patch_resp.json()
        page_id = existing_page_id
        page_url = data.get("url", "")

        # Block-level idempotency: wipe legacy Deal Room blocks to prevent duplicate Signal Radar & COI
        try:
            existing_blocks_resp = requests.get(
                f"https://api.notion.com/v1/blocks/{page_id}/children?page_size=100",
                headers=headers,
                timeout=15,
            )
            if existing_blocks_resp.status_code == 200:
                existing_blocks = existing_blocks_resp.json().get("results", [])
                has_existing_deal_room = False
                for b in existing_blocks:
                    b_type = b.get("type", "")
                    text_objs = b.get(b_type, {}).get("rich_text", [])
                    t_str = "".join([t.get("plain_text", "") for t in text_objs])
                    if (
                        "Company Signal Radar" in t_str
                        or "Cost of Inaction (COI)" in t_str
                        or "Role Mandate" in t_str
                        or "Company Intel & Capital Signals" in t_str
                        or "Cost of Inaction Breakdown" in t_str
                        or "TRIAGE_GATE" in t_str
                        or "Morning Scout Review" in t_str
                        or "How Your Experience Transfers" in t_str
                    ):
                        has_existing_deal_room = True
                        break

                if has_existing_deal_room:
                    for b in existing_blocks:
                        b_type = b.get("type", "")
                        text_objs = b.get(b_type, {}).get("rich_text", [])
                        t_str = "".join([t.get("plain_text", "") for t in text_objs])
                        # Preserve Battlecards, follow-up bumps, and Stage 5 work trials if already present
                        if "Executive Interview Battlecard" in t_str or "4-Day Follow-Up" in t_str or "Stage 5 Work Trial" in t_str:
                            continue
                        try:
                            requests.delete(f"https://api.notion.com/v1/blocks/{b['id']}", headers=headers, timeout=10)
                        except Exception:
                            pass
        except Exception as idemp_err:
            print(f"[!] Notice checking block idempotency on {page_id}: {idemp_err}")

        append_resp = requests.patch(
            f"https://api.notion.com/v1/blocks/{page_id}/children",
            headers=headers,
            json={"children": children},
            timeout=25,
        )
        if append_resp.status_code != 200:
            raise RuntimeError(f"Notion block append failed ({append_resp.status_code}): {append_resp.text}")
    else:
        payload = {
            "parent": {"database_id": job_db_id},
            "icon": logo_icon,
            "properties": properties,
            "children": children,
        }
        response = requests.post(
            "https://api.notion.com/v1/pages",
            headers=headers,
            json=payload,
            timeout=25,
        )
        if response.status_code != 200:
            raise RuntimeError(f"Notion API error ({response.status_code}): {response.text}")
        data = response.json()
        page_id = data.get("id", "")
        page_url = data.get("url", "")

    # Stage MIME .eml drafts for both Option A and Option B (with PDF attached)
    draft_info_a = stage_outbound_email_draft(
        company_name=job.company_name,
        role_title=job.role_title,
        challenger_email=clean_email_a,
        pdf_path=abs_resume_path,
        buyer_email=job.buyer_email,
        buyer_name_title=job.buyer_name_title,
        output_filename=f"{clean_co}_outbound_draft_option_a.eml",
    )
    stage_outbound_email_draft(
        company_name=job.company_name,
        role_title=job.role_title,
        challenger_email=clean_email_a,
        pdf_path=abs_resume_path,
        buyer_email=job.buyer_email,
        buyer_name_title=job.buyer_name_title,
        output_filename=f"{clean_co}_outbound_draft.eml",
    )
    draft_info_b = None
    if clean_email_b:
        draft_info_b = stage_outbound_email_draft(
            company_name=job.company_name,
            role_title=job.role_title,
            challenger_email=clean_email_b,
            pdf_path=abs_resume_path,
            buyer_email=job.buyer_email,
            buyer_name_title=job.buyer_name_title,
            output_filename=f"{clean_co}_outbound_draft_option_b.eml",
        )

    # Push Touch 1 draft directly to IMAP Drafts (Apple Mail / Gmail)
    target_to = draft_info_a.get("to") or job.buyer_email
    if target_to and opt_a_body:
        try:
            msg_id = push_draft_to_imap(
                to_addr=target_to,
                subject=email_subj,
                body=opt_a_body,
            )
            if msg_id:
                print(f"[+] Synced Touch 1 draft directly to IMAP Drafts ({msg_id})")
        except Exception as imap_err:
            print(f"[!] IMAP Drafts sync note: {imap_err}")

    # Patch Challenger Cold Email property and append clean Copy-Paste Outbound Draft Callout block
    if page_id:
        try:
            requests.patch(
                f"https://api.notion.com/v1/pages/{page_id}",
                headers=headers,
                json={
                    "properties": {
                        "Challenger Cold Email": {
                            "rich_text": [_rt(clean_email_a)]
                        }
                    }
                },
                timeout=15,
            )
            callout_text = (
                f"To: {draft_info_a['to']}\n"
                f"Subject: {draft_info_a['subject']}\n"
                f"Draft (Option A - Permission-First): {draft_info_a['eml_path']}\n"
            )
            if draft_info_b:
                callout_text += f"Draft (Option B - Asset-Linked): {draft_info_b['eml_path']}\n"
            callout_text += f"\n--- OPTION A (PERMISSION-FIRST) ---\n{draft_info_a['body']}"
            if draft_info_b:
                callout_text += f"\n\n--- OPTION B (ASSET-LINKED) ---\n{draft_info_b['body']}"

            copy_paste_callout = _callout(
                "Ready-to-Send Outbound Drafts (Copy-Paste for Notion Mail / Mobile)",
                callout_text,
                color="purple_background",
                icon=_company_logo_icon(job.company_name),
            )
            requests.patch(
                f"https://api.notion.com/v1/blocks/{page_id}/children",
                headers=headers,
                json={"children": [copy_paste_callout]},
                timeout=15,
            )
        except Exception as exc:
            print(f"[!] Warning: Failed to append outbound draft callout: {exc}")

        # Stage Multi-Threaded Committee Launchpad (CRO, Head of RevOps, Senior AE)
        try:
            stage_committee_outreach(job.company_name, page_id=page_id)
        except Exception as exc:
            print(f"[!] Notice staging multi-threaded committee outreach: {exc}")

    action_label = "updated in-place" if existing_page_id else "created"
    print(f"[+] Notion Job Deal Room {action_label}: {page_url}")
    return {
        "status": action_label,
        "page_id": page_id,
        "url": page_url,
        "role_archetype": synthesis.role_archetype,
        "fit_score": synthesis.fit_score,
        "local_resume_path": abs_resume_path,
        "eml_draft_path": draft_info_a["eml_path"],
        "eml_draft_option_b_path": draft_info_b["eml_path"] if draft_info_b else None,
        "blitz_deal_desk_url": blitz_deal_desk_url,
        "challenger_email": clean_email_a,
    }


def process_job_ingest(
    raw_payload: Dict[str, Any],
    existing_page_id: Optional[str] = None,
    dry_run: bool = False,
    force_offline: bool = False,
) -> Dict[str, Any]:
    """
    End-to-end Mode B pipeline:
      1. Validate Clay / Poller job payload
      2. Run Gemini Structured Synthesis (fit_score, jd_hard_skill_keywords, sample_icp_target_company, XYZ bullets)
      3. Enforce ATS Keyword Guardrail (Upgrade #4) & compile Markdown + WeasyPrint PDF resume
      4. If fit_score >= 90, auto-trigger Mode A Deal Desk Blitz (Upgrade #3) targeting sample_icp_target_company
      5. Write or update in-place the Notion Deal Room & stage MIME .eml draft with PDF attached (Upgrade #2)
    """
    job = ClayJobInput.model_validate(raw_payload)

    # Harvest or load CompanyDossier and calculate deterministic COI metrics
    dossier = None
    try:
        dossier = harvest_company_dossier(
            company_name=job.company_name,
            company_domain=job.company_url,
        )
    except Exception as exc:
        print(f"[!] Notice: Company dossier harvest notice for {job.company_name}: {exc}")

    coi_metrics = calculate_coi_metrics(dossier)
    print(
        f"[*] [COI Engine] {job.company_name} ({coi_metrics['headcount_bracket']}) -> "
        f"Annual SDR Tax: {coi_metrics['annual_sdr_tax_str']} "
        f"({coi_metrics['monthly_drag_str']}/mo across {coi_metrics['estimated_reps']} reps)",
        flush=True,
    )

    synthesis = synthesize_job_opportunity(
        job,
        force_offline=force_offline,
        dossier=dossier,
        coi_metrics=coi_metrics,
    )
    master_profile = load_master_profile()

    # Ensure sanitize_resume_markdown() runs on the LLM output before generating the PDF
    if synthesis.tailored_summary:
        synthesis.tailored_summary = sanitize_resume_markdown(synthesis.tailored_summary)
    for exp_item in synthesis.tailored_experience:
        exp_item.xyz_bullets = [sanitize_resume_markdown(b) for b in exp_item.xyz_bullets]

    # Upgrade #4: Compile tailored Markdown + ATS PDF with keyword guardrail
    abs_resume_path, resume_markdown = compile_tailored_resume(
        company_name=job.company_name,
        role_title=job.role_title,
        role_archetype=synthesis.role_archetype,
        tailored_summary=synthesis.tailored_summary,
        tailored_experience=[item.model_dump() for item in synthesis.tailored_experience],
        jd_hard_skill_keywords=synthesis.jd_hard_skill_keywords,
        master_profile=master_profile,
    )
    resume_markdown = sanitize_resume_markdown(resume_markdown)

    # Save output/resumes/{company}_resume.md
    clean_co = clean_slug(job.company_name)
    company_resume_md = os.path.abspath(f"output/resumes/{clean_co}_resume.md")
    with open(company_resume_md, "w", encoding="utf-8") as f:
        f.write(resume_markdown)
    print(f"[+] Saved company resume markdown -> {company_resume_md}")



    # Upgrade #3: Decoupled Stage 5 Deliverable — Standalone 3-Account GTME Blueprint when fit_score >= 90
    blitz_deliverable_path: Optional[str] = None
    if synthesis.fit_score >= 90:
        try:
            print(f"[*] Fit score {synthesis.fit_score} >= 90: Generating standalone Stage 5 GTME Blueprint...")
            from scripts.territory_blitz_engine import (
                generate_gtme_blueprint,
                save_gtme_deliverable_markdown,
                save_gtme_deliverable_json,
            )
            dossier_dict = dossier.model_dump() if (dossier and hasattr(dossier, "model_dump")) else (dossier if isinstance(dossier, dict) else {})
            blueprint = generate_gtme_blueprint(
                hiring_company=job.company_name,
                role_title=job.role_title,
                dossier_dict=dossier_dict,
                force_offline=force_offline,
            )
            md_path = save_gtme_deliverable_markdown(blueprint)
            save_gtme_deliverable_json(blueprint)
            blitz_deliverable_path = str(md_path)
            print(f"[+] Standalone Stage 5 GTME Deliverable ready at: {blitz_deliverable_path}")
        except Exception as exc:
            print(f"[!] Notice: Standalone GTME blueprint generation notice: {exc}")

    # Write/update Notion Deal Room & stage dual .eml drafts (Upgrade #2)
    notion_res = write_job_deal_room_to_notion(
        job,
        synthesis,
        abs_resume_path=abs_resume_path,
        resume_markdown=resume_markdown,
        blitz_deal_desk_url=None,  # Decoupled from Stage 2 early-stage deal rooms
        existing_page_id=existing_page_id,
        dry_run=dry_run,
        coi_metrics=coi_metrics,
        dossier=dossier,
    )

    return {
        "status": notion_res["status"],
        "company_name": job.company_name,
        "role_title": job.role_title,
        "role_archetype": synthesis.role_archetype,
        "fit_score": synthesis.fit_score,
        "local_resume_path": abs_resume_path,
        "eml_draft_path": notion_res.get("eml_draft_path"),
        "eml_draft_option_b_path": notion_res.get("eml_draft_option_b_path"),
        "blitz_deliverable_path": blitz_deliverable_path,
        "notion_page_id": notion_res.get("page_id"),
        "notion_url": notion_res.get("url"),
        "synthesis": synthesis.model_dump(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the Autonomous Job Search & Deal Room Engine on a staged Clay job JSON file."
    )
    parser.add_argument("payload_path", type=str, help="Path to staged Clay job JSON payload")
    parser.add_argument("--dry-run", action="store_true", help="Validate and synthesize without pushing to Notion")
    parser.add_argument("--offline", action="store_true", help="Use deterministic fallback instead of Gemini API")
    args = parser.parse_args()

    path = Path(args.payload_path)
    if not path.exists():
        print(f"[!] File not found: {path}", file=sys.stderr)
        sys.exit(1)

    raw_payload = json.loads(path.read_text(encoding="utf-8"))
    result = process_job_ingest(raw_payload, dry_run=args.dry_run, force_offline=args.offline)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
