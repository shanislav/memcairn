"""`memcairn doctor`: find the setup problems we actually ran into.

Client side: which MCP configs define a memory server (and which of them wins), a hosts
entry glued onto the previous line, DNS, the token, the SessionStart hook.
Server side: database integrity, vectors, and store hygiene (overgrown facts, stale todos,
stored text that reads like instructions).

Reports only; never changes anything. Runs outside MCP, so it costs agents no tokens.
"""

from __future__ import annotations

import json
import os
import re
import socket
import sqlite3
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

OK, WARN, FAIL = "ok", "warn", "fail"
MARK = {OK: "✔", WARN: "!", FAIL: "✘"}


@dataclass
class Finding:
    level: str
    text: str
    hint: str = ""

    def line(self) -> str:
        out = f"{MARK[self.level]} {self.text}"
        return out + (f"\n    → {self.hint}" if self.hint else "")


# ---------- client: MCP configs ----------

def config_files(home: Path, cwd: Path) -> list[tuple[str, Path]]:
    """(label, path) of every file that can define MCP servers for Claude Code / Desktop."""
    files = [("user (~/.claude.json)", home / ".claude.json"),
             ("ignored (~/.claude/settings.json)", home / ".claude" / "settings.json"),
             ("project (.mcp.json)", cwd / ".mcp.json")]
    desktop_dirs = [home / "Library" / "Application Support" / "Claude",
                    home / ".config" / "Claude"]
    for env in ("APPDATA", "LOCALAPPDATA"):
        if os.environ.get(env):
            desktop_dirs.append(Path(os.environ[env]) / "Claude")
    if os.environ.get("LOCALAPPDATA"):
        data = Path(os.environ["LOCALAPPDATA"]) / "Claude-Data"
        if data.is_dir():
            desktop_dirs += [p for p in data.iterdir() if p.is_dir()]
    for d in desktop_dirs:
        files.append((f"desktop app ({d})", d / "claude_desktop_config.json"))
    return files


def _servers(doc: dict, label: str, cwd: Path) -> list[tuple[str, str, dict]]:
    out = [(label, name, spec) for name, spec in (doc.get("mcpServers") or {}).items()]
    if label.startswith("user"):
        for proj, cfg in (doc.get("projects") or {}).items():
            same = Path(proj).resolve() == cwd.resolve() if proj else False
            for name, spec in (cfg.get("mcpServers") or {}).items():
                out.append((f"project entry in ~/.claude.json ({proj})"
                            + (" [this folder]" if same else ""), name, spec))
    return out


def _describe(spec: dict) -> str:
    if spec.get("url"):
        return spec["url"]
    return " ".join([spec.get("command", "")] + [str(a) for a in spec.get("args", [])]).strip()


def check_configs(home: Path, cwd: Path, name: str = "memory") -> list[Finding]:
    found, out = [], []
    for label, path in config_files(home, cwd):
        if not path.is_file():
            continue
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            out.append(Finding(WARN, f"can't read {path}: {e}"))
            continue
        found += _servers(doc, label, cwd)
    mine = [f for f in found if f[1] == name]
    effective = [f for f in mine if not f[0].startswith("ignored")]
    legacy = [f for f in found if "server-memory" in _describe(f[2])]
    from .clientsetup import plugin_enabled

    plugin = plugin_enabled(home)
    if plugin:
        out.append(Finding(OK, "Claude Code plugin memcairn is enabled"))
        if effective:
            out.append(Finding(WARN, f'a manual "{name}" MCP server is configured as well, so '
                               "the tools are there twice", "memcairn setup removes it"))
    elif not effective:
        out.append(Finding(FAIL, f'no MCP server named "{name}" configured',
                           "install the plugin (/plugin marketplace add shanislav/memcairn, "
                           "/plugin install memcairn@memcairn) or: claude mcp add --scope user "
                           '--transport http memory <url> --header "Authorization: Bearer <token>"'))
    for label, srv, spec in mine:
        kind = "memcairn" if spec.get("url") or "memcairn" in _describe(spec) else "OTHER"
        level = OK if kind == "memcairn" and label.startswith("user") else WARN
        if label.startswith("ignored"):
            level = WARN
        out.append(Finding(level, f'"{srv}" in {label}: {_describe(spec)}'))
    if len(effective) > 1:
        out.append(Finding(FAIL, f'{len(effective)} servers are named "{name}"; the most specific '
                           "scope (project, then desktop app) wins, so Memcairn may be shadowed",
                           "remove the others or register Memcairn under its own name"))
    for label, srv, _spec in legacy:
        if srv != name:
            out.append(Finding(WARN, f'old server-memory still configured as "{srv}" in {label}'))
    return out


