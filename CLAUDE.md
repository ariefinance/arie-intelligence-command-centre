# ARIE Intelligence Command Centre (Phase 1 — authoritative branch: main)

FastAPI service deployed on Railway. Scrapes financial/payments news, enriches items deterministically, runs Brand Watch, computes theme trajectories, and calls Claude to generate a decision-intelligence digest. Renders as a premium management HTML page at `/brief`. An optional second Claude call (`/posts`) generates LinkedIn content on demand.

**Current state**: Phase 1 delivered and consolidated into `main` (the single authoritative branch). All prior feature/UI branches have been folded in; the pre-reconstruction baseline is preserved under the annotated tag `pre-command-centre-rebuild`.

**Phase 2 features deferred** (do not implement):
- Notion outcome register
- `/ask` endpoint or search box
- Outcome tracking / SignalRecord model
- Lane B agentic rewrite
- New source expansion beyond current set
- LinkedIn scraping
- Google News redirect decoder

---

## Architecture

```
scrape_all()   →   enrich()   →   generate_digest()   →   render_digest_html()
(scraper.py)       (enrich.py)    (intelligence.py)        (digest_renderer.py)
                                          ↓ optional
                              generate_linkedin_posts()  ← /posts only
                                     (content.py)
```

### Files

| File | Purpose |
|---|---|
| `models.py` | Pydantic models: `ScrapedItem`, `ScrapeResult`, `LinkedInPost`, and all Digest models (`Digest`, `OpportunityRadarItem`, `DigestSection`, `DigestItem`) |
| `config.py` | Taxonomy keywords, watchlist entities, source weights, region keywords, enrichment constants |
| `scraper.py` | All scraping logic. RSS via feedparser, HTML via BeautifulSoup, Reddit via application-only OAuth (httpx) with anonymous RSS fallback. Free Google News RSS strategy for FATF and other topic feeds |
| `enrich.py` | Deterministic (no-LLM) enrichment: topic/subtopic classification, region tagging, source weighting, watchlist detection, relevance scoring, near-dup dedup, cap at 40 items |
| `trend_memory.py` | JSON file persistence of last run's topics/opportunities for week-over-week trend flagging. Needs Railway Volume to survive redeploys |
| `intelligence.py` | Single Claude API call (`claude-sonnet-4-6`, `max_tokens=16000`). Digest prompt → structured JSON → `Digest` model. Exports `_extract_json`, `MODEL`, `MAX_TOKENS` for use by `content.py` |
| `digest_renderer.py` | Renders `Digest` to inline-styled HTML email (navy/gold palette, 640px card) |
| `content.py` | Optional on-demand LinkedIn generation — second Claude call, gated to `/posts` endpoint only |
| `main.py` | FastAPI app. Five endpoints (see below) |
| `requirements.txt` | Python dependencies |
| `railway.toml` | Railway deployment config (nixpacks, uvicorn entry point) |
| `.env.example` | Required environment variables |

---

## Sources

| Source | Method | Notes |
|---|---|---|
| Reddit | OAuth-first (`httpx`), anonymous RSS fallback | Five subreddits: `r/fintech`, `r/smallbusiness`, `r/Entrepreneur`, `r/banking`, `r/paymentprocessing`. Uses application-only OAuth (60 req/min) when `REDDIT_CLIENT_ID` + `REDDIT_CLIENT_SECRET` are set — reliable on Railway IPs. Falls back to hardened old.reddit.com RSS (5s spacing, 10s 429 backoff + single retry) when creds are absent. The Community section is unreliable in production without OAuth creds. Post title + body only, no comments. |
| Finextra | RSS (`feedparser`) | Standard RSS, no special headers needed |
| FATF Publications | RSS via Google News (`feedparser`) | **Requires browser User-Agent** — `agent=BROWSER_UA` must be passed to `feedparser.parse()` |
| The Paypers | HTML scrape (`requests` + `BeautifulSoup`) | Parses article list page |
| FCA | RSS-first, HTML fallback | `_try_fca_rss()` tries FCA RSS; falls back to HTML scrape if 0 items |
| Google News topic feeds | RSS via `feedparser` | Free strategy for FATF, stablecoins, MiCA, AMLA, FSC Mauritius, AML/Fraud, Corridors, Film Incentives, Mauritius Film — all require browser User-Agent |

---

## Enrichment pipeline (enrich.py)

