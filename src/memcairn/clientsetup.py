"""`memcairn setup` / `memcairn unsetup`: the optional changes the plugin does not make.

The Claude Code plugin only adds the memory tools and the session-start overview. Changes
to the user's own Claude configuration are offered here, one by one, each with a diff, a
backup of the file and a y/n question; nothing touches CLAUDE.md. Every applied change is
recorded in ~/.memcairn/setup-state.json, so `unsetup` reverts exactly those.

Offered changes:
- client: write ~/.memcairn/client.toml (url + token for `sync` / `doctor`), taken from a
  manual MCP entry, since with the plugin the token lives in the keychain;
- manual-hook: remove a manual SessionStart hook that curls /context (the plugin has one);
- automemory: share Claude Code's auto memory folder of this project with your other
  machines through Memcairn (a section in ~/.memcairn/sync.toml). Auto memory keeps
  working as before (loaded every session, works offline); it just follows you around;
- sync-hook: run `memcairn sync --quiet` at session start and end (when there is
  something to sync);
- enable-auto-memory: turn auto memory back on if an earlier `setup` turned it off;
- auto-memory: turn auto memory OFF, only with --disable-auto-memory (for people who want
  Memcairn as their only memory; most people should keep auto memory).
"""

from __future__ import annotations

import contextlib
import copy
import datetime as dt
import difflib
import json
import os
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


def _session_groups(settings: dict, event: str = "SessionStart") -> list[dict]:
    return (settings.get("hooks") or {}).get(event) or []


AM_BEGIN = "# >>> memcairn setup: shared auto memory"
AM_END = "# <<< memcairn setup"


def _git_root(cwd: Path) -> Path | None:
    import subprocess

    git = shutil.which("git")
    if not git:
        return None
    r = subprocess.run([git, "-C", str(cwd), "rev-parse", "--show-toplevel"],
                       capture_output=True, text=True)
    return Path(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else None


def project_memory_dir(home: Path, cwd: Path, settings: dict) -> Path:
    """Where Claude Code keeps auto memory for the project at cwd (its git root)."""
    custom = settings.get("autoMemoryDirectory")
    if custom:
        return Path(custom).expanduser()
    root = _git_root(cwd) or cwd
    return home / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(root)) / "memory"


def automemory_block(folder: Path) -> str:
    return (f"\n{AM_BEGIN}\n[automemory]\ndir = \"{folder.as_posix()}\"\n"
            f"prefix = \"automemory\"\n{AM_END}\n")


def _is_context_hook(group: dict) -> bool:
    return any("/context" in (h.get("command") or "") for h in group.get("hooks", []))


def _is_sync_hook(group: dict) -> bool:
    return any(re.search(r"(?i)memcairn(\.exe)?\"?\s+sync", h.get("command") or "")
               for h in group.get("hooks", []))


def _exe() -> str:
    found = shutil.which("memcairn")
    return Path(found or sys.argv[0]).as_posix()


