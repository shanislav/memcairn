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

from .store import Store
from .vectors import from_env

app = typer.Typer(no_args_is_help=True, add_completion=False)
token_app = typer.Typer(no_args_is_help=True, help="Manage client tokens.")
import_app = typer.Typer(no_args_is_help=True, help="Import from other memory formats.")
app.add_typer(token_app, name="token")
app.add_typer(import_app, name="import")
doc_app = typer.Typer(help="Shared documents.", no_args_is_help=True)
app.add_typer(doc_app, name="doc")

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
    oauth: bool = typer.Option(False, envvar="MEMCAIRN_OAUTH",
                               help="Also accept OAuth (claude.ai connectors); needs --public-url."),
):
    """Run the MCP server."""
    store = open_store(db)
    from . import docs

    docs.index_all(store)  # documents written before the parts index existed, or older
    if store.embedder is not None:
        threading.Thread(target=_backfill_forever, args=(store,), name="backfill",
                         daemon=True).start()
    hours = float(os.environ.get("MEMCAIRN_REVIEW_HOURS", "24"))
    if hours > 0:
        threading.Thread(target=_review_forever, args=(store, hours), name="review",
                         daemon=True).start()
    try:
        _serve(store, transport, host, port, public_url, allowed_host, no_auth, compat, client,
               oauth)
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


def _review_forever(store: Store, hours: float) -> None:
    """Once in a while: when memories pile up long or status-like, leave a cleanup todo."""
    from .review import suggest_cleanup

    time.sleep(300)  # not during start-up
    while True:
        try:
            n, saving = suggest_cleanup(store, minimum=10, origin="Daily memory check",
                                        client="review")
            if n:
                logging.getLogger("memcairn").info("review: %d to clean (~%d tokens)", n, saving)
        except Exception:
            logging.getLogger("memcairn").exception("review failed")
        time.sleep(hours * 3600)


def _serve(store, transport, host, port, public_url, allowed_host, no_auth, compat, client,
           oauth=False):
    if transport == "stdio":
        from .server import build

        build(store, compat=compat, stdio_client=client).run("stdio")
        return
    import anyio
    from mcp.server.transport_security import TransportSecuritySettings

    auth_url = None if no_auth else (public_url or f"http://{host}:{port}")
    if oauth and not public_url:
        raise typer.BadParameter("--oauth needs --public-url (the address claude.ai reaches)")
    from .server import build

    server = build(store, compat=compat, auth_url=auth_url, oauth=oauth)
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
              db: Path = DbOpt,
              url: str = typer.Option(None, envvar="MEMCAIRN_PUBLIC_URL",
                                      help="Public server URL, for the connect instructions."),
              quiet: bool = typer.Option(False, "--quiet", help="Print only the token.")):
    """Create a client and print its token (shown once) with ready connect instructions."""
    from .server import hash_token

    store = open_store(db, vectors=False)
    token = "mcn_" + secrets.token_urlsafe(32)
    with store._tx():
        store.scope_id(scope)
        store.conn.execute(
            """INSERT INTO client (name, token_hash, default_scope, created_at) VALUES (?,?,?,?)
               ON CONFLICT(name) DO UPDATE SET token_hash = excluded.token_hash,
               default_scope = excluded.default_scope, revoked_at = NULL""",
            (name, hash_token(token), scope, now()))
    if quiet:
        typer.echo(token)
        return
    typer.echo(connect_text(name, token, url))


def connect_text(name: str, token: str, url: str | None) -> str:
    """What to send to the person setting up `name`: token plus copy-paste steps."""
    url = (url or "https://<your-server>").rstrip("/").removesuffix("/mcp")
    return f"""Token for {name} (shown only now): {token}

Send this to whoever sets up {name}:
--------------------------------------------------------------------------
Connect Claude Code to Memcairn memory

In Claude Code type:
  /plugin marketplace add shanislav/memcairn
  /plugin install memcairn@memcairn
When asked, enter
  Server URL: {url}
  Token:      {token}
Restart Claude Code. That's all.

No /plugin command (e.g. the desktop app)? In a terminal with the claude CLI:
  claude plugin marketplace add shanislav/memcairn
  claude plugin install memcairn@memcairn --config url={url} --config token={token}
--------------------------------------------------------------------------"""


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
    from .pairing import revoke_client

    ok = revoke_client(open_store(db, vectors=False), name)
    typer.echo("revoked, with its OAuth and browser sessions" if ok
               else "no such active client")


