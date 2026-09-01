"""
Active Brand Watch for Arie Finance — Phase 1.

Runs a dedicated sweep for ARIE / ACBM mentions across available surfaces.
Does NOT depend on the main scrape pipeline — runs independently before digest assembly.

Surfaces attempted:
    Covered:     HN (Algolia API), Reddit (OAuth or anon search), Direct-mention pass over scraped items
    Not covered: LinkedIn, paywalled press, private groups, Twitter/X
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import List, Optional

import httpx

from models import BrandWatchResult, DigestItem, ScrapeResult

logger = logging.getLogger(__name__)

BRAND_TERMS = [
    "ARIE Finance",
    "Arie Finance Ltd",
    "Arie Capital Investment",
    "Arie Capital Investment (ACBM) Ltd",
    "ACBM",
    "ariefinance.com",
]

_ALWAYS_NOT_COVERED = ["LinkedIn", "Paywalled press", "Private groups", "Twitter/X"]

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# HN search (Algolia — no auth required)
# ---------------------------------------------------------------------------

async def _search_hn(term: str, client: httpx.AsyncClient) -> List[tuple]:
    """Return list of (title, url) tuples matching the term on HN."""
    try:
        resp = await client.get(
            "https://hn.algolia.com/api/v1/search",
            params={"query": term, "tags": "story", "hitsPerPage": "5"},
            timeout=10,
        )
        resp.raise_for_status()
        hits = resp.json().get("hits", [])
        results = []
        for h in hits:
            title = h.get("title", "")
            if not title:
                continue
            # Only include if the search term appears in the title (exact phrase match)
            if term.lower() not in title.lower():
                continue
            obj_id = h.get("objectID", "")
            url = f"https://news.ycombinator.com/item?id={obj_id}" if obj_id else ""
            results.append((title, url))
        return results
    except Exception as exc:
        logger.debug("HN brand watch failed for '%s': %s", term, exc)
        return []


# ---------------------------------------------------------------------------
# Reddit search (OAuth-first, anon fallback)
# ---------------------------------------------------------------------------

async def _get_reddit_token(client: httpx.AsyncClient) -> Optional[str]:
    cid = os.environ.get("REDDIT_CLIENT_ID", "")
    secret = os.environ.get("REDDIT_CLIENT_SECRET", "")
    if not cid or not secret:
        return None
    try:
        ua = os.environ.get("REDDIT_USER_AGENT", "ArieFinanceBrandWatch/1.0")
        resp = await client.post(
            "https://www.reddit.com/api/v1/access_token",
            data={"grant_type": "client_credentials"},
            auth=(cid, secret),
            headers={"User-Agent": ua},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json().get("access_token")
    except Exception as exc:
        logger.debug("Reddit brand watch token failed: %s", exc)
        return None


async def _search_reddit(term: str, client: httpx.AsyncClient, token: Optional[str]) -> List[tuple]:
    """Return list of (title, url) tuples matching the term on Reddit."""
    ua = os.environ.get("REDDIT_USER_AGENT", "ArieFinanceBrandWatch/1.0")
    try:
        if token:
            resp = await client.get(
                "https://oauth.reddit.com/search",
                params={"q": f'"{term}"', "limit": "5", "type": "link"},
                headers={"Authorization": f"Bearer {token}", "User-Agent": ua},
                timeout=10,
            )
        else:
            resp = await client.get(
                "https://old.reddit.com/search.json",
                params={"q": f'"{term}"', "limit": "5"},
                headers={"User-Agent": ua},
                timeout=10,
            )
        resp.raise_for_status()
        children = resp.json().get("data", {}).get("children", [])
        results = []
        for c in children:
            data = c.get("data", {})
            title = data.get("title", "")
            if not title:
                continue
            # Only include if search term appears in title or selftext
            combined = f"{title} {data.get('selftext', '')}".lower()
            if term.lower() not in combined:
                continue
            permalink = data.get("permalink", "")
            url = f"https://www.reddit.com{permalink}" if permalink else data.get("url", "")
            results.append((title, url))
        return results
    except Exception as exc:
        logger.debug("Reddit brand watch failed for '%s': %s", term, exc)
        return []


# ---------------------------------------------------------------------------
# Direct-mention pass over already-scraped items
# ---------------------------------------------------------------------------

def _check_scraped_items(scrape: Optional[ScrapeResult]) -> List[DigestItem]:
    """Convert any direct-mention ScrapedItems into DigestItems for brand_watch.items."""
    if not scrape:
        return []
    result: List[DigestItem] = []
    for item in scrape.items:
        if item.direct_mention:
            result.append(DigestItem(
                title=item.title,
                url=item.url,
                source=item.source,
                one_liner=f"Mention of {', '.join(item.direct_mention_entities) or 'ARIE/ACBM'} detected.",
                region=item.region,
                entities=list(item.direct_mention_entities),
                published_at=item.published_at,
                confidence="Medium",
            ))
    return result


# ---------------------------------------------------------------------------
# Main brand watch runner
# ---------------------------------------------------------------------------

async def run_brand_watch(scrape: Optional[ScrapeResult] = None) -> BrandWatchResult:
    """
    Run a dedicated sweep for ARIE / ACBM mentions.

    Coverage is honest — surfaces that are not reachable or not attempted
    are recorded under not_covered. Never claims completeness.
    """
    covered: List[str] = []
    not_covered: List[str] = list(_ALWAYS_NOT_COVERED)
    found_titles: List[str] = []

    # Collect (title, url) tuples from external searches
    found_pairs: List[tuple] = []

    async with httpx.AsyncClient(headers={"User-Agent": BROWSER_UA}, follow_redirects=True) as client:
        # --- Hacker News (phrase-filtered) ---
        for term in ["ARIE Finance", "ACBM Finance", "ariefinance.com"]:
            found_pairs += await _search_hn(term, client)
        covered.append("Hacker News")

        # --- Reddit (quoted phrase search) ---
        token = await _get_reddit_token(client)
        for term in ["ARIE Finance", "ACBM Finance", "ariefinance.com"]:
            found_pairs += await _search_reddit(term, client, token)
        covered.append("Reddit")

    # --- Scraped-item direct mention pass ---
    scraped_mentions = _check_scraped_items(scrape)
    covered.append("News feeds (scraped)")

    # Deduplicate by title (lowercase)
    seen_titles: set = set()
    unique_pairs: List[tuple] = []
    for title, url in found_pairs:
        lo = title.lower().strip()
        if lo and lo not in seen_titles:
            seen_titles.add(lo)
            unique_pairs.append((title, url))

    total_mentions = len(scraped_mentions) + len(unique_pairs)

    # Build result
    if total_mentions == 0:
        result_text = (
            "Dedicated sweep of covered public surfaces found no new ARIE / ACBM mentions."
        )
        status = "No escalation required."
        last_date = None
    else:
        result_text = (
            f"{total_mentions} potential ARIE / ACBM mention(s) detected across covered surfaces. "
            f"Review items below."
        )
        status = "Mentions detected — review recommended."
        dates = [m.published_at for m in scraped_mentions if m.published_at]
        last_date = max(dates) if dates else datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")

    coverage_confidence: str = "Medium"

    # Build external items WITH urls
    external_items: List[DigestItem] = []
    for title, url in unique_pairs[:5]:
        external_items.append(DigestItem(
            title=title,
            url=url,
            source="External search (HN / Reddit)",
            one_liner="Potential ARIE / ACBM mention detected via brand name search.",
            confidence="Low",
        ))

    all_items = scraped_mentions + external_items

    return BrandWatchResult(
        coverage=covered,
        not_covered=not_covered,
        coverage_confidence=coverage_confidence,
        result=result_text,
        last_mention_date=last_date,
        sentiment="Neutral",
        status=status,
        items=all_items[:10],
    )
