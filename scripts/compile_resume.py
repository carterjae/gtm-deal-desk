#!/usr/bin/env python3
"""
Compiles tailored Google-XYZ resume summary & experience objects grounded in master_career_profile.json,
enforces the Deterministic ATS Keyword Gap Guardrail (Upgrade #4), and physically writes BOTH:
  - Clean Markdown (.md): os.path.abspath(f"output/resumes/Jae_Carter_{clean_company}_{clean_role}.md")
  - ATS-Optimized PDF (.pdf): os.path.abspath(f"output/resumes/Jae_Carter_{clean_company}_{clean_role}.pdf")
using Markdown + WeasyPrint + custom ATS CSS styling.
"""

import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
import markdown

if "/opt/homebrew/lib" not in os.environ.get("DYLD_FALLBACK_LIBRARY_PATH", ""):
    existing_dyld = os.environ.get("DYLD_FALLBACK_LIBRARY_PATH", "")
    os.environ["DYLD_FALLBACK_LIBRARY_PATH"] = (
        f"/opt/homebrew/lib:{existing_dyld}" if existing_dyld else "/opt/homebrew/lib"
    )

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROFILE_PATH = PROJECT_ROOT / "master_career_profile.json"
OUTPUT_RESUMES_DIR = PROJECT_ROOT / "output" / "resumes"

# Standard GTM/AI verified capability synonyms & competencies
STANDARD_VERIFIED_SYNONYMS: Set[str] = {
    "crm",
    "hubspot",
    "salesforce",
    "sql",
    "python",
    "apis",
    "rest api",
    "rest apis",
    "webhooks",
    "webhook",
    "cross-functional",
    "pipeline generation",
    "outbound",
    "enterprise outbound",
    "revops",
    "revenue operations",
    "gtm engineering",
    "gtm",
    "clay",
    "llm",
    "llms",
    "generative ai",
    "ai agents",
    "agentic workflows",
    "prompt engineering",
    "structured outputs",
    "data enrichment",
    "waterfall enrichment",
    "meddicc",
    "meddpicc",
    "command of the message",
    "challenger sale",
    "challenger",
    "prospecting",
    "lead scoring",
    "deal desk",
    "salesloft",
    "outreach",
    "apollo",
    "linkedin sales navigator",
    "notion",
    "notion api",
    "fastapi",
    "flask",
    "langchain",
    "langgraph",
    "json",
    "json schema",
    "pydantic",
    "git",
    "automation",
    "workflow automation",
    "signal scraping",
    "market intelligence",
    "abm",
    "account-based marketing",
    "b2b saas",
    "enterprise sales",
    "forecasting",
    "territory planning",
}

ATS_RESUME_CSS = """
@page {
    size: letter;
    margin: 0.45in 0.55in;
}
body {
    font-family: -apple-system, BlinkMacSystemFont, "Helvetica Neue", Helvetica, Arial, sans-serif;
    font-size: 9.5pt;
    line-height: 1.38;
    color: #111827;
    margin: 0;
    padding: 0;
}
h1 {
    font-size: 18pt;
    font-weight: 700;
    margin: 0 0 2pt 0;
    color: #0f172a;
    letter-spacing: -0.3px;
}
h2 {
    font-size: 10.5pt;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.6px;
    color: #0f172a;
    border-bottom: 1px solid #cbd5e1;
    padding-bottom: 2pt;
    margin: 10pt 0 5pt 0;
}
h3 {
    font-size: 9.8pt;
    font-weight: 700;
    color: #1e293b;
    margin: 7pt 0 2pt 0;
}
p {
    margin: 2pt 0 5pt 0;
}
ul {
    margin: 2pt 0 6pt 0;
    padding-left: 15pt;
}
li {
    margin-bottom: 2.5pt;
}
strong {
    font-weight: 600;
    color: #0f172a;
}
code {
    font-family: "SFMono-Regular", Menlo, Consolas, monospace;
    font-size: 8.8pt;
    background-color: #f1f5f9;
    padding: 1pt 3pt;
    border-radius: 2pt;
}
"""


