"""Hook Claude Code up to Memcairn in one command, without anyone handling a token.

- `memcairn pair <url> <code>`: a server; the code comes from "Add machine" in the web UI.
- `memcairn connect --local`: just this computer; Claude Code starts Memcairn itself
  (stdio), no server, no token.

With the `claude` CLI on PATH a server connection goes through the plugin (the token lands
in the system keychain). Otherwise, e.g. the desktop app only, Memcairn writes the "memory"
MCP entry into ~/.claude.json and a session-start hook into ~/.claude/settings.json,
both backed up and recorded, so `memcairn unsetup` takes them out again.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from . import client
from .clientsetup import (
    MATCHER,
    _exe,
    _load,
    _save_state,
    _session_groups,
    _write_json,
    load_state,
    plugin_enabled,
)

MARKETPLACE = "shanislav/memcairn"


def http_entry(url: str, token: str) -> dict:
    return {"type": "http", "url": f"{url}/mcp", "headers": {"Authorization": f"Bearer {token}"}}


def stdio_entry() -> dict:
    import socket

    name = re.sub(r"[^A-Za-z0-9._-]", "-", socket.gethostname()) or "local"
    return {"type": "stdio", "command": _exe(), "args": ["serve", "--client", name], "env": {}}


def _is_ours(group: dict) -> bool:
    """A hook that already loads the overview (curl …/context, or memcairn context)."""
    return any("/context" in (h.get("command") or "")
               or re.search(r'(?i)memcairn(\.exe)?"?\s+context', h.get("command") or "")
               for h in group.get("hooks", []))


def _record(home: Path, id_: str, file: Path, undo: dict) -> None:
    applied = load_state(home)
    applied.append({"id": id_, "file": str(file), "undo": undo,
                    "at": dt.datetime.now().isoformat(timespec="seconds")})
    _save_state(home, applied)


def write_entry(home: Path, entry: dict, echo: Callable[[str], None]) -> None:
    """The "memory" MCP server in ~/.claude.json (user scope) plus the overview hook."""
    backed: set[Path] = set()
    path = home / ".claude.json"
    doc = _load(path)
    previous = (doc.get("mcpServers") or {}).get("memory")
    doc.setdefault("mcpServers", {})["memory"] = entry
    _write_json(path, doc, backed)
    _record(home, "connect-mcp", path, {"previous": previous})
    echo(f'added the "memory" MCP server to {path}')

    settings = home / ".claude" / "settings.json"
    doc = _load(settings)
    if any(_is_ours(g) for g in _session_groups(doc)):
        return
    group = {"matcher": MATCHER, "hooks": [{"type": "command", "timeout": 10,
                                            "command": f'"{_exe()}" context'}]}
    doc.setdefault("hooks", {}).setdefault("SessionStart", []).append(group)
    _write_json(settings, doc, backed)
    _record(home, "context-hook", settings, {"groups": {"SessionStart": group}})
    echo(f"added a session-start hook that loads the memory overview ({settings})")


def plugin_install(url: str, token: str, echo: Callable[[str], None]) -> bool:
    exe = shutil.which("claude")
    if not exe:
        return False
    cmd = [exe, "plugin", "install", "memcairn", "--marketplace", MARKETPLACE,
           "--config", f"url={url}", "--config", f"token={token}"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if r.returncode != 0:
        echo("plugin install failed, falling back to a manual entry: "
             + (r.stderr or r.stdout).strip().replace(token, "***")[:300])
        return False
    echo("installed the Memcairn plugin (token stored in the system keychain)")
    return True


def pair(home: Path, url: str, code: str, echo: Callable[[str], None], *,
         use_plugin: bool = True, opener=None) -> str:
    """Trade a pairing code for a token and connect this machine: the client name."""
    import urllib.request

    url = url.rstrip("/").removesuffix("/mcp")
    req = urllib.request.Request(f"{url}/pair", data=json.dumps({"code": code}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with (opener or urllib.request.urlopen)(req, timeout=15) as r:
            got = json.loads(r.read())
    except Exception as e:  # HTTPError 401 included
        detail = getattr(e, "code", None)
        raise RuntimeError("the server refused the code (wrong, used or older than 10 "
                           "minutes): make a new one with Add machine" if detail == 401
                           else f"cannot reach {url}: {e}") from e
    name, token = got["name"], got["token"]
    client.write_client(url, token, home / ".memcairn" / "client.toml")
    echo(f"paired as '{name}'; server and token saved to {home / '.memcairn' / 'client.toml'}")
    if plugin_enabled(home):
        echo("the Memcairn plugin is already installed: it keeps its own token. If it "
             "should use this one, uninstall the plugin and run pair again.")
    elif not (use_plugin and plugin_install(url, token, echo)):
        write_entry(home, http_entry(url, token), echo)
    echo("Restart Claude Code (or start a new session) to load the memory.")
    return name


def connect_local(home: Path, echo: Callable[[str], None]) -> None:
    write_entry(home, stdio_entry(), echo)
    echo("Memory lives in ~/.memcairn/memcairn.db on this computer. Restart Claude Code. "
         "Browse it with: memcairn ui")
