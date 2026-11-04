"""Where the CLI on a client machine finds the server (for `sync` and `doctor`).

In order: explicit values (flags, sync.toml), env MEMCAIRN_URL / MEMCAIRN_TOKEN,
~/.memcairn/client.toml, then a manual `memory` MCP entry in ~/.claude.json. With the
Claude Code plugin the token sits in the system keychain, out of reach, so `memcairn
setup` writes client.toml (readable by the user only).
"""

from __future__ import annotations

import contextlib
import json
import os
import tomllib
from pathlib import Path

CLIENT = Path.home() / ".memcairn" / "client.toml"


def _claude_json(name: str, home: Path) -> tuple[str | None, str | None]:
    try:
        spec = json.loads((home / ".claude.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, None
    spec = (spec.get("mcpServers") or {}).get(name) or {}
    auth = (spec.get("headers") or {}).get("Authorization", "")
    return spec.get("url"), (auth[7:] if auth.lower().startswith("bearer ") else None)


def read_client(path: Path = CLIENT) -> dict:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def auth(url: str | None = None, token: str | None = None, *, name: str = "memory",
         home: Path | None = None, client_file: Path | None = None) -> tuple[str | None, str | None]:
    """(base url without /mcp, token); either may be None."""
    home = home or Path.home()
    sources = [
        (os.environ.get("MEMCAIRN_URL"), os.environ.get("MEMCAIRN_TOKEN")),
        tuple(read_client(client_file or home / ".memcairn" / "client.toml").get(k)
              for k in ("url", "token")),
        _claude_json(name, home),
    ]
    for u, t in sources:
        url, token = url or u, token or t
    if url:
        url = url.rstrip("/").removesuffix("/mcp")
    return url, token


def write_client(url: str, token: str, path: Path = CLIENT) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'url = "{url}"\ntoken = "{token}"\n', encoding="utf-8")
    with contextlib.suppress(OSError):  # no-op on Windows
        path.chmod(0o600)
