"""Google GenAI SDK synthesizer with zero-YAML dynamic seller resolution and enterprise-calibrated deal sizing."""

import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union
import yaml
from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()

from schema import (
    ClayInputData,
    CommandOfMessagePillar,
    CompetitiveBattlecard,
    DealDeskSynthesis,
    MEDDICCQualification,
    OutboundSequence,
    RampMilestones,
)


SELLER_PROFILE_PATH = Path("seller_profile.yaml")

# Known built-in enterprise seller reference profiles for offline fallback when YAML is bypassed
BUILTIN_SELLER_KNOWLEDGE: Dict[str, Dict[str, Any]] = {
    "braintrust": {
        "company_name": "Braintrust",
        "category": "Enterprise AI Evaluation, Prompt Regression Testing, Automated Red-Teaming & AI Proxy Infrastructure",
        "core_value_props": [
            "Eliminate the Annual Evaluation Tax with automated CI/CD prompt regression gates, LLM-as-a-judge scoring, and red-teaming",
            "Convert failing production traces into versioned golden datasets in one click across frontier and open-weights models",
            "Accelerate inference and enforce governance with the Braintrust AI Proxy, caching, and self-hosted Hybrid VPC data plane",
        ],
        "primary_wedges": [
            "Braintrust Automated CI/CD Evaluation & Prompt Regression Suite",
            "Braintrust Production Trace-to-Dataset Loop & Automated Red-Teaming",
            "Braintrust Enterprise AI Proxy Caching & Hybrid VPC Governance",
        ],
    },
}


