"""
Taxonomy, watchlist, source weights, and region keywords for Arie Finance Market Intelligence.

All values are plain Python dicts/lists — easy to edit without touching any logic code.
"""
from __future__ import annotations
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# ARIE / ACBM direct-mention entities (the client's OWN names).
# Matched case-insensitively against item title+summary.
# Used to flag items that mention Arie Finance or ACBM directly — distinct
# from the competitor WATCHLIST.
#
# Matching rules (applied in enrich.py):
#   - "arie finance" / "arie capital" / "acbm" matched as phrases with
#     word-ish boundary guards so bare "arie" in unrelated text doesn't fire.
#   - "ariefinance" matched as a substring (URL/handle form).
# Edit this list to add aliases, trading names, or abbreviations.
# ---------------------------------------------------------------------------

DIRECT_MENTION_ENTITIES: List[str] = [
    "Arie Finance",          # primary brand
    "Arie Finance Ltd",      # legal name
    "ariefinance",           # URL / handle form
    "Arie Capital",          # investment entity
    "Arie Capital Investment",  # full investment entity name
    "ACBM",                  # internal abbreviation
]

# ---------------------------------------------------------------------------
# Two-level taxonomy: topic -> keywords (lowercase substring match on title+summary)
# market_opportunities has subtopics (subtopic -> keywords).
# ---------------------------------------------------------------------------

# Core topics: keyword lists used for classification
TOPIC_KEYWORDS: Dict[str, List[str]] = {
    "payments_treasury": [
        "payment", "treasury", "cash management", "liquidity", "swift", "sepa",
        "real-time payment", "instant payment", "open banking", "bank transfer",
        "virtual account", "fx", "foreign exchange", "remittance", "money transfer",
        "wallet", "acquiring", "merchant", "pos", "card network", "interchange",
        "embedded finance", "banking as a service", "baas", "neobank", "challenger bank",
    ],
    "regulation_compliance": [
        "regulation", "compliance", "aml", "kyc", "fatf", "fca", "sec", "mifid",
        "mica", "amla", "anti-money laundering", "sanctions", "license", "licensing",
        "regulatory", "regulator", "enforcement", "directive", "legislation", "law",
        "policy", "fsc", "bank of mauritius", "prudential", "capital requirement",
        "gdpr", "data protection", "fraud", "financial crime", "watchdog",
    ],
    "cross_border_corridors": [
        "cross-border", "cross border", "corridor", "remittance", "international transfer",
        "africa", "india", "mena", "middle east", "mauritius", "kenya", "nigeria",
        "ghana", "egypt", "uae", "dubai", "singapore", "southeast asia", "latam",
        "emerging market", "diaspora", "forex", "fx corridor", "payment corridor",
    ],
    "fintech_infrastructure": [
        "api", "infrastructure", "platform", "developer", "sdk", "integration",
        "cloud", "saas", "microservice", "core banking", "ledger", "orchestration",
        "processor", "gateway", "switch", "iso 20022", "open finance", "data",
        "interoperability", "connectivity", "network", "rails", "protocol",
    ],
}

# market_opportunities: subtopic -> keywords
MARKET_OPPORTUNITIES_SUBTOPICS: Dict[str, List[str]] = {
    "film_production": [
        "film production", "film fund", "film rebate", "film incentive", "film commission",
        "co-production", "coproduction", "feature film", "motion picture",
        "production studio", "film studio", "screen daily", "streaming content production",
        "content production", "series production", "production finance",
        "tax rebate film", "tax incentive film", "location shooting",
        "hollywood", "bollywood", "nollywood", "cinema production",
    ],
    "global_expansion": [
        "expansion", "launch", "new market", "entry", "enter", "international expansion",
        "cross-border expansion", "overseas", "global rollout", "series b", "series c",
        "growth round", "scale", "scaling", "geographic", "footprint",
    ],
    "mauritius_investment": [
        "mauritius", "mauritian", "mur", "port louis", "fsc mauritius", "bank of mauritius",
        "ebene", "mauritius ias", "gbc", "global business", "africa hub",
    ],
    "treasury_pain_points": [
        "treasury pain", "working capital", "cash flow", "liquidity management",
        "hedging", "fx risk", "trapped cash", "repatriation", "payment friction",
        "treasury transformation", "cfo", "finance team", "accounts payable",
        "accounts receivable", "supply chain finance",
    ],
    "new_market_entrants": [
        "launch", "startup", "founded", "seed round", "pre-seed", "series a",
        "new player", "entrant", "disruptor", "challenger", "spinoff", "spin-out",
        "new product", "product launch", "announced today", "unveiled",
    ],
}

