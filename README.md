# Autonomous GTM Deal Desk & Signal Ingestion Daemon

> An event-driven, local-first Python daemon that ingests enriched B2B target accounts from Clay webhooks, synthesizes multi-framework executive deal intelligence (Challenger Sale + Command of the Message + MEDDICC) via Google GenAI (`gemini-2.5-flash`), and compiles structured Deal Desk rows + sub-pages in Notion.

---

## Architecture Topology

```text
  [Clay Waterfall / HTTP Trigger]
                 |
                 v  (HTTP POST :8000)
       +--------------------+
       | webhook_receiver.py|  <-- Non-blocking <5ms ACK, Net-New Logo Filter,
       +--------------------+      Zero-Credit ROI Math, JSON Staging
                 |
                 v
       +--------------------+
       |   gtm_engine.py    |  <-- Pydantic v2 Strict Schema Validation
       +--------------------+      Google GenAI SDK (Gemini 2.5 Structured Output)
                 |
                 v
       +--------------------+
       |  Jinja2 AST Blocks |  <-- Markdown to Notion AST Parser,
       +--------------------+      1,990c Rich-Text Chunker (Zero HTTP 400 Faults)
                 |
                 v  (REST API)
       +--------------------+
       |  Notion Deal Desk  |  <-- Idempotent Upsert, Sub-page Dossiers,
       +--------------------+      Zero Emojis, Governed Commercial Properties
```

---

## Core Technical Invariants

1. **Non-Blocking Ingestion (<5ms ACK SLA):** Incoming Clay payloads are immediately validated, staged to disk, and dispatched to background workers. The HTTP caller receives an instant `202 Accepted` to prevent Clay webhook timeouts during burst batch runs.
2. **Strict Pydantic Schema Enforcement:** Uses the official `google-genai` SDK with `response_mime_type="application/json"` and strict `DealIntelligenceSchema` Pydantic models. Zero conversational filler or hallucinated formatting.
3. **Deterministic 1,990-Character AST Chunking:** The Notion API enforces a hard 2,000-character ceiling per `rich_text` block. The custom parser recursively chunks arbitrary-length markdown blocks at 1,990 characters, preventing silent payload drops and HTTP 400 validation failures.
4. **Net-New Logo Exclusion Guardrail:** Evaluates accounts against an exclusion list (`excluded_customers` in `config.yaml`) prior to synthesis, stopping wasted enrichment spend and duplicate rep outreach.
5. **Zero-Credit Deterministic ROI Modeling:** Calculates acute operational drag metrics (e.g., engineering hours lost to manual regression testing or compliance debt) based on headcount signals before calling external LLMs.

---

## Quickstart

### 1. Prerequisites
- Python 3.10+
- Google Gemini API Key
- Notion Integration Token & Database ID

### 2. Installation
```bash
git clone https://github.com/carterjae/gtm-deal-desk.git
cd gtm-deal-desk
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Environment Setup
Copy the sample environment file and provide your API credentials:
```bash
cp .env.example .env
```
Edit `.env`:
```ini
GEMINI_API_KEY=your_gemini_api_key
NOTION_TOKEN=secret_your_notion_integration_token
NOTION_DEAL_DESK_DB_ID=your_notion_database_id
PORT=8000
```

### 4. Start the Ingestion Daemon
```bash
python webhook_receiver.py
```
By default, the daemon binds to `http://0.0.0.0:8000` listening for `POST /webhook/clay-ingest`.

### 5. Expose Daemon via Tunnel (For Live Clay Webhooks)
```bash
npx localtunnel --port 8000
# Target webhook URL in Clay: https://<subdomain>.loca.lt/webhook/clay-ingest
```

---

## Local Testing & Verification

### Simulate Clay Ingestion via cURL
Trigger a test payload against the running daemon:
```bash
curl -X POST http://localhost:8000/webhook/clay-ingest \
  -H "Content-Type: application/json" \
  -d '{
    "company_name": "Ramp",
    "domain": "ramp.com",
    "github_org": "ramp",
    "ai_engineer_count": 85,
    "has_job_post": true,
    "pain_type": "eval_tax"
  }'
```

### Standalone Synthesis & Push Run
Run the synthesis engine directly on a staged target:
```bash
python gtm_engine.py --file staging/ramp.json
```

---

## Project Structure

```text
.
├── config.yaml              # Seller positioning, target criteria, & exclusion rules
├── gtm_engine.py            # Gemini 2.5 structured synthesis & Notion API block compiler
├── webhook_receiver.py      # Non-blocking HTTP daemon & Clay webhook receiver
├── requirements.txt         # Pydantic, google-genai, Jinja2, requests
├── .env.example             # Environment variable template
├── staging/                 # Validated JSON payloads ready for synthesis
└── templates/               # Jinja2 markdown templates compiled into Notion blocks
    ├── challenger_thesis.j2 # "Why Now" commercial reframes
    ├── com_matrix.j2        # Command of the Message (Current -> Negative -> PBO)
    └── meddicc_risks.j2     # Governance & economic buyer qualification
```

---

## License
MIT License. Created by [Jae Carter](https://linkedin.com/in/jaecarter).
