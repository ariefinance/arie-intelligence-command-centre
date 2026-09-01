"""
Deterministic (no-LLM) enrichment pipeline for Arie Finance Market Intelligence.

Exported function
-----------------
    enrich(result: ScrapeResult) -> ScrapeResult

For each ScrapedItem the pipeline:
1. Classifies topic + subtopic via taxonomy keyword match.
2. Tags region via REGION_KEYWORDS.
3. Sets source_weight from SOURCE_WEIGHTS.
4. Detects watchlist_hits (named entities in title+summary).
5. Computes relevance_score (general relevance) and commercial_score (Arie BD signal).
6. Cross-source deduplicates near-duplicate titles (Jaccard > DEDUP_THRESHOLD),
   accumulating supporting_source_count on the surviving item.
7. Applies operational-noise demotion.
8. Sorts by commercial_score DESC (relevance_score as tie-break).
9. Enforces FILM_MAX_SHARE cap on generic film items.
10. Caps at ENRICHMENT_CAP items.

Relevance score formula
-----------------------
    relevance_score = (
        topic_priority_weight          # TOPIC_PRIORITY[topic], default 0.9
      + subtopic_boost                 # SUBTOPIC_PRIORITY_BOOST[subtopic], default 0.0
      + region_boost                   # REGION_BOOST[region]
      + recency_score                  # 0..0.3 (today=0.3, 7+ days ago=0.0)
      + source_weight * 0.5            # scaled to 0..0.5 contribution
      + watchlist_boost                # n_hits * WATCHLIST_HIT_BOOST
      + engagement_boost               # Reddit: log(score+1)*0.02 + log(comments+1)*0.01
    )
    clamped to [0.0, 3.0] for readability.

Commercial score formula
------------------------
    commercial_score = (
        sum(weight for each matched COMMERCIAL_SIGNALS signal)  # each signal counted once
      + min(supporting_source_count - 1, 5) * 0.1              # +0.1 per extra source, capped at +0.5
      + region_corridor_boost                                    # REGION_BOOST[region] (same as relevance)
      + source_weight * 0.3                                      # source quality contribution
    )
    Film items with NO payment-angle keyword AND no watchlist/corridor hits
    are multiplied by 0.4 before ranking (generic film demotion).
    clamped to [0.0, 5.0].

Final ranking: commercial_score DESC, relevance_score as tie-break.
Film cap: after ranking, generic film items (no payment angle) may occupy at most
FILM_MAX_SHARE of the ENRICHMENT_CAP slots. Film items with a payment angle bypass the cap.

Only stdlib used: re, datetime, math, string.
"""
from __future__ import annotations

import logging
import math
import re
import string
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

from config import (
    COMMERCIAL_SIGNALS,
    COMPETITOR_INTEL_SOURCES,
    COMPETITOR_INTEL_CAP,
    COMPETITOR_NAMES,
    DEDUP_THRESHOLD,
    DIRECT_MENTION_ENTITIES,
    ENRICHMENT_CAP,
    FILM_MAX_SHARE,
    FILM_PAYMENT_ANGLE_KEYWORDS,
    INTRODUCER_FILTER_KEYWORDS,
    INTRODUCER_SERVICE_KEYWORDS,
    INTRODUCER_JURISDICTION_KEYWORDS,
    INTRODUCER_EVENT_KEYWORDS,
    INTRODUCER_INTEL_SOURCES,
    INTRODUCER_INTEL_CAP,
    MARKET_OPPORTUNITIES_SUBTOPICS,
    MAURITIUS_INTEL_SOURCES,
    MAURITIUS_INTEL_CAP,
    NAMED_ENTITIES,
    OPERATIONAL_NOISE_MULTIPLIER,
    OPERATIONAL_NOISE_PATTERNS,
    MAURITIUS_GUIDE_EXCLUSION_PATTERNS,
    PAIN_CAP,
    PAIN_EXCLUDE_KEYWORDS,
    PAIN_SIGNAL_KEYWORDS,
    PAIN_SOURCE_CAP,
    PAIN_USER_TYPE_KEYWORDS,
    PROCUREMENT_HARD_DROP_PATTERNS,
    REGION_BOOST,
    REGION_KEYWORDS,
    SOURCE_WEIGHTS,
    SUBTOPIC_PRIORITY_BOOST,
    TOPIC_KEYWORDS,
    TOPIC_PRIORITY,
    WATCHLIST,
    WATCHLIST_HIT_BOOST,
    WATCHLIST_TOPICS,
)
from models import CustomerPainItem, ScrapedItem, ScrapeResult


# ---------------------------------------------------------------------------
# Step 1: classify topic + subtopic
# ---------------------------------------------------------------------------

def _classify_topic(text: str) -> Tuple[str, Optional[str]]:
    """
    Return (topic, subtopic) by first-match on lowercase keyword lists.
    Checks market_opportunities subtopics first (for precision), then falls back
    to core topics. Default: ("fintech_infrastructure", None).
    """
    lower = text.lower()

    # Check each market_opportunities subtopic explicitly
    for subtopic, keywords in MARKET_OPPORTUNITIES_SUBTOPICS.items():
        if any(kw in lower for kw in keywords):
            return "market_opportunities", subtopic

    # Core topics
    for topic, keywords in TOPIC_KEYWORDS.items():
        if topic == "market_opportunities":
            continue  # already handled above
        if any(kw in lower for kw in keywords):
            return topic, None

    return "fintech_infrastructure", None


# ---------------------------------------------------------------------------
# Step 2: tag region
# ---------------------------------------------------------------------------

def _tag_region(text: str) -> str:
    """Return the highest-priority region found in text (mauritius first)."""
    lower = text.lower()
    # Priority order: mauritius > africa > india > mena > asia > uk > eu > us
    priority = ["mauritius", "africa", "india", "mena", "asia", "uk", "eu", "us"]
    for region in priority:
        if any(kw in lower for kw in REGION_KEYWORDS[region]):
            return region
    return "global"


# ---------------------------------------------------------------------------
# Step 3: source weight lookup
# ---------------------------------------------------------------------------

def _get_source_weight(source: str) -> float:
    """
    Match source against SOURCE_WEIGHTS by prefix.
    e.g. "Reddit/r/fintech" matches "Reddit" key.
    """
    for key, weight in SOURCE_WEIGHTS.items():
        if source.startswith(key):
            return weight
    return 0.5  # sensible default


# ---------------------------------------------------------------------------
# Step 4: watchlist hit detection
# ---------------------------------------------------------------------------

