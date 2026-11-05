import io
import json
import time

import pytest
from starlette.testclient import TestClient

from memcairn import clientsetup, pairing
from memcairn.connect import connect_local, pair
from memcairn.db import connect
from memcairn.server import build, hash_token
from memcairn.store import Store

H = {"Authorization": "Bearer mcn_admin"}


@pytest.fixture
def http(tmp_path):
    store = Store(connect(tmp_path / "m.db"))
    with store.lock:
        store.conn.execute("INSERT INTO client (name, token_hash, created_at) VALUES "
                           "('admin', ?, '2026-01-01T00:00:00Z')", (hash_token("mcn_admin"),))
    store.remember("Lumina prod runs on shano.eu", entity="Lumina", type="project")
    app = build(store, auth_url="https://mem.example").streamable_http_app(host="testserver")
    with TestClient(app) as client:
        client.store = store
        yield client


def test_login_link_is_single_use_and_gives_a_working_session(http):
    code = pairing.create(http.store, "login", by="cli")
    assert len(code) == 9 and code[4] == "-"
    assert http.post("/api/login", json={"code": "WRONG-CODE"}).status_code == 401
    r = http.post("/api/login", json={"code": code.lower().replace("-", " ")})
    token = r.json()["token"]
    assert http.post("/api/login", json={"code": code}).status_code == 401  # spent
    s = {"Authorization": f"Bearer {token}"}
    assert http.get("/api/stats", headers=s).json()["client"] == "web-ui"
    # revoking web-ui logs out every browser; a new link brings it back
    http.post("/api/client/web-ui/revoke", json={"confirm": "web-ui"}, headers=H)
    assert http.get("/api/stats", headers=s).status_code == 401
    t2 = http.post("/api/login", json={"code": pairing.create(http.store, "login")}).json()
    assert http.get("/api/stats", headers={"Authorization": f"Bearer {t2['token']}"}
                    ).status_code == 200
    assert http.get("/api/stats", headers=s).status_code == 401  # old session stays dead


def test_codes_expire(http):
    code = pairing.create(http.store, "login")
    with http.store.lock:
        http.store.conn.execute("UPDATE onetime SET expires_at = ?", (int(time.time()) - 1,))
    assert http.post("/api/login", json={"code": code}).status_code == 401


def test_add_machine_code_becomes_a_client_token_once(http):
    assert http.post("/api/pair", json={"name": "bad name!"}, headers=H).status_code == 400
    assert http.post("/api/pair", json={"name": "laptop"}).status_code == 401
    r = http.post("/api/pair", json={"name": "laptop"}, headers=H).json()
    assert r["command"] == f"memcairn pair https://mem.example {r['code']}"
    got = http.post("/pair", json={"code": r["code"]}).json()
    assert got["name"] == "laptop" and got["url"] == "https://mem.example"
    assert http.post("/pair", json={"code": r["code"]}).status_code == 401
    assert http.get("/api/stats", headers={"Authorization": f"Bearer {got['token']}"}
                    ).json()["client"] == "laptop"
    # a second machine with the same name gets its own client
    r2 = http.post("/api/pair", json={"name": "laptop"}, headers=H).json()
    assert http.post("/pair", json={"code": r2["code"]}).json()["name"] == "laptop-2"
    assert pairing.session_client(http.store, got["token"]) is None  # not a session token


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_pair_writes_client_file_and_mcp_entry_and_unsetup_removes_them(tmp_path, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)  # no claude CLI: manual entry
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude.json").write_text(json.dumps({"projects": {}}), encoding="utf-8")
    sent = {}

    def opener(req, timeout):
        sent["url"], sent["body"] = req.full_url, json.loads(req.data)
        return _Resp(json.dumps({"name": "pc", "token": "mcn_new", "url": "x"}).encode())

    out = []
    assert pair(home, "https://mem.example/mcp", "K7QM-42XD", out.append, opener=opener) == "pc"
    assert sent == {"url": "https://mem.example/pair", "body": {"code": "K7QM-42XD"}}
    assert "mcn_new" not in "\n".join(out)  # the token is never printed
    assert 'token = "mcn_new"' in (home / ".memcairn" / "client.toml").read_text()
    entry = json.loads((home / ".claude.json").read_text())["mcpServers"]["memory"]
    assert entry == {"type": "http", "url": "https://mem.example/mcp",
                     "headers": {"Authorization": "Bearer mcn_new"}}
    hooks = json.loads((home / ".claude" / "settings.json").read_text())["hooks"]
    assert '" context' in hooks["SessionStart"][0]["hooks"][0]["command"]

    clientsetup.run_unsetup(home, lambda q: True, lambda s: None)
    assert "memory" not in json.loads((home / ".claude.json").read_text())["mcpServers"]
    assert "SessionStart" not in json.loads(
        (home / ".claude" / "settings.json").read_text()).get("hooks", {})


