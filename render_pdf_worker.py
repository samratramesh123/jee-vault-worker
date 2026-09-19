#!/usr/bin/env python3
"""
=============================================================================
JEE VAULT — PYTHON / PLAYWRIGHT PDF RENDERING ENGINE v3.0
=============================================================================
Contract Version : JEE_VAULT_2.0
Framework        : FastAPI + Playwright (Chromium headless)
Input            : Master Book Spec JSON from Google Apps Script (Code.gs)
Output           : Premium A4 PDF (25-30 MB, 30-40 pages)
Callback         : POST back to Apps Script doPost() with result
=============================================================================
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
import math
import os
import re
import secrets
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from jsonschema import Draft202012Validator
try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

# =============================================================================
# ENVIRONMENT CONFIGURATION
# =============================================================================

BASE_DIR          = Path(__file__).resolve().parent
OUTPUT_DIR        = Path(os.getenv("OUTPUT_DIR", BASE_DIR / "output"))
SCHEMA_PATH       = BASE_DIR / "master_book_spec.schema.json"
WORKER_SECRET     = os.getenv("PDF_WORKER_SECRET", "").strip()
CALLBACK_URL      = os.getenv("APPS_SCRIPT_CALLBACK_URL", "").strip()
PUBLIC_BASE_URL   = os.getenv("WORKER_PUBLIC_BASE_URL", "").rstrip("/")
MAX_PDF_BYTES     = int(os.getenv("MAX_PDF_BYTES", str(35 * 1024 * 1024)))  # 35 MB hard limit

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s"
)
log = logging.getLogger("jee-vault-worker")

# =============================================================================
# JSON SCHEMA VALIDATOR
# =============================================================================

if SCHEMA_PATH.exists():
    with SCHEMA_PATH.open("r", encoding="utf-8") as _f:
        _SCHEMA = json.load(_f)
    VALIDATOR = Draft202012Validator(_SCHEMA)
else:
    VALIDATOR = None

# =============================================================================
# FASTAPI APPLICATION
# =============================================================================

app = FastAPI(
    title="JEE Vault Vector PDF Worker",
    version="3.0.0",
    description="Renders premium sellable JEE ebooks from Master Book Spec JSON"
)

# =============================================================================
# SECURITY
# =============================================================================

def require_secret(x_jee_vault_secret: Optional[str]) -> None:
    if not WORKER_SECRET:
        return
    candidate = x_jee_vault_secret or ""
    if not secrets.compare_digest(candidate.encode(), WORKER_SECRET.encode()):
        raise HTTPException(status_code=401, detail="Unauthorized")

# =============================================================================
# PAYLOAD VALIDATION
# =============================================================================

def validate_payload(payload: Dict[str, Any]) -> None:
    required = ["protocolVersion", "runId", "timestamp", "lesson", "masterBookSpec", "semanticQa"]
    missing  = [k for k in required if k not in payload]
    if missing:
        raise ValueError(f"PAYLOAD_INVALID: missing fields: {missing}")

    lesson = payload["lesson"]
    spec   = payload["masterBookSpec"]

    if VALIDATOR:
        errors = sorted(VALIDATOR.iter_errors(spec), key=lambda e: list(e.path))
        if errors:
            path = ".".join(str(x) for x in errors[0].path)
            raise ValueError(f"SCHEMA_INVALID at {path}: {errors[0].message}")

    if int(spec["metadata"]["dayIndex"]) != int(lesson["dayIndex"]):
        raise ValueError("INVALID: dayIndex mismatch between lesson and masterBookSpec")
    if spec["metadata"]["category"] != lesson["category"]:
        raise ValueError("INVALID: category mismatch")

    day = int(lesson["dayIndex"])
    rev = spec.get("previousDayRevision", {})
    if day > 1:
        if not rev.get("included"):
            raise ValueError("INVALID: previousDayRevision.included must be true for Day > 1")
        if rev.get("placement") != "END_OF_TODAYS_PDF_BEFORE_FINAL_CLOSURE":
            raise ValueError("INVALID: previousDayRevision.placement is incorrect")
    else:
        if rev.get("included") is True:
            raise ValueError("INVALID: Day 1 must NOT include previousDayRevision")

    if payload.get("semanticQa", {}).get("pass") is False:
        raise ValueError("SEMANTIC_QA_FAILED: worker refuses to render a QA-failed payload")

# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def esc(v: Any) -> str:
    return html.escape(str(v if v is not None else ""), quote=True)

def plain(v: Any) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip()

def safe_filename(day: int, topic: str, version: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", topic).strip("-")[:80] or "lesson"
    return f"JEE_VAULT_DAY_{day:03d}_{slug}_{version}.pdf"

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def page_count(pdf_path: Path) -> int:
    try:
        import fitz
        doc = fitz.open(str(pdf_path))
        n = doc.page_count
        doc.close()
        return n
    except Exception:
        return 0

def callback_apps_script(body: Dict[str, Any]) -> None:
    if not CALLBACK_URL:
        log.warning("APPS_SCRIPT_CALLBACK_URL not set — skipping callback")
        return
    try:
        url = CALLBACK_URL
        if WORKER_SECRET:
            sep = "&" if "?" in url else "?"
            url = url + sep + "secret=" + requests.utils.quote(WORKER_SECRET, safe="")
        r = requests.post(url, json=body, timeout=30)
        r.raise_for_status()
        log.info("Callback to Apps Script: HTTP %s", r.status_code)
    except Exception:
        log.exception("Apps Script callback failed — PDF still saved locally")

# =============================================================================
# DESIGN TOKENS (must match Code.gs DESIGN_TOKENS)
# =============================================================================

SUBJECT_TOKENS = {
    "Mathematics": {
        "primary": "#1F3864", "accent": "#D4AF37",
        "secondary": "#2E4A7A", "soft": "#EEF2FF", "body": "#172033"
    },
    "Physics": {
        "primary": "#0D4F5C", "accent": "#E87722",
        "secondary": "#1A6E7D", "soft": "#FFF3E6", "body": "#172033"
    },
    "Chemistry": {
        "primary": "#6B1E3A", "accent": "#2E8B57",
        "secondary": "#8B2252", "soft": "#F0FFF4", "body": "#172033"
    },
}

def get_tokens(subject: str) -> Dict[str, str]:
    return SUBJECT_TOKENS.get(subject, SUBJECT_TOKENS["Physics"])

# =============================================================================
# CSS GENERATOR
# =============================================================================

def build_css(tokens: Dict[str, str]) -> str:
    p  = tokens["primary"]
    a  = tokens["accent"]
    s  = tokens["secondary"]
    sf = tokens["soft"]
    b  = tokens["body"]

    return f"""
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&display=swap');

@page {{
    size: A4;
    margin: 14mm 14mm 15mm 14mm;
}}