def _detect_watchlist_hits(text: str) -> List[str]:
    """Return list of watchlist entity names found (case-insensitive) in text."""
    lower = text.lower()
    return [entity for entity in WATCHLIST if entity.lower() in lower]


# ---------------------------------------------------------------------------
# Step 4b: direct-mention detection (Arie Finance / ACBM own entities)
# ---------------------------------------------------------------------------

# Pre-compiled patterns for each direct-mention entity.
# Phrases like "Arie Finance", "Arie Capital", "ACBM" use word-boundary guards
# to avoid false positives (e.g. "Malarie Finance" would not match).
# "ariefinance" is a URL/handle form and is matched as a plain substring.
_DIRECT_MENTION_PATTERNS: List[Tuple[str, re.Pattern]] = []

def _build_direct_mention_patterns() -> None:
    """Build regex patterns for DIRECT_MENTION_ENTITIES once at import time."""
    for entity in DIRECT_MENTION_ENTITIES:
        lower = entity.lower()
        if lower == "ariefinance":
            # URL/handle — substring match only, no boundary needed
            pattern = re.compile(re.escape(lower), re.IGNORECASE)
        else:
            # Phrase match with word-boundary guards on both sides
            pattern = re.compile(r"(?<!\w)" + re.escape(lower) + r"(?!\w)", re.IGNORECASE)
        _DIRECT_MENTION_PATTERNS.append((entity, pattern))

_build_direct_mention_patterns()


def _detect_direct_mentions(text: str) -> List[str]:
    """
    Return list of DIRECT_MENTION_ENTITIES names found in text.
    Matching is case-insensitive with word-boundary guards to avoid false positives.
    """
    return [entity for entity, pattern in _DIRECT_MENTION_PATTERNS if pattern.search(text)]


# ---------------------------------------------------------------------------
# Step 5: recency score
# ---------------------------------------------------------------------------

def _recency_score(published_at: Optional[str]) -> float:
    """
    Return a recency bonus 0..0.3.
    - today or yesterday: 0.30
    - 2-3 days ago: 0.20
    - 4-7 days ago: 0.10
    - older / unknown: 0.00
    """
    if not published_at:
        return 0.05  # small non-zero so undated items aren't fully penalised

    try:
        pub_date = date.fromisoformat(published_at[:10])
    except (ValueError, TypeError):
        return 0.05

    today = datetime.now(tz=timezone.utc).date()
    age = (today - pub_date).days

    if age <= 1:
        return 0.30
    if age <= 3:
        return 0.20
    if age <= 7:
        return 0.10
    return 0.00


# ---------------------------------------------------------------------------
# Step 5: engagement boost (Reddit only)
# ---------------------------------------------------------------------------

def _engagement_boost(item: ScrapedItem) -> float:
    """Log-scaled boost from Reddit upvotes and comment counts."""
    boost = 0.0
    if item.score is not None:
        boost += math.log(max(item.score, 0) + 1) * 0.02
    if item.num_comments is not None:
        boost += math.log(max(item.num_comments, 0) + 1) * 0.01
    return boost


# ---------------------------------------------------------------------------
# Step 5: compute relevance_score (general relevance)
# ---------------------------------------------------------------------------

def _compute_relevance_score(item: ScrapedItem) -> float:
    """
    relevance_score = topic_priority + subtopic_boost + region_boost
                    + recency + source_weight*0.5 + watchlist_boost + engagement

    Clamped to [0.0, 3.0].
    """
    topic_priority = TOPIC_PRIORITY.get(item.topic or "fintech_infrastructure", 0.9)
    subtopic_boost = SUBTOPIC_PRIORITY_BOOST.get(item.subtopic or "", 0.0)
    region_boost = REGION_BOOST.get(item.region or "global", 0.0)
    recency = _recency_score(item.published_at)
    src_contrib = (item.source_weight or 0.5) * 0.5
    watchlist_boost = len(item.watchlist_hits) * WATCHLIST_HIT_BOOST
    engagement = _engagement_boost(item)

    raw = (topic_priority + subtopic_boost + region_boost
           + recency + src_contrib + watchlist_boost + engagement)
    return min(max(raw, 0.0), 3.0)


# ---------------------------------------------------------------------------
# Step 5c: compute commercial_score (Arie BD signal)
# ---------------------------------------------------------------------------

def _compute_commercial_score(item: ScrapedItem) -> float:
    """
    commercial_score = (
        sum(weight for each matched COMMERCIAL_SIGNALS signal)   # each signal once
      + min(supporting_source_count - 1, 5) * 0.1               # +0.1 per extra source, capped at +0.5
      + region_corridor_boost                                     # REGION_BOOST[region]
      + source_weight * 0.3                                       # source quality contribution
    )
    Clamped to [0.0, 5.0].
    """
    text = f"{item.title} {item.summary}".lower()

    signal_score = 0.0
    for _name, (keywords, weight) in COMMERCIAL_SIGNALS.items():
        if any(kw in text for kw in keywords):
            signal_score += weight

    # Supporting-source bonus: +0.1 per additional source that covered the same story, max +0.5
    multi_source_bonus = min(item.supporting_source_count - 1, 5) * 0.1

    region_boost = REGION_BOOST.get(item.region or "global", 0.0)
    src_contrib = (item.source_weight or 0.5) * 0.3

    raw = signal_score + multi_source_bonus + region_boost + src_contrib
    return min(max(raw, 0.0), 5.0)


# ---------------------------------------------------------------------------
# Community relevance scoring (Reddit and other discourse sources)
# ---------------------------------------------------------------------------

# Source prefixes that indicate community / discourse content
_COMMUNITY_SOURCE_PREFIXES: tuple = ("Reddit",)

# Pain-point / question signals that boost community relevance
_PAIN_POINT_KEYWORDS: tuple = (
    "how do", "how can", "problem", "struggling", "fees", "chargeback",
    "frozen account", "frozen", "hold", "declined", "can't", "cannot",
    "issue", "help", "advice", "dispute", "delay", "kyc", "onboarding",
    "cross-border", "international payment", "fx", "currency", "frustrated",
    "stuck", "blocked", "rejected", "failed", "slow", "expensive", "question",
    "anyone know", "anyone else", "experience with", "bank refused", "transfer failed",
    "account closed", "compliance", "wire", "swift", "correspondent",
)


def _is_community_source(source: str) -> bool:
    """True if the source indicates community / discourse content (e.g. Reddit)."""
    return any(source.startswith(prefix) for prefix in _COMMUNITY_SOURCE_PREFIXES)


