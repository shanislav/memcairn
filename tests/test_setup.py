import json

from memcairn import client
from memcairn import clientsetup as cs

TOKEN = "tok-123"
MANUAL = {"type": "http", "url": "https://m.example.com/mcp",
          "headers": {"Authorization": f"Bearer {TOKEN}"}}
CTX_HOOK = {"matcher": "startup", "hooks": [
    {"type": "command", "command": "curl -sf -H x https://m.example.com/context"}]}
OTHER_HOOK = {"hooks": [{"type": "command", "command": "echo hi"}]}


def write(path, doc):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc), encoding="utf-8")


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def home_with_manual_setup(tmp_path, plugin=True):
    write(tmp_path / ".claude.json", {"mcpServers": {"memory": MANUAL, "other": {"url": "x"}},
                                      "projects": {"/p": {}}})
    settings = {"theme": "dark", "hooks": {"SessionStart": [CTX_HOOK, OTHER_HOOK]}}
    if plugin:
        settings["enabledPlugins"] = {"memcairn@memcairn": True}
    write(tmp_path / ".claude" / "settings.json", settings)
    (tmp_path / ".memcairn").mkdir()
    (tmp_path / ".memcairn" / "sync.toml").write_text("", encoding="utf-8")
    return tmp_path


def test_setup_moves_manual_setup_to_plugin_and_unsetup_restores(tmp_path):
    home = home_with_manual_setup(tmp_path)
    claude0, settings0 = read(home / ".claude.json"), read(home / ".claude" / "settings.json")
    shown = []
    n = cs.run_setup(home, lambda c, diff: shown.append((c.id, diff)) or True, lambda s: None,
                     cwd=tmp_path / "work")
    assert [i for i, _ in shown] == ["client", "manual-mcp", "manual-hook", "automemory",
                                     "sync-hook"] and n == 5
    assert all(TOKEN not in diff for _, diff in shown)  # diffs never show the token
    claude, settings = read(home / ".claude.json"), read(home / ".claude" / "settings.json")
    assert "memory" not in claude["mcpServers"] and claude["projects"] == {"/p": {}}
    assert "autoMemoryEnabled" not in settings and settings["theme"] == "dark"  # untouched
    groups = settings["hooks"]["SessionStart"]
    assert CTX_HOOK not in groups and OTHER_HOOK in groups
    assert any("sync --quiet" in h["command"] for g in groups for h in g["hooks"])
    assert any("sync --quiet" in h["command"]
               for g in settings["hooks"]["SessionEnd"] for h in g["hooks"])
    toml = (home / ".memcairn" / "sync.toml").read_text("utf-8")
    assert "[automemory]" in toml and "/memory" in toml
    assert client.auth(home=home) == ("https://m.example.com", TOKEN)  # from client.toml
    assert list(home.glob(".claude.json.pre-memcairn-*"))
    assert cs.plan(home, tmp_path / "work") == []  # idempotent

    cs.run_unsetup(home, lambda q: True, lambda s: None)
    assert read(home / ".claude.json") == claude0
    restored = read(home / ".claude" / "settings.json")
    assert restored["theme"] == "dark" and "SessionEnd" not in restored["hooks"]
    assert sorted(map(json.dumps, restored["hooks"]["SessionStart"])) == sorted(
        map(json.dumps, settings0["hooks"]["SessionStart"]))
    assert "[automemory]" not in (home / ".memcairn" / "sync.toml").read_text("utf-8")
    assert not (home / ".memcairn" / "client.toml").exists()


def test_project_memory_dir_matches_claude_code_naming(tmp_path):
    from pathlib import PureWindowsPath

    settings = {}
    folder = cs.project_memory_dir(tmp_path, tmp_path / "proj", settings)
    assert folder.parent.parent == tmp_path / ".claude" / "projects"
    assert folder.name == "memory" and "-proj" in folder.parent.name
    import re
    assert re.sub(r"[^A-Za-z0-9]", "-", str(PureWindowsPath("C:/Users/Shano/cloude"))) == \
        "C--Users-Shano-cloude"
    assert re.sub(r"[^A-Za-z0-9]", "-", "/root/work") == "-root-work"
    assert cs.project_memory_dir(tmp_path, tmp_path, {"autoMemoryDirectory": "~/m"}).name == "m"


