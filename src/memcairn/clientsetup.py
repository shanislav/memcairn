"""`memcairn setup` / `memcairn unsetup`: the optional changes the plugin does not make.

The Claude Code plugin only adds the memory tools and the session-start overview. Changes
to the user's own Claude configuration are offered here, one by one, each with a diff, a
backup of the file and a y/n question; nothing touches CLAUDE.md. Every applied change is
recorded in ~/.memcairn/setup-state.json, so `unsetup` reverts exactly those.

Offered changes:
- client: write ~/.memcairn/client.toml (url + token for `sync` / `doctor`), taken from a
  manual MCP entry, since with the plugin the token lives in the keychain;
- manual-mcp: remove a manual "memory" MCP entry from ~/.claude.json (the plugin has one);
- manual-hook: remove a manual SessionStart hook that curls /context (the plugin has one);
- auto-memory: turn off Claude Code's built-in auto memory (MEMORY.md is read into every
  session; with Memcairn it is redundant): autoMemoryEnabled = false;
- sync-hook: run `memcairn sync --quiet` at session start (only when sync.toml exists).
"""

from __future__ import annotations

import contextlib
import copy
import datetime as dt
import difflib
import json
import re
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import client

PLUGIN = "memcairn"
MATCHER = "startup|clear|compact"


@dataclass
class Change:
    id: str
    title: str
    file: Path
    key: str  # top-level key the diff shows
    apply: Callable[[dict], None]
    undo: dict = field(default_factory=dict)


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def plugin_enabled(home: Path) -> bool:
    enabled = _load(home / ".claude" / "settings.json").get("enabledPlugins") or {}
    return any(k.split("@")[0] == PLUGIN and v for k, v in enabled.items())


def _session_groups(settings: dict) -> list[dict]:
    return (settings.get("hooks") or {}).get("SessionStart") or []


def _is_context_hook(group: dict) -> bool:
    return any("/context" in (h.get("command") or "") for h in group.get("hooks", []))


def _is_sync_hook(group: dict) -> bool:
    return any(re.search(r"(?i)memcairn(\.exe)?\"?\s+sync", h.get("command") or "")
               for h in group.get("hooks", []))


def _exe() -> str:
    found = shutil.which("memcairn")
    return Path(found or sys.argv[0]).as_posix()


def plan(home: Path) -> list[Change]:
    claude_json, settings_path = home / ".claude.json", home / ".claude" / "settings.json"
    claude, settings = _load(claude_json), _load(settings_path)
    plugin = plugin_enabled(home)
    out: list[Change] = []

    manual = (claude.get("mcpServers") or {}).get("memory")
    client_file = home / ".memcairn" / "client.toml"
    url, token = client._claude_json("memory", home)
    if not client_file.is_file() and url and token:
        out.append(Change("client", f"save server URL and token for the CLI to {client_file}",
                          client_file, "", lambda d: None))
    if plugin and manual:
        def drop_mcp(d: dict) -> None:
            d["mcpServers"].pop("memory", None)
        out.append(Change("manual-mcp", 'remove the manual "memory" MCP server (the plugin '
                          "provides it)", claude_json, "mcpServers", drop_mcp,
                          {"spec": manual}))
    ctx = [g for g in _session_groups(settings) if _is_context_hook(g)]
    if plugin and ctx:
        def drop_hook(d: dict) -> None:
            d["hooks"]["SessionStart"] = [g for g in _session_groups(d) if g not in ctx]
            if not d["hooks"]["SessionStart"]:
                del d["hooks"]["SessionStart"]
        out.append(Change("manual-hook", "remove the manual SessionStart hook that loads "
                          "/context (the plugin provides it)", settings_path, "hooks",
                          drop_hook, {"groups": ctx}))
    if settings.get("autoMemoryEnabled") is not False:
        def no_auto(d: dict) -> None:
            d["autoMemoryEnabled"] = False
        out.append(Change("auto-memory", "turn off Claude Code auto memory (MEMORY.md is "
                          "read into every session; Memcairn replaces it)", settings_path,
                          "autoMemoryEnabled", no_auto,
                          {"previous": settings.get("autoMemoryEnabled")}))
    if (home / ".memcairn" / "sync.toml").is_file() and not any(
            _is_sync_hook(g) for g in _session_groups(settings)):
        group = {"matcher": MATCHER, "hooks": [
            {"type": "command", "command": f'"{_exe()}" sync --quiet', "timeout": 15}]}

        def add_sync(d: dict) -> None:
            d.setdefault("hooks", {}).setdefault("SessionStart", []).append(group)
        out.append(Change("sync-hook", "sync shared documents at session start "
                          "(memcairn sync --quiet)", settings_path, "hooks", add_sync,
                          {"group": group}))
    return out