def _community_score(item: ScrapedItem) -> float:
    """
    Score an item for community relevance (independent of commercial_score).

    Formula:
        engagement   = log(score+1)*0.15 + log(num_comments+1)*0.10   (log-dampened)
        pain_boost   = 0.30 if any pain-point keyword in title+summary
        score = engagement + pain_boost
    Clamped to [0.0, 5.0].
    """
    engagement = 0.0
    if item.score is not None:
        engagement += math.log(max(item.score, 0) + 1) * 0.15
    if item.num_comments is not None:
        engagement += math.log(max(item.num_comments, 0) + 1) * 0.10

    text = f"{item.title} {item.summary}".lower()
    pain_boost = 0.30 if any(kw in text for kw in _PAIN_POINT_KEYWORDS) else 0.0

    return min(engagement + pain_boost, 5.0)


# Number of community items to keep in the dedicated stream
_COMMUNITY_CAP: int = 8


# ---------------------------------------------------------------------------
# Film relevance helpers
# ---------------------------------------------------------------------------

_FILM_KEYWORDS: Set[str] = {
    "film production", "film fund", "film rebate", "film incentive", "film commission",
    "co-production", "coproduction", "feature film", "motion picture",
    "production studio", "film studio", "streaming content production",
    "content production", "series production", "hollywood", "bollywood",
    "nollywood", "cinema production",
}


def _is_film_item(item: ScrapedItem) -> bool:
    """True if item is classified as film_production subtopic or matches film keywords."""
    if item.topic == "market_opportunities" and item.subtopic == "film_production":
        return True
    text = f"{item.title} {item.summary}".lower()
    return any(kw in text for kw in _FILM_KEYWORDS)


def _has_film_payment_angle(item: ScrapedItem) -> bool:
    """True if film item has at least one direct Arie payment/treasury keyword."""
    text = f"{item.title} {item.summary}".lower()
    return any(kw in text for kw in FILM_PAYMENT_ANGLE_KEYWORDS)


def _film_has_direct_relevance(item: ScrapedItem) -> bool:
    """True if film item bypasses the FILM_MAX_SHARE cap.
    Bypass conditions: payment-angle keyword OR watchlist hit OR priority corridor region.
    """
    if _has_film_payment_angle(item):
        return True
    if item.watchlist_hits:
        return True
    if item.region in ("mauritius", "africa", "india", "mena"):
        return True
    return False


# ---------------------------------------------------------------------------
# Step 5b: operational-noise demotion
# ---------------------------------------------------------------------------

_NOISE_RES = [re.compile(p, re.IGNORECASE) for p in OPERATIONAL_NOISE_PATTERNS]


def _is_operational_noise(title: str) -> bool:
    """Return True if the title matches any operational-noise pattern."""
    return any(rx.search(title) for rx in _NOISE_RES)


# ---------------------------------------------------------------------------
# Step 6: near-duplicate deduplication (Jaccard on word tokens)
# Accumulates supporting_source_count on the surviving item.
# ---------------------------------------------------------------------------

_PUNCT_RE = re.compile(r"[" + re.escape(string.punctuation) + r"]")


def _tokenize(text: str) -> Set[str]:
    """Lowercase, normalize possessives, strip punctuation, split on whitespace. Drop tokens < 3 chars."""
    lower = text.lower()
    # Strip possessive suffixes before punct removal so "FATF's" and "FATF" tokenize identically.
    lower = re.sub(r"[’']s\b", "", lower)
    clean = _PUNCT_RE.sub(" ", lower)
    return {t for t in clean.split() if len(t) >= 3}


def _jaccard(a: Set[str], b: Set[str]) -> float:
    if not a and not b:
        return 1.0
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def _dedup(items: List[ScrapedItem]) -> List[ScrapedItem]:
    """
    Remove near-duplicate titles using Jaccard similarity on word tokens.
    When two items exceed DEDUP_THRESHOLD, keep the one with higher source_weight.
    The surviving item accumulates supporting_source_count from all merged duplicates.
    O(n^2) but n is at most a few hundred items here.
    """
    kept: List[ScrapedItem] = []
    token_sets: List[Set[str]] = []

    for item in items:
        tokens = _tokenize(item.title)
        duplicate = False
        for i, existing_tokens in enumerate(token_sets):
            if _jaccard(tokens, existing_tokens) >= DEDUP_THRESHOLD:
                # Merge: accumulate supporting_source_count
                kept[i].supporting_source_count += 1
                # Prefer higher source_weight survivor
                if item.source_weight > kept[i].source_weight:
                    # Transfer accumulated count to the higher-quality item
                    new_count = kept[i].supporting_source_count
                    kept[i] = item.model_copy()
                    kept[i].supporting_source_count = new_count
                    token_sets[i] = tokens
                duplicate = True
                break
        if not duplicate:
            kept.append(item)
            token_sets.append(tokens)

    return kept


# ---------------------------------------------------------------------------
# Step 6b: soft cluster dedup — merge multi-publisher coverage of the same story
# ---------------------------------------------------------------------------

# Event-specific anchor words. Two items sharing ≥1 of these are likely covering
# the same institutional event. Deliberately excludes general names (fsc, sec) that
# appear in unrelated stories (different companies getting licences, different
# enforcement targets) to prevent false merges.
_CLUSTER_EVENT_ANCHORS: frozenset = frozenset({
    # Named bodies that publish specific decisions/reports
    "fatf", "imf", "bis", "bcbs", "iosco", "amla", "cftc",
    # Regulatory frameworks
    "mica", "vasp", "cbdc", "stablecoin", "stablecoins",
    # Event-specific qualifiers (too specific to appear in unrelated stories)
    "greylist", "greylisted", "blacklist", "blacklisted",
    "plenary", "evaluation",
    # Position/appointment words (meaningful when co-occurring with an institution token)
    "vice", "presidency", "appointed", "elected",
})

_SOFT_DEDUP_THRESHOLD: float = 0.25   # looser than DEDUP_THRESHOLD (0.70)
_SOFT_DEDUP_WINDOW_DAYS: int = 3       # items must be published within 3 days


def _days_apart(a: ScrapedItem, b: ScrapedItem) -> Optional[int]:
    """Return absolute date difference in days, or None if either date is unknown."""
    if not a.published_at or not b.published_at:
        return None
    try:
        da = date.fromisoformat(a.published_at[:10])
        db = date.fromisoformat(b.published_at[:10])
        return abs((da - db).days)
    except (ValueError, TypeError):
        return None


