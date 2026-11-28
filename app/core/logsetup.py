"""Logging for the long-running processes (invariant 18: secrets never reach a log).

* every record passes through `redact` before any handler writes it
* HTTP client libraries log full request URLs at INFO (the Telegram Bot API puts the bot
  token in the URL), so they are held at WARNING
"""

from __future__ import annotations

import logging
from pathlib import Path

from app.core.scrub import redact

NOISY = (
    "httpx",
    "httpx2",
    "httpcore",
    "httpcore2",
    "anthropic",
    "urllib3",
    "websockets",
    "uvicorn.access",
)


class RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # a bad format string must not kill logging
            return True
        clean = redact(msg)
        if clean != msg or record.args:
            record.msg, record.args = clean, None
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        return True


def setup_logging(logfile: Path, level: int = logging.INFO) -> None:
    logfile.parent.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [
        logging.StreamHandler(),
        logging.FileHandler(logfile, encoding="utf-8"),
    ]
    f = RedactFilter()
    for h in handlers:
        h.addFilter(f)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=handlers,
        force=True,
    )
    for name in NOISY:
        logging.getLogger(name).setLevel(logging.WARNING)
