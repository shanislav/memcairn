"""One-time codes, so nobody has to copy tokens around.

- login: the web UI password (set in the UI or with `memcairn password`), or
  `memcairn login-link`, a link that logs in once (15 min; also the way back in when the
  password is forgotten). The browser gets a session of the built-in client `web-ui` that
  lasts 30 days from its last use; logging out ends it, revoking `web-ui` ends them all.
- pair: "Add machine" in the web UI shows a short code (10 min). `memcairn pair <url>
  <code>` on the new machine trades it for a token of its own and writes it straight into
  the configuration, and the OAuth approval page (claude.ai, mobile) takes it as well. The
  token itself is never shown to anyone, nor pasted into a chat.

Codes are single use and stored as hashes. 8 characters from a 32-letter alphabet are 40
bits: guessing one within its lifetime over HTTP is not practical.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import time

from .db import now
from .store import Store

ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I
LOGIN_TTL = 15 * 60
PAIR_TTL = 10 * 60
WEB_TTL = 30 * 24 * 3600  # sliding: renewed on use
MIN_PASSWORD = 10
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
    """Trade a login code for a browser session token."""
    if not redeem(store, code, "login"):
        return None
    return new_session(store, "link")


def new_session(store: Store, via: str) -> str:
    """A browser session of the web-ui client; `via` is 'link' or 'password'."""
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
               expires_at) VALUES (?, 'access', 'web-ui', ?, '[]', ?, ?)""",
            (_hash(token), WEB_CLIENT, f"web:{via}:{int(time.time())}",
             int(time.time()) + WEB_TTL))
    return token


def session_info(store: Store, token: str):
    """(via, started) of a browser session, or None for any other kind of token."""
    row = store.conn.execute(
        "SELECT resource FROM oauth_token WHERE token_hash = ? AND client_id = 'web-ui'",
        (_hash(token),)).fetchone()
    if not row:
        return None
    if not (row["resource"] or "").startswith("web:"):  # sessions from before passwords
        return "link", 0
    _, via, started = row["resource"].split(":")
    return via, int(started)


def end_session(store: Store, token: str) -> bool:
    with store._tx():
        return store.conn.execute(
            "UPDATE oauth_token SET revoked_at = ? WHERE token_hash = ? AND client_id = 'web-ui' "
            "AND revoked_at IS NULL", (now(), _hash(token))).rowcount > 0


# ---------- the web UI password (one person, one password) ----------

def _scrypt(password: str, salt: bytes, n: int = 2 ** 15) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=n, r=8, p=1, maxmem=64 * 2 ** 20,
                          dklen=32)


def set_password(store: Store, password: str | None) -> None:
    """Set the web UI password (None removes it)."""
    with store._tx():
        if password is None:
            store.conn.execute("DELETE FROM setting WHERE key = 'ui_password'")
            return
        if len(password) < MIN_PASSWORD:
            raise ValueError(f"the password needs at least {MIN_PASSWORD} characters")
        salt = secrets.token_bytes(16)
        value = "scrypt$32768$" + "$".join(base64.b64encode(x).decode()
                                           for x in (salt, _scrypt(password, salt)))
        store.conn.execute("INSERT OR REPLACE INTO setting (key, value) VALUES "
                           "('ui_password', ?)", (value,))
        # a new password logs out the browsers that came in with the old one
        store.conn.execute("UPDATE oauth_token SET revoked_at = ? WHERE client_id = 'web-ui' "
                           "AND resource LIKE 'web:password:%' AND revoked_at IS NULL", (now(),))


def has_password(store: Store) -> bool:
    return store.conn.execute("SELECT 1 FROM setting WHERE key = 'ui_password'").fetchone() \
        is not None


def check_password(store: Store, password: str) -> bool:
    row = store.conn.execute("SELECT value FROM setting WHERE key = 'ui_password'").fetchone()
    if not row or not password:
        return False
    _, n, salt, digest = row["value"].split("$")
    return hmac.compare_digest(_scrypt(password, base64.b64decode(salt), int(n)),
                               base64.b64decode(digest))


class Throttle:
    """At most `limit` failed logins per `window` seconds, for the whole server: with one
    password there is nothing per-user to lock, and the login link stays as a way in."""

    def __init__(self, limit: int = 5, window: int = 15 * 60):
        self.limit, self.window, self.fails = limit, window, []

    def wait(self) -> int:
        """Seconds until the next attempt is allowed (0: now)."""
        t = time.time()
        self.fails = [f for f in self.fails if f > t - self.window]
        return int(self.fails[0] + self.window - t) + 1 if len(self.fails) >= self.limit else 0

    def failed(self) -> None:
        self.fails.append(time.time())


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
    """The client behind a browser session or OAuth access token, if still valid. A browser
    session is renewed for 30 days whenever it is used (at most once a day)."""
    t = int(time.time())
    row = store.conn.execute(
        """SELECT t.subject, t.client_id, t.expires_at FROM oauth_token t
           JOIN client c ON c.name = t.subject
           WHERE t.token_hash = ? AND t.kind = 'access' AND t.revoked_at IS NULL
           AND t.expires_at > ? AND c.revoked_at IS NULL""",
        (_hash(token), t)).fetchone()
    if not row:
        return None
    if row["client_id"] == WEB_CLIENT and row["expires_at"] < t + WEB_TTL - 86400:
        with store._tx():
            store.conn.execute("UPDATE oauth_token SET expires_at = ? WHERE token_hash = ?",
                               (t + WEB_TTL, _hash(token)))
    return row["subject"]
