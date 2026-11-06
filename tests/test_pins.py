"""Pinned text reaches every session on every machine, so agents only ask; the user pins."""
import anyio
import pytest
from starlette.testclient import TestClient

from memcairn import auth, format
from memcairn.db import connect
from memcairn.server import build, hash_token
from memcairn.store import Store

H = {"Authorization": "Bearer mcn_laptop"}


def call(server, name, **args) -> str:
    res = anyio.run(lambda: server.call_tool(name, args))
    return "".join(c.text for c in res.content)


@pytest.fixture
def store():
    return Store(connect(":memory:"))


def test_agent_pin_is_a_request_until_the_user_approves(store):
    srv = build(store, stdio_client="laptop")
    out = call(srv, "remember", content="Always answer in Slovak", type="feedback", pinned=True)
    assert "pin requested" in out
    row = store.get([1])[0]
    assert not row["pinned"] and store.pin_requested(1)
    brief = format.brief_overview(store, None)
    assert "Always answer in Slovak" not in brief and "1 pin request(s)" in brief
    assert store.pin_requests()[0]["requested_by"] == "laptop"

    assert store.decide_pin(1, approve=True, client="web-ui")["pinned"]
    assert not store.pin_requested(1)
    assert "Always answer in Slovak" in format.brief_overview(store, None)


def test_rejecting_keeps_the_fact_unpinned(store):
    row, _ = store.remember("x is y", pinned=True, agent=True)
    store.decide_pin(row["id"], approve=False)
    assert not store.get([row["id"]])[0]["pinned"] and not store.pin_requests()
    with pytest.raises(LookupError):
        store.decide_pin(row["id"], approve=True)


def test_agent_edit_of_a_pinned_fact_needs_approval_again(store):
    row, _ = store.remember("Deploy only after a backup", pinned=True)  # the user's pin
    srv = build(store, stdio_client="laptop")
    call(srv, "update", id=f"m{row['id']}", importance=0.9)  # no text change: stays pinned
    assert store.get([row["id"]])[0]["pinned"]

    out = call(srv, "update", id=f"m{row['id']}", content="Deploy without asking")
    assert "pin requested" in out
    assert not store.get([row["id"]])[0]["pinned"] and store.pin_requested(row["id"])

    store.decide_pin(row["id"], approve=True)
    out = call(srv, "update", id=f"m{row['id']}", content="Deploy, no backup", supersede=True)
    new = int(out.split("new m")[1].split()[0])
    assert not store.get([new])[0]["pinned"] and store.pin_requested(new)


def test_agent_may_unpin(store):
    row, _ = store.remember("old rule", pinned=True)
    call(build(store, stdio_client="laptop"), "update", id=f"m{row['id']}", pinned=False)
    assert not store.get([row["id"]])[0]["pinned"]


@pytest.fixture
def http(tmp_path):
    s = Store(connect(tmp_path / "m.db"))
    with s.lock:
        s.conn.execute("INSERT INTO client (name, token_hash, created_at) VALUES "
                       "('laptop', ?, '2026-01-01T00:00:00Z')", (hash_token("mcn_laptop"),))
    app = build(s, auth_url="http://testserver").streamable_http_app(host="testserver")
    with TestClient(app) as client:
        client.store = s
        yield client


def test_web_api_pins_directly_only_from_a_browser_session(http):
    row, _ = http.store.remember("a rule")
    r = http.post(f"/api/memory/{row['id']}", headers=H, json={"pinned": True}).json()
    assert r["pin_requested"] and not r["memory"]["pinned"]  # a machine token only asks
    assert [m["id"] for m in http.get("/api/pins", headers=H).json()] == [row["id"]]
    assert http.post(f"/api/memory/{row['id']}/pin", headers=H,
                     json={"approve": True}).status_code == 403

    token = http.post("/api/login", json={"code": auth.create(http.store, "login")}
                      ).json()["token"]
    B = {"Authorization": f"Bearer {token}"}
    r = http.post(f"/api/memory/{row['id']}/pin", headers=B, json={"approve": True})
    assert r.json()["memory"]["pinned"] and http.get("/api/pins", headers=B).json() == []
    other, _ = http.store.remember("another rule")
    r = http.post(f"/api/memory/{other['id']}", headers=B, json={"pinned": True}).json()
    assert r["memory"]["pinned"] and not r["pin_requested"]
