from __future__ import annotations

"""Safe, local contract handling for a future Oracle attachment fallback.

This module deliberately has no Oracle/browser integration.  It validates the
immutable input surface, renders instructions for a caller to submit, parses a
single returned patch envelope, applies that patch inside the exact workspace,
and runs only the contract's explicit local verification command.
"""

from dataclasses import dataclass
import base64
import ctypes
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import tempfile
import unicodedata
import uuid
from typing import Any, Iterable, Mapping, Sequence


CONTRACT_SCHEMA = "codex.chatgpt.oracle-attachment-fallback/v1"
PATCH_SCHEMA = "codex.chatgpt.oracle-attachment-patch/v1"
REQUEST_SCHEMA = "codex.chatgpt.oracle-attachment-request/v1"
SNAPSHOT_SCHEMA = "codex.chatgpt.workspace-snapshot/v1"
SNAPSHOT_DELTA_SCHEMA = "codex.chatgpt.workspace-snapshot-delta/v1"
APPLY_RESULT_SCHEMA = "codex.chatgpt.oracle-attachment-apply-result/v1"
DIRECT_WRITE_ACCEPTANCE_SCHEMA = "codex.chatgpt.oracle-devspace-write-acceptance/v1"
TRANSACTION_SCHEMA = "codex.chatgpt.oracle-patch-transaction/v1"
CONTROL_SEAL_SCHEMA = "codex.chatgpt.oracle-patch-control-seal/v1"

PATCH_BEGIN_MARKER = "<<<CODEX_ORACLE_ATTACHMENT_PATCH_V1>>>"
PATCH_END_MARKER = "<<<END_CODEX_ORACLE_ATTACHMENT_PATCH_V1>>>"

ACTION_AUTHORITIES = {
    "read-only",
    "workspace-write",
    "mission-owned-adaptive-execution",
}
WRITE_AUTHORITIES = ACTION_AUTHORITIES - {"read-only"}
EDIT_OPERATIONS = {"add", "update", "delete", "move"}

