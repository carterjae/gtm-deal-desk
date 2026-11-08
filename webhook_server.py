#!/usr/bin/env python3
"""
Unified Clay Webhook Receiver, 1-Click Apple Shortcut Clipper, & 0-Credit Notion Inbox Poller Daemon (Port 8000):
  - POST /webhook/clay-ingest (and /webhook/clay) -> Mode A: Enterprise GTM Deal Desk
  - POST /webhook/job-ingest                      -> Mode B: Autonomous Job Search & Work-Trial Engine
  - POST /webhook/quick-clip                      -> 1-Click Safari / iPhone Apple Shortcut Job Clipper
  - Background Thread (every 60s)                 -> Polls NOTION_JOB_DB_ID for Stage == '0. Raw Inbox'
"""

import json
import os
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import requests
from dotenv import load_dotenv
from pydantic import ValidationError

PROJECT_ROOT = Path(__file__).resolve().parent
os.chdir(PROJECT_ROOT)
load_dotenv(PROJECT_ROOT / ".env")

from schema import ClayInputData

try:
    from job_engine import ClayJobInput, process_job_ingest
except ImportError:
    ClayJobInput = None
    process_job_ingest = None

try:
    from daemon_worker import poll_raw_inbox_once, start_background_poller_thread
except ImportError:
    poll_raw_inbox_once = None
    start_background_poller_thread = None

try:
    from scripts.spar_engine import SparSessionManager, list_active_targets, SUPPORTED_NEURAL_VOICES, generate_neural_audio_wav
except ImportError:
    SparSessionManager = None
    list_active_targets = None
    SUPPORTED_NEURAL_VOICES = []
    generate_neural_audio_wav = None
import base64


HOST = "0.0.0.0"
PORT = 8000
CLAY_PATHS = ("/webhook/clay-ingest", "/webhook/clay")
JOB_INGEST_PATH = "/webhook/job-ingest"
QUICK_CLIP_PATH = "/webhook/quick-clip"
GTM_AUDIT_PATH = "/api/gtm-audit"
SPAR_PATH = "/spar"
SPAR_API_TARGETS = "/api/spar/targets"
SPAR_API_VOICES = "/api/spar/voices"
SPAR_API_START = "/api/spar/start"
SPAR_API_TURN = "/api/spar/turn"
SPAR_API_SYNC = "/api/spar/sync"
SPAR_API_END_CALL = "/api/spar/end-call"
SPAR_API_SPEAK = "/api/spar/speak"
ALL_PATHS = (
    *CLAY_PATHS,
    JOB_INGEST_PATH,
    QUICK_CLIP_PATH,
    GTM_AUDIT_PATH,
    SPAR_PATH,
    SPAR_API_TARGETS,
    SPAR_API_VOICES,
    SPAR_API_START,
    SPAR_API_TURN,
    SPAR_API_SYNC,
    SPAR_API_END_CALL,
    SPAR_API_SPEAK,
)
STAGING_DIR = PROJECT_ROOT / "staging"
JOB_STAGING_DIR = STAGING_DIR / "jobs"
RUN_SCRIPT = PROJECT_ROOT / "run.py"
JOB_ENGINE_SCRIPT = PROJECT_ROOT / "job_engine.py"

# Rate Limiting State for Public /api/gtm-audit
AUDIT_IP_REQUESTS: Dict[str, List[float]] = {}
AUDIT_IP_GENERATIONS: Dict[str, List[float]] = {}
AUDIT_GLOBAL_GENERATIONS: List[float] = []

MAX_GENERATIONS_PER_IP_24H = 2
MAX_LOOKUPS_PER_IP_1H = 10
MAX_GLOBAL_GENERATIONS_24H = 20

FREEMAIL_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com", "icloud.com",
    "mail.com", "zoho.com", "protonmail.com", "proton.me", "live.com", "msn.com",
    "yandex.com", "gmx.com"
}