def clean_slug(text: str) -> str:
    """Sanitizes company or role name into a clean alphanumeric/underscore token."""
    cleaned = re.sub(r"[^A-Za-z0-9]+", "_", text.strip()).strip("_")
    return cleaned or "Target"


def load_master_profile() -> Dict[str, Any]:
    if not PROFILE_PATH.exists():
        return {}
    return json.loads(PROFILE_PATH.read_text(encoding="utf-8"))


def _build_verified_capability_corpus(master_profile: Dict[str, Any]) -> Tuple[Set[str], str]:
    """Builds a lowercase whitelist of verified tokens/phrases from master_career_profile.json + standard GTM/AI synonyms."""
    whitelist: Set[str] = set(STANDARD_VERIFIED_SYNONYMS)
    profile_dump = json.dumps(master_profile).lower()

    candidate = master_profile.get("candidate", {})
    tech_stack = candidate.get("technical_stack", {})
    if isinstance(tech_stack, dict):
        for items in tech_stack.values():
            for item in items:
                s = str(item).lower().strip()
                whitelist.add(s)
                for token in re.split(r"[,/()\-]+", s):
                    tok = token.strip()
                    if len(tok) >= 2:
                        whitelist.add(tok)

    return whitelist, profile_dump


def enforce_ats_keyword_guardrail(
    markdown_text: str,
    jd_hard_skill_keywords: Optional[List[str]] = None,
    master_profile: Optional[Dict[str, Any]] = None,
) -> Tuple[str, List[str]]:
    """
    Upgrade #4: Deterministic ATS Keyword Gap Guardrail.
    1. Compares jd_hard_skill_keywords (case-insensitive) against compiled resume text.
    2. Cross-checks missing keywords against Jae's verified capabilities in master_career_profile.json
       plus standard GTM/AI synonyms.
    3. Automatically injects missing verified keywords into the Core Technical & GTM Stack section.
    Returns (updated_markdown_text, injected_keywords).
    """
    if not jd_hard_skill_keywords:
        return markdown_text, []

    profile = master_profile if master_profile is not None else load_master_profile()
    whitelist, profile_blob = _build_verified_capability_corpus(profile)

    resume_lower = markdown_text.lower()
    injected: List[str] = []

    for raw_kw in jd_hard_skill_keywords:
        kw = str(raw_kw).strip()
        if not kw:
            continue
        kw_lower = kw.lower()

        # Skip if already present in the compiled resume
        if kw_lower in resume_lower:
            continue

        # Check if verified in whitelist or master_career_profile.json
        is_verified = (
            kw_lower in whitelist
            or kw_lower in profile_blob
            or any(
                (w in kw_lower or kw_lower in w)
                for w in whitelist
                if len(w) >= 3
            )
        )
        if is_verified and kw not in injected:
            injected.append(kw)

    if not injected:
        return markdown_text, []

    injected_line = f"- **ATS-Aligned Job Competencies:** {', '.join(injected)}"
    target_anchor = "## Core Technical & GTM Stack"
    if target_anchor in markdown_text:
        lines = markdown_text.splitlines()
        new_lines: List[str] = []
        inserted = False
        for i, line in enumerate(lines):
            new_lines.append(line)
            if not inserted and line.startswith("- **AI & Systems Engineering:**"):
                new_lines.append(injected_line)
                inserted = True
        if not inserted:
            new_lines.append(injected_line)
        updated_md = "\n".join(new_lines).strip() + "\n"
    else:
        updated_md = markdown_text.strip() + f"\n\n{injected_line}\n"

    print(f"[+] ATS Keyword Guardrail injected {len(injected)} verified keywords: {injected}")
    return updated_md, injected


