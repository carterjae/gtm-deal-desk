# Antigravity GTM Agent Rules
- **Model Engine:** Powered by Gemini (`gemini-3.8-flash`) via Google GenAI SDK (`google-genai`).
- **Workflow:** Ingest structured Clay payloads (`webhook_receiver.py`) -> run Gemini (`gemini-3.8-flash`) structured synthesis validated via Pydantic `DealIntelligenceSchema` -> render Jinja2 templates (`challenger_thesis.j2`, `com_matrix.j2`, `meddicc_risks.j2`) into structured Notion blocks -> execute Notion push script (`gtm_engine.py`).
- **Tone & Quality Bar:** Peer-to-peer executive sales advisor; strictly avoid boilerplate sales clichés, generic summaries, or emojis.
- **Guardrails:**
  - Never use emojis anywhere in Notion blocks, headers, callouts, or templates.
  - Never push raw, unformatted AI text to Notion. Always validate against `DealIntelligenceSchema` (`challenger_thesis`, `executive_insight`, `meddic_red_flags`, `estimated_arr`, `meddic_score` in `["Low Risk", "Moderate Risk", "High Risk"]`).
  - Always ensure the "Why Now" commercial thesis is anchored in real trigger events (funding, executive hires, or tech stack shifts) retrieved from Clay.
  - Enforce the Net-New Logo Exclusion Filter (`excluded_customers` in `config.yaml`) and maintain zero-login friction in Notion (never expose private Clay workspace URLs).