PRECACHED_BLUEPRINTS: Dict[str, Dict[str, Any]] = {
    "vercel": {
        "company_name": "Vercel",
        "domain": "vercel.com",
        "url": "https://married-spider-c74.notion.site/40c42e9ec1d08120bd9fc46d07b3c108",
        "accounts": ["Perplexity", "Cursor", "ElevenLabs"],
        "thesis": "Streaming token latency & sub-350ms ElevenLabs voice routing over Vercel Edge Network."
    },
    "vanta": {
        "company_name": "Vanta",
        "domain": "vanta.com",
        "url": "https://married-spider-c74.notion.site/40842e9ec1d0815784fdd63c46754437",
        "accounts": ["High-Growth SaaS", "Drata Displacements", "Secureframe Audits"],
        "thesis": "Eliminating the 40-day infosec review tax with automated continuous compliance Trust Centers."
    },
    "langchain": {
        "company_name": "LangChain",
        "domain": "langchain.com",
        "url": "https://married-spider-c74.notion.site/40c42e9ec1d081e094afca13800954cd",
        "accounts": ["Anthropic", "Scale AI", "Cohere"],
        "thesis": "LangSmith step-level trace latency, non-deterministic agent eval regression, and air-gapped VPCs."
    },
    "clay": {
        "company_name": "Clay",
        "domain": "clay.com",
        "url": "https://married-spider-c74.notion.site/40c42e9ec1d0815eba00ea7987e2bd72",
        "accounts": ["Ramp", "Notion", "Figma"],
        "thesis": "Tier-0 POSIX DNS pre-filtering ($0.00 gate) paired with idempotent CRM webhooks."
    },
    "sierra": {
        "company_name": "Sierra",
        "domain": "sierra.ai",
        "url": "https://married-spider-c74.notion.site/40c42e9ec1d081eab95be145f2e8b199",
        "accounts": ["Brex", "Chime", "WeightWatchers"],
        "thesis": "Deterministic transactional action execution and sub-300ms turn-taking voice agents."
    },
    "cursor": {
        "company_name": "Cursor",
        "domain": "cursor.com",
        "url": "https://married-spider-c74.notion.site/40c42e9ec1d081da839df96ea1219a11",
        "accounts": ["Stripe", "Datadog", "Figma"],
        "thesis": "Full-monorepo semantic indexing & developer velocity economics ($8.2M/yr capacity drain)."
    },
    "glean": {
        "company_name": "Glean",
        "domain": "glean.com",
        "url": "https://married-spider-c74.notion.site/40c42e9ec1d0818cb192c65552c8acb2",
        "accounts": ["Databricks", "Snowflake", "Workday"],
        "thesis": "Zero-trust ACL enterprise search across 50+ SaaS tools eliminating $7.4M/yr knowledge sprawl."
    },
    "sentinelone": {
        "company_name": "SentinelOne",
        "domain": "sentinelone.com",
        "url": "https://married-spider-c74.notion.site/40c42e9ec1d08134be68e692ea084139",
        "accounts": ["Enterprise Security", "CrowdStrike Migrations", "Legacy Symantec"],
        "thesis": "Purple AI value mapping and automated legacy AV footprint detection."
    },
    "scale": {
        "company_name": "Scale AI",
        "domain": "scale.com",
        "url": "https://married-spider-c74.notion.site/40c42e9ec1d08157aac2fa692d1afeaf",
        "accounts": ["Morgan Stanley", "Toyota", "Meta"],
        "thesis": "Automated production trace ingestion into Scale Data Engine for enterprise RLHF queues."
    },
    "modal": {
        "company_name": "Modal",
        "domain": "modal.com",
        "url": "https://married-spider-c74.notion.site/40c42e9ec1d081d385bcead6362a0ddd",
        "accounts": ["Substack", "Ramp", "Suno"],
        "thesis": "Serverless GPU container cold-start economics eliminating $1.8M/yr over-provisioning waste."
    },
    "elevenlabs": {
        "company_name": "ElevenLabs",
        "domain": "elevenlabs.io",
        "url": "https://married-spider-c74.notion.site/40c42e9ec1d081979112d6d8071a77b3",
        "accounts": ["Klarna", "Square", "Duolingo"],
        "thesis": "Sub-300ms turn-taking voice latency and low-latency edge audio streaming."
    },
    "perplexity": {
        "company_name": "Perplexity",
        "domain": "perplexity.ai",
        "url": "https://married-spider-c74.notion.site/40c42e9ec1d08127a4c4ff762e907e66",
        "accounts": ["Bridgewater", "Kirkland & Ellis", "Stripe"],
        "thesis": "Enterprise Pro search with verifiable source grounding and zero-data-retention compliance."
    },
    "databricks": {
        "company_name": "Databricks",
        "domain": "databricks.com",
        "url": "https://married-spider-c74.notion.site/40c42e9ec1d081c5930ff1f7cf8ad8c1",
        "accounts": ["Capital One", "AT&T", "Regeneron"],
        "thesis": "Lakehouse unified RAG indexing, Unity Catalog security gates, and Mosaic AI serving."
    },
    "linear": {
        "company_name": "Linear",
        "domain": "linear.app",
        "url": "https://married-spider-c74.notion.site/40c42e9ec1d081ea9d2adef912897edb",
        "accounts": ["OpenAI", "Ramp", "Vercel"],
        "thesis": "Real-time sync engine, git-branch issue linking, and eliminating 25% sprint administration drag."
    },
    "braintrust": {
        "company_name": "Braintrust",
        "domain": "usebraintrust.com",
        "url": "https://married-spider-c74.notion.site/40c42e9ec1d081d397a0e869d7946dd9",
        "accounts": ["Notion", "Zapier", "Airtable"],
        "thesis": "Enterprise LLM evaluation CI/CD assertion gates blocking silent prompt regressions."
    },
}


