"""
Pydantic models for Arie Finance Market Intelligence (v2/v3/phase1).
"""
from __future__ import annotations
from typing import Dict, List, Literal, Optional
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Scraper output
# ---------------------------------------------------------------------------

class ScrapedItem(BaseModel):
    source: str
    title: str
    url: str
    summary: str                          # selftext / description, max 300 chars
    score: Optional[int] = None           # Reddit only
    num_comments: Optional[int] = None    # Reddit only

    # --- enrichment fields ---
    published_at: Optional[str] = None    # ISO date parsed from feed entry
    region: Optional[str] = None          # africa | india | mena | mauritius | eu | uk | us | asia | global
    topic: Optional[str] = None           # taxonomy stream key (see config.py)
    subtopic: Optional[str] = None        # market_opportunities children only
    source_weight: float = 0.5            # per-source reliability 0..1
    relevance_score: float = 0.0          # computed by enrich.py (general relevance)
    commercial_score: float = 0.0         # computed by enrich.py (Arie BD signal strength)
    supporting_source_count: int = 1      # number of sources that covered this story (dedup merges)
    watchlist_hits: List[str] = Field(default_factory=list)  # tracked entities matched
    # --- direct-mention fields (populated by enrich.py) ---
    direct_mention: bool = False          # True if item mentions Arie Finance / ACBM
    direct_mention_entities: List[str] = Field(default_factory=list)  # which entities were matched


class ScrapeResult(BaseModel):
    items: List[ScrapedItem]
    source_counts: Dict[str, int]         # {source_name: count} — post-URL-dedup, per source group
    source_failures: List[str]            # sources that returned 0 items
    low_data_warning: bool                # True if total items < 15
    # --- pipeline stats (populated by scraper.py + enrich.py) ---
    total_collected: int = 0              # raw items fetched across all sources BEFORE any dedup
    total_after_dedup: int = 0            # after URL-dedup (scraper) + near-dup title dedup (enrich)
    total_selected: int = 0              # items selected into the digest (after ENRICHMENT_CAP)
    selected_source_counts: Dict[str, int] = Field(default_factory=dict)  # per-source counts of SELECTED items
    # --- community stream (populated by enrich.py, independent of commercial top-40 cap) ---
    community_items: List[ScrapedItem] = Field(default_factory=list)  # top ~8 community/discourse items
    # --- Customer Pain Intelligence stream (separate from all commercial scoring) ---
    pain_items_raw: List[ScrapedItem] = Field(default_factory=list)   # raw items from pain scrapers
    pain_items: List[ScrapedItem] = Field(default_factory=list)       # after enrich: filtered, scored, capped
    # --- v3 signal intelligence (populated by enrich.py, over all items before cap) ---
    entity_counts: Dict[str, int] = Field(default_factory=dict)           # named entity → mention count
    watchlist_topic_counts: Dict[str, int] = Field(default_factory=dict)  # topic → mention count
    # --- v4 dedicated intelligence streams (routed by source prefix in enrich.py) ---
    mauritius_items: List[ScrapedItem] = Field(default_factory=list)
    competitor_items: List[ScrapedItem] = Field(default_factory=list)
    introducer_items: List[ScrapedItem] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Content idea (used by content.py / /posts endpoint — replaces LinkedInPost)
# ---------------------------------------------------------------------------

class ContentIdea(BaseModel):
    angle: str           # hook / angle, one or two sentences — NOT a finished post
    based_on: str        # the opportunity/insight it draws from
    format: str          # e.g. "LinkedIn post", "Article", "Introducer talking point"
    source_title: str    # exact title from digest input
    source_url: str      # exact URL from digest input (mandatory — no fabrication)
    audience: str = ""   # e.g. "Introducers", "SME founders", "Both"


class CustomerPainItem(BaseModel):
    """A real user/business complaint about a payment platform — Customer Pain Intelligence stream."""
    pain_point: str                                      # what the user is experiencing (title/headline)
    platform_mentioned: str = ""                         # Wise, Revolut, Airwallex, etc.
    user_type: str = ""                                  # SME, freelancer, marketplace, exporter, agency, ecommerce
    geography: str = ""                                  # region / corridor if detectable
    evidence_excerpt: str = ""                           # short quote or summary from the original source
    source_url: str                                      # mandatory — always a real URL
    source: str = ""                                     # source name (e.g. "HackerNews", "Reddit/r/fintech")
    arie_relevance: str = ""                             # one specific sentence on ARIE BD relevance
    published_at: Optional[str] = None                  # ISO date from source
    # --- Phase 2: deterministic classification fields (set by classify_pain_item in enrich.py) ---
    pain_type: str = ""                                  # frozen_account | delayed_onboarding | kyc_friction | transfer_delay | payment_rejection | poor_support | fee_pricing | compliance_derisking | account_closure | other
    source_type: str = ""                                # real_user_complaint | forum_discussion | review | news_about_complaint | regulatory_notice | unknown
    severity: str = ""                                   # high | medium | low
    recommended_internal_action: str = ""
    compliance_caution: str = ""                         # non-empty only when relevant
    confidence: str = ""                                 # high | medium | low
    source_quality: str = ""                             # high | medium | low


