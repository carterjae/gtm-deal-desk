#!/usr/bin/env python3
"""
webhook_receiver.py — Automated Clay Webhook Receiver & Staging Ingestor
Listens for incoming enriched Clay payloads (or runs in --simulate mode),
enforces the Net-New Logo Exclusion Guardrail, computes 0-credit Rational Drowning
ROI math, and stages validated JSON payloads into gtm-deal-desk/staging/.
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import yaml

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.yaml"
STAGING_DIR = BASE_DIR / "staging"


def load_env_and_config():
    # Load root .env if present
    for env_candidate in [BASE_DIR / ".env", BASE_DIR.parent / ".env"]:
        if env_candidate.exists():
            with open(env_candidate, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

    if not CONFIG_PATH.exists():
        print(f"[!] Error: {CONFIG_PATH} not found.")
        sys.exit(1)

    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        raw = f.read()

    # Expand ${VAR} using environment variables (supporting NOTION_TOKEN fallback for NOTION_API_KEY)
    if "NOTION_API_KEY" not in os.environ and "NOTION_TOKEN" in os.environ:
        os.environ["NOTION_API_KEY"] = os.environ["NOTION_TOKEN"]

    expanded = os.path.expandvars(raw)
    return yaml.safe_load(expanded)


def compute_zero_credit_roi(ai_eng_count: int, pain_type: str = "eval_tax") -> dict:
    """Deterministic 0-credit Rational Drowning math."""
    if pain_type == "eval_tax":
        wasted_ftes = round(ai_eng_count * 0.30, 1)
        annual_cost = int(wasted_ftes * 240000)
    elif pain_type == "plumbing_tax":
        wasted_ftes = round(ai_eng_count * 0.35, 1)
        annual_cost = int(wasted_ftes * 240000)
    else:
        wasted_ftes = round(ai_eng_count * 0.25, 1)
        annual_cost = int(wasted_ftes * 180000)

    return {
        "wasted_ftes": wasted_ftes,
        "annual_eval_tax_num": annual_cost,
        "annual_eval_tax": f"${annual_cost:,}/yr ({wasted_ftes} Senior AI FTEs)",
        "estimated_arr": max(45000, min(150000, ai_eng_count * 4000)),
    }


def normalize_and_stage_payload(raw_payload: dict, config: dict) -> Path:
    """Validates net-new status, enriches with 0-credit ROI math, and writes to staging/."""
    STAGING_DIR.mkdir(parents=True, exist_ok=True)

    company_name = (
        raw_payload.get("company_name")
        or raw_payload.get("company")
        or "Unknown"
    ).strip()
    domain = raw_payload.get("domain") or f"https://{company_name.lower().replace(' ', '')}.com"
    clean_domain = domain.replace("https://", "").replace("http://", "").split("/")[0].lower()

    excluded = [d.lower() for d in config.get("hiring_company", {}).get("excluded_customers", [])]
    if clean_domain in excluded:
        raise ValueError(
            f"NET-NEW GUARDRAIL BLOCK: '{company_name}' ({clean_domain}) is an existing customer/partner of "
            f"{config['hiring_company']['name']}."
        )

    ai_eng_count = int(raw_payload.get("ai_eng_count") or raw_payload.get("icp_headcount") or 15)
    pain_type = config.get("hiring_company", {}).get("pain_point_type", "eval_tax")
    roi = compute_zero_credit_roi(ai_eng_count, pain_type)

    staged = {
        "company_name": company_name,
        "domain": domain if domain.startswith("http") else f"https://{domain}",
        "hiring_company": config.get("hiring_company", {}).get("name", "Braintrust"),
        "ai_product": raw_payload.get("ai_product", f"{company_name} AI Agent"),
        "ai_eng_count": ai_eng_count,
        "wasted_ftes": roi["wasted_ftes"],
        "annual_eval_tax": roi["annual_eval_tax"],
        "estimated_arr": int(raw_payload.get("estimated_arr") or roi["estimated_arr"]),
        "stage": raw_payload.get("stage", "Prospecting"),
        "meddic_score": raw_payload.get("meddic_score", "Strong - Validated Pain"),
        "eb_first_name": raw_payload.get("eb_first_name", "Engineering Lead"),
        "economic_buyer": raw_payload.get("economic_buyer", "VP of Product Engineering"),
        "champion": raw_payload.get("champion", "Lead AI Systems Architect"),
        "oss_hiring_signal": raw_payload.get(
            "oss_hiring_signal",
            f"Active AI Engineering Hiring (~{ai_eng_count} AI Engrs) + Production LLM Eval Bottleneck",
        ),
        "security_trigger": raw_payload.get("security_trigger", "SOC2 + Regulated"),
        "top_down_trigger": raw_payload.get(
            "top_down_trigger",
            f"{company_name} is scaling {raw_payload.get('ai_product', 'autonomous AI workflows')} across enterprise accounts where silent prompt regressions directly impact customer retention and SLA guarantees.",
        ),
        "bottleneck_summary": raw_payload.get(
            "bottleneck_summary",
            "manual spreadsheet grading, bespoke Python evaluation scripts, and vibes-based regression firefighting",
        ),
        "meddic_red_flags": raw_payload.get(
            "meddic_red_flags",
            f"Internal DIY Status Quo: {company_name}'s senior engineers maintaining bespoke eval scripts in-house instead of standardizing on an automated CI/CD evaluation harness.",
        ),
        "ingested_at": datetime.now(timezone.utc).isoformat(),
    }

    slug = re.sub(r"[^a-z0-9]+", "_", company_name.lower()).strip("_")
    target_file = STAGING_DIR / f"{slug}.json"
    sample_file = STAGING_DIR / "sample_target.json"

    with open(target_file, "w", encoding="utf-8") as f:
        json.dump(staged, f, indent=2)
    with open(sample_file, "w", encoding="utf-8") as f:
        json.dump(staged, f, indent=2)

    print(f"[+] Staged '{company_name}' -> {target_file} (and updated {sample_file.name})")
    return target_file


class ClayWebhookHandler(BaseHTTPRequestHandler):
    config = {}
    auto_sync = False

    def do_POST(self):
        expected_secret = self.config.get("clay", {}).get("webhook_secret", "")
        if expected_secret and not expected_secret.startswith("${"):
            incoming_secret = self.headers.get("X-Clay-Webhook-Secret", "")
            if incoming_secret != expected_secret:
                self.send_response(401)
                self.end_headers()
                self.wfile.write(b'{"error": "Unauthorized webhook secret"}')
                return

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8")

        try:
            payload = json.loads(body)
            records = payload if isinstance(payload, list) else [payload]
            staged_files = []
            for rec in records:
                path = normalize_and_stage_payload(rec, self.config)
                staged_files.append(str(path.name))

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "staged", "files": staged_files}).encode("utf-8"))
        except Exception as e:
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Clay Webhook Receiver & Staging Ingestor")
    parser.add_argument("--simulate", action="store_true", help="Simulate a Clay webhook batch for Braintrust")
    parser.add_argument("--port", type=int, default=8787, help="Port to listen on")
    args = parser.parse_args()

    cfg = load_env_and_config()

    if args.simulate:
        # 5 Verified Net-New Accounts for Braintrust (Zero overlap with Notion, Dropbox, Zapier, Airtable, Stripe)
        braintrust_batch = [
            {
                "company_name": "Intercom",
                "domain": "https://intercom.com",
                "ai_product": "Fin AI Agent",
                "ai_eng_count": 20,
                "estimated_arr": 85000,
                "stage": "Prospecting",
                "meddic_score": "Strong - Validated Pain",
                "eb_first_name": "Darragh",
                "economic_buyer": "Darragh (VP of Product Engineering)",
                "champion": "Lead Conversational AI Architect",
                "oss_hiring_signal": "Scaling Fin AI across 25K+ customers + Hiring Senior Conversational Eval Engineers",
                "security_trigger": "SOC2 + Regulated",
                "meddic_red_flags": "Status Quo DIY: Internal Python eval harnesses built for Fin v1 that struggle to scale to multi-step tool calling.",
            },
            {
                "company_name": "Ramp",
                "domain": "https://ramp.com",
                "ai_product": "Ramp Intelligence",
                "ai_eng_count": 16,
                "estimated_arr": 75000,
                "stage": "Discovery",
                "meddic_score": "Strong - Validated Pain",
                "eb_first_name": "Karim",
                "economic_buyer": "Karim (VP of Engineering & Platform)",
                "champion": "Staff AI Systems Architect",
                "oss_hiring_signal": "Autonomous expense policy & OCR agents + Zero tolerance for financial ledger hallucinations",
                "security_trigger": "Enterprise VPC",
                "meddic_red_flags": "Strict VPC/PII security review required before financial receipt traces can be logged.",
            },
            {
                "company_name": "Ashby",
                "domain": "https://ashbyhq.com",
                "ai_product": "Ashby AI Recruiting Copilot",
                "ai_eng_count": 8,
                "estimated_arr": 45000,
                "stage": "Prospecting",
                "meddic_score": "Strong - Validated Pain",
                "eb_first_name": "Abhik",
                "economic_buyer": "Abhik Pramanik (Co-Founder & VP of Engineering)",
                "champion": "Lead AI Platform Engineer",
                "oss_hiring_signal": "Scaling interview debrief summarization & candidate scoring where EEOC compliance is mandatory",
                "security_trigger": "SOC2 + Regulated",
                "meddic_red_flags": "Lean 8-engineer AI squad currently grading prompt outputs manually in spreadsheets.",
            },
            {
                "company_name": "Deel",
                "domain": "https://deel.com",
                "ai_product": "Deel AI Global Compliance Agent",
                "ai_eng_count": 22,
                "estimated_arr": 95000,
                "stage": "Prospecting",
                "meddic_score": "Moderate Risk",
                "eb_first_name": "Courtney",
                "economic_buyer": "Courtney (VP of Product Engineering)",
                "champion": "Staff AI Platform Architect",
                "oss_hiring_signal": "150+ country labor law reasoning where a single hallucinated contract clause creates legal liability",
                "security_trigger": "HIPAA / Zero Egress",
                "meddic_red_flags": "Multi-region GDPR data residency requirements across EU and US clusters.",
            },
            {
                "company_name": "Checkr",
                "domain": "https://checkr.com",
                "ai_product": "Checkr Trust & Verification AI",
                "ai_eng_count": 15,
                "estimated_arr": 65000,
                "stage": "Prospecting",
                "meddic_score": "Strong - Validated Pain",
                "eb_first_name": "Ketan",
                "economic_buyer": "Ketan (VP of Engineering & Data)",
                "champion": "Staff AI Platform Engineer",
                "oss_hiring_signal": "Automated background adjudication workflows requiring FCRA-grade deterministic accuracy",
                "security_trigger": "SOC2 + Regulated",
                "meddic_red_flags": "FCRA compliance team requires deterministic auditability on every LLM-as-a-judge scorer.",
            },
        ]
        for account in braintrust_batch:
            normalize_and_stage_payload(account, cfg)
        # Keep Intercom as sample_target.json primary demo
        normalize_and_stage_payload(braintrust_batch[0], cfg)
    else:
        host = cfg.get("clay", {}).get("webhook_host", "127.0.0.1")
        port = args.port or int(cfg.get("clay", {}).get("webhook_port", 8787))
        ClayWebhookHandler.config = cfg
        server = HTTPServer((host, port), ClayWebhookHandler)
        print(f"[*] Clay Webhook Receiver listening on http://{host}:{port}")
        server.serve_forever()