def is_freemail(email: str) -> bool:
    if not email or "@" not in email:
        return False
    dom = email.split("@")[-1].lower().strip()
    return dom in FREEMAIL_DOMAINS


def trigger_macos_audit_alert(company: str, email: str, ip: str, cached: bool) -> None:
    """Fires native macOS desktop notification when an external visitor queries an audit."""
    status_tag = "Cached Brief ($0 compute)" if cached else "Net-New Blueprint Generated"
    lead_id = email.strip() if email else f"IP: {ip}"
    msg = f"{lead_id} requested {company} GTM audit [{status_tag}]"
    script = f'display notification "{msg}" with title "[INBOUND LEAD] GTM Architecture Request" sound name "Glass"'
    try:
        subprocess.run(["osascript", "-e", script], check=False)
    except Exception:
        pass


def handle_gtm_audit(payload: Dict[str, Any], client_ip: str) -> Tuple[int, Dict[str, Any]]:
    """
    On-Demand Public GTM Architecture Audit Endpoint (/api/gtm-audit).
    Enforces work-email verification, 3-layer rate limiting, cache-first $0 retrieval,
    and real-time desktop alerts on macOS.
    """
    company_name = (payload.get("company_name") or payload.get("company") or "").strip()
    domain = (payload.get("domain") or "").strip()
    work_email = (payload.get("work_email") or payload.get("email") or "").strip()

    if not company_name and not domain:
        return 400, {
            "status": "error",
            "error": "Missing required field: please provide 'company_name' or 'domain' (e.g. 'runway.com')."
        }

    # Validate work email if passed
    if work_email:
        if not re.match(r"[^@]+@[^@]+\.[^@]+", work_email):
            return 400, {"status": "error", "error": "Invalid email address format."}
        if is_freemail(work_email):
            return 400, {
                "status": "error",
                "error": "Corporate work email (@company.com) required. Personal webmail (Gmail, Yahoo, iCloud, etc.) is restricted."
            }

    # Clean lookup key
    lookup_target = (company_name or domain).lower()
    clean_key = re.sub(r"[^a-z0-9]", "", lookup_target)

    # Check pre-cached blueprints (0-token response in <50ms)
    cached_match = None
    for k, v in PRECACHED_BLUEPRINTS.items():
        if k in clean_key or (domain and domain.lower() in v["domain"].lower()):
            cached_match = v
            break

    is_cached = cached_match is not None
    now = time.time()
    day_ago = now - 86400
    hour_ago = now - 3600

    # Rate limiting book-keeping
    AUDIT_IP_REQUESTS[client_ip] = [t for t in AUDIT_IP_REQUESTS.get(client_ip, []) if t > hour_ago]
    AUDIT_IP_GENERATIONS[client_ip] = [t for t in AUDIT_IP_GENERATIONS.get(client_ip, []) if t > day_ago]
    global AUDIT_GLOBAL_GENERATIONS
    AUDIT_GLOBAL_GENERATIONS = [t for t in AUDIT_GLOBAL_GENERATIONS if t > day_ago]

    # Enforce lookup limit per IP (10 / hr)
    if len(AUDIT_IP_REQUESTS[client_ip]) >= MAX_LOOKUPS_PER_IP_1H:
        return 429, {
            "status": "rate_limited",
            "error": "Hourly query quota reached (max 10 queries/hour). Book a direct architecture walkthrough with Jae at: https://calendar.notion.so/meet/jaecarter/mc5qt4yuy",
            "retry_after_minutes": 60
        }

    AUDIT_IP_REQUESTS[client_ip].append(now)

    # Run real-time protocol forensics ($0 local DNS/MX/DMARC probe)
    target_dom = domain or f"{re.sub(r'[^a-z0-9]', '', (company_name or 'target').lower())}.com"
    telemetry_summary = {}
    try:
        from scripts.telemetry_inspector import inspect_domain_telemetry
        report = inspect_domain_telemetry(target_dom, company_name=company_name)
        telemetry_summary = {
            "probed_domain": report.domain,
            "mx_provider": report.primary_mx_provider,
            "dmarc_policy": report.dmarc.policy,
            "dmarc_enforced": report.dmarc.is_enforced,
            "dmarc_risk": report.dmarc.deliverability_risk,
            "spf_includes_count": report.spf.lookup_count,
            "edge_network": report.http.edge_network,
            "edge_server": report.http.server,
            "calculated_annual_drag_usd": report.coi_model.annual_drag_usd,
            "matched_peer_case_study": {
                "benchmark_company": report.case_study.benchmark_company,
                "cohort": report.case_study.target_cohort,
                "outcome": report.case_study.positive_business_outcome,
                "citation": report.case_study.source_citation,
            }
        }
    except Exception:
        pass

    if is_cached and cached_match:
        trigger_macos_audit_alert(cached_match["company_name"], work_email, client_ip, cached=True)
        return 200, {
            "status": "success",
            "cached": True,
            "company_name": cached_match["company_name"],
            "deal_room_url": cached_match["url"],
            "target_accounts": cached_match["accounts"],
            "executive_thesis": cached_match["thesis"],
            "telemetry_forensics": telemetry_summary,
            "message": "Pre-computed enterprise GTM systems architecture brief retrieved in 28ms ($0 compute cost).",
            "calendar_url": "https://calendar.notion.so/meet/jaecarter/mc5qt4yuy",
            "github_repo": "https://github.com/carterjae"
        }

    # Net-new generation rate limits
    if len(AUDIT_IP_GENERATIONS[client_ip]) >= MAX_GENERATIONS_PER_IP_24H:
        return 429, {
            "status": "rate_limited",
            "error": "Daily on-demand generation quota reached (max 2 net-new audits per 24 hours per IP). Book a direct architecture walkthrough with Jae at: https://calendar.notion.so/meet/jaecarter/mc5qt4yuy",
            "retry_after_hours": 24
        }
    if len(AUDIT_GLOBAL_GENERATIONS) >= MAX_GLOBAL_GENERATIONS_24H:
        return 429, {
            "status": "rate_limited",
            "error": "Global daily generation limit reached. Check back tomorrow or schedule a direct walkthrough at: https://calendar.notion.so/meet/jaecarter/mc5qt4yuy",
            "retry_after_hours": 24
        }

    AUDIT_IP_GENERATIONS[client_ip].append(now)
    AUDIT_GLOBAL_GENERATIONS.append(now)

    target_name = company_name or domain.split(".")[0].capitalize()

    try:
        from scripts.generate_blitz import generate_gtme_blueprint_file
        blueprint_path = generate_gtme_blueprint_file(target_name, role_title="GTM Engineer", force_offline=False)
        trigger_macos_audit_alert(target_name, work_email, client_ip, cached=False)
        
        deal_room_url = f"https://jae-carter.vercel.app/?company={target_name}"
        return 200, {
            "status": "success",
            "cached": False,
            "company_name": target_name,
            "deal_room_url": deal_room_url,
            "blueprint_file": str(blueprint_path),
            "message": f"Synthesized bespoke 6-section GTM systems architecture for {target_name}.",
            "calendar_url": "https://calendar.notion.so/meet/jaecarter/mc5qt4yuy",
            "github_repo": "https://github.com/carterjae"
        }
    except Exception as exc:
        return 500, {
            "status": "error",
            "error": f"Failed synthesizing blueprint for {target_name}: {str(exc)}"
        }


