from __future__ import annotations

import importlib.util
import json
import os
import plistlib
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "bin" / "codexpro_cloudflared_launchd.py"


def load_module():
    assert MODULE_PATH.is_file()
    spec = importlib.util.spec_from_file_location("codexpro_cloudflared_launchd_test", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_cloudflared_artifacts_are_isolated_and_route_only_devspace(tmp_path: Path) -> None:
    module = load_module()
    project_root = tmp_path / "project"
    project_root.mkdir()
    credentials = tmp_path / "credentials.json"
    credentials.touch()
    config = tmp_path / "config.yml"

    spec = module.TunnelSpec.parse(
        hostname="devspace.example.com",
        tunnel_id="44ba10ff-1b67-47eb-a10c-9ec085647d98",
        credentials_file=credentials,
    )
    config_text = module.render_config(spec)
    runtime = module.ServiceRuntime(
        project_root=project_root.resolve(),
        cloudflared="/opt/homebrew/bin/cloudflared",
        config=config,
        logs=tmp_path / "logs",
    )
    plist = module.service_plist(runtime=runtime, tunnel_id=spec.tunnel_id)

    assert module.LABEL == "com.ventianima.codexpro-automation.cloudflared-devspace"
    assert "hostname: devspace.example.com" in config_text
    assert "service: http://127.0.0.1:7676" in config_text
    assert config_text.rstrip().endswith("- service: http_status:404")
    assert plist["CodexProManaged"] is True
    assert plist["ProgramArguments"] == [
        "/opt/homebrew/bin/cloudflared",
        "tunnel",
        "--no-autoupdate",
        "--config",
        str(config),
        "run",
        str(spec.tunnel_id),
    ]
    assert plistlib.loads(plistlib.dumps(plist))["Label"] == module.LABEL


def test_install_writes_private_config_and_managed_launchagent(tmp_path: Path) -> None:
    module = load_module()
    codex_home = tmp_path / "codex"
    launch_agents = tmp_path / "LaunchAgents"
    project_root = tmp_path / "project"
    project_root.mkdir()
    credentials = tmp_path / "credentials.json"
    credentials.touch()
    cloudflared = tmp_path / "cloudflared"
    cloudflared.touch(mode=0o755)
    spec = module.TunnelSpec.parse(
        hostname="devspace.example.com",
        tunnel_id="44ba10ff-1b67-47eb-a10c-9ec085647d98",
        credentials_file=credentials,
    )

    paths = module.InstallPaths(
        codex_home=codex_home,
        launch_agents=launch_agents,
        project_root=project_root,
        cloudflared=cloudflared,
    )
    result = module.install_service(paths=paths, spec=spec, load=False)

    config = Path(result["config"])
    plist_path = Path(result["plist"])
    assert config.parent == codex_home / "state" / "codexpro-cloudflare"
    if os.name == "posix":
        assert config.stat().st_mode & 0o777 == 0o600
    assert plistlib.loads(plist_path.read_bytes())["CodexProManaged"] is True
    assert result["loaded"] is False


def test_doctor_accepts_installed_managed_artifacts(tmp_path: Path, monkeypatch) -> None:
    module = load_module()
    codex_home = tmp_path / "codex"
    launch_agents = tmp_path / "LaunchAgents"
    project_root = tmp_path / "project"
    project_root.mkdir()
    credentials = tmp_path / "credentials.json"
    credentials.touch()
    cloudflared = tmp_path / "cloudflared"
    cloudflared.touch(mode=0o755)
    spec = module.TunnelSpec.parse(
        hostname="devspace.example.com",
        tunnel_id="44ba10ff-1b67-47eb-a10c-9ec085647d98",
        credentials_file=credentials,
    )
    paths = module.InstallPaths(codex_home, launch_agents, project_root, cloudflared)
    module.install_service(paths=paths, spec=spec, load=False)
    monkeypatch.setattr(module.sys, "platform", "darwin")
    monkeypatch.setattr(module, "_domain", lambda: "gui/501")
    monkeypatch.setattr(
        module,
        "_launchctl",
        lambda *args: subprocess.CompletedProcess(args, 0, "loaded", ""),
    )

    result = module.doctor_service(codex_home=codex_home, launch_agents=launch_agents)

    assert result["ok"] is True
    assert result["managed"] is True
    assert result["installed"] is True


def test_cli_installs_artifacts_without_loading_launchd(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex"
    launch_agents = tmp_path / "LaunchAgents"
    project_root = tmp_path / "project"
    project_root.mkdir()
    credentials = tmp_path / "credentials.json"
    credentials.touch()
    cloudflared = tmp_path / "cloudflared"
    cloudflared.touch(mode=0o755)

    completed = subprocess.run(
        [
            sys.executable,
            str(MODULE_PATH),
            "--codex-home",
            str(codex_home),
            "--launch-agents",
            str(launch_agents),
            "install",
            "--project-root",
            str(project_root),
            "--cloudflared",
            str(cloudflared),
            "--hostname",
            "devspace.example.com",
            "--tunnel-id",
            "44ba10ff-1b67-47eb-a10c-9ec085647d98",
            "--credentials-file",
            str(credentials),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["label"] == "com.ventianima.codexpro-automation.cloudflared-devspace"


def test_load_rejects_non_macos_before_writing(tmp_path: Path, monkeypatch) -> None:
    module = load_module()
    monkeypatch.setattr(module.sys, "platform", "win32")
    codex_home = tmp_path / "codex"
    launch_agents = tmp_path / "LaunchAgents"
    project_root = tmp_path / "project"
    project_root.mkdir()
    credentials = tmp_path / "credentials.json"
    credentials.touch()
    cloudflared = tmp_path / "cloudflared"
    cloudflared.touch()
    spec = module.TunnelSpec.parse(
        hostname="devspace.example.com",
        tunnel_id="44ba10ff-1b67-47eb-a10c-9ec085647d98",
        credentials_file=credentials,
    )
    paths = module.InstallPaths(codex_home, launch_agents, project_root, cloudflared)

    with pytest.raises(module.TunnelConfigError, match="launchd requires macOS"):
        module.install_service(paths=paths, spec=spec, load=True)

    assert not codex_home.exists()
    assert not launch_agents.exists()


def test_bootstrap_failure_restores_previous_managed_artifacts(tmp_path: Path, monkeypatch) -> None:
    module = load_module()
    monkeypatch.setattr(module.sys, "platform", "darwin")
    monkeypatch.setattr(module, "_domain", lambda: "gui/501")
    codex_home = tmp_path / "codex"
    launch_agents = tmp_path / "LaunchAgents"
    project_root = tmp_path / "project"
    project_root.mkdir()
    credentials = tmp_path / "credentials.json"
    credentials.touch()
    cloudflared = tmp_path / "cloudflared"
    cloudflared.touch()
    paths = module.InstallPaths(codex_home, launch_agents, project_root, cloudflared)
    old_spec = module.TunnelSpec.parse(
        hostname="old.example.com",
        tunnel_id="44ba10ff-1b67-47eb-a10c-9ec085647d98",
        credentials_file=credentials,
    )
    installed = module.install_service(paths=paths, spec=old_spec, load=False)
    config = Path(installed["config"])
    plist_path = Path(installed["plist"])
    old_config = config.read_bytes()
    old_plist = plist_path.read_bytes()
    calls: list[tuple[str, ...]] = []

    def launchctl(*args: str):
        calls.append(args)
        code = 1 if args[0] == "bootstrap" and len(calls) == 2 else 0
        return subprocess.CompletedProcess(args, code, "", "new bootstrap failed" if code else "")

    monkeypatch.setattr(module, "_launchctl", launchctl)
    new_spec = module.TunnelSpec.parse(
        hostname="new.example.com",
        tunnel_id="d59aaead-2c8a-4ab7-94d7-3a754989b3c7",
        credentials_file=credentials,
    )
    with pytest.raises(module.TunnelConfigError, match="new bootstrap failed"):
        module.install_service(paths=paths, spec=new_spec, load=True)

    assert config.read_bytes() == old_config
    assert plist_path.read_bytes() == old_plist
    assert [call[0] for call in calls] == ["bootout", "bootstrap", "bootstrap"]


def test_doctor_requires_loaded_service_on_macos(tmp_path: Path, monkeypatch) -> None:
    module = load_module()
    codex_home = tmp_path / "codex"
    launch_agents = tmp_path / "LaunchAgents"
    project_root = tmp_path / "project"
    project_root.mkdir()
    credentials = tmp_path / "credentials.json"
    credentials.touch()
    cloudflared = tmp_path / "cloudflared"
    cloudflared.touch()
    spec = module.TunnelSpec.parse(
        hostname="devspace.example.com",
        tunnel_id="44ba10ff-1b67-47eb-a10c-9ec085647d98",
        credentials_file=credentials,
    )
    module.install_service(
        paths=module.InstallPaths(codex_home, launch_agents, project_root, cloudflared),
        spec=spec,
        load=False,
    )
    monkeypatch.setattr(module.sys, "platform", "darwin")
    monkeypatch.setattr(module, "_domain", lambda: "gui/501")
    monkeypatch.setattr(
        module,
        "_launchctl",
        lambda *args: subprocess.CompletedProcess(args, 1, "", "not loaded"),
    )

    result = module.doctor_service(codex_home=codex_home, launch_agents=launch_agents)

    assert result["installed"] is True
    assert result["managed"] is True
    assert result["loaded"] is False
    assert result["ok"] is False


def test_uninstall_removes_only_managed_artifacts(tmp_path: Path) -> None:
    module = load_module()
    codex_home = tmp_path / "codex"
    launch_agents = tmp_path / "LaunchAgents"
    project_root = tmp_path / "project"
    project_root.mkdir()
    credentials = tmp_path / "credentials.json"
    credentials.touch()
    cloudflared = tmp_path / "cloudflared"
    cloudflared.touch()
    spec = module.TunnelSpec.parse(
        hostname="devspace.example.com",
        tunnel_id="44ba10ff-1b67-47eb-a10c-9ec085647d98",
        credentials_file=credentials,
    )
    installed = module.install_service(
        paths=module.InstallPaths(codex_home, launch_agents, project_root, cloudflared),
        spec=spec,
        load=False,
    )

    result = module.uninstall_service(
        codex_home=codex_home,
        launch_agents=launch_agents,
        unload=False,
    )

    assert result["ok"] is True
    assert sorted(result["removed"]) == sorted([installed["config"], installed["plist"]])
    assert credentials.exists()


def test_uninstall_preserves_unmanaged_conflict(tmp_path: Path) -> None:
    module = load_module()
    codex_home = tmp_path / "codex"
    launch_agents = tmp_path / "LaunchAgents"
    plist_path = launch_agents / f"{module.LABEL}.plist"
    plist_path.parent.mkdir(parents=True)
    plist_path.write_bytes(plistlib.dumps({"Label": module.LABEL, "CodexProManaged": False}))

    result = module.uninstall_service(
        codex_home=codex_home,
        launch_agents=launch_agents,
        unload=False,
    )

    assert result["ok"] is False
    assert result["conflicts"] == [str(plist_path.resolve())]
    assert plist_path.exists()


def test_install_rejects_orphan_config_without_overwriting(tmp_path: Path) -> None:
    module = load_module()
    codex_home = tmp_path / "codex"
    launch_agents = tmp_path / "LaunchAgents"
    project_root = tmp_path / "project"
    project_root.mkdir()
    credentials = tmp_path / "credentials.json"
    credentials.touch()
    cloudflared = tmp_path / "cloudflared"
    cloudflared.touch()
    config = codex_home / "state" / "codexpro-cloudflare" / "config.yml"
    config.parent.mkdir(parents=True)
    config.write_text("user-owned\n", encoding="utf-8")
    spec = module.TunnelSpec.parse(
        hostname="devspace.example.com",
        tunnel_id="44ba10ff-1b67-47eb-a10c-9ec085647d98",
        credentials_file=credentials,
    )

    with pytest.raises(module.TunnelConfigError, match="no managed LaunchAgent owner"):
        module.install_service(
            paths=module.InstallPaths(codex_home, launch_agents, project_root, cloudflared),
            spec=spec,
            load=False,
        )

    assert config.read_text(encoding="utf-8") == "user-owned\n"


def test_uninstall_preserves_artifacts_when_loaded_service_cannot_stop(tmp_path: Path, monkeypatch) -> None:
    module = load_module()
    codex_home = tmp_path / "codex"
    launch_agents = tmp_path / "LaunchAgents"
    project_root = tmp_path / "project"
    project_root.mkdir()
    credentials = tmp_path / "credentials.json"
    credentials.touch()
    cloudflared = tmp_path / "cloudflared"
    cloudflared.touch()
    spec = module.TunnelSpec.parse(
        hostname="devspace.example.com",
        tunnel_id="44ba10ff-1b67-47eb-a10c-9ec085647d98",
        credentials_file=credentials,
    )
    installed = module.install_service(
        paths=module.InstallPaths(codex_home, launch_agents, project_root, cloudflared),
        spec=spec,
        load=False,
    )
    monkeypatch.setattr(module.sys, "platform", "darwin")
    monkeypatch.setattr(module, "_domain", lambda: "gui/501")

    def launchctl(*args: str):
        code = 0 if args[0] == "print" else 1
        return subprocess.CompletedProcess(args, code, "", "permission denied" if code else "")

    monkeypatch.setattr(module, "_launchctl", launchctl)
    result = module.uninstall_service(
        codex_home=codex_home,
        launch_agents=launch_agents,
        unload=True,
    )

    assert result["ok"] is False
    assert Path(installed["config"]).exists()
    assert Path(installed["plist"]).exists()
