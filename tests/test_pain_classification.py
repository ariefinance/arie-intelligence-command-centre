"""
Tests for enrich.classify_pain_item — deterministic Phase 2 pain classifier.

All tests operate on pure in-memory CustomerPainItem objects.
No ANTHROPIC_API_KEY or network access required.
"""
import sys
import os

# Ensure project root is on the path so we can import project modules directly.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models import CustomerPainItem
from enrich import classify_pain_item


def _make_item(
    source: str,
    pain_point: str = "",
    evidence_excerpt: str = "",
    source_url: str = "https://example.com/test",
) -> CustomerPainItem:
    return CustomerPainItem(
        pain_point=pain_point,
        source=source,
        evidence_excerpt=evidence_excerpt,
        source_url=source_url,
    )


# ---------------------------------------------------------------------------
# source_type
# ---------------------------------------------------------------------------

class TestSourceType:
    def test_trustpilot_is_review(self):
        item = _make_item(source="Trustpilot/Wise")
        classify_pain_item(item)
        assert item.source_type == "review"

    def test_reddit_is_forum_discussion(self):
        item = _make_item(source="Reddit/r/fintech")
        classify_pain_item(item)
        assert item.source_type == "forum_discussion"

    def test_hackernews_is_forum_discussion(self):
        item = _make_item(source="HackerNews")
        classify_pain_item(item)
        assert item.source_type == "forum_discussion"

    def test_gnews_pain_is_news_about_complaint(self):
        item = _make_item(source="GNews/Pain/frozen-accounts")
        classify_pain_item(item)
        assert item.source_type == "news_about_complaint"

    def test_google_news_is_news_about_complaint(self):
        item = _make_item(source="Google News")
        classify_pain_item(item)
        assert item.source_type == "news_about_complaint"

    def test_unknown_source_is_unknown(self):
        item = _make_item(source="ManualSource/custom")
        classify_pain_item(item)
        assert item.source_type == "unknown"


# ---------------------------------------------------------------------------
# pain_type
# ---------------------------------------------------------------------------

class TestPainType:
    def test_frozen_account_from_freeze_keyword(self):
        item = _make_item(
            source="Reddit/r/banking",
            pain_point="My account has been frozen for 3 weeks",
            evidence_excerpt="They froze my funds with no explanation",
        )
        classify_pain_item(item)
        assert item.pain_type == "frozen_account"

    def test_frozen_account_from_past_tense_froze_only(self):
        # Regression: past-tense "froze" without the word "frozen"/"freeze"
        # must still classify as frozen_account (not transfer_delay via "stuck").
        item = _make_item(
            source="Trustpilot/Revolut",
            pain_point="Revolut froze my account and my salary was stuck",
            evidence_excerpt="They froze everything with no warning",
        )
        classify_pain_item(item)
        assert item.pain_type == "frozen_account"
        assert item.severity == "high"

    def test_kyc_friction(self):
        item = _make_item(
            source="Trustpilot/Revolut",
            pain_point="KYC verification keeps failing",
            evidence_excerpt="Submitted documents three times for identity verification",
        )
        classify_pain_item(item)
        assert item.pain_type == "kyc_friction"

    def test_account_closure(self):
        item = _make_item(
            source="Reddit/r/fintech",
            pain_point="Account closure without warning",
            evidence_excerpt="Revolut closed my account suddenly",
        )
        classify_pain_item(item)
        assert item.pain_type == "account_closure"

    def test_poor_support(self):
        item = _make_item(
            source="Trustpilot/Airwallex",
            pain_point="Customer service is impossible to reach",
            evidence_excerpt="No response from support after two weeks",
        )
        classify_pain_item(item)
        assert item.pain_type == "poor_support"

    def test_fee_pricing(self):
        # "transfers" would match transfer_delay first; use pricing/fee language without transfer
        item = _make_item(
            source="Reddit/r/smallbusiness",
            pain_point="Hidden fees eating into margins",
            evidence_excerpt="The exchange rate was terrible and there were hidden costs in pricing",
        )
        classify_pain_item(item)
        assert item.pain_type == "fee_pricing"

    def test_other_when_no_match(self):
        item = _make_item(
            source="Reddit/r/fintech",
            pain_point="General dissatisfaction with the app",
            evidence_excerpt="",
        )
        classify_pain_item(item)
        assert item.pain_type == "other"


# ---------------------------------------------------------------------------
# severity
# ---------------------------------------------------------------------------

class TestSeverity:
    def test_high_severity_for_frozen(self):
        item = _make_item(
            source="Reddit/r/banking",
            pain_point="Account frozen, cannot access funds",
            evidence_excerpt="My funds have been frozen for weeks",
        )
        classify_pain_item(item)
        assert item.severity == "high"

    def test_medium_severity_for_kyc(self):
        item = _make_item(
            source="Reddit/r/fintech",
            pain_point="KYC delay holding up onboarding",
            evidence_excerpt="Verification pending for 10 days",
        )
        classify_pain_item(item)
        assert item.severity == "medium"

    def test_low_severity_for_benign(self):
        item = _make_item(
            source="Reddit/r/entrepreneur",
            pain_point="Wondering which payment provider to use",
            evidence_excerpt="Just comparing options for my business",
        )
        classify_pain_item(item)
        assert item.severity == "low"


# ---------------------------------------------------------------------------
# compliance_caution
# ---------------------------------------------------------------------------

class TestComplianceCaution:
    def test_caution_non_empty_for_freeze(self):
        item = _make_item(
            source="Reddit/r/banking",
            pain_point="Account frozen unexpectedly",
            evidence_excerpt="Funds frozen for two weeks",
        )
        classify_pain_item(item)
        assert item.compliance_caution != ""
        assert "market risk awareness" in item.compliance_caution

    def test_caution_non_empty_for_kyc(self):
        item = _make_item(
            source="Trustpilot/Wise",
            pain_point="KYC document failure",
            evidence_excerpt="They rejected my kyc documents",
        )
        classify_pain_item(item)
        assert item.compliance_caution != ""

    def test_caution_empty_for_benign_fee_item(self):
        # "fee" alone should NOT trigger compliance caution
        item = _make_item(
            source="Reddit/r/fintech",
            pain_point="Transfer fees are too high",
            evidence_excerpt="The fees on this service are high compared to competitors",
        )
        classify_pain_item(item)
        assert item.compliance_caution == ""
