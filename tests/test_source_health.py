"""
Tests for scraper.build_source_health — deterministic source health builder.

No ANTHROPIC_API_KEY or network access required.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scraper import build_source_health


class TestBuildSourceHealth:
    def test_active_source(self):
        """A source with count > 0 should be 'active'."""
        items = build_source_health(
            source_counts={"Finextra": 5},
            source_failures=[],
            reddit_configured=False,
            last_checked="2026-06-22T10:00:00Z",
        )
        finextra = next(i for i in items if i.source_name == "Finextra")
        assert finextra.status == "active"
        assert finextra.items_collected == 5

    def test_failed_source_with_zero_count(self):
        """A source with 0 items (not Reddit) should be 'failed'."""
        items = build_source_health(
            source_counts={"Finextra": 0},
            source_failures=["Finextra"],
            reddit_configured=False,
            last_checked="2026-06-22T10:00:00Z",
        )
        finextra = next(i for i in items if i.source_name == "Finextra")
        assert finextra.status == "failed"

    def test_reddit_not_configured(self):
        """Reddit with 0 items and reddit_configured=False → not_configured."""
        items = build_source_health(
            source_counts={"Reddit": 0},
            source_failures=[],
            reddit_configured=False,
            last_checked="2026-06-22T10:00:00Z",
        )
        reddit = next(i for i in items if i.source_name == "Reddit")
        assert reddit.status == "not_configured"

    def test_reddit_configured_but_zero_is_failed(self):
        """Reddit with 0 items but reddit_configured=True → failed (not not_configured)."""
        items = build_source_health(
            source_counts={"Reddit": 0},
            source_failures=["Reddit"],
            reddit_configured=True,
            last_checked="2026-06-22T10:00:00Z",
        )
        reddit = next(i for i in items if i.source_name == "Reddit")
        assert reddit.status == "failed"

    def test_failures_sort_before_active(self):
        """failed / not_configured items should sort before active items."""
        items = build_source_health(
            source_counts={"Finextra": 10, "Reddit": 0, "ThePaypers": 3},
            source_failures=["Reddit"],
            reddit_configured=False,
            last_checked="2026-06-22T10:00:00Z",
        )
        non_active = [i for i in items if i.status in ("failed", "not_configured")]
        active = [i for i in items if i.status == "active"]
        if non_active and active:
            # All non-active items must appear before the first active item in the list
            last_non_active_idx = max(items.index(i) for i in non_active)
            first_active_idx = min(items.index(i) for i in active)
            assert last_non_active_idx < first_active_idx

    def test_source_in_failures_not_in_counts_is_included(self):
        """Sources appearing only in source_failures (not source_counts) are still included."""
        items = build_source_health(
            source_counts={},
            source_failures=["FCA"],
            reddit_configured=False,
            last_checked="2026-06-22T10:00:00Z",
        )
        fca = next((i for i in items if i.source_name == "FCA"), None)
        assert fca is not None
        assert fca.status == "failed"
