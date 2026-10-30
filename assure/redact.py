"""Secret redaction applied before anything is written to the evidence store."""

from __future__ import annotations

import re
from typing import Any

REDACTED = "[REDACTED]"
SECRET_KEY = re.compile(r"(pass(word)?|secret|token|api[_-]?key|credential|auth)", re.I)


class Redactor:
    def __init__(self, known_secrets: list[str] | None = None):
        # Longest first so overlapping secrets are fully masked.
        self.known = sorted({s for s in (known_secrets or []) if len(s) >= 6}, key=len, reverse=True)

    def text(self, value: str) -> str:
        for s in self.known:
            value = value.replace(s, REDACTED)
        return value

    def value(self, value: Any, key: str | None = None) -> Any:
        if key is not None and SECRET_KEY.search(key):
            return REDACTED
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {k: self.value(v, str(k)) for k, v in value.items()}
        if isinstance(value, list):
            return [self.value(v) for v in value]
        return value

    def preview(self, value: str, limit: int = 80) -> str:
        value = self.text(value)
        return value if len(value) <= limit else value[:limit] + f"…(+{len(value) - limit} chars)"