_BEARER = re.compile(r"(Bearer\s+)[^\s\"]+")


def diff(change: Change, before: dict, after: dict) -> str:
    if change.id == "client":
        return f"+ {change.file}: url, token (readable by you only)"

    def dump(d: dict) -> list[str]:
        if change.key not in d:
            return []
        text = json.dumps({change.key: d[change.key]}, indent=2, ensure_ascii=False)
        return _BEARER.sub(r"\1***", text).splitlines()
    lines = difflib.unified_diff(dump(before), dump(after), str(change.file),
                                 str(change.file), lineterm="", n=2)
    return "\n".join(list(lines)[2:])


def _stamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def _write_json(path: Path, doc: dict, backed: set[Path]) -> None:
    if path.is_file() and path not in backed:
        shutil.copy2(path, path.with_name(f"{path.name}.pre-memcairn-{_stamp()}"))
        backed.add(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _state_path(home: Path) -> Path:
    return home / ".memcairn" / "setup-state.json"


def _save_state(home: Path, applied: list[dict]) -> None:
    path = _state_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(applied, indent=2, ensure_ascii=False), encoding="utf-8")
    with contextlib.suppress(OSError):  # may hold a token (the removed MCP entry)
        path.chmod(0o600)


def load_state(home: Path) -> list[dict]:
    try:
        return json.loads(_state_path(home).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []


def run_setup(home: Path, confirm: Callable[[Change, str], bool],
              echo: Callable[[str], None]) -> int:
    changes = plan(home)
    if not plugin_enabled(home):
        echo("The Memcairn plugin is not enabled in Claude Code. Install it first:\n"
             "  /plugin marketplace add shanislav/memcairn\n"
             "  /plugin install memcairn@memcairn\n"
             "(only the optional changes below are offered until then)")
    if not changes:
        echo("nothing to change")
        return 0
    applied, backed, docs = load_state(home), set(), {}
    done = 0
    for c in changes:
        doc = docs.setdefault(c.file, _load(c.file)) if c.id != "client" else {}
        after = copy.deepcopy(doc)
        c.apply(after)
        if not confirm(c, diff(c, doc, after)):
            continue
        if c.id == "client":
            url, token = client._claude_json("memory", home)
            client.write_client(url.rstrip("/").removesuffix("/mcp"), token, c.file)
        else:
            _write_json(c.file, after, backed)
            docs[c.file] = after
        applied.append({"id": c.id, "file": str(c.file), "undo": c.undo,
                        "at": dt.datetime.now().isoformat(timespec="seconds")})
        _save_state(home, applied)
        done += 1
    if done:
        echo(f"{done} change(s) applied; backups next to the files (*.pre-memcairn-*). "
             "Restart Claude Code. Revert with: memcairn unsetup")
    return done


def run_unsetup(home: Path, confirm: Callable[[str], bool], echo: Callable[[str], None]) -> int:
    applied = load_state(home)
    if not applied:
        echo("nothing recorded by memcairn setup")
        return 0
    backed: set[Path] = set()
    kept, done = [], 0
    for a in reversed(applied):
        path, undo = Path(a["file"]), a["undo"]
        if not confirm(f"revert {a['id']} in {path}?"):
            kept.insert(0, a)
            continue
        if a["id"] == "client":
            path.unlink(missing_ok=True)
        else:
            doc = _load(path)
            if a["id"] == "manual-mcp":
                doc.setdefault("mcpServers", {})["memory"] = undo["spec"]
            elif a["id"] == "manual-hook":
                doc.setdefault("hooks", {}).setdefault("SessionStart", []).extend(undo["groups"])
            elif a["id"] == "auto-memory":
                if undo.get("previous") is None:
                    doc.pop("autoMemoryEnabled", None)
                else:
                    doc["autoMemoryEnabled"] = undo["previous"]
            elif a["id"] == "sync-hook":
                groups = [g for g in _session_groups(doc) if g != undo["group"]]
                if groups:
                    doc["hooks"]["SessionStart"] = groups
                else:
                    doc.get("hooks", {}).pop("SessionStart", None)
            _write_json(path, doc, backed)
        done += 1
    _save_state(home, kept)
    echo(f"{done} change(s) reverted. Restart Claude Code.")
    return done
