"""
Claude API integration for Arie Finance Market Intelligence (v2).

Architecture: code decides WHICH items and their hard facts; Claude writes the
WHY/ACTION prose; if Claude omits a field or the whole call fails, deterministic
templates fill in — the Digest is always complete and never contains '[SAMPLE]'.

Exports:
    generate_digest(scrape: ScrapeResult) -> Digest
        Single Claude call producing a tiered weekly digest with deterministic
        skeleton + Claude-enriched prose.  Full fallback on any API failure.

Helper exported for content.py:
    _extract_json(text: str) -> Dict[str, Any]
    MODEL, MAX_TOKENS
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import textwrap
from collections import Counter
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

import anthropic

import trend_memory
from brand_watch import run_brand_watch
from config import PAIN_USER_TYPE_KEYWORDS
from enrich import classify_pain_item
from scraper import build_source_health
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
    ScrapedItem,
    ScrapeResult,
    SocialPainPoint,
    SourceHealthItem,
    ThemeTrajectory,
    WatchlistTopic,
)

logger = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 16000

# ---------------------------------------------------------------------------
# Category display map: topic_key (+ film override) -> category label
# ---------------------------------------------------------------------------

_CATEGORY_MAP: Dict[str, str] = {
    "market_opportunities":   "Commercial Opportunity",
    "cross_border_corridors": "Cross-Border Corridors",
    "regulation_compliance":  "Regulation & Compliance",
    "payments_treasury":      "Payments & Treasury",
    "fintech_infrastructure": "Fintech & Infrastructure",
}

# Social/community source prefixes
_SOCIAL_SOURCES: Tuple[str, ...] = ("Reddit",)

# ---------------------------------------------------------------------------
# JSON extraction — tolerates code fences and minor formatting artifacts
# ---------------------------------------------------------------------------


def _extract_json(text: str) -> Dict[str, Any]:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```\s*$", "", text)
    text = text.strip()

    start = text.find("{")
    if start == -1:
        raise ValueError("No JSON object in Claude response")

    depth = 0
    in_string = False
    escape_next = False
    end = -1
    for idx, ch in enumerate(text[start:], start):
        if escape_next:
            escape_next = False
            continue
        if ch == "\\" and in_string:
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
        if not in_string:
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = idx + 1
                    break

    if end == -1:
        raise ValueError("Unbalanced braces in Claude response")
    return json.loads(text[start:end])


# ---------------------------------------------------------------------------
# parse_prospects — exported helper (also tested directly)
# ---------------------------------------------------------------------------

_VALID_PROSPECT_OWNERS = {"BD", "Management", "Compliance", "Ops"}
_VALID_CONFIDENCES = {"High", "Medium", "Low"}


def parse_prospects(data: Dict[str, Any]) -> List[ProspectLead]:
    """
    Parse the 'prospects' key from a Claude JSON dict into a List[ProspectLead].

    Defensive: missing key, wrong type, or malformed entries all yield [] without
    raising — the rest of the digest assembly must never be broken by this section.
    """
    try:
        raw = data.get("prospects", [])
        if not isinstance(raw, list):
            return []
        result: List[ProspectLead] = []
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            entity = str(entry.get("entity", "")).strip()
            if not entity:
                continue  # must have a named entity
            owner = str(entry.get("owner", "BD")).strip()
            if owner not in _VALID_PROSPECT_OWNERS:
                owner = "BD"
            confidence = str(entry.get("confidence", "Medium")).strip()
            if confidence not in _VALID_CONFIDENCES:
                confidence = "Medium"
            result.append(ProspectLead(
                entity=entity,
                why_now=str(entry.get("why_now", "")).strip(),
                contact_approach=str(entry.get("contact_approach", "")).strip(),
                owner=owner,
                confidence=confidence,
                source=str(entry.get("source", "")).strip(),
                source_url=str(entry.get("source_url", "")).strip(),
            ))
        return result
    except Exception:  # noqa: BLE001
        return []


# ---------------------------------------------------------------------------
# Deterministic helpers
# ---------------------------------------------------------------------------


def _days_since(published_at: Optional[str]) -> int:
    """Return days since published_at ISO date, or 999 if unknown/unparseable."""
    if not published_at:
        return 999
    try:
        pub = date.fromisoformat(published_at[:10])
        return (datetime.now(tz=timezone.utc).date() - pub).days
    except (ValueError, TypeError):
        return 999


def _derive_confidence(item: ScrapedItem) -> str:
    """
    Compute confidence from evidence fields on a ScrapedItem.

    HIGH   if supporting_source_count >= 3
        OR (>= 2 AND source_weight >= 0.7 AND recency <= 14 days AND commercial_score >= 1.5)
    MEDIUM if supporting_source_count >= 2
        OR (commercial_score >= 1.2 AND recency <= 21 days)
    LOW    otherwise
    """
    n = item.supporting_source_count
    sw = item.source_weight
    cs = item.commercial_score
    age = _days_since(item.published_at)

    if n >= 3:
        return "High"
    if n >= 2 and sw >= 0.7 and age <= 14 and cs >= 1.5:
        return "High"
    if n >= 2:
        return "Medium"
    if cs >= 1.2 and age <= 21:
        return "Medium"
    return "Low"


def _clean_summary(text: str, max_chars: int = 220) -> str:
    """
    Return a clean one-sentence summary from raw text.
    Strips leading/trailing whitespace, collapses inner whitespace,
    and trims to max_chars (at a word boundary if possible).
    """
    if not text:
        return ""
    cleaned = " ".join(text.split())
    # Try to cut at first sentence boundary within max_chars
    if len(cleaned) <= max_chars:
        return cleaned
    truncated = cleaned[:max_chars]
    for sep in (". ", "! ", "? ", ", "):
        idx = truncated.rfind(sep)
        if idx > max_chars // 2:
            return truncated[: idx + 1].rstrip()
    # Fall back to word boundary
    idx = truncated.rfind(" ")
    return (truncated[:idx] + "…") if idx > 0 else truncated


def _category_for_item(item: ScrapedItem) -> str:
    """Return the display category label for an item."""
    if item.topic == "market_opportunities" and item.subtopic == "film_production":
        return "Film Production"
    return _CATEGORY_MAP.get(item.topic or "", "Commercial Opportunity")


def _is_social(item: ScrapedItem) -> bool:
    return any(item.source.startswith(prefix) for prefix in _SOCIAL_SOURCES)


def _is_film(item: ScrapedItem) -> bool:
    return item.topic == "market_opportunities" and item.subtopic == "film_production"


def _normalize_title(title: str) -> str:
    """Lowercase, collapse spaces — used for near-duplicate signal detection."""
    return " ".join(title.lower().split())


def _jaccard_titles(a: str, b: str) -> float:
    ta = set(a.split())
    tb = set(b.split())
    if not ta and not tb:
        return 1.0
    union = ta | tb
    return len(ta & tb) / len(union) if union else 0.0


# ---------------------------------------------------------------------------
# Fallback prose templates (never '[SAMPLE]')
# ---------------------------------------------------------------------------


def _fallback_why_it_matters(category: str, region: Optional[str]) -> str:
    loc = region.title() if region and region != "global" else "the market"
    return (
        f"{category} development in {loc} — assess relevance to Arie's "
        f"introducer channel and cross-border SME clients."
    )


def _fallback_suggested_action(category: str) -> str:
    if "Film" in category:
        return (
            "Identify the production accountant or treasury contact; pitch rebate "
            "proceeds repatriation and multi-currency supplier payment capability."
        )
    if "Corridor" in category or "Cross-Border" in category:
        return (
            "Map to active corridor and flag to BD; assess whether a named entity "
            "is an introducer prospect or potential client."
        )
    if "Regulation" in category or "Compliance" in category:
        return (
            "Review for licensing or compliance impact; escalate to compliance officer "
            "and assess introducer channel implications."
        )
    if "Payments" in category or "Treasury" in category:
        return (
            "Assess treasury/payment friction angle; flag to BD if a named SME or "
            "treasury team is identified."
        )
    return (
        "Review for introducer/corridor fit; flag to BD if a named entity or "
        "treasury need is identified."
    )


def _fallback_one_liner(item: ScrapedItem) -> str:
    return _clean_summary(item.summary or item.title, 220) or item.title[:220]


def _fallback_what_happened(item: ScrapedItem) -> str:
    return _clean_summary(item.summary or item.title, 220) or item.title[:220]


def _fallback_pain_point(item: ScrapedItem) -> str:
    return _clean_summary(item.summary or item.title, 200) or item.title[:200]


def _fallback_content_angle(item: ScrapedItem) -> str:
    cat = _category_for_item(item)
    if "Corridor" in cat or "Cross-Border" in cat:
        angle_type = "corridor/cross-border payment"
    elif "Treasury" in cat or "Payments" in cat:
        angle_type = "treasury"
    else:
        angle_type = "payment intermediary"
    return f"Angle: how Arie's {angle_type} approach addresses this."


_COMPLIANCE_CAUTION = (
    "Institutional tone only. Do not promise onboarding outcomes, FX pricing, "
    "or returns. Do not position Arie as a bank."
)


def _fallback_film_relevance(item: ScrapedItem) -> str:
    loc = (item.region or "the region").title()
    return (
        f"Potential production-treasury / multi-currency payment / rebate-repatriation "
        f"relevance for Arie if a production engages {loc}."
    )


def _fallback_arie_relevance(platform: str, user_type: str) -> str:
    if platform:
        ut = f" {user_type}" if user_type else " business"
        return (
            f"{platform}{ut} experiencing account/payment friction is a direct prospect "
            f"for Arie's reliable FSC-licensed payment intermediary service."
        )
    return (
        "Signals cross-border payment or account friction — position Arie as a licensed, "
        "compliance-led alternative for affected SMEs or introducers."
    )


def _fallback_mauritius_why(item: ScrapedItem) -> str:
    return (
        f"Mauritius financial services development — assess for implications to ARIE's "
        f"licensing context, introducer channel, or client onboarding environment."
    )


def _fallback_competitor_why(competitor: str, item: ScrapedItem) -> str:
    name = competitor or "A direct competitor"
    return (
        f"{name} development — monitor for pricing, product, or market-access implications "
        f"that ARIE can position against in introducer conversations."
    )


def _fallback_introducer_why(item: ScrapedItem) -> str:
    return (
        f"CSP / management company development — assess whether this entity is a current or "
        f"prospective ARIE introducer and flag to BD if relevant."
    )


def _detect_pain_user_type_fallback(text: str) -> str:
    """Lightweight user-type detection using PAIN_USER_TYPE_KEYWORDS (fallback if Claude omits)."""
    lower = text.lower()
    for user_type, keywords in PAIN_USER_TYPE_KEYWORDS.items():
        if any(kw in lower for kw in keywords):
            return user_type
    return ""


def _fallback_executive_summary(
    top_signals: List[OpportunityRadarItem],
    n_comm: int,
    n_reg: int,
    n_film: int,
    signal_items: Optional[List["ScrapedItem"]] = None,
    regulatory_items: Optional[List["ScrapedItem"]] = None,
    film_items: Optional[List["ScrapedItem"]] = None,
    direct_mention_count: int = 0,
    social_items: Optional[List["ScrapedItem"]] = None,
) -> List[str]:
    """
    Observational bullets derived deterministically from the data.
    No counts-as-directives, no actions — factual/observational only.
    """
    bullets: List[str] = []

    # Bullet 1: lead market-signal theme (from the top signal, observational)
    if top_signals:
        lead = top_signals[0]
        cat = lead.category or "market"
        region = lead.region or ""
        loc = f" in {region.title()}" if region and region != "global" else ""
        headline = lead.opportunity[:90].rstrip(".")
        bullets.append(f"{headline}{loc} emerged as the lead market signal this week.")

    # Bullet 2: secondary signal theme or broad market activity
    if top_signals and len(top_signals) >= 2:
        second = top_signals[1]
        second_headline = second.opportunity[:90].rstrip(".")
        bullets.append(f"{second_headline} also surfaced with notable commercial relevance.")
    elif n_comm and n_comm >= 3:
        bullets.append(
            "Cross-border payments and commercial corridor activity remained active across multiple sources this week."
        )

    # Bullet 3: Mauritius / FSC presence
    mauritius_signals = [
        s for s in (signal_items or [])
        if (s.region or "").lower() in ("mauritius",)
        or "fsc" in (s.title or "").lower()
        or "mauritius" in (s.title or "").lower()
    ]
    reg_mauritius = [
        r for r in (regulatory_items or [])
        if (r.region or "").lower() == "mauritius"
        or "fsc" in (r.title or "").lower()
        or "mauritius" in (r.title or "").lower()
    ]
    if mauritius_signals or reg_mauritius:
        bullets.append(
            "Several Mauritius FSC-related licence and regulatory developments appeared across financial media this week."
        )
    elif n_reg:
        bullets.append(
            "Regulatory and compliance activity was present across key jurisdictions, with developments worth monitoring for Arie's licensing context."
        )

    # Bullet 4: community / social presence
    if social_items:
        bullets.append(
            "Community discussions on cross-border payment friction and SME treasury pain points continued to surface on financial forums."
        )

    # Bullet 5: direct mentions + film
    if direct_mention_count:
        bullets.append(
            f"Direct public mentions of ARIE Finance / ACBM were detected this week ({direct_mention_count} item(s))."
        )
    elif film_items:
        bullets.append(
            "No direct ARIE / ACBM public mentions detected this week. Film production activity with potential treasury relevance was noted."
        )
    else:
        bullets.append(
            "No direct ARIE / ACBM public mentions detected this week."
        )

    return bullets[:5]


# ---------------------------------------------------------------------------
# v3: entity activity + watchlist topic helpers (no LLM — pure count comparison)
# ---------------------------------------------------------------------------


def _direction(current: int, prev: int) -> str:
    """Return movement direction string for a count vs its prior week value."""
    if prev == 0 and current > 0:
        return "new"
    if current > prev:
        return "increasing"
    if current < prev:
        return "declining"
    return "stable"


def _compute_entity_activity(
    current: Dict[str, int],
    prev: Dict[str, int],
) -> List[EntityActivity]:
    """
    Build EntityActivity list from current and previous run entity counts.
    Only includes entities with at least one mention this week.
    Sorted by current count descending.
    """
    result: List[EntityActivity] = []
    for entity, count in sorted(current.items(), key=lambda x: -x[1]):
        if count == 0:
            continue
        p = prev.get(entity, 0)
        result.append(EntityActivity(
            entity=entity,
            count=count,
            prev_count=p,
            direction=_direction(count, p),
        ))
    return result


def _compute_watchlist_topic_list(
    current: Dict[str, int],
    prev: Dict[str, int],
) -> List[WatchlistTopic]:
    """
    Build WatchlistTopic list from current and previous run watchlist topic counts.
    Only includes topics with at least one mention this week.
    Sorted by current count descending.
    """
    result: List[WatchlistTopic] = []
    for topic, count in sorted(current.items(), key=lambda x: -x[1]):
        if count == 0:
            continue
        p = prev.get(topic, 0)
        result.append(WatchlistTopic(
            topic=topic,
            count=count,
            prev_count=p,
            direction=_direction(count, p),
        ))
    return result


# ---------------------------------------------------------------------------
# ARIE system prompt (used in the Claude call)
# ---------------------------------------------------------------------------

DIGEST_SYSTEM_PROMPT = """You are a senior market intelligence analyst. Your client is Arie Finance Ltd.

