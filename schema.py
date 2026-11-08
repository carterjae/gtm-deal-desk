"""Pydantic schemas for Clay staging inputs and structured Gemini Deal Desk outputs."""

from typing import Any, List, Literal, Optional, Union
from pydantic import BaseModel, Field, field_validator
from models.intelligence import (
    CapitalSignal,
    LeadershipSignal,
    StrategicNewsSignal,
    CompanyDossier,
)


class StakeholderSignal(BaseModel):
    """Key executive or champion stakeholder enriched via Clay."""
    name: str = Field(..., description="Full name of the stakeholder")
    title: str = Field(..., description="Current job title")
    role_type: Literal["Economic Buyer", "Champion", "Technical Evaluator", "Blocker"] = Field(
        ..., description="MEDDICC stakeholder persona classification"
    )
    linkedin_url: Optional[str] = Field(None, description="LinkedIn profile URL")
    outreach_angle: Optional[str] = Field(None, description="Signal-backed hook for outreach")


class ClayInputData(BaseModel):
    """Validated schema for incoming Clay account enrichment JSON payloads."""
    company_name: str = Field(..., description="Target company name")
    github_org: Optional[str] = Field(
        default=None,
        description="GitHub organization handle or URL informing OSS exposure, CI/CD posture, and Security / YPC risk",
    )
    has_job_post: Union[bool, str, None] = Field(
        default=False,
        description="Whether the company has active job postings (cast cleanly to boolean, default False)",
    )
    ai_engineer_count: Union[int, str, None] = Field(
        default=0,
        description="Estimated number of AI/ML or core platform engineers (default 0)",
    )
    # Multi-industry adaptable optional fields
    domain: Optional[str] = Field(default=None, description="Target company domain (e.g., ramp.com)")
    industry: Optional[str] = Field(default=None, description="Primary industry vertical")
    headcount: Optional[Union[int, str]] = Field(
        default=None, description="Total company employee headcount"
    )
    recent_funding_or_news: Optional[Union[str, List[str]]] = Field(
        default=None, description="Recent funding round, product launch, M&A, or executive news"
    )
    compliance_frameworks: Optional[Union[str, List[str]]] = Field(
        default=None, description="Existing or required compliance frameworks (e.g., SOC 2, ISO 27001, HIPAA, PCI-DSS)"
    )
    current_tech_stack: Optional[Union[str, List[str]]] = Field(
        default=None, description="Detected cloud, security, finance, or AI technology stack"
    )

    # Backwards-compatible optional fields
    employee_count: Optional[int] = Field(default=None, description="Alias for total headcount")
    funding_stage: Optional[str] = Field(default=None, description="Optional funding round")
    ai_product_wedge: Optional[str] = Field(default=None, description="Optional pre-identified product wedge")
    technographics: List[str] = Field(default_factory=list, description="Optional detected tech stack")
    security_stack: List[str] = Field(default_factory=list, description="Optional security/compliance signals")
    hiring_signals: List[str] = Field(default_factory=list, description="Optional hiring velocity strings")
    evaluation_triggers: List[str] = Field(default_factory=list, description="Optional evaluation bottleneck signals")
    trigger_events: List[str] = Field(default_factory=list, description="Optional recent trigger events")
    stakeholders: List[StakeholderSignal] = Field(default_factory=list, description="Optional enriched contacts")

    @field_validator("github_org", "domain", "industry", mode="before")
    @classmethod
    def sanitize_optional_str(cls, v: Any) -> Optional[str]:
        if v is None:
            return None
        s = str(v).strip()
        if not s or s.startswith("/") or s.lower() in ("null", "none", "n/a", "false"):
            return None
        return s

    @field_validator("ai_engineer_count", mode="before")
    @classmethod
    def sanitize_ai_engineer_count(cls, v: Any) -> int:
        if v is None or isinstance(v, bool):
            return 0
        if isinstance(v, (int, float)):
            return max(0, int(v))
        if isinstance(v, str):
            s = v.strip()
            if not s or s.startswith("/"):
                return 0
            try:
                return max(0, int(float(s.replace(",", ""))))
            except (ValueError, TypeError):
                return 0
        return 0

    @field_validator("headcount", mode="before")
    @classmethod
    def sanitize_headcount(cls, v: Any) -> Optional[int]:
        if v is None or isinstance(v, bool):
            return None
        if isinstance(v, (int, float)):
            return max(0, int(v))
        if isinstance(v, str):
            s = v.strip()
            if not s or s.startswith("/"):
                return None
            try:
                return max(0, int(float(s.replace(",", ""))))
            except (ValueError, TypeError):
                return None
        return None

    @field_validator("has_job_post", mode="before")
    @classmethod
    def sanitize_has_job_post(cls, v: Any) -> bool:
        if v is None:
            return False
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)):
            return bool(v)
        if isinstance(v, str):
            s = v.strip()
            if not s or s.startswith("/"):
                return False
            low = s.lower()
            if low in ("false", "0", "no", "none", "null", "n/a", "nan"):
                return False
            if low in ("true", "1", "yes", "y"):
                return True
            return True
        return False


