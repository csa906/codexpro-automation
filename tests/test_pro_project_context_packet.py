from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import zipfile

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills" / "chatgpt-pro-browser" / "scripts" / "build_project_context_packet.py"
SPEC = importlib.util.spec_from_file_location("pro_packet", SCRIPT)
assert SPEC and SPEC.loader
packet = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(packet)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_manifest(project: Path, evidence: list[tuple[str, str, int]], **overrides: object) -> Path:
    values = []
    for relative, category, priority in evidence:
        source = project / relative
        values.append({"path": str(source.resolve()), "category": category, "priority": priority, "sha256": digest(source)})
    manifest = {
        "schema": packet.SCHEMA,
        "project_root": str(project.resolve()),
        "question": "Decide whether the project can safely proceed.",
        "required_categories": ["rules", "state"],
        "local_transport_envelope_bytes": packet.TOTAL_ENVELOPE_BYTES,
        "answer_headroom_bytes": packet.ANSWER_HEADROOM_BYTES,
        "metadata_reserve_bytes": packet.METADATA_RESERVE_BYTES,
        "packet_path": str((project / ".ai-bridge" / "packet.zip").resolve()),
        "evidence": values,
    }
    manifest.update(overrides)
    path = project / "context.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def fixture_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    (project / "AGENTS.md").write_text("rules", encoding="utf-8")
    (project / "state.json").write_text('{"state":"current"}', encoding="utf-8")
    (project / "support.md").write_text("supporting evidence" * 100, encoding="utf-8")
    return project


def test_builder_maximum_fill_is_deterministic_and_validates(tmp_path: Path) -> None:
    project = fixture_project(tmp_path)
    manifest = write_manifest(project, [("AGENTS.md", "rules", 0), ("state.json", "state", 1), ("support.md", "state", 2)])
    first = packet.build(manifest)
    archive = Path(first["project_root"]) / ".ai-bridge" / "packet.zip"
    first_bytes = archive.read_bytes()
    assert [item["relative_path"] for item in first["included"]] == ["AGENTS.md", "state.json", "support.md"]
    assert first["local_transport_envelope"]["total_budget_bytes"] == packet.TOTAL_ENVELOPE_BYTES
    assert first["local_transport_envelope"]["answer_headroom_bytes"] == packet.ANSWER_HEADROOM_BYTES
    assert first["local_transport_envelope"]["metadata_reserve_bytes"] == packet.METADATA_RESERVE_BYTES
    assert first["collection"] == "explicit manifest allowlist; no recursive project scan"
    assert packet.validate(manifest)["packet_sha256"] == digest(archive)
    second = packet.build(manifest)
    assert archive.read_bytes() == first_bytes
    assert second["included"] == first["included"]


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda project, manifest: manifest["evidence"][1].update({"path": str((project.parent / "outside.md").resolve())}), "ROOT_ESCAPE"),
        (lambda project, manifest: manifest["evidence"][0].update({"sha256": "0" * 64}), "STALE_HASH"),
        (lambda project, manifest: manifest.update({"required_categories": ["rules", "state", "missing"]}), "REQUIRED_CATEGORY_MISSING"),
        (lambda project, manifest: manifest.update({"local_transport_envelope_bytes": 100, "answer_headroom_bytes": 20, "metadata_reserve_bytes": 20}), "BUDGET_PROFILE_MISMATCH"),
    ],
)
def test_builder_fails_closed_for_root_hash_category_and_budget(tmp_path: Path, mutate, code: str) -> None:
    project = fixture_project(tmp_path)
    manifest_path = write_manifest(project, [("AGENTS.md", "rules", 0), ("state.json", "state", 1)])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    mutate(project, manifest)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(packet.PacketError, match=code):
        packet.build(manifest_path)


@pytest.mark.parametrize("unsafe", [".env", "runtime/live.log", "data.sqlite", "cookies.json", "live_trading_state.json", "live_positions.json", "account_balances.json"])
def test_builder_rejects_secrets_volatile_and_mutable_state(tmp_path: Path, unsafe: str) -> None:
    project = fixture_project(tmp_path)
    source = project / unsafe
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("API_KEY=not-a-real-secret" if unsafe == ".env" else "unsafe", encoding="utf-8")
    manifest = write_manifest(project, [("AGENTS.md", "rules", 0), (unsafe, "state", 1)])
    with pytest.raises(packet.PacketError, match="UNSAFE_EVIDENCE"):
        packet.build(manifest)


def test_builder_rejects_duplicate_and_validates_stale_packet(tmp_path: Path) -> None:
    project = fixture_project(tmp_path)
    manifest = write_manifest(project, [("AGENTS.md", "rules", 0), ("state.json", "state", 1), ("state.json", "state", 2)])
    with pytest.raises(packet.PacketError, match="DUPLICATE_EVIDENCE"):
        packet.build(manifest)
    manifest = write_manifest(project, [("AGENTS.md", "rules", 0), ("state.json", "state", 1)])
    result = packet.build(manifest)
    archive = Path(result["project_root"]) / ".ai-bridge" / "packet.zip"
    archive.write_bytes(archive.read_bytes() + b"tampered")
    with pytest.raises(packet.PacketError, match="STALE_PACKET_HASH"):
        packet.validate(manifest)


