"""
Run-to-run continuity helper for Arie Finance Market Intelligence Digest.

Persists per-run topic counts + opportunities to a local JSON file so subsequent
runs can flag items as "new", "recurring", "accelerating", or "fading".

PRODUCTION NOTE: Railway's filesystem is ephemeral across deploys. Mount a
Railway Volume at the same path as TREND_MEMORY_PATH to make persistence
survive redeploys. Without a volume, trend flagging resets on each deploy
(safe — just loses the momentum signal).

History ring
------------
Retains up to _MAX_HISTORY past runs (12 weeks by default for weekly cadence).
Each run entry stores:
    {
        "generated_at": "YYYY-MM-DD...",
        "topics": {"topic_key": count, ...},          # per-topic item counts this run
        "opportunities": ["opportunity string", ...]
    }

Momentum labels
---------------
Computed by compute_momentum() over the 12-run history:

    "new"          — topic key absent from ALL prior runs in history.
    "accelerating" — frequency (item count) is strictly increasing over the
                     most recent 3 runs (run[-1] > run[-2] > run[-3]).
    "recurring"    — present in ≥4 of the last 12 runs, not accelerating/fading.
    "fading"       — was present in at least one of runs [-4..-2] but absent
                     (count == 0) in the most recent 2 runs.
    ""             — default / not enough history to classify confidently.

Thresholds are intentionally lenient to handle sparse weekly data.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional  # noqa: F401 — Optional used in save_run signature

logger = logging.getLogger(__name__)

TREND_MEMORY_PATH = Path("./trend_memory.json")

# ---------------------------------------------------------------------------
# Phase 1 — Theme trajectory tracking
# ---------------------------------------------------------------------------

# Themes to track for Trajectory Watch.  Display name → detection keywords (lowercase).
TRACKED_THEMES: Dict[str, List[str]] = {
    "ARIE / ACBM mentions":          ["arie finance", "acbm", "ariefinance.com"],
    "Mauritius FSC activity":         ["fsc mauritius", "fsc licence", "fsc license", "financial services commission", "fsc regulated"],
    "Mauritius Budget / policy":      ["mauritius budget", "mauritius government", "ministry of finance mauritius", "mauritius policy", "mauritius tax"],
    "Wise complaints":                ["wise complaint", "wise account frozen", "wise blocked", "wise banned", "wise issue"],
    "Revolut complaints":             ["revolut complaint", "revolut frozen", "revolut blocked", "revolut banned", "revolut issue"],
    "Airwallex activity":             ["airwallex"],
    "Customer onboarding pain":       ["account opening", "onboarding delay", "kyc delay", "refused account", "account rejected"],
    "Account freezes / debanking":    ["account frozen", "debanked", "account closed", "account suspended", "debanking"],
    "Cross-border payment delays":    ["cross-border payment", "international transfer delay", "swift delay", "payment stuck", "remittance delay"],
    "Introducer / CSP activity":      ["management company", "csp acquisition", "fiduciary", "trust administrator", "company secretary"],
    "Film / production incentives":   ["film rebate", "production incentive", "mauritius film", "screen incentive", "film production fund"],
    "Stablecoin / Africa corridors":  ["stablecoin africa", "africa corridor", "africa payment", "usdc africa", "africa remittance", "africa fintech"],
}

# Number of historical runs to retain (ring buffer).
# 12 runs ≈ 3 months at weekly cadence — enough to identify multi-week trends.
_MAX_HISTORY = 12


def load_previous_topics() -> Dict[str, Any]:
    """
    Return the last run's record: {"topics": {...}, "opportunities": [...],
    "generated_at": "..."} or {} if file missing/unreadable.

    "topics" is now a dict mapping topic_key -> count (int). Callers that
    previously expected a list should use list(topics.keys()).
    Callers should treat the result defensively — any key may be absent.
    """
    try:
        if not TREND_MEMORY_PATH.exists():
            return {}
        raw = TREND_MEMORY_PATH.read_text(encoding="utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict):
            return {}
        return {
            "topics": data.get("topics", {}),
            "opportunities": data.get("opportunities", []),
            "generated_at": data.get("generated_at", ""),
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("trend_memory: could not load previous topics: %s", exc)
        return {}


def load_previous_counts() -> Dict[str, Any]:
    """
    Return previous run's entity_counts and watchlist_topic_counts.
    Used by intelligence.py to compute week-over-week direction for v3 sections.
    Returns {"entity_counts": {...}, "watchlist_topic_counts": {...}} or empty dicts.
    """
    try:
        if not TREND_MEMORY_PATH.exists():
            return {"entity_counts": {}, "watchlist_topic_counts": {}}
        data = json.loads(TREND_MEMORY_PATH.read_text(encoding="utf-8"))
        return {
            "entity_counts": data.get("entity_counts", {}),
            "watchlist_topic_counts": data.get("watchlist_topic_counts", {}),
        }
    except Exception as exc:
        logger.warning("trend_memory: could not load previous counts: %s", exc)
        return {"entity_counts": {}, "watchlist_topic_counts": {}}


def save_run(
    topics: Any,  # Dict[str, int] preferred; List[str] also accepted for backward compat
    opportunities: List[str],
    generated_at: str,
    entity_counts: Optional[Dict[str, int]] = None,
    watchlist_topic_counts: Optional[Dict[str, int]] = None,
) -> None:
    """
    Persist the current run for next-week trend flagging.

    Parameters
    ----------
    topics:
        Preferred: Dict mapping topic_key -> item count for this run.
            E.g. {"regulation_compliance": 8, "film_production": 3, ...}
        Also accepted (backward compat): List[str] of topic keys — each gets count=1.
    opportunities:
        List of opportunity headline strings from the Opportunity Radar.
    generated_at:
        UTC ISO timestamp string for this run.

    Keeps a rolling "history" list of up to _MAX_HISTORY past runs (ring buffer).
    The top-level keys always reflect the most recent run for backwards compat.
    """
    # Normalise list -> dict for backward compatibility with intelligence.py caller
    if isinstance(topics, list):
        topics = {key: 1 for key in topics if key}
    try:
        # Load existing file for history ring
        existing: Dict[str, Any] = {}
        if TREND_MEMORY_PATH.exists():
            try:
                existing = json.loads(TREND_MEMORY_PATH.read_text(encoding="utf-8"))
                if not isinstance(existing, dict):
                    existing = {}
            except Exception:  # noqa: BLE001
                existing = {}

        # Build history ring
        prev_history: List[Dict[str, Any]] = existing.get("history", [])
        if not isinstance(prev_history, list):
            prev_history = []

        # Push previous current run into history (if it exists)
        if existing.get("generated_at"):
            prev_entry = {
                "topics": existing.get("topics", {}),
                "opportunities": existing.get("opportunities", []),
                "generated_at": existing.get("generated_at", ""),
            }
            prev_history = [prev_entry] + prev_history
        # Trim to max history
        prev_history = prev_history[:_MAX_HISTORY]

        record: Dict[str, Any] = {
            "generated_at": generated_at,
            "topics": topics,
            "opportunities": opportunities,
            "entity_counts": entity_counts or {},
            "watchlist_topic_counts": watchlist_topic_counts or {},
            "history": prev_history,
        }
        TREND_MEMORY_PATH.write_text(
            json.dumps(record, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        logger.info(
            "trend_memory: saved run %s (%d topics, %d opportunities)",
            generated_at,
            len(topics),
            len(opportunities),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("trend_memory: could not save run: %s", exc)


def compute_momentum(
    current_topics: Dict[str, int],
    history: List[Dict[str, Any]],
) -> Dict[str, str]:
    """
    Compute momentum label for each topic key present in current_topics.

    Parameters
    ----------
    current_topics:
        Dict topic_key -> count for the current run (not yet in history).
    history:
        List of past run records (newest first), each with a "topics" dict.
        May be empty or shorter than _MAX_HISTORY — handled defensively.

    Returns
    -------
    Dict mapping topic_key -> one of:
        "new"          — absent from ALL prior history runs.
        "accelerating" — count rising in most recent 3 runs (run[-1]>[-2]>[-3]).
        "recurring"    — present in ≥4 of the last 12 history runs, stable/not trending.
        "fading"       — present in at least one of runs[-4..-2] but count==0
                         in the 2 most recent history runs.
        ""             — not enough data to classify.

    Momentum thresholds (adjust here if weekly cadence changes):
        new:          0 appearances in history
        accelerating: strictly increasing counts over last 3 history runs
        recurring:    ≥4 appearances in full history window
        fading:       0 in 2 most recent + ≥1 in runs[2..5]
    """
    result: Dict[str, str] = {}

    for topic_key in current_topics:
        # Build count sequence over history (newest history entry = index 0)
        history_counts: List[int] = []
        for run in history:
            run_topics = run.get("topics", {})
            if isinstance(run_topics, dict):
                history_counts.append(run_topics.get(topic_key, 0))
            elif isinstance(run_topics, list):
                # Legacy: old format stored topics as list of keys
                history_counts.append(1 if topic_key in run_topics else 0)
            else:
                history_counts.append(0)

        # NEW: topic never appeared in any prior run
        if not any(c > 0 for c in history_counts):
            result[topic_key] = "new"
            continue

        n = len(history_counts)

        # FADING: present in at least one of runs [2..5] (0-indexed) but
        # absent (count==0) in the 2 most recent history runs.
        recent_two_zero = all(c == 0 for c in history_counts[:2]) if n >= 2 else False
        older_present = any(c > 0 for c in history_counts[2:6]) if n > 2 else False
        if recent_two_zero and older_present:
            result[topic_key] = "fading"
            continue

        # ACCELERATING: strictly increasing counts over the 3 most recent history runs.
        # Requires at least 3 history runs and all three have count > 0.
        if n >= 3:
            c0, c1, c2 = history_counts[0], history_counts[1], history_counts[2]
            if c0 > 0 and c1 > 0 and c2 > 0 and c0 > c1 > c2:
                result[topic_key] = "accelerating"
                continue

        # RECURRING: present in ≥4 of the full history window (up to 12 runs).
        appearances = sum(1 for c in history_counts if c > 0)
        if appearances >= 4:
            result[topic_key] = "recurring"
            continue

        # Not enough data for a confident label
        result[topic_key] = ""

    return result


# ---------------------------------------------------------------------------
# Phase 1 — Theme trajectory helpers
# ---------------------------------------------------------------------------


def count_themes_in_items(items: List[Any]) -> Dict[str, int]:
    """
    Count how many scraped items match each tracked theme.
    items: list of ScrapedItem-like objects with .title and .summary attributes.
    """
    counts: Dict[str, int] = {theme: 0 for theme in TRACKED_THEMES}
    for item in items:
        text = f"{getattr(item, 'title', '')} {getattr(item, 'summary', '')}".lower()
        for theme, keywords in TRACKED_THEMES.items():
            if any(kw in text for kw in keywords):
                counts[theme] += 1
    return counts


def save_theme_counts(theme_counts: Dict[str, int], run_date: str) -> None:
    """Persist this run's theme counts into trend_memory alongside other data."""
    try:
        existing: Dict[str, Any] = {}
        if TREND_MEMORY_PATH.exists():
            try:
                existing = json.loads(TREND_MEMORY_PATH.read_text(encoding="utf-8"))
                if not isinstance(existing, dict):
                    existing = {}
            except Exception:
                existing = {}

        # Append to theme history (most-recent first, max 12 weeks)
        prev_theme_history: List[Dict[str, Any]] = existing.get("theme_history", [])
        if not isinstance(prev_theme_history, list):
            prev_theme_history = []

        if existing.get("theme_counts"):
            prev_theme_history = [
                {"date": existing.get("generated_at", ""), "counts": existing["theme_counts"]}
            ] + prev_theme_history

        prev_theme_history = prev_theme_history[:11]  # keep 11 prior + 1 current = 12

        existing["theme_counts"] = theme_counts
        existing["theme_history"] = prev_theme_history

        TREND_MEMORY_PATH.write_text(
            json.dumps(existing, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.warning("trend_memory: could not save theme counts: %s", exc)


def load_theme_history() -> List[Dict[str, Any]]:
    """
    Return list of {date, counts} dicts (most-recent first), up to 12.
    Returns [] if file missing or unreadable.
    """
    try:
        if not TREND_MEMORY_PATH.exists():
            return []
        data = json.loads(TREND_MEMORY_PATH.read_text(encoding="utf-8"))
        return data.get("theme_history", [])
    except Exception as exc:
        logger.warning("trend_memory: could not load theme history: %s", exc)
        return []


def compute_trajectories(
    current_counts: Dict[str, int],
    history: List[Dict[str, Any]],
    max_trajectories: int = 5,
) -> List[Any]:  # returns List[ThemeTrajectory] — imported lazily to avoid circular import
    """
    Compute trajectory direction for each tracked theme.

    Returns list of dicts ready for ThemeTrajectory construction, ordered by:
      1. increasing trend first
      2. high business relevance (ARIE mentions, Mauritius, competitor complaints)
      3. absolute count in current period
    Capped at max_trajectories.

    Direction rules:
      up   — current count > previous count by >= 25%
      down — current count < previous count by >= 25%
      flat — otherwise
    """
    from models import ThemeTrajectory

    _PRIORITY_THEMES = {
        "ARIE / ACBM mentions",
        "Mauritius FSC activity",
        "Wise complaints",
        "Revolut complaints",
        "Airwallex activity",
        "Account freezes / debanking",
        "Customer onboarding pain",
    }

    results: List[ThemeTrajectory] = []

    for theme, current in current_counts.items():
        # Build counts_by_period: up to 2 prior runs from history (oldest→newest order for display)
        periods: Dict[str, int] = {}
        # Add history entries oldest→newest (we store newest-first, so reverse slice)
        for entry in reversed(history[:2]):
            date_str = entry.get("date", "")[:10]  # ISO date only
            cnt = entry.get("counts", {}).get(theme, 0)
            if date_str:
                periods[date_str] = cnt

        prev = history[0].get("counts", {}).get(theme, 0) if history else 0
        prev2 = history[1].get("counts", {}).get(theme, 0) if len(history) > 1 else None

        # Direction
        if prev == 0:
            direction = "up" if current > 0 else "flat"
        elif current > prev * 1.25:
            direction = "up"
        elif current < prev * 0.75:
            direction = "down"
        else:
            direction = "flat"

        # Skip themes with zero activity across all known periods
        if current == 0 and prev == 0 and (prev2 is None or prev2 == 0):
            continue

        # Confidence
        if len(history) >= 2:
            confidence = "High"
        elif len(history) == 1:
            confidence = "Medium"
        else:
            confidence = "Low"

        # Implication
        if direction == "up":
            implication = f"{theme} is increasing — worth attention this week."
        elif direction == "down":
            implication = f"{theme} is quieter than last period."
        else:
            implication = f"{theme} activity remains consistent."

        results.append(ThemeTrajectory(
            theme=theme,
            counts_by_period=periods,
            direction=direction,
            confidence=confidence,
            implication=implication,
        ))

    # Sort: up first, then priority themes, then by current count desc
    def _sort_key(t: ThemeTrajectory) -> tuple:
        dir_rank = 0 if t.direction == "up" else (1 if t.direction == "flat" else 2)
        priority_rank = 0 if t.theme in _PRIORITY_THEMES else 1
        current_cnt = current_counts.get(t.theme, 0)
        return (dir_rank, priority_rank, -current_cnt)

    results.sort(key=_sort_key)
    return results[:max_trajectories]
