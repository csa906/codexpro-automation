from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any, Iterable

BIN = Path(__file__).resolve().parent


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"module unavailable: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


PROFILES = _load("oracle_dispatch_profiles", BIN / "chatgpt_oracle_profiles.py")
RUNNER = _load("oracle_dispatch_runner", BIN / "chatgpt_oracle_run.py")
FALLBACK = _load("oracle_dispatch_fallback", BIN / "chatgpt_oracle_fallback.py")

FALLBACK_AUTHORITY_SCHEMA = "codex.chatgpt.oracle-attachment-fallback-authority/v1"
EXECUTION_AUTHORITY_SCHEMA = "codex.chatgpt.oracle-dispatch-execution-authority/v1"
DISPATCH_EPISODE_SCHEMA = "codex.chatgpt.oracle-dispatch-episode/v1"
ELIGIBLE_DEVSPACE_FALLBACK_CODES = frozenset({
    "DEVSPACE_EXACT_ROOT_UNAVAILABLE",
    "ORACLE_CONNECTOR_PRE_SUBMIT_FAILED",
})
WRITE_AUTHORITIES = frozenset({"workspace-write", "mission-owned-adaptive-execution"})

DEFAULT_ACTION_AUTHORITY = {
    "edit": "workspace-write",
    "orchestrator": "mission-owned-adaptive-execution",
}


