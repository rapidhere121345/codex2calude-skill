from __future__ import annotations

import json
from pathlib import Path
import stat
import sys
import textwrap
import tomllib

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/remote-research/scripts"
sys.path.insert(0, str(SCRIPTS))
import setup_client as setup
from test_remote_research_client import fake_environment


def make_legacy(key, pin, provider="claude"):
    user = provider + "mcp"
    cleaned = ["-T", "-p", setup.PORT, "-i", str(key)]
    for opt in ("BatchMode=yes", "IdentitiesOnly=yes", "StrictHostKeyChecking=yes",
                f"UserKnownHostsFile={pin}", "ConnectTimeout=10",
                "ServerAliveInterval=30", "ServerAliveCountMax=2"):
        cleaned += ["-o", opt]
    cleaned += [f"{user}@{setup.HOST}"]
    return {
        "command": "ssh", "args": cleaned, "startup_timeout_sec": 20,
        "tool_timeout_sec": 900,
        "enabled_tools": [f"{provider}_{name}" for name in (
            "status", "once", "start", "list_conversations",
            "export_conversation", "continue", "close")],
    }


def options(home, key, pin, client="codex", *more):
    return ["--client", client, "--" + client + "-home", str(home),
            "--key-path", str(key), "--known-hosts-path", str(pin), *more]


@pytest.mark.parametrize("kind", ["codex", "devin"])
def test_first_install_idempotent_and_unrelated_config(tmp_path, monkeypatch, kind, capsys):
    conf, env, _ = fake_environment(tmp_path, monkeypatch)
    home = tmp_path / kind
    home.mkdir(mode=0o700)
    target = home / ("config.toml" if kind == "codex" else "mcp_config.json")
    original = b'[unrelated]\nvalue = "keep"\n' if kind == "codex" else (
        b'{"mcpServers":{"other":{"command":"example"}},"option":"keep"}\n')
    target.write_bytes(original)
    key, pin = conf.parent / "test-key", conf.parent / "pin"
    argv = options(home, key, pin, kind)
    assert setup.main(argv) == 0
    after = target.read_bytes()
    assert setup.main(argv) == 0
    assert target.read_bytes() == after
    doc = tomllib.loads(after.decode()) if kind == "codex" else json.loads(after)
    servers = doc["mcp_servers"] if kind == "codex" else doc["mcpServers"]
    assert "remote_research" in servers
    if kind == "codex":
        assert servers["remote_research"]["tool_timeout_sec"] == 3780
    assert len(servers) == 1 if kind == "codex" else "other" in servers
    assert original in [x.read_bytes() for x in home.glob("*.pre-remote-*.bak")]
    assert not (target.stat().st_mode & stat.S_IROTH)
    assert "NOT A REAL KEY" not in capsys.readouterr().out


@pytest.mark.parametrize("kind", ["codex", "devin"])
def test_legacy_reuse_and_verify_disables_only_after_success(
    tmp_path, monkeypatch, kind,
):
    conf, env, log = fake_environment(tmp_path, monkeypatch)
    home = tmp_path / kind
    home.mkdir(mode=0o700)
    key, pin = conf.parent / "test-key", conf.parent / "pin"
    entry = make_legacy(key, pin)
    assert setup.locate_identity(entry, "claudemcp") == (key, pin)
    target = home / ("config.toml" if kind == "codex" else "mcp_config.json")
    if kind == "codex":
        target.write_text(setup.toml_block("claude_research", entry) +
                          setup.toml_block("other", {"command": "elsewhere"}))
    else:
        target.write_text(json.dumps({"mcpServers": {
            "claude_research": entry, "other": {"command": "elsewhere"}}}))
    common = ["--client", kind, "--" + kind + "-home", str(home)]
    assert setup.main(common) == 0
    before_verify = target.read_bytes()
    doc = tomllib.loads(before_verify.decode()) if kind == "codex" else json.loads(before_verify)
    servers = doc["mcp_servers"] if kind == "codex" else doc["mcpServers"]
    assert servers["claude_research"].get("enabled", True)
    # Verification uses a fake SSH executable, never a live provider.
    monkeypatch.setenv("PATH", env["PATH"])
    monkeypatch.setenv("FAKE_SSH_LOG", str(log))
    assert setup.main([*common, "--verify"]) == 0
    doc = (tomllib.loads(target.read_text()) if kind == "codex"
           else json.loads(target.read_text()))
    servers = doc["mcp_servers"] if kind == "codex" else doc["mcpServers"]
    assert servers["claude_research"]["enabled" if kind == "codex" else "disabled"] is (
        False if kind == "codex" else True
    )
    assert servers["other"]["command"] == "elsewhere"
    assert before_verify in [x.read_bytes() for x in home.glob("*.pre-remote-*.bak")]


def test_verify_failure_preserves_legacy_and_unified(tmp_path, monkeypatch):
    conf, env, log = fake_environment(tmp_path, monkeypatch)
    home = tmp_path / "codex"
    home.mkdir(mode=0o700)
    key, pin = conf.parent / "test-key", conf.parent / "pin"
    target = home / "config.toml"
    target.write_text(setup.toml_block("claude_research", make_legacy(key, pin)))
    original = target.read_bytes()
    monkeypatch.setenv("PATH", env["PATH"])
    monkeypatch.setenv("FAKE_SSH_LOG", str(log))
    monkeypatch.setenv("FAKE_PRINCIPAL", "owner")
    assert setup.main(options(home, key, pin, "codex", "--verify")) == 1
    assert target.read_bytes() == original


