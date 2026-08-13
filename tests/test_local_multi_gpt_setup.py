import importlib.util
import hashlib
import json
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "bin" / "codex_local_multi_gpt_setup.py"


def load_module():
    spec = importlib.util.spec_from_file_location("codex_local_multi_gpt_setup_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_resolver_skips_cli_that_cannot_parse_active_config(tmp_path, monkeypatch) -> None:
    module = load_module()
    old = tmp_path / "old-codex"
    current = tmp_path / "current-codex"
    old.write_text("old", encoding="utf-8")
    current.write_text("current", encoding="utf-8")
    monkeypatch.setattr(module, "_candidate_codex_commands", lambda: [old, current])

    def fake_run(argv, *, codex_home):
        command = Path(argv[0])
        if argv[1:] == ["--version"]:
            return subprocess.CompletedProcess(argv, 0, stdout="codex-cli 1\n", stderr="")
        return subprocess.CompletedProcess(argv, 1 if command == old else 0, stdout="[]", stderr="bad config" if command == old else "")

    monkeypatch.setattr(module, "_run", fake_run)
    selected, version = module.resolve_codex_cli(tmp_path)
    assert selected == current
    assert version == "codex-cli 1"


def test_exact_registration_requires_server_and_cli_paths(tmp_path) -> None:
    module = load_module()
    server = (tmp_path / "server.mjs").resolve()
    cli = (tmp_path / "codex.exe").resolve()
    value = {
        "enabled": True,
        "transport": {
            "type": "stdio",
            "command": "node",
            "args": [str(server)],
            "env": {"MULTI_GPT_CODEX_CLI_PATH": str(cli)},
        },
    }
    assert module._matches(value, server, cli) is True
    value["transport"]["args"] = [str(tmp_path / "other.mjs")]
    assert module._matches(value, server, cli) is False


def test_manifest_and_portable_lifecycle_default_to_no_local_multi_gpt() -> None:
    manifest = json.loads((ROOT / "install-manifest.json").read_text(encoding="utf-8"))
    optional = manifest["optional_components"]["local_multi_gpt"]
    assert optional["prompt"] == "Local Multi-GPT도 설치할까요? [y/N]"
    assert optional["default_install"] is False
    lifecycle = (ROOT / "bin" / "codexpro_lifecycle.py").read_text(encoding="utf-8")
    assert "--enable-local-multi-gpt" in lifecycle
    assert "sys.stdin.isatty()" in lifecycle


def test_registration_rollback_supports_read_only_preflight_and_exact_restore(tmp_path) -> None:
    module = load_module()
    codex_home = tmp_path / ".codex"
    config = codex_home / "config.toml"
    backup = codex_home / "backups" / "local-multi-gpt-registration" / "case" / "config.toml"
    receipt = codex_home / "receipts" / "local-multi-gpt-registration-case.json"
    config.parent.mkdir(parents=True)
    backup.parent.mkdir(parents=True)
    receipt.parent.mkdir(parents=True)
    before = b'model = "before"\n'
    after = b'model = "after"\n[mcp_servers.multi_gpt]\ncommand = "node"\n'
    backup.write_bytes(before)
    config.write_bytes(after)
    receipt.write_text(json.dumps({
        "schema": module.RECEIPT_SCHEMA,
        "config": str(config),
        "config_existed": True,
        "before_sha256": hashlib.sha256(before).hexdigest(),
        "after_sha256": hashlib.sha256(after).hexdigest(),
        "backup": str(backup),
    }), encoding="utf-8")

    ready = module.rollback(codex_home, receipt, dry_run=True)
    assert ready["status"] == "READY"
    assert config.read_bytes() == after
    restored = module.rollback(codex_home, receipt)
    assert restored["status"] == "COMPLETE"
    assert config.read_bytes() == before
