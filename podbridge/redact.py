"""Log redaction: secrets must never reach the logs (spec section 3).

Two layers:
  - patterns for cookie, bearer and token-shaped fields;
  - exact values of every secret decrypted at runtime (registered by the settings store).
"""

from __future__ import annotations

import logging
import re

REDACTED = "[REDACTED]"
MIN_SECRET_LENGTH = 6

_PATTERNS = [
    re.compile(r"(session_id=)[^;\s\"'&]+", re.IGNORECASE),
    re.compile(r"(Bearer\s+)[A-Za-z0-9._~+/=-]+", re.IGNORECASE),
    re.compile(
        r"""(["']?(?:cookie|authorization|password|refresh_?token|access_?token|playback_token)["']?"""
        r"""\s*[:=]\s*["']?)[^"',\s}]+""",
        re.IGNORECASE,
    ),
]

_secrets: set[str] = set()


def register_secret(value: str | None) -> None:
    if value and len(value) >= MIN_SECRET_LENGTH:
        _secrets.add(value)


def redact(text: str) -> str:
    for secret in sorted(_secrets, key=len, reverse=True):
        text = text.replace(secret, REDACTED)
    for pattern in _PATTERNS:
        text = pattern.sub(lambda m: m.group(1) + REDACTED, text)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        return True


def configure_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(RedactingFilter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
