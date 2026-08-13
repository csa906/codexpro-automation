#!/usr/bin/env python3
"""Build and validate the safe, deterministic project-context attachment for Pro.

The input manifest is an explicit allowlist.  This tool deliberately never walks a
project tree: callers decide which decision-relevant evidence exists and declare
its category, priority, and frozen hash.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any


SCHEMA = "codex.chatgpt.pro-project-context/v1"
LOCAL_PROFILE_ID = "oracle-pro-local-envelope-2026-08-03/v1"
TOTAL_ENVELOPE_BYTES = 64 * 1024 * 1024
ANSWER_HEADROOM_BYTES = 8 * 1024 * 1024
METADATA_RESERVE_BYTES = 1 * 1024 * 1024
EVIDENCE_BUDGET_BYTES = 55 * 1024 * 1024
MAX_SINGLE_FILE_BYTES = 32 * 1024 * 1024
MAX_PACKET_ZIP_BYTES = 32 * 1024 * 1024
INDEX_NAME = "evidence-index.json"
PACKET_NAME = "packet.json"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
UNSAFE_PARTS = {
    ".git", ".cache", "cache", "cookies", "cookie", "profile", "profiles",
    "browser", "browsers", "account", "accounts", "wallet", "wallets",
    "logs", "log", "tmp", "temp", "node_modules",
}
UNSAFE_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".wal", ".shm", ".log", ".pyc"}
UNSAFE_NAMES = {".env", "credentials", "credentials.json", "cookies.json", "login data"}
SECRET_PATTERN = re.compile(
    r"(?im)^\s*[\"']?(?:[A-Z0-9_]*(?:SECRET|TOKEN|PASSWORD|API[_-]?KEY|PRIVATE[_-]?KEY|"
    r"ACCESS[_-]?KEY)[A-Z0-9_]*|authorization)[\"']?\s*(?:=|:)\s*\S+|"
    r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----|\b(?:sk|rk|pk)_[A-Za-z0-9_-]{16,}"
)
JSON_SECRET_PATTERN = re.compile(r"(?i)[\"'](?:[a-z0-9_]*(?:secret|token|password|api[_-]?key|private[_-]?key|access[_-]?key)|authorization)[\"']\s*:\s*[\"']?\S+")


class PacketError(ValueError):
    """A packet cannot safely be attached to a Pro consultation."""


def _error(code: str, detail: str) -> PacketError:
    return PacketError(f"{code}: {detail}")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise _error("MANIFEST_INVALID", str(exc)) from exc
    if not isinstance(value, dict):
        raise _error("MANIFEST_INVALID", "top level must be an object")
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _path_from(value: Any, root: Path, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise _error("PATH_REQUIRED", label)
    path = Path(value)
    if not path.is_absolute():
        raise _error("PATH_ABSOLUTE_REQUIRED", label)
    lexical = Path(os.path.abspath(str(path)))
    if _inside(lexical, root):
        current = root
        for part in lexical.relative_to(root).parts:
            current = current / part
            if current.is_symlink():
                raise _error("SYMLINK_FORBIDDEN", str(current))
    elif path.is_symlink():
        raise _error("SYMLINK_FORBIDDEN", str(path))
    resolved = path.resolve(strict=False)
    if not _inside(resolved, root):
        raise _error("ROOT_ESCAPE", str(path))
    return resolved


def _unsafe_path(path: Path, root: Path) -> str | None:
    relative = path.relative_to(root)
    parts = {part.casefold() for part in relative.parts}
    if parts & UNSAFE_PARTS:
        return "unsafe volatile/profile/account path"
    if path.name.casefold() in UNSAFE_NAMES or path.suffix.casefold() in UNSAFE_SUFFIXES:
        return "unsafe secret, mutable database, or volatile file type"
    if any(word in path.name.casefold() for word in ("credential", "secret", "cookie", "token")):
        return "unsafe credential-like filename"
    if any(word in path.name.casefold() for word in ("live_trading", "live-trading", "live_trade", "live-trade", "account_state")):
        return "unsafe live-trading or account state filename"
    if any(word in path.name.casefold() for word in ("live_positions", "live_orders", "account_balances", "account_portfolio", "live_fills", "live_trades")):
        return "unsafe live/account-state filename"
    return None


def _safe_text_check(path: Path) -> None:
    # Binary artifacts are allowed only when their path/type has passed the
    # strict safety filter.  Search decodable text for obvious secret material.
    _safe_bytes_check(path.read_bytes(), str(path))


def _safe_bytes_check(raw: bytes, label: str) -> None:
    if b"PRIVATE KEY" in raw:
        raise _error("UNSAFE_SECRET_CONTENT", label)
    for encoding in ("utf-8", "utf-16", "utf-16-le", "utf-16-be"):
        try:
            content = raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        if SECRET_PATTERN.search(content) or JSON_SECRET_PATTERN.search(content):
            raise _error("UNSAFE_SECRET_CONTENT", label)


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o100644 << 16
    return info


def _canonical(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _entry_record(item: dict[str, Any]) -> dict[str, Any]:
    return {"absolute_path": str(item["path"]), **{key: item[key] for key in ("relative_path", "category", "priority", "sha256", "bytes")}}


def _validate_budget_profile(envelope: Any, answer: Any, reserve: Any) -> None:
    """Require the tested local profile; these are not provider/model limits."""
    expected = (TOTAL_ENVELOPE_BYTES, ANSWER_HEADROOM_BYTES, METADATA_RESERVE_BYTES)
    actual = (envelope, answer, reserve)
    if actual != expected:
        raise _error("BUDGET_PROFILE_MISMATCH", f"expected {expected}, got {actual}")


def _validate_file_size(size: int, path: Path) -> None:
    if size > MAX_SINGLE_FILE_BYTES:
        raise _error("SINGLE_FILE_CAP_EXCEEDED", str(path))


def _verify_archive(archive_path: Path, index: dict[str, Any], entries: list[dict[str, Any]]) -> None:
    included = index.get("included", [])
    expected_names = [PACKET_NAME, INDEX_NAME, *(f"evidence/{item['relative_path']}" for item in included)]
    by_relative = {item["relative_path"]: item for item in entries}
    try:
        with zipfile.ZipFile(archive_path) as archive:
            if archive.namelist() != expected_names:
                raise _error("ARCHIVE_INVALID", "member set/order differs")
            if json.loads(archive.read(INDEX_NAME).decode("utf-8")) != index:
                raise _error("ARCHIVE_INVALID", "evidence index differs")
            for record in included:
                source = by_relative.get(record.get("relative_path"))
                if source is None or _entry_record(source) != record:
                    raise _error("EVIDENCE_INDEX_INVALID", "included record differs")
                payload = archive.read(f"evidence/{record['relative_path']}")
                if len(payload) != record["bytes"] or hashlib.sha256(payload).hexdigest() != record["sha256"]:
                    raise _error("ARCHIVED_EVIDENCE_MISMATCH", record["relative_path"])
                _safe_bytes_check(payload, record["relative_path"])
    except (OSError, zipfile.BadZipFile, KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _error("ARCHIVE_INVALID", str(exc)) from exc


def _load_and_validate(manifest_path: Path) -> tuple[dict[str, Any], Path, Path, list[dict[str, Any]]]:
    manifest = _read_json(manifest_path)
    if manifest.get("schema") != SCHEMA:
        raise _error("SCHEMA_INVALID", "expected " + SCHEMA)
    root_value = manifest.get("project_root")
    if not isinstance(root_value, str) or not Path(root_value).is_absolute():
        raise _error("PROJECT_ROOT_REQUIRED", "project_root must be absolute")
    root_raw = Path(root_value)
    if root_raw.is_symlink():
        raise _error("SYMLINK_FORBIDDEN", str(root_raw))
    root = root_raw.resolve(strict=True)
    if not root.is_dir():
        raise _error("PROJECT_ROOT_REQUIRED", str(root))
    question = manifest.get("question")
    if not isinstance(question, str) or not question.strip():
        raise _error("QUESTION_REQUIRED", "question must be non-empty")
    categories = manifest.get("required_categories")
    if not isinstance(categories, list) or not categories or any(not isinstance(x, str) or not x for x in categories):
        raise _error("REQUIRED_CATEGORIES_REQUIRED", "declare non-empty project-specific categories")
    if len(set(categories)) != len(categories):
        raise _error("CATEGORY_DUPLICATE", "required_categories")
    envelope = manifest.get("local_transport_envelope_bytes")
    answer = manifest.get("answer_headroom_bytes")
    reserve = manifest.get("metadata_reserve_bytes")
    _validate_budget_profile(envelope, answer, reserve)
    output = _path_from(manifest.get("packet_path"), root, "packet_path")
    if output.exists() and output.is_symlink():
        raise _error("SYMLINK_FORBIDDEN", str(output))
    if output.suffix.casefold() != ".zip":
        raise _error("PACKET_PATH_INVALID", "packet_path must end in .zip")
    evidence = manifest.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        raise _error("EVIDENCE_REQUIRED", "explicit evidence allowlist is required")
    seen_paths: set[Path] = set()
    entries: list[dict[str, Any]] = []
    for item in evidence:
        if not isinstance(item, dict) or set(item) != {"path", "category", "priority", "sha256"}:
            raise _error("EVIDENCE_ENTRY_INVALID", "entries require only path/category/priority/sha256")
        path = _path_from(item["path"], root, "evidence path")
        if not path.is_file() or path.is_symlink():
            raise _error("REGULAR_FILE_REQUIRED", str(path))
        unsafe = _unsafe_path(path, root)
        if unsafe:
            raise _error("UNSAFE_EVIDENCE", f"{unsafe}: {path}")
        if path in seen_paths:
            raise _error("DUPLICATE_EVIDENCE", str(path))
        seen_paths.add(path)
        category = item["category"]
        if not isinstance(category, str) or not category or category not in categories:
            raise _error("CATEGORY_INVALID", str(category))
        priority = item["priority"]
        if not isinstance(priority, int) or priority < 0:
            raise _error("PRIORITY_INVALID", str(path))
        expected = item["sha256"]
        if not isinstance(expected, str) or not SHA256_RE.fullmatch(expected):
            raise _error("HASH_REQUIRED", str(path))
        actual = sha256_file(path)
        if actual != expected:
            raise _error("STALE_HASH", str(path))
        _safe_text_check(path)
        _validate_file_size(path.stat().st_size, path)
        relative = path.relative_to(root).as_posix()
        entries.append({"path": path, "relative_path": relative, "category": category, "priority": priority, "sha256": actual, "bytes": path.stat().st_size})
    receipt = output.with_suffix(output.suffix + ".index.json")
    manifest_resolved = manifest_path.resolve(strict=True)
    collisions = {"manifest": manifest_resolved}
    for label, candidate in (("packet", output), ("receipt", receipt)):
        if candidate == manifest_resolved:
            raise _error("OUTPUT_COLLISION", f"{label} would overwrite manifest")
        if candidate in seen_paths:
            raise _error("OUTPUT_COLLISION", f"{label} would overwrite evidence")
        collisions[label] = candidate
    if len(set(collisions.values())) != len(collisions):
        raise _error("OUTPUT_COLLISION", "manifest, packet, and receipt paths must differ")
    entries.sort(key=lambda item: (item["priority"], item["category"], item["relative_path"]))
    return manifest, root, output, entries


def build(manifest_path: Path) -> dict[str, Any]:
    manifest, root, output, entries = _load_and_validate(manifest_path)
    available = EVIDENCE_BUDGET_BYTES
    included: list[dict[str, Any]] = []
    omitted: list[dict[str, Any]] = []
    used_source_bytes = 0
    # The local envelope is a configured/proven transport limit, never claimed
    # to be a provider/model limit.  Reserve metadata before evidence selection.
    for item in entries:
        if used_source_bytes + item["bytes"] <= available:
            included.append(item)
            used_source_bytes += item["bytes"]
        else:
            omitted.append({**_entry_record(item), "reason": "local_transport_envelope_exhausted", "truncated": False})
    missing = sorted(set(manifest["required_categories"]) - {item["category"] for item in included})
    if missing:
        raise _error("REQUIRED_CATEGORY_MISSING", ", ".join(missing))
    archive_names = [f"evidence/{item['relative_path']}" for item in included]
    if len({name.casefold() for name in archive_names}) != len(archive_names):
        raise _error("ARCHIVE_COLLISION", "duplicate archive evidence name")
    index = {
        "schema": SCHEMA,
        "project_root": str(root),
        "question": manifest["question"],
        "local_transport_envelope": {"profile": LOCAL_PROFILE_ID, "label": "local proven/configured envelope, not a vendor or model limit", "total_budget_bytes": TOTAL_ENVELOPE_BYTES, "answer_headroom_bytes": ANSWER_HEADROOM_BYTES, "metadata_reserve_bytes": METADATA_RESERVE_BYTES, "evidence_budget_bytes": available, "max_single_file_bytes": MAX_SINGLE_FILE_BYTES, "max_packet_zip_bytes": MAX_PACKET_ZIP_BYTES, "used_source_bytes": used_source_bytes},
        "required_categories": manifest["required_categories"],
        "included": [_entry_record(item) for item in included],
        "omissions": omitted,
        "truncations": [],
        "selection_order": "priority ascending, category ascending, project-relative path ascending",
        "collection": "explicit manifest allowlist; no recursive project scan",
    }
    packet = {"schema": SCHEMA, "project_root": str(root), "question": manifest["question"], "read_instruction": "Read packet.json, evidence-index.json, and every included evidence file before answering. Resolve contradictions using the declared project rules and distinguish observed evidence from inference.", "evidence_index": INDEX_NAME}
    output.parent.mkdir(parents=True, exist_ok=True)
    temp_handle = tempfile.NamedTemporaryFile(prefix=f".{output.name}.", suffix=".tmp", dir=output.parent, delete=False)
    temp_path = Path(temp_handle.name)
    receipt_path = output.with_suffix(output.suffix + ".index.json")
    receipt_temp: Path | None = None
    try:
        temp_handle.close()
        with zipfile.ZipFile(temp_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9, strict_timestamps=True) as archive:
            archive.writestr(_zip_info(PACKET_NAME), _canonical(packet))
            archive.writestr(_zip_info(INDEX_NAME), _canonical(index))
            for item in included:
                archive.writestr(_zip_info(f"evidence/{item['relative_path']}"), item["path"].read_bytes())
        with temp_path.open("r+b") as handle:
            os.fsync(handle.fileno())
        archive_bytes = temp_path.stat().st_size
        if archive_bytes > MAX_PACKET_ZIP_BYTES:
            raise _error("OVERBUDGET", "archive exceeds local transport envelope")
        _verify_archive(temp_path, index, entries)
        index["local_transport_envelope"]["archive_bytes"] = archive_bytes
    # Store the final index outside the archive as a validator receipt.  It is
    # intentionally not an attachment and is regenerated deterministically.
        receipt = dict(index)
        receipt["packet_sha256"] = sha256_file(temp_path)
        receipt_handle = tempfile.NamedTemporaryFile(prefix=f".{receipt_path.name}.", suffix=".tmp", dir=output.parent, delete=False)
        receipt_temp = Path(receipt_handle.name)
        receipt_handle.write(_canonical(receipt))
        receipt_handle.flush()
        os.fsync(receipt_handle.fileno())
        receipt_handle.close()
        os.replace(temp_path, output)
        os.replace(receipt_temp, receipt_path)
        return receipt
    finally:
        if temp_path.exists():
            temp_path.unlink()
        if receipt_temp is not None and receipt_temp.exists():
            receipt_temp.unlink()


def validate(manifest_path: Path) -> dict[str, Any]:
    manifest, root, output, entries = _load_and_validate(manifest_path)
    if not output.is_file() or output.is_symlink():
        raise _error("PACKET_MISSING", str(output))
    if output.stat().st_size > MAX_PACKET_ZIP_BYTES:
        raise _error("OVERBUDGET", str(output))
    try:
        with zipfile.ZipFile(output) as archive:
            index = json.loads(archive.read(INDEX_NAME).decode("utf-8"))
    except (OSError, zipfile.BadZipFile, KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _error("ARCHIVE_INVALID", str(exc)) from exc
    if index.get("project_root") != str(root) or index.get("question") != manifest["question"]:
        raise _error("PACKET_BINDING_INVALID", "root or question differs")
    expected = [_entry_record(item) for item in entries]
    listed = index.get("included", []) + [{key: value for key, value in item.items() if key not in {"reason", "truncated"}} for item in index.get("omissions", [])]
    listed.sort(key=lambda item: (item["priority"], item["category"], item["relative_path"]))
    if listed != expected:
        raise _error("EVIDENCE_INDEX_INVALID", "allowlist/order differs")
    if set(manifest["required_categories"]) - {item.get("category") for item in index.get("included", [])}:
        raise _error("REQUIRED_CATEGORY_MISSING", "packet")
    _verify_archive(output, index, entries)
    receipt_path = output.with_suffix(output.suffix + ".index.json")
    try:
        receipt = _read_json(receipt_path)
    except PacketError as exc:
        raise _error("PACKET_RECEIPT_MISSING", str(receipt_path)) from exc
    receipt_core = {key: value for key, value in receipt.items() if key != "packet_sha256"}
    receipt_budget = dict(receipt_core.get("local_transport_envelope", {}))
    receipt_budget.pop("archive_bytes", None)
    receipt_core["local_transport_envelope"] = receipt_budget
    if receipt.get("packet_sha256") != sha256_file(output) or receipt_core != index:
        raise _error("STALE_PACKET_HASH", str(output))
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("build", "validate"))
    parser.add_argument("--manifest", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        result = build(args.manifest) if args.command == "build" else validate(args.manifest)
    except PacketError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
