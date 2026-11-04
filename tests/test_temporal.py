from datetime import date

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