# All market_opportunities keywords flattened (for top-level classification)
_MARKET_OPP_ALL_KEYWORDS = [kw for kws in MARKET_OPPORTUNITIES_SUBTOPICS.values() for kw in kws]
TOPIC_KEYWORDS["market_opportunities"] = _MARKET_OPP_ALL_KEYWORDS

# Topic priority multipliers (higher = more relevant to Arie Finance)
TOPIC_PRIORITY: Dict[str, float] = {
    "market_opportunities": 1.1,      # subtopic-specific boosts applied on top; reduced from 1.4
    "regulation_compliance": 1.3,
    "cross_border_corridors": 1.3,
    "payments_treasury": 1.1,
    "fintech_infrastructure": 0.9,
}

# Subtopic priority boosts (added on top of topic priority)
SUBTOPIC_PRIORITY_BOOST: Dict[str, float] = {
    "mauritius_investment": 0.3,
    "film_production": 0.1,           # reduced from 0.3 — generic film items deprioritised
    "treasury_pain_points": 0.2,
    "global_expansion": 0.1,
    "new_market_entrants": 0.1,
}

# ---------------------------------------------------------------------------
# Film rebalance: cap and direct-relevance signal
# ---------------------------------------------------------------------------

# Maximum share of the final ranked CAP that film items may occupy.
# Film items with a direct Arie payment/treasury angle bypass this cap.
FILM_MAX_SHARE: float = 0.25

# Keywords indicating direct Arie Finance relevance within a film item.
# A film item matching at least one of these bypasses FILM_MAX_SHARE.
FILM_PAYMENT_ANGLE_KEYWORDS: List[str] = [
    "payment",
    "treasury",
    "fx",
    "foreign exchange",
    "onboarding",
    "kyc",
    "vendor",
    "supplier payment",
    "payroll",
    "repatriation",
    "rebate documentation",
    "rebate processing",
    "production finance",
    "production accounting",
    "cash flow",
    "escrow",
    "settlement",
    "multi-currency",
    "multicurrency",
    "exchange rate",
    "bank account",
    "accounts payable",
    "wire transfer",
    "remittance",
]

# ---------------------------------------------------------------------------
# Watchlist: entity -> category (substring match, case-insensitive)
# ---------------------------------------------------------------------------

WATCHLIST: Dict[str, str] = {
    # Payments — incumbents + Arie competitors (also key pain intelligence targets)
    "Wise":               "payments",
    "Airwallex":          "payments",
    "Rapyd":              "payments",
    "Nium":               "payments",
    "Stripe":             "payments",
    "Adyen":              "payments",
    "Revolut":            "payments",
    "Payoneer":           "payments",
    "Mercury":            "payments",
    "WorldFirst":         "payments",
    "OFX":                "payments",
    "Monzo":              "payments",
    "Brex":               "payments",
    "PayPal":             "payments",
    "TransferMate":       "payments",
    "Currencies Direct":  "payments",
    # Stablecoins / crypto
    "Circle":        "stablecoin",
    "Ripple":        "stablecoin",
    "Tether":        "stablecoin",
    "Coinbase":      "stablecoin",
    # Film
    "Netflix":       "film",
    "Amazon MGM":    "film",
    "Warner Bros":   "film",
    "Disney":        "film",
    "Paramount":     "film",
    "Sony Pictures": "film",
}

# ---------------------------------------------------------------------------
# Region keywords: region -> keyword list (case-insensitive substring match)
# Regions: africa | india | mena | mauritius | eu | uk | us | asia | global
# ---------------------------------------------------------------------------

