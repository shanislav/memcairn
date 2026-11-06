"""Who may use the server, without anyone copying tokens around.

- **Clients**: one per machine or app, each with its own static token (only its hash is
  stored). Revoking a client also ends every OAuth and browser session issued for it.
- **Pairing**: "Add machine" in the web UI (or `memcairn add-machine`) makes a one-time code
  (10 min). `memcairn pair <url> <code>` on the new machine trades it for a token of its
  own and writes it straight into the configuration; the OAuth approval page (claude.ai,
  mobile) takes the code as well. Nobody sees the token.
- **Web UI**: one password (one person, no accounts), stored as a scrypt hash, wrong tries
  throttled. `memcairn login-link` prints a link that logs in once (15 min): the first
  login and the way back in after a forgotten password. A browser gets a session of the
  built-in client `web-ui`, valid 30 days from its last use; signing out ends it.
- **Admin actions** (add machine, revoke, permanent delete, password) need a browser
  session: a machine's token, or a connector's, can read and write memory but can't mint
  new tokens or cut off others.

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

from .db import hash_token, now
from .store import Store

ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I
LOGIN_TTL = 15 * 60
PAIR_TTL = 10 * 60
WEB_TTL = 30 * 24 * 3600  # sliding: renewed on use
MIN_PASSWORD = 10
CODE_GUESSES = 20  # wrong one-time codes per 15 minutes, all kinds together
WEB_CLIENT = "web-ui"
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,47}$")


def check_name(name: str) -> str:
    """A valid name for a new client; `web-ui` is taken by the browser sessions."""
    if not NAME.match(name or ""):
        raise ValueError("machine name: letters, digits, . _ - (up to 48)")
    if name.lower() == WEB_CLIENT:
        raise ValueError(f"'{WEB_CLIENT}' is reserved for the web UI's browser sessions")
    return name


def normalize(code: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (code or "").upper())


def new_token() -> str:
    return "mcn_" + secrets.token_urlsafe(32)


# ---------- one-time codes ----------

def create(store: Store, kind: str, name: str | None = None, *, by: str | None = None) -> str:
    """A fresh code like K7QM-42XD; kind is 'login' or 'pair' (pair needs a client name)."""
    if kind == "pair":
        check_name(name or "")
    code = "".join(secrets.choice(ALPHABET) for _ in range(8))
    ttl = LOGIN_TTL if kind == "login" else PAIR_TTL
    with store._tx():
        store.conn.execute("DELETE FROM onetime WHERE expires_at < ?", (int(time.time()),))
        store.conn.execute(
            "INSERT INTO onetime (code_hash, kind, name, created_by, expires_at) VALUES (?,?,?,?,?)",
            (hash_token(code), kind, name, by, int(time.time()) + ttl))
    return f"{code[:4]}-{code[4:]}"


class TooManyGuesses(ValueError):
    """Too many wrong codes lately; no code is accepted until the window passes."""


def redeem(store: Store, code: str, kind: str):
    """Spend a code: its row, or None when unknown, expired or already used. Raises
    TooManyGuesses after CODE_GUESSES wrong codes in 15 minutes (the server-side
    `memcairn token add` still works then)."""
    guesses = vars(store).setdefault("code_guesses", Throttle(CODE_GUESSES))
    wait = guesses.wait()
    if wait:
        raise TooManyGuesses(f"too many wrong codes; try again in {wait // 60 + 1} min")
    h = hash_token(normalize(code))
    with store._tx():
        row = store.conn.execute(
            "SELECT * FROM onetime WHERE code_hash = ? AND kind = ? AND expires_at >= ?",
            (h, kind, int(time.time()))).fetchone()
        if row:
            store.conn.execute("DELETE FROM onetime WHERE code_hash = ?", (h,))
    if not row:
        guesses.failed()
    return row


# ---------- clients ----------

def add_client(store: Store, name: str, scope: str = "global") -> tuple[str, str]:
    """A new client with its own token: (name, token). An existing name gets -2, -3…"""
    check_name(name)
    with store._tx():
        taken = {r["name"] for r in store.conn.execute("SELECT name FROM client")}
        final, n = name, 2
        while final in taken:
            final, n = f"{name}-{n}", n + 1
        token = new_token()
        store.scope_id(scope)
        store.conn.execute(
            "INSERT INTO client (name, token_hash, default_scope, created_at) VALUES (?,?,?,?)",
            (final, hash_token(token), scope, now()))
    return final, token


def pair(store: Store, code: str) -> tuple[str, str] | None:
    """Trade a pairing code for (client name, token)."""
    row = redeem(store, code, "pair")
    return add_client(store, row["name"]) if row else None


def revoke_client(store: Store, name: str) -> bool:
    """Revoke a client and, for good, every OAuth/browser session issued for it: a later
    `token add` or login with the same name must not bring old sessions back."""
    with store._tx():
        n = store.conn.execute("UPDATE client SET revoked_at = ? WHERE name = ? "
                               "AND revoked_at IS NULL", (now(), name)).rowcount
        store.conn.execute("UPDATE oauth_token SET revoked_at = ? WHERE subject = ? "
                           "AND revoked_at IS NULL", (now(), name))
    return bool(n)


def static_client(store: Store, token: str) -> str | None:
    """The client a static `mcn_…` token belongs to (and note that it was seen)."""
    row = store.conn.execute(
        "SELECT name FROM client WHERE token_hash = ? AND revoked_at IS NULL",
        (hash_token(token),)).fetchone()
    if not row:
        return None
    _seen(store, row["name"])
    return row["name"]


def _seen(store: Store, name: str) -> None:
    with store.lock:
        store.conn.execute("UPDATE client SET last_seen_at = ? WHERE name = ?", (now(), name))


# ---------- sessions (browser logins and OAuth access tokens) ----------

def session_client(store: Store, token: str) -> str | None:
    """The client behind a browser session or OAuth access token, if still valid. A browser
    session is renewed for 30 days whenever it is used (at most once a day)."""
    t = int(time.time())
    row = store.conn.execute(
        """SELECT t.subject, t.client_id, t.expires_at FROM oauth_token t
           JOIN client c ON c.name = t.subject
           WHERE t.token_hash = ? AND t.kind = 'access' AND t.revoked_at IS NULL
           AND t.expires_at > ? AND c.revoked_at IS NULL""",
        (hash_token(token), t)).fetchone()
    if not row:
        return None
    if row["client_id"] == WEB_CLIENT and row["expires_at"] < t + WEB_TTL - 86400:
        with store._tx():
            store.conn.execute("UPDATE oauth_token SET expires_at = ? WHERE token_hash = ?",
                               (t + WEB_TTL, hash_token(token)))
    _seen(store, row["subject"])
    return row["subject"]


def client_for(store: Store, token: str) -> str | None:
    """The client name for any kind of token, or None."""
    return (static_client(store, token) or session_client(store, token)) if token else None


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
                "VALUES (?,?,?,?)", (WEB_CLIENT, hash_token(new_token()), "global", now()))
        elif exists["revoked_at"]:  # logging in again after "log out all browsers"
            store.conn.execute("UPDATE client SET revoked_at = NULL WHERE name = ?",
                               (WEB_CLIENT,))
        store.conn.execute(
            """INSERT INTO oauth_token (token_hash, kind, client_id, subject, scopes, resource,
               expires_at) VALUES (?, 'access', ?, ?, '[]', ?, ?)""",
            (hash_token(token), WEB_CLIENT, WEB_CLIENT, f"web:{via}:{int(time.time())}",
             int(time.time()) + WEB_TTL))
    return token


def session_info(store: Store, token: str) -> tuple[str, int] | None:
    """(via, started) of a valid browser session, or None for any other token."""
    row = store.conn.execute(
        "SELECT resource FROM oauth_token WHERE token_hash = ? AND client_id = ? "
        "AND revoked_at IS NULL AND expires_at > ?",
        (hash_token(token), WEB_CLIENT, int(time.time()))).fetchone()
    if not row:
        return None
    if not (row["resource"] or "").startswith("web:"):  # sessions from before passwords
        return "link", 0
    _, via, started = row["resource"].split(":")
    return via, int(started)


def fresh_from_link(info: tuple[str, int]) -> bool:
    """A session that came from a login link within its lifetime (15 min): the way back in
    after a forgotten password, so it may set one without the current password."""
    via, started = info
    return via == "link" and time.time() - started < LOGIN_TTL


def end_session(store: Store, token: str) -> bool:
    with store._tx():
        return store.conn.execute(
            "UPDATE oauth_token SET revoked_at = ? WHERE token_hash = ? AND client_id = ? "
            "AND revoked_at IS NULL", (now(), hash_token(token), WEB_CLIENT)).rowcount > 0


# ---------- the web UI password ----------

def _scrypt(password: str, salt: bytes, n: int = 2 ** 15) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=n, r=8, p=1, maxmem=64 * 2 ** 20,
                          dklen=32)


def hash_password(password: str) -> str:
    """The stored form (slow on purpose: run it off the event loop)."""
    if len(password) < MIN_PASSWORD:
        raise ValueError(f"the password needs at least {MIN_PASSWORD} characters")
    salt = secrets.token_bytes(16)
    return "scrypt$32768$" + "$".join(base64.b64encode(x).decode()
                                      for x in (salt, _scrypt(password, salt)))


def verify_password(stored: str | None, password: str) -> bool:
    """Compare against the stored form (slow on purpose: run it off the event loop)."""
    if not stored or not password:
        return False
    _, n, salt, digest = stored.split("$")
    return hmac.compare_digest(_scrypt(password, base64.b64decode(salt), int(n)),
                               base64.b64decode(digest))


def stored_password(store: Store) -> str | None:
    row = store.conn.execute("SELECT value FROM setting WHERE key = 'ui_password'").fetchone()
    return row["value"] if row else None


def save_password(store: Store, stored: str | None) -> None:
    """Store a hash from hash_password (None removes the password). Browsers that logged in
    with the old password are logged out."""
    with store._tx():
        if stored is None:
            store.conn.execute("DELETE FROM setting WHERE key = 'ui_password'")
            return
        store.conn.execute("INSERT OR REPLACE INTO setting (key, value) VALUES "
                           "('ui_password', ?)", (stored,))
        store.conn.execute("UPDATE oauth_token SET revoked_at = ? WHERE client_id = ? "
                           "AND resource LIKE 'web:password:%' AND revoked_at IS NULL",
                           (now(), WEB_CLIENT))


def set_password(store: Store, password: str | None) -> None:
    """Set the web UI password (None removes it); for the CLI and tests."""
    save_password(store, None if password is None else hash_password(password))


def check_password(store: Store, password: str) -> bool:
    return verify_password(stored_password(store), password)


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