def test_connect_local_uses_stdio_and_adds_the_hook_once(tmp_path):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    connect_local(home, lambda s: None)
    connect_local(home, lambda s: None)
    entry = json.loads((home / ".claude.json").read_text())["mcpServers"]["memory"]
    assert entry["type"] == "stdio" and entry["args"][:2] == ["serve", "--client"]
    settings = json.loads((home / ".claude" / "settings.json").read_text())
    assert len(settings["hooks"]["SessionStart"]) == 1


def test_oauth_approval_takes_a_pairing_code(tmp_path):
    from memcairn.oauth import MemcairnOAuth

    store = Store(connect(tmp_path / "m.db"))
    provider = MemcairnOAuth(store, "https://mem.example")
    with store._tx():
        store.conn.execute(
            "INSERT INTO oauth_pending (id, client_id, params, created) VALUES (?,?,?,?)",
            ("req1", "c1", json.dumps({
                "state": "s", "scopes": [], "code_challenge": "x" * 43,
                "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
                "redirect_uri_provided_explicitly": True, "resource": None}), time.time()))
    code = pairing.create(store, "pair", "claude-ai")
    with pytest.raises(ValueError):
        provider.approve("req1", "NOPE-NOPE")
    url = provider.approve("req1", code)
    assert url.startswith("https://claude.ai/api/mcp/auth_callback?code=")
    assert store.conn.execute("SELECT 1 FROM client WHERE name = 'claude-ai'").fetchone()


def test_cli_add_machine_and_login_link_print_working_codes(tmp_path):
    from typer.testing import CliRunner

    from memcairn.cli import app

    db = tmp_path / "m.db"
    run = CliRunner().invoke
    out = run(app, ["add-machine", "laptop", "--db", str(db), "--url", "https://m.example"])
    assert out.exit_code == 0 and "memcairn pair https://m.example " in out.output
    code = out.output.split("memcairn pair https://m.example ")[1].split()[0]
    store = Store(connect(db))
    assert pairing.pair(store, code)[0] == "laptop"
    out = run(app, ["login-link", "--db", str(db), "--url", "https://m.example"])
    assert "https://m.example/ui#login=" in out.output
    assert run(app, ["add-machine", "bad name", "--db", str(db)]).exit_code != 0


def test_password_login_logout_and_change(http):
    assert http.get("/api/auth").json() == {"password": False}
    assert http.post("/api/login", json={"password": "anything-at-all"}).status_code == 401
    link = http.post("/api/login", json={"code": pairing.create(http.store, "login")}
                     ).json()["token"]
    L = {"Authorization": f"Bearer {link}"}
    # a client token can't set the web password; a browser session can
    assert http.post("/api/password", json={"new": "long enough pw"}, headers=H
                     ).status_code == 403
    assert http.post("/api/password", json={"new": "short"}, headers=L).status_code == 400
    assert http.post("/api/password", json={"new": "my cairn on the hill"}, headers=L
                     ).status_code == 200
    assert http.get("/api/auth").json() == {"password": True}
    tok = http.post("/api/login", json={"password": "my cairn on the hill"}).json()["token"]
    P = {"Authorization": f"Bearer {tok}"}
    assert http.get("/api/stats", headers=P).json()["client"] == "web-ui"
    # a password session must know the current password to change it
    assert http.post("/api/password", json={"current": "nope", "new": "another long one"},
                     headers=P).status_code == 401
    assert http.post("/api/password", json={"current": "my cairn on the hill",
                                            "new": "another long one"}, headers=P
                     ).status_code == 200
    assert http.get("/api/stats", headers=P).status_code == 401  # old-password sessions end
    # logout ends only this browser's session
    assert http.post("/api/logout", headers=L).json() == {"ended": True}
    assert http.get("/api/stats", headers=L).status_code == 401
    t2 = http.post("/api/login", json={"password": "another long one"}).json()["token"]
    assert http.get("/api/stats", headers={"Authorization": f"Bearer {t2}"}).status_code == 200


def test_wrong_passwords_are_throttled(http):
    pairing.set_password(http.store, "correct horse battery")
    for _ in range(5):
        assert http.post("/api/login", json={"password": "wrong guess!"}).status_code == 401
    r = http.post("/api/login", json={"password": "correct horse battery"})
    assert r.status_code == 429 and "try again" in r.json()["error"]
    # the login link still works while passwords are blocked
    assert http.post("/api/login", json={"code": pairing.create(http.store, "login")}).json()


def test_sessions_renew_on_use(http):
    tok = http.post("/api/login", json={"code": pairing.create(http.store, "login")}
                    ).json()["token"]
    with http.store.lock:
        http.store.conn.execute("UPDATE oauth_token SET expires_at = ? WHERE client_id = "
                                "'web-ui'", (int(time.time()) + 3600,))
    assert http.get("/api/stats", headers={"Authorization": f"Bearer {tok}"}).status_code == 200
    left = http.store.conn.execute("SELECT expires_at FROM oauth_token WHERE client_id = "
                                   "'web-ui'").fetchone()[0] - time.time()
    assert left > 29 * 86400
