"""OAuth 2.1 for clients that can't send a static bearer token (claude.ai custom connectors,
the Claude mobile and desktop apps' connectors).

The MCP SDK provides the endpoints (metadata, dynamic client registration, authorize,
token, revoke) and PKCE checks; this module stores clients, codes and tokens in SQLite and
adds one approval page. Approval needs no password: whoever connects pastes a Memcairn
client token (`memcairn token add claude-ai`) once. The OAuth tokens then act as that
client: writes are recorded as by=claude-ai, its default scope applies, and revoking the
client (`token revoke claude-ai`) cuts off every OAuth token issued for it.

Static `mcn_…` tokens keep working on the same endpoint, so Claude Code is unaffected.
Only hashes of tokens and codes are stored.
"""

from __future__ import annotations

import html
import json
import secrets
import time

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from .db import now
from .store import Store

ACCESS_TTL = 24 * 3600
REFRESH_TTL = 90 * 24 * 3600
CODE_TTL = 300
PENDING_TTL = 900


def _hash(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode()).hexdigest()


class MemcairnOAuth:
    def __init__(self, store: Store, public_url: str):
        self.store = store
        self.public_url = public_url.rstrip("/")

    # ---------- static tokens (Claude Code, CLI, hooks) ----------

    def client_for_static(self, token: str) -> str | None:
        row = self.store.conn.execute(
            "SELECT name FROM client WHERE token_hash = ? AND revoked_at IS NULL",
            (_hash(token),)).fetchone()
        return row["name"] if row else None

    def _seen(self, name: str) -> None:
        with self.store.lock:
            self.store.conn.execute("UPDATE client SET last_seen_at = ? WHERE name = ?",
                                    (now(), name))

    # ---------- clients ----------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        row = self.store.conn.execute("SELECT info FROM oauth_client WHERE client_id = ?",
                                      (client_id,)).fetchone()
        return OAuthClientInformationFull.model_validate_json(row["info"]) if row else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        with self.store._tx():
            self.store.conn.execute(
                "INSERT OR REPLACE INTO oauth_client (client_id, info, created_at) VALUES (?,?,?)",
                (client_info.client_id, client_info.model_dump_json(), now()))

    # ---------- authorization: park the request, the approval page finishes it ----------

    async def authorize(self, client: OAuthClientInformationFull,
                        params: AuthorizationParams) -> str:
        req = secrets.token_urlsafe(24)
        with self.store._tx():
            self.store.conn.execute(
                "DELETE FROM oauth_pending WHERE created < ?", (time.time() - PENDING_TTL,))
            self.store.conn.execute(
                "INSERT INTO oauth_pending (id, client_id, params, created) VALUES (?,?,?,?)",
                (req, client.client_id, params.model_dump_json(), time.time()))
        return f"{self.public_url}/oauth/approve?req={req}"

    def pending(self, req: str):
        row = self.store.conn.execute(
            "SELECT * FROM oauth_pending WHERE id = ? AND created >= ?",
            (req, time.time() - PENDING_TTL)).fetchone()
        if not row:
            return None, None
        return row, AuthorizationParams.model_validate_json(row["params"])

    def approve(self, req: str, memcairn_token: str) -> str:
        """Approval page POST: returns the redirect URL (with code, or with an error)."""
        row, params = self.pending(req)
        if not row:
            raise AuthorizeError("invalid_request", "approval expired, start again")
        name = self.client_for_static(memcairn_token.strip())
        if not name:  # or a pairing code from "Add machine" in the web UI
            from .pairing import pair

            paired = pair(self.store, memcairn_token)
            name = paired[0] if paired else None
        if not name:
            raise ValueError("unknown or revoked token")
        code = secrets.token_urlsafe(32)
        data = AuthorizationCode(
            code=code, scopes=params.scopes or [], expires_at=time.time() + CODE_TTL,
            client_id=row["client_id"], code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource, subject=name)
        with self.store._tx():
            self.store.conn.execute("DELETE FROM oauth_pending WHERE id = ?", (req,))
            self.store.conn.execute(
                "INSERT INTO oauth_code (code_hash, data, expires_at) VALUES (?,?,?)",
                (_hash(code), data.model_dump_json(exclude={"code"}), data.expires_at))
        return construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)

    def deny(self, req: str) -> str | None:
        row, params = self.pending(req)
        if not row:
            return None
        with self.store._tx():
            self.store.conn.execute("DELETE FROM oauth_pending WHERE id = ?", (req,))
        return construct_redirect_uri(str(params.redirect_uri), error="access_denied",
                                      state=params.state)

    async def load_authorization_code(self, client: OAuthClientInformationFull,
                                      authorization_code: str) -> AuthorizationCode | None:
        row = self.store.conn.execute(
            "SELECT data, expires_at FROM oauth_code WHERE code_hash = ?",
            (_hash(authorization_code),)).fetchone()
        if not row or row["expires_at"] < time.time():
            return None
        data = json.loads(row["data"]) | {"code": authorization_code}
        code = AuthorizationCode.model_validate(data)
        return code if code.client_id == client.client_id else None

    # ---------- tokens ----------

    def _issue(self, client_id: str, subject: str, scopes: list[str],
               resource: str | None) -> OAuthToken:
        access, refresh = "mca_" + secrets.token_urlsafe(32), "mcr_" + secrets.token_urlsafe(32)
        t = int(time.time())
        rows = [(_hash(access), "access", client_id, subject, json.dumps(scopes), resource,
                 t + ACCESS_TTL), (_hash(refresh), "refresh", client_id, subject,
                                   json.dumps(scopes), resource, t + REFRESH_TTL)]
        self.store.conn.executemany(
            """INSERT INTO oauth_token (token_hash, kind, client_id, subject, scopes, resource,
               expires_at) VALUES (?,?,?,?,?,?,?)""", rows)
        return OAuthToken(access_token=access, expires_in=ACCESS_TTL, refresh_token=refresh,
                          scope=" ".join(scopes) or None)

    async def exchange_authorization_code(self, client: OAuthClientInformationFull,
                                          authorization_code: AuthorizationCode) -> OAuthToken:
        with self.store._tx():
            gone = self.store.conn.execute(
                "DELETE FROM oauth_code WHERE code_hash = ?",
                (_hash(authorization_code.code),)).rowcount == 0
            if gone:  # used twice
                raise TokenError("invalid_grant", "authorization code already used")
            return self._issue(client.client_id, authorization_code.subject or "",
                               authorization_code.scopes, authorization_code.resource)

    def _token_row(self, token: str, kind: str):
        row = self.store.conn.execute(
            """SELECT t.* FROM oauth_token t JOIN client c ON c.name = t.subject
               WHERE t.token_hash = ? AND t.kind = ? AND t.revoked_at IS NULL
               AND t.expires_at > ? AND c.revoked_at IS NULL""",
            (_hash(token), kind, int(time.time()))).fetchone()
        return row

    async def load_refresh_token(self, client: OAuthClientInformationFull,
                                 refresh_token: str) -> RefreshToken | None:
        row = self._token_row(refresh_token, "refresh")
        if not row or row["client_id"] != client.client_id:
            return None
        return RefreshToken(token=refresh_token, client_id=row["client_id"],
                            scopes=json.loads(row["scopes"]), expires_at=row["expires_at"],
                            resource=row["resource"], subject=row["subject"])

    async def exchange_refresh_token(self, client: OAuthClientInformationFull,
                                     refresh_token: RefreshToken, scopes: list[str]) -> OAuthToken:
        with self.store._tx():  # rotate: the old refresh token is spent
            self.store.conn.execute("UPDATE oauth_token SET revoked_at = ? WHERE token_hash = ?",
                                    (now(), _hash(refresh_token.token)))
            return self._issue(client.client_id, refresh_token.subject or "",
                               scopes or refresh_token.scopes, refresh_token.resource)

    async def load_access_token(self, token: str) -> AccessToken | None:
        name = self.client_for_static(token)
        if name:  # a plain Memcairn token
            self._seen(name)
            return AccessToken(token=token, client_id=name, scopes=[])
        row = self._token_row(token, "access")
        if not row:
            return None
        self._seen(row["subject"])
        return AccessToken(token=token, client_id=row["client_id"],
                           scopes=json.loads(row["scopes"]), expires_at=row["expires_at"],
                           resource=row["resource"], subject=row["subject"])

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        with self.store._tx():
            self.store.conn.execute("UPDATE oauth_token SET revoked_at = ? WHERE token_hash = ?",
                                    (now(), _hash(token.token)))

    async def exchange_identity_assertion(self, client, params) -> OAuthToken:
        raise TokenError("unsupported_grant_type", "not supported")


