"""JSON API and the web UI (`/ui`): browse, search and edit memory in a browser.

Same bearer tokens (or OAuth tokens) as MCP; the page itself is static and holds no
data until a token is entered. Writes go through the store like any tool call, so they
are versioned and recorded with the client name (`by=<token's client>`).
"""

from __future__ import annotations

import json
from pathlib import Path

from . import docs
from .store import Conflict, NotFound, Store

UI = Path(__file__).with_name("ui.html")


def _mem(row, *, full: bool = True) -> dict:
    d = dict(row)
    if "tags" in d and isinstance(d["tags"], str):
        d["tags"] = json.loads(d["tags"])
    if not full:
        d["content"] = d["content"][:400]
    return d


def register(mcp, store: Store, authorize) -> None:
    from starlette.responses import HTMLResponse, JSONResponse

    def err(text: str, code: int = 400):
        return JSONResponse({"error": text}, status_code=code)

    def guarded(fn):
        async def handler(request):
            ok, name = await authorize(request)
            if not ok:
                return err("unauthorized", 401)
            try:
                return await fn(request, name)
            except NotFound as e:
                return err(str(e), 404)
            except (Conflict, ValueError) as e:
                return err(str(e), 409 if isinstance(e, Conflict) else 400)
        return handler

    @mcp.custom_route("/ui", methods=["GET"])
    async def ui(request):
        return HTMLResponse(UI.read_text(encoding="utf-8"),
                            headers={"Cache-Control": "no-store",
                                     "Content-Security-Policy":
                                     "default-src 'self'; style-src 'unsafe-inline'; "
                                     "script-src 'unsafe-inline'; frame-ancestors 'none'"})

    async def stats(request, name):
        return JSONResponse({"stats": store.stats(), "client": name,
                             "usage": [dict(r) for r in store.usage(7)]})

    async def search(request, name):
        p = request.query_params
        q, limit = p.get("q", "").strip(), min(int(p.get("limit", 50)), 200)
        kind, entity, status = p.get("type") or None, p.get("entity") or None, p.get("status")
        if q:
            res = store.recall(q, limit=limit, deep=p.get("deep") == "1", type=kind,
                               entity=entity)
            items = [_mem(h.memory, full=False) | {"entity_name": h.entity, "score": h.score,
                                                   "status": h.status} for h in res.hits]
            parts = [{"doc": c.doc, "anchor": c.anchor, "path": c.path, "content": c.content}
                     for c in res.chunks[:5]]
            return JSONResponse({"items": items, "parts": parts, "coverage": res.coverage})
        where, args = ["m.deleted_at IS NULL", "m.superseded_by IS NULL"], []
        if kind:
            where.append("m.type = ?")
            args.append(kind)
        if entity:
            where.append("e.name = ? COLLATE NOCASE")
            args.append(entity)
        if status:
            where.append("m.status = ?")
            args.append(status)
        rows = store.conn.execute(
            f"""SELECT m.*, e.name AS entity_name FROM memory m
                LEFT JOIN entity e ON e.id = m.entity_id WHERE {' AND '.join(where)}
                ORDER BY m.updated_at DESC LIMIT {limit}""", args).fetchall()
        return JSONResponse({"items": [_mem(r, full=False) for r in rows], "parts": []})

    async def entities(request, name):
        return JSONResponse([{"name": r["name"], "facts": r["facts"]}
                             for r in store.entities() if r["facts"]])

    async def memory(request, name):
        mid = int(request.path_params["id"])
        if request.method == "GET":
            row = store.get([mid], touch=False)[0]
            return JSONResponse({
                "memory": _mem(row),
                "history": [{"version": h["version"], "op": h["op"], "at": h["changed_at"],
                             "by": h["client"], "content": json.loads(h["snapshot"])["content"]}
                            for h in store.history(mid)],
                "replaced": [_mem(r) for r in store.predecessors(mid)]})
        body = await request.json()
        allowed = {k: body[k] for k in ("content", "type", "entity", "status", "pinned",
                                        "importance", "supersede", "expected_version")
                   if k in body}
        row = store.update(mid, client=name, **allowed)
        return JSONResponse({"memory": _mem(row)})

    async def forget(request, name):
        mid = int(request.path_params["id"])
        body = await request.json() if await request.body() else {}
        row = store.forget(mid, undo=bool(body.get("undo")), reason=body.get("reason"),
                           client=name)
        return JSONResponse({"memory": _mem(row)})

    async def create(request, name):
        body = await request.json()
        row, dups = store.remember(body.get("content", ""), type=body.get("type") or "fact",
                                   entity=body.get("entity") or None, client=name)
        return JSONResponse({"memory": _mem(row), "duplicates": [d["id"] for d in dups]})

    async def documents(request, name):
        return JSONResponse([dict(r) for r in docs.list_docs(store)])

    async def review_items(request, name):
        from .review import review

        return JSONResponse([{"kind": i.kind, "ids": i.ids, "entity": i.entity,
                              "saving": i.saving, "action": i.action, "text": i.text}
                             for i in review(store)[:100]])

    routes = [("/api/stats", stats, ["GET"]), ("/api/search", search, ["GET"]),
              ("/api/entities", entities, ["GET"]), ("/api/memory", create, ["POST"]),
              ("/api/memory/{id:int}", memory, ["GET", "POST"]),
              ("/api/memory/{id:int}/forget", forget, ["POST"]),
              ("/api/docs", documents, ["GET"]), ("/api/review", review_items, ["GET"])]
    for path, fn, methods in routes:
        mcp.custom_route(path, methods=methods)(guarded(fn))