def build_resume_markdown(
    company_name: str,
    role_title: str,
    role_archetype: str,
    tailored_summary: Optional[str] = None,
    tailored_experience: Optional[List[Dict[str, Any]]] = None,
    jd_hard_skill_keywords: Optional[List[str]] = None,
    master_profile: Optional[Dict[str, Any]] = None,
) -> str:
    """Builds the complete formatted Markdown resume and enforces the ATS keyword guardrail."""
    profile = master_profile if master_profile is not None else load_master_profile()
    candidate = profile.get("candidate", {})
    base_experiences = profile.get("work_experience", [])

    name = candidate.get("name", "Jae Carter")
    location = candidate.get("location", "Chicago, IL")
    phone = candidate.get("phone", "(708) 632-1404")
    email = candidate.get("email", "jaelincarter@icloud.com")
    linkedin = candidate.get("linkedin", "https://www.linkedin.com/in/jae-carter")
    github = candidate.get("github", "https://github.com/carterjae")
    portfolio = candidate.get("portfolio", "https://jae-carter.vercel.app")

    pos_headline = candidate.get(
        "positioning_headline",
        "Full-Stack GTM Engineer, AI Revenue Architect & Enterprise Outbound Operator",
    )
    summary_text = (tailored_summary or candidate.get("core_summary") or "").strip()
    tech_stack = candidate.get("technical_stack", {})
    certs = candidate.get("certifications", [])

    contact_parts = [location]
    if phone:
        contact_parts.append(phone)
    if email:
        contact_parts.append(f"[{email}](mailto:{email})")
    if linkedin:
        clean_li = linkedin.replace("https://", "").replace("http://", "").rstrip("/")
        contact_parts.append(f"[{clean_li}]({linkedin})")
    if github:
        clean_gh = github.replace("https://", "").replace("http://", "").rstrip("/")
        contact_parts.append(f"[{clean_gh}]({github})")
    if portfolio:
        clean_pf = portfolio.replace("https://", "").replace("http://", "").rstrip("/")
        contact_parts.append(f"[{clean_pf}]({portfolio})")

    contact_str = " | ".join(contact_parts)

    md_lines: List[str] = [
        f"# {name}",
        f"**{pos_headline}**  ",
        f"{contact_str}  ",
    ]
    if company_name and company_name.lower() not in ["master", "general", "baseline"]:
        md_lines.append(f"Target Role: **{role_title}** at **{company_name}** (`{role_archetype}`)")
    md_lines.extend([
        "",
        "## Executive Summary",
        summary_text,
        "",
    ])

    if isinstance(tech_stack, dict):
        gtm_stack = ", ".join(tech_stack.get("gtm_and_revops", []))
        ai_stack = ", ".join(tech_stack.get("ai_and_engineering", []))
        md_lines.extend(
            [
                "## Core Technical & GTM Stack",
                f"- **GTM & RevOps:** {gtm_stack}",
                f"- **AI & Systems Engineering:** {ai_stack}",
                "",
            ]
        )

    md_lines.append("## Professional Experience")
    experience_entries = tailored_experience if tailored_experience else base_experiences

    for exp in experience_entries:
        if hasattr(exp, "model_dump"):
            exp = exp.model_dump()
        comp_str = exp.get("company", "")
        role_str = exp.get("role") or exp.get("title", "")
        dates_str = exp.get("dates", "")
        loc_str = exp.get("location", "Chicago, IL")
        header_meta = f"{dates_str} | {loc_str}" if loc_str else dates_str
        md_lines.append(f"### {role_str} — {comp_str} ({header_meta})")

        bullets = (
            exp.get("xyz_bullets")
            or exp.get("bullets")
            or exp.get("raw_achievements")
            or []
        )
        for b in bullets:
            md_lines.append(f"- {b}")
        md_lines.append("")

    raw_md = "\n".join(md_lines).strip() + "\n"
    guarded_md, _ = enforce_ats_keyword_guardrail(
        raw_md,
        jd_hard_skill_keywords=jd_hard_skill_keywords,
        master_profile=profile,
    )
    return guarded_md


