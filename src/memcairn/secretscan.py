"""Reject content that looks like a credential before it is stored."""

from __future__ import annotations

import re

PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{40,})\b")),
    ("Anthropic/OpenAI key", re.compile(r"\bsk-(ant-)?[A-Za-z0-9_-]{20,}\b")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    (
        "password assignment",
        re.compile(
            # value must mix letters with digits/symbols, so prose ("heslo = zmazať")
            # and plain numbers ("token: 10000") pass
            r"(?i)\b(password|passwd|heslo|secret|api[_-]?key|token)\s*[:=]\s*['\"]?"
            r"(?=[^\s'\"]*[A-Za-z])(?=[^\s'\"]*[0-9!@#$%^&*()_+=.?/~-])[^\s'\",;]{6,}"
        ),
    ),
]


class SecretError(ValueError):
    pass


def check(text: str) -> None:
    for label, pattern in PATTERNS:
        if pattern.search(text):
            raise SecretError(
                f"refused: content looks like a secret ({label}). "
                "Store where to find it, not the value."
            )