class OracleDispatchError(RuntimeError):
    def __init__(self, code: str, message: str, evidence: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.evidence = evidence or {}


def _path_key(path: Path | str) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def _default_limits() -> dict[str, int]:
    return {
        "max_evidence_files": 64,
        "max_evidence_file_bytes": 1_048_576,
        "max_evidence_total_bytes": 16_777_216,
        "max_patch_operations": 128,
        "max_patch_file_bytes": 4_194_304,
        "max_patch_total_bytes": 16_777_216,
        "local_gate_timeout_seconds": 3_600,
    }


def _default_readonly_fallback_contract(
    *, project_root: Path, mission_path: Path, mission_sha256: str, reasoning_level: str
):
    return FALLBACK.validate_contract({
        "schema": FALLBACK.CONTRACT_SCHEMA,
        "project_root": str(project_root),
        "mission_path": str(mission_path),
        "mission_sha256": mission_sha256,
        "action_authority": "read-only",
        "reasoning_level": reasoning_level,
        "evidence_allowlist": [],
        "edit_path_allowlist": [],
        "local_gate_command": None,
        "limits": _default_limits(),
    })


def _prepare_fallback_contract(
    compiled: dict[str, Any], manifest: dict[str, Any], fallback_contract_path: Path | None
):
    root = Path(str(manifest["project_root"])).resolve(strict=True)
    mission = Path(str(compiled["contract"]["mission_path"])).resolve(strict=True)
    authority = str(manifest["action_authority"])
    reasoning = str(compiled["contract"]["reasoning_level"])
    if fallback_contract_path is None:
        if authority in WRITE_AUTHORITIES:
            raise OracleDispatchError(
                "MUTATING_EXECUTION_CONTRACT_REQUIRED",
                "write-capable Web GPT runs require a mission-explicit edit allowlist and local gate",
            )
        contract = _default_readonly_fallback_contract(
            project_root=root,
            mission_path=mission,
            mission_sha256=RUNNER.STATE.sha256_file(mission),
            reasoning_level=reasoning,
        )
    else:
        contract = FALLBACK.load_contract(fallback_contract_path)
    mismatches: dict[str, Any] = {}
    if _path_key(contract.project_root) != _path_key(root):
        mismatches["project_root"] = {"contract": str(contract.project_root), "run": str(root)}
    if _path_key(contract.mission_path) != _path_key(mission):
        mismatches["mission_path"] = {"contract": str(contract.mission_path), "run": str(mission)}
    if contract.action_authority != authority:
        mismatches["action_authority"] = {"contract": contract.action_authority, "run": authority}
    if contract.reasoning_level != reasoning:
        mismatches["reasoning_level"] = {"contract": contract.reasoning_level, "run": reasoning}
    if mismatches:
        raise OracleDispatchError(
            "FALLBACK_CONTRACT_RUN_MISMATCH",
            "fallback contract does not bind the exact primary root, mission, authority, and power",
            mismatches,
        )
    return contract


def _episode_dir(run_id: str) -> Path:
    return RUNNER.STATE.oracle_state_root() / "dispatcher-episodes" / run_id


def _other_active_episode(project_root: Path, run_id: str) -> dict[str, Any] | None:
    root = RUNNER.STATE.oracle_state_root() / "dispatcher-episodes"
    if not root.is_dir():
        return None
    expected = _path_key(project_root)
    for path in sorted(root.glob("*/journal.json"), key=lambda item: str(item)):
        try:
            value = json.loads(path.read_text(encoding="utf-8", errors="strict"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if (
            str(value.get("run_id") or "") != run_id
            and _path_key(str(value.get("project_root") or "")) == expected
            and str(value.get("status") or "") != "complete"
        ):
            return {
                "run_id": value.get("run_id"),
                "status": value.get("status"),
                "journal_path": str(path),
                "primary_run_dir": value.get("primary_run_dir"),
                "fallback": value.get("fallback"),
            }
    return None


def _hashed_reference(path: Path) -> dict[str, str]:
    return {"path": str(path), "sha256": RUNNER.STATE.sha256_file(path)}


def _read_hashed_json(reference: dict[str, Any], *, code: str) -> dict[str, Any]:
    if not isinstance(reference, dict) or set(reference) != {"path", "sha256"}:
        raise OracleDispatchError(code, "hashed JSON reference is invalid")
    path = Path(str(reference["path"])).expanduser().resolve(strict=True)
    expected = str(reference["sha256"]).strip().casefold()
    actual = RUNNER.STATE.sha256_file(path)
    if expected != actual:
        raise OracleDispatchError(code, "hashed JSON reference changed", {"path": str(path)})
    try:
        value = json.loads(path.read_text(encoding="utf-8", errors="strict"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OracleDispatchError(code, "hashed JSON reference is invalid JSON") from exc
    if not isinstance(value, dict):
        raise OracleDispatchError(code, "hashed JSON reference must contain one object")
    return value


def _rehydrate_contract(
    value: dict[str, Any],
    expected_sha256: str,
    before_snapshot: dict[str, Any],
    local_gate_executable_sha256: str | None,
):
    actual = FALLBACK.sha256_bytes(FALLBACK.canonical_json_bytes(value))
    if actual != expected_sha256:
        raise OracleDispatchError("EPISODE_CONTRACT_HASH_MISMATCH", "persisted normalized contract changed")
    # Validate the immutable baseline itself before trusting the originally
    # validated contract receipt. Current edit paths may already contain the
    # accepted direct-write result, so revalidating against live preimages would
    # incorrectly make exact-session recovery impossible.
    FALLBACK.compare_workspace_snapshots(before_snapshot, before_snapshot, declared_paths=())
    root = Path(str(value["project_root"])).resolve(strict=True)
    entries = {
        os.path.normcase(os.path.normpath(str(item["path"]))): item
        for item in before_snapshot["entries"]
    }
    evidence = []
    for item in value["evidence_allowlist"]:
        entry = entries.get(os.path.normcase(os.path.normpath(str(item["path"]))))
        if not entry or entry.get("kind") != "file" or entry.get("sha256") != item["sha256"]:
            raise OracleDispatchError("EPISODE_EVIDENCE_BASELINE_MISMATCH", str(item["path"]))
        evidence.append(FALLBACK.EvidenceFile(
            item["path"], item["category"], item["priority"], item["sha256"],
            root / Path(*Path(item["path"]).parts), int(entry["bytes"]),
        ))
    edits = []
    for item in value["edit_path_allowlist"]:
        entry = entries.get(os.path.normcase(os.path.normpath(str(item["path"]))))
        before_mode = int(entry["mode"]) if entry and entry.get("kind") == "file" else None
        before_metadata = entry.get("metadata") if entry and entry.get("kind") == "file" else None
        edits.append(FALLBACK.EditPath(
            item["path"], item["before_sha256"], tuple(item["operations"]),
            root / Path(*Path(item["path"]).parts), before_mode, before_metadata,
        ))
    limits = FALLBACK.ContractLimits(**value["limits"])
    return FALLBACK.FallbackContract(
        project_root=root,
        mission_path=Path(str(value["mission_path"])).resolve(),
        mission_sha256=str(value["mission_sha256"]),
        action_authority=str(value["action_authority"]),
        reasoning_level=str(value["reasoning_level"]),
        evidence_allowlist=tuple(evidence),
        edit_path_allowlist=tuple(edits),
        local_gate_command=(tuple(value["local_gate_command"]) if value["local_gate_command"] else None),
        local_gate_executable_sha256=local_gate_executable_sha256,
        limits=limits,
        contract_sha256=actual,
    )


def _create_dispatch_episode(
    *, compiled: dict[str, Any], base_manifest_path: Path, manifest: dict[str, Any], contract
) -> dict[str, Any]:
    run_id = str(manifest.get("run_id") or "")
    directory = _episode_dir(run_id)
    if directory.exists():
        return {"existing": True, "episode_dir": str(directory), "run_id": run_id}
    directory_durability = RUNNER.STATE.ensure_directory_durable(directory)
    if directory_durability.get("durable") is not True:
        raise OracleDispatchError(
            "EPISODE_DIRECTORY_DURABILITY_FAILED",
            "dispatch episode directory was not durably created",
            {"path": str(directory), "durability": directory_durability},
        )
    contract_path = directory / "contract.json"
    _write_json(contract_path, contract.contract_value())
    before_snapshot = FALLBACK.snapshot_workspace(contract.project_root)
    before_path = directory / "workspace-before.json"
    _write_json(before_path, before_snapshot)
    authority = {
        "schema": EXECUTION_AUTHORITY_SCHEMA,
        "claimed": True,
        "run_id": run_id,
        "project_root": str(contract.project_root),
        "mission_path": str(contract.mission_path),
        "mission_sha256": contract.mission_sha256,
        "action_authority": contract.action_authority,
        "reasoning_level": contract.reasoning_level,
        "thinking_time": compiled["contract"]["thinking_time"],
        "contract_sha256": contract.contract_sha256,
        "local_gate_executable_sha256": contract.local_gate_executable_sha256,
        "base_manifest": _hashed_reference(base_manifest_path),
        "contract": _hashed_reference(contract_path),
        "workspace_before": _hashed_reference(before_path),
    }
    authority_path = directory / "authority.json"
    authority_sha = _write_json(authority_path, authority)
    runtime_manifest = dict(manifest)
    runtime_manifest["execution_authority"] = {"path": str(authority_path), "sha256": authority_sha}
    runtime_manifest_path = directory / "primary-manifest.json"
    RUNNER.STATE.write_json_atomic(runtime_manifest_path, runtime_manifest)
    config = RUNNER.STATE.load_manifest(runtime_manifest_path)
    layout = RUNNER.STATE.create_layout(config, run_id=run_id)
    journal = {
        "schema": DISPATCH_EPISODE_SCHEMA,
        "status": "primary_claimed",
        "run_id": run_id,
        "project_root": str(contract.project_root),
        "action_authority": contract.action_authority,
        "reasoning_level": contract.reasoning_level,
        "authority": {"path": str(authority_path), "sha256": authority_sha},
        "contract": _hashed_reference(contract_path),
        "workspace_before": _hashed_reference(before_path),
        "runtime_manifest": _hashed_reference(runtime_manifest_path),
        "primary_run_dir": str(layout.run_dir),
        "fallback": None,
        "acceptance": None,
        "prepared_patch": None,
        "patch_finalization": None,
        "directory_durability": directory_durability,
    }
    journal_path = directory / "journal.json"
    _write_json(journal_path, journal)
    return {
        "existing": False,
        "episode_dir": str(directory),
        "journal_path": str(journal_path),
        "journal": journal,
        "contract": contract,
        "before_snapshot": before_snapshot,
        "runtime_manifest_path": str(runtime_manifest_path),
        "run_id": run_id,
    }


def _update_episode_journal(directory: Path, **updates: Any) -> dict[str, Any]:
    path = directory / "journal.json"
    current = json.loads(path.read_text(encoding="utf-8"))
    current.update(updates)
    _write_json(path, current)
    return current


def _exception_code(exc: Exception) -> str:
    return str(getattr(exc, "code", "") or type(exc).__name__).strip()


def _connector_failure_proof(run: dict[str, Any]) -> dict[str, Any] | None:
    state = run.get("result") if isinstance(run.get("result"), dict) else {}
    proof = state.get("pre_submit_failure") if isinstance(state.get("pre_submit_failure"), dict) else None
    if (
        proof
        and proof.get("code") == "ORACLE_CONNECTOR_PRE_SUBMIT_FAILED"
        and proof.get("prompt_submitted") is False
        and proof.get("output_absent") is True
        and proof.get("conversation_url_absent") is True
        and state.get("session_authority") == "pre_submit"
        and state.get("terminal_harvested") is not True
        and not str((state.get("oracle") or {}).get("conversation_url") or "").strip()
    ):
        return proof
    return None


def _write_json_with_durability(
    path: Path, value: dict[str, Any]
) -> tuple[str, dict[str, Any]]:
    durability = RUNNER.STATE.write_json_atomic_durable(path, value)
    if durability.get("durable") is not True:
        raise OracleDispatchError(
            "DISPATCH_DURABLE_WRITE_FAILED",
            "dispatch JSON did not reach its required durability boundary",
            {"path": str(path), "durability": durability},
        )
    actual = RUNNER.STATE.sha256_file(path)
    if actual != durability.get("sha256"):
        raise OracleDispatchError(
            "DISPATCH_DURABLE_WRITE_HASH_MISMATCH",
            "durable dispatch JSON hash changed after persistence",
            {"path": str(path)},
        )
    return actual, durability


def _write_json(path: Path, value: dict[str, Any]) -> str:
    digest, _durability = _write_json_with_durability(path, value)
    return digest


def _externalize_snapshots(receipt: dict[str, Any], directory: Path, *, prefix: str) -> dict[str, Any]:
    value = dict(receipt)
    snapshots = value.get("snapshots")
    if not isinstance(snapshots, dict):
        return value
    references: dict[str, dict[str, str]] = {}
    for name, snapshot in snapshots.items():
        if not isinstance(snapshot, dict):
            continue
        path = directory / f"{prefix}-{name}.json"
        digest = _write_json(path, snapshot)
        references[str(name)] = {"path": str(path), "sha256": digest}
    value["snapshots"] = references
    return value


def _record_mutation(run: dict[str, Any], *, status: str, evidence: dict[str, Any]) -> None:
    run_dir = Path(str(run.get("run_dir") or ""))
    state_path = run_dir / "state.json"
    if not state_path.is_file():
        return
    state = RUNNER.STATE.load_state(state_path)
    RUNNER.STATE.update_state(
        state_path,
        status=str(state.get("status") or "attention_required"),
        exit_code=state.get("exit_code"),
        mutation={"status": status, "evidence": evidence},
    )


def _load_episode_context(directory: Path) -> dict[str, Any]:
    episode_dir = directory.expanduser().resolve(strict=True)
    if not RUNNER.STATE.is_within(RUNNER.STATE.oracle_state_root() / "dispatcher-episodes", episode_dir):
        raise OracleDispatchError("EPISODE_PATH_INVALID", "dispatch episode is outside canonical host state")
    journal_path = episode_dir / "journal.json"
    journal = json.loads(journal_path.read_text(encoding="utf-8", errors="strict"))
    if journal.get("schema") != DISPATCH_EPISODE_SCHEMA or journal.get("run_id") != episode_dir.name:
        raise OracleDispatchError("EPISODE_JOURNAL_INVALID", "dispatch episode journal identity is invalid")
    authority = _read_hashed_json(journal["authority"], code="EPISODE_AUTHORITY_INVALID")
    if (
        authority.get("schema") != EXECUTION_AUTHORITY_SCHEMA
        or authority.get("claimed") is not True
        or authority.get("run_id") != journal.get("run_id")
        or authority.get("project_root") != journal.get("project_root")
        or authority.get("action_authority") != journal.get("action_authority")
        or authority.get("reasoning_level") != journal.get("reasoning_level")
        or authority.get("contract") != journal.get("contract")
        or authority.get("workspace_before") != journal.get("workspace_before")
    ):
        raise OracleDispatchError("EPISODE_AUTHORITY_BINDING_MISMATCH", "dispatch episode journal changed")
    contract_value = _read_hashed_json(journal["contract"], code="EPISODE_CONTRACT_INVALID")
    before_snapshot = _read_hashed_json(journal["workspace_before"], code="EPISODE_BASELINE_INVALID")
    contract = _rehydrate_contract(
        contract_value,
        str(authority.get("contract_sha256") or ""),
        before_snapshot,
        (
            str(authority.get("local_gate_executable_sha256"))
            if authority.get("local_gate_executable_sha256") is not None
            else None
        ),
    )
    return {
        "episode_dir": episode_dir,
        "journal": journal,
        "authority": authority,
        "contract": contract,
        "before_snapshot": before_snapshot,
    }


def _load_completed_episode_acceptance(directory: Path) -> dict[str, Any] | None:
    """Read an immutable completed acceptance without reopening its old baseline."""
    episode_dir = directory.expanduser().resolve(strict=True)
    episodes_root = (RUNNER.STATE.oracle_state_root() / "dispatcher-episodes").resolve()
    if not episodes_root.is_dir() or episode_dir.parent != episodes_root:
        raise OracleDispatchError(
            "EPISODE_PATH_INVALID", "dispatch episode is outside canonical host state"
        )
    journal_path = episode_dir / "journal.json"
    try:
        journal = json.loads(journal_path.read_text(encoding="utf-8", errors="strict"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OracleDispatchError(
            "EPISODE_JOURNAL_INVALID", "dispatch episode journal is invalid"
        ) from exc
    if (
        not isinstance(journal, dict)
        or journal.get("schema") != DISPATCH_EPISODE_SCHEMA
        or journal.get("run_id") != episode_dir.name
    ):
        raise OracleDispatchError(
            "EPISODE_JOURNAL_INVALID", "dispatch episode journal identity is invalid"
        )
    if journal.get("status") != "complete":
        return None

    reference = journal.get("acceptance")
    if not isinstance(reference, dict):
        raise OracleDispatchError(
            "EPISODE_ACCEPTANCE_INVALID", "completed episode has no acceptance reference"
        )
    acceptance_path = Path(str(reference.get("path") or "")).expanduser().resolve()
    if acceptance_path != episode_dir / "acceptance.json" or not acceptance_path.is_file():
        raise OracleDispatchError(
            "EPISODE_ACCEPTANCE_INVALID",
            "completed episode acceptance is outside its canonical path",
        )
    accepted = _read_hashed_json(reference, code="EPISODE_ACCEPTANCE_INVALID")
    if accepted.get("ok") is not True or accepted.get("status") not in {
        "complete",
        "attachment_fallback_complete",
        "attachment_fallback_applied",
    }:
        raise OracleDispatchError(
            "EPISODE_ACCEPTANCE_INVALID",
            "completed episode acceptance does not record a successful terminal result",
        )

    apply_receipt = accepted.get("apply_receipt")
    prepared_transaction = (
        apply_receipt.get("prepared_transaction")
        if isinstance(apply_receipt, dict)
        else None
    )
    if isinstance(journal.get("prepared_patch"), dict) or isinstance(
        prepared_transaction, dict
    ):
        finalization = journal.get("patch_finalization")
        if not isinstance(finalization, dict) or finalization.get("finalized") is not True:
            return None
    return accepted


def _terminal_run(run_dir: Path) -> dict[str, Any] | None:
    state_path = run_dir / "state.json"
    if not state_path.is_file():
        return None
    state = RUNNER.STATE.load_state(state_path)
    RUNNER.STATE.verify_execution_authority_state(state)
    RUNNER.STATE.verify_fallback_authority_state(state)
    output = Path(str((state.get("artifacts") or {}).get("output") or ""))
    if (
        state.get("session_authority") == "terminal"
        and state.get("terminal_harvested") is True
        and output.is_file()
        and output.stat().st_size > 0
    ):
        outcome = str(state.get("task_outcome") or "")
        return {
            "ok": outcome in {"executed", "not_applicable", "legacy_unclassified"},
            "status": "complete",
            "run_dir": str(run_dir),
            "result": state,
            "output_path": str(output),
        }
    return None


def _save_episode_acceptance(episode_dir: Path, result: dict[str, Any], *, complete: bool) -> dict[str, Any]:
    path = episode_dir / "acceptance.json"
    digest = _write_json(path, result)
    _update_episode_journal(
        episode_dir,
        status="complete" if complete else "local_attention",
        acceptance={"path": str(path), "sha256": digest},
    )
    return result


def _patch_transaction_root(episode_dir: Path) -> tuple[Path, dict[str, Any]]:
    root = episode_dir / "transactions"
    durability = RUNNER.STATE.ensure_directory_durable(root)
    if durability.get("durable") is not True:
        raise OracleDispatchError(
            "TRANSACTION_DIRECTORY_DURABILITY_FAILED",
            "patch transaction directory was not durably created",
            {"path": str(root), "durability": durability},
        )
    return root, durability


def _persist_accept_and_finalize_patch(
    *,
    episode_dir: Path,
    contract,
    patch: dict[str, Any],
    before_snapshot: dict[str, Any],
    fallback_run: dict[str, Any],
    output_path: Path,
    result_base: dict[str, Any],
) -> dict[str, Any]:
    with RUNNER.STATE.project_submit_mutex(contract.project_root, timeout_seconds=30):
        transaction_root, transaction_root_durability = _patch_transaction_root(episode_dir)
        fresh_journal = json.loads(
            (episode_dir / "journal.json").read_text(encoding="utf-8", errors="strict")
        )
        if fresh_journal.get("acceptance"):
            accepted_result = _read_hashed_json(
                fresh_journal["acceptance"], code="EPISODE_ACCEPTANCE_INVALID"
            )
            accepted_receipt = accepted_result.get("apply_receipt")
            if (
                accepted_result.get("ok") is True
                and isinstance(accepted_receipt, dict)
            ):
                finalization = FALLBACK.finalize_prepared_patch_envelope(
                    contract,
                    patch,
                    accepted_receipt,
                    receipt_reference={
                        "path": str(accepted_receipt.get("receipt_path") or ""),
                        "sha256": str(accepted_receipt.get("receipt_sha256") or ""),
                    },
                    episode_reference=fresh_journal["acceptance"],
                )
                _update_episode_journal(
                    episode_dir, status="complete", patch_finalization=finalization
                )
            return accepted_result
        prepared_receipt = FALLBACK.resume_or_apply_patch_envelope(
            contract,
            patch,
            baseline_snapshot=before_snapshot,
            transaction_root=transaction_root,
            retain_prepared_transaction=True,
        )
        apply_path = Path(str(fallback_run["run_dir"])) / "attachment-apply-result.json"
        persisted_receipt = _externalize_snapshots(
            prepared_receipt, apply_path.parent, prefix="attachment-apply-snapshot"
        )
        apply_sha, apply_durability = _write_json_with_durability(
            apply_path, persisted_receipt
        )
        accepted = bool(persisted_receipt.get("fallback_eligible"))
        receipt_reference = {"path": str(apply_path), "sha256": apply_sha}
        _record_mutation(
            fallback_run,
            status="applied" if accepted else "partial",
            evidence={**receipt_reference, "accepted": accepted},
        )
        inherited_durability = result_base.get("power_loss_durability")
        completed = {
            **result_base,
            "ok": accepted,
            "status": (
                "attachment_fallback_applied"
                if accepted
                else "attachment_fallback_verification_failed"
            ),
            "output_path": str(output_path),
            "apply_receipt": {
                **persisted_receipt,
                "receipt_path": str(apply_path),
                "receipt_sha256": apply_sha,
            },
            "power_loss_durability": {
                **(
                    inherited_durability
                    if isinstance(inherited_durability, dict)
                    else {}
                ),
                "episode_directory": fresh_journal.get("directory_durability"),
                "transaction_root": transaction_root_durability,
                "apply_receipt": apply_durability,
            },
        }
        acceptance_path = episode_dir / "acceptance.json"
        acceptance_sha = _write_json(acceptance_path, completed)
        acceptance_reference = {"path": str(acceptance_path), "sha256": acceptance_sha}
        _update_episode_journal(
            episode_dir,
            status="acceptance_prepared" if accepted else "local_attention",
            acceptance=acceptance_reference,
            prepared_patch=persisted_receipt.get("prepared_transaction"),
            patch_finalization=None,
        )
        if accepted:
            finalization = FALLBACK.finalize_prepared_patch_envelope(
                contract,
                patch,
                persisted_receipt,
                receipt_reference=receipt_reference,
                episode_reference=acceptance_reference,
            )
            _update_episode_journal(
                episode_dir,
                status="complete",
                patch_finalization=finalization,
            )
        return completed


def _finalize_persisted_episode_acceptance(
    context: dict[str, Any], accepted: dict[str, Any]
) -> None:
    journal = context["journal"]
    apply_receipt = accepted.get("apply_receipt")
    if (
        accepted.get("ok") is not True
        or not isinstance(apply_receipt, dict)
        or not isinstance(apply_receipt.get("prepared_transaction"), dict)
    ):
        return
    fallback_info = journal.get("fallback")
    if not isinstance(fallback_info, dict):
        raise OracleDispatchError(
            "EPISODE_PATCH_FINALIZATION_INVALID", "accepted patch has no fallback episode"
        )
    run = _terminal_run(Path(str(fallback_info.get("run_dir") or "")).resolve())
    if run is None:
        raise OracleDispatchError(
            "EPISODE_PATCH_FINALIZATION_INVALID", "accepted patch output is unavailable"
        )
    output_path = Path(str(run["output_path"]))
    patch = FALLBACK.parse_patch_envelope(
        output_path.read_text(encoding="utf-8", errors="strict"),
        context["contract"],
        revalidate_current=False,
    )
    receipt_reference = {
        "path": str(apply_receipt.get("receipt_path") or ""),
        "sha256": str(apply_receipt.get("receipt_sha256") or ""),
    }
    acceptance_reference = journal.get("acceptance")
    with RUNNER.STATE.project_submit_mutex(context["contract"].project_root, timeout_seconds=30):
        finalization = FALLBACK.finalize_prepared_patch_envelope(
            context["contract"],
            patch,
            apply_receipt,
            receipt_reference=receipt_reference,
            episode_reference=acceptance_reference,
        )
        _update_episode_journal(
            context["episode_dir"], status="complete", patch_finalization=finalization
        )


def _resume_episode(directory: Path) -> dict[str, Any]:
    completed_acceptance = _load_completed_episode_acceptance(directory)
    if completed_acceptance is not None:
        return {
            **completed_acceptance,
            "resumed": True,
            "no_resubmission": True,
        }
    context = _load_episode_context(directory)
    episode_dir = context["episode_dir"]
    journal = context["journal"]
    if journal.get("acceptance"):
        accepted = _read_hashed_json(journal["acceptance"], code="EPISODE_ACCEPTANCE_INVALID")
        _finalize_persisted_episode_acceptance(context, accepted)
        return {**accepted, "resumed": True, "no_resubmission": True}
    fallback_info = journal.get("fallback") if isinstance(journal.get("fallback"), dict) else None
    if fallback_info:
        fallback_run_dir = Path(str(fallback_info.get("run_dir") or "")).resolve()
        run = _terminal_run(fallback_run_dir)
        if run is None:
            return {
                "ok": False,
                "status": "existing_fallback_needs_exact_recovery",
                "run_dir": str(fallback_run_dir),
                "no_resubmission": True,
            }
        contract = context["contract"]
        output_path = Path(str(run["output_path"]))
        persisted_fallback_durability = fallback_info.get("power_loss_durability")
        run_state = run.get("result") if isinstance(run.get("result"), dict) else {}
        run_durability = run_state.get("power_loss_durability")
        resumed_durability = {
            **(
                persisted_fallback_durability
                if isinstance(persisted_fallback_durability, dict)
                else {}
            ),
            "oracle_run": run_durability if isinstance(run_durability, dict) else None,
        }
        if contract.action_authority not in WRITE_AUTHORITIES:
            result = {
                "ok": True,
                "status": "attachment_fallback_complete",
                "route": "attachment-fallback",
                "fallback": run,
                "output_path": str(output_path),
                "resumed": True,
                "no_resubmission": True,
                "power_loss_durability": resumed_durability,
            }
            return _save_episode_acceptance(episode_dir, result, complete=True)
        patch = FALLBACK.parse_patch_envelope(
            output_path.read_text(encoding="utf-8", errors="strict"),
            contract,
            revalidate_current=False,
        )
        result_base = {
            "route": "attachment-fallback",
            "fallback": run,
            "resumed": True,
            "no_resubmission": True,
            "power_loss_durability": resumed_durability,
        }
        return _persist_accept_and_finalize_patch(
            episode_dir=episode_dir,
            contract=contract,
            patch=patch,
            before_snapshot=context["before_snapshot"],
            fallback_run=run,
            output_path=output_path,
            result_base=result_base,
        )
    primary_run_dir = Path(str(journal.get("primary_run_dir") or "")).resolve()
    run = _terminal_run(primary_run_dir)
    if run is None:
        return {
            "ok": False,
            "status": "existing_primary_needs_exact_recovery",
            "run_dir": str(primary_run_dir),
            "no_resubmission": True,
        }
    acceptance = _host_verify_primary(run, context["contract"], context["before_snapshot"])
    result = {
        **run,
        "ok": bool(acceptance.get("accepted")),
        "status": "complete" if acceptance.get("accepted") else "host_verification_failed",
        "host_acceptance": acceptance,
        "resumed": True,
        "no_resubmission": True,
    }
    return _save_episode_acceptance(episode_dir, result, complete=bool(result["ok"]))


def resume_dispatch_run(run_dir: Path) -> dict[str, Any]:
    directory = run_dir.expanduser().resolve(strict=True)
    state = RUNNER.STATE.load_state(directory / "state.json")
    execution = RUNNER.STATE.verify_execution_authority_state(state)
    if execution is not None:
        return _resume_episode(Path(str((state.get("execution_authority") or {}).get("path"))).parent)
    fallback = RUNNER.STATE.verify_fallback_authority_state(state)
    if fallback is not None and str(fallback.get("episode_dir") or ""):
        return _resume_episode(Path(str(fallback["episode_dir"])))
    raise OracleDispatchError(
        "DISPATCH_EPISODE_UNAVAILABLE",
        "run has no persisted dispatch episode; recover the exact session and verify it manually",
    )


def _host_verify_primary(
    run: dict[str, Any], contract, before_snapshot: dict[str, Any]
) -> dict[str, Any]:
    authority = contract.action_authority
    if authority in WRITE_AUTHORITIES:
        task_outcome = str((run.get("result") or {}).get("task_outcome") or "")
        if task_outcome != "executed":
            return {
                "accepted": False,
                "code": "PRIMARY_TASK_OUTCOME_NOT_EXECUTED",
                "task_outcome": task_outcome,
            }
        receipt = FALLBACK.verify_direct_devspace_write(contract, before_snapshot)
        receipt_path = Path(str(run["run_dir"])) / "direct-write-acceptance.json"
        receipt = _externalize_snapshots(receipt, receipt_path.parent, prefix="direct-write-snapshot")
        receipt_sha = _write_json(receipt_path, receipt)
        evidence = {"path": str(receipt_path), "sha256": receipt_sha, "accepted": receipt["accepted"]}
        _record_mutation(
            run,
            status="applied" if receipt["accepted"] else "partial",
            evidence=evidence,
        )
        return {**receipt, "receipt": evidence}
    after = FALLBACK.snapshot_workspace(contract.project_root)
    delta = FALLBACK.compare_workspace_snapshots(before_snapshot, after, declared_paths=())
    return {
        "schema": "codex.chatgpt.oracle-readonly-acceptance/v1",
        "accepted": not delta["changes"],
        "delta": delta,
    }


def _launch_attachment_fallback(
    *,
    compiled: dict[str, Any],
    primary_manifest_path: Path,
    primary_failure: dict[str, Any],
    failure_code: str,
    contract,
    before_snapshot: dict[str, Any],
    after_snapshot: dict[str, Any],
    episode_dir: Path,
) -> dict[str, Any]:
    contract = FALLBACK.revalidate_contract(contract)
    request = FALLBACK.build_attachment_request(contract)
    episode_journal = json.loads((episode_dir / "journal.json").read_text(encoding="utf-8"))
    fallback_id = "fallback-" + hashlib.sha256(
        f"{episode_journal['run_id']}\0{contract.contract_sha256}".encode("utf-8")
    ).hexdigest()[:32]
    fallback_root = RUNNER.STATE.oracle_state_root() / "attachment-fallbacks" / fallback_id
    if fallback_root.exists():
        raise OracleDispatchError(
            "FALLBACK_ROOT_ALREADY_EXISTS", "attachment fallback root already exists"
        )
    fallback_root_durability = RUNNER.STATE.ensure_directory_durable(fallback_root)
    if fallback_root_durability.get("durable") is not True:
        raise OracleDispatchError(
            "FALLBACK_DIRECTORY_DURABILITY_FAILED",
            "attachment fallback root was not durably created",
            {"path": str(fallback_root), "durability": fallback_root_durability},
        )
    instruction_path = fallback_root / "mission.md"
    instruction_durability = RUNNER.STATE.write_text_atomic_durable(
        instruction_path,
        str(request["instructions"]),
        encoding="utf-8",
        newline="\n",
    )
    if instruction_durability.get("durable") is not True:
        raise OracleDispatchError(
            "FALLBACK_INSTRUCTION_DURABILITY_FAILED",
            "attachment fallback instructions were not durably persisted",
            {"path": str(instruction_path), "durability": instruction_durability},
        )
    origin_path = fallback_root / "origin-failure.json"
    before_path = fallback_root / "workspace-before.json"
    after_path = fallback_root / "workspace-after-failure.json"
    origin_sha = _write_json(origin_path, primary_failure)
    before_sha = _write_json(before_path, before_snapshot)
    after_sha = _write_json(after_path, after_snapshot)
    attachment_paths = [Path(value).resolve(strict=True) for value in request["attachments"]]
    exact_attachments = [instruction_path.resolve(), *attachment_paths]
    attachment_receipt = [
        {"path": str(path), "sha256": RUNNER.STATE.sha256_file(path)} for path in exact_attachments
    ]
    authority = {
        "schema": FALLBACK_AUTHORITY_SCHEMA,
        "consumed": True,
        "fallback_run_id": fallback_id,
        "primary_run_id": episode_journal["run_id"],
        "episode_dir": str(episode_dir),
        "project_root": str(contract.project_root),
        "action_authority": contract.action_authority,
        "thinking_time": compiled["contract"]["thinking_time"],
        "reasoning_level": contract.reasoning_level,
        "contract_sha256": contract.contract_sha256,
        "instruction_sha256": RUNNER.STATE.sha256_file(instruction_path),
        "attachments": attachment_receipt,
        "primary_manifest_sha256": RUNNER.STATE.sha256_file(primary_manifest_path),
        "origin_failure_code": failure_code,
        "origin_failure": {"path": str(origin_path), "sha256": origin_sha},
        "workspace_before": {"path": str(before_path), "sha256": before_sha},
        "workspace_after_failure": {"path": str(after_path), "sha256": after_sha},
    }
    authority_path = fallback_root / "authority.json"
    authority_sha = _write_json(authority_path, authority)
    fallback_manifest_path = fallback_root / "manifest.json"
    fallback_compiled = compile_manifest(
        mode="attachment",
        project_root=contract.project_root,
        mission_path=instruction_path,
        output_path=fallback_manifest_path,
        reasoning_level=contract.reasoning_level,
        attachment_paths=attachment_paths,
        action_authority=contract.action_authority,
    )
    fallback_manifest = json.loads(fallback_manifest_path.read_text(encoding="utf-8"))
    fallback_manifest["run_id"] = fallback_id
    fallback_manifest["fallback_authority"] = {"path": str(authority_path), "sha256": authority_sha}
    fallback_manifest_durability = RUNNER.STATE.write_json_atomic_durable(
        fallback_manifest_path, fallback_manifest
    )
    if fallback_manifest_durability.get("durable") is not True:
        raise OracleDispatchError(
            "FALLBACK_MANIFEST_DURABILITY_FAILED",
            "attachment fallback manifest was not durably persisted",
            {"path": str(fallback_manifest_path), "durability": fallback_manifest_durability},
        )
    _update_episode_journal(
        episode_dir,
        status="fallback_claimed",
        fallback={
            "run_id": fallback_id,
            "authority": {"path": str(authority_path), "sha256": authority_sha},
            "manifest": _hashed_reference(fallback_manifest_path),
            "run_dir": str(RUNNER.STATE.create_layout(
                RUNNER.STATE.load_manifest(fallback_manifest_path), run_id=fallback_id
            ).run_dir),
            "power_loss_durability": {
                "fallback_directory": fallback_root_durability,
                "fallback_instruction": instruction_durability,
                "fallback_manifest": fallback_manifest_durability,
            },
        },
    )
    fallback_run = RUNNER.execute_run(fallback_manifest_path)
    fallback_run_state = (
        fallback_run.get("result") if isinstance(fallback_run.get("result"), dict) else {}
    )
    fallback_run_durability = fallback_run_state.get("power_loss_durability")
    result: dict[str, Any] = {
        "ok": bool(fallback_run.get("ok")),
        "status": "attachment_fallback_complete" if fallback_run.get("ok") else "attachment_fallback_attention",
        "route": "attachment-fallback",
        "primary": primary_failure,
        "fallback": fallback_run,
        "fallback_contract_sha256": contract.contract_sha256,
        "fallback_authority": {"path": str(authority_path), "sha256": authority_sha},
        "fallback_manifest_path": fallback_compiled["oracle_manifest_path"],
        "power_loss_durability": {
            "fallback_directory": fallback_root_durability,
            "fallback_instruction": instruction_durability,
            "fallback_manifest": fallback_manifest_durability,
            "oracle_run": (
                fallback_run_durability
                if isinstance(fallback_run_durability, dict)
                else None
            ),
        },
    }
    if not fallback_run.get("ok"):
        _update_episode_journal(episode_dir, status="fallback_attention")
        return result
    output_path = Path(str((fallback_run.get("result") or {}).get("artifacts", {}).get("output") or ""))
    if not output_path.is_file():
        _update_episode_journal(episode_dir, status="fallback_attention")
        return {**result, "ok": False, "status": "attachment_fallback_output_missing"}
    if contract.action_authority not in WRITE_AUTHORITIES:
        completed = {**result, "output_path": str(output_path)}
        acceptance_path = episode_dir / "acceptance.json"
        acceptance_sha = _write_json(acceptance_path, completed)
        _update_episode_journal(
            episode_dir,
            status="complete",
            acceptance={"path": str(acceptance_path), "sha256": acceptance_sha},
        )
        return completed
    output_text = output_path.read_text(encoding="utf-8", errors="strict")
    patch = FALLBACK.parse_patch_envelope(output_text, contract)
    _record_mutation(
        fallback_run,
        status="intent",
        evidence={
            "contract_sha256": contract.contract_sha256,
            "output_path": str(output_path),
            "output_sha256": RUNNER.STATE.sha256_file(output_path),
        },
    )
    try:
        return _persist_accept_and_finalize_patch(
            episode_dir=episode_dir,
            contract=contract,
            patch=patch,
            before_snapshot=before_snapshot,
            fallback_run=fallback_run,
            output_path=output_path,
            result_base=result,
        )
    except Exception as exc:
        _record_mutation(
            fallback_run,
            status="partial",
            evidence={"code": _exception_code(exc), "message": str(exc)},
        )
        raise


def execute_with_automatic_fallback(
    compiled: dict[str, Any],
    *,
    fallback_contract_path: Path | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    manifest_path = Path(str(compiled["oracle_manifest_path"])).resolve(strict=True)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("transport") != "devspace":
        return RUNNER.execute_run(manifest_path, dry_run=dry_run)
    existing_episode = _episode_dir(str(manifest.get("run_id") or ""))
    if not dry_run and existing_episode.is_dir():
        return _resume_episode(existing_episode)
    if not dry_run:
        project_root = Path(str(manifest["project_root"])).resolve(strict=True)
        active = _other_active_episode(project_root, str(manifest.get("run_id") or ""))
        if active is not None:
            return {
                "ok": False,
                "status": "project_dispatch_episode_active",
                "active_episode": active,
                "no_resubmission": True,
            }
        with RUNNER.STATE.project_submit_mutex(project_root, timeout_seconds=30):
            wal_recovery = FALLBACK.recover_orphaned_patch_transactions(project_root)
        if wal_recovery.get("count"):
            return {
                "ok": False,
                "status": "orphaned_patch_transaction_recovered",
                "wal_recovery": wal_recovery,
                "no_submission": True,
            }
    contract = _prepare_fallback_contract(compiled, manifest, fallback_contract_path)
    if dry_run:
        preview = RUNNER.execute_run(manifest_path, dry_run=True)
        return {
            **preview,
            "fallback_contract": {
                "schema": FALLBACK.CONTRACT_SCHEMA,
                "sha256": contract.contract_sha256,
                "action_authority": contract.action_authority,
                "reasoning_level": contract.reasoning_level,
            },
        }
    episode = _create_dispatch_episode(
        compiled=compiled,
        base_manifest_path=manifest_path,
        manifest=manifest,
        contract=contract,
    )
    episode_dir = Path(str(episode["episode_dir"]))
    if episode.get("existing"):
        return _resume_episode(episode_dir)
    before_snapshot = episode["before_snapshot"]
    runtime_manifest_path = Path(str(episode["runtime_manifest_path"]))
    primary: dict[str, Any] | None = None
    primary_exception: Exception | None = None
    try:
        primary = RUNNER.execute_run(runtime_manifest_path)
    except Exception as exc:
        primary_exception = exc
    if primary is not None and primary.get("ok"):
        acceptance = _host_verify_primary(primary, contract, before_snapshot)
        result = {
            **primary,
            "ok": bool(acceptance.get("accepted")),
            "status": "complete" if acceptance.get("accepted") else "host_verification_failed",
            "host_acceptance": acceptance,
        }
        return _save_episode_acceptance(episode_dir, result, complete=bool(result["ok"]))
    if primary_exception is not None:
        failure_code = _exception_code(primary_exception)
        failure = {
            "ok": False,
            "status": "primary_exception",
            "error": {
                "code": failure_code,
                "message": str(primary_exception),
                "evidence": getattr(primary_exception, "evidence", {}),
            },
        }
        eligible = failure_code == "DEVSPACE_EXACT_ROOT_UNAVAILABLE"
    else:
        assert primary is not None
        proof = _connector_failure_proof(primary)
        failure_code = str((proof or {}).get("code") or "")
        failure = primary
        eligible = proof is not None
    if failure_code not in ELIGIBLE_DEVSPACE_FALLBACK_CODES or not eligible:
        if primary_exception is not None:
            raise primary_exception
        return primary
    after_snapshot = FALLBACK.snapshot_workspace(contract.project_root)
    mutation_delta = FALLBACK.compare_workspace_snapshots(before_snapshot, after_snapshot, declared_paths=())
    if mutation_delta["changes"]:
        blocked = {
            "ok": False,
            "status": "attachment_fallback_blocked_workspace_changed",
            "primary": failure,
            "mutation_delta": mutation_delta,
        }
        return _save_episode_acceptance(episode_dir, blocked, complete=False)
    FALLBACK.revalidate_contract(contract)
    return _launch_attachment_fallback(
        compiled=compiled,
        primary_manifest_path=manifest_path,
        primary_failure=failure,
        failure_code=failure_code,
        contract=contract,
        before_snapshot=before_snapshot,
        after_snapshot=after_snapshot,
        episode_dir=episode_dir,
    )


def compile_manifest(
    *,
    mode: str,
    project_root: Path,
    mission_path: Path | None,
    output_path: Path,
    reasoning_level: str | None = None,
    attachment_paths: Iterable[Path] | None = None,
    app_name: str | None = None,
    action_authority: str | None = None,
) -> dict[str, Any]:
    contract = PROFILES.build_launch_contract(
        mode,
        mission_path=mission_path,
        reasoning_level=reasoning_level,
        attachment_paths=list(attachment_paths or ()),
        app_name=app_name,
    )
    result = {"ok": True, "contract": contract, "oracle_manifest_path": None}
    if not contract["oracle_launch"]:
        return result
    root = project_root.expanduser().resolve(strict=True)
    target = output_path.expanduser().resolve()
    manifest_parent_durability = RUNNER.STATE.ensure_directory_durable(target.parent)
    manifest: dict[str, Any] = {
        "schema": RUNNER.STATE.SCHEMA,
        "project_root": str(root),
        "mission_path": contract["mission_path"],
        "mode": "browser",
        "task_kind": contract["task_kind"],
        "action_authority": (
            str(action_authority).strip().casefold()
            if action_authority
            else DEFAULT_ACTION_AUTHORITY.get(contract["task_kind"], "read-only")
        ),
        "transport": {
            "oracle-attachment-only": "attachment-only",
            "oracle-pro-attachment-only": "pro-attachment-only",
            "oracle-pro-devspace-readonly": "pro-devspace-readonly",
            "oracle-devspace": "devspace",
        }[contract["route"]],
        "model": contract.get("model") or "gpt-5.6",
        "model_strategy": "select",
        "thinking_time": contract["thinking_time"],
        "research": "deep" if contract["research"] else "off",
        "archive": "auto",
    }
    if contract["route"] in {"oracle-attachment-only", "oracle-pro-attachment-only"}:
        manifest["attachments"] = contract["attachments"]
        manifest["task_outcome_contract"] = "legacy"
    else:
        manifest["app_name"] = contract["app_name"]
        manifest["task_outcome_contract"] = "v1"
    identity = {
        "manifest_output": str(target),
        "project_root": manifest["project_root"],
        "mission_path": manifest["mission_path"],
        "mission_sha256": RUNNER.STATE.sha256_file(Path(str(manifest["mission_path"]))),
        "task_kind": manifest["task_kind"],
        "action_authority": manifest["action_authority"],
        "transport": manifest["transport"],
        "app_name": manifest.get("app_name"),
        "model": manifest["model"],
        "thinking_time": manifest["thinking_time"],
        "research": manifest["research"],
        "task_outcome_contract": manifest.get("task_outcome_contract"),
        "attachments": [
            {"path": str(Path(value).resolve()), "sha256": RUNNER.STATE.sha256_file(Path(value).resolve())}
            for value in manifest.get("attachments", [])
        ],
    }
    manifest["run_id"] = "dispatch-" + hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:32]
    manifest_durability = RUNNER.STATE.write_json_atomic_durable(target, manifest)
    if manifest_durability.get("durable") is not True:
        raise OracleDispatchError(
            "MANIFEST_DURABILITY_FAILED",
            "Oracle manifest was not durably persisted",
            {"path": str(target), "durability": manifest_durability},
        )
    result["oracle_manifest_path"] = str(target)
    result["power_loss_durability"] = {
        "manifest_parent": manifest_parent_durability,
        "manifest": manifest_durability,
    }
    return result


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Resolve a GPT mode and dispatch it to Oracle + DevSpace.")
    parser.add_argument("--mode")
    parser.add_argument("--project-root", type=Path)
    parser.add_argument("--mission-path", type=Path)
    parser.add_argument("--manifest-output", type=Path)
    parser.add_argument(
        "--resume-run",
        type=Path,
        help="resume host acceptance for an already recovered exact dispatch run; never submits",
    )
    parser.add_argument("--reasoning-level")
    parser.add_argument("--attachment", type=Path, action="append", default=[])
    parser.add_argument("--app-name")
    parser.add_argument(
        "--fallback-contract",
        type=Path,
        help="mission-bound attachment fallback and host verification contract",
    )
    parser.add_argument(
        "--action-authority",
        choices=("read-only", "workspace-write", "mission-owned-adaptive-execution"),
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.resume_run is not None:
            if any((args.mode, args.project_root, args.mission_path, args.manifest_output, args.fallback_contract, args.attachment)):
                raise OracleDispatchError(
                    "RESUME_ARGUMENT_CONFLICT",
                    "--resume-run cannot be combined with submission arguments",
                )
            value = resume_dispatch_run(args.resume_run)
            print(json.dumps(value, ensure_ascii=False, indent=2))
            return 0 if value.get("ok") else 1
        if not args.mode or args.project_root is None or args.manifest_output is None:
            raise OracleDispatchError(
                "SUBMISSION_ARGUMENTS_REQUIRED",
                "--mode, --project-root, and --manifest-output are required for a new run",
            )
        compiled = compile_manifest(
            mode=args.mode,
            project_root=args.project_root,
            mission_path=args.mission_path,
            output_path=args.manifest_output,
            reasoning_level=args.reasoning_level,
            attachment_paths=args.attachment,
            app_name=args.app_name,
            action_authority=args.action_authority,
        )
        if compiled["oracle_manifest_path"]:
            run = execute_with_automatic_fallback(
                compiled,
                fallback_contract_path=args.fallback_contract,
                dry_run=args.dry_run,
            )
            value = {**compiled, "run": run, "ok": bool(run.get("ok"))}
        else:
            value = compiled
    except Exception as exc:
        value = {
            "ok": False,
            "error": {
                "code": str(getattr(exc, "code", "") or "ORACLE_DISPATCH_FAILED"),
                "message": str(exc),
                "evidence": getattr(exc, "evidence", {}),
            },
        }
    print(json.dumps(value, ensure_ascii=False, indent=2))
    return 0 if value.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
