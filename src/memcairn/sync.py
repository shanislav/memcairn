"""`memcairn sync`: keep chosen local files in step with shared documents on the server.

Config (TOML, default ~/.memcairn/sync.toml), all optional except what you want synced:

    url = "https://memory.example.com"     # default: ~/.memcairn/client.toml or ~/.claude.json
    token = "..."                          # default: from the same place
    docs_dir = "~/.memcairn/docs"          # mirror every shared document here (optional)

    [[file]]
    name = "servers"                       # the document name on the server
    path = "~/notes/servers.md"            # where it lives on this machine

    [automemory]                           # share Claude Code's auto memory folder
    dir = "~/.claude/projects/<project>/memory"   # this machine's folder for the project
    prefix = "automemory"                  # document names: automemory.<file>; same everywhere

The auto memory set is every *.md in that folder (except files mapped by [[file]]).
Unlike [[file]] entries, deletions travel too: a file deleted on one machine is deleted
on the server and then on the others, but only if nobody changed it meanwhile.

Per file, only when something changed (by content hash against the last sync):
- changed here only → upload; changed on the server only → download;
- changed on both → three-way merge with `git merge-file` against the last synced
  version; a clean merge is uploaded and written; overlapping edits are left alone:
  the merge with conflict markers goes to `<path>.conflict` and an open todo asks the
  user to decide. Nothing is ever overwritten without a common base.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .docs import sha

HOME = Path.home() / ".memcairn"
DEFAULT_CONFIG = HOME / "sync.toml"
STATE = HOME / "sync-state.json"


@dataclass
class Entry:
    name: str
    path: Path
    deletes: bool = False  # propagate deletions (auto memory set)


class Remote:
    def __init__(self, url: str, token: str | None):
        self.base = url.rstrip("/").removesuffix("/mcp")
        self.token = token

    def _req(self, method: str, path: str, body: dict | None = None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"null")

    def list(self) -> dict[str, dict]:
        status, data = self._req("GET", "/docs")
        if status != 200:
            raise RuntimeError(f"GET /docs → {status}")
        return {d["name"]: d for d in data}

    def get(self, name: str, version: int | None = None) -> dict:
        status, data = self._req("GET", f"/docs/{name}" + (f"?version={version}" if version
                                                            else ""))
        if status != 200:
            raise RuntimeError(f"GET /docs/{name} → {status}: {data}")
        return data

    def put(self, name: str, content: str, base_version: int) -> tuple[int, dict]:
        return self._req("PUT", f"/docs/{name}", {"content": content,
                                                  "base_version": base_version})

    def delete(self, name: str, base_version: int) -> tuple[int, dict]:
        return self._req("DELETE", f"/docs/{name}?base_version={base_version}")

    def todo(self, text: str) -> None:
        """Leave an open todo through the MCP endpoint, so the next session sees it."""
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "remember", "arguments": {"content": text, "type": "todo",
                                              "entity": "Memcairn sync", "importance": 0.9}}}
        req = urllib.request.Request(self.base + "/mcp", data=json.dumps(body).encode(),
                                     method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json, text/event-stream")
        req.add_header("MCP-Protocol-Version", "2025-11-25")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        with contextlib.suppress(urllib.error.URLError, OSError):
            urllib.request.urlopen(req, timeout=10).read()


def load_config(path: Path) -> tuple[str | None, str | None, Path | None, list[Entry],
                                      tuple[Path, str] | None]:
    cfg = tomllib.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    from .client import auth

    url, token = auth(cfg.get("url"), cfg.get("token"), name=cfg.get("server", "memory"))
    docs_dir = Path(cfg["docs_dir"]).expanduser() if cfg.get("docs_dir") else None
    entries = [Entry(f["name"], Path(f["path"]).expanduser()) for f in cfg.get("file", [])]
    am = cfg.get("automemory")
    automemory = ((Path(am["dir"]).expanduser(), am.get("prefix", AUTOMEMORY))
                  if am and am.get("dir") else None)
    return url, token, docs_dir, entries, automemory


AUTOMEMORY = "automemory"


def doc_name(prefix: str, stem: str) -> str:
    return f"{prefix}.{re.sub(r'[^a-z0-9._-]', '-', stem.lower())}"[:80]


def file_name(stem: str) -> str:
    return "MEMORY.md" if stem == "memory" else f"{stem}.md"


def automemory_entries(folder: Path, prefix: str, server_names, skip: set[Path],
                       out: list[str]) -> list[Entry]:
    """The shared auto memory set: local *.md files plus documents only the server has."""
    names: dict[str, Path] = {}
    for p in sorted(folder.glob("*.md")) if folder.is_dir() else []:
        if p.resolve() in skip:
            continue
        n = doc_name(prefix, p.stem)
        if n in names:
            out.append(f"skipped {p.name}: same document name as {names[n].name}")
            continue
        names[n] = p
    for n in server_names:
        if n.startswith(prefix + ".") and n not in names:
            names[n] = folder / file_name(n[len(prefix) + 1:])
    return [Entry(n, p, deletes=True) for n, p in sorted(names.items())]


def merge3(local: str, base: str, remote: str) -> tuple[str, bool]:
    """(merged text, clean?) using git's merge-file; without git every overlap conflicts."""
    git = shutil.which("git")
    if not git:
        return local, False
    with tempfile.TemporaryDirectory() as tmp:
        paths = []
        for label, text in (("local", local), ("base", base), ("remote", remote)):
            p = Path(tmp) / label
            p.write_text(text, encoding="utf-8", newline="")
            paths.append(str(p))
        r = subprocess.run([git, "merge-file", "-p", "-L", "this machine", "-L", "last sync",
                            "-L", "server", *paths], capture_output=True)
        return r.stdout.decode("utf-8"), r.returncode == 0


