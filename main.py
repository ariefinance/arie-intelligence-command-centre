"""
Arie Finance Market Intelligence API (v2)
==========================================
FastAPI app deployable on Railway.

Pipeline: scrape_all() -> enrich() -> generate_digest()

Endpoints
---------
GET /health            — liveness probe
GET /scrape            — enriched scraped items + source counts as JSON
GET /refresh           — start background refresh, returns immediately (< 2 s)
GET /refresh/status    — poll refresh job state (idle/running/completed/failed)
GET /brief             — serve latest cached HTML digest instantly (< 2 s)
GET /digest.json       — serve latest cached digest as JSON instantly (< 2 s)
GET /digest            — canonical live pipeline → HTML (or ?format=json); kept for compat
GET /report            — back-compat alias for /brief (Make.com can switch to this)
GET /report/json       — back-compat alias for /digest.json
GET /posts?count=N     — optional on-demand content ideas (full pipeline + second Claude call)
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Arie Finance Market Intelligence API",
    description="Weekly scraper + Claude digest → HTML email / JSON",
    version="2.1.0",
)

# ---------------------------------------------------------------------------
# Background refresh state (in-process, reset on restart)
# ---------------------------------------------------------------------------

_refresh_job: dict = {
    "job_id": None,
    "status": "idle",       # idle | running | completed | failed
    "started_at": None,
    "completed_at": None,
    "error": None,
    "last_success_at": None,
}


async def _run_refresh_background(job_id: str) -> None:
    """
    Full pipeline coroutine. Runs on the event loop, completely independent of
    the originating HTTP request — client disconnect cannot cancel this task.
    """
    try:
        from scraper import scrape_all
        from enrich import enrich
        from intelligence import generate_digest
        from digest_renderer import render_digest_html
        import cache

        logger.info("Refresh %s: starting pipeline…", job_id)
        raw = await scrape_all()
        logger.info(
            "Refresh %s: scraped %d items | failures: %s | low_data: %s",
            job_id, len(raw.items), raw.source_failures or "none", raw.low_data_warning,
        )

        enriched = enrich(raw)
        logger.info("Refresh %s: enriched %d items (after dedup+cap)", job_id, len(enriched.items))

        digest = await generate_digest(enriched)
        logger.info(
            "Refresh %s: digest generated — %d signals | %d commercial | %d social | %d film",
            job_id,
            len(digest.top_signals),
            len(digest.commercial_opportunities),
            len(digest.social_pain_points),
            len(digest.film_watch),
        )

        html = render_digest_html(digest)
        cache.save_digest(digest.model_dump(), html)

        now = datetime.utcnow().isoformat() + "Z"
        _refresh_job.update({
            "status": "completed",
            "completed_at": now,
            "last_success_at": now,
            "error": None,
        })
        logger.info("Refresh %s: completed successfully.", job_id)

    except Exception as exc:
        logger.exception("Refresh %s: FAILED", job_id)
        _refresh_job.update({
            "status": "failed",
            "completed_at": datetime.utcnow().isoformat() + "Z",
            "error": str(exc),
        })


# ---------------------------------------------------------------------------
# "No brief yet" fallback HTML (returned by /brief and /report when no cache)
# ---------------------------------------------------------------------------

_NO_BRIEF_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Arie Finance — Intelligence Brief</title>
<style>
  body { margin: 0; padding: 0; background: #0b1a2e; font-family: Georgia, serif; }
  .card {
    max-width: 560px; margin: 80px auto; background: #122040;
    border: 1px solid #c9a84c; border-radius: 8px;
    padding: 48px 40px; text-align: center;
  }
  h1 { color: #c9a84c; font-size: 22px; margin: 0 0 16px; letter-spacing: 0.04em; }
  p  { color: #a8c0d8; font-size: 15px; line-height: 1.6; margin: 0 0 12px; }
  code { color: #c9a84c; font-family: monospace; font-size: 14px; }
</style>
</head>
<body>
<div class="card">
  <h1>ARIE FINANCE — INTELLIGENCE BRIEF</h1>
  <p>No brief has been generated yet.</p>
  <p>Call <code>GET /refresh</code> to run the pipeline and generate the first brief.</p>
  <p>This page will update automatically after the next refresh.</p>
</div>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health", tags=["ops"])
async def health():
    """Railway health check."""
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat() + "Z"}


@app.get("/scrape", tags=["data"])
async def scrape_endpoint():
    """
    Scrape all sources, run deterministic enrichment, and return enriched items
    + source counts as JSON. Useful for inspecting data quality before the full pipeline.
    """
    try:
        from scraper import scrape_all
        from enrich import enrich

        raw = await scrape_all()
        result = enrich(raw)
        return {
            "scraped_at": datetime.utcnow().isoformat() + "Z",
            "total": len(result.items),
            "source_counts": result.source_counts,
            "source_failures": result.source_failures,
            "low_data_warning": result.low_data_warning,
            "items": [item.model_dump() for item in result.items],
        }
    except Exception as exc:
        logger.exception("Scrape failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/refresh", tags=["intelligence"])
async def refresh_endpoint():
    """
    Start the full pipeline (scrape → enrich → generate_digest → render → cache)
    as a background task and return immediately (< 2 s).

    The pipeline continues even if the client disconnects. Poll GET /refresh/status
    to track progress. If a refresh is already running, returns the current job info.
    """
    if _refresh_job["status"] == "running":
        return JSONResponse(content={
            "status": "already_running",
            "job_id": _refresh_job["job_id"],
            "started_at": _refresh_job["started_at"],
            "message": "A refresh is already in progress. Poll GET /refresh/status.",
        })

    job_id = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    _refresh_job.update({
        "job_id": job_id,
        "status": "running",
        "started_at": datetime.utcnow().isoformat() + "Z",
        "completed_at": None,
        "error": None,
    })

    asyncio.create_task(_run_refresh_background(job_id))
    logger.info("Refresh %s: background task created.", job_id)

    return {
        "status": "started",
        "job_id": job_id,
        "message": "Refresh started. Poll GET /refresh/status for progress.",
    }


@app.get("/refresh/status", tags=["intelligence"])
async def refresh_status_endpoint():
    """
    Return the current state of the background refresh job.

    status values:
      idle      — no refresh has been triggered since last restart
      running   — pipeline is in progress
      completed — last refresh succeeded
      failed    — last refresh raised an exception (see "error" field)
    """
    import cache
    return {
        "job_id": _refresh_job["job_id"],
        "status": _refresh_job["status"],
        "started_at": _refresh_job["started_at"],
        "completed_at": _refresh_job["completed_at"],
        "error": _refresh_job["error"],
        "last_success_at": _refresh_job["last_success_at"],
        "latest_digest_available": cache.cache_exists(),
    }


# No-store headers so browsers/proxies never serve a stale management brief.
# Without these, /brief has no cache directives and browsers apply heuristic
# caching — re-showing a pre-refresh brief and making a fresh refresh look like
# it failed.
_NO_CACHE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}

# Build marker — bump on meaningful builds so /verification/status proves which
# code is actually running (Railway direct-upload deploys have no git SHA).
APP_VERSION = "cx-gate-hardened-2026-06-23"


def _current_brief_html() -> str:
    """
    The EXACT HTML body that GET /brief serves (cached digest HTML, or the
    placeholder when no digest is cached).

    Single source of truth shared by /brief and /verification/status so the two
    endpoints can never disagree about what is actually served.
    """
    import cache
    cached = cache.load_digest()
    if cached is None:
        return _NO_BRIEF_HTML
    _, html = cached
    return html


@app.get("/brief", response_class=HTMLResponse, tags=["intelligence"])
async def brief_endpoint():
    """
    Return the latest cached HTML digest instantly (< 2 s). Does NOT scrape or
    call Claude. Call /refresh first to generate a brief.

    If no brief has been generated yet, returns a clear placeholder page.
    """
    return HTMLResponse(content=_current_brief_html(), headers=_NO_CACHE_HEADERS)


@app.get("/digest.json", tags=["intelligence"])
async def digest_json_endpoint():
    """
    Return the latest cached digest as JSON instantly (< 2 s). Does NOT scrape
    or call Claude. Call /refresh first to generate a brief.
    """
    import cache
    cached = cache.load_digest()
    if cached is None:
        raise HTTPException(
            status_code=404,
            detail="No digest cached yet. Call GET /refresh to generate one.",
        )
    digest_dict, _ = cached
    return JSONResponse(content=digest_dict, headers=_NO_CACHE_HEADERS)


@app.get("/verification/status", tags=["ops"])
async def verification_status():
    """
    Read-only build/state proof. Lets anyone confirm — independent of browser or
    CDN cache — which code is running and what the latest brief contains.

    No secrets, no private data, no source content: only booleans, counts, the
    digest timestamp, and a build marker.
    """
    import re as _re
    import inspect as _inspect
    import hashlib as _hashlib
    import cache
    import enrich
    import scraper
    import digest_renderer

    # --- code capabilities (introspected at runtime) ---
    cx_enabled = hasattr(digest_renderer, "_render_customer_experience")
    sh_enabled = hasattr(scraper, "build_source_health") and hasattr(
        digest_renderer, "_render_source_health"
    )
    gate_present = hasattr(enrich, "is_cx_relevant")
    # Prove the gate is actually USED inside the CX renderer (not just defined).
    gate_used = False
    if cx_enabled and gate_present:
        try:
            src = _inspect.getsource(digest_renderer._render_customer_experience)
            gate_used = "is_cx_relevant" in src
        except Exception:
            gate_used = False
    headers_enabled = "_NO_CACHE_HEADERS" in globals()

    # --- digest-data state ---
    generated_at = None
    has_source_health = False
    pain_count = 0
    cached = cache.load_digest()
    if cached:
        digest_dict, _ = cached
        generated_at = digest_dict.get("generated_at")
        has_source_health = bool(digest_dict.get("source_health"))
        pain_count = len(digest_dict.get("customer_pain", []) or [])

    # --- scan the EXACT HTML that /brief serves (single source of truth) ---
    brief_html = _current_brief_html()
    brief_encoded = brief_html.encode("utf-8")
    brief_sha256 = _hashlib.sha256(brief_encoded).hexdigest()
    low = brief_html.lower()

    bad_from_brief = {
        "agentic_ai": "agentic ai" in low,
        "everyday_hardware": "everyday hardware" in low,
        "profitable_frontier": "profitable frontier" in low,
        # Visible artifact = the double-escaped form. Intentional "&nbsp;"
        # separators render as spaces and are NOT flagged.
        "nbsp": "&amp;nbsp;" in brief_html,
        "amp_nbsp": "&amp;nbsp;" in brief_html,
    }
    evidence_sources = "evidence sources" in low
    monitored_feeds = "monitored feeds" in low
    bare_sources = bool(_re.search(r"(?i)(?<!evidence )sources:", brief_html))

    # Legacy block — derived from the SAME brief HTML, so it cannot diverge.
    bad_terms_present = {
        "agentic_ai": bad_from_brief["agentic_ai"],
        "everyday_hardware": bad_from_brief["everyday_hardware"],
        "nbsp": bad_from_brief["nbsp"],
    }
    # Consistent by construction (both blocks read brief_html); the flag makes
    # that guarantee explicit and machine-checkable.
    consistent = all(
        bad_terms_present[k] == bad_from_brief[k]
        for k in ("agentic_ai", "everyday_hardware", "nbsp")
    )

    payload = {
        "app_version": APP_VERSION,
        "generated_at": generated_at,
        "customer_experience_enabled": cx_enabled,
        "source_health_enabled": sh_enabled,
        "cx_relevance_gate_enabled": bool(gate_present and gate_used),
        "hardening_headers_enabled": headers_enabled,
        "latest_digest_has_source_health": has_source_health,
        "latest_digest_customer_pain_count": pain_count,
        "brief_html_checked": True,
        "brief_bytes": len(brief_encoded),
        "brief_sha256": brief_sha256,
        "bad_terms_present": bad_terms_present,
        "bad_terms_present_from_brief_html": bad_from_brief,
        "verification_consistent_with_brief": consistent,
        "labels": {
            "evidence_sources_present": evidence_sources,
            "monitored_feeds_present": monitored_feeds,
            "bare_sources_present": bare_sources,
        },
    }
    return JSONResponse(content=payload, headers=_NO_CACHE_HEADERS)


@app.get("/digest", tags=["intelligence"])
async def digest_endpoint(format: str = Query(default="html", description="html | json")):
    """
    Canonical full pipeline: scrape → enrich → generate_digest → render.

    Returns HTML by default. Pass ?format=json to receive the structured Digest as JSON.
    Takes ~3–4 minutes end-to-end (single Claude call).

    Prefer /refresh + /brief for Make.com integration — no timeout risk.
    """
    try:
        from scraper import scrape_all
        from enrich import enrich
        from intelligence import generate_digest
        from digest_renderer import render_digest_html

        logger.info("Starting digest pipeline…")
        raw = await scrape_all()
        logger.info(
            "Scraped %d items | failures: %s | low_data: %s",
            len(raw.items), raw.source_failures or "none", raw.low_data_warning,
        )

        enriched = enrich(raw)
        logger.info("Enriched: %d items (after dedup+cap)", len(enriched.items))

        digest = await generate_digest(enriched)
        logger.info(
            "Digest generated: %d signals | %d commercial | %d social | %d film",
            len(digest.top_signals), len(digest.commercial_opportunities),
            len(digest.social_pain_points), len(digest.film_watch),
        )

        if format == "json":
            return JSONResponse(content=digest.model_dump())

        html = render_digest_html(digest)
        return HTMLResponse(content=html)
    except EnvironmentError as exc:
        raise HTTPException(status_code=500, detail=f"Config error: {exc}") from exc
    except Exception as exc:
        logger.exception("Digest pipeline failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/report", response_class=HTMLResponse, tags=["intelligence"])
async def report_html():
    """
    Back-compat alias for GET /brief (cached HTML).
    Returns the latest cached digest without triggering a live pipeline run.
    Make.com should switch to calling /refresh on schedule, and /brief to view.
    """
    return await brief_endpoint()


@app.get("/report/json", tags=["intelligence"])
async def report_json():
    """
    Back-compat alias for GET /digest.json (cached JSON).
    Returns the latest cached digest as JSON without triggering a live pipeline run.
    """
    return await digest_json_endpoint()


@app.get("/posts", tags=["content"])
async def posts_endpoint(count: int = Query(default=5, ge=1, le=8)):
    """
    Optional on-demand source-backed content ideas.

    Runs the FULL pipeline (scrape → enrich → generate_digest) then makes a
    SECOND Claude call to generate content ideas grounded in the digest.
    Each idea is an angle/hook tied to a specific verified source URL — NOT a
    finished post.

    WARNING: This endpoint runs ~4–5 minutes and consumes two Claude API calls.
    Use sparingly. Default: 5 ideas. Max: 8.
    """
    try:
        from scraper import scrape_all
        from enrich import enrich
        from intelligence import generate_digest
        from content import generate_content_ideas

        logger.info("Starting /posts pipeline (count=%d)…", count)
        raw = await scrape_all()
        enriched = enrich(raw)
        digest = await generate_digest(enriched)

        ideas = await generate_content_ideas(digest, count=count)
        return {
            "generated_at": datetime.utcnow().isoformat() + "Z",
            "ideas": [idea.model_dump() for idea in ideas],
        }
    except EnvironmentError as exc:
        raise HTTPException(status_code=500, detail=f"Config error: {exc}") from exc
    except Exception as exc:
        logger.exception("/posts pipeline failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
