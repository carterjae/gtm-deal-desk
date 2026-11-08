#!/usr/bin/env python3
"""
Daily 7:00 AM Autonomous Job Scout (scripts/morning_job_scout.py).

Queries live public ATS JSON APIs directly (Ashby & Greenhouse) so every single Job Posting URL
is a 100% real, active, HTTP-200 verified posting.
Generates:
  - 'Fit Justification' property (explaining the fit scoring)
  - Concise 'Role & Mandate Summary' (no raw JD text dumps!)
  - 'How Your Experience Transfers' (3 clear transfer bullets mapping Jae's background to the role)
"""

import argparse
from datetime import datetime, timedelta
import fcntl
import html
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse
import requests
from dotenv import load_dotenv
from google import genai
from google.genai import types

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
ENV_PATH = PROJECT_ROOT / ".env"
load_dotenv(ENV_PATH)

from scripts.compile_resume import load_master_profile
from scripts.contact_verifier import generate_hiring_committee_search_url

NOTION_VERSION = "2022-06-28"
SCOUT_STAGE = "00. Morning Scout (Approve / Pass)"
SCOUT_LOCK_FILE = "/tmp/morning_job_scout.lock"


def acquire_scout_lock() -> Optional[Any]:
    """Acquires an exclusive POSIX file lock to prevent concurrent scout runs."""
    try:
        lock_fd = open(SCOUT_LOCK_FILE, "w")
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return lock_fd
    except (IOError, OSError):
        return None


def release_scout_lock(lock_fd: Optional[Any]) -> None:
    """Releases the POSIX file lock."""
    if lock_fd:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            lock_fd.close()
        except Exception:
            pass

ASHBY_BOARDS: List[Tuple[str, str, str, str, str]] = [
    ("Vanta", "vanta", "https://www.vanta.com", "$150M Series C at $2.45B Valuation (Sequoia & Craft)", "VP of Revenue Operations & GTM Systems (Vanta)"),
    ("LangChain", "langchain", "https://www.langchain.com", "$25M Series A / $1.1B Valuation (IVP & Sequoia)", "Head of GTM & Revenue Operations (LangChain)"),
    ("Sierra", "sierra", "https://sierra.ai", "$175M Funding at $4.5B Valuation (Greenoaks & Benchmark)", "Head of Revenue Operations & GTM Systems (Sierra)"),
    ("Decagon", "decagon", "https://decagon.ai", "$65M Series B (Bain Capital Ventures & Accel)", "Jesse Zhang, Co-Founder & CEO / Head of GTM"),
    ("Ramp", "ramp", "https://ramp.com", "$150M Series D-2 at $7.65B Valuation (Khosla & Founders Fund)", "Head of GTM Engineering & RevOps (Ramp)"),
    ("Clay", "claylabs", "https://www.clay.com", "$62M Series B at $1.25B Valuation (Meritech & Sequoia)", "Varun Anand, Co-Founder & Head of GTM Engineering"),
    ("Harvey", "harvey", "https://www.harvey.ai", "$300M Series D at $3.0B Valuation (GV, OpenAI, Sequoia)", "VP of Revenue Operations & GTM Systems (Harvey)"),
    ("Linear", "linear", "https://linear.app", "$35M Series B (Sequoia & Accel)", "Head of Sales & Revenue Operations (Linear)"),
    ("Baseten", "baseten", "https://www.baseten.co", "$75M Series C (IVP & Spark Capital)", "Tuhin Srivastava, Co-Founder & CEO / Head of GTM"),
    ("Cohere", "cohere", "https://cohere.com", "$500M Series D at $5.5B Valuation", "VP of Revenue Operations & GTM Systems (Cohere)"),
    ("Modal", "modal", "https://modal.com", "$16M Series A (Redpoint Ventures)", "Erik Bernhardsson, Founder & CEO / Head of GTM"),
    ("Notion", "notion", "https://www.notion.so", "$10B Valuation (Sequoia & Index Ventures)", "Director of GTM Systems & Revenue Operations (Notion)"),
    ("Zapier", "zapier", "https://zapier.com", "$5B Valuation (Sequoia & Steadfast)", "Director of Revenue Operations & GTM Automation (Zapier)"),
    ("ElevenLabs", "elevenlabs", "https://elevenlabs.io", "$180M Series C at $3.3B Valuation (a16z & ICONIQ)", "Head of Revenue Operations & GTM Systems (ElevenLabs)"),
    ("Perplexity", "perplexity", "https://www.perplexity.ai", "$500M Round at $9B Valuation (IVP)", "Head of Enterprise GTM & Revenue Operations (Perplexity)"),
    ("PostHog", "posthog", "https://posthog.com", "$27M Series B (GV & Y Combinator)", "James Hawkins, Co-Founder & CEO"),
    ("Pinecone", "pinecone", "https://pinecone.io", "$100M Series B at $750M Valuation (a16z)", "VP of Revenue Operations & GTM Systems (Pinecone)"),
    ("Braintrust", "braintrust", "https://www.braintrust.com", "$36M Series A (a16z & Elad Gil)", "Ankur Goyal, Founder & CEO"),
    ("Replit", "replit", "https://replit.com", "$97M Series B at $1.16B Valuation (a16z)", "VP of GTM & Revenue Operations (Replit)"),
    ("Weaviate", "weaviate", "https://weaviate.io", "$50M Series B (Index Ventures & Battery)", "Head of Revenue Operations & GTM (Weaviate)"),
    ("Supabase", "supabase", "https://supabase.com", "$80M Series B (Coatue & Lightspeed)", "Head of Growth & Revenue Operations (Supabase)"),
    ("Tavus", "tavus", "https://tavus.io", "$18M Series A (Scale Venture Partners & Sequoia)", "Hassaan Raza, Co-Founder & CEO"),
    ("Writer", "writer", "https://writer.com", "$200M Series C at $1.9B Valuation (ICONIQ Growth)", "VP of Revenue Operations & GTM Systems (Writer)"),
    ("Cursor", "cursor", "https://www.cursor.com", "$105M Series B at $2.5B Valuation (a16z & Thrive)", "Head of Enterprise GTM & Operations (Cursor)"),
    ("Cognition", "cognition", "https://cognition.ai", "$175M Series A at $2B Valuation (Founders Fund)", "Head of GTM & Operations (Cognition)"),
    ("Fireworks AI", "fireworks", "https://fireworks.ai", "$52M Series B at $552M Valuation (Sequoia)", "Head of GTM & Revenue Operations (Fireworks AI)"),
    ("Snowflake", "snowflake", "https://www.snowflake.com", "Public Enterprise Data Cloud (NYSE: SNOW)", "VP of Global Revenue Operations & GTM Systems (Snowflake)"),
    ("Drata", "drata", "https://drata.com", "$200M Series C at $2B Valuation (ICONIQ & GGV)", "VP of Revenue Operations & GTM Systems (Drata)"),
    ("Hex", "hex", "https://hex.tech", "$52M Series B (Sequoia & a16z)", "Head of Revenue Operations & GTM (Hex)"),
    ("Snyk", "snyk", "https://snyk.io", "$7.4B Valuation (Accel & Tiger Global)", "VP of Revenue Operations & GTM Systems (Snyk)"),
    ("Miro", "miro", "https://miro.com", "$400M Series C at $17.5B Valuation (ICONIQ & Accel)", "VP of Revenue Operations & GTM Systems (Miro)"),
    ("Plaid", "plaid", "https://plaid.com", "$13.4B Valuation (Altimeter & Silver Lake)", "Head of Revenue Operations & GTM Systems (Plaid)"),
    ("Temporal", "temporal", "https://temporal.io", "$75M Series B at $1.5B Valuation (Index Ventures)", "VP of Revenue Operations & GTM (Temporal)"),
    ("ClickUp", "clickup", "https://clickup.com", "$400M Series C at $4B Valuation (a16z & Tiger Global)", "VP of Revenue Operations & GTM Systems (ClickUp)"),
    ("Lambda Labs", "lambda", "https://lambdalabs.com", "$480M Series D (Andra Capital & NVIDIA)", "VP of Revenue Operations & GTM (Lambda Labs)"),
    ("Docker", "docker", "https://www.docker.com", "$105M Series C at $2.1B Valuation (Bain Capital)", "VP of Revenue Operations & GTM Systems (Docker)"),
    ("Deepgram", "deepgram", "https://deepgram.com", "$72M Series B (Tiger Global & YC)", "Head of Revenue Operations & GTM (Deepgram)"),
    ("Synthesia", "synthesia", "https://synthesia.io", "$180M Series D at $2.1B Valuation (NEA & Accel)", "VP of Revenue Operations & GTM Systems (Synthesia)"),
    ("Resend", "resend", "https://resend.com", "$3M Seed (Y Combinator & SV Angel)", "Zeno Rocha, Founder & CEO"),
    ("G2", "g2", "https://www.g2.com", "$1.1B Valuation (Permira & Salesforce Ventures)", "VP of Revenue Operations & GTM Systems (G2)"),
    ("Pylon", "pylon", "https://usepylon.com", "$17M Series A (Andreessen Horowitz & General Catalyst)", "Marty Kausas, Co-Founder & CEO"),
    ("Unify", "unify", "https://www.unifygtm.com", "$18M Series A (Thrive Capital & OpenAI Startup Fund)", "Austin Hughes, Co-Founder & CEO"),
    ("Dust", "dust", "https://dust.tt", "$16M Series A (Sequoia Capital)", "Gabriel Hubert, Co-Founder & CEO"),
    ("Abridge", "abridge", "https://www.abridge.com", "$150M Series C (Lightspeed & Redpoint)", "VP of Revenue Operations & GTM Systems (Abridge)"),
    ("Zip", "zip", "https://ziphq.com", "$190M Series D at $2.2B Valuation (BOND & Y Combinator)", "VP of Revenue Operations & GTM Systems (Zip)"),
    ("Lovable", "lovable", "https://lovable.dev", "$15M Pre-Series A (Creandum)", "Head of Growth & GTM Operations (Lovable)"),
]