def _soft_dedup(items: List[ScrapedItem]) -> List[ScrapedItem]:
    """
    Second-pass dedup for multi-publisher clusters that evade the Jaccard threshold
    (e.g. "FATF India vice-presidency" covered by 4 outlets with different phrasing).

    An item merges into an existing survivor when ALL conditions hold:
      1. They share ≥1 event-specific anchor word from _CLUSTER_EVENT_ANCHORS.
      2. Title Jaccard ≥ _SOFT_DEDUP_THRESHOLD.
      3. Published within _SOFT_DEDUP_WINDOW_DAYS (ignored when either date is missing).

    supporting_source_count is accumulated; the higher source_weight item survives.
    Must be called AFTER _dedup() and the stale filter.
    """
    kept: List[ScrapedItem] = []
    token_sets: List[Set[str]] = []

    for item in items:
        tokens = _tokenize(item.title)
        item_anchors = tokens & _CLUSTER_EVENT_ANCHORS

        if not item_anchors:
            kept.append(item)
            token_sets.append(tokens)
            continue

        merged = False
        for i, existing_tokens in enumerate(token_sets):
            if not (item_anchors & (existing_tokens & _CLUSTER_EVENT_ANCHORS)):
                continue
            gap = _days_apart(item, kept[i])
            if gap is not None and gap > _SOFT_DEDUP_WINDOW_DAYS:
                continue
            if _jaccard(tokens, existing_tokens) < _SOFT_DEDUP_THRESHOLD:
                continue
            kept[i].supporting_source_count += item.supporting_source_count
            if item.source_weight > kept[i].source_weight:
                new_count = kept[i].supporting_source_count
                kept[i] = item.model_copy()
                kept[i].supporting_source_count = new_count
                token_sets[i] = tokens
            merged = True
            break

        if not merged:
            kept.append(item)
            token_sets.append(tokens)

    return kept


# ---------------------------------------------------------------------------
# Stale item filter — items older than this cutoff are excluded from all sections.
# Items with no published_at date pass through (unknown age ≠ stale).
# ---------------------------------------------------------------------------

_STALE_CUTOFF_DAYS: int = 60


def _is_stale(item: ScrapedItem) -> bool:
    """Return True if item's published_at is more than _STALE_CUTOFF_DAYS days old."""
    if not item.published_at:
        return False
    try:
        pub = date.fromisoformat(item.published_at[:10])
        return (datetime.now(tz=timezone.utc).date() - pub).days > _STALE_CUTOFF_DAYS
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# Customer Pain Intelligence enrichment
# (completely separate from commercial scoring — never competes with fintech news)
# ---------------------------------------------------------------------------

_PAIN_COMMUNITY_SOURCES: tuple = ("Reddit", "HackerNews")


def _detect_pain_user_type(text: str) -> str:
    """Return first matching user type from PAIN_USER_TYPE_KEYWORDS, or empty string."""
    lower = text.lower()
    for user_type, keywords in PAIN_USER_TYPE_KEYWORDS.items():
        if any(kw in lower for kw in keywords):
            return user_type
    return ""


# For non-community (news) sources, at least one of these keywords must appear
# alongside the pain signal keyword — prevents generic KYC info articles and
# domestic-only regulatory news from being treated as user pain complaints.
_PAIN_NEWS_BUSINESS_KEYWORDS: frozenset = frozenset({
    "business account", "sme account", "company account", "corporate account",
    "business banking", "sme banking", "business onboarding", "sme onboarding",
    "account closed", "account frozen", "account freeze", "account suspended",
    "account blocked", "account terminated", "account restricted",
    "payment delay", "payment delayed", "payment stuck", "payment failed",
    "transfer delay", "transfer failed", "transfer stuck",
    "cross-border payment", "cross border payment", "international payment",
    "debanked", "de-banked", "de-risked", "derisked",
    "kyb", "know your business",
    "onboarding delay", "due diligence delay",
    "sme", "small business", "small and medium",
    "merchant account", "banking access denied",
})


# ---------------------------------------------------------------------------
# Strict Customer Experience relevance gate
# ---------------------------------------------------------------------------
# An item qualifies for Customer Experience Intelligence ONLY when it shows clear
# evidence of a payment / banking / account / onboarding pain theme. Generic AI,
# startup, fintech, crypto, funding, hardware, SaaS, product, or market
# discussion must NOT qualify. Deterministic, no LLM.

# Strong, self-contained signals — any one of these qualifies the item.
_CX_STRONG_TERMS: frozenset = frozenset({
    # tracked payment/banking platforms (high-signal for this domain)
    "wise", "revolut", "payoneer", "airwallex", "stripe", "monzo", "starling",
    "n26", "paypal", "neobank", "mercury", "brex",
    # account access / closure / freeze
    "frozen", "froze", "freeze", "freezing", "debank", "de-bank", "debanked",
    "de-banked", "de-risk", "derisk", "de-risked", "derisked",
    "account closure", "closed my account", "closed account", "account closed",
    "account suspended", "account terminated", "account restricted",
    "blocked account", "account blocked", "locked account", "account locked",
    "locked out", "frozen funds", "funds frozen", "funds held", "held funds",
    "fund access", "access to funds", "access my funds", "withheld",
    # kyc / kyb / onboarding / compliance / documents
    "kyc", "kyb", "know your customer", "know your business",
    "onboarding", "compliance review", "due diligence", "document request",
    # explicit payment / transfer failure phrases
    "rejected payment", "payment rejected", "declined payment", "payment declined",
    "blocked payment", "payment blocked", "payment failed", "failed payment",
    "payment failure", "transfer failed", "failed transfer", "stuck transfer",
    "transfer stuck", "delayed payment", "payment delayed", "payment delay",
    "transfer delay", "missing payment", "chargeback",
    # NOTE: purely-topical payment terms (cross-border payment, remittance,
    # payment corridor, payout, international payment) are deliberately NOT
    # "strong" — a market/opportunity discussion mentions them without any
    # complaint. They qualify only via the money+problem tier below.
})

# Generic money/payment-flow nouns — qualify ONLY when paired with a problem word.
_CX_MONEY_TERMS: frozenset = frozenset({
    "payment", "payments", "transfer", "transaction", "deposit", "withdrawal",
    "withdraw", "wire", "ach", "sepa", "swift", "remittance", "payout",
    "cross-border", "cross border", "corridor",
})
_CX_PROBLEM_TERMS: frozenset = frozenset({
    "stuck", "delay", "delayed", "late", "pending", "blocked", "reject",
    "rejected", "declin", "fail", "failed", "frozen", "froze", "held",
    "missing", "lost", "not arrive", "didn't arrive", "did not arrive",
    "disappear", "no response", "not responded", "unresponsive",
    "can't access", "cannot access", "stopped working",
})


