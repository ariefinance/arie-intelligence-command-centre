"""
ARIE Intelligence Command Centre renderer — Phase 1.

Layout order:
  1. Header (command-centre wordmark + KPI metric cards)
  2. Priority Decisions (Executive Snapshot — premium cards)
  3. What Matters This Week  (3-5 decision-first cards from top_signals)
  4. Customer Experience Intelligence
  5. Trajectory Watch (compact tiles)
  6. Additional Intelligence (Mauritius, Competitor, Introducer, Film, Regulatory)
  7. Brand Watch
  8. Source Health (trust strip + collapsible)
  9. Evidence Appendix (collapsible)
 10. Footer

Rules:
  - No empty sections.
  - No blank cards.
  - Older items appear only as Background Context.
  - Premium ARIE palette: navy #0B1E3E / gold #C9A84C / warm off-white page bg.
"""
from __future__ import annotations

import html as _html
import re
from datetime import datetime, timezone
from typing import List, Optional
from urllib.parse import urlparse

from models import (
    BrandWatchResult,
    CompetitorIntelItem,
    CustomerPainItem,
    Digest,
    DigestItem,
    DirectMention,
    EntityActivity,
    FilmWatchItem,
    InterestingFind,
    IntroducerIntelItem,
    MauritiusIntelItem,
    OpportunityRadarItem,
    ProspectLead,
    ScrapeResult,
    SocialPainPoint,
    SourceHealthItem,
    ThemeTrajectory,
    WatchlistTopic,
)

# ---------------------------------------------------------------------------
# Primitive helpers
# ---------------------------------------------------------------------------

def _esc(s: object) -> str:
    # Decode any pre-existing HTML entities in source/feed text (e.g. a literal
    # "&nbsp;" embedded in an RSS title) BEFORE escaping, so they don't surface as
    # visible "&nbsp;" in the brief. Unescape-then-escape preserves injection
    # safety: the returned string is always canonically HTML-escaped.
    return _html.escape(_html.unescape(str(s or "")), quote=True)


def _safe(s: object, fallback: str = "") -> str:
    stripped = str(s or "").strip()
    if not stripped or stripped.lower() in ("[sample]", "sample"):
        return fallback
    return stripped


def _fmt_date() -> str:
    try:
        return datetime.utcnow().strftime("%-d %B %Y")
    except ValueError:
        return datetime.utcnow().strftime("%d %B %Y").lstrip("0")


def _fmt_pub_date(published_at: Optional[str]) -> str:
    if not published_at:
        return ""
    try:
        d = datetime.fromisoformat(published_at[:10])
        return d.strftime("%-d %b %Y")
    except Exception:
        try:
            d = datetime.fromisoformat(published_at[:10])
            return d.strftime("%d %b %Y").lstrip("0")
        except Exception:
            return ""


