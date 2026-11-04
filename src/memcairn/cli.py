"""Command line: serve, tokens, import/export, stats."""

from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
from pathlib import Path

import typer

from . import importers
from .db import connect, now
from .server import build, hash_token
from .store import Store
from .vectors import from_env

app = typer.Typer(no_args_is_help=True, add_completion=False)
token_app = typer.Typer(no_args_is_help=True, help="Manage client tokens.")
import_app = typer.Typer(no_args_is_help=True, help="Import from other memory formats.")
app.add_typer(token_app, name="token")
app.add_typer(import_app, name="import")

DEFAULT_DB = Path(os.environ.get("MEMCAIRN_DB", Path.home() / ".memcairn" / "memcairn.db"))
DbOpt = typer.Option(DEFAULT_DB, "--db", envvar="MEMCAIRN_DB", help="SQLite database path.")


def open_store(db: Path, *, vectors: bool = True) -> Store:
    return Store(connect(db), embedder=from_env() if vectors else None)


@app.command()
def serve(
    db: Path = DbOpt,
    transport: str = typer.Option("stdio", help="stdio or http"),
    host: str = typer.Option("127.0.0.1"),
    port: int = typer.Option(8765),
    public_url: str = typer.Option(None, envvar="MEMCAIRN_PUBLIC_URL",
                                   help="Base URL clients use (http mode); enables token auth."),
    allowed_host: list[str] = typer.Option(None, help="Extra allowed Host headers (http mode)."),
    no_auth: bool = typer.Option(False, help="Disable token auth in http mode (trusted LAN only)."),
    compat: bool = typer.Option(False, envvar="MEMCAIRN_COMPAT",
                                help="Also expose server-memory compatible tools."),
    client: str = typer.Option(None, envvar="MEMCAIRN_CLIENT", help="Client name (stdio)."),
):
    """Run the MCP server."""
    store = open_store(db)
    if store.embedder is not None:
        threading.Thread(target=_backfill_forever, args=(store,), name="backfill",
                         daemon=True).start()
    try:
        _serve(store, transport, host, port, public_url, allowed_host, no_auth, compat, client)
    finally:
        # Fold the WAL back into the main file so the .db alone is a complete copy.
        with store.lock:
            store.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            store.conn.close()


def _backfill_forever(store: Store, retry_seconds: int = 600) -> None:
    """Embed missing vectors; if the model can't be loaded (offline, disk), retry later."""
    while True:
        try:
            n = store.backfill_vectors()
            if n:
                logging.getLogger("memcairn").info("embedded %d memories", n)
            return
        except Exception:
            logging.getLogger("memcairn").exception(
                "backfill failed, retrying in %ds; recall uses full text meanwhile",
                retry_seconds)
            time.sleep(retry_seconds)


def _serve(store, transport, host, port, public_url, allowed_host, no_auth, compat, client):
    if transport == "stdio":
        build(store, compat=compat, stdio_client=client).run("stdio")
        return
    import anyio
    from mcp.server.transport_security import TransportSecuritySettings

    auth_url = None if no_auth else (public_url or f"http://{host}:{port}")
    server = build(store, compat=compat, auth_url=auth_url)
    hosts = [f"{host}:*", "127.0.0.1:*", "localhost:*", *(allowed_host or [])]
    if public_url:
        hosts.append(public_url.split("://", 1)[-1].split("/", 1)[0])
    security = TransportSecuritySettings(enable_dns_rebinding_protection=True,
                                         allowed_hosts=hosts, allowed_origins=[])
    anyio.run(lambda: server.run_streamable_http_async(
        host=host, port=port, stateless_http=True, json_response=True,
        transport_security=security))


@token_app.command("add")
def token_add(name: str, scope: str = typer.Option("global", help="Default scope."),
              db: Path = DbOpt):
    """Create a client and print its token (shown once)."""
    store = open_store(db, vectors=False)
    token = "mcn_" + secrets.token_urlsafe(32)
    with store._tx():
        store.scope_id(scope)
        store.conn.execute(
            """INSERT INTO client (name, token_hash, default_scope, created_at) VALUES (?,?,?,?)
               ON CONFLICT(name) DO UPDATE SET token_hash = excluded.token_hash,
               default_scope = excluded.default_scope, revoked_at = NULL""",
            (name, hash_token(token), scope, now()))
    typer.echo(token)


@token_app.command("list")
def token_list(db: Path = DbOpt):
    """List clients."""
    for r in open_store(db, vectors=False).conn.execute("SELECT * FROM client ORDER BY name"):
        state = "revoked" if r["revoked_at"] else "active"
        typer.echo(f"{r['name']:<24} {state:<8} scope={r['default_scope']} "
                   f"last_seen={r['last_seen_at'] or '-'}")


@token_app.command("revoke")
def token_revoke(name: str, db: Path = DbOpt):
    """Revoke a client's token."""
    store = open_store(db, vectors=False)
    with store._tx():
        n = store.conn.execute("UPDATE client SET revoked_at = ? WHERE name = ?",
                               (now(), name)).rowcount
    typer.echo("revoked" if n else "no such client")


@import_app.command("server-memory")
def import_server_memory(path: Path, scope: str = typer.Option("global"), db: Path = DbOpt):
    """Import @modelcontextprotocol/server-memory JSON/JSONL (memory.json)."""
    from . import review as rv

    store = open_store(db)
    stats = importers.import_server_memory(store, path, scope=scope)
    typer.echo(json.dumps(stats))
    # Imported memories are usually long logs; offer the one-time cleanup right away.
    n, saving = rv.suggest_cleanup(store, scope=scope)
    if n:
        typer.echo(f"\n{n} imported memories are long or read like status logs (~{saving} tokens "
                   "to save). An open todo now asks agents to offer a cleanup; run it in a "
                   "session with the 'cleanup' prompt (/mcp__memory__cleanup in Claude Code).")