def is_cx_relevant(text: str) -> bool:
    """
    Strict relevance gate for Customer Experience Intelligence.

    True only when the text shows a clear payment/banking/account/onboarding
    pain theme. Generic AI / startup / fintech / crypto / funding / hardware /
    SaaS / market discussion returns False. Deterministic, no LLM.
    """
    t = (text or "").lower()
    if any(term in t for term in _CX_STRONG_TERMS):
        return True
    if any(m in t for m in _CX_MONEY_TERMS) and any(p in t for p in _CX_PROBLEM_TERMS):
        return True
    return False


def _is_genuine_pain_item(item: ScrapedItem) -> bool:
    """
    True if item is a genuine user/business complaint about a payment platform.
    Requires at least one pain signal keyword and no promotional/news exclusion keywords.
    Non-community sources (GNews, news sites) also require a business-context keyword
    to exclude generic KYC info articles and domestic-only regulatory news.
    """
    text = f"{item.title} {item.summary}".lower()
    if not any(kw in text for kw in PAIN_SIGNAL_KEYWORDS):
        return False
    if any(kw in text for kw in PAIN_EXCLUDE_KEYWORDS):
        return False
    # Non-community sources need explicit business-context evidence
    if not any(item.source.startswith(prefix) for prefix in _PAIN_COMMUNITY_SOURCES):
        if not any(kw in text for kw in _PAIN_NEWS_BUSINESS_KEYWORDS):
            return False
    return True


def _pain_item_score(item: ScrapedItem) -> float:
    """
    Score a pain item for ranking. Higher = stronger genuine complaint signal.
    Does NOT use commercial_score — completely separate scoring axis.
    """
    text = f"{item.title} {item.summary}".lower()

    # Platform mention via watchlist_hits (already computed)
    platform_boost = 0.6 if item.watchlist_hits else 0.0

    # Pain keyword density
    n_kw = sum(1 for kw in PAIN_SIGNAL_KEYWORDS if kw in text)
    kw_score = min(n_kw * 0.25, 1.25)

    # Recency
    recency = _recency_score(item.published_at)

    # Engagement (Reddit/HN upvotes and comments)
    engagement = 0.0
    if item.score is not None:
        engagement += math.log(max(item.score, 0) + 1) * 0.05
    if item.num_comments is not None:
        engagement += math.log(max(item.num_comments, 0) + 1) * 0.03

    # Trust boost: named sources score slightly higher
    source_boost = _get_source_weight(item.source) * 0.2

    return min(platform_boost + kw_score + recency + engagement + source_boost, 5.0)


def enrich_pain_items(
    pain_items_raw: List[ScrapedItem],
    all_enriched: List[ScrapedItem],
) -> List[ScrapedItem]:
    """
    Build the Customer Pain Intelligence stream.

    Sources:
    - pain_items_raw: scraped from HN, Trustpilot, GNews/Pain*, manual_sources.json
    - all_enriched: pull Reddit/HN items from the main enriched stream that pass the
      pain signal filter (so Reddit counts as one source, not the whole stream)

    Returns a deduplicated list sorted by pain score DESC, capped at PAIN_CAP,
    with a per-source cap of PAIN_SOURCE_CAP to prevent any single source dominating.
    """
    candidates: List[ScrapedItem] = []

    # Enrich raw pain items (region, source weight, watchlist hits)
    for raw in pain_items_raw:
        item = raw.model_copy()
        text = f"{item.title} {item.summary}"
        item.region = _tag_region(text)
        item.source_weight = _get_source_weight(item.source)
        item.watchlist_hits = _detect_watchlist_hits(text)
        candidates.append(item)

    # Pull qualifying Reddit/HN items from main enriched stream
    for item in all_enriched:
        src = item.source
        if any(src.startswith(prefix) for prefix in _PAIN_COMMUNITY_SOURCES):
            candidates.append(item.model_copy())

    # Drop stale items (same cutoff as main enrichment pipeline)
    candidates = [i for i in candidates if not _is_stale(i)]

    # Filter to genuine complaints only
    candidates = [i for i in candidates if _is_genuine_pain_item(i)]

    # Strict relevance gate: drop generic tech/AI/fintech/funding discussions
    # that slipped through (e.g. "Agentic AI + Everyday Hardware"). Title-anchored
    # (NOT the body) so an off-topic post that only mentions payments in passing
    # is dropped before Claude — no misleading ARIE relevance is generated for it.
    candidates = [
        i for i in candidates if is_cx_relevant(i.title)
    ]

    # Dedup by URL (not title — pain items from different sources about same platform
    # are independently valuable)
    seen: set = set()
    deduped: List[ScrapedItem] = []
    for item in candidates:
        if item.url and item.url not in seen:
            seen.add(item.url)
            deduped.append(item)

    # Sort by pain score DESC
    deduped.sort(key=_pain_item_score, reverse=True)

    # Apply per-source cap then overall PAIN_CAP
    from collections import Counter
    src_counts: Counter = Counter()
    result: List[ScrapedItem] = []
    for item in deduped:
        src_key = item.source.split("/")[0]
        if src_counts[src_key] >= PAIN_SOURCE_CAP:
            continue
        src_counts[src_key] += 1
        result.append(item)
        if len(result) >= PAIN_CAP:
            break

    return result


# ---------------------------------------------------------------------------
# Phase 2: deterministic CustomerPainItem classifier
# ---------------------------------------------------------------------------

# Deterministic recommended_internal_action map (pain_type → action string).
# Module-level so it can be imported and tested independently.
PAIN_TYPE_ACTIONS: Dict[str, str] = {
    "frozen_account":       "Review onboarding & account-closure communications; ensure ARIE messaging on fund security is clear.",
    "account_closure":      "Review onboarding & account-closure communications; ensure ARIE messaging on fund security is clear.",
    "kyc_friction":         "Review KYC/KYB onboarding communication and document-request UX.",
    "delayed_onboarding":   "Review onboarding timelines and applicant status messaging.",
    "transfer_delay":       "Monitor corridor/payment category; prepare client-service FAQ on transfer timelines.",
    "payment_rejection":    "Monitor corridor/payment category; prepare client-service FAQ on transfer timelines.",
    "poor_support":         "Benchmark ARIE support responsiveness; prepare client-service FAQ.",
    "fee_pricing":          "Review website pricing transparency and messaging.",
    "compliance_derisking": "Risk watch: monitor de-risking trend; brief Compliance.",
    "other":                "No immediate action; monitor.",
    "":                     "No immediate action; monitor.",
}

