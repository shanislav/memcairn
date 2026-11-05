import pytest

from memcairn import format
from memcairn.db import connect
from memcairn.server import build, hash_token
from memcairn.store import Store


@pytest.fixture
def store():
    return Store(connect(":memory:"))


def ids(hits):
    return [h.memory["id"] for h in hits]


def test_pinned_then_todos_then_by_type(store):
    fact, _ = store.remember("plain fact", entity="A")
    fb, _ = store.remember("always run tests before commit", type="feedback", entity="B")
    todo, _ = store.remember("rotate the token", type="todo", entity="C")
    pin, _ = store.remember("server is home.example", entity="D", pinned=True)
    order = ids(store.context())
    assert order[:3] == [pin["id"], todo["id"], fb["id"]]
    assert order[-1] == fact["id"]


def test_at_most_two_facts_per_entity(store):
    for i in range(5):
        store.remember(f"lumina fact {i}", entity="Lumina")
    store.remember("obed fact", entity="OBED")
    hits = store.context()
    assert sum(h.entity == "Lumina" for h in hits) == 2
    assert any(h.entity == "OBED" for h in hits)


def test_age_counts_active_days_not_calendar(store):
    old, _ = store.remember("old lumina fact", entity="Lumina")
    quiet, _ = store.remember("old obed fact", entity="OBED")
    with store.lock:
        store.conn.execute("UPDATE memory SET recorded_at = '2025-01-01T00:00:00Z', "
                           "updated_at = '2025-01-01T00:00:00Z'")
    # Lumina keeps getting work on three later days; OBED is left alone.
    for day in ("2025-03-01", "2025-06-01", "2025-09-01"):
        row, _ = store.remember(f"newer lumina fact {day}", entity="Lumina")
        with store.lock:
            store.conn.execute("UPDATE memory SET recorded_at = ?, updated_at = ? WHERE id = ?",
                               (f"{day}T00:00:00Z", f"{day}T00:00:00Z", row["id"]))
    parts = {h.memory["id"]: h.parts for h in store.context(per_entity=10)}
    assert parts[old["id"]]["active_age"] == 3
    assert parts[quiet["id"]]["active_age"] == 0


def test_todo_lifecycle(store):
    todo, _ = store.remember("migrate backups", type="todo")
    assert todo["status"] == "open"
    store.update(todo["id"], status="done")
    assert todo["id"] not in ids(store.context())
    hit = store.recall("migrate backups").hits[0]
    assert hit.status == "done"
    assert "[done]" in format.hit_line(hit)
    with pytest.raises(ValueError):
        store.update(todo["id"], status="finished")
    fact, _ = store.remember("not a todo")
    with pytest.raises(ValueError):
        store.update(fact["id"], status="done")


def test_context_http_endpoint(tmp_path):
    from starlette.testclient import TestClient

    store = Store(connect(tmp_path / "m.db"))
    store.remember("server is home.example", pinned=True)
    with store.lock:
        store.conn.execute("INSERT INTO client (name, token_hash, created_at) VALUES "
                           "('pc', ?, '2026-01-01T00:00:00Z')", (hash_token("secret-token"),))
    app = build(store, auth_url="http://testserver").streamable_http_app(host="testserver")
    with TestClient(app) as http:
        assert http.get("/context").status_code == 401
        assert http.get("/context", headers={"Authorization": "Bearer nope"}).status_code == 401
        res = http.get("/context", headers={"Authorization": "Bearer secret-token"})
        assert res.status_code == 200
        assert "server is home.example" in res.text
        assert res.text.startswith("Long-term memory")


def test_pinned_rules_in_full_and_todos_capped():
    store = Store(connect(":memory:"))
    rule = ("Na firemných serveroch iba read-only: čítanie bez pýtania, čokoľvek čo zapisuje, "
            "maže alebo reštartuje len s explicitným súhlasom; " + "x" * 150 + " KONIEC")
    store.remember(rule, type="feedback", entity="Acme", pinned=True)
    for i in range(12):
        store.remember(f"old todo number {i}", type="todo", entity=f"T{i}")
    hits = store.context(max_todos=5)
    assert sum(h.memory["type"] == "todo" for h in hits) == 5 and store.todos_left == 7
    text = format.context(hits, max_tokens=900, todos_left=store.todos_left)
    assert "KONIEC" in text.splitlines()[1]  # the pinned rule first and not cut short
    assert "+7 older open todos" in text


def test_session_overview_stays_within_its_budget(tmp_path):
    from memcairn import format

    store = Store(connect(tmp_path / "m.db"))
    for i in range(60):
        store.remember(f"fact {i} " + "about servers, backups and deploys " * 5, entity=f"E{i}")
    for i in range(12):
        store.remember(f"open chore {i}", type="todo")
    hits = store.context("global", max_todos=5)
    for budget in (300, 600, 900):
        text = format.session_overview(hits, max_tokens=budget, todos_left=store.todos_left)
        assert format.tokens(text) <= budget, budget
        assert text.startswith(format.SESSION_INTRO) and "older open todos" in text