GREENHOUSE_BOARDS: List[Tuple[str, str, str, str, str]] = [
    ("Glean", "gleanwork", "https://www.glean.com", "$260M Series E at $4.6B Valuation (Altimeter & DST)", "VP of Revenue Operations & GTM Systems (Glean)"),
    ("Anthropic", "anthropic", "https://www.anthropic.com", "$8B+ Strategic Enterprise AI Investment", "Head of GTM Systems & Revenue Operations (Anthropic)"),
    ("Stripe", "stripe", "https://stripe.com", "$70B Enterprise Fintech Infrastructure Leader", "Director of GTM Systems & Revenue Operations (Stripe)"),
    ("Cloudflare", "cloudflare", "https://www.cloudflare.com", "Public Enterprise Cloud & AI Connectivity (NYSE: NET)", "VP of Global Revenue Operations & GTM Systems (Cloudflare)"),
    ("Databricks", "databricks", "https://www.databricks.com", "$10B Series J at $62B Valuation", "VP of Revenue Operations & GTM Systems (Databricks)"),
    ("Vercel", "vercel", "https://vercel.com", "$250M Series E at $3.25B Valuation (Accel)", "VP of Revenue Operations & GTM Engineering (Vercel)"),
    ("Scale AI", "scaleai", "https://scale.com", "$1B Series F at $13.8B Valuation (Accel)", "Head of Enterprise GTM Operations & Systems (Scale AI)"),
    ("Apollo.io", "apolloio", "https://www.apollo.io", "$100M Series D at $1.6B Valuation (Bain Capital)", "VP of Revenue Operations & GTM Engineering (Apollo.io)"),
    ("Gusto", "gusto", "https://gusto.com", "$9.5B Valuation (Fidelity & Generation)", "Director of GTM Systems & Revenue Operations (Gusto)"),
    ("Figma", "figma", "https://figma.com", "$12.5B Valuation (Sequoia & Kleiner Perkins)", "Director of Revenue Operations & GTM Systems (Figma)"),
    ("Samsara", "samsara", "https://samsara.com", "Public Enterprise IoT Operations Cloud (NYSE: IOT)", "VP of Global Revenue Operations & Systems (Samsara)"),
    ("Rubrik", "rubrik", "https://rubrik.com", "Public Enterprise Zero-Trust Data Security (NYSE: RBRK)", "VP of Revenue Operations & GTM Systems (Rubrik)"),
    ("MongoDB", "mongodb", "https://mongodb.com", "Public Enterprise Cloud Database (NASDAQ: MDB)", "VP of Global Revenue Operations & GTM Systems (MongoDB)"),
    ("Datadog", "datadog", "https://datadoghq.com", "Public Cloud Observability & Security (NASDAQ: DDOG)", "VP of Revenue Operations & GTM Analytics (Datadog)"),
    ("Elastic", "elastic", "https://elastic.co", "Public Enterprise Search & Observability (NYSE: ESTC)", "VP of Global Revenue Operations & Systems (Elastic)"),
    ("GitLab", "gitlab", "https://gitlab.com", "Public Enterprise DevSecOps Platform (NASDAQ: GTLB)", "VP of Revenue Operations & GTM Systems (GitLab)"),
    ("Asana", "asana", "https://asana.com", "Public Enterprise Work Management (NYSE: ASAN)", "VP of Revenue Operations & GTM Systems (Asana)"),
    ("Brex", "brex", "https://brex.com", "$12.3B Valuation (Greenoaks & TCV)", "VP of Revenue Operations & GTM Engineering (Brex)"),
    ("Carta", "carta", "https://carta.com", "$7.4B Valuation (Andreessen Horowitz & Lightspeed)", "VP of Revenue Operations & GTM Systems (Carta)"),
    ("Together AI", "togetherai", "https://together.ai", "$106M Funding at $1.25B Valuation (Salesforce & Kleiner Perkins)", "Head of Revenue Operations & Enterprise GTM (Together AI)"),
    ("Checkr", "checkr", "https://checkr.com", "$5B Valuation (T. Rowe Price & Coatue)", "VP of Revenue Operations & GTM Systems (Checkr)"),
    ("Fivetran", "fivetran", "https://fivetran.com", "$5.6B Valuation (a16z & General Catalyst)", "VP of Global Revenue Operations & Systems (Fivetran)"),
    ("CoreWeave", "coreweave", "https://coreweave.com", "$1.1B Series C at $19B Valuation (Coatue & Fidelity)", "VP of Revenue Operations & Commercial Systems (CoreWeave)"),
    ("Intercom", "intercom", "https://intercom.com", "Enterprise AI Customer Service Platform", "Director of Revenue Operations & GTM Systems (Intercom)"),
    ("AssemblyAI", "assemblyai", "https://assemblyai.com", "$50M Series C (Accel & Insight Partners)", "Head of Revenue Operations & GTM (AssemblyAI)"),
    ("Abacus.AI", "abacus", "https://abacus.ai", "$50M Series C (Tiger Global & Index)", "Bindu Reddy, CEO & Co-Founder / Head of GTM"),
    ("Cresta", "cresta", "https://cresta.com", "$125M Series D (World Innovation Lab & Sequoia)", "VP of Revenue Operations & GTM Systems (Cresta)"),
    ("Gong", "gongio", "https://www.gong.io", "$7.25B Valuation (Coatue, Sequoia & Thrive)", "VP of Global Revenue Operations & GTM Systems (Gong)"),
    ("Braze", "braze", "https://www.braze.com", "Public Customer Engagement Platform (NASDAQ: BRZE)", "VP of Revenue Operations & GTM Systems (Braze)"),
    ("Mixpanel", "mixpanel", "https://mixpanel.com", "$200M Series C at $1.05B Valuation (Bain Capital)", "VP of Revenue Operations & GTM Systems (Mixpanel)"),
    ("Verkada", "verkada", "https://www.verkada.com", "$100M Series D at $3.2B Valuation (Alkeon & Meritech)", "VP of Revenue Operations & GTM Engineering (Verkada)"),
    ("Abnormal Security", "AbnormalSecurity", "https://abnormalsecurity.com", "$250M Series D at $5.1B Valuation (Wellington & Greylock)", "VP of Revenue Operations & GTM Systems (Abnormal Security)"),
]

NON_US_MARKERS = (
    "uk", "england", "london", "dublin", "ireland", "germany", "munich", "berlin", "france", "paris", "lyon",
    "japan", "tokyo", "sweden", "stockholm", "amsterdam", "netherlands", "benelux", "dach",
    "emea", "apac", "latam", "sydney", "australia", "singapore", "india", "bangalore", "bengaluru",
    "brazil", "korea", "spain", "madrid", "barcelona", "toronto", "canada", "vancouver", "montreal",
    "china", "uae", "dubai", "saudi", "israel", "tel aviv", "europe", "eu", "mexico", "mexico city",
    "romania", "cluj", "poland", "warsaw", "portugal", "lisbon", "italy", "milan", "colombia",
    "bogota", "argentina", "philippines", "manila", "taiwan", "taipei", "hong kong"
)


def _get_notion_headers() -> Tuple[str, Dict[str, str]]:
    load_dotenv(ENV_PATH)
    token = (os.environ.get("NOTION_TOKEN") or os.environ.get("NOTION_API_KEY") or "").strip().strip('"').strip("'")
    job_db_id = (os.environ.get("NOTION_JOB_DB_ID") or "").strip().strip('"').strip("'")
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Notion-Version": NOTION_VERSION,
    }
    return job_db_id, headers


def _normalize_url_key(url: Optional[str]) -> str:
    """
    Normalizes a job URL to a canonical key for robust deduplication:
      - Strips protocol (http://, https://) and 'www.'
      - Strips query parameters (?utm_source=..., ?gh_jid=..., etc.)
      - Strips anchor fragments (#...)
      - Strips trailing slashes
    """
    if not url:
        return ""
    u = url.strip().lower()
    u = re.sub(r"^https?://(www\.)?", "", u)
    u = u.split("?")[0].split("#")[0]
    u = u.rstrip("/")
    return u


