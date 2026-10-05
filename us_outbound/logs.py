"""Structured logging with the secrets rules applied (SPEC 1.7).

Logs carry hashed emails, never raw addresses, and at most 200 characters of any
email body. Every log line is one JSON object on stdout, which Railway's log view picks up.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from datetime import date, datetime
from typing import Any

BODY_LIMIT = 200
# Keys whose values are email bodies or reply text and must be clipped.
BODY_KEYS = frozenset({"body", "text", "reply_text", "draft", "draft_reply", "html", "content", "message"})
# Keys whose values are secrets and must never be logged.
SECRET_KEYS = frozenset({"token", "api_key", "authorization", "password", "secret", "key"})

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def normalise_email(email: str) -> str:
    return email.strip().lower()


def hash_email(email: str) -> str:
    """The sha256 used everywhere an email is stored or logged (suppression, contacts.email_sha256)."""
    return hashlib.sha256(normalise_email(email).encode()).hexdigest()


def clip(text: str, limit: int = BODY_LIMIT) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


def _hash_emails_in(text: str) -> str:
    return EMAIL_RE.sub(lambda m: "email:" + hash_email(m.group(0))[:16], text)


def redact(value: Any, key: str = "") -> Any:
    """Return a copy of value that is safe to log."""
    k = key.lower()
    if k in SECRET_KEYS:
        return "[redacted]"
    if isinstance(value, dict):
        return {kk: redact(vv, str(kk)) for kk, vv in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [redact(v, key) for v in value]
    if isinstance(value, str):
        if k in BODY_KEYS:
            value = clip(value)
        return _hash_emails_in(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def log(event: str, **fields: Any) -> None:
    # sys.stdout is looked up at each call, so `accounts --csv` can send these lines to stderr
    # (contextlib.redirect_stdout in ops/cli.cmd_accounts) and keep stdout for the CSV.
    line = {"event": event, **redact(fields)}
    print(json.dumps(line, default=str, ensure_ascii=False), file=sys.stdout, flush=True)