@pytest.mark.parametrize("collision", [".ai-bridge/packet.zip", ".ai-bridge/packet.zip.index.json"])
def test_builder_rejects_packet_or_receipt_overwriting_evidence(tmp_path: Path, collision: str) -> None:
    project = fixture_project(tmp_path)
    source = project / collision
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("frozen evidence", encoding="utf-8")
    manifest = write_manifest(project, [("AGENTS.md", "rules", 0), (collision, "state", 1)])
    with pytest.raises(packet.PacketError, match="OUTPUT_COLLISION"):
        packet.build(manifest)


def test_validate_accepts_priority_skip_followed_by_smaller_included_file(tmp_path: Path, monkeypatch) -> None:
    project = fixture_project(tmp_path)
    monkeypatch.setattr(packet, "EVIDENCE_BUDGET_BYTES", 100)
    (project / "large-rules.md").write_bytes(b"x" * 110)
    (project / "state.json").write_bytes(b"s" * 20)
    (project / "small-rules.md").write_text("rules fallback", encoding="utf-8")
    manifest = write_manifest(project, [
        ("large-rules.md", "rules", 0),
        ("state.json", "state", 1),
        ("small-rules.md", "rules", 2),
    ])
    result = packet.build(manifest)
    assert [item["relative_path"] for item in result["included"]] == ["state.json", "small-rules.md"]
    assert [item["relative_path"] for item in result["omissions"]] == ["large-rules.md"]
    assert packet.validate(manifest)["packet_sha256"]


def test_tested_profile_rejects_tiny_budget_and_single_file_cap_without_large_fixture(tmp_path: Path) -> None:
    with pytest.raises(packet.PacketError, match="BUDGET_PROFILE_MISMATCH"):
        packet._validate_budget_profile(1, 0, 0)
    with pytest.raises(packet.PacketError, match="SINGLE_FILE_CAP_EXCEEDED"):
        packet._validate_file_size(packet.MAX_SINGLE_FILE_BYTES + 1, tmp_path / "oversized.md")


@pytest.mark.parametrize("payload", [b'{"api_key":"not-a-real-secret"}', '{"token":"not-a-real-secret"}'.encode("utf-16-le")])
def test_secret_scan_rejects_json_and_nul_containing_payloads(payload: bytes) -> None:
    with pytest.raises(packet.PacketError, match="UNSAFE_SECRET_CONTENT"):
        packet._safe_bytes_check(payload, "evidence.json")


def test_secret_scan_allows_policy_prose_but_rejects_authorization_secret_forms() -> None:
    packet._safe_bytes_check(
        b"No external account authorization is required for this policy evidence.",
        "AGENTS.md",
    )
    for payload in (
        b"Authorization: Bearer actual-secret-value",
        b'{"authorization":"Bearer actual-secret-value"}',
        b"authorization = actual-secret-value",
    ):
        with pytest.raises(packet.PacketError, match="UNSAFE_SECRET_CONTENT"):
            packet._safe_bytes_check(payload, "evidence.md")


def test_validate_rejects_extra_member_and_replaced_evidence_even_with_updated_receipt(tmp_path: Path) -> None:
    project = fixture_project(tmp_path)
    manifest = write_manifest(project, [("AGENTS.md", "rules", 0), ("state.json", "state", 1)])
    result = packet.build(manifest)
    archive = Path(result["project_root"]) / ".ai-bridge" / "packet.zip"
    with zipfile.ZipFile(archive, "a") as handle:
        handle.writestr("extra.txt", "unexpected")
    with pytest.raises(packet.PacketError, match="ARCHIVE_INVALID"):
        packet.validate(manifest)
    packet.build(manifest)
    with zipfile.ZipFile(archive) as original:
        payloads = {name: original.read(name) for name in original.namelist()}
    payloads["evidence/state.json"] = b'{"state":"replaced"}'
    with zipfile.ZipFile(archive, "w") as replacement:
        for name, value in payloads.items():
            replacement.writestr(name, value)
    receipt = archive.with_suffix(".zip.index.json")
    value = json.loads(receipt.read_text(encoding="utf-8"))
    value["packet_sha256"] = digest(archive)
    receipt.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(packet.PacketError, match="ARCHIVED_EVIDENCE_MISMATCH"):
        packet.validate(manifest)


def test_failed_overcap_build_preserves_existing_packet(tmp_path: Path, monkeypatch) -> None:
    project = fixture_project(tmp_path)
    manifest = write_manifest(project, [("AGENTS.md", "rules", 0), ("state.json", "state", 1)])
    result = packet.build(manifest)
    archive = Path(result["project_root"]) / ".ai-bridge" / "packet.zip"
    before = archive.read_bytes()
    monkeypatch.setattr(packet, "MAX_PACKET_ZIP_BYTES", 1)
    with pytest.raises(packet.PacketError, match="OVERBUDGET"):
        packet.build(manifest)
    assert archive.read_bytes() == before


def test_builder_rejects_symlink_without_following_it(tmp_path: Path) -> None:
    project = fixture_project(tmp_path)
    target = project / "state.json"
    link = project / "link.md"
    try:
        os.symlink(target, link)
    except OSError:
        pytest.skip("symlink creation is unavailable on this Windows test host")
    manifest = write_manifest(project, [("AGENTS.md", "rules", 0), ("link.md", "state", 1)])
    value = json.loads(manifest.read_text(encoding="utf-8"))
    value["evidence"][1]["path"] = str(link.absolute())
    manifest.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(packet.PacketError, match="SYMLINK_FORBIDDEN"):
        packet.build(manifest)