def _normalize_title_key(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", title.strip().lower())


def is_posting_fresh(date_str: Optional[Any], max_days: int = 21) -> bool:
    """
    Freshness Guardrail:
      - Checks the posting date string (ISO format or epoch timestamp).
      - Returns True if posted or updated within the last max_days (default 21 days).
      - Returns False if posted more than max_days ago.
    """
    if not date_str:
        return True
    try:
        if isinstance(date_str, (int, float)):
            if date_str > 1e11:
                dt = datetime.fromtimestamp(date_str / 1000.0)
            else:
                dt = datetime.fromtimestamp(date_str)
            return (datetime.now() - dt).days <= max_days
        clean_date = str(date_str).split("T")[0]
        dt = datetime.strptime(clean_date, "%Y-%m-%d")
        age_days = (datetime.now() - dt).days
        if age_days > max_days:
            return False
        return True
    except Exception:
        return True


def is_allowed_location(location_str: str, is_remote_flag: bool = False) -> Tuple[bool, bool, bool]:
    """
    Strict Location Filtering (Remote & Chicago Only):
      - Discard any role whose location is outside the United States.
      - Discard any role that is explicitly On-Site or Hybrid in non-Chicago cities (e.g. SF, NYC, Nashville).
      - Only accept roles explicitly labeled as 'Remote (US)', 'Remote - US', 'Chicago, IL',
        'Remote / Chicago', or 'Remote (North America)' / US Remote.
    Returns: (is_allowed, is_chicago, is_us_remote)
    """
    loc = (location_str or "").strip().lower()

    # 1. Non-US markers -> Immediate Reject
    for marker in NON_US_MARKERS:
        if re.search(r'\b' + re.escape(marker) + r'\b', loc):
            return False, False, False

    is_chicago = "chicago" in loc or "chicago, il" in loc or "greater chicago area" in loc

    # Check for accepted explicit remote labels
    accepted_remote_patterns = [
        "remote (us)", "remote - us", "remote, us", "remote (u.s.)", "remote - u.s.",
        "remote (united states)", "remote - united states", "remote, united states",
        "us remote", "u.s. remote", "united states (remote)", "us (remote)",
        "remote (north america)", "remote - north america", "north america (remote)",
        "remote / chicago", "chicago (remote)", "remote, il", "remote - il",
        "remote, usa", "remote (usa)", "remote - usa", "distributed (united states)"
    ]
    if any(p in loc for p in accepted_remote_patterns):
        return True, is_chicago, True

    # Chicago on-site, hybrid, or remote
    if is_chicago:
        return True, True, ("remote" in loc or is_remote_flag)

    # Non-Chicago US cities: if present and not matching explicit remote pattern, strictly discard
    non_chicago_cities = [
        "san francisco", "sf", "bay area", "new york", "nyc", "manhattan",
        "seattle", "austin", "boston", "los angeles", "la", "denver",
        "atlanta", "miami", "salt lake city", "washington dc", "dc", "palo alto",
        "mountain view", "sunnyvale", "redwood city", "san jose", "oakland",
        "nashville", "dallas", "houston", "raleigh", "durham", "charlotte",
        "san mateo", "santa clara", "cupertino", "menlo park", "burlingame",
        "irvine", "san diego", "philadelphia", "phoenix", "scottsdale"
    ]
    has_non_chicago_city = any(re.search(r'\b' + re.escape(c) + r'\b', loc) for c in non_chicago_cities)
    if has_non_chicago_city:
        return False, False, False

    # Standalone remote or distributed in US
    if is_remote_flag or loc in ("remote", "distributed", "anywhere"):
        return True, False, True

    return False, False, False


def resolve_final_url(url: str) -> str:
    """Follows redirects (including Google Grounding redirects) to extract the final live ATS or company URL."""
    if not url or not url.startswith(("http://", "https://")):
        return url
    if "vertexaisearch.cloud.google.com" in url or "google.com/url" in url:
        try:
            resp = requests.get(
                url,
                headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
                allow_redirects=True,
                timeout=10,
            )
            if resp.status_code == 200 and resp.url:
                return resp.url
        except Exception:
            pass
    return url


DISQUALIFIED_AGGREGATOR_DOMAINS = (
    "builtin.com",
    "builtinnyc.com",
    "builtinchicago.com",
    "builtinsf.com",
    "builtinla.com",
    "builtinaustin.com",
    "builtincolorado.com",
    "builtinboston.com",
    "builtinseattle.com",
    "join.com",
    "jobleads.com",
    "ziprecruiter.com",
    "indeed.com",
    "glassdoor.com",
    "simplyhired.com",
    "talent.com",
    "monster.com",
    "careerbuilder.com",
    "lensa.com",
    "geebo.com",
    "salary.com",
    "linkup.com",
    "snagajob.com",
    "dynamitejobs.com",
)


def verify_live_job_url(url: str) -> bool:
    """Verifies that a job posting URL returns HTTP 200, is not a secondary aggregator, and is not expired/404."""
    resolved = resolve_final_url(url)
    if not resolved or not resolved.startswith(("http://", "https://")):
        return False

    # Block secondary job board aggregators that host outdated caches or generic landing pages
    netloc = urlparse(resolved).netloc.lower()
    if any(agg in netloc for agg in DISQUALIFIED_AGGREGATOR_DOMAINS):
        return False

    try:
        resp = requests.get(
            resolved,
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
            timeout=10,
            allow_redirects=True,
        )
        if resp.status_code != 200:
            return False
        text_lower = resp.text[:6000].lower()
        dead_phrases = (
            "job is no longer available",
            "this job is no longer open",
            "position has been filled",
            "page not found",
            "404 not found",
            "no longer accepting applications",
            "job expired",
            "posting has closed",
            "this role has been closed",
            "this job has expired",
            "job has been removed",
            "job listing has ended",
        )
        if any(p in text_lower for p in dead_phrases):
            return False
        return True
    except Exception:
        return False


DISQUALIFIED_KEYWORDS = (
    "janitor", "clerk", "transit", "operator", "driver", "technician", "union",
    "police", "nurse", "paralegal", "custodian", "warehouse", "cashier", "ptc",
    "trainset", "conductor", "locomotive", "mechanic", "carpenter", "electrician",
    "security guard", "receptionist", "flight attendant", "cook", "food service",
    "maintenance", "laborer", "administrative assistant", "facilities", "dispatcher",
    "customer service representative", "call center", "telemarketer", "intern", "internship",
    "co-op", "fellow", "volunteer"
)


HIGH_SIGNAL_STACK_KEYWORDS = (
    "python", "typescript", "next.js", "llm", "gemini", "openai", "anthropic",
    "clay", "webhook", "api", "pydantic", "structured output", "notion",
    "salesforce", "hubspot", "apollo", "sql", "agent", "agentic", "evaluation",
    "meddicc", "challenger", "command of the message", "revops", "automation",
    "enrichment", "waterfall"
)

HEAVY_INFRA_PENALTY_KEYWORDS = (
    "8+ years", "10+ years", "12+ years", "c++", "rust", "cuda", "kernel",
    "kubernetes administrator", "embedded systems", "firmware", "fpga",
    "staff infrastructure engineer", "principal distributed systems"
)


def _apply_stage2_jd_rubric(base_score: int, jd_text: str) -> Optional[int]:
    """
    Stage 2 of the Two-Stage Scorer:
      Evaluates the JD body against Jae Carter's verified Python/TS/LLM/GTM stack
      and penalizes deep-systems / 8+ YoE requirements.
    """
    if not jd_text or len(jd_text.strip()) < 40:
        return base_score

    jd_lower = jd_text.lower()
    hits = sum(1 for kw in HIGH_SIGNAL_STACK_KEYWORDS if kw in jd_lower)
    penalties = sum(1 for bad in HEAVY_INFRA_PENALTY_KEYWORDS if bad in jd_lower)

    boost = min(5, hits // 2)
    penalty = penalties * 10
    final_score = max(0, min(99, base_score + boost - penalty))

    # Drop roles where deep-infra penalties pull fit below threshold
    if final_score < 72:
        return None
    return final_score


def classify_and_score_role(
    title: str,
    location: str,
    is_remote: bool,
    jd_text: str = "",
) -> Optional[Tuple[str, int]]:
    """
    Two-Stage Role Classifier & Scorer across 5 target tracks:
      1. GTM Engineer
      2. AI Revenue Architect / RevOps
      3. AI Operations / Technical RevOps
      4. Growth Engineer
      5. Founding / Outbound AE
    Stage 1: Title + Location heuristic & negative keyword blocklist.
    Stage 2: JD body stack & requirement rubric adjustment.
    """
    t = title.lower()

    # Strict Negative Keyword Blocklist
    if any(bad in t for bad in DISQUALIFIED_KEYWORDS):
        return None

    allowed, is_chicago, is_us_remote = is_allowed_location(location, is_remote)
    if not allowed:
        return None

    is_senior = any(s in t for s in ("senior", "sr", "lead", "staff", "principal", "founding", "head"))
    archetype: Optional[str] = None
    base_score: int = 0

    # Track 1: GTM Engineer (Highest Priority)
    if any(k in t for k in ("gtm engineer", "go-to-market engineer", "ai gtm", "gtm automation", "revenue engineer")):
        base_score = (98 if is_chicago else 95) if (is_senior or is_chicago) else 93
        archetype = "GTM Engineer"

    # Track 5 (New): AI Operations / Technical RevOps
    elif any(k in t for k in (
        "ai operations", "ai ops", "technical revops", "revops engineer",
        "gtm systems engineer", "business systems engineer", "revenue systems",
        "marketing operations engineer", "automation engineer"
    )):
        base_score = (95 if is_chicago else 93) if (is_senior or is_chicago) else 90
        archetype = "AI Operations / Technical RevOps"

    # Track 4: Growth Engineer (Technical / Code)
    elif "growth engineer" in t or "growth systems" in t:
        base_score = (94 if is_chicago else 91) if (is_senior or is_chicago) else 88
        archetype = "Growth Engineer"

    # Track 2: AI Revenue Architect / RevOps
    elif any(k in t for k in ("revenue architect", "gtm systems", "ai architect", "sales systems", "revenue intelligence")):
        base_score = (92 if is_chicago else 90) if (is_senior or is_chicago) else 86
        archetype = "AI Revenue Architect / RevOps"

    # Traditional Revenue Operations / Deal Desk
    elif any(k in t for k in ("revenue operations", "revops", "deal desk", "sales operations", "gtm operations")):
        jd_lower = (jd_text or "").lower()
        is_technical_revops = any(k in jd_lower for k in ("python", "sql", "clay", "webhook", "api", "llm")) or bool(re.search(r"\bai\b", jd_lower))
        if is_technical_revops:
            archetype = "AI Operations / Technical RevOps"
            base_score = 89 if is_senior else 85
        else:
            archetype = "AI Revenue Architect / RevOps"
            base_score = 82 if is_senior else 76

    # Track 3: Founding / Outbound AE
    elif "founding ae" in t or "founding account executive" in t or "technical account executive" in t or ("account executive" in t and ("systems" in t or bool(re.search(r"\bai\b", t)))):
        base_score = 85 if is_chicago else 80
        archetype = "Founding / Outbound AE"

    if not archetype:
        return None

    final_score = _apply_stage2_jd_rubric(base_score, jd_text)
    if final_score is None:
        return None

    return archetype, final_score


def _extract_salary_from_text(comp_summary: str, jd_text: str, archetype: str) -> str:
    if comp_summary:
        first_part = comp_summary.split("•")[0].strip()
        if "$" in first_part:
            has_equity = "equity" in comp_summary.lower()
            return f"{first_part} + Equity" if has_equity else first_part

    if jd_text:
        m = re.search(
            r"(\$[0-9]{2,3}(?:,[0-9]{3})?[kK]?\s*(?:[-–—]|to)\s*\$[0-9]{2,3}(?:,[0-9]{3})?[kK]?)",
            jd_text,
        )
        if m:
            return f"{m.group(1)} + Equity"

    defaults = {
        "GTM Engineer": "$175,000 – $220,000 + Equity (Benchmark)",
        "AI Revenue Architect / RevOps": "$165,000 – $210,000 + Equity (Benchmark)",
        "AI Operations / Technical RevOps": "$160,000 – $205,000 + Equity (Benchmark)",
        "Founding / Outbound AE": "$145,000 – $175,000 Base ($290k – $350k OTE) + Equity",
        "Growth Engineer": "$170,000 – $215,000 + Equity (Benchmark)",
    }
    return defaults.get(archetype, "$165,000 – $210,000 + Equity")


def _strip_html(raw_html: str) -> str:
    txt = html.unescape(raw_html or "")
    txt = re.sub(r"<(br|p|li|div|h[1-6])[^>]*>", "\n", txt, flags=re.IGNORECASE)
    txt = re.sub(r"<[^>]+>", "", txt)
    txt = re.sub(r"\n{3,}", "\n\n", txt)
    return txt.strip()


def generate_triage_brief(
    company_name: str,
    role_title: str,
    archetype: str,
    fit_score: int,
    jd_text: str,
) -> Dict[str, Any]:
    """
    Synthesizes:
      1. fit_justification: 1-2 sentence explanation of the Fit Score for the database property.
      2. role_summary: 2-3 sentence summary of the core mandate and stack.
      3. experience_transfer: 3 crisp bullet points mapping Jae Carter's background to this role.
    """
    load_dotenv(ENV_PATH)
    api_key = (os.environ.get("GEMINI_API_KEY") or "").strip().strip('"').strip("'")
    profile = load_master_profile()
    candidate = profile.get("candidate", {})

    fallback_justification = (
        f"Scores {fit_score}/100: Matches Jae Carter's {archetype} track combining enterprise outbound rigor "
        f"(Workday Federal / MEDDICC / Challenger) with production GTM systems engineering (Clay, Gemini API, Python daemons)."
    )
    fallback_summary = (
        f"{company_name} is hiring a {role_title} to scale enterprise revenue velocity. The role focuses on "
        f"driving full-cycle outbound execution, aligning technical capabilities with buyer pain points, and building repeatable pipeline."
    )
    fallback_transfer = [
        "Autonomous GTM & AI Systems: Experience architecting local Python webhook daemons, Clay waterfalls, and Gemini Structured Outputs maps directly to scaling high-intent account prospecting.",
        "Enterprise Sales Rigor & MEDDICC: Disciplined Challenger Sale commercial teaching and MEDDICC qualification honed at Workday Federal enable immediate multi-threading across VP/C-suite buyers.",
        "Market Intelligence & Research: AlphaSense primary research and expert survey triage experience equips Jae to uncover competitive triggers and executive 'Why Now' angles quickly.",
    ]

    fallback = {
        "fit_justification": fallback_justification,
        "role_summary": fallback_summary,
        "experience_transfer": fallback_transfer,
    }

    if not api_key:
        return fallback

    client = genai.Client(api_key=api_key)
    prompt = (
        f"Analyze this live job posting for candidate Jae Carter:\n\n"
        f"Company: {company_name}\n"
        f"Role: {role_title} ({archetype})\n"
        f"Fit Score: {fit_score}/100\n\n"
        f"Candidate Core Stack: Clay waterfalls, Python webhook daemons, Google Gemini API, Notion API Deal Desks, "
        f"Workday Federal enterprise outbound rigor, Challenger Sale, Command of the Message, MEDDICC qualification, AlphaSense research.\n\n"
        f"Job Description Snippet:\n{jd_text[:1800]}\n\n"
        "Use Google Search Grounding to find the company's current product ICP, recent funding round, and core role mandate.\n"
        "Return ONLY a valid JSON object with these exact keys:\n"
        "{\n"
        '  "fit_justification": "1-2 punchy sentences explaining why this role received this fit score based on specific stack/commercial overlap.",\n'
        '  "role_summary": "2-3 concise sentences summarizing what this role is being hired to build or execute.",\n'
        '  "experience_transfer": [\n'
        '    "Autonomous GTM & Systems: Concrete bullet showing how Jae\'s engineering stack applies.",\n'
        '    "Enterprise Outbound & Methodology: Concrete bullet showing how Workday Federal / MEDDICC / Challenger applies.",\n'
        '    "Domain & Strategic Alignment: Concrete bullet showing how Jae\'s background drives Day-1 impact."\n'
        "  ]\n"
        "}"
    )

    models_to_try = ["gemini-2.5-flash", "gemini-2.5-flash-lite"]
    for m in models_to_try:
        try:
            resp = client.models.generate_content(
                model=m,
                contents=prompt,
                config=types.GenerateContentConfig(
                    tools=[{"google_search": {}}],
                    temperature=0.2,
                ),
            )
            text = (resp.text or "").strip()
            match = re.search(r"\{.*\}", text, re.DOTALL)
            if match:
                data = json.loads(match.group(0))
                just = str(data.get("fit_justification") or "").strip()
                summary = str(data.get("role_summary") or "").strip()
                transfer = data.get("experience_transfer") or []
                if just and summary and isinstance(transfer, list) and len(transfer) >= 2:
                    return {
                        "fit_justification": just,
                        "role_summary": summary,
                        "experience_transfer": [str(x).strip() for x in transfer[:3]],
                    }
        except Exception as exc:
            print(f"[!] Warning in generate_triage_brief on {m}: {exc}")
            continue

    return fallback


def discover_live_jobs_via_gemini_search(max_candidates: int = 15) -> List[Dict[str, Any]]:
    """
    Uses Gemini with Google Search Grounding (tools=[{"google_search": {}}])
    to search high-signal tech & startup ATS/job domains:
      - site:aistartupjobs.com (Pre-IPO AI startups)
      - site:wellfound.com (Venture-backed startups)
      - site:boards.greenhouse.io OR site:job-boards.greenhouse.io
      - site:jobs.ashbyhq.com
      - site:jobs.lever.co
      - site:workatastartup.com
    Dynamically targets active roles posted in the last 7 days and filters out non-relevant listings.
    """
    load_dotenv(ENV_PATH)
    api_key = (os.environ.get("GEMINI_API_KEY") or "").strip().strip('"').strip("'")
    if not api_key:
        return []

    client = genai.Client(api_key=api_key)
    now = datetime.now()
    since_date = (now - timedelta(days=7)).strftime("%Y-%m-%d")
    today_str = now.strftime("%Y-%m-%d")

    prompt = (
        "Search Google for currently active, open tech & AI startup job postings on:\n"
        "- site:jobs.ashbyhq.com\n"
        "- site:boards.greenhouse.io OR site:job-boards.greenhouse.io\n"
        "- site:jobs.lever.co\n"
        "- site:aistartupjobs.com\n"
        "- site:workatastartup.com\n\n"
        "Target Keywords:\n"
        "('GTM Engineer' OR 'Go-To-Market Engineer' OR 'AI Operations' OR 'Technical RevOps' OR 'Revenue Systems' OR 'GTM Systems' OR 'Revenue Operations' OR 'RevOps' OR 'AI Architect' OR 'Founding AE' OR 'Growth Engineer')\n\n"
        "Mandatory Filters:\n"
        "- The job must be currently open (prefer roles posted recently) and located in the US (Remote US or Chicago, IL).\n"
        "- Filter out non-relevant listings (e.g. pure backend infrastructure, customer support, generic SDR, or international non-US roles).\n"
        "- CRITICAL: DO NOT return secondary job board aggregators (NO builtin.com, NO builtinnyc.com, NO join.com, NO jobleads.com, NO ziprecruiter.com, NO indeed.com, NO glassdoor.com).\n"
        "- Every job_url MUST be a DIRECT specific job posting with an application form (e.g. boards.greenhouse.io, jobs.ashbyhq.com, jobs.lever.co, or direct company career path company.com/careers/roles/*).\n"
        "- DO NOT return general careers directories or company homepages.\n"
        "- Return ONLY a valid JSON array (do not write any apologies or conversational prose). If few matches exist, return the best matching open roles you find in the JSON array:\n"
        "[\n"
        "  {\n"
        '    "company_name": "Company Name",\n'
        '    "role_title": "Exact Role Title",\n'
        '    "job_url": "https://... (direct link to the specific job posting)",\n'
        '    "company_url": "https://company.com",\n'
        '    "location": "Location (e.g. Remote, Chicago, New York)",\n'
        '    "salary_range": "Salary or OTE if listed (or empty string)",\n'
        '    "funding_stage": "Funding round/valuation if known (or empty string)",\n'
        '    "role_archetype": "GTM Engineer | AI Revenue Architect / RevOps | AI Operations / Technical RevOps | Founding / Outbound AE | Growth Engineer",\n'
        '    "job_description": "2-3 sentences summarizing the role mandate and responsibilities"\n'
        "  }\n"
        "]"
    )

    models_to_try = ["gemini-2.5-flash", "gemini-2.5-flash-lite"]
    discovered: List[Dict[str, Any]] = []

    for m in models_to_try:
        try:
            print(f"[*] [Live Web Search] Querying Gemini ({m}) with Google Search Grounding across target startup & ATS domains...", flush=True)
            resp = client.models.generate_content(
                model=m,
                contents=prompt,
                config=types.GenerateContentConfig(
                    tools=[{"google_search": {}}],
                    temperature=0.1,
                ),
            )
            text = (resp.text or "").strip()
            match = re.search(r"\[.*\]", text, re.DOTALL)
            if not match:
                print(f"[!] [Live Web Search] No JSON array detected from {m}. Preview: {text[:200]}", flush=True)
                continue

            try:
                items = json.loads(match.group(0))
            except Exception as e:
                print(f"[!] [Live Web Search] JSON parse error on {m}: {e}", flush=True)
                continue

            if not isinstance(items, list):
                continue

            print(f"[*] [Live Web Search] Gemini ({m}) returned {len(items)} candidates. Verifying live URLs...", flush=True)

            for item in items:
                comp = str(item.get("company_name") or "").strip()
                title = str(item.get("role_title") or "").strip()
                raw_url = str(item.get("job_url") or "").strip()
                loc = str(item.get("location") or "Remote").strip()
                if not comp or not title or not raw_url:
                    continue

                # Resolve Google Grounding redirect to real ATS URL
                j_url = resolve_final_url(raw_url)

                # Block secondary job board aggregators (builtin, join.com, jobleads, etc.)
                parsed_netloc = urlparse(j_url).netloc.lower()
                if any(agg in parsed_netloc for agg in DISQUALIFIED_AGGREGATOR_DOMAINS):
                    print(f"[-] [Aggregator Blocked] Discarding secondary aggregator URL: {j_url}", flush=True)
                    continue

                # Verify live URL returns HTTP 200
                if not verify_live_job_url(j_url):
                    print(f"[-] [Live Web Search] Skipping dead/unverified URL: {j_url}", flush=True)
                    continue

                is_remote = "remote" in loc.lower() or "distributed" in loc.lower()
                desc = str(item.get("job_description") or "").strip() or f"{title} at {comp}"
                classified = classify_and_score_role(title, loc, is_remote, jd_text=desc)
                if not classified:
                    print(f"[-] [Location/Rubric Filter] Discarding role: {comp} — {title} ({loc})", flush=True)
                    continue
                archetype, fit_score = classified

                salary = str(item.get("salary_range") or "").strip() or _extract_salary_from_text("", desc, archetype)
                funding = str(item.get("funding_stage") or "").strip() or "Venture-backed High-Growth AI/SaaS"
                comp_url = str(item.get("company_url") or "").strip() or f"https://www.{re.sub(r'[^a-z0-9]+', '', comp.lower())}.com"

                discovered.append({
                    "company_name": comp,
                    "role_title": f"{title} ({loc})" if loc and loc.lower() not in title.lower() else title,
                    "role_archetype": archetype,
                    "fit_score": fit_score,
                    "company_url": comp_url,
                    "job_url": j_url,
                    "funding_stage": funding,
                    "salary_range": salary,
                    "buyer_name_title": f"Head of Revenue / VP of Sales ({comp})",
                    "job_description": desc,
                })
                print(f"[+] [Live Web Search Found] {comp} — {title} (Fit: {fit_score}) | {j_url}", flush=True)

            if discovered:
                print(f"[+] [Live Web Search] Successfully found and verified {len(discovered)} live roles via Google Search Grounding.", flush=True)
                break
        except Exception as exc:
            print(f"[!] Warning during Gemini live search on {m}: {exc}", flush=True)
            continue

    return discovered


def extract_careers_domain(job_url: str, company_url: Optional[str] = None) -> Optional[str]:
    """
    Extracts the specific careers domain or ATS board path for secondary targeted searching.
    Examples:
      - 'https://careers.toasttab.com/jobs/123' -> 'careers.toasttab.com'
      - 'https://boards.greenhouse.io/gleanwork/jobs/456' -> 'boards.greenhouse.io/gleanwork'
      - 'https://jobs.ashbyhq.com/ramp/789' -> 'jobs.ashbyhq.com/ramp'
      - 'https://jobs.lever.co/palantir/abc' -> 'jobs.lever.co/palantir'
    """
    if not job_url:
        return None
    try:
        parsed = urlparse(job_url)
        netloc = parsed.netloc.lower()
        netloc = re.sub(r"^www\.", "", netloc)
        path = parsed.path.strip("/")
        parts = [p for p in path.split("/") if p]

        # Ignore secondary job aggregators right upfront
        if any(agg in netloc for agg in DISQUALIFIED_AGGREGATOR_DOMAINS):
            if company_url:
                c_parsed = urlparse(company_url)
                c_netloc = re.sub(r"^www\.", "", c_parsed.netloc.lower())
                if not any(agg in c_netloc for agg in DISQUALIFIED_AGGREGATOR_DOMAINS):
                    return c_netloc
            return None

        # Greenhouse board
        if "greenhouse.io" in netloc:
            if parts and parts[0] not in ("embed", "v1"):
                return f"boards.greenhouse.io/{parts[0]}"
            if "for=" in (parsed.query or ""):
                m = re.search(r"for=([a-zA-Z0-9_-]+)", parsed.query)
                if m:
                    return f"boards.greenhouse.io/{m.group(1)}"
            return "boards.greenhouse.io"

        # Ashby board
        if "ashbyhq.com" in netloc:
            if parts:
                return f"jobs.ashbyhq.com/{parts[0]}"
            return "jobs.ashbyhq.com"

        # Lever board
        if "lever.co" in netloc:
            if parts:
                return f"jobs.lever.co/{parts[0]}"
            return "jobs.lever.co"

        # Custom careers subdomain (e.g. careers.toasttab.com, jobs.company.com)
        if netloc.startswith("careers.") or netloc.startswith("jobs."):
            return netloc

        # If path starts with careers/ or jobs/
        if parts and parts[0] in ("careers", "jobs"):
            return f"{netloc}/{parts[0]}"

        # If company_url has careers subdomain
        if company_url:
            c_parsed = urlparse(company_url)
            c_netloc = re.sub(r"^www\.", "", c_parsed.netloc.lower())
            if c_netloc.startswith("careers.") or c_netloc.startswith("jobs."):
                return c_netloc

        return netloc or None
    except Exception:
        return None


def find_sibling_roles_via_gemini_search(
    company_name: str,
    careers_domain: str,
    primary_job_url: str,
    client: Optional[genai.Client],
) -> List[Dict[str, Any]]:
    """
    Executes secondary targeted check for a company's career portal:
    site:{careers_domain} ("GTM" OR "RevOps" OR "Revenue Intelligence" OR "Founding AE")
    """
    if not client:
        return []

    target_query = f'site:{careers_domain} ("GTM" OR "RevOps" OR "Revenue Intelligence" OR "Founding AE")'
    print(f"[*] [Secondary Targeted Check] Searching: {target_query}", flush=True)

    prompt = (
        f"Search Google for active job postings matching:\n"
        f"{target_query}\n\n"
        f"Target Company: {company_name}\n"
        f"Existing Primary Job URL to EXCLUDE: {primary_job_url}\n\n"
        "Find up to 2 distinct sibling job openings at this company matching our core tracks: "
        "('GTM Engineer', 'Revenue Operations' / 'RevOps', 'Revenue Intelligence', or 'Founding / Outbound AE').\n"
        "Requirements:\n"
        "- Do NOT return the primary job URL already listed above.\n"
        "- The job must be currently live and published within the last 14 days.\n"
        "- Must provide direct links to the individual job posting.\n\n"
        "Return ONLY a JSON array with these keys:\n"
        "[\n"
        "  {\n"
        '    "role_title": "Exact Role Title",\n'
        '    "job_url": "Direct job application URL",\n'
        '    "location": "Location (e.g. Remote, Chicago)",\n'
        '    "salary_range": "Salary or OTE if stated",\n'
        '    "role_archetype": "GTM Engineer | AI Revenue Architect / RevOps | Founding / Outbound AE | Growth Engineer",\n'
        '    "job_description": "2-3 sentences summarizing responsibilities"\n'
        "  }\n"
        "]\n"
        "If no other matching roles exist, return an empty array []."
    )

    models_to_try = ["gemini-2.5-flash", "gemini-2.5-flash-lite"]
    discovered_siblings: List[Dict[str, Any]] = []

    for m in models_to_try:
        try:
            resp = client.models.generate_content(
                model=m,
                contents=prompt,
                config=types.GenerateContentConfig(
                    tools=[{"google_search": {}}],
                    temperature=0.1,
                ),
            )
            text = (resp.text or "").strip()
            match = re.search(r"\[.*\]", text, re.DOTALL)
            if not match:
                continue

            try:
                items = json.loads(match.group(0))
            except Exception:
                continue

            if not isinstance(items, list):
                continue

            primary_norm_url = _normalize_url_key(primary_job_url)

            for item in items:
                title = str(item.get("role_title") or "").strip()
                raw_url = str(item.get("job_url") or "").strip()
                loc = str(item.get("location") or "Remote").strip()
                if not title or not raw_url:
                    continue

                j_url = resolve_final_url(raw_url)
                if _normalize_url_key(j_url) == primary_norm_url:
                    continue

                if not verify_live_job_url(j_url):
                    continue

                is_remote = "remote" in loc.lower() or "distributed" in loc.lower()
                classified = classify_and_score_role(title, loc, is_remote)
                if not classified:
                    continue
                archetype, fit_score = classified

                desc = str(item.get("job_description") or "").strip() or f"{title} at {company_name}"
                salary = str(item.get("salary_range") or "").strip() or _extract_salary_from_text("", "", archetype)

                discovered_siblings.append({
                    "company_name": company_name,
                    "role_title": f"{title} ({loc})" if loc and loc.lower() not in title.lower() else title,
                    "role_archetype": archetype,
                    "fit_score": fit_score,
                    "company_url": f"https://{careers_domain.split('/')[0]}",
                    "job_url": j_url,
                    "funding_stage": "High-Growth Enterprise Tech",
                    "salary_range": salary,
                    "buyer_name_title": f"Head of Revenue / VP of Sales ({company_name})",
                    "job_description": desc,
                })

            if discovered_siblings:
                break
        except Exception as exc:
            print(f"[!] Warning during secondary search on {m} for {careers_domain}: {exc}", flush=True)
            continue

    return discovered_siblings


def find_sibling_roles_for_company(
    company_name: str,
    careers_domain: str,
    primary_job_url: str,
    company_url: Optional[str],
    primary_item: Dict[str, Any],
    client: Optional[genai.Client],
) -> List[Dict[str, Any]]:
    """
    Discovers up to 1-2 sibling GTM roles at the same company:
    - If hosted on Ashby/Greenhouse/Lever: queries their API endpoints directly.
    - If custom domain (e.g. careers.toasttab.com): queries Google Search Grounding with
      site:{careers_domain} ("GTM" OR "RevOps" OR "Revenue Intelligence" OR "Founding AE").
    """
    primary_norm_url = _normalize_url_key(primary_job_url)
    siblings: List[Dict[str, Any]] = []

    # 1. Ashby Board
    if "jobs.ashbyhq.com" in careers_domain and "/" in careers_domain:
        slug = careers_domain.split("/")[1]
        try:
            resp = requests.get(
                f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true",
                timeout=8,
            )
            if resp.status_code == 200:
                for j in resp.json().get("jobs", []):
                    if not j.get("isListed", True):
                        continue
                    pub_at = j.get("publishedAt")
                    if not is_posting_fresh(pub_at, max_days=14):
                        continue
                    j_url = (j.get("jobUrl") or "").strip()
                    if not j_url or _normalize_url_key(j_url) == primary_norm_url:
                        continue
                    title = (j.get("title") or "").strip()
                    loc = (j.get("location") or "").strip()
                    is_remote = bool(j.get("isRemote"))
                    jd_plain = (j.get("descriptionPlain") or "").strip()
                    match = classify_and_score_role(title, loc, is_remote, jd_text=jd_plain)
                    if not match:
                        continue
                    archetype, fit_score = match
                    comp_summary = (
                        j.get("compensationTierSummary")
                        or (j.get("compensation") or {}).get("compensationTierSummary")
                        or ""
                    )
                    salary_str = _extract_salary_from_text(comp_summary, jd_plain, archetype)
                    loc_label = f"{loc} (Remote)" if (is_remote and "remote" not in loc.lower()) else loc
                    siblings.append({
                        "company_name": company_name,
                        "role_title": f"{title} ({loc_label})" if loc_label and loc_label.lower() not in title.lower() else title,
                        "role_archetype": archetype,
                        "fit_score": fit_score,
                        "company_url": company_url or primary_item.get("company_url"),
                        "job_url": j_url,
                        "funding_stage": primary_item.get("funding_stage", "High-Growth Enterprise Tech"),
                        "salary_range": salary_str,
                        "buyer_name_title": primary_item.get("buyer_name_title", ""),
                        "job_description": jd_plain,
                    })
                    if len(siblings) >= 1:
                        return siblings
        except Exception:
            pass

    # 2. Greenhouse Board
    elif "boards.greenhouse.io" in careers_domain and "/" in careers_domain:
        slug = careers_domain.split("/")[1]
        try:
            resp = requests.get(
                f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true",
                timeout=8,
            )
            if resp.status_code == 200:
                for j in resp.json().get("jobs", []):
                    upd_at = j.get("updated_at") or j.get("first_published")
                    if not is_posting_fresh(upd_at, max_days=14):
                        continue
                    j_url = (j.get("absolute_url") or "").strip()
                    if not j_url or _normalize_url_key(j_url) == primary_norm_url:
                        continue
                    title = (j.get("title") or "").strip()
                    loc = str((j.get("location") or {}).get("name") or "").strip()
                    is_remote = "remote" in loc.lower() or "distributed" in loc.lower()
                    jd_plain = _strip_html(j.get("content") or "")
                    match = classify_and_score_role(title, loc, is_remote, jd_text=jd_plain)
                    if not match:
                        continue
                    archetype, fit_score = match
                    salary_str = _extract_salary_from_text("", jd_plain, archetype)
                    siblings.append({
                        "company_name": company_name,
                        "role_title": f"{title} ({loc})" if loc and loc.lower() not in title.lower() else title,
                        "role_archetype": archetype,
                        "fit_score": fit_score,
                        "company_url": company_url or primary_item.get("company_url"),
                        "job_url": j_url,
                        "funding_stage": primary_item.get("funding_stage", "High-Growth Enterprise Tech"),
                        "salary_range": salary_str,
                        "buyer_name_title": primary_item.get("buyer_name_title", ""),
                        "job_description": jd_plain,
                    })
                    if len(siblings) >= 1:
                        return siblings
        except Exception:
            pass

    # 3. Lever Board
    elif "jobs.lever.co" in careers_domain and "/" in careers_domain:
        slug = careers_domain.split("/")[1]
        try:
            resp = requests.get(
                f"https://api.lever.co/v0/postings/{slug}?mode=json",
                timeout=8,
            )
            if resp.status_code == 200:
                for j in resp.json():
                    created_at = j.get("createdAt")
                    if not is_posting_fresh(created_at, max_days=14):
                        continue
                    j_url = (j.get("hostedUrl") or "").strip()
                    if not j_url or _normalize_url_key(j_url) == primary_norm_url:
                        continue
                    title = (j.get("text") or "").strip()
                    cats = j.get("categories") or {}
                    loc = str(cats.get("location") or "").strip()
                    wp_type = str(j.get("workplaceType") or "").lower()
                    is_remote = "remote" in loc.lower() or "remote" in wp_type or "distributed" in loc.lower()
                    jd_plain = (j.get("descriptionPlain") or "").strip()
                    match = classify_and_score_role(title, loc, is_remote, jd_text=jd_plain)
                    if not match:
                        continue
                    archetype, fit_score = match
                    salary_str = _extract_salary_from_text("", jd_plain, archetype)
                    siblings.append({
                        "company_name": company_name,
                        "role_title": f"{title} ({loc})" if loc and loc.lower() not in title.lower() else title,
                        "role_archetype": archetype,
                        "fit_score": fit_score,
                        "company_url": company_url or primary_item.get("company_url"),
                        "job_url": j_url,
                        "funding_stage": primary_item.get("funding_stage", "High-Growth Enterprise Tech"),
                        "salary_range": salary_str,
                        "buyer_name_title": primary_item.get("buyer_name_title", ""),
                        "job_description": jd_plain,
                    })
                    if len(siblings) >= 1:
                        return siblings
        except Exception:
            pass

    # 4. Custom careers domain via Gemini Grounding (e.g. site:careers.toasttab.com (...))
    is_standard_ats = any(ats in careers_domain for ats in ("ashbyhq.com", "greenhouse.io", "lever.co"))
    if not siblings and client and not is_standard_ats:
        siblings = find_sibling_roles_via_gemini_search(
            company_name=company_name,
            careers_domain=careers_domain,
            primary_job_url=primary_job_url,
            client=client,
        )

    return siblings


def discover_live_ats_jobs(max_candidates: int = 25, existing_urls: Optional[Set[str]] = None) -> List[Dict[str, Any]]:
    discovered: List[Dict[str, Any]] = []
    existing_url_set: Set[str] = existing_urls or set()

    # 1. Live Google Search Grounding via Gemini across Greenhouse, Ashby, Lever, YC
    try:
        live_search_jobs = discover_live_jobs_via_gemini_search(max_candidates=15)
        discovered.extend(live_search_jobs)
    except Exception as exc:
        print(f"[!] Warning during live web search discovery: {exc}", flush=True)

    # 2. Poll Ashby Public Posting API
    for comp_name, slug, comp_url, funding, buyer_hint in ASHBY_BOARDS:
        try:
            resp = requests.get(
                f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true",
                timeout=8,
            )
            if resp.status_code != 200:
                continue
            for j in resp.json().get("jobs", []):
                if not j.get("isListed", True):
                    continue
                # Freshness Guardrail: Last 21 days only
                pub_at = j.get("publishedAt")
                if not is_posting_fresh(pub_at, max_days=21):
                    continue
                title = (j.get("title") or "").strip()
                loc = (j.get("location") or "").strip()
                is_remote = bool(j.get("isRemote"))
                job_url = (j.get("jobUrl") or "").strip()
                if not title or not job_url:
                    continue

                jd_plain = (j.get("descriptionPlain") or "").strip()
                match = classify_and_score_role(title, loc, is_remote, jd_text=jd_plain)
                if not match:
                    continue
                archetype, fit_score = match

                comp_summary = (
                    j.get("compensationTierSummary")
                    or (j.get("compensation") or {}).get("compensationTierSummary")
                    or ""
                )
                salary_str = _extract_salary_from_text(comp_summary, jd_plain, archetype)
                loc_label = f"{loc} (Remote)" if (is_remote and "remote" not in loc.lower()) else loc

                discovered.append(
                    {
                        "company_name": comp_name,
                        "role_title": f"{title} ({loc_label})" if loc_label and loc_label.lower() not in title.lower() else title,
                        "role_archetype": archetype,
                        "fit_score": fit_score,
                        "company_url": comp_url,
                        "job_url": job_url,
                        "funding_stage": funding,
                        "salary_range": salary_str,
                        "buyer_name_title": buyer_hint,
                        "job_description": jd_plain,
                    }
                )
        except Exception:
            continue

    # 2. Poll Greenhouse Public Boards API
    for comp_name, slug, comp_url, funding, buyer_hint in GREENHOUSE_BOARDS:
        try:
            resp = requests.get(
                f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true",
                timeout=8,
            )
            if resp.status_code != 200:
                continue
            for j in resp.json().get("jobs", []):
                # Freshness Guardrail: Last 21 days only
                upd_at = j.get("updated_at") or j.get("first_published")
                if not is_posting_fresh(upd_at, max_days=21):
                    continue
                title = (j.get("title") or "").strip()
                loc = str((j.get("location") or {}).get("name") or "").strip()
                job_url = (j.get("absolute_url") or "").strip()
                if not title or not job_url:
                    continue

                is_remote = "remote" in loc.lower() or "distributed" in loc.lower()
                jd_plain = _strip_html(j.get("content") or "")
                match = classify_and_score_role(title, loc, is_remote, jd_text=jd_plain)
                if not match:
                    continue
                archetype, fit_score = match

                salary_str = _extract_salary_from_text("", jd_plain, archetype)

                discovered.append(
                    {
                        "company_name": comp_name,
                        "role_title": f"{title} ({loc})" if loc and loc.lower() not in title.lower() else title,
                        "role_archetype": archetype,
                        "fit_score": fit_score,
                        "company_url": comp_url,
                        "job_url": job_url,
                        "funding_stage": funding,
                        "salary_range": salary_str,
                        "buyer_name_title": buyer_hint,
                        "job_description": jd_plain,
                    }
                )
        except Exception:
            continue

    # 3. Targeted Sibling Check: Look for sibling GTM roles at discovered high-fit companies
    # Allow up to 2 distinct role listings per company domain
    company_primary_map: Dict[str, Dict[str, Any]] = {}
    for item in discovered:
        c_key = item["company_name"].lower()
        if c_key not in company_primary_map and item.get("fit_score", 0) >= 90:
            company_primary_map[c_key] = item

    client = None
    load_dotenv(ENV_PATH)
    api_key = (os.environ.get("GEMINI_API_KEY") or "").strip().strip('"').strip("'")
    if api_key:
        try:
            client = genai.Client(api_key=api_key)
        except Exception:
            pass

    for c_key, primary_item in list(company_primary_map.items())[:8]:
        existing_for_comp = [x for x in discovered if x["company_name"].lower() == c_key]
        if len(existing_for_comp) >= 2:
            continue

        comp_name = primary_item["company_name"]
        primary_url = primary_item.get("job_url", "")
        comp_url = primary_item.get("company_url", "")

        careers_domain = extract_careers_domain(primary_url, comp_url)
        if not careers_domain:
            continue

        try:
            siblings = find_sibling_roles_for_company(
                company_name=comp_name,
                careers_domain=careers_domain,
                primary_job_url=primary_url,
                company_url=comp_url,
                primary_item=primary_item,
                client=client,
            )
            for sib in siblings:
                sib_url_key = _normalize_url_key(sib.get("job_url"))
                if any(_normalize_url_key(x.get("job_url")) == sib_url_key for x in discovered):
                    continue
                print(f"[+] [Sibling Role Discovered] {comp_name} — {sib['role_title']} (Fit: {sib['fit_score']}) | {sib['job_url']}", flush=True)
                discovered.append(sib)
                break
        except Exception as exc:
            print(f"[!] Sibling discovery notice for {comp_name}: {exc}", flush=True)

    # Sort candidates by fit score
    discovered.sort(key=lambda x: x["fit_score"], reverse=True)

    # Diversity & Sibling Guardrail: Allow up to 2 distinct NET-NEW role listings per company domain
    diversified: List[Dict[str, Any]] = []
    company_counts: Dict[str, int] = {}
    seen_urls: Set[str] = set()

    for item in discovered:
        c_key = item["company_name"].lower()
        url_key = _normalize_url_key(item.get("job_url"))
        if not url_key or url_key in seen_urls or url_key in existing_url_set:
            continue

        if company_counts.get(c_key, 0) < 2:
            diversified.append(item)
            company_counts[c_key] = company_counts.get(c_key, 0) + 1
            seen_urls.add(url_key)

        if len(diversified) >= max_candidates:
            break

    return diversified


def fetch_existing_notion_dedup_sets(job_db_id: str, headers: Dict[str, str]) -> Tuple[Set[str], Set[str], Set[str]]:
    seen_urls: Set[str] = set()
    seen_titles: Set[str] = set()
    seen_company_roles: Set[str] = set()

    has_more = True
    start_cursor: Optional[str] = None

    while has_more:
        body: Dict[str, Any] = {"page_size": 100}
        if start_cursor:
            body["start_cursor"] = start_cursor

        resp = requests.post(
            f"https://api.notion.com/v1/databases/{job_db_id}/query",
            headers=headers,
            json=body,
            timeout=20,
        )
        if resp.status_code != 200:
            break

        data = resp.json()
        for page in data.get("results", []):
            props = page.get("properties", {})
            title_items = props.get("Company & Role", {}).get("title", [])
            title_str = "".join(t.get("plain_text", "") for t in title_items).strip()
            if title_str:
                norm_title = _normalize_title_key(title_str)
                seen_titles.add(norm_title)
                parts = re.split(r"[-—–:|]", title_str)
                if len(parts) >= 2:
                    comp_key = _normalize_title_key(parts[0])
                    # Strip common location tags like (Remote), (Distributed)
                    clean_role = re.sub(r"\(.*?\)", "", parts[1])
                    role_key = _normalize_title_key(clean_role)
                    seen_company_roles.add(f"{comp_key}::{role_key}")

            job_url = props.get("Job Posting URL", {}).get("url")
            if job_url:
                seen_urls.add(_normalize_url_key(job_url))

        has_more = bool(data.get("has_more"))
        start_cursor = data.get("next_cursor")

    return seen_urls, seen_titles, seen_company_roles


def _rt(content: str, bold: bool = False) -> Dict[str, Any]:
    return {
        "type": "text",
        "text": {"content": str(content)[:1990]},
        "annotations": {"bold": bold},
    }


def _bullet_block(bullet_text: str) -> Dict[str, Any]:
    """Renders a bulleted list item with bold prefix if colon is present."""
    if ":" in bullet_text:
        prefix, rest = bullet_text.split(":", 1)
        if len(prefix.strip()) <= 45:
            return {
                "object": "block",
                "type": "bulleted_list_item",
                "bulleted_list_item": {
                    "rich_text": [
                        _rt(f"{prefix.strip()}: ", bold=True),
                        _rt(rest.strip(), bold=False),
                    ]
                },
            }
    return {
        "object": "block",
        "type": "bulleted_list_item",
        "bulleted_list_item": {"rich_text": [_rt(bullet_text)]},
    }


def resolve_company_domain(company_name: str, company_url: Optional[str] = None) -> str:
    """Extracts or infers a clean domain for favicon resolution."""
    aggregator_domains = {
        "builtin.com", "join.com", "greenhouse.io", "ashbyhq.com",
        "lever.co", "workable.com", "jobleads.com", "linkedin.com",
        "indeed.com", "ziprecruiter.com", "glassdoor.com"
    }
    if company_url:
        netloc = urlparse(company_url).netloc.lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]
        if netloc and "." in netloc and not any(agg in netloc for agg in aggregator_domains):
            return netloc
    clean = re.sub(r"[^a-zA-Z0-9]", "", company_name.lower())
    special = {
        "elevenlabs": "elevenlabs.io",
        "sierra": "sierra.ai",
        "decagon": "decagon.ai",
        "baseten": "baseten.co",
        "harvey": "harvey.ai",
        "perplexity": "perplexity.ai",
        "pinecone": "pinecone.io",
        "weaviate": "weaviate.io",
        "tavus": "tavus.io",
        "temporal": "temporal.io",
        "cognition": "cognition.ai",
        "fireworks": "fireworks.ai",
        "snyk": "snyk.io",
        "linear": "linear.app",
        "modal": "modal.com",
        "cursor": "cursor.com",
        "supabase": "supabase.com",
        "drata": "drata.com",
        "deepgram": "deepgram.com",
        "apolloio": "apollo.io",
        "resend": "resend.com",
        "omnissa": "omnissa.com",
        "samsara": "samsara.com",
        "gitlab": "gitlab.com",
    }
    return special.get(clean, f"{clean}.com" if clean else "notion.so")


def create_triage_row_in_notion(
    job_item: Dict[str, Any],
    job_db_id: str,
    headers: Dict[str, str],
    dry_run: bool = False,
) -> Optional[str]:
    company_name = str(job_item.get("company_name") or "").strip()
    role_title = str(job_item.get("role_title") or "GTM Engineer").strip()
    job_url = str(job_item.get("job_url") or "").strip()
    if not company_name or not job_url:
        return None

    if not verify_live_job_url(job_url):
        print(f"[-] Skipping unverified/closed URL: {job_url}")
        return None

    archetype = str(job_item.get("role_archetype") or "GTM Engineer").replace(",", "").strip()
    fit_score = int(job_item.get("fit_score", 92))
    company_url = str(job_item.get("company_url") or "").strip() or None
    funding_stage = str(job_item.get("funding_stage") or "Venture-backed AI / Enterprise SaaS").strip()
    salary_range = str(job_item.get("salary_range") or "$165,000 – $210,000 + Equity").strip()
    buyer_hint = str(job_item.get("buyer_name_title") or "").strip()
    jd_text = str(job_item.get("job_description") or "").strip()

    # Generate concise summary + experience transfer brief + fit justification
    brief = generate_triage_brief(
        company_name=company_name,
        role_title=role_title,
        archetype=archetype,
        fit_score=fit_score,
        jd_text=jd_text,
    )

    card_title = f"{company_name} — {role_title}"
    domain = resolve_company_domain(company_name, company_url)
    logo_icon = {
        "type": "external",
        "external": {"url": f"https://unavatar.io/{domain}?fallback=https://www.google.com/s2/favicons?domain={domain}&sz=128"}
    }

    try:
        from scripts.contact_verifier import resolve_hiring_committee_contact
        resolved_contact = resolve_hiring_committee_contact(
            company_name=company_name,
            role_archetype=archetype,
            domain=domain,
            existing_buyer=buyer_hint,
        )
        if resolved_contact.get("buyer_email"):
            buyer_hint = f"{resolved_contact['buyer_display']} ({resolved_contact['buyer_email']})"
        elif resolved_contact.get("buyer_display"):
            buyer_hint = resolved_contact["buyer_display"]
    except Exception:
        pass

    buyer_linkedin = job_item.get("buyer_linkedin")
    if not buyer_linkedin:
        buyer_linkedin = generate_hiring_committee_search_url(company_name, archetype)

    props: Dict[str, Any] = {
        "Company & Role": {"title": [_rt(card_title)]},
        "Role Archetype": {"select": {"name": archetype}},
        "Stage": {"select": {"name": SCOUT_STAGE}},
        "Fit Score": {"number": fit_score},
        "Company URL": {"url": company_url},
        "Job Posting URL": {"url": job_url},
        "Buyer LinkedIn": {"url": buyer_linkedin},
        "Funding & Stage": {"rich_text": [_rt(funding_stage)]},
        "Salary Range": {"rich_text": [_rt(salary_range)]},
        "Fit Justification": {"rich_text": [_rt(brief["fit_justification"])]},
        "Top-Down Trigger": {"rich_text": [_rt(brief["role_summary"])]},
    }
    if buyer_hint:
        props["Economic Buyer"] = {"rich_text": [_rt(buyer_hint)]}

    transfer_blocks = [_bullet_block(b) for b in brief["experience_transfer"]]

    children: List[Dict[str, Any]] = [
        {
            "object": "block",
            "type": "callout",
            "callout": {
                "icon": logo_icon,
                "rich_text": [
                    _rt("[TRIAGE_GATE] Morning Scout Review\n", bold=True),
                    _rt(
                        "Action: Switch Stage to '0. Approved (Build Deal Room)' to trigger full Deal Room synthesis, "
                        "compile ATS resume PDF, and stage outbound drafts.\n"
                        "Or switch to '[DISQUALIFIED]' to archive."
                    ),
                ],
                "color": "gray_background",
            },
        },
        {
            "object": "block",
            "type": "heading_2",
            "heading_2": {"rich_text": [_rt("1. Role & Mandate Summary")]},
        },
        {
            "object": "block",
            "type": "callout",
            "callout": {
                "icon": logo_icon,
                "rich_text": [_rt(brief["role_summary"])],
                "color": "blue_background",
            },
        },
        {
            "object": "block",
            "type": "heading_2",
            "heading_2": {"rich_text": [_rt("2. How Your Experience Transfers")]},
        },
        *transfer_blocks,
    ]

    if dry_run:
        print(f"    [Dry-Run] {card_title} | Fit: {fit_score} | {brief['fit_justification']}")
        return job_url

    resp = requests.post(
        "https://api.notion.com/v1/pages",
        headers=headers,
        json={
            "parent": {"database_id": job_db_id},
            "icon": logo_icon,
            "properties": props,
            "children": children,
        },
        timeout=20,
    )
    if resp.status_code != 200:
        print(f"[!] Failed to create triage row for '{card_title}' ({resp.status_code}): {resp.text}")
        return None

    page_url = resp.json().get("url", "")
    print(f"[+] [Morning Scout | Triage Brief Added] '{card_title}' -> {page_url}")
    return page_url


def send_clean_macos_notification(title: str, message: str, subtitle: Optional[str] = None) -> None:
    """Fires a native background macOS notification banner via /usr/bin/osascript without opening Script Editor."""
    clean_title = title.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ").strip()
    clean_msg = message.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ").strip()
    script = f'display notification "{clean_msg}" with title "{clean_title}"'
    if subtitle:
        clean_sub = subtitle.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ").strip()
        script += f' subtitle "{clean_sub}"'
    script += ' sound name "default"'
    try:
        subprocess.run(
            ["/usr/bin/osascript", "-e", script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5,
        )
    except Exception:
        pass


def send_macos_notification(new_count: int) -> None:
    if new_count <= 0:
        msg = "Morning Scout complete: 0 new roles added (all discovered roles already in Notion)."
    else:
        msg = f"{new_count} verified live role(s) ready with Role Summaries & Experience Transfer in Notion."
    send_clean_macos_notification(
        title="[SCOUT] Morning GTM Job Scout",
        message=msg,
        subtitle="Notion Pipeline Updated",
    )


def run_morning_job_scout(max_new_jobs: int = 8, dry_run: bool = False) -> List[str]:
    lock_fd = acquire_scout_lock()
    if lock_fd is None:
        print("[*] [Morning Scout] Scout execution already in progress by another process. Skipping to avoid duplicate cards.")
        return []

    try:
        job_db_id, headers = _get_notion_headers()
        if not job_db_id:
            raise RuntimeError("Missing NOTION_JOB_DB_ID in .env")

        seen_urls, seen_titles, seen_company_roles = fetch_existing_notion_dedup_sets(job_db_id, headers)
        print(
            f"[*] [Morning Scout] Loaded deduplication index from Notion: "
            f"{len(seen_titles)} existing titles, {len(seen_urls)} existing URLs, {len(seen_company_roles)} unique company roles."
        )

        candidates = discover_live_ats_jobs(max_candidates=25, existing_urls=seen_urls)
        print(f"[*] [Morning Scout] Found {len(candidates)} net-new live ATS openings across Ashby, Greenhouse & Web Search.")

        created_urls: List[str] = []
        company_scout_counts: Dict[str, int] = {}
        for item in candidates:
            if len(created_urls) >= max_new_jobs:
                break

            comp = str(item.get("company_name") or "").strip()
            role = str(item.get("role_title") or "").strip()
            card_title = f"{comp} — {role}"
            title_key = _normalize_title_key(card_title)
            url_key = _normalize_url_key(item.get("job_url"))
            c_key = comp.lower()

            # Clean role key stripping location
            clean_role = re.sub(r"\(.*?\)", "", role)
            company_role_key = f"{_normalize_title_key(comp)}::{_normalize_title_key(clean_role)}"

            if company_scout_counts.get(c_key, 0) >= 2:
                print(f"[-] [Company Cap] Already accepted 2 roles for {comp}, skipping: {card_title}", flush=True)
                continue

            if url_key and url_key in seen_urls:
                print(f"[-] [Deduplicated URL] Skipping existing role: {card_title} ({item.get('job_url')})", flush=True)
                continue
            if title_key in seen_titles:
                print(f"[-] [Deduplicated Title] Skipping existing title: {card_title}", flush=True)
                continue
            if company_role_key in seen_company_roles:
                print(f"[-] [Deduplicated Company Role] Skipping matching role: {card_title}", flush=True)
                continue

            page_url = create_triage_row_in_notion(item, job_db_id, headers, dry_run=dry_run)
            if page_url:
                created_urls.append(page_url)
                seen_titles.add(title_key)
                seen_company_roles.add(company_role_key)
                company_scout_counts[c_key] = company_scout_counts.get(c_key, 0) + 1
                if url_key:
                    seen_urls.add(url_key)

        send_macos_notification(len(created_urls))
        return created_urls
    finally:
        release_scout_lock(lock_fd)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Daily 7:00 AM Autonomous GTM Job Scout (Triage Briefs)")
    parser.add_argument("--max", type=int, default=8, help="Max new triage roles to insert per run (default: 8)")
    parser.add_argument("--dry-run", action="store_true", help="Preview discovered roles without writing to Notion")
    args = parser.parse_args()

    urls = run_morning_job_scout(max_new_jobs=min(10, args.max), dry_run=args.dry_run)
    print(f"[+] Morning Job Scout finished. Created {len(urls)} verified live triage row(s).")
