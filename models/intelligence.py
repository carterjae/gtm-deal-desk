"""
Pydantic schemas for the unified Company Signal Harvester & Dossier layer.
"""

from typing import List, Optional
from pydantic import BaseModel, Field


class CapitalSignal(BaseModel):
    funding_stage: str = Field(description="e.g. Series A, Series B, Seed, Bootstrapped, Public")
    total_funding_amount: Optional[str] = Field(default="Undisclosed", description="e.g. $18M, $45M")
    latest_round_date: Optional[str] = Field(default="Recent", description="Month and year of latest round")
    lead_investors: List[str] = Field(default=[], description="Lead venture funds, e.g. Y Combinator, Sequoia")
    crunchbase_valuation_hint: Optional[str] = Field(default="N/A", description="Valuation or ARR estimate if public")


class LeadershipSignal(BaseModel):
    economic_buyer_name: str = Field(description="Probable VP of Sales, CRO, Head of GTM, or Founder")
    economic_buyer_title: str = Field(description="Exact executive title")
    economic_buyer_linkedin: Optional[str] = Field(default=None)
    recent_leadership_hires: List[str] = Field(default=[], description="Any executives who joined in the last 6 months")


class StrategicNewsSignal(BaseModel):
    recent_headline: str = Field(description="Most significant news headline or product launch from past 6 months")
    news_source: Optional[str] = Field(default="Press Release", description="e.g. PR Newswire, TechCrunch, Company Blog")
    why_now_trigger: str = Field(description="How this news creates urgency for hiring or GTM acceleration")


class CompanyDossier(BaseModel):
    company_name: str
    company_domain: str
    headcount_bracket: str = Field(description="e.g. 1-10, 11-50, 51-200, 201-500, 500+")
    capital: CapitalSignal
    leadership: LeadershipSignal
    news: StrategicNewsSignal
    verified_tech_stack: List[str] = Field(default=[], description="Key tools identified, e.g. Salesforce, HubSpot, Clay, Snowflake")