def _render_pdf_with_weasyprint_or_fallback(markdown_content: str, abs_pdf_path: str) -> None:
    """Renders Markdown to an ATS-optimized PDF via WeasyPrint + CSS, falling back to ReportLab if CFFI libs aren't loaded."""
    html_body = markdown.markdown(markdown_content, extensions=["extra", "sane_lists"])
    full_html = (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        f"<style>{ATS_RESUME_CSS}</style></head><body>{html_body}</body></html>"
    )
    try:
        from weasyprint import CSS, HTML

        HTML(string=full_html).write_pdf(abs_pdf_path, stylesheets=[CSS(string=ATS_RESUME_CSS)])
        return
    except Exception as weasy_err:
        print(f"[*] WeasyPrint CFFI note ({weasy_err}); rendering ATS PDF via ReportLab Platypus...")

    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.platypus import HRFlowable, Paragraph, SimpleDocTemplate

    doc = SimpleDocTemplate(
        abs_pdf_path,
        pagesize=letter,
        leftMargin=38,
        rightMargin=38,
        topMargin=32,
        bottomMargin=32,
    )
    styles = getSampleStyleSheet()
    h1_style = ParagraphStyle(
        "ResumeH1",
        parent=styles["Heading1"],
        fontName="Helvetica-Bold",
        fontSize=16,
        leading=19,
        spaceAfter=2,
        textColor=colors.HexColor("#0f172a"),
    )
    h2_style = ParagraphStyle(
        "ResumeH2",
        parent=styles["Heading2"],
        fontName="Helvetica-Bold",
        fontSize=10,
        leading=13,
        spaceBefore=8,
        spaceAfter=2,
        textColor=colors.HexColor("#0f172a"),
    )
    h3_style = ParagraphStyle(
        "ResumeH3",
        parent=styles["Heading3"],
        fontName="Helvetica-Bold",
        fontSize=9.5,
        leading=12,
        spaceBefore=5,
        spaceAfter=2,
        textColor=colors.HexColor("#1e293b"),
    )
    body_style = ParagraphStyle(
        "ResumeBody",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=9,
        leading=12.5,
        spaceAfter=3,
        textColor=colors.HexColor("#111827"),
    )
    bullet_style = ParagraphStyle(
        "ResumeBullet",
        parent=body_style,
        leftIndent=12,
        firstLineIndent=-8,
        spaceAfter=2,
    )

    def _md_inline_to_rl(line: str) -> str:
        s = line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        s = re.sub(r"\[(.+?)\]\((.+?)\)", r'<a href="\2" color="#0284c7"><u>\1</u></a>', s)
        s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
        s = re.sub(r"`(.+?)`", r"<font name='Courier'>\1</font>", s)
        return s

    story = []
    for raw_line in markdown_content.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("# "):
            story.append(Paragraph(_md_inline_to_rl(line[2:]), h1_style))
        elif line.startswith("## "):
            story.append(Paragraph(_md_inline_to_rl(line[3:].upper()), h2_style))
            story.append(HRFlowable(width="100%", thickness=0.6, color=colors.HexColor("#cbd5e1"), spaceAfter=3))
        elif line.startswith("### "):
            story.append(Paragraph(_md_inline_to_rl(line[4:]), h3_style))
        elif line.startswith("- "):
            story.append(Paragraph(f"• {_md_inline_to_rl(line[2:])}", bullet_style))
        else:
            story.append(Paragraph(_md_inline_to_rl(line), body_style))

    doc.build(story)