def slugify_company_name(company_name: str) -> str:
    """Sanitizes a company name into a safe lowercase filename slug."""
    slug = re.sub(r"[^a-z0-9]+", "_", company_name.strip().lower()).strip("_")
    return slug or "unnamed_target"


def stage_and_trigger(payload: Dict[str, Any], dry_run: bool = False) -> Dict[str, Any]:
    """Validates incoming Clay JSON payload, writes to staging/<company_slug>.json, and spawns run.py in the background."""
    validated = ClayInputData.model_validate(payload)
    company_slug = slugify_company_name(validated.company_name)

    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    staged_file_path = STAGING_DIR / f"{company_slug}.json"

    staged_payload = {
        "company_name": validated.company_name,
        "github_org": validated.github_org,
        "has_job_post": validated.has_job_post,
        "ai_engineer_count": validated.ai_engineer_count,
    }
    for k, v in payload.items():
        if k not in staged_payload and v is not None:
            staged_payload[k] = v

    staged_file_path.write_text(json.dumps(staged_payload, indent=2), encoding="utf-8")

    cmd = [sys.executable, str(RUN_SCRIPT), str(staged_file_path)]
    if dry_run:
        cmd.append("--dry-run")

    proc = subprocess.Popen(
        cmd,
        stdout=sys.stdout,
        stderr=sys.stderr,
        start_new_session=True,
    )

    print(
        f"[+] Staged '{validated.company_name}' -> {staged_file_path} | "
        f"Triggered background pipeline (PID: {proc.pid})",
        flush=True,
    )
    return {
        "status": "accepted",
        "company_name": validated.company_name,
        "staged_file": str(staged_file_path),
        "pid": proc.pid,
    }