ABOUT ARIE FINANCE:
Arie Finance Ltd is a licensed Payment Intermediary Services provider regulated by the Financial Services Commission (FSC) Mauritius. Licensed February 2026, launched May 2026. Services: payment intermediary services, client onboarding, corporate payment and treasury desk. Target clients: SMEs, cross-border businesses, import/export businesses, professional service firms. Primary acquisition channel: introducers — management companies, CSPs, trust administrators, fiduciaries, accounting firms, legal firms. Differentiators: FSC licensed, faster onboarding, commercial understanding, compliance-led operations, human relationship-driven service. Tone: institutional, professional, regulated. Never position as a bank.

BUSINESS DEVELOPMENT ANGLES:
1. FILM PRODUCTIONS: Mauritius now offers a ~30–40% film rebate on qualifying production spend. Arie can serve production companies needing multi-currency treasury, supplier payments, and efficient repatriation of rebate proceeds.
2. COMPANIES EXPANDING INTO AFRICA / MAURITIUS: Firms entering Africa or setting up Mauritius structures need multi-currency treasury. Watch for fundraising rounds targeting Africa expansion, new market entry announcements, Mauritius GBC/IAS mentions.

YOUR TASK:
You are given a structured digest skeleton built from enriched news items. For each item the skeleton supplies the factual fields (title, url, region, category, summary). Your ONLY job is to write the WHY/ACTION prose fields listed below. Do NOT invent items, URLs, figures, entity names, or facts. Use ONLY information visible in the skeleton.