# Keywords that trigger a compliance_caution note.
_COMPLIANCE_CAUTION_KEYWORDS: frozenset = frozenset({
    "freeze", "frozen", "froze", "freezing", "closure", "closed",
    "debank", "de-risk", "derisk",
    "kyc", "kyb", "sanction", "compliance", "fraud",
    "rejected payment", "fund access", "funds held",
})

_COMPLIANCE_CAUTION_TEXT = (
    "Sensitive topic (account access / compliance). "
    "Treat as market risk awareness only — do not use for targeted outreach to affected users."
)


def classify_pain_item(item: CustomerPainItem) -> None:
    """
    Derive and set the 7 Phase-2 classification fields on a CustomerPainItem in place.
    Pure deterministic function — no LLM, no I/O.

    Fields set: source_type, pain_type, severity, source_quality,
                confidence, compliance_caution, recommended_internal_action.
    """
    combined = f"{item.source} {item.pain_point} {item.evidence_excerpt}".lower()
    src_lower = item.source.lower()

    # --- source_type ---
    if "trustpilot" in src_lower:
        source_type = "review"
    elif (
        "reddit" in src_lower
        or "hackernews" in src_lower
        or "hacker news" in src_lower
        or src_lower.startswith("hn")
    ):
        source_type = "forum_discussion"
    elif (
        "gnews/pain" in src_lower
        or "google news" in src_lower
        or "gnews" in src_lower
        or "gn/" in src_lower
    ):
        source_type = "news_about_complaint"
    else:
        source_type = "unknown"

    # --- pain_type (first keyword match; order matters) ---
    pain_type_rules: List[Tuple[str, List[str]]] = [
        ("frozen_account",       ["freeze", "frozen", "froze", "freezing",
                                   "locked account", "account locked", "locked out",
                                   "on hold", "funds held"]),
        ("account_closure",      ["account closure", "closed my account", "closed account",
                                   "debank", "de-risk", "derisk"]),
        ("kyc_friction",         ["kyc", "kyb", "verification", "identity check", "document"]),
        ("delayed_onboarding",   ["onboarding", "sign up", "signup", "opening an account", "approval"]),
        ("transfer_delay",       ["transfer", "transaction", "payment delay", "stuck",
                                   "pending", "didn't arrive"]),
        ("payment_rejection",    ["rejected", "declined", "blocked payment"]),
        ("poor_support",         ["support", "customer service", "no response",
                                   "chatbot", "can't reach"]),
        ("fee_pricing",          ["fee", "fees", "pricing", "charge", "exchange rate", "hidden cost"]),
        ("compliance_derisking", ["compliance", "sanction", "fraud"]),
    ]
    pain_type = "other"
    for pt, keywords in pain_type_rules:
        if any(kw in combined for kw in keywords):
            pain_type = pt
            break

    # --- severity ---
    high_keywords = frozenset({
        "freeze", "frozen", "froze", "freezing", "locked account", "account locked",
        "locked out", "lost access", "funds held",
        "closure", "closed", "debank", "can't access", "sanction",
    })
    medium_keywords = frozenset({
        "onboarding", "kyc", "verification", "support", "delay",
        "rejected", "declined", "pending",
    })
    if any(kw in combined for kw in high_keywords):
        severity = "high"
    elif any(kw in combined for kw in medium_keywords):
        severity = "medium"
    else:
        severity = "low"

    # --- source_quality ---
    is_direct = source_type in ("review", "forum_discussion")
    has_good_excerpt = len(item.evidence_excerpt.strip()) > 40
    if is_direct and has_good_excerpt:
        source_quality = "high"
    elif is_direct:
        source_quality = "medium"
    elif source_type == "news_about_complaint":
        source_quality = "medium"
    else:
        source_quality = "low"

    # --- confidence ---
    if source_quality == "high" and severity == "high":
        confidence = "high"
    elif source_quality == "low" or severity == "low":
        confidence = "low"
    else:
        confidence = "medium"

    # --- compliance_caution ---
    if any(kw in combined for kw in _COMPLIANCE_CAUTION_KEYWORDS):
        compliance_caution = _COMPLIANCE_CAUTION_TEXT
    else:
        compliance_caution = ""

    # --- recommended_internal_action ---
    recommended_internal_action = PAIN_TYPE_ACTIONS.get(pain_type, PAIN_TYPE_ACTIONS["other"])

    # Mutate in place
    item.source_type = source_type
    item.pain_type = pain_type
    item.severity = severity
    item.source_quality = source_quality
    item.confidence = confidence
    item.compliance_caution = compliance_caution
    item.recommended_internal_action = recommended_internal_action


# ---------------------------------------------------------------------------
# v3: named entity and watchlist topic counting (runs over ALL items before cap)
# ---------------------------------------------------------------------------

def _count_named_entities(items: List[ScrapedItem]) -> Dict[str, int]:
    """
    Count distinct items mentioning each named entity (case-insensitive substring).
    Operates over the full deduplicated item list before the ENRICHMENT_CAP.
    Returns only entities with count > 0.
    """
    counts: Dict[str, int] = {}
    for entity, aliases in NAMED_ENTITIES.items():
        n = sum(
            1 for item in items
            if any(alias in f"{item.title} {item.summary}".lower() for alias in aliases)
        )
        if n:
            counts[entity] = n
    return counts


def _count_watchlist_topics(items: List[ScrapedItem]) -> Dict[str, int]:
    """
    Count distinct items matching each watchlist topic keyword list.
    Operates over the full deduplicated item list before the ENRICHMENT_CAP.
    Returns only topics with count > 0.
    """
    counts: Dict[str, int] = {}
    for topic, keywords in WATCHLIST_TOPICS.items():
        n = sum(
            1 for item in items
            if any(kw in f"{item.title} {item.summary}".lower() for kw in keywords)
        )
        if n:
            counts[topic] = n
    return counts


# ---------------------------------------------------------------------------
# Step 9: film cap enforcement
# ---------------------------------------------------------------------------