# ---------------------------------------------------------------------------
# v3 signal intelligence models
# ---------------------------------------------------------------------------

class InterestingFind(BaseModel):
    """An unusual or commercially noteworthy item selected by Claude across all categories."""
    title: str
    source: str
    source_url: str
    why_it_stands_out: str               # one sentence — why management should notice this
    category_hint: str = ""              # e.g. "New FSC entrant", "Wise complaint surge"
    region: Optional[str] = None
    published_at: Optional[str] = None


class WatchlistTopic(BaseModel):
    """A tracked topic with week-over-week mention count movement."""
    topic: str                           # display name, e.g. "Stablecoins"
    count: int                           # mentions this week
    prev_count: int = 0                  # mentions last week (0 = no prior data)
    direction: str = ""                  # "increasing" | "stable" | "declining" | "new"


class EntityActivity(BaseModel):
    """A named entity with week-over-week mention count movement."""
    entity: str                          # e.g. "Wise"
    count: int
    prev_count: int = 0
    direction: str = ""                  # "increasing" | "stable" | "declining" | "new"


# ---------------------------------------------------------------------------
# v4 Dedicated intelligence stream models
# ---------------------------------------------------------------------------

class MauritiusIntelItem(BaseModel):
    """A Mauritius government, regulatory, or investment development relevant to ARIE."""
    title: str
    source: str
    source_url: str
    what_happened: str = ""       # factual one-liner
    why_it_matters: str = ""      # ARIE relevance
    category: str = ""            # "EDB" | "Budget" | "FSC" | "BOM" | "Tax" | "Investment" | "Regulation"
    published_at: Optional[str] = None


class CompetitorIntelItem(BaseModel):
    """A product, pricing, expansion, or complaint signal from a tracked competitor."""
    competitor: str = ""          # Wise, Revolut, Airwallex, etc.
    title: str
    source: str
    source_url: str
    what_happened: str = ""
    why_it_matters: str = ""
    signal_type: str = ""         # "product" | "pricing" | "expansion" | "complaint" | "regulatory" | "outage"
    published_at: Optional[str] = None


class IntroducerIntelItem(BaseModel):
    """An acquisition, expansion, licence, or service event for a CSP/fiduciary/management company."""
    entity: str = ""              # CSP / fiduciary name if detectable
    title: str
    source: str
    source_url: str
    what_happened: str = ""
    why_it_matters: str = ""      # impact on ARIE's distribution network
    signal_type: str = ""         # "acquisition" | "expansion" | "licence" | "new_service" | "partnership"
    published_at: Optional[str] = None


# ---------------------------------------------------------------------------
# Phase 1 — Source Health model
# ---------------------------------------------------------------------------

class SourceHealthItem(BaseModel):
    source_name: str
    category: str = ""              # e.g. "Community", "News", "Regulatory", "Customer Pain"
    status: Literal["active", "partial", "failed", "not_configured"] = "active"
    items_collected: int = 0
    error_summary: str = ""
    last_checked: str = ""          # ISO timestamp, passed in from scrape time


# ---------------------------------------------------------------------------
# Phase 1 — Brand Watch + Trajectory models
# ---------------------------------------------------------------------------

class BrandWatchResult(BaseModel):
    coverage: List[str] = Field(default_factory=list)
    not_covered: List[str] = Field(default_factory=list)
    coverage_confidence: Literal["High", "Medium", "Limited"] = "Limited"
    result: str = ""
    last_mention_date: Optional[str] = None
    sentiment: str = "Neutral"
    status: str = ""
    items: List["DigestItem"] = Field(default_factory=list)


class ThemeTrajectory(BaseModel):
    theme: str
    counts_by_period: Dict[str, int] = Field(default_factory=dict)
    direction: Literal["up", "down", "flat"] = "flat"
    confidence: Literal["High", "Medium", "Low"] = "Medium"
    implication: str = ""


# ---------------------------------------------------------------------------
# Prospect leads model
# ---------------------------------------------------------------------------

class ProspectLead(BaseModel):
    entity: str                      # the named company / CSP / introducer to pursue
    why_now: str = ""                # the specific trigger + why Arie fits (1-2 sentences)
    contact_approach: str = ""       # concrete realistic first move
    owner: str = "BD"                # BD | Management | Compliance | Ops
    confidence: str = "Medium"       # High | Medium | Low
    source: str = ""
    source_url: str = ""


# ---------------------------------------------------------------------------
# Digest models
# ---------------------------------------------------------------------------

