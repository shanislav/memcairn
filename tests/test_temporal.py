from datetime import date

from memcairn import format
from memcairn.db import connect
from memcairn.store import Store
from memcairn.temporal import parse

TODAY = date(2026, 11, 4)  # a Wednesday


def test_relative_ranges():
    assert parse("čo sme robili minulý týždeň", TODAY) == (
        "čo sme robili", date(2026, 10, 26), date(2026, 11, 2))
    assert parse("lumina yesterday", TODAY) == ("lumina", date(2026, 11, 3), date(2026, 11, 4))
    assert parse("zmeny za posledných 10 dní", TODAY)[1:] == (date(2026, 10, 25),
                                                              date(2026, 11, 5))
    assert parse("last month", TODAY) == ("", date(2026, 10, 1), date(2026, 11, 1))


def test_months_and_iso():
    assert parse("firewall v októbri", TODAY) == ("firewall", date(2026, 10, 1),
                                                  date(2026, 11, 1))
    assert parse("UPS december", TODAY)[1] == date(2025, 12, 1)  # last December
    assert parse("deploy 2026-05", TODAY)[1:] == (date(2026, 5, 1), date(2026, 6, 1))
    assert parse("incident 2026-10-29", TODAY)[1:] == (date(2026, 10, 29), date(2026, 10, 30))


def test_ambiguous_words_are_not_dates():
    for q in ("Jan Novák telefón", "may the deploy fail", "plex server"):
        assert parse(q, TODAY) == (q, None, None)
    assert parse("in may 2026", TODAY)[1] == date(2026, 5, 1)


def test_recall_filters_by_time():
    store = Store(connect(":memory:"))
    old, _ = store.remember("firewall rule added", recorded_at="2026-09-10T00:00:00Z")
    new, _ = store.remember("firewall rule removed", recorded_at="2026-10-20T00:00:00Z")
    hits = store.recall("firewall v októbri 2026").hits
    assert [h.memory["id"] for h in hits] == [new["id"]]
    only_time = store.recall("2026-09").hits  # time alone lists that period
    assert [h.memory["id"] for h in only_time] == [old["id"]]


def test_filler_words_fall_back_to_the_period():
    store = Store(connect(":memory:"))
    row, _ = store.remember("firewall rule removed", recorded_at="2026-10-20T00:00:00Z")
    hits = store.recall("čo sa robilo v októbri 2026").hits
    assert [h.memory["id"] for h in hits] == [row["id"]]


def test_single_days():
    june3 = (date(2023, 6, 3), date(2023, 6, 4))
    for q in ("pet died on 3 June, 2023", "pet died June 3rd 2023", "pet died 3. júna 2023",
              "pet died 3.6.2023"):
        assert parse(q, TODAY) == ("pet died", *june3), q
    assert parse("backup on March 3", TODAY)[1] == date(2026, 3, 3)  # no year: the last one
    assert parse("I may go 2 times", TODAY) == ("I may go 2 times", None, None)


def test_a_date_with_nothing_noted_then_searches_around_it_and_then_anywhere():
    store = Store(connect(":memory:"))
    near, _ = store.remember("joined a gym", recorded_at="2023-06-20T00:00:00Z")
    far, _ = store.remember("left the gym", recorded_at="2024-01-05T00:00:00Z")
    # nothing was written on June 16: the fact noted four days later is found
    res = store.recall("gym on 16 June 2023")
    assert [h.memory["id"] for h in res.hits] == [near["id"]] and "week around" in res.note
    # nothing within a week either: any time, and the answer says so
    res = store.recall("gym on 1 March 2023")
    assert {h.memory["id"] for h in res.hits} == {near["id"], far["id"]}
    text = format.recall(res, max_tokens=600, limit=5, explain=False, deep=False)
    assert "these are from any time" in text
    assert not store.recall("gym in June 2023").note  # found in the period itself
