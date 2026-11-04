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