# Backwards-compatible alias
ClayStagingInput = ClayInputData


class CommandOfMessagePillar(BaseModel):
    """Force Management Command of the Message (CoM) value driver mapping."""
    current_state: str = Field(..., description="How the prospect currently operates without the seller's platform")
    negative_consequences: str = Field(..., description="Engineering, operational, and revenue cost of maintaining the status quo")
    desired_state: str = Field(..., description="Target operational state enabled by the seller's platform")
    positive_business_outcomes: str = Field(..., description="Quantified executive ROI and velocity gains")
    required_capabilities: List[str] = Field(
        ..., description="Technical and operational capabilities required to achieve Positive Business Outcomes"
    )
    differentiators: List[str] = Field(
        ..., description="Defensible product differentiators and trap-setting criteria against DIY/competitors"
    )


class MEDDICCQualification(BaseModel):
    """Structured MEDDICC qualification matrix and risk assessment."""
    metrics: str = Field(..., description="Quantified economic impact and cost of inaction")
    economic_buyer: str = Field(..., description="Identified Economic Buyer persona/title and budget authority")
    decision_criteria: List[str] = Field(..., description="Technical and commercial criteria required to win")
    decision_process: str = Field(..., description="Expected POC/evaluation validation and security/procurement workflow")
    identified_pain: str = Field(..., description="Core operational pain grounded in Clay triggers")
    champion: str = Field(..., description="Target technical/operational Champion persona/title and internal mobilization thesis")
    competition: str = Field(..., description="Primary competition (Status Quo / DIY processes / alternative vendors)")
    red_flags: List[str] = Field(..., description="Specific deal risks, blockers, and mitigation steps")


class OutboundSequence(BaseModel):
    """3-touch Challenger outbound email sequence grounded in Clay account signals."""
    email_1_subject: str = Field(..., description="Touch 1 subject line (Commercial insight / 'Why Now' hook)")
    email_1_body: str = Field(
        ...,
        description="Touch 1 body (<125 words, peer-to-peer tone, zero sales jargon, single clear CTA) anchored in Commercial insight / 'Why Now' hook",
    )
    email_2_subject: str = Field(..., description="Touch 2 subject line (Peer provocation / metric benchmarking)")
    email_2_body: str = Field(
        ...,
        description="Touch 2 body (<125 words, peer-to-peer tone, zero sales jargon, single clear CTA) anchored in Peer provocation / metric benchmarking",
    )
    email_3_subject: str = Field(..., description="Touch 3 subject line (Technical wedge / build-vs-buy friction)")
    email_3_body: str = Field(
        ...,
        description="Touch 3 body (<125 words, peer-to-peer tone, zero sales jargon, single clear CTA) anchored in Technical wedge / build-vs-buy friction",
    )


class CompetitiveBattlecard(BaseModel):
    """Competitive differentiation, trap-setting discovery questions, and objection rebuttal framework."""
    competitor_name: str = Field(
        ...,
        description="Primary competitor or internal DIY alternative encountered at this target company scale",
    )
    trap_setting_questions: List[str] = Field(
        ...,
        description="2 sharp trap-setting discovery questions grounded in the target's technical stack and scale that expose competitor/DIY weaknesses",
    )
    objection_rebuttal: str = Field(
        ...,
        description="Executive objection & rebuttal framework neutralizing the primary competitor or DIY build-vs-buy objection",
    )


