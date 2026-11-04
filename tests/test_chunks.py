from memcairn import docs, format
from memcairn.chunks import MAX_CHARS, chunk_markdown
from memcairn.db import connect
from memcairn.store import Store

INVENTORY = """---
name: Servers
---
## Home server

Main box, AlmaLinux 9, 192.168.9.1.

## Company servers

### Databases

| IP | Name | Role |
|----|------|------|
""" + "\n".join(f"| 192.168.20.{i} | db{i} | MySQL node {i} |" for i in range(1, 11)) + """

### Notes

Legacy boxes need `-o HostKeyAlgorithms=+ssh-rsa`.
"""


def test_headings_tables_and_anchors():
    cs = chunk_markdown(INVENTORY)
    by = {c.anchor: c for c in cs}
    assert by["home-server"].path == "Home server"
    assert by["notes"].path == "Company servers › Notes"  # H3 under the right H2
    row = by["192-168-20-3"]  # first cell names the row
    assert row.kind == "row" and row.content.startswith("| IP | Name | Role |")
    assert "db3" in row.content and "db4" not in row.content
    assert all(len(c.content) <= MAX_CHARS for c in cs)


def test_long_sections_split_but_code_stays_whole():
    code = "```\n" + "\n".join(f"line {i}" for i in range(400)) + "\n```"
    text = "## Big\n\n" + "\n".join(f"- item {i} " + "x" * 40 for i in range(200)) + "\n\n" + code
    cs = chunk_markdown(text)
    assert len(cs) > 2
    assert sum("```" in c.content for c in cs) == 1  # the fence is in exactly one part


def store_with_inventory():
    store = Store(connect(":memory:"))
    docs.put_doc(store, "servers", INVENTORY, base_version=0)
    return store


def test_recall_returns_the_matching_part_not_the_document():
    store = store_with_inventory()
    res = store.recall("db7 mysql")
    text = format.recall(res, max_tokens=600, limit=5, explain=False, deep=False)
    assert "doc:servers#192-168-20-7" in text and "Name: db7" in text
    assert "db8" not in text  # only that row
    assert format.tokens(text) < 80


def test_parts_are_capped_so_facts_still_show():
    store = store_with_inventory()
    for i in range(3):
        store.remember(f"db{i} mysql fact {i}", entity="DB")
    text = format.recall(store.recall("mysql node"), max_tokens=600, limit=5,
                         explain=False, deep=False)
    assert text.count("doc:servers#") <= 1


def test_get_part_outline_and_reindex_keeps_unchanged_parts():
    from memcairn.server import build
    import anyio

    store = store_with_inventory()
    ids = {r["anchor"]: r["id"] for r in store.conn.execute("SELECT * FROM doc_chunk")}
    srv = build(store, stdio_client="t")

    def get(ref):
        res = anyio.run(lambda: srv.call_tool("get", {"ids": [ref]}))
        return "".join(c.text for c in res.content)

    part = get("doc:servers#192-168-20-7")
    assert "| IP | Name | Role |" in part and "db7" in part and "Home server" not in part
    toc = get("doc:servers?toc")
    assert "Company servers" in toc and "#notes" in toc
    edited = INVENTORY.replace("Main box", "Main server")
    docs.put_doc(store, "servers", edited, base_version=1)
    after = {r["anchor"]: r["id"] for r in store.conn.execute("SELECT * FROM doc_chunk")}
    assert after == ids  # same anchors, same rows: nothing renumbered
    assert "Main server" in get("doc:servers#home-server")