GROUNDING RULES:
- For ALL sections: use the "key" field — copy the bracket label exactly as it appears in the skeleton (e.g. "SIGNAL/1", "COMM/3", "MAURITIUS/2"). Do NOT include source_url in any section.
- For interesting_finds: the key must be a bracket label from ANY section (e.g. "SIGNAL/1", "MAURITIUS/2", "COMPETITOR/1", "PAIN/3").
- Entity names must appear literally in the item title or summary — do not infer.
- Keep prose executive and specific (a manager must grasp why it matters in 10 seconds).
- Confidence is NOT yours to set — the system overwrites it deterministically.
- For social/Reddit items: write pain_point (the underlying user/business pain) and content_angle (a possible LinkedIn/article ANGLE — NOT a finished post). compliance_caution: always use exactly: "Institutional tone only. Do not promise onboarding outcomes, FX pricing, or returns. Do not position Arie as a bank."
- For film items: relevance must focus on the PAYMENT/TREASURY/REBATE-DOCUMENTATION angle for Arie.
- For MAURITIUS INTELLIGENCE items: what_happened = one factual sentence about what was announced or changed. why_it_matters = how this affects Arie's licensing context, introducer channel, or client environment in Mauritius. category = one of: EDB | Budget | FSC | BOM | Tax | Investment | Regulation | Bilateral.
- For COMPETITOR INTELLIGENCE items: what_happened = one factual sentence about what the competitor did. why_it_matters = the specific threat or opportunity for Arie (what to monitor or counter-position). signal_type = one of: product | pricing | expansion | complaint | regulatory | outage. competitor = exact competitor name (Wise, Revolut, Airwallex, Payoneer, Mercury, etc.).
- For INTRODUCER INTELLIGENCE items: what_happened = one factual sentence about the CSP/fiduciary event. why_it_matters = how this affects Arie's introducer network or distribution. signal_type = one of: acquisition | expansion | licence | new_service | partnership | merger.
- If you cannot ground a prose field in the provided data, return an empty string "".

OWNER RULES (for signals.suggested_owner):
Allowed values: BD | Ops | Compliance | Management | No action required
- BD: business development opportunity — a named company, market entry, or new client prospect
- Ops: operational process, payment infrastructure, or treasury execution matter
- Compliance: regulatory filing, licensing requirement, AML/KYC obligation, FATF development
- Management: strategic decision, positioning, or jurisdiction-level awareness
- No action required: informational — useful context but no concrete Arie action this week
Do NOT force an action if none is plausible. Do NOT use "monitor" as a suggested_action alone.

OUTPUT FORMAT:
Return ONLY a raw JSON object — no markdown fences, no preamble, no explanation. Schema:

{
  "signals": [
    {
      "key": "SIGNAL/1",
      "why_it_matters": "...",
      "suggested_action": "...",
      "what_happened": "...",
      "suggested_owner": "<BD|Ops|Compliance|Management|No action required>"
    }
  ],
  "commercial": [
    {
      "key": "COMM/1",
      "one_liner": "..."
    }
  ],
  "social": [
    {
      "key": "SOCIAL/1",
      "pain_point": "...",
      "content_angle": "...",
      "compliance_caution": "Institutional tone only. Do not promise onboarding outcomes, FX pricing, or returns. Do not position Arie as a bank."
    }
  ],
  "film": [
    {
      "key": "FILM/1",
      "relevance": "..."
    }
  ],
  "regulatory": [
    {
      "key": "REG/1",
      "one_liner": "..."
    }
  ],
  "pain": [
    {
      "key": "PAIN/1",
      "arie_relevance": "...",
      "user_type": "...",
      "geography": "..."
    }
  ],
  "mauritius": [
    {
      "key": "MAURITIUS/1",
      "what_happened": "...",
      "why_it_matters": "...",
      "category": "<EDB|Budget|FSC|BOM|Tax|Investment|Regulation>"
    }
  ],
  "competitor": [
    {
      "key": "COMPETITOR/1",
      "competitor": "<Wise|Revolut|Airwallex|Payoneer|Mercury|other>",
      "what_happened": "...",
      "why_it_matters": "...",
      "signal_type": "<product|pricing|expansion|complaint|regulatory|outage>"
    }
  ],
  "introducer": [
    {
      "key": "INTRODUCER/1",
      "what_happened": "...",
      "why_it_matters": "...",
      "signal_type": "<acquisition|expansion|licence|new_service|partnership>"
    }
  ],
  "interesting_finds": [
    {
      "key": "<bracket label from any section, e.g. SIGNAL/1, COMM/3, MAURITIUS/2, COMPETITOR/1, PAIN/3>",
      "why_it_stands_out": "<one sentence: why a senior manager should notice this>",
      "category_hint": "<5 words max, e.g. 'New FSC entrant', 'Wise complaint surge', 'Mauritius EDB film deal'>"
    }
  ],
  "executive_summary": ["bullet 1", "bullet 2", "bullet 3"]
}

INTERESTING FINDS INSTRUCTIONS (v4 — strict priority order):
Select exactly 3–5 items from ANY section (mauritius, competitor, introducer, pain, signals, commercial, film, regulatory).
You MUST return at least 3 items. Only return fewer than 3 if the entire digest contains fewer than 3 non-generic items — which is extremely unlikely.
Selection priority order — always prefer items higher on this list:
1. MAURITIUS: any EDB, Ministry of Finance, FSC, or Bank of Mauritius development management would not know
2. CUSTOMER PAIN: a specific complaint about Wise, Revolut, Airwallex that signals a BD opportunity for ARIE
3. COMPETITOR: a product launch, pricing change, corridor expansion, or regulatory action at a direct competitor
4. INTRODUCER: a CSP/management company acquisition, expansion, or new licence that affects ARIE's network
5. FILM: a Mauritius or Africa film production with explicit payment/treasury relevance
6. GENERIC FINTECH: only if the item would genuinely surprise management and is not found in business press