def stage_and_ingest_job(
    payload: Dict[str, Any],
    dry_run: bool = False,
    run_async: bool = False,
) -> Dict[str, Any]:
    """Validates incoming Clay job-enrichment JSON payload, stages to staging/jobs/, and runs job_engine.py."""
    validated = ClayJobInput.model_validate(payload)
    company_slug = slugify_company_name(validated.company_name)
    role_slug = slugify_company_name(validated.role_title)

    JOB_STAGING_DIR.mkdir(parents=True, exist_ok=True)
    staged_file_path = JOB_STAGING_DIR / f"{company_slug}_{role_slug}.json"
    staged_file_path.write_text(
        json.dumps(validated.model_dump(), indent=2),
        encoding="utf-8",
    )

    if run_async:
        cmd = [sys.executable, str(JOB_ENGINE_SCRIPT), str(staged_file_path)]
        if dry_run:
            cmd.append("--dry-run")
        proc = subprocess.Popen(
            cmd,
            stdout=sys.stdout,
            stderr=sys.stderr,
            start_new_session=True,
        )
        print(
            f"[+] Staged Job '{validated.company_name} — {validated.role_title}' -> {staged_file_path} | "
            f"Triggered background job_engine.py (PID: {proc.pid})",
            flush=True,
        )
        return {
            "status": "accepted",
            "company_name": validated.company_name,
            "role_title": validated.role_title,
            "staged_file": str(staged_file_path),
            "pid": proc.pid,
        }

    print(f"[*] Processing Job Ingest for '{validated.company_name} — {validated.role_title}'...", flush=True)
    result = process_job_ingest(payload, dry_run=dry_run)
    result["staged_file"] = str(staged_file_path)
    return result


