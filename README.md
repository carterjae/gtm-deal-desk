# Autonomous GTM Deal Desk & Revenue Architecture Daemon

> An event-driven, local-first Python & TypeScript revenue engineering architecture operating on macOS (Apple Silicon M4). Ingests live B2B market signals, Clay enrichment waterfalls, and ATS/EDGAR feeds into a **SQLite WAL durable queue (`daemon_worker.py`)**, synthesizes multi-framework Deal Desk blueprints (Challenger Sale + Command of the Message + MEDDICC) via **Google Gemini 2.5 Pro / 2.5 Flash** with strict Pydantic v2 validation, and orchestrates governed Notion Deal Rooms + multi-threaded outbound execution.

- **Live Interactive Deal Desk & System Topology:** [https://jae-carter.vercel.app](https://jae-carter.vercel.app)
- **Author:** [Jae Carter](https://www.linkedin.com/in/jae-carter) (`AI Systems & GTM Engineer`)

---

## System Architecture (`SYS.01` – `SYS.04`)

```text
  [Clay Waterfall / ATS & SEC EDGAR Signals / HTTP Trigger]
                             |
                             v  (HTTP POST :8000 / LaunchAgent)
             +-------------------------------+
             |  webhook_server.py / Scout    |  <-- Fast ACK (<50ms), SHA-256 Payload Dedup,
             +-------------------------------+      Two-Stage 5-Track Scorer (65+ Boards)
                             |
                             v
             +-------------------------------+
             |       daemon_worker.py        |  <-- SQLite WAL Durable Queue, Atomic Locking,
             +-------------------------------+      Exponential Backoff, Crash Recovery
                             |
              +--------------+--------------+
              |                             |
              v (Mode A: Enterprise GTM)    v (Mode B: Work-Trial & RevOps Engine)
    +-------------------+         +-------------------+
    |   gtm_engine.py   |         |   job_engine.py   |  <-- Gemini 2.5 Pro / Flash Structured JSON,
    +-------------------+         +-------------------+      Pydantic Schema & Entity Eval Guardrails
              |                             |
              +--------------+--------------+
                             |
                             v
             +-------------------------------+
             |   contact_verifier.py / DNS   |  <-- $0.00 Tier-0 DNS (MX/SPF/DMARC/DoH),
             +-------------------------------+      Headcount-Tiered Buyer Resolution
                             |
                             v  (Notion REST API + Resend / IMAP)
             +-------------------------------+
             |  Notion Deal Desk & Outreach  |  <-- 1,990-Char AST Chunking, ATS PDF Compiler,
             +-------------------------------+      Multi-Threaded Challenger/CoM Dispatch
```

---

## Core Engineering Invariants

1. **Durable SQLite WAL Queue & SHA-256 Idempotency (`daemon_worker.py`):** Decouples HTTP webhook ingestion from heavy LLM synthesis and PDF compilation. Enforces SHA-256 payload deduplication, atomic state transitions (`PENDING` -> `PROCESSING` -> `COMPLETED`), and exponential backoff on rate limits.
2. **Two-Stage 5-Track Scoring Engine (`scripts/morning_job_scout.py`):** Polls `65+` enterprise Greenhouse/Ashby boards and Google Search Grounding across 5 core tracks (`GTM Engineer`, `AI Operations / Technical RevOps`, `AI Revenue Architect / RevOps`, `Growth Engineer`, `Founding / Outbound AE`). Applies a Stage-1 title/location filter followed by a Stage-2 JD body stack & YoE rubric.
3. **Strict Pydantic v2 Schema & Evaluation Guardrails (`job_engine.py`, `gtm_engine.py`):** Uses the `google-genai` SDK (`response_mime_type="application/json"`) with deterministic post-generation audits checking for entity hallucinations, forbidden phrases, and ATS keyword gaps (`scripts/compile_resume.py`).
4. **Tier-0 DNS/MX/DoH & Headcount-Tiered Contact Resolution (`scripts/contact_verifier.py`):** Verifies domain mail gateways (`Google Workspace ASPMX`, `Microsoft 365`, `Proofpoint`, `Mimecast`) via POSIX `dig`/`nslookup` + Google DNS-over-HTTPS (`dns.google`) and resolves headcount-appropriate GTM/RevOps decision-makers without fabricating role-placeholder emails.
5. **Deterministic 1,990-Character Notion AST Chunking:** Recursively chunks rich-text blocks below Notion's 2,000-character API ceiling, guaranteeing zero `HTTP 400` payload drops across `45+` synthesized 9-section Deal Desk blueprints.

---

## Repository Structure

```text
.
├── config.yaml                     # Seller positioning, ICP criteria, & exclusion rules
├── daemon_worker.py                # SQLite WAL durable queue & autonomous background worker
├── webhook_server.py               # Dual-mode HTTP ingestion daemon (/webhook/clay-ingest & /webhook/job-ingest)
├── gtm_engine.py                   # Mode A: Enterprise Account Plan & Deal Desk synthesizer
├── job_engine.py                   # Mode B: 5-Track Fit Scorer, XYZ Resume Tailor, & Work-Trial synthesizer
├── scripts/
│   ├── morning_job_scout.py        # Daily 65+ board ATS & Google Search Grounding scout + Two-Stage Scorer
│   ├── contact_verifier.py         # Tier-0 DNS/MX/DoH + SMTP + Gemini Search Grounding contact resolver
│   ├── outreach_dispatcher.py      # Multi-threaded Challenger/CoM outreach & IMAP Drafts synchronizer
│   └── compile_resume.py           # Deterministic ATS keyword guardrail & WeasyPrint/ReportLab PDF compiler
├── templates/                      # Jinja2 markdown templates compiled into Notion AST blocks
└── staging/                        # Sample validated target payloads
```

---

## Quickstart

```bash
git clone https://github.com/carterjae/gtm-deal-desk.git
cd gtm-deal-desk
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

### Run the Autonomous Scout & Durable Worker
```bash
# 1. Discover & score live roles across 65+ ATS boards
python3 scripts/morning_job_scout.py --max 8

# 2. Start the SQLite WAL durable queue worker & Notion poller
python3 daemon_worker.py

# 3. Run Tier-0 DNS/MX + decision-maker verification for a target domain
python3 scripts/contact_verifier.py --name "Varun Anand" --domain "clay.com"
```

---

## License
MIT License. Built by [Jae Carter](https://www.linkedin.com/in/jae-carter).