Do NOT pick items simply because they are well-known, high-volume, or already covered by mainstream news.
Pick what is genuinely non-obvious. Management should think: "I didn't know this" or "This affects us."
Max 5. Always return at least 3.

PROSPECTS THIS WEEK (add a "prospects" array to your JSON output):
From the items in this digest, identify NAMED entities (companies, fund administrators, CSPs, introducers, regulated firms) that are GENUINE prospects for Arie — a regulated payment-intermediary / treasury provider with a Mauritius focus, cross-border corridors, and a CSP/introducer acquisition channel. A prospect qualifies only when: (1) there is a real commercial TRIGGER in the data (new licence, Mauritius/corridor expansion, fund or treasury setup, a specific payment/banking pain that Arie can solve), AND (2) there is a realistic Arie angle, AND (3) you can state a concrete first-contact approach grounded in the item.
BE STRICT: quality over quantity. Return 0–8 prospects. Do NOT invent, stretch, or include items that are merely market context, competitor news, regulatory background, or consumer complaints. An empty list is acceptable and honest. Do not encourage outreach to individuals; prospects must be businesses/entities with a commercial trigger.
For each prospect return: entity (exact name), why_now (trigger + Arie fit, ≤2 plain-English sentences, no internal codes), contact_approach (one concrete realistic first step), owner (BD|Management|Compliance|Ops), confidence (High|Medium|Low), source (publication name), source_url (exact URL from the skeleton, empty string if a GNews URL).
Add this to your existing JSON output as a top-level "prospects" array. If no items qualify, return "prospects": []."""


# ---------------------------------------------------------------------------
# Deterministic skeleton builder
# ---------------------------------------------------------------------------


def _build_skeleton(
    items: List[ScrapedItem],
    community_items: Optional[List[ScrapedItem]] = None,
) -> Tuple[
    List[ScrapedItem],          # top_signal_items
    List[ScrapedItem],          # commercial_items
    List[ScrapedItem],          # social_items
    List[ScrapedItem],          # film_items
    List[ScrapedItem],          # regulatory_items
    List[ScrapedItem],          # direct_mention_items
]:
    """
    Partition enriched items into sections deterministically.

    Rules:
    - direct_mention_items: item.direct_mention == True (from all items)
    - film_items: subtopic==film_production
    - social_items: sourced from community_items (the dedicated community stream built
      by enrich.py BEFORE the commercial top-40 cap).  Falls back to Reddit items
      within the capped `items` list only when community_items is None or empty.
    - top_signal_items: highest commercial_score, excluding film (unless payment-relevant),
      max 3 per category, deduplicated near-similar titles, ~6-8 items
    - commercial_items: commercially-relevant items NOT in top_signals,
      topics: market_opps (non-film) / corridors / payments / fintech, ~6-10
    - regulatory_items: regulation_compliance items
    """
    # Step 1: separate by type
    direct_mention_items: List[ScrapedItem] = [i for i in items if i.direct_mention]

    # All film items
    film_items: List[ScrapedItem] = [i for i in items if _is_film(i)]

    # Social items — use dedicated community stream when available; fall back to
    # Reddit items that survived the commercial cap otherwise.
    if community_items:
        social_items: List[ScrapedItem] = list(community_items)
    else:
        social_items = [i for i in items if _is_social(i)]
    social_urls: Set[str] = {i.url for i in social_items}

    # Film items with payment angle can be in top_signals
    film_payment_urls: Set[str] = set()
    for fi in film_items:
        text = f"{fi.title} {fi.summary}".lower()
        payment_kws = {
            "payment", "treasury", "fx", "foreign exchange", "repatriation",
            "multi-currency", "multicurrency", "rebate", "production finance",
            "cash flow", "settlement", "supplier payment",
        }
        if any(kw in text for kw in payment_kws):
            film_payment_urls.add(fi.url)

    film_urls: Set[str] = {i.url for i in film_items}
    regulatory_items: List[ScrapedItem] = [
        i for i in items
        if i.topic == "regulation_compliance"
        and i.url not in social_urls
    ]
    regulatory_urls: Set[str] = {i.url for i in regulatory_items}

    # Candidate pool for top_signals: exclude pure film (unless payment-angle), exclude social
    commercial_topics = {
        "market_opportunities", "cross_border_corridors",
        "payments_treasury", "fintech_infrastructure",
    }
    signal_candidates = [
        i for i in items
        if i.url not in social_urls
        and (
            i.url not in film_urls
            or i.url in film_payment_urls
        )
        and i.topic in commercial_topics
    ]

    # Sort by commercial_score DESC
    signal_candidates.sort(key=lambda x: (x.commercial_score, x.relevance_score), reverse=True)

    # Select top_signals: max 3 per category, deduplicate near-similar titles, target 6-8
    top_signal_items: List[OpportunityRadarItem] = []  # type: ignore[assignment]
    top_signal_items_raw: List[ScrapedItem] = []
    cat_counts: Counter = Counter()
    selected_titles: List[str] = []
    selected_urls: Set[str] = set()

    for item in signal_candidates:
        if len(top_signal_items_raw) >= 8:
            break
        cat = _category_for_item(item)
        if cat_counts[cat] >= 3:
            continue
        # Near-duplicate title check against already-selected signals
        norm = _normalize_title(item.title)
        if any(_jaccard_titles(norm, t) >= 0.65 for t in selected_titles):
            continue
        top_signal_items_raw.append(item)
        selected_titles.append(norm)
        selected_urls.add(item.url)
        cat_counts[cat] += 1

    # Commercial opportunities: commercially relevant, NOT in top_signals, NOT film, NOT social, NOT regulatory
    commercial_items = [
        i for i in items
        if i.url not in selected_urls
        and i.url not in film_urls
        and i.url not in social_urls
        and i.url not in regulatory_urls
        and i.topic in commercial_topics
    ]
    commercial_items = commercial_items[:10]

    return (
        top_signal_items_raw,
        commercial_items,
        social_items[:5],
        film_items,
        regulatory_items,
        direct_mention_items,
    )


def _format_skeleton_for_claude(
    signal_items: List[ScrapedItem],
    commercial_items: List[ScrapedItem],
    social_items: List[ScrapedItem],
    film_items: List[ScrapedItem],
    regulatory_items: List[ScrapedItem],
    pain_items: Optional[List[ScrapedItem]] = None,
    mauritius_items: Optional[List[ScrapedItem]] = None,
    competitor_items: Optional[List[ScrapedItem]] = None,
    introducer_items: Optional[List[ScrapedItem]] = None,
) -> str:
    """Format the skeleton sections into a readable prompt block for Claude."""
    lines: List[str] = []

    def _fmt_item(idx: int, item: ScrapedItem, section: str) -> str:
        return (
            f"[{section}/{idx}] URL: {item.url}\n"
            f"  TITLE: {item.title}\n"
            f"  CATEGORY: {_category_for_item(item)}\n"
            f"  REGION: {item.region or 'global'}\n"
            f"  SUMMARY: {_clean_summary(item.summary or '', 300)}"
        )

    def _fmt_pain_item(idx: int, item: ScrapedItem) -> str:
        platform = item.watchlist_hits[0] if item.watchlist_hits else "unknown platform"
        return (
            f"[PAIN/{idx}] URL: {item.url}\n"
            f"  TITLE: {item.title}\n"
            f"  PLATFORM: {platform}\n"
            f"  SOURCE: {item.source}\n"
            f"  SUMMARY: {_clean_summary(item.summary or '', 300)}"
        )

    def _fmt_competitor_item(idx: int, item: ScrapedItem) -> str:
        competitor = item.watchlist_hits[0] if item.watchlist_hits else ""
        return (
            f"[COMPETITOR/{idx}] URL: {item.url}\n"
            f"  TITLE: {item.title}\n"
            f"  COMPETITOR: {competitor or 'unknown'}\n"
            f"  SOURCE: {item.source}\n"
            f"  SUMMARY: {_clean_summary(item.summary or '', 300)}"
        )

    # v4 high-priority streams come first in the prompt
    if mauritius_items:
        lines.append(
            "=== MAURITIUS INTELLIGENCE (write what_happened, why_it_matters, category) ==="
        )
        for i, it in enumerate(mauritius_items, 1):
            lines.append(_fmt_item(i, it, "MAURITIUS"))

    if competitor_items:
        lines.append(
            "\n=== COMPETITOR INTELLIGENCE "
            "(write what_happened, why_it_matters, signal_type, competitor) ==="
        )
        for i, it in enumerate(competitor_items, 1):
            lines.append(_fmt_competitor_item(i, it))

    if introducer_items:
        lines.append(
            "\n=== INTRODUCER INTELLIGENCE (write what_happened, why_it_matters, signal_type) ==="
        )
        for i, it in enumerate(introducer_items, 1):
            lines.append(_fmt_item(i, it, "INTRODUCER"))

    if signal_items:
        lines.append("\n=== TOP SIGNALS (write why_it_matters, suggested_action, what_happened) ===")
        for i, it in enumerate(signal_items, 1):
            lines.append(_fmt_item(i, it, "SIGNAL"))

    if commercial_items:
        lines.append("\n=== COMMERCIAL OPPORTUNITIES (write one_liner) ===")
        for i, it in enumerate(commercial_items, 1):
            lines.append(_fmt_item(i, it, "COMM"))

    if social_items:
        lines.append("\n=== SOCIAL PAIN POINTS (write pain_point, content_angle, compliance_caution) ===")
        for i, it in enumerate(social_items, 1):
            lines.append(_fmt_item(i, it, "SOCIAL"))

    if film_items:
        lines.append("\n=== FILM WATCH (write relevance: focus on payment/treasury/rebate-doc angle) ===")
        for i, it in enumerate(film_items, 1):
            lines.append(_fmt_item(i, it, "FILM"))

    if regulatory_items:
        lines.append("\n=== REGULATORY WATCH (write one_liner) ===")
        for i, it in enumerate(regulatory_items, 1):
            lines.append(_fmt_item(i, it, "REG"))

    if pain_items:
        lines.append(
            "\n=== CUSTOMER PAIN ITEMS "
            "(write arie_relevance, user_type, geography — real complaints about competitor platforms) ==="
        )
        for i, it in enumerate(pain_items, 1):
            lines.append(_fmt_pain_item(i, it))

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Digest assembly with Claude prose map + deterministic fallbacks
# ---------------------------------------------------------------------------


def _assemble_digest(
    scrape: ScrapeResult,
    signal_items: List[ScrapedItem],
    commercial_items: List[ScrapedItem],
    social_items: List[ScrapedItem],
    film_items: List[ScrapedItem],
    regulatory_items: List[ScrapedItem],
    direct_mention_items: List[ScrapedItem],
    pain_items: List[ScrapedItem],
    claude_data: Optional[Dict[str, Any]],
    momentum: Dict[str, str],
    entity_activity: Optional[List[EntityActivity]] = None,
    watchlist_topics: Optional[List[WatchlistTopic]] = None,
    mauritius_items: Optional[List[ScrapedItem]] = None,
    competitor_items: Optional[List[ScrapedItem]] = None,
    introducer_items: Optional[List[ScrapedItem]] = None,
    brand_watch: Optional["BrandWatchResult"] = None,
    trajectories: Optional[List["ThemeTrajectory"]] = None,
) -> Digest:
    """
    Build the Digest from the deterministic skeleton + Claude prose map.
    claude_data may be None (full fallback) or missing individual fields (partial fallback).
    """
    generated_at = datetime.now(tz=timezone.utc).isoformat()

    # Build Claude prose lookups keyed by the bracket label (e.g. "SIGNAL/1").
    # All sections now use index-based keys — GNews redirect URLs are too long for Claude
    # to copy reliably, so URL-based matching was dropped in v4.2.
    def _index_by_key(section: str) -> Dict[str, Dict[str, Any]]:
        return {str(d.get("key", "")): d for d in (claude_data or {}).get(section, []) if isinstance(d, dict)}

    c_signals = _index_by_key("signals")
    c_commercial = _index_by_key("commercial")
    c_social = _index_by_key("social")
    c_film = _index_by_key("film")
    c_regulatory = _index_by_key("regulatory")

    # --- TOP SIGNALS ---
    top_signals: List[OpportunityRadarItem] = []
    for _i, item in enumerate(signal_items, 1):
        cat = _category_for_item(item)
        prose = c_signals.get(f"SIGNAL/{_i}", {})

        what_happened = str(prose.get("what_happened", "")).strip()
        if not what_happened or "[SAMPLE]" in what_happened:
            what_happened = _fallback_what_happened(item)

        why_it_matters = str(prose.get("why_it_matters", "")).strip()
        if not why_it_matters or "[SAMPLE]" in why_it_matters:
            why_it_matters = _fallback_why_it_matters(cat, item.region)

        suggested_action = str(prose.get("suggested_action", "")).strip()
        if not suggested_action or "[SAMPLE]" in suggested_action:
            suggested_action = _fallback_suggested_action(cat)

        # Suggested owner from Claude — validate against allowed values
        _VALID_OWNERS = {"BD", "Ops", "Compliance", "Management", "No action required"}
        suggested_owner = str(prose.get("suggested_owner", "")).strip()
        if suggested_owner not in _VALID_OWNERS:
            suggested_owner = "BD"  # sensible default for commercial signals

        top_signals.append(OpportunityRadarItem(
            opportunity=item.title,
            confidence=_derive_confidence(item),
            why_it_matters=why_it_matters,
            suggested_action=suggested_action,
            suggested_owner=suggested_owner,
            region=item.region or None,
            subtopic=item.subtopic or None,
            entities=list(item.watchlist_hits),
            source_title=item.title,
            source_url=item.url,
            supporting_source_count=item.supporting_source_count,
            category=cat,
            commercial_score=item.commercial_score,
            what_happened=what_happened,
            published_at=item.published_at,
            source=item.source,
        ))

    # --- DIRECT MENTIONS ---
    direct_mentions: List[DirectMention] = []
    for item in direct_mention_items:
        what = _clean_summary(item.summary or "", 200) or item.title[:200]
        entity = item.direct_mention_entities[0] if item.direct_mention_entities else "Arie Finance"
        direct_mentions.append(DirectMention(
            title=item.title,
            source=item.source,
            source_url=item.url,
            entity=entity,
            what_happened=what,
            published_at=item.published_at,
        ))

    # Momentum helper
    _valid_trends = {"new", "recurring", "accelerating", "fading", ""}

    def _get_trend(item: ScrapedItem) -> str:
        key = item.subtopic or item.topic or ""
        t = momentum.get(key, momentum.get(item.topic or "", ""))
        return t if t in _valid_trends else ""

    # --- COMMERCIAL OPPORTUNITIES ---
    commercial_opportunities: List[DigestItem] = []
    for _i, item in enumerate(commercial_items, 1):
        prose = c_commercial.get(f"COMM/{_i}", {})
        one_liner = str(prose.get("one_liner", "")).strip()
        if not one_liner or "[SAMPLE]" in one_liner:
            one_liner = _fallback_one_liner(item)
        commercial_opportunities.append(DigestItem(
            title=item.title,
            url=item.url,
            source=item.source,
            one_liner=one_liner,
            topic=item.topic,
            region=item.region or None,
            trend_flag=_get_trend(item),
            entities=list(item.watchlist_hits),
            supporting_source_count=item.supporting_source_count,
            published_at=item.published_at,
        ))

    # --- SOCIAL PAIN POINTS ---
    social_pain_points: List[SocialPainPoint] = []
    for _i, item in enumerate(social_items, 1):
        prose = c_social.get(f"SOCIAL/{_i}", {})

        pain_point = str(prose.get("pain_point", "")).strip()
        if not pain_point or "[SAMPLE]" in pain_point:
            pain_point = _fallback_pain_point(item)

        content_angle = str(prose.get("content_angle", "")).strip()
        if not content_angle or "[SAMPLE]" in content_angle:
            content_angle = _fallback_content_angle(item)

        compliance_caution = str(prose.get("compliance_caution", "")).strip()
        if not compliance_caution or "[SAMPLE]" in compliance_caution:
            compliance_caution = _COMPLIANCE_CAUTION

        social_pain_points.append(SocialPainPoint(
            pain_point=pain_point,
            source=item.source,
            source_url=item.url,
            content_angle=content_angle,
            compliance_caution=compliance_caution,
            published_at=item.published_at,
        ))

    # --- FILM WATCH ---
    film_watch: List[FilmWatchItem] = []
    for _i, item in enumerate(film_items, 1):
        prose = c_film.get(f"FILM/{_i}", {})
        relevance = str(prose.get("relevance", "")).strip()
        if not relevance or "[SAMPLE]" in relevance:
            relevance = _fallback_film_relevance(item)
        film_watch.append(FilmWatchItem(
            title=item.title,
            source=item.source,
            source_url=item.url,
            relevance=relevance,
            region=item.region or None,
            published_at=item.published_at,
        ))

    # --- REGULATORY WATCH ---
    regulatory_watch: List[DigestItem] = []
    for _i, item in enumerate(regulatory_items, 1):
        prose = c_regulatory.get(f"REG/{_i}", {})
        one_liner = str(prose.get("one_liner", "")).strip()
        if not one_liner or "[SAMPLE]" in one_liner:
            one_liner = _fallback_one_liner(item)
        regulatory_watch.append(DigestItem(
            title=item.title,
            url=item.url,
            source=item.source,
            one_liner=one_liner,
            topic=item.topic,
            region=item.region or None,
            trend_flag=_get_trend(item),
            entities=list(item.watchlist_hits),
            supporting_source_count=item.supporting_source_count,
            published_at=item.published_at,
        ))

    # --- CUSTOMER PAIN INTELLIGENCE ---
    c_pain = _index_by_key("pain")
    customer_pain: List[CustomerPainItem] = []
    for _i, item in enumerate(pain_items, 1):
        prose = c_pain.get(f"PAIN/{_i}", {})

        arie_relevance = str(prose.get("arie_relevance", "")).strip()
        if not arie_relevance or "[SAMPLE]" in arie_relevance:
            platform = item.watchlist_hits[0] if item.watchlist_hits else ""
            user_type_det = _detect_pain_user_type_fallback(f"{item.title} {item.summary}")
            arie_relevance = _fallback_arie_relevance(platform, user_type_det)

        user_type = str(prose.get("user_type", "")).strip()
        if not user_type:
            user_type = _detect_pain_user_type_fallback(f"{item.title} {item.summary}")

        geography = str(prose.get("geography", "")).strip() or item.region or ""
        evidence_excerpt = _clean_summary(item.summary or "", 220) or item.title[:220]
        platform_name = item.watchlist_hits[0] if item.watchlist_hits else ""

        customer_pain.append(CustomerPainItem(
            pain_point=item.title,
            platform_mentioned=platform_name,
            user_type=user_type,
            geography=geography,
            evidence_excerpt=evidence_excerpt,
            source_url=item.url,
            source=item.source,
            arie_relevance=arie_relevance,
            published_at=item.published_at,
        ))

    # --- MAURITIUS INTELLIGENCE ---
    c_mauritius = {str(d.get("key", "")): d for d in (claude_data or {}).get("mauritius", []) if isinstance(d, dict)}
    mauritius_intel: List[MauritiusIntelItem] = []
    for _i, item in enumerate(mauritius_items or [], 1):
        prose = c_mauritius.get(f"MAURITIUS/{_i}", {})
        what_happened = str(prose.get("what_happened", "")).strip()
        if not what_happened or "[SAMPLE]" in what_happened:
            what_happened = _fallback_what_happened(item)
        why_it_matters = str(prose.get("why_it_matters", "")).strip()
        if not why_it_matters or "[SAMPLE]" in why_it_matters:
            why_it_matters = _fallback_mauritius_why(item)
        category = str(prose.get("category", "")).strip()
        mauritius_intel.append(MauritiusIntelItem(
            title=item.title,
            source=item.source,
            source_url=item.url,
            what_happened=what_happened,
            why_it_matters=why_it_matters,
            category=category,
            published_at=item.published_at,
        ))

    # --- COMPETITOR INTELLIGENCE ---
    c_competitor = {str(d.get("key", "")): d for d in (claude_data or {}).get("competitor", []) if isinstance(d, dict)}
    competitor_intel: List[CompetitorIntelItem] = []
    for _i, item in enumerate(competitor_items or [], 1):
        prose = c_competitor.get(f"COMPETITOR/{_i}", {})
        what_happened = str(prose.get("what_happened", "")).strip()
        if not what_happened or "[SAMPLE]" in what_happened:
            what_happened = _fallback_what_happened(item)
        competitor_name = str(prose.get("competitor", "")).strip()
        if not competitor_name:
            competitor_name = item.watchlist_hits[0] if item.watchlist_hits else ""
        why_it_matters = str(prose.get("why_it_matters", "")).strip()
        if not why_it_matters or "[SAMPLE]" in why_it_matters:
            why_it_matters = _fallback_competitor_why(competitor_name, item)
        signal_type = str(prose.get("signal_type", "")).strip()
        competitor_intel.append(CompetitorIntelItem(
            competitor=competitor_name,
            title=item.title,
            source=item.source,
            source_url=item.url,
            what_happened=what_happened,
            why_it_matters=why_it_matters,
            signal_type=signal_type,
            published_at=item.published_at,
        ))

    # --- INTRODUCER INTELLIGENCE ---
    c_introducer = {str(d.get("key", "")): d for d in (claude_data or {}).get("introducer", []) if isinstance(d, dict)}
    introducer_intel: List[IntroducerIntelItem] = []
    for _i, item in enumerate(introducer_items or [], 1):
        prose = c_introducer.get(f"INTRODUCER/{_i}", {})
        what_happened = str(prose.get("what_happened", "")).strip()
        if not what_happened or "[SAMPLE]" in what_happened:
            what_happened = _fallback_what_happened(item)
        why_it_matters = str(prose.get("why_it_matters", "")).strip()
        if not why_it_matters or "[SAMPLE]" in why_it_matters:
            why_it_matters = _fallback_introducer_why(item)
        signal_type = str(prose.get("signal_type", "")).strip()
        introducer_intel.append(IntroducerIntelItem(
            title=item.title,
            source=item.source,
            source_url=item.url,
            what_happened=what_happened,
            why_it_matters=why_it_matters,
            signal_type=signal_type,
            published_at=item.published_at,
        ))

    # --- INTERESTING FINDS ---
    # Build a key → ScrapedItem lookup from all skeleton items so we can hydrate metadata.
    _all_skeleton: List[ScrapedItem] = (
        signal_items + commercial_items + social_items + film_items + regulatory_items
        + list(pain_items or []) + list(mauritius_items or [])
        + list(competitor_items or []) + list(introducer_items or [])
    )
    _url_to_item: Dict[str, ScrapedItem] = {i.url: i for i in _all_skeleton}
    _key_to_item: Dict[str, ScrapedItem] = {}
    for _i, it in enumerate(signal_items, 1):
        _key_to_item[f"SIGNAL/{_i}"] = it
    for _i, it in enumerate(commercial_items, 1):
        _key_to_item[f"COMM/{_i}"] = it
    for _i, it in enumerate(social_items, 1):
        _key_to_item[f"SOCIAL/{_i}"] = it
    for _i, it in enumerate(film_items, 1):
        _key_to_item[f"FILM/{_i}"] = it
    for _i, it in enumerate(regulatory_items, 1):
        _key_to_item[f"REG/{_i}"] = it
    for _i, it in enumerate(pain_items or [], 1):
        _key_to_item[f"PAIN/{_i}"] = it
    for _i, it in enumerate(mauritius_items or [], 1):
        _key_to_item[f"MAURITIUS/{_i}"] = it
    for _i, it in enumerate(competitor_items or [], 1):
        _key_to_item[f"COMPETITOR/{_i}"] = it
    for _i, it in enumerate(introducer_items or [], 1):
        _key_to_item[f"INTRODUCER/{_i}"] = it

    interesting_finds: List[InterestingFind] = []
    for find_data in (claude_data or {}).get("interesting_finds", []):
        if not isinstance(find_data, dict):
            continue
        key = str(find_data.get("key", "")).strip()
        why = str(find_data.get("why_it_stands_out", "")).strip()
        hint = str(find_data.get("category_hint", "")).strip()
        if not key or not why or "[SAMPLE]" in why:
            continue
        ref = _key_to_item.get(key) or _url_to_item.get(key)
        if ref is None:
            continue
        interesting_finds.append(InterestingFind(
            title=ref.title,
            source=ref.source,
            source_url=ref.url,
            why_it_stands_out=why,
            category_hint=hint,
            region=ref.region,
            published_at=ref.published_at,
        ))
        if len(interesting_finds) >= 5:
            break

    # --- EXECUTIVE SUMMARY ---
    claude_summary_raw = (claude_data or {}).get("executive_summary", [])
    if isinstance(claude_summary_raw, str):
        claude_summary = [b.strip("•– ").strip() for b in claude_summary_raw.split("\n") if b.strip()]
    else:
        claude_summary = [str(b).strip() for b in claude_summary_raw if str(b).strip()]
    # Filter out any [SAMPLE] contamination
    claude_summary = [b for b in claude_summary if "[SAMPLE]" not in b]

    if len(claude_summary) >= 3:
        executive_summary = claude_summary[:5]
    else:
        executive_summary = _fallback_executive_summary(
            top_signals,
            len(commercial_opportunities),
            len(regulatory_watch),
            len(film_watch),
            signal_items=signal_items,
            regulatory_items=regulatory_items,
            film_items=film_items,
            direct_mention_count=len(direct_mention_items),
            social_items=social_items,
        )

    # --- PROSPECTS THIS WEEK ---
    prospects = parse_prospects(claude_data or {})

    from models import BrandWatchResult as _BrandWatchResult
    return Digest(
        generated_at=generated_at,
        executive_summary=executive_summary,
        top_signals=top_signals,
        direct_mentions=direct_mentions,
        customer_pain=customer_pain,
        commercial_opportunities=commercial_opportunities,
        social_pain_points=social_pain_points,
        film_watch=film_watch,
        regulatory_watch=regulatory_watch,
        interesting_finds=interesting_finds,
        watchlist_topics=watchlist_topics or [],
        entity_activity=entity_activity or [],
        mauritius_intel=mauritius_intel,
        competitor_intel=competitor_intel,
        introducer_intel=introducer_intel,
        brand_watch=brand_watch or _BrandWatchResult(),
        trajectories=trajectories or [],
        prospects=prospects,
        scrape_meta=scrape,
    )


# ---------------------------------------------------------------------------
# Main digest entry point
# ---------------------------------------------------------------------------


async def generate_digest(scrape: ScrapeResult) -> Digest:
    """
    Build a tiered weekly intelligence digest with a deterministic skeleton
    enriched by a single Claude API call.

    If the API key is absent, Claude fails, or the response is malformed, the
    digest is built with deterministic fallbacks for ALL prose fields — still
    a complete, valid Digest with no '[SAMPLE]' strings.

    Pipeline:
    1. Build deterministic skeleton (partition items by section).
    2. Format skeleton into a Claude prompt.
    3. One Claude API call → prose enrichment keyed by source_url.
    4. Assemble Digest, merging Claude prose with deterministic fallbacks.
    5. Save trend_memory for next run.
    """
    items = scrape.items

    # Compute per-topic counts for momentum + trend_memory
    current_topics: Dict[str, int] = Counter(
        key
        for item in items
        for key in ([item.topic] if item.topic else [])
        + ([item.subtopic] if item.subtopic else [])
    )

    # v3: load previous entity and watchlist counts for week-over-week comparison
    prev_counts = trend_memory.load_previous_counts()
    entity_activity = _compute_entity_activity(
        scrape.entity_counts,
        prev_counts.get("entity_counts", {}),
    )
    watchlist_topics_list = _compute_watchlist_topic_list(
        scrape.watchlist_topic_counts,
        prev_counts.get("watchlist_topic_counts", {}),
    )
    logger.info(
        "v3 intelligence: %d entity signals | %d watchlist topics",
        len(entity_activity), len(watchlist_topics_list),
    )

    # Load history and compute momentum
    prev = trend_memory.load_previous_topics()
    last_week_opportunities: List[str] = prev.get("opportunities", [])

    history: List[Dict[str, Any]] = []
    try:
        if trend_memory.TREND_MEMORY_PATH.exists():
            raw_file = json.loads(trend_memory.TREND_MEMORY_PATH.read_text(encoding="utf-8"))
            history = raw_file.get("history", [])
            if not isinstance(history, list):
                history = []
    except Exception as _exc:  # noqa: BLE001
        logger.warning("trend_memory: could not load history for momentum: %s", _exc)

    momentum = trend_memory.compute_momentum(current_topics, history)
    logger.info("Momentum computed for %d topic keys", len(momentum))

    # Build deterministic skeleton — pass the dedicated community stream so the
    # social section is NOT limited to Reddit items that survived the commercial cap.
    (
        signal_items,
        commercial_items,
        social_items,
        film_items,
        regulatory_items,
        direct_mention_items,
    ) = _build_skeleton(items, community_items=scrape.community_items or None)

    # Customer pain stream — from enrich.py, already filtered and ranked
    pain_items = list(scrape.pain_items or [])

    logger.info(
        "Skeleton: %d signals, %d commercial, %d social, %d film, %d regulatory, %d direct, %d pain",
        len(signal_items), len(commercial_items), len(social_items),
        len(film_items), len(regulatory_items), len(direct_mention_items), len(pain_items),
    )

    # --- Claude call (wrapped; fallback on any failure) ---
    claude_data: Optional[Dict[str, Any]] = None
    api_key = os.environ.get("ANTHROPIC_API_KEY")

    if not api_key:
        logger.warning(
            "ANTHROPIC_API_KEY not set — building digest with deterministic fallbacks."
        )
    else:
        try:
            skeleton_text = _format_skeleton_for_claude(
                signal_items, commercial_items, social_items, film_items, regulatory_items,
                # Pain items use deterministic fallbacks (platform+user_type detection is sufficient).
                mauritius_items=list(scrape.mauritius_items or []),
                competitor_items=list(scrape.competitor_items or []),
                introducer_items=list(scrape.introducer_items or []),
            )

            trend_context = ""
            if last_week_opportunities:
                trend_context = (
                    f"\n\nLAST WEEK'S OPPORTUNITIES (for context; avoid exact duplicates):\n"
                    f"{json.dumps(last_week_opportunities[:10])}"
                )

            user_content = (
                f"Enrich the following digest skeleton for Arie Finance. "
                f"Write ONLY the prose fields requested per section. "
                f"Follow the output schema exactly — key by source_url."
                f"{trend_context}\n\n"
                f"DIGEST SKELETON:\n{skeleton_text}"
            )

            client = anthropic.Anthropic(api_key=api_key)
            logger.info("Calling Claude %s (digest) with %d items…", MODEL, len(items))
            response = await asyncio.to_thread(
                client.messages.create,
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=DIGEST_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_content}],
            )

            raw = response.content[0].text
            logger.info(
                "Claude digest response: %d chars, stop_reason=%s",
                len(raw), response.stop_reason,
            )

            if response.stop_reason == "max_tokens":
                logger.warning(
                    "Claude digest response truncated at %d tokens — using partial response + fallbacks.",
                    MAX_TOKENS,
                )
                # Attempt partial parse; if it fails we fall through to full fallback
            claude_data = _extract_json(raw)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Claude call failed (%s: %s) — building digest with deterministic fallbacks.",
                type(exc).__name__, exc,
            )
            claude_data = None

    # --- Phase 1: Brand Watch (dedicated sweep, independent of main pipeline) ---
    brand_watch_result = None
    try:
        brand_watch_result = await run_brand_watch(scrape=scrape)
        logger.info(
            "Brand Watch: %d items found | coverage_confidence=%s",
            len(brand_watch_result.items),
            brand_watch_result.coverage_confidence,
        )
    except Exception as _bw_exc:
        logger.warning("Brand Watch failed: %s — using empty result.", _bw_exc)
        from models import BrandWatchResult as _BrandWatchResult
        brand_watch_result = _BrandWatchResult(
            result="Brand Watch did not complete this run.",
            status="Sweep unavailable — check logs.",
        )

    # --- Phase 1: Theme trajectories ---
    all_items_for_themes = list(items) + list(scrape.pain_items or []) + list(scrape.mauritius_items or [])
    theme_counts = trend_memory.count_themes_in_items(all_items_for_themes)
    theme_history = trend_memory.load_theme_history()
    trajectories = trend_memory.compute_trajectories(theme_counts, theme_history, max_trajectories=5)
    logger.info("Trajectories computed: %d themes tracked.", len(trajectories))

    # Assemble final Digest
    digest = _assemble_digest(
        scrape=scrape,
        signal_items=signal_items,
        commercial_items=commercial_items,
        social_items=social_items,
        film_items=film_items,
        regulatory_items=regulatory_items,
        direct_mention_items=direct_mention_items,
        pain_items=pain_items,
        claude_data=claude_data,
        momentum=momentum,
        entity_activity=entity_activity,
        watchlist_topics=watchlist_topics_list,
        mauritius_items=list(scrape.mauritius_items or []),
        competitor_items=list(scrape.competitor_items or []),
        introducer_items=list(scrape.introducer_items or []),
        brand_watch=brand_watch_result,
        trajectories=trajectories,
    )

    # --- Phase 2: Deterministic pain classification (no Claude involvement) ---
    for _pain in digest.customer_pain:
        classify_pain_item(_pain)
    logger.info(
        "Phase 2 pain classification: %d items classified",
        len(digest.customer_pain),
    )

    # --- Source Health (deterministic, no Claude, assembled from scrape metadata) ---
    reddit_configured = bool(
        os.environ.get("REDDIT_CLIENT_ID") and os.environ.get("REDDIT_CLIENT_SECRET")
    )
    digest.source_health = build_source_health(
        source_counts=scrape.source_counts,
        source_failures=scrape.source_failures,
        reddit_configured=reddit_configured,
        last_checked=digest.generated_at,
    )
    logger.info(
        "Source health: %d sources tracked (%d failed/not_configured)",
        len(digest.source_health),
        sum(1 for s in digest.source_health if s.status in ("failed", "not_configured")),
    )

    # Persist run for next-week momentum + v3 entity/watchlist counts + theme trajectories
    run_opportunities = [r.opportunity for r in digest.top_signals if r.opportunity]
    trend_memory.save_run(
        topics=dict(current_topics),
        opportunities=run_opportunities,
        generated_at=digest.generated_at,
        entity_counts=scrape.entity_counts,
        watchlist_topic_counts=scrape.watchlist_topic_counts,
    )
    trend_memory.save_theme_counts(theme_counts, digest.generated_at)

    # Summary log
    logger.info(
        "Digest assembled: %d signals | %d direct | %d pain | %d commercial | "
        "%d social | %d film | %d regulatory | exec_summary=%d bullets",
        len(digest.top_signals),
        len(digest.direct_mentions),
        len(digest.customer_pain),
        len(digest.commercial_opportunities),
        len(digest.social_pain_points),
        len(digest.film_watch),
        len(digest.regulatory_watch),
        len(digest.executive_summary),
    )

    return digest


# ---------------------------------------------------------------------------
# Legacy verify helper (kept for any callers referencing it)
# ---------------------------------------------------------------------------


def verify_digest_sections(digest: Digest) -> Dict[str, bool]:
    """Returns section_name -> present for key digest elements."""
    input_urls: set = set()
    if digest.scrape_meta:
        input_urls = {item.url for item in digest.scrape_meta.items}

    top = digest.top_signals
    return {
        "executive_summary (>=3)":            len(digest.executive_summary) >= 3,
        "top_signals (>=1)":                  len(top) >= 1,
        "top_signals.source_url present":     all(bool(r.source_url) for r in top),
        "top_signals.source_url grounded":    all(r.source_url in input_urls for r in top) if input_urls else True,
        "top_signals.confidence valid":       all(r.confidence in ("High", "Medium", "Low") for r in top),
        "top_signals.what_happened present":  all(bool(r.what_happened) for r in top),
        "top_signals.why_it_matters present": all(bool(r.why_it_matters) for r in top),
        "top_signals.suggested_action present": all(bool(r.suggested_action) for r in top),
        "no [SAMPLE] in prose":               not any(
            "[SAMPLE]" in (r.why_it_matters + r.suggested_action + r.what_happened)
            for r in top
        ),
    }