REGION_KEYWORDS: Dict[str, List[str]] = {
    "mauritius": [
        "mauritius", "mauritian", "port louis", "fsc mauritius", "bank of mauritius",
        "ebene", "mur", "mauritius ias",
    ],
    "africa": [
        "africa", "african", "nigeria", "kenya", "ghana", "ethiopia", "tanzania",
        "south africa", "egypt", "rwanda", "senegal", "ivory coast", "francophone africa",
        "sub-saharan", "east africa", "west africa",
    ],
    "india": [
        "india", "indian", "upi", "rupee", "rbi", "reserve bank of india",
        "mumbai", "bangalore", "new delhi", "fintech india",
    ],
    "mena": [
        "mena", "middle east", "north africa", "uae", "dubai", "saudi arabia",
        "bahrain", "qatar", "kuwait", "oman", "riyadh", "abu dhabi",
    ],
    "asia": [
        "asia", "asian", "singapore", "hong kong", "malaysia", "indonesia",
        "thailand", "vietnam", "philippines", "southeast asia", "apac",
    ],
    "eu": [
        "europe", "european", "eu ", "mica", "amla", "ecb", "psd2", "psd3",
        "sepa", "eurozone", "france", "germany", "netherlands", "spain", "italy",
        "luxembourg", "ireland", "malta",
    ],
    "uk": [
        "uk", "united kingdom", "britain", "british", "fca", "bank of england",
        "london", "england", "scotland", "fintech uk",
    ],
    "us": [
        "united states", " usa", "u.s.", "federal reserve", "sec ", "occ ",
        "fdic", "cfpb", "new york", "silicon valley", "wall street",
    ],
    "global": [],  # fallback, no keywords needed
}

# Region relevance boost (added to relevance score)
REGION_BOOST: Dict[str, float] = {
    "mauritius": 0.35,
    "africa":    0.25,
    "india":     0.20,
    "mena":      0.20,
    "asia":      0.15,
    "uk":        0.10,
    "eu":        0.10,
    "us":        0.05,
    "global":    0.0,
}

# ---------------------------------------------------------------------------
# Source weights: source name prefix -> reliability float 0..1
# Native publisher RSS > Google News aggregator > Reddit
# ---------------------------------------------------------------------------

SOURCE_WEIGHTS: Dict[str, float] = {
    # Native publisher RSS
    "Bank of Mauritius":     0.90,
    "FCA":                   0.85,
    "FATF Publications":     0.85,
    "ECB":                   0.85,      # ECB Press and ECB Publications
    "Finextra":              0.80,
    "CoinDesk":              0.80,
    "The Block":             0.75,
    "PYMNTS":                0.75,
    "FinTech Global":        0.75,
    "The Paypers":           0.70,
    # Google News aggregator (topic-specific feeds)
    "GNews/Funding":         0.55,
    "GNews/FATF":            0.55,
    "GNews/Stablecoins":     0.55,
    "GNews/MiCA":            0.55,
    "GNews/AMLA":            0.55,
    "GNews/FSC_Mauritius":   0.60,
    "GNews/AML_Fraud":       0.55,
    "GNews/Corridors":       0.55,
    "GNews/Film_Incentives": 0.55,
    "GNews/Mauritius_Film":  0.60,
    "GNews/Screen_Daily":    0.55,
    "GNews/IMF":             0.55,      # IMF via Google News (native feed blocked from DC IPs)
    "GNews/WorldBank":       0.55,      # World Bank via Google News (native feed blocked)
    # v4 Mauritius Intelligence stream
    "GNews/EDB_Mauritius":        0.65,
    "GNews/Mauritius_Finance":    0.65,
    "GNews/Mauritius_Regulation": 0.60,
    # v4 Competitor Intelligence stream
    "GNews/Wise_Intel":           0.55,
    "GNews/Revolut_Intel":        0.55,
    "GNews/Competitor_Intel":     0.55,
    # v4 Introducer Intelligence stream
    "GNews/CSP_Mauritius":        0.60,
    "GNews/CSP_Offshore":         0.55,
    "GNews/CSP_Acquisition":      0.55,
    "GNews/CSP_UAE":              0.55,
    # Reddit
    "Reddit":                0.40,
}

# ---------------------------------------------------------------------------
# Operational-noise demotion patterns (case-insensitive substring/regex match).
# Items whose titles match any pattern are multiplied by OPERATIONAL_NOISE_MULTIPLIER
# BEFORE the sort+cap — they remain visible for transparency but fall below signal.
# Add patterns here to cover any new classes of admin/operational noise.
# ---------------------------------------------------------------------------

