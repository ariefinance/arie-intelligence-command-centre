"""
Tests for digest_renderer._esc — HTML entity normalisation (Fix 2).

Feed/RSS titles sometimes contain literal HTML entities (e.g. "&nbsp;&nbsp;").
_esc must decode pre-existing entities so they don't render as visible "&nbsp;",
while still escaping any real markup so the output stays injection-safe.

No network / API key required.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from digest_renderer import _esc


class TestEntityNormalisation:
    def test_literal_nbsp_is_not_rendered_verbatim(self):
        out = _esc("Chase Debanked My Company &nbsp;&nbsp; Yahoo Finance")
        # The visible output must NOT contain the literal entity text.
        assert "&nbsp;" not in out
        assert "&amp;nbsp;" not in out
        # The real words survive.
        assert "Chase Debanked My Company" in out
        assert "Yahoo Finance" in out

    def test_amp_entity_decoded_then_canonically_escaped(self):
        # "Profit &amp; Loss" (pre-escaped source) should render as "Profit & Loss"
        # i.e. canonically escaped to a single &amp; — never double-escaped.
        out = _esc("Profit &amp; Loss")
        assert out == "Profit &amp; Loss"
        assert "&amp;amp;" not in out


class TestInjectionSafetyPreserved:
    def test_raw_script_tag_is_escaped(self):
        out = _esc("<script>alert('x')</script>")
        assert "<script>" not in out
        assert "&lt;script&gt;" in out

    def test_pre_escaped_script_stays_escaped(self):
        # Even if the source already contains an escaped tag, decoding then
        # re-escaping must still yield safe, escaped output (not raw markup).
        out = _esc("&lt;script&gt;alert(1)&lt;/script&gt;")
        assert "<script>" not in out
        assert "&lt;script&gt;" in out

    def test_quotes_are_escaped_for_attribute_safety(self):
        out = _esc('a"b')
        assert '"' not in out
        assert "&quot;" in out