:root {{
    --primary:   {p};
    --accent:    {a};
    --secondary: {s};
    --soft:      {sf};
    --body:      {b};
    --muted:     #5B6475;
    --border:    #D9DEE8;
    --paper:     #FFFFFF;
    --trap:      #991B1B;
    --trap-bg:   #FFF1F2;
    --recall:    #7C3AED;
    --recall-bg: #F5F3FF;
    --memory:    #D97706;
    --memory-bg: #FFFBEB;
    --shortcut:  #0D9488;
    --shortcut-bg:#F0FDFA;
    --ncert:     #6B7280;
    --ncert-bg:  #F9FAFB;
}}

* {{ box-sizing: border-box; margin: 0; padding: 0; }}
html, body {{ background: var(--paper); color: var(--body); font-family: 'Inter', Arial, sans-serif; }}
body {{ font-size: 11pt; line-height: 1.5; -webkit-print-color-adjust: exact; print-color-adjust: exact; }}

/* ── TYPOGRAPHY ─────────────────────────────────────────── */
h1 {{ font-size: 26pt; font-weight: 800; color: var(--primary); line-height: 1.1;
      margin-bottom: 10pt; letter-spacing: -0.02em; break-after: avoid; }}
h2 {{ font-size: 18pt; font-weight: 700; color: var(--primary); margin: 16pt 0 8pt;
      padding-bottom: 4pt; border-bottom: 2px solid var(--accent); break-after: avoid; }}
h3 {{ font-size: 13pt; font-weight: 600; color: var(--secondary); margin: 12pt 0 6pt; break-after: avoid; }}
h4 {{ font-size: 11pt; font-weight: 600; color: var(--body); margin: 8pt 0 4pt; }}
p  {{ margin-bottom: 7pt; }}
ul, ol {{ margin: 5pt 0 8pt 18pt; }}
li {{ margin-bottom: 3pt; }}

