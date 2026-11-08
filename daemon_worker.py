#!/usr/bin/env python3
"""
0-Credit Notion Inbox Poller & Google Search Grounding Enricher (daemon_worker.py).

Polls NOTION_JOB_DB_ID every 60 seconds for rows where Stage == '0. Raw Inbox'.
Supports both:
  - Explicit 'Company — Role' titles
  - 1-Click Apple Shortcut URL-only clips (auto-detecting company_name & role_title from the Job Posting URL)
"""

import argparse
import datetime
import html
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse
import requests
from dotenv import load_dotenv
from google import genai
from google.genai import types

PROJECT_ROOT = Path(__file__).resolve().parent
ENV_PATH = PROJECT_ROOT / ".env"
load_dotenv(ENV_PATH)

from job_engine import process_job_ingest
from scripts.notifications import send_clean_mac_notification
from scripts.followup_engine import (
    generate_followup_bump,
    stage_followup_eml,
    format_followup_notion_blocks,
)

FOLLOWUP_THRESHOLD_DAYS = 4
FOLLOWUP_MARKER = "Touch 2:"

NOTION_VERSION = "2022-06-28"
DEFAULT_POLL_INTERVAL = 60

# Strict trigger keyword to avoid collisions with real companies (e.g. "Forescout")
SCOUT_TRIGGER_KEYWORDS = {
    "!run_morning_scout!",
    "!run_morning_scout",
}


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


def is_scout_trigger(title: Optional[str]) -> bool:
    """Strictly matches '!RUN_MORNING_SCOUT!' (case-insensitive) as the title, avoiding company collisions."""
    if not title:
        return False
    t = title.strip().lower()
    return t in SCOUT_TRIGGER_KEYWORDS


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


def _extract_plain_text(prop: Dict[str, Any]) -> str:
    ptype = prop.get("type")
    if ptype == "title":
        return "".join(x.get("plain_text", "") for x in prop.get("title", [])).strip()
    if ptype == "rich_text":
        return "".join(x.get("plain_text", "") for x in prop.get("rich_text", [])).strip()
    if ptype == "url":
        return (prop.get("url") or "").strip()
    if ptype == "email":
        return (prop.get("email") or "").strip()
    if ptype == "select" and prop.get("select"):
        return (prop["select"].get("name") or "").strip()
    return ""


def _infer_company_from_url(url_str: str) -> Tuple[str, str]:
    """Extracts a clean fallback (company_name, role_title) from a job board or company URL."""
    try:
        parsed = urlparse(url_str if "://" in url_str else f"https://{url_str}")
        host = (parsed.netloc or "").lower().removeprefix("www.")
        path_parts = [p for p in (parsed.path or "").split("/") if p]

        # Common ATS hosts: jobs.ashbyhq.com/<company>/<id>, boards.greenhouse.io/<company>/..., jobs.lever.co/<company>/...
        if any(ats in host for ats in ("ashbyhq.com", "greenhouse.io", "lever.co", "workable.com", "myworkdayjobs.com")):
            if path_parts:
                comp = path_parts[0].replace("-", " ").replace("_", " ").title()
                return comp, "GTM Engineer"

        domain_root = host.split(".")[0]
        if domain_root and domain_root not in ("linkedin", "wellfound", "ycombinator", "workatastartup"):
            return domain_root.replace("-", " ").title(), "GTM Engineer"
    except Exception:
        pass
    return "Target Startup", "GTM Engineer"


def _parse_company_and_role(raw_title: str, job_url: Optional[str] = None) -> Tuple[str, str, bool]:
    """
    Splits 'Company — Role' into (company_name, role_title, needs_url_title_detection).
    If raw_title is blank or is a raw URL from an Apple Shortcut clip, sets needs_url_title_detection=True.
    """
    cleaned = raw_title.strip()
    if not cleaned or cleaned.startswith(("http://", "https://")) or cleaned.lower() in ("auto", "url", "clip"):
        comp, role = _infer_company_from_url(job_url or cleaned)
        return comp, role, True

    for sep in ("—", "–", " - ", " | ", ": "):
        if sep in cleaned:
            parts = cleaned.split(sep, 1)
            comp = parts[0].strip()
            role = parts[1].strip()
            if comp and role:
                return comp, role, False
    return cleaned, "GTM Engineer", False


def _fetch_page_body_text(page_id: str, headers: Dict[str, str]) -> str:
    """Reads any raw JD text pasted as blocks inside the Notion page body."""
    try:
        resp = requests.get(
            f"https://api.notion.com/v1/blocks/{page_id}/children?page_size=100",
            headers=headers,
            timeout=15,
        )
        if resp.status_code != 200:
            return ""
        lines: List[str] = []
        for block in resp.json().get("results", []):
            btype = block.get("type", "")
            bdata = block.get(btype, {})
            if isinstance(bdata, dict) and "rich_text" in bdata:
                txt = "".join(t.get("plain_text", "") for t in bdata.get("rich_text", [])).strip()
                if txt and not txt.startswith("⏳ Morning Scout Triage Gate"):
                    lines.append(txt)
        return "\n".join(lines).strip()
    except Exception:
        return ""