class RampMilestones(BaseModel):
    """30-60-90 day AE territory and account execution milestones tailored to the active seller's product."""
    days_30: str = Field(
        ...,
        description="First 30 Days: Product certification, territory stack-ranking, and launching signal-grounded outbound on this target account",
    )
    days_60: str = Field(
        ...,
        description="First 60 Days: Multi-threading Champion to Economic Buyer, scoping the initial Land POC, and running competitive trap-setting",
    )
    days_90: str = Field(
        ...,
        description="First 90 Days: Closing the calibrated Land contract, establishing the expansion roadmap to Full Platform ARR, and scaling repeatable territory pipeline",
    )


class DealDeskSynthesis(BaseModel):
    """Structured output schema enforced via Gemini response_schema."""
    land_deal_size: int = Field(
        ...,
        description="Initial Land Contract Value in USD calibrated to Target Company Scale ($45k-$80k Mid-Market, $90k-$160k Large Enterprise, $175k-$250k Strategic/F500)",
    )
    expansion_arr: int = Field(
        ...,
        description="Full Platform Expansion ARR Potential in USD calibrated to Target Company Scale ($120k Mid-Market, $250k+ Large Enterprise, $350k-$600k+ Strategic/F500)",
    )
    estimated_arr: int = Field(
        ...,
        description="Primary Deal Size / ARR figure in USD pushed to Notion Deal Size column (matches land_deal_size or blended ARR)",
    )
    meddicc_health: Literal["Low Risk", "Moderate Risk", "High Risk"] = Field(
        ..., description="Overall MEDDICC deal qualification health score"
    )
    deal_stage: Literal["Prospecting", "Discovery", "Technical Validation", "Executive Alignment"] = Field(
        default="Prospecting", description="Recommended Deal Desk pipeline stage (e.g., 'Prospecting')"
    )
    ai_product_wedge: str = Field(
        ..., description="Synthesized primary Product Wedge from the active seller's portfolio matched to the target's signals"
    )
    annual_evaluation_tax: str = Field(
        ..., description="Quantified Annual Cost of Inaction / Evaluation or Operational Tax in USD and FTEs"
    )
    champion: str = Field(
        ...,
        description="Real-world Champion formatted strictly as 'Full Name (Official Title)', or '<Official Title> (Target Persona)' if unverified",
    )
    economic_buyer: str = Field(
        ...,
        description="Real-world executive Economic Buyer formatted strictly as 'Full Name (Official Title)', or '<Official Title> (Target Persona)' if unverified",
    )
    boss_hiring_signal: str = Field(
        ..., description="Synthesized Boss / Hiring Signal driven by has_job_post and recent_funding_or_news"
    )
    security_ypc: str = Field(
        ..., description="Concise Security / Compliance / Infrastructure posture informed by github_org and compliance_frameworks"
    )
    executive_summary_callout: str = Field(
        ..., description="High-impact 2-sentence executive summary for the top Notion callout block"
    )
    challenger_why_now: str = Field(
        ..., description="Challenger 'Why Now' commercial thesis linking the target's signals to the active seller's value props"
    )
    reframe_insight: str = Field(
        ..., description="Provocative commercial reframe teaching the prospect a hidden cost in their current workflow"
    )
    command_of_message: CommandOfMessagePillar = Field(
        ..., description="Command of the Message value driver matrix"
    )
    meddicc: MEDDICCQualification = Field(
        ..., description="Complete MEDDICC qualification and risk matrix"
    )
    executive_outreach_sequence: List[str] = Field(
        ..., description="2-step signal-grounded outbound sequence (Track A: Economic Buyer, Track B: Champion)"
    )
    outbound_sequence: OutboundSequence = Field(
        ..., description="3-touch outbound Challenger email sequence grounded in the target's Clay signals"
    )
    competitive_battlecard: CompetitiveBattlecard = Field(
        ...,
        description="Competitive differentiation battlecard with competitor name, 2 trap-setting discovery questions, and objection rebuttal",
    )
    ramp_milestones: RampMilestones = Field(
        ...,
        description="30-60-90 day AE territory execution goals tailored to the active seller's product and target account scale",
    )
    hiring_manager_pitch: str = Field(
        ...,
        description="Concise, high-signal cold email to the VP of Sales / CRO of the 'Selling For' company sharing this live Deal Desk account brief as a work-trial artifact",
    )