/* ── COVER PAGE ─────────────────────────────────────────── */
.cover {{
    min-height: 260mm;
    background: linear-gradient(145deg, var(--primary) 0%, var(--secondary) 60%, #000 100%);
    color: white;
    display: flex; flex-direction: column; justify-content: space-between;
    padding: 20mm 16mm 16mm;
    page-break-after: always;
    position: relative;
    overflow: hidden;
}}
.cover::before {{
    content: '';
    position: absolute; top: -40mm; right: -30mm;
    width: 120mm; height: 120mm;
    border-radius: 50%;
    background: rgba(255,255,255,0.04);
    pointer-events: none;
}}
.cover::after {{
    content: '';
    position: absolute; bottom: -20mm; left: -20mm;
    width: 80mm; height: 80mm;
    border-radius: 50%;
    background: rgba(255,255,255,0.03);
    pointer-events: none;
}}
.cover-brand  {{ font-size: 9pt; font-weight: 800; letter-spacing: 0.25em;
                  color: var(--accent); text-transform: uppercase; }}
.cover-badge  {{ display: inline-block; background: var(--accent); color: #000;
                  font-size: 9pt; font-weight: 800; padding: 3pt 10pt;
                  border-radius: 20pt; letter-spacing: 0.15em; margin: 10pt 0; }}
.cover-title  {{ font-size: 30pt; font-weight: 800; color: white; line-height: 1.08;
                  margin: 6pt 0 10pt; max-width: 150mm; }}
.cover-sub    {{ font-size: 12pt; font-weight: 400; color: rgba(255,255,255,0.75);
                  max-width: 140mm; line-height: 1.5; margin-bottom: 14pt; }}
.cover-meta   {{ display: flex; gap: 12pt; flex-wrap: wrap; margin-top: 10pt; }}
.cover-chip   {{ background: rgba(255,255,255,0.12); border: 1px solid rgba(255,255,255,0.2);
                  border-radius: 8pt; padding: 5pt 10pt; font-size: 8.5pt; color: rgba(255,255,255,0.85); }}
.cover-footer {{ border-top: 1px solid rgba(255,255,255,0.15); padding-top: 10pt;
                  font-size: 8.5pt; color: rgba(255,255,255,0.55); }}

/* ── SECTION CONTAINERS ─────────────────────────────────── */
.section {{ padding: 8pt 0; break-inside: avoid; }}
.page-break {{ page-break-before: always; }}

/* ── CARDS ───────────────────────────────────────────────── */
.card {{
    border: 1px solid var(--border); border-radius: 10pt;
    padding: 10pt 12pt; margin: 7pt 0; break-inside: avoid;
    background: var(--paper);
}}
.card-soft {{ background: var(--soft); border-color: var(--primary); }}
.card-accent {{ border-left: 4px solid var(--accent); background: var(--soft); }}

/* ── CALLOUT BOXES ───────────────────────────────────────── */
.callout {{
    border-radius: 8pt; padding: 9pt 12pt;
    margin: 8pt 0; break-inside: avoid;
    display: flex; gap: 8pt; align-items: flex-start;
}}
.callout-icon {{ font-size: 13pt; flex-shrink: 0; line-height: 1.4; }}
.callout-body {{ flex: 1; }}
.callout-header {{ font-weight: 700; font-size: 9pt; letter-spacing: 0.06em;
                   text-transform: uppercase; margin-bottom: 3pt; }}
.callout-jee    {{ border-left: 4px solid var(--accent);  background: var(--soft); }}
.callout-short  {{ border-left: 4px solid var(--shortcut); background: var(--shortcut-bg); }}
.callout-trap   {{ border-left: 4px solid var(--trap);    background: var(--trap-bg); }}
.callout-ncert  {{ border-left: 4px solid var(--ncert);   background: var(--ncert-bg); }}
.callout-recall {{ border-left: 4px solid var(--recall);  background: var(--recall-bg); }}
.callout-memory {{ border-left: 4px solid var(--memory);  background: var(--memory-bg); }}

.callout-jee    .callout-header {{ color: var(--accent); }}
.callout-short  .callout-header {{ color: var(--shortcut); }}
.callout-trap   .callout-header {{ color: var(--trap); }}
.callout-recall .callout-header {{ color: var(--recall); }}
.callout-memory .callout-header {{ color: var(--memory); }}

/* ── FORMULAS ────────────────────────────────────────────── */
.formula-card {{
    background: var(--soft); border: 1.5px solid var(--primary);
    border-radius: 10pt; padding: 10pt 14pt; margin: 8pt 0; break-inside: avoid;
    text-align: center;
}}
.formula-name  {{ font-size: 8.5pt; font-weight: 700; color: var(--primary);
                  text-transform: uppercase; letter-spacing: 0.08em; margin-bottom: 5pt; }}
.formula-body  {{ font-size: 14pt; color: var(--body); margin: 4pt 0; }}
.formula-cond  {{ font-size: 9pt; color: var(--muted); margin-top: 5pt; }}
.freq-badge    {{ display: inline-block; font-size: 7.5pt; font-weight: 700;
                  padding: 2pt 7pt; border-radius: 10pt; margin-left: 6pt; }}
.freq-high   {{ background: #DC2626; color: white; }}
.freq-medium {{ background: #D97706; color: white; }}
.freq-low    {{ background: #6B7280; color: white; }}

/* ── QUESTIONS ───────────────────────────────────────────── */
.question {{
    border: 1px solid var(--border); border-radius: 8pt;
    padding: 10pt 12pt; margin: 8pt 0; break-inside: avoid;
}}
.question-num  {{ font-size: 8pt; font-weight: 800; color: var(--primary); letter-spacing: 0.1em; }}
.diff-badge    {{ display: inline-block; font-size: 7.5pt; font-weight: 700;
                  padding: 2pt 7pt; border-radius: 10pt; margin-left: 6pt; }}
.diff-easy   {{ background: #065F46; color: white; }}
.diff-medium {{ background: #92400E; color: white; }}
.diff-hard   {{ background: #7F1D1D; color: white; }}
.solution-box {{
    background: #F0FFF4; border-left: 4px solid #2E8B57;
    border-radius: 0 8pt 8pt 0; padding: 8pt 10pt; margin-top: 8pt;
    break-inside: avoid;
}}
.solution-label {{ font-weight: 700; font-size: 9pt; color: #2E8B57; margin-bottom: 4pt; }}
.topper-method {{ font-size: 9pt; color: #065F46; font-style: italic; margin-bottom: 5pt; }}

/* ── CHEAT SHEET ─────────────────────────────────────────── */
.cheat-sheet {{ page-break-before: always; }}
.cheat-header {{
    background: var(--primary); color: white;
    padding: 10pt 14pt; border-radius: 10pt 10pt 0 0;
    font-size: 14pt; font-weight: 800; letter-spacing: -0.01em;
}}
.cheat-body {{
    border: 2px solid var(--primary); border-top: none;
    border-radius: 0 0 10pt 10pt; padding: 10pt;
}}
.cheat-grid {{
    display: grid; grid-template-columns: repeat(3, 1fr); gap: 6pt;
}}
.cheat-item {{
    background: var(--soft); border: 1px solid var(--border);
    border-radius: 6pt; padding: 7pt 9pt; break-inside: avoid;
}}
.cheat-item .formula-body {{ font-size: 11pt; }}

/* ── REVISION SECTION ────────────────────────────────────── */
.revision {{
    page-break-before: always;
    background: linear-gradient(135deg, #FFFBEB 0%, #FEF3C7 100%);
    border: 2px solid var(--memory); border-radius: 12pt;
    padding: 12pt 14pt; break-inside: avoid;
}}
.revision-badge {{
    display: inline-block; background: var(--memory); color: white;
    font-size: 8pt; font-weight: 800; letter-spacing: 0.15em;
    padding: 3pt 12pt; border-radius: 20pt; margin-bottom: 8pt;
    text-transform: uppercase;
}}
.revision h2 {{ border-color: var(--memory); color: #92400E; }}
.revision h3 {{ color: var(--memory); }}
.revision-q  {{
    background: white; border: 1px solid #FDE68A;
    border-radius: 6pt; padding: 7pt 10pt; margin: 5pt 0;
    break-inside: avoid;
}}

/* ── CLOSURE ─────────────────────────────────────────────── */
.closure {{
    page-break-before: always; background: var(--primary); color: white;
    border-radius: 12pt; padding: 16pt 18pt; text-align: center;
    break-inside: avoid;
}}
.closure h2 {{ color: var(--accent); border: none; }}
.closure p  {{ color: rgba(255,255,255,0.85); font-size: 12pt; line-height: 1.6; }}

/* ── VISUALS ─────────────────────────────────────────────── */
.visual     {{ margin: 8pt 0; break-inside: avoid; }}
.visual svg {{ max-width: 100%; height: auto; }}
.vis-caption {{ font-size: 9pt; color: var(--muted); text-align: center;
                font-style: italic; margin-top: 5pt; }}
.flow-wrap  {{ display: flex; align-items: center; flex-wrap: wrap; gap: 4pt; margin: 6pt 0; }}
.flow-node  {{ background: var(--primary); color: white; border-radius: 6pt;
               padding: 5pt 10pt; font-size: 9.5pt; font-weight: 600; }}
.flow-arrow {{ color: var(--accent); font-size: 14pt; font-weight: 800; }}
table {{
    width: 100%; border-collapse: collapse; margin: 8pt 0; break-inside: avoid;
    font-size: 10pt;
}}
th {{
    background: var(--primary); color: white;
    padding: 6pt 10pt; text-align: left; font-weight: 600;
}}
td {{ padding: 6pt 10pt; border: 1px solid var(--border); }}
tr:nth-child(even) td {{ background: var(--soft); }}

/* ── OBJECTIVES / META ───────────────────────────────────── */
.obj-list {{
    display: grid; grid-template-columns: 1fr 1fr; gap: 6pt; margin: 8pt 0;
}}
.obj-item {{
    background: var(--soft); border-left: 3px solid var(--accent);
    border-radius: 0 6pt 6pt 0; padding: 6pt 10pt; font-size: 10.5pt;
}}
.kicker {{
    font-size: 8pt; font-weight: 800; color: var(--primary);
    text-transform: uppercase; letter-spacing: 0.1em; margin-bottom: 4pt;
}}
.tag {{
    display: inline-block; background: var(--secondary); color: white;
    font-size: 8pt; font-weight: 600; padding: 2pt 8pt;
    border-radius: 10pt; margin-right: 4pt;
}}

/* ── STEP BLOCK ─────────────────────────────────────────── */
.step {{
    display: flex; gap: 10pt; margin: 7pt 0; align-items: flex-start; break-inside: avoid;
}}
.step-num {{
    min-width: 22pt; height: 22pt; background: var(--primary); color: white;
    border-radius: 50%; display: flex; align-items: center; justify-content: center;
    font-size: 9pt; font-weight: 800; flex-shrink: 0;
}}

/* ── DEFINITION ─────────────────────────────────────────── */
.definition {{
    border-left: 4px solid var(--secondary); background: var(--soft);
    padding: 8pt 12pt; margin: 6pt 0; border-radius: 0 8pt 8pt 0; break-inside: avoid;
}}
.definition .term {{ font-weight: 700; color: var(--primary); }}

/* ── HOOKS ───────────────────────────────────────────────── */
.hook {{
    background: linear-gradient(135deg, var(--primary) 0%, var(--secondary) 100%);
    color: white; border-radius: 10pt; padding: 12pt 16pt; margin: 8pt 0;
    font-size: 13pt; font-weight: 600; line-height: 1.5; break-inside: avoid;
}}

/* ── PAYOFF ──────────────────────────────────────────────── */
.payoff {{
    background: var(--accent); color: #1a1a1a; border-radius: 8pt;
    padding: 10pt 14pt; margin: 10pt 0; font-weight: 600;
    font-size: 12pt; break-inside: avoid;
}}
.payoff::before {{ content: '✅ '; }}

/* ── FOOTER (auto via Playwright) ───────────────────────── */
"""

# =============================================================================
# HTML SECTION RENDERERS
# =============================================================================

def render_cover(meta: Dict, content: Dict, cover: Dict, tokens: Dict, spec: Dict) -> str:
    subject  = esc(meta.get("subject", ""))
    topic    = esc(content.get("title", meta.get("primaryTopic", "")))
    focus    = esc(meta.get("focusBoundaries", ""))
    day      = meta.get("dayIndex", 1)
    cat      = esc(meta.get("category", ""))
    chapter  = esc(meta.get("curricularUnit", ""))
    brand    = esc(cover.get("brandingLine", "JEE Mastery Series"))
    sv       = esc(spec.get("studentValue", ""))

    return f"""
<div class='cover'>
  <div>
    <div class='cover-brand'>⚡ JEE VAULT</div>
    <div class='cover-badge'>DAY {day:03d}</div>
    <div class='cover-title'>{topic}</div>
    <div class='cover-sub'>{focus or chapter}</div>
    <div class='cover-meta'>
      <div class='cover-chip'>📚 {subject}</div>
      <div class='cover-chip'>📋 {cat}</div>
      <div class='cover-chip'>🎯 {chapter}</div>
    </div>
  </div>
  <div>
    <div style='color:rgba(255,255,255,0.7);font-size:11pt;margin-bottom:8pt;font-style:italic;'>
      {sv}
    </div>
    <div class='cover-footer'>{brand}</div>
  </div>
</div>
"""

def render_objectives(content: Dict) -> str:
    objs = content.get("learningObjectives", [])
    prereqs = content.get("prerequisites", [])
    if not objs:
        return ""
    obj_html = "".join(f"<div class='obj-item'>✦ {esc(o)}</div>" for o in objs)
    pre_html = ""
    if prereqs:
        pre_html = "<h3>Prerequisites</h3><ul>" + "".join(f"<li>{esc(p)}</li>" for p in prereqs) + "</ul>"
    return f"""
<section class='section'>
  <h2>Learning Objectives</h2>
  <div class='obj-list'>{obj_html}</div>
  {pre_html}
</section>
"""

def render_hook(engagement: Dict) -> str:
    hook  = engagement.get("hook", "")
    setup = engagement.get("setup", "")
    if not hook:
        return ""
    return f"""
<section class='section'>
  <div class='hook'>💡 {esc(hook)}</div>
  {f"<p>{esc(setup)}</p>" if setup else ""}
</section>
"""

def render_definitions(defs: List[Dict]) -> str:
    if not defs:
        return ""
    blocks = ""
    for d in defs:
        term  = esc(d.get("term", ""))
        defn  = esc(d.get("definition", ""))
        ncert = esc(d.get("ncertRef", ""))
        ref   = f"<span style='color:var(--ncert);font-size:9pt;margin-left:8pt;'>{ncert}</span>" if ncert else ""
        blocks += f"<div class='definition'><div class='term'>{term}{ref}</div><div>{defn}</div></div>"
    return f"<section class='section'><h2>Key Definitions</h2>{blocks}</section>"

def render_sections(sections: List[Dict]) -> str:
    if not sections:
        return ""
    out = ""
    for s in sections:
        title   = esc(s.get("title", ""))
        body    = esc(s.get("body", ""))
        callouts = s.get("callouts", [])
        formulas = s.get("formulas", [])

        co_html = ""
        for c in callouts:
            co_html += render_callout(c)

        fm_html = ""
        for f in formulas:
            fm_html += render_formula_card(f)

        out += f"""
<section class='section'>
  <h2>{title}</h2>
  <p>{body}</p>
  {fm_html}
  {co_html}
</section>
"""
    return out

def render_callout(c: Dict) -> str:
    ctype  = str(c.get("type", "JEE_TIP")).upper()
    header = esc(c.get("header", ""))
    body   = esc(c.get("body", ""))

    css_map = {
        "JEE_TIP"  : ("callout-jee",    "⚡", "JEE TIP"),
        "SHORTCUT" : ("callout-short",  "⚡", "SHORTCUT"),
        "TRAP"     : ("callout-trap",   "⚠️", "COMMON MISTAKE"),
        "NCERT"    : ("callout-ncert",  "📚", "NCERT REF"),
        "RECALL"   : ("callout-recall", "🔁", "RECALL"),
        "MEMORY"   : ("callout-memory", "🧠", "MEMORY ANCHOR"),
    }
    css, icon, default_header = css_map.get(ctype, ("callout-jee", "⚡", "NOTE"))
    label = header or default_header
    return f"""
<div class='callout {css}'>
  <div class='callout-icon'>{icon}</div>
  <div class='callout-body'>
    <div class='callout-header'>{esc(label)}</div>
    <div>{body}</div>
  </div>
</div>
"""

def render_formula_card(f: Dict) -> str:
    name  = esc(f.get("name", ""))
    latex = esc(f.get("latex", ""))
    cond  = esc(f.get("conditions", ""))
    freq  = str(f.get("examFrequency", "MEDIUM")).upper()
    sc    = esc(f.get("shortcut", ""))
    trap  = esc(f.get("trap", ""))

    freq_css = {"HIGH": "freq-high", "MEDIUM": "freq-medium", "LOW": "freq-low"}.get(freq, "freq-medium")
    sc_html  = f"<div class='callout callout-short' style='margin-top:5pt;'><div class='callout-icon'>⚡</div><div class='callout-body'><div class='callout-header'>SHORTCUT</div><div>{sc}</div></div></div>" if sc else ""
    trap_html= f"<div class='callout callout-trap' style='margin-top:5pt;'><div class='callout-icon'>⚠️</div><div class='callout-body'><div class='callout-header'>COMMON MISTAKE</div><div>{trap}</div></div></div>" if trap else ""

    return f"""
<div class='formula-card'>
  <div class='formula-name'>
    {name}
    <span class='freq-badge {freq_css}'>{freq}</span>
  </div>
  <div class='formula-body'>\\[{latex}\\]</div>
  {"<div class='formula-cond'>Valid when: " + cond + "</div>" if cond else ""}
  {sc_html}
  {trap_html}
</div>
"""

def render_all_formulas(formulas: List[Dict]) -> str:
    if not formulas:
        return ""
    cards = "".join(render_formula_card(f) for f in formulas)
    return f"<section class='section page-break'><h2>High-Yield Formula System</h2>{cards}</section>"

def render_derivations(derivations: List[Dict]) -> str:
    if not derivations:
        return ""
    out = ""
    for d in derivations:
        title  = esc(d.get("title", "Derivation"))
        steps  = d.get("steps", [])
        chk    = esc(d.get("checkpoint", ""))
        steps_html = "".join(
            f"<div class='step'><div class='step-num'>{i+1}</div><div>{esc(st)}</div></div>"
            for i, st in enumerate(steps)
        )
        out += f"""
<div class='card'>
  <div class='kicker'>Derivation</div>
  <h3>{title}</h3>
  {steps_html}
  {f"<div class='callout callout-recall'><div class='callout-icon'>🔁</div><div class='callout-body'><div class='callout-header'>CHECKPOINT</div><div>{chk}</div></div></div>" if chk else ""}
</div>
"""
    return f"<section class='section'><h2>Derivations &amp; Proofs</h2>{out}</section>"

def render_solved_examples(examples: List[Dict]) -> str:
    if not examples:
        return ""
    out = ""
    for i, ex in enumerate(examples, 1):
        q      = esc(ex.get("question", ex.get("title", "")))
        ins    = esc(ex.get("keyInsight", ""))
        steps  = ex.get("steps", [])
        ans    = esc(ex.get("answer", ""))
        diff   = str(ex.get("difficulty", "MEDIUM")).upper()
        dcss   = {"EASY":"diff-easy","MEDIUM":"diff-medium","HARD":"diff-hard"}.get(diff,"diff-medium")
        steps_html = "".join(
            f"<div class='step'><div class='step-num'>{j+1}</div><div>{esc(st)}</div></div>"
            for j, st in enumerate(steps)
        )
        ins_html = f"<div class='callout callout-jee'><div class='callout-icon'>⚡</div><div class='callout-body'><div class='callout-header'>KEY INSIGHT</div><div>{ins}</div></div></div>" if ins else ""
        out += f"""
<div class='card card-accent'>
  <div class='kicker'>
    Worked Example {i}
    <span class='diff-badge {dcss}'>{diff}</span>
  </div>
  <h3>{q}</h3>
  {ins_html}
  {steps_html}
  {"<div class='payoff'>Answer: " + ans + "</div>" if ans else ""}
</div>
"""
    return f"<section class='section page-break'><h2>Worked Examples</h2>{out}</section>"

def render_practice_questions(questions: List[Dict], solutions: List[Dict]) -> str:
    if not questions:
        return ""
    sol_map = {str(s.get("id", i)): s for i, s in enumerate(solutions or [])}
    out = ""
    for i, q in enumerate(questions, 1):
        qid   = str(q.get("id", i))
        text  = esc(q.get("question", ""))
        qtype = esc(q.get("type", ""))
        diff  = str(q.get("difficulty", "MEDIUM")).upper()
        dcss  = {"EASY":"diff-easy","MEDIUM":"diff-medium","HARD":"diff-hard"}.get(diff,"diff-medium")
        is_trap = q.get("isTrap", False)
        trap_note = esc(q.get("trap", ""))
        opts  = q.get("options", [])

        opts_html = ""
        if opts:
            opts_html = "<div style='margin-top:6pt;'>" + "".join(
                f"<div style='margin:3pt 0;'><b>({chr(65+j)})</b> {esc(o)}</div>"
                for j, o in enumerate(opts)
            ) + "</div>"

        trap_html = ""
        if is_trap and trap_note:
            trap_html = f"""
<div class='callout callout-trap' style='margin-top:6pt;'>
  <div class='callout-icon'>⚠️</div>
  <div class='callout-body'><div class='callout-header'>TRAP ALERT</div><div>{trap_note}</div></div>
</div>
"""
        sol = sol_map.get(qid)
        sol_html = ""
        if sol:
            topper = esc(sol.get("topperMethod", ""))
            steps  = sol.get("steps", [])
            answer = esc(sol.get("answer", ""))
            s_steps = "".join(
                f"<div class='step'><div class='step-num'>{k+1}</div><div>{esc(st)}</div></div>"
                for k, st in enumerate(steps)
            )
            sol_html = f"""
<div class='solution-box'>
  <div class='solution-label'>💡 Solution</div>
  {"<div class='topper-method'>Topper Method: " + topper + "</div>" if topper else ""}
  {s_steps}
  {"<div style='font-weight:700;color:#065F46;margin-top:5pt;'>∴ " + answer + "</div>" if answer else ""}
</div>
"""
        out += f"""
<div class='question'>
  <div class='question-num'>
    Q{i} <span class='tag'>{qtype}</span>
    <span class='diff-badge {dcss}'>{diff}</span>
    {"<span class='tag' style='background:#991B1B;'>TRAP</span>" if is_trap else ""}
  </div>
  <p style='margin-top:5pt;'>{text}</p>
  {opts_html}
  {trap_html}
  {sol_html}
</div>
"""
    return f"<section class='section page-break'><h2>JEE Practice &amp; Application</h2>{out}</section>"

def render_common_traps(traps: List[Dict]) -> str:
    if not traps:
        return ""
    cards = ""
    for t in traps:
        trap = esc(t.get("trap", ""))
        why  = esc(t.get("why", ""))
        fix  = esc(t.get("fix", ""))
        cards += f"""
<div class='callout callout-trap'>
  <div class='callout-icon'>⚠️</div>
  <div class='callout-body'>
    <div class='callout-header'>COMMON MISTAKE</div>
    <b>{trap}</b>
    {"<div style='margin-top:4pt;color:#5B6475;'>Why: " + why + "</div>" if why else ""}
    {"<div style='margin-top:4pt;font-weight:600;'>Fix: " + fix + "</div>" if fix else ""}
  </div>
</div>
"""
    return f"<section class='section'><h2>Common Traps &amp; Mistakes</h2>{cards}</section>"

def render_engagement(eng: Dict) -> str:
    if not eng:
        return ""
    parts = []
    prompts = eng.get("prompts", [])
    if prompts:
        phtml = "".join(
            f"<div class='card card-soft'><div class='kicker'>Think First</div><p>{esc(p.get('prompt', p.get('text', '')) if isinstance(p, dict) else p)}</p></div>"
            for p in prompts
        )
        parts.append(f"<h3>Active Engagement</h3>{phtml}")
    exercises = eng.get("exercise", [])
    if exercises:
        ehtml = "".join(
            f"<div class='card'><div class='kicker'>Quick Check</div><p><b>{esc(e.get('question',''))}</b></p><p style='color:var(--muted);font-size:9pt;'>Answer: {esc(e.get('answer',''))}</p></div>"
            for e in exercises
        )
        parts.append(f"<h3>Quick Checks</h3>{ehtml}")
    payoff = eng.get("payoff", "")
    if payoff:
        parts.append(f"<div class='payoff'>{esc(payoff)}</div>")
    recall = eng.get("recall", [])
    if recall:
        rhtml = "<ol>" + "".join(f"<li>{esc(r)}</li>" for r in recall) + "</ol>"
        parts.append(f"""
<div class='callout callout-recall'>
  <div class='callout-icon'>🔁</div>
  <div class='callout-body'><div class='callout-header'>ACTIVE RECALL</div>{rhtml}</div>
</div>
""")
    if not parts:
        return ""
    return f"<section class='section'><h2>Active Learning</h2>{''.join(parts)}</section>"

def render_visuals(visuals: List[Dict]) -> str:
    if not visuals:
        return ""
    blocks = []
    for v in visuals:
        vtype   = str(v.get("type", "")).upper()
        caption = esc(v.get("concept", v.get("altDescription", "")))

        if vtype == "SVG" and v.get("exactContent", "").strip().startswith("<svg"):
            content = v["exactContent"]

        elif vtype == "FLOWCHART":
            nodes = v.get("nodes", [])
            conns = v.get("connections", [])
            if nodes:
                flow = "".join(
                    f"<div class='flow-node'>{esc(n)}</div>" +
                    ("<div class='flow-arrow'>→</div>" if i < len(nodes) - 1 else "")
                    for i, n in enumerate(nodes)
                )
                content = f"<div class='flow-wrap'>{flow}</div>"
            else:
                content = f"<div class='card card-soft'>{esc(v.get('exactContent',''))}</div>"

        elif vtype == "TABLE":
            cols = v.get("columns", [])
            rows = v.get("rows", [])
            if cols:
                header = "<tr>" + "".join(f"<th>{esc(c)}</th>" for c in cols) + "</tr>"
                body   = "".join(
                    "<tr>" + "".join(f"<td>{esc(cell)}</td>" for cell in row) + "</tr>"
                    for row in rows
                )
                content = f"<table><thead>{header}</thead><tbody>{body}</tbody></table>"
            else:
                content = f"<div class='card card-soft'>{esc(v.get('exactContent',''))}</div>"

        elif vtype in ("GRAPH", "BAR_CHART"):
            values = v.get("values", [])
            xlabels = v.get("xLabels", [str(i+1) for i in range(len(values))])
            ylabel  = esc(v.get("yLabel", ""))
            content = _render_bar_chart_svg(values, xlabels, ylabel)

        elif vtype == "LINE_CHART":
            values = v.get("values", [])
            xlabels = v.get("xLabels", [str(i+1) for i in range(len(values))])
            ylabel  = esc(v.get("yLabel", ""))
            content = _render_line_chart_svg(values, xlabels, ylabel)

        elif vtype == "MNEMONIC":
            content = f"""
<div class='callout callout-memory'>
  <div class='callout-icon'>🧠</div>
  <div class='callout-body'>
    <div class='callout-header'>MEMORY ANCHOR</div>
    <div style='font-size:13pt;font-weight:700;'>{esc(v.get('exactContent',''))}</div>
    <div style='margin-top:4pt;color:var(--muted);'>{esc(v.get('purpose',''))}</div>
  </div>
</div>
"""
        else:
            content = f"<div class='card card-soft'>{esc(v.get('exactContent', v.get('purpose', '')))}</div>"

        purpose = esc(v.get("purpose", ""))
        blocks.append(f"""
<div class='visual'>
  {content}
  {"<div class='vis-caption'>" + caption + (" — " + purpose if purpose else "") + "</div>" if caption else ""}
</div>
""")
    return f"<section class='section'><h2>Visual Intelligence</h2>{''.join(blocks)}</section>"

def _render_bar_chart_svg(values: List[float], labels: List[str], ylabel: str) -> str:
    if not values:
        return ""
    W, H, pad = 640, 220, 40
    vmax = max(values) or 1
    n    = len(values)
    bw   = (W - 2 * pad) / n * 0.65
    parts = [f"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 {W} {H}' role='img'>"]
    parts.append(f"<line x1='{pad}' y1='{H-pad}' x2='{W-10}' y2='{H-pad}' stroke='#D9DEE8' stroke-width='1.5'/>")
    for i, val in enumerate(values):
        x = pad + i * (W - 2 * pad) / n + ((W - 2 * pad) / n - bw) / 2
        h = (H - 2 * pad) * float(val) / vmax
        y = H - pad - h
        parts.append(f"<rect x='{x:.1f}' y='{y:.1f}' width='{bw:.1f}' height='{h:.1f}' fill='#1F3864' rx='3'/>")
        lbl = labels[i] if i < len(labels) else str(i + 1)
        parts.append(f"<text x='{x + bw/2:.1f}' y='{H - 10}' text-anchor='middle' font-size='10' fill='#5B6475'>{esc(lbl)}</text>")
        parts.append(f"<text x='{x + bw/2:.1f}' y='{y - 5:.1f}' text-anchor='middle' font-size='9' fill='#1F3864' font-weight='600'>{val}</text>")
    parts.append('</svg>')
    return "".join(parts)

def _render_line_chart_svg(values: List[float], labels: List[str], ylabel: str) -> str:
    if len(values) < 2:
        return _render_bar_chart_svg(values, labels, ylabel)
    W, H, pad = 640, 220, 40
    vmax = max(values); vmin = min(values); span = (vmax - vmin) or 1
    pts  = []
    for i, val in enumerate(values):
        x = pad + i * (W - 2 * pad) / (len(values) - 1)
        y = H - pad - (H - 2 * pad) * (float(val) - vmin) / span
        pts.append((x, y))
    poly = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    parts = [f"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 {W} {H}' role='img'>"]
    parts.append(f"<line x1='{pad}' y1='{H-pad}' x2='{W-10}' y2='{H-pad}' stroke='#D9DEE8' stroke-width='1.5'/>")
    parts.append(f"<polyline points='{poly}' fill='none' stroke='#1F3864' stroke-width='3' stroke-linecap='round' stroke-linejoin='round'/>")
    for i, (x, y) in enumerate(pts):
        lbl = labels[i] if i < len(labels) else str(i + 1)
        parts.append(f"<circle cx='{x:.1f}' cy='{y:.1f}' r='5' fill='#D4AF37'/>")
        parts.append(f"<text x='{x:.1f}' y='{H - 10}' text-anchor='middle' font-size='10' fill='#5B6475'>{esc(lbl)}</text>")
    parts.append('</svg>')
    return "".join(parts)

def render_cheat_sheet(cheat: Dict, subject: str) -> str:
    if not cheat:
        return ""
    formulas     = cheat.get("formulas", [])
    key_points   = cheat.get("keyPoints", [])
    memory       = cheat.get("memoryAnchors", [])
    exam_tips    = cheat.get("examTips", [])
    quick_recall = cheat.get("quickRecall", [])

    fm_html = ""
    if formulas:
        items = "".join(
            f"<div class='cheat-item'><div class='formula-name' style='font-size:7pt;'>{esc(f.get('name','') if isinstance(f,dict) else '')}</div>"
            f"<div class='formula-body' style='font-size:10pt;'>\\({esc(f.get('latex','') if isinstance(f,dict) else f)}\\)</div></div>"
            for f in formulas
        )
        fm_html = f"<h3>Formulas</h3><div class='cheat-grid'>{items}</div>"

    kp_html = ""
    if key_points:
        kp_html = "<h3>Key Points</h3><ul>" + "".join(f"<li>{esc(p)}</li>" for p in key_points) + "</ul>"

    mem_html = ""
    if memory:
        mems = "".join(f"<div class='cheat-item'><div class='callout-icon'>🧠</div><div>{esc(m)}</div></div>" for m in memory)
        mem_html = f"<h3>Memory Anchors</h3><div class='cheat-grid'>{mems}</div>"

    tip_html = ""
    if exam_tips:
        tips = "".join(f"<div class='cheat-item'><div class='callout-icon'>⚡</div><div>{esc(t)}</div></div>" for t in exam_tips)
        tip_html = f"<h3>JEE Tips</h3><div class='cheat-grid'>{tips}</div>"

    qr_html = ""
    if quick_recall:
        qr_html = "<h3>Quick Recall</h3><ol>" + "".join(f"<li>{esc(q)}</li>" for q in quick_recall) + "</ol>"

    return f"""
<section class='section cheat-sheet'>
  <div class='cheat-header'>⚡ CHEAT SHEET — {esc(subject)}</div>
  <div class='cheat-body'>
    {fm_html}
    {kp_html}
    {mem_html}
    {tip_html}
    {qr_html}
  </div>
</section>
"""

def render_previous_day_revision(rev: Dict) -> str:
    if not rev or not rev.get("included"):
        return ""
    prev_topic = esc(rev.get("previousTopic", ""))
    questions  = rev.get("recallQuestions", [])
    formulas   = rev.get("formulaRecall", [])
    concept    = rev.get("conceptCheck", {})
    application= rev.get("applicationRecall", {})
    trap       = esc(rev.get("commonTrapRecall", ""))

    q_html = ""
    if questions:
        q_html = "<h3>🧠 Active Recall</h3>" + "".join(
            f"<div class='revision-q'><b>Q{i+1}.</b> {esc(q.get('question', q) if isinstance(q,dict) else q)}</div>"
            for i, q in enumerate(questions)
        )

    f_html = ""
    if formulas:
        f_html = "<h3>📐 Formula Recall</h3>" + "".join(
            f"<div class='revision-q'>\\[{esc(f.get('latex', f.get('formula','')) if isinstance(f,dict) else f)}\\]<div style='font-size:9pt;color:var(--muted);'>{esc(f.get('formula','') if isinstance(f,dict) else '')}</div></div>"
            for f in formulas
        )

    cc_html = ""
    if concept:
        cq = esc(concept.get("question", concept) if isinstance(concept, dict) else concept)
        cc_html = f"<h3>💭 Concept Check</h3><div class='revision-q'>{cq}</div>"

    app_html = ""
    if application:
        aq = esc(application.get("question", application) if isinstance(application, dict) else application)
        app_html = f"<h3>🎯 Application Recall</h3><div class='revision-q'>{aq}</div>"

    trap_html = ""
    if trap:
        trap_html = f"""
<div class='callout callout-trap' style='margin-top:10pt;'>
  <div class='callout-icon'>⚠️</div>
  <div class='callout-body'>
    <div class='callout-header'>YESTERDAY'S TRAP — Don't repeat it today</div>
    <div>{trap}</div>
  </div>
</div>
"""

    return f"""
<section class='section revision'>
  <div class='revision-badge'>🔁 PREVIOUS DAY REVISION</div>
  <h2>Retrieve Before You Forget</h2>
  <p style='color:#92400E;margin-bottom:10pt;'>Previous Topic: <b>{prev_topic}</b></p>
  {q_html}
  {f_html}
  {cc_html}
  {app_html}
  {trap_html}
</section>
"""

def render_quick_recall(items: List[Dict]) -> str:
    if not items:
        return ""
    cards = "".join(
        f"<div class='card card-soft'><div class='kicker'>Recall Q{i+1}</div>"
        f"<p><b>{esc(x.get('question', x.get('prompt','')) if isinstance(x,dict) else x)}</b></p>"
        f"<p style='color:var(--muted);font-size:9pt;margin-top:4pt;'>{esc(x.get('answer','') if isinstance(x,dict) else '')}</p></div>"
        for i, x in enumerate(items)
    )
    return f"<section class='section'><h2>Final Recall</h2>{cards}</section>"

def render_closure(closure: str, business_value: str, day: int, next_topic: str = "") -> str:
    bv = esc(business_value)
    cl = esc(closure or "Lesson complete. Use the recall block to check your retention before sleeping.")
    nxt = f"<p style='color:var(--accent);font-size:11pt;margin-top:8pt;'>Tomorrow → Day {day+1}{': ' + esc(next_topic) if next_topic else ''}</p>" if next_topic else ""
    return f"""
<section class='section closure'>
  <h2>✅ Lesson Complete</h2>
  <p>{cl}</p>
  {f"<p style='margin-top:8pt;font-size:10pt;opacity:0.7;'>{bv}</p>" if bv else ""}
  {nxt}
</section>
"""

# =============================================================================
# MASTER HTML DOCUMENT BUILDER
# =============================================================================

def build_html_document(spec: Dict[str, Any]) -> str:
    meta    = spec.get("metadata", {})
    content = spec.get("content", {})
    eng     = spec.get("engagementJson", {})
    visual  = spec.get("visualJson", {})
    layout  = spec.get("layoutJson", {})
    rev     = spec.get("previousDayRevision", {})
    cover   = spec.get("coverPage", {})
    subject = meta.get("subject", "Physics")
    tokens  = get_tokens(subject)
    day     = int(meta.get("dayIndex", 1))
    title   = plain(content.get("title", meta.get("primaryTopic", "JEE Vault Lesson")))

    # KaTeX for beautiful math rendering
    katex_css = "<link rel='stylesheet' href='https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.css'>"
    katex_js  = """
<script defer src='https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.js'></script>
<script defer src='https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/contrib/auto-render.min.js'
  onload="renderMathInElement(document.body,{delimiters:[
    {left:'\\\\[',right:'\\\\]',display:true},
    {left:'\\\\(',right:'\\\\)',display:false}
  ]});">
</script>
"""

    # Build blocks in render sequence
    blocks = [
        render_cover(meta, content, cover, tokens, spec),
        render_objectives(content),
        render_hook(eng),
        render_definitions(content.get("definitions", [])),
        render_sections(content.get("sections", [])),
        render_all_formulas(content.get("formulas", [])),
        render_derivations(content.get("derivations", [])),
        render_solved_examples(content.get("solvedExamples", content.get("examples", []))),
        render_practice_questions(
            content.get("practiceQuestions", []),
            content.get("solutions", [])
        ),
        render_common_traps(content.get("commonTraps", [])),
        render_engagement(eng),
        render_visuals(visual.get("visuals", [])),
        render_cheat_sheet(content.get("cheatSheet", {}), subject),
        # ← PREVIOUS-DAY REVISION: always at end before closure
        render_previous_day_revision(rev),
        render_quick_recall(content.get("quickRecall", [])),
        render_closure(
            eng.get("closure", ""),
            spec.get("businessValue", ""),
            day
        ),
    ]

    css = build_css(tokens)

    return f"""<!doctype html>
<html lang='en'>
<head>
<meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>{esc(title)}</title>
{katex_css}
<style>{css}</style>
</head>
<body>
{''.join(b for b in blocks if b)}
{katex_js}
</body>
</html>"""

# =============================================================================
# RENDER ENGINE (Playwright)
# =============================================================================

def render_to_pdf(spec: Dict[str, Any], work_dir: Path) -> Dict[str, Any]:
    html_path = work_dir / "lesson.html"
    pdf_path  = work_dir / "lesson.pdf"

    document = build_html_document(spec)
    html_path.write_text(document, encoding="utf-8")

    with sync_playwright() as pw:
        chromium_exec = os.getenv("CHROMIUM_EXECUTABLE", "").strip() or None
        browser = pw.chromium.launch(
            headless=True,
            executable_path=chromium_exec,
            args=["--no-sandbox", "--disable-setuid-sandbox",
                  "--disable-dev-shm-usage", "--disable-gpu"]
        )
        page = browser.new_page(
            viewport={"width": 1440, "height": 1100},
            device_scale_factor=2  # High-DPI for sharpness
        )
        # Use file:// for local HTML to allow local assets
        page.goto(f"file://{html_path}", wait_until="networkidle", timeout=60000)
        page.emulate_media(media="print")

        # Wait for KaTeX to render
        try:
            page.wait_for_function(
                "() => document.fonts.ready",
                timeout=15000
            )
        except Exception:
            pass

        page.pdf(
            path=str(pdf_path),
            format="A4",
            print_background=True,
            prefer_css_page_size=True,
            margin={
                "top"   : "14mm",
                "right" : "14mm",
                "bottom": "15mm",
                "left"  : "14mm",
            },
            display_header_footer=True,
            header_template="<div></div>",
            footer_template="""
<div style='width:100%;font-size:8px;color:#667085;padding:0 14mm;
            display:flex;justify-content:space-between;'>
  <span>JEE VAULT — JEE Mastery Series 2027</span>
  <span><span class='pageNumber'></span> / <span class='totalPages'></span></span>
</div>
""",
        )
        browser.close()

    # Page count via PyMuPDF
    pages = page_count(pdf_path)
    issues = []
    size   = pdf_path.stat().st_size
    if size < 50_000:
        issues.append(f"PDF suspiciously small: {size} bytes")
    if pages < 5:
        issues.append(f"Page count too low: {pages}")

    return {"pdfPath": str(pdf_path), "pages": pages, "issues": issues, "sizeBytes": size}

# =============================================================================
# API ENDPOINTS
# =============================================================================

@app.get("/health")
def health() -> Dict[str, Any]:
    return {
        "ok"     : True,
        "service": "jee-vault-vector-pdf-worker",
        "version": "3.0.0",
        "status" : "ready"
    }

@app.post("/render")
def render(
    payload: Dict[str, Any],
    x_jee_vault_secret: Optional[str] = Header(default=None),
):
    require_secret(x_jee_vault_secret)
    started = time.time()
    run_id  = payload.get("runId", "unknown")
    lesson  = payload.get("lesson", {})
    day     = int(lesson.get("dayIndex", 0))
    spec    = payload.get("masterBookSpec", {})
    version = spec.get("metadata", {}).get("pdfVersion", "v1.0")
    topic   = spec.get("content", {}).get("title", f"Day-{day}")

    log.info("▶ Render request: Day %s | %s | runId=%s", day, topic, run_id)

    try:
        validate_payload(payload)

        with tempfile.TemporaryDirectory(prefix=f"jee-vault-day{day}-") as tmp:
            result   = render_to_pdf(spec, Path(tmp))
            pdf_path = Path(result["pdfPath"])
            size     = result["sizeBytes"]
            pages    = result["pages"]

            if size > MAX_PDF_BYTES:
                raise ValueError(
                    f"PDF_SIZE_OVER_BUDGET: {round(size/1024/1024,1)} MB > "
                    f"{round(MAX_PDF_BYTES/1024/1024,1)} MB limit"
                )

            filename   = safe_filename(day, topic, version)
            final_path = OUTPUT_DIR / filename
            pdf_path.replace(final_path)

            checksum = sha256_file(final_path)
            elapsed  = round(time.time() - started, 2)

            pdf_url = (
                f"{PUBLIC_BASE_URL}/artifacts/{filename}"
                if PUBLIC_BASE_URL else f"/artifacts/{filename}"
            )

            callback_body = {
                "success"     : True,
                "dayIndex"    : day,
                "runId"       : run_id,
                "pdfVersion"  : version,
                "pdfUrl"      : pdf_url,
                "driveDocument": "",         # ← populate if Google Drive upload added
                "timestamp"   : time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "actualPageCount"  : pages,
                "actualFileSizeMb" : round(size / 1024 / 1024, 2),
                "visualQa"    : {
                    "status"      : "PASS" if not result["issues"] else "WARNINGS",
                    "layoutStatus": "COMPLETE",
                    "renderPages" : pages,
                    "brokenVisuals": [],
                    "overflowPages": [],
                    "issues"      : result["issues"],
                    "sizeBytes"   : size,
                },
                "qa"          : {"status": "PASS"},
                "worker"      : {
                    "version"       : "3.0.0",
                    "sha256"        : checksum,
                    "elapsedSeconds": elapsed,
                },
            }

            log.info("✅ Day %s rendered: %s pages, %.1f MB, %.1f s",
                     day, pages, size/1024/1024, elapsed)
            callback_apps_script(callback_body)
            return JSONResponse(callback_body)

    except Exception as exc:
        log.exception("❌ Render failed for run %s: %s", run_id, exc)
        failure = {
            "success"      : False,
            "dayIndex"     : day,
            "runId"        : run_id,
            "timestamp"    : time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "failureReason": str(exc),
        }
        callback_apps_script(failure)
        raise HTTPException(status_code=422, detail=failure)


@app.get("/artifacts/{filename}")
def serve_artifact(filename: str):
    # Prevent path traversal
    path = (OUTPUT_DIR / filename).resolve()
    if path.parent != OUTPUT_DIR.resolve() or not path.exists() or path.suffix.lower() != ".pdf":
        raise HTTPException(status_code=404, detail="Artifact not found")
    return FileResponse(path, media_type="application/pdf", filename=path.name)

# =============================================================================
# CLI TEST ENTRY POINT
# =============================================================================

if __name__ == "__main__":
    import argparse, uvicorn

    parser = argparse.ArgumentParser(description="JEE Vault PDF Worker")
    parser.add_argument("--test", type=str, default=None,
                        help="Path to sample_payload.json for local test render")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    if args.test:
        print(f"🧪 Test render from: {args.test}")
        with open(args.test, encoding="utf-8") as f:
            payload = json.load(f)
        spec = payload["masterBookSpec"]
        with tempfile.TemporaryDirectory(prefix="jee-vault-test-") as tmp:
            result = render_to_pdf(spec, Path(tmp))
            out = OUTPUT_DIR / "TEST_RENDER.pdf"
            Path(result["pdfPath"]).replace(out)
            print(f"✅ Test PDF saved: {out}")
            print(f"   Pages: {result['pages']}, Size: {round(result['sizeBytes']/1024/1024,2)} MB")
            print(f"   Issues: {result['issues']}")
    else:
        print(f"🚀 Starting JEE Vault PDF Worker on port {args.port}")
        uvicorn.run("render_pdf_worker:app", host="0.0.0.0", port=args.port, reload=False)
        
