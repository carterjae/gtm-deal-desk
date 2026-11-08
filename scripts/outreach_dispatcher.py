#!/usr/bin/env python3
"""
scripts/outreach_dispatcher.py
1-Click Multi-Thread Committee Outreach Dispatcher & IMAP Drafts Synchronizer.
Generates 3 multi-threaded Challenger + Command of the Message (CoM) tracks
tailored to the department-verified Buying Committee:
  - Track 1 (Operational Hiring Manager / Champion)
  - Track 2 (Executive Sponsor / Economic Buyer)
  - Track 3 (Technical Peer / Architect)
Renders clickable mailto: launch buttons directly in the Notion Deal Room card,
pushes MIME drafts directly to IMAP Drafts ([Gmail]/Drafts), and prepares tracking metadata.
"""

import os
import sys
import json
import re
import time
import imaplib
import subprocess
import urllib.parse
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

load_dotenv(PROJECT_ROOT / ".env")

from scripts.contact_verifier import clean_domain

try:
    import importlib
    _old_path = list(sys.path)
    sys.path = [p for p in sys.path if not p.endswith("hopeful-heisenberg") and not p.endswith("gtm-deal-desk") and p != "" and p != str(PROJECT_ROOT)]
    _pkg = importlib.import_module("notion_client")
    Client = getattr(_pkg, "Client", None)
    sys.path = _old_path
except Exception:
    Client = None

try:
    from scripts.generate_blitz import calculate_deterministic_coi
except ImportError:
    def calculate_deterministic_coi(dossier: dict) -> dict:
        headcount = dossier.get("headcount_bracket", "51-200")
        reps_map = {"1-10": 2, "11-50": 5, "51-200": 12, "201-500": 25, "501-1000": 50, "1000+": 95}
        reps = reps_map.get(headcount, 10)
        return {
            "headcount_bracket": headcount,
            "estimated_reps": reps,
            "weekly_hours_drag": reps * 10,
            "annual_coi_usd": reps * 10 * 55 * 52,
        }

NOTION_TOKEN = os.getenv("NOTION_TOKEN")
NOTION_DEAL_DESK_DB_ID = os.getenv("NOTION_DEAL_DESK_DB_ID") or os.getenv("NOTION_DATABASE_ID") or os.getenv("NOTION_JOB_DB_ID")
IMAP_HOST = os.getenv("IMAP_HOST") or os.getenv("IMAP_SERVER", "imap.gmail.com")
IMAP_USER = os.getenv("IMAP_USER")
IMAP_PASSWORD = os.getenv("IMAP_PASSWORD")
SENDER_NAME = os.getenv("SENDER_NAME", "Enterprise Deal Desk")

STAGED_OUTREACH_FILE = PROJECT_ROOT / "output" / "staged_outreach.json"