def fetch_live_ats_jd(job_url: Optional[str]) -> Optional[str]:
    """Retrieves full job description text directly from Ashby, Greenhouse, or live web page."""
    if not job_url or not job_url.startswith(("http://", "https://")):
        return None
    try:
        # 1. Greenhouse direct board API
        gh_match = re.search(r"(?:boards|job-boards)\.greenhouse\.io/(?:embed/job_board\?for=)?([^/]+)/jobs/([0-9]+)", job_url)
        if not gh_match:
            gh_match = re.search(r"gh_jid=([0-9]+)", job_url)
        if gh_match:
            if len(gh_match.groups()) == 2:
                board_slug, jid = gh_match.group(1), gh_match.group(2)
                api_url = f"https://boards-api.greenhouse.io/v1/boards/{board_slug}/jobs/{jid}?content=true"
            else:
                jid = gh_match.group(1)
                api_url = f"https://boards-api.greenhouse.io/v1/jobs/{jid}?content=true"
            gh_resp = requests.get(api_url, timeout=10)
            if gh_resp.status_code == 200:
                content = gh_resp.json().get("content") or ""
                txt = html.unescape(content)
                txt = re.sub(r"<(br|p|li|div|h[1-6])[^>]*>", "\n", txt, flags=re.IGNORECASE)
                txt = re.sub(r"<[^>]+>", "", txt)
                txt = re.sub(r"\n{3,}", "\n\n", txt).strip()
                if len(txt) > 200:
                    return txt

        # 2. General page HTML fetch
        resp = requests.get(
            job_url,
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
            timeout=12,
        )
        if resp.status_code == 200:
            txt = html.unescape(resp.text)
            txt = re.sub(r"<script[^>]*>.*?</script>", "", txt, flags=re.DOTALL | re.IGNORECASE)
            txt = re.sub(r"<style[^>]*>.*?</style>", "", txt, flags=re.DOTALL | re.IGNORECASE)
            txt = re.sub(r"<(br|p|li|div|h[1-6])[^>]*>", "\n", txt, flags=re.IGNORECASE)
            txt = re.sub(r"<[^>]+>", "", txt)
            txt = re.sub(r"\n{3,}", "\n\n", txt).strip()
            if len(txt) > 400:
                return txt[:8000]
    except Exception as exc:
        print(f"[!] Warning fetching live ATS JD from {job_url}: {exc}")
    return None


