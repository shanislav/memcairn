import shutil

import pytest

from memcairn import docs
from memcairn.db import connect
from memcairn.store import Conflict, Store
from memcairn.sync import Entry, merge3, sync_entry


class FakeRemote:
    """The sync client's view of the server, backed by a real Store in memory."""

    def __init__(self):
        self.store = Store(connect(":memory:"))
        self.todos = []

    def list(self):
        return {r["name"]: dict(r) for r in docs.list_docs(self.store)}

    def get(self, name, version=None):
        return dict(docs.get_doc(self.store, name, version))

    def put(self, name, content, base_version):
        try:
            row = docs.put_doc(self.store, name, content, base_version=base_version)
            return 200, {"version": row["version"], "sha": row["sha"]}
        except Conflict as e:
            return 409, {"error": str(e)}
        except ValueError as e:
            return 400, {"error": str(e)}

    def delete(self, name, base_version):
        cur = docs.get_doc(self.store, name)
        if cur["version"] != base_version:
            return 409, {"error": "changed since"}
        docs.delete_doc(self.store, name)
        return 200, {"deleted": name}

    def todo(self, text):
        self.todos.append(text)


TEXT = "# Servers\n\nalpha 10.0.0.1\n\nbeta 10.0.0.2\n\ngamma 10.0.0.3\n"


def machine(tmp_path, name):
    return Entry("servers", tmp_path / name / "servers.md")


def run(remote, e, state):
    return sync_entry(remote, e, remote.list().get(e.name), state)


def test_upload_download_and_noop(tmp_path):
    r, pc, srv = FakeRemote(), machine(tmp_path, "pc"), machine(tmp_path, "srv")
    pc.path.parent.mkdir()
    pc.path.write_text(TEXT, encoding="utf-8")
    s_pc, s_srv = {}, {}
    assert "uploaded new" in run(r, pc, s_pc)
    assert "downloaded" in run(r, srv, s_srv)
    assert srv.path.read_text(encoding="utf-8") == TEXT
    assert run(r, pc, s_pc) is None and run(r, srv, s_srv) is None  # nothing changed


def test_change_travels_one_way(tmp_path):
    r, pc, srv = FakeRemote(), machine(tmp_path, "pc"), machine(tmp_path, "srv")
    pc.path.parent.mkdir()
    pc.path.write_text(TEXT, encoding="utf-8")
    s_pc, s_srv = {}, {}
    run(r, pc, s_pc), run(r, srv, s_srv)
    srv.path.write_text(TEXT.replace("10.0.0.2", "10.0.0.22"), encoding="utf-8")
    assert "uploaded servers v2" in run(r, srv, s_srv)
    assert "updated servers from server v2" in run(r, pc, s_pc)
    assert "10.0.0.22" in pc.path.read_text(encoding="utf-8")


@pytest.mark.skipif(not shutil.which("git"), reason="needs git merge-file")
def test_both_changed_different_places_merges(tmp_path):
    r, pc, srv = FakeRemote(), machine(tmp_path, "pc"), machine(tmp_path, "srv")
    pc.path.parent.mkdir()
    pc.path.write_text(TEXT, encoding="utf-8")
    s_pc, s_srv = {}, {}
    run(r, pc, s_pc), run(r, srv, s_srv)
    srv.path.write_text(TEXT.replace("gamma 10.0.0.3", "gamma 10.0.0.33"), encoding="utf-8")
    run(r, srv, s_srv)
    pc.path.write_text(TEXT.replace("alpha 10.0.0.1", "alpha 10.0.0.11"), encoding="utf-8")
    assert "merged" in run(r, pc, s_pc)
    merged = pc.path.read_text(encoding="utf-8")
    assert "10.0.0.11" in merged and "10.0.0.33" in merged
    assert r.get("servers")["content"] == merged and not r.todos


@pytest.mark.skipif(not shutil.which("git"), reason="needs git merge-file")
def test_both_changed_same_line_is_a_conflict_not_an_overwrite(tmp_path):
    r, pc, srv = FakeRemote(), machine(tmp_path, "pc"), machine(tmp_path, "srv")
    pc.path.parent.mkdir()
    pc.path.write_text(TEXT, encoding="utf-8")
    s_pc, s_srv = {}, {}
    run(r, pc, s_pc), run(r, srv, s_srv)
    srv.path.write_text(TEXT.replace("beta 10.0.0.2", "beta 10.0.0.7"), encoding="utf-8")
    run(r, srv, s_srv)
    mine = TEXT.replace("beta 10.0.0.2", "beta 10.0.0.9")
    pc.path.write_text(mine, encoding="utf-8")
    line = run(r, pc, s_pc)
    assert line.startswith("CONFLICT")
    assert pc.path.read_text(encoding="utf-8") == mine  # untouched
    conflict = pc.path.with_name("servers.md.conflict").read_text(encoding="utf-8")
    assert "<<<<<<<" in conflict and "10.0.0.7" in conflict and "10.0.0.9" in conflict
    assert "10.0.0.7" in r.get("servers")["content"]  # server untouched too
    assert r.todos and "servers" in r.todos[0]


def test_stale_base_is_rejected_by_the_server():
    store = Store(connect(":memory:"))
    docs.put_doc(store, "x", "one", base_version=0)
    docs.put_doc(store, "x", "two", base_version=1)
    with pytest.raises(Conflict):
        docs.put_doc(store, "x", "three", base_version=1)
    assert docs.get_doc(store, "x", 1)["content"] == "one"  # every version kept
    with pytest.raises(ValueError):
        docs.put_doc(store, "Bad Name", "x", base_version=0)


