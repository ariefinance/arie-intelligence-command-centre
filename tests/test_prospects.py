"""
Tests for Prospects This Week capability.

Covers:
  1. parse_prospects() — happy path, malformed input, missing key
  2. _render_prospects() — cards, empty-state, required text fragments

No ANTHROPIC_API_KEY or network access required.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timezone

import pytest

from models import Digest, ProspectLead
from intelligence import parse_prospects
from digest_renderer import _render_prospects, _clean_codes


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _minimal_digest(**kwargs) -> Digest:
    base = dict(
        generated_at=datetime.now(tz=timezone.utc).isoformat(),
        executive_summary=["Test bullet"],
    )
    base.update(kwargs)
    return Digest(**base)


_FAKE_CLAUDE_JSON_WITH_PROSPECTS = {
    "signals": [],
    "commercial": [],
    "social": [],
    "film": [],
    "regulatory": [],
    "pain": [],
    "mauritius": [],
    "competitor": [],
    "introducer": [],
    "interesting_finds": [],
    "executive_summary": ["Bullet one", "Bullet two", "Bullet three"],
    "prospects": [
        {
            "entity": "Acme Fund Administrators Ltd",
            "why_now": "They just received a new FSC licence for fund admin services and are expanding their client base in Mauritius.",
            "contact_approach": "Find their head of operations on LinkedIn; approach via their Mauritius CSP connection.",
            "owner": "BD",
            "confidence": "High",
            "source": "Finextra",
            "source_url": "https://www.finextra.com/newsarticle/12345",
        },
        {
            "entity": "BlueSky Payments Corp",
            "why_now": "Announced expansion into East African corridors — aligns with Arie's cross-border treasury offering.",
            "contact_approach": "Contact their treasury director via their press release contact page.",
            "owner": "Management",
            "confidence": "Medium",
            "source": "The Paypers",
            "source_url": "https://www.thepaypers.com/article/67890",
        },
    ],
}


# ---------------------------------------------------------------------------
# parse_prospects tests
# ---------------------------------------------------------------------------

class TestParseProspects:
    def test_happy_path_returns_two_leads(self):
        leads = parse_prospects(_FAKE_CLAUDE_JSON_WITH_PROSPECTS)
        assert len(leads) == 2

    def test_entity_names_correct(self):
        leads = parse_prospects(_FAKE_CLAUDE_JSON_WITH_PROSPECTS)
        entities = [l.entity for l in leads]
        assert "Acme Fund Administrators Ltd" in entities
        assert "BlueSky Payments Corp" in entities

    def test_owner_values_preserved(self):
        leads = parse_prospects(_FAKE_CLAUDE_JSON_WITH_PROSPECTS)
        by_entity = {l.entity: l for l in leads}
        assert by_entity["Acme Fund Administrators Ltd"].owner == "BD"
        assert by_entity["BlueSky Payments Corp"].owner == "Management"

    def test_confidence_values_preserved(self):
        leads = parse_prospects(_FAKE_CLAUDE_JSON_WITH_PROSPECTS)
        by_entity = {l.entity: l for l in leads}
        assert by_entity["Acme Fund Administrators Ltd"].confidence == "High"
        assert by_entity["BlueSky Payments Corp"].confidence == "Medium"

    def test_why_now_and_contact_approach_present(self):
        leads = parse_prospects(_FAKE_CLAUDE_JSON_WITH_PROSPECTS)
        lead = leads[0]
        assert "FSC licence" in lead.why_now
        assert "LinkedIn" in lead.contact_approach

    def test_missing_prospects_key_returns_empty_list(self):
        data = {k: v for k, v in _FAKE_CLAUDE_JSON_WITH_PROSPECTS.items() if k != "prospects"}
        leads = parse_prospects(data)
        assert leads == []

    def test_malformed_prospects_key_returns_empty_list(self):
        data = {**_FAKE_CLAUDE_JSON_WITH_PROSPECTS, "prospects": "not a list"}
        leads = parse_prospects(data)
        assert leads == []

    def test_prospects_with_invalid_entries_skipped(self):
        data = {
            **_FAKE_CLAUDE_JSON_WITH_PROSPECTS,
            "prospects": [
                {"entity": ""},           # empty entity — must be skipped
                None,                      # null — must be skipped
                {"entity": "Valid Co"},    # valid minimal entry
            ],
        }
        leads = parse_prospects(data)
        assert len(leads) == 1
        assert leads[0].entity == "Valid Co"

    def test_invalid_owner_defaults_to_bd(self):
        data = {
            **_FAKE_CLAUDE_JSON_WITH_PROSPECTS,
            "prospects": [
                {"entity": "Test Corp", "owner": "SalesTeam"},
            ],
        }
        leads = parse_prospects(data)
        assert leads[0].owner == "BD"

    def test_invalid_confidence_defaults_to_medium(self):
        data = {
            **_FAKE_CLAUDE_JSON_WITH_PROSPECTS,
            "prospects": [
                {"entity": "Test Corp", "confidence": "Extreme"},
            ],
        }
        leads = parse_prospects(data)
        assert leads[0].confidence == "Medium"

    def test_does_not_raise_on_none_input_value(self):
        # Should not raise even if the value is completely unexpected
        leads = parse_prospects({"prospects": [{"entity": "OK"}, 42, None, []]})
        assert len(leads) == 1

    def test_empty_dict_returns_empty_list(self):
        leads = parse_prospects({})
        assert leads == []


# ---------------------------------------------------------------------------
# _render_prospects tests
# ---------------------------------------------------------------------------

class TestRenderProspects:
    def _digest_with_prospects(self, prospects):
        return _minimal_digest(prospects=prospects)

    def test_renders_heading(self):
        digest = self._digest_with_prospects([])
        html = _render_prospects(digest)
        assert "Prospects This Week" in html

    def test_empty_state_message_when_no_prospects(self):
        digest = self._digest_with_prospects([])
        html = _render_prospects(digest)
        assert "No qualified named prospects surfaced this run" in html
        assert "Priority Decisions" in html

    def test_empty_state_does_not_raise(self):
        digest = self._digest_with_prospects([])
        try:
            html = _render_prospects(digest)
            assert isinstance(html, str)
        except Exception as exc:
            raise AssertionError(f"_render_prospects raised on empty: {exc}") from exc

    def test_entity_names_appear_in_html(self):
        prospects = [
            ProspectLead(entity="Acme Fund Administrators Ltd", why_now="Trigger text here.", contact_approach="Find on LinkedIn."),
            ProspectLead(entity="BlueSky Payments Corp", why_now="Another trigger.", contact_approach="Email their CFO."),
        ]
        digest = self._digest_with_prospects(prospects)
        html = _render_prospects(digest)
        assert "Acme Fund Administrators Ltd" in html
        assert "BlueSky Payments Corp" in html

    def test_why_now_label_present(self):
        prospects = [ProspectLead(entity="Test Co", why_now="They expanded into Mauritius")]
        digest = self._digest_with_prospects(prospects)
        html = _render_prospects(digest)
        assert "Why now" in html
        assert "They expanded into Mauritius" in html

    def test_first_contact_label_present(self):
        prospects = [ProspectLead(entity="Test Co", contact_approach="Approach via their CSP")]
        digest = self._digest_with_prospects(prospects)
        html = _render_prospects(digest)
        assert "First contact" in html
        assert "Approach via their CSP" in html

    def test_contact_badge_present(self):
        prospects = [ProspectLead(entity="Alpha Corp")]
        digest = self._digest_with_prospects(prospects)
        html = _render_prospects(digest)
        # Badge text "Contact" must appear (for RM action cue)
        assert "Contact" in html

    def test_gnews_url_not_linked(self):
        """GNews redirect URLs must not be rendered as clickable links."""
        prospects = [ProspectLead(
            entity="GNews Corp",
            source="Google News",
            source_url="https://news.google.com/articles/abc123",
        )]
        digest = self._digest_with_prospects(prospects)
        html = _render_prospects(digest)
        # The gnews URL should NOT appear as an href (not clickable)
        assert 'href="https://news.google.com/articles/abc123"' not in html

    def test_no_internal_codes_in_output(self):
        prospects = [ProspectLead(entity="SIGNAL/1 Corp", why_now="COMM/3 triggered this.")]
        digest = self._digest_with_prospects(prospects)
        html = _render_prospects(digest)
        # _clean_codes should strip these
        assert "SIGNAL/1" not in html
        assert "COMM/3" not in html

    def test_does_not_raise_on_none_prospects_field(self):
        """Digest with no prospects field at all (default_factory=list) must not crash."""
        digest = _minimal_digest()  # no prospects kwarg → default []
        try:
            html = _render_prospects(digest)
            assert isinstance(html, str)
        except Exception as exc:
            raise AssertionError(f"_render_prospects raised on default: {exc}") from exc

    def test_owner_badge_rendered(self):
        prospects = [ProspectLead(entity="Beta Ltd", owner="BD")]
        digest = self._digest_with_prospects(prospects)
        html = _render_prospects(digest)
        assert "BD" in html

    def test_confidence_badge_rendered(self):
        prospects = [ProspectLead(entity="Gamma Inc", confidence="High")]
        digest = self._digest_with_prospects(prospects)
        html = _render_prospects(digest)
        assert "High" in html


# ---------------------------------------------------------------------------
# _clean_codes unit tests
# ---------------------------------------------------------------------------

class TestCleanCodes:
    """Direct unit tests for _clean_codes — period-preservation and artifact removal."""

    def test_paren_group_mid_sentence_period_preserved(self):
        """Paren group before a following clause must not eat the trailing period."""
        result = _clean_codes(
            "...licence (SIGNAL/1, SIGNAL/5, COMM/3) confirms a pipeline."
        )
        assert result == "...licence confirms a pipeline."

    def test_paren_group_inline_period_preserved(self):
        """Single-code paren group, sentence ends after the group."""
        result = _clean_codes(
            "Revolut payout (COMPETITOR/3) shows service failure."
        )
        assert result == "Revolut payout shows service failure."

    def test_bare_code_at_end_period_preserved(self):
        """Bare token right before the final period must not drop the period."""
        result = _clean_codes("Ends with a code COMM/1.")
        assert result == "Ends with a code."

    def test_bare_code_mid_sentence_no_double_space(self):
        """Bare token in the middle of a sentence must not leave a double space."""
        result = _clean_codes("Mid COMM/4 sentence continues here.")
        assert result == "Mid sentence continues here."

    def test_digits_only_ratio_untouched(self):
        """50/50 and 24/7 are digit-only ratios — must not be touched."""
        assert _clean_codes("Price was 50/50 split.") == "Price was 50/50 split."
        assert _clean_codes("Available 24/7.") == "Available 24/7."

    def test_empty_parens_removed(self):
        """Orphaned empty () must be stripped."""
        result = _clean_codes("Something () happened.")
        assert "()" not in result
        assert "( )" not in result

    def test_no_space_before_period(self):
        """No ' .' artifact must remain after stripping codes."""
        result = _clean_codes("Alert SIGNAL/2 raised.")
        assert " ." not in result

    def test_empty_string_returns_empty(self):
        assert _clean_codes("") == ""

    def test_none_passthrough(self):
        """None input must be returned as-is (falsy guard)."""
        assert _clean_codes(None) is None  # type: ignore[arg-type]

    def test_plain_text_untouched(self):
        """Text with no codes must be returned unchanged (modulo strip)."""
        plain = "Arie Finance is expanding into East Africa."
        assert _clean_codes(plain) == plain