def check_hook(home: Path) -> list[Finding]:
    from .clientsetup import plugin_enabled

    path = home / ".claude" / "settings.json"
    try:
        settings = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        settings = {}
    out = []
    sync_toml = home / ".memcairn" / "sync.toml"
    shared = sync_toml.is_file() and "[automemory]" in sync_toml.read_text(encoding="utf-8")
    if settings.get("autoMemoryEnabled") is False:
        out.append(Finding(OK, "auto memory is off: Memcairn is the only memory (preferences "
                           "go there as feedback)"))
    elif shared:
        out.append(Finding(OK, "auto memory is on and shared with your other machines"))
    else:
        out.append(Finding(OK, "auto memory is on, on this machine only",
                           "memcairn setup can share it with your other machines"))
    hooks = settings.get("hooks") or {}
    cmds = [h.get("command", "") for g in hooks.get("SessionStart", []) for h in g.get("hooks", [])]
    if plugin_enabled(home):
        return out + [Finding(OK, "session-start overview comes from the plugin")]
    if any("/context" in c for c in cmds):
        return out + [Finding(OK, "SessionStart hook injects /context")]
    return out + [Finding(WARN, "no SessionStart hook: memory reaches a session only if the model "
                    "calls recall", "see docs/setup.md, 'Inject memory at session start'")]


# ---------- client: network ----------

GLUED = re.compile(r"^\s*\S+\s+\S*?\d{1,3}(\.\d{1,3}){3}\s+\S")


def hosts_path() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32/drivers/etc/hosts"
    return Path("/etc/hosts")


def check_hosts(host: str, path: Path | None = None) -> list[Finding]:
    path = path or hosts_path()
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out = []
    for n, line in enumerate(lines, 1):
        body = line.split("#", 1)[0]
        if GLUED.match(body):
            out.append(Finding(FAIL, f"{path}:{n} looks like two entries glued together: "
                               f"{body.strip()[:80]}", "split it into two lines"))
        elif host in body.split()[1:]:
            out.append(Finding(OK, f"hosts maps {host} → {body.split()[0]}"))
    return out


def check_server(url: str, token: str | None) -> list[Finding]:
    u = urlparse(url)
    host, out = u.hostname or "", []
    try:
        addrs = sorted({a[4][0] for a in socket.getaddrinfo(host, u.port or 443)})
        bad = [a for a in addrs if a in ("0.0.0.0", "::")]
        out.append(Finding(FAIL if bad else OK, f"{host} resolves to {', '.join(addrs)}",
                           "a hosts entry points it nowhere" if bad else ""))
        if bad:
            return out
    except socket.gaierror as e:
        return [Finding(FAIL, f"{host} does not resolve: {e}", "check DNS / hosts")]
    base = f"{u.scheme}://{u.netloc}"
    req = urllib.request.Request(base + "/context?max_tokens=50")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            out.append(Finding(OK, f"server reachable, token accepted ({r.status})"))
    except urllib.error.HTTPError as e:
        hint = {401: "token missing, wrong or revoked", 403: "your IP is not allowed by the "
                "proxy (split-tunnel VPN? see docs/setup.md section 6)",
                404: "server too old for /context, or wrong URL"}.get(e.code, "")
        out.append(Finding(FAIL, f"server answered {e.code}", hint))
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        out.append(Finding(FAIL, f"can't reach {base}: {e}", "network, VPN or TLS problem"))
    return out


