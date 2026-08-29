from __future__ import annotations

import hashlib
import ctypes
import ctypes.util
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest


PATH = Path(__file__).resolve().parents[1] / "bin" / "chatgpt_oracle_fallback.py"


def _gate_executable() -> str:
    current = Path(sys.executable)
    try:
        info = os.lstat(current)
        reparse = bool(
            getattr(info, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        )
        if stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode) and not reparse:
            return str(current)
    except OSError:
        pass
    candidate = Path(sys.base_prefix) / ("python.exe" if os.name == "nt" else "bin/python3")
    assert candidate.is_file()
    return str(candidate)


GATE_EXECUTABLE = _gate_executable()


def load():
    spec = importlib.util.spec_from_file_location("oracle_fallback_test", PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def module():
    return load()


def sha(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def limits(**overrides: int) -> dict[str, int]:
    value = {
        "max_evidence_files": 16,
        "max_evidence_file_bytes": 100_000,
        "max_evidence_total_bytes": 500_000,
        "max_patch_operations": 16,
        "max_patch_file_bytes": 100_000,
        "max_patch_total_bytes": 500_000,
        "local_gate_timeout_seconds": 10,
    }
    value.update(overrides)
    return value


def make_contract_value(
    root: Path,
    *,
    authority: str = "workspace-write",
    evidence: list[dict[str, object]] | None = None,
    edits: list[dict[str, object]] | None = None,
    gate: list[str] | None | object = ...,
    reasoning: str = "Extra High",
    limit_overrides: dict[str, int] | None = None,
) -> dict[str, object]:
    mission = root / "mission.md"
    if not mission.exists():
        mission.write_text("Implement the bounded mission.\n", encoding="utf-8")
    if gate is ...:
        gate = [GATE_EXECUTABLE, "-c", "raise SystemExit(0)"] if authority != "read-only" else None
    return {
        "schema": "codex.chatgpt.oracle-attachment-fallback/v1",
        "project_root": str(root.resolve()),
        "mission_path": str(mission.resolve()),
        "mission_sha256": sha(mission.read_bytes()),
        "action_authority": authority,
        "reasoning_level": reasoning,
        "evidence_allowlist": evidence or [],
        "edit_path_allowlist": edits or [],
        "local_gate_command": gate,
        "limits": limits(**(limit_overrides or {})),
    }


def edit(path: str, before: str | None, *operations: str) -> dict[str, object]:
    return {"path": path, "before_sha256": before, "operations": list(operations)}


def patch_value(module, contract, operations: list[dict[str, object]], **overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema": module.PATCH_SCHEMA,
        "contract_sha256": contract.contract_sha256,
        "mission_sha256": contract.mission_sha256,
        "reasoning_level": contract.reasoning_level,
        "operations": operations,
    }
    value.update(overrides)
    return value


def envelope(module, value: dict[str, object]) -> str:
    return (
        module.PATCH_BEGIN_MARKER
        + "\n"
        + json.dumps(value, ensure_ascii=False, sort_keys=True)
        + "\n"
        + module.PATCH_END_MARKER
    )


def add_operation(path: str, content: str) -> dict[str, object]:
    return {
        "op": "add",
        "path": path,
        "before_sha256": None,
        "content": content,
        "after_sha256": sha(content),
    }


def update_operation(path: str, before: str, content: str) -> dict[str, object]:
    return {
        "op": "update",
        "path": path,
        "before_sha256": before,
        "content": content,
        "after_sha256": sha(content),
    }


def test_load_contract_binds_hashes_sorts_evidence_and_preserves_reasoning(module, tmp_path: Path) -> None:
    source_a = tmp_path / "a.py"
    source_b = tmp_path / "b.py"
    source_a.write_text("print('a')\n", encoding="utf-8")
    source_b.write_text("print('b')\n", encoding="utf-8")
    value = make_contract_value(
        tmp_path,
        evidence=[
            {"path": "b.py", "category": "tests", "priority": 20, "sha256": sha(source_b.read_bytes())},
            {"path": "a.py", "category": "source", "priority": 10, "sha256": sha(source_a.read_bytes())},
        ],
        edits=[edit("result.py", None, "add")],
        reasoning="Power 4/5 - Extra High",
    )
    contract_path = tmp_path / "fallback.json"
    contract_path.write_text(json.dumps(value), encoding="utf-8")

    contract = module.load_contract(contract_path)
    request = module.build_attachment_request(contract)

    assert [item.path for item in contract.evidence_allowlist] == ["a.py", "b.py"]
    assert request["reasoning_level"] == "Power 4/5 - Extra High"
    assert "reasoning_level: Power 4/5 - Extra High" in request["instructions"]
    assert "Return exactly one patch envelope and no text before or after it." in request["instructions"]
    assert request["attachments"] == [str((tmp_path / "mission.md").resolve()), str(source_a), str(source_b)]
    assert len(contract.contract_sha256) == 64


def test_read_only_contract_has_no_edit_gate_or_patch_output(module, tmp_path: Path) -> None:
    contract = module.validate_contract(make_contract_value(tmp_path, authority="read-only"))

    request = module.build_attachment_request(contract)

    assert request["action_authority"] == "read-only"
    assert module.PATCH_BEGIN_MARKER not in request["instructions"]
    with pytest.raises(module.FallbackContractError, match="PATCH_WRITE_AUTHORITY_REQUIRED"):
        module.parse_patch_envelope(envelope(module, patch_value(module, contract, [add_operation("x", "x")])), contract)


@pytest.mark.parametrize(
    "bad_path",
    ["../outside.py", "/absolute.py", "C:/escape.py", "nested\\escape.py", "./not-canonical.py"],
)
def test_path_escape_and_noncanonical_paths_are_rejected(module, tmp_path: Path, bad_path: str) -> None:
    value = make_contract_value(tmp_path, edits=[edit(bad_path, None, "add")])

    with pytest.raises(module.FallbackContractError, match="PATH_ESCAPE|RELATIVE_PATH_INVALID|RELATIVE_PATH_NOT_CANONICAL"):
        module.validate_contract(value)


@pytest.mark.parametrize(
    "bad_path",
    [
        "report.txt:secret-stream",
        "nested/control\x1f.txt",
        "trailing./file.txt",
        "trailing /file.txt",
        "CON",
        "con.txt",
        "nested/AUX.json",
        "COM1.log",
        "lpt9",
        "COM\u00b9.txt",
    ],
)
def test_windows_ambiguous_relative_paths_are_rejected_on_every_host(
    module, tmp_path: Path, bad_path: str
) -> None:
    with pytest.raises(module.FallbackContractError, match="RELATIVE_PATH_INVALID"):
        module.validate_contract(make_contract_value(tmp_path, edits=[edit(bad_path, None, "add")]))


def test_mission_and_evidence_inputs_cannot_also_be_edit_paths(module, tmp_path: Path) -> None:
    mission = tmp_path / "mission.md"
    mission.write_text("Implement the bounded mission.\n", encoding="utf-8")
    evidence = tmp_path / "evidence.txt"
    evidence.write_text("immutable evidence\n", encoding="utf-8")
    evidence_entry = {
        "path": "evidence.txt",
        "category": "source",
        "priority": 1,
        "sha256": sha(evidence.read_bytes()),
    }

    with pytest.raises(module.FallbackContractError, match="IMMUTABLE_INPUT_EDIT_OVERLAP"):
        module.validate_contract(
            make_contract_value(tmp_path, edits=[edit("mission.md", sha(mission.read_bytes()), "update")])
        )
    with pytest.raises(module.FallbackContractError, match="IMMUTABLE_INPUT_EDIT_OVERLAP"):
        module.validate_contract(
            make_contract_value(
                tmp_path,
                evidence=[evidence_entry],
                edits=[edit("evidence.txt", sha(evidence.read_bytes()), "update")],
            )
        )


def test_symlink_evidence_is_rejected(module, tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "target.txt"
    link = tmp_path / "link.txt"
    target.write_text("ordinary evidence\n", encoding="utf-8")
    link.write_text("ordinary evidence\n", encoding="utf-8")
    real_lstat = module.os.lstat

    def symlink_lstat(path: str | os.PathLike[str]):
        result = real_lstat(path)
        if Path(path) == link:
            values = list(result)
            values[stat.ST_MODE] = stat.S_IFLNK | 0o777
            return os.stat_result(values)
        return result

    monkeypatch.setattr(module.os, "lstat", symlink_lstat)
    value = make_contract_value(
        tmp_path,
        evidence=[{"path": "link.txt", "category": "source", "priority": 1, "sha256": sha(target.read_bytes())}],
        edits=[edit("out.txt", None, "add")],
    )

    with pytest.raises(module.FallbackContractError, match="REGULAR_FILE_REQUIRED"):
        module.validate_contract(value)


@pytest.mark.parametrize("unsafe_path", [".env", ".ssh/id_rsa", "cache/data.txt", "profiles/Default/Cookies"])
def test_unsafe_secret_profile_and_cache_paths_are_rejected(module, tmp_path: Path, unsafe_path: str) -> None:
    value = make_contract_value(tmp_path, edits=[edit(unsafe_path, None, "add")])

    with pytest.raises(module.FallbackContractError, match="UNSAFE_PATH"):
        module.validate_contract(value)


def test_unsafe_secret_content_is_rejected(module, tmp_path: Path) -> None:
    source = tmp_path / "settings.txt"
    source.write_text("api_key = 'realvalue123456789'\n", encoding="utf-8")
    value = make_contract_value(
        tmp_path,
        evidence=[{"path": "settings.txt", "category": "config", "priority": 1, "sha256": sha(source.read_bytes())}],
        edits=[edit("out.txt", None, "add")],
    )

    with pytest.raises(module.FallbackContractError, match="SECRET_CONTENT_REJECTED"):
        module.validate_contract(value)


@pytest.mark.parametrize(
    "content",
    [
        '{\n  "api_key": "realvalue123456789"\n}\n',
        "'client_secret': 'realvalue123456789'\n",
        'export OPENAI_API_KEY="realvalue123456789"\n',
        'AWS_SECRET_ACCESS_KEY: "realvalue123456789"\n',
    ],
)
def test_common_quoted_json_yaml_and_env_secret_assignments_are_rejected(
    module, tmp_path: Path, content: str
) -> None:
    source = tmp_path / "settings.txt"
    source.write_text(content, encoding="utf-8")
    with pytest.raises(module.FallbackContractError, match="SECRET_CONTENT_REJECTED"):
        module.validate_contract(
            make_contract_value(
                tmp_path,
                evidence=[
                    {
                        "path": "settings.txt",
                        "category": "config",
                        "priority": 1,
                        "sha256": sha(source.read_bytes()),
                    }
                ],
                edits=[edit("out.txt", None, "add")],
            )
        )


@pytest.mark.parametrize(
    "content",
    [
        "Authorization: Bearer bearer-secret-value-123456789\n",
        'curl -H "Authorization: Bearer bearer-secret-value-123456789" https://example.test\n',
        "DATABASE_URL=postgresql://service:database-password-123@example.test/db\n",
        "//registry.npmjs.org/:_authToken=npm-token-value-123456789\n",
        "endpoint=https://example.test/api?access_token=query-token-value-123456789\n",
    ],
)
def test_common_header_url_and_npm_credentials_are_rejected(
    module, tmp_path: Path, content: str
) -> None:
    source = tmp_path / "settings.txt"
    source.write_text(content, encoding="utf-8")

    with pytest.raises(module.FallbackContractError, match="SECRET_CONTENT_REJECTED"):
        module.validate_contract(
            make_contract_value(
                tmp_path,
                evidence=[
                    {
                        "path": "settings.txt",
                        "category": "config",
                        "priority": 1,
                        "sha256": sha(source.read_bytes()),
                    }
                ],
                edits=[edit("out.txt", None, "add")],
            )
        )


@pytest.mark.parametrize(
    "content",
    [
        "Authorization: Bearer ${SERVICE_TOKEN}\n",
        "DATABASE_URL=postgresql://service:${DATABASE_PASSWORD}@example.test/db\n",
        "//registry.npmjs.org/:_authToken=${NPM_TOKEN}\n",
    ],
)
def test_header_url_and_npm_secret_placeholders_remain_allowed(
    module, tmp_path: Path, content: str
) -> None:
    source = tmp_path / "settings.txt"
    source.write_text(content, encoding="utf-8")

    contract = module.validate_contract(
        make_contract_value(
            tmp_path,
            evidence=[
                {
                    "path": "settings.txt",
                    "category": "config",
                    "priority": 1,
                    "sha256": sha(source.read_bytes()),
                }
            ],
            edits=[edit("out.txt", None, "add")],
        )
    )

    assert contract.evidence_allowlist[0].path == "settings.txt"


def test_secret_placeholders_remain_allowed_when_the_path_is_explicitly_allowlisted(
    module, tmp_path: Path
) -> None:
    source = tmp_path / "settings.txt"
    source.write_text('{\n  "api_key": "${OPENAI_API_KEY}"\n}\n', encoding="utf-8")

    contract = module.validate_contract(
        make_contract_value(
            tmp_path,
            evidence=[
                {
                    "path": "settings.txt",
                    "category": "config",
                    "priority": 1,
                    "sha256": sha(source.read_bytes()),
                }
            ],
            edits=[edit("out.txt", None, "add")],
        )
    )

    assert contract.evidence_allowlist[0].path == "settings.txt"


def test_evidence_hash_and_size_caps_are_enforced(module, tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    source.write_text("four", encoding="utf-8")
    stale = make_contract_value(
        tmp_path,
        evidence=[{"path": "source.txt", "category": "source", "priority": 1, "sha256": "0" * 64}],
        edits=[edit("out.txt", None, "add")],
    )
    with pytest.raises(module.FallbackContractError, match="EVIDENCE_HASH_MISMATCH"):
        module.validate_contract(stale)

    too_large = make_contract_value(
        tmp_path,
        evidence=[{"path": "source.txt", "category": "source", "priority": 1, "sha256": sha(source.read_bytes())}],
        edits=[edit("out.txt", None, "add")],
        limit_overrides={"max_evidence_file_bytes": 3},
    )
    with pytest.raises(module.FallbackContractError, match="FILE_SIZE_LIMIT_EXCEEDED"):
        module.validate_contract(too_large)


def test_mission_attachment_uses_the_one_mib_hard_cap(module, tmp_path: Path) -> None:
    mission = tmp_path / "mission.md"
    mission.write_bytes(b"m" * 1_048_577)
    value = make_contract_value(tmp_path, edits=[edit("out.txt", None, "add")])

    with pytest.raises(module.FallbackContractError, match="FILE_SIZE_LIMIT_EXCEEDED"):
        module.validate_contract(value)


def test_write_authority_requires_exactly_one_explicit_gate_command(module, tmp_path: Path) -> None:
    with pytest.raises(module.FallbackContractError, match="LOCAL_GATE_COMMAND_REQUIRED"):
        module.validate_contract(make_contract_value(tmp_path, edits=[edit("out.txt", None, "add")], gate=None))
    with pytest.raises(module.FallbackContractError, match="LOCAL_GATE_COMMAND_INVALID"):
        module.validate_contract(make_contract_value(tmp_path, edits=[edit("out.txt", None, "add")], gate="pytest"))


def test_contract_json_duplicate_keys_are_rejected(module, tmp_path: Path) -> None:
    value = make_contract_value(tmp_path, edits=[edit("out.txt", None, "add")])
    text = json.dumps(value)
    schema_field = '"schema": "codex.chatgpt.oracle-attachment-fallback/v1",'
    text = text.replace(schema_field, schema_field + schema_field, 1)
    path = tmp_path / "fallback.json"
    path.write_text(text, encoding="utf-8")

    with pytest.raises(module.FallbackContractError, match="JSON_DUPLICATE_KEY"):
        module.load_contract(path)


def test_exact_envelope_rejects_malformed_surrounding_and_multiple_envelopes(module, tmp_path: Path) -> None:
    contract = module.validate_contract(make_contract_value(tmp_path, edits=[edit("out.txt", None, "add")]))
    valid = envelope(module, patch_value(module, contract, [add_operation("out.txt", "ok\n")]))

    assert module.parse_patch_envelope(valid, contract)["operations"][0]["path"] == "out.txt"
    with pytest.raises(module.FallbackContractError, match="PATCH_JSON_INVALID"):
        module.parse_patch_envelope(
            module.PATCH_BEGIN_MARKER + "\n{bad\n" + module.PATCH_END_MARKER, contract
        )
    with pytest.raises(module.FallbackContractError, match="PATCH_ENVELOPE_SURROUNDING_TEXT"):
        module.parse_patch_envelope("preface\n" + valid, contract)
    with pytest.raises(module.FallbackContractError, match="PATCH_ENVELOPE_COUNT_INVALID"):
        module.parse_patch_envelope(valid + "\n" + valid, contract)


def test_patch_binding_reasoning_allowlist_and_expected_hashes_are_exact(module, tmp_path: Path) -> None:
    current = tmp_path / "current.txt"
    current.write_text("before\n", encoding="utf-8")
    before = sha(current.read_bytes())
    contract = module.validate_contract(
        make_contract_value(tmp_path, edits=[edit("current.txt", before, "update")], reasoning="Extra High")
    )
    operation = update_operation("current.txt", before, "after\n")

    with pytest.raises(module.FallbackContractError, match="PATCH_CONTRACT_HASH_MISMATCH"):
        module.parse_patch_envelope(
            envelope(module, patch_value(module, contract, [operation], contract_sha256="0" * 64)), contract
        )
    with pytest.raises(module.FallbackContractError, match="PATCH_REASONING_LEVEL_MISMATCH"):
        module.parse_patch_envelope(
            envelope(module, patch_value(module, contract, [operation], reasoning_level="High")), contract
        )
    stale = dict(operation)
    stale["before_sha256"] = "0" * 64
    with pytest.raises(module.FallbackContractError, match="PATCH_BEFORE_HASH_MISMATCH"):
        module.parse_patch_envelope(envelope(module, patch_value(module, contract, [stale])), contract)
    undeclared = update_operation("undeclared.txt", before, "after\n")
    with pytest.raises(module.FallbackContractError, match="PATCH_PATH_NOT_ALLOWED"):
        module.parse_patch_envelope(envelope(module, patch_value(module, contract, [undeclared])), contract)


def test_patch_utf8_content_and_content_hash_are_validated(module, tmp_path: Path) -> None:
    contract = module.validate_contract(make_contract_value(tmp_path, edits=[edit("out.txt", None, "add")]))
    bad_hash = add_operation("out.txt", "safe\n")
    bad_hash["after_sha256"] = "0" * 64
    with pytest.raises(module.FallbackContractError, match="PATCH_AFTER_HASH_MISMATCH"):
        module.parse_patch_envelope(envelope(module, patch_value(module, contract, [bad_hash])), contract)

    invalid_utf8 = {
        "op": "add",
        "path": "out.txt",
        "before_sha256": None,
        "content": "\ud800",
        "after_sha256": "0" * 64,
    }
    with pytest.raises(module.FallbackContractError, match="PATCH_OUTPUT_UTF8_INVALID"):
        module.parse_patch_envelope(envelope(module, patch_value(module, contract, [invalid_utf8])), contract)


def test_workspace_snapshots_detect_declared_and_undeclared_mutation(module, tmp_path: Path) -> None:
    declared = tmp_path / "declared.txt"
    declared.write_text("before\n", encoding="utf-8")
    before = module.snapshot_workspace(tmp_path)
    declared.write_text("after\n", encoding="utf-8")
    (tmp_path / "rogue.txt").write_text("rogue\n", encoding="utf-8")
    after = module.snapshot_workspace(tmp_path)

    delta = module.compare_workspace_snapshots(before, after, declared_paths=["declared.txt"])

    assert [item["path"] for item in delta["declared_changes"]] == ["declared.txt"]
    assert [item["path"] for item in delta["undeclared_changes"]] == ["rogue.txt"]
    assert delta["eligible"] is False


def test_snapshot_structural_counters_are_part_of_validation(module, tmp_path: Path) -> None:
    (tmp_path / "source.txt").write_text("source\n", encoding="utf-8")
    snapshot = module.snapshot_workspace(tmp_path)
    snapshot["file_bytes"] += 1

    with pytest.raises(module.FallbackContractError, match="SNAPSHOT_FILE_BYTES_MISMATCH"):
        module.compare_workspace_snapshots(snapshot, snapshot)


def test_snapshot_fails_closed_on_any_symlink(module, tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "target.txt"
    target.write_text("target\n", encoding="utf-8")
    real_lstat = module.os.lstat

    def simulate_symlink(path: str | os.PathLike[str]) -> os.stat_result:
        info = real_lstat(path)
        if Path(path) == target:
            values = list(info)
            values[stat.ST_MODE] = stat.S_IFLNK | 0o777
            return os.stat_result(values)
        return info

    monkeypatch.setattr(module.os, "lstat", simulate_symlink)

    with pytest.raises(module.FallbackContractError, match="SNAPSHOT_LINK_FORBIDDEN"):
        module.snapshot_workspace(tmp_path)


def test_snapshot_fails_closed_on_any_reparse_point(module, tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "target.txt"
    target.write_text("target\n", encoding="utf-8")
    target_inode = os.lstat(target).st_ino
    real_is_reparse = module._is_reparse_stat

    def simulate_reparse(info: os.stat_result) -> bool:
        return real_is_reparse(info) or (
            stat.S_ISREG(info.st_mode) and info.st_ino == target_inode
        )

    monkeypatch.setattr(module, "_is_reparse_stat", simulate_reparse)

    with pytest.raises(module.FallbackContractError, match="SNAPSHOT_REPARSE_POINT_FORBIDDEN"):
        module.snapshot_workspace(tmp_path)


def test_apply_add_update_runs_shell_false_gate_and_returns_hashes(module, tmp_path: Path) -> None:
    current = tmp_path / "current.txt"
    current.write_text("before\n", encoding="utf-8")
    before_hash = sha(current.read_bytes())
    contract = module.validate_contract(
        make_contract_value(
            tmp_path,
            edits=[edit("current.txt", before_hash, "update"), edit("nested/new.txt", None, "add")],
            gate=[GATE_EXECUTABLE, "-c", "print('gate-ok')"],
        )
    )
    patch = patch_value(
        module,
        contract,
        [update_operation("current.txt", before_hash, "after\n"), add_operation("nested/new.txt", "new\n")],
    )

    result = module.apply_patch_envelope(contract, patch)

    assert current.read_text(encoding="utf-8") == "after\n"
    assert (tmp_path / "nested" / "new.txt").read_text(encoding="utf-8") == "new\n"
    assert result["gate"]["shell"] is False
    assert result["gate"]["executable_sha256"] == sha(Path(GATE_EXECUTABLE).read_bytes())
    assert result["gate"]["exit_code"] == 0
    assert result["gate"]["stdout_sha256"] in {sha(b"gate-ok\n"), sha(b"gate-ok\r\n")}
    assert result["gate"]["output_included"] is False
    assert result["fallback_eligible"] is True
    assert result["total_delta"]["undeclared_changes"] == []


def test_stale_hash_between_parse_and_apply_is_rejected_without_write(module, tmp_path: Path) -> None:
    current = tmp_path / "current.txt"
    current.write_text("before\n", encoding="utf-8")
    before_hash = sha(current.read_bytes())
    contract = module.validate_contract(
        make_contract_value(tmp_path, edits=[edit("current.txt", before_hash, "update")])
    )
    patch = module.parse_patch_envelope(
        envelope(module, patch_value(module, contract, [update_operation("current.txt", before_hash, "after\n")])),
        contract,
    )
    current.write_text("concurrent\n", encoding="utf-8")

    with pytest.raises(module.FallbackContractError, match="EDIT_PATH_HASH_MISMATCH"):
        module.apply_patch_envelope(contract, patch)
    assert current.read_text(encoding="utf-8") == "concurrent\n"


def test_apply_failure_rolls_back_every_touched_file(module, tmp_path: Path, monkeypatch) -> None:
    first = tmp_path / "a.txt"
    second = tmp_path / "b.txt"
    first.write_text("a-before\n", encoding="utf-8")
    second.write_text("b-before\n", encoding="utf-8")
    first_hash = sha(first.read_bytes())
    second_hash = sha(second.read_bytes())
    contract = module.validate_contract(
        make_contract_value(
            tmp_path,
            edits=[edit("a.txt", first_hash, "update"), edit("b.txt", second_hash, "update")],
        )
    )
    patch = patch_value(
        module,
        contract,
        [update_operation("a.txt", first_hash, "a-after\n"), update_operation("b.txt", second_hash, "b-after\n")],
    )
    real_write = module._write_existing_file_in_place
    failed = False

    def fail_second(path: Path, data: bytes, **metadata: object) -> None:
        nonlocal failed
        if path == second and not failed:
            failed = True
            raise OSError("injected apply failure")
        real_write(path, data, **metadata)

    monkeypatch.setattr(module, "_write_existing_file_in_place", fail_second)

    with pytest.raises(module.FallbackContractError, match="PATCH_APPLY_FAILED.*rollback=succeeded"):
        module.apply_patch_envelope(contract, patch)
    assert first.read_text(encoding="utf-8") == "a-before\n"
    assert second.read_text(encoding="utf-8") == "b-before\n"
    assert not list(module._default_transaction_root(tmp_path.resolve()).glob(".codex-oracle-fallback-*"))


def test_process_death_leaves_durable_wal_that_public_recovery_rolls_back(
    module, tmp_path: Path, monkeypatch
) -> None:
    first = tmp_path / "a.txt"
    second = tmp_path / "b.txt"
    first.write_text("a-before\n", encoding="utf-8")
    second.write_text("b-before\n", encoding="utf-8")
    first_hash = sha(first.read_bytes())
    second_hash = sha(second.read_bytes())
    contract = module.validate_contract(
        make_contract_value(
            tmp_path,
            edits=[edit("a.txt", first_hash, "update"), edit("b.txt", second_hash, "update")],
        )
    )
    patch = patch_value(
        module,
        contract,
        [update_operation("a.txt", first_hash, "a-after\n"), update_operation("b.txt", second_hash, "b-after\n")],
    )
    real_write = module._write_existing_file_in_place

    def die_after_first_mutation(path: Path, data: bytes, **metadata: object) -> None:
        real_write(path, data, **metadata)
        if path == first:
            raise KeyboardInterrupt("simulated process death")

    monkeypatch.setattr(module, "_write_existing_file_in_place", die_after_first_mutation)
    with pytest.raises(KeyboardInterrupt, match="simulated process death"):
        module.apply_patch_envelope(contract, patch)

    transactions = list(
        module._default_transaction_root(tmp_path.resolve()).glob(".codex-oracle-fallback-*")
    )
    assert len(transactions) == 1
    journal = json.loads((transactions[0] / "journal.json").read_text(encoding="utf-8"))
    assert journal["phase"] == "applying"
    assert journal["operations"][0]["progress"] == "target-in-place-write-intent"
    assert sorted(path.read_text(encoding="utf-8") for path in (transactions[0] / "backup").glob("*.bak")) == [
        "a-before\n",
        "b-before\n",
    ]
    assert first.read_text(encoding="utf-8") == "a-after\n"
    assert second.read_text(encoding="utf-8") == "b-before\n"

    monkeypatch.setattr(module, "_write_existing_file_in_place", real_write)
    recovery = module.recover_orphaned_patch_transactions(tmp_path)

    assert recovery["count"] == 1
    assert first.read_text(encoding="utf-8") == "a-before\n"
    assert second.read_text(encoding="utf-8") == "b-before\n"
    assert not list(module._default_transaction_root(tmp_path.resolve()).glob(".codex-oracle-fallback-*"))


def test_resume_helper_accepts_committed_postimage_without_reapplying(
    module, tmp_path: Path, monkeypatch
) -> None:
    contract = module.validate_contract(
        make_contract_value(tmp_path, edits=[edit("result.txt", None, "add")])
    )
    patch = patch_value(module, contract, [add_operation("result.txt", "committed\n")])
    real_remove = module._remove_transaction_tree

    def die_before_committed_cleanup(path: Path) -> None:
        raise KeyboardInterrupt("simulated death before receipt")

    monkeypatch.setattr(module, "_remove_transaction_tree", die_before_committed_cleanup)
    with pytest.raises(KeyboardInterrupt, match="simulated death before receipt"):
        module.resume_or_apply_patch_envelope(contract, patch)

    transactions = list(
        module._default_transaction_root(tmp_path.resolve()).glob(".codex-oracle-fallback-*")
    )
    assert len(transactions) == 1
    journal = json.loads((transactions[0] / "journal.json").read_text(encoding="utf-8"))
    assert journal["phase"] == "committed"
    assert (tmp_path / "result.txt").read_text(encoding="utf-8") == "committed\n"

    monkeypatch.setattr(module, "_remove_transaction_tree", real_remove)
    receipt = module.resume_or_apply_patch_envelope(contract, patch)

    assert receipt["applied"] is True
    assert receipt["expected_state_after_gate"]["ok"] is True
    assert receipt["fallback_eligible"] is True
    assert (tmp_path / "result.txt").read_text(encoding="utf-8") == "committed\n"
    assert not list(module._default_transaction_root(tmp_path.resolve()).glob(".codex-oracle-fallback-*"))


def test_resume_uses_persisted_baseline_to_detect_pre_crash_rogue_file(
    module, tmp_path: Path, monkeypatch
) -> None:
    contract = module.validate_contract(
        make_contract_value(tmp_path, edits=[edit("result.txt", None, "add")])
    )
    patch = patch_value(module, contract, [add_operation("result.txt", "committed\n")])
    baseline = module.snapshot_workspace(tmp_path)
    real_remove = module._remove_transaction_tree

    def die_before_committed_cleanup(path: Path) -> None:
        (tmp_path / "rogue.txt").write_text("created before crash\n", encoding="utf-8")
        raise KeyboardInterrupt("simulated death before receipt")

    monkeypatch.setattr(module, "_remove_transaction_tree", die_before_committed_cleanup)
    with pytest.raises(KeyboardInterrupt, match="simulated death before receipt"):
        module.resume_or_apply_patch_envelope(contract, patch, baseline_snapshot=baseline)

    monkeypatch.setattr(module, "_remove_transaction_tree", real_remove)
    receipt = module.resume_or_apply_patch_envelope(
        contract, patch, baseline_snapshot=baseline
    )

    assert receipt["snapshots"]["before"]["sha256"] == baseline["sha256"]
    assert [item["path"] for item in receipt["apply_delta"]["undeclared_changes"]] == [
        "rogue.txt"
    ]
    assert [item["path"] for item in receipt["total_delta"]["undeclared_changes"]] == [
        "rogue.txt"
    ]
    assert receipt["fallback_eligible"] is False


def test_delete_and_move_require_explicit_source_and_destination_authority(module, tmp_path: Path) -> None:
    deleted = tmp_path / "delete.txt"
    source = tmp_path / "source.txt"
    deleted.write_text("delete me\n", encoding="utf-8")
    source.write_text("move me\n", encoding="utf-8")
    deleted_hash = sha(deleted.read_bytes())
    source_hash = sha(source.read_bytes())
    contract = module.validate_contract(
        make_contract_value(
            tmp_path,
            edits=[
                edit("delete.txt", deleted_hash, "delete"),
                edit("source.txt", source_hash, "move"),
                edit("destination.txt", None, "move"),
            ],
        )
    )
    operations = [
        {"op": "delete", "path": "delete.txt", "before_sha256": deleted_hash},
        {
            "op": "move",
            "path": "source.txt",
            "destination": "destination.txt",
            "before_sha256": source_hash,
            "destination_before_sha256": None,
            "after_sha256": source_hash,
        },
    ]

    result = module.apply_patch_envelope(contract, patch_value(module, contract, operations))

    assert not deleted.exists()
    assert not source.exists()
    assert (tmp_path / "destination.txt").read_text(encoding="utf-8") == "move me\n"
    assert result["fallback_eligible"] is True


def test_move_destination_without_move_authority_is_rejected(module, tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    source.write_text("move me\n", encoding="utf-8")
    source_hash = sha(source.read_bytes())
    contract = module.validate_contract(
        make_contract_value(
            tmp_path,
            edits=[edit("source.txt", source_hash, "move"), edit("destination.txt", None, "add")],
        )
    )
    operation = {
        "op": "move",
        "path": "source.txt",
        "destination": "destination.txt",
        "before_sha256": source_hash,
        "destination_before_sha256": None,
        "after_sha256": source_hash,
    }

    with pytest.raises(module.FallbackContractError, match="PATCH_MOVE_DESTINATION_NOT_ALLOWED"):
        module.parse_patch_envelope(envelope(module, patch_value(module, contract, [operation])), contract)


def test_gate_failure_leaves_applied_changes_for_inspection(module, tmp_path: Path) -> None:
    contract = module.validate_contract(
        make_contract_value(
            tmp_path,
            edits=[edit("result.txt", None, "add")],
            gate=[GATE_EXECUTABLE, "-c", "import sys; print('gate-failed'); sys.exit(7)"],
        )
    )

    result = module.apply_patch_envelope(
        contract, patch_value(module, contract, [add_operation("result.txt", "inspect me\n")])
    )

    assert (tmp_path / "result.txt").read_text(encoding="utf-8") == "inspect me\n"
    assert result["applied"] is True
    assert result["gate"]["exit_code"] == 7
    assert result["gate"]["ok"] is False
    assert result["fallback_eligible"] is False


def test_missing_gate_executable_is_rejected_before_any_write(module, tmp_path: Path) -> None:
    missing_executable = tmp_path / "missing-gate-executable.exe"
    with pytest.raises(module.FallbackContractError, match="LOCAL_GATE_EXECUTABLE_PATH_MISSING"):
        module.validate_contract(
            make_contract_value(
                tmp_path,
                edits=[edit("result.txt", None, "add")],
                gate=[str(missing_executable)],
            )
        )
    assert not (tmp_path / "result.txt").exists()


def test_gate_executable_must_be_absolute_and_cannot_alias_an_edit_path(
    module, tmp_path: Path
) -> None:
    with pytest.raises(module.FallbackContractError, match="LOCAL_GATE_EXECUTABLE_PATH_NOT_ABSOLUTE"):
        module.validate_contract(
            make_contract_value(
                tmp_path,
                edits=[edit("result.txt", None, "add")],
                gate=["python", "-c", "raise SystemExit(0)"],
            )
        )

    verifier = tmp_path / "verify.py"
    verifier.write_text("raise SystemExit(0)\n", encoding="utf-8")
    with pytest.raises(module.FallbackContractError, match="LOCAL_GATE_EDIT_PATH_OVERLAP"):
        module.validate_contract(
            make_contract_value(
                tmp_path,
                edits=[edit("verify.py", sha(verifier.read_bytes()), "update")],
                gate=[GATE_EXECUTABLE, str(verifier)],
            )
        )


def test_gate_undeclared_mutation_is_reported_and_disqualifies_result(module, tmp_path: Path) -> None:
    contract = module.validate_contract(
        make_contract_value(
            tmp_path,
            edits=[edit("result.txt", None, "add")],
            gate=[
                GATE_EXECUTABLE,
                "-c",
                "from pathlib import Path; Path('rogue.txt').write_text('rogue\\n', encoding='utf-8')",
            ],
        )
    )

    result = module.apply_patch_envelope(
        contract, patch_value(module, contract, [add_operation("result.txt", "declared\n")])
    )

    assert result["gate"]["exit_code"] == 0
    assert [item["path"] for item in result["gate_delta"]["undeclared_changes"]] == ["rogue.txt"]
    assert result["fallback_eligible"] is False


def test_gate_declared_file_mutation_is_detected_by_expected_final_hash(module, tmp_path: Path) -> None:
    contract = module.validate_contract(
        make_contract_value(
            tmp_path,
            edits=[edit("result.txt", None, "add")],
            gate=[
                GATE_EXECUTABLE,
                "-c",
                "from pathlib import Path; Path('result.txt').write_text('changed\\n', encoding='utf-8')",
            ],
        )
    )

    result = module.apply_patch_envelope(
        contract, patch_value(module, contract, [add_operation("result.txt", "declared\n")])
    )

    assert result["gate"]["exit_code"] == 0
    assert result["expected_state_after_gate"]["ok"] is False
    assert result["fallback_eligible"] is False


def test_direct_devspace_write_acceptance_runs_gate_after_scoped_update(module, tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("before\n", encoding="utf-8")
    before_hash = sha(target.read_bytes())
    contract = module.validate_contract(
        make_contract_value(
            tmp_path,
            edits=[edit("target.txt", before_hash, "update")],
            gate=[GATE_EXECUTABLE, "-c", "print('verified')"],
        )
    )
    before = module.snapshot_workspace(tmp_path)
    target.write_text("after\n", encoding="utf-8")
    after_hash = sha(target.read_bytes())

    receipt = module.verify_direct_devspace_write(contract, before)

    assert receipt["schema"] == module.DIRECT_WRITE_ACCEPTANCE_SCHEMA
    assert receipt["write_scope_ok"] is True
    assert receipt["operation_proof"]["operations"] == [
        {
            "op": "update",
            "path": "target.txt",
            "before_sha256": before_hash,
            "after_sha256": after_hash,
        }
    ]
    assert receipt["gate"]["shell"] is False
    assert receipt["gate"]["exit_code"] == 0
    assert receipt["gate_clean"] is True
    assert receipt["accepted"] is True


def test_direct_devspace_scope_violation_skips_gate(module, tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("before\n", encoding="utf-8")
    before_hash = sha(target.read_bytes())
    contract = module.validate_contract(
        make_contract_value(
            tmp_path,
            edits=[edit("target.txt", before_hash, "update")],
            gate=[
                GATE_EXECUTABLE,
                "-c",
                "from pathlib import Path; Path('gate-ran.txt').write_text('ran', encoding='utf-8')",
            ],
        )
    )
    before = module.snapshot_workspace(tmp_path)
    target.write_text("after\n", encoding="utf-8")
    (tmp_path / "rogue.txt").write_text("rogue\n", encoding="utf-8")

    receipt = module.verify_direct_devspace_write(contract, before)

    assert receipt["write_scope_ok"] is False
    assert receipt["gate"]["skipped"] is True
    assert receipt["gate"]["skip_reason"] == "DIRECT_WRITE_SCOPE_INVALID"
    assert not (tmp_path / "gate-ran.txt").exists()
    assert receipt["accepted"] is False


def test_direct_devspace_no_change_is_not_a_successful_write(module, tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("unchanged\n", encoding="utf-8")
    before_hash = sha(target.read_bytes())
    contract = module.validate_contract(
        make_contract_value(tmp_path, edits=[edit("target.txt", before_hash, "update")])
    )
    before = module.snapshot_workspace(tmp_path)

    receipt = module.verify_direct_devspace_write(contract, before)

    assert receipt["write_scope_ok"] is False
    assert receipt["operation_proof"]["violations"] == [
        {"path": "", "reason": "DIRECT_WRITE_NO_CHANGE"}
    ]
    assert receipt["gate"]["skipped"] is True
    assert receipt["accepted"] is False


def test_direct_devspace_gate_drift_is_rejected(module, tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("before\n", encoding="utf-8")
    before_hash = sha(target.read_bytes())
    contract = module.validate_contract(
        make_contract_value(
            tmp_path,
            edits=[edit("target.txt", before_hash, "update")],
            gate=[
                GATE_EXECUTABLE,
                "-c",
                "from pathlib import Path; Path('gate-drift.txt').write_text('drift', encoding='utf-8')",
            ],
        )
    )
    before = module.snapshot_workspace(tmp_path)
    target.write_text("after\n", encoding="utf-8")

    receipt = module.verify_direct_devspace_write(contract, before)

    assert receipt["write_scope_ok"] is True
    assert receipt["gate"]["exit_code"] == 0
    assert receipt["gate_clean"] is False
    assert [item["path"] for item in receipt["gate_delta"]["undeclared_changes"]] == ["gate-drift.txt"]
    assert receipt["accepted"] is False


def test_direct_devspace_move_requires_paired_move_authority(module, tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    destination = tmp_path / "destination.txt"
    source.write_text("move\n", encoding="utf-8")
    source_hash = sha(source.read_bytes())
    contract = module.validate_contract(
        make_contract_value(
            tmp_path,
            edits=[edit("source.txt", source_hash, "move"), edit("destination.txt", None, "move")],
        )
    )
    before = module.snapshot_workspace(tmp_path)
    source.replace(destination)

    receipt = module.verify_direct_devspace_write(contract, before)

    assert receipt["operation_proof"]["operations"] == [
        {
            "op": "move",
            "path": "source.txt",
            "destination": "destination.txt",
            "before_sha256": source_hash,
            "after_sha256": source_hash,
        }
    ]
    assert receipt["accepted"] is True


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode semantics required")
def test_direct_devspace_rejects_chmod_only_and_update_mode_drift(module, tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("before\n", encoding="utf-8")
    target.chmod(0o640)
    before_hash = sha(target.read_bytes())
    contract = module.validate_contract(
        make_contract_value(tmp_path, edits=[edit("target.txt", before_hash, "update")])
    )

    before = module.snapshot_workspace(tmp_path)
    target.chmod(0o600)
    chmod_only = module.verify_direct_devspace_write(contract, before)
    assert {item["reason"] for item in chmod_only["operation_proof"]["violations"]} == {
        "DIRECT_WRITE_METADATA_ONLY_CHANGE"
    }
    assert chmod_only["accepted"] is False

    target.write_text("after\n", encoding="utf-8")
    content_and_mode = module.verify_direct_devspace_write(contract, before)
    assert "DIRECT_WRITE_MODE_CHANGED" in {
        item["reason"] for item in content_and_mode["operation_proof"]["violations"]
    }
    assert content_and_mode["accepted"] is False


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode semantics required")
def test_direct_devspace_move_requires_matching_sha_and_mode(module, tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    destination = tmp_path / "destination.txt"
    source.write_text("move\n", encoding="utf-8")
    source.chmod(0o700)
    source_hash = sha(source.read_bytes())
    contract = module.validate_contract(
        make_contract_value(
            tmp_path,
            edits=[edit("source.txt", source_hash, "move"), edit("destination.txt", None, "move")],
        )
    )
    before = module.snapshot_workspace(tmp_path)
    source.replace(destination)
    destination.chmod(0o600)

    receipt = module.verify_direct_devspace_write(contract, before)

    assert "DIRECT_WRITE_MOVE_MODE_MISMATCH" in {
        item["reason"] for item in receipt["operation_proof"]["violations"]
    }
    assert receipt["accepted"] is False


@pytest.mark.skipif(os.name != "nt", reason="Windows alternate data streams required")
def test_windows_ads_is_snapshotted_and_preserved_by_update_move_delete(
    module, tmp_path: Path
) -> None:
    updated = tmp_path / "updated.txt"
    moved = tmp_path / "moved.txt"
    deleted = tmp_path / "deleted.txt"
    for path, content in (
        (updated, "before\n"),
        (moved, "move\n"),
        (deleted, "delete\n"),
    ):
        path.write_text(content, encoding="utf-8")
        Path(str(path) + ":codex-proof").write_bytes((path.name + "-ads").encode("utf-8"))

    initial = module.snapshot_workspace(tmp_path)
    initial_map = {item["path"]: item for item in initial["entries"]}
    assert any(
        stream["name"] == ":codex-proof:$DATA"
        for stream in initial_map["updated.txt"]["metadata"]["streams"]
    )
    Path(str(updated) + ":codex-proof").write_bytes(b"changed-ads-only")
    changed = module.snapshot_workspace(tmp_path)
    delta = module.compare_workspace_snapshots(initial, changed, declared_paths=())
    assert [item["path"] for item in delta["undeclared_changes"]] == ["updated.txt"]
    Path(str(updated) + ":codex-proof").write_bytes(b"updated.txt-ads")

    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_attributes = kernel32.GetFileAttributesW
    get_attributes.argtypes = [wintypes.LPCWSTR]
    get_attributes.restype = wintypes.DWORD
    set_attributes = kernel32.SetFileAttributesW
    set_attributes.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
    set_attributes.restype = wintypes.BOOL
    original_attributes = int(get_attributes(str(updated)))
    assert set_attributes(str(updated), original_attributes | 0x1 | 0x2)

    updated_hash = sha(updated.read_bytes())
    moved_hash = sha(moved.read_bytes())
    deleted_hash = sha(deleted.read_bytes())
    updated_identity = os.lstat(updated).st_ino
    moved_identity = os.lstat(moved).st_ino
    contract = module.validate_contract(
        make_contract_value(
            tmp_path,
            edits=[
                edit("updated.txt", updated_hash, "update"),
                edit("moved.txt", moved_hash, "move"),
                edit("destination.txt", None, "move"),
                edit("deleted.txt", deleted_hash, "delete"),
            ],
        )
    )
    expected_updated_metadata = dict(
        next(item for item in contract.edit_path_allowlist if item.path == "updated.txt").before_metadata
    )
    expected_moved_metadata = dict(
        next(item for item in contract.edit_path_allowlist if item.path == "moved.txt").before_metadata
    )
    operations = [
        update_operation("updated.txt", updated_hash, "after\n"),
        {
            "op": "move",
            "path": "moved.txt",
            "destination": "destination.txt",
            "before_sha256": moved_hash,
            "destination_before_sha256": None,
            "after_sha256": moved_hash,
        },
        {"op": "delete", "path": "deleted.txt", "before_sha256": deleted_hash},
    ]

    receipt = module.apply_patch_envelope(
        contract, patch_value(module, contract, operations)
    )

    assert Path(str(updated) + ":codex-proof").read_bytes() == b"updated.txt-ads"
    assert Path(str(tmp_path / "destination.txt") + ":codex-proof").read_bytes() == b"moved.txt-ads"
    assert os.lstat(updated).st_ino == updated_identity
    assert os.lstat(tmp_path / "destination.txt").st_ino == moved_identity
    final = module.snapshot_workspace(tmp_path)
    final_map = {item["path"]: item for item in final["entries"]}
    assert final_map["updated.txt"]["metadata"] == expected_updated_metadata
    assert final_map["destination.txt"]["metadata"] == expected_moved_metadata
    assert not deleted.exists()
    assert receipt["fallback_eligible"] is True
    assert set_attributes(str(updated), int(get_attributes(str(updated))) & ~0x1 & ~0x2)


@pytest.mark.skipif(os.name != "nt", reason="Windows alternate data streams required")
def test_windows_ads_survives_transaction_rollback(module, tmp_path: Path, monkeypatch) -> None:
    first = tmp_path / "a.txt"
    second = tmp_path / "b.txt"
    first.write_text("a-before\n", encoding="utf-8")
    second.write_text("b-before\n", encoding="utf-8")
    Path(str(first) + ":codex-proof").write_bytes(b"first-ads")
    Path(str(second) + ":codex-proof").write_bytes(b"second-ads")
    first_hash = sha(first.read_bytes())
    second_hash = sha(second.read_bytes())
    contract = module.validate_contract(
        make_contract_value(
            tmp_path,
            edits=[edit("a.txt", first_hash, "update"), edit("b.txt", second_hash, "update")],
        )
    )
    patch = patch_value(
        module,
        contract,
        [update_operation("a.txt", first_hash, "a-after\n"), update_operation("b.txt", second_hash, "b-after\n")],
    )
    real_write = module._write_existing_file_in_place

    def fail_second(path: Path, data: bytes, **metadata: object) -> None:
        if path == second:
            raise OSError("injected apply failure")
        real_write(path, data, **metadata)

    monkeypatch.setattr(module, "_write_existing_file_in_place", fail_second)
    with pytest.raises(module.FallbackContractError, match="rollback=succeeded"):
        module.apply_patch_envelope(contract, patch)

    assert first.read_text(encoding="utf-8") == "a-before\n"
    assert second.read_text(encoding="utf-8") == "b-before\n"
    assert Path(str(first) + ":codex-proof").read_bytes() == b"first-ads"
    assert Path(str(second) + ":codex-proof").read_bytes() == b"second-ads"


@pytest.mark.skipif(os.name != "nt", reason="Windows alternate data streams required")
def test_windows_delete_and_move_rollback_restore_original_file_identity_and_ads(
    module, tmp_path: Path, monkeypatch
) -> None:
    deleted = tmp_path / "a-delete.txt"
    moved = tmp_path / "b-move.txt"
    failed = tmp_path / "z-fail.txt"
    for path, content in ((deleted, "delete\n"), (moved, "move\n"), (failed, "before\n")):
        path.write_text(content, encoding="utf-8")
        Path(str(path) + ":codex-proof").write_bytes((path.name + "-ads").encode())
    deleted_identity = os.lstat(deleted).st_ino
    moved_identity = os.lstat(moved).st_ino
    deleted_hash = sha(deleted.read_bytes())
    moved_hash = sha(moved.read_bytes())
    failed_hash = sha(failed.read_bytes())
    contract = module.validate_contract(
        make_contract_value(
            tmp_path,
            edits=[
                edit("a-delete.txt", deleted_hash, "delete"),
                edit("b-move.txt", moved_hash, "move"),
                edit("destination.txt", None, "move"),
                edit("z-fail.txt", failed_hash, "update"),
            ],
        )
    )
    operations = [
        {"op": "delete", "path": "a-delete.txt", "before_sha256": deleted_hash},
        {
            "op": "move",
            "path": "b-move.txt",
            "destination": "destination.txt",
            "before_sha256": moved_hash,
            "destination_before_sha256": None,
            "after_sha256": moved_hash,
        },
        update_operation("z-fail.txt", failed_hash, "after\n"),
    ]
    real_write = module._write_existing_file_in_place

    def fail_last(path: Path, data: bytes, **metadata: object) -> None:
        if path == failed:
            raise OSError("injected apply failure")
        real_write(path, data, **metadata)

    monkeypatch.setattr(module, "_write_existing_file_in_place", fail_last)
    with pytest.raises(module.FallbackContractError, match="rollback=succeeded"):
        module.apply_patch_envelope(contract, patch_value(module, contract, operations))

    assert os.lstat(deleted).st_ino == deleted_identity
    assert os.lstat(moved).st_ino == moved_identity
    assert Path(str(deleted) + ":codex-proof").read_bytes() == b"a-delete.txt-ads"
    assert Path(str(moved) + ":codex-proof").read_bytes() == b"b-move.txt-ads"
    assert not (tmp_path / "destination.txt").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX uid/gid/xattr semantics required")
def test_posix_uid_gid_and_xattrs_are_snapshotted_and_preserved(
    module, tmp_path: Path
) -> None:
    if not hasattr(os, "setxattr"):
        pytest.skip("xattr API unavailable")
    updated = tmp_path / "updated.txt"
    moved = tmp_path / "moved.txt"
    updated.write_text("before\n", encoding="utf-8")
    moved.write_text("move\n", encoding="utf-8")
    try:
        os.setxattr(updated, "user.codex-proof", b"updated-xattr")
        os.setxattr(moved, "user.codex-proof", b"moved-xattr")
    except OSError as exc:
        pytest.skip(f"xattrs unavailable: {exc}")

    before = module.snapshot_workspace(tmp_path)
    before_map = {item["path"]: item for item in before["entries"]}
    updated_metadata = before_map["updated.txt"]["metadata"]
    assert updated_metadata["uid"] == os.lstat(updated).st_uid
    assert updated_metadata["gid"] == os.lstat(updated).st_gid
    assert [item["name"] for item in updated_metadata["xattrs"]] == ["user.codex-proof"]
    os.setxattr(updated, "user.codex-proof", b"changed-xattr")
    changed = module.snapshot_workspace(tmp_path)
    delta = module.compare_workspace_snapshots(before, changed, declared_paths=())
    assert [item["path"] for item in delta["undeclared_changes"]] == ["updated.txt"]
    os.setxattr(updated, "user.codex-proof", b"updated-xattr")

    updated_hash = sha(updated.read_bytes())
    moved_hash = sha(moved.read_bytes())
    contract = module.validate_contract(
        make_contract_value(
            tmp_path,
            edits=[
                edit("updated.txt", updated_hash, "update"),
                edit("moved.txt", moved_hash, "move"),
                edit("destination.txt", None, "move"),
            ],
        )
    )
    operations = [
        update_operation("updated.txt", updated_hash, "after\n"),
        {
            "op": "move",
            "path": "moved.txt",
            "destination": "destination.txt",
            "before_sha256": moved_hash,
            "destination_before_sha256": None,
            "after_sha256": moved_hash,
        },
    ]

    receipt = module.apply_patch_envelope(
        contract, patch_value(module, contract, operations)
    )

    assert os.getxattr(updated, "user.codex-proof") == b"updated-xattr"
    assert os.getxattr(tmp_path / "destination.txt", "user.codex-proof") == b"moved-xattr"
    assert receipt["fallback_eligible"] is True


@pytest.mark.skipif(os.name == "nt", reason="POSIX ACL xattr semantics required")
def test_posix_acl_bytes_are_snapshotted_and_preserved(module, tmp_path: Path) -> None:
    library = ctypes.util.find_library("acl")
    if not library:
        pytest.skip("libacl unavailable")
    target = tmp_path / "acl.txt"
    target.write_text("before\n", encoding="utf-8")
    acl = ctypes.CDLL(library, use_errno=True)
    acl.acl_from_text.argtypes = [ctypes.c_char_p]
    acl.acl_from_text.restype = ctypes.c_void_p
    acl.acl_set_file.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p]
    acl.acl_set_file.restype = ctypes.c_int
    acl.acl_free.argtypes = [ctypes.c_void_p]
    acl.acl_free.restype = ctypes.c_int
    descriptor = acl.acl_from_text(b"u::rw-,u:0:r--,g::r--,m::r--,o::---")
    if not descriptor:
        pytest.skip("acl_from_text unavailable")
    try:
        if acl.acl_set_file(os.fsencode(target), 0x8000, descriptor) != 0:
            pytest.skip(f"acl_set_file unavailable: errno={ctypes.get_errno()}")
    finally:
        acl.acl_free(descriptor)

    before_acl = os.getxattr(target, "system.posix_acl_access", follow_symlinks=False)
    snapshot = module.snapshot_workspace(tmp_path)
    entry = next(item for item in snapshot["entries"] if item["path"] == "acl.txt")
    acl_entry = next(
        item for item in entry["metadata"]["xattrs"] if item["name"] == "system.posix_acl_access"
    )
    assert acl_entry["sha256"] == sha(before_acl)

    before_hash = sha(target.read_bytes())
    contract = module.validate_contract(
        make_contract_value(tmp_path, edits=[edit("acl.txt", before_hash, "update")])
    )
    receipt = module.apply_patch_envelope(
        contract,
        patch_value(module, contract, [update_operation("acl.txt", before_hash, "after\n")]),
    )

    assert os.getxattr(target, "system.posix_acl_access", follow_symlinks=False) == before_acl
    assert receipt["fallback_eligible"] is True


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode semantics required")
def test_patch_update_and_move_preserve_and_verify_source_modes(module, tmp_path: Path) -> None:
    updated = tmp_path / "updated.sh"
    moved = tmp_path / "moved.sh"
    updated.write_text("before\n", encoding="utf-8")
    moved.write_text("move\n", encoding="utf-8")
    updated.chmod(0o751)
    moved.chmod(0o740)
    updated_hash = sha(updated.read_bytes())
    moved_hash = sha(moved.read_bytes())
    contract = module.validate_contract(
        make_contract_value(
            tmp_path,
            edits=[
                edit("updated.sh", updated_hash, "update"),
                edit("moved.sh", moved_hash, "move"),
                edit("destination.sh", None, "move"),
            ],
        )
    )
    operations = [
        update_operation("updated.sh", updated_hash, "after\n"),
        {
            "op": "move",
            "path": "moved.sh",
            "destination": "destination.sh",
            "before_sha256": moved_hash,
            "destination_before_sha256": None,
            "after_sha256": moved_hash,
        },
    ]

    receipt = module.apply_patch_envelope(contract, patch_value(module, contract, operations))

    assert stat.S_IMODE(os.lstat(updated).st_mode) == 0o751
    assert stat.S_IMODE(os.lstat(tmp_path / "destination.sh").st_mode) == 0o740
    modes = {
        item["path"]: (item["expected_mode"], item["actual_mode"])
        for item in receipt["expected_state_after_gate"]["paths"]
    }
    assert modes["updated.sh"] == (0o751, 0o751)
    assert modes["destination.sh"] == (0o740, 0o740)


@pytest.mark.skipif(os.name != "nt", reason="Windows alternate data streams required")
def test_resume_rejects_gate_added_ads_using_durable_add_metadata(
    module, tmp_path: Path, monkeypatch
) -> None:
    contract = module.validate_contract(
        make_contract_value(
            tmp_path,
            edits=[edit("result.txt", None, "add")],
            gate=[
                GATE_EXECUTABLE,
                "-c",
                "from pathlib import Path; Path('result.txt:gate-proof').write_bytes(b'gate')",
            ],
        )
    )
    patch = patch_value(module, contract, [add_operation("result.txt", "result\n")])
    baseline = module.snapshot_workspace(tmp_path)
    real_snapshot = module.snapshot_workspace
    crashed = False

    def crash_after_gate(project_root: Path, **kwargs: object) -> dict[str, object]:
        nonlocal crashed
        ads = tmp_path / "result.txt:gate-proof"
        if ads.exists() and not crashed:
            crashed = True
            raise KeyboardInterrupt("crash after gate metadata drift")
        return real_snapshot(project_root, **kwargs)

    monkeypatch.setattr(module, "snapshot_workspace", crash_after_gate)
    with pytest.raises(KeyboardInterrupt, match="crash after gate metadata drift"):
        module.resume_or_apply_patch_envelope(
            contract, patch, baseline_snapshot=baseline
        )
    monkeypatch.setattr(module, "snapshot_workspace", real_snapshot)
    with pytest.raises(
        module.FallbackContractError,
        match="TRANSACTION_RECOVERY_CONFLICT|PATCH_WORKSPACE_STATE_CONFLICT",
    ):
        module.resume_or_apply_patch_envelope(
            contract, patch, baseline_snapshot=baseline
        )


def test_gate_cannot_delete_host_sealed_transaction_or_rollback_evidence(
    module, tmp_path: Path, monkeypatch
) -> None:
    sealed_root = tmp_path.parent / f"{tmp_path.name}-sealed-transactions"
    monkeypatch.setenv("CODEX_ORACLE_STATE_ROOT", str(sealed_root.parent))
    contract = module.validate_contract(
        make_contract_value(
            tmp_path,
            edits=[edit("result.txt", None, "add")],
            gate=[
                GATE_EXECUTABLE,
                "-c",
                "from pathlib import Path; import os, shutil; "
                "root=os.environ.get('CODEX_ORACLE_STATE_ROOT'); "
                "[shutil.rmtree(p) for p in (Path(root).rglob('.codex-oracle-fallback-*') if root else [])]; "
                "[shutil.rmtree(p) for p in Path('.').glob('.codex-oracle-fallback-*')]",
            ],
        )
    )
    patch = patch_value(module, contract, [add_operation("result.txt", "result\n")])

    receipt = module.resume_or_apply_patch_envelope(
        contract,
        patch,
        baseline_snapshot=module.snapshot_workspace(tmp_path),
        transaction_root=sealed_root,
        retain_prepared_transaction=True,
    )

    assert receipt["fallback_eligible"] is True
    assert receipt["gate"]["environment_sanitized"] is True
    assert not list(tmp_path.glob(".codex-oracle-fallback-*"))
    transaction = sealed_root / receipt["prepared_transaction"]["transaction_id"]
    assert transaction.is_dir()
    assert (transaction / "backup").is_dir()
    assert (transaction / "journal.json").is_file()
    assert list(sealed_root.glob("oracle-fallback-seal-*.json"))


def test_dirty_sealed_control_inventory_is_never_adopted_on_resume(
    module, tmp_path: Path
) -> None:
    sealed_root = tmp_path.parent / f"{tmp_path.name}-sealed-transactions"
    target = tmp_path / "target.txt"
    target.write_text("before\n", encoding="utf-8")
    before_hash = sha(target.read_bytes())
    contract = module.validate_contract(
        make_contract_value(tmp_path, edits=[edit("target.txt", before_hash, "update")])
    )
    patch = patch_value(
        module, contract, [update_operation("target.txt", before_hash, "after\n")]
    )
    baseline = module.snapshot_workspace(tmp_path)
    receipt = module.resume_or_apply_patch_envelope(
        contract,
        patch,
        baseline_snapshot=baseline,
        transaction_root=sealed_root,
        retain_prepared_transaction=True,
    )
    transaction = sealed_root / receipt["prepared_transaction"]["transaction_id"]
    backup = next((transaction / "backup").glob("*.bak"))
    backup.write_bytes(b"forged control\n")
    journal_path = transaction / "journal.json"
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    journal["control_inventory"] = module._control_inventory(transaction)
    module._journal_write(transaction, journal)

    with pytest.raises(
        module.FallbackContractError,
        match="TRANSACTION_CONTROL_SEAL_INVALID|TRANSACTION_CONTROL_INVENTORY_MISMATCH",
    ):
        module.resume_or_apply_patch_envelope(
            contract,
            patch,
            baseline_snapshot=baseline,
            transaction_root=sealed_root,
            retain_prepared_transaction=True,
        )

@pytest.mark.skipif(os.name != "nt", reason="Windows alternate data streams required")
def test_large_ads_restore_bytes_are_externalized_and_recovery_reads_them_bounded(
    module, tmp_path: Path, monkeypatch
) -> None:
    target = tmp_path / "target.txt"
    target.write_text("before\n", encoding="utf-8")
    ads = Path(str(target) + ":large-proof")
    payload = b"m" * 3_200_000
    ads.write_bytes(payload)
    before_hash = sha(target.read_bytes())
    contract = module.validate_contract(
        make_contract_value(
            tmp_path,
            edits=[edit("target.txt", before_hash, "update")],
            limit_overrides={
                "max_patch_file_bytes": 4_194_304,
                "max_patch_total_bytes": 16_777_216,
            },
        )
    )
    patch = patch_value(
        module, contract, [update_operation("target.txt", before_hash, "after\n")]
    )
    real_write = module._write_existing_file_in_place

    def die_after_write(path: Path, data: bytes, **metadata: object) -> None:
        real_write(path, data, **metadata)
        raise KeyboardInterrupt("crash with externalized metadata")

    monkeypatch.setattr(module, "_write_existing_file_in_place", die_after_write)
    with pytest.raises(KeyboardInterrupt, match="externalized metadata"):
        module.apply_patch_envelope(contract, patch)

    transaction = next(
        module._default_transaction_root(tmp_path.resolve()).glob(".codex-oracle-fallback-*")
    )
    journal = transaction / "journal.json"
    assert journal.stat().st_size <= module.TRANSACTION_JOURNAL_MAX_BYTES
    blobs = list((transaction / "metadata").glob("*.blob"))
    assert blobs
    assert all(blob.stat().st_size <= contract.limits.max_patch_file_bytes for blob in blobs)

    monkeypatch.setattr(module, "_write_existing_file_in_place", real_write)
    recovery = module.recover_orphaned_patch_transactions(tmp_path)
    assert recovery["count"] == 1
    assert target.read_text(encoding="utf-8") == "before\n"
    assert ads.read_bytes() == payload


@pytest.mark.skipif(os.name != "nt", reason="Windows read-only attributes required")
def test_recovery_converges_after_crash_before_readonly_metadata_restore(
    module, tmp_path: Path, monkeypatch
) -> None:
    from ctypes import wintypes

    target = tmp_path / "readonly.txt"
    target.write_text("before\n", encoding="utf-8")
    Path(str(target) + ":proof").write_bytes(b"preserve")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_attributes = kernel32.GetFileAttributesW
    get_attributes.argtypes = [wintypes.LPCWSTR]
    get_attributes.restype = wintypes.DWORD
    set_attributes = kernel32.SetFileAttributesW
    set_attributes.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
    set_attributes.restype = wintypes.BOOL
    original_attributes = int(get_attributes(str(target))) | 0x1
    assert set_attributes(str(target), original_attributes)
    before_hash = sha(target.read_bytes())
    contract = module.validate_contract(
        make_contract_value(tmp_path, edits=[edit("readonly.txt", before_hash, "update")])
    )
    patch = patch_value(
        module, contract, [update_operation("readonly.txt", before_hash, "after\n")]
    )
    real_restore = module._restore_file_metadata
    crashed = False

    def crash_before_restore(*args: object, **kwargs: object) -> None:
        nonlocal crashed
        if not crashed:
            crashed = True
            raise KeyboardInterrupt("crash before metadata restore")
        real_restore(*args, **kwargs)

    monkeypatch.setattr(module, "_restore_file_metadata", crash_before_restore)
    with pytest.raises(KeyboardInterrupt, match="crash before metadata restore"):
        module.apply_patch_envelope(contract, patch)
    assert int(get_attributes(str(target))) & 0x1 == 0

    monkeypatch.setattr(module, "_restore_file_metadata", real_restore)
    recovery = module.recover_orphaned_patch_transactions(tmp_path)
    assert recovery["count"] == 1
    assert target.read_text(encoding="utf-8") == "before\n"
    assert Path(str(target) + ":proof").read_bytes() == b"preserve"
    assert int(get_attributes(str(target))) == original_attributes
    assert set_attributes(str(target), original_attributes & ~0x1)


@pytest.mark.skipif(os.name == "nt", reason="POSIX owner-write mode semantics required")
def test_posix_recovery_converges_after_crash_before_mode_and_xattr_restore(
    module, tmp_path: Path, monkeypatch
) -> None:
    target = tmp_path / "protected.txt"
    target.write_text("before\n", encoding="utf-8")
    try:
        os.setxattr(target, "user.codex-proof", b"preserve")
    except OSError as exc:
        pytest.skip(f"xattrs unavailable: {exc}")
    target.chmod(0o400)
    before_hash = sha(target.read_bytes())
    contract = module.validate_contract(
        make_contract_value(tmp_path, edits=[edit("protected.txt", before_hash, "update")])
    )
    patch = patch_value(
        module, contract, [update_operation("protected.txt", before_hash, "after\n")]
    )
    real_restore = module._restore_file_metadata
    crashed = False

    def crash_before_restore(*args: object, **kwargs: object) -> None:
        nonlocal crashed
        if not crashed:
            crashed = True
            raise KeyboardInterrupt("crash before POSIX metadata restore")
        real_restore(*args, **kwargs)

    monkeypatch.setattr(module, "_restore_file_metadata", crash_before_restore)
    with pytest.raises(KeyboardInterrupt, match="POSIX metadata restore"):
        module.apply_patch_envelope(contract, patch)
    assert stat.S_IMODE(os.lstat(target).st_mode) == 0o600

    monkeypatch.setattr(module, "_restore_file_metadata", real_restore)
    recovery = module.recover_orphaned_patch_transactions(tmp_path)
    assert recovery["count"] == 1
    assert target.read_text(encoding="utf-8") == "before\n"
    assert stat.S_IMODE(os.lstat(target).st_mode) == 0o400
    assert os.getxattr(target, "user.codex-proof") == b"preserve"
    target.chmod(0o600)


@pytest.mark.skipif(os.name != "nt", reason="Windows inherited DACL semantics required")
def test_windows_add_inherits_target_parent_dacl(module, tmp_path: Path) -> None:
    parent = tmp_path / "nested"
    parent.mkdir()
    user = os.environ.get("USERNAME")
    if not user:
        pytest.skip("USERNAME unavailable")
    result = subprocess.run(
        ["icacls", str(parent), "/inheritance:r", "/grant:r", f"{user}:(OI)(CI)F"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip(f"icacls unavailable: {result.stderr}")
    control = parent / "control.txt"
    control.write_text("control\n", encoding="utf-8")
    control_metadata, _ = module._filesystem_metadata(control, os.lstat(control))
    contract = module.validate_contract(
        make_contract_value(tmp_path, edits=[edit("nested/result.txt", None, "add")])
    )

    receipt = module.apply_patch_envelope(
        contract,
        patch_value(module, contract, [add_operation("nested/result.txt", "result\n")]),
    )

    added = parent / "result.txt"
    added_metadata, _ = module._filesystem_metadata(added, os.lstat(added))
    assert added_metadata["owner_dacl"] == control_metadata["owner_dacl"]
    assert receipt["fallback_eligible"] is True


def test_add_parent_stage_crash_is_recovered_without_target_or_orphan(
    module, tmp_path: Path, monkeypatch
) -> None:
    parent = tmp_path / "nested"
    parent.mkdir()
    contract = module.validate_contract(
        make_contract_value(tmp_path, edits=[edit("nested/result.txt", None, "add")])
    )
    patch = patch_value(module, contract, [add_operation("nested/result.txt", "result\n")])
    real_write = module._write_new_file_exclusive

    def die_after_parent_stage(path: Path, data: bytes) -> None:
        real_write(path, data)
        raise KeyboardInterrupt("crash after inherited stage")

    monkeypatch.setattr(module, "_write_new_file_exclusive", die_after_parent_stage)
    with pytest.raises(KeyboardInterrupt, match="inherited stage"):
        module.apply_patch_envelope(contract, patch)
    assert list(parent.glob(".codex-oracle-add-*.tmp"))

    monkeypatch.setattr(module, "_write_new_file_exclusive", real_write)
    recovery = module.recover_orphaned_patch_transactions(tmp_path)
    assert recovery["count"] == 1
    assert not (parent / "result.txt").exists()
    assert not list(parent.glob(".codex-oracle-add-*.tmp"))


@pytest.mark.skipif(os.name == "nt", reason="POSIX default ACL semantics required")
def test_posix_add_inherits_target_parent_default_acl(module, tmp_path: Path) -> None:
    library = ctypes.util.find_library("acl")
    if not library:
        pytest.skip("libacl unavailable")
    parent = tmp_path / "nested"
    parent.mkdir()
    acl = ctypes.CDLL(library, use_errno=True)
    acl.acl_from_text.argtypes = [ctypes.c_char_p]
    acl.acl_from_text.restype = ctypes.c_void_p
    acl.acl_set_file.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p]
    acl.acl_set_file.restype = ctypes.c_int
    acl.acl_free.argtypes = [ctypes.c_void_p]
    acl.acl_free.restype = ctypes.c_int
    descriptor = acl.acl_from_text(b"u::rwx,u:0:r-x,g::---,m::r-x,o::---")
    if not descriptor:
        pytest.skip("acl_from_text unavailable")
    try:
        if acl.acl_set_file(os.fsencode(parent), 0x4000, descriptor) != 0:
            pytest.skip(f"acl_set_file unavailable: errno={ctypes.get_errno()}")
    finally:
        acl.acl_free(descriptor)
    control = parent / "control.txt"
    control.write_text("control\n", encoding="utf-8")
    expected_acl = os.getxattr(control, "system.posix_acl_access", follow_symlinks=False)
    contract = module.validate_contract(
        make_contract_value(tmp_path, edits=[edit("nested/result.txt", None, "add")])
    )

    receipt = module.apply_patch_envelope(
        contract,
        patch_value(module, contract, [add_operation("nested/result.txt", "result\n")]),
    )

    assert (
        os.getxattr(
            tmp_path / "nested" / "result.txt",
            "system.posix_acl_access",
            follow_symlinks=False,
        )
        == expected_acl
    )
    assert receipt["fallback_eligible"] is True


@pytest.mark.parametrize(
    "content",
    [
        "url=https://example.test/path?client_secret=client-secret-value-123456789\n",
        "url=https://example.test/path?refresh_token=refresh-token-value-123456789\n",
        "endpoint=https://generic-token-value-123456789@example.test/path\n",
    ],
)
def test_additional_query_and_url_userinfo_secrets_are_rejected(
    module, tmp_path: Path, content: str
) -> None:
    source = tmp_path / "settings.txt"
    source.write_text(content, encoding="utf-8")
    with pytest.raises(module.FallbackContractError, match="SECRET_CONTENT_REJECTED"):
        module.validate_contract(
            make_contract_value(
                tmp_path,
                evidence=[
                    {
                        "path": "settings.txt",
                        "category": "config",
                        "priority": 1,
                        "sha256": sha(source.read_bytes()),
                    }
                ],
                edits=[edit("out.txt", None, "add")],
            )
        )


@pytest.mark.parametrize(
    "content",
    [
        "url=https://example.test/path?client_secret=${CLIENT_SECRET}\n",
        "url=https://example.test/path?refresh_token={{REFRESH_TOKEN}}\n",
        "endpoint=https://${GENERIC_TOKEN}@example.test/path\n",
    ],
)
def test_additional_query_and_url_userinfo_placeholders_remain_allowed(
    module, tmp_path: Path, content: str
) -> None:
    source = tmp_path / "settings.txt"
    source.write_text(content, encoding="utf-8")
    contract = module.validate_contract(
        make_contract_value(
            tmp_path,
            evidence=[
                {
                    "path": "settings.txt",
                    "category": "config",
                    "priority": 1,
                    "sha256": sha(source.read_bytes()),
                }
            ],
            edits=[edit("out.txt", None, "add")],
        )
    )
    assert contract.evidence_allowlist[0].path == "settings.txt"
