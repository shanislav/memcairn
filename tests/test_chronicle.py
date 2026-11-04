from memcairn.chronicle import chronicle
from memcairn.db import connect
from memcairn.store import Store


def test_chronicle_tells_the_story_in_date_order():
    store = Store(connect(":memory:"))
    old, _ = store.remember("fw3 disk is dying, long incident log ...", entity="FireClu3",
                            recorded_at="2026-07-20T00:00:00Z")
    new = store.update(old["id"], content="2026-07-20 fw3 SSD died; failover to FW1",
                       supersede=True)
    store.remember("restore scripts in /root/restore/", entity="FireClu3", type="howto",
                   recorded_at="2026-07-21T00:00:00Z")
    store.remember("return stonith to safe values", entity="FireClu3", type="todo")
    md = chronicle(store, "fireclu")  # partial, case-insensitive
    assert md.startswith("# FireClu3")
    assert "## Open todos" in md and "return stonith" in md
    assert f"~~m{old['id']}~~" in md and "long incident log" in md  # original kept
    assert md.index("2026-07-20") < md.index("2026-07-21")
    assert f"m{old['id']}" not in chronicle(store, "FireClu3", current_only=True)
    assert "failover to FW1" in md and new["id"]


def test_get_history_shows_what_a_fact_replaced():
    from memcairn import format
    store = Store(connect(":memory:"))
    old, _ = store.remember("very long original text " * 5, entity="X")
    new = store.update(old["id"], content="short conclusion", supersede=True)
    text = format.predecessors(store.predecessors(new["id"]))
    assert f"replaced m{old['id']}" in text


def test_chronicle_shows_wording_before_an_in_place_rewrite():
    store = Store(connect(":memory:"))
    row, _ = store.remember("long original incident log with every detail", entity="NAS")
    store.update(row["id"], content="short conclusion")
    md = chronicle(store, "NAS")
    assert "short conclusion" in md and "earlier wording" in md and "every detail" in md


def test_cleanup_suggestion_after_import(tmp_path):
    import json

    from memcairn import review as rv
    from memcairn.importers import import_server_memory

    store = Store(connect(":memory:"))
    p = tmp_path / "memory.json"
    p.write_text("\n".join(json.dumps({"type": "entity", "name": f"E{i}", "entityType": "x",
                                       "observations": ["y" * 700]}) for i in range(6)),
                 encoding="utf-8")
    import_server_memory(store, p)
    n, saving = rv.suggest_cleanup(store)
    assert n == 6 and saving > 0
    todos = [h for h in store.context() if h.memory["type"] == "todo"]
    assert todos and "cleanup" in todos[0].memory["content"]
    rv.suggest_cleanup(store)  # running again updates the same todo
    assert sum(h.memory["type"] == "todo" for h in store.context()) == 1


def test_cleanup_prompt_lists_items():
    import anyio

    from memcairn.server import build
    store = Store(connect(":memory:"))
    store.remember("z" * 800, entity="Big")
    srv = build(store, stdio_client="t")
    res = anyio.run(lambda: srv.get_prompt("cleanup", {"count": "5"}))
    text = "".join(getattr(m.content, "text", "") for m in res.messages)
    assert "at most 400" in text and "m1" in text
