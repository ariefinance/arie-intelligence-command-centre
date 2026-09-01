"""
Tests for the strict Customer Experience Intelligence relevance gate.

Covers:
  - enrich.is_cx_relevant (the deterministic gate)
  - enrich.classify_pain_item (pain_type/severity for valid items)
  - digest_renderer._render_customer_experience (irrelevant items not rendered;
    empty-state when no valid direct complaints remain)

No network / API key required.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timezone

from models import CustomerPainItem, Digest
from enrich import is_cx_relevant, classify_pain_item
from digest_renderer import _render_customer_experience


# Title from the live false-positive that triggered this fix.
AI_HARDWARE_TITLE = "Where is the next profitable frontier for Agentic AI + Everyday Hardware?"
GENERIC_FUNDING_NEWS = "Fintech firm raises Series B funding round to expand AI product"


# ---------------------------------------------------------------------------
# Relevance gate (is_cx_relevant)
# ---------------------------------------------------------------------------

class TestRelevanceGate:
    def test_agentic_ai_hardware_is_not_relevant(self):
        assert is_cx_relevant(AI_HARDWARE_TITLE) is False

    def test_generic_fintech_funding_news_is_not_relevant(self):
        assert is_cx_relevant(GENERIC_FUNDING_NEWS) is False

    def test_generic_ai_saas_is_not_relevant(self):
        assert is_cx_relevant("AI startup launches new SaaS hardware platform") is False

    def test_wise_frozen_account_is_relevant(self):
        assert is_cx_relevant("My Wise account was frozen and support has not responded") is True

    def test_kyc_onboarding_blocked_is_relevant(self):
        assert is_cx_relevant("KYC took weeks and onboarding was blocked") is True

    def test_payment_stuck_is_relevant(self):
        assert is_cx_relevant("Payment stuck for 5 days") is True

    def test_transfer_failed_support_unresponsive_is_relevant(self):
        assert is_cx_relevant("Transfer failed and support is not responding") is True

    def test_money_noun_without_problem_word_is_not_relevant(self):
        # "New payment app launches" mentions payment but is not a complaint.
        assert is_cx_relevant("New payment app launches for freelancers") is False

    def test_topical_cross_border_without_problem_is_not_relevant(self):
        # A market/opportunity discussion that merely names payment topics must
        # NOT qualify on those topical mentions alone.
        assert is_cx_relevant("cross-border payment opportunities and remittance for AI startups") is False

    def test_cross_border_with_problem_word_is_relevant(self):
        # A genuine complaint about a cross-border payment still qualifies.
        assert is_cx_relevant("My cross-border payment is stuck and the transfer failed") is True


# ---------------------------------------------------------------------------
# Classification of the required valid cases
# ---------------------------------------------------------------------------

def _item(pain_point: str, source: str = "Reddit/r/fintech") -> CustomerPainItem:
    it = CustomerPainItem(
        pain_point=pain_point,
        source=source,
        source_url="https://old.reddit.com/r/fintech/x",
        evidence_excerpt=pain_point,
    )
    classify_pain_item(it)
    return it


class TestClassificationOfValidCases:
    def test_wise_frozen_is_frozen_account_high(self):
        it = _item("My Wise account was frozen and support has not responded",
                   source="Trustpilot/Wise")
        assert it.pain_type == "frozen_account"
        assert it.severity == "high"

    def test_kyc_onboarding_blocked_is_kyc_or_onboarding(self):
        it = _item("KYC took weeks and onboarding was blocked")
        assert it.pain_type in ("kyc_friction", "delayed_onboarding")

    def test_payment_stuck_is_transfer_or_rejection(self):
        it = _item("Payment stuck for 5 days")
        assert it.pain_type in ("transfer_delay", "payment_rejection")


# ---------------------------------------------------------------------------
# Render-layer gate
# ---------------------------------------------------------------------------

def _digest(pain_items: list) -> Digest:
    return Digest(
        generated_at=datetime.now(tz=timezone.utc).isoformat(),
        executive_summary=["Test bullet"],
        customer_pain=pain_items,
    )


def _pain(pain_point: str, source: str, source_type: str) -> CustomerPainItem:
    it = CustomerPainItem(
        pain_point=pain_point,
        source=source,
        source_url="https://old.reddit.com/r/x/y",
        evidence_excerpt=pain_point,
        source_type=source_type,
        published_at=datetime.now(tz=timezone.utc).date().isoformat(),
    )
    classify_pain_item(it)
    it.source_type = source_type  # keep explicit source_type for grouping
    return it


class TestRenderGate:
    def test_ai_hardware_item_not_rendered(self):
        ai = _pain(AI_HARDWARE_TITLE, "Reddit/r/artificial", "forum_discussion")
        html = _render_customer_experience(_digest([ai]))
        assert "Agentic AI" not in html
        # No valid direct complaints remain → empty-state shown.
        assert "No verified real-user complaint signals found in this run." in html

    def test_valid_item_still_rendered(self):
        good = _pain("My Wise account was frozen and support has not responded",
                     "Trustpilot/Wise", "review")
        html = _render_customer_experience(_digest([good]))
        assert "Wise account was frozen" in html
        assert "No verified real-user complaint signals found in this run." not in html

    def test_mixed_drops_only_irrelevant(self):
        ai = _pain(AI_HARDWARE_TITLE, "Reddit/r/artificial", "forum_discussion")
        good = _pain("KYC took weeks and onboarding was blocked",
                     "Reddit/r/fintech", "forum_discussion")
        html = _render_customer_experience(_digest([ai, good]))
        assert "Agentic AI" not in html
        assert "KYC took weeks" in html

    def test_offtopic_title_with_payment_body_is_excluded(self):
        # REGRESSION (the live failure): an AI/hardware post whose BODY mentions
        # cross-border payments must NOT render. The gate is title-anchored, so an
        # incidental payment mention in the excerpt cannot promote it.
        ai = _pain(AI_HARDWARE_TITLE, "Reddit/r/artificial", "forum_discussion")
        ai.evidence_excerpt = (
            "The next frontier could be cross-border payment rails and remittance "
            "infrastructure for AI agents and everyday hardware."
        )
        html = _render_customer_experience(_digest([ai]))
        assert "Agentic AI" not in html
        assert "No verified real-user complaint signals found in this run." in html
