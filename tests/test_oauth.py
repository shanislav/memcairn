import base64
import hashlib
import secrets
from urllib.parse import parse_qs, urlparse

import pytest
from starlette.testclient import TestClient

from memcairn.db import connect
from memcairn.server import build, hash_token
from memcairn.store import Store

REDIRECT = "https://claude.ai/api/mcp/auth_callback"
MCP_HEADERS = {"Accept": "application/json, text/event-stream",
               "MCP-Protocol-Version": "2025-11-25"}


def add_client(store, name, token):
    with store.lock:
        store.conn.execute("INSERT INTO client (name, token_hash, default_scope, created_at) "
                           "VALUES (?, ?, 'global', '2026-01-01T00:00:00Z')",
                           (name, hash_token(token)))


@pytest.fixture
def http(tmp_path):
    store = Store(connect(tmp_path / "m.db"))
    add_client(store, "claude-ai", "mcn_approve")
    add_client(store, "pc", "mcn_static")
    from mcp.server.transport_security import TransportSecuritySettings

    app = build(store, auth_url="http://localhost", oauth=True).streamable_http_app(
        host="localhost", stateless_http=True, json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))
    with TestClient(app, base_url="http://localhost", follow_redirects=False) as client:
        client.store = store
        yield client


def pkce():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge.rstrip(b"=").decode()


def start(http):
    meta = http.get("/.well-known/oauth-authorization-server").json()
    reg = http.post(meta["registration_endpoint"], json={
        "client_name": "Claude", "redirect_uris": [REDIRECT],
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"]}).json()
    verifier, challenge = pkce()
    res = http.get("/authorize", params={
        "response_type": "code", "client_id": reg["client_id"], "redirect_uri": REDIRECT,
        "code_challenge": challenge, "code_challenge_method": "S256", "state": "xyz"})
    assert res.status_code == 302
    approve = urlparse(res.headers["location"])
    req = parse_qs(approve.query)["req"][0]
    return reg, verifier, req


def call_tool(http, token, name, args):
    return http.post("/mcp", headers={**MCP_HEADERS, "Authorization": f"Bearer {token}"},
                     json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": name, "arguments": args}})


def test_full_flow_acts_as_the_approving_client(http):
    reg, verifier, req = start(http)
    page = http.get("/oauth/approve", params={"req": req})
    assert page.status_code == 200 and "Claude" in page.text and "claude.ai" in page.text
    bad = http.post("/oauth/approve", data={"req": req, "token": "nope", "action": "approve"})
    assert bad.status_code == 401
    ok = http.post("/oauth/approve", data={"req": req, "token": "mcn_approve",
                                           "action": "approve"})
    assert ok.status_code == 302
    back = parse_qs(urlparse(ok.headers["location"]).query)
    assert back["state"] == ["xyz"]
    tok = http.post("/token", data={
        "grant_type": "authorization_code", "code": back["code"][0], "redirect_uri": REDIRECT,
        "client_id": reg["client_id"], "code_verifier": verifier}).json()
    assert tok["access_token"].startswith("mca_") and tok["refresh_token"]
    # the code works once
    again = http.post("/token", data={
        "grant_type": "authorization_code", "code": back["code"][0], "redirect_uri": REDIRECT,
        "client_id": reg["client_id"], "code_verifier": verifier})
    assert again.status_code == 400

    res = call_tool(http, tok["access_token"], "remember", {"content": "via claude.ai"})
    assert res.status_code == 200, res.text
    row = http.store.conn.execute(
        "SELECT client FROM memory WHERE content = 'via claude.ai'").fetchone()
    assert row["client"] == "claude-ai"  # recorded as the client that approved

    new = http.post("/token", data={"grant_type": "refresh_token",
                                    "refresh_token": tok["refresh_token"],
                                    "client_id": reg["client_id"]}).json()
    assert new["access_token"] != tok["access_token"]
    reuse = http.post("/token", data={"grant_type": "refresh_token",
                                      "refresh_token": tok["refresh_token"],
                                      "client_id": reg["client_id"]})
    assert reuse.status_code == 400  # rotated

    # revoking the Memcairn client cuts off its OAuth tokens too
    with http.store.lock:
        http.store.conn.execute("UPDATE client SET revoked_at = 'x' WHERE name = 'claude-ai'")
    assert call_tool(http, new["access_token"], "recall", {"query": "x"}).status_code == 401


def test_static_tokens_still_work_and_deny_redirects(http):
    assert call_tool(http, "mcn_static", "recall", {"query": "x"}).status_code == 200
    assert call_tool(http, "wrong", "recall", {"query": "x"}).status_code == 401
    assert http.get("/context", headers={"Authorization": "Bearer mcn_static"}).status_code == 200
    _, _, req = start(http)
    res = http.post("/oauth/approve", data={"req": req, "action": "deny"})
    assert "error=access_denied" in res.headers["location"]
    assert http.get("/oauth/approve", params={"req": req}).status_code == 400  # spent
