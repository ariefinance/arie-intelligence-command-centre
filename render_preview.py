"""Local preview harness: render the real production digest fixture to preview.html.

Usage: python render_preview.py [out.html]
Not shipped to production — a dev tool for visual auditing the renderer.
"""
import json
import sys

from models import Digest
import digest_renderer

OUT = sys.argv[1] if len(sys.argv) > 1 else "preview.html"

data = json.load(open("verification/sample_digest.json", encoding="utf-8"))
digest = Digest.model_validate(data)
html = digest_renderer.render_digest_html(digest)
with open(OUT, "w", encoding="utf-8") as f:
    f.write(html)
print(f"wrote {OUT} ({len(html)} bytes)")