@doc_app.command("list")
def doc_list(db: Path = DbOpt):
    """Shared documents: name, version, size, last writer."""
    from . import docs

    for d in docs.list_docs(open_store(db, vectors=False)):
        typer.echo(f"{d['name']:<28} v{d['version']:<4} {d['size']:>7} chars  "
                   f"{d['updated_at'][:10]}  by={d['client'] or '-'}")


@doc_app.command("delete")
def doc_delete(name: str, db: Path = DbOpt,
               yes: bool = typer.Option(False, "--yes", help="Don't ask.")):
    """Delete a document with all versions (remove it from every sync.toml first)."""
    from . import docs

    store = open_store(db, vectors=False)
    if not yes and not typer.confirm(f"delete document {name} with all its versions?"):
        raise typer.Exit(1)
    typer.echo(f"deleted {name} ({docs.delete_doc(store, name)} versions)")


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
                   "session with the 'cleanup' prompt (/mcp__plugin_memcairn_memory__cleanup with the plugin).")


@import_app.command("claude-memory")
def import_claude_memory(
    folder: Path,
    scope: str = typer.Option("global"),
    skip: list[str] = typer.Option(None, help="File name patterns to leave out."),
    db: Path = DbOpt,
):
    """Import a Claude Code memory folder (~/.claude/projects/<project>/memory)."""
    store = open_store(db)
    typer.echo(json.dumps(importers.import_claude_memory(store, folder, scope=scope,
                                                         skip=skip or [])))


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
def sync(
    config: Path = typer.Option(None, help="Config file (default ~/.memcairn/sync.toml)."),
    quiet: bool = typer.Option(False, help="Print only conflicts and errors (for hooks)."),
):
    """Sync chosen local files with shared documents on the server (only what changed)."""
    from . import sync as s

    try:
        lines = s.run(config or s.DEFAULT_CONFIG)
    except Exception as e:  # a hook must never break session start
        lines = [f"error: memcairn sync failed: {e}"]
    for line in lines:
        if not quiet or line.startswith(("CONFLICT", "error")):
            typer.echo(f"memcairn sync: {line}")


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
def purge(
    values_file: Path = typer.Option(..., "--values", help="File with one string to remove "
                                     "per line ('-' = stdin). Never pass secrets as arguments."),
    db: Path = DbOpt,
    dry_run: bool = typer.Option(False, help="Only count where the strings occur."),
    replacement: str = typer.Option("[removed]", help="Text put in their place."),
):
    """Remove strings for real (a leaked secret): facts, history, documents, indexes, logs."""
    import sys

    from . import purge as pg

    text = sys.stdin.read() if str(values_file) == "-" else values_file.read_text("utf-8")
    values = [v.strip() for v in text.splitlines() if v.strip()]
    store = open_store(db, vectors=not dry_run)
    counts = pg.purge_text(store, values, replacement=replacement, dry_run=dry_run)
    typer.echo(", ".join(f"{t}: {n}" for t, n in counts.items()))
    if not dry_run:
        left = pg.leftovers(store, values)
        typer.echo(f"left anywhere: {left}. Backups made earlier still contain the text.")


@app.command()
def setup(
    yes: bool = typer.Option(False, "--yes", help="Apply every offered change without asking."),
    dry_run: bool = typer.Option(False, help="Only show what would change."),
    disable_auto_memory: bool = typer.Option(
        False, help="Also turn off Claude Code auto memory (Memcairn as the only memory)."),
):
    """Optional client changes the plugin doesn't make: each shown as a diff, backed up, y/n.

    Run it in the project whose auto memory you want to share (your main workspace).
    """
    from . import clientsetup as cs

    def confirm(change, diff_text):
        typer.echo(f"\n* {change.title}\n{diff_text}")
        if dry_run:
            return False
        return yes or typer.confirm("apply?", default=True)

    cs.run_setup(Path.home(), confirm, typer.echo, cwd=Path.cwd(),
                 disable_auto_memory=disable_auto_memory)