def load_staged_outreach() -> dict:
    """Loads staged outreach tracking metadata."""
    if STAGED_OUTREACH_FILE.exists():
        try:
            return json.loads(STAGED_OUTREACH_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_staged_outreach(company_key: str, data: dict):
    """Saves staged outreach data for threading continuity."""
    STAGED_OUTREACH_FILE.parent.mkdir(parents=True, exist_ok=True)
    staged = load_staged_outreach()
    staged[company_key] = data
    STAGED_OUTREACH_FILE.write_text(json.dumps(staged, indent=2), encoding="utf-8")


def resolve_poc_email_and_name(poc: dict, domain: str, default_prefix: str = "") -> tuple[str, str]:
    """
    Resolves a realistic personalized email address and first name from a POC object.
    If a real full name is present (e.g. 'Emilio Escobar', 'Varun Anand'):
      - First Name: 'Emilio'
      - Email: 'emilio.escobar@datadog.com'
    If only a generic title or placeholder is present:
      - First Name: 'there'
      - Email: '' (Empty string to prevent hard-bouncing role placeholder addresses like hiring.manager@domain)
    """
    raw_name = poc.get("full_name_or_search_query", "") or poc.get("name", "")
    generic_words = {
        "hiring", "manager", "director", "head", "vp", "executive", "sponsor",
        "peer", "lead", "engineer", "team", "unknown", "n/a", "site:linkedin",
        "revops", "operations", "revenue", "sales", "gtm", "systems", "chief", "officer"
    }

    clean_name = re.sub(r'\(.*?\)', '', raw_name).strip()
    words = clean_name.split()

    is_real_name = (
        len(words) in (2, 3)
        and not any(w.lower() in generic_words for w in words)
        and "site:" not in clean_name.lower()
        and "/" not in clean_name
    )

    if is_real_name:
        first = words[0].strip().capitalize()
        last = words[-1].strip().capitalize()
        clean_first = re.sub(r'[^a-zA-Z]', '', first.lower())
        clean_last = re.sub(r'[^a-zA-Z]', '', last.lower())
        return f"{clean_first}.{clean_last}@{domain}", first

    return "", "there"


def resolve_seller_config() -> dict:
    """Loads active seller configuration from target_company.yml, seller_profile.yaml, or config.yaml."""
    for conf_name in ("target_company.yml", "seller_profile.yaml", "config.yaml"):
        p = PROJECT_ROOT / conf_name
        if p.exists():
            try:
                import yaml
                data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
                if "hiring_company" in data:
                    hc = data["hiring_company"]
                    if isinstance(hc, dict):
                        return {
                            "name": hc.get("name", "Braintrust"),
                            "domain": hc.get("domain", "braintrust.dev"),
                            "category": hc.get("product", "Enterprise AI Evaluation & Observability"),
                            "pain_point_type": hc.get("pain_point_type", "eval_tax"),
                        }
                    return {
                        "name": str(hc),
                        "domain": f"{str(hc).lower().replace(' ', '')}.com",
                        "category": data.get("product_summary", "Enterprise AI Platform"),
                        "pain_point_type": data.get("pain_point_type", "eval_tax"),
                    }
                if "seller" in data or "company_name" in data:
                    s_name = data.get("seller") or data.get("company_name", "Braintrust")
                    return {
                        "name": s_name,
                        "domain": data.get("domain", f"{s_name.lower().replace(' ', '')}.com"),
                        "category": data.get("category", "Enterprise Software Platform"),
                        "pain_point_type": data.get("pain_point_type", "eval_tax"),
                    }
            except Exception:
                pass
    return {
        "name": "Braintrust",
        "domain": "braintrust.dev",
        "category": "Enterprise AI Evaluation, Observability & Proxy Infrastructure",
        "pain_point_type": "eval_tax",
    }


def build_committee_outreach_tracks(
    company_name: str,
    dossier: Optional[dict] = None,
    coi_usd: Optional[int] = None,
) -> list[dict]:
    """
    Builds 3 multi-threaded Challenger + Command of the Message (CoM) enterprise outbound tracks
    dynamically mapped to the target account's verified Buying Committee:
      - Track 1: Operational Champion (Problem Owner / Workflow Friction)
      - Track 2: Executive Sponsor (Economic Buyer / Financial ROI & PBOs)
      - Track 3: Technical Peer (Evaluator / Architecture & Security Invariants)
    Dynamically adapts to ANY enterprise software category (AI Infrastructure, Security/Compliance,
    RevOps, Developer Tools, or Finance) based on the active seller and target account dossier.
    """
    dossier = dossier or {}
    domain = clean_domain(dossier.get("domain") or f"{re.sub(r'[^a-zA-Z0-9]', '', company_name.lower())}.com")
    seller = resolve_seller_config()
    seller_name = dossier.get("hiring_company") or dossier.get("seller_name") or seller.get("name", "Braintrust")

    pain_type = str(dossier.get("pain_point_type") or seller.get("pain_point_type", "eval_tax")).lower()
    product_wedge = dossier.get("ai_product") or dossier.get("product_summary") or f"{company_name}'s production platform"
    top_down_trigger = dossier.get("top_down_trigger") or f"{company_name}'s expansion across high-volume enterprise workflows"
    bottleneck = dossier.get("bottleneck_summary") or dossier.get("identified_pain")
    security_trigger = dossier.get("security_trigger") or "SOC2 Type II & Enterprise VPC"
    financial_tax = dossier.get("annual_eval_tax") or (f"${coi_usd:,}/yr in capacity drag" if coi_usd else "$240,000+/yr in senior builder capacity")

    # Dynamic ICP persona mapping based on pain point category
    if "eval" in pain_type or "ai" in pain_type or "prompt" in pain_type:
        default_dept = "AI Engineering & Platform"
        default_champ_title = dossier.get("champion") or "Lead Conversational AI Architect"
        default_eb_title = dossier.get("economic_buyer") or "VP of Product Engineering"
        default_eval_title = "Staff AI Platform Engineer"
        status_quo_pain = bottleneck or "bespoke Python evaluation scripts, manual spreadsheet grading, and prompt regression firefighting"
        solution_angle = f"automated CI/CD regression gates, LLM-as-a-judge scoring, and zero-data retention proxy caching"
    elif "compliance" in pain_type or "security" in pain_type or "soc2" in pain_type:
        default_dept = "Security & Compliance"
        default_champ_title = dossier.get("champion") or "Head of Information Security / CISO"
        default_eb_title = dossier.get("economic_buyer") or "Chief Financial Officer / VP Operations"
        default_eval_title = "Staff DevOps & Security Architect"
        status_quo_pain = bottleneck or "manual evidence screenshots, audit prep scrambles, and vendor security review friction"
        solution_angle = "continuous automated compliance, real-time trust center, and instant audit reporting"
    elif "revops" in pain_type or "data_hygiene" in pain_type or "gtm" in pain_type:
        default_dept = "Revenue Operations & GTM Systems"
        default_champ_title = dossier.get("champion") or "Head of GTM Engineering / Director of RevOps"
        default_eb_title = dossier.get("economic_buyer") or "Chief Revenue Officer / VP of Sales"
        default_eval_title = "CRM & Data Systems Architect"
        status_quo_pain = bottleneck or "manual enrichment waterfall debugging, unverified lead credit burn, and CRM drift"
        solution_angle = "zero-cost Tier-0 DNS/MX pre-filtering, automated waterfall routing, and idempotent CRM synchronization"
    else:
        default_dept = dossier.get("owning_department") or "Engineering & Platform"
        default_champ_title = dossier.get("champion") or f"Director of {default_dept}"
        default_eb_title = dossier.get("economic_buyer") or f"VP of {default_dept}"
        default_eval_title = "Principal Systems Architect"
        status_quo_pain = bottleneck or "manual operational friction and fragmented point-tool maintenance"
        solution_angle = "unified architecture automation, real-time telemetry, and enterprise governance"

    # Resolve POCs
    pocs = dossier.get("buying_committee_pocs", [])
    if len(pocs) < 3:
        pocs = [
            {
                "tier_label": "Tier 1: Operational Champion (Problem Owner)",
                "exact_title": default_champ_title,
                "full_name_or_search_query": dossier.get("champion_name") or default_champ_title,
            },
            {
                "tier_label": "Tier 2: Executive Sponsor (Economic Buyer)",
                "exact_title": default_eb_title,
                "full_name_or_search_query": dossier.get("eb_first_name") or dossier.get("economic_buyer") or default_eb_title,
            },
            {
                "tier_label": "Tier 3: Technical Peer (Architecture Evaluator)",
                "exact_title": default_eval_title,
                "full_name_or_search_query": default_eval_title,
            }
        ]

    tracks = []
    portfolio_url = f"https://jae-carter.vercel.app/?company={urllib.parse.quote(company_name)}"

    # Track 1: Operational Champion (Problem Owner — Workflow Friction & Tooling Breakdown)
    champ = pocs[0]
    champ_email, champ_first = resolve_poc_email_and_name(champ, domain, "champion")
    if not champ_email:
        try:
            from scripts.contact_verifier import resolve_hiring_committee_contact
            resolved = resolve_hiring_committee_contact(company_name, champ.get("exact_title", default_champ_title), domain=domain)
            if resolved.get("buyer_email"):
                champ_email = resolved["buyer_email"]
                champ_first = resolved["buyer_name"].split()[0]
        except Exception:
            pass

    champ_subject = f"{company_name}'s {product_wedge} / eliminating {status_quo_pain.split(',')[0].strip()}"
    champ_body = (
        f"Hi {champ_first} —\n\n"
        f"Noticed {company_name} is actively scaling {product_wedge}.\n\n"
        f"Usually when teams hit this velocity, the hidden drag isn't model or platform choice—it's {status_quo_pain}. Across peer engineering teams, senior builders lose 15–20 hours per sprint maintaining custom scripts and firefighting edge cases.\n\n"
        f"{seller_name} drops directly into your CI/CD and deployment pipeline to provide {solution_angle}—freeing your builders to focus on core product features.\n\n"
        f"We put together an architectural teardown tailored to {company_name}'s stack:\n"
        f"{portfolio_url}\n\n"
        f"Open to a 10-minute peer walkthrough on Thursday to see how peer teams handle this?"
    )
    tracks.append({
        "track_id": "tier1_operational_champion",
        "label": f"Track 1: {champ.get('tier_label', 'Operational Champion')} — {champ.get('exact_title', default_champ_title)}",
        "search_hint": champ.get("full_name_or_search_query", default_champ_title),
        "to_placeholder": champ_email,
        "subject": champ_subject,
        "body": champ_body
    })

    # Track 2: Executive Sponsor (Economic Buyer — Financial ROI & Business Velocity)
    eb = pocs[1]
    eb_email, eb_first = resolve_poc_email_and_name(eb, domain, "economic.buyer")
    if not eb_email and dossier.get("eb_first_name"):
        eb_first = dossier.get("eb_first_name")
    if not eb_email:
        try:
            from scripts.contact_verifier import resolve_hiring_committee_contact
            resolved = resolve_hiring_committee_contact(company_name, eb.get("exact_title", default_eb_title), domain=domain)
            if resolved.get("buyer_email"):
                eb_email = resolved["buyer_email"]
                eb_first = resolved["buyer_name"].split()[0]
        except Exception:
            pass

    eb_subject = f"{company_name} engineering velocity / reclaiming {financial_tax}"
    eb_body = (
        f"Hi {eb_first} —\n\n"
        f"Saw {company_name}'s strategic push around {top_down_trigger.split('.')[0].strip()}.\n\n"
        f"At {company_name}'s scale, maintaining unmonitored status-quo processes quietly drains an estimated {financial_tax} in senior capacity while introducing enterprise SLA risk.\n\n"
        f"{seller_name} gives {company_name}'s leadership board-level reliability, automated regression protection, and unified governance ({security_trigger}) out of the box.\n\n"
        f"I prepared an executive Deal Desk blueprint mapped to {company_name}'s motion:\n"
        f"{portfolio_url}\n\n"
        f"Worth a 12-minute executive briefing next week to review how peer leaders are unlocking this capacity?"
    )
    tracks.append({
        "track_id": "tier2_executive_sponsor",
        "label": f"Track 2: {eb.get('tier_label', 'Executive Sponsor')} — {eb.get('exact_title', default_eb_title)}",
        "search_hint": eb.get("full_name_or_search_query", default_eb_title),
        "to_placeholder": eb_email,
        "subject": eb_subject,
        "body": eb_body
    })

    # Track 3: Technical Peer (Architecture & Security Evaluator)
    peer = pocs[2]
    peer_email, peer_first = resolve_poc_email_and_name(peer, domain, "platform.architect")
    peer_subject = f"{security_trigger} architecture + zero-friction deployment for {company_name}"
    peer_body = (
        f"Hi {peer_first} —\n\n"
        f"Saw your team is overseeing core infrastructure and integration standards at {company_name}.\n\n"
        f"Quick architecture question: when evaluating tooling across {default_dept}, what is your team's stance on zero-data retention, hybrid VPC deployment, and sub-50ms latency SLAs?\n\n"
        f"We architected {seller_name} to deploy into enterprise environments in minutes with strict Pydantic/OpenAPI contracts and full {security_trigger} compliance.\n\n"
        f"I put together a live architecture workbench for {company_name} showing the exact pipeline topology and data plane:\n"
        f"{portfolio_url}\n\n"
        f"Mind if I get a quick peer sanity check on the architecture?"
    )
    tracks.append({
        "track_id": "tier3_technical_evaluator",
        "label": f"Track 3: {peer.get('tier_label', 'Technical Evaluator')} — {peer.get('exact_title', default_eval_title)}",
        "search_hint": peer.get("full_name_or_search_query", default_eval_title),
        "to_placeholder": peer_email,
        "subject": peer_subject,
        "body": peer_body
    })

    return tracks

    return tracks


def build_mailto_url(to_addr: str, subject: str, body: str) -> str:
    """Creates an RFC 6068 URL-encoded mailto: link for 1-click launching from Notion."""
    params = urllib.parse.urlencode(
        {"subject": subject, "body": body},
        quote_via=urllib.parse.quote
    )
    return f"mailto:{to_addr}?{params}"


def push_draft_to_imap(to_addr: str, subject: str, body: str, in_reply_to: str = None) -> str | None:
    """
    Appends an email directly into the user's IMAP Drafts folder ([Gmail]/Drafts or Drafts)
    so it is immediately ready in Apple Mail / Gmail without touching Finder.
    Returns the generated Message-ID.
    """
    if not IMAP_USER or not IMAP_PASSWORD:
        return None

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"{SENDER_NAME} <{IMAP_USER}>"
    msg["To"] = to_addr
    msg["Date"] = formatdate(localtime=True)
    msg_id = make_msgid(domain="hopefulheisenberg.local")
    msg["Message-ID"] = msg_id

    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to

    msg.set_content(body)

    try:
        mail = imaplib.IMAP4_SSL(IMAP_HOST)
        mail.login(IMAP_USER, IMAP_PASSWORD)

        # Detect standard Drafts mailbox name across Gmail / iCloud / Fastmail
        draft_folders = ['"[Gmail]/Drafts"', "Drafts", "INBOX.Drafts"]
        appended = False
        for folder in draft_folders:
            status, _ = mail.append(
                folder,
                "\\Draft",
                imaplib.Time2Internaldate(time.time()),
                msg.as_bytes()
            )
            if status == "OK":
                appended = True
                break

        mail.logout()
        if appended:
            return msg_id
    except Exception as e:
        print(f"[!] IMAP Drafts sync note: {e}")

    return None


def append_one_click_committee_outreach_to_notion(
    notion: Client,
    page_id: str,
    company_name: str,
    tracks: list[dict],
    imap_synced: bool
):
    """
    Appends a collapsible '1-Click Multi-Thread Committee Launchpad' section
    to the Notion Deal Room card with clickable mailto: buttons for all 3 personas.
    Maintains idempotency by removing prior versions if present.
    """
    # Remove existing launchpad block for clean idempotent refresh
    try:
        existing_blocks = notion.blocks.children.list(block_id=page_id, page_size=100).get("results", [])
        for b in existing_blocks:
            if b.get("type") == "heading_2":
                title_text = "".join([t.get("plain_text", "") for t in b["heading_2"].get("rich_text", [])])
                if "1-Click Multi-Thread Committee Launchpad" in title_text or "1-Click" in title_text:
                    try:
                        notion.blocks.delete(block_id=b["id"])
                    except Exception:
                        pass
    except Exception as e:
        print(f"[*] Note inspecting existing blocks on {page_id}: {e}")

    clean_dom = f"{re.sub(r'[^a-zA-Z0-9]', '', company_name.lower())}.com"
    logo_url = f"https://unavatar.io/{clean_dom}?fallback=https://www.google.com/s2/favicons?domain={clean_dom}&sz=128"

    children_blocks = [
        {
            "object": "block",
            "type": "callout",
            "callout": {
                "icon": {"type": "external", "external": {"url": logo_url}},
                "color": "blue_background",
                "rich_text": [{
                    "type": "text",
                    "text": {
                        "content": (
                            f"Zero-Finder Workflow Active:\n"
                            f"1. Click any 'Launch Pre-Filled Email' link below to pop open your default Mail client with that persona's exact Challenger/CoM copy.\n"
                            f"2. IMAP Drafts Sync: {'[SYNCED] Synced directly to your email Drafts folder' if imap_synced else '[INFO] Set IMAP_USER/IMAP_PASSWORD in .env to auto-push to Drafts'}.\n"
                            f"3. Auto-Advance: Once you send any email to @{company_name.lower().replace(' ', '')}'s domain, the IMAP Sent Watcher will automatically flip this card to '3. Outbound Sent' and start the 4-day Touch 2 timer."
                        )
                    }
                }]
            }
        }
    ]

    for t in tracks:
        mailto_url = build_mailto_url(t["to_placeholder"], t["subject"], t["body"])
        header_text = f"{t['label']}\nTarget Lookup: {t['search_hint']}\nSubject: {t['subject']}\n\n"

        children_blocks.append({
            "object": "block",
            "type": "callout",
            "callout": {
                "icon": {"type": "external", "external": {"url": logo_url}},
                "color": "gray_background",
                "rich_text": [
                    {"type": "text", "text": {"content": header_text}},
                    {
                        "type": "text",
                        "text": {
                            "content": f"Click to Launch {t['track_id'].replace('_', ' ').title()} Email in Mail App",
                            "link": {"url": mailto_url}
                        },
                        "annotations": {"bold": True, "underline": True, "color": "blue"}
                    },
                    {"type": "text", "text": {"content": f"\n\n{t['body']}"}}
                ]
            }
        })

    launchpad_toggle = {
        "object": "block",
        "type": "heading_2",
        "heading_2": {
            "is_toggleable": True,
            "rich_text": [{
                "type": "text",
                "text": {"content": "1-Click Multi-Thread Committee Launchpad (No Finder Needed)"}
            }],
            "children": children_blocks
        }
    }

    notion.blocks.children.append(block_id=page_id, children=[launchpad_toggle])
    print(f"[+] Added 1-Click Multi-Thread Committee Launchpad to Notion card for {company_name}.")


def send_via_macos_mail(to_addr: str, subject: str, body: str, auto_send: bool = True) -> bool:
    """
    Dispatches email via native Apple Mail using /usr/bin/osascript.
    If auto_send=True, sends immediately in background without popup.
    If auto_send=False, opens the draft visibly in Apple Mail for 1-click review.
    """
    clean_to = to_addr.replace('"', '\\"').strip()
    clean_sub = subject.replace('"', '\\"').strip()
    clean_body = body.replace('"', '\\"').replace("\n", "\\n")

    if auto_send:
        script = f'''
        tell application "Mail"
            set newMsg to make new outgoing message with properties {{subject:"{clean_sub}", content:"{clean_body}", visible:false}}
            tell newMsg
                make new to recipient at end of to recipients with properties {{address:"{clean_to}"}}
                send
            end tell
        end tell
        '''
    else:
        script = f'''
        tell application "Mail"
            activate
            set newMsg to make new outgoing message with properties {{subject:"{clean_sub}", content:"{clean_body}", visible:true}}
            tell newMsg
                make new to recipient at end of to recipients with properties {{address:"{clean_to}"}}
            end tell
        end tell
        '''

    try:
        res = subprocess.run(["/usr/bin/osascript", "-e", script], capture_output=True, text=True, timeout=10)
        return res.returncode == 0
    except Exception as e:
        print(f"[!] Warning executing Apple Mail osascript: {e}")
        return False


def send_clean_macos_notification(title: str, message: str, subtitle: Optional[str] = None) -> None:
    """Fires native macOS notification banner."""
    clean_title = title.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ").strip()
    clean_msg = message.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ").strip()
    script = f'display notification "{clean_msg}" with title "{clean_title}"'
    if subtitle:
        clean_sub = subtitle.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ").strip()
        script += f' subtitle "{clean_sub}"'
    script += ' sound name "default"'
    try:
        subprocess.run(["/usr/bin/osascript", "-e", script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
    except Exception:
        pass


def list_pending_deal_rooms() -> list[dict]:
    """Retrieves all opportunities currently in '2. Deal Room Generated' waiting for outbound dispatch."""
    if not NOTION_TOKEN or not NOTION_JOB_DB_ID:
        print("[-] NOTION_TOKEN or NOTION_JOB_DB_ID missing in .env.")
        return []

    notion = Client(auth=NOTION_TOKEN)
    res = notion.databases.query(
        database_id=NOTION_JOB_DB_ID,
        filter={"property": "Stage", "select": {"equals": "2. Deal Room Generated"}}
    )
    pages = res.get("results", [])
    opportunities = []

    for p in pages:
        p_id = p["id"]
        props = p.get("properties", {})
        title_objs = props.get("Company & Role", {}).get("title", [])
        title = title_objs[0].get("plain_text", "Untitled") if title_objs else "Untitled"
        score = props.get("Fit Score", {}).get("number", 0)
        buyer_objs = props.get("Economic Buyer", {}).get("rich_text", [])
        buyer = buyer_objs[0].get("plain_text", "Not Specified") if buyer_objs else "Not Specified"
        email = props.get("Buyer Email", {}).get("email") or ""
        archetype = (props.get("Role Archetype", {}).get("select") or {}).get("name", "GTM Engineer")
        url = p.get("url", "")

        opportunities.append({
            "page_id": p_id,
            "title": title,
            "fit_score": score or 0,
            "buyer": buyer,
            "email": email,
            "archetype": archetype,
            "notion_url": url,
            "page_obj": p,
        })

    opportunities.sort(key=lambda x: x["fit_score"], reverse=True)

    return opportunities


def dispatch_deal_room(
    opportunity: dict,
    auto_send: bool = True,
    dry_run: bool = False,
    sender_email: Optional[str] = None
) -> bool:
    """
    Executes outbound dispatch for a single Deal Room:
    1. Resolves recipient work email (using contact_verifier if unpopulated).
    2. Extracts Touch 1 Challenger copy.
    3. Dispatches via native macOS Apple Mail or IMAP Drafts.
    4. Advances Notion stage to '3. Outbound Sent'.
    5. Appends dispatch log & starts 4-day follow-up countdown.
    """
    notion = Client(auth=NOTION_TOKEN)
    p = opportunity.get("page_obj")
    page_id = opportunity["page_id"]
    props = p.get("properties", {}) if p else {}

    title = opportunity["title"]
    comp = title.split("—")[0].split("//")[0].split("-")[0].strip()

    buyer = opportunity.get("buyer") or ""
    to_email = opportunity.get("email") or ""
    archetype = opportunity.get("archetype") or "GTM Engineer"
    comp_url = props.get("Company URL", {}).get("url") or ""

    # 1. Resolve contact email if missing
    unverified_prefixes = ("hiring.manager@", "exec.sponsor@", "gtm.systems@", "revenue@", "head.of", "vp.of", "director.")
    if to_email and any(to_email.lower().startswith(p) for p in unverified_prefixes):
        to_email = ""

    if not to_email:
        from scripts.contact_verifier import resolve_hiring_committee_contact
        print(f"[*] Resolving hiring committee email for {comp}...")
        res = resolve_hiring_committee_contact(comp, archetype, domain=comp_url, existing_buyer=buyer)
        to_email = res["buyer_email"]
        buyer = res["buyer_display"]
        if not to_email:
            print(
                f"[!] [Bounce Guardrail] Blocking live dispatch for '{title}': "
                f"No verified human contact resolved yet ({buyer}). "
                f"Use the Buyer LinkedIn search link ({res.get('buyer_linkedin')}) to populate a real name first."
            )
            return False
        if not dry_run:
            notion.pages.update(
                page_id=page_id,
                properties={
                    "Economic Buyer": {"rich_text": [{"type": "text", "text": {"content": buyer}}]},
                    "Buyer Email": {"email": to_email},
                }
            )
            print(f"[+] Populated verified recipient in Notion: {buyer} <{to_email}>")

    # 2. Extract Touch 1 Challenger cold email copy
    cold_objs = props.get("Challenger Cold Email", {}).get("rich_text", [])
    raw_copy = "".join([t.get("plain_text", "") for t in cold_objs]).strip()

    subject = f"{comp} GTM systems architecture — Jae Carter"
    body = raw_copy

    if "Subject:" in raw_copy:
        parts = raw_copy.split("\n\n", 1)
        sub_line = parts[0].replace("Subject:", "").strip()
        if sub_line:
            subject = sub_line
        if len(parts) > 1:
            body = parts[1].strip()

    if dry_run:
        print(f"\n=======================================================")
        print(f" [DRY-RUN DISPATCH] {title}")
        print(f"=======================================================")
        print(f"To:      {buyer} <{to_email}>")
        print(f"Subject: {subject}")
        print(f"\nBody:\n{body[:350]}...\n")
        print(f"Action:  Would send Touch 1 & advance Stage -> '3. Outbound Sent'")
        print(f"=======================================================\n")
        return True

    # 3. Dispatch email
    sent = False
    # Try native Apple Mail first
    if sys.platform == "darwin":
        sent = send_via_macos_mail(to_email, subject, body, auto_send=auto_send)
        if sent:
            print(f"[+] Successfully dispatched Touch 1 via macOS Apple Mail to {to_email}")

    # Fallback to IMAP Drafts if Mail dispatch failed or on non-macOS
    if not sent:
        msg_id = push_draft_to_imap(to_email, subject, body)
        if msg_id:
            sent = True
            print(f"[+] Synced Touch 1 draft directly to IMAP Drafts folder ({msg_id})")

    if not sent:
        print(f"[!] Warning: Could not dispatch email via Apple Mail or IMAP.")
        return False

    # 4. Advance Notion card to '3. Outbound Sent'
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    notion.pages.update(
        page_id=page_id,
        properties={
            "Stage": {"select": {"name": "3. Outbound Sent"}}
        }
    )

    # 5. Append dispatch audit callout
    audit_callout = {
        "object": "block",
        "type": "callout",
        "callout": {
            "icon": {"type": "emoji", "emoji": "🚀"},
            "color": "green_background",
            "rich_text": [
                {"type": "text", "text": {"content": f"[OUTBOUND DISPATCHED]\n"}, "annotations": {"bold": True}},
                {"type": "text", "text": {"content": f"Touch 1 sent on {now_iso} to {to_email}.\nAutomated 4-day follow-up countdown initiated."}}
            ]
        }
    }
    try:
        notion.blocks.children.append(block_id=page_id, children=[audit_callout])
    except Exception:
        pass

    send_clean_macos_notification("[DISPATCH SENT] Touch 1 Delivered", f"{comp} — {buyer}")
    print(f"[+] Advanced '{title}' to '3. Outbound Sent'!")
    return True


def stage_committee_outreach(company_or_file: str, page_id: Optional[str] = None) -> dict:
    """Main entry point to stage 1-click Notion mailto buttons and push drafts to IMAP."""
    target_path = Path(company_or_file)
    if target_path.exists() and target_path.is_file():
        dossier = json.loads(target_path.read_text(encoding="utf-8"))
        company_name = dossier.get("company_name") or target_path.stem
    else:
        company_name = company_or_file
        clean_co = re.sub(r"[^\w\-]", "_", company_name)
        dossier_path = PROJECT_ROOT / f"output/dossiers/{clean_co}_dossier.json"
        dossier = json.loads(dossier_path.read_text(encoding="utf-8")) if dossier_path.exists() else {}

    clean_co = re.sub(r"[^\w\-]", "_", company_name)
    coi_metrics = calculate_deterministic_coi(dossier)
    tracks = build_committee_outreach_tracks(company_name, dossier, coi_metrics.get("annual_coi_usd"))

    staged_data = {
        "company_name": company_name,
        "clean_co": clean_co,
        "coi_usd": coi_metrics.get("annual_coi_usd"),
        "tracks": {}
    }

    print(f"\n=======================================================")
    print(f" 🎯 3-PERSONA BUYING COMMITTEE OUTREACH: {company_name.upper()}")
    print(f"=======================================================")
    for t in tracks:
        print(f"\n[{t['label']}]")
        print(f"Recipient Lookup: {t['search_hint']} <{t['to_placeholder'] or 'UNRESOLVED'}>")
        print(f"Subject         : {t['subject']}")
        print(f"Body Preview    :\n{t['body'][:220]}...")
        print(f"-------------------------------------------------------")

    imap_synced = False
    for t in tracks:
        if t["to_placeholder"]:
            msg_id = push_draft_to_imap(t["to_placeholder"], t["subject"], t["body"])
            t["message_id"] = msg_id
            staged_data["tracks"][t["track_id"]] = {
                "to": t["to_placeholder"],
                "subject": t["subject"],
                "message_id": msg_id
            }
            if msg_id:
                imap_synced = True
                print(f"[+] Synced {t['track_id']} directly to IMAP Drafts folder ({msg_id})")

    # Persist staged outreach metadata locally
    save_staged_outreach(clean_co.lower(), staged_data)

    if not NOTION_TOKEN or not NOTION_DEAL_DESK_DB_ID:
        print("\n[i] Note: NOTION_TOKEN or NOTION_DEAL_DESK_DB_ID omitted. Skipped Notion block append.")
        return staged_data

    try:
        notion = Client(auth=NOTION_TOKEN)
        target_page_id = page_id
        if not target_page_id:
            res = notion.databases.query(
                database_id=NOTION_DEAL_DESK_DB_ID,
                filter={"property": "Company Name", "title": {"contains": company_name}}
            )
            pages = res.get("results", [])
            if not pages:
                # Try fallback filter on 'Company & Role' if database uses older schema
                res = notion.databases.query(
                    database_id=NOTION_DEAL_DESK_DB_ID,
                    filter={"property": "Company & Role", "title": {"contains": company_name}}
                )
                pages = res.get("results", [])
            if not pages:
                print(f"[-] No Notion Deal Desk page found for {company_name}.")
                return staged_data
            target_page_id = pages[0]["id"]

        append_one_click_committee_outreach_to_notion(
            notion=notion,
            page_id=target_page_id,
            company_name=company_name,
            tracks=tracks,
            imap_synced=imap_synced
        )
    except Exception as exc:
        print(f"[!] Notion append note: {exc}")
    return staged_data


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="1-Click Multi-Thread Committee Outreach Dispatcher")
    parser.add_argument("target", nargs="?", default=None, help="Target company name or Page ID")
    parser.add_argument("--list", action="store_true", help="List all pending Deal Rooms in Stage 2 ready for dispatch")
    parser.add_argument("--dispatch", type=str, help="Company name or Page ID to dispatch Touch 1 for")
    parser.add_argument("--dispatch-all", action="store_true", help="Dispatch Touch 1 for all ready Deal Rooms")
    parser.add_argument("--max", type=int, default=5, help="Max opportunities to dispatch when using --dispatch-all (default: 5)")
    parser.add_argument("--review", action="store_true", help="Open draft visibly in Apple Mail instead of background auto-send")
    parser.add_argument("--dry-run", action="store_true", help="Preview dispatch payload without sending or advancing Notion")

    args = parser.parse_args()

    if args.list:
        pending = list_pending_deal_rooms()
        print(f"\n=======================================================")
        print(f" 📬 PENDING OUTBOUND OPPORTUNITIES ({len(pending)} READY IN STAGE 2)")
        print(f"=======================================================")
        for idx, item in enumerate(pending):
            print(f"[{idx+1}] {item['title']} (Fit: {item['fit_score']})")
            print(f"    Recipient : {item['buyer']} <{item['email'] or 'UNRESOLVED'}>")
            print(f"    Notion URL: {item['notion_url']}")
        print(f"=======================================================\n")
    elif args.dispatch:
        pending = list_pending_deal_rooms()
        target_item = None
        for item in pending:
            if args.dispatch.lower() in item["title"].lower() or args.dispatch == item["page_id"]:
                target_item = item
                break
        if target_item:
            dispatch_deal_room(target_item, auto_send=not args.review, dry_run=args.dry_run)
        else:
            print(f"[!] No matching Deal Room found in Stage 2 for '{args.dispatch}'. Use --list to see pending.")
    elif args.dispatch_all:
        pending = list_pending_deal_rooms()
        to_dispatch = pending[:args.max]
        print(f"[*] Dispatching {len(to_dispatch)} opportunities (max={args.max})...")
        for item in to_dispatch:
            dispatch_deal_room(item, auto_send=not args.review, dry_run=args.dry_run)
            time.sleep(1.0)
    elif args.target:
        stage_committee_outreach(args.target)
    else:
        # Default behavior: show pending list
        pending = list_pending_deal_rooms()
        print(f"\nFound {len(pending)} Deal Rooms ready for dispatch. Run with --list or --dispatch <company>.")
