"""Redaction.

A secret must never reach a terminal report, a JSON artefact, or a CI log. But
redaction has a second failure mode, and it is the one that actually bit: on a
real image the container died with `Cannot connect to host ***redacted***`,
because the storage endpoint was an env value and every env value was treated as
a secret. The one fact needed to diagnose the crash was the fact removed.

So the rule is by *name*, not by value. A variable whose name says it holds a
key, a token, a secret or a password is masked; a hostname, a port, a database
name or a bucket is not. Request headers stay masked wholesale — `Authorization`
matches none of those four words and is a credential regardless.
"""

from __future__ import annotations

import re
from typing import Any

MASK = "***redacted***"
#: Below this length a value is too likely to collide with ordinary text
#: ("app", "db") and masking it would corrupt the report instead of protecting it.
MIN_SECRET_LENGTH = 5

#: What a secret-bearing variable name looks like. Substring, not whole word:
#: `AWS_SECRET_ACCESS_KEY`, `apikey` and `DB_PASSWORD` all have to match.
SECRET_NAME_PATTERN = re.compile(r"KEY|TOKEN|SECRET|PASSWORD", re.IGNORECASE)


def names_a_secret(name: str) -> bool:
    """Whether a variable of this name should have its value masked."""
    return SECRET_NAME_PATTERN.search(name) is not None


class Redactor:
    def __init__(self, secrets: list[str]) -> None:
        # Longest first, so an overlapping short secret cannot break a long one.
        self._secrets = sorted(
            {s for s in secrets if len(s) >= MIN_SECRET_LENGTH}, key=len, reverse=True
        )

    def text(self, value: str) -> str:
        for secret in self._secrets:
            value = value.replace(secret, MASK)
        return value

    def apply(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {k: self.apply(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.apply(v) for v in value]
        return value