class OpportunityRadarItem(BaseModel):
    opportunity: str                                    # signal title / headline opportunity
    confidence: str                                     # High / Medium / Low
    suggested_owner: str = ""                           # BD | Ops | Compliance | Management | No action required
    why_it_matters: str                                 # insight + Arie impact
    suggested_action: str                               # concrete BD action
    region: Optional[str] = None
    subtopic: Optional[str] = None
    entities: List[str] = Field(default_factory=list)  # named cos/people/regulators
    source_title: str
    source_url: str
    supporting_source_count: int = 1                    # how many sources covered the story
    # --- v2 additions for "Top Signals" card ---
    category: str = ""                                  # display category, e.g. "Commercial Opportunity"
    commercial_score: float = 0.0                       # enrichment commercial_score for ranking context
    what_happened: str = ""                             # factual one/two-liner of the event
    # --- v2.1 additions ---
    published_at: Optional[str] = None                  # ISO date from source feed
    source: str = ""                                    # source name (e.g. "Finextra")


class DigestItem(BaseModel):
    title: str
    url: str
    source: str
    one_liner: str                                      # why it matters, <=1 sentence
    topic: Optional[str] = None
    region: Optional[str] = None
    trend_flag: str = ""                                # "new"|"recurring"|"accelerating"|"fading"|""
    entities: List[str] = Field(default_factory=list)
    supporting_source_count: int = 1                    # how many sources covered the story
    published_at: Optional[str] = None                  # ISO date from source feed
    # --- Phase 1 additions ---
    implication: str = ""
    suggested_action: str = ""
    suggested_owner: str = ""                           # BD | Ops | Compliance | Management | No action required
    confidence: Literal["High", "Medium", "Low"] = "Medium"
    source_corroboration: Literal["single", "multi"] = "single"


# ---------------------------------------------------------------------------
# v2 digest section models
# ---------------------------------------------------------------------------

class DirectMention(BaseModel):
    """An item that directly mentions Arie Finance or ACBM."""
    title: str
    source: str
    source_url: str
    entity: str                                         # which DIRECT_MENTION_ENTITIES matched
    what_happened: str = ""                             # brief factual description
    published_at: Optional[str] = None                  # ISO date from source feed


class SocialPainPoint(BaseModel):
    """A pain point surfaced from social/community sources (e.g. Reddit) for content angles."""
    pain_point: str
    source: str
    source_url: str
    content_angle: str                                  # suggested LinkedIn/content angle
    compliance_caution: str = ""                        # any compliance caveat to note
    published_at: Optional[str] = None                  # ISO date from source feed


class FilmWatchItem(BaseModel):
    """A film/production finance item relevant to Arie's film vertical."""
    title: str
    source: str
    source_url: str
    relevance: str = ""                                 # payment/treasury/rebate-doc relevance
    region: Optional[str] = None
    published_at: Optional[str] = None                  # ISO date from source feed


class Digest(BaseModel):
    generated_at: str                                   # UTC ISO timestamp
    executive_summary: List[str]                        # 3-5 bullets

    # --- v2 explicit sections (the new data contract) ---
    top_signals: List[OpportunityRadarItem] = Field(default_factory=list)       # "Top Signals for Management"
    direct_mentions: List[DirectMention] = Field(default_factory=list)           # always rendered (empty → fallback)
    customer_pain: List[CustomerPainItem] = Field(default_factory=list)          # Customer Pain Intelligence (before Market Signals)
    commercial_opportunities: List[DigestItem] = Field(default_factory=list)
    social_pain_points: List[SocialPainPoint] = Field(default_factory=list)
    film_watch: List[FilmWatchItem] = Field(default_factory=list)
    regulatory_watch: List[DigestItem] = Field(default_factory=list)

    # --- v3 signal intelligence sections ---
    interesting_finds: List[InterestingFind] = Field(default_factory=list)      # 3-5 unusual/noteworthy items
    watchlist_topics: List[WatchlistTopic] = Field(default_factory=list)        # topic trend tracking
    entity_activity: List[EntityActivity] = Field(default_factory=list)         # named entity mention counts

    # --- v4 dedicated intelligence streams ---
    mauritius_intel: List[MauritiusIntelItem] = Field(default_factory=list)
    competitor_intel: List[CompetitorIntelItem] = Field(default_factory=list)
    introducer_intel: List[IntroducerIntelItem] = Field(default_factory=list)

    # --- Phase 1 additions ---
    brand_watch: BrandWatchResult = Field(default_factory=BrandWatchResult)
    trajectories: List[ThemeTrajectory] = Field(default_factory=list)
    source_health: List[SourceHealthItem] = Field(default_factory=list)

    # --- Prospects This Week ---
    prospects: List[ProspectLead] = Field(default_factory=list)

    scrape_meta: Optional[ScrapeResult] = None