def _domain(url: str) -> str:
    try:
        parsed = urlparse(url)
        host = parsed.netloc or ""
        return re.sub(r"^www\.", "", host)
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Render-layer sanitizers (fix #2, #3, #5, #6 helpers)
# ---------------------------------------------------------------------------

# Matches standalone reference codes like SIGNAL/1, COMPETITOR/3, COMM/4
_CODE_TOKEN_RE = re.compile(r"\b[A-Z]{2,12}/\d+\b")
# Matches a parenthesised group of one or more codes: (SIGNAL/1, COMM/3)
_CODE_PAREN_RE = re.compile(r"\s*\([A-Z]{2,12}/\d+(?:\s*,\s*[A-Z]{2,12}/\d+)*\)")
# Matches "Duplicate signal reinforcing <CODE>"
_DUPLICATE_RE = re.compile(r"Duplicate signal reinforcing\s+[A-Z]{2,12}/\d+\.?", re.IGNORECASE)
# Source suffix on headlines: " - Source Name" or " — Source Name" at the end
_SOURCE_SUFFIX_RE = re.compile(r"\s+[-—]\s+[^-—]{2,60}$")


def _clean_codes(text: str) -> str:
    """
    Strip internal reference codes from a user-visible string.
    - Removes parenthesised groups like (SIGNAL/1, COMM/3)
    - Removes bare tokens like SIGNAL/1
    - Removes "Duplicate signal reinforcing CODE" phrases
    - Cleans up leftover doubled spaces, " ," or dangling "()"
    - Preserves sentence-ending punctuation (periods, commas, semicolons, colons)
    """
    if not text:
        return text
    t = _DUPLICATE_RE.sub("", text)
    t = _CODE_PAREN_RE.sub("", t)
    t = _CODE_TOKEN_RE.sub("", t)
    # Remove literal "(URL not decoded)"
    t = t.replace("(URL not decoded)", "")
    # Clean up leftover artefacts
    t = re.sub(r"\s+,", ",", t)
    t = re.sub(r"\(\s*\)", "", t)
    # Collapse multiple spaces to one
    t = re.sub(r" {2,}", " ", t)
    # Remove any space directly before sentence punctuation (space-before-period artifact)
    t = re.sub(r"\s+([.,;:])", r"\1", t)
    return t.strip()


def _strip_source_suffix(title: str) -> str:
    """
    Remove trailing ' - Source Name' or ' — Source Name' suffixes from headlines.
    e.g. 'Chase Debanked... - Yahoo Finance' → 'Chase Debanked...'
    Only strips when the suffix looks like a source attribution (short, no sub-dashes).
    """
    if not title:
        return title
    m = _SOURCE_SUFFIX_RE.search(title)
    if m:
        return title[: m.start()].strip()
    return title


def _action_kind(action_text: str) -> tuple[str, str]:
    """
    Classify a suggested_action string into a label + CSS kind for an action badge.
    Returns (label, badge_kind).
    """
    t = (action_text or "").lower()
    if any(w in t for w in (
        "approach", "outreach", "contact", "engage", "identify",
        "reach", "prioritise outreach", "prioritize outreach",
    )):
        return "Contact", "status-active"
    if any(w in t for w in (
        "positioning", "talking point", "use in", "use it in", "reference",
        "leverage", "messaging", "mention in",
    )):
        return "Talking point", "status-neutral"
    if any(w in t for w in (
        "monitor", "watch", "track", "no immediate action", "no action",
    )):
        return "Watch only", "status-watch"
    return "Review", "status-neutral"


def _meta_line(source: str, url: str, published_at: Optional[str]) -> str:
    src = _safe(source) or _domain(url) or "Source"
    date_str = _fmt_pub_date(published_at)
    if date_str:
        return f"{_esc(src)} &nbsp;·&nbsp; {_esc(date_str)}"
    return _esc(src)


def _days_old(published_at: Optional[str]) -> int:
    if not published_at:
        return 0  # Unknown date → treat as fresh (conservative; don't hide undated items)
    try:
        pub = datetime.fromisoformat(published_at[:10]).replace(tzinfo=timezone.utc)
        return (datetime.now(tz=timezone.utc) - pub).days
    except Exception:
        return 999  # Non-ISO date format → treat as old to avoid surfacing stale content


def _is_gnews(url: str) -> bool:
    """True when URL is a Google News redirect — not a real publisher URL."""
    return bool(url) and "news.google.com" in url


def _item_headline(url: str, title: str, css_class: str = "item-headline") -> str:
    """Render a headline as a link, or plain span if URL is a GNews redirect."""
    t = _esc(title)
    if _is_gnews(url):
        return f'<span class="{css_class}">{t}</span>'
    return f'<a class="{css_class}" href="{_esc(url)}" target="_blank" rel="noopener">{t}</a>'


def _read_source_link(url: str) -> str:
    """Render 'Read source ↗' link, or a plain note for GNews redirects."""
    if _is_gnews(url):
        return '<span class="via-gnews">via Google News</span>'
    return f'<a class="source-link" href="{_esc(url)}" target="_blank" rel="noopener">Read source &#8599;</a>'


_BG_CONTEXT_LABEL = (
    '<div class="bg-context-label">Background Context</div>'
)


# ---------------------------------------------------------------------------
# Badge helper
# ---------------------------------------------------------------------------

def _badge(text: str, kind: str = "neutral") -> str:
    """
    Render a small pill badge.
    kind variants: owner-bd | owner-ops | owner-compliance | owner-management | owner-no
                   conf-high | conf-medium | conf-low
                   sev-high | sev-medium | sev-low
                   status-active | status-watch | status-failed | status-neutral
                   category | source | traj-up | traj-down | traj-flat | traj-baseline
    """
    return f'<span class="badge badge-{_esc(kind)}">{_esc(text)}</span>'


# ---------------------------------------------------------------------------
# CSS
# ---------------------------------------------------------------------------

CSS = """
:root {
  --navy:#0B1E3E; --navy-2:#16284A; --ink:#1B2A41; --slate:#475569; --muted:#64748B;
  --page:#EAEEF4; --card:#FFFFFF; --line:#E2E8F0; --gold:#C2A24A; --gold-deep:#A6852F;
  --green:#0F7B5A; --green-bg:#E7F4EE; --amber:#B7791F; --amber-bg:#FBF3E2;
  --red:#B4413C; --red-bg:#FBEAE9; --indigo:#3F4A8A;
  --radius:14px; --radius-sm:10px;
  --shadow:0 1px 2px rgba(11,30,62,.06),0 12px 30px -14px rgba(11,30,62,.18);
  /* legacy compat aliases used by existing render helpers */
  --page-bg:#EAEEF4; --card-bg:#FFFFFF; --navy-mid:#1B2A41; --navy-light:#1E3A5F;
  --border:#E2E8F0; --border-light:#EEF2F7;
  --text-primary:#0B1E3E; --text-secondary:#374151; --text-muted:#64748B; --text-faint:#94A3B8;
  --green-bg:#E7F4EE; --amber-bg:#FBF3E2; --red-bg:#FBEAE9;
  --purple:#6D28D9; --purple-bg:#F5F3FF; --blue:#1D4ED8; --blue-bg:#EFF6FF;
  --gold-dark:#A6852F;
  --shadow-sm:0 1px 2px rgba(11,30,62,.04),0 4px 12px rgba(11,30,62,.06);
  --shadow-md:0 1px 3px rgba(11,30,62,.06),0 8px 24px rgba(11,30,62,.08);
  --radius-xs:5px;
  --font:-apple-system,"Segoe UI",Inter,Roboto,Helvetica,Arial,sans-serif;
}

*,*::before,*::after{box-sizing:border-box;}
html,body{overflow-x:hidden;}

body{
  margin:0;padding:0;
  background:var(--page);
  font-family:var(--font);
  font-size:15px;line-height:1.55;
  color:var(--ink);
  -webkit-font-smoothing:antialiased;
}

.brief-wrap{
  max-width:1200px;
  margin:0 auto;
  padding:0 20px 64px;
}

.sr-only{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0,0,0,0);}

/* ── COMMAND HEADER ── */
.cmd-header{
  background:linear-gradient(135deg,var(--navy),var(--navy-2));
  border-radius:18px;
  margin-top:24px;
  padding:30px 34px;
  color:#fff;
  box-shadow:var(--shadow);
  position:relative;
  overflow:hidden;
}
.cmd-header::after{
  content:"";position:absolute;bottom:0;left:0;right:0;height:3px;
  background:linear-gradient(90deg,transparent 0%,var(--gold) 30%,#E8C96A 55%,var(--gold) 75%,transparent 100%);
}
.cmd-header-inner{
  display:flex;align-items:flex-start;justify-content:space-between;gap:24px;flex-wrap:wrap;
}
.cmd-eyebrow-text{
  font-size:11px;font-weight:700;letter-spacing:1.6px;text-transform:uppercase;color:var(--gold);
}
.cmd-status-dot{
  display:inline-flex;align-items:center;gap:5px;
  font-size:11px;color:#86EFAC;font-weight:600;
}
.cmd-status-dot::before{
  content:"";display:inline-block;width:7px;height:7px;border-radius:50%;
  background:#4ADE80;box-shadow:0 0 0 2px rgba(74,222,128,.25);
}
.cmd-title{
  font-size:clamp(24px,2.6vw,32px);font-weight:800;color:#fff;
  margin:6px 0;letter-spacing:-.02em;line-height:1.15;
}
.cmd-subtitle{font-size:14px;color:#C7D2E5;margin:0;line-height:1.5;}
.cmd-meta-block{text-align:right;flex-shrink:0;}
.cmd-internal-pill{
  display:inline-block;padding:4px 12px;border-radius:20px;
  font-size:11px;font-weight:700;letter-spacing:.8px;text-transform:uppercase;
  background:rgba(194,162,74,.18);color:#E8C96A;border:1px solid rgba(194,162,74,.35);margin-bottom:8px;
}
.cmd-date{font-size:12px;color:#9FB0C9;display:block;}
.cmd-wordmark-block{}
.cmd-eyebrow{display:flex;align-items:center;gap:10px;margin-bottom:10px;}

/* ── KPI GRID ── */
.kpi-grid{
  display:grid;
  grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
  gap:12px;margin-top:22px;
}
.kpi-card{
  background:rgba(255,255,255,.06);
  border:1px solid rgba(255,255,255,.14);
  border-radius:12px;padding:14px 16px;
}
.kpi-label{font-size:11px;font-weight:700;letter-spacing:.8px;text-transform:uppercase;color:#9FB0C9;margin-bottom:4px;}
.kpi-value{font-size:26px;font-weight:800;color:#fff;line-height:1;margin-bottom:4px;}
.kpi-context{font-size:11px;color:#7E92AE;line-height:1.4;}

/* ── TAB BAR ── */
.tab-bar{
  position:sticky;top:10px;z-index:20;
  margin:22px 0 28px;
  display:flex;gap:6px;padding:6px;
  background:rgba(255,255,255,.85);
  backdrop-filter:blur(8px);
  -webkit-backdrop-filter:blur(8px);
  border:1px solid var(--line);
  border-radius:14px;
  box-shadow:var(--shadow);
  overflow-x:auto;
}
.tab-btn{
  padding:9px 16px;border-radius:10px;
  font-size:13px;font-weight:600;
  color:var(--slate);background:transparent;border:0;
  white-space:nowrap;cursor:pointer;transition:background .15s,color .15s;
  font-family:var(--font);
}
.tab-btn:hover:not(.active){background:#F1F4F9;}
.tab-btn.active{background:var(--navy);color:#fff;}
.tab-btn-secondary{font-size:12px;font-weight:500;color:var(--muted);padding:8px 13px;}
.tab-btn-secondary:hover:not(.active){background:#F1F4F9;color:var(--slate);}
.tab-btn-secondary.active{background:var(--navy);color:#fff;}
.tab-divider{display:inline-block;width:1px;background:var(--line);margin:4px 4px;align-self:stretch;}

/* ── TAB PANELS ── */
.tab-panel{display:none;}
.tab-panel.active{display:block;}

/* ── SECTION STRUCTURE ── */
.section{margin-bottom:52px;}
.section-header{
  display:flex;align-items:center;gap:12px;margin-bottom:22px;
  padding-bottom:14px;border-bottom:1px solid var(--line);
}
.section-title{
  font-size:13px;font-weight:800;letter-spacing:1.6px;text-transform:uppercase;
  color:var(--navy);margin:0;
}
.section-rule{flex:1;height:1px;background:var(--border-light);}
.section-count{
  font-size:11px;font-weight:600;color:var(--muted);
  background:var(--border-light);padding:2px 8px;border-radius:10px;
}

/* ── BADGE SYSTEM ── */
.badge{
  display:inline-flex;align-items:center;padding:3px 10px;border-radius:20px;
  font-size:11px;font-weight:700;white-space:nowrap;line-height:1.4;letter-spacing:.3px;
}
.badge-owner-bd         {background:var(--blue-bg);color:var(--blue);border:1px solid #BFDBFE;}
.badge-owner-ops        {background:var(--green-bg);color:var(--green);border:1px solid #A7F3D0;}
.badge-owner-compliance {background:#FFF7ED;color:#C2410C;border:1px solid #FED7AA;}
.badge-owner-management {background:var(--navy);color:var(--gold);border:1px solid #1E3A5F;}
.badge-owner-no         {background:#F9FAFB;color:var(--muted);border:1px solid var(--line);}
.badge-conf-high        {background:var(--green-bg);color:var(--green);border:1px solid #6EE7B7;}
.badge-conf-medium      {background:var(--amber-bg);color:var(--amber);border:1px solid #FCD34D;}
.badge-conf-low         {background:var(--red-bg);color:var(--red);border:1px solid #FECACA;}
.badge-sev-high         {background:#FEE2E2;color:#DC2626;border:1px solid #FCA5A5;}
.badge-sev-medium       {background:#FEF3C7;color:#B45309;border:1px solid #FCD34D;}
.badge-sev-low          {background:#F3F4F6;color:var(--muted);border:1px solid var(--line);}
.badge-status-active    {background:var(--green-bg);color:var(--green);border:1px solid #A7F3D0;}
.badge-status-watch     {background:var(--amber-bg);color:var(--amber);border:1px solid #FCD34D;}
.badge-status-failed    {background:var(--red-bg);color:var(--red);border:1px solid #FECACA;}
.badge-status-neutral   {background:var(--border-light);color:var(--muted);border:1px solid var(--line);}
.badge-traj-up          {background:var(--green-bg);color:var(--green);border:1px solid #A7F3D0;}
.badge-traj-down        {background:var(--red-bg);color:var(--red);border:1px solid #FECACA;}
.badge-traj-flat        {background:var(--border-light);color:var(--muted);border:1px solid var(--line);}
.badge-traj-baseline    {background:var(--amber-bg);color:var(--amber);border:1px solid #FCD34D;}
.badge-category         {background:#EDE9FE;color:var(--purple);border:1px solid #C4B5FD;}
.badge-source           {background:var(--border-light);color:var(--muted);border:1px solid var(--line);}
.badge-neutral          {background:var(--border-light);color:var(--muted);border:1px solid var(--line);}

/* ── PRIORITY DECISION CARDS ── */
.priority-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:16px;}
@media(max-width:720px){.priority-grid{grid-template-columns:1fr;}}
.priority-card{
  background:var(--card);border:1px solid var(--line);border-radius:var(--radius);
  padding:22px 24px;box-shadow:var(--shadow);
  display:flex;flex-direction:column;gap:12px;border-top:3px solid var(--gold);
}
.priority-card-number{font-size:11px;font-weight:800;letter-spacing:1.2px;text-transform:uppercase;color:var(--gold-deep);}
.priority-card-text{font-size:15px;color:var(--text-secondary);line-height:1.6;flex:1;margin:0;}

/* ── BRAND WATCH ── */
.brand-watch-card{
  background:var(--navy);border-radius:var(--radius);padding:28px 32px;
  color:#EEF2F8;box-shadow:var(--shadow-md);
}
.bw-result{font-size:16px;font-weight:600;margin:0 0 20px;color:#fff;line-height:1.5;}
.bw-grid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:24px;font-size:13px;margin-bottom:20px;}
@media(max-width:620px){.bw-grid{grid-template-columns:1fr;}}
.bw-label{font-size:10px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:var(--gold);margin-bottom:5px;}
.bw-value{color:#BFD2E8;line-height:1.55;}
.bw-confidence{display:inline-block;padding:3px 10px;border-radius:12px;font-size:11px;font-weight:700;margin-top:4px;}
.bw-confidence.High    {background:#166534;color:#BBF7D0;border:1px solid #166534;}
.bw-confidence.Medium  {background:rgba(194,162,74,.25);color:#E8C96A;border:1px solid rgba(194,162,74,.4);}
.bw-confidence.Limited {background:rgba(148,163,184,.2);color:#CBD5E1;border:1px solid rgba(148,163,184,.3);}
.bw-status{font-size:13px;color:#6B8FB5;margin-top:16px;border-top:1px solid rgba(255,255,255,.08);padding-top:14px;}
.bw-mentions-label{font-size:10px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:var(--gold);margin:16px 0 8px;padding-top:16px;border-top:1px solid rgba(255,255,255,.08);}
.bw-mention-item{margin-bottom:10px;}
.bw-mention-title{color:var(--gold);font-size:14px;font-weight:600;text-decoration:none;}
.bw-mention-title:hover{text-decoration:underline;}
.bw-mention-source{font-size:12px;color:#64849D;}

/* ── WHAT MATTERS / DECISION CARDS ── */
.matters-card{
  background:var(--card);border-radius:var(--radius);padding:24px 26px;margin-bottom:16px;
  border:1px solid var(--line);border-top:4px solid var(--navy);box-shadow:var(--shadow-sm);
}
.matters-card:last-child{margin-bottom:0;}
.matters-card-top{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;margin-bottom:16px;flex-wrap:wrap;}
.matters-finding{font-size:17px;font-weight:700;color:var(--navy);line-height:1.35;margin:0;flex:1;}
.matters-badges{display:flex;gap:6px;flex-wrap:wrap;flex-shrink:0;}
.matters-grid{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:16px;}
@media(max-width:580px){.matters-grid{grid-template-columns:1fr;}}
.matters-field{}
.matters-field-label{font-size:10px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:var(--text-faint);margin-bottom:4px;}
.matters-field-value{font-size:14px;color:var(--text-secondary);line-height:1.55;}
.matters-action-row{
  display:flex;align-items:flex-start;gap:12px;
  background:#F8FAFC;border-radius:var(--radius-sm);padding:14px 16px;border:1px solid var(--border-light);
}
.matters-action-text{font-size:14px;color:var(--text-secondary);line-height:1.55;flex:1;}
.matters-evidence{
  font-size:12px;color:var(--text-faint);margin-top:12px;padding-top:10px;
  border-top:1px solid var(--border-light);display:flex;align-items:center;gap:8px;flex-wrap:wrap;
}

/* ── TRAJECTORY WATCH ── */
.traj-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:14px;}
@media(max-width:640px){.traj-grid{grid-template-columns:1fr;}}
.traj-tile{
  background:var(--card);border:1px solid var(--line);border-radius:var(--radius);
  padding:18px 20px;display:flex;align-items:flex-start;gap:14px;box-shadow:var(--shadow-sm);
}
.traj-direction-icon{font-size:20px;font-weight:900;flex-shrink:0;width:30px;text-align:center;line-height:1;padding-top:2px;}
.dir-up{color:var(--green);}.dir-down{color:var(--red);}.dir-flat{color:var(--muted);}
.traj-body{flex:1;min-width:0;}
.traj-theme{font-size:14px;font-weight:700;color:var(--text-primary);margin-bottom:6px;}
.traj-counts{font-size:12px;color:var(--muted);font-family:"SF Mono","Fira Code","Cascadia Code",monospace;margin-bottom:4px;}
.traj-implication{font-size:13px;color:var(--text-secondary);margin-top:6px;line-height:1.5;}
.traj-footer{margin-top:8px;display:flex;align-items:center;gap:8px;flex-wrap:wrap;}
.traj-empty{font-size:14px;color:var(--muted);padding:16px 0;}

/* ── CUSTOMER EXPERIENCE INTELLIGENCE ── */
.cxi-caption{
  font-size:13px;color:var(--muted);margin:-12px 0 20px;line-height:1.6;
  padding:10px 14px;background:var(--amber-bg);border-left:3px solid var(--gold);border-radius:var(--radius-xs);
}
.cxi-group-label{
  font-size:11px;font-weight:800;letter-spacing:1.2px;text-transform:uppercase;
  color:var(--muted);margin:0 0 12px;padding-bottom:8px;border-bottom:1px dashed var(--line);
}
.cxi-card{
  background:var(--card);border:1px solid var(--line);border-left:4px solid var(--gold);
  border-radius:var(--radius);padding:18px 20px;margin-bottom:12px;box-shadow:var(--shadow-sm);
}
.cxi-card-header{display:flex;align-items:flex-start;gap:10px;margin-bottom:10px;flex-wrap:wrap;}
.cxi-headline{font-size:14px;font-weight:600;color:var(--navy-light);text-decoration:none;flex:1;line-height:1.4;}
.cxi-headline:hover{text-decoration:underline;}
.cxi-tags{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px;}
.cxi-excerpt{font-size:13px;color:var(--text-secondary);margin:8px 0;font-style:italic;border-left:2px solid var(--border);padding-left:10px;}
.cxi-field-label{font-size:10px;font-weight:700;letter-spacing:.8px;text-transform:uppercase;color:var(--text-faint);margin-top:10px;margin-bottom:3px;}
.cxi-field-value{font-size:13px;color:var(--text-secondary);line-height:1.55;}
.cxi-caution{margin-top:10px;padding:8px 12px;font-size:12px;color:#92400E;background:var(--amber-bg);border-left:3px solid var(--gold);border-radius:var(--radius-xs);}
.cxi-footer{margin-top:10px;font-size:11px;color:var(--text-faint);border-top:1px solid var(--border-light);padding-top:8px;display:flex;align-items:center;gap:8px;flex-wrap:wrap;}
.cxi-empty{padding:16px 20px;border:1px solid var(--line);border-radius:var(--radius-sm);font-size:14px;color:var(--muted);background:var(--card);margin-bottom:12px;}
.cxi-group-block{margin-bottom:28px;}

/* ── ADDITIONAL INTELLIGENCE ── */
.subsection{margin-bottom:36px;}
.subsection-header{display:flex;align-items:center;gap:10px;margin-bottom:14px;}
.subsection-title{font-size:11px;font-weight:800;letter-spacing:1.2px;text-transform:uppercase;color:var(--muted);margin:0;}
.subsection-rule{flex:1;height:1px;background:var(--border-light);}
.intel-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:12px;}
@media(max-width:680px){.intel-grid{grid-template-columns:1fr;}}
.intel-card{
  background:var(--card);border:1px solid var(--line);border-radius:var(--radius);
  padding:16px 18px;box-shadow:var(--shadow-sm);display:flex;flex-direction:column;gap:0;
  border-top:3px solid var(--navy-light);
}
.intel-card.competitor{border-top-color:var(--red);}
.intel-card.introducer{border-top-color:var(--purple);}
.intel-card.pain      {border-top-color:var(--amber);}
.intel-card.film      {border-top-color:var(--gold);}
.intel-card.regulatory{border-top-color:#0369A1;}
.intel-stream-badge{display:inline-block;font-size:10px;font-weight:700;letter-spacing:.8px;text-transform:uppercase;color:var(--text-faint);margin-bottom:8px;}
.intel-card .item-headline{font-size:14px;font-weight:600;color:var(--navy-light);text-decoration:none;line-height:1.4;display:block;margin-bottom:4px;}
.intel-card .item-headline:hover{text-decoration:underline;}
.intel-card .item-meta{font-size:12px;color:var(--text-faint);}
.intel-what{font-size:13px;color:var(--text-secondary);margin-top:8px;line-height:1.5;}
.intel-why{font-size:12px;color:#1E3A5F;background:#EEF4FF;border-radius:var(--radius-xs);padding:7px 10px;margin-top:8px;line-height:1.5;}
.intel-card.competitor .intel-why{background:#FEF2F2;color:#7F1D1D;}
.intel-card.introducer .intel-why{background:var(--purple-bg);color:#4C1D95;}
.intel-card.pain       .intel-why{background:var(--amber-bg);color:#92400E;}
.intel-card-footer{margin-top:10px;padding-top:8px;border-top:1px solid var(--border-light);font-size:12px;color:var(--text-faint);}
.intel-tag{display:inline-block;font-size:10px;font-weight:600;padding:2px 8px;border-radius:10px;background:var(--blue-bg);color:var(--blue);margin-right:4px;margin-bottom:4px;}
.bg-reg-item{padding:8px 0;border-bottom:1px solid var(--border-light);}
.bg-reg-title{font-size:13px;color:var(--muted);text-decoration:none;}
.bg-reg-title:hover{text-decoration:underline;}
.bg-reg-meta{font-size:11px;color:var(--text-faint);}
.bg-context-label{margin:16px 0 8px;font-size:10px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:var(--text-faint);}

/* ── SOURCE HEALTH ── */
.source-health-strip{
  display:flex;align-items:center;gap:16px;padding:14px 18px;
  background:var(--card);border:1px solid var(--line);border-radius:var(--radius-sm);margin-bottom:6px;flex-wrap:wrap;
}
.sh-stat{font-size:13px;color:var(--text-secondary);}
.sh-stat strong{color:var(--text-primary);}
.sh-sep{color:var(--line);}
.source-health-details summary{
  cursor:pointer;font-size:12px;font-weight:600;color:var(--muted);letter-spacing:.5px;
  padding:10px 0;list-style:none;border-top:1px solid var(--border-light);display:flex;align-items:center;gap:6px;
}
.source-health-details summary::-webkit-details-marker{display:none;}
.source-health-details summary::before{content:"▶";font-size:9px;}
.source-health-details[open] summary::before{content:"▼";}
.sh-table-wrap{padding:14px 0 0;overflow-x:auto;-webkit-overflow-scrolling:touch;}
.sh-table{width:100%;border-collapse:collapse;font-size:13px;min-width:360px;}
.sh-table th{text-align:left;font-size:10px;font-weight:700;letter-spacing:.8px;text-transform:uppercase;color:var(--text-faint);padding:6px 10px;border-bottom:1px solid var(--line);}
.sh-table td{padding:8px 10px;border-bottom:1px solid var(--border-light);color:var(--text-secondary);vertical-align:middle;}
.sh-table td:last-child{text-align:right;font-weight:600;}
.sh-note{font-size:11px;color:var(--text-faint);margin:10px 0 0;line-height:1.6;}

/* ── EVIDENCE APPENDIX ── */
details summary{
  cursor:pointer;font-size:12px;font-weight:600;color:var(--muted);letter-spacing:.5px;
  padding:12px 0;list-style:none;border-top:1px solid var(--line);
}
details summary::-webkit-details-marker{display:none;}
details summary::before{content:"▶  ";font-size:9px;}
details[open] summary::before{content:"▼  ";}
.appendix-inner{padding:14px 0 0;}
.appendix-stats{
  font-size:13px;color:var(--muted);margin:0 0 14px;line-height:2;padding:12px 16px;
  background:var(--card);border:1px solid var(--line);border-radius:var(--radius-sm);
}
.appendix-table{width:100%;border-collapse:collapse;font-size:13px;}
.appendix-table th{text-align:left;font-size:10px;font-weight:700;letter-spacing:.8px;text-transform:uppercase;color:var(--text-faint);padding:6px 10px;border-bottom:1px solid var(--line);}
.appendix-table td{padding:7px 10px;border-bottom:1px solid var(--border-light);color:var(--text-secondary);}
.appendix-table td:last-child{text-align:right;font-weight:600;}
.appendix-warning{margin-top:12px;padding:10px 14px;font-size:12px;border-radius:var(--radius-xs);}
.appendix-warning.failure {background:var(--red-bg);color:#991B1B;border:1px solid #FECACA;}
.appendix-warning.low-data{background:var(--amber-bg);color:#92400E;border:1px solid #FDE68A;}

/* ── GENERIC HELPERS ── */
.source-link{display:inline-flex;align-items:center;gap:3px;font-size:12px;color:var(--navy);text-decoration:none;opacity:.65;transition:opacity .15s;}
.source-link:hover{opacity:1;text-decoration:underline;}
.via-gnews{display:inline-block;font-size:11px;color:var(--text-faint);}
.item-headline{font-size:15px;font-weight:600;color:var(--navy);text-decoration:none;line-height:1.4;}
.item-headline:hover{text-decoration:underline;}
.item-meta{font-size:12px;color:var(--text-faint);margin-top:3px;}
.item-summary{font-size:14px;color:var(--text-secondary);margin-top:6px;line-height:1.55;}
.item-relevance{font-size:13px;color:var(--muted);margin-top:5px;}
.item-relevance strong{color:var(--text-secondary);font-weight:600;}
.empty-state{padding:16px 20px;border:1px solid var(--line);border-radius:var(--radius-sm);font-size:14px;color:var(--muted);background:var(--card);}

/* ── OVERVIEW TAB HELPERS ── */
.overview-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:20px;margin-bottom:32px;}
.overview-bw-card{
  background:var(--navy);border-radius:var(--radius);padding:20px 24px;color:#EEF2F8;
  box-shadow:var(--shadow);
}
.overview-bw-label{font-size:10px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:var(--gold);margin-bottom:8px;}
.overview-sh-card{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);padding:18px 22px;box-shadow:var(--shadow);}
.overview-sh-label{font-size:10px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:var(--muted);margin-bottom:10px;}
.overview-sh-stat{font-size:22px;font-weight:800;color:var(--navy);}
.overview-sh-sub{font-size:12px;color:var(--muted);margin-top:4px;}

/* ── PROSPECTS THIS WEEK ── */
.prospects-module{
  background:linear-gradient(135deg,#0B1E3E 0%,#16284A 100%);
  border-radius:var(--radius);padding:28px 32px;margin-bottom:32px;
  box-shadow:var(--shadow-md);position:relative;overflow:hidden;
}
.prospects-module::after{
  content:"";position:absolute;bottom:0;left:0;right:0;height:3px;
  background:linear-gradient(90deg,transparent 0%,var(--gold) 30%,#E8C96A 55%,var(--gold) 75%,transparent 100%);
}
.prospects-eyebrow{
  font-size:10px;font-weight:800;letter-spacing:1.8px;text-transform:uppercase;
  color:var(--gold);margin-bottom:6px;
}
.prospects-heading{
  font-size:20px;font-weight:800;color:#fff;margin:0 0 6px;letter-spacing:-.02em;
}
.prospects-subhead{font-size:13px;color:#9FB0C9;margin:0 0 22px;line-height:1.5;}
.prospects-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:14px;}
@media(max-width:680px){.prospects-grid{grid-template-columns:1fr;}}
.prospect-card{
  background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.14);
  border-radius:var(--radius-sm);padding:18px 20px;display:flex;flex-direction:column;gap:8px;
  border-top:3px solid var(--gold);
}
.prospect-entity{font-size:16px;font-weight:800;color:#fff;margin:0;letter-spacing:-.01em;}
.prospect-field-label{font-size:10px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:var(--gold);margin-bottom:2px;}
.prospect-field-value{font-size:13px;color:#BFD2E8;line-height:1.55;}
.prospect-contact{
  background:rgba(201,168,76,.12);border:1px solid rgba(201,168,76,.3);
  border-radius:var(--radius-xs);padding:10px 12px;
}
.prospect-contact .prospect-field-value{color:#E8C96A;}
.prospect-footer{
  display:flex;align-items:center;gap:8px;flex-wrap:wrap;
  padding-top:10px;border-top:1px solid rgba(255,255,255,.08);margin-top:4px;
}
.prospect-source{font-size:11px;color:#64849D;}
.badge-prospect-contact{background:var(--gold);color:var(--navy);border:1px solid #A6852F;}
.prospects-empty{
  font-size:14px;color:#9FB0C9;padding:14px 0;line-height:1.6;
}

/* ── FOOTER ── */
.brief-footer{margin-top:56px;padding:20px 0;border-top:1px solid var(--line);font-size:12px;color:var(--text-faint);text-align:center;letter-spacing:.3px;}
.brief-footer a{color:var(--text-faint);}

/* ── RESPONSIVE ── */
@media(max-width:720px){
  .cmd-header{border-radius:12px;padding:22px 20px;}
  .cmd-header-inner{flex-direction:column;gap:16px;}
  .cmd-meta-block{text-align:left;}
  .matters-card{padding:18px;}
  .brand-watch-card{padding:20px;}
}
@media(max-width:480px){
  .brief-wrap{padding:0 14px 40px;}
  .kpi-value{font-size:22px;}
  .tab-btn{padding:8px 12px;font-size:12px;}
}
"""


# ---------------------------------------------------------------------------
# Owner helpers (used by tests)
# ---------------------------------------------------------------------------

_VALID_OWNERS = {"BD", "Ops", "Compliance", "Management", "No action required"}

def _owner_class(owner: str) -> str:
    if owner == "BD":
        return "owner-BD"
    if owner == "Ops":
        return "owner-Ops"
    if owner == "Compliance":
        return "owner-Compliance"
    if owner == "Management":
        return "owner-Management"
    return "owner-No"


# ---------------------------------------------------------------------------
# Header (command centre)
# ---------------------------------------------------------------------------

def _render_header(digest: Digest, date_str: str) -> str:
    brand_n = len(digest.brand_watch.items) if digest.brand_watch else 0
    signal_n = min(len(digest.top_signals or []), 5)
    traj_n = len(digest.trajectories or [])
    pain_n = len(digest.customer_pain or [])
    mauritius_n = len(digest.mauritius_intel or [])
    meta = digest.scrape_meta
    sources_n = 0
    if meta:
        sc = meta.selected_source_counts or meta.source_counts or {}
        sources_n = len(sc)

    def _plural(n: int, singular: str, plural: str) -> str:
        return singular if n == 1 else plural

    kpi_defs = [
        ("Brand mentions",   brand_n,     "public surface sweep"),
        ("Decision signals", signal_n,    _plural(signal_n, "priority action", "priority actions")),
        ("Trajectories",     traj_n,      "themes tracked"),
        ("Pain signals",     pain_n,      _plural(pain_n, "CX signal", "CX signals")),
        ("Mauritius intel",  mauritius_n, "FSC / market items"),
        ("Evidence sources", sources_n,   "monitored feeds"),
    ]
    kpis_html = "".join(
        f'<div class="kpi-card">'
        f'<div class="kpi-label">{_esc(label)}</div>'
        f'<div class="kpi-value">{_esc(str(val))}</div>'
        f'<div class="kpi-context">{_esc(ctx)}</div>'
        f'</div>'
        for label, val, ctx in kpi_defs
    )

    return (
        f'<div class="brief-wrap">'
        f'<header class="cmd-header">'
        f'<div class="cmd-header-inner">'
        f'<div class="cmd-wordmark-block">'
        f'<div class="cmd-eyebrow">'
        f'<span class="cmd-eyebrow-text">ARIE FINANCE LTD &nbsp;&middot;&nbsp; INTERNAL INTELLIGENCE</span>'
        f'</div>'
        f'<h1 class="cmd-title">ARIE Intelligence Command Centre</h1>'
        f'<p class="cmd-subtitle">Decision intelligence for management &mdash; week of {_esc(date_str)}</p>'
        f'</div>'
        f'<div class="cmd-meta-block">'
        f'<div style="display:flex;align-items:center;gap:6px;justify-content:flex-end;margin-bottom:8px;">'
        f'<span class="cmd-status-dot">Verified &middot; live</span>'
        f'</div>'
        f'<span class="cmd-date">Generated {_esc(date_str)}</span>'
        f'</div>'
        f'</div>'
        f'<div class="kpi-grid">{kpis_html}</div>'
        f'</header>'
    )


# ---------------------------------------------------------------------------
# Tab bar
# ---------------------------------------------------------------------------

def _render_tab_bar() -> str:
    """
    Render the sticky 8-tab navigation bar.
    Primary tabs (RM core): Prospects (default), Overview, Priority Decisions,
                             Market Pain Signals, Market Intelligence.
    Secondary tabs (muted): Trajectory Watch, Source Health, Evidence.
    """
    primary_tabs = [
        ("prospects", "Prospects",            True),   # DEFAULT active tab
        ("overview",  "Overview",             False),
        ("priority",  "Priority Decisions",   False),
        ("cx",        "Market Pain Signals",  False),
        ("market",    "Market Intelligence",  False),
    ]
    secondary_tabs = [
        ("trajectory",    "Trajectory Watch", False),
        ("source-health", "Source Health",    False),
        ("evidence",      "Evidence",         False),
    ]

    primary_html = "".join(
        f'<button class="tab-btn{" active" if active else ""}" '
        f'data-target="panel-{tid}" role="tab" '
        f'aria-selected="{"true" if active else "false"}">'
        f'{_esc(label)}</button>'
        for tid, label, active in primary_tabs
    )
    secondary_html = "".join(
        f'<button class="tab-btn tab-btn-secondary" '
        f'data-target="panel-{tid}" role="tab" aria-selected="false">'
        f'{_esc(label)}</button>'
        for tid, label, _ in secondary_tabs
    )

    return (
        f'<nav class="tab-bar" role="tablist">'
        f'{primary_html}'
        f'<span class="tab-divider" aria-hidden="true"></span>'
        f'{secondary_html}'
        f'</nav>'
    )


# ---------------------------------------------------------------------------
# 1. Executive Snapshot (Priority Decision cards)
# ---------------------------------------------------------------------------

def _render_executive_snapshot(digest: Digest) -> str:
    bullets = [_clean_codes(_safe(b)) for b in (digest.executive_summary or [])[:5] if _safe(b)]
    bullets = [b for b in bullets if b]
    if not bullets:
        return ""

    cards_html = "".join(
        f'<div class="priority-card">'
        f'<div class="priority-card-number">Priority {i + 1}</div>'
        f'<p class="priority-card-text">{_esc(b)}</p>'
        f'</div>'
        for i, b in enumerate(bullets)
    )

    return (
        f'<section class="section">'
        f'<div class="section-header">'
        f'<h2 class="section-title">Priority Decisions</h2>'
        f'<div class="section-rule"></div>'
        f'<span class="section-count">{_esc(str(len(bullets)))} items</span>'
        f'</div>'
        f'<div class="priority-grid">{cards_html}</div>'
        f'</section>'
    )


# ---------------------------------------------------------------------------
# 2. Brand Watch
# ---------------------------------------------------------------------------

def _render_brand_watch(digest: Digest) -> str:
    bw = digest.brand_watch
    if not bw:
        bw = BrandWatchResult()

    result_text = _clean_codes(_safe(bw.result, "Dedicated sweep of covered public surfaces found no new ARIE / ACBM mentions."))
    coverage_str = " &nbsp;·&nbsp; ".join(_esc(c) for c in (bw.coverage or []))
    not_covered_str = " &nbsp;·&nbsp; ".join(_esc(c) for c in (bw.not_covered or []))
    confidence = bw.coverage_confidence or "Limited"
    last_date = _safe(bw.last_mention_date, "No prior mention recorded")
    status = _clean_codes(_safe(bw.status, "No escalation required."))

    grid_html = (
        f'<div class="bw-grid">'
        f'<div>'
        f'<div class="bw-label">Covered surfaces</div>'
        f'<div class="bw-value">{coverage_str or "News feeds"}</div>'
        f'</div>'
        f'<div>'
        f'<div class="bw-label">Not covered</div>'
        f'<div class="bw-value">{not_covered_str or "&#8212;"}</div>'
        f'</div>'
        f'<div>'
        f'<div class="bw-label">Coverage confidence</div>'
        f'<div class="bw-value">'
        f'<span class="bw-confidence {_esc(confidence)}">{_esc(confidence)}</span>'
        f'</div>'
        f'</div>'
        f'</div>'
    )

    last_mention_html = ""
    if last_date:
        last_mention_html = (
            f'<div class="bw-label" style="margin-top:16px;padding-top:16px;border-top:1px solid rgba(255,255,255,.08);">Last known mention</div>'
            f'<div class="bw-value">{_esc(last_date)}</div>'
        )

    items_html = ""
    if bw.items:
        items_html = '<div class="bw-mentions-label">Mentions detected</div>'
        for item in bw.items[:5]:
            title = _safe(item.title, "Mention")
            url = item.url or ""
            source = _safe(item.source, "")
            link = (
                f'<a class="bw-mention-title" href="{_esc(url)}" target="_blank" rel="noopener">{_esc(title)}</a>'
                if url else
                f'<span style="color:#BFD2E8;font-size:14px;font-weight:600;">{_esc(title)}</span>'
            )
            items_html += (
                f'<div class="bw-mention-item">'
                f'{link}'
                f'<div class="bw-mention-source">{_esc(source)}</div>'
                f'</div>'
            )

    return (
        f'<section class="section">'
        f'<div class="section-header">'
        f'<h2 class="section-title">Brand Watch</h2>'
        f'<div class="section-rule"></div>'
        f'</div>'
        f'<div class="brand-watch-card">'
        f'<p class="bw-result">{_esc(result_text)}</p>'
        f'{grid_html}'
        f'{last_mention_html}'
        f'{items_html}'
        f'<div class="bw-status">{_esc(status)}</div>'
        f'</div>'
        f'</section>'
    )


# ---------------------------------------------------------------------------
# 3. What Matters This Week (decision-first cards)
# ---------------------------------------------------------------------------

_STOP_WORDS = frozenset({
    "the", "a", "an", "of", "in", "to", "for", "and", "or", "is", "are",
    "was", "at", "on", "by", "its", "it", "with", "has", "have", "be",
    "this", "that", "from", "as", "new", "said", "will", "been", "also",
})

_ACRONYM_RE = re.compile(r"\b[A-Z]{2,}\b")


def _wm_normalise(text: str) -> str:
    """Lowercase, normalise British/US spelling variants, strip punctuation."""
    t = text.lower()
    t = t.replace("licence", "license").replace("licenced", "licensed").replace("licencing", "licensing")
    t = re.sub(r"[^a-z0-9 ]", " ", t)
    return t


def _wm_jaccard(a: str, b: str) -> float:
    """Word-level Jaccard on normalised, non-stop, length > 2 words."""
    ta = {w for w in _wm_normalise(a).split() if w not in _STOP_WORDS and len(w) > 2}
    tb = {w for w in _wm_normalise(b).split() if w not in _STOP_WORDS and len(w) > 2}
    union = ta | tb
    return len(ta & tb) / len(union) if union else 0.0


def _proxy_entity_set(sig: "OpportunityRadarItem") -> set:
    """
    Extract uppercase acronyms (FSC, AXI, BCP, UAE…) from text as proxy entities.
    Used when sig.entities is empty — Claude frequently omits entity extraction.
    """
    text = f"{sig.what_happened or sig.opportunity or ''} {sig.source_title or ''}"
    return {m.group().lower() for m in _ACRONYM_RE.finditer(text)}


def _dedup_what_matters(signals: List[OpportunityRadarItem]) -> List[OpportunityRadarItem]:
    """
    Post-selection dedup for What Matters cards.

    A card is suppressed when ALL of the following hold against any already-kept card:
      1. At least one shared named entity (same regulator/company)
      2. Content similarity >= 0.30 on what_happened + why_it_matters words
      3. Published within 7 days of each other

    Fallback: suppress if content similarity alone >= 0.50 (catches entity-less dupes).

    Keeps the higher-source-count item; falls back to higher confidence.
    Does NOT touch digest.top_signals — only affects the rendered card list.
    """
    _CONF_RANK = {"High": 3, "Medium": 2, "Low": 1}

    def _is_better(challenger: OpportunityRadarItem, incumbent: OpportunityRadarItem) -> bool:
        if (challenger.supporting_source_count or 1) > (incumbent.supporting_source_count or 1):
            return True
        return _CONF_RANK.get(challenger.confidence, 0) > _CONF_RANK.get(incumbent.confidence, 0)

    kept: List[OpportunityRadarItem] = []

    for sig in signals:
        sig_text = f"{sig.what_happened or sig.opportunity} {sig.why_it_matters}".strip()
        sig_entities = {e.lower() for e in (sig.entities or [])} or _proxy_entity_set(sig)
        sig_date = (sig.published_at or "")[:10]

        duplicate_idx: Optional[int] = None

        for idx, prior in enumerate(kept):
            prior_entities = {e.lower() for e in (prior.entities or [])} or _proxy_entity_set(prior)
            shared = sig_entities & prior_entities
            sim = _wm_jaccard(sig_text, f"{prior.what_happened or prior.opportunity} {prior.why_it_matters}")

            # what_happened-only similarity — more stable than full text because Claude's
            # why_it_matters paragraphs often diverge even for the same event, diluting sim.
            wh_only_sim = (
                _wm_jaccard(sig.what_happened, prior.what_happened)
                if (sig.what_happened and prior.what_happened) else 0.0
            )

            # Criterion A: entity overlap anchors same-event detection.
            # Use max(full_text, what_happened) Jaccard so divergent why_it_matters can't
            # mask a genuine PR-rewrite duplicate. Threshold 0.18 is safe when shared
            # acronyms (FSC, BCP…) already confirm the same regulated entity / event.
            entity_match = bool(shared) and max(sim, wh_only_sim) >= 0.18

            # Criterion B: high similarity alone (entity-less fallback)
            high_sim = sim >= 0.50

            # Criterion B2: what_happened sentence similarity alone (no entity requirement)
            high_wh_sim = wh_only_sim >= 0.30

            if not (entity_match or high_sim or high_wh_sim):
                continue

            # Criterion C: 7-day publication window (skip check if either date missing)
            prior_date = (prior.published_at or "")[:10]
            if sig_date and prior_date:
                try:
                    delta = abs(
                        (datetime.fromisoformat(sig_date) - datetime.fromisoformat(prior_date)).days
                    )
                    if delta > 7:
                        continue
                except (ValueError, TypeError):
                    pass

            duplicate_idx = idx
            break

        if duplicate_idx is None:
            kept.append(sig)
        elif _is_better(sig, kept[duplicate_idx]):
            kept[duplicate_idx] = sig  # swap for the better-sourced version

    return kept


def _render_what_matters(digest: Digest) -> str:
    signals = [s for s in (digest.top_signals or []) if s.source_url]
    signals = _dedup_what_matters(signals)
    # Prefer signals within 14 days; fall back to older ones only to reach the 3-card minimum
    fresh = [s for s in signals if _days_old(s.published_at) <= 14]
    stale = [s for s in signals if _days_old(s.published_at) > 14]
    if len(fresh) >= 3:
        signals = fresh[:5]
    else:
        # Top up with the least-old stale signals until we have 3
        needed = 3 - len(fresh)
        signals = (fresh + stale[:needed])[:5]
    if not signals:
        return ""

    cards_html = ""
    for sig in signals:
        finding = _clean_codes(_safe(sig.what_happened or sig.opportunity, "Signal"))
        why = _clean_codes(_safe(sig.why_it_matters, "Assess relevance to ARIE's current pipeline."))
        action = _clean_codes(_safe(sig.suggested_action, "Review and flag to relevant team."))
        owner = sig.suggested_owner if sig.suggested_owner in _VALID_OWNERS else "BD"
        confidence = _safe(sig.confidence, "Medium")
        source_name = _safe(sig.source, _domain(sig.source_url))
        pub_date = _fmt_pub_date(sig.published_at)
        corroboration = "Multiple sources" if (sig.supporting_source_count or 1) > 1 else "1 source"
        evidence = source_name
        if pub_date:
            evidence += f" &nbsp;·&nbsp; {_esc(pub_date)}"
        evidence += f" &nbsp;·&nbsp; {_esc(corroboration)}"

        owner_badge_kind = {
            "BD": "owner-bd",
            "Ops": "owner-ops",
            "Compliance": "owner-compliance",
            "Management": "owner-management",
        }.get(owner, "owner-no")
        conf_badge_kind = f"conf-{confidence.lower()}"
        action_label, action_kind = _action_kind(action)

        entities_html = ""
        if sig.entities:
            entities_html = ", ".join(_esc(_clean_codes(e)) for e in sig.entities[:4])
        else:
            region = _safe(sig.region, "")
            entities_html = _esc(region.title() if region else sig.category or "Commercial signal")

        # Evidence link — suppress "(URL not decoded)" for GNews
        if _is_gnews(sig.source_url):
            evidence_link = f'<span style="color:var(--text-muted);">{evidence}</span>'
        else:
            evidence_link = (
                f'<a href="{_esc(sig.source_url)}" target="_blank" rel="noopener" '
                f'style="color:var(--navy);text-decoration:underline;">{evidence}</a>'
            )

        cards_html += (
            f'<div class="matters-card">'
            f'<div class="matters-card-top">'
            f'<p class="matters-finding">{_esc(finding)}</p>'
            f'<div class="matters-badges">'
            + _badge(owner, owner_badge_kind)
            + _badge(f"{confidence} confidence", conf_badge_kind)
            + _badge(action_label, action_kind)
            + f'</div>'
            f'</div>'
            f'<div class="matters-grid">'
            f'<div class="matters-field">'
            f'<div class="matters-field-label">Why it matters to ARIE</div>'
            f'<div class="matters-field-value">{_esc(why)}</div>'
            f'</div>'
            f'<div class="matters-field">'
            f'<div class="matters-field-label">Entities / context</div>'
            f'<div class="matters-field-value">{entities_html}</div>'
            f'</div>'
            f'</div>'
            f'<div class="matters-action-row">'
            + _badge(owner, owner_badge_kind)
            + f'<span class="matters-action-text">{_esc(action)}</span>'
            f'</div>'
            f'<div class="matters-evidence">Evidence: {evidence_link}</div>'
            f'</div>'
        )

    return (
        f'<section class="section">'
        f'<div class="section-header">'
        f'<h2 class="section-title">What Matters This Week</h2>'
        f'<div class="section-rule"></div>'
        f'<span class="section-count">{_esc(str(len(signals)))} signals</span>'
        f'</div>'
        f'{cards_html}'
        f'</section>'
    )


# ---------------------------------------------------------------------------
# 4. Trajectory Watch (compact tiles)
# ---------------------------------------------------------------------------

_DIR_ARROW = {"up": "↑", "down": "↓", "flat": "→"}
_DIR_CLASS = {"up": "dir-up", "down": "dir-down", "flat": "dir-flat"}


def _render_trajectory_watch(digest: Digest) -> str:
    trajs = digest.trajectories or []
    if not trajs:
        return ""

    # Separate trajectories that have real history from pure-baseline ones
    real_trajs = [t for t in trajs if t.counts_by_period and len(t.counts_by_period) >= 2]

    # If ALL trajectories are baseline (no prior history), render one honest line instead
    # of N identical "Tracking started — baseline forming" tiles (zero information).
    if not real_trajs:
        return (
            f'<section class="section">'
            f'<div class="section-header">'
            f'<h2 class="section-title">Trajectory Watch</h2>'
            f'<div class="section-rule"></div>'
            f'</div>'
            f'<p class="traj-empty">'
            f'Trajectory data is still collecting — the first week-over-week comparison will be available next week.'
            f'</p>'
            f'</section>'
        )

    tiles_html = ""
    for traj in real_trajs:
        implication = _safe(traj.implication, "")
        conf = _safe(traj.confidence, "")
        periods = traj.counts_by_period

        arrow = _DIR_ARROW.get(traj.direction, "→")
        dir_class = _DIR_CLASS.get(traj.direction, "dir-flat")
        sorted_periods = sorted(periods.items())
        count_display = " → ".join(str(c) for _, c in sorted_periods) + " mentions"
        badge_kind = {"up": "traj-up", "down": "traj-down", "flat": "traj-flat"}.get(
            traj.direction, "traj-flat"
        )
        label = {"up": "Rising", "down": "Falling", "flat": "Stable"}.get(
            traj.direction, "Stable"
        )
        traj_badge = _badge(label, badge_kind)
        tile_implication = implication

        tiles_html += (
            f'<div class="traj-tile">'
            f'<div class="traj-direction-icon {_esc(dir_class)}">{_esc(arrow)}</div>'
            f'<div class="traj-body">'
            f'<div class="traj-theme">{_esc(traj.theme)}</div>'
        )
        if count_display:
            tiles_html += f'<div class="traj-counts">{_esc(count_display)}</div>'
        if tile_implication:
            tiles_html += f'<div class="traj-implication">{_esc(tile_implication)}</div>'
        tiles_html += f'<div class="traj-footer">{traj_badge}'
        if conf:
            tiles_html += f'<span style="font-size:11px;color:var(--text-faint);">Confidence: {_esc(conf)}</span>'
        tiles_html += f'</div></div></div>'

    return (
        f'<section class="section">'
        f'<div class="section-header">'
        f'<h2 class="section-title">Trajectory Watch</h2>'
        f'<div class="section-rule"></div>'
        f'</div>'
        f'<div class="traj-grid">{tiles_html}</div>'
        f'</section>'
    )


# ---------------------------------------------------------------------------
# 5. Additional Intelligence helpers
# ---------------------------------------------------------------------------

def _render_intel_card(item: object, stream_label: str, card_class: str = "") -> str:
    """Render a generic intel-card for any Additional Intelligence subsection item."""
    raw_title = _safe(getattr(item, "title", ""), stream_label + " item")
    title = _strip_source_suffix(_clean_codes(raw_title))
    raw_what = _safe(getattr(item, "what_happened", None) or raw_title, raw_title)
    what = _clean_codes(raw_what)
    # Suppress what_happened if it closely echoes the title
    what_words = set(what.lower().split())
    title_words = set(title.lower().split())
    what_overlap = len(what_words & title_words) / max(len(what_words | title_words), 1)
    if what_overlap >= 0.7:
        what = ""
    raw_why = _safe(getattr(item, "why_it_matters", "") or getattr(item, "arie_relevance", "") or "", "")
    why = _clean_codes(raw_why)
    why_label = "Why it matters"
    category = _safe(
        getattr(item, "category", "") or getattr(item, "signal_type", "") or
        getattr(item, "competitor", "") or getattr(item, "entity", "") or
        getattr(item, "platform_mentioned", ""), ""
    )
    url = _safe(getattr(item, "source_url", "") or getattr(item, "url", ""), "")
    pub = getattr(item, "published_at", None)
    meta = _meta_line(getattr(item, "source", ""), url, pub)
    tag = f'<span class="intel-tag">{_esc(category)}</span>' if category else ""
    extra_class = f" {card_class}" if card_class else ""

    # Action kind badge for scannability
    action_text = _clean_codes(_safe(getattr(item, "suggested_action", "") or why, ""))
    action_label, action_kind = _action_kind(action_text)

    out = (
        f'<div class="intel-card{extra_class}">'
        f'<span class="intel-stream-badge">{_esc(stream_label)}</span>'
        f'&nbsp;{_badge(action_label, action_kind)}'
        f'{tag}'
        + _item_headline(url, title)
        + f'<div class="item-meta">{meta}</div>'
    )
    if what:
        out += f'<div class="intel-what">{_esc(what)}</div>'
    if why:
        out += f'<div class="intel-why"><strong>{_esc(why_label)}:</strong> {_esc(why)}</div>'
    out += f'<div class="intel-card-footer">' + _read_source_link(url) + f'</div></div>'
    return out


def _render_bg_context(items_html: str) -> str:
    return _BG_CONTEXT_LABEL + items_html


def _subsection_wrap(title: str, content_html: str) -> str:
    return (
        f'<div class="subsection">'
        f'<div class="subsection-header">'
        f'<h3 class="subsection-title">{_esc(title)}</h3>'
        f'<div class="subsection-rule"></div>'
        f'</div>'
        f'<div class="intel-grid">{content_html}</div>'
        f'</div>'
    )


_INTEL_CUTOFF_DAYS = 30  # "current" window for intel subsections (raised from 14)


def _dedup_intel_items(items: list) -> list:
    """
    Deduplicate a list of intel items within a subsection by normalised title.
    Items whose title is near-identical (word-Jaccard >= 0.65, stop-words excluded)
    to an already-kept item are dropped. First occurrence wins.
    """
    kept: list = []
    kept_titles: list[str] = []
    for item in items:
        raw = _safe(getattr(item, "title", "") or getattr(item, "pain_point", "") or "", "")
        norm = {w for w in raw.lower().split() if w not in _STOP_WORDS and len(w) > 2}
        duplicate = False
        for kt in kept_titles:
            kt_words = {w for w in kt.lower().split() if w not in _STOP_WORDS and len(w) > 2}
            union = norm | kt_words
            if union and len(norm & kt_words) / len(union) >= 0.65:
                duplicate = True
                break
        if not duplicate:
            kept.append(item)
            kept_titles.append(raw)
    return kept


def _subsection_wrap_smart(
    title: str,
    fresh_html: str,
    older_html: str,
) -> str:
    """
    Render a subsection avoiding an empty main grid.
    - If fresh_html has content → render grid + optional Background Context block.
    - If fresh_html is empty but older_html exists → render older items directly (no BG label, no empty grid).
    - Never emits <div class="intel-grid"></div> (empty grid).
    """
    if fresh_html:
        header = (
            f'<div class="subsection">'
            f'<div class="subsection-header">'
            f'<h3 class="subsection-title">{title}</h3>'
            f'<div class="subsection-rule"></div>'
            f'</div>'
            f'<div class="intel-grid">{fresh_html}</div>'
        )
        if older_html:
            header += _render_bg_context(older_html)
        header += "</div>"
        return header
    elif older_html:
        # No fresh items — render older items directly without BG label
        return (
            f'<div class="subsection">'
            f'<div class="subsection-header">'
            f'<h3 class="subsection-title">{title}</h3>'
            f'<div class="subsection-rule"></div>'
            f'</div>'
            f'<div class="intel-grid">{older_html}</div>'
            f'</div>'
        )
    return ""


def _render_mauritius_subsection(digest: Digest) -> str:
    items = _dedup_intel_items([m for m in (digest.mauritius_intel or []) if m.source_url])
    if not items:
        return ""
    fresh = [m for m in items if _days_old(m.published_at) <= _INTEL_CUTOFF_DAYS]
    older = [m for m in items if _days_old(m.published_at) > _INTEL_CUTOFF_DAYS]
    fresh_html = "".join(_render_intel_card(m, "Mauritius Intelligence") for m in fresh[:6])
    older_html = "".join(_render_intel_card(m, "Mauritius Intelligence") for m in older[:3])
    return _subsection_wrap_smart("Mauritius", fresh_html, older_html)


def _render_competitor_subsection(digest: Digest) -> str:
    items = _dedup_intel_items([c for c in (digest.competitor_intel or []) if c.source_url])
    if not items:
        return ""
    fresh = [c for c in items if _days_old(c.published_at) <= _INTEL_CUTOFF_DAYS]
    older = [c for c in items if _days_old(c.published_at) > _INTEL_CUTOFF_DAYS]
    fresh_html = "".join(_render_intel_card(c, "Competitor Intelligence", "competitor") for c in fresh[:5])
    older_html = "".join(_render_intel_card(c, "Competitor Intelligence", "competitor") for c in older[:3])
    return _subsection_wrap_smart("Competitor", fresh_html, older_html)


def _render_pain_subsection(digest: Digest) -> str:
    items = [p for p in (digest.customer_pain or []) if p.source_url]
    if not items:
        return ""
    fresh = [p for p in items if _days_old(getattr(p, "published_at", None)) <= _INTEL_CUTOFF_DAYS]
    older = [p for p in items if _days_old(getattr(p, "published_at", None)) > _INTEL_CUTOFF_DAYS]
    fresh_html = "".join(_render_intel_card(p, "Customer Pain Intelligence", "pain") for p in fresh[:5])
    older_html = "".join(_render_intel_card(p, "Customer Pain Intelligence", "pain") for p in older[:3])
    return _subsection_wrap_smart("Customer Pain", fresh_html, older_html)


def _render_introducer_subsection(digest: Digest) -> str:
    items = _dedup_intel_items([i for i in (digest.introducer_intel or []) if i.source_url])
    if not items:
        return ""
    fresh = [i for i in items if _days_old(i.published_at) <= _INTEL_CUTOFF_DAYS]
    older = [i for i in items if _days_old(i.published_at) > _INTEL_CUTOFF_DAYS]
    fresh_html = "".join(_render_intel_card(i, "Introducer Intelligence", "introducer") for i in fresh[:5])
    older_html = "".join(_render_intel_card(i, "Introducer Intelligence", "introducer") for i in older[:3])
    return _subsection_wrap_smart("Introducer &amp; CSP", fresh_html, older_html)


def _render_film_subsection(digest: Digest) -> str:
    items = _dedup_intel_items([fw for fw in (digest.film_watch or []) if fw.source_url])
    if not items:
        return ""
    fresh = [fw for fw in items if _days_old(fw.published_at) <= _INTEL_CUTOFF_DAYS]
    older = [fw for fw in items if _days_old(fw.published_at) > _INTEL_CUTOFF_DAYS]
    fresh_html = "".join(_render_intel_card(fw, "Film Production Watch", "film") for fw in fresh[:5])
    older_html = "".join(_render_intel_card(fw, "Film Production Watch", "film") for fw in older[:3])
    return _subsection_wrap_smart("Film Production", fresh_html, older_html)


def _render_regulatory_subsection(digest: Digest) -> str:
    _REG_RELEVANCE_KEYWORDS: frozenset = frozenset({
        "fatf", "aml", "kyc", "fsc", "correspondent", "payment system",
        "cross-border", "fintech", "digital asset", "crypto", "stablecoin",
        "cbdc", "financial crime", "money laundering", "sanctions",
        "licensing", "payment service", "e-money", "vasp",
    })
    items = _dedup_intel_items([
        i for i in (digest.regulatory_watch or [])
        if i.url and any(kw in f"{i.title} {i.one_liner}".lower() for kw in _REG_RELEVANCE_KEYWORDS)
    ])
    fresh = [i for i in items if _days_old(i.published_at) <= _INTEL_CUTOFF_DAYS]
    older = [i for i in items if _days_old(i.published_at) > _INTEL_CUTOFF_DAYS]
    if not fresh and not older:
        return ""

    def _reg_card(item) -> str:
        one_liner = _safe(item.one_liner, "")
        meta = _meta_line(item.source, item.url, item.published_at)
        c = (
            f'<div class="intel-card regulatory">'
            f'<span class="intel-stream-badge">Regulatory Watch</span>'
            + _item_headline(item.url, item.title)
            + f'<div class="item-meta">{meta}</div>'
        )
        if one_liner:
            c += f'<div class="intel-why">{_esc(one_liner)}</div>'
        c += f'<div class="intel-card-footer">' + _read_source_link(item.url) + f'</div></div>'
        return c

    def _reg_bg_card(item) -> str:
        meta = _meta_line(item.source, item.url, item.published_at)
        gnews = _is_gnews(item.url)
        title_html = (
            f'<span class="bg-reg-title">{_esc(item.title)}</span>'
            if gnews else
            f'<a class="bg-reg-title" href="{_esc(item.url)}" target="_blank" rel="noopener">{_esc(item.title)}</a>'
        )
        return (
            f'<div class="bg-reg-item">'
            f'{title_html}'
            f'<div class="bg-reg-meta">{meta}</div>'
            f'</div>'
        )

    fresh_html = "".join(_reg_card(i) for i in fresh[:5])
    older_html = "".join(_reg_bg_card(i) for i in older[:3])
    return _subsection_wrap_smart("Regulatory", fresh_html, older_html)


# ---------------------------------------------------------------------------
# 6. Customer Experience Intelligence
# ---------------------------------------------------------------------------

_PAIN_TYPE_LABELS: dict = {
    "frozen_account":       "Frozen Account",
    "account_closure":      "Account Closure",
    "kyc_friction":         "KYC / KYB Friction",
    "delayed_onboarding":   "Delayed Onboarding",
    "transfer_delay":       "Transfer Delay",
    "payment_rejection":    "Payment Rejection",
    "poor_support":         "Poor Support",
    "fee_pricing":          "Fee / Pricing",
    "compliance_derisking": "Compliance / De-risking",
    "other":                "Other",
}

_SOURCE_TYPE_LABELS: dict = {
    "review":               "Direct user review",
    "forum_discussion":     "Forum discussion",
    "real_user_complaint":  "Direct user complaint",
    "news_about_complaint": "Reported in media",
    "regulatory_notice":    "Regulatory notice",
    "unknown":              "Source unknown",
}

_DIRECT_SOURCE_TYPES = frozenset({"review", "forum_discussion", "real_user_complaint"})
_MEDIA_SOURCE_TYPES  = frozenset({"news_about_complaint", "regulatory_notice", "unknown"})


def _render_cxi_card(item: CustomerPainItem) -> str:
    """Render a single Customer Experience Intelligence card."""
    severity = (item.severity or "low").lower()
    sev_badge = _badge(severity.capitalize(), f"sev-{severity}")

    pain_type_label = _PAIN_TYPE_LABELS.get(item.pain_type or "", "")
    tags_html = '<div class="cxi-tags">'
    if pain_type_label:
        tags_html += f'<span class="badge badge-category">{_esc(pain_type_label)}</span>'
    if item.platform_mentioned:
        tags_html += f'<span class="badge badge-source">{_esc(item.platform_mentioned)}</span>'
    tags_html += '</div>'

    # Strip source suffix from headline (e.g. "Title - Yahoo Finance" → "Title")
    pain_point = _strip_source_suffix(_clean_codes(item.pain_point or ""))

    headline_html = (
        f'<span class="cxi-headline">{_esc(pain_point)}</span>'
        if _is_gnews(item.source_url)
        else (
            f'<a class="cxi-headline" href="{_esc(item.source_url)}" '
            f'target="_blank" rel="noopener">{_esc(pain_point)}</a>'
        )
    )

    # Suppress excerpt if it approximately equals the headline (ratio > 0.7 word overlap)
    excerpt_html = ""
    if item.evidence_excerpt:
        exc_clean = _clean_codes(item.evidence_excerpt)
        exc_words = set(exc_clean.lower().split())
        headline_words = set(pain_point.lower().split())
        overlap = len(exc_words & headline_words) / max(len(exc_words | headline_words), 1)
        if overlap < 0.7:
            excerpt_html = f'<div class="cxi-excerpt">{_esc(exc_clean)}</div>'

    arie_rel_html = ""
    if item.arie_relevance:
        arie_rel_html = (
            f'<div class="cxi-field-label">What this tells us</div>'
            f'<div class="cxi-field-value">{_esc(_clean_codes(item.arie_relevance))}</div>'
        )

    action_html = ""
    if item.recommended_internal_action:
        action_html = (
            f'<div class="cxi-field-label">Suggested internal action</div>'
            f'<div class="cxi-field-value">{_esc(_clean_codes(item.recommended_internal_action))}</div>'
        )

    caution_html = (
        f'<div class="cxi-caution">&#9888; {_esc(item.compliance_caution)}</div>'
        if item.compliance_caution else ""
    )

    src_type_label = _SOURCE_TYPE_LABELS.get(item.source_type or "", item.source_type or "")
    confidence_label = (item.confidence or "").capitalize()
    sq_label = (item.source_quality or "").capitalize()
    footer_parts = []
    if confidence_label:
        footer_parts.append(f"Confidence: {_esc(confidence_label)}")
    if sq_label:
        footer_parts.append(f"Source quality: {_esc(sq_label)}")
    if src_type_label:
        footer_parts.append(_esc(src_type_label))
    footer_meta = " &nbsp;·&nbsp; ".join(footer_parts)
    footer_html = (
        f'<div class="cxi-footer">'
        f'{sev_badge}'
        + (f'<span>{footer_meta}</span>' if footer_meta else "")
        + f' &nbsp; {_read_source_link(item.source_url)}'
        f'</div>'
    )

    return (
        f'<div class="cxi-card">'
        f'<div class="cxi-card-header">'
        f'<div style="flex:1">'
        f'{tags_html}'
        f'{headline_html}'
        f'</div>'
        f'</div>'
        f'{excerpt_html}'
        f'{arie_rel_html}'
        f'{action_html}'
        f'{caution_html}'
        f'{footer_html}'
        f'</div>'
    )


def _render_customer_experience(digest: Digest) -> str:
    """
    Render the Customer Experience Intelligence section — a top-level section distinct from
    Additional Intelligence. Shows direct user complaints first, then media-reported items.
    Never crashes on empty data; empty-state messages shown when a group has no items.
    """
    from enrich import is_cx_relevant

    # Authoritative relevance gate: never render generic AI/tech/fintech/funding
    # discussion as Customer Experience Intelligence, even if it reached the digest.
    # Title-anchored (headline + platform only, NOT the body excerpt): an off-topic
    # post whose body merely mentions "cross-border payments" must NOT qualify on
    # that incidental mention. The complaint must be evident in the headline itself.
    all_items = [
        p for p in (digest.customer_pain or [])
        if p.source_url
        and is_cx_relevant(f"{p.pain_point} {p.platform_mentioned}")
    ]

    direct_items = [p for p in all_items if (p.source_type or "unknown") in _DIRECT_SOURCE_TYPES]
    media_items  = [p for p in all_items if (p.source_type or "unknown") in _MEDIA_SOURCE_TYPES]

    def _split_freshness(items: List[CustomerPainItem]):
        fresh = [p for p in items if _days_old(getattr(p, "published_at", None)) <= 14]
        older = [p for p in items if _days_old(getattr(p, "published_at", None)) > 14]
        return fresh, older

    # --- Direct user complaints group ---
    d_fresh, d_older = _split_freshness(direct_items)
    if not direct_items:
        direct_html = (
            '<div class="cxi-empty">'
            'No verified real-user complaint signals found in this run.'
            '</div>'
        )
    else:
        direct_html = "".join(_render_cxi_card(p) for p in d_fresh[:5])
        if d_older:
            direct_html += _render_bg_context(
                "".join(_render_cxi_card(p) for p in d_older[:3])
            )

    # --- Media-reported group ---
    m_fresh, m_older = _split_freshness(media_items)
    if media_items:
        media_html = "".join(_render_cxi_card(p) for p in m_fresh[:5])
        if m_older:
            media_html += _render_bg_context(
                "".join(_render_cxi_card(p) for p in m_older[:3])
            )
        media_group_html = (
            f'<div class="cxi-group-block">'
            f'<div class="cxi-group-label">Reported in media</div>'
            f'{media_html}'
            f'</div>'
        )
    else:
        media_group_html = ""

    return (
        f'<section class="section">'
        f'<div class="section-header">'
        f'<h2 class="section-title">Market Pain Signals'
        f'<span class="sr-only"> (Customer Experience Intelligence)</span>'
        f'</h2>'
        f'<div class="section-rule"></div>'
        f'</div>'
        f'<p class="cxi-caption">'
        f'Market signals on payment &amp; banking pain points &mdash; for CX, product, and risk awareness. '
        f'Not for outreach.'
        f'</p>'
        f'<div class="cxi-group-block">'
        f'<div class="cxi-group-label">Direct user complaints</div>'
        f'{direct_html}'
        f'</div>'
        f'{media_group_html}'
        f'</section>'
    )


def _render_additional_intelligence(digest: Digest) -> str:
    # Note: Customer Pain is now rendered as its own top-level section via
    # _render_customer_experience(). _render_pain_subsection is intentionally
    # excluded here to avoid duplication.
    subsections = (
        _render_mauritius_subsection(digest)
        + _render_competitor_subsection(digest)
        + _render_introducer_subsection(digest)
        + _render_film_subsection(digest)
        + _render_regulatory_subsection(digest)
    )
    if not subsections.strip():
        return ""
    return (
        f'<section class="section">'
        f'<div class="section-header">'
        f'<h2 class="section-title">Additional Intelligence</h2>'
        f'<div class="section-rule"></div>'
        f'</div>'
        f'{subsections}'
        f'</section>'
    )


# ---------------------------------------------------------------------------
# 7. Source Health trust panel
# ---------------------------------------------------------------------------

_HEALTH_PILL_STYLES: dict = {
    "active":         "background:#D1FAE5;color:#065F46;border:1px solid #6EE7B7;",
    "partial":        "background:#FEF3C7;color:#92400E;border:1px solid #FCD34D;",
    "failed":         "background:#FEE2E2;color:#991B1B;border:1px solid #FCA5A5;",
    "not_configured": "background:#F3F4F6;color:#6B7280;border:1px solid #D1D5DB;",
}
_HEALTH_PILL_LABELS: dict = {
    "active":         "active",
    "partial":        "partial",
    "failed":         "failed",
    "not_configured": "not configured",
}


def _render_source_health(digest: Digest) -> str:
    """
    Render a compact source-health strip.
    Returns "" when digest.source_health is empty (no section rendered).
    Groups: failed + not_configured are already sorted first by build_source_health.
    """
    items = digest.source_health or []
    if not items:
        return ""

    active_n = sum(1 for i in items if i.status == "active")
    failed_n = sum(1 for i in items if i.status == "failed")
    not_cfg_n = sum(1 for i in items if i.status == "not_configured")

    strip_html = (
        f'<div class="source-health-strip">'
        f'<span class="sh-stat"><strong>{_esc(str(active_n))}</strong> active</span>'
    )
    if failed_n:
        strip_html += (
            f'<span class="sh-sep">·</span>'
            f'<span class="sh-stat" style="color:var(--red);">'
            f'<strong>{_esc(str(failed_n))}</strong> failed</span>'
        )
    if not_cfg_n:
        strip_html += (
            f'<span class="sh-sep">·</span>'
            f'<span class="sh-stat" style="color:var(--text-muted);">'
            f'<strong>{_esc(str(not_cfg_n))}</strong> not configured</span>'
        )
    strip_html += (
        f'<span class="sh-sep">·</span>'
        f'<span class="sh-stat">{_esc(str(len(items)))} total monitored feeds</span>'
        f'</div>'
    )

    rows_html = ""
    for item in items:
        pill_style = _HEALTH_PILL_STYLES.get(item.status, _HEALTH_PILL_STYLES["failed"])
        pill_label = _HEALTH_PILL_LABELS.get(item.status, item.status)
        count_text = _esc(str(item.items_collected)) if item.items_collected else "0"
        error_html = (
            f'<span style="color:var(--text-muted);font-size:11px;margin-left:6px;">{_esc(item.error_summary)}</span>'
            if item.error_summary else ""
        )
        category_html = (
            f'<span style="color:var(--text-faint);font-size:11px;">{_esc(item.category)}</span>'
            if item.category else ""
        )
        rows_html += (
            f'<tr>'
            f'<td>{_esc(item.source_name)}</td>'
            f'<td>{category_html}</td>'
            f'<td>'
            f'<span style="display:inline-block;padding:2px 8px;border-radius:10px;font-size:11px;'
            f'font-weight:700;{pill_style}">{_esc(pill_label)}</span>'
            f'{error_html}</td>'
            f'<td>{count_text}</td>'
            f'</tr>'
        )

    details_html = (
        f'<details class="source-health-details" style="margin-top:6px;">'
        f'<summary>Source Health &mdash; {_esc(str(len(items)))} monitored feeds</summary>'
        f'<div class="sh-table-wrap">'
        f'<table class="sh-table">'
        f'<thead><tr>'
        f'<th>Source</th><th>Category</th><th>Status</th><th style="text-align:right;">Items</th>'
        f'</tr></thead>'
        f'<tbody>{rows_html}</tbody>'
        f'</table>'
        f'<p class="sh-note">Source health reflects the most recent scrape. '
        f'Failed or unconfigured sources are shown so gaps in coverage are visible.</p>'
        f'</div>'
        f'</details>'
    )

    return f'<div style="margin-bottom:36px;">{strip_html}{details_html}</div>'


# ---------------------------------------------------------------------------
# 8. Evidence Appendix
# ---------------------------------------------------------------------------

def _render_evidence_appendix(digest: Digest) -> str:
    meta = digest.scrape_meta
    if not meta:
        return (
            f'<details><summary>Evidence Appendix</summary>'
            f'<div class="appendix-inner"><p class="appendix-stats">No scrape metadata available.</p></div>'
            f'</details>'
        )

    selected_counts = meta.selected_source_counts or {}
    source_counts = selected_counts if selected_counts else (meta.source_counts or {})

    stats_html = (
        f'<p class="appendix-stats">'
        f'Collected: <strong>{_esc(str(meta.total_collected))}</strong>'
        f' &nbsp;·&nbsp; After deduplication: <strong>{_esc(str(meta.total_after_dedup))}</strong>'
        f' &nbsp;·&nbsp; Selected: <strong>{_esc(str(meta.total_selected))}</strong>'
        f' &nbsp;·&nbsp; Evidence sources: <strong>{_esc(str(len(source_counts)))}</strong>'
        f'</p>'
    )

    rows_html = "".join(
        f'<tr><td>{_esc(src)}</td><td>{_esc(str(cnt))}</td></tr>'
        for src, cnt in sorted(source_counts.items(), key=lambda x: -x[1])
    )

    table_html = (
        f'<table class="appendix-table">'
        f'<thead><tr><th>Source</th><th>Items</th></tr></thead>'
        f'<tbody>{rows_html}</tbody>'
        f'</table>'
    )

    extra = ""
    if meta.source_failures:
        extra += (
            f'<div class="appendix-warning failure">'
            f'Source failures: {", ".join(_esc(f) for f in meta.source_failures)}'
            f'</div>'
        )
    if meta.low_data_warning:
        extra += (
            f'<div class="appendix-warning low-data">'
            f'Low data warning: fewer than 15 items collected.'
            f'</div>'
        )

    return (
        f'<details><summary>Evidence Appendix — sources &amp; pipeline stats</summary>'
        f'<div class="appendix-inner">{stats_html}{table_html}{extra}</div>'
        f'</details>'
    )


# ---------------------------------------------------------------------------
# Prospects This Week
# ---------------------------------------------------------------------------

_VALID_PROSPECT_OWNERS = {"BD", "Management", "Compliance", "Ops"}
_CONF_BADGE_KIND = {"High": "conf-high", "Medium": "conf-medium", "Low": "conf-low"}
_OWNER_BADGE_KIND_PROSPECT = {
    "BD": "owner-bd", "Ops": "owner-ops",
    "Compliance": "owner-compliance", "Management": "owner-management",
}


def _render_prospects(digest: Digest) -> str:
    """
    Render the 'Prospects This Week' module.

    When digest.prospects is non-empty: one card per lead, prominently styled.
    When empty: honest empty-state (no named prospects qualified this run).
    Never raises on any input.
    """
    prospects = digest.prospects or []

    if not prospects:
        empty_html = (
            '<p class="prospects-empty">'
            'No qualified named prospects surfaced this run — see Priority Decisions for the broader signal set.'
            '</p>'
        )
        cards_html = empty_html
    else:
        cards = []
        for p in prospects:
            entity = _esc(_clean_codes(_safe(p.entity, "Unknown entity")))
            why_now = _esc(_clean_codes(_safe(p.why_now, "")))
            contact = _esc(_clean_codes(_safe(p.contact_approach, "")))
            owner = p.owner if p.owner in _VALID_PROSPECT_OWNERS else "BD"
            confidence = p.confidence if p.confidence in {"High", "Medium", "Low"} else "Medium"
            owner_bk = _OWNER_BADGE_KIND_PROSPECT.get(owner, "owner-no")
            conf_bk = _CONF_BADGE_KIND.get(confidence, "conf-medium")
            src_name = _safe(p.source, "")
            src_url = _safe(p.source_url, "")
            # Source footer: suppress GNews redirect URLs
            if src_name and not _is_gnews(src_url):
                if src_url:
                    source_html = (
                        f'<a href="{_esc(src_url)}" target="_blank" rel="noopener" '
                        f'class="prospect-source">{_esc(src_name)} &#8599;</a>'
                    )
                else:
                    source_html = f'<span class="prospect-source">{_esc(src_name)}</span>'
            elif src_name:
                source_html = f'<span class="prospect-source">{_esc(src_name)}</span>'
            else:
                source_html = ""

            card = f'<div class="prospect-card">'
            card += f'<p class="prospect-entity">{entity}</p>'
            if why_now:
                card += (
                    f'<div>'
                    f'<div class="prospect-field-label">Why now</div>'
                    f'<div class="prospect-field-value">{why_now}</div>'
                    f'</div>'
                )
            if contact:
                card += (
                    f'<div class="prospect-contact">'
                    f'<div class="prospect-field-label">First contact</div>'
                    f'<div class="prospect-field-value">{contact}</div>'
                    f'</div>'
                )
            card += f'<div class="prospect-footer">'
            card += _badge(owner, owner_bk)
            card += _badge(f"{confidence} confidence", conf_bk)
            card += _badge("Contact", "prospect-contact")
            card += f'</div>'
            # Source on its own footer row — clearly separated from action badges
            if source_html:
                card += (
                    f'<div style="margin-top:6px;padding-top:6px;'
                    f'border-top:1px solid rgba(255,255,255,.08);font-size:11px;color:#64849D;">'
                    f'Source: {source_html}'
                    f'</div>'
                )
            card += f'</div>'
            cards.append(card)

        cards_html = '<div class="prospects-grid">' + "".join(cards) + '</div>'

    count_label = f"{len(prospects)} leads" if prospects else "0 leads this run"

    return (
        f'<div class="prospects-module">'
        f'<div class="prospects-eyebrow">Relationship Intelligence &nbsp;&middot;&nbsp; {_esc(count_label)}</div>'
        f'<h2 class="prospects-heading">Prospects This Week</h2>'
        f'<p class="prospects-subhead">'
        f'Named entities with a commercial trigger for Arie — who to contact, why now, and how.'
        f'</p>'
        f'{cards_html}'
        f'</div>'
    )


# ---------------------------------------------------------------------------
# Overview tab
# ---------------------------------------------------------------------------

def _render_overview_tab(digest: Digest) -> str:
    """
    Overview tab — true 30-second view for a non-technical RM.
    Contains:
      1. 3 executive priority cards (headline + 1-2 line implication only — no duplication of full cards)
      2. One-line Brand Watch status
      3. Source Health mini chip (active / failed counts)
    The full decision cards live exclusively in the Priority Decisions tab.
    """
    # Pick top 3 signals for the compact overview (same logic as What Matters tab)
    signals = [s for s in (digest.top_signals or []) if s.source_url]
    signals = _dedup_what_matters(signals)
    fresh = [s for s in signals if _days_old(s.published_at) <= 14]
    stale = [s for s in signals if _days_old(s.published_at) > 14]
    if len(fresh) >= 3:
        top3 = fresh[:3]
    else:
        needed = 3 - len(fresh)
        top3 = (fresh + stale[:needed])[:3]

    # Compact priority cards — headline + short implication ONLY (no action/why grid/evidence)
    cards_html = ""
    for i, sig in enumerate(top3):
        finding = _clean_codes(_safe(sig.what_happened or sig.opportunity, "Signal"))
        owner = sig.suggested_owner if sig.suggested_owner in _VALID_OWNERS else "BD"
        confidence = _safe(sig.confidence, "Medium")
        owner_badge_kind = {
            "BD": "owner-bd", "Ops": "owner-ops",
            "Compliance": "owner-compliance", "Management": "owner-management",
        }.get(owner, "owner-no")
        conf_badge_kind = f"conf-{confidence.lower()}"
        action_label, action_kind = _action_kind(_clean_codes(_safe(sig.suggested_action, "")))

        cards_html += (
            f'<div class="priority-card">'
            f'<div class="priority-card-number">Priority {i + 1}'
            f' &nbsp; {_badge(owner, owner_badge_kind)}'
            f' {_badge(f"{confidence} confidence", conf_badge_kind)}'
            f' {_badge(action_label, action_kind)}'
            f'</div>'
            f'<p class="priority-card-text">{_esc(finding)}</p>'
            f'</div>'
        )

    exec_section = (
        f'<div class="section" style="margin-bottom:28px;">'
        f'<div class="section-header">'
        f'<h2 class="section-title">This Week at a Glance</h2>'
        f'<div class="section-rule"></div>'
        f'<span class="section-count">{len(top3)} priorities</span>'
        f'</div>'
        f'<div class="priority-grid">{cards_html}</div>'
        f'</div>'
    ) if top3 else ""

    # One-line Brand Watch status
    bw = digest.brand_watch
    if bw:
        bw_result = _safe(bw.result, "No new ARIE / ACBM mentions detected.")
        bw_confidence = bw.coverage_confidence or "Limited"
        bw_summary = (
            f'<div class="overview-bw-card">'
            f'<div class="overview-bw-label">Brand Watch</div>'
            f'<p style="color:#BFD2E8;font-size:14px;margin:0 0 8px;line-height:1.5;">{_esc(bw_result)}</p>'
            f'<span class="bw-confidence {_esc(bw_confidence)}">{_esc(bw_confidence)} coverage</span>'
            f'</div>'
        )
    else:
        bw_summary = ""

    # Source Health mini chip
    sh_items = digest.source_health or []
    active_n = sum(1 for i in sh_items if i.status == "active")
    failed_n = sum(1 for i in sh_items if i.status == "failed")
    sh_color = "var(--green)" if failed_n == 0 else "var(--amber)"
    sh_summary = (
        f'<div class="overview-sh-card">'
        f'<div class="overview-sh-label">Source Health</div>'
        f'<div class="overview-sh-stat" style="color:{sh_color};">{active_n} active</div>'
        f'<div class="overview-sh-sub">{failed_n} failed &nbsp;·&nbsp; {len(sh_items)} monitored feeds</div>'
        f'</div>'
    )

    sidebar_html = (
        f'<div class="overview-grid">{bw_summary}{sh_summary}</div>'
        if (bw_summary or sh_summary) else ""
    )

    # Compact prospects banner — full cards are in the Prospects tab
    prospect_count = len(digest.prospects or [])
    if prospect_count > 0:
        leads_word = "lead" if prospect_count == 1 else "leads"
        prospects_banner = (
            f'<div style="margin-bottom:20px;padding:12px 18px;'
            f'background:linear-gradient(135deg,#0B1E3E 0%,#16284A 100%);'
            f'border-radius:var(--radius-sm);border-bottom:2px solid var(--gold);">'
            f'<span style="color:#9FB0C9;font-size:13px;">'
            f'<strong style="color:var(--gold);">{prospect_count} prospect {leads_word}</strong>'
            f' this week &mdash; open the <strong style="color:#fff;">Prospects</strong> tab to review.'
            f'</span>'
            f'</div>'
        )
    else:
        prospects_banner = (
            f'<div style="margin-bottom:20px;padding:12px 18px;'
            f'background:var(--card);border:1px solid var(--line);border-radius:var(--radius-sm);">'
            f'<span style="color:var(--muted);font-size:13px;">'
            f'No qualified named prospects surfaced this run — see Priority Decisions for the broader signal set.'
            f'</span>'
            f'</div>'
        )

    return prospects_banner + exec_section + sidebar_html


# ---------------------------------------------------------------------------
# Tab script
# ---------------------------------------------------------------------------

def _render_tab_script() -> str:
    """Inline JS for tab switching — vanilla only, no framework."""
    return """<script>
(function(){
  var btns=document.querySelectorAll('.tab-btn');
  var panels=document.querySelectorAll('.tab-panel');
  btns.forEach(function(btn){
    btn.addEventListener('click',function(){
      btns.forEach(function(b){b.classList.remove('active');b.setAttribute('aria-selected','false');});
      panels.forEach(function(p){p.classList.remove('active');});
      btn.classList.add('active');
      btn.setAttribute('aria-selected','true');
      var target=document.getElementById(btn.getAttribute('data-target'));
      if(target){target.classList.add('active');}
    });
  });
})();
</script>"""


# ---------------------------------------------------------------------------
# Footer
# ---------------------------------------------------------------------------

def _render_footer() -> str:
    return (
        f'<footer class="brief-footer">'
        f'Arie Finance Ltd &nbsp;&middot;&nbsp; Internal use only &nbsp;&middot;&nbsp; Not for distribution'
        f'</footer>'
    )


# ---------------------------------------------------------------------------
# Main render function
# ---------------------------------------------------------------------------

def render_digest_html(digest: Digest) -> str:
    """
    Render a complete standalone HTML management page from a Digest.

    Layout: Command Header → 8-tab navigation → tab panels → footer
    Tab panels: Prospects (default) | Overview | Priority Decisions |
                Market Pain Signals | Market Intelligence |
                Trajectory Watch | Source Health | Evidence
    """
    date_str = _fmt_date()

    # Build individual section HTML (reuse existing render helpers)
    exec_html       = _render_executive_snapshot(digest)
    matters_html    = _render_what_matters(digest)
    cx_html         = _render_customer_experience(digest)
    traj_html       = _render_trajectory_watch(digest)
    intel_html      = _render_additional_intelligence(digest)
    bw_html         = _render_brand_watch(digest)
    sh_html         = _render_source_health(digest)
    evidence_html   = _render_evidence_appendix(digest)
    prospects_html  = _render_prospects(digest)

    # Tab panels — content always in DOM; inactive panels hidden via CSS
    tab_panels = (
        # Prospects — standalone tab, DEFAULT active
        f'<section class="tab-panel active" id="panel-prospects" role="tabpanel">'
        f'{prospects_html}'
        f'</section>'

        # Overview
        f'<section class="tab-panel" id="panel-overview" role="tabpanel">'
        f'{_render_overview_tab(digest)}'
        f'</section>'

        # Priority Decisions — exec summary + what matters (no prospects here)
        f'<section class="tab-panel" id="panel-priority" role="tabpanel">'
        f'{exec_html}'
        f'{matters_html}'
        f'</section>'

        # Customer Experience
        f'<section class="tab-panel" id="panel-cx" role="tabpanel">'
        f'{cx_html}'
        f'</section>'

        # Trajectory Watch
        f'<section class="tab-panel" id="panel-trajectory" role="tabpanel">'
        f'{traj_html}'
        f'</section>'

        # Market Intelligence
        f'<section class="tab-panel" id="panel-market" role="tabpanel">'
        f'{bw_html}'
        f'{intel_html}'
        f'</section>'

        # Source Health
        f'<section class="tab-panel" id="panel-source-health" role="tabpanel">'
        f'{sh_html}'
        f'</section>'

        # Evidence
        f'<section class="tab-panel" id="panel-evidence" role="tabpanel">'
        f'{evidence_html}'
        f'</section>'
    )

    body = (
        _render_header(digest, date_str)   # opens <div class="brief-wrap"> + <header>
        + _render_tab_bar()
        + tab_panels
        + _render_footer()
        + '</div>'                          # closes <div class="brief-wrap">
    )

    return (
        "<!DOCTYPE html>\n"
        "<html lang=\"en\">\n"
        "<head>\n"
        "<meta charset=\"UTF-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\">\n"
        f"<title>ARIE Intelligence Command Centre &mdash; {_esc(date_str)}</title>\n"
        f"<style>{CSS}</style>\n"
        "</head>\n"
        "<body>\n"
        f"{body}\n"
        f"{_render_tab_script()}\n"
        "</body>\n"
        "</html>"
    )