# ---------- server: database ----------

INSTRUCTION_LIKE = re.compile(
    r"(?i)\b(ignore (all |any )?(previous|prior|above) (instructions|messages)|"
    r"disregard (the )?(previous|above)|you are now|system prompt|"
    r"do not tell the user|new instructions:)")


def check_db(db: Path, *, long_chars: int = 500, big_entity: int = 30,
             stale_todo_days: int = 30) -> list[Finding]:
    if not db.is_file():
        return [Finding(FAIL, f"database {db} not found")]
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    q = lambda sql, *a: c.execute(sql, a).fetchall()  # noqa: E731
    out = []
    integrity = q("PRAGMA quick_check")[0][0]
    out.append(Finding(OK if integrity == "ok" else FAIL, f"integrity: {integrity}"))
    live = "deleted_at IS NULL AND superseded_by IS NULL"
    n = q(f"SELECT count(*) FROM memory WHERE {live}")[0][0]
    out.append(Finding(OK, f"{n} live memories, schema v{q('PRAGMA user_version')[0][0]}"))
    try:
        vec = dict(q("SELECT model, count(*) FROM memory_vec GROUP BY model"))
        if vec:
            model, count = max(vec.items(), key=lambda kv: kv[1])
            lvl = OK if count >= n * 0.95 else WARN
            out.append(Finding(lvl, f"vectors: {count}/{n} ({model})",
                               "" if lvl == OK else "restart the server to backfill"))
    except sqlite3.OperationalError:
        pass
    longs = q(f"SELECT id, length(content) FROM memory WHERE {live} AND length(content) > ? "
              "ORDER BY 2 DESC", long_chars)
    if longs:
        ids = ", ".join(f"m{i}" for i, _ in longs[:8])
        out.append(Finding(WARN, f"{len(longs)} memories over {long_chars} chars (e.g. {ids})",
                           "split into a short conclusion + separate facts; long facts cost "
                           "tokens on every hit"))
    bigs = q(f"""SELECT e.name, count(*) FROM memory m JOIN entity e ON e.id = m.entity_id
                 WHERE m.{live.replace(' AND ', ' AND m.')} GROUP BY e.id HAVING count(*) > ?
                 ORDER BY 2 DESC""", big_entity)
    if bigs:
        names = ", ".join(f"{name} ({k})" for name, k in bigs[:6])
        out.append(Finding(WARN, f"{len(bigs)} entities with over {big_entity} facts: {names}",
                           "consider splitting by sub-topic or superseding outdated facts"))
    try:
        stale = q(f"""SELECT id FROM memory WHERE {live} AND type = 'todo' AND status = 'open'
                      AND updated_at < datetime('now', ?)""", f"-{stale_todo_days} days")
        if stale:
            out.append(Finding(WARN, f"{len(stale)} todos open for over {stale_todo_days} days "
                               f"({', '.join(f'm{i}' for (i,) in stale[:8])})",
                               'close them: update(id, status="done"|"dropped")'))
    except sqlite3.OperationalError:
        pass
    sus = [mid for mid, text in q(f"SELECT id, content FROM memory WHERE {live}")
           if INSTRUCTION_LIKE.search(text)]
    if sus:
        out.append(Finding(WARN, f"{len(sus)} memories read like instructions to a model "
                           f"({', '.join(f'm{i}' for i in sus[:8])})",
                           "possible stored prompt injection; review and forget if so"))
    dups = q(f"""SELECT count(*) FROM (SELECT content FROM memory WHERE {live}
                 GROUP BY content HAVING count(*) > 1)""")[0][0]
    if dups:
        out.append(Finding(WARN, f"{dups} texts stored more than once"))
    c.close()
    return out


def report(findings: list[Finding]) -> tuple[str, int]:
    text = "\n".join(f.line() for f in findings)
    fails = sum(f.level == FAIL for f in findings)
    warns = sum(f.level == WARN for f in findings)
    summary = f"\n{fails} problem(s), {warns} warning(s)" if fails or warns else "\nall good"
    return text + summary, 1 if fails else 0
