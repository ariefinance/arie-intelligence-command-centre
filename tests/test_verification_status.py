"""
Tests for the read-only /verification/status proof endpoint.

No network / API key required — uses FastAPI TestClient against the in-process
app. Asserts the JSON shape and that code-capability flags are True (the code is
present), without depending on a cached digest existing.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hashlib
from unittest.mock import patch

from fastapi.testclient import TestClient

from main import app

client = TestClient(app)


def _fake_cache(html: str):
    """Return a (digest_dict, html) tuple as cache.load_digest would."""
    return ({"customer_pain": [], "source_health": [{"x": 1}]}, html)


def test_status_returns_200_and_no_cache_headers():
    r = client.get("/verification/status")
    assert r.status_code == 200
    assert "no-store" in r.headers.get("cache-control", "").lower()


def test_status_has_all_required_keys():
    j = client.get("/verification/status").json()
    for key in [
        "app_version",
        "generated_at",
        "customer_experience_enabled",
        "source_health_enabled",
        "cx_relevance_gate_enabled",
        "hardening_headers_enabled",
        "latest_digest_has_source_health",
        "latest_digest_customer_pain_count",
        "brief_html_checked",
        "brief_bytes",
        "brief_sha256",
        "bad_terms_present",
        "bad_terms_present_from_brief_html",
        "verification_consistent_with_brief",
        "labels",
    ]:
        assert key in j, f"missing key: {key}"
    assert set(j["bad_terms_present"]) == {"agentic_ai", "everyday_hardware", "nbsp"}
    assert set(j["bad_terms_present_from_brief_html"]) == {
        "agentic_ai", "everyday_hardware", "profitable_frontier", "nbsp", "amp_nbsp",
    }
    assert set(j["labels"]) == {
        "evidence_sources_present",
        "monitored_feeds_present",
        "bare_sources_present",
    }


class TestStatusReflectsBriefHtml:
    """The endpoint must report what /brief actually serves — never a different cache."""

    BAD_HTML = (
        "<html><body><h2>Customer Experience Intelligence</h2>"
        "<div>Where is the next profitable frontier for Agentic AI + Everyday Hardware?</div>"
        "</body></html>"
    )
    CLEAN_HTML = (
        "<html><body><h2>Customer Experience Intelligence</h2>"
        "<span>Evidence sources</span><span>monitored feeds</span>"
        "<div>My Wise account was frozen</div></body></html>"
    )

    def test_bad_item_in_brief_makes_status_report_true(self):
        with patch("cache.load_digest", return_value=_fake_cache(self.BAD_HTML)):
            brief = client.get("/brief").text
            j = client.get("/verification/status").json()
        assert "Agentic AI" in brief  # /brief really serves it
        b = j["bad_terms_present_from_brief_html"]
        assert b["agentic_ai"] is True
        assert b["everyday_hardware"] is True
        assert b["profitable_frontier"] is True
        # legacy block agrees, and consistency flag is True (same HTML scanned)
        assert j["bad_terms_present"]["agentic_ai"] is True
        assert j["verification_consistent_with_brief"] is True
        assert j["brief_html_checked"] is True

    def test_clean_brief_makes_status_report_false(self):
        with patch("cache.load_digest", return_value=_fake_cache(self.CLEAN_HTML)):
            j = client.get("/verification/status").json()
        b = j["bad_terms_present_from_brief_html"]
        assert b["agentic_ai"] is False
        assert b["everyday_hardware"] is False
        assert b["profitable_frontier"] is False
        assert j["labels"]["evidence_sources_present"] is True
        assert j["labels"]["monitored_feeds_present"] is True
        assert j["verification_consistent_with_brief"] is True

    def test_sha256_and_bytes_match_brief_response(self):
        with patch("cache.load_digest", return_value=_fake_cache(self.CLEAN_HTML)):
            brief_bytes = client.get("/brief").content
            j = client.get("/verification/status").json()
        assert j["brief_sha256"] == hashlib.sha256(brief_bytes).hexdigest()
        assert j["brief_bytes"] == len(brief_bytes)


def test_status_code_capability_flags_are_true():
    # These reflect the running code, not digest data — must be True on this build.
    j = client.get("/verification/status").json()
    assert j["customer_experience_enabled"] is True
    assert j["source_health_enabled"] is True
    assert j["cx_relevance_gate_enabled"] is True
    assert j["hardening_headers_enabled"] is True
    assert isinstance(j["app_version"], str) and j["app_version"]


def test_status_counts_are_well_typed():
    j = client.get("/verification/status").json()
    assert isinstance(j["latest_digest_customer_pain_count"], int)
    assert isinstance(j["latest_digest_has_source_health"], bool)