For each item: classify topic + subtopic → tag region → set source_weight → detect watchlist hits → compute relevance_score → demote operational noise → deduplicate near-duplicate titles (Jaccard > 0.70) → sort desc → cap at 40 items.

Two-level taxonomy: five topic streams (`market_opportunities`, `regulation_compliance`, `cross_border_corridors`, `payments_treasury`, `fintech_infrastructure`), with `market_opportunities` having five subtopics (`film_production`, `global_expansion`, `mauritius_investment`, `treasury_pain_points`, `new_market_entrants`).

---

## Digest output (intelligence.py / digest_renderer.py)

### Tier 1 — Opportunity Radar
6–10 items with the strongest commercial signal for Arie Finance. Each item: `opportunity`, `confidence` (High/Medium/Low), `why_it_matters`, `suggested_action`, `entities`, `source_title`, `source_url`. Rendered as the hero section in the HTML digest.

### Tier 2 — Topic sections
Remaining relevant items organised by section: Market Opportunities, Cross-Border Corridors, Regulation & Compliance, Payments & Treasury, Fintech & Infrastructure. Each item carries a `trend_flag` (`new` / `continuing` / `""`) derived from trend_memory.

### Watchlist activity
Named entities (Wise, Airwallex, Netflix, etc.) matched in items are summarised in a callout box.

---

## Endpoints

| Endpoint | Latency | Purpose |
|---|---|---|
| `GET /health` | instant | Railway liveness probe |
| `GET /scrape` | ~2 min | scrape + enrich, return JSON (no Claude call) |
| `GET /refresh` | ~3–4 min | full pipeline → stores HTML + JSON to disk cache |
| `GET /brief` | **< 2 s** | return cached HTML digest — **management uses this** |
| `GET /digest.json` | **< 2 s** | return cached digest as JSON |
| `GET /digest` | ~3–4 min | live pipeline (kept for compat; use `/refresh` + `/brief` instead) |
| `GET /report` | **< 2 s** | alias for `/brief` (cached HTML) |
| `GET /report/json` | **< 2 s** | alias for `/digest.json` (cached JSON) |
| `GET /posts?count=N` | ~4–5 min | full pipeline + second Claude call → content ideas JSON |

No authentication on any endpoint.

**Make.com integration pattern:**
- Scheduler → `GET /refresh` (weekly, no response body needed — just trigger it)
- Email step → `GET /brief` to fetch the cached HTML immediately after refresh completes
  (or wait for `/refresh` to complete and use its JSON response to confirm success)

---

## Environment Variables

```
ANTHROPIC_API_KEY=          # Required
REDDIT_CLIENT_ID=           # Optional — Reddit app client ID (see https://www.reddit.com/prefs/apps)
REDDIT_CLIENT_SECRET=       # Optional — Reddit app client secret
REDDIT_USER_AGENT=          # Optional — defaults to built-in ArieFinance UA
```

`REDDIT_CLIENT_ID` + `REDDIT_CLIENT_SECRET` are **optional but strongly recommended in production**.
When present, Reddit is fetched via application-only OAuth (60 req/min) — reliable on Railway
datacenter IPs. Without them, a hardened anonymous RSS fallback runs (5s spacing between subs,
10s wait + single retry on 429), but Railway IPs are often rate-limited after the first request,
leading to an empty Community & Customer Conversations section.

Set in Railway service → Variables. Never commit `.env`.

---

## Deployment

Hosted on Railway. To redeploy after code changes:

```bash
railway up --service cfe352dc-a300-4f5f-a0b1-f3aaeaa51e45 --detach
```

Railway project ID: `de664eb0-0bc6-47b7-aea2-b87ef7cc9480`
Service URL: set as Railway public domain (configured in Railway dashboard).

**trend_memory.json** — mount a Railway Volume at the path `./trend_memory.json` to persist week-over-week trend data across deploys. Without a volume, trend flagging resets on each deploy (safe — just loses the "continuing" signal).

---

## v2 Constraints

