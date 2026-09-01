"""
Optional on-demand content idea generation for Arie Finance Market Intelligence (v2).

Produces source-backed CONTENT IDEAS (angles/hooks) grounded in digest items —
NOT finished posts. Each idea is tied to a specific, verifiable digest source URL.

Gated to the /posts endpoint only. Never called from the main digest pipeline.

Exported function
-----------------
    generate_content_ideas(digest: Digest, count: int = 5) -> List[ContentIdea]
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict, List

import anthropic

from intelligence import MODEL, MAX_TOKENS, _extract_json
from models import ContentIdea, Digest

logger = logging.getLogger(__name__)

# Tone injected into the content ideas prompt
_ARIE_TONE = (
    "Arie Finance Ltd is a licensed Payment Intermediary regulated by FSC Mauritius. "
    "Tone: institutional, professional, regulated. "
    "Never position Arie as a bank. "
    "Never promise onboarding outcomes, FX pricing, or investment returns. "
    "Each idea MUST be grounded in a specific item from the digest provided — "
    "zero fabrication. The source_url and source_title MUST be copied verbatim "
    "from the digest input; never invent or paraphrase a URL or title."
)


async def generate_content_ideas(digest: Digest, count: int = 5) -> List[ContentIdea]:
    """
    Generate `count` source-backed content ideas from the digest's opportunity radar
    and top section items. Uses a single Claude call (MODEL / MAX_TOKENS).

    Each idea is an angle/hook (NOT a finished post) grounded in ONE specific digest
    item, with the exact source_title and source_url from the input.

    Returns List[ContentIdea]. Raises EnvironmentError if ANTHROPIC_API_KEY is not set.
    Raises ValueError if Claude response is truncated.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise EnvironmentError("ANTHROPIC_API_KEY environment variable is not set")

    count = max(1, min(count, 8))

    # Build compact digest input — top_signals + commercial_opportunities + social_pain_points
    radar_lines: List[str] = []
    for r in (digest.top_signals or [])[:8]:
        radar_lines.append(
            f"- OPPORTUNITY: {r.opportunity} | CONFIDENCE: {r.confidence} | "
            f"SOURCE_TITLE: {r.source_title} | SOURCE_URL: {r.source_url}"
        )

    # commercial_opportunities + regulatory_watch + social_pain_points (top items)
    section_lines: List[str] = []
    for item in (digest.commercial_opportunities or [])[:8]:
        section_lines.append(
            f"- [Commercial] TITLE: {item.title} | ONE_LINER: {item.one_liner} "
            f"| SOURCE: {item.source} | URL: {item.url}"
        )
    for item in (digest.regulatory_watch or [])[:4]:
        section_lines.append(
            f"- [Regulatory] TITLE: {item.title} | ONE_LINER: {item.one_liner} "
            f"| SOURCE: {item.source} | URL: {item.url}"
        )
    for pp in (digest.social_pain_points or [])[:3]:
        section_lines.append(
            f"- [Community] PAIN_POINT: {pp.pain_point} | ANGLE: {pp.content_angle} "
            f"| SOURCE: {pp.source} | URL: {pp.source_url}"
        )

    digest_summary = (
        "TOP SIGNALS:\n" + "\n".join(radar_lines or ["(none)"]) + "\n\n"
        "COMMERCIAL & REGULATORY ITEMS:\n" + "\n".join(section_lines[:15] or ["(none)"])
    )

    user_content = (
        f"Produce exactly {count} content ideas for Arie Finance Ltd, each grounded in ONE "
        f"specific item from the digest below. {_ARIE_TONE}\n\n"
        f"DIGEST:\n{digest_summary}\n\n"
        f"REQUIREMENTS FOR EACH IDEA:\n"
        f"- angle: one or two sentences describing the hook/angle — NOT a finished post.\n"
        f"- based_on: what insight or opportunity the idea draws from (summarise briefly).\n"
        f"- format: one of 'LinkedIn post', 'Article', 'Introducer talking point', "
        f"'Newsletter blurb'.\n"
        f"- source_title: copy EXACTLY from the digest input — do not paraphrase.\n"
        f"- source_url: copy EXACTLY from the digest input — do not invent.\n"
        f"- audience: one of 'Introducers', 'SME founders', 'Both', or ''.\n"
        f"If you cannot ground an idea in a provided source, skip it.\n\n"
        f"Return ONLY a raw JSON object — no markdown fences, no preamble:\n"
        f'{{"content_ideas": [{{"angle": "...", "based_on": "...", "format": "...", '
        f'"source_title": "...", "source_url": "...", "audience": "..."}}]}}'
    )

    system_prompt = (
        "You are a senior content strategist for Arie Finance Ltd, a licensed Payment "
        "Intermediary regulated by FSC Mauritius. Your task is to generate content IDEAS "
        "(angles and hooks) grounded in real market intelligence — not finished posts. "
        "Every idea must cite a real source from the digest with its exact URL and title. "
        "Never fabricate facts, company names, URLs, or statistics. "
        "Return ONLY the raw JSON object specified — nothing else."
    )

    client = anthropic.Anthropic(api_key=api_key)
    logger.info("Calling Claude %s for %d content ideas…", MODEL, count)
    response = await asyncio.to_thread(
        client.messages.create,
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=system_prompt,
        messages=[{"role": "user", "content": user_content}],
    )

    raw = response.content[0].text
    logger.info(
        "Claude content ideas response: %d chars, stop_reason=%s", len(raw), response.stop_reason
    )
    if response.stop_reason == "max_tokens":
        raise ValueError(
            f"Claude content ideas response truncated at {MAX_TOKENS} tokens — increase MAX_TOKENS"
        )

    data = _extract_json(raw)
    ideas: List[ContentIdea] = []

    # Build known URL set for grounding check — all v2 fields with source URLs
    known_urls = {r.source_url for r in (digest.top_signals or []) if r.source_url}
    for item in digest.commercial_opportunities:
        if item.url:
            known_urls.add(item.url)
    for item in digest.regulatory_watch:
        if item.url:
            known_urls.add(item.url)
    for item in digest.film_watch:
        if item.source_url:
            known_urls.add(item.source_url)
    for pp in digest.social_pain_points:
        if pp.source_url:
            known_urls.add(pp.source_url)

    for raw_idea in data.get("content_ideas", []):
        if not isinstance(raw_idea, Dict):
            continue
        src_url = str(raw_idea.get("source_url", "")).strip()
        src_title = str(raw_idea.get("source_title", "")).strip()
        angle = str(raw_idea.get("angle", "")).strip()

        if not src_url or not angle:
            logger.warning("Content idea missing source_url or angle — skipped")
            continue
        if src_url not in known_urls:
            logger.warning(
                "Content idea source_url not in digest input (possible hallucination) — skipped: %s",
                src_url,
            )
            continue

        ideas.append(ContentIdea(
            angle=angle,
            based_on=str(raw_idea.get("based_on", "")).strip(),
            format=str(raw_idea.get("format", "LinkedIn post")).strip(),
            source_title=src_title,
            source_url=src_url,
            audience=str(raw_idea.get("audience", "")).strip(),
        ))

    logger.info("Generated %d content ideas", len(ideas))
    return ideas
