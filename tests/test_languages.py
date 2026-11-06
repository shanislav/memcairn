"""Recall that doesn't depend on word lists for one language."""
from memcairn.db import connect
from memcairn.store import Store, split_unspaced, terms


def test_unspaced_scripts_are_searched_by_character_pairs():
    assert terms("报销系统") == ["报销", "销系", "系统"]
    assert terms("社内Wikiはどこ")[:3] == ["社内", "wiki", "はど"]
    assert "ど" in terms("どこ")[0]  # kana marks are part of the letter, not diacritics
    assert terms("Účetnictví") == ["ucetnictvi"]
    assert split_unspaced("apollo 内存").split() == ["apollo", "内存"]

    store = Store(connect(":memory:"))
    row, _ = store.remember("报销系统部署在服务器 apollo 上，使用 Odoo 17。", entity="报销系统")
    store.remember("会议室的投影仪是爱普生 EB-W06。", entity="会议室")
    store.remember("勤怠システムはクラウドの KING OF TIME を使っている。")
    hits = store.recall("报销系统部署在哪台服务器").hits
    assert hits[0].memory["id"] == row["id"]
    assert store.recall("勤怠システム").hits[0].memory["content"].startswith("勤怠")


def test_words_found_everywhere_count_little():
    store = Store(connect(":memory:"))
    for i in range(8):
        store.remember(f"server s{i} runs the backup agent", entity="servers")
    rare, _ = store.remember("Mailgun sends the invoices", entity="billing")
    w = store._term_weights(["server", "mailgun", "unknownword"])
    assert w["mailgun"] > 3 * w["server"]
    assert w["unknownword"] == (w["server"] + w["mailgun"]) / 2  # unseen: an average word
    assert store.recall("mailgun server").hits[0].memory["id"] == rare["id"]


def test_old_databases_are_reindexed_once(tmp_path):
    conn = connect(tmp_path / "m.db")
    store = Store(conn)
    row, _ = store.remember("会议室怎么预订")
    with store.lock:  # as an old version wrote it: the whole run as one token
        conn.execute("DELETE FROM memory_fts WHERE rowid = ?", (row["id"],))
        conn.execute("INSERT INTO memory_fts (rowid, content, tags, entity) VALUES (?,?,'','')",
                     (row["id"], row["content"]))
        conn.execute("DELETE FROM setting WHERE key = 'index_unspaced'")
    match = "SELECT count(*) FROM memory_fts WHERE memory_fts MATCH '\"预订\"'"
    assert conn.execute(match).fetchone()[0] == 0  # the old index can't find a pair
    assert Store(conn).recall("预订").hits[0].memory["id"] == row["id"]  # reindexed on open
    assert conn.execute(match).fetchone()[0] == 1