# Hard-drop patterns for procurement/tender notices.
# Applied as an absolute filter regardless of source weight — these are never intelligence.
PROCUREMENT_HARD_DROP_PATTERNS: List[str] = [
    "expression of interest",
    "registration of potential suppliers",
    "registration of suppliers",
    "supplier registration",
    "vendor registration",
    "invitation to bid",
    "bid notice",
    "request for quotation",
    "request for tender",
    "call for bids",
    "call for tenders",
    "call for proposals",
    "prequalification",
]

# Patterns that flag an article as a generic jurisdiction guide.
# Used in the Mauritius stream filter: drop unless "mauritius" appears in title+summary.
MAURITIUS_GUIDE_EXCLUSION_PATTERNS: List[str] = [
    "laws and regulations 2024",
    "laws and regulations 2025",
    "laws and regulations 2026",
    "fintech laws and regulations",
    "country guide",
    "iclg",
    "jurisdiction guide",
    "regulatory guide",
]

OPERATIONAL_NOISE_PATTERNS: List[str] = [
    # Tender / auction / procurement notices
    r"notice of tender",
    r"\btender\b",
    r"\bauction\b",
    r"advance notice",
    r"issue of",
    r"prospectus",
    r"request for proposal",
    # Treasury instruments
    r"treasury bill",
    r"treasury certificate",
    # Dissemination / statistical releases
    r"dissemination of",
    r"exchange rate indices",
    # Administrative cadence items — use plain substring (no \b after colon)
    r"\bweekly\b",
    r"results:",          # "Results: Bank of Mauritius Bills", "Results: Treasury Bills"
    r"\brepo rate\b",
    # BOM operational bulletins
    r"open market operations",
    r"secondary market transactions",
    r"gross official international reserves",
    r"gross tourism earnings",
    r"central bank survey",
    r"intervention on the domestic",
]

# Score multiplier applied to operational-noise items (demote but don't drop)
OPERATIONAL_NOISE_MULTIPLIER: float = 0.25

# Watchlist hit boost per matched entity
WATCHLIST_HIT_BOOST: float = 0.08

# Cap for items passed to Claude
ENRICHMENT_CAP: int = 40

# Dedup similarity threshold (Jaccard on word tokens)
DEDUP_THRESHOLD: float = 0.70

# ---------------------------------------------------------------------------
# Commercial opportunity signals for Arie Finance
#
# Each entry: signal_name -> (keyword_list, weight)
# The commercial_score is the weighted sum of matched signals (each counted once)
# plus supporting_source bonus and region/source quality boosts.
# Higher weight = stronger commercial signal for Arie Finance's BD pipeline.
# ---------------------------------------------------------------------------

COMMERCIAL_SIGNALS: Dict[str, Tuple[List[str], float]] = {
    # Introducers / referral ecosystem — Arie's primary distribution channel
    "introducers_csp": ([
        "introducer", "csp", "corporate service provider", "management company",
        "trust company", "fiduciary", "family office", "accountant", "law firm",
        "auditor", "compliance officer", "wealth manager",
    ], 1.0),

    # Cross-border trade / SME payments — core customer profile
    "cross_border_sme": ([
        "cross-border", "cross border", "sme", "small business", "import",
        "export", "multi-currency", "multicurrency", "international payment",
        "global payment", "b2b payment", "trade payment",
    ], 1.0),

    # Payment friction / correspondent banking pain — Arie's value proposition
    "payment_friction": ([
        "payment delay", "high fees", "fx spread", "friction", "correspondent banking",
        "de-risking", "derisking", "unbanked", "underbanked", "nostro", "vostro",
        "banking desert", "account closure", "bank exit",
    ], 0.9),

    # Onboarding / KYC / compliance burden — Arie's differentiation
    "onboarding_kyc": ([
        "onboarding", "kyc", "kyb", "know your customer", "account opening",
        "compliance burden", "due diligence", "cdd", "enhanced due diligence",
        "edd", "beneficial owner", "ubo",
    ], 0.8),

    # FX / treasury / hedging — Arie's product set
    "fx_treasury": ([
        "fx", "hedging", "treasury", "liquidity", "cash management",
        "foreign exchange", "currency risk", "fx risk", "spot rate",
        "forward contract", "swap", "cash pooling", "netting",
    ], 0.9),

    # Priority corridors — Arie's target geographies
    "corridors": ([
        "africa", "india", "mena", "middle east", "mauritius", "kenya", "nigeria",
        "uae", "dubai", "south africa", "ghana", "ethiopia", "egypt",
        "saudi arabia", "bahrain", "rwanda",
    ], 0.8),

    # Stablecoins / trade finance — emerging opportunity, lower weight until paired
    # with a commercial/payment term (the keyword list itself acts as gating here)
    "stablecoin_tradefin": ([
        "stablecoin settlement", "tokenised deposit", "tokenized deposit",
        "trade finance", "receivables finance", "supply chain finance",
        "invoice finance", "accounts receivable", "factoring",
    ], 0.6),
}


