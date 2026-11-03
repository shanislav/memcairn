import json

import pytest

from memcairn import format
from memcairn.db import connect
from memcairn.importers import import_server_memory
from memcairn.secretscan import SecretError
from memcairn.store import Conflict, Store, normalize_type


@pytest.fixture
def store():
    return Store(connect(":memory:"))


def test_remember_and_recall(store):
    store.remember("Obed app runs on FastAPI and React", entity="OBED", type="project")
    store.remember("Lumina replaces Radarr", entity="Lumina")
    res = store.recall("fastapi")
    assert [h.entity for h in res.hits] == ["OBED"]
    assert res.coverage == 1.0


def test_recall_folds_diacritics_and_inflection(store):
    store.remember("Firemná aplikácia na objednávanie obedov", entity="OBED")
    assert store.recall("firemna").hits
    # Slovak inflection: "obedy" shares the stem with "obedov" only in deep mode.
    assert store.recall("obedy", deep=True).hits


def test_low_confidence_hint(store):
    store.remember("Plex runs on shano.eu")
    res = store.recall("plex kubernetes cluster autoscaling")
    text = format.recall(res, max_tokens=600, limit=5, explain=False, deep=False)
    assert "low confidence" in text


def test_stopwords_do_not_count_as_matches(store):
    store.remember("Plex runs natively on the home server")
    res = store.recall("recept na sviečkovú")  # only "na" would match
    assert res.coverage < 0.5


def test_weak_results_are_trimmed(store):
    for i in range(10):
        store.remember(f"server note {i} about backups")
    text = format.recall(store.recall("server kubernetes autoscaling"), max_tokens=600,
                         limit=5, explain=False, deep=False)
    assert text.count("\nm") <= 3
    assert "low confidence" in text


def test_supersede_keeps_history(store):
    old, _ = store.remember("Lumina prod runs upstream", entity="Lumina")
    new = store.update(old["id"], content="Lumina prod runs the fork master", supersede=True)
    hits = store.recall("lumina prod").hits
    assert [h.memory["id"] for h in hits] == [new["id"]]
    deep = store.recall("lumina prod", deep=True).hits
    assert {h.memory["id"] for h in deep} == {old["id"], new["id"]}
    assert store.get([old["id"]])[0]["superseded_by"] == new["id"]


def test_optimistic_version_conflict(store):
    row, _ = store.remember("fact one")
    store.update(row["id"], content="fact one edited", expected_version=1)
    with pytest.raises(Conflict):
        store.update(row["id"], content="stale edit", expected_version=1)
    ops = [h["op"] for h in store.history(row["id"])]
    assert ops == ["create", "update"]


def test_forget_and_undo(store):
    row, _ = store.remember("temporary thing")
    store.forget(row["id"])
    assert not store.recall("temporary").hits
    store.forget(row["id"], undo=True)
    assert store.recall("temporary").hits


def test_idempotency(store):
    a, _ = store.remember("same", idempotency_key="k1", client="pc")
    b, _ = store.remember("same", idempotency_key="k1", client="pc")
    assert a["id"] == b["id"]


def test_near_duplicates(store):
    store.remember("shano.eu firewall is foomuuri not firewalld")
    _, dups = store.remember("firewall on shano.eu is foomuuri, not firewalld")
    assert dups


def test_secret_rejected(store):
    with pytest.raises(SecretError):
        store.remember("token ghp_" + "a" * 36)
    with pytest.raises(SecretError):
        store.remember("db password=hunter2secret")
    # prose and numbers are not secrets
    store.remember("admin heslo = zmazať po prvom prihlásení")
    store.remember("max token: 10000 per request")


def test_scope_inheritance(store):
    store.remember("global fact about servers")
    store.remember("project fact about servers", scope="project:obed")
    assert len(store.recall("servers", scope="project:obed").hits) == 2
    assert len(store.recall("servers").hits) == 1


def test_expired_hidden(store):
    store.remember("short lived note", expires_at="2000-01-01T00:00:00Z")
    assert not store.recall("short lived").hits
    assert store.recall("short lived", deep=True).hits


def test_type_normalization():
    assert normalize_type("Projekt") == "project"
    assert normalize_type("servers") == "server"


def test_budget_truncates(store):
    for i in range(30):
        store.remember(f"server note number {i} " + "x" * 120)
    text = format.recall(store.recall("server note"), max_tokens=200, limit=30,
                         explain=False, deep=False)
    assert format.tokens(text) <= 230
    assert "more" in text


def test_import_server_memory(store, tmp_path):
    lines = [
        {"type": "entity", "name": "OBED", "entityType": "projekt",
         "observations": ["FastAPI backend", "React frontend"]},
        {"type": "entity", "name": "shano.eu", "entityType": "server", "observations": []},
        {"type": "relation", "from": "OBED", "to": "shano.eu", "relationType": "runs_on"},
    ]
    p = tmp_path / "memory.json"
    p.write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")
    stats = import_server_memory(store, p)
    assert stats == {"entities": 2, "facts": 2, "relations": 1, "skipped_secrets": 0}
    assert store.find_entity("obed")["type"] == "project"