def _apply_film_cap(items: List[ScrapedItem], cap: int) -> List[ScrapedItem]:
    """
    Walk the ranked list and enforce FILM_MAX_SHARE on generic film items.

    - Non-film items: always admitted freely.
    - Film items with direct Arie relevance (payment angle / watchlist / corridor): admitted freely.
    - Generic film items: admitted only until FILM_MAX_SHARE * cap slots are filled.
    - After the cap quota is exhausted, remaining slots are filled from non-film items
      that didn't make it through initially.

    Returns at most `cap` items.
    """
    max_generic_film = max(1, int(cap * FILM_MAX_SHARE))

    admitted: List[ScrapedItem] = []
    overflow_nonfilm: List[ScrapedItem] = []
    generic_film_count = 0

    for item in items:
        if not _is_film_item(item):
            if len(admitted) < cap:
                admitted.append(item)
            else:
                overflow_nonfilm.append(item)
        elif _film_has_direct_relevance(item):
            # Relevant film: admit freely
            if len(admitted) < cap:
                admitted.append(item)
        else:
            # Generic film: subject to cap
            if generic_film_count < max_generic_film and len(admitted) < cap:
                admitted.append(item)
                generic_film_count += 1
            # else: silently drop (cap reached for generic film)

    # Fill any remaining slots with non-film items that were pushed out
    for item in overflow_nonfilm:
        if len(admitted) >= cap:
            break
        admitted.append(item)

    return admitted[:cap]


# ---------------------------------------------------------------------------
# v4: Dedicated intelligence stream routing helpers
# ---------------------------------------------------------------------------

_MAURITIUS_STREAM_PREFIXES: Tuple[str, ...] = tuple(MAURITIUS_INTEL_SOURCES.keys())
_COMPETITOR_STREAM_PREFIXES: Tuple[str, ...] = tuple(COMPETITOR_INTEL_SOURCES.keys())
_INTRODUCER_STREAM_PREFIXES: Tuple[str, ...] = tuple(INTRODUCER_INTEL_SOURCES.keys())
_ALL_NEW_STREAM_PREFIXES: Tuple[str, ...] = (
    _MAURITIUS_STREAM_PREFIXES + _COMPETITOR_STREAM_PREFIXES + _INTRODUCER_STREAM_PREFIXES
)


def _detect_competitor_name(item: ScrapedItem) -> str:
    """Return the first tracked competitor name found in item title+summary, or ''."""
    text = f"{item.title} {item.summary}".lower()
    for name in COMPETITOR_NAMES:
        if name.lower() in text:
            return name
    return ""