# ---------------------------------------------------------------------------
# Customer Pain Intelligence — separate stream, never competes with fintech news
# ---------------------------------------------------------------------------

# Max items in the final pain stream passed to the digest.
PAIN_CAP: int = 15

# Per-source cap inside the pain stream (prevents any single source dominating).
PAIN_SOURCE_CAP: int = 4

# Keywords that indicate genuine user/business complaints (lowercase match).
PAIN_SIGNAL_KEYWORDS: List[str] = [
    "account closed", "account frozen", "account suspended", "account blocked",
    "account terminated", "account restricted", "account locked",
    "kyc", "aml", "verification delay", "compliance hold", "compliance review",
    "compliance nightmare",
    "payment stuck", "payment delayed", "transfer failed", "transfer held",
    "transfer stuck", "transfer delayed",
    "debanked", "de-banked", "de-risked", "derisked",
    "rejected", "declined", "access denied", "locked out",
    "funds on hold", "money stuck", "cannot withdraw", "withdrawal blocked",
    "unresponsive support", "no response", "ignored by support",
    "months waiting", "weeks waiting", "waiting for verification",
    "bank refused", "bank rejected", "bank exit", "bank closed",
    "correspondent banking problem", "wire rejected", "swift rejected",
    "onboarding delay", "kyb delay", "due diligence delay",
    "frozen funds", "held funds", "seized funds",
]

# Keywords that indicate news/promo rather than genuine complaint — exclude these.
PAIN_EXCLUDE_KEYWORDS: List[str] = [
    "press release", "partnership", "raises $", "raises €", "funding round",
    "series a", "series b", "series c", "seed round",
    "launches", "announces", "expands into", "integrates with",
    "acquires", "merger", "product launch", "new feature", "new product",
    "case study", "webinar", "whitepaper", "report finds", "survey finds",
    "market size", "growth forecast",
]

# Google News RSS queries targeting complaint signals.
# Each becomes a GNews RSS search URL in scraper.py.
PAIN_GNEWS_QUERIES: List[str] = [
    "Wise business account closed",
    "Revolut business account frozen",
    "Payoneer account verification delay",
    "Airwallex account closed",
    "business banking KYC delay",
    "company bank account rejected",
    "cross-border payment stuck",
    "supplier payment delayed",
    "AML review account frozen",
    "debanked business account",
]

# HN Algolia search queries for payment/banking complaints.
HN_PAIN_QUERIES: List[str] = [
    "bank account closed business",
    "payment account frozen",
    "KYC business account delay",
    "debanked",
    "Wise account closed",
    "Revolut business frozen",
    "cross border payment failed",
    "fintech account suspended",
]

# Trustpilot review pages to scrape (platform_name, trustpilot_domain).
# Only 1-2 star reviews are targeted.
TRUSTPILOT_TARGETS: List[Tuple[str, str]] = [
    ("Wise",      "wise.com"),
    ("Revolut",   "revolut.com"),
    ("Airwallex", "airwallex.com"),
    ("Payoneer",  "payoneer.com"),
]

# User type detection keywords (first match wins).
PAIN_USER_TYPE_KEYWORDS: Dict[str, List[str]] = {
    "ecommerce":   ["ecommerce", "e-commerce", "shopify", "amazon seller", "etsy", "woocommerce", "online store"],
    "exporter":    ["export", "import", "importer", "exporter", "supplier", "manufacturer", "shipper", "trade finance"],
    "marketplace": ["marketplace", "platform payments", "payout", "vendor payout", "seller"],
    "agency":      ["agency", "consultancy", "consultant", "bureau", "creative agency", "marketing agency"],
    "freelancer":  ["freelancer", "freelance", "contractor", "self-employed", "sole trader", "independent contractor"],
    "SME":         ["small business", "sme", "startup", "ltd", "llc", "business account", "company account"],
}

