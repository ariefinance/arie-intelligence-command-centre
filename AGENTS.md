# Arie Finance Market Intelligence Scraper (v2)

FastAPI service deployed on Railway. Scrapes financial/payments news, enriches items deterministically, runs a single Codex API call to generate a tiered intelligence digest (Opportunity Radar + topic sections), and returns it as a rendered HTML email body. An optional second Codex call (/posts) generates LinkedIn content on demand. Make.com calls /report on a schedule and handles email delivery.

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
| `scraper.py` | All scraping logic. RSS via feedparser, HTML via BeautifulSoup, Reddit via PRAW. Free Google News RSS strategy for FATF and other topic feeds |
| `enrich.py` | Deterministic (no-LLM) enrichment: topic/subtopic classification, region tagging, source weighting, watchlist detection, relevance scoring, near-dup dedup, cap at 40 items |
| `trend_memory.py` | JSON file persistence of last run's topics/opportunities for week-over-week trend flagging. Needs Railway Volume to survive redeploys |
| `intelligence.py` | Single Codex API call (`Codex-sonnet-4-6`, `max_tokens=16000`). Digest prompt → structured JSON → `Digest` model. Exports `_extract_json`, `MODEL`, `MAX_TOKENS` for use by `content.py` |
| `digest_renderer.py` | Renders `Digest` to inline-styled HTML email (navy/gold palette, 640px card) |
| `content.py` | Optional on-demand LinkedIn generation — second Codex call, gated to `/posts` endpoint only |
| `main.py` | FastAPI app. Five endpoints (see below) |
| `requirements.txt` | Python dependencies |
| `railway.toml` | Railway deployment config (nixpacks, uvicorn entry point) |
| `.env.example` | Required environment variables |

---

## Sources

| Source | Method | Notes |
|---|---|---|
| Reddit | PRAW | `r/fintech` + `r/paymentprocessing`. Post title + body only, no comments |
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

- `GET /health` — Railway liveness probe
- `GET /scrape` — scrape all sources + enrich, return JSON with enriched item list + per-source counts + failures
- `GET /digest` — canonical pipeline → HTML digest. Add `?format=json` for JSON response
- `GET /report` — back-compat alias for `/digest` (HTML). Make.com calls this
- `GET /report/json` — back-compat alias for `/digest?format=json`
- `GET /posts?count=N` — optional on-demand: full pipeline + second Codex call → JSON `{generated_at, posts:[{content, word_count}]}`. Default count=4, max=6. Takes ~4–5 min

No authentication on any endpoint.

---

## Environment Variables

```
ANTHROPIC_API_KEY=
REDDIT_CLIENT_ID=
REDDIT_CLIENT_SECRET=
REDDIT_USER_AGENT=
```

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

- **No database** — stateless scrape-on-demand; trend_memory.json is the only persistence (Railway Volume)
- **No authentication** on endpoints
- **Single Codex call per digest** — `generate_digest()` is one call. LinkedIn posts are a separate opt-in call via `/posts`
- **No Supabase, no Trustpilot scraping, no PYMNTS**
- **Reddit**: title + body only, no comment harvesting
- **All logic on Railway** — Make.com only handles scheduling and email delivery
- **Do not modify** `intelligence.py`, `digest_renderer.py`, `models.py`, or `main.py` unless a `scraper.py` or `enrich.py` change requires a new model field

---

## Integration: Make.com

Make.com calls `GET /report` on a weekly schedule and sends the returned HTML as an email body. `/report` is a back-compat alias for `/digest` and will continue to work.

---

## Known Quirks

- FATF and all Google News topic feeds require `agent=BROWSER_UA` passed to `feedparser.parse()` — Railway IPs get 0 results without it.
- Codex `max_tokens=16000` — raised from 8192 after 85-item prompts were truncated mid-JSON (manifested as "Unbalanced braces in Codex response").
- `/digest` and `/report` take ~3–4 minutes end-to-end. `/posts` takes ~4–5 min (two Codex calls). Make.com webhook timeout must be set accordingly.
