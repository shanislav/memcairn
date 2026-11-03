import json

import anyio
import pytest

from memcairn import format
from memcairn.db import connect
from memcairn.server import build
from memcairn.store import Store


def text_of(result) -> str:
    return "".join(c.text for c in result.content)


@pytest.fixture
def server():
    return build(Store(connect(":memory:")), stdio_client="test")


def call(server, name, **args):
    return text_of(anyio.run(lambda: server.call_tool(name, args)))


def tool_list_tokens(server) -> int:
    tools = anyio.run(server.list_tools)
    return format.tokens(json.dumps([t.model_dump(exclude_none=True, by_alias=True)
                                     for t in tools]))


def test_tool_list_is_cheap(server):
    # Token economy: the whole fixed tool-list cost stays under ~1000 tokens.
    assert tool_list_tokens(server) < 1000, tool_list_tokens(server)


def test_tool_order_is_stable(server):
    names = [t.name for t in anyio.run(server.list_tools)]
    assert names == ["recall", "get", "context", "remember", "update", "forget", "link"]


def test_result_not_duplicated(server):
    # structuredContent would repeat the text and double the token cost
    result = anyio.run(lambda: server.call_tool("context", {}))
    assert result.structured_content is None


def test_roundtrip(server):
    out = call(server, "remember", content="Memcairn uses SQLite FTS5", entity="memcairn")
    assert out.startswith("ok m1")
    hits = call(server, "recall", query="sqlite")
    assert "m1 · fact · memcairn" in hits
    full = call(server, "get", ids=["m1"], history=True)
    assert "by=test" in full and "create" in full
    assert "error" in call(server, "get", ids=["m99"])


def test_compat_tools():
    srv = build(Store(connect(":memory:")), compat=True, stdio_client="t")
    call(srv, "create_entities", entities=[
        {"name": "OBED", "entityType": "project", "observations": ["lunch ordering app"]}])
    call(srv, "create_relations", relations=[{"from": "OBED", "to": "shano", "relationType": "on"}])
    assert "lunch ordering app" in call(srv, "open_nodes", names=["OBED"])
    assert "OBED (project, 1 facts)" in call(srv, "read_graph")
    assert "lunch" in call(srv, "search_nodes", query="lunch")