def handle_quick_clip(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    1-Click Apple Shortcut / Safari Web Clipper ingestion endpoint (/webhook/quick-clip).
    Accepts any lightweight JSON (e.g. {"url": "...", "title": "...", "text": "..."}),
    creates a '0. Raw Inbox' row in NOTION_JOB_DB_ID, and triggers async 0-credit enrichment.
    """
    load_dotenv(PROJECT_ROOT / ".env")
    token = (os.environ.get("NOTION_TOKEN") or os.environ.get("NOTION_API_KEY") or "").strip().strip('"').strip("'")
    job_db_id = (os.environ.get("NOTION_JOB_DB_ID") or "").strip().strip('"').strip("'")

    job_url = (
        payload.get("job_url")
        or payload.get("url")
        or payload.get("link")
        or ""
    ).strip()
    company_name = (payload.get("company_name") or payload.get("company") or "").strip()
    role_title = (payload.get("role_title") or payload.get("role") or "").strip()
    raw_title = (payload.get("title") or "").strip()
    raw_text = (
        payload.get("job_description")
        or payload.get("text")
        or payload.get("body")
        or payload.get("selection")
        or ""
    ).strip()

    if company_name and role_title:
        card_title = f"{company_name} — {role_title}"
    elif raw_title:
        card_title = raw_title
    elif company_name:
        card_title = f"{company_name} — GTM Engineer"
    else:
        card_title = "Quick-Clipped Job — GTM Engineer"

    props: Dict[str, Any] = {
        "Company & Role": {"title": [{"text": {"content": card_title[:1990]}}]},
        "Stage": {"select": {"name": "0. Raw Inbox"}},
    }
    if job_url and job_url.startswith(("http://", "https://")):
        props["Job Posting URL"] = {"url": job_url}

    children = []
    if raw_text:
        children.append(
            {
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [{"type": "text", "text": {"content": raw_text[:1990]}}]
                },
            }
        )

    resp = requests.post(
        "https://api.notion.com/v1/pages",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Notion-Version": "2022-06-28",
        },
        json={"parent": {"database_id": job_db_id}, "properties": props, "children": children},
        timeout=20,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Quick-clip Notion creation failed ({resp.status_code}): {resp.text}")

    page_data = resp.json()
    page_id = page_data.get("id")
    page_url = page_data.get("url")

    # Trigger immediate background enrichment pass so the user gets an instant 200 OK response
    threading.Thread(target=poll_raw_inbox_once, daemon=True).start()

    print(f"[+] [Quick-Clip] Created '0. Raw Inbox' row '{card_title}' -> {page_url} (enrichment started)", flush=True)
    return {
        "status": "clipped",
        "title": card_title,
        "notion_page_id": page_id,
        "notion_url": page_url,
        "message": "Created '0. Raw Inbox' card and triggered background 0-credit enrichment + PDF upload.",
    }


class ClayWebhookHandler(BaseHTTPRequestHandler):
    """HTTP request handler for POST /webhook/clay-ingest, /webhook/job-ingest, and /webhook/quick-clip."""

    def _send_json(self, status_code: int, body: Dict[str, Any]) -> None:
        raw = json.dumps(body).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        clean_path = self.path.split("?", 1)[0].rstrip("/")
        query_string = self.path.split("?", 1)[1] if "?" in self.path else ""
        client_ip = (
            self.headers.get("X-Forwarded-For", "").split(",")[0].strip()
            or self.client_address[0]
        )

        if clean_path == GTM_AUDIT_PATH:
            from urllib.parse import parse_qs
            qs = parse_qs(query_string)
            domain = qs.get("domain", [""])[0].strip()
            company = qs.get("company", [""])[0].strip() or qs.get("company_name", [""])[0].strip()
            if domain or company:
                status_code, result = handle_gtm_audit({"domain": domain, "company_name": company}, client_ip)
                self._send_json(status_code, result)
            else:
                self._send_json(
                    200,
                    {
                        "endpoint": GTM_AUDIT_PATH,
                        "description": "On-Demand Autonomous GTM Systems & Architecture Audit API",
                        "rate_limits": {
                            "max_lookups_per_ip_hour": MAX_LOOKUPS_PER_IP_1H,
                            "max_generations_per_ip_24h": MAX_GENERATIONS_PER_IP_24H,
                            "max_global_generations_24h": MAX_GLOBAL_GENERATIONS_24H,
                        },
                        "precached_targets_count": len(PRECACHED_BLUEPRINTS),
                        "calendar_booking": "https://calendly.com/jaecarter",
                        "usage": "POST JSON with {'domain': 'company.com', 'work_email': 'you@company.com'}"
                    },
                )
            return

        if clean_path == SPAR_PATH:
            spar_html = PROJECT_ROOT / "scripts" / "spar_ui.html"
            if spar_html.exists():
                content = spar_html.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
            else:
                self._send_json(404, {"error": "spar_ui.html not found"})
            return

        if clean_path == SPAR_API_TARGETS:
            try:
                targets = list_active_targets()
                self._send_json(200, {"status": "success", "targets": targets})
            except Exception as e:
                self._send_json(500, {"status": "error", "error": str(e)})
            return

        if clean_path == SPAR_API_VOICES:
            self._send_json(200, {"status": "success", "voices": SUPPORTED_NEURAL_VOICES})
            return

        if clean_path in ("/health", *CLAY_PATHS, JOB_INGEST_PATH, QUICK_CLIP_PATH):
            self._send_json(
                200,
                {
                    "status": "ok",
                    "endpoints": [*CLAY_PATHS, JOB_INGEST_PATH, QUICK_CLIP_PATH, GTM_AUDIT_PATH, SPAR_PATH],
                    "raw_inbox_poller": "active (60s)",
                    "port": PORT,
                },
            )
        else:
            self._send_json(
                404,
                {
                    "error": (
                        f"Not found. Send POST requests to {CLAY_PATHS[0]}, "
                        f"{JOB_INGEST_PATH}, {QUICK_CLIP_PATH}, or {GTM_AUDIT_PATH}"
                    )
                },
            )

    def do_POST(self) -> None:
        clean_path = self.path.split("?", 1)[0].rstrip("/")
        query_lower = self.path.lower()
        dry_run = "dry_run=true" in query_lower
        run_async = "async=true" in query_lower
        client_ip = (
            self.headers.get("X-Forwarded-For", "").split(",")[0].strip()
            or self.client_address[0]
        )

        if clean_path not in ALL_PATHS:
            self._send_json(
                404,
                {
                    "error": (
                        f"Invalid path '{clean_path}'. "
                        f"Expected '{CLAY_PATHS[0]}', '{JOB_INGEST_PATH}', '{QUICK_CLIP_PATH}', or '{GTM_AUDIT_PATH}'."
                    )
                },
            )
            return

        content_length = int(self.headers.get("Content-Length", "0"))
        if content_length <= 0:
            self._send_json(400, {"error": "Empty request body."})
            return

        raw_body = self.rfile.read(content_length)
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except json.JSONDecodeError as exc:
            self._send_json(400, {"error": f"Invalid JSON payload: {exc}"})
            return

        try:
            if clean_path == SPAR_API_START:
                co = payload.get("company_name", "Target")
                discovery = payload.get("discovery_mode", False)
                phone_mode = payload.get("phone_mode", False)
                voice_name = payload.get("voice_name", "aoede")
                session = SparSessionManager.create_session(
                    co,
                    discovery_mode=discovery,
                    phone_mode=phone_mode,
                    voice_name=voice_name,
                )
                self._send_json(200, session)
            elif clean_path == SPAR_API_TURN:
                sess_id = payload.get("session_id", "")
                resp_text = payload.get("candidate_response", "")
                turn_res = SparSessionManager.submit_turn(sess_id, resp_text)
                self._send_json(200, turn_res)
            elif clean_path == SPAR_API_END_CALL:
                sess_id = payload.get("session_id", "")
                scorecard_res = SparSessionManager.generate_post_call_scorecard(sess_id)
                self._send_json(200, scorecard_res)
            elif clean_path == SPAR_API_SPEAK:
                text = payload.get("text", "")
                voice = payload.get("voice_name", "aoede")
                wav_b = generate_neural_audio_wav(text, voice_name=voice)
                audio_url = f"data:audio/wav;base64,{base64.b64encode(wav_b).decode('ascii')}" if wav_b else None
                self._send_json(200, {"status": "success" if audio_url else "error", "audio_url": audio_url})
            elif clean_path == SPAR_API_SYNC:
                sess_id = payload.get("session_id", "")
                sync_res = SparSessionManager.sync_to_notion(sess_id)
                self._send_json(200, sync_res)
            elif clean_path == GTM_AUDIT_PATH:
                status_code, result = handle_gtm_audit(payload, client_ip)
                self._send_json(status_code, result)
            elif clean_path == QUICK_CLIP_PATH:
                result = handle_quick_clip(payload)
                self._send_json(200, result)
            elif clean_path == JOB_INGEST_PATH:
                result = stage_and_ingest_job(payload, dry_run=dry_run, run_async=run_async)
                self._send_json(200, result)
            else:
                result = stage_and_trigger(payload, dry_run=dry_run)
                self._send_json(200, result)
        except ValidationError as exc:
            self._send_json(422, {"error": "Schema validation failed", "details": exc.errors()})
        except Exception as exc:
            self._send_json(500, {"error": str(exc)})

    def log_message(self, format: str, *args: Any) -> None:
        sys.stdout.write(f"[Webhook] {self.address_string()} - {format % args}\n")
        sys.stdout.flush()


def create_http_server(host: str = HOST, port: int = PORT) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), ClayWebhookHandler)


def start_http_server_thread(host: str = HOST, port: int = PORT, start_poller: bool = False) -> threading.Thread:
    """Starts the HTTP webhook & sparring server in a background daemon thread."""
    if start_poller:
        start_background_poller_thread(interval_seconds=60)
    server = create_http_server(host, port)
    t = threading.Thread(target=server.serve_forever, daemon=True, name="HopefulHeisenbergHTTPServer")
    t.start()
    print(f"[*] Background HTTP Server active on http://{host}:{port}/spar", flush=True)
    return t


def run_server(host: str = HOST, port: int = PORT, poll_interval: int = 60) -> None:
    """Starts the 0-Credit Notion Inbox Poller thread and the HTTP webhook server on port 8000."""
    if poll_interval > 0:
        start_background_poller_thread(interval_seconds=poll_interval)
    server = create_http_server(host, port)
    print(
        f"[*] Clay Deal Desk & Job Pipeline Webhook Server listening on:\n"
        f"    - http://{host}:{port}/spar (Live Voice Sparring Room)\n"
        f"    - http://{host}:{port}{CLAY_PATHS[0]}\n"
        f"    - http://{host}:{port}{JOB_INGEST_PATH}\n"
        f"    - http://{host}:{port}{QUICK_CLIP_PATH}\n"
        f"    - http://{host}:{port}{GTM_AUDIT_PATH} (On-Demand Public Audit API)\n"
        f"    - Background Notion '0. Raw Inbox' Poller: every {poll_interval}s",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[*] Shutting down webhook server...", flush=True)
        server.server_close()


if __name__ == "__main__":
    run_server()
