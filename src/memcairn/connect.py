"""Hook Claude Code up to Memcairn in one command, without anyone handling a token.

- `memcairn pair <url> <code>`: a server; the code comes from "Add machine" in the web UI.
- `memcairn connect`: the server already in ~/.memcairn/client.toml (e.g. after `setup`).
- `memcairn connect --local`: just this computer; Claude Code starts Memcairn itself
  (stdio), no server, no token.

Each writes the "memory" MCP entry into ~/.claude.json (user scope) and, unless the plugin
already has one, a session-start hook into ~/.claude/settings.json, both backed up and
recorded, so `memcairn unsetup` takes them out again.

Why a manual entry and not the plugin's server: Claude Code drops duplicates by URL with a
fixed priority, manual entry > claude.ai connector > plugin. Someone who also added
Memcairn as a claude.ai connector (for the phone) would otherwise get the connector in
Claude Code, writing as `claude-ai` instead of this machine. With the manual entry
Claude Code hides both the connector and the plugin's server by itself; the plugin's
session-start hook keeps working.
"""

from __future__ import annotations

import datetime as dt
import json
import re
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
    if previous == entry:
        echo(f'the "memory" MCP server in {path} is already set')
    else:
        doc.setdefault("mcpServers", {})["memory"] = entry
        _write_json(path, doc, backed)
        _record(home, "connect-mcp", path, {"previous": previous})
        echo(f'added the "memory" MCP server to {path}')

    settings = home / ".claude" / "settings.json"
    doc = _load(settings)
    if plugin_enabled(home) or any(_is_ours(g) for g in _session_groups(doc)):
        return  # the plugin's hook or an earlier one already loads the overview
    group = {"matcher": MATCHER, "hooks": [{"type": "command", "timeout": 10,
                                            "command": f'"{_exe()}" context'}]}
    doc.setdefault("hooks", {}).setdefault("SessionStart", []).append(group)
    _write_json(settings, doc, backed)
    _record(home, "context-hook", settings, {"groups": {"SessionStart": group}})
    echo(f"added a session-start hook that loads the memory overview ({settings})")


def pair(home: Path, url: str, code: str, echo: Callable[[str], None], *,
         opener=None) -> str:
    """Trade a pairing code for a token and connect this machine: the client name."""
    import urllib.error
    import urllib.request

    url = url.rstrip("/").removesuffix("/mcp")
    req = urllib.request.Request(f"{url}/pair", data=json.dumps({"code": code}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with (opener or urllib.request.urlopen)(req, timeout=15) as r:
            body = r.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError("the server refused the code (wrong, used or older than 10 "
                           "minutes): make a new one with Add machine" if e.code == 401
                           else f"{url} answered HTTP {e.code}") from e
    except OSError as e:  # DNS, refused, timeout, TLS
        raise RuntimeError(f"cannot reach {url}: {e}") from e
    try:
        got = json.loads(body)
        name, token = got["name"], got["token"]
    except (ValueError, KeyError, TypeError) as e:
        raise RuntimeError(f"{url} gave an unexpected answer: is it a Memcairn server? "
                           "(a proxy or login page in between?)") from e
    toml = home / ".memcairn" / "client.toml"
    client.write_client(url, token, toml)  # first: from here on `memcairn connect` can finish
    echo(f"paired as '{name}'; server and token saved to {toml}")
    try:
        write_entry(home, http_entry(url, token), echo)
    except OSError as e:
        raise RuntimeError(f"paired, but writing the Claude Code configuration failed ({e}); "
                           "fix that and run: memcairn connect") from e
    echo("Restart Claude Code (or start a new session) to load the memory.")
    return name


def connect_saved(home: Path, echo: Callable[[str], None]) -> None:
    """The manual entry for the server and token in ~/.memcairn/client.toml."""
    url, token = client.auth(home=home)
    if not (url and token):
        raise RuntimeError("no server in ~/.memcairn/client.toml: use memcairn pair <url> "
                           "<code> (code from Add machine in the web UI)")
    write_entry(home, http_entry(url, token), echo)
    echo("Restart Claude Code (or start a new session).")


def connect_local(home: Path, echo: Callable[[str], None]) -> None:
    write_entry(home, stdio_entry(), echo)
    echo("Memory lives in ~/.memcairn/memcairn.db on this computer. Restart Claude Code. "
         "Browse it with: memcairn ui")