def approve_page(client_name: str, redirect_host: str, req: str, error: str = "") -> str:
    e = html.escape
    err = f'<p class="err">{e(error)}</p>' if error else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Memcairn</title>
<style>body{{font:16px system-ui,sans-serif;max-width:28rem;margin:3rem auto;padding:0 1rem;
color:#222;background:#fafafa}}input{{width:100%;padding:.6rem;font:inherit;box-sizing:border-box}}
button{{padding:.6rem 1rem;font:inherit;margin:.8rem .5rem 0 0}}.err{{color:#b00}}
@media (prefers-color-scheme:dark){{body{{color:#eee;background:#161616}}}}</style></head><body>
<h1>Connect to Memcairn</h1>
<p><b>{e(client_name)}</b> wants to read and write this memory server.
After approval it is sent back to <b>{e(redirect_host)}</b>.</p>
<p>To approve, enter a pairing code: in the Memcairn web UI open Overview, choose
<b>Add machine</b> and give it a name (e.g. claude-ai). A client token works too.</p>
{err}<form method="post"><input type="hidden" name="req" value="{e(req)}">
<input name="token" autocomplete="off" placeholder="K7QM-42XD" autofocus>
<button name="action" value="approve">Approve</button>
<button name="action" value="deny">Deny</button></form></body></html>"""
