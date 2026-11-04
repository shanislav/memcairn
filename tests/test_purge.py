from pathlib import Path

from typer.testing import CliRunner

from memcairn import docs
from memcairn import purge as pg
from memcairn.cli import app
from memcairn.db import connect
from memcairn.store import Store

LEAK = "Qx7-leaked-Zz9"


def leaky_store(path: Path) -> Store:
    store = Store(connect(path))
    m, _ = store.remember("storage1 admin password is somewhere", entity="Storage")
    # got in through an update (secretscan sees no pattern here), now in history too
    store.update(m["id"], content=f"storage1 admin uses {LEAK} for the console")
    store.update(m["id"], content="storage1 admin password is local only")
    docs.put_doc(store, "inv", f"# Inv\n\n| IP | Name | Note |\n|---|---|---|\n"
                 f"| 10.0.0.1 | a | {LEAK} |\n| 10.0.0.2 | b | x |\n| 10.0.0.3 | c | y |\n",
                 base_version=0)
    docs.put_doc(store, "inv", "# Inv\n\nclean now\n", base_version=1)
    store.log_call(client="t", tool="recall", query=f"find {LEAK}", tokens=1, hits=0, ms=1)
    return store


def test_purge_removes_text_everywhere_including_free_pages(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMCAIRN_LOG_QUERIES", "1")
    db = tmp_path / "m.db"
    store = leaky_store(db)
    assert pg.leftovers(store, [LEAK]) > 0
    dry = pg.purge_text(store, [LEAK], dry_run=True)
    assert dry["memory_history"] >= 1 and dry["document_version"] == 1
    assert pg.leftovers(store, [LEAK]) > 0  # dry run changed nothing
    pg.purge_text(store, [LEAK])
    assert pg.leftovers(store, [LEAK]) == 0
    store.conn.close()
    raw = db.read_bytes() + (db.with_name("m.db-wal").read_bytes()
                             if db.with_name("m.db-wal").exists() else b"")
    assert LEAK.encode() not in raw  # not even in free pages


def test_purge_cli_reads_values_from_a_file(tmp_path):
    db = tmp_path / "m.db"
    leaky_store(db).conn.close()
    values = tmp_path / "v.txt"
    values.write_text(LEAK + "\n", encoding="utf-8")
    out = CliRunner().invoke(app, ["purge", "--values", str(values), "--db", str(db)]).output
    assert "left anywhere: 0" in out and LEAK not in out
