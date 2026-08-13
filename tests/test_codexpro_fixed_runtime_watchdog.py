from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


if os.name != "nt":
    pytest.skip("legacy fixed-runtime watchdog is Windows-only", allow_module_level=True)


ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "bin" / "codexpro_fixed_runtime_watchdog.py"
SPEC = importlib.util.spec_from_file_location("codexpro_fixed_runtime_watchdog_test", SCRIPT)
assert SPEC and SPEC.loader
WATCHDOG = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = WATCHDOG
SPEC.loader.exec_module(WATCHDOG)


def _args(tmp_path: Path) -> argparse.Namespace:
    return argparse.Namespace(
        root="C:\\",
        port=8790,
        hostname="fixed.example.test",
        bootstrap=str(ROOT / "bin" / "codexpro_project_cloudflare_bootstrap.ps1"),
        state_dir=str(tmp_path),
        interval=5,
        repair_timeout=10,
        once=True,
    )


def test_once_repairs_missing_listener_only_when_fixed_tunnel_is_present(tmp_path: Path, monkeypatch) -> None:
    listening = iter([False, True])
    calls: list[tuple[Path, str, int]] = []
    monkeypatch.setattr(WATCHDOG, "fixed_tunnel_present", lambda _hostname, _port: True)
    monkeypatch.setattr(WATCHDOG, "tcp_listening", lambda _port, timeout=1.5: next(listening))
    monkeypatch.setattr(
        WATCHDOG,
        "repair",
        lambda bootstrap, root, timeout: calls.append((bootstrap, root, timeout))
        or subprocess.CompletedProcess([], 0, "{}", ""),
    )

    assert WATCHDOG.run(_args(tmp_path)) == 0
    state = json.loads((tmp_path / "fixed-runtime-watchdog-CDrive.json").read_text(encoding="utf-8"))
    assert state["status"] == "repaired-local-listener"
    assert state["restart_count"] == 1
    assert calls and calls[0][1] == "C:\\"


def test_once_never_repairs_when_exact_fixed_tunnel_is_absent(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(WATCHDOG, "fixed_tunnel_present", lambda _hostname, _port: False)
    monkeypatch.setattr(WATCHDOG, "tcp_listening", lambda _port, timeout=1.5: False)
    monkeypatch.setattr(WATCHDOG, "repair", lambda *_args: (_ for _ in ()).throw(AssertionError("repair called")))

    assert WATCHDOG.run(_args(tmp_path)) == 2
    state = json.loads((tmp_path / "fixed-runtime-watchdog-CDrive.json").read_text(encoding="utf-8"))
    assert state["status"] == "stopped-fixed-tunnel-absent"
    assert state["restart_count"] == 0
