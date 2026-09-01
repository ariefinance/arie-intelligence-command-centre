# Production Hardening Verification — ARIE Intelligence Command Centre

**Verification timestamp:** 2026-06-22 18:28 UTC (cache-bust token `ts=1782152875`)
**Verified by:** Claude Code (final merge-readiness verification)
**Method:** Cache-busted command-line fetches (`curl`) against the public production URL. No browser, no screenshots.

---

## 1. Branch & Commit
- **Branch:** `feat/source-health-pain-radar`
- **Commit:** `e043da2` (`harden: no-store headers, HTML-entity decode in titles, clearer source labels`)
- **Base of branch:** `command-centre-phase1 @ ad6fb54`; linear descendant of `main @ e2aa4d7`

## 2. Railway Deployment
- **Active deployment ID:** `1d723bcb-cef0-409a-829a-b4ab728bf13b`
- **Status:** ● Online (EU West)
- **Source:** direct `railway up` upload of `e043da2` (Railway shows no git SHA for direct uploads)
- **Rollback / revert since hardening deploy:** none (same deployment ID across all checks)

## 3. Cache-busted URLs used
- `/brief`  → `https://arie-intelligence-production.up.railway.app/brief?verify=e043da2-1782152875`
- `/digest.json` → `https://arie-intelligence-production.up.railway.app/digest.json?verify=e043da2-1782152875`

## 4. Response headers — `/brief`
```
HTTP/1.1 200 OK
Cache-Control: no-store, no-cache, must-revalidate, max-age=0
expires: 0
pragma: no-cache
```

## 5. Response headers — `/digest.json`
```
HTTP/1.1 200 OK
Cache-Control: no-store, no-cache, must-revalidate, max-age=0
expires: 0
pragma: no-cache
```

## 6. Acceptance evidence (from saved cache-busted responses)

| # | Check | Result | Evidence |
|---|---|---|---|
| 1 | `/brief` returns 200 | PASS | `HTTP/1.1 200 OK` |
| 2 | `/brief` no-cache headers | PASS | `Cache-Control: no-store, no-cache, must-revalidate, max-age=0` |
| 3 | `/digest.json` 200 + valid JSON | PASS | parsed OK; `source_health=34`; `generated_at=2026-06-22T18:11:42.974023+00:00` |
| 4 | `/digest.json` no-cache headers | PASS | `Cache-Control: no-store, no-cache, must-revalidate, max-age=0` |
| 5 | `Customer Experience Intelligence` appears | PASS | 2 occurrences in `/brief` |
| 6 | `Source Health` appears | PASS | present (`Source Health — 34 monitored feeds`) |
| 7 | `Evidence sources` appears | PASS | present (header chip + Evidence Appendix) |
| 8 | `monitored feeds` appears | PASS | present in Source Health summary |
| 9 | literal `&nbsp;&nbsp;` absent | PASS | grep count = 0 |
| 10 | `&amp;nbsp;` (double-escaped) absent | PASS | grep count = 0 |
| 11 | bare `>Sources:` absent | PASS | grep count = 0 |
| 12 | old `sources monitored` absent | PASS | grep count = 0 |
| 13 | `/digest.json` contains `source_health` | PASS | 34 entries |
| 14 | no banned outreach/poaching phrases | PASS | `target unhappy`, `contact complainant`, `contact this customer`, `poach`, `outreach to unhappy`, `reach out to this user` → all count = 0 |
| 15 | app healthy | PASS | `/health` → `{"status":"ok",...}` |

**Brief response size:** 56,314 bytes (fully-rendered hardened brief).

## 7. Tests
```
python -m pytest tests/ -q   → 41 passed
python -c "import models, scraper, enrich, intelligence, digest_renderer, main; print('IMPORTS_OK')"  → IMPORTS_OK
```

## 8. Conclusion
All 15 acceptance checks PASS against the cache-busted public production response. Production deployment `1d723bcb` serves the hardened build (`e043da2`). The previously-reported discrepancy was a pre-header-fix client-side browser cache, not a server state — confirmed here by direct cache-busted fetches. **Merge-ready.**