def enrich_with_google_search_grounding(
    company_name: str,
    role_title: str,
    job_url: Optional[str] = None,
    raw_jd_text: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Uses Gemini with Google Search Grounding (tools=[{"google_search": {}}])
    to enrich company_name, role_title, funding_stage, product_summary, company_url,
    buyer_name_title, buyer_email, buyer_linkedin, and job_description for 0 Clay credits.
    """
    load_dotenv(ENV_PATH)
    api_key = (os.environ.get("GEMINI_API_KEY") or "").strip().strip('"').strip("'")
    slug_domain = re.sub(r"[^a-z0-9]+", "", company_name.lower()) + ".com"

    fallback = {
        "company_name": company_name,
        "role_title": role_title,
        "company_url": f"https://www.{slug_domain}",
        "funding_stage": f"Venture-backed high-growth AI/Enterprise technology ({company_name})",
        "product_summary": f"{company_name} enterprise software and AI infrastructure platform.",
        "buyer_name_title": f"VP of Revenue & Head of GTM ({company_name})",
        "buyer_email": f"gtm@{slug_domain}",
        "buyer_linkedin": None,
        "job_description": raw_jd_text
        or (
            f"We are hiring a {role_title} at {company_name} to architect autonomous GTM pipelines using Clay, "
            f"Python, Webhooks, REST APIs, LLM Structured Outputs, CRM/Salesforce/HubSpot, SQL, and Notion Deal Desks "
            f"while driving enterprise outbound pipeline with MEDDICC, Command of the Message, and The Challenger Sale."
        ),
    }

    if not api_key:
        return fallback

    client = genai.Client(api_key=api_key)
    prompt = (
        f"Use Google Search to research the company '{company_name}' and the role '{role_title}'"
        + (f" (Job Posting URL: {job_url})" if job_url else "")
        + ".\n"
        "Return ONLY a valid JSON object (no markdown commentary) with these exact keys:\n"
        "{\n"
        '  "company_name": "exact company name",\n'
        '  "role_title": "exact role title from the job posting",\n'
        '  "company_url": "official website URL",\n'
        '  "funding_stage": "latest funding round, amount, valuation, and lead investors",\n'
        '  "product_summary": "1-2 sentence summary of their core product, ICP, and value proposition",\n'
        '  "buyer_name_title": "Real name and title of their functional GTM/revenue executive (e.g. President of GTM, CRO, VP of RevOps, Head of GTM Systems, or VP of Sales; only fall back to CEO/Founder if early-stage startup under 50 people)",\n'
        '  "buyer_email": "inferred or public work email for that executive (e.g. first.last@domain.com)",\n'
        '  "buyer_linkedin": "Direct LinkedIn profile URL for that specific GTM executive (e.g. https://www.linkedin.com/in/...)",\n'
        '  "job_description": "Comprehensive summary of responsibilities and hard skills required for this role"\n'
        "}\n"
    )

    for model_name in ["gemini-2.5-pro", "gemini-3-flash-preview", "gemini-2.5-flash"]:
        try:
            print(f"[*] [0-Credit Poller] Enriching '{company_name} — {role_title}' via {model_name} + Google Search Grounding...")
            resp = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    tools=[{"google_search": {}}],
                    temperature=0.1,
                ),
            )
            text = (resp.text or "").strip()
            match = re.search(r"\{.*\}", text, re.DOTALL)
            if match:
                parsed = json.loads(match.group(0))
                return {
                    "company_name": parsed.get("company_name") or company_name,
                    "role_title": parsed.get("role_title") or role_title,
                    "company_url": parsed.get("company_url") or fallback["company_url"],
                    "funding_stage": parsed.get("funding_stage") or fallback["funding_stage"],
                    "product_summary": parsed.get("product_summary") or fallback["product_summary"],
                    "buyer_name_title": parsed.get("buyer_name_title") or fallback["buyer_name_title"],
                    "buyer_email": parsed.get("buyer_email") or fallback["buyer_email"],
                    "buyer_linkedin": parsed.get("buyer_linkedin"),
                    "job_description": raw_jd_text or parsed.get("job_description") or fallback["job_description"],
                }
        except Exception as exc:
            err_str = str(exc).upper()
            if "404" in err_str or "NOT_FOUND" in err_str or "LIMIT: 0" in err_str:
                continue
            print(f"[!] Google Search Grounding warning on {model_name}: {exc}")
            continue

    return fallback


def query_raw_inbox_rows(job_db_id: str, headers: Dict[str, str]) -> List[Dict[str, Any]]:
    """Queries NOTION_JOB_DB_ID for rows where Stage == '0. Approved (Build Deal Room)' or '0. Raw Inbox'."""
    resp = requests.post(
        f"https://api.notion.com/v1/databases/{job_db_id}/query",
        headers=headers,
        json={
            "filter": {
                "or": [
                    {
                        "property": "Stage",
                        "select": {"equals": "0. Approved (Build Deal Room)"},
                    },
                    {
                        "property": "Stage",
                        "select": {"equals": "0. Raw Inbox"},
                    },
                    {
                        "property": "Stage",
                        "select": {"is_empty": True},
                    },
                ]
            }
        },
        timeout=20,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Notion query for Approved / Raw Inbox rows failed ({resp.status_code}): {resp.text}")
    return resp.json().get("results", [])


def poll_raw_inbox_once(notion_client: Any = None, database_id: Optional[str] = None) -> int:
    """Finds and processes all '0. Raw Inbox' and '0. Approved' rows in NOTION_JOB_DB_ID."""
    job_db_id, headers = _get_notion_headers()
    target_db_id = database_id or job_db_id
    if not target_db_id:
        return 0

    rows = query_raw_inbox_rows(target_db_id, headers)
    if not rows:
        return 0

    processed_count = 0
    for row in rows:
        page_id = row.get("id")
        props = row.get("properties", {})
        raw_title = _extract_plain_text(props.get("Company & Role", {}))
        existing_job_url = _extract_plain_text(props.get("Job Posting URL", {})) or None
        if not page_id or (not raw_title and not existing_job_url):
            continue

        # Manual Scout Trigger check: !RUN_MORNING_SCOUT! or [TRIGGER_JOB_SCOUT]
        if is_scout_trigger(raw_title):
            print(f"[*] [Manual Trigger Detected] Found '{raw_title}' (Page ID: {page_id}). Running Morning Job Scout now...", flush=True)
            send_clean_macos_notification("[DAEMON] GTM Job Daemon", f"Running scout for: {raw_title}")
            try:
                requests.patch(
                    f"https://api.notion.com/v1/pages/{page_id}",
                    headers=headers,
                    json={"archived": True},
                    timeout=15,
                )
                print(f"[+] Archived trigger row '{raw_title}' in Notion.", flush=True)
            except Exception as exc:
                print(f"[!] Warning archiving trigger row: {exc}", flush=True)

            try:
                from scripts.morning_job_scout import run_morning_job_scout
                created_urls = run_morning_job_scout(max_new_jobs=8)
                print(f"[+] Morning Scout execution complete. Added {len(created_urls)} new role(s).", flush=True)
                send_clean_macos_notification("[SCOUT] Morning GTM Job Scout", f"Scout complete: {len(created_urls)} new role(s) ready in Notion")
            except Exception as exc:
                print(f"[!] Morning Scout execution failed: {exc}", flush=True)

            processed_count += 1
            continue

        company_name, role_title, needs_url_detection = _parse_company_and_role(raw_title, job_url=existing_job_url)
        existing_company_url = _extract_plain_text(props.get("Company URL", {})) or None
        existing_funding = _extract_plain_text(props.get("Funding & Stage", {})) or None
        existing_salary = _extract_plain_text(props.get("Salary Range", {})) or None
        existing_buyer = _extract_plain_text(props.get("Economic Buyer", {})) or None
        existing_email = _extract_plain_text(props.get("Buyer Email", {})) or None
        existing_linkedin = _extract_plain_text(props.get("Buyer LinkedIn", {})) or None

        raw_jd_text = _fetch_page_body_text(page_id, headers)
        if (not raw_jd_text or len(raw_jd_text) < 400) and existing_job_url:
            live_jd = fetch_live_ats_jd(existing_job_url)
            if live_jd:
                raw_jd_text = live_jd

        if needs_url_detection or not existing_funding or not existing_buyer or not raw_jd_text:
            grounded = enrich_with_google_search_grounding(
                company_name=company_name,
                role_title=role_title,
                job_url=existing_job_url or (raw_title if raw_title.startswith("http") else None),
                raw_jd_text=raw_jd_text if raw_jd_text else None,
            )
            if needs_url_detection:
                company_name = grounded.get("company_name") or company_name
                role_title = grounded.get("role_title") or role_title
        else:
            grounded = {}

        payload = {
            "company_name": company_name,
            "role_title": role_title,
            "company_url": existing_company_url or grounded.get("company_url"),
            "job_url": existing_job_url or (raw_title if raw_title.startswith("http") else None),
            "funding_stage": existing_funding or grounded.get("funding_stage"),
            "salary_range": existing_salary or grounded.get("salary_range"),
            "product_summary": grounded.get("product_summary") or f"{company_name} Enterprise Platform",
            "buyer_name_title": existing_buyer or grounded.get("buyer_name_title"),
            "buyer_email": existing_email or grounded.get("buyer_email"),
            "buyer_linkedin": existing_linkedin or grounded.get("buyer_linkedin"),
            "job_description": raw_jd_text or grounded.get("job_description") or f"{role_title} at {company_name}",
        }

        print(f"[*] [0-Credit Poller] Processing Raw Inbox row '{company_name} — {role_title}' (Page ID: {page_id})...")
        process_job_ingest(payload, existing_page_id=page_id)
        send_clean_macos_notification(
            title="[DEAL ROOM] Deal Room Generated",
            message=f"{company_name} — {role_title}",
            subtitle="PDF Uploaded & Draft Staged",
        )
        processed_count += 1

    return processed_count


class _SimpleNotionClient:
    """Lightweight adapter mimicking notion_client API for blocks and databases."""

    def __init__(self, headers: Dict[str, str]):
        self.headers = headers
        self.databases = self._Databases(headers)
        self.blocks = self._Blocks(headers)
        self.pages = self._Pages(headers)

    class _Databases:
        def __init__(self, headers):
            self.headers = headers

        def query(self, database_id: str, filter: Dict[str, Any]):
            resp = requests.post(
                f"https://api.notion.com/v1/databases/{database_id}/query",
                headers=self.headers,
                json={"filter": filter},
                timeout=20,
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
                timeout=20,
            )
            return resp.json()

        class _Children:
            def __init__(self, headers):
                self.headers = headers

            def list(self, block_id: str, page_size: int = 100):
                resp = requests.get(
                    f"https://api.notion.com/v1/blocks/{block_id}/children?page_size={page_size}",
                    headers=self.headers,
                    timeout=15,
                )
                return resp.json() if resp.status_code == 200 else {"results": []}

            def append(self, block_id: str, children: list):
                resp = requests.patch(
                    f"https://api.notion.com/v1/blocks/{block_id}/children",
                    headers=self.headers,
                    json={"children": children},
                    timeout=25,
                )
                return resp.json() if resp.status_code == 200 else {}

    class _Pages:
        def __init__(self, headers):
            self.headers = headers

        def create(self, **kwargs):
            resp = requests.post(
                "https://api.notion.com/v1/pages",
                headers=self.headers,
                json=kwargs,
                timeout=30,
            )
            if resp.status_code not in (200, 201):
                print(f"[!] Notion API pages.create failed ({resp.status_code}): {resp.text}")
            return resp.json() if resp.status_code in (200, 201) else {}

        def retrieve(self, page_id: str):
            resp = requests.get(
                f"https://api.notion.com/v1/pages/{page_id}",
                headers=self.headers,
                timeout=20,
            )
            return resp.json() if resp.status_code == 200 else {}

        def update(self, page_id: str, **kwargs):
            resp = requests.patch(
                f"https://api.notion.com/v1/pages/{page_id}",
                headers=self.headers,
                json=kwargs,
                timeout=25,
            )
            return resp.json() if resp.status_code == 200 else {}


def has_followup_already(notion_client, page_id: str) -> bool:
    """Checks if the 4-day follow up block has already been appended."""
    try:
        if notion_client is None:
            _, headers = _get_notion_headers()
            notion_client = _SimpleNotionClient(headers)

        response = notion_client.blocks.children.list(block_id=page_id, page_size=100)
        for block in response.get("results", []):
            b_type = block.get("type", "")
            if b_type.startswith("heading_"):
                text_objs = block.get(b_type, {}).get("rich_text", [])
                full_text = "".join([t.get("plain_text", "") for t in text_objs])
                if FOLLOWUP_MARKER in full_text:
                    return True
        return False
    except Exception as e:
        print(f"[!] Error inspecting page blocks for follow-up on {page_id}: {e}")
        return False


def check_and_process_stage_3_followups(notion_client=None, database_id: Optional[str] = None, force_followup: bool = False):
    """
    Finds cards in '3. Outbound Sent' that have been untouched for >= 4 days
    and stages a follow-up draft.
    """
    try:
        if not database_id:
            db_id, headers = _get_notion_headers()
            database_id = db_id
        else:
            _, headers = _get_notion_headers()

        if notion_client is None:
            notion_client = _SimpleNotionClient(headers)

        results = notion_client.databases.query(
            database_id=database_id,
            filter={
                "property": "Stage",
                "select": {
                    "equals": "3. Outbound Sent"
                }
            }
        ).get("results", [])

        if not results:
            return 0

        now = datetime.datetime.now(datetime.timezone.utc)
        processed_count = 0

        for page in results:
            page_id = page["id"]

            # 1. Idempotency Check
            if has_followup_already(notion_client, page_id):
                continue

            # 2. Check Elapsed Time via last_edited_time
            last_edited_str = page.get("last_edited_time")
            if not last_edited_str:
                continue

            last_edited_dt = datetime.datetime.fromisoformat(last_edited_str.replace("Z", "+00:00"))
            elapsed_days = (now - last_edited_dt).days

            # Only trigger if 4 or more days have passed
            if not force_followup and elapsed_days < FOLLOWUP_THRESHOLD_DAYS:
                continue

            # 3. Parse Metadata & Dossier
            title_prop = page["properties"].get("Company & Role", {}).get("title", [])
            raw_title = title_prop[0]["plain_text"] if title_prop else "Target Company"
            company_match = re.split(r"[-—–:|]", raw_title)
            company_name = company_match[0].strip() if company_match else raw_title.strip()
            role_title = company_match[1].strip() if len(company_match) > 1 else raw_title.strip()

            clean_co = re.sub(r"[^\w\-]", "_", company_name)
            dossier_file = Path(f"output/dossiers/{clean_co}_dossier.json")
            dossier = {}
            if dossier_file.exists():
                with open(dossier_file, "r", encoding="utf-8") as f:
                    dossier = json.load(f)

            print(f"[*] 4-day threshold reached for {raw_title} ({elapsed_days} days). Generating follow-up bump...")

            # 4. Generate & Stage
            bump = generate_followup_bump(company_name, role_title, dossier)
            draft_path = stage_followup_eml(company_name, bump)
            blocks = format_followup_notion_blocks(bump, draft_path.name)

            # 5. Append to Notion
            notion_client.blocks.children.append(block_id=page_id, children=blocks)
            print(f"[+] Appended follow-up blocks to {raw_title}")

            # 6. Native macOS Notification
            send_clean_mac_notification(
                title="[FOLLOW-UP] 4-Day Follow-Up Staged",
                message=f"Follow-up draft ready for {company_name} in Apple Mail & Notion."
            )
            processed_count += 1

        return processed_count

    except Exception as e:
        print(f"[!] Error in check_and_process_stage_3_followups: {e}")
        return 0


BATTLECARD_MARKER = "[INTERVIEW] Executive Interview Battlecard (Stage 4)"


def has_battlecard_already(page_id: str, headers: Optional[Dict[str, str]] = None, notion_client: Any = None) -> bool:
    """
    Idempotency check: Inspects page child blocks to verify if
    the battlecard heading has already been written.
    """
    try:
        if notion_client is not None and hasattr(notion_client, "blocks"):
            from scripts.interview_prep_engine import battlecard_already_exists
            return battlecard_already_exists(notion_client, page_id)

        if not headers:
            _, headers = _get_notion_headers()
        resp = requests.get(
            f"https://api.notion.com/v1/blocks/{page_id}/children?page_size=100",
            headers=headers,
            timeout=15,
        )
        if resp.status_code != 200:
            return False
        blocks = resp.json().get("results", [])

        for block in blocks:
            b_type = block.get("type", "")
            if b_type == "heading_1":
                text_objects = block.get("heading_1", {}).get("rich_text", [])
                full_text = "".join([t.get("plain_text", "") for t in text_objects])
                if "Executive Interview Battlecard" in full_text:
                    return True
        return False
    except Exception as e:
        print(f"[!] Error checking blocks for battlecard on {page_id}: {e}")
        return False


def check_and_process_stage_4(notion_client: Any = None, database_id: Optional[str] = None) -> int:
    """
    Polls for rows in '4. Hiring Manager Screen' and appends
    the Clay/Lindahl Battlecard if not already present.
    """
    from scripts.interview_prep_engine import (
        generate_interview_battlecard,
        format_battlecard_notion_blocks,
        save_battlecard_markdown,
    )
    from scripts.notifications import send_clean_mac_notification

    job_db_id, headers = _get_notion_headers()
    target_db_id = database_id or job_db_id
    if not target_db_id:
        return 0

    processed_count = 0
    try:
        if notion_client is not None and hasattr(notion_client, "databases"):
            results = notion_client.databases.query(
                database_id=target_db_id,
                filter={
                    "property": "Stage",
                    "select": {
                        "equals": "4. Hiring Manager Screen"
                    }
                }
            ).get("results", [])
        else:
            resp = requests.post(
                f"https://api.notion.com/v1/databases/{target_db_id}/query",
                headers=headers,
                json={
                    "filter": {
                        "property": "Stage",
                        "select": {"equals": "4. Hiring Manager Screen"},
                    }
                },
                timeout=20,
            )
            if resp.status_code != 200:
                print(f"[!] Error querying Stage 4 rows ({resp.status_code}): {resp.text}")
                return 0
            results = resp.json().get("results", [])

        if not results:
            return 0

        for page in results:
            page_id = page["id"]

            # 1. Idempotency Gate
            if has_battlecard_already(page_id, headers=headers, notion_client=notion_client):
                continue

            # 2. Extract Company & Role Metadata
            title_prop = page.get("properties", {}).get("Company & Role", {}).get("title", [])
            raw_title = title_prop[0]["plain_text"] if title_prop else "Target Company"

            # Parse company name from title (e.g. "Toast — GTM Engineer" -> "Toast")
            company_match = re.split(r"[-—–:|]", raw_title)
            company_name = company_match[0].strip() if company_match else raw_title.strip()
            role_title = company_match[1].strip() if len(company_match) > 1 else raw_title.strip()

            print(f"\n[*] Stage 4 detected for '{raw_title}'. Building Interview Battlecard...")

            # 3. Load Cached Dossier or Fallback
            clean_company = re.sub(r"[^\w\-]", "_", company_name)
            dossier_candidates = [
                Path(f"output/dossiers/{clean_company}_dossier.json"),
                Path(f"output/dossiers/{clean_company.lower()}.json"),
                Path(f"output/dossiers/{clean_company}.json"),
                Path(f"output/dossiers/{company_name.lower()}.json"),
                Path("output/dossiers/toast.json") if "toast" in company_name.lower() else None,
            ]
            dossier = None
            for cand in dossier_candidates:
                if cand and cand.exists():
                    try:
                        with open(cand, "r", encoding="utf-8") as f:
                            dossier = json.load(f)
                            break
                    except Exception:
                        pass

            if not dossier:
                # Fallback structure if dossier hasn't been cached on disk
                dossier = {
                    "company_name": company_name,
                    "headcount_bracket": "500+",
                    "capital": {"funding_stage": "Growth", "total_funding_amount": "Undisclosed"},
                    "leadership": {"economic_buyer_name": "Revenue Leadership", "economic_buyer_title": "VP of Sales / CRO"},
                    "news": {"recent_headline": "Active scaling and enterprise expansion"},
                    "verified_tech_stack": ["Salesforce", "Clay", "HubSpot", "Python"],
                }

            # 4. Generate Clay & Lindahl Battlecard
            battlecard = generate_interview_battlecard(company_name, role_title, dossier)
            blocks = format_battlecard_notion_blocks(battlecard)

            # 5. Append Blocks to Notion Page
            if notion_client is not None and hasattr(notion_client, "blocks"):
                notion_client.blocks.children.append(
                    block_id=page_id,
                    children=blocks
                )
            else:
                append_resp = requests.patch(
                    f"https://api.notion.com/v1/blocks/{page_id}/children",
                    headers=headers,
                    json={"children": blocks},
                    timeout=25,
                )
                if append_resp.status_code != 200:
                    print(f"[!] Warning appending Battlecard blocks to Notion ({append_resp.status_code}): {append_resp.text}")
                else:
                    print(f"[+] Appended Battlecard to {raw_title} (Page ID: {page_id})")

            # Save local markdown brief for review
            try:
                from job_engine import calculate_coi_metrics
                coi = calculate_coi_metrics(dossier)
                md_path = save_battlecard_markdown(battlecard, company_name, role_title, coi)
                print(f"[+] Saved local Markdown brief -> {md_path}")
            except Exception as exc:
                print(f"[!] Notice saving local markdown: {exc}")

            # 6. Fire Native macOS Desktop Notification
            send_clean_mac_notification(
                title="[INTERVIEW] Interview Battlecard Staged",
                message=f"Battlecard appended to Notion for {company_name}!",
                subtitle="Stage 4 Hiring Manager Screen Prep"
            )
            processed_count += 1

    except Exception as e:
        print(f"[!] Error in check_and_process_stage_4: {e}")

    return processed_count


STAGE_5_BLITZ_MARKER = "Stage 5 Work Trial Architecture Live"


def has_stage_5_blitz_already(page_id: str, headers: Optional[Dict[str, str]] = None, notion_client: Any = None) -> bool:
    """
    Idempotency check: Inspects page child blocks to verify if
    the Stage 5 Work Trial Blueprint has already been published/appended.
    """
    try:
        if notion_client is not None and hasattr(notion_client, "blocks"):
            response = notion_client.blocks.children.list(block_id=page_id, page_size=100)
            blocks = response.get("results", [])
        else:
            if not headers:
                _, headers = _get_notion_headers()
            resp = requests.get(
                f"https://api.notion.com/v1/blocks/{page_id}/children?page_size=100",
                headers=headers,
                timeout=15,
            )
            if resp.status_code != 200:
                return False
            blocks = resp.json().get("results", [])

        for block in blocks:
            b_type = block.get("type", "")
            if b_type.startswith("heading_") or b_type in ("paragraph", "callout", "toggle"):
                text_objects = block.get(b_type, {}).get("rich_text", [])
                full_text = "".join([t.get("plain_text", "") for t in text_objects])
                if (
                    STAGE_5_BLITZ_MARKER in full_text
                    or "Stage 5 Work Trial Architecture Live" in full_text
                ):
                    return True
        return False
    except Exception as e:
        print(f"[!] Error checking blocks for Stage 5 blitz on {page_id}: {e}")
        return False


def check_and_process_stage_5(notion: Any = None, db_id: Optional[str] = None, database_id: Optional[str] = None) -> int:
    """
    Watches for cards in '5. Work Trial / Panel'.
    Generates the 3-Account Blitz, publishes it to the Public Notion Hub,
    and updates the Option B placeholder in the Deal Room.
    """
    from scripts.generate_blitz import generate_gtme_blueprint_file
    from scripts.publish_blitz import (
        publish_blueprint_to_notion,
        inject_link_into_notion_card,
        update_staged_eml_files,
    )
    from scripts.notifications import send_clean_mac_notification

    job_db_id, headers = _get_notion_headers()
    target_db_id = db_id or database_id or job_db_id
    if not target_db_id:
        return 0

    client_auth = notion if (notion is not None and hasattr(notion, "pages")) else _SimpleNotionClient(headers)
    processed_count = 0
    try:
        stage_filter = {
            "property": "Stage",
            "select": {"equals": "5. Work Trial / Panel"},
        }
        if client_auth is not None and hasattr(client_auth, "databases"):
            results = client_auth.databases.query(
                database_id=target_db_id,
                filter=stage_filter
            ).get("results", [])
        else:
            resp = requests.post(
                f"https://api.notion.com/v1/databases/{target_db_id}/query",
                headers=headers,
                json={
                    "filter": stage_filter
                },
                timeout=20,
            )
            if resp.status_code != 200:
                print(f"[!] Error querying Stage 5 rows ({resp.status_code}): {resp.text}")
                return 0
            results = resp.json().get("results", [])

        if not results:
            return 0

        for page in results:
            page_id = page["id"]
            title_prop = page.get("properties", {}).get("Company & Role", {}).get("title", [])
            raw_title = title_prop[0]["plain_text"] if title_prop else ""
            if not raw_title:
                continue

            company_match = re.split(r"[-—–:|]", raw_title)
            company_name = company_match[0].strip() if company_match else raw_title.strip()
            role_title = company_match[1].strip() if len(company_match) > 1 else "GTM Engineer"
            clean_co = re.sub(r"[^\w\-]", "_", company_name)
            blueprint_path = Path(f"output/blitzes/{clean_co}_gtme_blueprint.md")

            # 1. Generate blueprint if it doesn't exist
            if not blueprint_path.exists():
                print(f"[*] Auto-generating Stage 5 Blitz for {company_name}...")
                generate_gtme_blueprint_file(company_name, role_title)

            # 2. Check if already published
            marker_file = Path(f"output/blitzes/{clean_co}_published.flag")
            already_has_blocks = has_stage_5_blitz_already(page_id, headers=headers, notion_client=client_auth)
            if not marker_file.exists() and not already_has_blocks and blueprint_path.exists():
                print(f"[*] Publishing Stage 5 Blitz to Public Notion Hub for {company_name}...")
                markdown_content = blueprint_path.read_text(encoding="utf-8")
                public_url = publish_blueprint_to_notion(client_auth, company_name, markdown_content)

                # Overwrite Option B placeholder with live URL
                inject_link_into_notion_card(client_auth, target_db_id, company_name, public_url, target_page_id=page_id)
                update_staged_eml_files(company_name, public_url)
                marker_file.write_text(public_url, encoding="utf-8")

                # Append Stage 5 Work Trial Callout to Card with real company logo
                clean_dom = f"{re.sub(r'[^a-zA-Z0-9]', '', company_name.lower())}.com"
                logo_url = f"https://unavatar.io/{clean_dom}?fallback=https://www.google.com/s2/favicons?domain={clean_dom}&sz=128"
                stage_5_blocks = [
                    {
                        "object": "block",
                        "type": "callout",
                        "callout": {
                            "icon": {"type": "external", "external": {"url": logo_url}},
                            "color": "blue_background",
                            "rich_text": [
                                {"type": "text", "text": {"content": "Stage 5 Work Trial Architecture Live\n"}, "annotations": {"bold": True}},
                                {"type": "text", "text": {"content": f"Published Blueprint: {public_url}\nAuto-injected into Option B outbound drafts."}}
                            ]
                        }
                    }
                ]
                if hasattr(client_auth, "blocks"):
                    client_auth.blocks.children.append(block_id=page_id, children=stage_5_blocks)
                else:
                    requests.patch(
                        f"https://api.notion.com/v1/blocks/{page_id}/children",
                        headers=headers,
                        json={"children": stage_5_blocks},
                        timeout=25,
                    )

                # Fire Native macOS Notification
                send_clean_mac_notification(
                    title="[WORK TRIAL] Work Trial Blitz Published",
                    message=f"{company_name} Stage 5 Blueprint live on Notion Hub!",
                    subtitle="Stage 5 Work Trial / Panel"
                )
                print(f"[+] Stage 5 Blitz successfully published and wired for {company_name}!")
                processed_count += 1

    except Exception as e:
        print(f"[!] Error in check_and_process_stage_5: {e}")

    return processed_count


# Aliases for backward compatibility
has_interview_battlecard = has_battlecard_already
has_followup_bump = has_followup_already
has_stage_5_blitz = has_stage_5_blitz_already
poll_hiring_manager_screen_once = check_and_process_stage_4
poll_stage_5_work_trial_once = check_and_process_stage_5
poll_stage_3_followups_once = check_and_process_stage_3_followups
poll_inbox_and_approved = poll_raw_inbox_once


def run_daemon_loop(interval_seconds: int = DEFAULT_POLL_INTERVAL) -> None:
    """Runs the 60-second Notion '0. Approved / Raw Inbox', '3. Outbound Sent', '4. Hiring Manager Screen', and '5. Work Trial / Panel' poller loop and 7:00 AM Morning Scout."""
    from datetime import datetime
    from scripts.morning_job_scout import run_morning_job_scout

    # Start the HTTP Webhook & Sparring Server on port 8000 in a background daemon thread
    try:
        from webhook_server import start_http_server_thread
        start_http_server_thread(port=8000, start_poller=False)
    except Exception as exc:
        print(f"[!] Warning: Could not start HTTP server thread on port 8000: {exc}", file=sys.stderr, flush=True)

    job_db_id, headers = _get_notion_headers()
    NOTION_JOB_DB_ID = job_db_id
    notion = _SimpleNotionClient(headers)

    print(
        f"[*] 0-Credit Notion Job Poller active (Interval: {interval_seconds}s | "
        "Watching Stage == '0. Approved / Raw Inbox', '3. Outbound Sent' (4-Day Bump), '4. Hiring Manager Screen', & '5. Work Trial / Panel' | Daily Scout @ 07:00)",
        flush=True,
    )
    last_scout_date: Optional[str] = None
    while True:
        try:
            now = datetime.now()
            today_str = now.strftime("%Y-%m-%d")
            # Morning Scout check
            if now.hour == 7 and now.minute < 5 and last_scout_date != today_str:
                print(f"[*] [07:00 Trigger] Running autonomous Morning Job Scout for {today_str}...", flush=True)
                try:
                    run_morning_job_scout(max_new_jobs=8)
                except Exception as exc:
                    print(f"[!] Morning Scout trigger notice: {exc}", file=sys.stderr, flush=True)
                last_scout_date = today_str

            # 1. Raw Inbox & Approved
            try:
                poll_inbox_and_approved(notion, NOTION_JOB_DB_ID)
            except Exception as e:
                print(f"[!] Error in poll_inbox_and_approved: {e}", file=sys.stderr, flush=True)

            # 2. Stage 3 Follow-Up Bumps (>= 4 days)
            try:
                check_and_process_stage_3_followups(notion, NOTION_JOB_DB_ID)
            except Exception as e:
                print(f"[!] Error in check_and_process_stage_3_followups: {e}", file=sys.stderr, flush=True)

            # 3. Stage 4 Interview Screens
            try:
                check_and_process_stage_4(notion, NOTION_JOB_DB_ID)
            except Exception as e:
                print(f"[!] Error in check_and_process_stage_4: {e}", file=sys.stderr, flush=True)

            # 4. Stage 5 Work Trial & Panel
            try:
                check_and_process_stage_5(notion, NOTION_JOB_DB_ID)
            except Exception as e:
                print(f"[!] Error in check_and_process_stage_5: {e}", file=sys.stderr, flush=True)

            # 5. Autonomous Multi-Touch Outbound Cadence (Touch 2 LinkedIn alerts, Touch 3 Notion staged bumps)
            try:
                from scripts.cadence_manager import process_automated_cadence_cycle
                process_automated_cadence_cycle(auto_open_browser=True)
            except Exception as e:
                print(f"[!] Error in process_automated_cadence_cycle: {e}", file=sys.stderr, flush=True)

        except Exception as e:
            print(f"[!] Daemon cycle exception: {e}", file=sys.stderr, flush=True)

        time.sleep(interval_seconds)


run_daemon_poller_loop = run_daemon_loop


def start_background_poller_thread(interval_seconds: int = DEFAULT_POLL_INTERVAL) -> threading.Thread:
    """Starts the 0-Credit Notion Inbox Poller in a background daemon thread alongside webhook_server.py."""
    thread = threading.Thread(
        target=run_daemon_poller_loop,
        kwargs={"interval_seconds": interval_seconds},
        daemon=True,
        name="NotionRawInboxPoller",
    )
    thread.start()
    return thread


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="0-Credit Notion Job Inbox Poller & Stage 4/5 Automation (daemon_worker.py)")
    parser.add_argument("--once", action="store_true", help="Run a single poll pass for Inbox, Stage 3 Follow-ups, Stage 4 Screen, and Stage 5 Work Trial rows and exit")
    parser.add_argument("--battlecards-only", action="store_true", help="Poll only Stage 4 '4. Hiring Manager Screen' rows and exit")
    parser.add_argument("--blitzes-only", action="store_true", help="Poll only Stage 5 '5. Work Trial / Panel' rows and exit")
    parser.add_argument("--followups-only", action="store_true", help="Poll only Stage 3 '3. Outbound Sent' rows for 4-day follow-up bumps and exit")
    parser.add_argument("--force-followup", action="store_true", help="Bypass 4-day wait threshold and generate follow-up bump for Stage 3 rows")
    parser.add_argument("--interval", type=int, default=DEFAULT_POLL_INTERVAL, help="Polling interval in seconds")
    args = parser.parse_args()

    if args.followups_only:
        fo = check_and_process_stage_3_followups(force_followup=args.force_followup)
        print(f"[+] Stage 3 Follow-Up poll pass complete. Processed {fo} row(s).")
    elif args.battlecards_only:
        hm = poll_hiring_manager_screen_once()
        print(f"[+] Stage 4 poll pass complete. Processed {hm} row(s).")
    elif args.blitzes_only:
        st5 = poll_stage_5_work_trial_once()
        print(f"[+] Stage 5 Work Trial poll pass complete. Processed {st5} row(s).")
    elif args.once:
        n = poll_raw_inbox_once()
        fo = check_and_process_stage_3_followups(force_followup=args.force_followup)
        hm = poll_hiring_manager_screen_once()
        st5 = poll_stage_5_work_trial_once()
        print(f"[+] Poll pass complete. Processed {n} inbox row(s), {fo} follow-up(s), {hm} Stage 4 screen row(s), and {st5} Stage 5 work trial row(s).")
    else:
        run_daemon_poller_loop(interval_seconds=args.interval)