def load_seller_profile(
    profile_path: Path = SELLER_PROFILE_PATH,
    seller_override: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Resolves the seller profile from seller_profile.yaml if defined;
    otherwise returns a dynamic seller descriptor (is_dynamic=True) so Gemini retrieves
    its built-in knowledge of that seller's enterprise software suite automatically.
    """
    data: Dict[str, Any] = {}
    if profile_path.exists():
        try:
            data = yaml.safe_load(profile_path.read_text(encoding="utf-8")) or {}
        except Exception:
            data = {}

    sellers: Dict[str, Any] = data.get("sellers", {})
    raw_choice = (seller_override or os.environ.get("ACTIVE_SELLER") or data.get("active_seller", "vanta")).strip()
    lookup_key = raw_choice.lower()

    # 1. Check if the seller exists in seller_profile.yaml (by key or company_name)
    for k, profile in sellers.items():
        if k.lower() == lookup_key or str(profile.get("company_name", "")).strip().lower() == lookup_key:
            return {"seller_key": k, "is_dynamic": False, **profile}

    # 2. Check built-in knowledge (e.g., Braintrust) and mark as dynamic so Gemini uses its full domain knowledge
    if lookup_key in BUILTIN_SELLER_KNOWLEDGE:
        return {"seller_key": lookup_key, "is_dynamic": True, **BUILTIN_SELLER_KNOWLEDGE[lookup_key]}

    # 3. Zero-YAML Dynamic Seller Resolution: instruct Gemini to retrieve its knowledge of raw_choice
    display_name = raw_choice if raw_choice else "Vanta"
    return {
        "seller_key": lookup_key or "dynamic",
        "company_name": display_name,
        "is_dynamic": True,
        "category": f"Dynamic Enterprise Software Suite ({display_name})",
        "core_value_props": [],
        "primary_wedges": [],
    }


def _format_list_or_str(val: Union[str, List[str], None], default: str = "Not specified") -> str:
    if val is None:
        return default
    if isinstance(val, list):
        return ", ".join(str(x) for x in val) if val else default
    return str(val).strip() or default


def _infer_target_scale(target: ClayInputData) -> Tuple[str, int, int, Tuple[int, int], Tuple[int, int]]:
    """
    Infers the target company's employee & engineering footprint and returns:
    (scale_tier_label, estimated_total_employees, scaled_engineering_footprint, (land_min, land_max), (exp_min, exp_max))
    """
    known_f500_or_strategic = {
        "workday": (18800, 450),
        "salesforce": (72000, 1800),
        "snowflake": (7000, 350),
        "servicenow": (22000, 600),
        "jpmorgan": (300000, 2500),
        "stripe": (8000, 400),
        "datadog": (5200, 260),
        "uber": (30000, 750),
        "airbnb": (6900, 240),
        "intuit": (18000, 500),
        "adobe": (29000, 800),
        "cisco": (84000, 1200),
        "crowdstrike": (8500, 320),
        "palantir": (4000, 280),
        "databricks": (6500, 420),
    }
    known_large_enterprise = {
        "deel": (4000, 95),
        "gusto": (3000, 75),
        "brex": (1200, 45),
        "rippling": (2800, 85),
        "klaviyo": (2000, 60),
        "confluent": (2800, 90),
    }

    name_key = target.company_name.strip().lower()
    explicit_hc = target.headcount or target.employee_count

    if explicit_hc:
        total_hc = explicit_hc
    elif name_key in known_f500_or_strategic:
        total_hc = known_f500_or_strategic[name_key][0]
    elif name_key in known_large_enterprise:
        total_hc = known_large_enterprise[name_key][0]
    else:
        total_hc = max(350, target.ai_engineer_count * 25) if target.ai_engineer_count else 600

    # Ensure engineering footprint scales with overall enterprise size, not just a single squad
    if name_key in known_f500_or_strategic and target.ai_engineer_count <= 15:
        scaled_eng = known_f500_or_strategic[name_key][1]
    elif total_hc >= 10000:
        scaled_eng = max(target.ai_engineer_count, int(total_hc * 0.025))
    elif total_hc >= 2500:
        scaled_eng = max(target.ai_engineer_count, int(total_hc * 0.03))
    else:
        scaled_eng = max(target.ai_engineer_count, max(8, int(total_hc * 0.035)))

    if total_hc >= 10000:
        return (
            "Strategic Enterprise / Fortune 500 (10,000+ employees)",
            total_hc,
            scaled_eng,
            (175000, 250000),
            (350000, 600000),
        )
    if total_hc >= 2500:
        return (
            "Large Enterprise (2,500–10,000 employees)",
            total_hc,
            scaled_eng,
            (90000, 160000),
            (250000, 350000),
        )
    return (
        "Mid-Market (<2,500 employees)",
        total_hc,
        scaled_eng,
        (45000, 80000),
        (120000, 160000),
    )


def build_system_instruction(seller: Dict[str, Any]) -> str:
    """Constructs dynamic system instructions anchored to either YAML seller profile or zero-YAML Gemini knowledge."""
    seller_name = seller.get("company_name", "Vanta")
    is_dynamic = seller.get("is_dynamic", False)
    category = seller.get("category", "Enterprise B2B Software")
    value_props = seller.get("core_value_props", [])
    wedges = seller.get("primary_wedges", [])

    if is_dynamic and not value_props:
        seller_block = f"""Active Seller: {seller_name} (Dynamic Zero-YAML Resolution Mode)
- Instruction: Retrieve your comprehensive built-in knowledge of {seller_name}'s enterprise software suite, product architecture, pricing model, and competitive differentiators (for example, if {seller_name} is Braintrust: AI evaluations, CI/CD prompt regression testing, automated red-teaming, trace-to-dataset loops, and AI proxy caching).
- Automatically derive {seller_name}'s core value pillars and select the highest-conviction enterprise entry wedge (`ai_product_wedge`) for the target prospect."""
    else:
        vp_str = "\n".join(f"  - {vp}" for vp in value_props)
        w_str = "\n".join(f"  - {w}" for w in wedges)
        seller_block = f"""Active Seller Profile ({seller_name} — {category}):
- If {seller_name} has additional enterprise capabilities beyond the YAML summary, combine the YAML profile below with your knowledge of {seller_name}'s full enterprise product suite:
- Core Value Propositions:
{vp_str}
- Primary Product Wedges:
{w_str}"""

    return f"""You are a Senior Enterprise Account Executive and Deal Desk Strategist representing {seller_name}.
You synthesize target account signals into a structured executive Deal Desk intelligence brief tailored specifically to {seller_name}'s commercial playbook.

{seller_block}

Follow these strict Enterprise Deal Desk rules:
1. Enterprise-Calibrated Deal Sizing Logic (Differentiate strictly by Target Company Scale):
   Determine the target company's real-world total employee headcount and engineering scale:
   - Mid-Market (<2,500 employees):
     * `land_deal_size`: $45,000–$80,000
     * `expansion_arr`: $120,000
   - Large Enterprise (2,500–10,000 employees):
     * `land_deal_size`: $90,000–$160,000
     * `expansion_arr`: $250,000+ (e.g., $250,000–$350,000)
   - Strategic Enterprise / Fortune 500 (10,000+ employees, e.g., Workday, Salesforce, Snowflake, ServiceNow, JPMorgan):
     * `land_deal_size`: $175,000–$250,000
     * `expansion_arr`: $350,000–$600,000+
   - Set `estimated_arr` equal to `land_deal_size` (or include the land-to-expand framing in the executive summary).

2. Enterprise-Wide 'Annual Evaluation Tax' (`annual_evaluation_tax`):
   - Scale the 'Annual Evaluation Tax' (cost of inaction / developer productivity tax / manual compliance or operational drag) with the target company's OVERALL engineering/AI footprint across the enterprise—never under-scope a Large or Strategic Enterprise to a single small squad.
   - Format clearly with both annual dollar impact and FTE equivalent (e.g., '$2,880,000/yr (12.0 Senior AI FTEs — Land: $195k | Full Platform ARR: $450k)').

3. Executive Buying Committee (`champion` and `economic_buyer`):
   - Identify the specific full name and official title of the real-world executive/leader at `company_name` based on public corporate records (e.g., 'Karim Atiyeh (Co-Founder & CTO)', 'Carl Eschenbach (CEO)', or 'Sayan Chakraborty (Co-President)').
   - Format the output string strictly as: "Full Name (Official Title)".
   - If a specific individual cannot be verified with high certainty, provide the target title followed by '(Target Persona)'.

4. Signal Mapping:
   - `ai_product_wedge`: Derive {seller_name}'s specific enterprise entry wedge tailored to the target company's products and tech stack.
   - `boss_hiring_signal`: Synthesize active AI/engineering expansion mandates, board priorities, and hiring velocity.
   - `security_ypc`: Classify security, governance, VPC/YPC data residency, and compliance requirements.
   - `outbound_sequence`: 3-touch Challenger email sequence (<125 words each, peer-to-peer tone, zero generic sales jargon, single clear CTA).

5. Executive Job-Search & Work-Trial Playbook Assets:
   - `competitive_battlecard`: Identify {seller_name}'s primary competitor or internal DIY alternative at `company_name`'s scale (`competitor_name`), formulate 2 sharp trap-setting discovery questions grounded in the target's technical stack and scale (`trap_setting_questions`), and provide an executive objection rebuttal framework (`objection_rebuttal`).
   - `ramp_milestones`: Build a concrete 30-60-90 day AE territory ramp plan (`days_30`, `days_60`, `days_90`) tailored specifically to selling {seller_name} into `company_name` and lookalike enterprise accounts.
   - `hiring_manager_pitch`: Write a concise, high-conviction cold email from the candidate AE to the VP of Sales / CRO at {seller_name} sharing this live Deal Desk brief on `company_name` (highlighting the ${'{'}land_deal_size{'}'} Land -> ${'{'}expansion_arr{'}'} Expansion thesis and immediate territory readiness).
"""


def _is_active_job_post(has_job_post: Any) -> bool:
    """Determines whether `has_job_post` represents an active hiring signal."""
    if isinstance(has_job_post, bool):
        return has_job_post
    if isinstance(has_job_post, str):
        cleaned = has_job_post.strip().lower()
        return cleaned not in ("", "false", "no", "0", "none", "null")
    return bool(has_job_post)


def build_prompt(target: ClayInputData, seller: Dict[str, Any]) -> str:
    """Constructs the structured synthesis prompt with scale-calibrated sizing guidance."""
    seller_name = seller.get("company_name", "Vanta")
    is_dynamic = seller.get("is_dynamic", False)
    scale_label, est_hc, scaled_eng, (land_min, land_max), (exp_min, exp_max) = _infer_target_scale(target)

    active_hiring = _is_active_job_post(target.has_job_post)
    job_post_detail = (
        "Active hiring signal detected (True)"
        if active_hiring
        else "No active job post / Frozen headcount (False)"
    )
    github_detail = target.github_org or "Enterprise GitHub / Private Repositories"
    tech_stack_str = _format_list_or_str(target.current_tech_stack or target.technographics, "Inferred from corporate engineering footprint")
    compliance_str = _format_list_or_str(target.compliance_frameworks or target.security_stack, "SOC 2 Type II, ISO 27001, Hybrid VPC")
    news_str = _format_list_or_str(target.recent_funding_or_news or target.trigger_events, target.funding_stage or "Enterprise AI & Platform Expansion")

    return f"""Analyze the following target account as an Enterprise Account Executive at {seller_name} and synthesize a complete Deal Desk intelligence brief:

Active Seller: {seller_name} ({'Dynamic Zero-YAML Seller Resolution — retrieve full product suite knowledge for ' + seller_name if is_dynamic else seller.get('category')})
Target Company Name: {target.company_name}
Inferred / Verified Scale Tier: {scale_label} (~{est_hc:,} total employees; ~{scaled_eng} AI/Platform engineers across the enterprise)
Calibrated Deal Sizing Guardrails for {target.company_name}:
- Required `land_deal_size` range: ${land_min:,} – ${land_max:,}
- Required `expansion_arr` range: ${exp_min:,} – ${exp_max:,}+
- Set `estimated_arr` to `land_deal_size`.

Additional Target Signals:
- Domain: {target.domain or 'Infer from company_name'}
- Industry: {target.industry or 'Infer from company_name'}
- Active Job Posting Signal (`has_job_post`): {job_post_detail}
- GitHub Organization (`github_org`): {github_detail}
- Recent Funding or News (`recent_funding_or_news`): {news_str}
- Compliance Frameworks (`compliance_frameworks`): {compliance_str}
- Current Tech Stack (`current_tech_stack`): {tech_stack_str}

Produce a structured JSON response conforming strictly to the DealDeskSynthesis schema:
1. If {seller_name} is not in static YAML (or is a specialized platform like Braintrust, Datadog, Wiz, etc.), dynamically retrieve your knowledge of {seller_name}'s enterprise product suite (e.g., for Braintrust: AI evaluations, prompt regression testing, automated red-teaming, datasets, and proxy caching) and derive its core value pillars and entry wedge (`ai_product_wedge`).
2. Enforce the calibrated deal sizing for {scale_label}: set `land_deal_size` between ${land_min:,} and ${land_max:,}, `expansion_arr` between ${exp_min:,} and ${exp_max:,}+, and `estimated_arr` equal to `land_deal_size`.
3. Scale `annual_evaluation_tax` across {target.company_name}'s enterprise-wide AI/engineering footprint (~{scaled_eng} builders across multi-squad initiatives, not just a single squad), and include both the annual tax ($ and FTEs) and the Land / Expansion ARR summary.
4. For `champion` and `economic_buyer`, provide the specific full name and official title of the real-world executive at {target.company_name} formatted strictly as "Full Name (Official Title)", or "<Official Title> (Target Persona)" if unverified.
5. Generate the 3-touch `outbound_sequence` from {seller_name} to {target.company_name} (<125 words per email, peer-to-peer tone, zero sales jargon, single clear CTA).
6. Synthesize `competitive_battlecard` (competitor name, 2 trap-setting discovery questions, objection rebuttal) and `ramp_milestones` (30-day, 60-day, and 90-day execution goals) grounded directly in {target.company_name}'s scale ({scale_label}) and technical stack ({tech_stack_str}).
7. Synthesize `hiring_manager_pitch` as a concise executive cold email to the VP of Sales / CRO at {seller_name} sharing this live Deal Desk account brief on {target.company_name}.
"""


def _deterministic_fallback(target: ClayInputData, seller: Dict[str, Any]) -> DealDeskSynthesis:
    """High-precision deterministic synthesis with enterprise-calibrated deal sizing and zero-YAML seller support."""
    seller_name = seller.get("company_name", "Vanta")
    seller_category = seller.get("category", f"{seller_name} Enterprise Platform")
    wedges = seller.get("primary_wedges") or [
        f"{seller_name} Enterprise Evaluation, Governance & Automation Suite"
    ]
    value_props = seller.get("core_value_props") or [
        f"Eliminate manual engineering and operational drag across enterprise teams with {seller_name}",
        f"Accelerate production release velocity and enterprise governance with {seller_name}",
    ]

    scale_label, total_hc, scaled_eng, (land_min, land_max), (exp_min, exp_max) = _infer_target_scale(target)
    land_deal_size = int((land_min + land_max) // 2)
    expansion_arr = int(exp_min if total_hc < 10000 else (exp_min + exp_max) // 2)
    estimated_arr = land_deal_size

    wasted_ftes = round(max(2.0, scaled_eng * 0.25), 1)
    eval_tax_usd = int(round(wasted_ftes * 240000))
    annual_eval_tax_str = (
        f"${eval_tax_usd:,}/yr ({wasted_ftes:g} Senior AI/Platform FTEs | "
        f"Land: ${land_deal_size:,} -> Expansion ARR: ${expansion_arr:,})"
    )

    active_hiring = _is_active_job_post(target.has_job_post)
    primary_wedge = target.ai_product_wedge or wedges[0]
    ai_wedge = primary_wedge if primary_wedge.lower().startswith(seller_name.lower()) else f"{seller_name}: {primary_wedge}"

    news_str = _format_list_or_str(target.recent_funding_or_news or target.trigger_events, "")
    if active_hiring:
        boss_hiring_signal = (
            f"Active Enterprise Expansion ({scale_label}) — Scaling ~{scaled_eng} AI/platform engineers"
            + (f" ({news_str})" if news_str else "")
            + f" requires standardized {seller_name} guardrails"
        )
    else:
        boss_hiring_signal = (
            f"Enterprise Efficiency Mandate ({scale_label}) — Reclaim {wasted_ftes:g} FTEs across ~{scaled_eng} engineers with {seller_name}"
        )

    compliance_str = _format_list_or_str(target.compliance_frameworks or target.security_stack, "SOC 2 Type II + Hybrid VPC")
    if target.github_org:
        security_ypc = f"{compliance_str} (github.com/{target.github_org})"
    else:
        security_ypc = f"{compliance_str} + Self-Hosted VPC"

    verified_directory = {
        "ramp": ("Karim Atiyeh (Co-Founder & CTO)", "Nik Koblov (VP of Engineering)"),
        "workday": ("Sayan Chakraborty (Co-President)", "Jim Stratton (CTO)"),
        "salesforce": ("Parker Harris (Co-Founder & CTO)", "Srini Tallapragada (President & Chief Engineering Officer)"),
        "snowflake": ("Sridhar Ramaswamy (CEO)", "Benoit Dageville (Co-Founder & President of Products)"),
        "intercom": ("Darragh Curran (CTO)", "Fergal Reid (VP of AI)"),
        "deel": ("Yaron Lavi (CTO)", "Head of AI Engineering (Target Persona)"),
        "checkr": ("Toma Bedolla (CTO)", "VP of Engineering (Target Persona)"),
        "ashby": ("Abhik Pramanik (Co-Founder & CTO)", "Head of Infrastructure & Security (Target Persona)"),
    }
    company_key = target.company_name.strip().lower()
    eb = next((s for s in target.stakeholders if s.role_type == "Economic Buyer"), None)
    champ = next((s for s in target.stakeholders if s.role_type == "Champion"), None)

    if eb:
        eb_label = f"{eb.name} ({eb.title})"
    elif company_key in verified_directory:
        eb_label = verified_directory[company_key][0]
    else:
        eb_label = "Chief Technology Officer (Target Persona)"

    if champ:
        champ_label = f"{champ.name} ({champ.title})"
    elif company_key in verified_directory:
        champ_label = verified_directory[company_key][1]
    else:
        champ_label = "VP of AI & Platform Engineering (Target Persona)"

    return DealDeskSynthesis(
        land_deal_size=land_deal_size,
        expansion_arr=expansion_arr,
        estimated_arr=estimated_arr,
        meddicc_health="Low Risk" if active_hiring else "Moderate Risk",
        deal_stage="Prospecting",
        ai_product_wedge=ai_wedge,
        annual_evaluation_tax=annual_eval_tax_str,
        champion=champ_label,
        economic_buyer=eb_label,
        boss_hiring_signal=boss_hiring_signal,
        security_ypc=security_ypc,
        executive_summary_callout=(
            f"{target.company_name} ({scale_label}, ~{scaled_eng} AI/platform engineers) incurs an estimated ${eval_tax_usd:,}/yr "
            f"({wasted_ftes:g} FTEs) in evaluation and infrastructure drag—supporting a ${land_deal_size:,} Land deal expanding to ${expansion_arr:,} Full Platform ARR for {seller_name}."
        ),
        challenger_why_now=(
            f"At {target.company_name}'s scale (~{total_hc:,} employees, ~{scaled_eng} AI/platform builders), fragmented squad-level testing and manual workflows "
            f"create a ${eval_tax_usd:,}/yr enterprise productivity tax that directly bottlenecks tier-1 product launches."
        ),
        reframe_insight=(
            f"The bottleneck at {target.company_name} is no longer model or cloud access—it is the ${eval_tax_usd:,}/yr ({wasted_ftes:g} FTEs) "
            f"enterprise-wide Evaluation & Operational Tax where senior builders maintain DIY plumbing instead of shipping core roadmap features."
        ),
        command_of_message=CommandOfMessagePillar(
            current_state=(
                f"~{scaled_eng} engineers across multiple squads relying on fragmented evaluation scripts, manual spot-checks, and siloed observability."
            ),
            negative_consequences=(
                f"${eval_tax_usd:,}/yr ({wasted_ftes:g} FTEs) in unrecovered engineering capacity, delayed enterprise releases, and regression risk."
            ),
            desired_state=(
                f"Standardized enterprise {seller_category} deployed via {ai_wedge}."
            ),
            positive_business_outcomes=(
                f"Reclaim ${eval_tax_usd:,}/yr ({wasted_ftes:g} FTEs), establish a ${land_deal_size:,} initial deployment, and scale to ${expansion_arr:,} full-platform ROI."
            ),
            required_capabilities=value_props,
            differentiators=wedges,
        ),
        meddicc=MEDDICCQualification(
            metrics=(
                f"${eval_tax_usd:,}/yr ({wasted_ftes:g} FTEs) enterprise-wide cost of inaction across ~{scaled_eng} engineers; "
                f"Land: ${land_deal_size:,} | Expansion ARR: ${expansion_arr:,}."
            ),
            economic_buyer=f"{eb_label} — owns enterprise engineering productivity, platform consolidation, and budget authority.",
            decision_criteria=[
                f"Enterprise-grade integration across {target.github_org or target.company_name}'s CI/CD and cloud footprint",
                f"Security & governance alignment with {security_ypc}",
                f"Measurable productivity recovery across ~{scaled_eng} builders",
            ],
            decision_process=(
                f"Scoped 14-day pilot (${land_deal_size:,} Land scope) led by {champ_label}, followed by multi-squad expansion (${expansion_arr:,} ARR) approved by {eb_label}."
            ),
            identified_pain=f"Enterprise-wide evaluation and infrastructure sprawl across ~{scaled_eng} engineers costing ${eval_tax_usd:,}/yr.",
            champion=f"{champ_label} — owns platform standardization, release velocity, and production reliability.",
            competition="Internal DIY squad-level scripts and fragmented point tools.",
            red_flags=[
                "Multi-Squad Sprawl: Ensure initial Land POC is scoped to a high-visibility flagship team before expanding enterprise-wide.",
                f"InfoSec & VPC Review: Engage security early around {security_ypc} deployment.",
            ],
        ),
        executive_outreach_sequence=[
            (
                f"Track A (Economic Buyer — {eb_label}): Subject: {target.company_name}'s ~{scaled_eng} AI/platform engineers / cutting the ${eval_tax_usd:,} eval tax\n"
                f"Across ~{scaled_eng} builders at {target.company_name}, maintaining squad-by-squad evaluation and governance plumbing quietly consumes ~${eval_tax_usd:,}/yr ({wasted_ftes:g} FTEs). "
                f"Open to a 12-minute brief on how {seller_name} consolidates that into an automated enterprise control plane?"
            ),
            (
                f"Track B (Champion — {champ_label}): Subject: Standardizing {primary_wedge} across {target.company_name}\n"
                f"With ~{scaled_eng} engineers shipping across {target.github_org or target.company_name}, are you standardizing automated CI/CD gates centrally or maintaining custom harnesses per squad?"
            ),
        ],
        outbound_sequence=OutboundSequence(
            email_1_subject=f"{target.company_name}'s ~{scaled_eng} AI builders + the ${eval_tax_usd:,}/yr eval tax",
            email_1_body=(
                f"{eb_label.split('(')[0].strip()},\n\n"
                f"At {target.company_name}'s scale (~{total_hc:,} employees, ~{scaled_eng} AI/platform engineers), the biggest drag on release velocity isn't model selection—it's squad-by-squad evaluation and governance sprawl.\n\n"
                f"Across comparable enterprise engineering orgs, ~25% of builder capacity (${eval_tax_usd:,}/yr or {wasted_ftes:g} FTEs) goes toward manual regression triage and DIY tooling.\n\n"
                f"Open to a 12-minute walkthrough on Thursday on how {seller_name} eliminates that tax?"
            ),
            email_2_subject=f"Re: {target.company_name} benchmark — reclaiming {wasted_ftes:g} senior FTEs",
            email_2_body=(
                f"{eb_label.split('(')[0].strip()},\n\n"
                f"When enterprise teams scale past {max(20, scaled_eng // 2)} AI engineers, homegrown testing harnesses break down because every squad measures accuracy, latency, and safety differently.\n\n"
                f"{value_props[0]}.\n\n"
                f"Worth a 10-minute conversation next week to review how a scoped ${land_deal_size:,} initial deployment unlocks ${eval_tax_usd:,}/yr in engineering capacity?"
            ),
            email_3_subject=f"{seller_name} architecture for {target.company_name}'s engineering org",
            email_3_body=(
                f"{champ_label.split('(')[0].strip()},\n\n"
                f"Maintaining custom evaluation and governance plumbing across ~{scaled_eng} engineers creates permanent build-vs-buy friction—especially under {security_ypc} requirements.\n\n"
                f"{seller_name} drops into your existing GitHub and cloud workflows in minutes, giving every squad unified CI/CD regression gates, automated red-teaming, and proxy caching.\n\n"
                f"Should I send over a 2-page enterprise architecture brief tailored to {target.company_name}?"
            ),
        ),
        competitive_battlecard=CompetitiveBattlecard(
            competitor_name=f"Internal DIY Evaluation Scripts & Point Observability Tools (at {scale_label})",
            trap_setting_questions=[
                f"When your ~{scaled_eng} engineers push prompt or model updates across {target.github_org or target.company_name}'s repositories, how do you automatically block a GitHub PR when accuracy regresses on edge-case production traces?",
                f"How many senior engineering hours per sprint are spent maintaining custom evaluation scripts and {security_ypc} data-plane plumbing rather than shipping core product roadmap features?",
            ],
            objection_rebuttal=(
                f"Objection: 'Our engineers already built an internal evaluation harness.' -> "
                f"Rebuttal: Internal harnesses work for a single prototype, but across ~{scaled_eng} builders at {target.company_name}, maintaining DIY scripts quietly taxes {annual_eval_tax_str} in senior FTE capacity. "
                f"{seller_name} replaces that maintenance burden in 15 minutes while providing turnkey CI/CD gates, automated red-teaming, and {security_ypc} governance."
            ),
        ),
        ramp_milestones=RampMilestones(
            days_30=(
                f"Complete {seller_name} technical & demo certification, audit the top 50 territory accounts by engineering headcount and hiring signals, "
                f"and launch multi-threaded Challenger outbound into {target.company_name} ({eb_label} & {champ_label}) plus 14 lookalike accounts."
            ),
            days_60=(
                f"Convert {target.company_name} discovery into a scoped 14-day Sandbox POC (${land_deal_size:,} Land scope) with {champ_label}, "
                f"complete {security_ypc} InfoSec review, and generate $600k+ in qualified Stage-2 territory pipeline."
            ),
            days_90=(
                f"Close the ${land_deal_size:,} initial Land contract at {target.company_name}, co-author the multi-squad expansion roadmap toward "
                f"${expansion_arr:,} Full Platform ARR with {eb_label}, and achieve 100%+ quarterly pipeline quota coverage."
            ),
        ),
        hiring_manager_pitch=(
            f"Subject: Work Trial Brief: {seller_name} -> {target.company_name} (${land_deal_size:,} Land / ${expansion_arr:,} Expansion ARR)\n\n"
            f"Hi [VP of Sales / CRO],\n\n"
            f"Ahead of our conversation, I built an automated Deal Desk & Account Intelligence engine that ingests live account signals and synthesizes a full MEDDICC, Command of the Message, and Challenger pursuit plan for {seller_name}.\n\n"
            f"For {target.company_name} ({scale_label}, ~{scaled_eng} AI/platform engineers), I mapped a ${land_deal_size:,} Land wedge ({ai_wedge}) anchored in reclaiming ${eval_tax_usd:,}/yr ({wasted_ftes:g} FTEs) in engineering drag, with a clear path to ${expansion_arr:,} Full Platform ARR via {eb_label} and {champ_label}.\n\n"
            f"Here is the live Notion Deal Desk brief—I'd welcome 15 minutes this week to walk through how I'll deploy this playbook across your territory on Day 1."
        ),
    )


def synthesize_account(
    target: ClayInputData,
    config: Dict[str, Any],
    force_offline: bool = False,
    seller_override: Optional[str] = None,
) -> DealDeskSynthesis:
    """Runs Gemini (`gemini-2.5-flash`) structured synthesis with dynamic seller resolution and enterprise-calibrated sizing."""
    load_dotenv()
    seller = load_seller_profile(seller_override=seller_override)
    mode_tag = "Dynamic Zero-YAML Knowledge" if seller.get("is_dynamic") else "YAML Playbook"
    print(f"[*] Active Seller Persona: {seller.get('company_name')} [{mode_tag}]")

    llm_cfg = config.get("llm", {})
    model_name = llm_cfg.get("model", "gemini-2.5-flash").removeprefix("models/")
    temperature = float(llm_cfg.get("temperature", 0.2))

    raw_key = os.environ.get("GEMINI_API_KEY") or llm_cfg.get("api_key", "")
    api_key = "" if str(raw_key).startswith("${") else str(raw_key)

    if force_offline or not api_key:
        if not force_offline:
            print("[*] GEMINI_API_KEY not found in environment; using deterministic schema-validated synthesis.")
        return _deterministic_fallback(target, seller)

    client = genai.Client(api_key=api_key)
    system_instruction = build_system_instruction(seller)
    prompt = build_prompt(target, seller)

    max_retries = 4
    initial_delay = 2
    response = None

    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    temperature=temperature,
                    response_mime_type="application/json",
                    response_schema=DealDeskSynthesis,
                ),
            )
            break
        except Exception as exc:
            err_msg = str(exc).upper()
            is_transient = (
                "503" in err_msg
                or "UNAVAILABLE" in err_msg
                or "TIMEOUT" in err_msg
                or "TIMED OUT" in err_msg
                or isinstance(exc, (TimeoutError, ConnectionError))
            )
            if not is_transient or attempt == max_retries - 1:
                raise
            delay = initial_delay * (2 ** attempt)
            print(
                f"[!] 503 high demand spike encountered. Retrying in {delay}s (Attempt {attempt + 1}/{max_retries})..."
            )
            time.sleep(delay)

    return DealDeskSynthesis.model_validate_json(response.text)