def plan(home: Path, cwd: Path | None = None, *,
         disable_auto_memory: bool = False) -> list[Change]:
    cwd = cwd or Path.cwd()
    settings_path = home / ".claude" / "settings.json"
    settings = _load(settings_path)
    plugin = plugin_enabled(home)
    out: list[Change] = []

    client_file = home / ".memcairn" / "client.toml"
    url, token = client._claude_json("memory", home)
    if not client_file.is_file() and url and token:
        out.append(Change("client", f"save server URL and token for the CLI to {client_file}",
                          client_file, "", lambda d: None))
    # a manual "memory" entry next to the plugin stays: Claude Code prefers it and hides
    # the plugin's server and a claude.ai connector to the same URL (no tools twice)
    # only the /context hooks themselves: a group may also hold other hooks (e.g. sync)
    ctx = [{**({"matcher": g["matcher"]} if "matcher" in g else {}),
            "hooks": [h for h in g.get("hooks", []) if _is_context_hook({"hooks": [h]})]}
           for g in _session_groups(settings) if _is_context_hook(g)]
    if plugin and ctx:
        def drop_hook(d: dict) -> None:
            groups = []
            for g in _session_groups(d):
                hooks = [h for h in g.get("hooks", []) if not _is_context_hook({"hooks": [h]})]
                if hooks:
                    groups.append({**g, "hooks": hooks})
            if groups:
                d["hooks"]["SessionStart"] = groups
            else:
                del d["hooks"]["SessionStart"]
        out.append(Change("manual-hook", "remove the manual SessionStart hook that loads "
                          "/context (the plugin provides it)", settings_path, "hooks",
                          drop_hook, {"groups": ctx}))
    auto_off = settings.get("autoMemoryEnabled") is False
    by_setup = any(a["id"] == "auto-memory" for a in load_state(home))
    if disable_auto_memory and not auto_off:
        def no_auto(d: dict) -> None:
            d["autoMemoryEnabled"] = False
        out.append(Change("auto-memory", "turn off Claude Code auto memory (Memcairn becomes "
                          "your only memory)", settings_path, "autoMemoryEnabled", no_auto,
                          {"previous": settings.get("autoMemoryEnabled")}))
    elif auto_off and by_setup and not disable_auto_memory:
        def auto_on(d: dict) -> None:
            d.pop("autoMemoryEnabled", None)
        out.append(Change("enable-auto-memory", "turn Claude Code auto memory back on (an "
                          "earlier memcairn setup turned it off; Memcairn now complements it)",
                          settings_path, "autoMemoryEnabled", auto_on, {"previous": False}))
        auto_off = False

    sync_toml = home / ".memcairn" / "sync.toml"
    toml_text = sync_toml.read_text(encoding="utf-8") if sync_toml.is_file() else ""
    folder = project_memory_dir(home, cwd, settings)
    share = not auto_off and "[automemory]" not in toml_text
    if share:
        out.append(Change("automemory", f"share this project's auto memory ({folder}) with "
                          "your other machines", sync_toml, "", lambda d: None,
                          {"block": automemory_block(folder)}))
    if sync_toml.is_file() or share:
        groups = {}
        for event, matcher in (("SessionStart", MATCHER), ("SessionEnd", None)):
            if not any(_is_sync_hook(g) for g in _session_groups(settings, event)):
                g = {"hooks": [{"type": "command", "command": f'"{_exe()}" sync --quiet',
                                "timeout": 15}]}
                groups[event] = {"matcher": matcher, **g} if matcher else g
        if groups:
            def add_sync(d: dict) -> None:
                for event, g in groups.items():
                    d.setdefault("hooks", {}).setdefault(event, []).append(g)
            out.append(Change("sync-hook", "run memcairn sync --quiet at session "
                              + " and ".join(e.removeprefix("Session").lower() for e in groups),
                              settings_path, "hooks", add_sync, {"groups": groups}))
    return out


_BEARER = re.compile(r"(Bearer\s+)[^\s\"]+")