CONTRACT_MAX_BYTES = 1_048_576
MISSION_MAX_BYTES = 1_048_576
HARD_MAX_EVIDENCE_FILES = 64
HARD_MAX_EVIDENCE_FILE_BYTES = 1_048_576
HARD_MAX_EVIDENCE_TOTAL_BYTES = 16_777_216
HARD_MAX_PATCH_OPERATIONS = 128
HARD_MAX_PATCH_FILE_BYTES = 4_194_304
HARD_MAX_PATCH_TOTAL_BYTES = 16_777_216
HARD_MAX_GATE_TIMEOUT_SECONDS = 3_600
DEFAULT_SNAPSHOT_MAX_ENTRIES = 100_000
DEFAULT_SNAPSHOT_MAX_FILE_BYTES = 1_073_741_824
DEFAULT_SNAPSHOT_MAX_TOTAL_BYTES = 4_294_967_296
MAX_METADATA_ITEMS_PER_FILE = 64
MAX_METADATA_NAME_BYTES = 1_024
TRANSACTION_JOURNAL_MAX_BYTES = 16_777_216

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CATEGORY_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_WINDOWS_RESERVED_DEVICE_RE = re.compile(
    r"(?i)^(?:con|prn|aux|nul|clock\$|conin\$|conout\$|com[1-9\u00b9\u00b2\u00b3]|lpt[1-9\u00b9\u00b2\u00b3])$"
)
_KNOWN_SECRET_RE = re.compile(
    r"(?:"
    r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"
    r"|\bAKIA[0-9A-Z]{16}\b"
    r"|\bASIA[0-9A-Z]{16}\b"
    r"|\bgithub_pat_[A-Za-z0-9_]{20,}\b"
    r"|\bgh[pousr]_[A-Za-z0-9]{20,}\b"
    r"|\bsk-[A-Za-z0-9_-]{20,}\b"
    r"|\bxox[baprs]-[A-Za-z0-9-]{10,}\b"
    r"|\bAIza[0-9A-Za-z_-]{30,}\b"
    r"|\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"
    r")"
)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)^\s*(?:export\s+)?(?P<quote>[\"']?)"
    r"(?:(?:[a-z0-9]+[_-])*(?:"
    r"api[_-]?key|access[_-]?(?:key|token)|auth[_-]?token|bearer[_-]?token|"
    r"client[_-]?secret|connection[_-]?string|password|passwd|private[_-]?key|"
    r"secret(?:[_-]?(?:access[_-]?key|key))?|token"
    r"))(?P=quote)\s*[:=]\s*(?P<value>.+?)\s*[,;]?\s*$"
)
_AUTHORIZATION_SECRET_RE = re.compile(
    r"(?i)\bauthorization\s*[:=]\s*[\"']?\s*(?:bearer|basic)\s+(?P<value>[^\s\"',;]+)"
)
_NPM_AUTH_SECRET_RE = re.compile(
    r"(?i)(?://[^\s=]+/)?(?::_authToken|_auth|password)\s*=\s*[\"']?(?P<value>[^\s\"']+)"
)
_CREDENTIAL_URL_RE = re.compile(
    r"(?i)\b[a-z][a-z0-9+.-]*://(?P<username>[^\s/@:]+):(?P<value>[^\s/@]+)@"
)
_TOKEN_USERINFO_URL_RE = re.compile(
    r"(?i)\b[a-z][a-z0-9+.-]*://(?P<value>[^\s/@:]+)@"
)
_URL_QUERY_SECRET_RE = re.compile(
    r"(?i)[?&](?:access[_-]?token|api[_-]?key|auth[_-]?token|client[_-]?secret|"
    r"password|refresh[_-]?token|secret|token)="
    r"(?P<value>[^\s&#\"']+)"
)
_PLACEHOLDER_PREFIXES = (
    "${",
    "{{",
    "<",
    "os.",
    "env.",
    "getenv(",
    "process.env",
    "redacted",
    "placeholder",
    "example",
    "dummy",
    "test-only",
    "changeme",
)
_UNSAFE_DIRECTORY_NAMES = {
    ".aws",
    ".azure",
    ".cache",
    ".docker",
    ".gnupg",
    ".kube",
    ".ssh",
    "__pycache__",
    "browser profile",
    "cache",
    "caches",
    "chrome profile",
    "profile",
    "profiles",
    "user data",
}
_UNSAFE_FILE_NAMES = {
    ".env",
    ".git-credentials",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "cookies",
    "cookies.sqlite",
    "credentials",
    "credentials.json",
    "history",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_rsa",
    "login data",
    "local state",
    "shadow",
    "web data",
}


class FallbackContractError(ValueError):
    """A stable-code validation or application failure."""

    def __init__(self, code: str, detail: str | None = None):
        self.code = code
        self.detail = detail
        super().__init__(code if detail is None else f"{code}: {detail}")


@dataclass(frozen=True)
class EvidenceFile:
    path: str
    category: str
    priority: int
    sha256: str
    absolute_path: Path
    bytes: int

    def contract_value(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "category": self.category,
            "priority": self.priority,
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class EditPath:
    path: str
    before_sha256: str | None
    operations: tuple[str, ...]
    absolute_path: Path
    before_mode: int | None
    before_metadata: Mapping[str, Any] | None

    def contract_value(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "before_sha256": self.before_sha256,
            "operations": list(self.operations),
        }


@dataclass(frozen=True)
class ContractLimits:
    max_evidence_files: int
    max_evidence_file_bytes: int
    max_evidence_total_bytes: int
    max_patch_operations: int
    max_patch_file_bytes: int
    max_patch_total_bytes: int
    local_gate_timeout_seconds: int

    def contract_value(self) -> dict[str, int]:
        return {
            "max_evidence_files": self.max_evidence_files,
            "max_evidence_file_bytes": self.max_evidence_file_bytes,
            "max_evidence_total_bytes": self.max_evidence_total_bytes,
            "max_patch_operations": self.max_patch_operations,
            "max_patch_file_bytes": self.max_patch_file_bytes,
            "max_patch_total_bytes": self.max_patch_total_bytes,
            "local_gate_timeout_seconds": self.local_gate_timeout_seconds,
        }


@dataclass(frozen=True)
class FallbackContract:
    project_root: Path
    mission_path: Path
    mission_sha256: str
    action_authority: str
    reasoning_level: str
    evidence_allowlist: tuple[EvidenceFile, ...]
    edit_path_allowlist: tuple[EditPath, ...]
    local_gate_command: tuple[str, ...] | None
    local_gate_executable_sha256: str | None
    limits: ContractLimits
    contract_sha256: str

    def contract_value(self) -> dict[str, Any]:
        return {
            "schema": CONTRACT_SCHEMA,
            "project_root": str(self.project_root),
            "mission_path": str(self.mission_path),
            "mission_sha256": self.mission_sha256,
            "action_authority": self.action_authority,
            "reasoning_level": self.reasoning_level,
            "evidence_allowlist": [item.contract_value() for item in self.evidence_allowlist],
            "edit_path_allowlist": [item.contract_value() for item in self.edit_path_allowlist],
            "local_gate_command": list(self.local_gate_command) if self.local_gate_command else None,
            "limits": self.limits.contract_value(),
        }


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_bounded_binary(path: Path, *, max_bytes: int, code: str) -> bytes:
    before = _assert_regular_file(path, code=code)
    if before.st_size > max_bytes:
        raise FallbackContractError(code, str(path))
    try:
        with path.open("rb") as handle:
            data = handle.read(max_bytes + 1)
    except OSError as exc:
        raise FallbackContractError(code, str(path)) from exc
    after = _assert_regular_file(path, code=code)
    if (
        len(data) != before.st_size
        or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    ):
        raise FallbackContractError(code, str(path))
    return data


def _strict_json_loads(text: str, *, code: str) -> Any:
    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise FallbackContractError("JSON_DUPLICATE_KEY", key)
            result[key] = item
        return result

    def reject_constant(value: str) -> None:
        raise FallbackContractError("JSON_NONFINITE_NUMBER", value)

    try:
        return json.loads(text, object_pairs_hook=object_pairs, parse_constant=reject_constant)
    except FallbackContractError:
        raise
    except json.JSONDecodeError as exc:
        raise FallbackContractError(code) from exc


def _require_exact_keys(
    value: Mapping[str, Any], required: set[str], *, field: str, optional: set[str] | None = None
) -> None:
    optional = optional or set()
    missing = required - set(value)
    extra = set(value) - required - optional
    if missing:
        raise FallbackContractError("REQUIRED_KEYS_MISSING", f"{field}: {','.join(sorted(missing))}")
    if extra:
        raise FallbackContractError("UNKNOWN_KEYS", f"{field}: {','.join(sorted(extra))}")


def _path_key(path: Path | str) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def _is_reparse_stat(value: os.stat_result) -> bool:
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes = getattr(value, "st_file_attributes", 0)
    return bool(flag and attributes & flag)


def _assert_regular_file(path: Path, *, code: str) -> os.stat_result:
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise FallbackContractError(code, str(path)) from exc
    if stat.S_ISLNK(info.st_mode) or _is_reparse_stat(info) or not stat.S_ISREG(info.st_mode):
        raise FallbackContractError(code, str(path))
    return info


def _metadata_sha256(value: Mapping[str, Any]) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def _windows_metadata(
    path: Path, *, include_restore: bool, max_payload_bytes: int
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

    get_attributes = kernel32.GetFileAttributesW
    get_attributes.argtypes = [wintypes.LPCWSTR]
    get_attributes.restype = wintypes.DWORD
    attributes = int(get_attributes(str(path)))
    if attributes == 0xFFFFFFFF:
        raise FallbackContractError("FILESYSTEM_METADATA_READ_FAILED", str(path))

    owner_and_dacl = 0x00000001 | 0x00000004
    get_security = advapi32.GetFileSecurityW
    get_security.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    get_security.restype = wintypes.BOOL
    needed = wintypes.DWORD()
    get_security(str(path), owner_and_dacl, None, 0, ctypes.byref(needed))
    if needed.value <= 0 or ctypes.get_last_error() not in {0, 122}:
        raise FallbackContractError("FILESYSTEM_SECURITY_DESCRIPTOR_UNAVAILABLE", str(path))
    security_buffer = (ctypes.c_ubyte * needed.value)()
    if not get_security(
        str(path), owner_and_dacl, security_buffer, needed.value, ctypes.byref(needed)
    ):
        raise FallbackContractError("FILESYSTEM_SECURITY_DESCRIPTOR_UNAVAILABLE", str(path))
    security = bytes(security_buffer[: needed.value])

    class StreamData(ctypes.Structure):
        _fields_ = [("size", ctypes.c_longlong), ("name", wintypes.WCHAR * 296)]

    find_first = kernel32.FindFirstStreamW
    find_first.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(StreamData), wintypes.DWORD]
    find_first.restype = wintypes.HANDLE
    find_next = kernel32.FindNextStreamW
    find_next.argtypes = [wintypes.HANDLE, ctypes.POINTER(StreamData)]
    find_next.restype = wintypes.BOOL
    find_close = kernel32.FindClose
    find_close.argtypes = [wintypes.HANDLE]
    find_close.restype = wintypes.BOOL
    stream_data = StreamData()
    handle = find_first(str(path), 0, ctypes.byref(stream_data), 0)
    invalid_handle = ctypes.c_void_p(-1).value
    if handle == invalid_handle:
        error = ctypes.get_last_error()
        if error != 38:  # ERROR_HANDLE_EOF means the object has no data streams.
            raise FallbackContractError("FILESYSTEM_STREAM_ENUMERATION_UNAVAILABLE", f"{path}: {error}")
        stream_names: list[tuple[str, int]] = []
    else:
        stream_names = []
        try:
            while True:
                name = str(stream_data.name)
                size = int(stream_data.size)
                if name != "::$DATA":
                    if not name.startswith(":") or not name.endswith(":$DATA") or size < 0:
                        raise FallbackContractError("FILESYSTEM_STREAM_ENUMERATION_INVALID", str(path))
                    stream_names.append((name, size))
                if not find_next(handle, ctypes.byref(stream_data)):
                    error = ctypes.get_last_error()
                    if error != 38:
                        raise FallbackContractError(
                            "FILESYSTEM_STREAM_ENUMERATION_UNAVAILABLE", f"{path}: {error}"
                        )
                    break
        finally:
            find_close(handle)

    streams: list[dict[str, Any]] = []
    restore_streams: list[dict[str, Any]] = []
    if len(stream_names) > MAX_METADATA_ITEMS_PER_FILE or any(
        len(name.encode("utf-8", errors="strict")) > MAX_METADATA_NAME_BYTES
        for name, _size in stream_names
    ):
        raise FallbackContractError("FILESYSTEM_METADATA_ITEM_LIMIT_EXCEEDED", str(path))
    running_payload = len(security)
    for name, reported_size in sorted(stream_names):
        running_payload += reported_size
        if running_payload > max_payload_bytes:
            raise FallbackContractError("FILESYSTEM_METADATA_SIZE_EXCEEDED", str(path))
        try:
            with Path(str(path) + name).open("rb") as handle:
                data = handle.read(reported_size + 1)
        except OSError as exc:
            raise FallbackContractError("FILESYSTEM_STREAM_READ_FAILED", f"{path}{name}") from exc
        if len(data) != reported_size:
            raise FallbackContractError("FILESYSTEM_METADATA_RACE", f"{path}{name}")
        streams.append({"name": name, "bytes": len(data), "sha256": sha256_bytes(data)})
        if include_restore:
            restore_streams.append({"name": name, "value_base64": base64.b64encode(data).decode("ascii")})

    public = {
        "platform": "windows",
        "attributes": attributes,
        "owner_dacl": {"bytes": len(security), "sha256": sha256_bytes(security)},
        "streams": streams,
    }
    restore = None
    if include_restore:
        restore = {
            "platform": "windows",
            "attributes": attributes,
            "owner_dacl_base64": base64.b64encode(security).decode("ascii"),
            "streams": restore_streams,
        }
    return public, restore


def _posix_metadata(
    path: Path, info: os.stat_result, *, include_restore: bool
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    if not hasattr(os, "listxattr") or not hasattr(os, "getxattr"):
        raise FallbackContractError("FILESYSTEM_XATTR_UNAVAILABLE", str(path))
    try:
        names = sorted(os.listxattr(path, follow_symlinks=False))
        if len(names) > MAX_METADATA_ITEMS_PER_FILE or any(
            len(name.encode("utf-8", errors="strict")) > MAX_METADATA_NAME_BYTES
            for name in names
        ):
            raise FallbackContractError("FILESYSTEM_METADATA_ITEM_LIMIT_EXCEEDED", str(path))
        values = [(name, os.getxattr(path, name, follow_symlinks=False)) for name in names]
    except OSError as exc:
        raise FallbackContractError("FILESYSTEM_XATTR_UNAVAILABLE", str(path)) from exc
    xattrs = [
        {"name": name, "bytes": len(value), "sha256": sha256_bytes(value)}
        for name, value in values
    ]
    public = {
        "platform": "posix",
        "uid": int(info.st_uid),
        "gid": int(info.st_gid),
        "xattrs": xattrs,
    }
    restore = None
    if include_restore:
        restore = {
            "platform": "posix",
            "uid": int(info.st_uid),
            "gid": int(info.st_gid),
            "xattrs": [
                {"name": name, "value_base64": base64.b64encode(value).decode("ascii")}
                for name, value in values
            ],
        }
    return public, restore


def _filesystem_metadata(
    path: Path,
    info: os.stat_result,
    *,
    include_restore: bool = False,
    max_payload_bytes: int = DEFAULT_SNAPSHOT_MAX_FILE_BYTES,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    try:
        if os.name == "nt":
            public, restore = _windows_metadata(
                path,
                include_restore=include_restore,
                max_payload_bytes=max_payload_bytes,
            )
        else:
            public, restore = _posix_metadata(path, info, include_restore=include_restore)
    except FallbackContractError:
        raise
    except Exception as exc:
        raise FallbackContractError("FILESYSTEM_METADATA_READ_FAILED", str(path)) from exc
    payload_bytes = sum(int(item["bytes"]) for item in public.get("streams", public.get("xattrs", [])))
    owner_dacl = public.get("owner_dacl")
    if isinstance(owner_dacl, Mapping):
        payload_bytes += int(owner_dacl.get("bytes", 0))
    if payload_bytes > max_payload_bytes:
        raise FallbackContractError("FILESYSTEM_METADATA_SIZE_EXCEEDED", str(path))
    return public, restore


def _validate_metadata(value: Any, *, field: str) -> int:
    if not isinstance(value, Mapping):
        raise FallbackContractError("FILESYSTEM_METADATA_INVALID", field)
    platform = value.get("platform")
    payload_bytes = 0
    if platform == "windows":
        _require_exact_keys(value, {"platform", "attributes", "owner_dacl", "streams"}, field=field)
        attributes = value.get("attributes")
        if not isinstance(attributes, int) or isinstance(attributes, bool) or not 0 <= attributes <= 0xFFFFFFFF:
            raise FallbackContractError("FILESYSTEM_METADATA_INVALID", field)
        owner_dacl = value.get("owner_dacl")
        if not isinstance(owner_dacl, Mapping):
            raise FallbackContractError("FILESYSTEM_METADATA_INVALID", field)
        _require_exact_keys(owner_dacl, {"bytes", "sha256"}, field=f"{field}.owner_dacl")
        if (
            not isinstance(owner_dacl.get("bytes"), int)
            or isinstance(owner_dacl.get("bytes"), bool)
            or owner_dacl.get("bytes") < 0
            or not isinstance(owner_dacl.get("sha256"), str)
            or not _SHA256_RE.fullmatch(owner_dacl["sha256"])
        ):
            raise FallbackContractError("FILESYSTEM_METADATA_INVALID", field)
        payload_bytes += owner_dacl["bytes"]
        items = value.get("streams")
        name_rule = lambda name: name.startswith(":") and name.endswith(":$DATA") and name != "::$DATA"
    elif platform == "posix":
        _require_exact_keys(value, {"platform", "uid", "gid", "xattrs"}, field=field)
        if any(
            not isinstance(value.get(name), int) or isinstance(value.get(name), bool) or value.get(name) < 0
            for name in ("uid", "gid")
        ):
            raise FallbackContractError("FILESYSTEM_METADATA_INVALID", field)
        items = value.get("xattrs")
        name_rule = lambda name: bool(name) and "\x00" not in name
    else:
        raise FallbackContractError("FILESYSTEM_METADATA_INVALID", field)
    if not isinstance(items, list) or len(items) > MAX_METADATA_ITEMS_PER_FILE:
        raise FallbackContractError("FILESYSTEM_METADATA_INVALID", field)
    names: list[str] = []
    for index, item in enumerate(items):
        if not isinstance(item, Mapping):
            raise FallbackContractError("FILESYSTEM_METADATA_INVALID", field)
        _require_exact_keys(item, {"name", "bytes", "sha256"}, field=f"{field}[{index}]")
        name = item.get("name")
        if (
            not isinstance(name, str)
            or not name_rule(name)
            or len(name.encode("utf-8", errors="strict")) > MAX_METADATA_NAME_BYTES
            or not isinstance(item.get("bytes"), int)
            or isinstance(item.get("bytes"), bool)
            or item.get("bytes") < 0
            or not isinstance(item.get("sha256"), str)
            or not _SHA256_RE.fullmatch(item["sha256"])
        ):
            raise FallbackContractError("FILESYSTEM_METADATA_INVALID", field)
        names.append(name)
        payload_bytes += item["bytes"]
    if names != sorted(names) or len(set(names)) != len(names):
        raise FallbackContractError("FILESYSTEM_METADATA_INVALID", field)
    return payload_bytes


def _blob_reference(data: bytes) -> dict[str, Any]:
    digest = sha256_bytes(data)
    return {"path": f"{digest}.blob", "bytes": len(data), "sha256": digest}


def _write_metadata_blob(directory: Path, data: bytes) -> dict[str, Any]:
    reference = _blob_reference(data)
    path = directory / reference["path"]
    if path.exists():
        _assert_regular_file(path, code="TRANSACTION_METADATA_BLOB_INVALID")
        if path.stat().st_size != len(data) or sha256_file(path) != reference["sha256"]:
            raise FallbackContractError("TRANSACTION_METADATA_BLOB_INVALID", str(path))
    else:
        _write_stage(path, data, mode=0o600)
    return reference


def _externalize_restore_metadata(
    restore: Mapping[str, Any], directory: Path
) -> dict[str, Any]:
    try:
        if restore.get("platform") == "windows":
            security = base64.b64decode(restore["owner_dacl_base64"], validate=True)
            return {
                "platform": "windows",
                "attributes": restore["attributes"],
                "owner_dacl_blob": _write_metadata_blob(directory, security),
                "streams": [
                    {
                        "name": item["name"],
                        "blob": _write_metadata_blob(
                            directory,
                            base64.b64decode(item["value_base64"], validate=True),
                        ),
                    }
                    for item in restore["streams"]
                ],
            }
        return {
            "platform": "posix",
            "uid": restore["uid"],
            "gid": restore["gid"],
            "xattrs": [
                {
                    "name": item["name"],
                    "blob": _write_metadata_blob(
                        directory,
                        base64.b64decode(item["value_base64"], validate=True),
                    ),
                }
                for item in restore["xattrs"]
            ],
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise FallbackContractError("TRANSACTION_RESTORE_METADATA_INVALID") from exc


def _read_metadata_blob(
    root: Path,
    reference: Any,
    *,
    field: str,
    max_bytes: int,
    cache: dict[str, bytes] | None = None,
    max_cached_bytes: int = HARD_MAX_PATCH_TOTAL_BYTES,
) -> bytes:
    if not isinstance(reference, Mapping):
        raise FallbackContractError("TRANSACTION_METADATA_BLOB_INVALID", field)
    _require_exact_keys(reference, {"path", "bytes", "sha256"}, field=field)
    relative = reference.get("path")
    size = reference.get("bytes")
    digest = reference.get("sha256")
    if (
        not isinstance(relative, str)
        or not re.fullmatch(r"[0-9a-f]{64}\.blob", relative)
        or not isinstance(size, int)
        or isinstance(size, bool)
        or not 0 <= size <= max_bytes
        or not isinstance(digest, str)
        or not _SHA256_RE.fullmatch(digest)
        or relative != f"{digest}.blob"
    ):
        raise FallbackContractError("TRANSACTION_METADATA_BLOB_INVALID", field)
    path = root / relative
    if cache is not None and relative in cache:
        data = cache[relative]
        if len(data) != size or sha256_bytes(data) != digest:
            raise FallbackContractError("TRANSACTION_METADATA_BLOB_INVALID", field)
        return data
    if cache is not None and sum(len(item) for item in cache.values()) + size > max_cached_bytes:
        raise FallbackContractError("TRANSACTION_METADATA_TOTAL_SIZE_EXCEEDED", field)
    info = _assert_regular_file(path, code="TRANSACTION_METADATA_BLOB_INVALID")
    if info.st_size != size:
        raise FallbackContractError("TRANSACTION_METADATA_BLOB_INVALID", field)
    try:
        with path.open("rb") as handle:
            data = handle.read(size + 1)
    except OSError as exc:
        raise FallbackContractError("TRANSACTION_METADATA_BLOB_INVALID", field) from exc
    if len(data) != size or sha256_bytes(data) != digest:
        raise FallbackContractError("TRANSACTION_METADATA_BLOB_INVALID", field)
    if cache is not None:
        cache[relative] = data
    return data


def _validate_restore_metadata(
    restore: Any,
    public: Mapping[str, Any],
    *,
    field: str,
    blob_root: Path,
    max_blob_bytes: int,
    blob_cache: dict[str, bytes] | None = None,
) -> None:
    if not isinstance(restore, Mapping) or restore.get("platform") != public.get("platform"):
        raise FallbackContractError("TRANSACTION_RESTORE_METADATA_INVALID", field)
    if restore.get("platform") == "windows":
        _require_exact_keys(
            restore, {"platform", "attributes", "owner_dacl_blob", "streams"}, field=field
        )
        if restore.get("attributes") != public.get("attributes"):
            raise FallbackContractError("TRANSACTION_RESTORE_METADATA_INVALID", field)
        security = _read_metadata_blob(
            blob_root,
            restore.get("owner_dacl_blob"),
            field=f"{field}.owner_dacl_blob",
            max_bytes=max_blob_bytes,
            cache=blob_cache,
        )
        expected_security = public["owner_dacl"]
        if len(security) != expected_security["bytes"] or sha256_bytes(security) != expected_security["sha256"]:
            raise FallbackContractError("TRANSACTION_RESTORE_METADATA_INVALID", field)
        public_items = public["streams"]
    else:
        _require_exact_keys(restore, {"platform", "uid", "gid", "xattrs"}, field=field)
        if restore.get("uid") != public.get("uid") or restore.get("gid") != public.get("gid"):
            raise FallbackContractError("TRANSACTION_RESTORE_METADATA_INVALID", field)
        public_items = public["xattrs"]
    restore_items = restore.get("streams" if restore.get("platform") == "windows" else "xattrs")
    if not isinstance(restore_items, list) or len(restore_items) != len(public_items):
        raise FallbackContractError("TRANSACTION_RESTORE_METADATA_INVALID", field)
    for actual, expected in zip(restore_items, public_items, strict=True):
        if not isinstance(actual, Mapping):
            raise FallbackContractError("TRANSACTION_RESTORE_METADATA_INVALID", field)
        _require_exact_keys(actual, {"name", "blob"}, field=field)
        if actual.get("name") != expected.get("name"):
            raise FallbackContractError("TRANSACTION_RESTORE_METADATA_INVALID", field)
        data = _read_metadata_blob(
            blob_root,
            actual.get("blob"),
            field=f"{field}.{actual.get('name')}",
            max_bytes=max_blob_bytes,
            cache=blob_cache,
        )
        if len(data) != expected["bytes"] or sha256_bytes(data) != expected["sha256"]:
            raise FallbackContractError("TRANSACTION_RESTORE_METADATA_INVALID", field)


def _restore_file_metadata(
    path: Path,
    mode: int,
    public: Mapping[str, Any],
    restore: Mapping[str, Any],
    *,
    blob_root: Path,
    max_blob_bytes: int,
) -> None:
    _validate_restore_metadata(
        restore,
        public,
        field=str(path),
        blob_root=blob_root,
        max_blob_bytes=max_blob_bytes,
    )
    try:
        if os.name == "nt":
            from ctypes import wintypes

            current, _ = _windows_metadata(
                path,
                include_restore=False,
                max_payload_bytes=max_blob_bytes,
            )
            expected_streams = {item["name"]: item for item in restore["streams"]}
            for item in current["streams"]:
                if item["name"] not in expected_streams:
                    Path(str(path) + item["name"]).unlink()
            for name, item in expected_streams.items():
                data = _read_metadata_blob(
                    blob_root,
                    item["blob"],
                    field=f"{path}:{name}",
                    max_bytes=max_blob_bytes,
                )
                Path(str(path) + name).write_bytes(data)
            if current["owner_dacl"] != public["owner_dacl"]:
                advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
                set_security = advapi32.SetFileSecurityW
                set_security.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.LPVOID]
                set_security.restype = wintypes.BOOL
                security = _read_metadata_blob(
                    blob_root,
                    restore["owner_dacl_blob"],
                    field=f"{path}:owner_dacl",
                    max_bytes=max_blob_bytes,
                )
                buffer = ctypes.create_string_buffer(security)
                if not set_security(str(path), 0x00000001 | 0x00000004, buffer):
                    raise OSError(ctypes.get_last_error(), "SetFileSecurityW")
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            set_attributes = kernel32.SetFileAttributesW
            set_attributes.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
            set_attributes.restype = wintypes.BOOL
            if not set_attributes(str(path), int(restore["attributes"])):
                raise OSError(ctypes.get_last_error(), "SetFileAttributesW")
        else:
            current_names = set(os.listxattr(path, follow_symlinks=False))
            expected_values = {
                item["name"]: _read_metadata_blob(
                    blob_root,
                    item["blob"],
                    field=f"{path}:{item['name']}",
                    max_bytes=max_blob_bytes,
                )
                for item in restore["xattrs"]
            }
            for name in current_names - set(expected_values):
                os.removexattr(path, name, follow_symlinks=False)
            for name, value in expected_values.items():
                os.setxattr(path, name, value, follow_symlinks=False)
            current_info = os.lstat(path)
            if (current_info.st_uid, current_info.st_gid) != (restore["uid"], restore["gid"]):
                os.chown(path, restore["uid"], restore["gid"], follow_symlinks=False)
            os.chmod(path, mode, follow_symlinks=False)
    except Exception as exc:
        raise FallbackContractError("FILESYSTEM_METADATA_RESTORE_FAILED", str(path)) from exc
    final_info = _assert_regular_file(path, code="FILESYSTEM_METADATA_RESTORE_FAILED")
    final, _ = _filesystem_metadata(path, final_info)
    if stat.S_IMODE(final_info.st_mode) != mode or final != public:
        raise FallbackContractError("FILESYSTEM_METADATA_RESTORE_FAILED", str(path))


def _canonical_absolute_existing(path_text: Any, *, kind: str) -> Path:
    if not isinstance(path_text, str) or not path_text.strip():
        raise FallbackContractError(f"{kind}_PATH_INVALID")
    raw = Path(path_text).expanduser()
    if not raw.is_absolute():
        raise FallbackContractError(f"{kind}_PATH_NOT_ABSOLUTE", path_text)
    lexical = Path(os.path.abspath(raw))
    try:
        resolved = raw.resolve(strict=True)
    except (OSError, ValueError) as exc:
        raise FallbackContractError(f"{kind}_PATH_MISSING", path_text) from exc
    if _path_key(lexical) != _path_key(resolved):
        raise FallbackContractError(f"{kind}_PATH_NOT_EXACT", path_text)
    return resolved


def _canonical_root(path_text: Any) -> Path:
    root = _canonical_absolute_existing(path_text, kind="PROJECT_ROOT")
    try:
        info = os.lstat(root)
    except OSError as exc:
        raise FallbackContractError("PROJECT_ROOT_INVALID", str(root)) from exc
    if stat.S_ISLNK(info.st_mode) or _is_reparse_stat(info) or not stat.S_ISDIR(info.st_mode):
        raise FallbackContractError("PROJECT_ROOT_INVALID", str(root))
    return root


def _default_transaction_root(project_root: Path) -> Path:
    root_key = sha256_bytes(str(project_root).encode("utf-8", errors="strict"))
    return Path(tempfile.gettempdir()) / "Codex" / "oracle-fallback" / root_key


def _prepare_transaction_root(project_root: Path, transaction_root: Path | None) -> Path:
    candidate = (
        _default_transaction_root(project_root)
        if transaction_root is None
        else Path(transaction_root).expanduser()
    )
    if not candidate.is_absolute():
        raise FallbackContractError("TRANSACTION_ROOT_NOT_ABSOLUTE", str(candidate))
    lexical = Path(os.path.abspath(candidate))
    try:
        missing: list[Path] = []
        current = lexical
        while not current.exists():
            if current.parent == current:
                raise FallbackContractError("TRANSACTION_ROOT_INVALID", str(candidate))
            missing.append(current)
            current = current.parent
        if not current.is_dir():
            raise FallbackContractError("TRANSACTION_ROOT_INVALID", str(current))
        for directory in reversed(missing):
            if os.name == "nt":
                from ctypes import wintypes

                temporary = directory.parent / f".{directory.name}.mkdir.{uuid.uuid4().hex}"
                temporary.mkdir(mode=0o700)
                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                move = kernel32.MoveFileExW
                move.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
                move.restype = wintypes.BOOL
                if not move(str(temporary), str(directory), 0x8):
                    error = ctypes.get_last_error()
                    temporary.rmdir()
                    raise OSError(error, "MoveFileExW")
            else:
                directory.mkdir(mode=0o700)
                _fsync_directory(directory.parent)
        resolved = lexical.resolve(strict=True)
        os.chmod(resolved, 0o700)
    except OSError as exc:
        raise FallbackContractError("TRANSACTION_ROOT_INVALID", str(candidate)) from exc
    if _path_key(lexical) != _path_key(resolved):
        raise FallbackContractError("TRANSACTION_ROOT_NOT_EXACT", str(candidate))
    info = os.lstat(resolved)
    if stat.S_ISLNK(info.st_mode) or _is_reparse_stat(info) or not stat.S_ISDIR(info.st_mode):
        raise FallbackContractError("TRANSACTION_ROOT_INVALID", str(candidate))
    if resolved == project_root or project_root in resolved.parents or resolved in project_root.parents:
        raise FallbackContractError("TRANSACTION_ROOT_PROJECT_OVERLAP", str(candidate))
    return resolved


def _reject_windows_ambiguous_path(value: str, *, field: str) -> None:
    if (
        "\\" in value
        or ":" in value
        or _WINDOWS_DRIVE_RE.match(value)
        or any(unicodedata.category(character) == "Cc" for character in value)
    ):
        raise FallbackContractError("RELATIVE_PATH_INVALID", f"{field}: {value!r}")
    pure = PurePosixPath(value)
    if pure.is_absolute() or value.startswith("/") or any(part in {"", ".", ".."} for part in pure.parts):
        raise FallbackContractError("PATH_ESCAPE", f"{field}: {value}")
    for part in pure.parts:
        if part.endswith((".", " ")):
            raise FallbackContractError("RELATIVE_PATH_INVALID", f"{field}: {value!r}")
        device_alias = part.split(".", 1)[0]
        if _WINDOWS_RESERVED_DEVICE_RE.fullmatch(device_alias):
            raise FallbackContractError("RELATIVE_PATH_INVALID", f"{field}: {value!r}")


def _validate_relative_path(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise FallbackContractError("RELATIVE_PATH_INVALID", field)
    _reject_windows_ambiguous_path(value, field=field)
    pure = PurePosixPath(value)
    if pure.is_absolute() or value.startswith("/") or any(part in {"", ".", ".."} for part in pure.parts):
        raise FallbackContractError("PATH_ESCAPE", f"{field}: {value}")
    normalized = pure.as_posix()
    if normalized != value:
        raise FallbackContractError("RELATIVE_PATH_NOT_CANONICAL", f"{field}: {value}")
    _reject_unsafe_path(normalized, field=field)
    return normalized


def _reject_unsafe_path(relative: str, *, field: str) -> None:
    parts = [part.casefold() for part in PurePosixPath(relative).parts]
    filename = parts[-1]
    directories = parts[:-1]
    if any(part in _UNSAFE_DIRECTORY_NAMES for part in directories):
        raise FallbackContractError("UNSAFE_PATH", f"{field}: {relative}")
    if filename in _UNSAFE_FILE_NAMES or filename.startswith(".env."):
        raise FallbackContractError("UNSAFE_PATH", f"{field}: {relative}")
    if filename.endswith((".pem", ".p12", ".pfx")) or filename.endswith(".key"):
        raise FallbackContractError("UNSAFE_PATH", f"{field}: {relative}")


def _workspace_path(root: Path, relative: str) -> Path:
    candidate = root.joinpath(*PurePosixPath(relative).parts)
    lexical = Path(os.path.abspath(candidate))
    if root != lexical and root not in lexical.parents:
        raise FallbackContractError("PATH_ESCAPE", relative)
    return lexical


def _assert_existing_parents_safe(root: Path, path: Path) -> None:
    current = root
    try:
        relative_parts = path.relative_to(root).parts
    except ValueError as exc:
        raise FallbackContractError("PATH_ESCAPE", str(path)) from exc
    for part in relative_parts[:-1]:
        current = current / part
        if not current.exists():
            continue
        try:
            info = os.lstat(current)
        except OSError as exc:
            raise FallbackContractError("PATH_PARENT_INVALID", str(current)) from exc
        if stat.S_ISLNK(info.st_mode) or _is_reparse_stat(info) or not stat.S_ISDIR(info.st_mode):
            raise FallbackContractError("PATH_PARENT_UNSAFE", str(current))


def _decode_safe_text(data: bytes, *, field: str) -> str:
    if data.startswith(b"\xef\xbb\xbf"):
        raise FallbackContractError("UTF8_BOM_FORBIDDEN", field)
    if b"\x00" in data:
        raise FallbackContractError("BINARY_CONTENT_FORBIDDEN", field)
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise FallbackContractError("UTF8_TEXT_REQUIRED", field) from exc
    if "\ufffd" in text:
        raise FallbackContractError("UTF8_TEXT_REQUIRED", field)
    _reject_secret_content(text, field=field)
    return text


def _reject_secret_content(text: str, *, field: str) -> None:
    if _KNOWN_SECRET_RE.search(text):
        raise FallbackContractError("SECRET_CONTENT_REJECTED", field)

    def real_secret(value: str) -> bool:
        candidate = value.strip().strip("\"'").strip()
        folded = candidate.casefold()
        return bool(
            candidate
            and not folded.startswith(_PLACEHOLDER_PREFIXES)
            and len(candidate) >= 8
            and not any(character.isspace() for character in candidate)
        )

    for line in text.splitlines():
        for pattern in (
            _AUTHORIZATION_SECRET_RE,
            _NPM_AUTH_SECRET_RE,
            _CREDENTIAL_URL_RE,
            _TOKEN_USERINFO_URL_RE,
            _URL_QUERY_SECRET_RE,
        ):
            secret = pattern.search(line)
            if secret and real_secret(secret.group("value")):
                raise FallbackContractError("SECRET_CONTENT_REJECTED", field)
        match = _SECRET_ASSIGNMENT_RE.match(line)
        if not match:
            continue
        candidate = match.group("value").strip()
        quoted = len(candidate) >= 2 and candidate[0] in "\"'" and candidate[-1] == candidate[0]
        if quoted:
            candidate = candidate[1:-1].strip()
        else:
            candidate = candidate.strip("\"'").strip()
        if real_secret(candidate) and (quoted or not any(character.isspace() for character in candidate)):
            raise FallbackContractError("SECRET_CONTENT_REJECTED", field)


def _read_safe_file(path: Path, *, max_bytes: int, field: str) -> tuple[bytes, os.stat_result]:
    before = _assert_regular_file(path, code="REGULAR_FILE_REQUIRED")
    if before.st_size > max_bytes:
        raise FallbackContractError("FILE_SIZE_LIMIT_EXCEEDED", f"{field}: {before.st_size}>{max_bytes}")
    try:
        with path.open("rb") as handle:
            data = handle.read(max_bytes + 1)
    except OSError as exc:
        raise FallbackContractError("FILE_READ_FAILED", field) from exc
    after = _assert_regular_file(path, code="REGULAR_FILE_REQUIRED")
    if len(data) != before.st_size or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise FallbackContractError("FILE_CHANGED_DURING_READ", field)
    _decode_safe_text(data, field=field)
    return data, after


def _positive_int(value: Any, *, field: str, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= maximum:
        raise FallbackContractError("LIMIT_INVALID", field)
    return value


def _validate_limits(value: Any) -> ContractLimits:
    if not isinstance(value, Mapping):
        raise FallbackContractError("LIMITS_INVALID")
    fields = {
        "max_evidence_files": HARD_MAX_EVIDENCE_FILES,
        "max_evidence_file_bytes": HARD_MAX_EVIDENCE_FILE_BYTES,
        "max_evidence_total_bytes": HARD_MAX_EVIDENCE_TOTAL_BYTES,
        "max_patch_operations": HARD_MAX_PATCH_OPERATIONS,
        "max_patch_file_bytes": HARD_MAX_PATCH_FILE_BYTES,
        "max_patch_total_bytes": HARD_MAX_PATCH_TOTAL_BYTES,
        "local_gate_timeout_seconds": HARD_MAX_GATE_TIMEOUT_SECONDS,
    }
    _require_exact_keys(value, set(fields), field="limits")
    normalized = {name: _positive_int(value.get(name), field=name, maximum=maximum) for name, maximum in fields.items()}
    if normalized["max_evidence_file_bytes"] > normalized["max_evidence_total_bytes"]:
        raise FallbackContractError("LIMITS_INCONSISTENT", "evidence")
    if normalized["max_patch_file_bytes"] > normalized["max_patch_total_bytes"]:
        raise FallbackContractError("LIMITS_INCONSISTENT", "patch")
    return ContractLimits(**normalized)


def _validate_command(value: Any, *, required: bool) -> tuple[str, ...] | None:
    if value is None:
        if required:
            raise FallbackContractError("LOCAL_GATE_COMMAND_REQUIRED")
        return None
    if not isinstance(value, list) or not value or len(value) > 128:
        raise FallbackContractError("LOCAL_GATE_COMMAND_INVALID")
    try:
        invalid = any(
            not isinstance(item, str)
            or not item
            or "\x00" in item
            or len(item.encode("utf-8", errors="strict")) > 8_192
            for item in value
        )
    except UnicodeError as exc:
        raise FallbackContractError("LOCAL_GATE_COMMAND_INVALID") from exc
    if invalid:
        raise FallbackContractError("LOCAL_GATE_COMMAND_INVALID")
    _reject_secret_content("\n".join(value), field="local_gate_command")
    return tuple(value)


def _bind_gate_command(
    command: tuple[str, ...] | None,
    *,
    root: Path,
    edits: Sequence[EditPath],
) -> str | None:
    if command is None:
        return None
    executable = _canonical_absolute_existing(command[0], kind="LOCAL_GATE_EXECUTABLE")
    info = _assert_regular_file(executable, code="LOCAL_GATE_EXECUTABLE_INVALID")
    if not os.access(executable, os.X_OK) or (os.name != "nt" and not stat.S_IMODE(info.st_mode) & 0o111):
        raise FallbackContractError("LOCAL_GATE_EXECUTABLE_INVALID", str(executable))
    edit_keys = {_path_key(item.absolute_path) for item in edits}
    for argument in command:
        raw = Path(argument).expanduser()
        candidate = Path(os.path.abspath(raw if raw.is_absolute() else root / raw))
        candidate_keys = {_path_key(candidate)}
        try:
            candidate_keys.add(_path_key(candidate.resolve(strict=True)))
        except (OSError, ValueError):
            pass
        if candidate_keys & edit_keys:
            raise FallbackContractError("LOCAL_GATE_EDIT_PATH_OVERLAP", argument)
    return sha256_file(executable)


def validate_contract(value: Mapping[str, Any]) -> FallbackContract:
    """Validate and bind a fallback contract to current immutable inputs."""

    if not isinstance(value, Mapping):
        raise FallbackContractError("CONTRACT_OBJECT_REQUIRED")
    required = {
        "schema",
        "project_root",
        "mission_path",
        "mission_sha256",
        "action_authority",
        "reasoning_level",
        "evidence_allowlist",
        "edit_path_allowlist",
        "local_gate_command",
        "limits",
    }
    _require_exact_keys(value, required, field="contract")
    if value.get("schema") != CONTRACT_SCHEMA:
        raise FallbackContractError("CONTRACT_SCHEMA_INVALID")

    root = _canonical_root(value.get("project_root"))
    mission = _canonical_absolute_existing(value.get("mission_path"), kind="MISSION")
    _assert_regular_file(mission, code="MISSION_FILE_INVALID")
    expected_mission_hash = value.get("mission_sha256")
    if not isinstance(expected_mission_hash, str) or not _SHA256_RE.fullmatch(expected_mission_hash):
        raise FallbackContractError("MISSION_SHA256_INVALID")
    mission_bytes, _ = _read_safe_file(mission, max_bytes=MISSION_MAX_BYTES, field="mission_path")
    if sha256_bytes(mission_bytes) != expected_mission_hash:
        raise FallbackContractError("MISSION_HASH_MISMATCH", str(mission))

    authority = value.get("action_authority")
    if authority not in ACTION_AUTHORITIES:
        raise FallbackContractError("ACTION_AUTHORITY_INVALID")
    reasoning = value.get("reasoning_level")
    if not isinstance(reasoning, str) or not reasoning.strip() or reasoning != reasoning.strip():
        raise FallbackContractError("REASONING_LEVEL_INVALID")
    try:
        reasoning_bytes = reasoning.encode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise FallbackContractError("REASONING_LEVEL_INVALID") from exc
    if len(reasoning_bytes) > 128:
        raise FallbackContractError("REASONING_LEVEL_INVALID")

    limits = _validate_limits(value.get("limits"))
    evidence_values = value.get("evidence_allowlist")
    if not isinstance(evidence_values, list):
        raise FallbackContractError("EVIDENCE_ALLOWLIST_INVALID")
    if len(evidence_values) > limits.max_evidence_files:
        raise FallbackContractError("EVIDENCE_FILE_COUNT_EXCEEDED")
    evidence: list[EvidenceFile] = []
    evidence_keys: set[str] = set()
    evidence_total = 0
    for index, item in enumerate(evidence_values):
        if not isinstance(item, Mapping):
            raise FallbackContractError("EVIDENCE_ENTRY_INVALID", str(index))
        _require_exact_keys(item, {"path", "category", "priority", "sha256"}, field=f"evidence[{index}]")
        relative = _validate_relative_path(item.get("path"), field=f"evidence[{index}].path")
        key = _path_key(relative)
        if key in evidence_keys:
            raise FallbackContractError("EVIDENCE_PATH_DUPLICATE", relative)
        evidence_keys.add(key)
        category = item.get("category")
        priority = item.get("priority")
        expected = item.get("sha256")
        if not isinstance(category, str) or not _CATEGORY_RE.fullmatch(category):
            raise FallbackContractError("EVIDENCE_CATEGORY_INVALID", relative)
        if not isinstance(priority, int) or isinstance(priority, bool) or not 0 <= priority <= 1_000:
            raise FallbackContractError("EVIDENCE_PRIORITY_INVALID", relative)
        if not isinstance(expected, str) or not _SHA256_RE.fullmatch(expected):
            raise FallbackContractError("EVIDENCE_SHA256_INVALID", relative)
        absolute = _workspace_path(root, relative)
        _assert_existing_parents_safe(root, absolute)
        data, info = _read_safe_file(
            absolute, max_bytes=limits.max_evidence_file_bytes, field=f"evidence:{relative}"
        )
        actual = sha256_bytes(data)
        if actual != expected:
            raise FallbackContractError("EVIDENCE_HASH_MISMATCH", relative)
        evidence_total += info.st_size
        if evidence_total > limits.max_evidence_total_bytes:
            raise FallbackContractError("EVIDENCE_TOTAL_SIZE_EXCEEDED")
        evidence.append(EvidenceFile(relative, category, priority, expected, absolute, info.st_size))
    evidence.sort(key=lambda item: (item.priority, item.category, item.path))

    immutable_workspace_keys = set(evidence_keys)
    try:
        mission_relative = mission.relative_to(root).as_posix()
    except ValueError:
        mission_relative = None
    if mission_relative is not None:
        immutable_workspace_keys.add(_path_key(mission_relative))

    edit_values = value.get("edit_path_allowlist")
    if not isinstance(edit_values, list):
        raise FallbackContractError("EDIT_PATH_ALLOWLIST_INVALID")
    edits: list[EditPath] = []
    edit_keys: set[str] = set()
    for index, item in enumerate(edit_values):
        if not isinstance(item, Mapping):
            raise FallbackContractError("EDIT_PATH_ENTRY_INVALID", str(index))
        _require_exact_keys(item, {"path", "before_sha256", "operations"}, field=f"edit_path[{index}]")
        relative = _validate_relative_path(item.get("path"), field=f"edit_path[{index}].path")
        key = _path_key(relative)
        if key in edit_keys:
            raise FallbackContractError("EDIT_PATH_DUPLICATE", relative)
        if key in immutable_workspace_keys:
            raise FallbackContractError("IMMUTABLE_INPUT_EDIT_OVERLAP", relative)
        edit_keys.add(key)
        before_hash = item.get("before_sha256")
        if before_hash is not None and (not isinstance(before_hash, str) or not _SHA256_RE.fullmatch(before_hash)):
            raise FallbackContractError("EDIT_BEFORE_SHA256_INVALID", relative)
        operations = item.get("operations")
        if (
            not isinstance(operations, list)
            or not operations
            or any(not isinstance(operation, str) or operation not in EDIT_OPERATIONS for operation in operations)
            or len(set(operations)) != len(operations)
        ):
            raise FallbackContractError("EDIT_OPERATIONS_INVALID", relative)
        normalized_operations = tuple(sorted(operations))
        if before_hash is None and any(operation in {"update", "delete"} for operation in operations):
            raise FallbackContractError("EDIT_OPERATION_STATE_INVALID", relative)
        if before_hash is not None and "add" in operations:
            raise FallbackContractError("EDIT_OPERATION_STATE_INVALID", relative)
        absolute = _workspace_path(root, relative)
        _assert_existing_parents_safe(root, absolute)
        if before_hash is None:
            if absolute.exists() or absolute.is_symlink():
                raise FallbackContractError("EDIT_PATH_EXPECTED_ABSENT", relative)
            before_mode = None
            before_metadata = None
        else:
            info = _assert_regular_file(absolute, code="EDIT_PATH_REGULAR_FILE_REQUIRED")
            if info.st_size > limits.max_patch_file_bytes:
                raise FallbackContractError("PATCH_FILE_SIZE_EXCEEDED", relative)
            if sha256_file(absolute) != before_hash:
                raise FallbackContractError("EDIT_PATH_HASH_MISMATCH", relative)
            before_mode = stat.S_IMODE(info.st_mode)
            before_metadata, _ = _filesystem_metadata(
                absolute, info, max_payload_bytes=limits.max_patch_file_bytes
            )
        edits.append(
            EditPath(
                relative,
                before_hash,
                normalized_operations,
                absolute,
                before_mode,
                before_metadata,
            )
        )
    edits.sort(key=lambda item: item.path)

    command = _validate_command(value.get("local_gate_command"), required=authority in WRITE_AUTHORITIES)
    if authority == "read-only":
        if edits:
            raise FallbackContractError("READ_ONLY_EDIT_PATHS_FORBIDDEN")
        if command is not None:
            raise FallbackContractError("READ_ONLY_GATE_FORBIDDEN")
    elif not edits:
        raise FallbackContractError("WRITE_EDIT_PATHS_REQUIRED")
    gate_executable_sha256 = _bind_gate_command(command, root=root, edits=edits)

    provisional = {
        "schema": CONTRACT_SCHEMA,
        "project_root": str(root),
        "mission_path": str(mission),
        "mission_sha256": expected_mission_hash,
        "action_authority": authority,
        "reasoning_level": reasoning,
        "evidence_allowlist": [item.contract_value() for item in evidence],
        "edit_path_allowlist": [item.contract_value() for item in edits],
        "local_gate_command": list(command) if command else None,
        "limits": limits.contract_value(),
    }
    contract_hash = sha256_bytes(canonical_json_bytes(provisional))
    return FallbackContract(
        project_root=root,
        mission_path=mission,
        mission_sha256=expected_mission_hash,
        action_authority=authority,
        reasoning_level=reasoning,
        evidence_allowlist=tuple(evidence),
        edit_path_allowlist=tuple(edits),
        local_gate_command=command,
        local_gate_executable_sha256=gate_executable_sha256,
        limits=limits,
        contract_sha256=contract_hash,
    )


def load_contract(path: Path) -> FallbackContract:
    """Load strict UTF-8 JSON from a regular file and validate current bindings."""

    source = Path(path).expanduser()
    info = _assert_regular_file(source, code="CONTRACT_FILE_INVALID")
    if info.st_size > CONTRACT_MAX_BYTES:
        raise FallbackContractError("CONTRACT_SIZE_LIMIT_EXCEEDED")
    try:
        with source.open("rb") as handle:
            raw = handle.read(CONTRACT_MAX_BYTES + 1)
    except OSError as exc:
        raise FallbackContractError("CONTRACT_READ_FAILED", str(source)) from exc
    if raw.startswith(b"\xef\xbb\xbf"):
        raise FallbackContractError("CONTRACT_BOM_FORBIDDEN")
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise FallbackContractError("CONTRACT_JSON_INVALID") from exc
    after = _assert_regular_file(source, code="CONTRACT_FILE_INVALID")
    if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise FallbackContractError("CONTRACT_CHANGED_DURING_READ")
    value = _strict_json_loads(text, code="CONTRACT_JSON_INVALID")
    if not isinstance(value, Mapping):
        raise FallbackContractError("CONTRACT_OBJECT_REQUIRED")
    return validate_contract(value)


def revalidate_contract(contract: FallbackContract) -> FallbackContract:
    """Recheck every path/hash binding represented by a loaded contract."""

    if not isinstance(contract, FallbackContract):
        raise FallbackContractError("FALLBACK_CONTRACT_REQUIRED")
    current = validate_contract(contract.contract_value())
    if current.contract_sha256 != contract.contract_sha256:
        raise FallbackContractError("CONTRACT_BINDING_CHANGED")
    original_modes = {item.path: item.before_mode for item in contract.edit_path_allowlist}
    original_metadata = {item.path: item.before_metadata for item in contract.edit_path_allowlist}
    for item in current.edit_path_allowlist:
        if original_modes.get(item.path) != item.before_mode:
            raise FallbackContractError("EDIT_PATH_MODE_MISMATCH", item.path)
        if original_metadata.get(item.path) != item.before_metadata:
            raise FallbackContractError("EDIT_PATH_METADATA_MISMATCH", item.path)
    if current.local_gate_executable_sha256 != contract.local_gate_executable_sha256:
        raise FallbackContractError("LOCAL_GATE_EXECUTABLE_CHANGED")
    return current


def render_attachment_instructions(contract: FallbackContract) -> str:
    """Render the exact, hash-bound prompt text for a caller-owned submission."""

    contract = revalidate_contract(contract)
    evidence_lines = [
        f"- priority={item.priority} category={item.category} path={item.path} sha256={item.sha256}"
        for item in contract.evidence_allowlist
    ] or ["- none"]
    edit_lines = [
        f"- path={item.path} before_sha256={item.before_sha256 or 'null'} operations={','.join(item.operations)}"
        for item in contract.edit_path_allowlist
    ] or ["- none"]
    sections = [
        "[ORACLE ATTACHMENT FALLBACK CONTRACT]",
        f"schema: {CONTRACT_SCHEMA}",
        f"contract_sha256: {contract.contract_sha256}",
        f"project_root: {contract.project_root}",
        f"mission_path: {contract.mission_path}",
        f"mission_sha256: {contract.mission_sha256}",
        f"action_authority: {contract.action_authority}",
        f"reasoning_level: {contract.reasoning_level}",
        "",
        "The attached mission file and allowlisted evidence are immutable inputs. Verify their listed hashes.",
        "Do not claim to have inspected any unattached file, do not execute commands, and do not exceed the declared paths.",
        "The host, not this session, owns local application and verification.",
        "",
        "[EVIDENCE ATTACHMENT ORDER]",
        *evidence_lines,
        "",
        "[EDIT PATH AUTHORITY]",
        *edit_lines,
    ]
    if contract.action_authority in WRITE_AUTHORITIES:
        exemplar = {
            "schema": PATCH_SCHEMA,
            "contract_sha256": contract.contract_sha256,
            "mission_sha256": contract.mission_sha256,
            "reasoning_level": contract.reasoning_level,
            "operations": [],
        }
        sections.extend(
            [
                "",
                "[REQUIRED OUTPUT]",
                "Return exactly one patch envelope and no text before or after it.",
                "Every operation must be one of add, update, delete, or move and match the allowlist and hashes.",
                "Add/update objects include UTF-8 content and its after_sha256. Delete objects include the bound before_sha256.",
                "Move objects include path, destination, before_sha256, destination_before_sha256=null, and after_sha256.",
                PATCH_BEGIN_MARKER,
                json.dumps(exemplar, ensure_ascii=False, indent=2, sort_keys=True),
                PATCH_END_MARKER,
            ]
        )
    else:
        sections.extend(
            [
                "",
                "[REQUIRED OUTPUT]",
                "Answer the attached mission without proposing or encoding workspace changes.",
            ]
        )
    return "\n".join(sections).strip() + "\n"


def build_attachment_request(contract: FallbackContract) -> dict[str, Any]:
    """Return submission inputs without performing an Oracle submission."""

    contract = revalidate_contract(contract)
    attachments: list[str] = []
    seen: set[str] = set()
    for path in (contract.mission_path, *(item.absolute_path for item in contract.evidence_allowlist)):
        key = _path_key(path)
        if key not in seen:
            seen.add(key)
            attachments.append(str(path))
    return {
        "schema": REQUEST_SCHEMA,
        "contract_sha256": contract.contract_sha256,
        "mission_sha256": contract.mission_sha256,
        "project_root": str(contract.project_root),
        "mission_path": str(contract.mission_path),
        "action_authority": contract.action_authority,
        "reasoning_level": contract.reasoning_level,
        "instructions": render_attachment_instructions(contract),
        "attachments": attachments,
    }


def _edit_map(contract: FallbackContract) -> dict[str, EditPath]:
    return {_path_key(item.path): item for item in contract.edit_path_allowlist}


def _validate_patch_value(value: Mapping[str, Any], contract: FallbackContract) -> dict[str, Any]:
    _require_exact_keys(
        value,
        {"schema", "contract_sha256", "mission_sha256", "reasoning_level", "operations"},
        field="patch",
    )
    if value.get("schema") != PATCH_SCHEMA:
        raise FallbackContractError("PATCH_SCHEMA_INVALID")
    if value.get("contract_sha256") != contract.contract_sha256:
        raise FallbackContractError("PATCH_CONTRACT_HASH_MISMATCH")
    if value.get("mission_sha256") != contract.mission_sha256:
        raise FallbackContractError("PATCH_MISSION_HASH_MISMATCH")
    if value.get("reasoning_level") != contract.reasoning_level:
        raise FallbackContractError("PATCH_REASONING_LEVEL_MISMATCH")
    if contract.action_authority not in WRITE_AUTHORITIES:
        raise FallbackContractError("PATCH_WRITE_AUTHORITY_REQUIRED")
    operations = value.get("operations")
    if not isinstance(operations, list) or not operations:
        raise FallbackContractError("PATCH_OPERATIONS_REQUIRED")
    if len(operations) > contract.limits.max_patch_operations:
        raise FallbackContractError("PATCH_OPERATION_COUNT_EXCEEDED")

    allowlist = _edit_map(contract)
    touched: set[str] = set()
    normalized: list[dict[str, Any]] = []
    total_content_bytes = 0
    for index, raw in enumerate(operations):
        if not isinstance(raw, Mapping):
            raise FallbackContractError("PATCH_OPERATION_INVALID", str(index))
        operation = raw.get("op")
        if operation not in EDIT_OPERATIONS:
            raise FallbackContractError("PATCH_OPERATION_UNKNOWN", str(operation))
        if operation in {"add", "update"}:
            required = {"op", "path", "before_sha256", "content", "after_sha256"}
        elif operation == "delete":
            required = {"op", "path", "before_sha256"}
        else:
            required = {
                "op",
                "path",
                "destination",
                "before_sha256",
                "destination_before_sha256",
                "after_sha256",
            }
        _require_exact_keys(raw, required, field=f"patch.operations[{index}]")
        relative = _validate_relative_path(raw.get("path"), field=f"patch.operations[{index}].path")
        key = _path_key(relative)
        allowed = allowlist.get(key)
        if allowed is None or operation not in allowed.operations:
            raise FallbackContractError("PATCH_PATH_NOT_ALLOWED", relative)
        if key in touched:
            raise FallbackContractError("PATCH_PATH_TOUCHED_MULTIPLE_TIMES", relative)
        touched.add(key)
        if raw.get("before_sha256") != allowed.before_sha256:
            raise FallbackContractError("PATCH_BEFORE_HASH_MISMATCH", relative)

        item: dict[str, Any] = {
            "op": operation,
            "path": relative,
            "before_sha256": allowed.before_sha256,
        }
        if operation in {"add", "update"}:
            content = raw.get("content")
            after_hash = raw.get("after_sha256")
            if not isinstance(content, str):
                raise FallbackContractError("PATCH_CONTENT_INVALID", relative)
            try:
                content_bytes = content.encode("utf-8", errors="strict")
            except UnicodeError as exc:
                raise FallbackContractError("PATCH_CONTENT_UTF8_INVALID", relative) from exc
            if content_bytes.startswith(b"\xef\xbb\xbf") or b"\x00" in content_bytes:
                raise FallbackContractError("PATCH_CONTENT_UTF8_INVALID", relative)
            _reject_secret_content(content, field=f"patch:{relative}")
            if len(content_bytes) > contract.limits.max_patch_file_bytes:
                raise FallbackContractError("PATCH_FILE_SIZE_EXCEEDED", relative)
            total_content_bytes += len(content_bytes)
            if total_content_bytes > contract.limits.max_patch_total_bytes:
                raise FallbackContractError("PATCH_TOTAL_SIZE_EXCEEDED")
            actual_after = sha256_bytes(content_bytes)
            if not isinstance(after_hash, str) or after_hash != actual_after:
                raise FallbackContractError("PATCH_AFTER_HASH_MISMATCH", relative)
            item.update({"content": content, "after_sha256": after_hash})
        elif operation == "move":
            destination = _validate_relative_path(
                raw.get("destination"), field=f"patch.operations[{index}].destination"
            )
            destination_key = _path_key(destination)
            destination_allowed = allowlist.get(destination_key)
            if destination_allowed is None or "move" not in destination_allowed.operations:
                raise FallbackContractError("PATCH_MOVE_DESTINATION_NOT_ALLOWED", destination)
            if destination_key in touched or destination_key == key:
                raise FallbackContractError("PATCH_PATH_TOUCHED_MULTIPLE_TIMES", destination)
            touched.add(destination_key)
            if destination_allowed.before_sha256 is not None or raw.get("destination_before_sha256") is not None:
                raise FallbackContractError("PATCH_MOVE_DESTINATION_EXPECTED_ABSENT", destination)
            if raw.get("after_sha256") != allowed.before_sha256:
                raise FallbackContractError("PATCH_AFTER_HASH_MISMATCH", destination)
            item.update(
                {
                    "destination": destination,
                    "destination_before_sha256": None,
                    "after_sha256": allowed.before_sha256,
                }
            )
        normalized.append(item)
    normalized.sort(key=lambda item: (item["path"], item["op"], item.get("destination", "")))
    return {
        "schema": PATCH_SCHEMA,
        "contract_sha256": contract.contract_sha256,
        "mission_sha256": contract.mission_sha256,
        "reasoning_level": contract.reasoning_level,
        "operations": normalized,
    }


def parse_patch_envelope(
    output_text: str,
    contract: FallbackContract,
    *,
    revalidate_current: bool = True,
) -> dict[str, Any]:
    """Parse exactly one marker-bounded JSON document with no surrounding text."""

    if revalidate_current:
        contract = revalidate_contract(contract)
    if not isinstance(output_text, str):
        raise FallbackContractError("PATCH_OUTPUT_TEXT_REQUIRED")
    try:
        encoded = output_text.encode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise FallbackContractError("PATCH_OUTPUT_UTF8_INVALID") from exc
    maximum = contract.limits.max_patch_total_bytes + 1_048_576
    if len(encoded) > maximum:
        raise FallbackContractError("PATCH_OUTPUT_SIZE_EXCEEDED")
    if output_text.count(PATCH_BEGIN_MARKER) != 1 or output_text.count(PATCH_END_MARKER) != 1:
        raise FallbackContractError("PATCH_ENVELOPE_COUNT_INVALID")
    stripped = output_text.strip()
    if not stripped.startswith(PATCH_BEGIN_MARKER) or not stripped.endswith(PATCH_END_MARKER):
        raise FallbackContractError("PATCH_ENVELOPE_SURROUNDING_TEXT")
    body = stripped[len(PATCH_BEGIN_MARKER) : -len(PATCH_END_MARKER)].strip()
    if not body:
        raise FallbackContractError("PATCH_ENVELOPE_EMPTY")
    value = _strict_json_loads(body, code="PATCH_JSON_INVALID")
    if not isinstance(value, Mapping):
        raise FallbackContractError("PATCH_OBJECT_REQUIRED")
    return _validate_patch_value(value, contract)


def _snapshot_digest(root: str, entries: Sequence[Mapping[str, Any]]) -> str:
    return sha256_bytes(
        canonical_json_bytes({"schema": SNAPSHOT_SCHEMA, "project_root": root, "entries": list(entries)})
    )


def snapshot_workspace(
    project_root: Path,
    *,
    max_entries: int = DEFAULT_SNAPSHOT_MAX_ENTRIES,
    max_file_bytes: int = DEFAULT_SNAPSHOT_MAX_FILE_BYTES,
    max_total_bytes: int = DEFAULT_SNAPSHOT_MAX_TOTAL_BYTES,
    _exclude_transaction: Path | None = None,
) -> dict[str, Any]:
    """Hash the workspace, failing closed on every link or reparse point."""

    root = _canonical_root(str(Path(project_root).expanduser()))
    max_entries = _positive_int(max_entries, field="snapshot.max_entries", maximum=1_000_000)
    max_file_bytes = _positive_int(
        max_file_bytes, field="snapshot.max_file_bytes", maximum=16_000_000_000
    )
    max_total_bytes = _positive_int(
        max_total_bytes, field="snapshot.max_total_bytes", maximum=64_000_000_000
    )
    excluded_key: str | None = None
    if _exclude_transaction is not None:
        excluded = Path(_exclude_transaction)
        if (
            excluded.parent != root
            or not excluded.name.startswith(".codex-oracle-fallback-")
        ):
            raise FallbackContractError("TRANSACTION_PATH_INVALID", str(excluded))
        info = os.lstat(excluded)
        if stat.S_ISLNK(info.st_mode) or _is_reparse_stat(info) or not stat.S_ISDIR(info.st_mode):
            raise FallbackContractError("TRANSACTION_PATH_INVALID", str(excluded))
        excluded_key = _path_key(excluded)
    entries: list[dict[str, Any]] = []
    total_bytes = 0

    def walk(directory: Path, relative_parent: PurePosixPath | None = None) -> None:
        nonlocal total_bytes
        try:
            children = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError as exc:
            raise FallbackContractError("SNAPSHOT_DIRECTORY_READ_FAILED", str(directory)) from exc
        for child in children:
            if excluded_key is not None and _path_key(child.path) == excluded_key:
                continue
            relative = child.name if relative_parent is None else f"{relative_parent.as_posix()}/{child.name}"
            _reject_windows_ambiguous_path(relative, field="snapshot.path")
            try:
                # DirEntry.stat() and os.lstat() can expose different inode
                # placeholders on Windows.  Use the same syscall family on
                # both sides of the hash to make the race predicate stable.
                info = os.lstat(child.path)
            except OSError as exc:
                raise FallbackContractError("SNAPSHOT_ENTRY_READ_FAILED", relative) from exc
            if stat.S_ISLNK(info.st_mode):
                raise FallbackContractError("SNAPSHOT_LINK_FORBIDDEN", relative)
            elif _is_reparse_stat(info):
                raise FallbackContractError("SNAPSHOT_REPARSE_POINT_FORBIDDEN", relative)
            elif stat.S_ISDIR(info.st_mode):
                metadata, _ = _filesystem_metadata(
                    Path(child.path), info, max_payload_bytes=max_file_bytes
                )
                total_bytes += _validate_metadata(metadata, field=f"snapshot:{relative}")
                entry = {
                    "path": relative,
                    "kind": "directory",
                    "mode": stat.S_IMODE(info.st_mode),
                    "metadata": metadata,
                }
            elif stat.S_ISREG(info.st_mode):
                if info.st_size > max_file_bytes:
                    raise FallbackContractError("SNAPSHOT_FILE_SIZE_EXCEEDED", relative)
                total_bytes += info.st_size
                if total_bytes > max_total_bytes:
                    raise FallbackContractError("SNAPSHOT_TOTAL_SIZE_EXCEEDED")
                path = Path(child.path)
                metadata, _ = _filesystem_metadata(path, info, max_payload_bytes=max_file_bytes)
                digest = sha256_file(path)
                after = os.lstat(path)
                after_metadata, _ = _filesystem_metadata(
                    path, after, max_payload_bytes=max_file_bytes
                )
                if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) != (
                    after.st_dev,
                    after.st_ino,
                    after.st_size,
                    after.st_mtime_ns,
                ) or metadata != after_metadata:
                    raise FallbackContractError("SNAPSHOT_RACE", relative)
                total_bytes += _validate_metadata(metadata, field=f"snapshot:{relative}")
                if total_bytes > max_total_bytes:
                    raise FallbackContractError("SNAPSHOT_TOTAL_SIZE_EXCEEDED")
                entry = {
                    "path": relative,
                    "kind": "file",
                    "mode": stat.S_IMODE(info.st_mode),
                    "bytes": info.st_size,
                    "sha256": digest,
                    "metadata": metadata,
                }
            else:
                metadata, _ = _filesystem_metadata(
                    Path(child.path), info, max_payload_bytes=max_file_bytes
                )
                total_bytes += _validate_metadata(metadata, field=f"snapshot:{relative}")
                entry = {
                    "path": relative,
                    "kind": "special",
                    "mode": stat.S_IMODE(info.st_mode),
                    "metadata": metadata,
                }
            if total_bytes > max_total_bytes:
                raise FallbackContractError("SNAPSHOT_TOTAL_SIZE_EXCEEDED")
            entries.append(entry)
            if len(entries) > max_entries:
                raise FallbackContractError("SNAPSHOT_ENTRY_COUNT_EXCEEDED")
            if entry["kind"] == "directory":
                walk(Path(child.path), PurePosixPath(relative))

    walk(root)
    entries.sort(key=lambda item: item["path"])
    root_text = str(root)
    return {
        "schema": SNAPSHOT_SCHEMA,
        "project_root": root_text,
        "entries": entries,
        "entry_count": len(entries),
        "file_bytes": total_bytes,
        "sha256": _snapshot_digest(root_text, entries),
    }


def _validate_snapshot(value: Mapping[str, Any]) -> None:
    _require_exact_keys(
        value,
        {"schema", "project_root", "entries", "entry_count", "file_bytes", "sha256"},
        field="snapshot",
    )
    if value.get("schema") != SNAPSHOT_SCHEMA:
        raise FallbackContractError("SNAPSHOT_SCHEMA_INVALID")
    root_text = value.get("project_root")
    if not isinstance(root_text, str) or not Path(root_text).is_absolute():
        raise FallbackContractError("SNAPSHOT_ROOT_INVALID")
    entries = value.get("entries")
    if not isinstance(entries, list) or any(not isinstance(entry, Mapping) for entry in entries):
        raise FallbackContractError("SNAPSHOT_ENTRIES_INVALID")
    if (
        not isinstance(value.get("entry_count"), int)
        or isinstance(value.get("entry_count"), bool)
        or value.get("entry_count") != len(entries)
    ):
        raise FallbackContractError("SNAPSHOT_COUNT_MISMATCH")
    paths: list[str] = []
    file_bytes = 0
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        path = entry.get("path")
        if (
            not isinstance(path, str)
            or not path
            or "\\" in path
            or _WINDOWS_DRIVE_RE.match(path)
            or PurePosixPath(path).is_absolute()
            or PurePosixPath(path).as_posix() != path
            or any(part in {"", ".", ".."} for part in PurePosixPath(path).parts)
        ):
            raise FallbackContractError("SNAPSHOT_ENTRY_PATH_INVALID", str(index))
        _reject_windows_ambiguous_path(path, field=f"snapshot.entries[{index}].path")
        key = _path_key(path)
        if key in seen:
            raise FallbackContractError("SNAPSHOT_ENTRY_PATH_DUPLICATE", path)
        seen.add(key)
        paths.append(path)
        kind = entry.get("kind")
        mode = entry.get("mode")
        if not isinstance(mode, int) or isinstance(mode, bool) or not 0 <= mode <= 0o7777:
            raise FallbackContractError("SNAPSHOT_ENTRY_MODE_INVALID", path)
        if kind == "file":
            _require_exact_keys(
                entry,
                {"path", "kind", "mode", "bytes", "sha256", "metadata"},
                field=f"snapshot.entries[{index}]",
            )
            size = entry.get("bytes")
            if not isinstance(size, int) or isinstance(size, bool) or size < 0:
                raise FallbackContractError("SNAPSHOT_FILE_SIZE_INVALID", path)
            if not isinstance(entry.get("sha256"), str) or not _SHA256_RE.fullmatch(str(entry.get("sha256"))):
                raise FallbackContractError("SNAPSHOT_FILE_HASH_INVALID", path)
            file_bytes += size
            file_bytes += _validate_metadata(
                entry.get("metadata"), field=f"snapshot.entries[{index}].metadata"
            )
        elif kind in {"symlink", "reparse-point"}:
            raise FallbackContractError("SNAPSHOT_UNSAFE_ENTRY", path)
        elif kind in {"directory", "special"}:
            _require_exact_keys(
                entry, {"path", "kind", "mode", "metadata"}, field=f"snapshot.entries[{index}]"
            )
            file_bytes += _validate_metadata(
                entry.get("metadata"), field=f"snapshot.entries[{index}].metadata"
            )
        else:
            raise FallbackContractError("SNAPSHOT_ENTRY_KIND_INVALID", path)
    if paths != sorted(paths):
        raise FallbackContractError("SNAPSHOT_ENTRY_ORDER_INVALID")
    if (
        not isinstance(value.get("file_bytes"), int)
        or isinstance(value.get("file_bytes"), bool)
        or value.get("file_bytes") != file_bytes
    ):
        raise FallbackContractError("SNAPSHOT_FILE_BYTES_MISMATCH")
    expected = _snapshot_digest(str(value.get("project_root")), entries)
    if value.get("sha256") != expected:
        raise FallbackContractError("SNAPSHOT_HASH_MISMATCH")


def compare_workspace_snapshots(
    before: Mapping[str, Any], after: Mapping[str, Any], *, declared_paths: Iterable[str] = ()
) -> dict[str, Any]:
    """Return all changes and separate explicitly declared file/parent changes."""

    _validate_snapshot(before)
    _validate_snapshot(after)
    if _path_key(str(before.get("project_root"))) != _path_key(str(after.get("project_root"))):
        raise FallbackContractError("SNAPSHOT_ROOT_MISMATCH")
    declared = {_path_key(_validate_relative_path(path, field="declared_paths")) for path in declared_paths}
    before_map = {_path_key(entry["path"]): dict(entry) for entry in before["entries"]}
    after_map = {_path_key(entry["path"]): dict(entry) for entry in after["entries"]}
    changes: list[dict[str, Any]] = []
    declared_changes: list[dict[str, Any]] = []
    undeclared_changes: list[dict[str, Any]] = []
    for key in sorted(set(before_map) | set(after_map)):
        old = before_map.get(key)
        new = after_map.get(key)
        if old == new:
            continue
        if old is None:
            change_type = "added"
            path = str(new["path"])
        elif new is None:
            change_type = "deleted"
            path = str(old["path"])
        else:
            change_type = "modified"
            path = str(new["path"])
        record = {"path": path, "change": change_type, "before": old, "after": new}
        changes.append(record)
        path_key = _path_key(path)
        directory_add_implied = bool(
            change_type == "added"
            and new
            and new.get("kind") == "directory"
            and any(declared_path.startswith(path_key + os.sep) or declared_path.startswith(path_key + "/") for declared_path in declared)
        )
        if path_key in declared or directory_add_implied:
            declared_changes.append(record)
        else:
            undeclared_changes.append(record)
    return {
        "schema": SNAPSHOT_DELTA_SCHEMA,
        "project_root": str(before["project_root"]),
        "before_sha256": str(before["sha256"]),
        "after_sha256": str(after["sha256"]),
        "changes": changes,
        "declared_changes": declared_changes,
        "undeclared_changes": undeclared_changes,
        "eligible": not undeclared_changes,
    }


def _revalidate_edit_state(contract: FallbackContract) -> None:
    for item in contract.edit_path_allowlist:
        _assert_existing_parents_safe(contract.project_root, item.absolute_path)
        if item.before_sha256 is None:
            if item.absolute_path.exists() or item.absolute_path.is_symlink():
                raise FallbackContractError("EDIT_PATH_EXPECTED_ABSENT", item.path)
        else:
            info = _assert_regular_file(item.absolute_path, code="EDIT_PATH_REGULAR_FILE_REQUIRED")
            if sha256_file(item.absolute_path) != item.before_sha256:
                raise FallbackContractError("EDIT_PATH_HASH_MISMATCH", item.path)
            if stat.S_IMODE(info.st_mode) != item.before_mode:
                raise FallbackContractError("EDIT_PATH_MODE_MISMATCH", item.path)
            metadata, _ = _filesystem_metadata(
                item.absolute_path,
                info,
                max_payload_bytes=contract.limits.max_patch_file_bytes,
            )
            if metadata != item.before_metadata:
                raise FallbackContractError("EDIT_PATH_METADATA_MISMATCH", item.path)


def _fsync_directory(path: Path) -> None:
    """Persist a directory entry where the host supports directory fsync."""

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        if os.name == "nt":
            return
        raise
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_stage(path: Path, data: bytes, *, mode: int = 0o644) -> int:
    try:
        with path.open("xb") as handle:
            handle.write(data)
            handle.flush()
            if hasattr(os, "fchmod"):
                os.fchmod(handle.fileno(), mode)
            else:
                os.chmod(path, mode)
            os.fsync(handle.fileno())
        _fsync_directory(path.parent)
        return stat.S_IMODE(os.lstat(path).st_mode)
    except OSError as exc:
        raise FallbackContractError("TRANSACTION_STAGE_FAILED", str(path)) from exc


def _replace_file(source: Path, destination: Path) -> None:
    os.replace(source, destination)


def _link_new_file(source: Path, destination: Path) -> None:
    """Atomically create a new name without ever overwriting a raced writer."""

    os.link(source, destination, follow_symlinks=False)


def _write_new_file_exclusive(path: Path, data: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(path, flags, 0o666)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        _fsync_directory(path.parent)
    except OSError as exc:
        raise FallbackContractError("TRANSACTION_INHERITED_STAGE_FAILED", str(path)) from exc


def _move_file_no_replace(source: Path, destination: Path) -> None:
    """Move one regular-file identity without overwriting a raced destination."""

    if os.name == "nt":
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        move_file = kernel32.MoveFileExW
        move_file.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
        move_file.restype = wintypes.BOOL
        if not move_file(str(source), str(destination), 0x8):  # MOVEFILE_WRITE_THROUGH
            raise OSError(ctypes.get_last_error(), "MoveFileExW")
    else:
        os.link(source, destination, follow_symlinks=False)
        _fsync_directory(destination.parent)
        source.unlink()


def _write_existing_file_in_place(
    path: Path,
    data: bytes,
    *,
    mode: int,
    metadata: Mapping[str, Any],
    restore_metadata: Mapping[str, Any],
    blob_root: Path,
    max_blob_bytes: int,
) -> None:
    """Durably replace the unnamed data stream while retaining file identity."""

    try:
        if os.name == "nt" and int(metadata.get("attributes", 0)) & 0x1:
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            set_attributes = kernel32.SetFileAttributesW
            set_attributes.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
            set_attributes.restype = wintypes.BOOL
            if not set_attributes(str(path), int(metadata["attributes"]) & ~0x1):
                raise OSError(ctypes.get_last_error(), "SetFileAttributesW")
        elif os.name != "nt" and not mode & stat.S_IWUSR:
            os.chmod(path, mode | stat.S_IWUSR, follow_symlinks=False)
        with path.open("r+b", buffering=0) as handle:
            handle.seek(0)
            handle.write(data)
            handle.truncate()
            os.fsync(handle.fileno())
    except Exception as exc:
        raise FallbackContractError("TRANSACTION_IN_PLACE_WRITE_FAILED", str(path)) from exc
    finally:
        _restore_file_metadata(
            path,
            mode,
            metadata,
            restore_metadata,
            blob_root=blob_root,
            max_blob_bytes=max_blob_bytes,
        )
    _fsync_directory(path.parent)


def _backup_matches_original(path: Path, original: Mapping[str, Any]) -> bool:
    try:
        info = _assert_regular_file(path, code="TRANSACTION_BACKUP_MISMATCH")
        return (
            info.st_size == original.get("bytes")
            and info.st_size <= HARD_MAX_PATCH_FILE_BYTES
            and sha256_file(path) == original.get("sha256")
        )
    except FallbackContractError:
        return False


def _state_metadata_matches(actual: Mapping[str, Any], expected: Mapping[str, Any]) -> bool:
    return bool(
        actual.get("exists")
        and expected.get("exists")
        and actual.get("mode") == expected.get("mode")
        and actual.get("metadata") == expected.get("metadata")
    )


def _state_identity_matches(actual: Mapping[str, Any], expected: Mapping[str, Any]) -> bool:
    return bool(
        actual.get("exists")
        and expected.get("exists")
        and isinstance(actual.get("identity"), Mapping)
        and actual.get("identity") == expected.get("identity")
    )


def _remove_transaction_tree(path: Path) -> None:
    if path.parent == path or not path.name.startswith(".codex-oracle-fallback-"):
        raise FallbackContractError("TRANSACTION_PATH_INVALID", str(path))
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode) or _is_reparse_stat(info) or not stat.S_ISDIR(info.st_mode):
        raise FallbackContractError("TRANSACTION_PATH_INVALID", str(path))

    def remove(directory: Path) -> None:
        entries = sorted(
            os.scandir(directory),
            key=lambda entry: (entry.name == "journal.json", entry.name),
        )
        for entry in entries:
            child = Path(entry.path)
            child_info = os.lstat(child)
            if stat.S_ISLNK(child_info.st_mode) or _is_reparse_stat(child_info):
                raise FallbackContractError("TRANSACTION_TREE_UNSAFE", str(child))
            if stat.S_ISDIR(child_info.st_mode):
                remove(child)
            elif stat.S_ISREG(child_info.st_mode):
                child.unlink()
            else:
                raise FallbackContractError("TRANSACTION_TREE_UNSAFE", str(child))
        directory.rmdir()

    parent = path.parent
    seal = _control_seal_path(path)
    remove(path)
    if seal.exists() or seal.is_symlink():
        seal_info = os.lstat(seal)
        if stat.S_ISLNK(seal_info.st_mode) or _is_reparse_stat(seal_info) or not stat.S_ISREG(
            seal_info.st_mode
        ):
            raise FallbackContractError("TRANSACTION_CONTROL_SEAL_INVALID", str(seal))
        seal.unlink()
    _fsync_directory(parent)


def _ensure_target_parents(
    root: Path,
    target: Path,
    *,
    before_create: Any | None = None,
    after_create: Any | None = None,
) -> None:
    missing: list[Path] = []
    current = target.parent
    while current != root and not current.exists():
        missing.append(current)
        current = current.parent
    _assert_existing_parents_safe(root, target)
    for directory in reversed(missing):
        if before_create is not None:
            before_create(directory)
        _assert_existing_parents_safe(root, directory)
        directory.mkdir()
        _fsync_directory(directory.parent)
        if after_create is not None:
            after_create(directory)


def _expected_states(
    contract: FallbackContract,
    patch: Mapping[str, Any],
    *,
    staged_modes: Mapping[int, int] | None = None,
    staged_metadata: Mapping[int, Mapping[str, Any]] | None = None,
) -> dict[str, dict[str, Any] | None]:
    expected: dict[str, dict[str, Any] | None] = {}
    edits = _edit_map(contract)
    staged_modes = staged_modes or {}
    staged_metadata = staged_metadata or {}
    for operation_index, operation in enumerate(patch["operations"]):
        source = edits[_path_key(operation["path"])]
        if operation["op"] in {"add", "update"}:
            expected[operation["path"]] = {
                "sha256": operation["after_sha256"],
                "mode": (
                    source.before_mode
                    if operation["op"] == "update"
                    else staged_modes.get(operation_index, 0o666 if os.name == "nt" else 0o644)
                ),
                "metadata": (
                    source.before_metadata
                    if operation["op"] == "update"
                    else staged_metadata.get(operation_index)
                ),
            }
        elif operation["op"] == "delete":
            expected[operation["path"]] = None
        else:
            expected[operation["path"]] = None
            expected[operation["destination"]] = {
                "sha256": operation["after_sha256"],
                "mode": source.before_mode,
                "metadata": source.before_metadata,
            }
    return expected


def _verify_expected_states(
    root: Path, expected: Mapping[str, Mapping[str, Any] | None]
) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    ok = True
    for relative in sorted(expected):
        wanted = expected[relative]
        path = _workspace_path(root, relative)
        actual: str | None
        actual_mode: int | None = None
        actual_metadata: Mapping[str, Any] | None = None
        state = "absent"
        try:
            if path.exists() or path.is_symlink():
                _assert_existing_parents_safe(root, path)
                info = _assert_regular_file(path, code="EXPECTED_STATE_NOT_REGULAR_FILE")
                actual = sha256_file(path)
                actual_mode = stat.S_IMODE(info.st_mode)
                actual_metadata, _ = _filesystem_metadata(path, info)
                state = "file"
            else:
                actual = None
        except FallbackContractError:
            actual = None
            state = "unsafe"
        expected_hash = None if wanted is None else wanted["sha256"]
        expected_mode = None if wanted is None else wanted.get("mode")
        expected_metadata = None if wanted is None else wanted.get("metadata")
        matches = (
            state != "unsafe"
            and actual == expected_hash
            and (expected_mode is None or actual_mode == expected_mode)
            and (expected_metadata is None or actual_metadata == expected_metadata)
        )
        ok = ok and matches
        records.append(
            {
                "path": relative,
                "expected_sha256": expected_hash,
                "actual_sha256": actual,
                "expected_mode": expected_mode,
                "actual_mode": actual_mode,
                "expected_metadata_sha256": (
                    None if expected_metadata is None else _metadata_sha256(expected_metadata)
                ),
                "actual_metadata_sha256": (
                    None if actual_metadata is None else _metadata_sha256(actual_metadata)
                ),
                "state": state,
                "matches": matches,
            }
        )
    return {"ok": ok, "paths": records}


def _journal_write(transaction: Path, journal: Mapping[str, Any]) -> None:
    data = canonical_json_bytes(journal)
    if len(data) > TRANSACTION_JOURNAL_MAX_BYTES:
        raise FallbackContractError("TRANSACTION_JOURNAL_SIZE_EXCEEDED", str(transaction))
    descriptor, temporary_name = tempfile.mkstemp(prefix="journal-", suffix=".tmp", dir=transaction)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, transaction / "journal.json")
        _fsync_directory(transaction)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise FallbackContractError("TRANSACTION_JOURNAL_WRITE_FAILED", str(transaction)) from exc


def _path_file_state(root: Path, relative: str) -> dict[str, Any]:
    path = _workspace_path(root, relative)
    _assert_existing_parents_safe(root, path)
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        return {
            "exists": False,
            "sha256": None,
            "mode": None,
            "metadata": None,
            "identity": None,
        }
    except OSError as exc:
        raise FallbackContractError("TRANSACTION_STATE_READ_FAILED", relative) from exc
    if stat.S_ISLNK(before.st_mode) or _is_reparse_stat(before) or not stat.S_ISREG(before.st_mode):
        raise FallbackContractError("TRANSACTION_TARGET_UNSAFE", relative)
    before_metadata, _ = _filesystem_metadata(path, before)
    digest = sha256_file(path)
    after = os.lstat(path)
    metadata, _ = _filesystem_metadata(path, after)
    if (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        stat.S_IMODE(before.st_mode),
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        stat.S_IMODE(after.st_mode),
    ) or before_metadata != metadata:
        raise FallbackContractError("TRANSACTION_STATE_RACE", relative)
    return {
        "exists": True,
        "sha256": digest,
        "mode": stat.S_IMODE(after.st_mode),
        "metadata": metadata,
        "bytes": int(after.st_size),
        "identity": {"device": int(after.st_dev), "inode": int(after.st_ino)},
    }


def _state_matches(actual: Mapping[str, Any], expected: Mapping[str, Any]) -> bool:
    if actual.get("exists") is not expected.get("exists"):
        return False
    if not expected.get("exists"):
        return True
    expected_mode = expected.get("mode")
    return actual.get("sha256") == expected.get("sha256") and (
        expected_mode is None or actual.get("mode") == expected_mode
    ) and (
        expected.get("metadata") is None or actual.get("metadata") == expected.get("metadata")
    )


def _journal_state_record(relative: str, value: Mapping[str, Any] | None) -> dict[str, Any]:
    if value is None:
        return {
            "path": relative,
            "exists": False,
            "sha256": None,
            "mode": None,
            "metadata": None,
        }
    return {
        "path": relative,
        "exists": True,
        "sha256": value["sha256"],
        "mode": value.get("mode"),
        "metadata": value.get("metadata"),
    }


def _control_inventory(transaction: Path) -> dict[str, Any]:
    expected_names = {"backup", "stage", "trash", "metadata"}
    actual_names = {
        child.name
        for child in os.scandir(transaction)
        if child.name != "journal.json"
    }
    if actual_names != expected_names:
        raise FallbackContractError("TRANSACTION_CONTROL_INVENTORY_MISMATCH", str(transaction))
    roots: list[dict[str, Any]] = []
    for name in sorted(expected_names):
        path = transaction / name
        info = os.lstat(path)
        if stat.S_ISLNK(info.st_mode) or _is_reparse_stat(info) or not stat.S_ISDIR(info.st_mode):
            raise FallbackContractError("TRANSACTION_CONTROL_INVENTORY_MISMATCH", str(path))
        snapshot = snapshot_workspace(path)
        roots.append(
            {
                "name": name,
                "sha256": snapshot["sha256"],
                "entry_count": snapshot["entry_count"],
                "file_bytes": snapshot["file_bytes"],
            }
        )
    return {
        "sha256": sha256_bytes(canonical_json_bytes(roots)),
        "roots": roots,
    }


def _control_seal_path(transaction: Path) -> Path:
    suffix = transaction.name.removeprefix(".codex-oracle-fallback-")
    return transaction.parent / f"oracle-fallback-seal-{suffix}.json"


def _finalized_transaction_path(transaction: Path) -> Path:
    suffix = transaction.name.removeprefix(".codex-oracle-fallback-")
    return transaction.parent / f"oracle-fallback-finalized-{suffix}.evidence"


def _finalization_marker_path(transaction: Path) -> Path:
    suffix = transaction.name.removeprefix(".codex-oracle-fallback-")
    return transaction.parent / f"oracle-fallback-finalized-{suffix}.json"


def _write_atomic_bytes(path: Path, data: bytes, *, prefix: str) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=prefix, suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            if hasattr(os, "fchmod"):
                os.fchmod(handle.fileno(), 0o600)
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise FallbackContractError("TRANSACTION_HOST_SEAL_WRITE_FAILED", str(path)) from exc


def _control_seal_value(journal: Mapping[str, Any]) -> dict[str, Any]:
    inventory = journal.get("control_inventory")
    return {
        "schema": CONTROL_SEAL_SCHEMA,
        "transaction_id": journal.get("transaction_id"),
        "project_root": journal.get("project_root"),
        "contract_sha256": journal.get("contract_sha256"),
        "patch_sha256": journal.get("patch_sha256"),
        "expected_sha256": journal.get("expected_sha256"),
        "control_inventory_sha256": (
            inventory.get("sha256") if isinstance(inventory, Mapping) else None
        ),
    }


def _write_control_seal(transaction: Path, journal: Mapping[str, Any]) -> None:
    path = _control_seal_path(transaction)
    data = canonical_json_bytes(_control_seal_value(journal))
    _write_atomic_bytes(path, data, prefix="control-seal-")


def _validate_control_seal(transaction: Path, journal: Mapping[str, Any]) -> str:
    path = _control_seal_path(transaction)
    raw = _read_bounded_binary(
        path, max_bytes=65_536, code="TRANSACTION_CONTROL_SEAL_INVALID"
    )
    try:
        value = _strict_json_loads(
            raw.decode("utf-8", errors="strict"), code="TRANSACTION_CONTROL_SEAL_INVALID"
        )
    except UnicodeError as exc:
        raise FallbackContractError("TRANSACTION_CONTROL_SEAL_INVALID", str(path)) from exc
    if not isinstance(value, Mapping) or dict(value) != _control_seal_value(journal):
        raise FallbackContractError("TRANSACTION_CONTROL_SEAL_INVALID", str(path))
    return sha256_bytes(raw)


def _load_transaction_journal(transaction: Path, root: Path) -> dict[str, Any]:
    journal_path = transaction / "journal.json"
    info = _assert_regular_file(journal_path, code="TRANSACTION_JOURNAL_INVALID")
    if info.st_size > TRANSACTION_JOURNAL_MAX_BYTES:
        raise FallbackContractError("TRANSACTION_JOURNAL_INVALID", str(transaction))
    try:
        with journal_path.open("rb") as handle:
            raw = handle.read(TRANSACTION_JOURNAL_MAX_BYTES + 1)
        if len(raw) != info.st_size:
            raise FallbackContractError("TRANSACTION_JOURNAL_INVALID", str(transaction))
        value = _strict_json_loads(
            raw.decode("utf-8", errors="strict"), code="TRANSACTION_JOURNAL_INVALID"
        )
    except (OSError, UnicodeError) as exc:
        raise FallbackContractError("TRANSACTION_JOURNAL_INVALID", str(transaction)) from exc
    if not isinstance(value, Mapping):
        raise FallbackContractError("TRANSACTION_JOURNAL_INVALID", str(transaction))
    required = {
        "schema",
        "transaction_id",
        "project_root",
        "contract_sha256",
        "patch_sha256",
        "phase",
        "operations",
        "originals",
        "expected",
        "expected_sha256",
        "directory_intents",
        "control_inventory",
        "acceptance",
    }
    _require_exact_keys(value, required, field="transaction.journal")
    if (
        value.get("schema") != TRANSACTION_SCHEMA
        or value.get("transaction_id") != transaction.name
        or _path_key(str(value.get("project_root"))) != _path_key(root)
        or value.get("phase")
        not in {
            "preparing",
            "prepared",
            "applying",
            "applied",
            "recovering",
            "committed",
            "accepted",
            "rolled-back",
        }
        or not isinstance(value.get("contract_sha256"), str)
        or not _SHA256_RE.fullmatch(str(value.get("contract_sha256")))
        or not isinstance(value.get("patch_sha256"), str)
        or not _SHA256_RE.fullmatch(str(value.get("patch_sha256")))
        or not isinstance(value.get("expected_sha256"), str)
        or not _SHA256_RE.fullmatch(str(value.get("expected_sha256")))
    ):
        raise FallbackContractError("TRANSACTION_JOURNAL_INVALID", str(transaction))
    originals = value.get("originals")
    expected = value.get("expected")
    operations = value.get("operations")
    directories = value.get("directory_intents")
    if not all(isinstance(items, list) for items in (originals, expected, operations, directories)):
        raise FallbackContractError("TRANSACTION_JOURNAL_INVALID", str(transaction))
    seen_originals: set[str] = set()
    blob_cache: dict[str, bytes] = {}
    for index, item in enumerate(originals):
        if not isinstance(item, Mapping):
            raise FallbackContractError("TRANSACTION_JOURNAL_INVALID", str(transaction))
        _require_exact_keys(
            item,
            {
                "path",
                "exists",
                "sha256",
                "bytes",
                "mode",
                "metadata",
                "identity",
                "restore_metadata",
                "backup",
            },
            field=f"transaction.originals[{index}]",
        )
        relative = _validate_relative_path(item.get("path"), field=f"transaction.originals[{index}].path")
        key = _path_key(relative)
        if key in seen_originals or not isinstance(item.get("exists"), bool):
            raise FallbackContractError("TRANSACTION_JOURNAL_INVALID", str(transaction))
        seen_originals.add(key)
        if item["exists"]:
            if (
                not isinstance(item.get("sha256"), str)
                or not _SHA256_RE.fullmatch(item["sha256"])
                or not isinstance(item.get("mode"), int)
                or isinstance(item.get("mode"), bool)
                or not isinstance(item.get("bytes"), int)
                or isinstance(item.get("bytes"), bool)
                or not 0 <= item.get("bytes") <= HARD_MAX_PATCH_FILE_BYTES
                or not isinstance(item.get("backup"), str)
                or not re.fullmatch(r"[0-9]{4}\.bak", item["backup"])
            ):
                raise FallbackContractError("TRANSACTION_JOURNAL_INVALID", str(transaction))
            identity = item.get("identity")
            if not isinstance(identity, Mapping):
                raise FallbackContractError("TRANSACTION_JOURNAL_INVALID", str(transaction))
            _require_exact_keys(
                identity, {"device", "inode"}, field=f"transaction.originals[{index}].identity"
            )
            if any(
                not isinstance(identity.get(name), int)
                or isinstance(identity.get(name), bool)
                or identity.get(name) < 0
                for name in ("device", "inode")
            ):
                raise FallbackContractError("TRANSACTION_JOURNAL_INVALID", str(transaction))
            _validate_metadata(item.get("metadata"), field=f"transaction.originals[{index}].metadata")
            _validate_restore_metadata(
                item.get("restore_metadata"),
                item["metadata"],
                field=f"transaction.originals[{index}].restore_metadata",
                blob_root=transaction / "metadata",
                max_blob_bytes=HARD_MAX_PATCH_FILE_BYTES,
                blob_cache=blob_cache,
            )
        elif any(
            item.get(name) is not None
            for name in (
                "sha256",
                "bytes",
                "mode",
                "metadata",
                "identity",
                "restore_metadata",
                "backup",
            )
        ):
            raise FallbackContractError("TRANSACTION_JOURNAL_INVALID", str(transaction))
    if sum(item.get("bytes") or 0 for item in originals) > HARD_MAX_PATCH_TOTAL_BYTES:
        raise FallbackContractError("TRANSACTION_BACKUP_TOTAL_SIZE_EXCEEDED", str(transaction))
    metadata_directory = transaction / "metadata"
    if metadata_directory.exists():
        metadata_info = os.lstat(metadata_directory)
        if (
            stat.S_ISLNK(metadata_info.st_mode)
            or _is_reparse_stat(metadata_info)
            or not stat.S_ISDIR(metadata_info.st_mode)
        ):
            raise FallbackContractError("TRANSACTION_METADATA_BLOB_INVALID", str(transaction))
        if value.get("phase") != "preparing":
            blob_names: set[str] = set()
            for child in os.scandir(metadata_directory):
                _assert_regular_file(Path(child.path), code="TRANSACTION_METADATA_BLOB_INVALID")
                if not re.fullmatch(r"[0-9a-f]{64}\.blob", child.name):
                    raise FallbackContractError("TRANSACTION_METADATA_BLOB_INVALID", child.name)
                blob_names.add(child.name)
            if blob_names != set(blob_cache):
                raise FallbackContractError("TRANSACTION_METADATA_BLOB_INVALID", str(transaction))
    elif blob_cache or value.get("phase") != "preparing":
        raise FallbackContractError("TRANSACTION_METADATA_BLOB_INVALID", str(transaction))
    seen_expected: set[str] = set()
    for index, item in enumerate(expected):
        if not isinstance(item, Mapping):
            raise FallbackContractError("TRANSACTION_JOURNAL_INVALID", str(transaction))
        _require_exact_keys(
            item,
            {"path", "exists", "sha256", "mode", "metadata"},
            field=f"transaction.expected[{index}]",
        )
        relative = _validate_relative_path(item.get("path"), field=f"transaction.expected[{index}].path")
        key = _path_key(relative)
        if key in seen_expected or key not in seen_originals or not isinstance(item.get("exists"), bool):
            raise FallbackContractError("TRANSACTION_JOURNAL_INVALID", str(transaction))
        seen_expected.add(key)
        if item["exists"]:
            if (
                not isinstance(item.get("sha256"), str)
                or not _SHA256_RE.fullmatch(item["sha256"])
                or not isinstance(item.get("mode"), int)
                or isinstance(item.get("mode"), bool)
            ):
                raise FallbackContractError("TRANSACTION_JOURNAL_INVALID", str(transaction))
            if item.get("metadata") is not None:
                _validate_metadata(
                    item.get("metadata"), field=f"transaction.expected[{index}].metadata"
                )
        elif any(item.get(name) is not None for name in ("sha256", "mode", "metadata")):
            raise FallbackContractError("TRANSACTION_JOURNAL_INVALID", str(transaction))
    if seen_expected != seen_originals:
        raise FallbackContractError("TRANSACTION_JOURNAL_INVALID", str(transaction))
    if value.get("expected_sha256") != sha256_bytes(canonical_json_bytes(expected)):
        raise FallbackContractError("TRANSACTION_JOURNAL_INVALID", str(transaction))
    inventory = value.get("control_inventory")
    acceptance = value.get("acceptance")
    if value.get("phase") in {"committed", "accepted"}:
        if not isinstance(inventory, Mapping) or set(inventory) != {"sha256", "roots"}:
            raise FallbackContractError("TRANSACTION_CONTROL_INVENTORY_MISMATCH", str(transaction))
        current_inventory = _control_inventory(transaction)
        if dict(inventory) != current_inventory:
            raise FallbackContractError("TRANSACTION_CONTROL_INVENTORY_MISMATCH", str(transaction))
        _validate_control_seal(transaction, value)
    elif inventory is not None:
        raise FallbackContractError("TRANSACTION_JOURNAL_INVALID", str(transaction))
    if value.get("phase") == "accepted":
        if not isinstance(acceptance, Mapping) or set(acceptance) != {"receipt", "episode"}:
            raise FallbackContractError("TRANSACTION_ACCEPTANCE_INVALID", str(transaction))
    elif acceptance is not None:
        raise FallbackContractError("TRANSACTION_JOURNAL_INVALID", str(transaction))
    for index, item in enumerate(operations):
        if not isinstance(item, Mapping):
            raise FallbackContractError("TRANSACTION_JOURNAL_INVALID", str(transaction))
        _require_exact_keys(
            item,
            {"index", "op", "path", "destination", "add_stage", "progress"},
            field=f"transaction.operations[{index}]",
        )
        if item.get("index") != index or item.get("op") not in EDIT_OPERATIONS or not isinstance(item.get("progress"), str):
            raise FallbackContractError("TRANSACTION_JOURNAL_INVALID", str(transaction))
        _validate_relative_path(item.get("path"), field=f"transaction.operations[{index}].path")
        if item.get("destination") is not None:
            _validate_relative_path(item.get("destination"), field=f"transaction.operations[{index}].destination")
        add_stage = item.get("add_stage")
        if item.get("op") == "add":
            relative_stage = _validate_relative_path(
                add_stage, field=f"transaction.operations[{index}].add_stage"
            )
            parent = PurePosixPath(item["path"]).parent
            expected_name = f".codex-oracle-add-{transaction.name}-{index:04d}.tmp"
            expected_stage = (
                expected_name if parent == PurePosixPath(".") else f"{parent.as_posix()}/{expected_name}"
            )
            if relative_stage != expected_stage:
                raise FallbackContractError("TRANSACTION_JOURNAL_INVALID", str(transaction))
        elif add_stage is not None:
            raise FallbackContractError("TRANSACTION_JOURNAL_INVALID", str(transaction))
    for index, item in enumerate(directories):
        if not isinstance(item, Mapping):
            raise FallbackContractError("TRANSACTION_JOURNAL_INVALID", str(transaction))
        _require_exact_keys(item, {"path", "progress"}, field=f"transaction.directory_intents[{index}]")
        _validate_relative_path(item.get("path"), field=f"transaction.directory_intents[{index}].path")
        if item.get("progress") not in {"pending", "creating", "created"}:
            raise FallbackContractError("TRANSACTION_JOURNAL_INVALID", str(transaction))
    return dict(value)


def _recover_transaction(transaction: Path, root: Path) -> None:
    journal = _load_transaction_journal(transaction, root)
    if journal["phase"] in {"preparing", "prepared"}:
        journal["phase"] = "rolled-back"
        _journal_write(transaction, journal)
        _remove_transaction_tree(transaction)
        return
    originals = {item["path"]: dict(item) for item in journal["originals"]}
    expected = {item["path"]: dict(item) for item in journal["expected"]}

    if journal["phase"] in {"committed", "accepted", "rolled-back"}:
        wanted = expected if journal["phase"] in {"committed", "accepted"} else originals
        for relative, state in wanted.items():
            if not _state_matches(_path_file_state(root, relative), state):
                raise FallbackContractError("TRANSACTION_RECOVERY_CONFLICT", relative)
        _remove_transaction_tree(transaction)
        return

    for relative, original in originals.items():
        if original["exists"]:
            backup = transaction / "backup" / original["backup"]
            if not _backup_matches_original(backup, original):
                raise FallbackContractError("TRANSACTION_BACKUP_MISMATCH", relative)

    current_states = {relative: _path_file_state(root, relative) for relative in originals}
    update_partials = {
        item["path"]
        for item in journal["operations"]
        if item["op"] == "update" and item["progress"] != "pending"
    }
    for relative, current in current_states.items():
        ordinary = _state_matches(current, originals[relative]) or _state_matches(
            current, expected[relative]
        )
        partial_update = relative in update_partials and _state_identity_matches(
            current, originals[relative]
        )
        if not (ordinary or partial_update):
            raise FallbackContractError("TRANSACTION_RECOVERY_CONFLICT", relative)

    journal["phase"] = "recovering"
    _journal_write(transaction, journal)
    def reconstruct(relative: str, original: Mapping[str, Any]) -> None:
        target = _workspace_path(root, relative)
        backup = transaction / "backup" / original["backup"]
        data = _read_bounded_binary(
            backup, max_bytes=HARD_MAX_PATCH_FILE_BYTES, code="TRANSACTION_BACKUP_MISMATCH"
        )
        replacement = transaction / f"recovery-{sha256_bytes(relative.encode('utf-8'))[:16]}.tmp"
        replacement.unlink(missing_ok=True)
        _write_stage(replacement, data, mode=original["mode"])
        _ensure_target_parents(root, target)
        _assert_existing_parents_safe(root, target)
        if _path_file_state(root, relative)["exists"]:
            raise FallbackContractError("TRANSACTION_RECOVERY_CONFLICT", relative)
        _replace_file(replacement, target)
        _restore_file_metadata(
            target,
            original["mode"],
            original["metadata"],
            original["restore_metadata"],
            blob_root=transaction / "metadata",
            max_blob_bytes=HARD_MAX_PATCH_FILE_BYTES,
        )
        _fsync_directory(target.parent)

    for operation in reversed(journal["operations"]):
        relative = operation["path"]
        original = originals[relative]
        target = _workspace_path(root, relative)
        if operation["op"] == "add":
            current = _path_file_state(root, relative)
            stage_relative = operation["add_stage"]
            inherited_stage = _workspace_path(root, stage_relative)

            def remove_inherited_stage() -> None:
                if not (inherited_stage.exists() or inherited_stage.is_symlink()):
                    return
                _assert_existing_parents_safe(root, inherited_stage)
                _assert_regular_file(
                    inherited_stage, code="TRANSACTION_RECOVERY_TARGET_UNSAFE"
                )
                inherited_stage.unlink()
                _fsync_directory(inherited_stage.parent)

            if _state_matches(current, original):
                if operation["progress"] == "pending" and (
                    inherited_stage.exists() or inherited_stage.is_symlink()
                ):
                    raise FallbackContractError("TRANSACTION_RECOVERY_CONFLICT", stage_relative)
                remove_inherited_stage()
                continue
            if expected[relative].get("metadata") is None:
                raise FallbackContractError("TRANSACTION_RECOVERY_CONFLICT", relative)
            if not _state_matches(current, expected[relative]):
                raise FallbackContractError("TRANSACTION_RECOVERY_CONFLICT", relative)
            _assert_regular_file(target, code="TRANSACTION_RECOVERY_TARGET_UNSAFE")
            target.unlink()
            _fsync_directory(target.parent)
            remove_inherited_stage()
        elif operation["op"] == "update":
            current = _path_file_state(root, relative)
            if _state_matches(current, original):
                continue
            if not (
                _state_matches(current, expected[relative])
                or _state_identity_matches(current, original)
            ):
                raise FallbackContractError("TRANSACTION_RECOVERY_CONFLICT", relative)
            backup = transaction / "backup" / original["backup"]
            _write_existing_file_in_place(
                target,
                _read_bounded_binary(
                    backup,
                    max_bytes=HARD_MAX_PATCH_FILE_BYTES,
                    code="TRANSACTION_BACKUP_MISMATCH",
                ),
                mode=original["mode"],
                metadata=original["metadata"],
                restore_metadata=original["restore_metadata"],
                blob_root=transaction / "metadata",
                max_blob_bytes=HARD_MAX_PATCH_FILE_BYTES,
            )
        elif operation["op"] == "delete":
            current = _path_file_state(root, relative)
            if _state_matches(current, original):
                continue
            if not _state_matches(current, expected[relative]):
                raise FallbackContractError("TRANSACTION_RECOVERY_CONFLICT", relative)
            trash = transaction / "trash" / f"{operation['index']:04d}.deleted"
            if trash.exists() and _state_matches(
                _path_file_state(transaction / "trash", trash.name), original
            ):
                _ensure_target_parents(root, target)
                _replace_file(trash, target)
                _fsync_directory(target.parent)
            else:
                reconstruct(relative, original)
        else:
            destination_relative = operation["destination"]
            destination = _workspace_path(root, destination_relative)
            current = _path_file_state(root, relative)
            destination_state = _path_file_state(root, destination_relative)
            if _state_matches(current, original) and _state_matches(
                destination_state, originals[destination_relative]
            ):
                continue
            if _state_matches(current, original) and _state_matches(
                destination_state, expected[destination_relative]
            ):
                _assert_regular_file(destination, code="TRANSACTION_RECOVERY_TARGET_UNSAFE")
                destination.unlink()
                _fsync_directory(destination.parent)
                continue
            if not _state_matches(current, expected[relative]) or not _state_matches(
                destination_state, expected[destination_relative]
            ):
                raise FallbackContractError("TRANSACTION_RECOVERY_CONFLICT", relative)
            _ensure_target_parents(root, target)
            _move_file_no_replace(destination, target)
            _fsync_directory(destination.parent)
            _fsync_directory(target.parent)

    directory_paths = sorted(
        (_workspace_path(root, item["path"]) for item in journal["directory_intents"]),
        key=lambda item: len(item.parts),
        reverse=True,
    )
    for directory in directory_paths:
        try:
            info = os.lstat(directory)
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or _is_reparse_stat(info) or not stat.S_ISDIR(info.st_mode):
            raise FallbackContractError("TRANSACTION_RECOVERY_DIRECTORY_UNSAFE", str(directory))
        _assert_existing_parents_safe(root, directory)
        try:
            directory.rmdir()
        except OSError as exc:
            raise FallbackContractError("TRANSACTION_RECOVERY_DIRECTORY_NOT_EMPTY", str(directory)) from exc
        _fsync_directory(directory.parent)
    for relative, original in originals.items():
        if not _state_matches(_path_file_state(root, relative), original):
            raise FallbackContractError("TRANSACTION_RECOVERY_CONFLICT", relative)
    journal["phase"] = "rolled-back"
    _journal_write(transaction, journal)
    _remove_transaction_tree(transaction)


def recover_orphaned_patch_transactions(
    project_root: Path, *, transaction_root: Path | None = None
) -> dict[str, Any]:
    """Rollback durable orphan WALs before later local work.

    This is intentionally an exclusive, local recovery protocol.  It protects
    against crashes and benign races by rechecking parents and exact hashes and
    modes, but it cannot distinguish a live writer from a crashed writer or
    defend a workspace against a malicious local actor that can forge both the
    journal and its fsynced backups.  Callers must serialize patch/recovery work.
    """

    root = _canonical_root(str(Path(project_root).expanduser()))
    sealed_root = _prepare_transaction_root(root, transaction_root)
    recovered: list[str] = []
    for child in sorted(os.scandir(sealed_root), key=lambda item: item.name):
        if not child.name.startswith(".codex-oracle-fallback-"):
            continue
        transaction = Path(child.path)
        info = os.lstat(transaction)
        if stat.S_ISLNK(info.st_mode) or _is_reparse_stat(info) or not stat.S_ISDIR(info.st_mode):
            raise FallbackContractError("TRANSACTION_PATH_INVALID", str(transaction))
        if not any(os.scandir(transaction)):
            transaction.rmdir()
            _fsync_directory(sealed_root)
            recovered.append(child.name)
            continue
        _recover_transaction(transaction, root)
        recovered.append(child.name)
    return {
        "project_root": str(root),
        "transaction_root": str(sealed_root),
        "recovered": recovered,
        "count": len(recovered),
    }


def _expected_states_from_journal(
    journal: Mapping[str, Any],
) -> dict[str, dict[str, Any] | None]:
    return {
        item["path"]: (
            None
            if not item["exists"]
            else {
                "sha256": item["sha256"],
                "mode": item["mode"],
                "metadata": item["metadata"],
            }
        )
        for item in journal["expected"]
    }


def _recover_except_matching_committed(
    root: Path,
    transaction_root: Path,
    *,
    contract_sha256: str,
    patch_sha256: str,
) -> tuple[Path, dict[str, Any]] | None:
    retained: tuple[Path, dict[str, Any]] | None = None
    for child in sorted(os.scandir(transaction_root), key=lambda item: item.name):
        if not child.name.startswith(".codex-oracle-fallback-"):
            continue
        transaction = Path(child.path)
        info = os.lstat(transaction)
        if stat.S_ISLNK(info.st_mode) or _is_reparse_stat(info) or not stat.S_ISDIR(info.st_mode):
            raise FallbackContractError("TRANSACTION_PATH_INVALID", str(transaction))
        if not any(os.scandir(transaction)):
            transaction.rmdir()
            _fsync_directory(transaction_root)
            continue
        journal = _load_transaction_journal(transaction, root)
        if journal["phase"] == "committed":
            if (
                journal["contract_sha256"] != contract_sha256
                or journal["patch_sha256"] != patch_sha256
            ):
                raise FallbackContractError(
                    "TRANSACTION_COMMITTED_PATCH_MISMATCH", transaction.name
                )
            if retained is not None:
                raise FallbackContractError("TRANSACTION_MULTIPLE_COMMITTED_MATCHES")
            expected = _expected_states_from_journal(journal)
            for relative, wanted in expected.items():
                actual = _path_file_state(root, relative)
                if not _state_matches(actual, _journal_state_record(relative, wanted)):
                    raise FallbackContractError("TRANSACTION_RECOVERY_CONFLICT", relative)
            retained = (transaction, journal)
            continue
        if journal["phase"] == "accepted":
            raise FallbackContractError("TRANSACTION_ALREADY_ACCEPTED", transaction.name)
        _recover_transaction(transaction, root)
    return retained


def _apply_transaction(
    contract: FallbackContract, patch: Mapping[str, Any], transaction_root: Path
) -> tuple[dict[str, dict[str, Any] | None], Path]:
    root = contract.project_root
    _revalidate_edit_state(contract)
    touched = sorted(
        {
            operation["path"]
            for operation in patch["operations"]
        }
        | {
            operation["destination"]
            for operation in patch["operations"]
            if operation["op"] == "move"
        }
    )
    transaction = Path(tempfile.mkdtemp(prefix=".codex-oracle-fallback-", dir=transaction_root))
    _fsync_directory(transaction_root)
    backup_dir = transaction / "backup"
    stage_dir = transaction / "stage"
    trash_dir = transaction / "trash"
    metadata_dir = transaction / "metadata"
    journal: dict[str, Any] = {
        "schema": TRANSACTION_SCHEMA,
        "transaction_id": transaction.name,
        "project_root": str(root),
        "contract_sha256": contract.contract_sha256,
        "patch_sha256": sha256_bytes(canonical_json_bytes(patch)),
        "phase": "preparing",
        "operations": [],
        "originals": [],
        "expected": [],
        "expected_sha256": sha256_bytes(canonical_json_bytes([])),
        "directory_intents": [],
        "control_inventory": None,
        "acceptance": None,
    }
    _journal_write(transaction, journal)
    stage_paths: dict[int, Path] = {}
    staged_modes: dict[int, int] = {}

    try:
        backup_dir.mkdir()
        stage_dir.mkdir()
        trash_dir.mkdir()
        metadata_dir.mkdir()
        _fsync_directory(transaction)
        original_records: list[dict[str, Any]] = []
        metadata_total_bytes = 0
        backup_total_bytes = 0
        for index, relative in enumerate(touched):
            state = _path_file_state(root, relative)
            if state["exists"]:
                path = _workspace_path(root, relative)
                metadata, restore_metadata = _filesystem_metadata(
                    path,
                    _assert_regular_file(path, code="TRANSACTION_TARGET_UNSAFE"),
                    include_restore=True,
                    max_payload_bytes=contract.limits.max_patch_file_bytes,
                )
                if metadata != state["metadata"] or restore_metadata is None:
                    raise FallbackContractError("TRANSACTION_STATE_RACE", relative)
                metadata_total_bytes += _validate_metadata(
                    metadata, field=f"transaction.originals:{relative}"
                )
                if metadata_total_bytes > contract.limits.max_patch_total_bytes:
                    raise FallbackContractError("TRANSACTION_METADATA_TOTAL_SIZE_EXCEEDED")
                restore_metadata = _externalize_restore_metadata(
                    restore_metadata, metadata_dir
                )
                data = _read_bounded_binary(
                    path,
                    max_bytes=contract.limits.max_patch_file_bytes,
                    code="TRANSACTION_BACKUP_MISMATCH",
                )
                backup_total_bytes += len(data)
                if backup_total_bytes > contract.limits.max_patch_total_bytes:
                    raise FallbackContractError("TRANSACTION_BACKUP_TOTAL_SIZE_EXCEEDED")
                backup = backup_dir / f"{index:04d}.bak"
                _write_stage(backup, data, mode=0o600)
                if sha256_bytes(data) != state["sha256"]:
                    raise FallbackContractError("TRANSACTION_BACKUP_MISMATCH", relative)
                original_records.append(
                    {
                        "path": relative,
                        "exists": True,
                        "sha256": state["sha256"],
                        "bytes": len(data),
                        "mode": state["mode"],
                        "metadata": metadata,
                        "identity": state["identity"],
                        "restore_metadata": restore_metadata,
                        "backup": backup.name,
                    }
                )
            else:
                original_records.append(
                    {
                        "path": relative,
                        "exists": False,
                        "sha256": None,
                        "bytes": None,
                        "mode": None,
                        "metadata": None,
                        "identity": None,
                        "restore_metadata": None,
                        "backup": None,
                    }
                )
        for index, operation in enumerate(patch["operations"]):
            if operation["op"] in {"add", "update"}:
                data = operation["content"].encode("utf-8", errors="strict")
            else:
                continue
            mode = 0o600 if operation["op"] == "update" else 0o644
            stage = stage_dir / f"{index:04d}.new"
            staged_modes[index] = _write_stage(stage, data, mode=mode)
            stage_paths[index] = stage

        expected = _expected_states(
            contract,
            patch,
            staged_modes=staged_modes,
        )
        planned_directories: set[str] = set()
        for operation in patch["operations"]:
            if operation["op"] not in {"add", "move"}:
                continue
            relative = operation["destination"] if operation["op"] == "move" else operation["path"]
            target = _workspace_path(root, relative)
            current = target.parent
            while current != root and not current.exists():
                planned_directories.add(current.relative_to(root).as_posix())
                current = current.parent
        expected_records = [
            _journal_state_record(relative, expected[relative]) for relative in touched
        ]
        journal.update(
            {
                "phase": "prepared",
                "operations": [
                    {
                        "index": index,
                        "op": operation["op"],
                        "path": operation["path"],
                        "destination": operation.get("destination"),
                        "add_stage": (
                            (
                                f"{PurePosixPath(operation['path']).parent.as_posix()}/"
                                if PurePosixPath(operation["path"]).parent != PurePosixPath(".")
                                else ""
                            )
                            + f".codex-oracle-add-{transaction.name}-{index:04d}.tmp"
                            if operation["op"] == "add"
                            else None
                        ),
                        "progress": "pending",
                    }
                    for index, operation in enumerate(patch["operations"])
                ],
                "originals": original_records,
                "expected": expected_records,
                "expected_sha256": sha256_bytes(canonical_json_bytes(expected_records)),
                "directory_intents": [
                    {"path": relative, "progress": "pending"}
                    for relative in sorted(planned_directories, key=lambda item: len(PurePosixPath(item).parts))
                ],
            }
        )
        _journal_write(transaction, journal)
        _revalidate_edit_state(contract)
        journal["phase"] = "applying"
        _journal_write(transaction, journal)

        def set_operation_progress(index: int, progress: str) -> None:
            journal["operations"][index]["progress"] = progress
            _journal_write(transaction, journal)

        def set_directory_progress(directory: Path, progress: str) -> None:
            relative = directory.relative_to(root).as_posix()
            for item in journal["directory_intents"]:
                if item["path"] == relative:
                    item["progress"] = progress
                    _journal_write(transaction, journal)
                    return
            raise FallbackContractError("TRANSACTION_DIRECTORY_INTENT_MISSING", relative)

        def set_expected_state(relative: str, value: Mapping[str, Any] | None) -> None:
            expected[relative] = None if value is None else dict(value)
            for item_index, item in enumerate(journal["expected"]):
                if item["path"] == relative:
                    journal["expected"][item_index] = _journal_state_record(relative, value)
                    journal["expected_sha256"] = sha256_bytes(
                        canonical_json_bytes(journal["expected"])
                    )
                    _journal_write(transaction, journal)
                    return
            raise FallbackContractError("TRANSACTION_EXPECTED_STATE_MISSING", relative)

        for index, operation in enumerate(patch["operations"]):
            source = _workspace_path(root, operation["path"])
            if operation["op"] == "add":
                _ensure_target_parents(
                    root,
                    source,
                    before_create=lambda directory: set_directory_progress(directory, "creating"),
                    after_create=lambda directory: set_directory_progress(directory, "created"),
                )
                _assert_existing_parents_safe(root, source)
                if _path_file_state(root, operation["path"])["exists"]:
                    raise FallbackContractError("EDIT_PATH_EXPECTED_ABSENT", operation["path"])
                inherited_relative = journal["operations"][index]["add_stage"]
                inherited_stage = _workspace_path(root, inherited_relative)
                _assert_existing_parents_safe(root, inherited_stage)
                if inherited_stage.exists() or inherited_stage.is_symlink():
                    raise FallbackContractError("TRANSACTION_INHERITED_STAGE_CONFLICT", inherited_relative)
                set_operation_progress(index, "inherited-stage-create-intent")
                data = _read_bounded_binary(
                    stage_paths[index],
                    max_bytes=contract.limits.max_patch_file_bytes,
                    code="TRANSACTION_STAGE_FAILED",
                )
                _write_new_file_exclusive(inherited_stage, data)
                inherited_state = _path_file_state(root, inherited_relative)
                if inherited_state["sha256"] != operation["after_sha256"]:
                    raise FallbackContractError("TRANSACTION_INHERITED_STAGE_MISMATCH", inherited_relative)
                set_expected_state(
                    operation["path"],
                    {
                        "sha256": operation["after_sha256"],
                        "mode": inherited_state["mode"],
                        "metadata": inherited_state["metadata"],
                    },
                )
                set_operation_progress(index, "target-create-intent")
                _move_file_no_replace(inherited_stage, source)
                _fsync_directory(source.parent)
                set_operation_progress(index, "complete")
                stage_paths[index].unlink()
            elif operation["op"] == "update":
                set_operation_progress(index, "target-in-place-write-intent")
                _assert_existing_parents_safe(root, source)
                original = next(item for item in original_records if item["path"] == operation["path"])
                if not _state_matches(_path_file_state(root, operation["path"]), original):
                    raise FallbackContractError("EDIT_PATH_STATE_MISMATCH", operation["path"])
                _write_existing_file_in_place(
                    source,
                    _read_bounded_binary(
                        stage_paths[index],
                        max_bytes=contract.limits.max_patch_file_bytes,
                        code="TRANSACTION_STAGE_FAILED",
                    ),
                    mode=original["mode"],
                    metadata=original["metadata"],
                    restore_metadata=original["restore_metadata"],
                    blob_root=metadata_dir,
                    max_blob_bytes=contract.limits.max_patch_file_bytes,
                )
                set_operation_progress(index, "complete")
                stage_paths[index].unlink()
            elif operation["op"] == "delete":
                set_operation_progress(index, "source-remove-intent")
                _assert_existing_parents_safe(root, source)
                original = next(item for item in original_records if item["path"] == operation["path"])
                if not _state_matches(_path_file_state(root, operation["path"]), original):
                    raise FallbackContractError("EDIT_PATH_STATE_MISMATCH", operation["path"])
                _replace_file(source, trash_dir / f"{index:04d}.deleted")
                _fsync_directory(source.parent)
                set_operation_progress(index, "complete")
            else:
                destination = _workspace_path(root, operation["destination"])
                _ensure_target_parents(
                    root,
                    destination,
                    before_create=lambda directory: set_directory_progress(directory, "creating"),
                    after_create=lambda directory: set_directory_progress(directory, "created"),
                )
                set_operation_progress(index, "source-move-intent")
                _assert_existing_parents_safe(root, source)
                original = next(item for item in original_records if item["path"] == operation["path"])
                if not _state_matches(_path_file_state(root, operation["path"]), original):
                    raise FallbackContractError("EDIT_PATH_STATE_MISMATCH", operation["path"])
                _assert_existing_parents_safe(root, destination)
                if _path_file_state(root, operation["destination"])["exists"]:
                    raise FallbackContractError("EDIT_PATH_EXPECTED_ABSENT", operation["destination"])
                _move_file_no_replace(source, destination)
                _fsync_directory(source.parent)
                _fsync_directory(destination.parent)
                set_operation_progress(index, "complete")
        state = _verify_expected_states(root, expected)
        if not state["ok"]:
            raise FallbackContractError("PATCH_POST_APPLY_STATE_MISMATCH")
        journal["control_inventory"] = _control_inventory(transaction)
        journal["phase"] = "committed"
        _write_control_seal(transaction, journal)
        _journal_write(transaction, journal)
        return expected, transaction
    except Exception as exc:
        try:
            _recover_transaction(transaction, root)
            rollback = "succeeded"
        except Exception as rollback_exc:
            rollback = f"failed: {type(rollback_exc).__name__}: {rollback_exc}"
        detail = f"{type(exc).__name__}: {exc}; rollback={rollback}"
        raise FallbackContractError("PATCH_APPLY_FAILED", detail) from exc


def _run_local_gate(contract: FallbackContract) -> dict[str, Any]:
    if contract.local_gate_command is None:
        raise FallbackContractError("LOCAL_GATE_COMMAND_REQUIRED")
    command = list(contract.local_gate_command)
    executable = Path(command[0])
    _assert_regular_file(executable, code="LOCAL_GATE_EXECUTABLE_INVALID")
    executable_sha256 = sha256_file(executable)
    if executable_sha256 != contract.local_gate_executable_sha256:
        raise FallbackContractError("LOCAL_GATE_EXECUTABLE_CHANGED", str(executable))
    gate_environment = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith(("CODEX", "ORACLE", "OPENAI", "CHATGPT"))
    }
    gate_environment["LOCAL_GATE_SANITIZED"] = "1"
    try:
        completed = subprocess.run(
            command,
            cwd=contract.project_root,
            shell=False,
            check=False,
            capture_output=True,
            timeout=contract.limits.local_gate_timeout_seconds,
            env=gate_environment,
        )
        stdout = completed.stdout or b""
        stderr = completed.stderr or b""
        exit_code: int | None = completed.returncode
        timed_out = False
        launch_error = None
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or b""
        stderr = exc.stderr or b""
        if isinstance(stdout, str):
            stdout = stdout.encode("utf-8", errors="replace")
        if isinstance(stderr, str):
            stderr = stderr.encode("utf-8", errors="replace")
        exit_code = None
        timed_out = True
        launch_error = None
    except OSError as exc:
        stdout = b""
        stderr = str(exc).encode("utf-8", errors="replace")
        exit_code = None
        timed_out = False
        launch_error = f"{type(exc).__name__}: {exc}"
    return {
        "command": command,
        "executable_sha256": executable_sha256,
        "cwd": str(contract.project_root),
        "shell": False,
        "environment_sanitized": True,
        "timeout_seconds": contract.limits.local_gate_timeout_seconds,
        "timed_out": timed_out,
        "launch_error": launch_error,
        "exit_code": exit_code,
        "ok": not timed_out and launch_error is None and exit_code == 0,
        "stdout_bytes": len(stdout),
        "stdout_sha256": sha256_bytes(stdout),
        "stderr_bytes": len(stderr),
        "stderr_sha256": sha256_bytes(stderr),
        "output_included": False,
    }


def _snapshot_map(snapshot: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {_path_key(entry["path"]): dict(entry) for entry in snapshot["entries"]}


def _validate_direct_before_snapshot(contract: FallbackContract, before: Mapping[str, Any]) -> None:
    _validate_snapshot(before)
    if _path_key(str(before["project_root"])) != _path_key(contract.project_root):
        raise FallbackContractError("DIRECT_WRITE_SNAPSHOT_ROOT_MISMATCH")
    if sha256_bytes(canonical_json_bytes(contract.contract_value())) != contract.contract_sha256:
        raise FallbackContractError("CONTRACT_BINDING_CHANGED")
    entries = _snapshot_map(before)
    expected_files: dict[str, str] = {
        item.path: item.sha256 for item in contract.evidence_allowlist
    }
    try:
        mission_relative = contract.mission_path.relative_to(contract.project_root).as_posix()
    except ValueError:
        mission_relative = ""
    if mission_relative:
        expected_files[mission_relative] = contract.mission_sha256
    for item in contract.edit_path_allowlist:
        entry = entries.get(_path_key(item.path))
        if item.before_sha256 is None:
            if entry is not None:
                raise FallbackContractError("DIRECT_WRITE_BEFORE_STATE_MISMATCH", item.path)
        elif (
            entry is None
            or entry.get("kind") != "file"
            or entry.get("sha256") != item.before_sha256
            or entry.get("mode") != item.before_mode
            or entry.get("metadata") != item.before_metadata
        ):
            raise FallbackContractError("DIRECT_WRITE_BEFORE_STATE_MISMATCH", item.path)
    for relative, expected in expected_files.items():
        entry = entries.get(_path_key(relative))
        if entry is None or entry.get("kind") != "file" or entry.get("sha256") != expected:
            raise FallbackContractError("DIRECT_WRITE_INPUT_BINDING_MISMATCH", relative)


def _infer_direct_write_operations(
    contract: FallbackContract,
    before: Mapping[str, Any],
    after: Mapping[str, Any],
) -> dict[str, Any]:
    before_map = _snapshot_map(before)
    after_map = _snapshot_map(after)
    edits = _edit_map(contract)
    adds: list[tuple[EditPath, dict[str, Any]]] = []
    deletes: list[tuple[EditPath, dict[str, Any]]] = []
    updates: list[tuple[EditPath, dict[str, Any], dict[str, Any]]] = []
    violations: list[dict[str, str]] = []
    changed_count = 0
    total_bytes = 0

    for item in contract.edit_path_allowlist:
        key = _path_key(item.path)
        old = before_map.get(key)
        new = after_map.get(key)
        if old == new:
            continue
        changed_count += 1
        if old is None and new is not None and new.get("kind") == "file":
            adds.append((item, new))
        elif old is not None and old.get("kind") == "file" and new is None:
            deletes.append((item, old))
        elif (
            old is not None
            and old.get("kind") == "file"
            and new is not None
            and new.get("kind") == "file"
        ):
            metadata_changed = old.get("metadata") != new.get("metadata")
            if old.get("sha256") == new.get("sha256") and (
                old.get("mode") != new.get("mode") or metadata_changed
            ):
                violations.append({"path": item.path, "reason": "DIRECT_WRITE_METADATA_ONLY_CHANGE"})
            elif old.get("mode") != new.get("mode"):
                violations.append({"path": item.path, "reason": "DIRECT_WRITE_MODE_CHANGED"})
            elif metadata_changed:
                violations.append({"path": item.path, "reason": "DIRECT_WRITE_METADATA_CHANGED"})
            else:
                updates.append((item, old, new))
        else:
            violations.append({"path": item.path, "reason": "DIRECT_WRITE_NON_FILE_CHANGE"})

    inferred: list[dict[str, Any]] = []
    remaining_adds = list(adds)
    remaining_deletes: list[tuple[EditPath, dict[str, Any]]] = []
    for source, old in deletes:
        match_index: int | None = None
        if "move" in source.operations:
            for index, (destination, new) in enumerate(remaining_adds):
                if "move" in destination.operations and new.get("sha256") == old.get("sha256"):
                    if (
                        new.get("mode") == old.get("mode")
                        and new.get("metadata") == old.get("metadata")
                    ):
                        match_index = index
                        break
                    violations.append(
                        {
                            "path": destination.path,
                            "reason": (
                                "DIRECT_WRITE_MOVE_MODE_MISMATCH"
                                if new.get("mode") != old.get("mode")
                                else "DIRECT_WRITE_MOVE_METADATA_MISMATCH"
                            ),
                        }
                    )
        if match_index is None:
            remaining_deletes.append((source, old))
            continue
        destination, new = remaining_adds.pop(match_index)
        inferred.append(
            {
                "op": "move",
                "path": source.path,
                "destination": destination.path,
                "before_sha256": old["sha256"],
                "after_sha256": new["sha256"],
            }
        )

    for item, new in remaining_adds:
        if "add" not in item.operations:
            violations.append({"path": item.path, "reason": "DIRECT_WRITE_ADD_NOT_ALLOWED"})
            continue
        inferred.append({"op": "add", "path": item.path, "before_sha256": None, "after_sha256": new["sha256"]})
    for item, old in remaining_deletes:
        if "delete" not in item.operations:
            violations.append({"path": item.path, "reason": "DIRECT_WRITE_DELETE_NOT_ALLOWED"})
            continue
        inferred.append({"op": "delete", "path": item.path, "before_sha256": old["sha256"]})
    for item, old, new in updates:
        if "update" not in item.operations:
            violations.append({"path": item.path, "reason": "DIRECT_WRITE_UPDATE_NOT_ALLOWED"})
            continue
        inferred.append(
            {"op": "update", "path": item.path, "before_sha256": old["sha256"], "after_sha256": new["sha256"]}
        )

    for item, new in [*adds, *((item, new) for item, _old, new in updates)]:
        absolute = _workspace_path(contract.project_root, item.path)
        try:
            data, _ = _read_safe_file(
                absolute,
                max_bytes=contract.limits.max_patch_file_bytes,
                field=f"direct-write:{item.path}",
            )
        except FallbackContractError as exc:
            violations.append({"path": item.path, "reason": exc.code})
            continue
        if sha256_bytes(data) != new.get("sha256"):
            violations.append({"path": item.path, "reason": "DIRECT_WRITE_SNAPSHOT_RACE"})
            continue
        total_bytes += len(data)
    if total_bytes > contract.limits.max_patch_total_bytes:
        violations.append({"path": "", "reason": "DIRECT_WRITE_TOTAL_SIZE_EXCEEDED"})
    if len(inferred) > contract.limits.max_patch_operations or changed_count > contract.limits.max_patch_operations * 2:
        violations.append({"path": "", "reason": "DIRECT_WRITE_OPERATION_COUNT_EXCEEDED"})
    if changed_count == 0:
        violations.append({"path": "", "reason": "DIRECT_WRITE_NO_CHANGE"})
    inferred.sort(key=lambda item: (item["path"], item["op"], item.get("destination", "")))
    return {"ok": not violations, "operations": inferred, "violations": violations}


def _skipped_gate_receipt(contract: FallbackContract, reason: str) -> dict[str, Any]:
    empty_hash = sha256_bytes(b"")
    return {
        "command": list(contract.local_gate_command or ()),
        "executable_sha256": contract.local_gate_executable_sha256,
        "cwd": str(contract.project_root),
        "shell": False,
        "timeout_seconds": contract.limits.local_gate_timeout_seconds,
        "timed_out": False,
        "launch_error": None,
        "exit_code": None,
        "ok": False,
        "skipped": True,
        "skip_reason": reason,
        "stdout_bytes": 0,
        "stdout_sha256": empty_hash,
        "stderr_bytes": 0,
        "stderr_sha256": empty_hash,
        "output_included": False,
    }


def verify_direct_devspace_write(
    contract: FallbackContract, before_snapshot: Mapping[str, Any]
) -> dict[str, Any]:
    """Host-verify a direct DevSpace write without applying or submitting anything.

    The caller captures ``before_snapshot`` before the direct web run.  This
    function accepts only allowlisted file operations, runs the one explicit
    gate when that scope proof passes, and requires the gate itself to leave
    the entire workspace byte-for-byte unchanged.
    """

    if not isinstance(contract, FallbackContract):
        raise FallbackContractError("FALLBACK_CONTRACT_REQUIRED")
    if contract.action_authority not in WRITE_AUTHORITIES:
        raise FallbackContractError("DIRECT_WRITE_AUTHORITY_REQUIRED")
    _validate_direct_before_snapshot(contract, before_snapshot)
    after_write = snapshot_workspace(contract.project_root)
    declared = [item.path for item in contract.edit_path_allowlist]
    write_delta = compare_workspace_snapshots(before_snapshot, after_write, declared_paths=declared)
    operation_proof = _infer_direct_write_operations(contract, before_snapshot, after_write)
    write_scope_ok = bool(write_delta["eligible"] and operation_proof["ok"])

    if not write_scope_ok:
        gate = _skipped_gate_receipt(contract, "DIRECT_WRITE_SCOPE_INVALID")
        after_gate = after_write
        gate_delta = compare_workspace_snapshots(after_write, after_gate, declared_paths=())
    else:
        gate = _run_local_gate(contract)
        gate["skipped"] = False
        gate["skip_reason"] = None
        after_gate = snapshot_workspace(contract.project_root)
        gate_delta = compare_workspace_snapshots(after_write, after_gate, declared_paths=())
    gate_clean = not gate_delta["changes"]
    accepted = bool(write_scope_ok and gate["ok"] and gate_clean)
    return {
        "schema": DIRECT_WRITE_ACCEPTANCE_SCHEMA,
        "contract_sha256": contract.contract_sha256,
        "mission_sha256": contract.mission_sha256,
        "project_root": str(contract.project_root),
        "reasoning_level": contract.reasoning_level,
        "write_scope_ok": write_scope_ok,
        "operation_proof": operation_proof,
        "write_delta": write_delta,
        "gate": gate,
        "gate_clean": gate_clean,
        "gate_delta": gate_delta,
        "snapshots": {
            "before": dict(before_snapshot),
            "after_write": after_write,
            "after_gate": after_gate,
        },
        "accepted": accepted,
    }


def _verify_immutable_contract_bindings(contract: FallbackContract) -> None:
    if not isinstance(contract, FallbackContract):
        raise FallbackContractError("FALLBACK_CONTRACT_REQUIRED")
    if sha256_bytes(canonical_json_bytes(contract.contract_value())) != contract.contract_sha256:
        raise FallbackContractError("CONTRACT_BINDING_CHANGED")
    root = _canonical_root(str(contract.project_root))
    if _path_key(root) != _path_key(contract.project_root):
        raise FallbackContractError("CONTRACT_BINDING_CHANGED")
    mission, _ = _read_safe_file(contract.mission_path, max_bytes=MISSION_MAX_BYTES, field="mission_path")
    if sha256_bytes(mission) != contract.mission_sha256:
        raise FallbackContractError("MISSION_HASH_MISMATCH", str(contract.mission_path))
    for item in contract.evidence_allowlist:
        _assert_existing_parents_safe(root, item.absolute_path)
        data, _ = _read_safe_file(
            item.absolute_path,
            max_bytes=contract.limits.max_evidence_file_bytes,
            field=f"evidence:{item.path}",
        )
        if sha256_bytes(data) != item.sha256:
            raise FallbackContractError("EVIDENCE_HASH_MISMATCH", item.path)
    if contract.local_gate_command is not None:
        executable = Path(contract.local_gate_command[0])
        _assert_regular_file(executable, code="LOCAL_GATE_EXECUTABLE_INVALID")
        if sha256_file(executable) != contract.local_gate_executable_sha256:
            raise FallbackContractError("LOCAL_GATE_EXECUTABLE_CHANGED", str(executable))


def _contract_preimage_states(contract: FallbackContract) -> dict[str, dict[str, Any]]:
    return {
        item.path: {
            "exists": item.before_sha256 is not None,
            "sha256": item.before_sha256,
            "mode": item.before_mode,
            "metadata": item.before_metadata,
        }
        for item in contract.edit_path_allowlist
    }


def _classify_patch_workspace_state(
    contract: FallbackContract,
    patch: Mapping[str, Any],
) -> tuple[str, dict[str, dict[str, Any] | None]]:
    preimages = _contract_preimage_states(contract)
    touched_expected = _expected_states(contract, patch)
    postimages: dict[str, dict[str, Any]] = {path: dict(value) for path, value in preimages.items()}
    for path, value in touched_expected.items():
        postimages[path] = _journal_state_record(path, value)
        postimages[path].pop("path")
    current = {path: _path_file_state(contract.project_root, path) for path in preimages}
    if all(_state_matches(current[path], preimages[path]) for path in preimages):
        return "preimage", touched_expected
    unbound_add_metadata = any(
        operation["op"] == "add"
        and touched_expected[operation["path"]] is not None
        and touched_expected[operation["path"]].get("metadata") is None
        for operation in patch["operations"]
    )
    if not unbound_add_metadata and all(
        _state_matches(current[path], postimages[path]) for path in postimages
    ):
        return "postimage", touched_expected
    return "conflict", touched_expected


def resume_or_apply_patch_envelope(
    contract: FallbackContract,
    patch: Mapping[str, Any],
    *,
    baseline_snapshot: Mapping[str, Any] | None = None,
    transaction_root: Path | None = None,
    retain_prepared_transaction: bool = False,
) -> dict[str, Any]:
    """Recover orphan WALs, then idempotently apply or verify a local patch.

    This helper never submits web work.  A fully matching postimage is verified
    without replaying file mutations; every other non-preimage state fails
    closed.
    """

    if not isinstance(contract, FallbackContract):
        raise FallbackContractError("FALLBACK_CONTRACT_REQUIRED")
    if baseline_snapshot is not None:
        _validate_direct_before_snapshot(contract, baseline_snapshot)
    _verify_immutable_contract_bindings(contract)
    normalized = _validate_patch_value(patch, contract)
    patch_sha256 = sha256_bytes(canonical_json_bytes(normalized))
    sealed_root = _prepare_transaction_root(contract.project_root, transaction_root)
    retained = _recover_except_matching_committed(
        contract.project_root,
        sealed_root,
        contract_sha256=contract.contract_sha256,
        patch_sha256=patch_sha256,
    )
    transaction: Path | None = None
    if retained is not None:
        transaction, retained_journal = retained
        expected = _expected_states_from_journal(retained_journal)
        after_apply = snapshot_workspace(
            contract.project_root
        )
        before = dict(baseline_snapshot) if baseline_snapshot is not None else after_apply
    else:
        workspace_state, expected = _classify_patch_workspace_state(contract, normalized)
        if workspace_state == "conflict":
            _revalidate_edit_state(contract)
            raise FallbackContractError("PATCH_WORKSPACE_STATE_CONFLICT")
        if workspace_state == "preimage":
            contract = revalidate_contract(contract)
            normalized = _validate_patch_value(normalized, contract)
            current_preimage = snapshot_workspace(contract.project_root)
            before = (
                dict(baseline_snapshot)
                if baseline_snapshot is not None
                else current_preimage
            )
            expected, transaction = _apply_transaction(contract, normalized, sealed_root)
            after_apply = snapshot_workspace(
                contract.project_root
            )
        else:
            after_apply = snapshot_workspace(contract.project_root)
            before = dict(baseline_snapshot) if baseline_snapshot is not None else after_apply
    after_apply_state = _verify_expected_states(contract.project_root, expected)
    if not after_apply_state["ok"]:
        raise FallbackContractError("PATCH_POST_APPLY_STATE_MISMATCH")
    declared = sorted(expected)
    apply_delta = compare_workspace_snapshots(before, after_apply, declared_paths=declared)
    control_before = snapshot_workspace(transaction) if transaction is not None else None
    gate = _run_local_gate(contract)
    after_gate = snapshot_workspace(
        contract.project_root
    )
    gate_delta = compare_workspace_snapshots(after_apply, after_gate, declared_paths=declared)
    total_delta = compare_workspace_snapshots(before, after_gate, declared_paths=declared)
    final_state = _verify_expected_states(contract.project_root, expected)
    control_clean = True
    if transaction is not None:
        control_after = snapshot_workspace(transaction)
        control_delta = compare_workspace_snapshots(control_before, control_after)
        control_clean = not control_delta["changes"]
        current_journal = _load_transaction_journal(transaction, contract.project_root)
        control_clean = bool(
            control_clean
            and current_journal["phase"] == "committed"
            and current_journal["contract_sha256"] == contract.contract_sha256
            and current_journal["patch_sha256"] == patch_sha256
            and _expected_states_from_journal(current_journal) == expected
        )
    fallback_eligible = bool(
        gate["ok"] and total_delta["eligible"] and final_state["ok"] and control_clean
    )
    prepared_transaction: dict[str, Any] | None = None
    if transaction is not None and final_state["ok"] and control_clean:
        prepared_journal = _load_transaction_journal(transaction, contract.project_root)
        prepared_transaction = {
            "transaction_root": str(sealed_root),
            "transaction_id": transaction.name,
            "contract_sha256": contract.contract_sha256,
            "patch_sha256": patch_sha256,
            "expected_sha256": prepared_journal["expected_sha256"],
            "control_inventory_sha256": prepared_journal["control_inventory"]["sha256"],
            "control_seal_sha256": _validate_control_seal(transaction, prepared_journal),
            "journal_sha256": sha256_file(transaction / "journal.json"),
            "retained": bool(retain_prepared_transaction),
        }
    if (
        transaction is not None
        and final_state["ok"]
        and control_clean
        and not retain_prepared_transaction
    ):
        _remove_transaction_tree(transaction)
    return {
        "schema": APPLY_RESULT_SCHEMA,
        "contract_sha256": contract.contract_sha256,
        "mission_sha256": contract.mission_sha256,
        "project_root": str(contract.project_root),
        "reasoning_level": contract.reasoning_level,
        "applied": True,
        "operations": [
            {key: value for key, value in operation.items() if key != "content"}
            for operation in normalized["operations"]
        ],
        "expected_states_sha256": sha256_bytes(
            canonical_json_bytes(
                [_journal_state_record(relative, expected[relative]) for relative in sorted(expected)]
            )
        ),
        "expected_state_after_gate": final_state,
        "prepared_transaction": prepared_transaction,
        "gate": gate,
        "snapshots": {"before": before, "after_apply": after_apply, "after_gate": after_gate},
        "apply_delta": apply_delta,
        "gate_delta": gate_delta,
        "total_delta": total_delta,
        "fallback_eligible": fallback_eligible,
    }


def apply_patch_envelope(contract: FallbackContract, patch: Mapping[str, Any]) -> dict[str, Any]:
    """Recover, apply once, and run the explicit gate without web submission."""

    return resume_or_apply_patch_envelope(contract, patch)


def _load_hashed_json_reference(reference: Mapping[str, Any], *, code: str) -> dict[str, Any]:
    if not isinstance(reference, Mapping) or set(reference) != {"path", "sha256"}:
        raise FallbackContractError(code)
    path = Path(str(reference["path"])).expanduser()
    if not path.is_absolute():
        raise FallbackContractError(code)
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise FallbackContractError(code, str(path)) from exc
    if _path_key(path) != _path_key(resolved):
        raise FallbackContractError(code, str(path))
    raw = _read_bounded_binary(
        resolved, max_bytes=TRANSACTION_JOURNAL_MAX_BYTES, code=code
    )
    if sha256_bytes(raw) != reference["sha256"]:
        raise FallbackContractError(code, str(path))
    try:
        value = _strict_json_loads(raw.decode("utf-8", errors="strict"), code=code)
    except UnicodeError as exc:
        raise FallbackContractError(code, str(path)) from exc
    if not isinstance(value, Mapping):
        raise FallbackContractError(code, str(path))
    return dict(value)


def finalize_prepared_patch_envelope(
    contract: FallbackContract,
    patch: Mapping[str, Any],
    prepared_receipt: Mapping[str, Any],
    *,
    receipt_reference: Mapping[str, Any],
    episode_reference: Mapping[str, Any],
) -> dict[str, Any]:
    """Durably accept a retained prepared transaction after host references exist."""

    if not isinstance(contract, FallbackContract):
        raise FallbackContractError("FALLBACK_CONTRACT_REQUIRED")
    normalized = _validate_patch_value(patch, contract)
    patch_sha256 = sha256_bytes(canonical_json_bytes(normalized))
    transaction_reference = prepared_receipt.get("prepared_transaction")
    if not isinstance(transaction_reference, Mapping):
        raise FallbackContractError("PREPARED_TRANSACTION_REFERENCE_INVALID")
    required = {
        "transaction_root",
        "transaction_id",
        "contract_sha256",
        "patch_sha256",
        "expected_sha256",
        "control_inventory_sha256",
        "control_seal_sha256",
        "journal_sha256",
        "retained",
    }
    if set(transaction_reference) != required or transaction_reference.get("retained") is not True:
        raise FallbackContractError("PREPARED_TRANSACTION_REFERENCE_INVALID")
    if (
        not isinstance(receipt_reference, Mapping)
        or set(receipt_reference) != {"path", "sha256"}
        or not isinstance(episode_reference, Mapping)
        or set(episode_reference) != {"path", "sha256"}
    ):
        raise FallbackContractError("TRANSACTION_ACCEPTANCE_INVALID")
    sealed_root = _prepare_transaction_root(
        contract.project_root, Path(str(transaction_reference["transaction_root"]))
    )
    transaction_id = str(transaction_reference["transaction_id"])
    if not transaction_id.startswith(".codex-oracle-fallback-"):
        raise FallbackContractError("PREPARED_TRANSACTION_REFERENCE_INVALID")
    transaction = sealed_root / transaction_id
    normalized_receipt_reference = {
        "path": str(Path(str(receipt_reference["path"])).resolve(strict=True)),
        "sha256": str(receipt_reference["sha256"]),
    }
    normalized_episode_reference = {
        "path": str(Path(str(episode_reference["path"])).resolve(strict=True)),
        "sha256": str(episode_reference["sha256"]),
    }
    marker_value = {
        "schema": "codex.chatgpt.oracle-patch-finalization/v1",
        "prepared_transaction": dict(transaction_reference),
        "receipt": normalized_receipt_reference,
        "episode": normalized_episode_reference,
    }
    if not transaction.exists():
        persisted_receipt = _load_hashed_json_reference(
            receipt_reference, code="PREPARED_RECEIPT_REFERENCE_INVALID"
        )
        persisted_episode = _load_hashed_json_reference(
            episode_reference, code="PREPARED_EPISODE_REFERENCE_INVALID"
        )
        marker_path = _finalization_marker_path(transaction)
        marker_raw = _read_bounded_binary(
            marker_path, max_bytes=TRANSACTION_JOURNAL_MAX_BYTES, code="TRANSACTION_FINALIZATION_INVALID"
        )
        marker = _strict_json_loads(
            marker_raw.decode("utf-8", errors="strict"), code="TRANSACTION_FINALIZATION_INVALID"
        )
        if (
            marker != marker_value
            or persisted_receipt.get("prepared_transaction") != dict(transaction_reference)
            or persisted_episode.get("apply_receipt", {}).get("receipt_sha256")
            != receipt_reference.get("sha256")
            or not _finalized_transaction_path(transaction).is_dir()
        ):
            raise FallbackContractError("TRANSACTION_FINALIZATION_INVALID")
        return {
            "finalized": True,
            "transaction_id": transaction_id,
            "phase": "finalized",
            "evidence_path": str(_finalized_transaction_path(transaction)),
            "receipt_sha256": str(receipt_reference["sha256"]),
            "episode_sha256": str(episode_reference["sha256"]),
        }
    journal = _load_transaction_journal(transaction, contract.project_root)
    if (
        journal["phase"] not in {"committed", "accepted"}
        or journal["contract_sha256"] != contract.contract_sha256
        or journal["patch_sha256"] != patch_sha256
        or transaction_reference.get("contract_sha256") != contract.contract_sha256
        or transaction_reference.get("patch_sha256") != patch_sha256
        or transaction_reference.get("expected_sha256") != journal["expected_sha256"]
        or transaction_reference.get("control_inventory_sha256")
        != journal["control_inventory"]["sha256"]
        or transaction_reference.get("control_seal_sha256")
        != _validate_control_seal(transaction, journal)
    ):
        raise FallbackContractError("PREPARED_TRANSACTION_BINDING_MISMATCH")
    if journal["phase"] == "committed" and transaction_reference.get("journal_sha256") != sha256_file(
        transaction / "journal.json"
    ):
        raise FallbackContractError("PREPARED_TRANSACTION_BINDING_MISMATCH")
    persisted_receipt = _load_hashed_json_reference(
        receipt_reference, code="PREPARED_RECEIPT_REFERENCE_INVALID"
    )
    if (
        persisted_receipt.get("fallback_eligible") is not True
        or persisted_receipt.get("prepared_transaction") != dict(transaction_reference)
    ):
        raise FallbackContractError("PREPARED_RECEIPT_REFERENCE_INVALID")
    persisted_episode = _load_hashed_json_reference(
        episode_reference, code="PREPARED_EPISODE_REFERENCE_INVALID"
    )
    apply_reference = persisted_episode.get("apply_receipt")
    if (
        persisted_episode.get("ok") is not True
        or not isinstance(apply_reference, Mapping)
        or apply_reference.get("receipt_path") != receipt_reference.get("path")
        or apply_reference.get("receipt_sha256") != receipt_reference.get("sha256")
        or apply_reference.get("prepared_transaction") != dict(transaction_reference)
    ):
        raise FallbackContractError("PREPARED_EPISODE_REFERENCE_INVALID")
    expected = _expected_states_from_journal(journal)
    if not _verify_expected_states(contract.project_root, expected)["ok"]:
        raise FallbackContractError("PREPARED_TRANSACTION_WORKSPACE_MISMATCH")
    if journal["phase"] == "committed":
        journal["acceptance"] = {
            "receipt": normalized_receipt_reference,
            "episode": normalized_episode_reference,
        }
        journal["phase"] = "accepted"
        _journal_write(transaction, journal)
    elif journal["acceptance"] != {
        "receipt": normalized_receipt_reference,
        "episode": normalized_episode_reference,
    }:
        raise FallbackContractError("TRANSACTION_ACCEPTANCE_INVALID")
    _write_atomic_bytes(
        _finalization_marker_path(transaction),
        canonical_json_bytes(marker_value),
        prefix="finalization-",
    )
    finalized_path = _finalized_transaction_path(transaction)
    if finalized_path.exists():
        raise FallbackContractError("TRANSACTION_FINALIZATION_INVALID", str(finalized_path))
    os.replace(transaction, finalized_path)
    _fsync_directory(sealed_root)
    return {
        "finalized": True,
        "transaction_id": transaction_id,
        "phase": "finalized",
        "evidence_path": str(finalized_path),
        "receipt_sha256": str(receipt_reference["sha256"]),
        "episode_sha256": str(episode_reference["sha256"]),
    }


__all__ = [
    "ACTION_AUTHORITIES",
    "APPLY_RESULT_SCHEMA",
    "CONTRACT_SCHEMA",
    "DIRECT_WRITE_ACCEPTANCE_SCHEMA",
    "PATCH_BEGIN_MARKER",
    "PATCH_END_MARKER",
    "PATCH_SCHEMA",
    "REQUEST_SCHEMA",
    "SNAPSHOT_DELTA_SCHEMA",
    "SNAPSHOT_SCHEMA",
    "TRANSACTION_SCHEMA",
    "ContractLimits",
    "EditPath",
    "EvidenceFile",
    "FallbackContract",
    "FallbackContractError",
    "apply_patch_envelope",
    "build_attachment_request",
    "canonical_json_bytes",
    "compare_workspace_snapshots",
    "finalize_prepared_patch_envelope",
    "load_contract",
    "parse_patch_envelope",
    "recover_orphaned_patch_transactions",
    "revalidate_contract",
    "render_attachment_instructions",
    "resume_or_apply_patch_envelope",
    "sha256_bytes",
    "sha256_file",
    "snapshot_workspace",
    "validate_contract",
    "verify_direct_devspace_write",
]
