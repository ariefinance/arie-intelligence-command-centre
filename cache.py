"""
Digest cache: persists the latest generated digest (JSON + HTML) to disk.

Paths are relative to the working directory — same convention as trend_memory.json.
On Railway: ensure the working directory is on a persistent Volume so the cache
survives redeploys. Without a Volume, the cache resets on each redeploy (safe —
just requires a POST /refresh after each deploy).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

DIGEST_JSON_PATH = Path("./latest_digest.json")
DIGEST_HTML_PATH = Path("./latest_digest.html")


def save_digest(digest_json: dict, html: str) -> None:
    """Atomically write the latest digest JSON and HTML to disk."""
    _write_atomic(DIGEST_JSON_PATH, json.dumps(digest_json, indent=2, ensure_ascii=False))
    _write_atomic(DIGEST_HTML_PATH, html)
    logger.info("Digest cached: %s / %s", DIGEST_JSON_PATH, DIGEST_HTML_PATH)


def load_digest() -> Optional[Tuple[dict, str]]:
    """
    Load cached digest from disk.
    Returns (digest_dict, html_str) or None if no cache exists or read fails.
    """
    if not DIGEST_JSON_PATH.exists() or not DIGEST_HTML_PATH.exists():
        return None
    try:
        digest_dict = json.loads(DIGEST_JSON_PATH.read_text(encoding="utf-8"))
        html = DIGEST_HTML_PATH.read_text(encoding="utf-8")
        return digest_dict, html
    except Exception as exc:
        logger.error("Failed to load digest cache: %s", exc)
        return None


def cache_exists() -> bool:
    return DIGEST_JSON_PATH.exists() and DIGEST_HTML_PATH.exists()


def _write_atomic(path: Path, text: str) -> None:
    """Write text to a temp file then rename — avoids a partially-written cache."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
