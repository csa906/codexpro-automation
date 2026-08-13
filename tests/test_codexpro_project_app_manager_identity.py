from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest


if os.name != "nt":
    pytest.skip("legacy drive-scoped CodexPro app manager is Windows-only", allow_module_level=True)

BIN = Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(BIN))

import codexpro_project_app_manager as manager  # noqa: E402


def write_registry(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def test_workspace_path_resolves_to_one_drive_scoped_app() -> None:
    assert manager.drive_app_root(Path(r"C:\Users\TestUser\repo")) == Path("C:\\").resolve()


def test_fixed_cdrive_policy_rejects_dynamic_endpoint_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry_path = tmp_path / "registry.json"
    monkeypatch.setattr(manager, "REGISTRY_PATH", registry_path)
    write_registry(
        registry_path,
        {
            "schema_version": 2,
            "projects": {
                "C:\\": {
                    "slug": "CDrive",
                    "app_name": "CodexPro-CDrive-v14",
                    "version": 14,
                    "port": 8790,
                    "public_url": "https://fixed.example.test/mcp?codexpro_token=fixed",
                    "status": "active",
                }
            },
            "retired_apps": [],
        },
    )
    (tmp_path / "drive-tunnel-policy.json").write_text(
        json.dumps(
            {
                "default_provider": "cloudflare",
                "drives": {"C:\\": {"provider": "ngrok", "hostname": "fixed.example.test"}},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="DRIVE_TUNNEL_POLICY_MISMATCH"):
        manager.decide(
            root=Path("C:\\"),
            public_url="https://dynamic.trycloudflare.com/mcp?codexpro_token=dynamic",
            preferred_port=8790,
            update=False,
        )


def test_update_refuses_same_endpoint_different_token_for_other_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    registry_path = tmp_path / "registry.json"
    monkeypatch.setattr(manager, "REGISTRY_PATH", registry_path)
    write_registry(
        registry_path,
        {
            "schema_version": 1,
            "projects": {
                r"C:\Users\TestUser\.codex": {
                    "slug": "codex",
                    "app_name": "CodexPro-codex-v01",
                    "version": 1,
                    "port": 8790,
                    "public_url": "https://same.trycloudflare.com/mcp?codexpro_token=aaa",
                    "status": "active",
                },
                r"C:\workspace\BB": {
                    "slug": "bb",
                    "app_name": "CodexPro-bb-v01",
                    "version": 1,
                    "port": 8793,
                    "public_url": "https://old.trycloudflare.com/mcp?codexpro_token=old",
                    "status": "active",
                },
            },
            "retired_apps": [],
        },
    )
    with pytest.raises(RuntimeError, match="already registered"):
        manager.decide(
            root=Path(r"C:\workspace\BB"),
            public_url="https://same.trycloudflare.com/mcp?codexpro_token=bbb",
            preferred_port=8793,
            update=True,
        )


def test_preferred_open_port_not_forced_when_replacing_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    registry_path = tmp_path / "registry.json"
    monkeypatch.setattr(manager, "REGISTRY_PATH", registry_path)
    write_registry(
        registry_path,
        {
            "schema_version": 1,
            "projects": {
                r"C:\workspace\BB": {
                    "slug": "bb",
                    "app_name": "CodexPro-bb-v01",
                    "version": 1,
                    "port": 8793,
                    "public_url": "https://old.trycloudflare.com/mcp?codexpro_token=old",
                    "status": "active",
                }
            },
            "retired_apps": [],
        },
    )
    monkeypatch.setattr(manager, "port_open", lambda port: port == 8793)
    decision = manager.decide(
        root=Path(r"C:\workspace\BB"),
        public_url="https://new.trycloudflare.com/mcp?codexpro_token=new",
        preferred_port=8793,
        update=False,
    )
    assert decision.port != 8793


def test_verified_open_port_is_allowed_after_identity_probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    registry_path = tmp_path / "registry.json"
    monkeypatch.setattr(manager, "REGISTRY_PATH", registry_path)
    write_registry(
        registry_path,
        {
            "schema_version": 1,
            "projects": {
                r"C:\workspace\BB": {
                    "slug": "bb",
                    "app_name": "CodexPro-bb-v01",
                    "version": 1,
                    "port": 8793,
                    "public_url": "https://old.trycloudflare.com/mcp?codexpro_token=old",
                    "status": "active",
                }
            },
            "retired_apps": [],
        },
    )
    monkeypatch.setattr(manager, "port_open", lambda port: port == 8794)
    decision = manager.decide(
        root=Path(r"C:\workspace\BB"),
        public_url="https://new.trycloudflare.com/mcp?codexpro_token=new",
        preferred_port=8794,
        update=False,
        verified_open_port=True,
    )
    assert decision.port == 8794


def test_verified_open_port_is_kept_for_new_candidate_after_identity_probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    registry_path = tmp_path / "registry.json"
    monkeypatch.setattr(manager, "REGISTRY_PATH", registry_path)
    write_registry(
        registry_path,
        {"schema_version": 2, "projects": {}, "retired_apps": [], "pending_reconciles": {}},
    )
    monkeypatch.setattr(manager, "port_open", lambda port: port == 8789)

    decision = manager.decide(
        root=Path(r"C:\Users\TestUser\.codex"),
        public_url="https://candidate.example.test/mcp?codexpro_token=[REDACTED_SECRET]",
        preferred_port=8789,
        update=True,
        verified_open_port=True,
    )

    assert decision.action == "create"
    assert decision.port == 8789
    registry = manager.load_registry()
    pending = registry["pending_reconciles"][decision.transaction_id]
    assert pending["candidate"]["port"] == 8789


def test_verified_open_port_repairs_pending_candidate_port(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    registry_path = tmp_path / "registry.json"
    monkeypatch.setattr(manager, "REGISTRY_PATH", registry_path)
    write_registry(
        registry_path,
        {"schema_version": 2, "projects": {}, "retired_apps": [], "pending_reconciles": {}},
    )
    monkeypatch.setattr(manager, "port_open", lambda port: port in {8789, 8791})
    first = manager.decide(
        root=Path(r"C:\Users\TestUser\.codex"),
        public_url="https://candidate.example.test/mcp?codexpro_token=[REDACTED_SECRET]",
        preferred_port=8791,
        update=True,
        verified_open_port=True,
    )

    repaired = manager.decide(
        root=Path(r"C:\Users\TestUser\.codex"),
        public_url=first.public_url,
        preferred_port=8789,
        update=True,
        verified_open_port=True,
    )

    assert repaired.transaction_id == first.transaction_id
    assert repaired.port == 8789
    registry = manager.load_registry()
    pending = registry["pending_reconciles"][first.transaction_id]
    assert pending["candidate"]["port"] == 8789


def test_recovery_candidate_url_rebind_requires_explicit_absence_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry_path = tmp_path / "registry.json"
    monkeypatch.setattr(manager, "REGISTRY_PATH", registry_path)
    root = r"C:\\Users\\TestUser\\.codex"
    write_registry(
        registry_path,
        {
            "schema_version": 2,
            "projects": {
                root: {
                    "slug": "TestUser-codex",
                    "app_name": "CodexPro-TestUser-codex-v01",
                    "version": 1,
                    "port": 8789,
                    "public_url": "https://old.example.test/mcp?codexpro_token=old",
                    "status": "active",
                }
            },
            "retired_apps": [],
            "pending_reconciles": {},
        },
    )
    monkeypatch.setattr(manager, "port_open", lambda port: port == 8789)
    first = manager.decide(
        root=Path(root),
        public_url="https://candidate-old.example.test/mcp?codexpro_token=oldcandidate",
        preferred_port=8789,
        update=True,
        force_recreate=True,
        verified_open_port=True,
    )
    manager.record_reconcile_failure(first.to_dict(), {"state": "app-confirmed-missing"})

    with pytest.raises(RuntimeError, match="different CodexPro candidate"):
        manager.decide(
            root=Path(root),
            public_url="https://candidate-new.example.test/mcp?codexpro_token=newcandidate",
            preferred_port=8789,
            update=True,
            verified_open_port=True,
        )

    rebound = manager.decide(
        root=Path(root),
        public_url="https://candidate-new.example.test/mcp?codexpro_token=newcandidate",
        preferred_port=8789,
        update=True,
        verified_open_port=True,
        rebind_pending_after_app_absence=True,
    )

    assert rebound.transaction_id == first.transaction_id
    assert rebound.app_name == first.app_name
    assert rebound.public_url.endswith("newcandidate")
    pending = manager.load_registry()["pending_reconciles"][first.transaction_id]
    assert pending["phase"] == "prepared"
    assert pending["recovery_rebind"]["reason"] == "candidate-app-absence-confirmed"


def test_force_recreate_keeps_old_runtime_port_reserved_for_candidate_first_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry_path = tmp_path / "registry.json"
    monkeypatch.setattr(manager, "REGISTRY_PATH", registry_path)
    write_registry(
        registry_path,
        {
            "schema_version": 2,
            "projects": {
                r"C:\Users\TestUser\.codex": {
                    "slug": "TestUser-codex",
                    "app_name": "CodexPro-TestUser-codex-v01",
                    "version": 1,
                    "port": 8789,
                    "public_url": "https://old.example.test/mcp?codexpro_token=old",
                    "status": "active",
                }
            },
            "retired_apps": [],
            "pending_reconciles": {},
        },
    )
    monkeypatch.setattr(manager, "port_open", lambda port: port == 8789)

    decision = manager.decide(
        root=Path(r"C:\Users\TestUser\.codex"),
        public_url=None,
        preferred_port=8791,
        update=False,
        force_recreate=True,
    )

    assert decision.action == "force-recreate"
    assert decision.old_app_name == "CodexPro-TestUser-codex-v01"
    assert decision.app_name == "CodexPro-TestUser-codex-v02"
    assert decision.port == 8791
    assert decision.port != 8789


def test_force_recreate_keeps_identity_verified_candidate_port_when_staging_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry_path = tmp_path / "registry.json"
    monkeypatch.setattr(manager, "REGISTRY_PATH", registry_path)
    write_registry(
        registry_path,
        {
            "schema_version": 2,
            "projects": {
                r"C:\Users\TestUser\.codex": {
                    "slug": "TestUser-codex",
                    "app_name": "CodexPro-TestUser-codex-v01",
                    "version": 1,
                    "port": 8789,
                    "public_url": "https://old.example.test/mcp?codexpro_token=old",
                    "status": "active",
                }
            },
            "retired_apps": [],
            "pending_reconciles": {},
        },
    )
    monkeypatch.setattr(manager, "port_open", lambda port: port in {8789, 8791})

    decision = manager.decide(
        root=Path(r"C:\Users\TestUser\.codex"),
        public_url="https://candidate.example.test/mcp?codexpro_token=new",
        preferred_port=8791,
        update=True,
        force_recreate=True,
        verified_open_port=True,
    )

    assert decision.port == 8791
    pending = manager.load_registry()["pending_reconciles"][decision.transaction_id]
    assert pending["candidate"]["port"] == 8791


def test_candidate_confirmation_requires_exact_url_connection_and_full_access() -> None:
    decision = {
        "app_name": "CodexPro-Test-v02",
        "public_url": "https://candidate.example.test/mcp?codexpro_token=[REDACTED_SECRET]",
    }
    confirmed = {
        "ok": True,
        "state": "confirmed-visible",
        "app_name": "CodexPro-Test-v02",
        "connect_confirm": {"ok": True},
        "final_url_check": {"ok": True, "url": decision["public_url"]},
        "final_permission_check": {"ok": True, "value": "full_access"},
    }
    assert manager._result_confirms_candidate(decision, confirmed) is True
    assert manager._result_confirms_candidate(
        decision,
        {**confirmed, "final_url_check": {"ok": True, "url": "https://other.example.test/mcp?codexpro_token=[REDACTED_SECRET]"}},
    ) is False
    assert manager._result_confirms_candidate(
        decision,
        {**confirmed, "connect_confirm": {"ok": False}},
    ) is False
    assert manager._result_confirms_candidate(
        decision,
        {**confirmed, "final_permission_check": {"ok": False}},
    ) is False


def test_different_drive_does_not_inherit_fixed_url_or_old_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    registry_path = tmp_path / "registry.json"
    monkeypatch.setattr(manager, "REGISTRY_PATH", registry_path)
    write_registry(
        registry_path,
        {
            "schema_version": 2,
            "projects": {
                r"C:\\": {
                    "slug": "CDrive",
                    "app_name": "CodexPro-CDrive-v11",
                    "version": 11,
                    "port": 8790,
                    "public_url": "https://fixed.example.test/mcp?codexpro_token=[REDACTED_SECRET]",
                    "status": "active",
                }
            },
            "retired_apps": [],
            "pending_reconciles": {},
        },
    )
    monkeypatch.setattr(manager, "port_open", lambda port: False)

    decision = manager.decide(
        root=Path("D:\\"),
        public_url=None,
        preferred_port=8794,
        update=False,
    )

    assert decision.action == "create"
    assert decision.app_name.startswith("CodexPro-DDrive-")
    assert decision.public_url is None
    assert decision.old_app_name is None
    assert decision.old_public_url is None


def test_different_drive_cannot_claim_fixed_endpoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    registry_path = tmp_path / "registry.json"
    monkeypatch.setattr(manager, "REGISTRY_PATH", registry_path)
    write_registry(
        registry_path,
        {
            "schema_version": 2,
            "projects": {
                r"C:\\": {
                    "slug": "CDrive",
                    "app_name": "CodexPro-CDrive-v11",
                    "version": 11,
                    "port": 8790,
                    "public_url": "https://fixed.example.test/mcp?codexpro_token=[REDACTED_SECRET]",
                    "status": "active",
                }
            },
            "retired_apps": [],
            "pending_reconciles": {},
        },
    )

    with pytest.raises(RuntimeError, match="already registered to another root"):
        manager.decide(
            root=Path("D:\\"),
            public_url="https://fixed.example.test/mcp?codexpro_token=[REDACTED_SECRET]",
            preferred_port=8794,
            update=True,
        )
