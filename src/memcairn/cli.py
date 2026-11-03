"""Command line: serve, tokens, import/export, stats."""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path

import typer

from . import importers
from .db import connect, now
from .server import build, hash_token
from .store import Store

app = typer.Typer(no_args_is_help=True, add_completion=False)
token_app = typer.Typer(no_args_is_help=True, help="Manage client tokens.")
import_app = typer.Typer(no_args_is_help=True, help="Import from other memory formats.")
app.add_typer(token_app, name="token")
app.add_typer(import_app, name="import")

DEFAULT_DB = Path(os.environ.get("MEMCAIRN_DB", Path.home() / ".memcairn" / "memcairn.db"))
DbOpt = typer.Option(DEFAULT_DB, "--db", envvar="MEMCAIRN_DB", help="SQLite database path.")


def open_store(db: Path) -> Store:
    return Store(connect(db))


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
    try:
        _serve(store, transport, host, port, public_url, allowed_host, no_auth, compat, client)
    finally:
        # Fold the WAL back into the main file so the .db alone is a complete copy.
        with store.lock:
            store.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            store.conn.close()


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
    store = open_store(db)
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
    for r in open_store(db).conn.execute("SELECT * FROM client ORDER BY name"):
        state = "revoked" if r["revoked_at"] else "active"
        typer.echo(f"{r['name']:<24} {state:<8} scope={r['default_scope']} "
                   f"last_seen={r['last_seen_at'] or '-'}")


@token_app.command("revoke")
def token_revoke(name: str, db: Path = DbOpt):
    """Revoke a client's token."""
    store = open_store(db)
    with store._tx():
        n = store.conn.execute("UPDATE client SET revoked_at = ? WHERE name = ?",
                               (now(), name)).rowcount
    typer.echo("revoked" if n else "no such client")


@import_app.command("server-memory")
def import_server_memory(path: Path, scope: str = typer.Option("global"), db: Path = DbOpt):
    """Import @modelcontextprotocol/server-memory JSON/JSONL (memory.json)."""
    stats = importers.import_server_memory(open_store(db), path, scope=scope)
    typer.echo(json.dumps(stats))


@app.command()
def export(path: Path, db: Path = DbOpt):
    """Export everything (except tokens) to JSON."""
    path.write_text(json.dumps(importers.export_json(open_store(db)), ensure_ascii=False,
                               indent=1), encoding="utf-8")
    typer.echo(f"written {path}")


@app.command()
def stats(db: Path = DbOpt):
    """Counts of memories, entities, relations."""
    for k, v in open_store(db).stats().items():
        typer.echo(f"{k:<12} {v}")


if __name__ == "__main__":
    app()
