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
    n = cs.run_setup(home, lambda c, diff: shown.append((c.id, diff)) or True, lambda s: None)
    assert [i for i, _ in shown] == ["client", "manual-mcp", "manual-hook", "auto-memory",
                                     "sync-hook"] and n == 5
    assert all(TOKEN not in diff for _, diff in shown)  # diffs never show the token
    claude, settings = read(home / ".claude.json"), read(home / ".claude" / "settings.json")
    assert "memory" not in claude["mcpServers"] and claude["projects"] == {"/p": {}}
    assert settings["autoMemoryEnabled"] is False and settings["theme"] == "dark"
    groups = settings["hooks"]["SessionStart"]
    assert CTX_HOOK not in groups and OTHER_HOOK in groups
    assert any("sync --quiet" in h["command"] for g in groups for h in g["hooks"])
    assert client.auth(home=home) == ("https://m.example.com", TOKEN)  # from client.toml
    assert list(home.glob(".claude.json.pre-memcairn-*"))
    assert cs.plan(home) == []  # idempotent

    cs.run_unsetup(home, lambda q: True, lambda s: None)
    assert read(home / ".claude.json") == claude0
    restored = read(home / ".claude" / "settings.json")
    assert restored["theme"] == "dark" and "autoMemoryEnabled" not in restored
    assert sorted(map(json.dumps, restored["hooks"]["SessionStart"])) == sorted(
        map(json.dumps, settings0["hooks"]["SessionStart"]))
    assert not (home / ".memcairn" / "client.toml").exists()


def test_without_plugin_manual_entries_stay(tmp_path):
    home = home_with_manual_setup(tmp_path, plugin=False)
    ids = [c.id for c in cs.plan(home)]
    assert "manual-mcp" not in ids and "manual-hook" not in ids and "auto-memory" in ids


def test_declined_changes_are_not_written(tmp_path):
    home = home_with_manual_setup(tmp_path)
    before = (home / ".claude" / "settings.json").read_text(encoding="utf-8")
    assert cs.run_setup(home, lambda c, diff: False, lambda s: None) == 0
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

    assert len(INSTRUCTIONS) < 1500  # sent every session; MCP limit is 4096
