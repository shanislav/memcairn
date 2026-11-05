import json

from memcairn.db import connect
from memcairn.importers import ESTIMATED, fact_dates, import_server_memory, record_date
from memcairn.store import Store


def test_only_a_leading_date_is_a_record_date():
    assert record_date("2026-10-01: switched prod to the fork") == "2026-10-01"
    assert record_date("FIX Chimera1C7 (.211) 2026-07-01: stop salt-minion") == "2026-07-01"
    assert record_date("APC Smart-UPS on USB, battery made 2005-10-06, replace soon") is None
    assert record_date("planned for 2099-01-01") is None


def test_undated_facts_inherit_and_are_marked_estimated():
    dates = fact_dates(["2026-07-01: a", "b without a date"])
    assert dates == [("2026-07-01T00:00:00Z", False), ("2026-07-01T00:00:00Z", True)]
    assert fact_dates(["no dates here"], "2026-06-03") == [("2026-06-03T00:00:00Z", True)]


def test_import_uses_the_oldest_record_date_not_a_date_in_some_text(tmp_path):
    p = tmp_path / "memory.json"
    rows = [{"type": "entity", "name": "UPS", "entityType": "x",
             "observations": ["Battery made 2005-10-06, still holds 20 minutes"]},
            {"type": "entity", "name": "Lumina", "entityType": "x",
             "observations": ["2026-06-03: first deploy", "2026-09-01: fork"]}]
    p.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    store = Store(connect(":memory:"))
    import_server_memory(store, p)
    ups = store.conn.execute("SELECT recorded_at, tags FROM memory WHERE content LIKE "
                             "'Battery%'").fetchone()
    assert ups["recorded_at"].startswith("2026-06-03") and ESTIMATED in json.loads(ups["tags"])
    dated = store.conn.execute("SELECT tags FROM memory WHERE content LIKE '2026-09-01%'"
                               ).fetchone()
    assert ESTIMATED not in json.loads(dated["tags"])
