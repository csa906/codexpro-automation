from __future__ import annotations

import importlib.util
import hashlib
import json
import os
import sys
from pathlib import Path

import pytest


PATH = Path(__file__).resolve().parents[1] / "bin" / "chatgpt_oracle_dispatch.py"


@pytest.fixture(autouse=True)
def default_workspace_app(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CODEX_CHATGPT_APP_NAME", "DevSpace")
    monkeypatch.setenv(
        "CODEX_ORACLE_STATE_ROOT", str(tmp_path.parent / f"{tmp_path.name}-host-state")
    )


def load():
    spec = importlib.util.spec_from_file_location("oracle_dispatch_test", PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_regular_and_deep_research_compile_to_oracle_without_attachments(tmp_path: Path) -> None:
    module = load()
    mission = tmp_path / "mission.md"
    mission.write_text("work", encoding="utf-8")
    for mode, research, thinking in (
        ("direct", "off", "extra-high"),
        ("edit", "off", "extra-high"),
        ("orchestrator", "off", "heavy"),
        ("deep-research", "deep", "extra-high"),
    ):
        target = tmp_path / f"{mode}.json"
        result = module.compile_manifest(
            mode=mode, project_root=tmp_path, mission_path=mission, output_path=target
        )
        value = json.loads(target.read_text(encoding="utf-8"))
        assert result["contract"]["attachments"] == []
        assert value["app_name"] == "DevSpace"
        assert value["task_outcome_contract"] == "v1"
        assert value["model"] == "gpt-5.6-sol"
        assert value["model_strategy"] == "select"
        assert value["thinking_time"] == thinking
        assert value["research"] == research


def test_regular_high_is_forwarded_as_the_visible_high_tier(tmp_path: Path) -> None:
    module = load()
    mission = tmp_path / "mission.md"
    mission.write_text("work", encoding="utf-8")
    target = tmp_path / "high.json"

    result = module.compile_manifest(
        mode="direct",
        project_root=tmp_path,
        mission_path=mission,
        output_path=target,
        reasoning_level="High",
    )

    value = json.loads(target.read_text(encoding="utf-8"))
    assert result["contract"]["reasoning_level"] == "High"
    assert result["contract"]["thinking_time"] == "extended"
    assert value["thinking_time"] == "extended"


def test_configured_app_name_is_forwarded_to_manifest_and_composer(tmp_path: Path) -> None:
    module = load()
    mission = tmp_path / "mission.md"
    mission.write_text("work", encoding="utf-8")
    target = tmp_path / "custom-app.json"

    result = module.compile_manifest(
        mode="direct",
        project_root=tmp_path,
        mission_path=mission,
        output_path=target,
        app_name="codex",
    )

    value = json.loads(target.read_text(encoding="utf-8"))
    assert value["app_name"] == "codex"
    assert result["contract"]["composer_prompt"].startswith("@codex ")


def test_regular_medium_is_forwarded_as_the_visible_medium_tier(tmp_path: Path) -> None:
    module = load()
    mission = tmp_path / "mission.md"
    mission.write_text("work", encoding="utf-8")
    target = tmp_path / "medium.json"

    result = module.compile_manifest(
        mode="direct",
        project_root=tmp_path,
        mission_path=mission,
        output_path=target,
        reasoning_level="Medium",
    )

    value = json.loads(target.read_text(encoding="utf-8"))
    assert result["contract"]["reasoning_level"] == "Medium"
    assert result["contract"]["thinking_time"] == "standard"
    assert value["thinking_time"] == "standard"


def test_pro_attachment_compiles_attachment_only_oracle_and_manual_never_launches(tmp_path: Path) -> None:
    module = load()
    prompt = tmp_path / "prompt.txt"
    packet = tmp_path / "packet.zip"
    prompt.write_text("instructions", encoding="utf-8")
    packet.write_bytes(b"PK\x03\x04packet")
    pro_target = tmp_path / "pro.json"
    pro = module.compile_manifest(
        mode="pro-attachment",
        project_root=tmp_path,
        mission_path=prompt,
        output_path=pro_target,
        attachment_paths=[prompt, packet],
    )
    value = json.loads(pro_target.read_text(encoding="utf-8"))
    assert pro["contract"]["route"] == "oracle-attachment-only"
    assert pro["contract"]["task_kind"] == "direct"
    assert pro["contract"]["thinking_time"] == "heavy"
    assert value["transport"] == "attachment-only"
    assert value["task_kind"] == "direct"
    assert value["task_outcome_contract"] == "legacy"
    assert value["model"] == "gpt-5.6-sol"
    assert value["thinking_time"] == "heavy"
    assert value["attachments"] == [str(prompt.resolve()), str(packet.resolve())]
    assert "app_name" not in value

    manual_target = tmp_path / "manual.json"
    manual = module.compile_manifest(
        mode="manual", project_root=tmp_path, mission_path=None, output_path=manual_target
    )
    assert manual["oracle_manifest_path"] is None
    assert not manual_target.exists()


def test_pro_alias_uses_regular_devspace_with_power5(tmp_path: Path) -> None:
    module = load()
    mission = tmp_path / "mission.md"
    mission.write_text("read only", encoding="utf-8")
    target = tmp_path / "pro-readonly.json"

    result = module.compile_manifest(
        mode="pro", project_root=tmp_path, mission_path=mission, output_path=target
    )

    value = json.loads(target.read_text(encoding="utf-8"))
    assert result["contract"]["route"] == "oracle-devspace"
    assert value["transport"] == "devspace"
    assert value["task_kind"] == "direct"
    assert value["action_authority"] == "read-only"
    assert value["app_name"] == "DevSpace"
    assert value["model"] == "gpt-5.6-sol"
    assert value["model_strategy"] == "select"
    assert value["thinking_time"] == "heavy"
    assert value["research"] == "off"
    assert value["task_outcome_contract"] == "v1"
    assert "attachments" not in value


def test_pro_cli_dry_run_validates_compiled_manifest_without_submission(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    module = load()
    prompt = tmp_path / "prompt.txt"
    packet = tmp_path / "packet.zip"
    target = tmp_path / "pro-dry-run.json"
    prompt.write_text("instructions", encoding="utf-8")
    packet.write_bytes(b"PK\x03\x04packet")
    monkeypatch.setenv("CODEX_ORACLE_STATE_ROOT", str(tmp_path.parent / "host-state-pro-dry-run"))

    exit_code = module.main([
        "--mode", "pro-attachment",
        "--project-root", str(tmp_path),
        "--mission-path", str(prompt),
        "--attachment", str(packet),
        "--manifest-output", str(target),
        "--dry-run",
    ])

    assert exit_code == 0
    emitted = json.loads(capsys.readouterr().out)
    manifest = json.loads(target.read_text(encoding="utf-8"))
    assert emitted["ok"] is True
    assert emitted["run"]["status"] == "dry-run"
    assert emitted["run"]["transport"] == "attachment-only"
    assert manifest["task_kind"] == "direct"
    assert manifest["task_outcome_contract"] == "legacy"
    assert manifest["model"] == "gpt-5.6-sol"
    assert manifest["thinking_time"] == "heavy"


def test_edit_power5_uses_same_workspace_write_authority_as_other_powers(tmp_path: Path) -> None:
    module = load()
    mission = tmp_path / "mission.md"
    mission.write_text("edit the scoped files", encoding="utf-8")
    target = tmp_path / "edit-power5.json"

    result = module.compile_manifest(
        mode="edit",
        project_root=tmp_path,
        mission_path=mission,
        output_path=target,
        reasoning_level="Pro",
    )

    manifest = json.loads(target.read_text(encoding="utf-8"))
    assert result["contract"]["reasoning_level"] == "Pro"
    assert manifest["thinking_time"] == "heavy"
    assert manifest["transport"] == "devspace"
    assert manifest["action_authority"] == "workspace-write"


def test_generic_attachment_preserves_nonpro_power(tmp_path: Path) -> None:
    module = load()
    mission = tmp_path / "mission.md"
    mission.write_text("answer from evidence", encoding="utf-8")
    target = tmp_path / "attachment-medium.json"

    result = module.compile_manifest(
        mode="attachment",
        project_root=tmp_path,
        mission_path=mission,
        output_path=target,
        attachment_paths=[mission],
        reasoning_level="Medium",
    )

    manifest = json.loads(target.read_text(encoding="utf-8"))
    assert result["contract"]["reasoning_level"] == "Medium"
    assert manifest["transport"] == "attachment-only"
    assert manifest["thinking_time"] == "standard"


def fallback_limits() -> dict[str, int]:
    return {
        "max_evidence_files": 8,
        "max_evidence_file_bytes": 1_048_576,
        "max_evidence_total_bytes": 2_000_000,
        "max_patch_operations": 8,
        "max_patch_file_bytes": 1_000_000,
        "max_patch_total_bytes": 2_000_000,
        "local_gate_timeout_seconds": 30,
    }


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def write_fallback_contract(
    module,
    root: Path,
    mission: Path,
    *,
    reasoning: str,
    authority: str,
    edits: list[dict[str, object]],
    gate: list[str] | None,
) -> Path:
    if gate and gate[0] == sys.executable:
        gate = [
            str(Path(sys.base_prefix) / ("python.exe" if sys.platform == "win32" else "bin/python")),
            *gate[1:],
        ]
    value = {
        "schema": module.FALLBACK.CONTRACT_SCHEMA,
        "project_root": str(root.resolve()),
        "mission_path": str(mission.resolve()),
        "mission_sha256": digest(mission.read_bytes()),
        "action_authority": authority,
        "reasoning_level": reasoning,
        "evidence_allowlist": [],
        "edit_path_allowlist": edits,
        "local_gate_command": gate,
        "limits": fallback_limits(),
    }
    path = root / "fallback-contract.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def install_write_fallback_scenario(
    module, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[dict[str, object], Path, dict[str, int]]:
    mission = tmp_path / "mission.md"
    mission.write_text("create generated.txt", encoding="utf-8")
    content = "generated by fallback\n"
    contract_path = write_fallback_contract(
        module,
        tmp_path,
        mission,
        reasoning="Pro",
        authority="workspace-write",
        edits=[{"path": "generated.txt", "before_sha256": None, "operations": ["add"]}],
        gate=[sys.executable, "-c", "raise SystemExit(0)"],
    )
    manifest_path = tmp_path / "manifest.json"
    compiled = module.compile_manifest(
        mode="edit",
        project_root=tmp_path,
        mission_path=mission,
        output_path=manifest_path,
        reasoning_level="Pro",
    )
    contract = module.FALLBACK.load_contract(contract_path)
    fallback_run_dir = tmp_path.parent / f"{tmp_path.name}-fallback-run"
    fallback_run_dir.mkdir(exist_ok=True)
    output = fallback_run_dir / "output.md"
    patch = {
        "schema": module.FALLBACK.PATCH_SCHEMA,
        "contract_sha256": contract.contract_sha256,
        "mission_sha256": contract.mission_sha256,
        "reasoning_level": "Pro",
        "operations": [
            {
                "op": "add",
                "path": "generated.txt",
                "before_sha256": None,
                "content": content,
                "after_sha256": digest(content.encode("utf-8")),
            }
        ],
    }
    output.write_text(
        f"{module.FALLBACK.PATCH_BEGIN_MARKER}\n{json.dumps(patch)}\n"
        f"{module.FALLBACK.PATCH_END_MARKER}\n",
        encoding="utf-8",
    )
    calls = {"primary": 0, "fallback": 0}
    terminal = {
        "ok": True,
        "status": "complete",
        "run_dir": str(fallback_run_dir),
        "result": {
            "artifacts": {"output": str(output)},
            "power_loss_durability": {
                "run_directory": {
                    "durable": True,
                    "path": str(fallback_run_dir),
                    "created": [{"path": str(fallback_run_dir)}],
                }
            },
        },
        "output_path": str(output),
    }

    def execute(path: Path, **_kwargs):
        manifest = json.loads(Path(path).read_text(encoding="utf-8"))
        if manifest["transport"] == "devspace":
            calls["primary"] += 1
            raise module.RUNNER.OracleRunError(
                "DEVSPACE_EXACT_ROOT_UNAVAILABLE", "exact root unavailable"
            )
        calls["fallback"] += 1
        return terminal

    monkeypatch.setattr(module.RUNNER, "execute_run", execute)
    monkeypatch.setattr(module, "_terminal_run", lambda _run_dir: terminal)
    return compiled, contract_path, calls


def install_completed_episode_with_legacy_baseline(
    module, tmp_path: Path
) -> tuple[Path, dict[str, object]]:
    mission = tmp_path / "mission.md"
    mission.write_text("read only", encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    compiled = module.compile_manifest(
        mode="direct",
        project_root=tmp_path,
        mission_path=mission,
        output_path=manifest_path,
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    contract = module._prepare_fallback_contract(compiled, manifest, None)
    episode = module._create_dispatch_episode(
        compiled=compiled,
        base_manifest_path=manifest_path,
        manifest=manifest,
        contract=contract,
    )
    episode_dir = Path(episode["episode_dir"])
    accepted: dict[str, object] = {
        "ok": True,
        "status": "complete",
        "host_acceptance": {"accepted": True},
    }
    module._save_episode_acceptance(episode_dir, accepted, complete=True)

    journal_path = episode_dir / "journal.json"
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    before_path = Path(journal["workspace_before"]["path"])
    before_sha = module._write_json(before_path, {"schema": "legacy-snapshot/v0"})
    before_reference = {"path": str(before_path), "sha256": before_sha}
    authority_path = Path(journal["authority"]["path"])
    authority = json.loads(authority_path.read_text(encoding="utf-8"))
    authority["workspace_before"] = before_reference
    authority_sha = module._write_json(authority_path, authority)
    journal["workspace_before"] = before_reference
    journal["authority"] = {"path": str(authority_path), "sha256": authority_sha}
    module._write_json(journal_path, journal)
    return episode_dir, accepted


def test_mutating_run_requires_mission_bound_contract(tmp_path: Path) -> None:
    module = load()
    mission = tmp_path / "mission.md"
    mission.write_text("edit one file", encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    compiled = module.compile_manifest(
        mode="edit",
        project_root=tmp_path,
        mission_path=mission,
        output_path=manifest_path,
        reasoning_level="Pro",
    )

    with pytest.raises(module.OracleDispatchError) as exc:
        module.execute_with_automatic_fallback(compiled)

    assert exc.value.code == "MUTATING_EXECUTION_CONTRACT_REQUIRED"


def test_power5_devspace_write_uses_same_authority_and_host_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = load()
    mission = tmp_path / "mission.md"
    target = tmp_path / "result.txt"
    mission.write_text("create result.txt", encoding="utf-8")
    contract_path = write_fallback_contract(
        module,
        tmp_path,
        mission,
        reasoning="Pro",
        authority="workspace-write",
        edits=[{"path": "result.txt", "before_sha256": None, "operations": ["add"]}],
        gate=[sys.executable, "-c", "raise SystemExit(0)"],
    )
    manifest_path = tmp_path / "manifest.json"
    compiled = module.compile_manifest(
        mode="edit",
        project_root=tmp_path,
        mission_path=mission,
        output_path=manifest_path,
        reasoning_level="Pro",
    )
    run_dir = tmp_path.parent / "host-run"
    run_dir.mkdir()

    def execute(_manifest: Path, **_kwargs):
        target.write_text("done\n", encoding="utf-8")
        return {
            "ok": True,
            "status": "complete",
            "run_dir": str(run_dir),
            "result": {"task_outcome": "executed"},
        }

    monkeypatch.setattr(module.RUNNER, "execute_run", execute)
    result = module.execute_with_automatic_fallback(
        compiled, fallback_contract_path=contract_path
    )

    assert result["ok"] is True
    assert result["host_acceptance"]["accepted"] is True
    assert result["host_acceptance"]["operation_proof"]["operations"][0]["op"] == "add"
    assert json.loads(manifest_path.read_text(encoding="utf-8"))["action_authority"] == "workspace-write"


def test_deterministic_devspace_preflight_failure_falls_back_at_same_power(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = load()
    monkeypatch.setenv("CODEX_ORACLE_STATE_ROOT", str(tmp_path.parent / "oracle-state"))
    mission = tmp_path / "mission.md"
    mission.write_text("answer from this mission", encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    compiled = module.compile_manifest(
        mode="direct",
        project_root=tmp_path,
        mission_path=mission,
        output_path=manifest_path,
        reasoning_level="Medium",
    )
    seen: list[dict[str, object]] = []

    def execute(path: Path, **_kwargs):
        manifest = json.loads(Path(path).read_text(encoding="utf-8"))
        seen.append(manifest)
        if manifest["transport"] == "devspace":
            raise module.RUNNER.OracleRunError(
                "DEVSPACE_EXACT_ROOT_UNAVAILABLE", "exact root is not registered"
            )
        run_dir = tmp_path.parent / "fallback-run"
        run_dir.mkdir(exist_ok=True)
        output = run_dir / "output.md"
        output.write_text("fallback answer", encoding="utf-8")
        return {
            "ok": True,
            "run_dir": str(run_dir),
            "result": {"artifacts": {"output": str(output)}},
        }

    monkeypatch.setattr(module.RUNNER, "execute_run", execute)
    result = module.execute_with_automatic_fallback(compiled)

    assert result["ok"] is True
    assert result["route"] == "attachment-fallback"
    assert [item["transport"] for item in seen] == ["devspace", "attachment-only"]
    assert seen[1]["thinking_time"] == "standard"
    assert seen[1]["action_authority"] == "read-only"
    authority = json.loads(Path(result["fallback_authority"]["path"]).read_text(encoding="utf-8"))
    assert authority["consumed"] is True
    assert authority["origin_failure_code"] == "DEVSPACE_EXACT_ROOT_UNAVAILABLE"


def test_workspace_mutation_blocks_attachment_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = load()
    mission = tmp_path / "mission.md"
    mission.write_text("read only", encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    compiled = module.compile_manifest(
        mode="direct", project_root=tmp_path, mission_path=mission, output_path=manifest_path
    )
    calls = 0

    def execute(_path: Path, **_kwargs):
        nonlocal calls
        calls += 1
        (tmp_path / "unexpected.txt").write_text("mutation", encoding="utf-8")
        raise module.RUNNER.OracleRunError(
            "DEVSPACE_EXACT_ROOT_UNAVAILABLE", "exact root is unavailable"
        )

    monkeypatch.setattr(module.RUNNER, "execute_run", execute)
    result = module.execute_with_automatic_fallback(compiled)

    assert result["ok"] is False
    assert result["status"] == "attachment_fallback_blocked_workspace_changed"
    assert calls == 1


def test_write_fallback_applies_strict_patch_and_runs_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = load()
    monkeypatch.setenv("CODEX_ORACLE_STATE_ROOT", str(tmp_path.parent / "oracle-state-write"))
    mission = tmp_path / "mission.md"
    mission.write_text("create generated.txt", encoding="utf-8")
    content = "generated by fallback\n"
    contract_path = write_fallback_contract(
        module,
        tmp_path,
        mission,
        reasoning="Pro",
        authority="workspace-write",
        edits=[{"path": "generated.txt", "before_sha256": None, "operations": ["add"]}],
        gate=[sys.executable, "-c", "raise SystemExit(0)"],
    )
    manifest_path = tmp_path / "manifest.json"
    compiled = module.compile_manifest(
        mode="edit",
        project_root=tmp_path,
        mission_path=mission,
        output_path=manifest_path,
        reasoning_level="Pro",
    )
    contract = module.FALLBACK.load_contract(contract_path)

    def execute(path: Path, **_kwargs):
        manifest = json.loads(Path(path).read_text(encoding="utf-8"))
        if manifest["transport"] == "devspace":
            raise module.RUNNER.OracleRunError(
                "DEVSPACE_EXACT_ROOT_UNAVAILABLE", "exact root unavailable"
            )
        run_dir = tmp_path.parent / "fallback-write-run"
        run_dir.mkdir(exist_ok=True)
        output = run_dir / "output.md"
        patch = {
            "schema": module.FALLBACK.PATCH_SCHEMA,
            "contract_sha256": contract.contract_sha256,
            "mission_sha256": contract.mission_sha256,
            "reasoning_level": "Pro",
            "operations": [{
                "op": "add",
                "path": "generated.txt",
                "before_sha256": None,
                "content": content,
                "after_sha256": digest(content.encode("utf-8")),
            }],
        }
        output.write_text(
            f"{module.FALLBACK.PATCH_BEGIN_MARKER}\n{json.dumps(patch)}\n{module.FALLBACK.PATCH_END_MARKER}\n",
            encoding="utf-8",
        )
        return {
            "ok": True,
            "run_dir": str(run_dir),
            "result": {"artifacts": {"output": str(output)}},
        }

    monkeypatch.setattr(module.RUNNER, "execute_run", execute)
    result = module.execute_with_automatic_fallback(
        compiled, fallback_contract_path=contract_path
    )

    assert result["ok"] is True
    assert result["status"] == "attachment_fallback_applied"
    assert (tmp_path / "generated.txt").read_text(encoding="utf-8") == content
    assert result["apply_receipt"]["gate"]["ok"] is True
    assert not list(tmp_path.glob(".codex-oracle-fallback-*"))
    episode_dir = next(
        (module.RUNNER.STATE.oracle_state_root() / "dispatcher-episodes").iterdir()
    )
    transaction_root = episode_dir / "transactions"
    assert not list(transaction_root.glob(".codex-oracle-fallback-*"))
    assert list(transaction_root.glob("oracle-fallback-finalized-*.evidence"))


def test_completed_legacy_episode_resumes_from_hash_bound_acceptance_without_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = load()
    episode_dir, accepted = install_completed_episode_with_legacy_baseline(module, tmp_path)

    def reject_full_rehydration(_directory: Path) -> dict[str, object]:
        raise AssertionError("completed immutable acceptance must not reopen its baseline")

    monkeypatch.setattr(module, "_load_episode_context", reject_full_rehydration)
    resumed = module._resume_episode(episode_dir)

    assert resumed == {
        **accepted,
        "resumed": True,
        "no_resubmission": True,
    }


@pytest.mark.parametrize("failure", ["missing", "corrupt", "hash_mismatch"])
def test_completed_legacy_episode_acceptance_failures_are_closed(
    tmp_path: Path, failure: str
) -> None:
    module = load()
    episode_dir, _accepted = install_completed_episode_with_legacy_baseline(module, tmp_path)
    journal_path = episode_dir / "journal.json"
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    acceptance_path = episode_dir / "acceptance.json"
    if failure == "missing":
        journal["acceptance"] = None
    elif failure == "corrupt":
        acceptance_path.write_text("{not-json", encoding="utf-8")
        journal["acceptance"]["sha256"] = module.RUNNER.STATE.sha256_file(
            acceptance_path
        )
    else:
        acceptance_path.write_text("{}", encoding="utf-8")
    module._write_json(journal_path, journal)

    with pytest.raises(module.OracleDispatchError) as exc:
        module._resume_episode(episode_dir)

    assert exc.value.code == "EPISODE_ACCEPTANCE_INVALID"


@pytest.mark.parametrize("status", ["acceptance_prepared", "primary_claimed"])
def test_noncomplete_legacy_episode_still_uses_full_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    module = load()
    episode_dir, _accepted = install_completed_episode_with_legacy_baseline(module, tmp_path)
    journal_path = episode_dir / "journal.json"
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    journal["status"] = status
    module._write_json(journal_path, journal)
    checked: list[Path] = []

    def reject_legacy_baseline(directory: Path) -> dict[str, object]:
        checked.append(directory)
        raise module.OracleDispatchError(
            "EPISODE_BASELINE_INVALID", "legacy baseline requires full validation"
        )

    monkeypatch.setattr(module, "_load_episode_context", reject_legacy_baseline)
    with pytest.raises(module.OracleDispatchError) as exc:
        module._resume_episode(episode_dir)

    assert exc.value.code == "EPISODE_BASELINE_INVALID"
    assert checked == [episode_dir]


def test_crash_claim_blocks_duplicate_and_resumes_terminal_host_acceptance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = load()
    mission = tmp_path / "mission.md"
    target = tmp_path / "result.txt"
    mission.write_text("create result.txt", encoding="utf-8")
    contract_path = write_fallback_contract(
        module,
        tmp_path,
        mission,
        reasoning="Pro",
        authority="workspace-write",
        edits=[{"path": "result.txt", "before_sha256": None, "operations": ["add"]}],
        gate=[sys.executable, "-c", "raise SystemExit(0)"],
    )
    manifest_path = tmp_path / "manifest.json"
    compiled = module.compile_manifest(
        mode="edit",
        project_root=tmp_path,
        mission_path=mission,
        output_path=manifest_path,
        reasoning_level="Pro",
    )
    base_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    calls = 0

    def crash(_path: Path, **_kwargs):
        nonlocal calls
        calls += 1
        raise KeyboardInterrupt("simulated process death")

    monkeypatch.setattr(module.RUNNER, "execute_run", crash)
    with pytest.raises(KeyboardInterrupt):
        module.execute_with_automatic_fallback(
            compiled, fallback_contract_path=contract_path
        )

    episode_dir = module._episode_dir(base_manifest["run_id"])
    journal = json.loads((episode_dir / "journal.json").read_text(encoding="utf-8"))
    runtime_manifest = Path(journal["runtime_manifest"]["path"])
    config = module.RUNNER.STATE.load_manifest(runtime_manifest)
    layout = module.RUNNER.STATE.create_layout(config, run_id=base_manifest["run_id"])
    layout.run_dir.mkdir(parents=True)
    target.write_text("done\n", encoding="utf-8")
    layout.output_path.write_text("completed\nTASK_OUTCOME: EXECUTED\n", encoding="utf-8")
    state = module.RUNNER.STATE.state_payload(
        config, layout, status="complete", resolved_version=module.RUNNER.STATE.ORACLE_CUSTOM_PACKAGE_VERSION, exit_code=0
    )
    state.update({
        "session_authority": "terminal",
        "terminal_harvested": True,
        "artifact_sha256": module.RUNNER.STATE.sha256_file(layout.output_path),
        "transport_status": "complete",
        "task_outcome": "executed",
        "task_outcome_reason": "explicit-output-marker",
    })
    module.RUNNER.STATE.write_json_atomic(layout.state_path, state)

    resumed = module.execute_with_automatic_fallback(
        compiled, fallback_contract_path=contract_path
    )

    assert calls == 1
    assert resumed["ok"] is True
    assert resumed["resumed"] is True
    assert resumed["no_resubmission"] is True
    assert resumed["host_acceptance"]["accepted"] is True


def test_crash_claim_without_run_state_never_submits_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = load()
    mission = tmp_path / "mission.md"
    mission.write_text("read only", encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    compiled = module.compile_manifest(
        mode="direct", project_root=tmp_path, mission_path=mission, output_path=manifest_path
    )
    calls = 0

    def crash(_path: Path, **_kwargs):
        nonlocal calls
        calls += 1
        raise KeyboardInterrupt("simulated process death")

    monkeypatch.setattr(module.RUNNER, "execute_run", crash)
    with pytest.raises(KeyboardInterrupt):
        module.execute_with_automatic_fallback(compiled)
    resumed = module.execute_with_automatic_fallback(compiled)

    assert calls == 1
    assert resumed["ok"] is False
    assert resumed["status"] == "existing_primary_needs_exact_recovery"
    assert resumed["no_resubmission"] is True


def test_crash_immediately_after_helper_prepare_resumes_without_resubmission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = load()
    compiled, contract_path, calls = install_write_fallback_scenario(
        module, tmp_path, monkeypatch
    )
    real_externalize = module._externalize_snapshots
    crashed = False

    def crash_after_helper(
        receipt: dict[str, object], directory: Path, *, prefix: str
    ) -> dict[str, object]:
        nonlocal crashed
        if prefix == "attachment-apply-snapshot" and not crashed:
            crashed = True
            raise KeyboardInterrupt("crash immediately after helper prepare")
        return real_externalize(receipt, directory, prefix=prefix)

    monkeypatch.setattr(module, "_externalize_snapshots", crash_after_helper)
    with pytest.raises(KeyboardInterrupt, match="helper prepare"):
        module.execute_with_automatic_fallback(
            compiled, fallback_contract_path=contract_path
        )

    episode_dirs = list(
        (module.RUNNER.STATE.oracle_state_root() / "dispatcher-episodes").iterdir()
    )
    assert len(episode_dirs) == 1
    assert list((episode_dirs[0] / "transactions").glob(".codex-oracle-fallback-*"))
    monkeypatch.setattr(module, "_externalize_snapshots", real_externalize)
    resumed = module.execute_with_automatic_fallback(
        compiled, fallback_contract_path=contract_path
    )

    assert resumed["ok"] is True
    assert resumed["resumed"] is True
    assert resumed["power_loss_durability"]["fallback_instruction"]["durable"] is True
    assert resumed["power_loss_durability"]["oracle_run"]["run_directory"]["durable"] is True
    assert calls == {"primary": 1, "fallback": 1}
    journal = json.loads((episode_dirs[0] / "journal.json").read_text(encoding="utf-8"))
    assert journal["patch_finalization"]["finalized"] is True


def test_crash_after_receipt_and_episode_acceptance_finalizes_on_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = load()
    compiled, contract_path, calls = install_write_fallback_scenario(
        module, tmp_path, monkeypatch
    )
    real_finalize = module.FALLBACK.finalize_prepared_patch_envelope
    crashed = False

    def crash_before_finalize(*args: object, **kwargs: object) -> dict[str, object]:
        nonlocal crashed
        if not crashed:
            crashed = True
            raise KeyboardInterrupt("crash after durable acceptance before finalize")
        return real_finalize(*args, **kwargs)

    monkeypatch.setattr(
        module.FALLBACK, "finalize_prepared_patch_envelope", crash_before_finalize
    )
    with pytest.raises(KeyboardInterrupt, match="durable acceptance"):
        module.execute_with_automatic_fallback(
            compiled, fallback_contract_path=contract_path
        )

    episode_dir = next(
        (module.RUNNER.STATE.oracle_state_root() / "dispatcher-episodes").iterdir()
    )
    journal = json.loads((episode_dir / "journal.json").read_text(encoding="utf-8"))
    assert journal["status"] == "acceptance_prepared"
    assert journal["acceptance"] is not None
    assert journal["patch_finalization"] is None
    resumed = module.execute_with_automatic_fallback(
        compiled, fallback_contract_path=contract_path
    )

    assert resumed["ok"] is True
    assert resumed["resumed"] is True
    assert calls == {"primary": 1, "fallback": 1}
    finalized = json.loads((episode_dir / "journal.json").read_text(encoding="utf-8"))
    assert finalized["status"] == "complete"
    assert finalized["patch_finalization"]["finalized"] is True


@pytest.mark.parametrize("failure_point", ["receipt", "acceptance", "journal"])
def test_durable_handshake_write_failure_never_calls_finalize(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    module = load()
    compiled, contract_path, _calls = install_write_fallback_scenario(
        module, tmp_path, monkeypatch
    )
    real_write = module.RUNNER.STATE.write_json_atomic_durable
    finalized = False

    def fail_selected(path: Path, payload: dict[str, object], **kwargs: object):
        name = Path(path).name
        should_fail = (
            (failure_point == "receipt" and name == "attachment-apply-result.json")
            or (failure_point == "acceptance" and name == "acceptance.json")
            or (
                failure_point == "journal"
                and name == "journal.json"
                and payload.get("status") == "acceptance_prepared"
            )
        )
        if should_fail:
            raise module.RUNNER.STATE.OracleStateError(
                "DURABLE_JSON_WRITE_FAILED", f"injected {failure_point} failure"
            )
        return real_write(path, payload, **kwargs)

    def finalize_spy(*_args: object, **_kwargs: object) -> dict[str, object]:
        nonlocal finalized
        finalized = True
        return {"finalized": True}

    monkeypatch.setattr(module.RUNNER.STATE, "write_json_atomic_durable", fail_selected)
    monkeypatch.setattr(
        module.FALLBACK, "finalize_prepared_patch_envelope", finalize_spy
    )

    with pytest.raises(module.RUNNER.STATE.OracleStateError, match=failure_point):
        module.execute_with_automatic_fallback(
            compiled, fallback_contract_path=contract_path
        )

    assert finalized is False


def test_dispatcher_durable_handshake_orders_receipt_acceptance_journal_then_finalize(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = load()
    compiled, contract_path, _calls = install_write_fallback_scenario(
        module, tmp_path, monkeypatch
    )
    real_write = module.RUNNER.STATE.write_json_atomic_durable
    real_finalize = module.FALLBACK.finalize_prepared_patch_envelope
    events: list[str] = []

    def record_write(path: Path, payload: dict[str, object], **kwargs: object):
        name = Path(path).name
        if name == "attachment-apply-result.json":
            events.append("receipt-durable")
        elif name == "acceptance.json":
            events.append("acceptance-durable")
        elif name == "journal.json" and payload.get("status") == "acceptance_prepared":
            events.append("journal-reference-durable")
        return real_write(path, payload, **kwargs)

    def record_finalize(*args: object, **kwargs: object):
        events.append("finalize")
        return real_finalize(*args, **kwargs)

    monkeypatch.setattr(module.RUNNER.STATE, "write_json_atomic_durable", record_write)
    monkeypatch.setattr(
        module.FALLBACK, "finalize_prepared_patch_envelope", record_finalize
    )

    result = module.execute_with_automatic_fallback(
        compiled, fallback_contract_path=contract_path
    )

    assert result["ok"] is True
    assert events == [
        "receipt-durable",
        "acceptance-durable",
        "journal-reference-durable",
        "finalize",
    ]


def test_dispatcher_durably_creates_episode_and_transaction_roots_and_preserves_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = load()
    compiled, contract_path, _calls = install_write_fallback_scenario(
        module, tmp_path, monkeypatch
    )
    real_ensure = module.RUNNER.STATE.ensure_directory_durable
    ensured: list[Path] = []

    def record_ensure(path: Path, **kwargs: object):
        ensured.append(Path(path))
        return real_ensure(path, **kwargs)

    monkeypatch.setattr(module.RUNNER.STATE, "ensure_directory_durable", record_ensure)
    result = module.execute_with_automatic_fallback(
        compiled, fallback_contract_path=contract_path
    )

    episode_dir = next(
        (module.RUNNER.STATE.oracle_state_root() / "dispatcher-episodes").iterdir()
    )
    assert episode_dir in ensured
    assert episode_dir / "transactions" in ensured
    evidence = result["power_loss_durability"]["apply_receipt"]
    assert evidence["durable"] is True
    assert result["power_loss_durability"]["fallback_instruction"]["durable"] is True
    assert result["power_loss_durability"]["oracle_run"]["run_directory"][
        "durable"
    ] is True
    if os.name == "nt":
        assert evidence["parent_directory"]["directory_flush_supported"] is False
        assert "MoveFileExW MOVEFILE_WRITE_THROUGH" in evidence["parent_directory"]["boundary"]
        assert "MoveFileExW MOVEFILE_WRITE_THROUGH" in result["power_loss_durability"][
            "episode_directory"
        ]["boundary"]
        assert "MoveFileExW MOVEFILE_WRITE_THROUGH" in result["power_loss_durability"][
            "transaction_root"
        ]["boundary"]
    acceptance = json.loads((episode_dir / "acceptance.json").read_text(encoding="utf-8"))
    assert acceptance["power_loss_durability"] == result["power_loss_durability"]


def test_fallback_instruction_durable_write_failure_prevents_submission_and_finalize(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = load()
    compiled, contract_path, calls = install_write_fallback_scenario(
        module, tmp_path, monkeypatch
    )
    finalized = False
    real_write = module.RUNNER.STATE.write_text_atomic_durable

    def fail_fallback_instruction(path: Path, text: str, **kwargs):
        if "attachment-fallbacks" in Path(path).parts and Path(path).name == "mission.md":
            raise module.RUNNER.STATE.OracleStateError(
                "INJECTED_FALLBACK_INSTRUCTION_DURABILITY_FAILURE",
                "fallback instruction durability fault",
            )
        return real_write(path, text, **kwargs)

    def finalize_spy(*_args, **_kwargs):
        nonlocal finalized
        finalized = True
        return {"finalized": True}

    monkeypatch.setattr(
        module.RUNNER.STATE,
        "write_text_atomic_durable",
        fail_fallback_instruction,
    )
    monkeypatch.setattr(
        module.FALLBACK, "finalize_prepared_patch_envelope", finalize_spy
    )

    with pytest.raises(
        module.RUNNER.STATE.OracleStateError,
        match="fallback instruction durability fault",
    ):
        module.execute_with_automatic_fallback(
            compiled, fallback_contract_path=contract_path
        )

    assert calls == {"primary": 1, "fallback": 0}
    assert finalized is False