@app.command()
def pair(url: str = typer.Argument(..., help="Server address, e.g. https://memory.example.com"),
         code: str = typer.Argument(..., help="Code from Add machine in the web UI."),
         no_plugin: bool = typer.Option(False, help="Write a manual MCP entry even when the "
                                        "claude CLI could install the plugin.")):
    """Connect this machine to a Memcairn server with a pairing code (no token to copy)."""
    from .connect import pair as do_pair

    try:
        do_pair(Path.home(), url, code, typer.echo, use_plugin=not no_plugin)
    except RuntimeError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(1) from e


@app.command("connect")
def connect_cmd(local: bool = typer.Option(..., "--local", help="Memory on this computer only.")):
    """Connect Claude Code to a memory on this computer: no server, no token."""
    from .connect import connect_local

    connect_local(Path.home(), typer.echo)


@app.command("login-link")
def login_link(db: Path = DbOpt,
               url: str = typer.Option(None, envvar="MEMCAIRN_PUBLIC_URL",
                                       help="Public server URL.")):
    """Print a web UI link that logs in once (valid 15 minutes). Run it on the server."""
    from . import pairing

    code = pairing.create(open_store(db, vectors=False), "login", by="cli")
    base = (url or "http://127.0.0.1:8765").rstrip("/").removesuffix("/mcp")
    typer.echo(f"{base}/ui#login={code}\n(single use, valid {pairing.LOGIN_TTL // 60} "
               "minutes; the browser then stays logged in for 7 days)")


@app.command()
def ui(db: Path = DbOpt, port: int = typer.Option(0, help="Port; 0 picks a free one."),
       no_browser: bool = typer.Option(False, help="Only print the link.")):
    """Open the web UI for the memory on this computer (logs in by itself)."""
    import socket
    import webbrowser

    from . import pairing

    if not port:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
    store = open_store(db)
    link = f"http://127.0.0.1:{port}/ui#login={pairing.create(store, 'login', by='ui')}"
    typer.echo(f"Memcairn web UI: {link}\n(Ctrl+C to stop)")
    if not no_browser:
        threading.Timer(1.0, webbrowser.open, args=(link,)).start()
    import contextlib

    with contextlib.suppress(KeyboardInterrupt):  # token auth stays on: web pages can reach localhost
        _serve(store, "http", "127.0.0.1", port, f"http://127.0.0.1:{port}", None, False,
               False, None)


@app.command()
def context(db: Path = DbOpt):
    """Print the memory overview (for a session-start hook); silent when unreachable."""
    from .client import auth

    url, token = auth()
    try:
        if url:
            import urllib.request

            req = urllib.request.Request(f"{url}/context",
                                         headers={"Authorization": f"Bearer {token}"})
            with urllib.request.urlopen(req, timeout=3) as r:
                typer.echo(r.read().decode())
        elif db.is_file():
            from . import format

            store = open_store(db, vectors=False)
            hits = store.context("global", max_todos=5)
            typer.echo(format.SESSION_INTRO + "\n" + format.context(
                hits, max_tokens=900, todos_left=store.todos_left))
    except Exception:  # never block a session start
        pass


@app.command()
def unsetup(yes: bool = typer.Option(False, "--yes", help="Revert everything without asking."),
            only: str = typer.Option(None, help="Revert just this change id, e.g. automemory.")):
    """Revert what `memcairn setup` changed (it keeps a record in ~/.memcairn)."""
    from . import clientsetup as cs

    cs.run_unsetup(Path.home(), lambda q: yes or typer.confirm(q, default=True), typer.echo,
                   only=only)


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
    from .client import auth

    url, token = auth(url, token, name=name)
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
