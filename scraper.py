"""
Async scrapers for Arie Finance Market Intelligence.

Sources
-------
Reddit: application-only OAuth (60 req/min, preferred) when REDDIT_CLIENT_ID +
        REDDIT_CLIENT_SECRET env vars are present; falls back to hardened
        old.reddit.com RSS with 429 backoff otherwise.
        Subreddits: r/fintech, r/smallbusiness, r/Entrepreneur, r/banking, r/paymentprocessing
Native RSS       : Finextra (main), CoinDesk policy, The Block, PYMNTS cross-border,
                   FinTech Global, Bank of Mauritius, FCA (RSS-first + HTML fallback),
                   ECB Press, ECB Publications
Google News RSS  : Funding, FATF, Stablecoins, MiCA, AMLA, FSC Mauritius, AML/Fraud, Corridors,
                   Film Incentives (broad), Mauritius Film, Screen Daily proxy,
                   IMF (native blocked from DC IPs — via GNews proxy),
                   World Bank (native blocked from DC IPs — via GNews proxy)
HTML             : The Paypers (HTML scrape)

FSC Mauritius removed as standalone HTML source — covered via Google News feed instead.
FATF covered via GNews/FATF Google News feed (native FATF RSS/HTML always returned 0 items).

Data quality safeguard: ScrapeResult.low_data_warning is True if total items < 15.
Any source returning 0 items is logged as WARNING and added to source_failures.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import feedparser
import httpx
from bs4 import BeautifulSoup

from config import (
    HN_PAIN_QUERIES,
    PAIN_GNEWS_QUERIES,
    TRUSTPILOT_TARGETS,
    MAURITIUS_INTEL_SOURCES,
    COMPETITOR_INTEL_SOURCES,
    INTRODUCER_INTEL_SOURCES,
)
from models import ScrapedItem, ScrapeResult, SourceHealthItem

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

REDDIT_SUBREDDITS = ["fintech", "smallbusiness", "Entrepreneur", "banking", "paymentprocessing"]

# Native publisher RSS feeds (all parsed via _parse_rss which sends BROWSER_UA)
RSS_SOURCES: Dict[str, str] = {
    "Finextra":              "https://www.finextra.com/rss/headlines.aspx",
    "CoinDesk":              "https://www.coindesk.com/arc/outboundfeeds/rss/?category=policy",
    "The Block":             "https://www.theblock.co/rss.xml",
    "PYMNTS":                "https://www.pymnts.com/category/cross-border-commerce/feed/",
    "FinTech Global":        "https://fintech.global/feed/",
    "Bank of Mauritius":     "https://www.bom.mu/latest-news.xml",
    # ECB native RSS — confirmed working from datacenter IPs
    "ECB Press":             "https://www.ecb.europa.eu/rss/press.html",
    "ECB Publications":      "https://www.ecb.europa.eu/rss/pub.html",
}

# Google News RSS feeds (aggregator; also need BROWSER_UA, which _parse_rss sends)
GNEWS_SOURCES: Dict[str, str] = {
    "GNews/Funding":         "https://news.google.com/rss/search?q=fintech+payments+funding+round&hl=en&gl=US&ceid=US:en",
    "GNews/FATF":            "https://news.google.com/rss/search?q=FATF+financial+action+task+force&hl=en&gl=US&ceid=US:en",
    "GNews/Stablecoins":     "https://news.google.com/rss/search?q=stablecoin+tokenization+payments&hl=en&gl=US&ceid=US:en",
    "GNews/MiCA":            "https://news.google.com/rss/search?q=MiCA+regulation+crypto+stablecoin+EU+2026&hl=en&gl=US&ceid=US:en",
    "GNews/AMLA":            "https://news.google.com/rss/search?q=AMLA+EU+anti-money-laundering+authority+fintech&hl=en&gl=US&ceid=US:en",
    "GNews/FSC_Mauritius":   "https://news.google.com/rss/search?q=FSC+Mauritius+OR+%22Bank+of+Mauritius%22+regulation&hl=en&gl=US&ceid=US:en",
    "GNews/AML_Fraud":       "https://news.google.com/rss/search?q=AML+fraud+enforcement+fintech+payments&hl=en&gl=US&ceid=US:en",
    "GNews/Corridors":       "https://news.google.com/rss/search?q=cross-border+payments+Africa+India+MENA&hl=en&gl=US&ceid=US:en",
    "GNews/Film_Incentives": "https://news.google.com/rss/search?q=film+production+tax+incentive+location+international&hl=en&gl=US&ceid=US:en",
    "GNews/Mauritius_Film":  "https://news.google.com/rss/search?q=%22Mauritius%22+film+rebate+OR+production&hl=en&gl=US&ceid=US:en",
    "GNews/Screen_Daily":    "https://news.google.com/rss/search?q=screen+daily+film+production+finance+incentive&hl=en&gl=US&ceid=US:en",
    # IMF and World Bank native feeds blocked from datacenter IPs — use Google News proxy instead
    "GNews/IMF":             "https://news.google.com/rss/search?q=IMF+cross-border+payments+OR+financial+stability+OR+fintech&hl=en&gl=US&ceid=US:en",
    "GNews/WorldBank":       "https://news.google.com/rss/search?q=%22World+Bank%22+payments+OR+remittances+OR+trade+finance&hl=en&gl=US&ceid=US:en",
}

HTML_SOURCES: Dict[str, str] = {
    "The Paypers": "https://thepaypers.com/news",
    "FCA":         "https://www.fca.org.uk/news/news-stories",
}

FCA_RSS_URLS = [
    "https://www.fca.org.uk/rss.xml",
    "https://www.fca.org.uk/news/rss.xml",
    "https://www.fca.org.uk/news/news-stories.rss",
    "https://www.fca.org.uk/news.rss",
    "https://www.fca.org.uk/publication/feeds/press-releases.xml",
]

REDDIT_UA = "ArieFinance-MarketIntel/1.0 (weekly scraper; contact ismael@ariefinance.com)"

# ---------------------------------------------------------------------------
# Published date helpers
# ---------------------------------------------------------------------------

def _parse_published_at(entry: object) -> Optional[str]:
    """
    Extract a published date from a feedparser entry and return an ISO-8601 string.
    Tries entry.published_parsed, entry.updated_parsed, then entry.published / entry.updated
    as raw strings (RFC-2822 format from RSS). Returns None on failure.
    """
    # feedparser gives time_struct tuples for parsed dates
    for attr in ("published_parsed", "updated_parsed"):
        ts = getattr(entry, attr, None)
        if ts is not None:
            try:
                dt = datetime(*ts[:6], tzinfo=timezone.utc)
                return dt.date().isoformat()
            except Exception:
                pass
    # Fall back to raw string fields
    for attr in ("published", "updated"):
        raw = getattr(entry, attr, None)
        if raw and isinstance(raw, str):
            try:
                dt = parsedate_to_datetime(raw)
                return dt.date().isoformat()
            except Exception:
                # Try ISO parse as last resort
                try:
                    return raw[:10]  # first 10 chars of "YYYY-MM-DD..."
                except Exception:
                    pass
    return None
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": BROWSER_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
}
LOW_DATA_THRESHOLD = 15
HTTP_TIMEOUT = 20


# ---------------------------------------------------------------------------
# Reddit scraper
#
# Strategy A — Application-only OAuth (preferred):
#   Used when REDDIT_CLIENT_ID + REDDIT_CLIENT_SECRET env vars are set.
#   Obtains a bearer token via POST /api/v1/access_token (client_credentials),
#   then fetches each subreddit via oauth.reddit.com at 60 req/min.
#   If token acquisition fails, falls back to Strategy B automatically.
#
# Strategy B — Hardened anonymous RSS fallback:
#   Used when OAuth creds are absent or token fetch fails.
#   Fetches old.reddit.com top.rss with 5s spacing between subreddits.
#   On HTTP 429: waits 10s and retries once; skips sub on second 429.
# ---------------------------------------------------------------------------

_REDDIT_OAUTH_TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
_REDDIT_API_BASE = "https://oauth.reddit.com"
_REDDIT_RSS_BASE = "https://old.reddit.com"


def _reddit_ua() -> str:
    """Return the Reddit User-Agent: env var if set, else the hardcoded default."""
    return os.environ.get("REDDIT_USER_AGENT", REDDIT_UA)


async def _reddit_get_token(client: httpx.AsyncClient) -> Optional[str]:
    """
    Obtain an application-only OAuth bearer token.
    Returns the token string on success, None on any failure.
    """
    client_id = os.environ.get("REDDIT_CLIENT_ID", "")
    client_secret = os.environ.get("REDDIT_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        return None
    try:
        resp = await client.post(
            _REDDIT_OAUTH_TOKEN_URL,
            auth=(client_id, client_secret),
            data={"grant_type": "client_credentials"},
            headers={"User-Agent": _reddit_ua()},
            timeout=HTTP_TIMEOUT,
        )
        if resp.status_code != 200:
            logger.warning(
                "Reddit OAuth token fetch returned HTTP %d — falling back to RSS",
                resp.status_code,
            )
            return None
        token = resp.json().get("access_token")
        if not token:
            logger.warning("Reddit OAuth: no access_token in response — falling back to RSS")
            return None
        logger.info("Reddit OAuth: bearer token acquired")
        return token
    except Exception as exc:
        logger.warning("Reddit OAuth token fetch FAILED (%s) — falling back to RSS", exc)
        return None


async def _reddit_fetch_sub_oauth(
    client: httpx.AsyncClient, sub: str, token: str
) -> List[ScrapedItem]:
    """Fetch top posts for one subreddit using the authenticated API."""
    url = f"{_REDDIT_API_BASE}/r/{sub}/top"
    try:
        resp = await client.get(
            url,
            params={"t": "week", "limit": "25"},
            headers={
                "Authorization": f"Bearer {token}",
                "User-Agent": _reddit_ua(),
            },
            timeout=HTTP_TIMEOUT,
        )
        if resp.status_code != 200:
            logger.warning("Reddit OAuth r/%s: HTTP %d", sub, resp.status_code)
            return []
        children = resp.json().get("data", {}).get("children", [])
        items: List[ScrapedItem] = []
        for child in children:
            d = child.get("data", {})
            title = (d.get("title") or "").strip()
            if not title:
                continue
            permalink = d.get("permalink", "")
            post_url = (
                f"https://www.reddit.com{permalink}"
                if permalink.startswith("/")
                else d.get("url", "")
            )
            selftext = d.get("selftext", "") or ""
            clean = re.sub(r"\s+", " ", selftext).strip()[:300]
            created_utc = d.get("created_utc")
            published_at: Optional[str] = None
            if created_utc:
                try:
                    published_at = datetime.fromtimestamp(
                        float(created_utc), tz=timezone.utc
                    ).date().isoformat()
                except Exception:
                    pass
            items.append(ScrapedItem(
                source=f"Reddit/r/{sub}",
                title=title,
                url=post_url,
                summary=clean or title[:300],
                score=d.get("score"),
                num_comments=d.get("num_comments"),
                published_at=published_at,
            ))
        logger.info("Reddit OAuth r/%s: %d items", sub, len(items))
        return items
    except Exception as exc:
        logger.warning("Reddit OAuth r/%s FAILED: %s", sub, exc)
        return []


async def _reddit_fetch_sub_rss(sub: str) -> List[ScrapedItem]:
    """
    Fetch one subreddit via old.reddit.com RSS with a single 429-retry.
    Returns empty list if both attempts fail or the sub is rate-limited.
    """
    url = f"{_REDDIT_RSS_BASE}/r/{sub}/top.rss?sort=top&t=week&limit=25"

    def _do_parse() -> feedparser.FeedParserDict:
        return feedparser.parse(
            url,
            agent=_reddit_ua(),
            request_headers={"Accept": "application/rss+xml,*/*"},
        )

    loop = asyncio.get_event_loop()

    for attempt in (1, 2):
        feed = await loop.run_in_executor(None, _do_parse)
        status = getattr(feed, "status", None)
        if status == 429 or (not feed.entries and status == 429):
            if attempt == 1:
                logger.warning(
                    "Reddit r/%s RSS: HTTP 429 — waiting 4s before retry", sub
                )
                await asyncio.sleep(4)
                continue
            else:
                logger.warning(
                    "Reddit r/%s RSS: HTTP 429 on retry — skipping sub", sub
                )
                return []
        # Successful response (entries may still be empty if Reddit returns HTML)
        items: List[ScrapedItem] = []
        for entry in feed.entries[:25]:
            title = getattr(entry, "title", "").strip()
            if not title:
                continue
            link = getattr(entry, "link", "")
            raw = getattr(entry, "summary", "") or ""
            clean = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", raw)).strip()[:300]
            items.append(ScrapedItem(
                source=f"Reddit/r/{sub}",
                title=title,
                url=link,
                summary=clean or title[:300],
                published_at=_parse_published_at(entry),
            ))
        logger.info(
            "Reddit r/%s RSS: %d items (feed had %d entries, status=%s)",
            sub, len(items), len(feed.entries), status,
        )
        if len(items) < 5:
            logger.warning(
                "Reddit r/%s returned only %d items — possible rate limit (status=%s)",
                sub, len(items), status,
            )
        return items

    return []  # unreachable but satisfies type checker


async def _scrape_reddit_all(client: httpx.AsyncClient) -> Tuple[str, List[ScrapedItem]]:
    """
    Fetch all configured subreddits.

    Tries application-only OAuth first (when REDDIT_CLIENT_ID + REDDIT_CLIENT_SECRET
    are present).  Falls back to hardened anonymous RSS (5s spacing, 429 backoff)
    if creds are absent or the token fetch fails.
    """
    token = await _reddit_get_token(client)

    items: List[ScrapedItem] = []

    if token:
        # OAuth path — no spacing needed; authenticated API allows 60 req/min
        logger.info("Reddit: using OAuth path (%d subreddits)", len(REDDIT_SUBREDDITS))
        for sub in REDDIT_SUBREDDITS:
            result = await _reddit_fetch_sub_oauth(client, sub, token)
            items.extend(result)
    else:
        # Hardened anonymous RSS fallback — 2s gap between requests
        logger.info("Reddit: using anonymous RSS fallback (%d subreddits)", len(REDDIT_SUBREDDITS))
        for i, sub in enumerate(REDDIT_SUBREDDITS):
            if i > 0:
                await asyncio.sleep(2)
            result = await _reddit_fetch_sub_rss(sub)
            items.extend(result)

    logger.info("Reddit total: %d items across %d subreddits", len(items), len(REDDIT_SUBREDDITS))
    return "Reddit", items


# ---------------------------------------------------------------------------
# RSS scraper (Finextra)
# ---------------------------------------------------------------------------

def _parse_rss(source_name: str, feed_url: str) -> List[ScrapedItem]:
    try:
        feed = feedparser.parse(feed_url, agent=BROWSER_UA,
                                request_headers={"Accept": "application/rss+xml,*/*"})
        items = []
        for entry in feed.entries[:20]:
            title = getattr(entry, "title", "").strip()
            link = getattr(entry, "link", "")
            raw = getattr(entry, "summary", "") or getattr(entry, "description", "") or title
            summary = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", raw)).strip()[:300]
            if title:
                items.append(ScrapedItem(
                    source=source_name,
                    title=title,
                    url=link,
                    summary=summary,
                    published_at=_parse_published_at(entry),
                ))
        logger.info("RSS %s: %d items", source_name, len(items))
        return items
    except Exception as exc:
        logger.warning("RSS %s FAILED: %s", source_name, exc)
        return []


async def _scrape_rss_all() -> List[Tuple[str, List[ScrapedItem]]]:
    loop = asyncio.get_event_loop()
    all_sources = {
        **RSS_SOURCES,
        **GNEWS_SOURCES,
        **MAURITIUS_INTEL_SOURCES,
        **COMPETITOR_INTEL_SOURCES,
        **INTRODUCER_INTEL_SOURCES,
    }
    tasks = [loop.run_in_executor(None, _parse_rss, name, url)
             for name, url in all_sources.items()]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    return [(name, r if isinstance(r, list) else [])
            for name, r in zip(all_sources.keys(), results)]


# ---------------------------------------------------------------------------
# HTML scrapers (per-site)
# ---------------------------------------------------------------------------

def _parse_paypers(html: str) -> List[dict]:
    soup = BeautifulSoup(html, "lxml")
    BASE = "https://thepaypers.com"
    items = []
    candidates = (soup.select(".news-item") or soup.select("article")
                  or soup.select(".article-item") or soup.select("li.item"))
    if candidates:
        for el in candidates[:20]:
            a = el.find("a", href=True)
            if not a:
                continue
            title = a.get_text(strip=True)
            href = a["href"]
            if not href.startswith("http"):
                href = BASE + href
            p = el.find("p")
            summary = p.get_text(strip=True)[:300] if p else title
            if len(title) > 10:
                items.append({"title": title, "url": href, "summary": summary})
        return items
    news_re = re.compile(r"^/[a-z\-]+/news/[a-z\-]+")
    seen: set = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if news_re.match(href) and href not in seen:
            seen.add(href)
            title = a.get_text(strip=True)
            if len(title) > 15:
                items.append({"title": title, "url": BASE + href, "summary": title})
                if len(items) >= 20:
                    break
    return items


def _parse_fca(html: str) -> List[dict]:
    soup = BeautifulSoup(html, "lxml")
    BASE = "https://www.fca.org.uk"
    items = []
    candidates = (soup.select("article") or soup.select(".news-item") or soup.select(".fc-news"))
    if candidates:
        for art in candidates[:20]:
            heading = art.find(["h2", "h3", "h4"])
            a = art.find("a", href=True)
            if not heading and not a:
                continue
            title = (heading or a).get_text(strip=True)
            href = a["href"] if a else ""
            if href and not href.startswith("http"):
                href = BASE + href
            p = art.find("p")
            summary = p.get_text(strip=True)[:300] if p else title
            if len(title) > 5:
                items.append({"title": title, "url": href, "summary": summary})
        if items:
            return items
    seen: set = set()
    for tag in soup.select("h2 a, h3 a, h4 a"):
        href = tag.get("href", "")
        title = tag.get_text(strip=True)
        if len(title) > 10 and ("/news/" in href or "/enforcement/" in href):
            if not href.startswith("http"):
                href = BASE + href
            if href not in seen:
                seen.add(href)
                items.append({"title": title, "url": href, "summary": title})
    if items:
        return items[:20]
    for a in soup.find_all("a", href=True):
        href = a["href"]
        title = a.get_text(strip=True)
        if len(title) > 15 and "/news/" in href and href not in seen:
            seen.add(href)
            if not href.startswith("http"):
                href = BASE + href
            items.append({"title": title, "url": href, "summary": title})
            if len(items) >= 20:
                break
    return items


_HTML_PARSERS = {
    "The Paypers":       _parse_paypers,
    "FCA":               _parse_fca,
}


# ---------------------------------------------------------------------------
# FCA RSS-first strategy
# ---------------------------------------------------------------------------

def _try_rss(source_name: str, rss_urls: List[str]) -> List[ScrapedItem]:
    """Generic RSS-first helper. Try URLs in order; return items from first that works."""
    for rss_url in rss_urls:
        try:
            feed = feedparser.parse(rss_url, agent=BROWSER_UA)
            if not feed.entries:
                logger.debug("%s RSS %s: no entries", source_name, rss_url)
                continue
            items = []
            for entry in feed.entries[:20]:
                title = getattr(entry, "title", "").strip()
                link = getattr(entry, "link", "")
                raw = (getattr(entry, "summary", "")
                       or getattr(entry, "description", "") or title)
                summary = re.sub(r"\s+", " ",
                                 re.sub(r"<[^>]+>", " ", raw)).strip()[:300]
                if title:
                    items.append(ScrapedItem(
                        source=source_name,
                        title=title,
                        url=link,
                        summary=summary,
                        published_at=_parse_published_at(entry),
                    ))
            if items:
                logger.info("%s RSS (%s): %d items", source_name, rss_url, len(items))
                return items
        except Exception as exc:
            logger.debug("%s RSS %s failed: %s", source_name, rss_url, exc)
    logger.warning("%s: all RSS URLs returned 0 items", source_name)
    return []


def _try_fca_rss() -> List[ScrapedItem]:
    return _try_rss("FCA", FCA_RSS_URLS)


# ---------------------------------------------------------------------------
# HTML scrape dispatcher
# ---------------------------------------------------------------------------

async def _scrape_html_source(
    client: httpx.AsyncClient, name: str, url: str
) -> Tuple[str, List[ScrapedItem]]:
    if name == "FCA":
        loop = asyncio.get_event_loop()
        rss_items = await loop.run_in_executor(None, _try_fca_rss)
        if rss_items:
            return name, rss_items
        logger.warning("FCA RSS failed; falling back to HTML scrape")
    try:
        r = await client.get(url, headers=HEADERS, timeout=HTTP_TIMEOUT,
                             follow_redirects=True)
        r.raise_for_status()
        html = r.text
        if len(html) < 500:
            logger.warning("HTML %s: response too short (%d bytes) — bot block?",
                           name, len(html))
            return name, []
        parser_fn = _HTML_PARSERS[name]
        raw = parser_fn(html)
        items = [
            ScrapedItem(source=name, title=it["title"], url=it["url"],
                        summary=it.get("summary", it["title"])[:300])
            for it in raw
        ]
        logger.info("HTML %s: %d items", name, len(items))
        return name, items
    except Exception as exc:
        logger.warning("HTML %s FAILED: %s", name, exc)
        return name, []


async def _scrape_html_all(client: httpx.AsyncClient) -> List[Tuple[str, List[ScrapedItem]]]:
    results = await asyncio.gather(
        *[_scrape_html_source(client, name, url) for name, url in HTML_SOURCES.items()],
        return_exceptions=True,
    )
    return [r for r in results if isinstance(r, tuple)]


# ---------------------------------------------------------------------------
# Customer Pain Intelligence scrapers
# (separate stream — results go into ScrapeResult.pain_items_raw)
# ---------------------------------------------------------------------------

async def _scrape_hn_pain(client: httpx.AsyncClient) -> Tuple[str, List[ScrapedItem]]:
    """
    Search Hacker News via the Algolia API for payment/banking complaint signals.
    Returns up to 15 items per query, filtered to the last 30 days.
    """
    items: List[ScrapedItem] = []
    seen_urls: set = set()
    cutoff = int((datetime.now(tz=timezone.utc) - timedelta(days=30)).timestamp())

    for query in HN_PAIN_QUERIES:
        try:
            resp = await client.get(
                "https://hn.algolia.com/api/v1/search",
                params={
                    "query": query,
                    "tags": "(story,ask_hn,show_hn)",
                    "hitsPerPage": "15",
                    "numericFilters": f"created_at_i>{cutoff}",
                },
                headers={"User-Agent": BROWSER_UA},
                timeout=HTTP_TIMEOUT,
            )
            if resp.status_code != 200:
                logger.warning("HN pain query '%s': HTTP %d", query, resp.status_code)
                continue
            for hit in resp.json().get("hits", []):
                hn_id = hit.get("objectID", "")
                url = hit.get("url") or f"https://news.ycombinator.com/item?id={hn_id}"
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)
                title = (hit.get("title") or "").strip()
                if not title:
                    continue
                raw_text = hit.get("story_text") or hit.get("comment_text") or ""
                summary = re.sub(r"<[^>]+>", " ", raw_text)
                summary = re.sub(r"\s+", " ", summary).strip()[:300] or title[:300]
                created_at = hit.get("created_at", "")
                items.append(ScrapedItem(
                    source="HackerNews",
                    title=title,
                    url=url,
                    summary=summary,
                    score=hit.get("points"),
                    num_comments=hit.get("num_comments"),
                    published_at=created_at[:10] if len(created_at) >= 10 else None,
                ))
        except Exception as exc:
            logger.warning("HN pain query '%s' FAILED: %s", query, exc)

    logger.info("HN pain: %d items across %d queries", len(items), len(HN_PAIN_QUERIES))
    return "HackerNews", items


async def _scrape_trustpilot_pain(client: httpx.AsyncClient) -> Tuple[str, List[ScrapedItem]]:
    """
    Attempt to extract 1-2 star reviews from Trustpilot for key payment platforms.
    Uses JSON-LD extraction. Returns empty list if blocked or no JSON-LD reviews found.
    """
    items: List[ScrapedItem] = []
    for platform_name, domain in TRUSTPILOT_TARGETS:
        url = f"https://www.trustpilot.com/review/{domain}?stars=1&stars=2"
        try:
            resp = await client.get(url, headers=HEADERS, timeout=HTTP_TIMEOUT, follow_redirects=True)
            if resp.status_code not in (200,):
                logger.warning("Trustpilot %s: HTTP %d — skipping", platform_name, resp.status_code)
                continue
            soup = BeautifulSoup(resp.text, "lxml")
            found = 0
            for script_tag in soup.find_all("script", type="application/ld+json"):
                try:
                    data = json.loads(script_tag.string or "")
                except Exception:
                    continue
                entries = data if isinstance(data, list) else [data]
                for entry in entries:
                    if entry.get("@type") != "Review":
                        continue
                    body = (entry.get("reviewBody") or "").strip()
                    rating = float((entry.get("reviewRating") or {}).get("ratingValue", 5))
                    if not body or rating > 2:
                        continue
                    headline = f"{platform_name} complaint: {body[:90].rstrip('.')}..."
                    items.append(ScrapedItem(
                        source=f"Trustpilot/{platform_name}",
                        title=headline,
                        url=url,
                        summary=body[:300],
                        published_at=None,
                    ))
                    found += 1
                    if found >= 5:
                        break
                if found >= 5:
                    break
            logger.info("Trustpilot %s: %d reviews extracted", platform_name, found)
        except Exception as exc:
            logger.warning("Trustpilot %s FAILED: %s", platform_name, exc)

    logger.info("Trustpilot pain total: %d items", len(items))
    return "Trustpilot", items


def _parse_pain_gnews_all() -> List[Tuple[str, List[ScrapedItem]]]:
    """
    Parse Google News RSS for each PAIN_GNEWS_QUERIES search term.
    Each query uses the same BROWSER_UA strategy as the main GNews feeds.
    """
    results: List[Tuple[str, List[ScrapedItem]]] = []
    for idx, query in enumerate(PAIN_GNEWS_QUERIES, 1):
        source_name = f"GNews/Pain{idx}"
        encoded = query.replace(" ", "+")
        feed_url = f"https://news.google.com/rss/search?q={encoded}&hl=en&gl=US&ceid=US:en"
        items = _parse_rss(source_name, feed_url)
        results.append((source_name, items))
    return results


def _read_manual_sources() -> Tuple[str, List[ScrapedItem]]:
    """
    Read manual_sources.json (LinkedIn/Facebook/X links collected manually).
    File format: JSON array of objects with keys: url, title, summary (opt),
    source (opt, default "Manual"), published_at (opt, YYYY-MM-DD).
    Returns empty list if file does not exist or is malformed.
    """
    path = Path(__file__).parent / "manual_sources.json"
    if not path.exists():
        return "Manual", []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        entries = raw if isinstance(raw, list) else raw.get("items", [])
        items: List[ScrapedItem] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            url = (entry.get("url") or "").strip()
            title = (entry.get("title") or "").strip()
            if not url or not title or url.startswith("http") is False:
                continue
            items.append(ScrapedItem(
                source=entry.get("source", "Manual"),
                title=title,
                url=url,
                summary=(entry.get("summary") or title)[:300],
                published_at=entry.get("published_at"),
            ))
        logger.info("Manual sources: %d items from %s", len(items), path.name)
        return "Manual", items
    except Exception as exc:
        logger.warning("manual_sources.json read FAILED: %s", exc)
        return "Manual", []


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

async def scrape_all() -> ScrapeResult:
    """Run all scrapers concurrently and return a ScrapeResult."""
    loop = asyncio.get_event_loop()
    async with httpx.AsyncClient() as client:
        (
            reddit_result,
            rss_results,
            html_results,
            pain_hn_result,
            pain_tp_result,
            pain_gnews_raw,
            pain_manual_result,
        ) = await asyncio.gather(
            _scrape_reddit_all(client),
            _scrape_rss_all(),
            _scrape_html_all(client),
            _scrape_hn_pain(client),
            _scrape_trustpilot_pain(client),
            loop.run_in_executor(None, _parse_pain_gnews_all),
            loop.run_in_executor(None, _read_manual_sources),
            return_exceptions=True,
        )

    # --- collect pain items_raw (separate from main commercial stream) ---
    pain_items_raw: List[ScrapedItem] = []
    seen_pain_urls: set = set()

    def _add_pain(result: object) -> None:
        if not isinstance(result, tuple):
            return
        _, pitems = result
        if not isinstance(pitems, list):
            return
        for it in pitems:
            if it.url and it.url not in seen_pain_urls:
                seen_pain_urls.add(it.url)
                pain_items_raw.append(it)

    _add_pain(pain_hn_result)
    _add_pain(pain_tp_result)
    _add_pain(pain_manual_result)
    if isinstance(pain_gnews_raw, list):
        for pair in pain_gnews_raw:
            _add_pain(pair)

    logger.info("Pain items raw: %d (before enrichment)", len(pain_items_raw))

    groups: Dict[str, List[ScrapedItem]] = {}

    if isinstance(reddit_result, tuple):
        _, items = reddit_result
        groups["Reddit"] = items

    if isinstance(rss_results, list):
        for name, items in rss_results:
            groups[name] = items

    if isinstance(html_results, list):
        for name, items in html_results:
            groups[name] = items

    seen: set = set()
    all_items: List[ScrapedItem] = []
    source_counts: Dict[str, int] = {}
    failures: List[str] = []

    # Raw total across all sources BEFORE URL-dedup (sum of all items per group)
    raw_collected: int = sum(len(items) for items in groups.values())

    for group_name, items in groups.items():
        count = 0
        for item in items:
            if item.url not in seen:
                seen.add(item.url)
                all_items.append(item)
                count += 1
        source_counts[group_name] = count
        if count == 0:
            logger.warning("SOURCE RETURNED 0 ITEMS: %s", group_name)
            failures.append(group_name)

    # Post-URL-dedup count (before title-dedup; title-dedup happens in enrich.py)
    total_after_url_dedup = len(all_items)

    total = total_after_url_dedup
    low_data = total < LOW_DATA_THRESHOLD
    if low_data:
        logger.warning("LOW DATA WARNING: only %d items (threshold: %d)", total, LOW_DATA_THRESHOLD)

    logger.info(
        "Total items: %d (raw: %d before URL-dedup) | failures: %s",
        total, raw_collected, failures or "none",
    )

    return ScrapeResult(
        items=all_items,
        source_counts=source_counts,
        source_failures=failures,
        low_data_warning=low_data,
        total_collected=raw_collected,
        total_after_dedup=total_after_url_dedup,  # enrich will update this after title-dedup
        pain_items_raw=pain_items_raw,
    )


# ---------------------------------------------------------------------------
# Source Health assembly (deterministic, no-LLM)
# ---------------------------------------------------------------------------

# Static category mapping: source-name prefix → display category.
# Checked in order; first match wins.
_SOURCE_CATEGORY_PREFIXES: List[Tuple[str, str]] = [
    ("Reddit",          "Community"),
    ("HackerNews",      "Customer Pain"),
    ("Trustpilot",      "Customer Pain"),
    ("GNews/Pain",      "Customer Pain"),
    ("GNews/FATF",      "Regulatory"),
    ("GNews/FSC",       "Regulatory"),
    ("GNews/AML",       "Regulatory"),
    ("GNews/MiCA",      "Regulatory"),
    ("GNews/AMLA",      "Regulatory"),
    ("FCA",             "Regulatory"),
    ("GNews/",          "News"),         # remaining GNews feeds
    ("Manual",          "Manual"),
]

# Source names whose zero-item outcome is explained by missing Reddit OAuth creds.
_REDDIT_SOURCE_NAMES = {"Reddit"}


def _source_category(source_name: str) -> str:
    """Return a display category for a source name using the static prefix map."""
    for prefix, category in _SOURCE_CATEGORY_PREFIXES:
        if source_name.startswith(prefix):
            return category
    return "News"


def build_source_health(
    source_counts: Dict[str, int],
    source_failures: List[str],
    reddit_configured: bool,
    last_checked: str,
) -> List[SourceHealthItem]:
    """
    Build a deterministic list of SourceHealthItem from scrape result fields.

    Status rules:
    - items > 0  → active
    - items == 0 AND source is a Reddit source AND reddit_configured is False
                 → not_configured
    - items == 0 AND source in source_failures → failed
    - items == 0 otherwise → failed
    Also includes any name in source_failures not already covered by source_counts.

    No thresholds are fabricated — "partial" is not emitted in this version
    because no reliable expected-minimum baseline exists in the codebase.

    Args:
        source_counts:      {source_name: item_count} from ScrapeResult
        source_failures:    list of source names that returned 0 items
        reddit_configured:  True if REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET are set
        last_checked:       ISO timestamp to stamp on each item (pass digest.generated_at)

    Returns:
        List[SourceHealthItem] sorted so failures/not_configured appear first.
    """
    items: List[SourceHealthItem] = []
    seen: set = set()

    # Process every source seen in source_counts
    for source_name, count in source_counts.items():
        seen.add(source_name)
        category = _source_category(source_name)

        if count > 0:
            status = "active"
            error_summary = ""
        elif source_name in _REDDIT_SOURCE_NAMES and not reddit_configured:
            status = "not_configured"
            error_summary = "Reddit OAuth credentials not set"
        else:
            status = "failed"
            error_summary = "Returned 0 items"

        items.append(SourceHealthItem(
            source_name=source_name,
            category=category,
            status=status,
            items_collected=count,
            error_summary=error_summary,
            last_checked=last_checked,
        ))

    # Include any failure names not already in source_counts
    for source_name in source_failures:
        if source_name in seen:
            continue
        seen.add(source_name)
        category = _source_category(source_name)
        if source_name in _REDDIT_SOURCE_NAMES and not reddit_configured:
            status = "not_configured"
            error_summary = "Reddit OAuth credentials not set"
        else:
            status = "failed"
            error_summary = "Returned 0 items"
        items.append(SourceHealthItem(
            source_name=source_name,
            category=category,
            status=status,
            items_collected=0,
            error_summary=error_summary,
            last_checked=last_checked,
        ))

    # Sort: failed + not_configured first, then active; within each group alphabetically
    _STATUS_ORDER = {"failed": 0, "not_configured": 1, "partial": 2, "active": 3}
    items.sort(key=lambda x: (_STATUS_ORDER.get(x.status, 9), x.source_name))

    return items
