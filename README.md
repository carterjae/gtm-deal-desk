# Autonomous Enterprise GTM Deal Desk & Revenue Architecture Daemon

> An event-driven, local-first Python & TypeScript revenue engineering daemon operating on macOS (Apple Silicon M4). Ingests live B2B target account signals, Clay enrichment waterfalls, and CRM webhooks into an idempotent **SQLite WAL durable queue (`daemon_worker.py`)**, synthesizes multi-framework enterprise Deal Desk blueprints (The Challenger Sale + Command of the Message + MEDDICC) via **Google Gemini 2.5 Pro / 2.5 Flash** with strict Pydantic v2 validation, verifies corporate mail exchange deliverability via native **DNS-over-HTTPS (DoH)**, and orchestrates governed Notion Deal Rooms + multi-threaded committee outreach.

- **Live Interactive Deal Desk & System Topology:** [https://jae-carter.vercel.app](https://jae-carter.vercel.app)
- **Author:** [Jae Carter](https://www.linkedin.com/in/jae-carter) (`AI Systems & GTM Engineer`)

---

## System Architecture

```text
  [Clay Waterfall / CRM Webhook / CLI Staging / HTTP POST :8000]
                               │
                               ▼
               +-------------------------------+
               |       webhook_server.py       |  <-- Non-blocking ACK (<50ms), SHA-256 Payload Hash,
               +-------------------------------+      Atomic SQLite WAL Queue Enqueue
                               │
                               ▼
               +-------------------------------+
               |       daemon_worker.py        |  <-- SQLite WAL Consumer, Atomic Locking,
               +-------------------------------+      Exponential Backoff, Dead-Letter Recovery
                               │
                               ▼
               +-------------------------------+
               |    synthesizer.py (Gemini)    |  <-- Gemini 2.5 Pro / Flash Structured Outputs,
               +-------------------------------+      Strict Pydantic v2 Schema Enforcement
                               │
                               ▼
               +-------------------------------+
               |   contact_verifier.py (DoH)   |  <-- $0.00 Tier-0 DNS (MX/SPF/DMARC/DoH),
               +-------------------------------+      Headcount-Tiered GTM Buyer Resolution
                               │
                               ▼
               +-------------------------------+
               |  notion_client.py / DB Push   |  <-- Recursive 1,990-Char AST Block Chunking,
               +-------------------------------+      Notion Deal Desk Sub-Pages & Properties
                               │
                               ▼
               +-------------------------------+
               |    outreach_dispatcher.py     |  <-- 3-Persona Multi-Threaded Outreach (Champion,
               +-------------------------------+      Economic Buyer, Technical Peer) + IMAP Drafts
```

---

## Core Engineering Invariants

1. **Durable SQLite WAL Queue & SHA-256 Idempotency (`daemon_worker.py`):** Decouples HTTP webhook ingestion from heavy LLM synthesis. Enforces SHA-256 payload deduplication, atomic state transitions (`PENDING` -> `PROCESSING` -> `COMPLETED`), and exponential backoff on Gemini/Notion rate limits.
2. **Tier-0 DNS/MX/DoH & Headcount-Tiered Contact Resolution (`scripts/contact_verifier.py`):** Zero-cost deliverability validation via native POSIX `dig`/`nslookup` + Google DNS-over-HTTPS (`https://dns.google/resolve?name={domain}&type=MX`). Automatically identifies verified GTM/RevOps decision-makers (VP RevOps, CRO, Head of GTM Systems) without emailing Fortune 500 CEOs or bouncing generic role-placeholder emails (`hiring.manager@`).
3. **Strict Pydantic v2 Schema Enforcement (`schema.py`, `synthesizer.py`):** Uses the official `google-genai` SDK with `response_mime_type="application/json"` and strict `response_schema` enforcement to guarantee 100% valid JSON, eliminating hallucinated fields and schema drift across MEDDICC, Command of the Message, and financial ROI calculations.
4. **Deterministic 1,990-Character Notion AST Chunking (`notion_client.py`):** Recursively chunks rich-text blocks and bulleted lists below Notion's 2,000-character API limit, guaranteeing zero `HTTP 400` payload drops across complete 9-section enterprise Deal Desk dossiers.
5. **Multi-Threaded Buying Committee Dispatcher (`scripts/outreach_dispatcher.py`):** Synthesizes 3 distinct multi-threaded outreach tracks tailored to the target enterprise:
   - **Track 1:** Operational Champion (VP / Director of Revenue Operations or GTM Systems)
   - **Track 2:** Executive Sponsor (Chief Revenue Officer or VP of Sales)
   - **Track 3:** Technical Peer (Principal Architect or Lead GTM Systems Engineer)
   Pushes plain-text, linkless first touches directly into your IMAP Drafts folder (`[Gmail]/Drafts` or `Drafts`) with clickable `mailto:` triggers.

---

## Repository Structure

```text
.
├── config.yaml                     # Active seller profile, ICP rules, and exclusion lists
├── target_company.yml              # Active target account configuration & pain-type definitions
├── run.py                          # CLI orchestrator: Validates JSON, runs synthesis, and syncs to Notion
├── synthesizer.py                  # Gemini 2.5 Pro / Flash multi-framework Deal Desk synthesis engine
├── schema.py                       # Pydantic v2 input/output data contracts (ClayInputData, DealDeskSynthesis)
├── notion_client.py                # Notion REST API client (AST block formatting, chunking & property mapping)
├── notion_poller.py                # Bi-directional Notion Deal Desk database poller and state synchronizer
├── daemon_worker.py                # SQLite WAL durable queue & autonomous background worker daemon
├── webhook_server.py               # Fast non-blocking HTTP webhook daemon (/webhook/clay-ingest & /api/gtm-audit)
├── models/
│   ├── intelligence.py             # Pydantic models for company dossiers, signal metadata & ICP scoring
│   └── gtme_deliverable.py         # Structured output specifications for Stage-5 GTM blueprints
├── scripts/
│   ├── contact_verifier.py         # Tier-0 DNS/MX/DoH deliverability verifier & executive contact resolver
│   └── outreach_dispatcher.py      # Multi-threaded Buying Committee outreach synthesizer & IMAP drafts engine
├── templates/                      # Jinja2 markdown templates compiled into Notion AST blocks
└── staging/                        # Validated sample target account payloads (Intercom, Ramp, Deel, Checkr)
```

---

## Quickstart

```bash
# 1. Clone & create virtual environment
git clone https://github.com/carterjae/gtm-deal-desk.git
cd gtm-deal-desk
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. Configure environment
cp .env.example .env
# Add GEMINI_API_KEY, NOTION_TOKEN, and NOTION_DEAL_DESK_DB_ID
```

### 1. Synthesize an Enterprise Deal Desk Account Plan via CLI
```bash
# Dry-run synthesis on a sample target account (e.g. Intercom, Ramp, Deel)
python3 run.py staging/sample_target.json --dry-run

# Run full live synthesis and push to Notion Deal Desk
python3 run.py staging/sample_target.json
```

### 2. Verify Corporate Mail Exchangers & Decision-Maker via DNS-over-HTTPS
```bash
python3 scripts/contact_verifier.py --name "Varun Anand" --domain "clay.com" --no-smtp
```

### 3. Generate Multi-Threaded Buying Committee Outreach
```bash
python3 scripts/outreach_dispatcher.py
```

### 4. Start the Non-Blocking Webhook Daemon
```bash
python3 webhook_server.py
```

---

## License
MIT License. Crafted with frontline enterprise GTM rigor by [Jae Carter](https://github.com/carterjae).