- **No database** — stateless scrape-on-demand; `trend_memory.json`, `latest_digest.json`, and `latest_digest.html` are the only persistence (Railway Volume)
- **No authentication** on endpoints
- **Single Claude call per digest** — `generate_digest()` is one call. LinkedIn posts are a separate opt-in call via `/posts`
- **No Supabase, no Trustpilot scraping, no PYMNTS**
- **Reddit**: title + body only, no comment harvesting
- **All logic on Railway** — Make.com only handles scheduling and email delivery
- **Do not modify** `intelligence.py`, `digest_renderer.py`, `models.py`, or `main.py` unless a `scraper.py` or `enrich.py` change requires a new model field
- **Cache files** (`latest_digest.json`, `latest_digest.html`) live in the working directory alongside `trend_memory.json` — mount the Railway Volume at the same path to survive redeploys

---

## Integration: Make.com

**Updated pattern (v2.1):**
1. Weekly scheduler → `GET /refresh` (runs the full pipeline, stores to cache)
2. After `/refresh` completes → `GET /brief` (fetches cached HTML instantly — no timeout risk)
3. Email step sends the HTML from `/brief` as the email body

`/report` is now an alias for `/brief` (cached HTML). Any existing Make.com scenario using `/report` will continue to work and will now be fast (< 2 s) instead of triggering a live pipeline run.

---

## Known Quirks

- FATF and all Google News topic feeds require `agent=BROWSER_UA` passed to `feedparser.parse()` — Railway IPs get 0 results without it.
- Reddit Community section reliability depends on OAuth creds (`REDDIT_CLIENT_ID` + `REDDIT_CLIENT_SECRET`) being set in Railway. Anonymous RSS fallback frequently 429s on datacenter IPs, yielding 0 items. Set these in Railway → Variables before going live.
- Claude `max_tokens=16000` — raised from 8192 after 85-item prompts were truncated mid-JSON (manifested as "Unbalanced braces in Claude response").
- `/digest` (live) takes ~3–4 minutes end-to-end. Use `/refresh` + `/brief` instead to avoid proxy timeouts.
- `/refresh` takes ~3–4 min. Make.com's webhook timeout for the `/refresh` call must be set accordingly (or use a fire-and-forget HTTP module).
- `/brief`, `/report`, `/digest.json` serve from cache and respond in < 2 s — no timeout risk.
- `/posts` takes ~4–5 min (two Claude calls). Use sparingly.

---

## Phase 1 — Command Centre (consolidated into `main`)

### Brand Watch (brand_watch.py)
- Dedicated sweep: HN Algolia API + Reddit search + direct-mention pass over scraped items
- NEVER claims completeness. Always shows: covered surfaces / not covered surfaces / coverage confidence
- Honest zero-mention state: "Dedicated sweep of covered public surfaces found no new ARIE / ACBM mentions."
- `coverage_confidence`: High / Medium / Limited — Medium is standard (3 surface types covered)
- Do NOT add surfaces you haven't tested — record them under `not_covered` instead

### Decision-First Cards (What Matters This Week)
- 3–5 cards from `top_signals`, rendered as decision-intelligence cards
- Each card: Finding (what_happened) / Why it matters / Suggested action / Owner / Confidence / Evidence
- `suggested_owner` must be one of: `BD | Ops | Compliance | Management | No action required`
- Claude generates `suggested_owner`; validated against allowed set in `_assemble_digest`
- No article-title-led cards — use `what_happened` as the card headline, not the raw feed title

### Trajectory Watch (trend_memory.py)
- Tracks 12 fixed themes (see `TRACKED_THEMES` dict in trend_memory.py)
- `count_themes_in_items()` counts keyword matches across all scraped items
- `save_theme_counts()` appends to `theme_history` in trend_memory.json (max 12 runs)
- `compute_trajectories()` returns 3–5 ThemeTrajectory objects, sorted: up → flat → down
- Direction: up if current > prev * 1.25; down if current < prev * 0.75; flat otherwise
- Gracefully degrades: on first run, shows "First observation this run"
- Uses Railway Volume (same path as trend_memory.json) — survives redeploys

### Layout (digest_renderer.py)
- New section order: Header → Executive Snapshot → Brand Watch → What Matters This Week → Trajectory Watch → Additional Intelligence → Evidence Appendix → Footer
- "Additional Intelligence" replaces all prior named sections — dynamic subsections only when content exists
- Subsections: Mauritius / Competitor / Customer Pain / Introducer / Film / Regulatory
- No empty sections, no blank cards
- Regulatory section: recent items (≤ 14 days) in main view; older items as "Background Context"
- Premium ARIE palette: navy #0B1E3E / gold #C9A84C / ivory/white backgrounds