def diff(change: Change, before: dict, after: dict) -> str:
    if change.id == "client":
        return f"+ {change.file}: url, token (readable by you only)"
    if change.id == "automemory":
        return "\n".join(f"+ {line}" for line in change.undo["block"].strip().splitlines())

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
    text = json.dumps(doc, indent=2, ensure_ascii=False) + "\n"
    # Claude Code may be running and read the file at any moment: never show it half a file.
    tmp = path.with_name(f".{path.name}.memcairn-tmp")
    tmp.write_text(text, encoding="utf-8")
    try:
        os.replace(tmp, path)
    except PermissionError:  # Windows, file held open: write in place instead
        tmp.unlink(missing_ok=True)
        path.write_text(text, encoding="utf-8")


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
              echo: Callable[[str], None], cwd: Path | None = None,
              disable_auto_memory: bool = False) -> int:
    changes = plan(home, cwd, disable_auto_memory=disable_auto_memory)
    if not plugin_enabled(home) and not (_load(home / ".claude.json").get("mcpServers")
                                         or {}).get("memory"):
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
        special = c.id in ("client", "automemory")
        doc = {} if special else docs.setdefault(c.file, _load(c.file))
        after = copy.deepcopy(doc)
        c.apply(after)
        if not confirm(c, diff(c, doc, after)):
            continue
        if c.id == "client":
            url, token = client._claude_json("memory", home)
            client.write_client(url.rstrip("/").removesuffix("/mcp"), token, c.file)
        elif c.id == "automemory":
            c.file.parent.mkdir(parents=True, exist_ok=True)
            if c.file.is_file() and c.file not in backed:
                shutil.copy2(c.file, c.file.with_name(f"{c.file.name}.pre-memcairn-{_stamp()}"))
                backed.add(c.file)
            text = c.file.read_text(encoding="utf-8") if c.file.is_file() else (
                "# memcairn sync: url/token come from ~/.memcairn/client.toml\n")
            c.file.write_text(text.rstrip("\n") + "\n" + c.undo["block"], encoding="utf-8")
        else:
            # Re-read: Claude Code may have written the file while the user was answering.
            after = _load(c.file)
            c.apply(after)
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


def run_unsetup(home: Path, confirm: Callable[[str], bool], echo: Callable[[str], None],
                only: str | None = None) -> int:
    applied = load_state(home)
    if not applied:
        echo("nothing recorded by memcairn setup")
        return 0
    backed: set[Path] = set()
    kept, done = [], 0
    for a in reversed(applied):
        path, undo = Path(a["file"]), a["undo"]
        if (only and a["id"] != only) or not confirm(f"revert {a['id']} in {path}?"):
            kept.insert(0, a)
            continue
        if a["id"] == "client":
            path.unlink(missing_ok=True)
        elif a["id"] == "automemory":
            if path.is_file():
                text = path.read_text(encoding="utf-8")
                text = re.sub(rf"\n?{re.escape(AM_BEGIN)}.*?{re.escape(AM_END)}\n?", "\n",
                              text, flags=re.S)
                path.write_text(text.rstrip("\n") + "\n", encoding="utf-8")
        else:
            doc = _load(path)
            if a["id"] == "manual-mcp":
                doc.setdefault("mcpServers", {})["memory"] = undo["spec"]
            elif a["id"] == "connect-mcp":  # memcairn pair / connect --local
                if undo.get("previous"):
                    doc.setdefault("mcpServers", {})["memory"] = undo["previous"]
                else:
                    (doc.get("mcpServers") or {}).pop("memory", None)
            elif a["id"] == "context-hook":
                group = undo["groups"]["SessionStart"]
                groups = [g for g in _session_groups(doc) if g != group]
                if groups:
                    doc["hooks"]["SessionStart"] = groups
                else:
                    doc.get("hooks", {}).pop("SessionStart", None)
            elif a["id"] == "manual-hook":
                doc.setdefault("hooks", {}).setdefault("SessionStart", []).extend(undo["groups"])
            elif a["id"] in ("auto-memory", "enable-auto-memory"):
                if undo.get("previous") is None:
                    doc.pop("autoMemoryEnabled", None)
                else:
                    doc["autoMemoryEnabled"] = undo["previous"]
            elif a["id"] == "sync-hook":
                added = undo.get("groups") or {"SessionStart": undo["group"]}  # older records
                for event, group in added.items():
                    groups = [g for g in _session_groups(doc, event) if g != group]
                    if groups:
                        doc["hooks"][event] = groups
                    else:
                        doc.get("hooks", {}).pop(event, None)
            _write_json(path, doc, backed)
        done += 1
    _save_state(home, kept)
    echo(f"{done} change(s) reverted. Restart Claude Code.")
    return done