def _read(path: Path) -> str | None:
    return path.read_text(encoding="utf-8") if path.is_file() else None


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".memcairn-tmp")
    tmp.write_text(text, encoding="utf-8", newline="")
    os.replace(tmp, path)


def sync_entry(remote: Remote, e: Entry, server: dict | None, state: dict) -> str | None:
    """Sync one file; returns a short line describing what happened, or None."""
    local = _read(e.path)
    known = state.get(e.name)  # {"version", "sha"} of the last sync, or None
    local_changed = local is not None and (known is None or sha(local) != known["sha"])
    remote_changed = server is not None and (known is None or server["version"] != known["version"])

    def done(version: int, digest: str) -> None:
        state[e.name] = {"version": version, "sha": digest}

    if server is None and local is None:
        state.pop(e.name, None)
        return None
    if e.deletes and known and server is None and not local_changed:
        e.path.unlink()  # deleted on the server (by another machine), unchanged here
        state.pop(e.name, None)
        return f"removed {e.path.name} (deleted on another machine)"
    if e.deletes and known and local is None and not remote_changed:
        status, data = remote.delete(e.name, known["version"])
        if status == 200:
            state.pop(e.name, None)
            return f"deleted {e.name} on the server (deleted here)"
        server = remote.list().get(e.name)  # changed meanwhile: keep it, download below
    if server is None:  # new here
        status, data = remote.put(e.name, local, 0)
        if status == 200:
            done(data["version"], data["sha"])
            return f"uploaded new {e.name}"
        return _upload_error(e, data)
    if local is None:  # new on the server
        doc = remote.get(e.name)
        _write(e.path, doc["content"])
        done(doc["version"], doc["sha"])
        return f"downloaded {e.name} → {e.path}"
    if not local_changed and not remote_changed:
        return None
    if local_changed and not remote_changed:
        status, data = remote.put(e.name, local, known["version"] if known else 0)
        if status == 200:
            done(data["version"], data["sha"])
            return f"uploaded {e.name} v{data['version']}"
        if status != 409:
            return _upload_error(e, data)
        server = remote.list().get(e.name)  # someone wrote meanwhile: fall through to merge
    if remote_changed and not local_changed:
        doc = remote.get(e.name)
        _write(e.path, doc["content"])
        done(doc["version"], doc["sha"])
        return f"updated {e.name} from server v{doc['version']}"
    # changed on both sides
    current = remote.get(e.name)
    if sha(local) == current["sha"]:
        done(current["version"], current["sha"])
        return None
    base = remote.get(e.name, known["version"])["content"] if known else ""
    merged, clean = merge3(local, base, current["content"])
    if clean:
        status, data = remote.put(e.name, merged, current["version"])
        if status == 200:
            _write(e.path, merged)
            done(data["version"], data["sha"])
            return f"merged {e.name} (changes on both sides, no overlap)"
    conflict = e.path.with_name(e.path.name + ".conflict")
    _write(conflict, merged if not clean else current["content"])
    remote.todo(f"Sync conflict in document '{e.name}': this machine ({e.path}) and the "
                f"server both changed the same part. Merge proposal with markers is in "
                f"{conflict}; show the user both versions, write the agreed result to {e.path},"
                " delete the .conflict file and run memcairn sync.")
    return f"CONFLICT {e.name}: see {conflict}"


def _upload_error(e: Entry, data) -> str:
    msg = (data or {}).get("error", data) if isinstance(data, dict) else data
    if "secret" in str(msg):
        return f"skipped {e.path.name}: {msg} (the file stays local)"
    return f"error uploading {e.name}: {msg}"


def run(config: Path = DEFAULT_CONFIG, state_path: Path = STATE) -> list[str]:
    url, token, docs_dir, entries, automemory = load_config(config)
    if not url:
        return ["no server configured: run 'memcairn setup' or set url/token in sync.toml"]
    remote = Remote(url, token)
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {}
    server = remote.list()
    out: list[str] = []
    prefix = automemory[1] if automemory else AUTOMEMORY
    if automemory:
        skip = {e.path.resolve() for e in entries}
        entries += automemory_entries(automemory[0], prefix, server, skip, out)
    if docs_dir:  # mirror every shared document not mapped explicitly
        mapped = {e.name for e in entries}
        local_docs = {p.stem for p in docs_dir.glob("*.md")} if docs_dir.is_dir() else set()
        for name in sorted((set(server) | local_docs) - mapped):
            if not name.startswith(prefix + "."):  # auto memory belongs in its own folder
                entries.append(Entry(name, docs_dir / f"{name}.md"))
    for e in entries:
        try:
            line = sync_entry(remote, e, server.get(e.name), state)
        except (RuntimeError, OSError, urllib.error.URLError) as err:
            line = f"error syncing {e.name}: {err}"
        if line:
            out.append(line)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=1), encoding="utf-8")
    return out
