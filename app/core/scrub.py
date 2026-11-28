"""Detect and redact token-like strings (invariant 18).

Used by: the repo scan test (Phase 0), log/alert formatters and fixture recorders
(Phase 1+), and the AI prompt builder (Phase 2). Patterns err on the side of flagging.
"""

from __future__ import annotations

import re

PATTERNS: dict[str, re.Pattern[str]] = {
    "jwt": re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]*"),
    # also inside the Bot API URL: https://api.telegram.org/bot<id>:<secret>/getUpdates
    "telegram_bot_token": re.compile(r"(?:\b|(?<=bot))\d{8,10}:[A-Za-z0-9_-]{35}\b"),
    "anthropic_key": re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"),
    "openai_key": re.compile(r"\bsk-[A-Za-z0-9]{32,}\b"),
    "google_api_key": re.compile(r"AIza[0-9A-Za-z_-]{35}"),
    "aws_access_key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "private_key_block": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    # Any identifier containing a secret-ish word, e.g. api_secret=, ALGO_FYERS_APP_SECRET=,
    # "access_token": ... (no word boundary, so prefixes like ALGO_ and api_ are caught).
    "assigned_secret": re.compile(
        r"(?i)[A-Za-z0-9_]*(?:secret|token|api[_-]?key|password|passphrase|jkey|appidhash)"
        r"[A-Za-z0-9_]*[\"']?[ \t]*[:=][ \t]*[\"']?([A-Za-z0-9/+_.-]{20,})"
    ),
    # Bare long hex: SHA-256 hashes such as Flattrade's api_secret hash or Fyers appIdHash.
    "long_hex": re.compile(r"\b[0-9a-fA-F]{48,}\b"),
}


def find_secrets(text: str) -> list[tuple[str, str]]:
    """Return (pattern name, first 6 chars + '...') for each hit. Never the full value."""
    hits = []
    for name, pat in PATTERNS.items():
        for m in pat.finditer(text):
            value = m.group(m.lastindex or 0)
            hits.append((name, value[:6] + "..."))
    return hits


def redact(text: str) -> str:
    for pat in PATTERNS.values():
        text = pat.sub("[REDACTED]", text)
    return text
