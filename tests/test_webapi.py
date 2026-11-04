import pytest
from starlette.testclient import TestClient

from memcairn import docs
from memcairn.db import connect
from memcairn.server import build, hash_token
from memcairn.store import Store

H = {"Authorization": "Bearer mcn_web"}


@pytest.fixture
def http(tmp_path):
    store = Store(connect(tmp_path / "m.db"))
    with store.lock:
        store.conn.execute("INSERT INTO client (name, token_hash, created_at) VALUES "
                           "('browser', ?, '2026-01-01T00:00:00Z')", (hash_token("mcn_web"),))
    store.remember("Lumina prod runs on shano.eu", entity="Lumina", type="project")
    store.remember("check plex port", entity="Plex", type="todo")
    docs.put_doc(store, "inv", "# Inv\n\n## Home\n\nshano.eu is 192.168.9.1\n", base_version=0)
    app = build(store, auth_url="http://testserver").streamable_http_app(host="testserver")
    with TestClient(app) as client:
        client.store = store
        yield client


def test_ui_page_is_static_and_api_needs_a_token(http):
    page = http.get("/ui")
    assert page.status_code == 200 and "<title>Memcairn</title>" in page.text
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert "Lumina" not in page.text  # no data without a token
    assert http.get("/api/search").status_code == 401
    assert http.get("/api/stats", headers=H).json()["client"] == "browser"


def test_search_edit_forget_through_the_api(http):
    r = http.get("/api/search", params={"q": "lumina prod"}, headers=H).json()
    mid = r["items"][0]["id"]
    assert r["items"][0]["entity_name"] == "Lumina"
    assert http.get("/api/search", params={"q": "shano.eu"}, headers=H).json()["parts"]
    todos = http.get("/api/search", params={"status": "open"}, headers=H).json()["items"]
    assert [t["type"] for t in todos] == ["todo"]

    d = http.get(f"/api/memory/{mid}", headers=H).json()
    upd = http.post(f"/api/memory/{mid}", headers=H, json={
        "content": "Lumina prod runs on shano.eu (fork master)",
        "expected_version": d["memory"]["version"]}).json()["memory"]
    assert upd["version"] == 2
    stale = http.post(f"/api/memory/{mid}", headers=H, json={"content": "x", "expected_version": 1})
    assert stale.status_code == 409
    hist = http.get(f"/api/memory/{mid}", headers=H).json()["history"]
    assert len(hist) == 2 and hist[-1]["by"] == "browser"
    gone = http.post(f"/api/memory/{mid}/forget", headers=H, json={}).json()["memory"]
    assert gone["deleted_at"]
    assert http.post(f"/api/memory/{mid}/forget", headers=H,
                     json={"undo": True}).json()["memory"]["deleted_at"] is None

    new = http.post("/api/memory", headers=H, json={"content": "db password=hunter2secret"})
    assert new.status_code == 400  # secrets refused here too
    ok = http.post("/api/memory", headers=H, json={"content": "Immich runs on shano.eu",
                                                   "entity": "Immich"}).json()
    assert ok["memory"]["entity_id"]
    assert {e["name"] for e in http.get("/api/entities", headers=H).json()} >= {"Lumina", "Immich"}
    assert http.get("/api/memory/99999", headers=H).status_code == 404