def test_conflicts_and_symlink_fail_closed(tmp_path, monkeypatch):
    conf, _, _ = fake_environment(tmp_path, monkeypatch)
    home = tmp_path / "codex"
    home.mkdir(mode=0o700)
    key, pin = conf.parent / "test-key", conf.parent / "pin"
    target = home / "config.toml"
    target.write_text(setup.toml_block("claude_research", {"command": "custom"}))
    before = target.read_bytes()
    assert setup.main(options(home, key, pin)) == 1
    assert target.read_bytes() == before
    target.unlink()
    target.write_text(setup.toml_block("remote_research", {"command": "custom"}))
    assert setup.main(options(home, key, pin)) == 1
    target.unlink()
    target.symlink_to(conf)
    assert setup.main(options(home, key, pin)) == 1
    target.unlink()
    pin.unlink()
    pin.symlink_to(key)
    assert setup.main(options(home, key, pin)) == 1


def test_prefers_claude_key_when_both_legacy_recognized(tmp_path, monkeypatch):
    conf, env, log = fake_environment(tmp_path, monkeypatch)
    home = tmp_path / "codex"
    home.mkdir(mode=0o700)
    key, pin = conf.parent / "test-key", conf.parent / "pin"
    other = conf.parent / "gemini-key"
    other.write_bytes(b"NOT A REAL KEY")
    other.chmod(0o600)
    Path(f"{other}.pub").write_text("ssh-ed25519 test")
    target = home / "config.toml"
    target.write_text(setup.toml_block("claude_research", make_legacy(key, pin)) +
                      setup.toml_block("gemini_research", make_legacy(other, pin, "gemini")))
    assert setup.main(["--codex-home", str(home)]) == 0
    private = json.loads((home / "remote-research/client.json").read_text())
    assert private["ssh_command"][private["ssh_command"].index("-i") + 1] == str(key)
    data = tomllib.loads(target.read_text())["mcp_servers"]
    assert "enabled" not in data["claude_research"]
    assert "enabled" not in data["gemini_research"]
    monkeypatch.setenv("PATH", env["PATH"])
    monkeypatch.setenv("FAKE_SSH_LOG", str(log))
    assert setup.main(["--codex-home", str(home), "--verify"]) == 0
    data = tomllib.loads(target.read_text())["mcp_servers"]
    assert data["claude_research"]["enabled"] is False
    assert data["gemini_research"]["enabled"] is False


def test_key_without_public_file_and_flexible_enabled_field(tmp_path, monkeypatch):
    conf, env, log = fake_environment(tmp_path, monkeypatch)
    key, pin = conf.parent / "test-key", conf.parent / "pin"
    Path(f"{key}.pub").unlink()  # Not required, and the private key is never read.
    home = tmp_path / "codex"
    home.mkdir(mode=0o700)
    target = home / "config.toml"
    target.write_text(setup.toml_block("claude_research", make_legacy(key, pin)) +
                      "  enabled=true  # keep this comment\n")
    monkeypatch.setenv("PATH", env["PATH"])
    monkeypatch.setenv("FAKE_SSH_LOG", str(log))
    assert setup.main(options(home, key, pin, "codex", "--verify")) == 0
    assert tomllib.loads(target.read_text())["mcp_servers"]["claude_research"]["enabled"] is False
    assert "enabled = false  # keep this comment" in target.read_text()


def test_quoted_legacy_table_cannot_be_rewritten(tmp_path, monkeypatch):
    conf, env, log = fake_environment(tmp_path, monkeypatch)
    key, pin = conf.parent / "test-key", conf.parent / "pin"
    home = tmp_path / "codex"
    home.mkdir(mode=0o700)
    target = home / "config.toml"
    original = setup.toml_block("claude_research", make_legacy(key, pin)).replace(
        "[mcp_servers.claude_research]", '[mcp_servers."claude_research"]',
    )
    target.write_text(original)
    monkeypatch.setenv("PATH", env["PATH"])
    monkeypatch.setenv("FAKE_SSH_LOG", str(log))
    assert setup.main(options(home, key, pin, "codex", "--verify")) == 1
    assert target.read_text() == original


def test_verify_sequential_handshake_ignores_notifications_not_bad_ids(
    tmp_path, monkeypatch,
):
    facade = tmp_path / "fake_facade.py"
    facade.write_text(textwrap.dedent("""\
        import json, sys
        initialized = False
        for line in sys.stdin:
            request = json.loads(line)
            method = request["method"]
            if method == "initialize":
                print(json.dumps({"jsonrpc":"2.0","method":"notifications/message",
                                  "params":{"level":"info"}}), flush=True)
                print(json.dumps({"jsonrpc":"2.0","id":request["id"],"result":{
                    "protocolVersion":"2024-11-05"}}), flush=True)
            elif method == "notifications/initialized":
                initialized = True
            elif method == "tools/list":
                if not initialized:
                    sys.exit(2)
                print(json.dumps({"jsonrpc":"2.0","id":request["id"],"result":{
                    "tools":[{"name":n} for n in """ + repr(list(setup.TOOLS)) + """]}}),
                    flush=True)
            elif method == "tools/call":
                print(json.dumps({"jsonrpc":"2.0","id":request["id"],"result":{
                    "structuredContent":{"principal":"partner","providers":{}}}}),
                    flush=True)
    """))
    monkeypatch.setattr(setup, "CLIENT", facade)
    setup.verify(tmp_path / "unused.json", "partner")
    duplicate = facade.read_text().replace(
        'elif method == "tools/call":',
        'elif method == "tools/call":\n'
        '        print(json.dumps({"jsonrpc":"2.0","id":2,"result":{}}), flush=True)',
    )
    facade.write_text(duplicate)
    with pytest.raises(setup.SetupError, match="duplicate"):
        setup.verify(tmp_path / "unused.json", "partner")