def _dedup_stream_by_url(items: List[ScrapedItem]) -> List[ScrapedItem]:
    """URL-dedup for a stream list (order-preserving)."""
    seen: Set[str] = set()
    result: List[ScrapedItem] = []
    for item in items:
        if item.url not in seen:
            seen.add(item.url)
            result.append(item)
    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def enrich(result: ScrapeResult) -> ScrapeResult:
    """
    Run the full deterministic enrichment pipeline on a ScrapeResult.

    Returns a new ScrapeResult with enriched items (sorted by commercial_score DESC,
    relevance_score as tie-break, capped at ENRICHMENT_CAP with film cap applied).
    source_counts and source_failures are preserved from the raw result.

    Pipeline stats set on the returned ScrapeResult:
      total_collected   — carried forward from scraper (raw pre-URL-dedup count)
      total_after_dedup — post-URL-dedup (from scraper) + post-title-dedup (from this step)
      total_selected    — len(capped), i.e. items after ENRICHMENT_CAP
      selected_source_counts — per-source counts of the selected items
    """
    enriched_items: List[ScrapedItem] = []

    noise_urls: Set[str] = set()  # track which item URLs are operational noise

    for raw_item in result.items:
        item = raw_item.model_copy()

        text = f"{item.title} {item.summary}"

        # 1. Classify topic + subtopic
        item.topic, item.subtopic = _classify_topic(text)

        # 2. Tag region
        item.region = _tag_region(text)

        # 3. Source weight
        item.source_weight = _get_source_weight(item.source)

        # 4. Watchlist hits
        item.watchlist_hits = _detect_watchlist_hits(text)

        # 4b. Direct-mention detection (Arie Finance / ACBM own entities)
        matched = _detect_direct_mentions(text)
        item.direct_mention = bool(matched)
        item.direct_mention_entities = matched

        # 5. Relevance score (general)
        item.relevance_score = _compute_relevance_score(item)

        # 5b. Demote operational/administrative noise (tender notices, auctions, etc.)
        if _is_operational_noise(item.title):
            item.relevance_score *= OPERATIONAL_NOISE_MULTIPLIER
            noise_urls.add(item.url)

        enriched_items.append(item)

    # 6. Near-duplicate dedup (cross-source title similarity) — also merges supporting_source_count
    enriched_items = _dedup(enriched_items)

    # Filter items older than _STALE_CUTOFF_DAYS. Items with no published_at pass through.
    enriched_items = [i for i in enriched_items if not _is_stale(i)]

    # 6b. Soft cluster-dedup: merge multi-publisher coverage of the same event.
    # Catches near-identical headlines that use different phrasing (e.g. FATF India × 4 sources).
    enriched_items = _soft_dedup(enriched_items)

    # Hard-drop procurement/tender notices — these are never market intelligence.
    # Applied before stream routing so they are excluded from all sections.
    def _is_procurement_noise(item: ScrapedItem) -> bool:
        text = f"{item.title} {item.summary}".lower()
        return any(pat in text for pat in PROCUREMENT_HARD_DROP_PATTERNS)

    enriched_items = [i for i in enriched_items if not _is_procurement_noise(i)]

    # After all dedup passes + stale filter
    total_after_dedup = len(enriched_items)

    # v4: Route dedicated intelligence stream items out of the main commercial pool.
    # Items from new stream sources are enriched (region/watchlist/relevance) but must
    # NOT compete with the main top-40 cap — they feed separate digest sections.
    mauritius_items = [
        i for i in enriched_items
        if any(i.source.startswith(p) for p in _MAURITIUS_STREAM_PREFIXES)
    ]
    competitor_items = [
        i for i in enriched_items
        if any(i.source.startswith(p) for p in _COMPETITOR_STREAM_PREFIXES)
    ]
    introducer_items = [
        i for i in enriched_items
        if any(i.source.startswith(p) for p in _INTRODUCER_STREAM_PREFIXES)
    ]
    # Remove them from the main pool before commercial scoring and ranking.
    enriched_items = [
        i for i in enriched_items
        if not any(i.source.startswith(p) for p in _ALL_NEW_STREAM_PREFIXES)
    ]

    # Filter competitor items to those that actually mention a tracked competitor.
    competitor_items = [
        i for i in competitor_items
        if any(name.lower() in f"{i.title} {i.summary}".lower() for name in COMPETITOR_NAMES)
    ]

    # Two-tier introducer filter:
    # Pass if (a) any service keyword appears in title+summary, OR
    #         (b) a jurisdiction keyword AND an event keyword both appear.
    # GNews snippets are short — keywords may be in the article but not the 300-char summary,
    # so the jurisdiction+event path catches relevant items the service-keyword path misses.
    def _passes_introducer_filter(item: ScrapedItem) -> bool:
        text = f"{item.title} {item.summary}".lower()
        if any(kw in text for kw in INTRODUCER_SERVICE_KEYWORDS):
            return True
        has_jurisdiction = any(kw in text for kw in INTRODUCER_JURISDICTION_KEYWORDS)
        has_event = any(kw in text for kw in INTRODUCER_EVENT_KEYWORDS)
        return has_jurisdiction and has_event

    introducer_items = [i for i in introducer_items if _passes_introducer_filter(i)]

    # Drop generic jurisdiction guides from the Mauritius stream unless the article
    # explicitly mentions Mauritius (e.g. ICLG BVI guide, Cayman country handbook).
    def _is_mauritius_guide_noise(item: ScrapedItem) -> bool:
        text = f"{item.title} {item.summary}".lower()
        if not any(pat in text for pat in MAURITIUS_GUIDE_EXCLUSION_PATTERNS):
            return False
        return "mauritius" not in text

    mauritius_items = [i for i in mauritius_items if not _is_mauritius_guide_noise(i)]

    # Sort each stream by relevance_score DESC, then dedup by URL, then cap.
    mauritius_items.sort(key=lambda x: x.relevance_score, reverse=True)
    competitor_items.sort(key=lambda x: x.relevance_score, reverse=True)
    introducer_items.sort(key=lambda x: x.relevance_score, reverse=True)

    mauritius_items = _dedup_stream_by_url(mauritius_items)[:MAURITIUS_INTEL_CAP]
    competitor_items = _dedup_stream_by_url(competitor_items)[:COMPETITOR_INTEL_CAP]
    introducer_items = _dedup_stream_by_url(introducer_items)[:INTRODUCER_INTEL_CAP]

    logger.info(
        "v4 intel streams: %d mauritius | %d competitor | %d introducer",
        len(mauritius_items), len(competitor_items), len(introducer_items),
    )

    # 5c. Commercial score — computed after dedup so supporting_source_count is final.
    # Also apply noise demotion to commercial_score (noise items match treasury/mauritius
    # keywords and would otherwise rank at the top despite being admin bulletins).
    for item in enriched_items:
        item.commercial_score = _compute_commercial_score(item)
        if item.url in noise_urls:
            item.commercial_score *= OPERATIONAL_NOISE_MULTIPLIER

    # Film demotion: generic film items (no payment angle, no watchlist, no corridor)
    # get their commercial_score AND relevance_score multiplied by 0.4.
    for item in enriched_items:
        if _is_film_item(item) and not _film_has_direct_relevance(item):
            item.commercial_score *= 0.4
            item.relevance_score *= 0.4

    # Community stream — built from FULL set BEFORE the commercial cap so that
    # Reddit / discourse items with low commercial_score are never excluded.
    # Ranked by community_score (engagement + pain-point signals), top _COMMUNITY_CAP kept.
    community_candidates = [i for i in enriched_items if _is_community_source(i.source)]
    for item in community_candidates:
        item._community_score_val = _community_score(item)  # type: ignore[attr-defined]
    community_candidates.sort(
        key=lambda x: getattr(x, "_community_score_val", 0.0),
        reverse=True,
    )
    community_items = community_candidates[:_COMMUNITY_CAP]

    # Customer Pain Intelligence stream — separate from commercial ranking.
    # Pulls from pain_items_raw (new sources) + qualifying Reddit/HN items.
    pain_items = enrich_pain_items(result.pain_items_raw, enriched_items)
    logger.info(
        "Pain stream: %d items (from %d raw + community candidates)",
        len(pain_items), len(result.pain_items_raw),
    )

    # v3: count named entity and watchlist topic mentions across the full deduplicated
    # item set BEFORE the cap — so counts reflect the full picture, not just top-40.
    entity_counts = _count_named_entities(enriched_items)
    watchlist_topic_counts = _count_watchlist_topics(enriched_items)
    logger.info(
        "Entity counts: %d entities with mentions | Watchlist topics: %d topics with mentions",
        len(entity_counts), len(watchlist_topic_counts),
    )

    # 7 & 8. Sort by commercial_score DESC, relevance_score as tie-break
    enriched_items.sort(
        key=lambda x: (x.commercial_score, x.relevance_score),
        reverse=True,
    )

    # 9. Enforce film cap (generic film items ≤ FILM_MAX_SHARE of CAP)
    capped = _apply_film_cap(enriched_items, ENRICHMENT_CAP)

    # --- Pipeline stats ---
    # Collapse Reddit sub-sources to "Reddit" prefix to match source_counts style
    from collections import Counter
    selected_counter: Counter = Counter()
    for item in capped:
        # source may be "Reddit/r/fintech" — normalise to "Reddit"
        key = item.source.split("/")[0] if item.source.startswith("Reddit/") else item.source
        selected_counter[key] += 1

    return ScrapeResult(
        items=capped,
        source_counts=result.source_counts,  # original post-URL-dedup per-source counts preserved
        source_failures=result.source_failures,
        low_data_warning=result.low_data_warning,
        # Stats from scraper (total_collected set there; total_after_dedup = post-URL)
        total_collected=result.total_collected,
        # After both URL-dedup (scraper) AND title-dedup (this step)
        total_after_dedup=total_after_dedup,
        total_selected=len(capped),
        selected_source_counts=dict(selected_counter),
        # Community stream — independent of commercial top-40 cap
        community_items=community_items,
        # Customer Pain Intelligence stream — entirely separate scoring axis
        pain_items_raw=result.pain_items_raw,
        pain_items=pain_items,
        # v3 signal intelligence — counts over full pre-cap item set
        entity_counts=entity_counts,
        watchlist_topic_counts=watchlist_topic_counts,
        # v4 dedicated intelligence streams
        mauritius_items=mauritius_items,
        competitor_items=competitor_items,
        introducer_items=introducer_items,
    )