def sanitize_resume_markdown(resume_text: str) -> str:
    """
    Deterministically strips any meta-commentary mentioning Google XYZ,
    XYZ formula, or prompt formatting instructions.
    """
    if not resume_text:
        return ""
    patterns = [
        r"\(tailored for google xyz format\)",
        r"\(google xyz format\)",
        r"\(xyz format\)",
        r"\(formatted to google xyz\)",
        r"\[google xyz formula\]",
        r"google xyz format",
        r"google xyz formula",
        r"\(tailored for xyz\)",
        r"\(xyz formula\)",
        r"tailored for xyz",
        r"xyz formula",
        r"google xyz",
    ]
    cleaned = resume_text
    # Ensure Professional Experience heading is strictly clean without swallowing next line
    cleaned = re.sub(
        r"##[ \t]+Professional[ \t]+Experience[^\n]*",
        "## Professional Experience",
        cleaned,
        flags=re.IGNORECASE,
    )
    for p in patterns:
        cleaned = re.sub(p, "", cleaned, flags=re.IGNORECASE)

    # Strip third-person phrasing from resume summary and bullets
    third_person_patterns = [
        (r"\bJae Carter is an?\s+", ""),
        (r"\bJae is an?\s+", ""),
        (r"\bJae Carter\s+brings\b", "Brings"),
        (r"\bJae\s+brings\b", "Brings"),
        (r"\bHe demonstrates\b", "Demonstrates"),
        (r"\bThe candidate demonstrates\b", "Demonstrates"),
        (r"\bThe candidate\b", "I"),
    ]
    for p, repl in third_person_patterns:
        cleaned = re.sub(p, repl, cleaned, flags=re.IGNORECASE)

    # Clean up double parentheses or trailing spaces left behind
    cleaned = re.sub(r"\(\s*\)", "", cleaned)
    cleaned = re.sub(r"  +", " ", cleaned)
    return cleaned.strip()



def compile_tailored_resume(

    company_name: str,
    role_title: str,
    role_archetype: str,
    tailored_summary: Optional[str] = None,
    tailored_experience: Optional[List[Dict[str, Any]]] = None,
    jd_hard_skill_keywords: Optional[List[str]] = None,
    master_profile: Optional[Dict[str, Any]] = None,
) -> Tuple[str, str]:
    """
    Enforces the ATS keyword guardrail and physically writes BOTH:
      1. Clean Markdown resume: os.path.abspath(f"output/resumes/Jae_Carter_{clean_company}_{clean_role}.md")
      2. ATS-optimized PDF resume: os.path.abspath(f"output/resumes/Jae_Carter_{clean_company}_{clean_role}.pdf")
    Returns (verified_abs_pdf_path, markdown_content).
    """
    os.chdir(PROJECT_ROOT)
    os.makedirs("output/resumes", exist_ok=True)

    clean_company = clean_slug(company_name)
    clean_role = clean_slug(role_title)

    rel_md_path = f"output/resumes/Jae_Carter_{clean_company}_{clean_role}.md"
    rel_pdf_path = f"output/resumes/Jae_Carter_{clean_company}_{clean_role}.pdf"
    abs_md_path = os.path.abspath(rel_md_path)
    abs_pdf_path = os.path.abspath(rel_pdf_path)

    markdown_content = build_resume_markdown(
        company_name=company_name,
        role_title=role_title,
        role_archetype=role_archetype,
        tailored_summary=tailored_summary,
        tailored_experience=tailored_experience,
        jd_hard_skill_keywords=jd_hard_skill_keywords,
        master_profile=master_profile,
    )
    markdown_content = sanitize_resume_markdown(markdown_content)


    with open(abs_md_path, "w", encoding="utf-8") as f:
        f.write(markdown_content)

    company_alias_md = os.path.abspath(f"output/resumes/{clean_company}_resume.md")
    with open(company_alias_md, "w", encoding="utf-8") as f:
        f.write(markdown_content)

    _render_pdf_with_weasyprint_or_fallback(markdown_content, abs_pdf_path)


    if not os.path.isfile(abs_md_path):
        raise RuntimeError(f"Failed to verify physical Markdown resume on disk at {abs_md_path}")
    if not os.path.isfile(abs_pdf_path):
        raise RuntimeError(f"Failed to verify physical PDF resume on disk at {abs_pdf_path}")

    print(f"[+] Physically wrote tailored Markdown resume -> {abs_md_path}")
    print(f"[+] Physically wrote ATS-optimized PDF resume -> {abs_pdf_path}")
    return abs_pdf_path, markdown_content


if __name__ == "__main__":
    company = sys.argv[1] if len(sys.argv) > 1 else "Braintrust"
    role = sys.argv[2] if len(sys.argv) > 2 else "GTM Engineer"
    archetype = sys.argv[3] if len(sys.argv) > 3 else "GTM Engineer"
    pdf_path, _ = compile_tailored_resume(company, role, archetype)
    print(f"[+] Verified absolute PDF path: {pdf_path}")