def test_merge3_clean_and_conflict():
    if not shutil.which("git"):
        pytest.skip("needs git")
    base = "a\nb\nc\n"
    assert merge3("A\nb\nc\n", base, "a\nb\nC\n") == ("A\nb\nC\n", True)
    _, clean = merge3("a\nX\nc\n", base, "a\nY\nc\n")
    assert not clean


def test_docs_http_routes(tmp_path):
    from starlette.testclient import TestClient

    from memcairn.server import build, hash_token

    store = Store(connect(tmp_path / "m.db"))
    with store.lock:
        store.conn.execute("INSERT INTO client (name, token_hash, created_at) VALUES "
                           "('pc', ?, '2026-01-01T00:00:00Z')", (hash_token("t0k"),))
    app = build(store, auth_url="http://testserver").streamable_http_app(host="testserver")
    h = {"Authorization": "Bearer t0k"}
    with TestClient(app) as http:
        assert http.get("/docs").status_code == 401
        assert http.put("/docs/servers", json={"content": "v1", "base_version": 0},
                        headers=h).json()["version"] == 1
        assert http.put("/docs/servers", json={"content": "v2", "base_version": 0},
                        headers=h).status_code == 409
        assert http.get("/docs/servers", headers=h).json()["content"] == "v1"
        assert http.get("/docs", headers=h).json()[0]["name"] == "servers"
        assert http.put("/docs/secret", json={"content": "password=hunter2secret",
                                              "base_version": 0}, headers=h).status_code == 400
        # delete only the version the caller last saw
        assert http.delete("/docs/servers?base_version=7", headers=h).status_code == 409
        assert http.delete("/docs/servers?base_version=1").status_code == 401
        assert http.delete("/docs/servers?base_version=1", headers=h).status_code == 200
        assert http.get("/docs/servers", headers=h).status_code == 404


def test_import_turns_long_notes_into_documents(tmp_path):
    from memcairn.importers import import_claude_memory

    store = Store(connect(":memory:"))
    (tmp_path / "MEMORY.md").write_text("- index", encoding="utf-8")
    (tmp_path / "feedback_x.md").write_text(
        "---\nname: feedback-x\ndescription: be brief\ntype: feedback\n---\nAnswer first.",
        encoding="utf-8")
    (tmp_path / "project_servers.md").write_text(
        "---\nname: Servers\ndescription: server inventory\nmetadata:\n  type: project\n---\n"
        + "host 10.0.0.1 root\n" * 120, encoding="utf-8")
    stats = import_claude_memory(store, tmp_path)
    assert (stats["facts"], stats["documents"]) == (2, 1)
    assert docs.get_doc(store, "project_servers")["content"].startswith("---")
    ref = store.recall("server inventory").hits[0].memory
    assert ref["type"] == "reference" and "doc:project_servers" in ref["content"]
    assert import_claude_memory(store, tmp_path)["documents"] == 1  # idempotent, no new version
    assert docs.get_doc(store, "project_servers")["version"] == 1


def test_get_reads_a_document_through_mcp():
    import anyio

    from memcairn.server import build
    store = Store(connect(":memory:"))
    docs.put_doc(store, "servers", "alpha 10.0.0.1", base_version=0)
    srv = build(store, stdio_client="t")
    res = anyio.run(lambda: srv.call_tool("get", {"ids": ["doc:servers"]}))
    assert "alpha 10.0.0.1" in "".join(c.text for c in res.content)


def sync_set(remote, folder, state, skip=()):
    from memcairn.sync import automemory_entries

    out = []
    for e in automemory_entries(folder, "automemory", remote.list(), set(skip), out):
        line = sync_entry(remote, e, remote.list().get(e.name), state)
        if line:
            out.append(line)
    return out


def test_auto_memory_folder_is_shared_and_deletes_travel_safely(tmp_path):
    r = FakeRemote()
    pc, srv = tmp_path / "pc-memory", tmp_path / "srv-memory"
    pc.mkdir()
    (pc / "MEMORY.md").write_text("- [Read-only](feedback_ro.md): firm servers\n", "utf-8")
    (pc / "feedback_ro.md").write_text("Only read on firm servers.\n", "utf-8")
    (pc / "servers.md").write_text("mapped elsewhere\n", "utf-8")
    (pc / "secret.md").write_text("db password=hunter2secret\n", "utf-8")
    s_pc, s_srv = {}, {}
    out = sync_set(r, pc, s_pc, skip={(pc / "servers.md").resolve()})
    assert any("skipped secret.md" in line for line in out)
    assert set(r.list()) == {"automemory.memory", "automemory.feedback_ro"}

    sync_set(r, srv, s_srv)  # second machine, empty folder: gets everything
    assert (srv / "MEMORY.md").read_text("utf-8").startswith("- [Read-only]")
    assert (srv / "feedback_ro.md").is_file()

    (pc / "feedback_ro.md").unlink()  # deleted on the PC, unchanged on the server
    assert any("deleted automemory.feedback_ro" in x for x in sync_set(r, pc, s_pc))
    assert any("removed feedback_ro.md" in x for x in sync_set(r, srv, s_srv))
    assert not (srv / "feedback_ro.md").exists()

    (pc / "feedback_x.md").write_text("x\n", "utf-8")
    sync_set(r, pc, s_pc), sync_set(r, srv, s_srv)
    (srv / "feedback_x.md").write_text("x changed on the server machine\n", "utf-8")
    sync_set(r, srv, s_srv)  # uploads the change
    (pc / "feedback_x.md").unlink()  # the PC deletes a file someone else just changed
    sync_set(r, pc, s_pc)
    assert (pc / "feedback_x.md").read_text("utf-8").startswith("x changed")  # kept
