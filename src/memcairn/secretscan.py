"""Reject content that looks like a credential before it is stored."""

from __future__ import annotations

import re

# A password-looking value: no spaces, 6+ chars, letters mixed with digits.
_RANDOM = r"(?=[^\s|`'\",;]*[A-Za-z])(?=[^\s|`'\",;]*\d)[^\s|`'\",;]{6,}"

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
    (
        # "ADMIN:Abc12345", "root:Xy12ab" — a login with its password
        "login:password",
        re.compile(r"(?i)\b(admin|administrator|root|user)\s*:\s*" + _RANDOM),
    ),
    (
        # "ILO .10 / Abc12345" — a BMC address followed by its password
        "BMC password",
        re.compile(r"(?i)\b(ilo|idrac|ipmi|bmc)\b[^\n|]{0,40}?\s/\s*(\w+:)?" + _RANDOM),
    ),
]

# Table columns whose cells hold passwords: | ILO | ... | .10 / Abc12345 |
_SECRET_COLUMN = re.compile(r"(?i)\b(ilo|idrac|ipmi|bmc|heslo|password|passwd)\b")
_CELL_SECRET = re.compile(r"(?:^|\s/\s*)(\w+:)?" + _RANDOM + r"$")


class SecretError(ValueError):
    pass


def _refuse(label: str) -> None:
    raise SecretError(f"refused: content looks like a secret ({label}). "
                      "Store where to find it, not the value.")


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _check_tables(text: str) -> None:
    secret_cols: list[int] = []
    for line in text.splitlines():
        if not line.lstrip().startswith("|"):
            secret_cols = []
            continue
        cells = _cells(line)
        if not secret_cols:  # header row of a new table
            secret_cols = [i for i, c in enumerate(cells) if _SECRET_COLUMN.search(c)] or [-1]
            continue
        for i in secret_cols:
            if 0 <= i < len(cells) and _CELL_SECRET.search(cells[i].strip("`")):
                _refuse("password in a table column")


def check(text: str) -> None:
    for label, pattern in PATTERNS:
        if pattern.search(text):
            _refuse(label)
    _check_tables(text)