@app.command()
def export(path: Path, db: Path = DbOpt):
    """Export everything (except tokens) to JSON."""
    path.write_text(json.dumps(importers.export_json(open_store(db)), ensure_ascii=False,
                               indent=1), encoding="utf-8")
    typer.echo(f"written {path}")


@app.command()
def backup(path: Path, db: Path = DbOpt):
    """Consistent copy of the database, safe while the server is running."""
    import sqlite3

    src = connect(db)
    dst = sqlite3.connect(path)
    with dst:
        src.backup(dst)
    n = dst.execute("SELECT count(*) FROM memory").fetchone()[0]
    dst.close()
    src.close()
    typer.echo(f"written {path} ({n} memories)")


@app.command()
def stats(db: Path = DbOpt, days: int = typer.Option(7, help="Usage window in days."),
          queries: bool = typer.Option(False, help="Also list recent recall queries.")):
    """Counts, and what agents used: calls, tokens per call, empty results."""
    store = open_store(db, vectors=False)
    for k, v in store.stats().items():
        typer.echo(f"{k:<12} {v}")
    rows = store.usage(days)
    if not rows:
        typer.echo(f"\nno tool calls in the last {days} days")
        return
    typer.echo(f"\nlast {days} days   calls  avg tok  max tok  avg hits  empty  avg ms  errors")
    for r in rows:
        typer.echo(f"{r['tool']:<16} {r['calls']:>6} {r['avg_tokens']:>8} {r['max_tokens']:>8} "
                   f"{r['avg_hits'] if r['avg_hits'] is not None else '-':>9} {r['empty']:>6} "
                   f"{r['avg_ms']:>7} {r['errors']:>7}")
    if queries:
        typer.echo("\nrecent recall queries (hits, tokens):")
        for r in store.conn.execute(
                """SELECT ts, client, query, hits, tokens FROM call_log WHERE tool = 'recall'
                   ORDER BY id DESC LIMIT 30"""):
            typer.echo(f"{r['ts'][:16]} {r['client'] or '-':<14} {r['hits']:>2} "
                       f"{r['tokens']:>4}  {r['query']}")


@app.command()
def chronicle(
    name: str = typer.Argument(..., help="Entity name (exact, or a part of it)."),
    db: Path = DbOpt,
    out: Path = typer.Option(None, help="Write markdown here instead of printing."),
    current_only: bool = typer.Option(False, help="Leave out replaced earlier versions."),
):
    """Everything memory knows about a topic, in date order, as markdown."""
    from .chronicle import chronicle as build_chronicle

    text = build_chronicle(open_store(db, vectors=False), name, current_only=current_only)
    if out:
        out.write_text(text, encoding="utf-8")
        typer.echo(f"written {out}")
    else:
        typer.echo(text)


@app.command()
def review(
    db: Path = DbOpt,
    kind: list[str] = typer.Option(None, help="dup, similar, long, status, todo (default all)"),
    limit: int = typer.Option(30, help="Show at most this many items."),
    fix_duplicates: bool = typer.Option(False, help="Soft-delete exact duplicate copies."),
):
    """What to clean up, biggest token savings first. Only --fix-duplicates changes data."""
    from . import review as rv

    store = open_store(db, vectors=False)
    if fix_duplicates:
        typer.echo(f"forgot {rv.fix_exact_duplicates(store)} duplicate copies "
                   "(restore with forget undo=true)")
    items = rv.review(store, kind or None)
    counts: dict[str, int] = {}
    for i in items:
        counts[i.kind] = counts.get(i.kind, 0) + 1
    for i in items[:limit]:
        typer.echo(i.line())
    total = sum(i.saving for i in items)
    typer.echo(f"\n{len(items)} items ({', '.join(f'{k} {v}' for k, v in counts.items())}); "
               f"~{total} tokens saved per full read if all were fixed")


@app.command()
def doctor(
    db: Path = DbOpt,
    url: str = typer.Option(None, help="Server URL; default: the 'memory' entry in ~/.claude.json."),
    token: str = typer.Option(None, envvar="MEMCAIRN_TOKEN", help="Token; default: from config."),
    name: str = typer.Option("memory", help="MCP server name Memcairn is registered under."),
):
    """Check this machine's setup (configs, hosts, token, hook) and the database, if present."""
    from . import doctor as d

    home, cwd = Path.home(), Path.cwd()
    is_client = url or any(p.is_file() for _, p in d.config_files(home, cwd))
    # e.g. inside the server container: no Claude here, so only the database is checked
    findings = d.check_configs(home, cwd, name) + d.check_hook(home) if is_client else []
    if not url:
        try:
            spec = json.loads((home / ".claude.json").read_text(encoding="utf-8"))
            spec = spec.get("mcpServers", {}).get(name, {})
            url = spec.get("url")
            auth = spec.get("headers", {}).get("Authorization", "")
            token = token or (auth[7:] if auth.lower().startswith("bearer ") else None)
        except (OSError, json.JSONDecodeError):
            pass
    if url:
        from urllib.parse import urlparse

        findings += d.check_hosts(urlparse(url).hostname or "")
        findings += d.check_server(url, token)
    if db.is_file():
        findings += d.check_db(db)
    text, code = d.report(findings)
    typer.echo(text)
    raise typer.Exit(code)


if __name__ == "__main__":
    app()
