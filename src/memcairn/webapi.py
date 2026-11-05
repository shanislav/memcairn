"""JSON API and the web UI (`/ui`): browse, search and edit memory in a browser.

The page itself is static and holds no data; it logs in with the web UI password, a
one-time login link or a client token (see auth.py). Writes go through the store like any
tool call, so they are versioned and recorded with the client name. Admin actions (add
machine, revoke, permanent delete, password) need a browser session.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import auth, docs
from .store import Conflict, NotFound, Store

UI = Path(__file__).with_name("ui.html")


def _mem(row, *, full: bool = True) -> dict:
    d = dict(row)
    if "tags" in d and isinstance(d["tags"], str):
        d["tags"] = json.loads(d["tags"])
    if not full:
        d["content"] = d["content"][:400]
    return d


def register(mcp, store: Store, authorize, public_url: str | None = None) -> None:
    from starlette.responses import HTMLResponse, JSONResponse

    def err(text: str, code: int = 400):
        return JSONResponse({"error": text}, status_code=code)

    def bearer(request) -> str:
        h = request.headers.get("authorization", "")
        return h[7:] if h.lower().startswith("bearer ") else ""

    def guarded(fn, *, admin: bool = False):
        """Any valid token; with admin=True only a browser session (or no auth at all), so a
        machine's or connector's token can't mint new tokens or cut off others."""
        async def handler(request):
            ok, name = await authorize(request)
            if not ok:
                return err("unauthorized", 401)
            if admin and public_url and not auth.session_info(store, bearer(request)):
                return err("only from the web UI (log in with your password)", 403)
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
        q, limit = p.get("q", "").strip(), max(1, min(int(p.get("limit", 50)), 200))
        kind, entity, status = p.get("type") or None, p.get("entity") or None, p.get("status")
        pinned = p.get("pinned") == "1"
        if q:
            res = store.recall(q, limit=limit, deep=p.get("deep") == "1", type=kind,
                               entity=entity)
            items = [_mem(h.memory, full=False) | {"entity_name": h.entity, "score": h.score,
                                                   "status": h.status} for h in res.hits
                     if h.memory["pinned"] or not pinned]
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
        if pinned:
            where.append("m.pinned = 1")
        order = ("m.updated_at DESC" if p.get("sort") == "changed"
                 else "m.recorded_at DESC, m.id DESC")  # newest noted first
        rows = store.conn.execute(
            f"""SELECT m.*, e.name AS entity_name FROM memory m
                LEFT JOIN entity e ON e.id = m.entity_id WHERE {' AND '.join(where)}
                ORDER BY {order} LIMIT {limit}""", args).fetchall()
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

    async def overview(request, name):
        from .manage import overview as build_overview

        data = build_overview(store)
        data["client"] = name
        return JSONResponse(data)

    async def delete(request, name):
        from .manage import hard_delete

        body = await request.json() if await request.body() else {}
        mid = int(request.path_params["id"])
        if body.get("confirm") != f"m{mid}":
            return err("permanent delete needs confirm: \"m<id>\"")
        return JSONResponse({"deleted": hard_delete(store, mid)})

    async def rename(request, name):
        from .manage import rename_entity

        body = await request.json()
        moved = rename_entity(store, body.get("from", ""), body.get("to", ""), client=name)
        return JSONResponse({"moved": moved})

    async def revoke(request, name):
        body = await request.json() if await request.body() else {}
        target = request.path_params["name"]
        if body.get("confirm") != target:
            return err("revoking needs confirm: \"<client name>\"")
        if not auth.revoke_client(store, target):
            return err(f"no active client {target!r}", 404)
        return JSONResponse({"revoked": target, "self": target == name})

    async def pair_code(request, name):
        """Add machine: a pairing code plus the command that uses it."""
        body = await request.json() if await request.body() else {}
        machine = (body.get("name") or "").strip()
        code = auth.create(store, "pair", machine, by=name)
        url = (public_url or str(request.base_url)).rstrip("/")
        return JSONResponse({"code": code, "name": machine, "url": url,
                             "expires_in": auth.PAIR_TTL,
                             "command": f"memcairn pair {url} {code}"})

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
              ("/api/entity/rename", rename, ["POST"]), ("/api/overview", overview, ["GET"]),
              ("/api/docs", documents, ["GET"]), ("/api/review", review_items, ["GET"])]
    admin_routes = [("/api/memory/{id:int}/delete", delete), ("/api/client/{name}/revoke", revoke),
                    ("/api/pair", pair_code)]
    for path, fn, methods in routes:
        mcp.custom_route(path, methods=methods)(guarded(fn))
    for path, fn in admin_routes:
        mcp.custom_route(path, methods=["POST"])(guarded(fn, admin=True))

    # no token yet: these trade a password or a one-time code for one
    async def body(request) -> dict:
        try:
            data = await request.json()
            return data if isinstance(data, dict) else {}
        except ValueError:
            return {}

    async def body_code(request) -> str:
        return str((await body(request)).get("code", ""))

    throttle = auth.Throttle()

    async def password_ok(password: str) -> tuple[bool, object]:
        """(ok, error response): throttled, and scrypt runs off the event loop."""
        import anyio

        wait = throttle.wait()
        if wait:
            return False, err(f"too many wrong passwords; try again in {wait // 60 + 1} min",
                              429)
        if await anyio.to_thread.run_sync(auth.verify_password, auth.stored_password(store),
                                          password):
            return True, None
        throttle.failed()
        return False, None

    @mcp.custom_route("/api/auth", methods=["GET"])
    async def auth_info(request):
        return JSONResponse({"password": auth.stored_password(store) is not None})

    @mcp.custom_route("/api/login", methods=["POST"])
    async def login(request):
        data = await body(request)
        if "password" in data:
            ok, blocked = await password_ok(str(data["password"]))
            if not ok:
                return blocked or err("wrong password", 401)
            token = auth.new_session(store, "password")
        else:
            token = auth.web_session(store, str(data.get("code", "")))
            if not token:
                return err("unknown, used or expired login link", 401)
        return JSONResponse({"token": token, "client": auth.WEB_CLIENT})

    async def logout(request, name):
        return JSONResponse({"ended": auth.end_session(store, bearer(request))})

    async def password(request, name):
        """Set or change the web UI password. Changing it needs the current one, except
        within 15 minutes of logging in with a login link (the way back in)."""
        import anyio

        info = auth.session_info(store, bearer(request))
        data = await body(request)
        if auth.stored_password(store) and not (info and auth.fresh_from_link(info)):
            ok, blocked = await password_ok(str(data.get("current", "")))
            if not ok:
                return blocked or err("the current password is wrong (forgot it? log in with "
                                      "memcairn login-link, then set a new one)", 401)
        stored = await anyio.to_thread.run_sync(auth.hash_password, str(data.get("new", "")))
        auth.save_password(store, stored)
        return JSONResponse({"password": True})

    mcp.custom_route("/api/logout", methods=["POST"])(guarded(logout))
    mcp.custom_route("/api/password", methods=["POST"])(guarded(password, admin=True))

    @mcp.custom_route("/pair", methods=["POST"])
    async def pair(request):
        got = auth.pair(store, await body_code(request))
        if not got:
            return err("unknown, used or expired pairing code", 401)
        return JSONResponse({"name": got[0], "token": got[1],
                             "url": (public_url or str(request.base_url)).rstrip("/")})