def test_auto_memory_off_only_on_request_and_back_on(tmp_path):
    home = home_with_manual_setup(tmp_path)
    assert "auto-memory" not in [c.id for c in cs.plan(home, tmp_path)]
    cs.run_setup(home, lambda c, d: c.id == "auto-memory", lambda s: None, cwd=tmp_path,
                 disable_auto_memory=True)
    assert read(home / ".claude" / "settings.json")["autoMemoryEnabled"] is False
    ids = [c.id for c in cs.plan(home, tmp_path)]
    assert "enable-auto-memory" in ids  # setup turned it off, so it offers to undo that
    assert "automemory" in ids  # and sharing, since it comes back on in the same run
    cs.run_setup(home, lambda c, d: c.id == "enable-auto-memory", lambda s: None, cwd=tmp_path)
    assert "autoMemoryEnabled" not in read(home / ".claude" / "settings.json")


def test_context_hook_removed_but_its_group_neighbours_kept(tmp_path):
    home = home_with_manual_setup(tmp_path)
    sync = {"type": "command", "command": "/root/.local/bin/memcairn sync --quiet"}
    mixed = {"hooks": [sync, CTX_HOOK["hooks"][0]]}
    write(home / ".claude" / "settings.json", {"enabledPlugins": {"memcairn@memcairn": True},
                                               "hooks": {"SessionStart": [mixed]}})
    cs.run_setup(home, lambda c, diff: c.id == "manual-hook", lambda s: None, cwd=tmp_path)
    assert read(home / ".claude" / "settings.json")["hooks"]["SessionStart"] == [{"hooks": [sync]}]
    sync_change = next(c for c in cs.plan(home, tmp_path) if c.id == "sync-hook")
    assert list(sync_change.undo["groups"]) == ["SessionEnd"]  # start already syncs


def test_without_plugin_manual_entries_stay(tmp_path):
    home = home_with_manual_setup(tmp_path, plugin=False)
    ids = [c.id for c in cs.plan(home, tmp_path)]
    assert "manual-mcp" not in ids and "manual-hook" not in ids and "automemory" in ids


def test_declined_changes_are_not_written(tmp_path):
    home = home_with_manual_setup(tmp_path)
    before = (home / ".claude" / "settings.json").read_text(encoding="utf-8")
    assert cs.run_setup(home, lambda c, diff: False, lambda s: None, cwd=home) == 0
    assert (home / ".claude" / "settings.json").read_text(encoding="utf-8") == before
    assert cs.load_state(home) == []


def test_plugin_files_are_consistent():
    from pathlib import Path

    root = Path(__file__).parents[1]
    market = json.loads((root / ".claude-plugin" / "marketplace.json").read_text("utf-8"))
    entry = market["plugins"][0]
    plugin = root / entry["source"]
    manifest = json.loads((plugin / ".claude-plugin" / "plugin.json").read_text("utf-8"))
    assert manifest["name"] == entry["name"] == cs.PLUGIN
    assert manifest["userConfig"]["token"]["sensitive"] is True
    hooks = json.loads((plugin / "hooks" / "hooks.json").read_text("utf-8"))
    cmd = hooks["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    assert "user_config" not in cmd and cmd.endswith("|| true")  # shell form, never fails
    for skill in (plugin / "skills").glob("*/SKILL.md"):
        assert "disable-model-invocation: true" in skill.read_text("utf-8")  # zero tokens


def test_instructions_stay_small():
    from memcairn.server import INSTRUCTIONS

    assert len(INSTRUCTIONS) < 1800  # sent every session; MCP limit is 4096


def test_token_add_prints_ready_connect_steps(tmp_path):
    from typer.testing import CliRunner

    from memcairn.cli import app

    db = str(tmp_path / "m.db")
    out = CliRunner().invoke(app, ["token", "add", "laptop", "--db", db,
                                   "--url", "https://m.example.com/mcp"]).output
    token = out.split("(shown only now): ")[1].split()[0]
    assert token.startswith("mcn_")
    assert "/plugin install memcairn@memcairn" in out
    assert "Server URL: https://m.example.com\n" in out  # /mcp stripped for the plugin
    assert f"--config token={token}" in out
    quiet = CliRunner().invoke(app, ["token", "add", "pc", "--db", db, "--quiet"]).output
    assert quiet.strip().startswith("mcn_") and "\n" not in quiet.strip()