# ---------------------------------------------------------------------------
# v3 Named Entity Intelligence
# entity display name -> [aliases to match] (case-insensitive substring)
# Keep aliases specific enough to avoid false positives.
# ---------------------------------------------------------------------------

NAMED_ENTITIES: Dict[str, List[str]] = {
    # Competitors
    "Wise":            ["wise", "transferwise"],
    "Revolut":         ["revolut"],
    "Airwallex":       ["airwallex"],
    "Payoneer":        ["payoneer"],
    "Mercury":         ["mercury bank"],
    # Employer / Contractor platforms
    "Deel":            ["deel"],
    "Remote":          ["remote.com", "remote payroll"],
    "Multiplier":      ["multiplier"],
    # Mauritius ecosystem
    "AfrAsia":         ["afrasia"],
    "Bank One":        ["bank one mauritius"],
    "Absa Mauritius":  ["absa mauritius"],
    "FSC Mauritius":   ["fsc mauritius", "mauritius fsc"],
    # Arie own entities
    "ARIE Finance":    ["arie finance", "ariefinance"],
    "ACBM":            ["acbm"],
}

# ---------------------------------------------------------------------------
# v3 Watchlist Topics for trend tracking
# topic display name -> [keywords to match] (case-insensitive substring)
# ---------------------------------------------------------------------------

WATCHLIST_TOPICS: Dict[str, List[str]] = {
    "Stablecoins":               ["stablecoin", "usdc", "usdt", "tether", "pyusd"],
    "Africa cross-border":       ["africa", "african", "pan-african"],
    "Mauritius licensing":       ["mauritius", "fsc mauritius", "fsc licence"],
    "Wise complaints":           ["wise", "transferwise"],
    "Revolut activity":          ["revolut"],
    "Airwallex activity":        ["airwallex"],
    "Film production":           ["film production", "film rebate", "mfdc", "film incentive", "production finance"],
    "CSP / Mgmt Companies":      ["csp", "management company", "corporate service provider", "trust company", "fiduciary"],
}

# ---------------------------------------------------------------------------
# v4 Dedicated Intelligence Streams — GNews Sources, Caps, and Filter Lists
# ---------------------------------------------------------------------------

# GNews feeds for Mauritius Intelligence stream.
# Tracks EDB announcements, Ministry of Finance developments, licensing news,
# and investment initiatives beyond what the existing FSC_Mauritius feed covers.
MAURITIUS_INTEL_SOURCES: Dict[str, str] = {
    "GNews/EDB_Mauritius":        "https://news.google.com/rss/search?q=EDB+Mauritius+investment+incentive+fintech&hl=en&gl=MU&ceid=MU:en",
    "GNews/Mauritius_Finance":    "https://news.google.com/rss/search?q=Mauritius+%22Ministry+of+Finance%22+OR+budget+tax+%22financial+services%22&hl=en&gl=MU&ceid=MU:en",
    "GNews/Mauritius_Regulation": "https://news.google.com/rss/search?q=Mauritius+%22financial+services%22+regulation+licence+2025&hl=en&gl=US&ceid=US:en",
}

# GNews feeds for Competitor Intelligence stream.
# Focuses on product launches, pricing, expansion, account issues, and regulatory
# actions at ARIE's direct competitors.
COMPETITOR_INTEL_SOURCES: Dict[str, str] = {
    "GNews/Wise_Intel":       "https://news.google.com/rss/search?q=Wise+business+payment+product+pricing+expansion+account&hl=en&gl=US&ceid=US:en",
    "GNews/Revolut_Intel":    "https://news.google.com/rss/search?q=Revolut+business+banking+expansion+licence+product+complaint&hl=en&gl=US&ceid=US:en",
    "GNews/Competitor_Intel": "https://news.google.com/rss/search?q=Airwallex+OR+Payoneer+OR+Mercury+payment+business+product+expansion+2025&hl=en&gl=US&ceid=US:en",
}

