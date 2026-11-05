"""One-time codes, so nobody has to copy tokens around.

- login: `memcairn login-link` prints a link to the web UI that logs in once (15 min).
  The browser gets a session (7 days) of the built-in client `web-ui`; revoking `web-ui`
  logs out every browser.
- pair: "Add machine" in the web UI shows a short code (10 min). `memcairn pair <url>
  <code>` on the new machine trades it for a token of its own and writes it straight into
  the configuration, and the OAuth approval page (claude.ai, mobile) takes it as well. The
  token itself is never shown to anyone, nor pasted into a chat.

Codes are single use and stored as hashes. 8 characters from a 32-letter alphabet are 40
bits: guessing one within its lifetime over HTTP is not practical.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import time

from .db import now
from .store import Store

ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I
LOGIN_TTL = 15 * 60
PAIR_TTL = 10 * 60
WEB_TTL = 7 * 24 * 3600
WEB_CLIENT = "web-ui"
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,47}$")


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def normalize(code: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (code or "").upper())


def new_token() -> str:
    return "mcn_" + secrets.token_urlsafe(32)


def create(store: Store, kind: str, name: str | None = None, *, by: str | None = None) -> str:
    """A fresh code like K7QM-42XD; kind is 'login' or 'pair' (pair needs a client name)."""
    if kind == "pair" and not NAME.match(name or ""):
        raise ValueError("machine name: letters, digits, . _ - (up to 48)")
    code = "".join(secrets.choice(ALPHABET) for _ in range(8))
    ttl = LOGIN_TTL if kind == "login" else PAIR_TTL
    with store._tx():
        store.conn.execute("DELETE FROM onetime WHERE expires_at < ?", (int(time.time()),))
        store.conn.execute(
            "INSERT INTO onetime (code_hash, kind, name, created_by, expires_at) VALUES (?,?,?,?,?)",
            (_hash(code), kind, name, by, int(time.time()) + ttl))
    return f"{code[:4]}-{code[4:]}"


def redeem(store: Store, code: str, kind: str):
    """Spend a code: its row, or None when unknown, expired or already used."""
    h = _hash(normalize(code))
    with store._tx():
        row = store.conn.execute(
            "SELECT * FROM onetime WHERE code_hash = ? AND kind = ? AND expires_at >= ?",
            (h, kind, int(time.time()))).fetchone()
        if row:
            store.conn.execute("DELETE FROM onetime WHERE code_hash = ?", (h,))
    return row


def add_client(store: Store, name: str, scope: str = "global") -> tuple[str, str]:
    """A new client with its own token: (name, token). An existing name gets -2, -3…"""
    with store._tx():
        taken = {r["name"] for r in store.conn.execute("SELECT name FROM client")}
        final, n = name, 2
        while final in taken:
            final, n = f"{name}-{n}", n + 1
        token = new_token()
        store.scope_id(scope)
        store.conn.execute(
            "INSERT INTO client (name, token_hash, default_scope, created_at) VALUES (?,?,?,?)",
            (final, _hash(token), scope, now()))
    return final, token


def pair(store: Store, code: str) -> tuple[str, str] | None:
    """Trade a pairing code for (client name, token)."""
    row = redeem(store, code, "pair")
    return add_client(store, row["name"]) if row else None


def web_session(store: Store, code: str) -> str | None:
    """Trade a login code for a browser session token (7 days)."""
    if not redeem(store, code, "login"):
        return None
    token = new_token()
    with store._tx():
        exists = store.conn.execute("SELECT revoked_at FROM client WHERE name = ?",
                                    (WEB_CLIENT,)).fetchone()
        if not exists:  # the sessions hang off this client; its own token is never used
            store.conn.execute(
                "INSERT INTO client (name, token_hash, default_scope, created_at) "
                "VALUES (?,?,?,?)", (WEB_CLIENT, _hash(new_token()), "global", now()))
        elif exists["revoked_at"]:  # a new login link after "log out all browsers"
            store.conn.execute("UPDATE client SET revoked_at = NULL WHERE name = ?",
                               (WEB_CLIENT,))
        store.conn.execute(
            """INSERT INTO oauth_token (token_hash, kind, client_id, subject, scopes, resource,
               expires_at) VALUES (?, 'access', 'web-ui', ?, '[]', NULL, ?)""",
            (_hash(token), WEB_CLIENT, int(time.time()) + WEB_TTL))
    return token


def revoke_client(store: Store, name: str) -> bool:
    """Revoke a client and, for good, every OAuth/browser session issued for it: a later
    `token add` or login link with the same name must not bring old sessions back."""
    with store._tx():
        n = store.conn.execute("UPDATE client SET revoked_at = ? WHERE name = ? "
                               "AND revoked_at IS NULL", (now(), name)).rowcount
        store.conn.execute("UPDATE oauth_token SET revoked_at = ? WHERE subject = ? "
                           "AND revoked_at IS NULL", (now(), name))
    return bool(n)


def session_client(store: Store, token: str) -> str | None:
    """The client behind a browser session or OAuth access token, if still valid."""
    row = store.conn.execute(
        """SELECT t.subject FROM oauth_token t JOIN client c ON c.name = t.subject
           WHERE t.token_hash = ? AND t.kind = 'access' AND t.revoked_at IS NULL
           AND t.expires_at > ? AND c.revoked_at IS NULL""",
        (_hash(token), int(time.time()))).fetchone()
    return row["subject"] if row else None
