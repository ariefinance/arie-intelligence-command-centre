"""
Tests for digest_renderer._render_customer_experience — empty-state and group-split behaviour.

No ANTHROPIC_API_KEY or network access required. Tests pure renderer function.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timezone

from models import CustomerPainItem, Digest, BrandWatchResult
from digest_renderer import _render_customer_experience


def _make_digest(pain_items: list) -> Digest:
    """Build a minimal Digest with the given customer_pain list."""
    return Digest(
        generated_at=datetime.now(tz=timezone.utc).isoformat(),
        executive_summary=["Test bullet"],
        customer_pain=pain_items,
    )


def _make_pain_item(
    source_type: str = "forum_discussion",
    pain_point: str = "My Wise account was frozen during KYC review",
    source_url: str = "https://reddit.com/r/fintech/test",
    evidence_excerpt: str = "Short excerpt for testing purposes here.",
    published_at: str = "2026-06-20",
) -> CustomerPainItem:
    item = CustomerPainItem(
        pain_point=pain_point,
        source_url=source_url,
        evidence_excerpt=evidence_excerpt,
        published_at=published_at,
    )
    item.source_type = source_type
    item.severity = "medium"
    item.pain_type = "kyc_friction"
    item.confidence = "medium"
    item.source_quality = "medium"
    item.compliance_caution = ""
    item.recommended_internal_action = "Review KYC/KYB onboarding communication."
    item.arie_relevance = "Signals friction with a competitor platform."
    return item


class TestRenderCustomerExperienceEmptyState:
    def test_empty_list_renders_empty_state_message(self):
        digest = _make_digest([])
        html = _render_customer_experience(digest)
        assert "No verified real-user complaint signals found in this run." in html

    def test_empty_list_does_not_raise(self):
        digest = _make_digest([])
        try:
            html = _render_customer_experience(digest)
            assert isinstance(html, str)
        except Exception as exc:
            raise AssertionError(f"_render_customer_experience raised: {exc}") from exc

    def test_returns_string_containing_section_title(self):
        digest = _make_digest([])
        html = _render_customer_experience(digest)
        assert "Customer Experience Intelligence" in html

    def test_returns_caption_text(self):
        digest = _make_digest([])
        html = _render_customer_experience(digest)
        assert "Not for outreach" in html


class TestRenderCustomerExperienceGroupSplit:
    def test_direct_group_label_present(self):
        digest = _make_digest([])
        html = _render_customer_experience(digest)
        assert "Direct user complaints" in html

    def test_direct_item_shows_in_direct_group(self):
        item = _make_pain_item(source_type="forum_discussion", pain_point="Wise frozen account test")
        digest = _make_digest([item])
        html = _render_customer_experience(digest)
        assert "Wise frozen account test" in html
        # The empty-state message should NOT appear because we have a direct item
        assert "No verified real-user complaint signals found in this run." not in html

    def test_media_item_with_zero_direct_still_shows_empty_state_for_direct(self):
        """One news_about_complaint item + zero direct → empty state in direct group, item in media group."""
        news_item = _make_pain_item(
            source_type="news_about_complaint",
            pain_point="SME payment delays reported widely",
            source_url="https://news.google.com/articles/test",
        )
        digest = _make_digest([news_item])
        html = _render_customer_experience(digest)
        # Direct group shows empty-state
        assert "No verified real-user complaint signals found in this run." in html
        # Media group shows the item
        assert "SME payment delays reported widely" in html
        assert "Reported in media" in html

    def test_severity_pill_in_output(self):
        item = _make_pain_item(source_type="review", pain_point="KYC rejection pain")
        item.severity = "high"
        digest = _make_digest([item])
        html = _render_customer_experience(digest)
        assert "High" in html or "sev-high" in html

    def test_suggested_internal_action_in_output(self):
        item = _make_pain_item(source_type="review")
        item.recommended_internal_action = "Review KYC/KYB onboarding communication."
        digest = _make_digest([item])
        html = _render_customer_experience(digest)
        assert "Suggested internal action" in html
        assert "Review KYC/KYB onboarding" in html

    def test_compliance_caution_renders_when_non_empty(self):
        item = _make_pain_item(source_type="review")
        item.compliance_caution = "Sensitive topic — treat as market risk awareness only."
        digest = _make_digest([item])
        html = _render_customer_experience(digest)
        assert "Sensitive topic" in html

    def test_no_outreach_wording_in_html(self):
        item = _make_pain_item(source_type="forum_discussion")
        digest = _make_digest([item])
        html = _render_customer_experience(digest)
        # Check that none of the banned marketing/targeting words appear
        for banned in ["poach", "target unhappy", "outreach to unhappy"]:
            assert banned not in html.lower(), f"Banned phrase found: '{banned}'"