# GNews feeds for Introducer Intelligence stream.
# Tracks acquisitions, mergers, expansion, and licence events at CSPs,
# management companies, and fiduciaries relevant to ARIE's referral network.
INTRODUCER_INTEL_SOURCES: Dict[str, str] = {
    # Mauritius-specific: any CSP/management company/fiduciary/fund admin activity
    "GNews/CSP_Mauritius": (
        "https://news.google.com/rss/search?q=%22corporate+services%22+OR+%22management+company%22"
        "+OR+fiduciary+OR+%22fund+administrator%22+OR+%22trust+company%22+Mauritius"
        "&hl=en&gl=MU&ceid=MU:en"
    ),
    # Offshore jurisdictions: BVI / Cayman / Seychelles / DIFC / ADGM CSP/trust/fund activity
    "GNews/CSP_Offshore": (
        "https://news.google.com/rss/search?q=%22corporate+service+provider%22+OR+%22trust+company%22"
        "+OR+%22fund+administrator%22+OR+fiduciary+BVI+OR+Cayman+OR+Seychelles+OR+DIFC+OR+ADGM"
        "&hl=en&gl=US&ceid=US:en"
    ),
    # Acquisitions / mergers / licences in the CSP/trust/fiduciary industry globally
    "GNews/CSP_Acquisition": (
        "https://news.google.com/rss/search?q=%22corporate+services%22+OR+%22trust+services%22"
        "+OR+%22fiduciary+services%22+acquisition+OR+merger+OR+expansion+OR+licence+2025"
        "&hl=en&gl=US&ceid=US:en"
    ),
    # UAE / Dubai / DIFC / ADGM CSP and fund admin
    "GNews/CSP_UAE": (
        "https://news.google.com/rss/search?q=%22corporate+service+provider%22+OR+fiduciary"
        "+OR+%22fund+administrator%22+OR+%22trust+services%22+Dubai+OR+UAE+OR+DIFC+OR+ADGM"
        "&hl=en&gl=AE&ceid=AE:en"
    ),
}

# Competitor names tracked in Competitor Intelligence stream.
COMPETITOR_NAMES: List[str] = [
    "Wise", "Revolut", "Airwallex", "Payoneer", "Mercury", "WorldFirst", "OFX",
    "Deel", "Remote", "Multiplier", "Stripe", "Nium", "Rapyd",
]

# Service-level keywords: article must mention at least one CSP/fiduciary/trust concept.
INTRODUCER_SERVICE_KEYWORDS: List[str] = [
    "corporate service provider", "corporate services", "csp",
    "management company", "management companies",
    "fiduciary", "trust company", "trust services", "trust administration", "trustee",
    "fund administrator", "fund administration", "fund services",
    "company formation", "company incorporation",
    "global business company", "gbc", "authorised company",
    "private client", "family office", "wealth structuring",
    "registered agent", "registered office",
    "domiciliation", "entity management", "entity administration",
    "offshore company", "offshore structuring",
    "corporate administrator", "corporate administration",
    "governance services",
]

# Jurisdiction keywords used in the two-tier filter.
INTRODUCER_JURISDICTION_KEYWORDS: List[str] = [
    "mauritius", "difc", "adgm", "bvi", "british virgin islands",
    "cayman islands", "cayman", "seychelles", "jersey", "guernsey",
    "singapore", "dubai", "abu dhabi", "uae",
]

# Event keywords used in the two-tier filter.
INTRODUCER_EVENT_KEYWORDS: List[str] = [
    "acquisition", "acquires", "acquired", "merger", "merges", "merged",
    "expansion", "expands", "expanded", "new office", "opens office",
    "licence", "license", "licensed", "regulated", "regulation",
    "partnership", "partners", "joint venture",
    "launches", "launch", "appoints", "appointed", "new service", "new product",
]

# Kept for backward-compat import in enrich.py (unused — replaced by three-list approach).
INTRODUCER_FILTER_KEYWORDS: List[str] = INTRODUCER_SERVICE_KEYWORDS

# Max items passed to Claude per new stream.
MAURITIUS_INTEL_CAP: int = 6
COMPETITOR_INTEL_CAP: int = 6
INTRODUCER_INTEL_CAP: int = 6
