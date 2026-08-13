from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


SCHEMA = "codex.chatgpt.agbrowse-run/v1"
WEB_MULTI_MANIFEST_SCHEMAS = {
    "codex.chatgpt.web-multi/v1",
    "codex.chatgpt.web-multi/v2",
}
PARENT_FAMILIES = {"web-multi", "parallel-implementation"}
PARALLEL_IMPLEMENTATION_MANIFEST_SCHEMA = "codex.chatgpt.comprehensive-workflow/v3"
PARALLEL_IMPLEMENTATION_FEATURE_ENV = "CODEX_CHATGPT_PARALLEL_IMPLEMENTATION_V1"
PARALLEL_IMPLEMENTATION_FEATURE_KEY = "parallel_implementation_v1"
WEB_MULTI_V2_ALLOWED_KEYS = {
    "schema",
    "workflow_id",
    "project_root",
    "question",
    "source_snapshot_path",
    "source_snapshot_sha256",
    "output_dir",
    "chatgpt_app_name",
    "planner_policy",
    "semantics_version",
    "max_iterations",
    "mode_variant",
    "agbrowse_contract",
    "agbrowse_contract_sha256",
    "provider_failure_retry_limit",
    "provider_parallel_limit",
    "app_decision_path",
    "chatgpt_app_server_url",
    "timeout_seconds",
    "send_timeout_seconds",
    "session_show_timeout_seconds",
    "recovery_timeout_seconds",
    "safe_pre_submit_retry_limit",
    "pre_submit_retry_deadline_seconds",
    "inline_recovery_round_limit",
    "wave_submission_barrier_timeout_seconds",
    "retry_of_workflow_id",
    "provider_failure_retry_index",
    "provider_failure_parent_run_id",
}


def validate_web_multi_parent_manifest(manifest: dict[str, Any]) -> None:
    schema = str(manifest.get("schema") or "")
    if schema not in WEB_MULTI_MANIFEST_SCHEMAS:
        raise StateError(
            "PARENT_MANIFEST_SCHEMA_INVALID",
            "web Multi-GPT parent manifest schema is required",
        )
    if schema != "codex.chatgpt.web-multi/v2":
        return
    unknown = set(manifest) - WEB_MULTI_V2_ALLOWED_KEYS
    if unknown:
        raise StateError(
            "PARENT_MANIFEST_V2_KEYS_INVALID",
            "web Multi-GPT v2 manifest keys are not exact",
            {"unknown": sorted(unknown)},
        )
    if "solver_count" in manifest:
        raise StateError(
            "PARENT_MANIFEST_V2_SOLVER_COUNT_FORBIDDEN",
            "solver_count is forbidden in dynamic v2 manifests",
        )
    if str(manifest.get("planner_policy") or "") not in {
        "upstream-nonempty-prefix10",
        "strict-6-10",
    }:
        raise StateError("PARENT_MANIFEST_V2_POLICY_INVALID", "invalid Planner policy")
    if str(manifest.get("semantics_version") or "") != "upstream-parity-v1":
        raise StateError("PARENT_MANIFEST_V2_SEMANTICS_INVALID", "invalid runtime semantics version")
    mode_variant = str(manifest.get("mode_variant") or "High")
    if mode_variant not in {"High", "Very High"}:
        raise StateError(
            "PARENT_MANIFEST_V2_MODE_VARIANT_INVALID",
            "web Multi-GPT v2 supports exact mode_variant High or Very High",
        )
    if "agbrowse_contract_sha256" in manifest:
        supplied_contract_sha256 = manifest.get("agbrowse_contract_sha256")
        if not isinstance(supplied_contract_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", supplied_contract_sha256) is None:
            raise StateError(
                "PARENT_AGBROWSE_CONTRACT_SHA256_INVALID",
                "agbrowse_contract_sha256 must be exactly 64 lowercase hexadecimal characters",
            )
        contract = Path(
            str(
                manifest.get("agbrowse_contract")
                or Path.home() / ".codex" / "contracts" / "agbrowse-0.1.18.json"
            )
        ).expanduser().resolve()
        if not contract.is_file():
            raise StateError("PARENT_AGBROWSE_CONTRACT_MISSING", str(contract))
        actual_contract_sha256 = sha256_file(contract)
        if supplied_contract_sha256 != actual_contract_sha256:
            raise StateError(
                "PARENT_AGBROWSE_CONTRACT_SHA256_MISMATCH",
                "agbrowse_contract_sha256 does not match the resolved agbrowse_contract file",
                {
                    "path": str(contract),
                    "expected": supplied_contract_sha256,
                    "actual": actual_contract_sha256,
                },
            )


def validate_parallel_implementation_parent_manifest(
    manifest: dict[str, Any],
    *,
    environ: dict[str, str] | os._Environ[str] | None = None,
) -> None:
    """Admit v3 only after both explicit gates are present."""
    env = os.environ if environ is None else environ
    features = manifest.get("features") if isinstance(manifest.get("features"), dict) else {}
    if str(manifest.get("schema") or "") != PARALLEL_IMPLEMENTATION_MANIFEST_SCHEMA:
        raise StateError("PARALLEL_IMPLEMENTATION_SCHEMA_REQUIRED", "parallel implementation requires workflow v3")
    if features.get(PARALLEL_IMPLEMENTATION_FEATURE_KEY) is not True:
        raise StateError("PARALLEL_IMPLEMENTATION_MANIFEST_GATE_REQUIRED", "parallel implementation manifest gate is absent")
    if str(env.get(PARALLEL_IMPLEMENTATION_FEATURE_ENV) or "") != "1":
        raise StateError("PARALLEL_IMPLEMENTATION_ENV_GATE_REQUIRED", "parallel implementation environment gate is absent")


def validate_parent_family_manifest(
    parent_family: str,
    manifest: dict[str, Any],
    *,
    environ: dict[str, str] | os._Environ[str] | None = None,
) -> None:
    if parent_family not in PARENT_FAMILIES:
        raise StateError("PARENT_FAMILY_INVALID", "parent_family is not registered", {"parent_family": parent_family})
    if parent_family == "web-multi":
        validate_web_multi_parent_manifest(manifest)
        return
    validate_parallel_implementation_parent_manifest(manifest, environ=environ)


def classify_parent_family(candidate: dict[str, Any]) -> str | None:
    """Classify explicit parents and strict read-only legacy Web Multi state."""
    if str(candidate.get("record_kind") or "") != "parent":
        return None
    explicit = candidate.get("parent_family")
    if explicit is not None:
        family = str(explicit)
        return family if family in PARENT_FAMILIES else None
    requested = candidate.get("requested") if isinstance(candidate.get("requested"), dict) else {}
    if (
        requested.get("workflow") == "web-multi-gpt"
        and str(candidate.get("parent_run_id") or "") == str(candidate.get("run_id") or "")
        and isinstance(candidate.get("workflow_id"), str)
        and bool(candidate.get("workflow_id"))
        and isinstance(candidate.get("lease_nonce"), str)
        and bool(candidate.get("lease_nonce"))
        and isinstance(candidate.get("children"), list)
        and isinstance(candidate.get("phase_events"), list)
    ):
        return "web-multi"
    return None


CANONICAL_CHAT_RE = re.compile(r"^https://chatgpt\.com/c/[A-Za-z0-9_-]+(?:[?#].*)?$")
PROMPT_FILE_HANDOFF = (
    "The attached prompt file is the user-provided task instruction for this conversation, "
    "not reference or webpage content. Read it completely and follow it. "
    "Return only the output format requested by that file."
)
MAX_PROMPT_FILE_BYTES = 2_000_000

PHASES = {
    "CREATED",
    "PREFLIGHTED",
    "LEASED",
    "SEND_STARTED",
    "SUBMITTED",
    "URL_BOUND",
    "RESPONSE_IN_PROGRESS",
    "RESULT_CAPTURED",
    "VERIFIED",
    "COMPLETE",
    "COMPLETE_SUPERSEDED",
    "PREFLIGHT_BLOCKED",
    "SEND_REJECTED",
    "PROVIDER_FAILED_TERMINAL",
    "SUBMISSION_UNCERTAIN_IDENTITY_MISSING",
    "RECOVERY_REQUIRED",
    "RECOVERING",
    "BLOCKED_RECOVERY_EXHAUSTED",
    "BLOCKED_MANIFEST_MISMATCH",
    "BLOCKED_OWNER_MISMATCH",
    "BLOCKED_TARGET_AMBIGUOUS",
    "BLOCKED_APP_TRANSACTION",
    "CANCELLED_PRE_SUBMISSION",
    "USER_STOP_REQUESTED",
    "ABANDONED_UNCERTAIN",
}

ALLOWED_TRANSITIONS = {
    "CREATED": {"PREFLIGHTED", "PREFLIGHT_BLOCKED", "BLOCKED_MANIFEST_MISMATCH"},
    "PREFLIGHTED": {"LEASED", "PREFLIGHT_BLOCKED", "BLOCKED_APP_TRANSACTION"},
    "LEASED": {"SEND_STARTED", "PREFLIGHT_BLOCKED", "BLOCKED_APP_TRANSACTION"},
    "SEND_STARTED": {"SUBMITTED", "SEND_REJECTED", "SUBMISSION_UNCERTAIN_IDENTITY_MISSING", "RECOVERY_REQUIRED", "RECOVERING"},
    "SUBMITTED": {"URL_BOUND", "RECOVERY_REQUIRED", "RECOVERING", "PROVIDER_FAILED_TERMINAL", "SUBMISSION_UNCERTAIN_IDENTITY_MISSING", "BLOCKED_TARGET_AMBIGUOUS"},
    "URL_BOUND": {"RESPONSE_IN_PROGRESS", "RESULT_CAPTURED", "RECOVERY_REQUIRED", "PROVIDER_FAILED_TERMINAL"},
    "RESPONSE_IN_PROGRESS": {"RESULT_CAPTURED", "RECOVERY_REQUIRED", "RECOVERING", "PROVIDER_FAILED_TERMINAL", "BLOCKED_RECOVERY_EXHAUSTED"},
    "RECOVERY_REQUIRED": {
        "RECOVERING",
        "SEND_REJECTED",
        "BLOCKED_RECOVERY_EXHAUSTED",
        "BLOCKED_MANIFEST_MISMATCH",
        "BLOCKED_OWNER_MISMATCH",
        "BLOCKED_TARGET_AMBIGUOUS",
        "PROVIDER_FAILED_TERMINAL",
    },
    "RECOVERING": {
        "URL_BOUND",
        "RESPONSE_IN_PROGRESS",
        "RESULT_CAPTURED",
        "RECOVERY_REQUIRED",
        "BLOCKED_RECOVERY_EXHAUSTED",
        "BLOCKED_TARGET_AMBIGUOUS",
        "PROVIDER_FAILED_TERMINAL",
    },
    "RESULT_CAPTURED": {"VERIFIED", "RECOVERY_REQUIRED"},
    "VERIFIED": {"COMPLETE", "RECOVERY_REQUIRED"},
    "PREFLIGHT_BLOCKED": {"PREFLIGHTED", "CANCELLED_PRE_SUBMISSION"},
    "SEND_REJECTED": {"PREFLIGHTED", "CANCELLED_PRE_SUBMISSION"},
    "BLOCKED_APP_TRANSACTION": {"PREFLIGHTED", "CANCELLED_PRE_SUBMISSION"},
    "SUBMISSION_UNCERTAIN_IDENTITY_MISSING": {"SEND_REJECTED", "RECOVERING"},
    "BLOCKED_RECOVERY_EXHAUSTED": {"RECOVERING", "SEND_REJECTED"},
    "USER_STOP_REQUESTED": {"RECOVERING"},
}

TERMINAL_PHASES = {
    "COMPLETE",
    "COMPLETE_SUPERSEDED",
    "PROVIDER_FAILED_TERMINAL",
    "SUBMISSION_UNCERTAIN_IDENTITY_MISSING",
    "BLOCKED_RECOVERY_EXHAUSTED",
    "BLOCKED_MANIFEST_MISMATCH",
    "BLOCKED_OWNER_MISMATCH",
    "BLOCKED_TARGET_AMBIGUOUS",
    "BLOCKED_APP_TRANSACTION",
    "CANCELLED_PRE_SUBMISSION",
    "ABANDONED_UNCERTAIN",
}

PARENT_PHASES = {
    "PARENT_CREATED",
    "PARENT_ACTIVE",
    "PARENT_DRAINING",
    "PARENT_RECOVERY_REQUIRED",
    "PARENT_COMPLETE",
    "PARENT_FAILED_CLOSED",
    # This spelling is intentionally shared with the child vocabulary.  Every
    # parent operation below branches on record_kind before interpreting it.
    "USER_STOP_REQUESTED",
}

PARENT_TERMINAL_PHASES = {"PARENT_COMPLETE", "PARENT_FAILED_CLOSED"}

CHILD_SAFE_TERMINAL_PHASES = {
    "COMPLETE",
    "CANCELLED_PRE_SUBMISSION",
    "SEND_REJECTED",
    "PROVIDER_FAILED_TERMINAL",
    "PREFLIGHT_BLOCKED",
    "BLOCKED_APP_TRANSACTION",
    "ABANDONED_UNCERTAIN",
}

UNCERTAIN_OR_SUBMITTED_PHASES = {
    "SEND_STARTED",
    "SUBMITTED",
    "URL_BOUND",
    "RESPONSE_IN_PROGRESS",
    "RESULT_CAPTURED",
    "VERIFIED",
    "RECOVERY_REQUIRED",
    "RECOVERING",
    "SUBMISSION_UNCERTAIN_IDENTITY_MISSING",
    "BLOCKED_RECOVERY_EXHAUSTED",
    "BLOCKED_MANIFEST_MISMATCH",
    "BLOCKED_OWNER_MISMATCH",
    "BLOCKED_TARGET_AMBIGUOUS",
    "USER_STOP_REQUESTED",
}

SAFE_STALE_PRE_SUBMISSION_PHASES = {
    "CREATED",
    "PREFLIGHTED",
    "LEASED",
    "PREFLIGHT_BLOCKED",
    "BLOCKED_APP_TRANSACTION",
}

REQUIRED_IMMUTABLE = {
    "schema",
    "run_id",
    "project_root",
    "project_key",
    "manifest_path",
    "manifest_sha256",
    "prompt_sha256",
    "requested",
    "agbrowse",
    "owner",
    "created_at",
}


class StateError(RuntimeError):
    def __init__(self, code: str, message: str, evidence: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.evidence = evidence or {}

    def envelope(self) -> dict[str, Any]:
        return {"ok": False, "error": {"code": self.code, "message": str(self), "evidence": self.evidence}}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def immutable_file_snapshot(path: Path, *, embed_bytes: bool = True) -> dict[str, Any]:
    """Describe one exact regular file without following mutable aliases."""
    raw = path.expanduser()
    try:
        resolved = raw.resolve(strict=True)
        info = resolved.stat()
        is_reparse = bool(
            raw.is_symlink()
            or (hasattr(os.path, "isjunction") and os.path.isjunction(raw))
        )
        if is_reparse or not resolved.is_file() or not stat.S_ISREG(info.st_mode):
            raise OSError("not a regular non-reparse file")
        data = resolved.read_bytes()
    except (OSError, RuntimeError, ValueError) as exc:
        raise StateError(
            "IMMUTABLE_SOURCE_FILE_INVALID",
            "source evidence must be one readable regular non-reparse file",
            {"path": str(raw)},
        ) from exc
    value = {
        "path": str(raw),
        "resolved_path": str(resolved),
        "sha256": sha256_bytes(data),
        "bytes": len(data),
        "regular_file": True,
        "symlink": False,
        "reparse_point": False,
    }
    if embed_bytes:
        value["bytes_base64"] = base64.b64encode(data).decode("ascii")
    return value


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for attempt in range(40):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if os.name != "nt" or attempt == 39:
                try:
                    tmp.unlink()
                except OSError:
                    pass
                raise
            time.sleep(min(0.01 * (attempt + 1), 0.1))


def write_immutable_json_exclusive(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    """Publish a small immutable descriptor, accepting only byte-identical retries."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise StateError("IMMUTABLE_DESCRIPTOR_PATH_INVALID", "immutable descriptor cannot be a symlink")
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    except FileExistsError:
        if not path.is_file() or path.is_symlink() or path.read_bytes() != data:
            raise StateError("IMMUTABLE_DESCRIPTOR_CONFLICT", "immutable descriptor already exists with different bytes")
    else:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    return {"path": str(path), "sha256": sha256_bytes(data), "bytes": len(data)}


@contextmanager
def exclusive_state_lock(path: Path, timeout_seconds: int = 120):
    """Cross-process one-byte lock used for parent create/drain transitions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    if path.stat().st_size == 0:
        handle.write(b"\0")
        handle.flush()
    deadline = time.monotonic() + max(1, timeout_seconds)
    locked = False
    try:
        while not locked:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except OSError:
                if time.monotonic() >= deadline:
                    raise StateError(
                        "PARENT_TRANSITION_LOCK_TIMEOUT",
                        "timed out waiting for the parent transition lock",
                        {"path": str(path)},
                    )
                time.sleep(0.05)
        yield
    finally:
        if locked:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def read_json(path: Path) -> dict[str, Any]:
    """Read an atomically published state file, tolerating a Windows replace race.

    ``write_json_atomic`` publishes through ``os.replace``.  Antivirus/indexing
    hooks and concurrent readers can nevertheless observe a short missing or
    partially available window on Windows.  Retry only those transient read or
    JSON-decode failures; a persistently malformed state still fails closed.
    """
    last_error: Exception | None = None
    for attempt in range(6):
        try:
            value = json.loads(path.read_text(encoding="utf-8-sig"))
            if not isinstance(value, dict):
                raise StateError("STATE_INVALID", f"JSON object required: {path}")
            return value
        except StateError:
            raise
        except (FileNotFoundError, PermissionError, OSError, UnicodeError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt < 5:
                time.sleep(0.02 * (attempt + 1))
                continue
            break
        except Exception as exc:
            last_error = exc
            break
    raise StateError("STATE_UNREADABLE", f"cannot read JSON state: {path}", {"detail": str(last_error)}) from last_error


def load_manifest(path: Path) -> dict[str, Any]:
    raw = path.read_text(encoding="utf-8-sig")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        try:
            import yaml  # type: ignore

            value = yaml.safe_load(raw)
        except Exception as exc:
            raise StateError("MANIFEST_INVALID", f"manifest is not valid JSON/YAML: {path}", {"detail": str(exc)}) from exc
    if not isinstance(value, dict):
        raise StateError("MANIFEST_INVALID", "manifest root must be an object")
    return value


def prompt_contract(manifest: dict[str, Any], *, require_file: bool = False) -> dict[str, Any]:
    transport = str(manifest.get("prompt_transport") or "inline").strip().casefold()
    if transport == "file":
        raw_path = str(manifest.get("prompt_file") or "").strip()
        expected_sha256 = str(manifest.get("prompt_file_sha256") or "").strip().casefold()
        if not raw_path or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
            raise StateError(
                "PROMPT_FILE_CONTRACT_INVALID",
                "file prompt transport requires prompt_file and lowercase prompt_file_sha256",
            )
        try:
            prompt_file = Path(raw_path).expanduser().resolve(strict=True)
        except (OSError, RuntimeError, ValueError) as exc:
            raise StateError("PROMPT_FILE_INVALID", "prompt file is unavailable", {"path": raw_path}) from exc
        if not prompt_file.is_file() or prompt_file.is_symlink():
            raise StateError("PROMPT_FILE_INVALID", "prompt file must be a regular non-symlink file")
        data = prompt_file.read_bytes()
        if not data or len(data) > MAX_PROMPT_FILE_BYTES:
            raise StateError(
                "PROMPT_FILE_INVALID",
                f"prompt file must contain 1..{MAX_PROMPT_FILE_BYTES} bytes",
                {"bytes": len(data)},
            )
        actual_sha256 = sha256_bytes(data)
        if actual_sha256 != expected_sha256:
            raise StateError(
                "PROMPT_FILE_HASH_MISMATCH",
                "prompt file bytes do not match prompt_file_sha256",
                {"expected": expected_sha256, "actual": actual_sha256, "path": str(prompt_file)},
            )
        try:
            prompt_text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise StateError("PROMPT_FILE_ENCODING_INVALID", "prompt file must be strict UTF-8") from exc
        if not prompt_text.strip() or "\ufffd" in prompt_text:
            raise StateError("PROMPT_FILE_ENCODING_INVALID", "prompt file is empty or contains replacement characters")
        outer_prompt = str(manifest.get("question") or manifest.get("prompt") or "")
        if outer_prompt != PROMPT_FILE_HANDOFF:
            raise StateError(
                "PROMPT_HANDOFF_INVALID",
                "file prompt transport requires the exact short composer handoff",
            )
        files = manifest.get("files") or []
        if isinstance(files, str):
            files = [files]
        resolved_files: list[Path] = []
        for item in files:
            try:
                resolved_files.append(Path(str(item)).expanduser().resolve(strict=True))
            except (OSError, RuntimeError, ValueError) as exc:
                raise StateError("ATTACHMENT_INVALID", "prompt attachment list contains an unavailable path") from exc
        if sum(path == prompt_file for path in resolved_files) != 1:
            raise StateError(
                "PROMPT_FILE_ATTACHMENT_MISMATCH",
                "prompt_file must appear exactly once in files, including when a ZIP is also attached",
                {"prompt_file": str(prompt_file)},
            )
        return {
            "transport": "file",
            "prompt_text": prompt_text,
            "prompt_sha256": actual_sha256,
            "prompt_file": str(prompt_file),
            "prompt_file_bytes": len(data),
            "dispatch_text": PROMPT_FILE_HANDOFF,
        }
    if require_file:
        raise StateError(
            "PROMPT_FILE_REQUIRED",
            "ChatGPT web submissions require an immutable prompt file; inline task prompts are forbidden",
            {"transport": transport},
        )
    for key in ("question", "prompt"):
        if manifest.get(key) is not None:
            prompt_text = str(manifest[key])
            return {
                "transport": "inline-legacy",
                "prompt_text": prompt_text,
                "prompt_sha256": sha256_bytes(prompt_text.encode("utf-8")),
                "prompt_file": None,
                "prompt_file_bytes": None,
                "dispatch_text": prompt_text,
            }
    raise StateError("PROMPT_MISSING", "manifest requires question or prompt")


def canonical_project_root(value: str | os.PathLike[str]) -> Path:
    path = Path(value).expanduser()
    if not path.exists() or not path.is_dir():
        raise StateError("PROJECT_ROOT_INVALID", f"project root must be an existing directory: {path}")
    return path.resolve()


def project_key(path: Path) -> str:
    normalized = os.path.normcase(str(path.resolve())).rstrip("\\/")
    return sha256_bytes(normalized.encode("utf-8"))[:24]


def canonical_conversation_url(value: str | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not CANONICAL_CHAT_RE.fullmatch(text):
        raise StateError("CONVERSATION_URL_INVALID", "canonical https://chatgpt.com/c/<id> URL required", {"value": text})
    return text


def process_identity(pid: int | None = None) -> dict[str, Any]:
    selected = os.getpid() if pid is None else int(pid)
    creation_time: float | None = None
    alive = False
    try:
        import psutil  # type: ignore

        try:
            proc = psutil.Process(selected)
            creation_time = float(proc.create_time())
            alive = proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
            return {"pid": selected, "creation_time": creation_time, "alive": alive}
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            return {"pid": selected, "creation_time": None, "alive": False}
        except psutil.AccessDenied:
            return {"pid": selected, "creation_time": None, "alive": True}
    except ImportError:
        pass

    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            process_query_limited_information = 0x1000
            still_active = 259
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            handle = kernel32.OpenProcess(process_query_limited_information, False, selected)
            if not handle:
                return {"pid": selected, "creation_time": None, "alive": False}
            try:
                exit_code = wintypes.DWORD()
                created = wintypes.FILETIME()
                exited = wintypes.FILETIME()
                kernel = wintypes.FILETIME()
                user = wintypes.FILETIME()
                if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    alive = int(exit_code.value) == still_active
                if kernel32.GetProcessTimes(
                    handle,
                    ctypes.byref(created),
                    ctypes.byref(exited),
                    ctypes.byref(kernel),
                    ctypes.byref(user),
                ):
                    ticks = (int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime)
                    creation_time = (ticks / 10_000_000) - 11_644_473_600
            finally:
                kernel32.CloseHandle(handle)
            return {"pid": selected, "creation_time": creation_time, "alive": alive}
        except Exception:
            return {"pid": selected, "creation_time": None, "alive": False}

    try:
        completed = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(selected)],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        value = completed.stdout.strip()
        alive = completed.returncode == 0 and bool(value)
        if alive:
            creation_time = datetime.strptime(value, "%a %b %d %H:%M:%S %Y").astimezone().timestamp()
    except (OSError, subprocess.SubprocessError, ValueError):
        try:
            os.kill(selected, 0)
            alive = True
        except OSError:
            alive = False
    return {"pid": selected, "creation_time": creation_time, "alive": alive}


def same_process(identity: dict[str, Any]) -> bool:
    pid = int(identity.get("pid") or 0)
    if pid <= 0:
        return False
    current = process_identity(pid)
    if not current["alive"]:
        return False
    expected = identity.get("creation_time")
    actual = current.get("creation_time")
    if expected is None or actual is None:
        return True
    return abs(float(expected) - float(actual)) < 0.01


def _prompt_text(manifest: dict[str, Any]) -> str:
    return str(prompt_contract(manifest)["prompt_text"])


def recovery_prompt_alias_name(run_id: str, manifest: dict[str, Any]) -> str:
    """Return a readable but still run-owned recovery attachment name."""
    correlation = manifest.get("workflow_correlation")
    stage = str(correlation.get("stage") or "") if isinstance(correlation, dict) else ""
    label = re.sub(r"[^a-z0-9]+", "-", stage.casefold()).strip("-")[:48]
    return f"{label}-instructions-prompt-{run_id}.txt" if label else f"prompt-{run_id}.txt"


def accepted_recovery_prompt_alias_names(run_id: str, manifest: dict[str, Any]) -> set[str]:
    """Accept the deterministic alias as well as exact legacy run names."""
    return {f"prompt-{run_id}.txt", recovery_prompt_alias_name(run_id, manifest)}


def _requested_contract(manifest: dict[str, Any]) -> dict[str, Any]:
    mode = str(manifest.get("mode_label") or manifest.get("model") or "GPT-5.6")
    mode_key = mode.strip().casefold()
    app_policy = str(manifest.get("app_policy") or ("forbidden" if mode_key == "pro" else "required"))
    app_name = str(manifest.get("chatgpt_app_name") or manifest.get("app_name") or "").strip()
    if mode_key == "pro":
        if app_policy != "forbidden" or app_name:
            raise StateError("APP_POLICY_FORBIDDEN", "Pro requires app_policy=forbidden and no app name")
    else:
        if app_policy != "required":
            raise StateError(
                "APP_POLICY_REQUIRED",
                "every non-Pro ChatGPT mode requires app_policy=required",
            )
        if not app_name:
            raise StateError("APP_REQUIRED", "every non-Pro ChatGPT mode requires chatgpt_app_name")
    return {
        "workflow": str(manifest.get("workflow_mode") or "direct"),
        "mode": mode,
        "reasoning": manifest.get("mode_variant") or manifest.get("effort"),
        "search": bool(manifest.get("search_enabled") or manifest.get("web_search")),
        "transport": str(manifest.get("prompt_transport") or ("attachment" if manifest.get("files") else "inline")),
        "app_policy": app_policy,
    }


class RunPaths:
    def __init__(self, project_dir: Path, runs_dir: Path, run_dir: Path, state_file: Path, lock_file: Path):
        self.project_dir = project_dir
        self.runs_dir = runs_dir
        self.run_dir = run_dir
        self.state_file = state_file
        self.lock_file = lock_file
        self.parent_transition_lock = project_dir / "parent-transition.lock"


class RunStore:
    def __init__(self, root: Path | None = None):
        self.root = (root or (Path.home() / ".codex" / "state" / "chatgpt-agbrowse")).resolve()

    def paths(self, project_root: Path, run_id: str) -> RunPaths:
        key = project_key(project_root)
        project_dir = self.root / "projects" / key
        runs_dir = project_dir / "runs"
        run_dir = runs_dir / run_id
        return RunPaths(project_dir, runs_dir, run_dir, run_dir / "run.json", project_dir / "active.lock")

    def _read_existing_lock(self, lock_file: Path) -> dict[str, Any] | None:
        if not lock_file.exists():
            return None
        try:
            return read_json(lock_file)
        except StateError:
            if not lock_file.exists():
                return None
            raise

    @staticmethod
    def _provider_failed_terminal_settled(record: dict[str, Any], state_file: Path) -> bool:
        if str(record.get("phase") or "") != "PROVIDER_FAILED_TERMINAL":
            return False
        cleanup = record.get("cleanup_evidence") if isinstance(record.get("cleanup_evidence"), dict) else {}
        state = str(cleanup.get("state") or "")
        target = str(cleanup.get("target_id") or "")
        url = str(cleanup.get("conversation_url") or "")
        if (
            cleanup.get("ok") is not True
            or state not in {"closed-and-absent", "already-absent"}
            or bool(record.get("cleanup_pending"))
            or int(record.get("owned_open_tabs") or 0) != 0
            or str(record.get("owned_tab_state") or "") != state
            or not target
            or target != str(record.get("current_target_id") or "")
            or not url
            or url != str(record.get("conversation_url") or "")
            or record.get("result") is not None
            or str(record.get("terminal_block_code") or "") != "PROVIDER_TERMINAL_ERROR_UI"
        ):
            return False
        evidence = cleanup.get("evidence") if isinstance(cleanup.get("evidence"), dict) else {}
        try:
            evidence_path = Path(str(evidence.get("path") or "")).expanduser().resolve(strict=True)
            evidence_path.relative_to(state_file.parent)
            if (
                not evidence_path.is_file()
                or evidence_path.is_symlink()
                or sha256_file(evidence_path) != str(evidence.get("sha256") or "")
            ):
                return False
        except (OSError, RuntimeError, ValueError):
            return False
        failure_events = [
            item
            for item in record.get("recovery_events") or []
            if isinstance(item, dict) and str(item.get("kind") or "") == "provider-terminal-error-ui"
        ]
        if len(failure_events) != 1:
            return False
        failure = failure_events[0]
        if (
            str(failure.get("signature") or "") != "chatgpt-stream-error-retry-v1"
            or str(failure.get("provider_status") or "").lower()
            not in {"complete", "completed", "done", "response_ready", "history-adjudicated-terminal"}
            or str(failure.get("session_id") or "") != str(record.get("session_id") or "")
            or str(failure.get("target_id") or "") != target
            or str(failure.get("conversation_url") or "") != url
        ):
            return False
        try:
            failure_path = Path(str(failure.get("answer_path") or "")).expanduser().resolve(strict=True)
            failure_path.relative_to(state_file.parent)
            if (
                not failure_path.is_file()
                or failure_path.is_symlink()
                or sha256_file(failure_path) != str(failure.get("answer_sha256") or "")
                or failure_path.stat().st_size != int(failure.get("answer_bytes") or -1)
            ):
                return False
        except (OSError, RuntimeError, TypeError, ValueError):
            return False
        return True

    def _active_or_uncertain_records(self, runs_dir: Path) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        if not runs_dir.exists():
            return rows
        for path in sorted(runs_dir.glob("*/run.json")):
            try:
                record = read_json(path)
            except StateError:
                rows.append({"run_id": path.parent.name, "phase": "STATE_UNREADABLE", "path": str(path)})
                continue
            kind = str(record.get("record_kind") or "standalone")
            phase = str(record.get("phase") or "")
            if kind == "child":
                # Children are owned by the one parent project lease and never
                # independently block a later parent after that lease settles.
                continue
            settled = {"COMPLETE", "COMPLETE_SUPERSEDED", "CANCELLED_PRE_SUBMISSION", "ABANDONED_UNCERTAIN"} | PARENT_TERMINAL_PHASES
            if phase == "PROVIDER_FAILED_TERMINAL" and self._provider_failed_terminal_settled(record, path):
                continue
            if phase not in settled:
                rows.append({"run_id": record.get("run_id"), "phase": record.get("phase"), "path": str(path)})
        return rows

    @staticmethod
    def _owner_observation(record: dict[str, Any]) -> dict[str, Any]:
        stored = record.get("owner") if isinstance(record.get("owner"), dict) else {}
        pid = int(stored.get("pid") or 0)
        observed = process_identity(pid) if pid > 0 else {"pid": pid, "creation_time": None, "alive": False}
        return {
            "stored": {
                "pid": pid,
                "creation_time": stored.get("creation_time"),
                "alive": stored.get("alive"),
            },
            "observed": observed,
            "same_process": same_process(stored),
        }

    @staticmethod
    def _crossed_send_boundary(record: dict[str, Any]) -> bool:
        if str(record.get("phase") or "") in UNCERTAIN_OR_SUBMITTED_PHASES | {"SEND_REJECTED"}:
            return True
        if any(
            str(item.get("to") or "") == "SEND_STARTED"
            for item in (record.get("phase_events") or [])
            if isinstance(item, dict)
        ):
            return True
        return bool(
            record.get("session_id")
            or record.get("conversation_url")
            or record.get("submission_receipt")
            or record.get("result")
        )

    @classmethod
    def _verified_pre_submit_target_cleanup(
        cls,
        state_file: Path,
        record: dict[str, Any],
    ) -> dict[str, Any] | None:
        target_id = str(record.get("current_target_id") or "")
        zero_provider_rejection = cls._send_rejected_failure_evidence_proof(state_file, record)
        if not target_id or (
            cls._crossed_send_boundary(record) and zero_provider_rejection is None
        ):
            return None
        expected_path = (state_file.parent / "tab-lifecycle.json").resolve()
        allowed_recovery_kinds = {
            "app-chat-surface-preparation-failed",
            "app-composer-preparation-failed",
            "app-composer-target-activation-failed",
            "app-selection-evidence-missing",
            "pre-submit-command-budget-exceeded",
            "pre-submit-rejection",
            "prepared-target-evidence-failed",
            "verified-pre-submit-tab-cleanup",
        }
        for recovery in reversed(record.get("recovery_events") or []):
            if not isinstance(recovery, dict) or str(recovery.get("kind") or "") not in allowed_recovery_kinds:
                continue
            cleanup = recovery.get("cleanup")
            if not isinstance(cleanup, dict):
                continue
            if not (
                cleanup.get("ok") is True
                and cleanup.get("state") == "closed-and-absent"
                and str(cleanup.get("target_id") or "") == target_id
            ):
                continue
            evidence = cleanup.get("evidence")
            if not isinstance(evidence, dict):
                continue
            try:
                evidence_path = Path(str(evidence.get("path") or "")).expanduser().resolve(strict=True)
            except (OSError, RuntimeError, ValueError):
                continue
            if evidence_path != expected_path or not evidence_path.is_file() or evidence_path.is_symlink():
                continue
            expected_sha256 = str(evidence.get("sha256") or "")
            if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256) or sha256_file(evidence_path) != expected_sha256:
                continue
            try:
                ledger = read_json(evidence_path)
            except StateError:
                continue
            if not (
                ledger.get("schema") == "codex.chatgpt.agbrowse-tab-lifecycle/v1"
                and ledger.get("run_id") == record.get("run_id")
                and ledger.get("project_key") == record.get("project_key")
                and ledger.get("manifest_sha256") == record.get("manifest_sha256")
            ):
                continue
            matching_events = [
                item
                for item in (ledger.get("events") or [])
                if isinstance(item, dict)
                and item.get("kind") == "cleanup"
                and item.get("ok") is True
                and item.get("state") == "closed-and-absent"
                and str(item.get("target_id") or "") == target_id
            ]
            if not matching_events:
                continue
            event = matching_events[-1]
            nested_event = evidence.get("event")
            compared_keys = (
                "kind",
                "reason",
                "url",
                "ok",
                "state",
                "target_id",
                "before_count",
                "after_count",
                "before_sha256",
                "after_sha256",
                "close_stdout_sha256",
            )
            if not isinstance(nested_event, dict) or any(
                nested_event.get(key) != event.get(key) for key in compared_keys
            ) or any(
                cleanup.get(key) != event.get(key) for key in compared_keys if key in cleanup
            ):
                continue
            before_count = event.get("before_count")
            after_count = event.get("after_count")
            if not (
                isinstance(before_count, int)
                and isinstance(after_count, int)
                and before_count == after_count + 1
                and re.fullmatch(r"[0-9a-f]{64}", str(event.get("before_sha256") or ""))
                and re.fullmatch(r"[0-9a-f]{64}", str(event.get("after_sha256") or ""))
                and re.fullmatch(r"[0-9a-f]{64}", str(event.get("close_stdout_sha256") or ""))
            ):
                continue
            return {
                "path": str(evidence_path),
                "sha256": expected_sha256,
                "target_id": target_id,
                "reason": event.get("reason"),
                "before_count": before_count,
                "after_count": after_count,
            }
        return None

    @classmethod
    def _safe_stale_pre_submission(cls, state_file: Path, record: dict[str, Any]) -> bool:
        if str(record.get("phase") or "") == "SEND_REJECTED":
            return bool(
                cls._send_rejected_failure_evidence_proof(state_file, record) is not None
                and cls._verified_pre_submit_target_cleanup(state_file, record) is not None
            )
        return bool(
            str(record.get("phase") or "") in SAFE_STALE_PRE_SUBMISSION_PHASES
            and not cls._crossed_send_boundary(record)
            and (
                not record.get("current_target_id")
                or cls._verified_pre_submit_target_cleanup(state_file, record) is not None
            )
        )

    @staticmethod
    def _complete_result_capture_valid(state_file: Path, record: dict[str, Any]) -> bool:
        result = record.get("result") if isinstance(record.get("result"), dict) else {}
        try:
            result_path = Path(str(result.get("path") or "")).expanduser().resolve(strict=True)
            result_path.relative_to(state_file.parent.resolve(strict=True))
            if not result_path.is_file() or result_path.is_symlink():
                return False
            actual_bytes = result_path.stat().st_size
            actual_sha256 = sha256_file(result_path)
        except (OSError, RuntimeError, TypeError, ValueError):
            return False
        return bool(
            actual_bytes > 0
            and actual_bytes == int(result.get("bytes") or -1)
            and re.fullmatch(r"[0-9a-f]{64}", str(result.get("sha256") or ""))
            and actual_sha256 == str(result.get("sha256") or "")
            and str(result.get("provider_status") or "").lower()
            in {
                "complete",
                "completed",
                "done",
                "response_ready",
                "history-adjudicated-terminal",
                "exact-url-adjudicated-terminal",
            }
            and isinstance(result.get("evidence"), dict)
            and bool(result["evidence"])
        )

    @staticmethod
    def _send_claim_proof(state_file: Path, record: dict[str, Any]) -> dict[str, Any] | None:
        claim_file = state_file.parent / "send.claim"
        if not claim_file.is_file() or claim_file.is_symlink():
            return None
        try:
            claim = read_json(claim_file)
        except StateError:
            return None
        exact = {
            "run_id": record.get("run_id"),
            "parent_run_id": record.get("parent_run_id"),
            "parent_workflow_id": record.get("parent_workflow_id"),
            "parent_lease_nonce": record.get("parent_lease_nonce"),
            "project_root": record.get("project_root"),
            "project_key": record.get("project_key"),
            "stage_id": record.get("stage_id"),
            "role": record.get("role"),
            "lane": record.get("lane"),
            "iteration": record.get("iteration"),
            "manifest_sha256": record.get("manifest_sha256"),
            "prompt_sha256": record.get("prompt_sha256"),
            "send_limit": record.get("send_limit"),
        }
        actual_sha256 = sha256_file(claim_file)
        descriptor = record.get("send_claim") if isinstance(record.get("send_claim"), dict) else {}
        if (
            Path(str(descriptor.get("path") or "")) != claim_file
            or str(descriptor.get("sha256") or "") != actual_sha256
            or str(descriptor.get("claimed_at") or "") != str(claim.get("claimed_at") or "")
        ):
            return None
        if (
            claim.get("schema") != "codex.chatgpt.child-send-claim/v1"
            or any(claim.get(key) != value for key, value in exact.items())
            or not str(claim.get("claimed_at") or "")
        ):
            adopted = RunStore._legacy_send_claim_adoption_proof(
                state_file, record, claim=claim
            )
            return adopted or RunStore._parent_stop_legacy_claim_proof(
                state_file, record, claim=claim
            )
        return {
            "path": str(claim_file),
            "sha256": actual_sha256,
            "bytes": claim_file.stat().st_size,
            "identity": exact,
            "claimed_at": claim["claimed_at"],
        }

    @staticmethod
    def _parent_stop_legacy_claim_proof(
        state_file: Path, record: dict[str, Any], *, claim: dict[str, Any]
    ) -> dict[str, Any] | None:
        legacy_keys = {"schema", "run_id", "parent_run_id", "stage_id", "manifest_sha256", "prompt_sha256", "claimed_at"}
        if (
            set(claim) != legacy_keys
            or claim.get("schema") != "codex.chatgpt.child-send-claim/v1"
            or any(claim.get(key) != record.get(key) for key in ("run_id", "parent_run_id", "stage_id", "manifest_sha256", "prompt_sha256"))
            or not str(claim.get("claimed_at") or "")
        ):
            return None
        claim_file = state_file.parent / "send.claim"
        descriptor = record.get("send_claim") if isinstance(record.get("send_claim"), dict) else {}
        try:
            parent_file = state_file.parent.parent / str(record.get("parent_run_id") or "") / "run.json"
            parent = read_json(parent_file)
            scope_ref = parent.get("parent_stop_scope") if isinstance(parent.get("parent_stop_scope"), dict) else {}
            scope_path = Path(str(scope_ref.get("path") or ""))
            if (
                str(parent.get("phase") or "") not in {"USER_STOP_REQUESTED", "PARENT_DRAINING", "PARENT_FAILED_CLOSED"}
                or not scope_path.is_file() or scope_path.is_symlink()
                or sha256_file(scope_path) != str(scope_ref.get("sha256") or "")
                or scope_path.stat().st_size != int(scope_ref.get("bytes") or -1)
            ):
                return None
            scope = read_json(scope_path)
        except (OSError, TypeError, ValueError, StateError):
            return None
        entries = [entry for entry in scope.get("ordered_children") or [] if isinstance(entry, dict) and str(entry.get("run_id") or "") == str(record.get("run_id") or "")]
        actual_sha = sha256_file(claim_file)
        if (
            scope.get("schema") != "codex.chatgpt.parent-wide-user-stop/v1"
            or scope.get("explicit_user_request") is not True
            or len(entries) != 1
            or any(entries[0].get(key) != record.get(key) for key in ("run_id", "stage_id", "role", "lane", "iteration"))
            or Path(str(descriptor.get("path") or "")) != claim_file
            or str(descriptor.get("sha256") or "") != actual_sha
            or str(descriptor.get("claimed_at") or "") != str(claim["claimed_at"])
        ):
            return None
        identity = {
            "run_id": record.get("run_id"), "parent_run_id": record.get("parent_run_id"),
            "parent_workflow_id": record.get("parent_workflow_id"), "parent_lease_nonce": record.get("parent_lease_nonce"),
            "project_root": record.get("project_root"), "project_key": record.get("project_key"),
            "stage_id": record.get("stage_id"), "role": record.get("role"), "lane": record.get("lane"),
            "iteration": record.get("iteration"), "manifest_sha256": record.get("manifest_sha256"),
            "prompt_sha256": record.get("prompt_sha256"), "send_limit": record.get("send_limit"),
        }
        return {
            "path": str(claim_file), "sha256": actual_sha, "bytes": claim_file.stat().st_size,
            "identity": identity, "claimed_at": claim["claimed_at"], "parent_stop_scope": scope_ref,
        }

    @staticmethod
    def _send_rejected_failure_evidence_proof(
        state_file: Path, record: dict[str, Any]
    ) -> dict[str, Any] | None:
        if (
            str(record.get("phase") or "") != "SEND_REJECTED"
            or (
                record.get("send_attempt_count") is not None
                and int(record.get("send_attempt_count") or 0) != 1
            )
            or (
                record.get("send_limit") is not None
                and int(record.get("send_limit") or 0) != 1
            )
            or record.get("session_id")
            or record.get("conversation_url")
            or record.get("submission_receipt") is not None
            or record.get("result") is not None
        ):
            return None
        events = [
            event
            for event in record.get("recovery_events") or []
            if isinstance(event, dict) and str(event.get("kind") or "") == "pre-submit-rejection"
        ]
        if not events:
            return RunStore._legacy_process_not_created_evidence_proof(
                state_file, record
            )
        if len(events) != 1:
            return None
        event = events[0]
        normalized = event.get("error") if isinstance(event.get("error"), dict) else {}
        evidence = event.get("evidence") if isinstance(event.get("evidence"), dict) else {}
        evidence_dir = state_file.parent / "agbrowse-evidence"
        stdout_path = Path(str(evidence.get("stdout") or ""))
        stderr_path = Path(str(evidence.get("stderr") or ""))
        try:
            if (
                stdout_path.resolve(strict=True) != (evidence_dir / "send.stdout.txt").resolve(strict=True)
                or stderr_path.resolve(strict=True) != (evidence_dir / "send.stderr.txt").resolve(strict=True)
                or stdout_path.is_symlink()
                or stderr_path.is_symlink()
            ):
                return None
            stdout_sha256 = sha256_file(stdout_path)
            stderr_sha256 = sha256_file(stderr_path)
            stdout_recorded_sha256 = str(evidence.get("stdout_sha256") or "")
            stderr_recorded_sha256 = str(evidence.get("stderr_sha256") or "")
            stdout_legacy_text_sha256 = sha256_bytes(
                stdout_path.read_text(encoding="utf-8").encode("utf-8")
            )
            stderr_legacy_text_sha256 = sha256_bytes(
                stderr_path.read_text(encoding="utf-8").encode("utf-8")
            )
            if (
                stdout_recorded_sha256 not in {stdout_sha256, stdout_legacy_text_sha256}
                or stderr_recorded_sha256 not in {stderr_sha256, stderr_legacy_text_sha256}
                or not isinstance(evidence.get("exit_code"), int)
                or int(evidence["exit_code"]) == 0
            ):
                return None
            stdout_text = stdout_path.read_text(encoding="utf-8")
            stderr_text = stderr_path.read_text(encoding="utf-8")
        except (OSError, RuntimeError, UnicodeError, ValueError):
            return None
        nonempty = [text for text in (stdout_text, stderr_text) if text.strip()]
        if len(nonempty) != 1:
            return None
        try:
            payload = json.loads(nonempty[0])
        except json.JSONDecodeError:
            return None
        raw_error = payload.get("error") if isinstance(payload, dict) and isinstance(payload.get("error"), dict) else {}
        error_code = str(raw_error.get("errorCode") or raw_error.get("code") or "")
        error_stage = str(raw_error.get("stage") or "")
        if (
            payload.get("ok") is not False
            or raw_error.get("mutationAllowed") is not False
            or not error_code
            or not error_stage
            or normalized.get("mutation_allowed") is not False
            or str(normalized.get("error_code") or "") != error_code
            or str(normalized.get("error_stage") or "") != error_stage
            or any(
                payload.get(key) not in (None, "", [], {})
                for key in (
                    "sessionId",
                    "session_id",
                    "conversationUrl",
                    "conversation_url",
                    "answer",
                    "result",
                )
            )
        ):
            return None
        return {
            "schema": "codex.chatgpt.zero-provider-failure-evidence/v1",
            "run_id": record["run_id"],
            "parent_run_id": record.get("parent_run_id"),
            "stage_id": record.get("stage_id"),
            "stdout": {
                "path": str(stdout_path),
                "sha256": stdout_sha256,
                "bytes": stdout_path.stat().st_size,
            },
            "stderr": {
                "path": str(stderr_path),
                "sha256": stderr_sha256,
                "bytes": stderr_path.stat().st_size,
            },
            "exit_code": evidence["exit_code"],
            "error_code": error_code,
            "error_stage": error_stage,
        }

    @staticmethod
    def _legacy_process_not_created_evidence_proof(
        state_file: Path, record: dict[str, Any]
    ) -> dict[str, Any] | None:
        events = [
            event
            for event in record.get("recovery_events") or []
            if isinstance(event, dict)
            and str(event.get("kind") or "")
            == "verified-mutation-disallowed-reclassification"
        ]
        if len(events) != 1:
            return None
        event = events[0]
        evidence_path = Path(str(event.get("evidence_path") or ""))
        expected_path = (
            state_file.parent
            / "agbrowse-evidence"
            / "send-process-not-created-legacy-evidence.json"
        )
        try:
            if (
                evidence_path.resolve(strict=True) != expected_path.resolve(strict=True)
                or evidence_path.is_symlink()
                or sha256_file(evidence_path)
                != str(event.get("evidence_sha256") or "")
            ):
                return None
            evidence = read_json(evidence_path)
        except (OSError, RuntimeError, ValueError, StateError):
            return None
        if (
            event.get("mutation_allowed") is not False
            or str(event.get("proof_kind") or "")
            != "send-runner-process-not-created"
            or str(event.get("exception_type") or "") != "FileNotFoundError"
            or not str(event.get("historical_command_executable") or "")
            or evidence.get("schema")
            != "codex.chatgpt.send-process-not-created/v1"
            or str(evidence.get("kind") or "")
            != "send-runner-process-not-created"
            or evidence.get("mutation_allowed") is not False
            or str(evidence.get("exception_type") or "") != "FileNotFoundError"
            or int(evidence.get("winerror") or 0) != 2
            or str(evidence.get("historical_command_executable") or "")
            != str(event.get("historical_command_executable") or "")
            or str(evidence.get("run_id") or "") != str(record.get("run_id") or "")
            or str(evidence.get("parent_run_id") or "")
            != str(record.get("parent_run_id") or "")
            or str(evidence.get("target_id") or "")
            != str(record.get("current_target_id") or "")
            or str(evidence.get("target_url") or "") != "https://chatgpt.com/"
            or evidence.get("session_id") is not None
            or evidence.get("conversation_url") is not None
            or evidence.get("send_evidence_present") is not False
            or not str(evidence.get("source") or "")
            or not Path(str(evidence.get("pinned_executable_path") or "")).is_absolute()
            or re.fullmatch(
                r"[0-9a-f]{64}",
                str(evidence.get("pinned_executable_sha256") or ""),
            )
            is None
            or re.fullmatch(
                r"[0-9a-f]{64}", str(evidence.get("faulty_bridge_sha256") or "")
            )
            is None
            or re.fullmatch(
                r"[0-9a-f]{64}",
                str(evidence.get("run_state_sha256_before_adjudication") or ""),
            )
            is None
        ):
            return None
        return {
            "schema": "codex.chatgpt.zero-provider-failure-evidence/v1",
            "run_id": record["run_id"],
            "parent_run_id": record["parent_run_id"],
            "stage_id": record["stage_id"],
            "legacy_process_not_created": {
                "path": str(evidence_path),
                "sha256": sha256_file(evidence_path),
                "bytes": evidence_path.stat().st_size,
            },
            "pinned_executable": {
                "path": str(evidence["pinned_executable_path"]),
                "sha256": str(evidence["pinned_executable_sha256"]),
            },
            "exit_code": -1,
            "error_code": "SEND_PROCESS_NOT_CREATED",
            "error_stage": "runner-process-creation",
        }

    @staticmethod
    def _send_rejected_zero_provider_proof(
        state_file: Path, record: dict[str, Any]
    ) -> dict[str, Any] | None:
        failure = RunStore._send_rejected_failure_evidence_proof(state_file, record)
        claim = RunStore._send_claim_proof(state_file, record)
        if failure is None or claim is None:
            return None
        return {
            "schema": "codex.chatgpt.zero-provider-proof/v1",
            **{key: value for key, value in failure.items() if key != "schema"},
            "claim": claim,
        }

    @staticmethod
    def _legacy_send_claim_adoption_proof(
        state_file: Path,
        record: dict[str, Any],
        *,
        claim: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        claim_file = state_file.parent / "send.claim"
        legacy_keys = {
            "schema",
            "run_id",
            "parent_run_id",
            "stage_id",
            "manifest_sha256",
            "prompt_sha256",
            "claimed_at",
        }
        try:
            raw_claim = claim_file.read_bytes()
            parsed_claim = claim if isinstance(claim, dict) else read_json(claim_file)
        except (OSError, StateError):
            return None
        if (
            set(parsed_claim) != legacy_keys
            or parsed_claim.get("schema") != "codex.chatgpt.child-send-claim/v1"
            or any(
                parsed_claim.get(key) != record.get(key)
                for key in (
                    "run_id",
                    "parent_run_id",
                    "stage_id",
                    "manifest_sha256",
                    "prompt_sha256",
                )
            )
            or not str(parsed_claim.get("claimed_at") or "")
        ):
            return None
        source = record.get("send_claim") if isinstance(record.get("send_claim"), dict) else {}
        claim_sha256 = sha256_bytes(raw_claim)
        if (
            Path(str(source.get("path") or "")) != claim_file
            or str(source.get("sha256") or "") != claim_sha256
            or str(source.get("claimed_at") or "") != str(parsed_claim["claimed_at"])
        ):
            return None
        descriptor = (
            record.get("legacy_send_claim_adoption")
            if isinstance(record.get("legacy_send_claim_adoption"), dict)
            else {}
        )
        try:
            adoption_path = Path(str(descriptor.get("path") or "")).resolve(strict=True)
            adoption_path.relative_to((state_file.parent / "user-stop").resolve(strict=True))
            if (
                adoption_path.name != "legacy-send-claim-adoption.json"
                or adoption_path.is_symlink()
                or sha256_file(adoption_path) != str(descriptor.get("sha256") or "")
                or adoption_path.stat().st_size != int(descriptor.get("bytes") or -1)
            ):
                return None
            adoption = read_json(adoption_path)
            parent_file = state_file.parent.parent / str(record.get("parent_run_id") or "") / "run.json"
            parent = read_json(parent_file)
        except (OSError, RuntimeError, TypeError, ValueError, StateError):
            return None
        child_identity = {
            "run_id": record.get("run_id"),
            "parent_run_id": record.get("parent_run_id"),
            "parent_workflow_id": record.get("parent_workflow_id"),
            "parent_lease_nonce": record.get("parent_lease_nonce"),
            "project_root": record.get("project_root"),
            "project_key": record.get("project_key"),
            "stage_id": record.get("stage_id"),
            "role": record.get("role"),
            "lane": record.get("lane"),
            "iteration": record.get("iteration"),
            "manifest_sha256": record.get("manifest_sha256"),
            "prompt_sha256": record.get("prompt_sha256"),
            "send_limit": record.get("send_limit"),
        }
        parent_identity = {
            "run_id": parent.get("run_id"),
            "workflow_id": parent.get("workflow_id"),
            "lease_nonce": parent.get("lease_nonce"),
            "project_root": parent.get("project_root"),
            "project_key": parent.get("project_key"),
            "manifest_sha256": parent.get("manifest_sha256"),
        }
        entries = [
            entry
            for entry in parent.get("children") or []
            if isinstance(entry, dict)
            and str(entry.get("run_id") or "") == str(record.get("run_id") or "")
        ]
        if len(entries) != 1:
            return None
        child_entry = {
            key: entries[0].get(key)
            for key in ("run_id", "stage_id", "role", "lane", "iteration")
        }
        if (
            parent.get("schema") != SCHEMA
            or str(parent.get("record_kind") or "") != "parent"
            or str(parent.get("phase") or "") not in {"USER_STOP_REQUESTED", "PARENT_DRAINING"}
            or child_identity["parent_run_id"] != parent_identity["run_id"]
            or child_identity["parent_workflow_id"] != parent_identity["workflow_id"]
            or child_identity["parent_lease_nonce"] != parent_identity["lease_nonce"]
            or child_identity["project_root"] != parent_identity["project_root"]
            or child_identity["project_key"] != parent_identity["project_key"]
            or any(
                child_entry.get(key) != child_identity.get(key)
                for key in ("run_id", "stage_id", "role", "lane", "iteration")
            )
        ):
            return None
        authority = adoption.get("authority") if isinstance(adoption.get("authority"), dict) else {}
        source_phase = str(authority.get("source_phase") or "")
        if source_phase == "SEND_REJECTED":
            expected_authority = {
                "source_phase": "SEND_REJECTED",
                "zero_provider_evidence": RunStore._send_rejected_failure_evidence_proof(
                    state_file, record
                ),
            }
            authority_valid = expected_authority["zero_provider_evidence"] is not None
        elif source_phase == "USER_STOP_REQUESTED":
            stop = record.get("user_stop") if isinstance(record.get("user_stop"), dict) else {}
            legacy_binding = (
                stop.get("legacy_binding")
                if isinstance(stop.get("legacy_binding"), dict)
                else {}
            )
            binding_path = Path(str(legacy_binding.get("path") or ""))
            authority_valid = bool(
                str(record.get("phase") or "") in {"USER_STOP_REQUESTED", "ABANDONED_UNCERTAIN"}
                and binding_path.is_file()
                and not binding_path.is_symlink()
                and sha256_file(binding_path) == str(legacy_binding.get("sha256") or "")
                and all(
                    record.get(key)
                    for key in ("session_id", "current_target_id", "conversation_url")
                )
            )
            expected_authority = {
                "source_phase": "USER_STOP_REQUESTED",
                "legacy_stop_binding": legacy_binding,
                "session_id": record.get("session_id"),
                "target_id": record.get("current_target_id"),
                "conversation_url": record.get("conversation_url"),
            }
        else:
            return None
        if not authority_valid:
            return None
        expected_claim = {
            "path": str(claim_file),
            "sha256": claim_sha256,
            "bytes": len(raw_claim),
            "bytes_base64": base64.b64encode(raw_claim).decode("ascii"),
            "parsed": parsed_claim,
        }
        if (
            set(adoption)
            != {
                "schema",
                "claim",
                "child_identity",
                "parent_identity",
                "parent_child_entry",
                "authority",
                "preimages",
                "adopted_at",
            }
            or adoption.get("schema")
            != "codex.chatgpt.legacy-send-claim-adoption/v1"
            or not str(adoption.get("adopted_at") or "")
            or not isinstance(adoption.get("preimages"), dict)
            or set(adoption["preimages"])
            != {
                "child_state_sha256",
                "parent_state_sha256",
                "parent_lock_sha256",
            }
            or any(
                re.fullmatch(r"[0-9a-f]{64}", str(value or "")) is None
                for value in adoption["preimages"].values()
            )
            or adoption.get("claim") != expected_claim
            or adoption.get("child_identity") != child_identity
            or adoption.get("parent_identity") != parent_identity
            or adoption.get("parent_child_entry") != child_entry
            or authority != expected_authority
        ):
            return None
        return {
            "path": str(claim_file),
            "sha256": claim_sha256,
            "bytes": len(raw_claim),
            "identity": child_identity,
            "claimed_at": parsed_claim["claimed_at"],
            "compatibility_adoption": descriptor,
        }

    @staticmethod
    def _pre_submit_cleanup_proof(
        state_file: Path, record: dict[str, Any], cleanup: dict[str, Any]
    ) -> dict[str, Any] | None:
        target_id = str(record.get("current_target_id") or "")
        if (
            not target_id
            or cleanup.get("ok") is not True
            or str(cleanup.get("state") or "") not in {"closed-and-absent", "already-absent"}
            or str(cleanup.get("target_id") or "") != target_id
        ):
            return None
        evidence = cleanup.get("evidence") if isinstance(cleanup.get("evidence"), dict) else {}
        try:
            evidence_path = Path(str(evidence.get("path") or "")).resolve(strict=True)
            evidence_path.relative_to(state_file.parent.resolve(strict=True))
            if (
                not evidence_path.is_file()
                or evidence_path.is_symlink()
                or sha256_file(evidence_path) != str(evidence.get("sha256") or "")
            ):
                return None
            ledger = read_json(evidence_path)
        except (OSError, RuntimeError, ValueError, StateError):
            return None
        event = evidence.get("event") if isinstance(evidence.get("event"), dict) else {}
        cleanup_state = str(cleanup.get("state") or "")
        expected_kind = "cleanup" if cleanup_state == "closed-and-absent" else "cleanup-already-absent"
        ledger_events = ledger.get("events") if isinstance(ledger.get("events"), list) else []
        matching_events = [
            candidate
            for candidate in ledger_events
            if isinstance(candidate, dict)
            and {
                key: value for key, value in candidate.items() if key != "at"
            }
            == event
        ]
        exact_reason = (
            str(event.get("reason") or "")
            == "explicit-user-stop-zero-provider-sibling"
        )
        if cleanup_state == "already-absent":
            event_shape_valid = bool(
                exact_reason
                and re.fullmatch(r"[0-9a-f]{64}", str(event.get("tabs_sha256") or ""))
            )
        else:
            event_shape_valid = bool(
                exact_reason
                and event.get("ok") is True
                and str(event.get("state") or "") == cleanup_state
                and isinstance(event.get("before_count"), int)
                and int(event["before_count"]) >= 1
                and isinstance(event.get("after_count"), int)
                and int(event["after_count"]) >= 0
                and re.fullmatch(r"[0-9a-f]{64}", str(event.get("before_sha256") or ""))
                and re.fullmatch(r"[0-9a-f]{64}", str(event.get("after_sha256") or ""))
                and re.fullmatch(
                    r"[0-9a-f]{64}", str(event.get("close_stdout_sha256") or "")
                )
            )
        if (
            ledger.get("schema") != "codex.chatgpt.agbrowse-tab-lifecycle/v1"
            or str(ledger.get("run_id") or "") != str(record.get("run_id") or "")
            or str(ledger.get("project_key") or "") != str(record.get("project_key") or "")
            or str(ledger.get("manifest_sha256") or "") != str(record.get("manifest_sha256") or "")
            or str(event.get("target_id") or "") != target_id
            or str(event.get("kind") or "") != expected_kind
            or not event_shape_valid
            or len(matching_events) != 1
        ):
            return None
        return {
            "path": str(evidence_path),
            "sha256": sha256_file(evidence_path),
            "bytes": evidence_path.stat().st_size,
            "event": event,
        }

    @staticmethod
    def _send_rejected_zero_provider_settled(state_file: Path, record: dict[str, Any]) -> bool:
        """A claimed send may be safe only with durable mutationAllowed=false proof.

        This intentionally grants no retry authority; it is a drain-only
        classification for an exact cleaned pre-submit target.
        """
        proof = RunStore._send_rejected_zero_provider_proof(state_file, record)
        if proof is None:
            return False
        cleanup = record.get("cleanup_evidence") if isinstance(record.get("cleanup_evidence"), dict) else {}
        cleanup_proof = RunStore._pre_submit_cleanup_proof(state_file, record, cleanup)
        settlement = (
            record.get("zero_provider_settlement")
            if isinstance(record.get("zero_provider_settlement"), dict)
            else {}
        )
        try:
            settlement_path = Path(str(settlement.get("path") or "")).resolve(strict=True)
            settlement_value = read_json(settlement_path)
        except (OSError, RuntimeError, ValueError, StateError):
            return False
        return bool(
            cleanup_proof is not None
            and not bool(record.get("cleanup_pending"))
            and int(record.get("owned_open_tabs") or 0) == 0
            and settlement_path.parent == state_file.parent / "user-stop"
            and not settlement_path.is_symlink()
            and sha256_file(settlement_path) == str(settlement.get("sha256") or "")
            and settlement_path.stat().st_size == int(settlement.get("bytes") or -1)
            and settlement_value.get("schema") == "codex.chatgpt.zero-provider-settlement/v1"
            and settlement_value.get("proof") == proof
            and settlement_value.get("cleanup") == cleanup_proof
        )

    def _duplicate_completed_owner_proof(self, state_file: Path, record: dict[str, Any]) -> dict[str, Any] | None:
        if (
            str(record.get("phase") or "") != "BLOCKED_TARGET_AMBIGUOUS"
            or str(record.get("terminal_block_code") or "") != "CONVERSATION_URL_OWNED_BY_FOREIGN_RUN"
        ):
            return None
        collision = next(
            (
                item
                for item in reversed(record.get("recovery_events") or [])
                if isinstance(item, dict)
                and str(item.get("kind") or "") == "conversation-url-owned-by-foreign-run"
            ),
            None,
        )
        if not isinstance(collision, dict):
            return None
        candidate_recovery = collision.get("candidate_recovery")
        if not isinstance(candidate_recovery, dict) or str(candidate_recovery.get("kind") or "") not in {
            "doctor-reattach",
            "history-fingerprint-match",
        }:
            return None
        foreign = collision.get("foreign_owner")
        if not isinstance(foreign, dict):
            return None
        foreign_run_id = str(foreign.get("run_id") or "")
        if not re.fullmatch(r"[0-9a-f]{32}", foreign_run_id):
            return None
        owner_state_file = state_file.parent.parent / foreign_run_id / "run.json"
        try:
            owner = read_json(owner_state_file)
        except StateError:
            return None
        candidate_url = str(collision.get("conversation_url") or "")
        if (
            str(owner.get("run_id") or "") != foreign_run_id
            or str(owner.get("phase") or "") != "COMPLETE"
            or str(owner.get("project_key") or "") != str(record.get("project_key") or "")
            or str(owner.get("project_root") or "") != str(record.get("project_root") or "")
            or str(owner.get("prompt_sha256") or "") != str(record.get("prompt_sha256") or "")
            or str(owner.get("conversation_url") or "") != candidate_url
            or not record.get("session_id")
            or not self._complete_result_capture_valid(owner_state_file, owner)
        ):
            return None
        result = dict(owner["result"])
        return {
            "schema": "codex.chatgpt.duplicate-completed-owner-proof/v1",
            "superseded_run_id": record.get("run_id"),
            "authoritative_run_id": foreign_run_id,
            "project_key": record.get("project_key"),
            "prompt_sha256": record.get("prompt_sha256"),
            "conversation_url": candidate_url,
            "candidate_recovery_kind": candidate_recovery.get("kind"),
            "authoritative_result": {
                "path": result.get("path"),
                "sha256": result.get("sha256"),
                "bytes": result.get("bytes"),
                "provider_status": result.get("provider_status"),
            },
        }

    def _settle_duplicate_completed_owner(
        self,
        state_file: Path,
        record: dict[str, Any],
        lock_file: Path,
        lock: dict[str, Any],
        owner_observation: dict[str, Any],
    ) -> dict[str, Any]:
        if owner_observation.get("same_process"):
            raise StateError("ACTIVE_PROJECT_OWNER", "the recorded owner process is still the same live process")
        proof = self._duplicate_completed_owner_proof(state_file, record)
        if proof is None:
            raise StateError(
                "DUPLICATE_COMPLETE_OWNER_UNPROVEN",
                "completed duplicate URL ownership could not be proven exactly",
            )
        evidence_path = state_file.parent / "duplicate-completed-owner-proof.json"
        write_json_atomic(evidence_path, proof)
        now = utc_now()
        prior_phase = str(record.get("phase") or "")
        descriptor = {
            "path": str(evidence_path),
            "sha256": sha256_file(evidence_path),
            "authoritative_run_id": proof["authoritative_run_id"],
            "conversation_url": proof["conversation_url"],
        }
        record.setdefault("phase_events", []).append({"from": prior_phase, "to": "COMPLETE_SUPERSEDED", "at": now})
        record.setdefault("recovery_events", []).append(
            {
                "at": now,
                "kind": "duplicate-completed-owner-settled",
                "owner_observation": owner_observation,
                "proof": descriptor,
            }
        )
        record["superseded_complete"] = descriptor
        record["recovery_count"] = int(record.get("recovery_count") or 0) + 1
        record["phase"] = "COMPLETE_SUPERSEDED"
        record["phase_at"] = now
        record["updated_at"] = now
        record["terminal_block_code"] = None
        write_json_atomic(state_file, record)

        current_lock = read_json(lock_file)
        if (
            current_lock.get("run_id") != record.get("run_id")
            or current_lock.get("manifest_sha256") != lock.get("manifest_sha256")
            or current_lock.get("owner", {}).get("nonce") != record.get("owner", {}).get("nonce")
            or current_lock.get("owner", {}).get("epoch") != record.get("owner", {}).get("epoch")
        ):
            raise StateError(
                "BLOCKED_OWNER_MISMATCH",
                "project lease changed while settling duplicate completed ownership",
            )
        lock_file.unlink()
        return record

    def _cancel_stale_pre_submission(
        self,
        state_file: Path,
        record: dict[str, Any],
        lock_file: Path,
        lock: dict[str, Any],
        owner_observation: dict[str, Any],
    ) -> dict[str, Any]:
        if owner_observation.get("same_process"):
            raise StateError("ACTIVE_PROJECT_OWNER", "the recorded owner process is still the same live process")
        cleanup_evidence = self._verified_pre_submit_target_cleanup(state_file, record)
        if not self._safe_stale_pre_submission(state_file, record):
            raise StateError(
                "STALE_OWNER_NOT_SAFE_TO_CANCEL",
                "stale run is not a proven pre-submission run",
                {
                    "run_id": record.get("run_id"),
                    "phase": record.get("phase"),
                    "session_id": record.get("session_id"),
                    "target_id": record.get("current_target_id"),
                    "conversation_url": record.get("conversation_url"),
                },
            )
        now = utc_now()
        prior_phase = str(record.get("phase") or "")
        record.setdefault("phase_events", []).append({"from": prior_phase, "to": "CANCELLED_PRE_SUBMISSION", "at": now})
        record.setdefault("recovery_events", []).append(
            {
                "at": now,
                "kind": "stale-owner-pre-submission-reconciled",
                "owner_observation": owner_observation,
                "manifest_current_sha256": (
                    sha256_file(Path(str(record.get("manifest_path"))))
                    if Path(str(record.get("manifest_path"))).is_file()
                    else None
                ),
                "manifest_recorded_sha256": record.get("manifest_sha256"),
                "pre_submit_target_cleanup": cleanup_evidence,
            }
        )
        record["recovery_count"] = int(record.get("recovery_count") or 0) + 1
        record["phase"] = "CANCELLED_PRE_SUBMISSION"
        record["phase_at"] = now
        record["updated_at"] = now
        record["terminal_block_code"] = None
        if cleanup_evidence is not None:
            record["current_target_id"] = None
        write_json_atomic(state_file, record)

        current_lock = read_json(lock_file)
        if (
            current_lock.get("run_id") != record.get("run_id")
            or current_lock.get("manifest_sha256") != lock.get("manifest_sha256")
            or current_lock.get("owner", {}).get("nonce") != record.get("owner", {}).get("nonce")
            or current_lock.get("owner", {}).get("epoch") != record.get("owner", {}).get("epoch")
        ):
            raise StateError(
                "BLOCKED_OWNER_MISMATCH",
                "project lease changed while reconciling a stale pre-submission run",
            )
        lock_file.unlink()
        return record

    @staticmethod
    def _user_stop_authorization(record: dict[str, Any], authorization: dict[str, Any]) -> dict[str, Any]:
        required_true = (
            "explicit_user_request",
            "mutation_may_have_occurred",
            "duplicate_risk_acknowledged",
        )
        missing_true = [key for key in required_true if authorization.get(key) is not True]
        expected = {
            "run_id": record.get("run_id"),
            "project_root": record.get("project_root"),
            "session_id": record.get("session_id"),
            "target_id": record.get("current_target_id"),
            "conversation_url": record.get("conversation_url"),
        }
        mismatches = {
            key: {"expected": value, "actual": authorization.get(key)}
            for key, value in expected.items()
            if authorization.get(key) != value
        }
        if authorization.get("schema") != "codex.chatgpt.user-stop-authorization/v1" or missing_true or mismatches:
            raise StateError(
                "USER_STOP_AUTHORIZATION_INVALID",
                "an exact, explicit user-stop authorization bound to this run is required",
                {"missing_true": missing_true, "mismatches": mismatches},
            )
        reason = str(authorization.get("reason") or "").strip()
        reason_bytes = reason.encode("utf-8")
        if (
            not reason
            or len(reason_bytes) > 512
            or any(ord(character) < 32 or ord(character) == 127 for character in reason)
        ):
            raise StateError(
                "USER_STOP_AUTHORIZATION_INVALID",
                "user-stop reason must be 1..512 UTF-8 bytes without control characters",
            )
        clean = dict(authorization)
        clean["reason"] = reason
        return clean

    def _strict_parent_children(self, paths: RunPaths, parent: dict[str, Any]) -> list[tuple[Path, dict[str, Any]]]:
        """Stop-drain scanner: no silently skipped or unlisted child is safe."""
        entries = parent.get("children")
        if not isinstance(entries, list):
            raise StateError("USER_STOP_CHILD_SET_INVALID", "parent children must be a list")
        expected: dict[str, dict[str, Any]] = {}
        for entry in entries:
            if not isinstance(entry, dict) or not str(entry.get("run_id") or ""):
                raise StateError("USER_STOP_CHILD_SET_INVALID", "parent child entry is invalid")
            run_id = str(entry["run_id"])
            if run_id in expected:
                raise StateError("USER_STOP_CHILD_SET_INVALID", "parent contains a duplicate child entry", {"run_id": run_id})
            expected[run_id] = entry
        found: dict[str, tuple[Path, dict[str, Any]]] = {}
        for child_file in sorted(paths.runs_dir.glob("*/run.json")):
            child = read_json(child_file)
            if str(child.get("record_kind") or "") != "child" or str(child.get("parent_run_id") or "") != str(parent.get("run_id") or ""):
                continue
            run_id = str(child.get("run_id") or "")
            if not run_id or run_id in found or run_id not in expected:
                raise StateError("USER_STOP_CHILD_SET_INVALID", "child state does not exactly match parent child set", {"run_id": run_id})
            entry = expected[run_id]
            for key in ("stage_id", "role", "lane", "iteration"):
                if entry.get(key) != child.get(key):
                    raise StateError("USER_STOP_CHILD_SET_INVALID", "child stage metadata differs from parent entry", {"run_id": run_id, "field": key})
            bindings = (
                ("project_root", parent.get("project_root")),
                ("project_key", parent.get("project_key")),
                ("parent_run_id", parent.get("run_id")),
                ("parent_workflow_id", parent.get("workflow_id")),
                ("parent_lease_nonce", parent.get("lease_nonce")),
            )
            for key, expected_value in bindings:
                if str(child.get(key) or "") != str(expected_value or ""):
                    raise StateError(
                        "USER_STOP_CHILD_SET_INVALID",
                        "child ownership binding differs from parent",
                        {"run_id": run_id, "field": key},
                    )
            found[run_id] = (child_file, child)
        if set(found) != set(expected):
            raise StateError("USER_STOP_CHILD_SET_INVALID", "parent child record is missing", {"missing": sorted(set(expected) - set(found))})
        return [found[run_id] for run_id in sorted(found)]

    @staticmethod
    def historical_owned_target_ids(child_state_file: Path, child: dict[str, Any]) -> list[str]:
        """Return the complete ownership union without adopting any observed target."""
        targets = {str(child.get("current_target_id") or "")}
        for event in child.get("target_rebind_events") or []:
            if isinstance(event, dict):
                targets.update({str(event.get("old_target_id") or ""), str(event.get("new_target_id") or "")})
        lifecycle_path = child_state_file.parent / "tab-lifecycle.json"
        if lifecycle_path.exists():
            immutable_file_snapshot(lifecycle_path, embed_bytes=False)
            lifecycle = read_json(lifecycle_path)
            if lifecycle.get("schema") != "codex.chatgpt.agbrowse-tab-lifecycle/v1":
                raise StateError("HISTORICAL_TARGET_EVIDENCE_INVALID", "tab lifecycle schema is invalid")
            for event in lifecycle.get("events") or []:
                if isinstance(event, dict):
                    targets.add(str(event.get("target_id") or ""))
        composer_paths = {child_state_file.parent / "composer-app-evidence.json"}
        requested = child.get("requested") if isinstance(child.get("requested"), dict) else {}
        for key in ("composer_app_evidence", "composer_evidence"):
            value = child.get(key) or requested.get(key)
            if value:
                composer_paths.add(Path(str(value)))
        for path in composer_paths:
            if path.exists():
                immutable_file_snapshot(path, embed_bytes=False)
                composer = read_json(path)
                targets.add(str(composer.get("target_id") or composer.get("targetId") or ""))
        targets.discard("")
        return sorted(targets)

    def parent_historical_owned_target_ids(self, paths: RunPaths, parent: dict[str, Any]) -> list[str]:
        targets: set[str] = set()
        for child_file, child in self._strict_parent_children(paths, parent):
            targets.update(self.historical_owned_target_ids(child_file, child))
        return sorted(targets)

    @staticmethod
    def historical_owned_urls(child_state_file: Path, child: dict[str, Any]) -> list[str]:
        """Return URLs recorded by child ownership evidence without inferring ownership."""
        urls = {str(child.get("conversation_url") or "")}
        for event in child.get("target_rebind_events") or []:
            if isinstance(event, dict):
                for key in ("conversation_url", "old_conversation_url", "new_conversation_url", "url"):
                    urls.add(str(event.get(key) or ""))
        for evidence_name in ("tab-lifecycle.json", "composer-app-evidence.json"):
            path = child_state_file.parent / evidence_name
            if path.exists():
                immutable_file_snapshot(path, embed_bytes=False)
                evidence = read_json(path)
                values = evidence.get("events") if isinstance(evidence.get("events"), list) else [evidence]
                for value in values:
                    if isinstance(value, dict):
                        urls.add(str(value.get("url") or ""))
        urls.discard("")
        return sorted(urls)

    def parent_historical_owned_urls(self, paths: RunPaths, parent: dict[str, Any]) -> list[str]:
        urls: set[str] = set()
        for child_file, child in self._strict_parent_children(paths, parent):
            urls.update(self.historical_owned_urls(child_file, child))
        return sorted(urls)

    def user_stop_target_drift_candidate(self, run_dir: str | os.PathLike[str]) -> dict[str, Any]:
        """Validate static authority for the narrow read-only target-drift branch."""
        state_file, initial = self.load(run_dir)
        if str(initial.get("record_kind") or "") != "child":
            raise StateError("TARGET_DRIFT_CHILD_REQUIRED", "target drift requires a child")
        paths, parent_file, _, lock_file, _ = self._child_stop_context(state_file, initial)
        with exclusive_state_lock(paths.parent_transition_lock):
            child, parent, lock = read_json(state_file), read_json(parent_file), read_json(lock_file)
            if any(str(item.get("phase") or "") != "USER_STOP_REQUESTED" for item in (child, parent, lock)):
                raise StateError("TARGET_DRIFT_STOP_PHASE_INVALID", "child, parent, and lock must remain stopped")
            scope_ref = parent.get("parent_stop_scope") if isinstance(parent.get("parent_stop_scope"), dict) else {}
            scope_path = Path(str(scope_ref.get("path") or ""))
            stop = child.get("user_stop") if isinstance(child.get("user_stop"), dict) else {}
            auth = stop.get("authorization") if isinstance(stop.get("authorization"), dict) else {}
            auth_path = Path(str(auth.get("path") or ""))
            request = (lock.get("user_stop_requests") or {}).get(str(child.get("run_id") or ""))
            if (
                scope_ref != lock.get("parent_stop_scope")
                or not scope_path.is_file() or scope_path.is_symlink()
                or sha256_file(scope_path) != str(scope_ref.get("sha256") or "")
                or scope_path.stat().st_size != int(scope_ref.get("bytes") or -1)
                or not auth_path.is_file() or auth_path.is_symlink()
                or sha256_file(auth_path) != str(stop.get("authorization_sha256") or "")
                or not isinstance(request, dict)
            ):
                raise StateError("TARGET_DRIFT_AUTHORITY_INVALID", "scope or authorization is not exact")
            scope = read_json(scope_path)
            entries = [entry for entry in scope.get("ordered_children") or [] if isinstance(entry, dict) and str(entry.get("run_id") or "") == str(child.get("run_id") or "")]
            epoch = str(stop.get("stop_epoch_nonce") or "")
            if (
                scope.get("schema") != "codex.chatgpt.parent-wide-user-stop/v1"
                or scope.get("explicit_user_request") is not True
                or len(entries) != 1
                or any(entries[0].get(key) != child.get(key) for key in ("run_id", "stage_id", "role", "lane", "iteration"))
                or not epoch
                or str(request.get("stop_epoch_nonce") or "") != epoch
                or str(lock.get("stop_epoch_nonce") or "") != epoch
                or str(scope.get("stop_epoch_nonce") or "") != epoch
            ):
                raise StateError("TARGET_DRIFT_AUTHORITY_INVALID", "stop epoch or child tuple is not exact")
            return {
                "child_identity": {key: child.get(key) for key in ("run_id", "parent_run_id", "stage_id", "role", "lane", "iteration", "project_root", "project_key", "parent_workflow_id", "parent_lease_nonce", "manifest_sha256", "prompt_sha256")},
                "parent_identity": {key: parent.get(key) for key in ("run_id", "project_root", "project_key", "workflow_id", "lease_nonce", "manifest_sha256", "owner")},
                "lock_identity": {key: lock.get(key) for key in ("schema", "record_kind", "run_id", "parent_run_id", "project_root", "project_key", "workflow_id", "lease_nonce", "manifest_sha256", "owner", "phase", "stop_epoch_nonce")},
                "authorization": auth,
                "authorization_sha256": stop.get("authorization_sha256"),
                "stop_epoch_nonce": epoch,
                "parent_stop_scope": scope_ref,
                "recorded": {"session_id": child.get("session_id"), "target_id": child.get("current_target_id"), "conversation_url": child.get("conversation_url")},
                "historical_owned_target_ids": self.parent_historical_owned_target_ids(paths, parent),
                "preimages": {"child": immutable_file_snapshot(state_file, embed_bytes=False), "parent": immutable_file_snapshot(parent_file, embed_bytes=False), "lock": immutable_file_snapshot(lock_file, embed_bytes=False), "scope": immutable_file_snapshot(scope_path, embed_bytes=False), "authorization": immutable_file_snapshot(auth_path, embed_bytes=False)},
            }

    def _child_stop_context(self, state_file: Path, child: dict[str, Any]) -> tuple[RunPaths, Path, dict[str, Any], Path, dict[str, Any]]:
        root = canonical_project_root(str(child.get("project_root") or ""))
        paths = self.paths(root, str(child.get("parent_run_id") or ""))
        parent_file = paths.runs_dir / str(child.get("parent_run_id") or "") / "run.json"
        if not parent_file.is_file():
            raise StateError("USER_STOP_PARENT_MISSING", "child parent record is missing")
        parent = read_json(parent_file)
        lock_file, lock = self._verify_lock(state_file, child)
        if (
            str(parent.get("record_kind") or "") != "parent"
            or str(parent.get("project_root") or "") != str(root)
            or str(parent.get("project_key") or "") != str(child.get("project_key") or "")
            or str(parent.get("workflow_id") or "") != str(child.get("parent_workflow_id") or "")
            or str(parent.get("lease_nonce") or "") != str(child.get("parent_lease_nonce") or "")
            or str(lock.get("manifest_sha256") or "") != str(parent.get("manifest_sha256") or "")
        ):
            raise StateError("BLOCKED_OWNER_MISMATCH", "child stop binding does not match the exact parent")
        return paths, parent_file, parent, lock_file, lock

    @staticmethod
    def _stop_authorization_path(paths: RunPaths, child_run_id: str) -> Path:
        return paths.run_dir / "user-stop" / f"{child_run_id}.authorization.json"

    def begin_user_stop(
        self,
        run_dir: str | os.PathLike[str],
        *,
        authorization: dict[str, Any],
    ) -> dict[str, Any]:
        state_file, record = self.load(run_dir)
        if str(record.get("record_kind") or "") == "child":
            return self._begin_child_user_stop(state_file, record, authorization)
        if record.get("phase") == "ABANDONED_UNCERTAIN":
            return record
        lock_file, lock = self._verify_lock(state_file, record)
        current = str(record.get("phase") or "")
        if current == "USER_STOP_REQUESTED":
            return record
        if current in {"COMPLETE", "CANCELLED_PRE_SUBMISSION"}:
            raise StateError("USER_STOP_PHASE_INVALID", f"cannot abandon terminal run in phase {current}")
        if not self._crossed_send_boundary(record):
            raise StateError(
                "USER_STOP_PHASE_INVALID",
                "pre-submission runs must use the supported pre-submission cancellation path",
                {"phase": current},
            )
        clean = self._user_stop_authorization(record, authorization)
        now = utc_now()
        clean["recorded_at"] = now
        auth_sha256 = sha256_bytes(
            json.dumps(clean, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )
        record["user_stop"] = {
            "status": "requested",
            "authorization": clean,
            "authorization_sha256": auth_sha256,
            "challenge_nonce": uuid.uuid4().hex,
            "confirmation": None,
        }
        record.setdefault("phase_events", []).append({"from": current, "to": "USER_STOP_REQUESTED", "at": now})
        record.setdefault("recovery_events", []).append(
            {
                "at": now,
                "kind": "explicit-user-stop-requested",
                "authorization_sha256": auth_sha256,
                "source_phase": current,
            }
        )
        record["recovery_count"] = int(record.get("recovery_count") or 0) + 1
        record["phase"] = "USER_STOP_REQUESTED"
        record["phase_at"] = now
        record["updated_at"] = now
        record["terminal_block_code"] = "USER_STOP_CONFIRMATION_PENDING"
        write_json_atomic(state_file, record)
        lock.update(
            {
                "phase": "USER_STOP_REQUESTED",
                "session_id": record.get("session_id"),
                "target_id": record.get("current_target_id"),
                "conversation_url": record.get("conversation_url"),
                "heartbeat_at": now,
            }
        )
        write_json_atomic(lock_file, lock)
        return record

    def withdraw_unconfirmed_user_stop(self, run_dir: str | os.PathLike[str], *, authorization: dict[str, Any]) -> dict[str, Any]:
        """Withdraw only a standalone recovering stop before provider confirmation."""
        state_file, record = self.load(run_dir)
        if str(record.get("record_kind") or "standalone") != "standalone":
            raise StateError("USER_STOP_WITHDRAWAL_KIND_INVALID", "only standalone runs support stop withdrawal")
        lock_file, lock = self._verify_lock(state_file, record)
        stop = record.get("user_stop") if isinstance(record.get("user_stop"), dict) else {}
        events = record.get("phase_events") if isinstance(record.get("phase_events"), list) else []
        prior = events[-1] if events else {}
        if (record.get("phase") != "USER_STOP_REQUESTED" or stop.get("status") != "requested"
                or stop.get("confirmation") is not None or stop.get("provider_stop") is not None
                or prior.get("from") != "RECOVERING" or prior.get("to") != "USER_STOP_REQUESTED"):
            raise StateError("USER_STOP_WITHDRAWAL_NOT_SAFE", "withdrawal requires an unconfirmed RECOVERING stop")
        clean = self._user_stop_authorization(record, authorization)
        now = utc_now()
        digest = sha256_bytes(json.dumps(clean, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        record["user_stop"] = {**stop, "status": "withdrawn-before-confirmation", "withdrawal_authorization": clean, "withdrawal_authorization_sha256": digest, "withdrawn_at": now}
        record.setdefault("phase_events", []).append({"from": "USER_STOP_REQUESTED", "to": "RECOVERING", "at": now, "reason": "withdrawn-before-provider-stop"})
        record.setdefault("recovery_events", []).append({"at": now, "kind": "explicit-user-stop-withdrawn-before-confirmation", "withdrawal_authorization_sha256": digest})
        record.update({"phase": "RECOVERING", "phase_at": now, "updated_at": now, "terminal_block_code": None})
        write_json_atomic(state_file, record)
        lock.update({"phase": "RECOVERING", "heartbeat_at": now})
        write_json_atomic(lock_file, lock)
        return record

    def _begin_child_user_stop(self, state_file: Path, child: dict[str, Any], authorization: dict[str, Any]) -> dict[str, Any]:
        """Publish parent-owned stop intent before mutable parent/child state.

        A child never owns active.lock.  This is deliberately separate from the
        legacy standalone implementation above so normal runs retain their
        existing behavior.
        """
        if str(child.get("phase") or "") == "ABANDONED_UNCERTAIN":
            return child
        if not self._crossed_send_boundary(child):
            raise StateError("USER_STOP_PHASE_INVALID", "pre-submission child cannot use post-send user stop")
        if not all(str(child.get(k) or "") for k in ("session_id", "current_target_id", "conversation_url")):
            raise StateError("USER_STOP_IDENTITY_MISSING", "child user stop requires exact session, target, and canonical URL")
        paths, parent_file, parent, lock_file, lock = self._child_stop_context(state_file, child)
        clean = self._user_stop_authorization(child, authorization)
        with exclusive_state_lock(paths.parent_transition_lock):
            child = read_json(state_file)
            parent = read_json(parent_file)
            lock = read_json(lock_file)
            # Re-check all bindings under the barrier and ensure no live owner
            # can be interrupted by an external confirmation action.
            self._child_stop_context(state_file, child)
            if self._owner_observation(parent).get("same_process") or self._owner_observation(child).get("same_process"):
                raise StateError("USER_STOP_OWNER_ACTIVE", "live parent or child owner blocks explicit stop confirmation")
            existing = (lock.get("user_stop_requests") or {}).get(str(child.get("run_id") or ""))
            auth_path = self._stop_authorization_path(paths, str(child["run_id"]))
            persisted_auth: dict[str, Any] | None = None
            if auth_path.exists():
                if not auth_path.is_file() or auth_path.is_symlink():
                    raise StateError("USER_STOP_AUTHORIZATION_CONFLICT", "immutable child stop authorization path is invalid")
                persisted_auth = read_json(auth_path)
            stop_epoch = str(
                (existing or {}).get("stop_epoch_nonce")
                or (persisted_auth or {}).get("stop_epoch_nonce")
                or uuid.uuid4().hex
            )
            fixed_auth = {
                "schema": "codex.chatgpt.user-stop-authorization/v2",
                "explicit_user_request": True,
                "mutation_may_have_occurred": True,
                "duplicate_risk_acknowledged": True,
                "reason": clean["reason"],
                "reason_sha256": sha256_bytes(clean["reason"].encode("utf-8")),
                "stop_epoch_nonce": stop_epoch,
                "project_root": child["project_root"], "project_key": child["project_key"],
                "parent_run_id": parent["run_id"], "workflow_id": parent["workflow_id"],
                "parent_lease_nonce": parent["lease_nonce"], "parent_manifest_sha256": parent["manifest_sha256"],
                "child_run_id": child["run_id"], "child_manifest_sha256": child["manifest_sha256"],
                "child_prompt_sha256": child["prompt_sha256"], "stage_id": child["stage_id"],
                "role": child["role"], "lane": child["lane"], "iteration": child["iteration"],
                "source_phase": child["phase"], "session_id": child["session_id"],
                "target_id": child["current_target_id"], "canonical_conversation_url": child["conversation_url"],
                "send_attempt_count": child.get("send_attempt_count"), "send_limit": child.get("send_limit"),
                "send_claim_sha256": sha256_file(state_file.parent / "send.claim") if (state_file.parent / "send.claim").is_file() else None,
            }
            if persisted_auth is not None:
                mismatches = {
                    key: {"expected": value, "actual": persisted_auth.get(key)}
                    for key, value in fixed_auth.items()
                    if persisted_auth.get(key) != value
                }
                if (
                    mismatches
                    or not str(persisted_auth.get("requested_at") or "")
                    or (
                        not existing
                        and (
                            str(persisted_auth.get("parent_preimage_sha256") or "") != sha256_file(parent_file)
                            or str(persisted_auth.get("child_preimage_sha256") or "") != sha256_file(state_file)
                            or str(persisted_auth.get("lock_preimage_sha256") or "") != sha256_file(lock_file)
                        )
                    )
                ):
                    raise StateError(
                        "USER_STOP_AUTHORIZATION_CONFLICT",
                        "existing authorization descriptor does not bind the exact unchanged stop preimage",
                        {"mismatches": mismatches},
                    )
                auth_payload = persisted_auth
            else:
                auth_payload = {
                    **fixed_auth,
                    "requested_at": utc_now(),
                    "parent_preimage_sha256": sha256_file(parent_file),
                    "child_preimage_sha256": sha256_file(state_file),
                    "lock_preimage_sha256": sha256_file(lock_file),
                }
            if existing:
                descriptor = existing.get("authorization") if isinstance(existing.get("authorization"), dict) else {}
                if (
                    str(existing.get("stop_epoch_nonce") or "") != stop_epoch
                    or Path(str(descriptor.get("path") or "")) != auth_path
                    or not auth_path.is_file()
                    or auth_path.is_symlink()
                    or str(descriptor.get("sha256") or "") != sha256_file(auth_path)
                    or int(descriptor.get("bytes") or -1) != auth_path.stat().st_size
                ):
                    raise StateError("USER_STOP_AUTHORIZATION_CONFLICT", "existing stop request does not match immutable authorization")
                persisted = read_json(auth_path)
                if persisted != auth_payload:
                    raise StateError("USER_STOP_AUTHORIZATION_CONFLICT", "retry reason differs from immutable user-stop authorization")
            else:
                descriptor = write_immutable_json_exclusive(auth_path, auth_payload)
            requests = dict(lock.get("user_stop_requests") or {})
            requests[str(child["run_id"])] = {"authorization": descriptor, "stop_epoch_nonce": stop_epoch}
            # Parent lock changes first: a crash after this point blocks sends.
            lock.update({"phase": "USER_STOP_REQUESTED", "recovery_required": True, "stop_epoch_nonce": stop_epoch, "user_stop_requests": requests, "heartbeat_at": utc_now()})
            for key in ("session_id", "target_id", "conversation_url"):
                lock.pop(key, None)
            write_json_atomic(lock_file, lock)
            if str(parent.get("phase") or "") != "USER_STOP_REQUESTED":
                parent.setdefault("phase_events", []).append({"from": parent.get("phase"), "to": "USER_STOP_REQUESTED", "at": utc_now()})
            parent.update({"phase": "USER_STOP_REQUESTED", "phase_at": utc_now(), "updated_at": utc_now(), "recovery_required": True, "user_stop_requests": requests})
            write_json_atomic(parent_file, parent)
            if str(child.get("phase") or "") != "USER_STOP_REQUESTED":
                child.setdefault("phase_events", []).append({"from": child.get("phase"), "to": "USER_STOP_REQUESTED", "at": utc_now()})
            child.update({"phase": "USER_STOP_REQUESTED", "phase_at": utc_now(), "updated_at": utc_now(), "terminal_block_code": "USER_STOP_CONFIRMATION_PENDING", "user_stop": {"status": "requested", "authorization": descriptor, "authorization_sha256": descriptor["sha256"], "stop_epoch_nonce": stop_epoch}})
            write_json_atomic(state_file, child)
            return child

    def finalize_user_stop(
        self,
        run_dir: str | os.PathLike[str],
        *,
        confirmation: dict[str, Any],
    ) -> dict[str, Any]:
        state_file, record = self.load(run_dir)
        if str(record.get("record_kind") or "") == "child":
            return self._finalize_child_user_stop(state_file, record, confirmation)
        if record.get("phase") == "ABANDONED_UNCERTAIN":
            return record
        lock_file, lock = self._verify_lock(state_file, record)
        if record.get("phase") != "USER_STOP_REQUESTED":
            raise StateError("USER_STOP_PHASE_INVALID", "finalization requires USER_STOP_REQUESTED")

        try:
            evidence_path = Path(str(confirmation.get("path") or "")).expanduser().resolve(strict=True)
            evidence_path.relative_to(state_file.parent)
        except (OSError, RuntimeError, ValueError) as exc:
            raise StateError("USER_STOP_EVIDENCE_INVALID", "confirmation evidence must be inside the exact run directory") from exc
        if not evidence_path.is_file() or evidence_path.is_symlink():
            raise StateError("USER_STOP_EVIDENCE_INVALID", "confirmation evidence must be a regular non-symlink file")
        actual_hash = sha256_file(evidence_path)
        actual_bytes = evidence_path.stat().st_size
        if actual_hash != str(confirmation.get("sha256") or "") or actual_bytes != int(confirmation.get("bytes") or -1):
            raise StateError(
                "USER_STOP_EVIDENCE_INVALID",
                "confirmation evidence hash or byte count does not match",
                {"actual_sha256": actual_hash, "actual_bytes": actual_bytes},
            )
        evidence = read_json(evidence_path)
        classification = evidence.get("classification") if isinstance(evidence.get("classification"), dict) else {}
        user_stop = record.get("user_stop") if isinstance(record.get("user_stop"), dict) else {}
        expected_identity = {
            "run_id": record.get("run_id"),
            "session_id": record.get("session_id"),
            "target_id": record.get("current_target_id"),
            "conversation_url": record.get("conversation_url"),
            "authorization_sha256": user_stop.get("authorization_sha256"),
            "challenge_nonce": user_stop.get("challenge_nonce"),
        }
        mismatches = {
            key: {"expected": value, "actual": evidence.get(key)}
            for key, value in expected_identity.items()
            if evidence.get(key) != value
        }
        valid_classification = bool(
            evidence.get("schema") == "codex.chatgpt.user-stop-evidence/v1"
            and evidence.get("mutation_may_have_occurred") is True
            and evidence.get("tab_closed") is False
            and classification.get("identity_match") is True
            and classification.get("generation_active") is False
            and (
                classification.get("terminal_session") is True
                or classification.get("identity_missing_owner_dead") is True
            )
        )
        if mismatches or not valid_classification:
            raise StateError(
                "USER_STOP_EVIDENCE_INVALID",
                "evidence does not prove the exact run is no longer generating",
                {"mismatches": mismatches, "classification": classification},
            )

        now = utc_now()
        descriptor = {
            "path": str(evidence_path),
            "sha256": actual_hash,
            "bytes": actual_bytes,
        }
        user_stop["status"] = "confirmed-abandoned-uncertain"
        user_stop["confirmation"] = descriptor
        user_stop["confirmed_at"] = now
        record["user_stop"] = user_stop
        record.setdefault("phase_events", []).append(
            {"from": "USER_STOP_REQUESTED", "to": "ABANDONED_UNCERTAIN", "at": now}
        )
        record.setdefault("recovery_events", []).append(
            {
                "at": now,
                "kind": "explicit-user-abandoned-uncertain",
                "confirmation": descriptor,
                "mutation_may_have_occurred": True,
            }
        )
        record["recovery_count"] = int(record.get("recovery_count") or 0) + 1
        record["phase"] = "ABANDONED_UNCERTAIN"
        record["phase_at"] = now
        record["updated_at"] = now
        record["terminal_block_code"] = None
        write_json_atomic(state_file, record)

        current_lock = read_json(lock_file)
        if (
            current_lock.get("run_id") != record.get("run_id")
            or current_lock.get("manifest_sha256") != lock.get("manifest_sha256")
            or current_lock.get("owner", {}).get("nonce") != record.get("owner", {}).get("nonce")
            or current_lock.get("owner", {}).get("epoch") != record.get("owner", {}).get("epoch")
        ):
            raise StateError("BLOCKED_OWNER_MISMATCH", "project lease changed while finalizing user stop")
        lock_file.unlink()
        return record

    def _finalize_child_user_stop(self, state_file: Path, child: dict[str, Any], confirmation: dict[str, Any]) -> dict[str, Any]:
        if str(child.get("phase") or "") == "ABANDONED_UNCERTAIN":
            return child
        paths, parent_file, parent, lock_file, lock = self._child_stop_context(state_file, child)
        with exclusive_state_lock(paths.parent_transition_lock):
            child = read_json(state_file)
            parent = read_json(parent_file)
            lock = read_json(lock_file)
            if str(child.get("phase") or "") != "USER_STOP_REQUESTED" or str(parent.get("phase") or "") != "USER_STOP_REQUESTED" or str(lock.get("phase") or "") != "USER_STOP_REQUESTED":
                raise StateError("USER_STOP_PHASE_INVALID", "child and parent must remain in explicit stop state")
            user_stop = child.get("user_stop") if isinstance(child.get("user_stop"), dict) else {}
            auth = user_stop.get("authorization") if isinstance(user_stop.get("authorization"), dict) else {}
            auth_path = Path(str((auth or {}).get("path") or ""))
            if not auth_path.is_file() or auth_path.is_symlink() or sha256_file(auth_path) != str(user_stop.get("authorization_sha256") or ""):
                raise StateError("USER_STOP_AUTHORIZATION_INVALID", "immutable child stop authorization is unavailable")
            request = (lock.get("user_stop_requests") or {}).get(str(child.get("run_id") or ""))
            if not isinstance(request, dict) or str(request.get("stop_epoch_nonce") or "") != str(user_stop.get("stop_epoch_nonce") or "") or str(lock.get("stop_epoch_nonce") or "") != str(user_stop.get("stop_epoch_nonce") or ""):
                raise StateError("USER_STOP_AUTHORIZATION_INVALID", "parent lock stop epoch does not bind the exact child")
            evidence_path = Path(str(confirmation.get("path") or ""))
            if not evidence_path.is_file() or evidence_path.is_symlink() or sha256_file(evidence_path) != str(confirmation.get("sha256") or ""):
                raise StateError("USER_STOP_EVIDENCE_INVALID", "exact stop adjudication descriptor is unavailable")
            evidence = read_json(evidence_path)
            if evidence.get("schema") != "codex.chatgpt.user-stop-adjudication/v2" or evidence.get("terminal") is not True:
                raise StateError("USER_STOP_EVIDENCE_INVALID", "stop adjudication is not terminal")
            cleanup = evidence.get("cleanup") if isinstance(evidence.get("cleanup"), dict) else {}
            if cleanup.get("ok") is not True or str(cleanup.get("state") or "") not in {"closed-and-absent", "already-absent"}:
                raise StateError("USER_STOP_CLEANUP_PENDING", "exact stopped target cleanup is not proven")
            if str(cleanup.get("target_id") or "") != str(child.get("current_target_id") or "") or str(cleanup.get("conversation_url") or "") != str(child.get("conversation_url") or ""):
                raise StateError("USER_STOP_CLEANUP_IDENTITY_MISMATCH", "cleanup identity differs from stopped child")
            child["cleanup_pending"] = False
            child["owned_tab_state"] = str(cleanup["state"])
            child["owned_open_tabs"] = 0
            child["cleanup_evidence"] = cleanup
            child["user_stop"]["confirmation"] = {"path": str(evidence_path), "sha256": sha256_file(evidence_path), "bytes": evidence_path.stat().st_size}
            child["user_stop"]["status"] = "confirmed-abandoned-uncertain"
            child.setdefault("phase_events", []).append({"from": "USER_STOP_REQUESTED", "to": "ABANDONED_UNCERTAIN", "at": utc_now()})
            child.update({"phase": "ABANDONED_UNCERTAIN", "phase_at": utc_now(), "updated_at": utc_now(), "terminal_block_code": None})
            write_json_atomic(state_file, child)
            # Deliberately do not remove the parent lock; parent drain owns it.
            return child

    def finalize_user_stop_target_drift(
        self, run_dir: str | os.PathLike[str], *, abandonment: dict[str, Any]
    ) -> dict[str, Any]:
        """Abandon one stopped child without adopting or closing its survivor."""
        state_file, initial = self.load(run_dir)
        if str(initial.get("phase") or "") == "ABANDONED_UNCERTAIN":
            existing = (initial.get("user_stop") or {}).get("target_drift_abandonment")
            if existing == abandonment:
                return initial
            raise StateError("TARGET_DRIFT_DESCRIPTOR_CONFLICT", "a different abandonment is already recorded")
        candidate = self.user_stop_target_drift_candidate(run_dir)
        paths, parent_file, _, lock_file, _ = self._child_stop_context(state_file, initial)
        with exclusive_state_lock(paths.parent_transition_lock):
            child, parent, lock = read_json(state_file), read_json(parent_file), read_json(lock_file)
            descriptor_path = Path(str(abandonment.get("path") or ""))
            try:
                descriptor_path.resolve(strict=True).relative_to((state_file.parent / "user-stop").resolve(strict=True))
            except (OSError, RuntimeError, ValueError) as exc:
                raise StateError("TARGET_DRIFT_DESCRIPTOR_INVALID", "descriptor is outside child user-stop evidence") from exc
            if (
                not descriptor_path.is_file() or descriptor_path.is_symlink()
                or sha256_file(descriptor_path) != str(abandonment.get("sha256") or "")
                or descriptor_path.stat().st_size != int(abandonment.get("bytes") or -1)
            ):
                raise StateError("TARGET_DRIFT_DESCRIPTOR_INVALID", "descriptor bytes are not exact")
            evidence = read_json(descriptor_path)
            current_preimages = {
                "child": immutable_file_snapshot(state_file, embed_bytes=False),
                "parent": immutable_file_snapshot(parent_file, embed_bytes=False),
                "lock": immutable_file_snapshot(lock_file, embed_bytes=False),
            }
            decision = str(evidence.get("decision") or "")
            survivor = evidence.get("protected_survivor") if isinstance(evidence.get("protected_survivor"), dict) else {}
            stale = evidence.get("reported_stale_target") if isinstance(evidence.get("reported_stale_target"), dict) else {}
            historical = candidate["historical_owned_target_ids"]
            required_absent = evidence.get("required_absent_target_ids")
            absence_union = evidence.get("historical_target_absence_union")
            live_survivor_valid = bool(
                decision == "abandon-without-close"
                and required_absent == historical
                and absence_union == required_absent
                and survivor.get("ownership_adopted") is False
                and survivor.get("close_authorized") is False
                and survivor.get("tab_closed") is False
                and survivor.get("classification") == "unowned-or-foreign-protected"
                and str(survivor.get("target_id") or "") not in historical
            )
            stale_target_id = str(stale.get("target_id") or "")
            no_live_target_valid = bool(
                decision == "abandon-without-close-no-live-target"
                and stale_target_id
                and stale_target_id not in historical
                and required_absent == sorted({*historical, stale_target_id})
                and absence_union == required_absent
                and stale.get("ownership_adopted") is False
                and stale.get("close_authorized") is False
                and stale.get("tab_closed") is False
                and stale.get("proven_absent") is True
                and stale.get("classification") == "unowned-reported-stale-target-absent"
                and not survivor
            )
            proof_rounds = evidence.get("proof_rounds")
            same_target_stale_sent_valid = bool(
                decision == "abandon-without-close-stale-sent-session"
                and isinstance(proof_rounds, list)
                and len(proof_rounds) == 2
                and stale_target_id == str(candidate["recorded"].get("target_id") or "")
                and stale_target_id in historical
                and required_absent == historical
                and absence_union == required_absent
                and stale.get("ownership_adopted") is False
                and stale.get("close_authorized") is False
                and stale.get("tab_closed") is False
                and stale.get("proven_absent") is True
                and stale.get("classification") == "owned-reported-stale-target-absent"
                and not survivor
                and all(
                    isinstance(round_, dict)
                    and round_.get("valid") is True
                    and round_.get("stale_sent_session_valid") is True
                    and round_.get("session_virtual_url") is True
                    and round_.get("stored_target_absent") is True
                    and round_.get("status") == "sent"
                    and str(round_.get("survivor_target_id") or "") == stale_target_id
                    and not round_.get("helper_values")
                    and re.fullmatch(
                        r"https://chatgpt\.com/c/WEB:[0-9A-Fa-f-]{16,}(?:[?#].*)?",
                        str(round_.get("session_url") or ""),
                    )
                    for round_ in proof_rounds
                )
                and str(proof_rounds[0].get("session_url") or "")
                == str(proof_rounds[1].get("session_url") or "")
                == str(stale.get("conversation_url") or "")
            )
            if (
                evidence.get("schema") != "codex.chatgpt.user-stop-target-drift-abandonment/v1"
                or not (
                    live_survivor_valid
                    or no_live_target_valid
                    or same_target_stale_sent_valid
                )
                or evidence.get("submission_outcome") != "unknown"
                or evidence.get("provider_terminal_asserted") is not False
                or evidence.get("provider_mutation_may_have_occurred") is not True
                or evidence.get("recorded") != candidate["recorded"]
                or evidence.get("historical_owned_target_ids") != historical
                or evidence.get("parent_stop_scope") != candidate["parent_stop_scope"]
                or evidence.get("stop_epoch_nonce") != candidate["stop_epoch_nonce"]
                or evidence.get("authorization_sha256") != candidate["authorization_sha256"]
                or any(evidence.get("preimages", {}).get(key) != current_preimages[key] for key in current_preimages)
            ):
                raise StateError("TARGET_DRIFT_DESCRIPTOR_INVALID", "descriptor does not bind the unchanged stop state")
            recorded_target = str(child.get("current_target_id") or "")
            recorded_url = child.get("conversation_url")
            rebind_events = list(child.get("target_rebind_events") or [])
            receipt = child.get("submission_receipt")
            child.setdefault("user_stop", {})["target_drift_abandonment"] = abandonment
            child["user_stop"]["status"] = "confirmed-target-drift-abandoned-uncertain"
            child["result"] = None
            child["cleanup_pending"] = False
            child["owned_open_tabs"] = 0
            child["owned_tab_state"] = (
                "historical-target-absent-survivor-protected"
                if live_survivor_valid
                else "historical-and-reported-targets-absent"
            )
            child["terminal_block_code"] = None
            now = utc_now()
            child.setdefault("phase_events", []).append({"from": "USER_STOP_REQUESTED", "to": "ABANDONED_UNCERTAIN", "at": now, "reason": "target-drift-no-close"})
            child.update({"phase": "ABANDONED_UNCERTAIN", "phase_at": now, "updated_at": now})
            if (
                str(child.get("current_target_id") or "") != recorded_target
                or child.get("conversation_url") != recorded_url
                or list(child.get("target_rebind_events") or []) != rebind_events
                or child.get("submission_receipt") != receipt
            ):
                raise StateError("TARGET_DRIFT_OWNERSHIP_MUTATION_FORBIDDEN", "target ownership changed during abandonment")
            write_json_atomic(state_file, child)
            return child

    def adopt_legacy_user_stop(self, parent_run_dir: str | os.PathLike[str]) -> dict[str, Any]:
        """Adopt only the observed v1 child/parent lock split topology.

        No broad inference: a second matching child, missing v1 challenge, live
        owner, or any lease/root/workflow mismatch stays fail-closed.
        """
        parent_file, initial = self.load(parent_run_dir)
        if str(initial.get("record_kind") or "") != "parent":
            raise StateError("PARENT_RECORD_REQUIRED", "legacy stop adoption requires parent")
        paths = self.paths(canonical_project_root(initial["project_root"]), str(initial["run_id"]))
        with exclusive_state_lock(paths.parent_transition_lock):
            parent = read_json(parent_file); lock = read_json(paths.lock_file)
            if str(parent.get("phase") or "") == "USER_STOP_REQUESTED":
                return parent
            if str(parent.get("phase") or "") != "PARENT_ACTIVE" or str(lock.get("record_kind") or "") != "parent" or str(lock.get("phase") or "") != "USER_STOP_REQUESTED":
                raise StateError("PROJECT_LOCK_STATE_AMBIGUOUS", "not an exact legacy user-stop split")
            for key, expected in (("project_root", parent.get("project_root")), ("project_key", parent.get("project_key")), ("workflow_id", parent.get("workflow_id")), ("lease_nonce", parent.get("lease_nonce")), ("manifest_sha256", parent.get("manifest_sha256"))):
                if str(lock.get(key) or "") != str(expected or ""):
                    raise StateError("PROJECT_LOCK_STATE_AMBIGUOUS", "legacy lock binding differs from parent", {"field": key})
            if self._owner_observation(parent).get("same_process"):
                raise StateError("PROJECT_LOCK_STATE_AMBIGUOUS", "live parent owner blocks legacy adoption")
            identity = (str(lock.get("session_id") or ""), str(lock.get("target_id") or ""), str(lock.get("conversation_url") or ""))
            if not all(identity):
                raise StateError("PROJECT_LOCK_STATE_AMBIGUOUS", "legacy lock has incomplete child identity")
            matches = [(p, c) for p, c in self._strict_parent_children(paths, parent) if (str(c.get("session_id") or ""), str(c.get("current_target_id") or ""), str(c.get("conversation_url") or "")) == identity and str(c.get("phase") or "") == "USER_STOP_REQUESTED"]
            if len(matches) != 1:
                raise StateError("PROJECT_LOCK_STATE_AMBIGUOUS", "legacy lock must bind exactly one stopped child", {"match_count": len(matches)})
            child_file, child = matches[0]
            if self._owner_observation(child).get("same_process"):
                raise StateError("PROJECT_LOCK_STATE_AMBIGUOUS", "live child owner blocks legacy adoption")
            v1 = child.get("user_stop") if isinstance(child.get("user_stop"), dict) else {}
            auth = v1.get("authorization") if isinstance(v1.get("authorization"), dict) else {}
            auth_hash = sha256_bytes(json.dumps(auth, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))
            if auth.get("schema") != "codex.chatgpt.user-stop-authorization/v1" or str(v1.get("authorization_sha256") or "") != auth_hash or not v1.get("challenge_nonce"):
                raise StateError("PROJECT_LOCK_STATE_AMBIGUOUS", "legacy child v1 authorization is incomplete")
            binding_path = paths.run_dir / "user-stop" / f"{child['run_id']}.legacy-binding.json"
            persisted_binding: dict[str, Any] | None = None
            if binding_path.exists():
                if not binding_path.is_file() or binding_path.is_symlink():
                    raise StateError("PROJECT_LOCK_STATE_AMBIGUOUS", "legacy binding descriptor path is invalid")
                persisted_binding = read_json(binding_path)
            epoch = str((persisted_binding or {}).get("stop_epoch_nonce") or uuid.uuid4().hex)
            binding = {"schema": "codex.chatgpt.user-stop-legacy-binding-adjudication/v1", "parent_run_id": parent["run_id"], "child_run_id": child["run_id"], "project_root": parent["project_root"], "project_key": parent["project_key"], "workflow_id": parent["workflow_id"], "lease_nonce": parent["lease_nonce"], "manifest_sha256": parent["manifest_sha256"], "session_id": identity[0], "target_id": identity[1], "conversation_url": identity[2], "v1_authorization_sha256": v1["authorization_sha256"], "v1_challenge_nonce": v1["challenge_nonce"], "parent_preimage_sha256": sha256_file(parent_file), "child_preimage_sha256": sha256_file(child_file), "lock_preimage_sha256": sha256_file(paths.lock_file), "stop_epoch_nonce": epoch}
            if persisted_binding is not None and persisted_binding != binding:
                raise StateError("PROJECT_LOCK_STATE_AMBIGUOUS", "legacy binding descriptor differs from the unchanged exact preimage")
            descriptor = write_immutable_json_exclusive(binding_path, binding)
            request = {"authorization": descriptor, "authorization_sha256": descriptor["sha256"], "stop_epoch_nonce": epoch, "legacy_binding": descriptor}
            lock.update({"phase": "USER_STOP_REQUESTED", "recovery_required": True, "stop_epoch_nonce": epoch, "user_stop_requests": {str(child["run_id"]): request}})
            for key in ("session_id", "target_id", "conversation_url"):
                lock.pop(key, None)
            parent.update({"phase": "USER_STOP_REQUESTED", "phase_at": utc_now(), "updated_at": utc_now(), "recovery_required": True, "user_stop_requests": {str(child["run_id"]): request}})
            child["user_stop"] = {**v1, "authorization": descriptor, "authorization_sha256": descriptor["sha256"], "stop_epoch_nonce": epoch, "legacy_binding": descriptor}
            child["updated_at"] = utc_now()
            write_json_atomic(paths.lock_file, lock); write_json_atomic(parent_file, parent); write_json_atomic(child_file, child)
            return parent

    def establish_parent_wide_user_stop_scope(
        self,
        parent_run_dir: str | os.PathLike[str],
        *,
        manager_authorization: dict[str, Any],
        target_child_run_id: str,
    ) -> dict[str, Any]:
        """Publish and latch explicit authority over the exact listed workflow."""
        parent_file, initial = self.load(parent_run_dir)
        if str(initial.get("record_kind") or "") != "parent":
            raise StateError("PARENT_RECORD_REQUIRED", "parent-wide stop scope requires a parent")
        paths = self.paths(canonical_project_root(initial["project_root"]), str(initial["run_id"]))
        with exclusive_state_lock(paths.parent_transition_lock):
            parent = read_json(parent_file)
            lock = read_json(paths.lock_file)
            if (
                str(parent.get("phase") or "") != "USER_STOP_REQUESTED"
                or str(lock.get("phase") or "") != "USER_STOP_REQUESTED"
                or manager_authorization.get("explicit_user_request") is not True
                or str(manager_authorization.get("scope_kind") or "")
                != "exact-parent-and-listed-children"
            ):
                raise StateError(
                    "PARENT_WIDE_STOP_AUTHORIZATION_INVALID",
                    "explicit exact-parent stop authority is required",
                )
            children = self._strict_parent_children(paths, parent)
            ordered_entries = [
                {
                    key: entry.get(key)
                    for key in ("run_id", "stage_id", "role", "lane", "iteration")
                }
                for entry in parent.get("children") or []
            ]
            child_digest = sha256_bytes(
                json.dumps(
                    ordered_entries,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            target_matches = [
                (child_file, child)
                for child_file, child in children
                if str(child.get("run_id") or "") == str(target_child_run_id)
            ]
            if len(target_matches) != 1:
                raise StateError(
                    "PARENT_WIDE_STOP_TARGET_AMBIGUOUS",
                    "scope must bind exactly one historical target child",
                )
            target_file, target = target_matches[0]
            stop = target.get("user_stop") if isinstance(target.get("user_stop"), dict) else {}
            authorization = stop.get("authorization") if isinstance(stop.get("authorization"), dict) else {}
            auth_path = Path(str(authorization.get("path") or ""))
            if (
                str(target.get("phase") or "") != "USER_STOP_REQUESTED"
                or not auth_path.is_file()
                or auth_path.is_symlink()
                or sha256_file(auth_path) != str(stop.get("authorization_sha256") or "")
            ):
                raise StateError(
                    "PARENT_WIDE_STOP_TARGET_AUTH_INVALID",
                    "historical target-child authorization is not exact",
                )
            stop_epoch = str(lock.get("stop_epoch_nonce") or stop.get("stop_epoch_nonce") or "")
            if not stop_epoch or stop_epoch != str(stop.get("stop_epoch_nonce") or ""):
                raise StateError(
                    "PARENT_WIDE_STOP_EPOCH_MISMATCH",
                    "parent and target stop epochs differ",
                )
            scope_dir = paths.run_dir / "user-stop"
            manager_path = scope_dir / "parent-wide-manager-authorization.json"
            if manager_path.exists():
                manager_descriptor = immutable_file_snapshot(manager_path, embed_bytes=False)
                persisted_manager = read_json(manager_path)
                if persisted_manager != manager_authorization:
                    raise StateError(
                        "PARENT_WIDE_STOP_AUTHORIZATION_CONFLICT",
                        "manager authorization changed across retry",
                    )
                manager_descriptor = {
                    "path": str(manager_path),
                    "sha256": sha256_file(manager_path),
                    "bytes": manager_path.stat().st_size,
                    "bytes_base64": base64.b64encode(manager_path.read_bytes()).decode("ascii"),
                }
            else:
                manager_descriptor = write_immutable_json_exclusive(
                    manager_path, manager_authorization
                )
                manager_descriptor["bytes_base64"] = base64.b64encode(
                    manager_path.read_bytes()
                ).decode("ascii")
            scope_path = scope_dir / "parent-wide-stop-scope.json"
            if scope_path.exists():
                descriptor = {
                    "path": str(scope_path),
                    "sha256": sha256_file(scope_path),
                    "bytes": scope_path.stat().st_size,
                }
                scope = read_json(scope_path)
            else:
                reason = str(manager_authorization.get("reason") or "explicit user stop")
                scope = {
                    "schema": "codex.chatgpt.parent-wide-user-stop/v1",
                    "authorization_id": str(
                        manager_authorization.get("authorization_id") or uuid.uuid4().hex
                    ),
                    "issued_at": str(manager_authorization.get("issued_at") or utc_now()),
                    "explicit_user_request": True,
                    "scope_kind": "exact-parent-and-listed-children",
                    "stop_epoch_nonce": stop_epoch,
                    "reason": reason,
                    "reason_sha256": sha256_bytes(reason.encode("utf-8")),
                    "authorized_actions": [
                        "close-exact-owned-targets",
                        "mark-abandoned-uncertain",
                        "drain-parent-failed-closed",
                    ],
                    "prohibited_actions": [
                        "send",
                        "retry",
                        "recovery-submission",
                        "target-rebind",
                        "result-capture",
                        "result-promotion",
                        "parent-aggregation",
                        "reopening",
                    ],
                    "parent_identity": {
                        "run_id": parent.get("run_id"),
                        "workflow_id": parent.get("workflow_id"),
                        "lease_nonce": parent.get("lease_nonce"),
                        "recorded_project_root": parent.get("project_root"),
                        "canonical_project_root": str(canonical_project_root(parent["project_root"])),
                        "project_key": parent.get("project_key"),
                        "manifest_sha256": parent.get("manifest_sha256"),
                    },
                    "ordered_children": ordered_entries,
                    "child_count": len(ordered_entries),
                    "child_list_sha256": child_digest,
                    "sorted_child_run_ids": sorted(
                        str(entry["run_id"]) for entry in ordered_entries
                    ),
                    "manager_authorization": manager_descriptor,
                    "target_child_authorization": {
                        "run_id": target.get("run_id"),
                        "authorization": authorization,
                        "authorization_sha256": stop.get("authorization_sha256"),
                        "challenge_nonce": stop.get("challenge_nonce"),
                        "reason_sha256": stop.get("reason_sha256"),
                        "session_id": target.get("session_id"),
                        "target_id": target.get("current_target_id"),
                        "conversation_url": target.get("conversation_url"),
                    },
                    "preimages": {
                        "parent": immutable_file_snapshot(parent_file, embed_bytes=False),
                        "lock": immutable_file_snapshot(paths.lock_file, embed_bytes=False),
                        "target_child": immutable_file_snapshot(target_file, embed_bytes=False),
                    },
                }
                descriptor = write_immutable_json_exclusive(scope_path, scope)
            expected_parent_identity = {
                "run_id": parent.get("run_id"),
                "workflow_id": parent.get("workflow_id"),
                "lease_nonce": parent.get("lease_nonce"),
                "recorded_project_root": parent.get("project_root"),
                "canonical_project_root": str(canonical_project_root(parent["project_root"])),
                "project_key": parent.get("project_key"),
                "manifest_sha256": parent.get("manifest_sha256"),
            }
            if (
                scope.get("schema") != "codex.chatgpt.parent-wide-user-stop/v1"
                or scope.get("explicit_user_request") is not True
                or scope.get("parent_identity") != expected_parent_identity
                or scope.get("ordered_children") != ordered_entries
                or scope.get("child_count") != len(ordered_entries)
                or scope.get("child_list_sha256") != child_digest
                or scope.get("stop_epoch_nonce") != stop_epoch
            ):
                raise StateError(
                    "PARENT_WIDE_STOP_SCOPE_CONFLICT",
                    "persisted stop scope differs from the exact workflow",
                )
            reference = {**descriptor, "stop_epoch_nonce": stop_epoch}
            parent["parent_stop_scope"] = reference
            parent["user_stop_tombstone"] = {
                "permanent": True,
                "stop_epoch_nonce": stop_epoch,
                "scope": reference,
            }
            lock["parent_stop_scope"] = reference
            lock["user_stop_tombstone"] = parent["user_stop_tombstone"]
            parent["updated_at"] = utc_now()
            lock["heartbeat_at"] = utc_now()
            write_json_atomic(parent_file, parent)
            write_json_atomic(paths.lock_file, lock)
            return reference

    def reconcile_project_lock(
        self,
        project_root: str | os.PathLike[str],
        *,
        apply_safe_pre_submission: bool = False,
    ) -> dict[str, Any]:
        root = canonical_project_root(project_root)
        paths = self.paths(root, "unused")
        lock = self._read_existing_lock(paths.lock_file)
        records = self._active_or_uncertain_records(paths.runs_dir)
        if lock is None:
            if records:
                return {
                    "ok": False,
                    "state": "PROJECT_LOCK_STATE_AMBIGUOUS",
                    "reason": "active or uncertain records exist without the project lock",
                    "records": records[:10],
                }
            return {"ok": True, "state": "CLEAR", "project_root": str(root), "changed": False}

        run_id = str(lock.get("run_id") or "")
        if not run_id:
            return {
                "ok": False,
                "state": "PROJECT_LOCK_STATE_AMBIGUOUS",
                "reason": "project lock has no run_id",
            }
        state_file = paths.runs_dir / run_id / "run.json"
        if not state_file.is_file():
            return {
                "ok": False,
                "state": "PROJECT_LOCK_STATE_AMBIGUOUS",
                "reason": "project lock points to a missing run record",
                "run_id": run_id,
            }
        _, record = self.load(state_file)
        try:
            verified_lock_file, verified_lock = self._verify_lock(state_file, record)
        except StateError as exc:
            return {
                "ok": False,
                "state": "PROJECT_LOCK_STATE_AMBIGUOUS",
                "reason": exc.code,
                "run_id": run_id,
            }
        observation = self._owner_observation(record)
        evidence = {
            "run_id": run_id,
            "phase": record.get("phase"),
            "session_id": record.get("session_id"),
            "target_id": record.get("current_target_id"),
            "conversation_url": record.get("conversation_url"),
            "owner_observation": observation,
        }
        if str(record.get("record_kind") or "") == "parent" and str(record.get("phase") or "") == "PARENT_ACTIVE" and str(verified_lock.get("phase") or "") == "USER_STOP_REQUESTED":
            return {"ok": False, "state": "LEGACY_USER_STOP_BINDING_RECOVERABLE", "changed": False, "run_id": run_id, "supported_confirm_command": f'python "{Path(__file__).resolve().with_name("chatgpt_agbrowse_bridge.py")}" confirm-user-stop --run "<exact-legacy-child-run-dir>"', **evidence}
        if str(record.get("phase") or "") == "USER_STOP_REQUESTED":
            return {
                "ok": False,
                "state": "USER_STOP_CONFIRMATION_PENDING",
                "changed": False,
                "reason": "the exact run must be observed terminal before its project lock can be released",
                "supported_abandon_command": (
                    f'python "{Path(__file__).resolve().with_name("chatgpt_agbrowse_run.py")}" '
                    f'--abandon-uncertain-run "{state_file.parent}" --explicit-user-request '
                    f'--reason "confirm explicit user stop"'
                ),
                **evidence,
            }
        if observation["same_process"]:
            return {"ok": False, "state": "ACTIVE_PROJECT_OWNER", "changed": False, **evidence}

        duplicate_proof = self._duplicate_completed_owner_proof(state_file, record)
        if duplicate_proof is not None:
            duplicate_evidence = {
                **evidence,
                "authoritative_run_id": duplicate_proof["authoritative_run_id"],
                "conversation_url": duplicate_proof["conversation_url"],
            }
            if not apply_safe_pre_submission:
                return {
                    "ok": True,
                    "state": "STALE_DUPLICATE_COMPLETE_OWNER_SAFE_TO_SETTLE",
                    "changed": False,
                    **duplicate_evidence,
                }
            settled = self._settle_duplicate_completed_owner(
                state_file,
                record,
                verified_lock_file,
                verified_lock,
                observation,
            )
            return {
                "ok": True,
                "state": "STALE_DUPLICATE_COMPLETE_OWNER_SETTLED",
                "changed": True,
                **duplicate_evidence,
                "phase": settled.get("phase"),
            }

        if str(record.get("phase") or "") in {"COMPLETE", "COMPLETE_SUPERSEDED", "CANCELLED_PRE_SUBMISSION", "ABANDONED_UNCERTAIN"}:
            if not apply_safe_pre_submission:
                return {"ok": True, "state": "TERMINAL_ORPHAN_LOCK_DETECTED", "changed": False, **evidence}
            verified_lock_file.unlink()
            return {"ok": True, "state": "TERMINAL_ORPHAN_LOCK_REMOVED", "changed": True, **evidence}

        if self._safe_stale_pre_submission(state_file, record):
            if not apply_safe_pre_submission:
                return {"ok": True, "state": "STALE_PRE_SUBMISSION_SAFE_TO_CANCEL", "changed": False, **evidence}
            cancelled = self._cancel_stale_pre_submission(
                state_file,
                record,
                verified_lock_file,
                verified_lock,
                observation,
            )
            return {
                "ok": True,
                "state": "STALE_PRE_SUBMISSION_CANCELLED",
                "changed": True,
                **evidence,
                "phase": cancelled.get("phase"),
            }

        if self._crossed_send_boundary(record):
            return {
                "ok": False,
                "state": "STALE_OWNER_UNRESOLVED_SUBMISSION",
                "changed": False,
                "reason": "dead owner is after or at the send boundary; recover only the original exact run",
                "supported_abandon_command": (
                    f'python "{Path(__file__).resolve().with_name("chatgpt_agbrowse_run.py")}" '
                    f'--abandon-uncertain-run "{state_file.parent}" --explicit-user-request '
                    f'--reason "explicitly abandon stale uncertain run"'
                ),
                **evidence,
            }
        if record.get("current_target_id"):
            return {
                "ok": False,
                "state": "STALE_OWNER_PRE_SUBMIT_TARGET_PRESENT",
                "changed": False,
                "reason": "an owned pre-submit target requires exact target cleanup evidence before release",
                **evidence,
            }
        return {
            "ok": False,
            "state": "PROJECT_LOCK_STATE_AMBIGUOUS",
            "changed": False,
            "reason": "stale owner could not be classified safely",
            **evidence,
        }

    def _parent_children(self, runs_dir: Path, parent_run_id: str) -> list[tuple[Path, dict[str, Any]]]:
        children: list[tuple[Path, dict[str, Any]]] = []
        if not runs_dir.is_dir():
            return children
        for state_file in sorted(runs_dir.glob("*/run.json")):
            try:
                record = read_json(state_file)
            except StateError:
                continue
            if (
                str(record.get("record_kind") or "") == "child"
                and str(record.get("parent_run_id") or "") == parent_run_id
            ):
                children.append((state_file, record))
        return children

    def create_parent_workflow(
        self,
        *,
        project_root: str | os.PathLike[str],
        manifest_path: str | os.PathLike[str],
        workflow_id: str,
        agbrowse_contract: dict[str, Any] | None = None,
        parent_family: str = "web-multi",
        owner_pid: int | None = None,
    ) -> dict[str, Any]:
        root = canonical_project_root(project_root)
        manifest_file = Path(manifest_path).expanduser().resolve()
        if not manifest_file.is_file():
            raise StateError("MANIFEST_MISSING", f"manifest file missing: {manifest_file}")
        manifest = load_manifest(manifest_file)
        validate_parent_family_manifest(parent_family, manifest)
        if str(manifest.get("workflow_id") or "") != str(workflow_id or ""):
            raise StateError("PARENT_WORKFLOW_ID_MISMATCH", "parent workflow_id does not match its manifest")
        manifest_root = canonical_project_root(str(manifest.get("project_root") or root))
        if manifest_root != root:
            raise StateError("PARENT_PROJECT_ROOT_MISMATCH", "parent manifest project root is not exact")
        manifest_hash = sha256_file(manifest_file)
        question_hash = sha256_bytes(
            json.dumps(manifest.get("question"), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )
        run_id = uuid.uuid4().hex
        paths = self.paths(root, run_id)
        paths.project_dir.mkdir(parents=True, exist_ok=True)
        with exclusive_state_lock(paths.parent_transition_lock):
            existing_lock = self._read_existing_lock(paths.lock_file)
            existing_records = self._active_or_uncertain_records(paths.runs_dir)
            if existing_lock or existing_records:
                raise StateError(
                    "SAME_PROJECT_ACTIVE_OR_UNCERTAIN",
                    "same project already has an active or uncertain parent/standalone workflow",
                    {"lock": existing_lock, "records": existing_records[:10]},
                )
            identity = process_identity(owner_pid)
            owner_nonce = uuid.uuid4().hex
            lease_nonce = uuid.uuid4().hex
            epoch = int(time.time_ns())
            now = utc_now()
            owner = {**identity, "nonce": owner_nonce, "epoch": epoch}
            lock = {
                "schema": SCHEMA,
                "record_kind": "parent",
                "parent_family": parent_family,
                "run_id": run_id,
                "parent_run_id": run_id,
                "project_root": str(root),
                "project_key": project_key(root),
                "workflow_id": workflow_id,
                "manifest_sha256": manifest_hash,
                "lease_nonce": lease_nonce,
                "owner": owner,
                "phase": "PARENT_ACTIVE",
                "recovery_required": False,
                "heartbeat_at": now,
            }
            try:
                fd = os.open(paths.lock_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(lock, handle, ensure_ascii=False, indent=2)
                    handle.write("\n")
            except FileExistsError as exc:
                raise StateError("SAME_PROJECT_ACTIVE_OR_UNCERTAIN", "project parent lock appeared during creation") from exc
            record = {
                "schema": SCHEMA,
                "record_kind": "parent",
                "parent_family": parent_family,
                "run_id": run_id,
                "parent_run_id": run_id,
                "workflow_id": workflow_id,
                "lease_nonce": lease_nonce,
                "project_root": str(root),
                "project_key": project_key(root),
                "manifest_path": str(manifest_file),
                "manifest_sha256": manifest_hash,
                "prompt_sha256": question_hash,
                "requested": {
                    "workflow": "web-multi-gpt" if parent_family == "web-multi" else "parallel-implementation-v1",
                    "mode": "GPT-5.6",
                    "app_policy": "required",
                },
                "agbrowse": dict(agbrowse_contract or {}),
                "parent_capabilities": (
                    ["advisory-coordinator"]
                    if parent_family == "web-multi"
                    else ["canonical-lease", "implementation-graph", "staging-authority", "finalizer"]
                ),
                "owner": owner,
                "created_at": now,
                "updated_at": now,
                "phase": "PARENT_ACTIVE",
                "phase_at": now,
                "phase_events": [
                    {"from": None, "to": "PARENT_CREATED", "at": now},
                    {"from": "PARENT_CREATED", "to": "PARENT_ACTIVE", "at": now},
                ],
                "children": [],
                "result": None,
                "failure": None,
                "recovery_required": False,
                "runtime_recovery_failure": None,
                "owned_open_tabs": 0,
            }
            try:
                write_json_atomic(paths.state_file, record)
            except Exception:
                try:
                    paths.lock_file.unlink()
                except OSError:
                    pass
                raise
        return {**record, "run_dir": str(paths.run_dir), "state_file": str(paths.state_file)}

    def create_child_run(
        self,
        *,
        parent_run_dir: str | os.PathLike[str],
        manifest_path: str | os.PathLike[str],
        agbrowse_contract: dict[str, Any],
        role: str,
        lane: int,
        iteration: int,
        stage_id: str,
        send_limit: int = 1,
        owner_pid: int | None = None,
        unit_workspace_root: str | os.PathLike[str] | None = None,
        app_scope_root: str | os.PathLike[str] | None = None,
        component_id: str | None = None,
        unit_id: str | None = None,
        attempt_id: str | None = None,
        input_base_oid: str | None = None,
        topology_receipt_sha256: str | None = None,
    ) -> dict[str, Any]:
        parent_state_file, initial_parent = self.load(parent_run_dir)
        if str(initial_parent.get("record_kind") or "") != "parent":
            raise StateError("PARENT_RECORD_REQUIRED", "child creation requires an exact parent run")
        parent_family = classify_parent_family(initial_parent)
        if parent_family is None:
            raise StateError("PARENT_FAMILY_INVALID", "child creation requires a registered parent family")
        root = canonical_project_root(initial_parent["project_root"])
        parent_paths = self.paths(root, str(initial_parent["run_id"]))
        manifest_file = Path(manifest_path).expanduser().resolve()
        if not manifest_file.is_file():
            raise StateError("MANIFEST_MISSING", f"child manifest file missing: {manifest_file}")
        manifest = load_manifest(manifest_file)
        prompt = prompt_contract(manifest, require_file=True)
        if int(send_limit) != 1:
            raise StateError("CHILD_SEND_LIMIT_INVALID", "parent-owned children require send_limit=1")
        if str(manifest.get("app_policy") or "") != "required":
            raise StateError("CHILD_APP_POLICY_INVALID", "parent-owned child app_policy must be required")
        manifest_root = canonical_project_root(str(manifest.get("project_root") or root))
        if parent_family == "web-multi":
            if manifest_root != root:
                raise StateError("CHILD_PROJECT_ROOT_MISMATCH", "Web Multi child manifest project root is not exact")
        else:
            if not all((unit_workspace_root, app_scope_root, component_id, unit_id, attempt_id, input_base_oid, topology_receipt_sha256)):
                raise StateError("PARALLEL_CHILD_AUTHORITY_INCOMPLETE", "parallel implementation child authority is incomplete")
            exact_unit_root = canonical_project_root(str(unit_workspace_root))
            exact_app_root = canonical_project_root(str(app_scope_root))
            if manifest_root != exact_unit_root or exact_app_root != exact_unit_root:
                raise StateError("PARALLEL_CHILD_SCOPE_MISMATCH", "parallel child manifest and app scope must equal the unit workspace root")
            if not re.fullmatch(r"[0-9a-f]{40,64}", str(input_base_oid)):
                raise StateError("PARALLEL_CHILD_INPUT_BASE_INVALID", "parallel child input_base_oid is invalid")
            if not re.fullmatch(r"[0-9a-f]{64}", str(topology_receipt_sha256)):
                raise StateError("PARALLEL_CHILD_TOPOLOGY_RECEIPT_INVALID", "parallel child topology receipt hash is invalid")
        correlation = manifest.get("workflow_correlation") if isinstance(manifest.get("workflow_correlation"), dict) else {}
        if str(correlation.get("workflow_id") or manifest.get("workflow_id") or "") != str(initial_parent.get("workflow_id") or ""):
            raise StateError("CHILD_WORKFLOW_ID_MISMATCH", "child workflow binding does not match the parent")

        with exclusive_state_lock(parent_paths.parent_transition_lock):
            parent = read_json(parent_state_file)
            if not parent_paths.lock_file.is_file():
                raise StateError("PARENT_NOT_ACTIVE", "parent project lock is absent; child creation is forbidden")
            lock = read_json(parent_paths.lock_file)
            if (
                str(parent.get("phase") or "") != "PARENT_ACTIVE"
                or str(lock.get("phase") or "") != "PARENT_ACTIVE"
                or bool(parent.get("recovery_required"))
                or bool(lock.get("recovery_required"))
                or str(lock.get("parent_run_id") or lock.get("run_id") or "") != str(parent.get("run_id") or "")
                or str(lock.get("lease_nonce") or "") != str(parent.get("lease_nonce") or "")
                or str(lock.get("workflow_id") or "") != str(parent.get("workflow_id") or "")
                or str(lock.get("manifest_sha256") or "") != str(parent.get("manifest_sha256") or "")
            ):
                raise StateError(
                    "PARENT_NOT_ACTIVE",
                    "child creation is forbidden unless the exact parent is active and recovery-free",
                    {
                        "parent_phase": parent.get("phase"),
                        "lock_phase": lock.get("phase"),
                        "recovery_required": bool(parent.get("recovery_required") or lock.get("recovery_required")),
                    },
                )
            existing_stages = {
                str(child.get("stage_id") or "")
                for _, child in self._parent_children(parent_paths.runs_dir, str(parent["run_id"]))
            }
            if not stage_id or stage_id in existing_stages:
                raise StateError("CHILD_STAGE_DUPLICATE", "stage_id must be nonempty and unique within the parent")

            run_id = uuid.uuid4().hex
            child_paths = self.paths(root, run_id)
            identity = process_identity(owner_pid)
            now = utc_now()
            manifest_hash = sha256_file(manifest_file)
            prompt_hash = str(prompt["prompt_sha256"])
            alias_name = recovery_prompt_alias_name(run_id, manifest)
            alias_path = child_paths.run_dir / alias_name
            owner = {**identity, "nonce": uuid.uuid4().hex, "epoch": int(time.time_ns())}
            record = {
                "schema": SCHEMA,
                "record_kind": "child",
                "parent_family": parent_family,
                "run_id": run_id,
                "parent_run_id": str(parent["run_id"]),
                "parent_workflow_id": str(parent["workflow_id"]),
                "parent_lease_nonce": str(parent["lease_nonce"]),
                "role": str(role),
                "lane": int(lane),
                "iteration": int(iteration),
                "stage_id": str(stage_id),
                "send_limit": 1,
                "send_attempt_count": 0,
                "send_claim": None,
                "project_root": str(root),
                "canonical_project_root": str(root),
                "unit_workspace_root": str(unit_workspace_root) if parent_family == "parallel-implementation" else None,
                "app_scope_root": str(app_scope_root) if parent_family == "parallel-implementation" else None,
                "component_id": str(component_id) if parent_family == "parallel-implementation" else None,
                "unit_id": str(unit_id) if parent_family == "parallel-implementation" else None,
                "attempt_id": str(attempt_id) if parent_family == "parallel-implementation" else None,
                "input_base_oid": str(input_base_oid) if parent_family == "parallel-implementation" else None,
                "topology_receipt_sha256": str(topology_receipt_sha256) if parent_family == "parallel-implementation" else None,
                "project_key": project_key(root),
                "manifest_path": str(manifest_file),
                "manifest_sha256": manifest_hash,
                "prompt_sha256": prompt_hash,
                "recovery_identity": {
                    "schema": "codex.chatgpt.recovery-identity/v1",
                    "token": run_id,
                    "attachment_name": alias_name,
                    "attachment_path": str(alias_path),
                    "attachment_sha256": prompt_hash,
                    "source_prompt_path": str(prompt["prompt_file"]),
                },
                "requested": _requested_contract(manifest),
                "agbrowse": dict(agbrowse_contract),
                "owner": owner,
                "created_at": now,
                "updated_at": now,
                "phase": "CREATED",
                "phase_at": now,
                "session_id": None,
                "current_target_id": None,
                "conversation_url": None,
                "submission_receipt": None,
                "result": None,
                "terminal_block_code": None,
                "recovery_count": 0,
                "phase_events": [{"from": None, "to": "CREATED", "at": now}],
                "target_rebind_events": [],
                "recovery_events": [],
                "app_evidence_refs": [],
                "selection_evidence_refs": [],
                "cleanup_pending": False,
                "owned_tab_state": None,
                "owned_open_tabs": 0,
                "pre_submit_retry_count": 0,
                "pre_submit_retry_authority": None,
            }
            staging = parent_paths.runs_dir / f".child-{run_id}.tmp"
            try:
                staging.mkdir(parents=True, exist_ok=False)
                prompt_bytes = Path(str(prompt["prompt_file"])).read_bytes()
                if sha256_bytes(prompt_bytes) != prompt_hash:
                    raise StateError("CHILD_PROMPT_HASH_MISMATCH", "child prompt changed before durable creation")
                (staging / alias_name).write_bytes(prompt_bytes)
                write_json_atomic(staging / "run.json", record)
                os.replace(staging, child_paths.run_dir)
            except Exception:
                if staging.exists():
                    shutil.rmtree(staging, ignore_errors=True)
                raise

            children = list(parent.get("children") or [])
            children.append({"run_id": run_id, "stage_id": stage_id, "role": role, "lane": int(lane), "iteration": int(iteration)})
            parent["children"] = children
            parent["updated_at"] = utc_now()
            write_json_atomic(parent_state_file, parent)
        return {**record, "run_dir": str(child_paths.run_dir), "state_file": str(child_paths.state_file)}

    def assert_child_send_available(self, run_dir: str | os.PathLike[str]) -> dict[str, Any]:
        state_file, record = self.load(run_dir)
        if str(record.get("record_kind") or "standalone") != "child":
            return record
        _, lock = self._verify_lock(state_file, record)
        if str(lock.get("phase") or "") == "USER_STOP_REQUESTED" or bool(lock.get("user_stop_requests")):
            raise StateError("PARENT_USER_STOP_REQUESTED", "child send is forbidden after immutable parent stop intent")
        claim_file = state_file.parent / "send.claim"
        authority = record.get("pre_submit_retry_authority")
        retired_retry = False
        if claim_file.exists() and isinstance(authority, dict):
            events = record.get("recovery_events") if isinstance(record.get("recovery_events"), list) else []
            latest = events[-1] if events and isinstance(events[-1], dict) else {}
            target_id = str(record.get("current_target_id") or "")
            cleanup = record.get("cleanup_evidence") if isinstance(record.get("cleanup_evidence"), dict) else {}
            evidence = cleanup.get("evidence") if isinstance(cleanup.get("evidence"), dict) else {}
            activation = next((event for event in reversed(events[:-1]) if isinstance(event, dict) and str(event.get("kind") or "") == "app-composer-target-activation-failed"), {})
            try:
                cleanup_path = Path(str(evidence.get("path") or "")).expanduser().resolve(strict=True)
                cleanup_path.relative_to(state_file.parent)
                cleanup_valid = cleanup_path.is_file() and not cleanup_path.is_symlink() and sha256_file(cleanup_path) == str(evidence.get("sha256") or "")
                retired_evidence = Path(str(authority.get("retired_replacement_evidence_path") or "")).expanduser().resolve(strict=True)
                retired_evidence.relative_to(state_file.parent)
                retired_evidence_valid = retired_evidence.is_file() and not retired_evidence.is_symlink() and sha256_file(retired_evidence) == str(authority.get("retired_replacement_evidence_sha256") or "")
            except (OSError, RuntimeError, ValueError):
                cleanup_valid = retired_evidence_valid = False
            retired_retry = bool(
                str(record.get("phase") or "") in {"PREFLIGHT_BLOCKED", "LEASED"}
                and int(record.get("send_attempt_count") or 0) == 1
                and not record.get("session_id") and not record.get("conversation_url")
                and record.get("submission_receipt") is None and record.get("result") is None
                and authority.get("eligible") is True and authority.get("consumed_at") is None
                and not authority.get("replacement_target_id")
                and str(authority.get("retired_replacement_target_id") or "") == target_id
                and str(authority.get("run_id") or "") == str(record.get("run_id") or "")
                and str(authority.get("parent_run_id") or "") == str(record.get("parent_run_id") or "")
                and retired_evidence_valid
                and str(latest.get("kind") or "") == "stale-pre-submit-retry-replacement-retired"
                and str(latest.get("target_id") or "") == target_id
                and str(latest.get("send_claim_sha256") or "") == str(authority.get("claim_sha256") or "")
                and str(latest.get("cleanup_lifecycle_sha256") or "") == str(evidence.get("sha256") or "")
                and cleanup.get("ok") is True and str(cleanup.get("target_id") or "") == target_id
                and cleanup_valid
                and isinstance(activation.get("cleanup"), dict)
                and str(activation["cleanup"].get("target_id") or "") == target_id
                and activation["cleanup"].get("ok") is True
            )
            if retired_retry:
                candidate = self.pre_submit_retry_candidate(run_dir)
                retired_retry = str(authority.get("claim_sha256") or "") == str(candidate.get("claim_sha256") or "")
        if claim_file.exists() and isinstance(authority, dict):
            candidate = self.pre_submit_retry_candidate(run_dir)
            if (
                authority.get("eligible") is True
                and authority.get("consumed_at") is None
                and str(authority.get("run_id") or "") == str(record.get("run_id") or "")
                and str(authority.get("parent_run_id") or "") == str(record.get("parent_run_id") or "")
                and str(authority.get("claim_sha256") or "") == str(candidate["claim_sha256"])
                and str(authority.get("send_stderr_sha256") or "") == str(candidate["send_stderr_sha256"])
                and str(authority.get("send_stdout_sha256") or "") == str(candidate["send_stdout_sha256"])
                and str(authority.get("replacement_target_id") or authority.get("cleanup_target_id") or "")
                == str(record.get("current_target_id") or "")
                and (
                    not authority.get("replacement_target_id")
                    or (
                        bool(authority.get("replacement_evidence_sha256"))
                        and Path(str(authority.get("replacement_evidence_path") or "")).is_file()
                        and sha256_file(Path(str(authority.get("replacement_evidence_path"))))
                        == str(authority.get("replacement_evidence_sha256"))
                    )
                )
                and str(record.get("owned_tab_state") or "") in {"closed-and-absent", "already-absent"}
                and not bool(record.get("cleanup_pending"))
                and int(record.get("owned_open_tabs") or 0) == 0
            ):
                return record
        if retired_retry:
            return record
        if claim_file.exists() or int(record.get("send_attempt_count") or 0) >= int(record.get("send_limit") or 1):
            raise StateError(
                "SEND_ALREADY_ATTEMPTED",
                "parent-owned child permits exactly one authoritative send",
                {"run_id": record.get("run_id"), "phase": record.get("phase")},
            )
        return record

    def pre_submit_retry_candidate(self, run_dir: str | os.PathLike[str]) -> dict[str, Any]:
        """Prove that the existing child claim stopped before provider mutation.

        This is deliberately read-only.  It does not authorize another dispatch;
        authorization additionally requires exact owned-tab cleanup under the
        same active parent lease.
        """
        state_file, record = self.load(run_dir)
        if str(record.get("record_kind") or "") != "child":
            raise StateError("CHILD_RECORD_REQUIRED", "pre-submit retry proof requires a child run")
        phase = str(record.get("phase") or "")
        existing_authority = record.get("pre_submit_retry_authority")
        authorized_pre_send = bool(
            phase in {"PREFLIGHTED", "LEASED", "PREFLIGHT_BLOCKED"}
            and isinstance(existing_authority, dict)
            and existing_authority.get("eligible") is True
            and existing_authority.get("consumed_at") is None
        )
        if phase != "SEND_REJECTED" and not authorized_pre_send:
            raise StateError("PRE_SUBMIT_RETRY_PHASE_INVALID", "pre-submit retry proof requires SEND_REJECTED or an authorized pre-send phase")
        if any(
            record.get(key) is not None
            for key in ("session_id", "conversation_url", "submission_receipt", "result")
        ):
            raise StateError("PRE_SUBMIT_RETRY_IDENTITY_CONFLICT", "submission identity or result already exists")
        if int(record.get("send_attempt_count") or 0) != 1 or int(record.get("send_limit") or 1) != 1:
            raise StateError("PRE_SUBMIT_RETRY_COUNT_INVALID", "retry candidate must preserve one authoritative send claim")

        claim_file = state_file.parent / "send.claim"
        if not claim_file.is_file() or claim_file.is_symlink():
            raise StateError("CHILD_SEND_CLAIM_MISSING", "retry candidate is missing its immutable send claim")
        if self._send_claim_proof(state_file, record) is None:
            raise StateError("CHILD_SEND_CLAIM_INVALID", "retry candidate send claim identity is not exact")

        evidence_dir = state_file.parent / "agbrowse-evidence"
        stdout_path = evidence_dir / "send.stdout.txt"
        stderr_path = evidence_dir / "send.stderr.txt"
        if not stdout_path.is_file() or stdout_path.is_symlink() or not stderr_path.is_file() or stderr_path.is_symlink():
            raise StateError("PRE_SUBMIT_RETRY_EVIDENCE_MISSING", "send stdout/stderr evidence is incomplete")
        stdout_text = stdout_path.read_text(encoding="utf-8")
        if stdout_text.strip():
            raise StateError("PRE_SUBMIT_RETRY_STDOUT_CONFLICT", "nonempty send stdout conflicts with a pre-submit rejection")
        try:
            payload = json.loads(stderr_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise StateError("PRE_SUBMIT_RETRY_STDERR_INVALID", "send stderr is not one exact JSON failure envelope") from exc
        error = payload.get("error") if isinstance(payload, dict) and isinstance(payload.get("error"), dict) else {}
        error_code = str(error.get("errorCode") or error.get("error_code") or "")
        error_stage = str(error.get("stage") or "")
        error_evidence = error.get("evidence") if isinstance(error.get("evidence"), dict) else {}
        if payload.get("ok") is not False or error.get("mutationAllowed") is not False or not error_code or not error_stage:
            raise StateError("PRE_SUBMIT_RETRY_MUTATION_UNPROVEN", "failure envelope does not prove mutationAllowed=false")
        conflicting_keys = {
            "sessionId", "session_id", "conversationUrl", "conversation_url", "answer", "result"
        }
        if any(payload.get(key) not in (None, "", [], {}) for key in conflicting_keys):
            raise StateError("PRE_SUBMIT_RETRY_IDENTITY_CONFLICT", "failure envelope contains submission identity or result")
        def is_matching_pre_submit_event(event: object) -> bool:
            if not isinstance(event, dict):
                return False
            kind = str(event.get("kind") or "")
            if kind == "verified-mutation-disallowed-reclassification":
                return (
                    str(event.get("error_code") or "") == error_code
                    and str(event.get("error_stage") or "") == error_stage
                )
            if kind == "pre-submit-rejection":
                recorded = event.get("error") if isinstance(event.get("error"), dict) else {}
                return (
                    str(recorded.get("error_code") or "") == error_code
                    and str(recorded.get("error_stage") or "") == error_stage
                    and recorded.get("mutation_allowed") is False
                )
            return False

        matching_events = [event for event in record.get("recovery_events") or [] if is_matching_pre_submit_event(event)]
        if not matching_events:
            raise StateError("PRE_SUBMIT_RETRY_RECLASSIFICATION_MISSING", "verified reclassification does not match send evidence")
        return {
            "schema": "codex.chatgpt.pre-submit-retry-candidate/v1",
            "run_id": record["run_id"],
            "parent_run_id": record["parent_run_id"],
            "stage_id": record["stage_id"],
            "target_id": record.get("current_target_id"),
            "claim_path": str(claim_file),
            "claim_sha256": sha256_file(claim_file),
            "send_stdout_path": str(stdout_path),
            "send_stdout_sha256": sha256_file(stdout_path),
            "send_stderr_path": str(stderr_path),
            "send_stderr_sha256": sha256_file(stderr_path),
            "error_code": error_code,
            "error_stage": error_stage,
            "capacity_reason": str(error_evidence.get("reason") or ""),
            "capacity_current": int(error_evidence.get("current") or 0),
            "capacity_limit": int(error_evidence.get("limit") or 0),
        }

    def authorize_child_pre_submit_retry(
        self,
        run_dir: str | os.PathLike[str],
        cleanup: dict[str, Any],
    ) -> dict[str, Any]:
        state_file, record = self.load(run_dir)
        self._verify_lock(state_file, record)
        candidate = self.pre_submit_retry_candidate(run_dir)
        target_id = str(record.get("current_target_id") or "")
        if (
            cleanup.get("ok") is not True
            or str(cleanup.get("state") or "") not in {"closed-and-absent", "already-absent"}
            or str(cleanup.get("target_id") or "") != target_id
        ):
            raise StateError("PRE_SUBMIT_RETRY_CLEANUP_UNPROVEN", "exact owned pre-submit target cleanup is required")
        lifecycle = cleanup.get("evidence") if isinstance(cleanup.get("evidence"), dict) else {}
        lifecycle_path = Path(str(lifecycle.get("path") or ""))
        try:
            lifecycle_path = lifecycle_path.expanduser().resolve(strict=True)
            lifecycle_path.relative_to(state_file.parent)
        except (OSError, RuntimeError, ValueError) as exc:
            raise StateError("PRE_SUBMIT_RETRY_CLEANUP_UNPROVEN", "cleanup lifecycle evidence path is invalid") from exc
        if (
            not lifecycle_path.is_file()
            or lifecycle_path.is_symlink()
            or sha256_file(lifecycle_path) != str(lifecycle.get("sha256") or "")
        ):
            raise StateError("PRE_SUBMIT_RETRY_CLEANUP_UNPROVEN", "cleanup lifecycle evidence hash is invalid")
        now = utc_now()
        prior = record.get("pre_submit_retry_authority") if isinstance(record.get("pre_submit_retry_authority"), dict) else {}
        authority = {
            **candidate,
            "schema": "codex.chatgpt.pre-submit-retry-authority/v1",
            "eligible": True,
            "authorized_at": now,
            "consumed_at": None,
            "retry_sequence": int(prior.get("retry_sequence") or 0) + 1,
            "cleanup_target_id": target_id,
            "cleanup_state": cleanup["state"],
            "cleanup_lifecycle_path": str(lifecycle_path),
            "cleanup_lifecycle_sha256": sha256_file(lifecycle_path),
        }
        record["pre_submit_retry_authority"] = authority
        record["cleanup_pending"] = False
        record["owned_tab_state"] = str(cleanup["state"])
        record["owned_open_tabs"] = 0
        record["cleanup_evidence"] = cleanup
        record["updated_at"] = now
        write_json_atomic(state_file, record)
        return record

    def reconcile_stale_child_pre_submit_retry_target(
        self,
        run_dir: str | os.PathLike[str],
        cleanup: dict[str, Any],
    ) -> dict[str, Any]:
        """Quarantine one absent retry composer before resuming its exact child.

        This is deliberately narrower than general pre-submit cleanup: the
        child must already carry an unconsumed, exact mutation-disallowed retry
        authority, and the parent must be in its own active recovery window.
        It never creates a target or a send claim.
        """
        state_file, record = self.load(run_dir)
        if str(record.get("record_kind") or "") != "child":
            raise StateError("CHILD_RECORD_REQUIRED", "stale pre-submit reconciliation requires a child run")
        if str(record.get("phase") or "") != "LEASED":
            raise StateError("STALE_PRE_SUBMIT_PHASE_INVALID", "stale pre-submit reconciliation requires LEASED")
        _, lock = self._verify_lock(state_file, record)
        parent_file = state_file.parent.parent / str(record.get("parent_run_id") or "") / "run.json"
        try:
            parent = read_json(parent_file)
        except (OSError, StateError) as exc:
            raise StateError("STALE_PRE_SUBMIT_PARENT_INVALID", "exact active parent record is required") from exc
        if not (
            str(parent.get("phase") or "") == "PARENT_ACTIVE"
            and parent.get("recovery_required") is True
            and str(lock.get("phase") or "") == "PARENT_ACTIVE"
            and lock.get("recovery_required") is True
            and str(parent.get("run_id") or "") == str(record.get("parent_run_id") or "")
            and str(lock.get("run_id") or "") == str(record.get("parent_run_id") or "")
        ):
            raise StateError("STALE_PRE_SUBMIT_PARENT_NOT_RECOVERING", "parent is not in the exact active recovery window")
        candidate = self.pre_submit_retry_candidate(run_dir)
        authority = record.get("pre_submit_retry_authority")
        target_id = str(record.get("current_target_id") or "")
        if not (
            isinstance(authority, dict)
            and authority.get("eligible") is True
            and authority.get("consumed_at") is None
            and target_id
            and str(authority.get("replacement_target_id") or "") == target_id
            and str(authority.get("claim_sha256") or "") == str(candidate.get("claim_sha256") or "")
        ):
            raise StateError("STALE_PRE_SUBMIT_AUTHORITY_INVALID", "current target is not the exact unconsumed retry replacement")
        lifecycle = cleanup.get("evidence") if isinstance(cleanup.get("evidence"), dict) else {}
        lifecycle_path = Path(str(lifecycle.get("path") or ""))
        try:
            lifecycle_path = lifecycle_path.expanduser().resolve(strict=True)
            lifecycle_path.relative_to(state_file.parent)
        except (OSError, RuntimeError, ValueError) as exc:
            raise StateError("STALE_PRE_SUBMIT_ABSENCE_UNPROVEN", "target absence lifecycle path is invalid") from exc
        if not (
            cleanup.get("ok") is True
            and str(cleanup.get("state") or "") in {"closed-and-absent", "already-absent"}
            and str(cleanup.get("target_id") or "") == target_id
            and lifecycle_path.is_file()
            and not lifecycle_path.is_symlink()
            and sha256_file(lifecycle_path) == str(lifecycle.get("sha256") or "")
        ):
            raise StateError("STALE_PRE_SUBMIT_ABSENCE_UNPROVEN", "exact replacement target absence is required")
        self.record_child_cleanup(run_dir, cleanup)
        return self.transition(
            run_dir,
            "PREFLIGHT_BLOCKED",
            block_code="STALE_PRE_SUBMIT_RETRY_TARGET_ABSENT",
            recovery_event={
                "kind": "stale-pre-submit-retry-target-reconciled",
                "target_id": target_id,
                "cleanup_state": str(cleanup.get("state") or ""),
                "cleanup_lifecycle_sha256": sha256_file(lifecycle_path),
                "send_claim_sha256": str(candidate.get("claim_sha256") or ""),
            },
        )

    def retire_absent_child_pre_submit_retry_replacement(
        self, run_dir: str | os.PathLike[str]
    ) -> dict[str, Any]:
        """Retire only a replacement target proven absent after activation.

        The immutable pre-submit claim remains usable once.  This does not
        create a tab or submit anything; it removes the already-absent
        replacement binding so the normal composer path may bind one fresh
        replacement under the same authority.
        """
        state_file, record = self.load(run_dir)
        if str(record.get("record_kind") or "") != "child" or str(record.get("phase") or "") != "PREFLIGHT_BLOCKED":
            raise StateError("STALE_REPLACEMENT_PHASE_INVALID", "absent retry replacement requires PREFLIGHT_BLOCKED child")
        _, lock = self._verify_lock(state_file, record)
        parent_file = state_file.parent.parent / str(record.get("parent_run_id") or "") / "run.json"
        parent = read_json(parent_file)
        if not (
            str(parent.get("phase") or "") == "PARENT_ACTIVE" and parent.get("recovery_required") is True
            and str(lock.get("phase") or "") == "PARENT_ACTIVE" and lock.get("recovery_required") is True
        ):
            raise StateError("STALE_REPLACEMENT_PARENT_NOT_RECOVERING", "parent is not in exact active recovery")
        candidate = self.pre_submit_retry_candidate(run_dir)
        authority = record.get("pre_submit_retry_authority")
        target_id = str(record.get("current_target_id") or "")
        events = record.get("recovery_events") if isinstance(record.get("recovery_events"), list) else []
        latest = events[-1] if events and isinstance(events[-1], dict) else {}
        prior_reconciled = any(
            isinstance(event, dict)
            and str(event.get("kind") or "") == "stale-pre-submit-retry-target-reconciled"
            and str(event.get("target_id") or "") == target_id
            and str(event.get("send_claim_sha256") or "") == str(candidate.get("claim_sha256") or "")
            for event in events[:-1]
        )
        cleanup = latest.get("cleanup") if isinstance(latest.get("cleanup"), dict) else {}
        evidence = cleanup.get("evidence") if isinstance(cleanup.get("evidence"), dict) else {}
        lifecycle_path = Path(str(evidence.get("path") or ""))
        try:
            lifecycle_path = lifecycle_path.expanduser().resolve(strict=True)
            lifecycle_path.relative_to(state_file.parent)
            absence_valid = lifecycle_path.is_file() and not lifecycle_path.is_symlink() and sha256_file(lifecycle_path) == str(evidence.get("sha256") or "")
        except (OSError, RuntimeError, ValueError):
            absence_valid = False
        if not (
            isinstance(authority, dict) and authority.get("eligible") is True and authority.get("consumed_at") is None
            and str(authority.get("replacement_target_id") or "") == target_id
            and str(authority.get("claim_sha256") or "") == str(candidate.get("claim_sha256") or "")
            and str(latest.get("kind") or "") == "app-composer-target-activation-failed"
            and cleanup.get("ok") is True
            and str(cleanup.get("state") or "") in {"closed-and-absent", "already-absent"}
            and str(cleanup.get("target_id") or "") == target_id
            and absence_valid and prior_reconciled
        ):
            raise StateError("STALE_REPLACEMENT_ABSENCE_UNPROVEN", "replacement retirement requires exact absent activation failure")
        authority = dict(authority)
        authority.update({
            "retired_replacement_target_id": target_id,
            "retired_replacement_evidence_path": authority.get("replacement_evidence_path"),
            "retired_replacement_evidence_sha256": authority.get("replacement_evidence_sha256"),
            "retired_at": utc_now(),
            "retirement_cleanup_lifecycle_sha256": sha256_file(lifecycle_path),
        })
        for key in ("replacement_target_id", "replacement_bound_at", "replacement_evidence_path", "replacement_evidence_sha256"):
            authority.pop(key, None)
        record["pre_submit_retry_authority"] = authority
        write_json_atomic(state_file, record)
        # The retirement event is bound to the activation failure's fresh
        # absence proof, not to an older reconciliation cleanup record.
        self.record_child_cleanup(run_dir, cleanup)
        return self.transition(
            run_dir, "PREFLIGHT_BLOCKED", block_code="STALE_PRE_SUBMIT_RETRY_REPLACEMENT_RETIRED",
            recovery_event={
                "kind": "stale-pre-submit-retry-replacement-retired", "target_id": target_id,
                "send_claim_sha256": str(candidate.get("claim_sha256") or ""),
                "cleanup_lifecycle_sha256": sha256_file(lifecycle_path),
            },
        )

    def backfill_retired_child_pre_submit_cleanup(self, run_dir: str | os.PathLike[str]) -> dict[str, Any]:
        """Repair only the legacy cleanup pointer omitted by older retirement code."""
        state_file, record = self.load(run_dir)
        if str(record.get("record_kind") or "") != "child" or str(record.get("phase") or "") != "PREFLIGHT_BLOCKED":
            raise StateError("RETIRED_CLEANUP_BACKFILL_PHASE_INVALID", "legacy cleanup backfill requires PREFLIGHT_BLOCKED child")
        _, lock = self._verify_lock(state_file, record)
        parent = read_json(state_file.parent.parent / str(record.get("parent_run_id") or "") / "run.json")
        if not (str(parent.get("phase") or "") == "PARENT_ACTIVE" and parent.get("recovery_required") is True and lock.get("recovery_required") is True):
            raise StateError("RETIRED_CLEANUP_BACKFILL_PARENT_INVALID", "parent must be exact active recovery")
        candidate = self.pre_submit_retry_candidate(run_dir)
        authority = record.get("pre_submit_retry_authority")
        events = record.get("recovery_events") if isinstance(record.get("recovery_events"), list) else []
        latest = events[-1] if events and isinstance(events[-1], dict) else {}
        target_id = str(record.get("current_target_id") or "")
        activation = next((event for event in reversed(events[:-1]) if isinstance(event, dict) and str(event.get("kind") or "") == "app-composer-target-activation-failed"), {})
        cleanup = activation.get("cleanup") if isinstance(activation.get("cleanup"), dict) else {}
        evidence = cleanup.get("evidence") if isinstance(cleanup.get("evidence"), dict) else {}
        current_cleanup = record.get("cleanup_evidence") if isinstance(record.get("cleanup_evidence"), dict) else {}
        current_cleanup_hash = str((current_cleanup.get("evidence") or {}).get("sha256") or "")
        try:
            path = Path(str(evidence.get("path") or "")).expanduser().resolve(strict=True)
            path.relative_to(state_file.parent)
            evidence_valid = path.is_file() and not path.is_symlink() and sha256_file(path) == str(evidence.get("sha256") or "")
        except (OSError, RuntimeError, ValueError):
            evidence_valid = False
        if not (
            isinstance(authority, dict) and authority.get("eligible") is True and authority.get("consumed_at") is None
            and not authority.get("replacement_target_id")
            and str(authority.get("retired_replacement_target_id") or "") == target_id
            and str(authority.get("claim_sha256") or "") == str(candidate.get("claim_sha256") or "")
            and str(latest.get("kind") or "") == "stale-pre-submit-retry-replacement-retired"
            and str(latest.get("target_id") or "") == target_id
            and str(latest.get("send_claim_sha256") or "") == str(candidate.get("claim_sha256") or "")
            and str(latest.get("cleanup_lifecycle_sha256") or "") == str(evidence.get("sha256") or "")
            and cleanup.get("ok") is True and str(cleanup.get("state") or "") in {"closed-and-absent", "already-absent"}
            and str(cleanup.get("target_id") or "") == target_id and evidence_valid
            and not record.get("session_id") and not record.get("conversation_url")
            and record.get("submission_receipt") is None and record.get("result") is None
            and str(current_cleanup.get("target_id") or "") == target_id
        ):
            raise StateError("RETIRED_CLEANUP_BACKFILL_UNPROVEN", "legacy retired cleanup evidence is not exact")
        if current_cleanup_hash == str(evidence.get("sha256") or ""):
            return record
        return self.record_child_cleanup(run_dir, cleanup)

    def confirm_child_retry_replacement(
        self,
        run_dir: str | os.PathLike[str],
        *,
        target_id: str,
        evidence_path: str | os.PathLike[str],
    ) -> dict[str, Any]:
        state_file, record = self.load(run_dir)
        self._verify_lock(state_file, record)
        authority = record.get("pre_submit_retry_authority")
        if not isinstance(authority, dict) or authority.get("eligible") is not True or authority.get("consumed_at") is not None:
            raise StateError("PRE_SUBMIT_RETRY_AUTHORITY_MISSING", "replacement target requires unconsumed retry authority")
        cleanup_target = str(
            authority.get("retired_replacement_target_id")
            or authority.get("cleanup_target_id")
            or ""
        )
        target_id = str(target_id or "")
        if not cleanup_target or not target_id or target_id == cleanup_target or target_id != str(record.get("current_target_id") or ""):
            raise StateError("PRE_SUBMIT_RETRY_REPLACEMENT_INVALID", "replacement target identity is not exact")
        matching_rebind = any(
            isinstance(event, dict)
            and str(event.get("old_target_id") or "") == cleanup_target
            and str(event.get("new_target_id") or "") == target_id
            and str(event.get("reason") or "") == "pre-submit-composer-retry"
            for event in record.get("target_rebind_events") or []
        )
        if not matching_rebind:
            raise StateError("PRE_SUBMIT_RETRY_REPLACEMENT_INVALID", "exact retry target rebind event is missing")
        path = Path(evidence_path).expanduser().resolve(strict=True)
        try:
            path.relative_to(state_file.parent)
        except ValueError as exc:
            raise StateError("PRE_SUBMIT_RETRY_REPLACEMENT_EVIDENCE_INVALID", "replacement evidence escaped the run directory") from exc
        evidence = read_json(path)
        is_app_selection = str(evidence.get("state") or "") == "composer-app-mention-tab-confirmed"
        is_research_selection = str(evidence.get("state") or "") == "deep-research-selected"
        if path.is_symlink() or not (is_app_selection or is_research_selection):
            raise StateError("PRE_SUBMIT_RETRY_REPLACEMENT_EVIDENCE_INVALID", "replacement composer evidence is not an approved capability selection")
        if is_app_selection:
            if (
                str(evidence.get("target_id") or "") != target_id
                or str(evidence.get("selection_method") or "") != "exact-at-mention-then-tab"
            ):
                raise StateError("PRE_SUBMIT_RETRY_REPLACEMENT_EVIDENCE_INVALID", "replacement app composer evidence is not exact")
        else:
            # A generic visible pill is not authority after a process restart.  The
            # persisted transition proof, all selection hashes, and the immutable
            # selection-evidence reference must bind this exact child/workflow/tab.
            self.verify_manifest(record)
            manifest = load_manifest(Path(str(record["manifest_path"])))
            correlation = manifest.get("workflow_correlation") if isinstance(manifest.get("workflow_correlation"), dict) else {}
            workflow_id = str(
                correlation.get("workflow_id")
                or record.get("parent_workflow_id")
                or record.get("workflow_id")
                or record.get("run_id")
                or ""
            )
            proof = evidence.get("selection_proof") if isinstance(evidence.get("selection_proof"), dict) else {}
            required_hashes = ("token_sha256", "before_snapshot_sha256", "after_snapshot_sha256", "action_transcript_sha256")
            expected_token_hash = sha256_bytes("@심층 리서치".encode("utf-8"))
            exact_hashes = all(re.fullmatch(r"[0-9a-f]{64}", str(evidence.get(key) or "")) for key in required_hashes)
            proof_hashes_match = (
                str(proof.get("token_sha256") or "") == expected_token_hash
                and str(proof.get("before_snapshot_sha256") or "") == str(evidence.get("before_snapshot_sha256") or "")
                and str(proof.get("after_snapshot_sha256") or "") == str(evidence.get("after_snapshot_sha256") or "")
                and str(proof.get("action_transcript_sha256") or "") == str(evidence.get("action_transcript_sha256") or "")
                and bool(re.fullmatch(r"[0-9a-f]{64}", str(proof.get("marker_identity_sha256") or "")))
            )
            evidence_hash = sha256_file(path)
            immutable_ref = any(
                isinstance(ref, dict)
                and str(ref.get("kind") or "") == "deep-research-selection"
                and str(ref.get("path") or "") == str(path)
                and str(ref.get("sha256") or "") == evidence_hash
                and str(ref.get("target_id") or "") == target_id
                for ref in record.get("selection_evidence_refs") or []
            )
            if not (
                evidence.get("schema") == "codex.chatgpt.capability-selection/v1"
                and str(evidence.get("run_id") or "") == str(record.get("run_id") or "")
                and str(evidence.get("workflow_id") or "") == workflow_id
                and str(evidence.get("target_id") or "") == target_id
                and str(evidence.get("selection_transport") or "") == "preselected-research"
                and str(evidence.get("token_sha256") or "") == expected_token_hash
                and exact_hashes
                and isinstance(evidence.get("selected_marker"), dict)
                and str(proof.get("kind") or "") == "token-to-pill-transition"
                and proof_hashes_match
                and immutable_ref
            ):
                raise StateError("PRE_SUBMIT_RETRY_REPLACEMENT_EVIDENCE_INVALID", "replacement Deep Research evidence does not bind exact immutable capability authority")
        authority = dict(authority)
        existing_replacement = str(authority.get("replacement_target_id") or "")
        if existing_replacement and existing_replacement != target_id:
            raise StateError("PRE_SUBMIT_RETRY_REPLACEMENT_IMMUTABLE", "replacement target cannot change")
        authority.update(
            {
                "replacement_target_id": target_id,
                "replacement_bound_at": utc_now(),
                "replacement_evidence_path": str(path),
                "replacement_evidence_sha256": sha256_file(path),
            }
        )
        record["pre_submit_retry_authority"] = authority
        record["updated_at"] = utc_now()
        write_json_atomic(state_file, record)
        return record

    def claim_child_send(
        self,
        run_dir: str | os.PathLike[str],
        *,
        authority_bindings: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        state_file, record = self.load(run_dir)
        if str(record.get("record_kind") or "") != "child":
            return self.transition(run_dir, "SEND_STARTED")
        root = canonical_project_root(str(record.get("project_root") or ""))
        paths = self.paths(root, str(record.get("parent_run_id") or ""))
        with exclusive_state_lock(paths.parent_transition_lock):
            state_file, record = self.load(run_dir)
            self.assert_child_send_available(run_dir)
            parent_file = paths.runs_dir / str(record.get("parent_run_id") or "") / "run.json"
            parent = read_json(parent_file)
            _, lock = self._verify_lock(state_file, record)
            if (
                str(parent.get("phase") or "") != "PARENT_ACTIVE"
                or str(lock.get("phase") or "") != "PARENT_ACTIVE"
                or bool(parent.get("recovery_required"))
                or bool(lock.get("recovery_required"))
                or bool(parent.get("parent_stop_scope"))
                or bool(lock.get("parent_stop_scope"))
                or bool(parent.get("user_stop_tombstone"))
                or bool(lock.get("user_stop_tombstone"))
            ):
                raise StateError("PARENT_USER_STOP_REQUESTED", "parent is not eligible for a new child send claim")
            if str(record.get("phase") or "") != "LEASED":
                raise StateError("SEND_PHASE_INVALID", "child send claim requires LEASED")
            now = utc_now()
            if str(record.get("parent_family") or "") == "parallel-implementation":
                bindings = authority_bindings if isinstance(authority_bindings, dict) else {}
                required_hashes = {
                    "topology_receipt_sha256": record.get("topology_receipt_sha256"),
                    "listener_identity_receipt_sha256": bindings.get("listener_identity_receipt_sha256"),
                    "tunnel_identity_receipt_sha256": bindings.get("tunnel_identity_receipt_sha256"),
                    "server_identity_payload_sha256": bindings.get("server_identity_payload_sha256"),
                    "app_scope_receipt_sha256": bindings.get("app_scope_receipt_sha256"),
                }
                invalid = [
                    key
                    for key, value in required_hashes.items()
                    if re.fullmatch(r"[0-9a-f]{64}", str(value or "")) is None
                ]
                if invalid:
                    raise StateError(
                        "PARALLEL_SEND_AUTHORITY_INCOMPLETE",
                        "parallel send claim lacks exact authority bindings",
                        {"invalid": invalid},
                    )
                claim = {
                    "schema": "codex.chatgpt.child-send-claim/v2",
                    "run_id": record["run_id"],
                    "parent_run_id": record["parent_run_id"],
                    "stage_id": record["stage_id"],
                    "component_id": record["component_id"],
                    "unit_id": record["unit_id"],
                    "attempt_id": record["attempt_id"],
                    "input_base_oid": record["input_base_oid"],
                    "manifest_sha256": record["manifest_sha256"],
                    "prompt_sha256": record["prompt_sha256"],
                    **required_hashes,
                    "provider_invocation_state": "CLAIMED_NOT_INVOKED",
                    "claimed_at": now,
                }
                claim["send_claim_sha256"] = sha256_bytes(
                    json.dumps(claim, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
                )
            else:
                claim = {
                    "schema": "codex.chatgpt.child-send-claim/v1",
                    "run_id": record["run_id"],
                    "parent_run_id": record["parent_run_id"],
                    "parent_workflow_id": record["parent_workflow_id"],
                    "parent_lease_nonce": record["parent_lease_nonce"],
                    "project_root": record["project_root"],
                    "project_key": record["project_key"],
                    "stage_id": record["stage_id"],
                    "role": record["role"],
                    "lane": record["lane"],
                    "iteration": record["iteration"],
                    "manifest_sha256": record["manifest_sha256"],
                    "prompt_sha256": record["prompt_sha256"],
                    "send_limit": record["send_limit"],
                    "claimed_at": now,
                }
            claim_file = state_file.parent / "send.claim"
            if claim_file.exists():
                existing = read_json(claim_file)
                if str(record.get("parent_family") or "") == "parallel-implementation":
                    fixed_keys = (
                        "schema", "run_id", "parent_run_id", "stage_id", "component_id", "unit_id", "attempt_id",
                        "input_base_oid", "manifest_sha256", "prompt_sha256", "topology_receipt_sha256",
                        "listener_identity_receipt_sha256", "tunnel_identity_receipt_sha256",
                        "server_identity_payload_sha256", "app_scope_receipt_sha256",
                    )
                    mismatches = {
                        key: {"existing": existing.get(key), "expected": claim.get(key)}
                        for key in fixed_keys
                        if existing.get(key) != claim.get(key)
                    }
                    digest_payload = {key: value for key, value in existing.items() if key != "send_claim_sha256"}
                    expected_digest = sha256_bytes(
                        json.dumps(digest_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
                    )
                    if mismatches or str(existing.get("send_claim_sha256") or "") != expected_digest:
                        raise StateError(
                            "SEND_CLAIM_CONFLICT",
                            "existing parallel send claim does not match the exact authority binding",
                            {"mismatches": mismatches},
                        )
                return self.transition(run_dir, "SEND_STARTED")
            try:
                fd = os.open(claim_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(claim, handle, ensure_ascii=False, indent=2)
                    handle.write("\n")
            except FileExistsError as exc:
                raise StateError("SEND_ALREADY_ATTEMPTED", "child send claim appeared during creation") from exc
            return self.transition(run_dir, "SEND_STARTED")

    def record_parallel_send_disposition(
        self,
        run_dir: str | os.PathLike[str],
        invocation_state: str,
        *,
        recovery_evidence: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        state_file, record = self.load(run_dir)
        if str(record.get("record_kind") or "") != "child" or str(record.get("parent_family") or "") != "parallel-implementation":
            raise StateError("PARALLEL_CHILD_REQUIRED", "send disposition requires a parallel implementation child")
        self._verify_lock(state_file, record)
        claim_file = state_file.parent / "send.claim"
        if not claim_file.is_file() or claim_file.is_symlink():
            raise StateError("CHILD_SEND_CLAIM_MISSING", "parallel send disposition requires its immutable claim")
        claim = read_json(claim_file)
        claim_sha256 = sha256_file(claim_file)
        if claim.get("schema") != "codex.chatgpt.child-send-claim/v2":
            raise StateError("CHILD_SEND_CLAIM_INVALID", "parallel send claim schema is not exact")
        allowed_states = {
            "CLAIMED_NOT_INVOKED",
            "ZERO_MUTATION_PROVEN",
            "INVOKED_MUTATION_UNKNOWN",
            "INVOKED_MUTATION_CONFIRMED",
            "RECOVERED_EXACT_SESSION",
        }
        if invocation_state not in allowed_states:
            raise StateError("SEND_DISPOSITION_INVALID", "parallel send invocation state is invalid")
        disposition_file = state_file.parent / "send.disposition.json"
        previous = read_json(disposition_file) if disposition_file.is_file() else None
        previous_state = str((previous or {}).get("invocation_state") or "CLAIMED_NOT_INVOKED")
        allowed_transitions = {
            "CLAIMED_NOT_INVOKED": allowed_states,
            "ZERO_MUTATION_PROVEN": {"ZERO_MUTATION_PROVEN", "INVOKED_MUTATION_UNKNOWN", "INVOKED_MUTATION_CONFIRMED", "RECOVERED_EXACT_SESSION"},
            "INVOKED_MUTATION_UNKNOWN": {"INVOKED_MUTATION_UNKNOWN", "INVOKED_MUTATION_CONFIRMED", "RECOVERED_EXACT_SESSION"},
            "INVOKED_MUTATION_CONFIRMED": {"INVOKED_MUTATION_CONFIRMED", "RECOVERED_EXACT_SESSION"},
            "RECOVERED_EXACT_SESSION": {"RECOVERED_EXACT_SESSION"},
        }
        if invocation_state not in allowed_transitions.get(previous_state, set()):
            raise StateError(
                "SEND_DISPOSITION_ROLLBACK_FORBIDDEN",
                "parallel send disposition cannot move back to a resubmittable state",
                {"from": previous_state, "to": invocation_state},
            )
        evidence_descriptor = None
        if invocation_state != "CLAIMED_NOT_INVOKED":
            evidence = recovery_evidence if isinstance(recovery_evidence, dict) else {}
            try:
                evidence_path = Path(str(evidence.get("path") or "")).expanduser().resolve(strict=True)
                evidence_path.relative_to(state_file.parent)
            except (OSError, RuntimeError, ValueError) as exc:
                raise StateError("SEND_DISPOSITION_EVIDENCE_INVALID", "send disposition evidence must be inside the child run") from exc
            evidence_sha256 = str(evidence.get("sha256") or "")
            if not evidence_path.is_file() or evidence_path.is_symlink() or sha256_file(evidence_path) != evidence_sha256:
                raise StateError("SEND_DISPOSITION_EVIDENCE_INVALID", "send disposition evidence is missing or hash-mismatched")
            evidence_descriptor = {"path": str(evidence_path), "sha256": evidence_sha256}
        disposition = {
            "schema": "codex.chatgpt.child-send-disposition/v1",
            "run_id": record["run_id"],
            "parent_run_id": record["parent_run_id"],
            "unit_id": record["unit_id"],
            "attempt_id": record["attempt_id"],
            "send_claim_sha256": claim_sha256,
            "invocation_state": invocation_state,
            "recovery_evidence": evidence_descriptor,
            "updated_at": utc_now(),
        }
        write_json_atomic(disposition_file, disposition)
        record["send_disposition"] = disposition
        record["updated_at"] = disposition["updated_at"]
        if invocation_state in {"INVOKED_MUTATION_UNKNOWN", "INVOKED_MUTATION_CONFIRMED"}:
            record["recovery_required"] = True
        write_json_atomic(state_file, record)
        return record

    def adopt_legacy_send_claim_for_user_stop(
        self, run_dir: str | os.PathLike[str]
    ) -> dict[str, Any]:
        """Bind an exact historical seven-field claim without rewriting it."""
        state_file, initial = self.load(run_dir)
        if str(initial.get("record_kind") or "") != "child":
            raise StateError("CHILD_RECORD_REQUIRED", "legacy send-claim adoption requires a child")
        root = canonical_project_root(str(initial.get("project_root") or ""))
        paths = self.paths(root, str(initial.get("parent_run_id") or ""))
        with exclusive_state_lock(paths.parent_transition_lock):
            state_file, record = self.load(run_dir)
            parent_file = (
                state_file.parent.parent
                / str(record.get("parent_run_id") or "")
                / "run.json"
            )
            parent = read_json(parent_file)
            lock_file, lock = self._verify_lock(state_file, record)
            if (
                str(parent.get("phase") or "") != "USER_STOP_REQUESTED"
                or str(lock.get("phase") or "") != "USER_STOP_REQUESTED"
            ):
                raise StateError(
                    "PARENT_USER_STOP_CONFIRMATION_REQUIRED",
                    "legacy claim adoption is limited to an exact parent stop drain",
                )
            exact_children = [
                (child_file, child)
                for child_file, child in self._strict_parent_children(paths, parent)
                if str(child.get("run_id") or "") == str(record.get("run_id") or "")
            ]
            if len(exact_children) != 1 or exact_children[0][0] != state_file:
                raise StateError(
                    "LEGACY_SEND_CLAIM_CHILD_AMBIGUOUS",
                    "parent must contain exactly one exact child identity",
                )
            claim_file = state_file.parent / "send.claim"
            try:
                if not claim_file.is_file() or claim_file.is_symlink():
                    raise OSError("legacy claim is not a regular file")
                raw_claim = claim_file.read_bytes()
                claim = read_json(claim_file)
            except (OSError, StateError) as exc:
                raise StateError(
                    "LEGACY_SEND_CLAIM_INVALID",
                    "legacy claim is unavailable or malformed",
                ) from exc
            legacy_keys = {
                "schema",
                "run_id",
                "parent_run_id",
                "stage_id",
                "manifest_sha256",
                "prompt_sha256",
                "claimed_at",
            }
            source = (
                record.get("send_claim")
                if isinstance(record.get("send_claim"), dict)
                else {}
            )
            claim_sha256 = sha256_bytes(raw_claim)
            if (
                set(claim) != legacy_keys
                or claim.get("schema") != "codex.chatgpt.child-send-claim/v1"
                or any(
                    claim.get(key) != record.get(key)
                    for key in (
                        "run_id",
                        "parent_run_id",
                        "stage_id",
                        "manifest_sha256",
                        "prompt_sha256",
                    )
                )
                or not str(claim.get("claimed_at") or "")
                or Path(str(source.get("path") or "")) != claim_file
                or str(source.get("sha256") or "") != claim_sha256
                or str(source.get("claimed_at") or "") != str(claim["claimed_at"])
            ):
                raise StateError(
                    "LEGACY_SEND_CLAIM_INVALID",
                    "only the exact historical seven-field claim shape may be adopted",
                )
            child_identity = {
                "run_id": record.get("run_id"),
                "parent_run_id": record.get("parent_run_id"),
                "parent_workflow_id": record.get("parent_workflow_id"),
                "parent_lease_nonce": record.get("parent_lease_nonce"),
                "project_root": record.get("project_root"),
                "project_key": record.get("project_key"),
                "stage_id": record.get("stage_id"),
                "role": record.get("role"),
                "lane": record.get("lane"),
                "iteration": record.get("iteration"),
                "manifest_sha256": record.get("manifest_sha256"),
                "prompt_sha256": record.get("prompt_sha256"),
                "send_limit": record.get("send_limit"),
            }
            parent_identity = {
                "run_id": parent.get("run_id"),
                "workflow_id": parent.get("workflow_id"),
                "lease_nonce": parent.get("lease_nonce"),
                "project_root": parent.get("project_root"),
                "project_key": parent.get("project_key"),
                "manifest_sha256": parent.get("manifest_sha256"),
            }
            entry = next(
                item
                for item in parent["children"]
                if isinstance(item, dict)
                and str(item.get("run_id") or "") == str(record.get("run_id") or "")
            )
            child_entry = {
                key: entry.get(key)
                for key in ("run_id", "stage_id", "role", "lane", "iteration")
            }
            if (
                child_identity["parent_run_id"] != parent_identity["run_id"]
                or child_identity["parent_workflow_id"] != parent_identity["workflow_id"]
                or child_identity["parent_lease_nonce"] != parent_identity["lease_nonce"]
                or child_identity["project_root"] != parent_identity["project_root"]
                or child_identity["project_key"] != parent_identity["project_key"]
                or any(
                    child_entry.get(key) != child_identity.get(key)
                    for key in ("run_id", "stage_id", "role", "lane", "iteration")
                )
            ):
                raise StateError(
                    "LEGACY_SEND_CLAIM_IDENTITY_MISMATCH",
                    "authoritative parent and child identities differ",
                )
            phase = str(record.get("phase") or "")
            if phase == "SEND_REJECTED":
                zero_provider = self._send_rejected_failure_evidence_proof(
                    state_file, record
                )
                if zero_provider is None:
                    raise StateError(
                        "SEND_REJECTED_ZERO_PROVIDER_UNPROVEN",
                        "legacy rejection lacks immutable zero-provider evidence",
                    )
                authority = {
                    "source_phase": "SEND_REJECTED",
                    "zero_provider_evidence": zero_provider,
                }
            elif phase in {"USER_STOP_REQUESTED", "ABANDONED_UNCERTAIN"}:
                stop = (
                    record.get("user_stop")
                    if isinstance(record.get("user_stop"), dict)
                    else {}
                )
                binding = (
                    stop.get("legacy_binding")
                    if isinstance(stop.get("legacy_binding"), dict)
                    else {}
                )
                binding_path = Path(str(binding.get("path") or ""))
                if (
                    not binding_path.is_file()
                    or binding_path.is_symlink()
                    or sha256_file(binding_path) != str(binding.get("sha256") or "")
                    or not all(
                        record.get(key)
                        for key in ("session_id", "current_target_id", "conversation_url")
                    )
                ):
                    raise StateError(
                        "LEGACY_USER_STOP_BINDING_INVALID",
                        "stopped legacy child lacks its immutable exact binding",
                    )
                authority = {
                    "source_phase": "USER_STOP_REQUESTED",
                    "legacy_stop_binding": binding,
                    "session_id": record.get("session_id"),
                    "target_id": record.get("current_target_id"),
                    "conversation_url": record.get("conversation_url"),
                }
            else:
                raise StateError(
                    "LEGACY_SEND_CLAIM_PHASE_INVALID",
                    "legacy claim adoption is limited to stopped or zero-provider children",
                )
            adoption_path = (
                state_file.parent / "user-stop" / "legacy-send-claim-adoption.json"
            )
            if adoption_path.exists():
                if not adoption_path.is_file() or adoption_path.is_symlink():
                    raise StateError(
                        "LEGACY_SEND_CLAIM_ADOPTION_AMBIGUOUS",
                        "existing adoption path is not an immutable regular file",
                    )
                descriptor = {
                    "path": str(adoption_path),
                    "sha256": sha256_file(adoption_path),
                    "bytes": adoption_path.stat().st_size,
                }
            else:
                payload = {
                    "schema": "codex.chatgpt.legacy-send-claim-adoption/v1",
                    "claim": {
                        "path": str(claim_file),
                        "sha256": claim_sha256,
                        "bytes": len(raw_claim),
                        "bytes_base64": base64.b64encode(raw_claim).decode("ascii"),
                        "parsed": claim,
                    },
                    "child_identity": child_identity,
                    "parent_identity": parent_identity,
                    "parent_child_entry": child_entry,
                    "authority": authority,
                    "preimages": {
                        "child_state_sha256": sha256_file(state_file),
                        "parent_state_sha256": sha256_file(parent_file),
                        "parent_lock_sha256": sha256_file(lock_file),
                    },
                    "adopted_at": utc_now(),
                }
                descriptor = write_immutable_json_exclusive(adoption_path, payload)
            candidate = dict(record)
            candidate["legacy_send_claim_adoption"] = descriptor
            if self._legacy_send_claim_adoption_proof(state_file, candidate) is None:
                raise StateError(
                    "LEGACY_SEND_CLAIM_ADOPTION_AMBIGUOUS",
                    "existing adoption does not bind the exact current authority",
                )
            record["legacy_send_claim_adoption"] = descriptor
            record["updated_at"] = utc_now()
            write_json_atomic(state_file, record)
            return record

    def user_stop_send_rejected_candidate(
        self, run_dir: str | os.PathLike[str]
    ) -> dict[str, Any]:
        state_file, record = self.load(run_dir)
        if str(record.get("record_kind") or "") != "child":
            raise StateError("CHILD_RECORD_REQUIRED", "zero-provider settlement requires a child")
        _, lock = self._verify_lock(state_file, record)
        parent_file = state_file.parent.parent / str(record.get("parent_run_id") or "") / "run.json"
        parent = read_json(parent_file)
        if (
            str(parent.get("phase") or "") != "USER_STOP_REQUESTED"
            or str(lock.get("phase") or "") != "USER_STOP_REQUESTED"
        ):
            raise StateError("PARENT_USER_STOP_CONFIRMATION_REQUIRED", "parent stop drain is not active")
        proof = self._send_rejected_zero_provider_proof(state_file, record)
        if proof is None:
            raise StateError(
                "SEND_REJECTED_ZERO_PROVIDER_UNPROVEN",
                "SEND_REJECTED child lacks immutable zero-provider evidence",
            )
        return proof

    def parent_stop_submission_uncertain_candidate(
        self, run_dir: str | os.PathLike[str]
    ) -> dict[str, Any]:
        """Classify a hash-mismatched SEND_REJECTED child without inferring send outcome."""
        state_file, record = self.load(run_dir)
        if (
            str(record.get("record_kind") or "") != "child"
            or str(record.get("phase") or "") != "SEND_REJECTED"
            or int(record.get("send_attempt_count") or 0) != 1
            or int(record.get("send_limit") or 0) != 1
            or record.get("session_id") is not None
            or record.get("conversation_url") is not None
            or record.get("submission_receipt") is not None
            or record.get("result") is not None
        ):
            raise StateError(
                "PARENT_STOP_UNCERTAIN_CHILD_INVALID",
                "submission-uncertain settlement requires the unchanged rejected identity",
            )
        root = canonical_project_root(record["project_root"])
        paths = self.paths(root, str(record.get("parent_run_id") or ""))
        parent_file = paths.runs_dir / str(record.get("parent_run_id") or "") / "run.json"
        parent = read_json(parent_file)
        lock = read_json(paths.lock_file)
        children = self._strict_parent_children(paths, parent)
        if len([child for child_file, child in children if child_file == state_file]) != 1:
            raise StateError("PARENT_STOP_UNCERTAIN_CHILD_AMBIGUOUS", "child topology is not exact")
        scope_ref = parent.get("parent_stop_scope") if isinstance(parent.get("parent_stop_scope"), dict) else {}
        if scope_ref != lock.get("parent_stop_scope"):
            raise StateError("PARENT_WIDE_STOP_SCOPE_MISMATCH", "parent and lock scope references differ")
        scope_path = Path(str(scope_ref.get("path") or ""))
        if (
            str(parent.get("phase") or "") != "USER_STOP_REQUESTED"
            or str(lock.get("phase") or "") != "USER_STOP_REQUESTED"
            or not scope_path.is_file()
            or scope_path.is_symlink()
            or sha256_file(scope_path) != str(scope_ref.get("sha256") or "")
            or scope_path.stat().st_size != int(scope_ref.get("bytes") or -1)
        ):
            raise StateError("PARENT_WIDE_STOP_SCOPE_INVALID", "immutable parent stop scope is unavailable")
        scope = read_json(scope_path)
        ordered_entries = [
            {key: entry.get(key) for key in ("run_id", "stage_id", "role", "lane", "iteration")}
            for entry in parent.get("children") or []
        ]
        ordered_digest = sha256_bytes(
            json.dumps(ordered_entries, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )
        if (
            scope.get("schema") != "codex.chatgpt.parent-wide-user-stop/v1"
            or scope.get("explicit_user_request") is not True
            or scope.get("ordered_children") != ordered_entries
            or scope.get("child_count") != len(ordered_entries)
            or scope.get("child_list_sha256") != ordered_digest
            or str(scope.get("stop_epoch_nonce") or "") != str(scope_ref.get("stop_epoch_nonce") or "")
        ):
            raise StateError("PARENT_WIDE_STOP_SCOPE_INVALID", "scope does not bind the live strict child set")
        claim_file = state_file.parent / "send.claim"
        claim_snapshot = immutable_file_snapshot(claim_file)
        try:
            claim = json.loads(base64.b64decode(claim_snapshot["bytes_base64"]).decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise StateError("LEGACY_SEND_CLAIM_INVALID", "claim bytes are not exact JSON") from exc
        legacy_keys = {"schema", "run_id", "parent_run_id", "stage_id", "manifest_sha256", "prompt_sha256", "claimed_at"}
        send_claim = record.get("send_claim") if isinstance(record.get("send_claim"), dict) else {}
        if (
            set(claim) != legacy_keys
            or claim.get("schema") != "codex.chatgpt.child-send-claim/v1"
            or any(claim.get(key) != record.get(key) for key in ("run_id", "parent_run_id", "stage_id", "manifest_sha256", "prompt_sha256"))
            or not str(claim.get("claimed_at") or "")
            or Path(str(send_claim.get("path") or "")) != claim_file
            or str(send_claim.get("sha256") or "") != claim_snapshot["sha256"]
            or str(send_claim.get("claimed_at") or "") != str(claim["claimed_at"])
        ):
            raise StateError("LEGACY_SEND_CLAIM_INVALID", "legacy claim bytes or descriptor differ")
        indexed = [
            (index, event)
            for index, event in enumerate(record.get("recovery_events") or [])
            if isinstance(event, dict) and str(event.get("kind") or "") == "pre-submit-rejection"
        ]
        if len(indexed) != 1:
            raise StateError("SEND_REJECTION_EVENT_AMBIGUOUS", "exactly one historical rejection event is required")
        event_index, event = indexed[0]
        evidence = event.get("evidence") if isinstance(event.get("evidence"), dict) else {}
        stderr_path = Path(str(evidence.get("stderr") or ""))
        stdout_path = Path(str(evidence.get("stdout") or ""))
        stderr_snapshot = immutable_file_snapshot(stderr_path)
        stdout_snapshot = immutable_file_snapshot(stdout_path)
        recorded_stderr = str(evidence.get("stderr_sha256") or "")
        recorded_stdout = str(evidence.get("stdout_sha256") or "")
        if (
            stderr_path.resolve(strict=True) != (state_file.parent / "agbrowse-evidence" / "send.stderr.txt").resolve(strict=True)
            or stdout_path.resolve(strict=True) != (state_file.parent / "agbrowse-evidence" / "send.stdout.txt").resolve(strict=True)
            or not re.fullmatch(r"[0-9a-f]{64}", recorded_stderr)
            or not re.fullmatch(r"[0-9a-f]{64}", recorded_stdout)
            or stderr_snapshot["sha256"] == recorded_stderr
        ):
            raise StateError(
                "SEND_REJECTION_EVIDENCE_NOT_MISMATCHED",
                "this path requires a preserved recorded-versus-actual stderr mismatch",
            )
        child_entry = next(
            {key: entry.get(key) for key in ("run_id", "stage_id", "role", "lane", "iteration")}
            for entry in parent["children"]
            if str(entry.get("run_id") or "") == str(record.get("run_id") or "")
        )
        return {
            "schema": "codex.chatgpt.parent-stop-submission-uncertain-candidate/v1",
            "decision": "abandon-under-parent-wide-user-stop",
            "source_phase": "SEND_REJECTED",
            "submission_outcome": "unknown",
            "provider_mutation_may_have_occurred": True,
            "zero_provider_asserted": False,
            "pre_submit_asserted": False,
            "retry_authorized": False,
            "send_authorized": False,
            "recovery_authorized": False,
            "result_capture_authorized": False,
            "result_promotion_authorized": False,
            "parent_stop_scope": scope_ref,
            "parent_identity": {
                "run_id": parent.get("run_id"), "workflow_id": parent.get("workflow_id"),
                "lease_nonce": parent.get("lease_nonce"), "project_root": parent.get("project_root"),
                "project_key": parent.get("project_key"), "manifest_sha256": parent.get("manifest_sha256"),
            },
            "parent_child_entry": child_entry,
            "child_identity": {
                "run_id": record.get("run_id"), "parent_run_id": record.get("parent_run_id"),
                "workflow_id": record.get("parent_workflow_id"), "lease_nonce": record.get("parent_lease_nonce"),
                "project_root": record.get("project_root"), "project_key": record.get("project_key"),
                "manifest_sha256": record.get("manifest_sha256"), "prompt_sha256": record.get("prompt_sha256"),
                "stage_id": record.get("stage_id"), "role": record.get("role"), "lane": record.get("lane"),
                "iteration": record.get("iteration"), "send_attempt_count": 1, "send_limit": 1,
                "session_id": None, "target_id": record.get("current_target_id"), "conversation_url": None,
                "submission_receipt": None, "result": None,
            },
            "claim": {**claim_snapshot, "parsed": claim, "claimed_at": claim["claimed_at"], "record_descriptor": send_claim},
            "source_state": immutable_file_snapshot(state_file),
            "rejection_event": {
                "index": event_index,
                "json_pointer": f"/recovery_events/{event_index}/evidence/stderr_sha256",
                "canonical_sha256": sha256_bytes(json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")),
            },
            "stderr_discrepancy": {
                "recorded_path": str(evidence.get("stderr") or ""), "recorded_sha256": recorded_stderr,
                "actual": stderr_snapshot, "hashes_match": False, "evidence_integrity": "mismatch",
                "historical_payload_semantics_authoritative": False,
            },
            "stdout_discrepancy": {
                "recorded_path": str(evidence.get("stdout") or ""), "recorded_sha256": recorded_stdout,
                "actual": stdout_snapshot, "hashes_match": stdout_snapshot["sha256"] == recorded_stdout,
            },
            "topology": {"child_count": len(children), "ordered_child_list_sha256": ordered_digest},
            "preimages": {
                "parent": immutable_file_snapshot(parent_file),
                "lock": immutable_file_snapshot(paths.lock_file),
            },
        }

    def pending_retired_retry_rebind_candidate(self, run_dir: str | os.PathLike[str]) -> bool:
        """Read-only proof for a fresh replacement awaiting existing confirmation."""
        state_file, record = self.load(run_dir)
        try:
            candidate = self.pre_submit_retry_candidate(run_dir)
        except StateError:
            return False
        authority = record.get("pre_submit_retry_authority")
        target = str(record.get("current_target_id") or "")
        evidence_path = state_file.parent / "composer-app-evidence.json"
        try:
            evidence = read_json(evidence_path)
            evidence_valid = (
                evidence_path.is_file() and not evidence_path.is_symlink()
                and str(evidence.get("state") or "") == "composer-app-mention-tab-confirmed"
                and str(evidence.get("target_id") or "") == target
                and evidence.get("new_target_proven") is True
            )
        except (OSError, StateError):
            evidence_valid = False
        rebind = any(
            isinstance(event, dict)
            and str(event.get("old_target_id") or "") == str(authority.get("retired_replacement_target_id") or "")
            and str(event.get("new_target_id") or "") == target
            and str(event.get("reason") or "") == "pre-submit-composer-retry"
            for event in record.get("target_rebind_events") or []
        ) if isinstance(authority, dict) else False
        return bool(
            str(record.get("phase") or "") == "LEASED"
            and (state_file.parent / "send.claim").is_file()
            and int(record.get("send_attempt_count") or 0) == 1
            and not record.get("session_id") and not record.get("conversation_url")
            and record.get("submission_receipt") is None and record.get("result") is None
            and isinstance(authority, dict) and authority.get("eligible") is True and authority.get("consumed_at") is None
            and not authority.get("replacement_target_id") and bool(authority.get("retired_replacement_target_id"))
            and str(authority.get("claim_sha256") or "") == str(candidate.get("claim_sha256") or "")
            and rebind and evidence_valid
            and str(record.get("owned_tab_state") or "") in {"closed-and-absent", "already-absent"}
            and not bool(record.get("cleanup_pending")) and int(record.get("owned_open_tabs") or 0) == 0
        )

    def settle_parent_stopped_submission_uncertain_child(
        self,
        run_dir: str | os.PathLike[str],
        *,
        preclose: dict[str, Any],
        settlement: dict[str, Any],
    ) -> dict[str, Any]:
        """Dedicated guarded SEND_REJECTED -> ABANDONED_UNCERTAIN transition."""
        state_file, initial = self.load(run_dir)
        paths = self.paths(canonical_project_root(initial["project_root"]), str(initial["parent_run_id"]))
        with exclusive_state_lock(paths.parent_transition_lock):
            state_file, record = self.load(run_dir)
            parent = read_json(paths.runs_dir / str(record["parent_run_id"]) / "run.json")
            lock = read_json(paths.lock_file)
            if (
                str(record.get("phase") or "") != "SEND_REJECTED"
                or str(parent.get("phase") or "") != "USER_STOP_REQUESTED"
                or str(lock.get("phase") or "") != "USER_STOP_REQUESTED"
                or parent.get("parent_stop_scope") != lock.get("parent_stop_scope")
            ):
                raise StateError("PARENT_STOP_UNCERTAIN_FINALIZE_INVALID", "stop barrier or source phase changed")
            def exact_descriptor(value: dict[str, Any], schema: str) -> dict[str, Any]:
                path = Path(str(value.get("path") or ""))
                if (
                    not path.is_file() or path.is_symlink()
                    or sha256_file(path) != str(value.get("sha256") or "")
                    or path.stat().st_size != int(value.get("bytes") or -1)
                ):
                    raise StateError("PARENT_STOP_UNCERTAIN_DESCRIPTOR_INVALID", "immutable descriptor changed")
                payload = read_json(path)
                if payload.get("schema") != schema:
                    raise StateError("PARENT_STOP_UNCERTAIN_DESCRIPTOR_INVALID", "descriptor schema differs")
                return payload
            before = exact_descriptor(preclose, "codex.chatgpt.parent-stop-submission-uncertain-preclose/v1")
            after = exact_descriptor(settlement, "codex.chatgpt.parent-stop-submission-uncertain-settlement/v1")
            cleanup = after.get("cleanup") if isinstance(after.get("cleanup"), dict) else {}
            cleanup_proof = cleanup.get("durable_cleanup_proof") if isinstance(cleanup.get("durable_cleanup_proof"), dict) else {}
            cleanup_proof_payload = exact_descriptor(
                cleanup_proof,
                "codex.chatgpt.parent-stop-submission-uncertain-cleanup-proof/v1",
            )
            cleanup_core = dict(cleanup); cleanup_core.pop("durable_cleanup_proof", None)
            if (
                after.get("preclose") != preclose
                or before.get("child_identity", {}).get("run_id") != record.get("run_id")
                or after.get("child_identity", {}).get("run_id") != record.get("run_id")
                or after.get("recorded_session_id") is not None
                or after.get("recorded_conversation_url") is not None
                or after.get("zero_provider_asserted") is not False
                or after.get("provider_mutation_may_have_occurred") is not True
                or after.get("result_promoted") is not False
                or cleanup.get("state") not in {"closed-and-absent", "already-absent"}
                or cleanup.get("target_id") != record.get("current_target_id")
                or cleanup.get("target_absent_after") is not True
                or cleanup_proof_payload.get("preclose") != preclose
                or cleanup_proof_payload.get("cleanup") != cleanup_core
                or record.get("session_id") is not None
                or record.get("conversation_url") is not None
                or record.get("submission_receipt") is not None
                or record.get("result") is not None
            ):
                raise StateError("PARENT_STOP_UNCERTAIN_SETTLEMENT_INVALID", "settlement changes historical uncertainty")
            record["parent_stop_submission_uncertain"] = {
                "scope": parent.get("parent_stop_scope"), "preclose": preclose, "settlement": settlement,
            }
            record.pop("zero_provider_settlement", None)
            record["cleanup_pending"] = False
            record["owned_open_tabs"] = 0
            record["owned_tab_state"] = cleanup["state"]
            record["cleanup_evidence"] = cleanup
            record.setdefault("phase_events", []).append({"from": "SEND_REJECTED", "to": "ABANDONED_UNCERTAIN", "at": utc_now(), "authority": "parent-stop-submission-uncertain"})
            record["phase"] = "ABANDONED_UNCERTAIN"
            record["phase_at"] = utc_now()
            record["updated_at"] = utc_now()
            record["terminal_block_code"] = None
            write_json_atomic(state_file, record)
            return record

    def attach_parent_stop_submission_uncertain_preclose(
        self, run_dir: str | os.PathLike[str], *, preclose: dict[str, Any]
    ) -> dict[str, Any]:
        state_file, initial = self.load(run_dir)
        paths = self.paths(canonical_project_root(initial["project_root"]), str(initial["parent_run_id"]))
        with exclusive_state_lock(paths.parent_transition_lock):
            state_file, record = self.load(run_dir)
            if str(record.get("phase") or "") != "SEND_REJECTED":
                raise StateError("PARENT_STOP_UNCERTAIN_PHASE_INVALID", "pre-close reference requires SEND_REJECTED")
            path = Path(str(preclose.get("path") or ""))
            if (
                not path.is_file() or path.is_symlink()
                or path.parent != state_file.parent / "user-stop"
                or sha256_file(path) != str(preclose.get("sha256") or "")
                or path.stat().st_size != int(preclose.get("bytes") or -1)
            ):
                raise StateError("PARENT_STOP_UNCERTAIN_DESCRIPTOR_INVALID", "pre-close descriptor is not exact")
            payload = read_json(path)
            if (
                payload.get("schema") != "codex.chatgpt.parent-stop-submission-uncertain-preclose/v1"
                or payload.get("decision") != "abandon-under-parent-wide-user-stop"
                or payload.get("zero_provider_asserted") is not False
                or payload.get("provider_mutation_may_have_occurred") is not True
                or payload.get("child_identity", {}).get("run_id") != record.get("run_id")
            ):
                raise StateError("PARENT_STOP_UNCERTAIN_DESCRIPTOR_INVALID", "pre-close decision is invalid")
            existing = record.get("pending_parent_stop_submission_uncertain")
            if existing not in (None, preclose):
                raise StateError("PARENT_STOP_UNCERTAIN_DESCRIPTOR_CONFLICT", "a different pre-close decision is pending")
            record["pending_parent_stop_submission_uncertain"] = preclose
            record["updated_at"] = utc_now()
            write_json_atomic(state_file, record)
            return record

    def settle_user_stop_send_rejected(
        self,
        run_dir: str | os.PathLike[str],
        *,
        cleanup: dict[str, Any],
    ) -> dict[str, Any]:
        state_file, record = self.load(run_dir)
        proof = self.user_stop_send_rejected_candidate(run_dir)
        self.record_child_cleanup(run_dir, cleanup)
        record = read_json(state_file)
        cleanup_value = (
            record.get("cleanup_evidence") if isinstance(record.get("cleanup_evidence"), dict) else {}
        )
        cleanup_proof = self._pre_submit_cleanup_proof(state_file, record, cleanup_value)
        if cleanup_proof is None:
            raise StateError(
                "SEND_REJECTED_CLEANUP_UNPROVEN",
                "SEND_REJECTED child exact target cleanup is not immutable",
            )
        payload = {
            "schema": "codex.chatgpt.zero-provider-settlement/v1",
            "run_id": record["run_id"],
            "parent_run_id": record["parent_run_id"],
            "proof": proof,
            "cleanup": cleanup_proof,
        }
        descriptor = write_immutable_json_exclusive(
            state_file.parent / "user-stop" / "zero-provider-settlement.json",
            payload,
        )
        record["zero_provider_settlement"] = descriptor
        record["updated_at"] = utc_now()
        write_json_atomic(state_file, record)
        return record

    def record_child_cleanup(self, run_dir: str | os.PathLike[str], cleanup: dict[str, Any]) -> dict[str, Any]:
        state_file, record = self.load(run_dir)
        if str(record.get("record_kind") or "") != "child":
            raise StateError("CHILD_RECORD_REQUIRED", "cleanup evidence can be attached only to a child")
        try:
            self._verify_lock(state_file, record)
        except StateError as exc:
            # A later same-project parent lease must not strand an exact,
            # already-terminal child's uniquely owned tab.  The tab lifecycle
            # performs the target+URL+immutable-result ownership check before
            # reaching this write; here additionally require the historical
            # parent binding to be terminal and exact before recording cleanup.
            parent_file = state_file.parent.parent / str(record.get("parent_run_id") or "") / "run.json"
            try:
                parent = read_json(parent_file)
            except Exception:
                parent = {}
            historical_terminal = (
                exc.code == "BLOCKED_OWNER_MISMATCH"
                and str(record.get("phase") or "") in {"COMPLETE", "PROVIDER_FAILED_TERMINAL"}
                and str(parent.get("phase") or "") in {"PARENT_COMPLETE", "PARENT_FAILED_CLOSED"}
                and str(parent.get("run_id") or "") == str(record.get("parent_run_id") or "")
                and str(parent.get("workflow_id") or "") == str(record.get("parent_workflow_id") or "")
                and str(parent.get("lease_nonce") or "") == str(record.get("parent_lease_nonce") or "")
            )
            if not historical_terminal:
                raise
        state = str(cleanup.get("state") or "")
        if cleanup.get("ok") is not True or state not in {"closed-and-absent", "already-absent"}:
            record["cleanup_pending"] = True
            record["owned_tab_state"] = state or "cleanup-pending"
            record["owned_open_tabs"] = 1 if record.get("current_target_id") else 0
        else:
            cleanup_target = str(cleanup.get("target_id") or record.get("current_target_id") or "")
            if record.get("current_target_id") and cleanup_target != str(record.get("current_target_id")):
                raise StateError("CHILD_CLEANUP_TARGET_MISMATCH", "cleanup target does not match the child target")
            cleanup_url = str(cleanup.get("conversation_url") or "")
            expected_url = str(record.get("conversation_url") or "")
            if expected_url and (not cleanup_url or cleanup_url != expected_url):
                raise StateError("CHILD_CLEANUP_URL_MISMATCH", "cleanup URL does not match the child canonical URL")
            if str(record.get("phase") or "") == "COMPLETE" and not expected_url:
                raise StateError("CHILD_CLEANUP_URL_MISMATCH", "cleanup URL does not match the child canonical URL")
            record["cleanup_pending"] = False
            record["owned_tab_state"] = state
            record["owned_open_tabs"] = 0
        record["cleanup_evidence"] = cleanup
        record["updated_at"] = utc_now()
        write_json_atomic(state_file, record)
        return record

    def record_terminal_cleanup(self, run_dir: str | os.PathLike[str], cleanup: dict[str, Any]) -> dict[str, Any]:
        state_file, record = self.load(run_dir)
        if str(record.get("record_kind") or "standalone") == "child":
            return self.record_child_cleanup(run_dir, cleanup)
        if str(record.get("phase") or "") not in {"COMPLETE", "PROVIDER_FAILED_TERMINAL"}:
            raise StateError(
                "TERMINAL_CLEANUP_PHASE_INVALID",
                "standalone cleanup evidence requires a safe terminal run",
            )
        state = str(cleanup.get("state") or "")
        target = str(cleanup.get("target_id") or record.get("current_target_id") or "")
        url = str(cleanup.get("conversation_url") or record.get("conversation_url") or "")
        if record.get("current_target_id") and target != str(record.get("current_target_id")):
            raise StateError("TERMINAL_CLEANUP_TARGET_MISMATCH", "cleanup target does not match the run target")
        if record.get("conversation_url") and url != str(record.get("conversation_url")):
            raise StateError("TERMINAL_CLEANUP_URL_MISMATCH", "cleanup URL does not match the canonical run URL")
        clean = cleanup.get("ok") is True and state in {"closed-and-absent", "already-absent"}
        record["cleanup_pending"] = not clean
        record["owned_tab_state"] = state or "cleanup-pending"
        record["owned_open_tabs"] = 0 if clean else (1 if record.get("current_target_id") else 0)
        record["cleanup_evidence"] = {
            **cleanup,
            "target_id": target,
            "conversation_url": url,
        }
        record["updated_at"] = utc_now()
        write_json_atomic(state_file, record)
        return record

    def rebind_terminal_target(
        self,
        run_dir: str | os.PathLike[str],
        candidate: dict[str, Any],
    ) -> dict[str, Any]:
        state_file, initial = self.load(run_dir)
        project_lock = state_file.parent.parent.parent / "parent-transition.lock"
        with exclusive_state_lock(project_lock):
            record = read_json(state_file)
            self.verify_manifest(record)
            if str(record.get("record_kind") or "standalone") == "parent":
                raise StateError("TERMINAL_TARGET_REBIND_RUN_INVALID", "parent records do not own conversation targets")
            if str(record.get("record_kind") or "standalone") == "child":
                self._verify_lock(state_file, record)
            phase = str(record.get("phase") or "")
            old_target_id = str(record.get("current_target_id") or "")
            new_target_id = str(candidate.get("new_target_id") or "")
            url = str(record.get("conversation_url") or "")
            evidence = candidate.get("evidence") if isinstance(candidate.get("evidence"), dict) else {}
            evidence_error: str | None = None
            try:
                evidence_path = Path(str(evidence.get("path") or "")).expanduser().resolve(strict=True)
                evidence_path.relative_to(state_file.parent)
                if (
                    not evidence_path.is_file()
                    or evidence_path.is_symlink()
                    or sha256_file(evidence_path) != str(evidence.get("sha256") or "")
                ):
                    evidence_error = "evidence-invalid"
            except (OSError, RuntimeError, ValueError):
                evidence_error = "evidence-path-invalid"
            if (
                phase not in {"COMPLETE", "PROVIDER_FAILED_TERMINAL"}
                or candidate.get("ok") is not True
                or str(candidate.get("phase") or "") != phase
                or str(candidate.get("conversation_url") or "") != url
                or str(candidate.get("old_target_id") or "") != old_target_id
                or not new_target_id
                or new_target_id == old_target_id
                or candidate.get("old_target_absent") is not True
                or int(candidate.get("url_match_count") or 0) != 1
                or candidate.get("foreign_owner_absent") is not True
                or evidence_error is not None
            ):
                raise StateError(
                    "TERMINAL_TARGET_REBIND_UNPROVEN",
                    "terminal target rebind requires one exact URL match and immutable absence/ownership evidence",
                    {"evidence_error": evidence_error},
                )
            now = utc_now()
            record["target_rebind_events"].append(
                {
                    "at": now,
                    "old_target_id": old_target_id,
                    "new_target_id": new_target_id,
                    "conversation_url": url,
                    "reason": "terminal-exact-url-after-browser-restart",
                    "evidence": evidence,
                }
            )
            record["current_target_id"] = new_target_id
            record["updated_at"] = now
            write_json_atomic(state_file, record)
            return record

    def resume_parent_workflow(
        self,
        parent_run_dir: str | os.PathLike[str],
        manifest_path: str | os.PathLike[str],
        owner_pid: int | None = None,
        *,
        reactivate: bool = False,
    ) -> dict[str, Any]:
        state_file, initial = self.load(parent_run_dir)
        if str(initial.get("record_kind") or "") != "parent":
            raise StateError("PARENT_RECORD_REQUIRED", "resume requires a parent record")
        root = canonical_project_root(initial["project_root"])
        paths = self.paths(root, str(initial["run_id"]))
        manifest_file = Path(manifest_path).expanduser().resolve()
        with exclusive_state_lock(paths.parent_transition_lock):
            record = read_json(state_file)
            original_record = json.loads(json.dumps(record))
            lock = self._read_existing_lock(paths.lock_file)
            if str(manifest_file) != str(Path(str(record["manifest_path"])).resolve()) or sha256_file(manifest_file) != record["manifest_sha256"]:
                raise StateError("BLOCKED_MANIFEST_MISMATCH", "resume manifest does not match the immutable parent")
            if str(record.get("phase") or "") == "USER_STOP_REQUESTED":
                raise StateError("PARENT_USER_STOP_CONFIRMATION_REQUIRED", "resume is forbidden while explicit user stop is pending")
            if lock is not None and str(lock.get("phase") or "") == "USER_STOP_REQUESTED":
                raise StateError("PARENT_USER_STOP_CONFIRMATION_REQUIRED", "resume is forbidden while parent lock has explicit stop intent")
            if str(record.get("phase") or "") in PARENT_TERMINAL_PHASES:
                raise StateError("PARENT_ALREADY_TERMINAL", "terminal parent cannot be resumed")
            if reactivate:
                raise StateError(
                    "PARENT_REACTIVATION_INVALID",
                    "a parent that entered draining or recovery-required cannot return to active",
                    {"phase": str(record.get("phase") or "")},
                )
            requested_pid = int(owner_pid or os.getpid())
            if same_process(record.get("owner") or {}) and requested_pid != int((record.get("owner") or {}).get("pid") or 0):
                raise StateError("ACTIVE_PROJECT_OWNER", "live parent owner cannot be replaced")
            if lock is not None and (
                str(lock.get("parent_run_id") or lock.get("run_id") or "") != str(record["run_id"])
                or str(lock.get("lease_nonce") or "") != str(record["lease_nonce"])
                or str(lock.get("workflow_id") or "") != str(record["workflow_id"])
            ):
                raise StateError("BLOCKED_OWNER_MISMATCH", "resume parent lease identity is not exact")
            if lock is None:
                foreign_records = [
                    item
                    for item in self._active_or_uncertain_records(paths.runs_dir)
                    if str(item.get("run_id") or "") != str(record["run_id"])
                ]
                if foreign_records:
                    raise StateError(
                        "SAME_PROJECT_ACTIVE_OR_UNCERTAIN",
                        "another active or uncertain project run prevents parent lock reconstruction",
                        {"records": foreign_records[:10]},
                    )
            identity = process_identity(requested_pid)
            owner = {**identity, "nonce": uuid.uuid4().hex, "epoch": int(time.time_ns())}
            record["owner"] = owner
            now = utc_now()
            recreated_lock = lock is None
            if lock is None:
                lock = {
                    "schema": SCHEMA,
                    "record_kind": "parent",
                    "run_id": record["run_id"],
                    "parent_run_id": record["run_id"],
                    "project_root": record["project_root"],
                    "project_key": record["project_key"],
                    "workflow_id": record["workflow_id"],
                    "manifest_sha256": record["manifest_sha256"],
                    "lease_nonce": record["lease_nonce"],
                    "owner": owner,
                    "phase": record["phase"],
                    "heartbeat_at": now,
                }
                try:
                    fd = os.open(paths.lock_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
                    with os.fdopen(fd, "w", encoding="utf-8") as handle:
                        json.dump(lock, handle, ensure_ascii=False, indent=2)
                        handle.write("\n")
                except FileExistsError as exc:
                    raise StateError(
                        "SAME_PROJECT_ACTIVE_OR_UNCERTAIN",
                        "project parent lock appeared during exact resume reconstruction",
                    ) from exc
                recovery_events = record.setdefault("parent_lock_recovery_events", [])
                recovery_events.append(
                    {
                        "kind": "missing-parent-lock-recreated",
                        "at": now,
                        "lease_nonce": record["lease_nonce"],
                        "owner_pid": requested_pid,
                    }
                )
                record["parent_lock_recovery_events"] = recovery_events[-20:]
            else:
                lock["owner"] = owner
            record["updated_at"] = now
            lock["heartbeat_at"] = now
            try:
                write_json_atomic(state_file, record)
                write_json_atomic(paths.lock_file, lock)
            except Exception:
                if recreated_lock:
                    try:
                        write_json_atomic(state_file, original_record)
                    except Exception as rollback_exc:
                        raise StateError(
                            "PARENT_LOCK_RECONSTRUCTION_ROLLBACK_FAILED",
                            "failed to restore the original parent record after lock reconstruction failure",
                            {"detail": str(rollback_exc)},
                        ) from rollback_exc
                    try:
                        paths.lock_file.unlink()
                    except OSError as rollback_exc:
                        raise StateError(
                            "PARENT_LOCK_RECONSTRUCTION_ROLLBACK_FAILED",
                            "restored the parent record but could not remove the reconstructed lock",
                            {"detail": str(rollback_exc)},
                        ) from rollback_exc
                raise
        return record

    def mark_parent_runtime_recovery(
        self,
        parent_run_dir: str | os.PathLike[str],
        *,
        failure: dict[str, Any],
    ) -> dict[str, Any]:
        state_file, initial = self.load(parent_run_dir)
        if str(initial.get("record_kind") or "") != "parent":
            raise StateError("PARENT_RECORD_REQUIRED", "runtime recovery requires a parent record")
        root = canonical_project_root(initial["project_root"])
        paths = self.paths(root, str(initial["run_id"]))
        with exclusive_state_lock(paths.parent_transition_lock):
            record = read_json(state_file)
            lock = read_json(paths.lock_file)
            if (
                str(record.get("phase") or "") != "PARENT_ACTIVE"
                or str(lock.get("phase") or "") != "PARENT_ACTIVE"
                or str(lock.get("parent_run_id") or lock.get("run_id") or "") != str(record.get("run_id") or "")
                or str(lock.get("lease_nonce") or "") != str(record.get("lease_nonce") or "")
                or str(lock.get("manifest_sha256") or "") != str(record.get("manifest_sha256") or "")
            ):
                raise StateError(
                    "PARENT_RUNTIME_RECOVERY_PHASE_INVALID",
                    "runtime recovery can be marked only on the exact active parent",
                )
            now = utc_now()
            events = list(record.get("runtime_recovery_events") or [])
            events.append({"kind": "runtime-recovery-required", "at": now, "failure": failure})
            record["runtime_recovery_events"] = events[-20:]
            record["recovery_required"] = True
            record["runtime_recovery_failure"] = failure
            record["updated_at"] = now
            lock["recovery_required"] = True
            lock["heartbeat_at"] = now
            write_json_atomic(state_file, record)
            write_json_atomic(paths.lock_file, lock)
        return record

    def clear_parent_runtime_recovery(self, parent_run_dir: str | os.PathLike[str]) -> dict[str, Any]:
        state_file, initial = self.load(parent_run_dir)
        if str(initial.get("record_kind") or "") != "parent":
            raise StateError("PARENT_RECORD_REQUIRED", "runtime recovery requires a parent record")
        root = canonical_project_root(initial["project_root"])
        paths = self.paths(root, str(initial["run_id"]))
        with exclusive_state_lock(paths.parent_transition_lock):
            record = read_json(state_file)
            lock = read_json(paths.lock_file)
            if (
                str(record.get("phase") or "") != "PARENT_ACTIVE"
                or str(lock.get("phase") or "") != "PARENT_ACTIVE"
                or str(lock.get("parent_run_id") or lock.get("run_id") or "") != str(record.get("run_id") or "")
                or str(lock.get("lease_nonce") or "") != str(record.get("lease_nonce") or "")
            ):
                raise StateError(
                    "PARENT_RUNTIME_RECOVERY_PHASE_INVALID",
                    "runtime recovery can be cleared only on the exact active parent",
                )

            def exact_reconciled_pre_submit_retry(child_state: Path, child: dict[str, Any]) -> bool:
                """Accept only the one-send retry state reconciled before recovery clear.

                A normal PREFLIGHT_BLOCKED child remains zero-send only.  This
                narrow exception preserves the immutable mutation-disallowed
                claim and requires the fresh stale-target absence event created
                by ``reconcile_stale_child_pre_submit_retry_target``.
                """
                try:
                    candidate = self.pre_submit_retry_candidate(child_state.parent)
                except StateError:
                    return False
                authority = child.get("pre_submit_retry_authority")
                target_id = str(child.get("current_target_id") or "")
                cleanup = child.get("cleanup_evidence") if isinstance(child.get("cleanup_evidence"), dict) else {}
                evidence = cleanup.get("evidence") if isinstance(cleanup.get("evidence"), dict) else {}
                events = child.get("recovery_events") if isinstance(child.get("recovery_events"), list) else []
                latest = events[-1] if events and isinstance(events[-1], dict) else {}
                retired = str(latest.get("kind") or "") == "stale-pre-submit-retry-replacement-retired"
                lifecycle_path = Path(str(evidence.get("path") or ""))
                try:
                    lifecycle_path = lifecycle_path.expanduser().resolve(strict=True)
                    lifecycle_path.relative_to(child_state.parent)
                    lifecycle_valid = (
                        lifecycle_path.is_file()
                        and not lifecycle_path.is_symlink()
                        and sha256_file(lifecycle_path) == str(evidence.get("sha256") or "")
                        and sha256_file(lifecycle_path) == str(latest.get("cleanup_lifecycle_sha256") or "")
                    )
                except (OSError, RuntimeError, ValueError):
                    lifecycle_valid = False
                replacement_path = Path(str(
                    authority.get("retired_replacement_evidence_path") if retired else authority.get("replacement_evidence_path")
                    or ""
                )) if isinstance(authority, dict) else Path()
                try:
                    replacement_valid = (
                        replacement_path.is_file()
                        and not replacement_path.is_symlink()
                        and sha256_file(replacement_path) == str(
                            authority.get("retired_replacement_evidence_sha256") if retired else authority.get("replacement_evidence_sha256")
                            or ""
                        )
                    )
                except OSError:
                    replacement_valid = False
                prior_reconciled = any(
                    isinstance(event, dict)
                    and str(event.get("kind") or "") == "stale-pre-submit-retry-target-reconciled"
                    and str(event.get("target_id") or "") == target_id
                    and str(event.get("send_claim_sha256") or "") == str(candidate.get("claim_sha256") or "")
                    for event in events[:-1]
                )
                prior_activation_failure = any(
                    isinstance(event, dict)
                    and str(event.get("kind") or "") == "app-composer-target-activation-failed"
                    and isinstance(event.get("cleanup"), dict)
                    and str(event["cleanup"].get("target_id") or "") == target_id
                    and event["cleanup"].get("ok") is True
                    for event in events[:-1]
                )
                return bool(
                    str(child.get("phase") or "") == "PREFLIGHT_BLOCKED"
                    and (child_state.parent / "send.claim").is_file()
                    and int(child.get("send_attempt_count") or 0) == 1
                    and not child.get("session_id")
                    and not child.get("conversation_url")
                    and child.get("submission_receipt") is None
                    and child.get("result") is None
                    and str(child.get("parent_run_id") or "") == str(record.get("run_id") or "")
                    and str(child.get("parent_workflow_id") or "") == str(record.get("workflow_id") or "")
                    and str(child.get("parent_lease_nonce") or "") == str(record.get("lease_nonce") or "")
                    and isinstance(authority, dict)
                    and authority.get("eligible") is True
                    and authority.get("consumed_at") is None
                    and str(authority.get("run_id") or "") == str(child.get("run_id") or "")
                    and str(authority.get("parent_run_id") or "") == str(record.get("run_id") or "")
                    and str(authority.get("claim_sha256") or "") == str(candidate.get("claim_sha256") or "")
                    and str(authority.get("retired_replacement_target_id") if retired else authority.get("replacement_target_id") or "") == target_id
                    and replacement_valid
                    and cleanup.get("ok") is True
                    and str(cleanup.get("state") or "") in {"closed-and-absent", "already-absent"}
                    and str(cleanup.get("target_id") or "") == target_id
                    and lifecycle_valid
                    and str(child.get("owned_tab_state") or "") in {"closed-and-absent", "already-absent"}
                    and not bool(child.get("cleanup_pending"))
                    and int(child.get("owned_open_tabs") or 0) == 0
                    and (
                        (
                            not retired
                            and str(latest.get("kind") or "") == "stale-pre-submit-retry-target-reconciled"
                            and str(latest.get("cleanup_state") or "") == str(cleanup.get("state") or "")
                        )
                        or (
                            retired
                            and prior_reconciled
                            and prior_activation_failure
                            and str(latest.get("cleanup_lifecycle_sha256") or "") == str(evidence.get("sha256") or "")
                        )
                    )
                    and str(latest.get("target_id") or "") == target_id
                    and str(latest.get("send_claim_sha256") or "") == str(candidate.get("claim_sha256") or "")
                )

            unsafe: list[dict[str, Any]] = []
            for child_state, child in self._parent_children(paths.runs_dir, str(record["run_id"])):
                phase = str(child.get("phase") or "")
                summary = {
                    "run_id": child.get("run_id"),
                    "stage_id": child.get("stage_id"),
                    "phase": phase,
                }
                if phase == "COMPLETE":
                    if (
                        bool(child.get("cleanup_pending"))
                        or int(child.get("owned_open_tabs") or 0) != 0
                        or str(child.get("owned_tab_state") or "") not in {"closed-and-absent", "already-absent"}
                    ):
                        unsafe.append({**summary, "reason": "completed child cleanup is not durable"})
                    continue
                if phase == "SEND_REJECTED":
                    try:
                        self.pre_submit_retry_candidate(child_state.parent)
                    except StateError as exc:
                        unsafe.append({**summary, "reason": exc.code})
                    continue
                if phase == "LEASED" and self.pending_retired_retry_rebind_candidate(child_state.parent):
                    continue
                if phase == "PREFLIGHT_BLOCKED" and exact_reconciled_pre_submit_retry(child_state, child):
                    continue
                if phase in {
                    "CREATED",
                    "PREFLIGHTED",
                    "LEASED",
                    "PREFLIGHT_BLOCKED",
                    "BLOCKED_APP_TRANSACTION",
                    "CANCELLED_PRE_SUBMISSION",
                }:
                    claim_exists = (child_state.parent / "send.claim").exists()
                    target_absent = not child.get("current_target_id")
                    target_clean = bool(
                        str(child.get("owned_tab_state") or "") in {"closed-and-absent", "already-absent"}
                        and not bool(child.get("cleanup_pending"))
                        and int(child.get("owned_open_tabs") or 0) == 0
                    )
                    if (
                        claim_exists
                        or int(child.get("send_attempt_count") or 0) != 0
                        or child.get("session_id")
                        or child.get("conversation_url")
                        or child.get("submission_receipt") is not None
                        or child.get("result") is not None
                        or not (target_absent or target_clean)
                    ):
                        unsafe.append({**summary, "reason": "pre-submit child carries send or unclean target evidence"})
                    continue
                unsafe.append({**summary, "reason": "child remains active or uncertain"})
            if unsafe:
                raise StateError(
                    "PARENT_RUNTIME_RECOVERY_PENDING",
                    "existing children are not yet safe to continue the parent workflow",
                    {"children": unsafe},
                )
            now = utc_now()
            events = list(record.get("runtime_recovery_events") or [])
            events.append({"kind": "runtime-recovery-cleared", "at": now})
            record["runtime_recovery_events"] = events[-20:]
            record["recovery_required"] = False
            record["runtime_recovery_failure"] = None
            record["updated_at"] = now
            lock["recovery_required"] = False
            lock["heartbeat_at"] = now
            write_json_atomic(state_file, record)
            write_json_atomic(paths.lock_file, lock)
        return record

    def reopen_failed_parent_workflow(
        self,
        parent_run_dir: str | os.PathLike[str],
        manifest_path: str | os.PathLike[str],
        owner_pid: int | None = None,
    ) -> dict[str, Any]:
        state_file, initial = self.load(parent_run_dir)
        if str(initial.get("record_kind") or "") != "parent":
            raise StateError("PARENT_RECORD_REQUIRED", "failed-parent reopen requires a parent record")
        root = canonical_project_root(initial["project_root"])
        paths = self.paths(root, str(initial["run_id"]))
        manifest_file = Path(manifest_path).expanduser().resolve(strict=True)
        with exclusive_state_lock(paths.parent_transition_lock):
            record = read_json(state_file)
            tombstone = record.get("user_stop_tombstone") if isinstance(record.get("user_stop_tombstone"), dict) else {}
            if tombstone.get("permanent") is True or record.get("parent_stop_scope"):
                raise StateError(
                    "PARENT_USER_STOP_TOMBSTONE",
                    "a parent-wide stopped workflow can never be reopened",
                )
            failure = record.get("failure") if isinstance(record.get("failure"), dict) else {}
            failure_code = str(failure.get("code") or "")
            if str(record.get("phase") or "") != "PARENT_FAILED_CLOSED":
                raise StateError("PARENT_REOPEN_PHASE_INVALID", "only a failed-closed parent can use deterministic reopen")
            if failure_code not in {
                "CHILD_IDENTITY_INCOMPLETE",
                "IMMUTABLE_ARTIFACT_CONFLICT",
                "CHILD_NOT_COMPLETE",
                "PRE_SUBMIT_RETRY_SESSION_NOT_QUIESCENT",
                "APP_TRANSACTION_FAILED",
                "APP_COMPOSER_PREP_FAILED",
                "STAGE_ENVELOPE_INVALID_JSON",
                "AGBROWSE_JSON_INVALID",
            }:
                raise StateError(
                    "PARENT_REOPEN_FAILURE_UNSUPPORTED",
                    "failed parent reason is not a proven local post-child integration failure",
                    {"failure_code": failure_code},
                )
            if record.get("result") is not None:
                raise StateError("PARENT_REOPEN_RESULT_PRESENT", "a parent with a durable result cannot be reopened")
            if (
                str(Path(str(record.get("manifest_path") or "")).resolve()) != str(manifest_file)
                or sha256_file(manifest_file) != str(record.get("manifest_sha256") or "")
            ):
                raise StateError("BLOCKED_MANIFEST_MISMATCH", "failed-parent manifest identity changed")
            if same_process(record.get("owner") or {}):
                raise StateError("ACTIVE_PROJECT_OWNER", "live failed-parent owner cannot be replaced")
            if paths.lock_file.exists():
                raise StateError("SAME_PROJECT_ACTIVE_OR_UNCERTAIN", "project lock already exists during failed-parent reopen")
            existing_records = self._active_or_uncertain_records(paths.runs_dir)
            if existing_records:
                raise StateError(
                    "SAME_PROJECT_ACTIVE_OR_UNCERTAIN",
                    "another active or uncertain project run prevents failed-parent reopen",
                    {"records": existing_records[:10]},
                )
            children = self._parent_children(paths.runs_dir, str(record["run_id"]))
            if not children:
                raise StateError("PARENT_REOPEN_CHILDREN_MISSING", "failed-parent reopen requires completed child evidence")
            unsafe: list[dict[str, Any]] = []
            retry_candidates: list[dict[str, Any]] = []
            safe_pre_submit_candidates: list[dict[str, Any]] = []
            for child_state, child in children:
                result = child.get("result") if isinstance(child.get("result"), dict) else {}
                result_path = Path(str(result.get("path") or ""))
                summary = {
                    "run_id": child.get("run_id"),
                    "stage_id": child.get("stage_id"),
                    "phase": child.get("phase"),
                    "send_attempt_count": int(child.get("send_attempt_count") or 0),
                    "cleanup_pending": bool(child.get("cleanup_pending")),
                    "owned_tab_state": child.get("owned_tab_state"),
                    "owned_open_tabs": int(child.get("owned_open_tabs") or 0),
                }
                if str(child.get("phase") or "") in {"PREFLIGHT_BLOCKED", "BLOCKED_APP_TRANSACTION"}:
                    claim_exists = (child_state.parent / "send.claim").exists()
                    target_absent = not child.get("current_target_id")
                    target_clean = bool(
                        str(child.get("owned_tab_state") or "") in {"closed-and-absent", "already-absent"}
                        and not bool(child.get("cleanup_pending"))
                        and int(child.get("owned_open_tabs") or 0) == 0
                    )
                    safe = bool(
                        int(child.get("send_attempt_count") or 0) == 0
                        and not claim_exists
                        and not child.get("session_id")
                        and not child.get("conversation_url")
                        and child.get("submission_receipt") is None
                        and child.get("result") is None
                        and (target_absent or target_clean)
                    )
                    if safe:
                        safe_pre_submit_candidates.append(summary)
                    else:
                        unsafe.append({**summary, "reason": "pre-submit child carries send, identity, result, or unclean target evidence"})
                    continue
                if str(child.get("phase") or "") == "SEND_REJECTED":
                    try:
                        retry_candidate = self.pre_submit_retry_candidate(child_state.parent)
                    except StateError as exc:
                        unsafe.append({**summary, "retry_error": exc.code})
                    else:
                        retry_candidates.append(
                            {
                                **summary,
                                "claim_sha256": retry_candidate["claim_sha256"],
                                "send_stderr_sha256": retry_candidate["send_stderr_sha256"],
                                "target_id": retry_candidate.get("target_id"),
                            }
                        )
                    continue
                valid_result = False
                try:
                    result_path = result_path.expanduser().resolve(strict=True)
                    result_path.relative_to(child_state.parent)
                    valid_result = (
                        result_path.is_file()
                        and not result_path.is_symlink()
                        and sha256_file(result_path) == str(result.get("sha256") or "")
                        and result_path.stat().st_size == int(result.get("bytes") or -1)
                    )
                except (OSError, RuntimeError, TypeError, ValueError):
                    valid_result = False
                if (
                    str(child.get("phase") or "") != "COMPLETE"
                    or summary["send_attempt_count"] != 1
                    or summary["cleanup_pending"]
                    or summary["owned_tab_state"] not in {"closed-and-absent", "already-absent"}
                    or summary["owned_open_tabs"] != 0
                    or not child.get("session_id")
                    or not child.get("current_target_id")
                    or not CANONICAL_CHAT_RE.fullmatch(str(child.get("conversation_url") or ""))
                    or not valid_result
                ):
                    unsafe.append(summary)
            if failure_code == "CHILD_NOT_COMPLETE":
                failure_text = str(failure.get("message") or "")
                retry_stage_ids = {str(item.get("stage_id") or "") for item in retry_candidates}
                if not retry_candidates or not any(stage_id and stage_id in failure_text for stage_id in retry_stage_ids):
                    unsafe.append(
                        {
                            "parent_failure": failure_code,
                            "reason": "failure does not identify an exact retryable SEND_REJECTED child",
                        }
                    )
            elif failure_code == "PRE_SUBMIT_RETRY_SESSION_NOT_QUIESCENT":
                if len(retry_candidates) != 1:
                    unsafe.append(
                        {
                            "parent_failure": failure_code,
                            "reason": "session-quiescence retry requires exactly one proven SEND_REJECTED child",
                        }
                    )
            elif failure_code in {"APP_TRANSACTION_FAILED", "APP_COMPOSER_PREP_FAILED"}:
                if not safe_pre_submit_candidates or retry_candidates:
                    unsafe.append(
                        {
                            "parent_failure": failure_code,
                            "reason": "app-transaction reopen requires one or more zero-send safe pre-submit children",
                        }
                    )
            elif failure_code == "STAGE_ENVELOPE_INVALID_JSON":
                failure_message = str(failure.get("message") or "")
                local_parser_failure = bool(
                    re.fullmatch(
                        r"(?:Expecting ',' delimiter|Invalid \\escape|Unterminated string starting at): "
                        r"line \d+ column \d+ \(char \d+\)",
                        failure_message,
                    )
                )
                if (
                    not local_parser_failure
                    or bool(record.get("recovery_required"))
                    or int(record.get("owned_open_tabs") or 0) != 0
                    or retry_candidates
                    or safe_pre_submit_candidates
                ):
                    unsafe.append(
                        {
                            "parent_failure": failure_code,
                            "reason": "parser reopen requires an exact local JSON decoder failure after fully cleaned completed children",
                        }
                    )
            elif failure_code == "AGBROWSE_JSON_INVALID":
                if (
                    str(failure.get("message") or "") != "agbrowse returned non-JSON stdout"
                    or len(retry_candidates) != 1
                ):
                    unsafe.append(
                        {
                            "parent_failure": failure_code,
                            "reason": "agbrowse JSON reopen requires one exact retryable pre-submit child",
                        }
                    )
            elif retry_candidates:
                unsafe.extend(
                    {**item, "reason": "retryable child is incompatible with the recorded parent failure"}
                    for item in retry_candidates
                )
            if failure_code not in {"APP_TRANSACTION_FAILED", "APP_COMPOSER_PREP_FAILED"} and safe_pre_submit_candidates:
                unsafe.extend(
                    {**item, "reason": "safe pre-submit child is incompatible with the recorded parent failure"}
                    for item in safe_pre_submit_candidates
                )
            if unsafe:
                raise StateError(
                    "PARENT_REOPEN_CHILD_EVIDENCE_UNSAFE",
                    "all existing children must be complete, one-send, exact, hashed, and cleaned",
                    {"children": unsafe},
                )
            identity = process_identity(owner_pid)
            owner = {**identity, "nonce": uuid.uuid4().hex, "epoch": int(time.time_ns())}
            now = utc_now()
            lock = {
                "schema": SCHEMA,
                "record_kind": "parent",
                "run_id": record["run_id"],
                "parent_run_id": record["run_id"],
                "project_root": record["project_root"],
                "project_key": record["project_key"],
                "workflow_id": record["workflow_id"],
                "manifest_sha256": record["manifest_sha256"],
                "lease_nonce": record["lease_nonce"],
                "owner": owner,
                "phase": "PARENT_ACTIVE",
                "heartbeat_at": now,
            }
            try:
                fd = os.open(paths.lock_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(lock, handle, ensure_ascii=False, indent=2)
                    handle.write("\n")
            except FileExistsError as exc:
                raise StateError("SAME_PROJECT_ACTIVE_OR_UNCERTAIN", "project lock appeared during failed-parent reopen") from exc
            prior_failures = list(record.get("prior_failures") or [])
            prior_failures.append({"at": now, "phase": "PARENT_FAILED_CLOSED", "failure": failure})
            record["prior_failures"] = prior_failures
            record["failure"] = None
            record["owner"] = owner
            record["phase_events"].append(
                {
                    "from": "PARENT_FAILED_CLOSED",
                    "to": "PARENT_ACTIVE",
                    "at": now,
                    "reason": "deterministic-local-post-child-reopen",
                    "retry_candidate_count": len(retry_candidates),
                    "safe_pre_submit_candidate_count": len(safe_pre_submit_candidates),
                }
            )
            record["phase"] = "PARENT_ACTIVE"
            record["phase_at"] = now
            record["updated_at"] = now
            record["child_scan"] = [
                {
                    "run_id": child.get("run_id"),
                    "stage_id": child.get("stage_id"),
                    "phase": child.get("phase"),
                    "send_attempt_count": int(child.get("send_attempt_count") or 0),
                    "cleanup_pending": bool(child.get("cleanup_pending")),
                    "owned_open_tabs": int(child.get("owned_open_tabs") or 0),
                }
                for _, child in children
            ]
            record["pre_submit_retry_candidates"] = retry_candidates
            record["safe_pre_submit_candidates"] = safe_pre_submit_candidates
            try:
                write_json_atomic(state_file, record)
            except Exception:
                try:
                    paths.lock_file.unlink()
                except OSError:
                    pass
                raise
            return record

    @staticmethod
    def _terminal_lock_file_snapshot(
        lock_file: Path,
    ) -> tuple[dict[str, Any], bytes]:
        """Read one regular lock through one handle and retain its stable OS identity."""
        raw_path = lock_file.expanduser()
        try:
            if raw_path.is_symlink() or (
                hasattr(os.path, "isjunction") and os.path.isjunction(raw_path)
            ):
                raise OSError("terminal lock is a reparse point")
            with raw_path.open("rb") as handle:
                before = os.fstat(handle.fileno())
                if not stat.S_ISREG(before.st_mode):
                    raise OSError("terminal lock is not regular")
                if os.name == "nt":
                    import ctypes
                    import msvcrt

                    class _ByHandleFileInformation(ctypes.Structure):
                        _fields_ = [
                            ("dwFileAttributes", ctypes.c_ulong),
                            ("ftCreationTimeLow", ctypes.c_ulong),
                            ("ftCreationTimeHigh", ctypes.c_ulong),
                            ("ftLastAccessTimeLow", ctypes.c_ulong),
                            ("ftLastAccessTimeHigh", ctypes.c_ulong),
                            ("ftLastWriteTimeLow", ctypes.c_ulong),
                            ("ftLastWriteTimeHigh", ctypes.c_ulong),
                            ("dwVolumeSerialNumber", ctypes.c_ulong),
                            ("nFileSizeHigh", ctypes.c_ulong),
                            ("nFileSizeLow", ctypes.c_ulong),
                            ("nNumberOfLinks", ctypes.c_ulong),
                            ("nFileIndexHigh", ctypes.c_ulong),
                            ("nFileIndexLow", ctypes.c_ulong),
                        ]

                    info = _ByHandleFileInformation()
                    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                    ok = kernel32.GetFileInformationByHandle(
                        ctypes.c_void_p(msvcrt.get_osfhandle(handle.fileno())),
                        ctypes.byref(info),
                    )
                    if not ok:
                        raise OSError(ctypes.get_last_error(), "file identity unavailable")
                    os_identity = {
                        "kind": "windows-file-id",
                        "volume_serial": int(info.dwVolumeSerialNumber),
                        "file_index_high": int(info.nFileIndexHigh),
                        "file_index_low": int(info.nFileIndexLow),
                    }
                else:
                    os_identity = {
                        "kind": "posix-device-inode",
                        "device": int(before.st_dev),
                        "inode": int(before.st_ino),
                    }
                data = handle.read()
                after = os.fstat(handle.fileno())
                if (
                    before.st_dev != after.st_dev
                    or before.st_ino != after.st_ino
                    or before.st_size != after.st_size
                    or len(data) != after.st_size
                ):
                    raise OSError("terminal lock changed while being read")
            resolved = raw_path.resolve(strict=True)
            snapshot = {
                "path": str(raw_path),
                "resolved_path": str(resolved),
                "sha256": sha256_bytes(data),
                "bytes": len(data),
                "regular_file": True,
                "symlink": False,
                "reparse_point": False,
                "os_identity": os_identity,
            }
            return snapshot, data
        except (OSError, RuntimeError, ValueError) as exc:
            raise StateError(
                "BLOCKED_OWNER_MISMATCH",
                "terminal stopped lock is not one stable regular file",
            ) from exc

    @staticmethod
    def _validate_terminal_user_stop_lock(
        lock_file: Path, parent: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any], bytes]:
        """Validate the exact terminal lock bytes and every stop authority binding."""
        expected = (
            parent.get("terminal_user_stop_lock")
            if isinstance(parent.get("terminal_user_stop_lock"), dict)
            else {}
        )
        try:
            observed, raw_bytes = RunStore._terminal_lock_file_snapshot(lock_file)
            parsed = json.loads(raw_bytes.decode("utf-8"))
            if not isinstance(parsed, dict):
                raise ValueError("terminal lock JSON must be an object")
            lock = parsed
            canonical_parent_root = str(canonical_project_root(parent["project_root"]))
            canonical_lock_root = str(canonical_project_root(lock["project_root"]))
            owner = lock.get("owner") if isinstance(lock.get("owner"), dict) else {}
            parent_owner = parent.get("owner") if isinstance(parent.get("owner"), dict) else {}
            scope = parent.get("parent_stop_scope")
            scope_value = scope if isinstance(scope, dict) else {}
            tombstone = parent.get("user_stop_tombstone") if isinstance(parent.get("user_stop_tombstone"), dict) else {}
            stop_epoch = str(scope_value.get("stop_epoch_nonce") or tombstone.get("stop_epoch_nonce") or "")
            exact_descriptor = {
                "path": str(lock_file),
                "resolved_path": str(lock_file.resolve(strict=True)),
                "sha256": str(expected.get("sha256") or ""),
                "bytes": int(expected.get("bytes") or -1),
                "regular_file": True,
                "symlink": False,
                "reparse_point": False,
            }
            observed_descriptor = {key: observed[key] for key in exact_descriptor}
            valid = bool(
                expected == exact_descriptor
                and observed_descriptor == exact_descriptor
                and re.fullmatch(r"[0-9a-f]{64}", exact_descriptor["sha256"])
                and lock.get("schema") == SCHEMA
                and lock.get("record_kind") == "parent"
                and str(lock.get("run_id") or "") == str(parent.get("run_id") or "")
                and str(lock.get("parent_run_id") or "") == str(parent.get("run_id") or "")
                and canonical_lock_root == canonical_parent_root
                and str(lock.get("project_root") or "") == canonical_parent_root
                and str(parent.get("project_root") or "") == canonical_parent_root
                and str(lock.get("project_key") or "") == str(parent.get("project_key") or "")
                and str(lock.get("workflow_id") or "") == str(parent.get("workflow_id") or "")
                and str(lock.get("lease_nonce") or "") == str(parent.get("lease_nonce") or "")
                and str(lock.get("manifest_sha256") or "") == str(parent.get("manifest_sha256") or "")
                and bool(str(owner.get("nonce") or ""))
                and str(owner.get("nonce") or "") == str(parent_owner.get("nonce") or "")
                and owner.get("epoch") == parent_owner.get("epoch")
                and owner == parent_owner
                and bool(stop_epoch)
                and str(lock.get("stop_epoch_nonce") or "") == stop_epoch
                and str(tombstone.get("stop_epoch_nonce") or "") == stop_epoch
                and tombstone.get("permanent") is True
                and lock.get("parent_stop_scope") == scope
                and lock.get("user_stop_scan") == parent.get("user_stop_scan")
                and lock.get("user_stop_tombstone") == tombstone
                and str(lock.get("phase") or "") == "PARENT_FAILED_CLOSED"
                and str(parent.get("phase") or "") == "PARENT_FAILED_CLOSED"
            )
        except (KeyError, OSError, RuntimeError, TypeError, ValueError, StateError):
            valid = False
            lock = {}
            observed = {}
            raw_bytes = b""
        if not valid:
            raise StateError("BLOCKED_OWNER_MISMATCH", "terminal stopped lock is not the exact immutable terminal lock")
        return lock, observed, raw_bytes

    @staticmethod
    def _unlink_validated_terminal_user_stop_lock(lock_file: Path, parent: dict[str, Any]) -> None:
        """Fail closed unless the same exact lock is still present immediately before unlink."""
        _, first_snapshot, first_bytes = RunStore._validate_terminal_user_stop_lock(lock_file, parent)
        _, final_snapshot, final_bytes = RunStore._validate_terminal_user_stop_lock(lock_file, parent)
        if (
            final_snapshot.get("os_identity") != first_snapshot.get("os_identity")
            or final_snapshot.get("sha256") != first_snapshot.get("sha256")
            or final_snapshot.get("bytes") != first_snapshot.get("bytes")
            or final_bytes != first_bytes
        ):
            raise StateError("BLOCKED_OWNER_MISMATCH", "terminal stopped lock was replaced before unlink")
        lock_file.unlink()

    def finalize_user_stopped_parent(
        self,
        parent_run_dir: str | os.PathLike[str],
        *,
        tab_absence_evidence: dict[str, Any] | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Strict, no-retry release path for an explicitly stopped workflow."""
        parent_file, initial = self.load(parent_run_dir)
        if str(initial.get("record_kind") or "") != "parent":
            raise StateError("PARENT_RECORD_REQUIRED", "user-stop drain requires a parent record")
        paths = self.paths(canonical_project_root(initial["project_root"]), str(initial["run_id"]))
        with exclusive_state_lock(paths.parent_transition_lock):
            parent = read_json(parent_file)
            lock = read_json(paths.lock_file)
            parent_phase = str(parent.get("phase") or "")
            lock_phase = str(lock.get("phase") or "")
            if parent_phase == lock_phase == "PARENT_FAILED_CLOSED":
                self._unlink_validated_terminal_user_stop_lock(paths.lock_file, parent)
                return parent
            if parent_phase != lock_phase or parent_phase not in {"USER_STOP_REQUESTED", "PARENT_DRAINING"}:
                raise StateError("PARENT_USER_STOP_CONFIRMATION_REQUIRED", "parent is not in explicit user-stop drain state")
            was_draining = parent_phase == "PARENT_DRAINING"
            children = self._strict_parent_children(paths, parent)
            summaries: list[dict[str, Any]] = []
            unsafe: list[dict[str, Any]] = []
            for child_file, child in children:
                phase = str(child.get("phase") or "")
                claim = child_file.parent / "send.claim"
                identity = {
                    "project_root": child.get("project_root"),
                    "project_key": child.get("project_key"),
                    "parent_run_id": child.get("parent_run_id"),
                    "parent_workflow_id": child.get("parent_workflow_id"),
                    "parent_lease_nonce": child.get("parent_lease_nonce"),
                    "run_id": child.get("run_id"),
                    "stage_id": child.get("stage_id"),
                    "role": child.get("role"),
                    "lane": child.get("lane"),
                    "iteration": child.get("iteration"),
                    "session_id": child.get("session_id"),
                    "target_id": child.get("current_target_id"),
                    "conversation_url": child.get("conversation_url"),
                }
                summary = {
                    "run_id": child.get("run_id"),
                    "stage_id": child.get("stage_id"),
                    "phase": phase,
                    "state_sha256": sha256_file(child_file),
                    "state_bytes": child_file.stat().st_size,
                    "send_claim": (
                        {"sha256": sha256_file(claim), "bytes": claim.stat().st_size}
                        if claim.is_file() and not claim.is_symlink()
                        else None
                    ),
                    "identity": identity,
                }
                clean = not bool(child.get("cleanup_pending")) and int(child.get("owned_open_tabs") or 0) == 0 and str(child.get("owned_tab_state") or "") in {"closed-and-absent", "already-absent"}
                claim_proof = self._send_claim_proof(child_file, child) if claim.exists() else None
                safe = False
                if phase == "COMPLETE":
                    safe = claim_proof is not None and clean and bool(child.get("session_id")) and bool(child.get("current_target_id")) and bool(child.get("conversation_url")) and self._complete_result_capture_valid(child_file, child)
                elif phase == "PROVIDER_FAILED_TERMINAL":
                    safe = claim_proof is not None and clean and self._provider_failed_terminal_settled(child, child_file)
                elif phase == "ABANDONED_UNCERTAIN":
                    stop = child.get("user_stop") if isinstance(child.get("user_stop"), dict) else {}
                    drift_ref = stop.get("target_drift_abandonment") if isinstance(stop.get("target_drift_abandonment"), dict) else {}
                    uncertain = child.get("parent_stop_submission_uncertain") if isinstance(child.get("parent_stop_submission_uncertain"), dict) else {}
                    if drift_ref:
                        drift_path = Path(str(drift_ref.get("path") or ""))
                        try:
                            drift = read_json(drift_path)
                            survivor = drift.get("protected_survivor") if isinstance(drift.get("protected_survivor"), dict) else {}
                            stale = drift.get("reported_stale_target") if isinstance(drift.get("reported_stale_target"), dict) else {}
                            decision = str(drift.get("decision") or "")
                            historical = self.parent_historical_owned_target_ids(paths, parent)
                            required_absent = drift.get("required_absent_target_ids")
                            absence_union = drift.get("historical_target_absence_union")
                            live_shape = bool(
                                decision == "abandon-without-close"
                                and required_absent == historical
                                and absence_union == required_absent
                                and survivor.get("ownership_adopted") is False
                                and survivor.get("close_authorized") is False
                                and str(survivor.get("target_id") or "") not in historical
                            )
                            stale_id = str(stale.get("target_id") or "")
                            absent_shape = bool(
                                decision == "abandon-without-close-no-live-target"
                                and stale_id and stale_id not in historical
                                and required_absent == sorted({*historical, stale_id})
                                and absence_union == required_absent
                                and stale.get("ownership_adopted") is False
                                and stale.get("close_authorized") is False
                                and stale.get("proven_absent") is True
                                and not survivor
                            )
                            proof_rounds = drift.get("proof_rounds")
                            same_target_stale_sent_shape = bool(
                                decision == "abandon-without-close-stale-sent-session"
                                and isinstance(proof_rounds, list)
                                and len(proof_rounds) == 2
                                and stale_id == str(child.get("current_target_id") or "")
                                and stale_id in historical
                                and required_absent == historical
                                and absence_union == required_absent
                                and stale.get("ownership_adopted") is False
                                and stale.get("close_authorized") is False
                                and stale.get("tab_closed") is False
                                and stale.get("proven_absent") is True
                                and stale.get("classification")
                                == "owned-reported-stale-target-absent"
                                and not survivor
                                and all(
                                    isinstance(round_, dict)
                                    and round_.get("valid") is True
                                    and round_.get("stale_sent_session_valid") is True
                                    and round_.get("session_virtual_url") is True
                                    and round_.get("stored_target_absent") is True
                                    and round_.get("status") == "sent"
                                    and str(round_.get("survivor_target_id") or "")
                                    == stale_id
                                    and not round_.get("helper_values")
                                    and re.fullmatch(
                                        r"https://chatgpt\.com/c/WEB:[0-9A-Fa-f-]{16,}(?:[?#].*)?",
                                        str(round_.get("session_url") or ""),
                                    )
                                    for round_ in proof_rounds
                                )
                                and str(proof_rounds[0].get("session_url") or "")
                                == str(proof_rounds[1].get("session_url") or "")
                                == str(stale.get("conversation_url") or "")
                            )
                            drift_ok = bool(
                                drift_path.is_file() and not drift_path.is_symlink()
                                and sha256_file(drift_path) == str(drift_ref.get("sha256") or "")
                                and drift_path.stat().st_size == int(drift_ref.get("bytes") or -1)
                                and drift.get("schema") == "codex.chatgpt.user-stop-target-drift-abandonment/v1"
                                and drift.get("recorded", {}).get("target_id") == child.get("current_target_id")
                                and drift.get("recorded", {}).get("conversation_url") == child.get("conversation_url")
                                and drift.get("historical_owned_target_ids") == historical
                                and (
                                    live_shape
                                    or absent_shape
                                    or same_target_stale_sent_shape
                                )
                            )
                        except (OSError, TypeError, ValueError, StateError):
                            drift_ok = False
                        safe = bool(
                            drift_ok and not child.get("cleanup_pending")
                            and int(child.get("owned_open_tabs") or 0) == 0
                            and child.get("owned_tab_state") in {
                                "historical-target-absent-survivor-protected",
                                "historical-and-reported-targets-absent",
                            }
                            and child.get("result") is None
                        )
                    elif uncertain:
                        preclose = uncertain.get("preclose") if isinstance(uncertain.get("preclose"), dict) else {}
                        settlement = uncertain.get("settlement") if isinstance(uncertain.get("settlement"), dict) else {}
                        try:
                            pre_path = Path(str(preclose.get("path") or "")); settle_path = Path(str(settlement.get("path") or ""))
                            pre = read_json(pre_path); settled = read_json(settle_path)
                            uncertain_ok = bool(
                                pre_path.is_file() and not pre_path.is_symlink()
                                and settle_path.is_file() and not settle_path.is_symlink()
                                and sha256_file(pre_path) == str(preclose.get("sha256") or "")
                                and sha256_file(settle_path) == str(settlement.get("sha256") or "")
                                and pre.get("schema") == "codex.chatgpt.parent-stop-submission-uncertain-preclose/v1"
                                and settled.get("schema") == "codex.chatgpt.parent-stop-submission-uncertain-settlement/v1"
                                and settled.get("preclose") == preclose
                                and settled.get("zero_provider_asserted") is False
                                and settled.get("provider_mutation_may_have_occurred") is True
                                and settled.get("result_promoted") is False
                                and settled.get("recorded_session_id") is None
                                and settled.get("recorded_conversation_url") is None
                                and pre.get("claim", {}).get("sha256") == sha256_file(claim)
                            )
                        except (OSError, StateError):
                            uncertain_ok = False
                        safe = bool(
                            uncertain_ok and clean and child.get("session_id") is None
                            and child.get("conversation_url") is None
                            and child.get("submission_receipt") is None and child.get("result") is None
                            and not child.get("zero_provider_settlement")
                        )
                    else:
                        confirmation = stop.get("confirmation") if isinstance(stop.get("confirmation"), dict) else {}
                        evidence_path = Path(str(confirmation.get("path") or ""))
                        evidence_ok = False
                        if evidence_path.is_file() and str(confirmation.get("sha256") or "") == sha256_file(evidence_path):
                            try:
                                evidence = read_json(evidence_path)
                                evidence_ok = evidence.get("schema") == "codex.chatgpt.user-stop-adjudication/v2" and evidence.get("terminal") is True and str(evidence.get("run_id") or "") == str(child.get("run_id") or "") and str(evidence.get("session_id") or "") == str(child.get("session_id") or "") and str(evidence.get("target_id") or "") == str(child.get("current_target_id") or "") and str(evidence.get("conversation_url") or "") == str(child.get("conversation_url") or "")
                            except StateError:
                                evidence_ok = False
                        safe = claim_proof is not None and clean and bool(child.get("session_id")) and bool(child.get("current_target_id")) and bool(child.get("conversation_url")) and Path(str(stop.get("authorization", {}).get("path") if isinstance(stop.get("authorization"), dict) else "")).is_file() and evidence_ok
                elif phase == "SEND_REJECTED":
                    safe = self._send_rejected_zero_provider_settled(child_file, child)
                if phase in {"PREFLIGHT_BLOCKED", "BLOCKED_APP_TRANSACTION", "CANCELLED_PRE_SUBMISSION"}:
                    target = str(child.get("current_target_id") or "")
                    cleanup_value = (
                        child.get("cleanup_evidence")
                        if isinstance(child.get("cleanup_evidence"), dict)
                        else {}
                    )
                    target_safe = not target or self._pre_submit_cleanup_proof(
                        child_file, child, cleanup_value
                    ) is not None
                    safe = bool(
                        not claim.exists()
                        and int(child.get("send_attempt_count") or 0) == 0
                        and child.get("submission_receipt") is None
                        and not child.get("session_id")
                        and not child.get("conversation_url")
                        and not child.get("result")
                        and target_safe
                    )
                if not safe:
                    unsafe.append({**summary, "reason": "unsafe-child-or-cleanup"})
                summaries.append({**summary, "safe": safe})
            if dry_run:
                return {
                    "schema": "codex.chatgpt.parent-user-stop-child-scan-read-only/v1",
                    "parent_run_id": parent.get("run_id"),
                    "parent_phase": parent_phase,
                    "lock_phase": lock_phase,
                    "strict_terminal_scan_ready": not unsafe,
                    "children": summaries,
                    "unsafe": unsafe,
                }
            if unsafe:
                parent["child_scan"] = summaries
                parent["updated_at"] = utc_now()
                write_json_atomic(parent_file, parent)
                return parent
            if parent.get("parent_stop_scope"):
                tabs_ref = tab_absence_evidence if isinstance(tab_absence_evidence, dict) else {}
                tabs_path = Path(str(tabs_ref.get("path") or ""))
                try:
                    tabs_scan = read_json(tabs_path)
                except (OSError, StateError):
                    tabs_scan = {}
                known_targets = self.parent_historical_owned_target_ids(paths, parent)
                protected_survivors: list[dict[str, Any]] = []
                for _, child in children:
                    stop = child.get("user_stop") if isinstance(child.get("user_stop"), dict) else {}
                    drift_ref = stop.get("target_drift_abandonment") if isinstance(stop.get("target_drift_abandonment"), dict) else {}
                    if drift_ref:
                        drift = read_json(Path(str(drift_ref.get("path") or "")))
                        required = drift.get("required_absent_target_ids")
                        if not isinstance(required, list) or not all(isinstance(item, str) and item for item in required):
                            raise StateError("TARGET_DRIFT_DESCRIPTOR_INVALID", "required target-absence set is invalid")
                        known_targets = sorted({*known_targets, *required})
                        survivor = drift.get("protected_survivor")
                        if isinstance(survivor, dict):
                            protected_survivors.append(survivor)
                retry_scan_valid = True
                if tabs_scan.get("schema") == "codex.chatgpt.parent-stop-final-tab-scan/v2":
                    previous_ref = tabs_scan.get("previous_scan") if isinstance(tabs_scan.get("previous_scan"), dict) else {}
                    previous_path = Path(str(previous_ref.get("path") or ""))
                    drift = tabs_scan.get("external_inventory_drift") if isinstance(tabs_scan.get("external_inventory_drift"), dict) else {}
                    try:
                        previous_scan = read_json(previous_path)
                        current_rows = tabs_scan.get("normalized_tabs")
                        previous_rows = previous_scan.get("normalized_tabs")
                        if not isinstance(current_rows, list) or not isinstance(previous_rows, list):
                            raise ValueError("tab rows missing")
                        current_ids = [str(row.get("targetId") or "") for row in current_rows if isinstance(row, dict)]
                        previous_ids = [str(row.get("targetId") or "") for row in previous_rows if isinstance(row, dict)]
                        if (
                            len(current_ids) != len(current_rows) or len(previous_ids) != len(previous_rows)
                            or any(not value for value in [*current_ids, *previous_ids])
                            or len(set(current_ids)) != len(current_ids)
                            or len(set(previous_ids)) != len(previous_ids)
                        ):
                            raise ValueError("tab identities ambiguous")
                        row_key = lambda row: json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                        old_map = {row_key(row): row for row in previous_rows}
                        new_map = {row_key(row): row for row in current_rows}
                        expected_added = [new_map[key] for key in sorted(set(new_map) - set(old_map))]
                        expected_removed = [old_map[key] for key in sorted(set(old_map) - set(new_map))]
                        boundary_ids = {*known_targets, *(str(item.get("target_id") or "") for item in protected_survivors)}
                        boundary_urls = set(self.parent_historical_owned_urls(paths, parent))
                        boundary_urls.update(str(item.get("conversation_url") or "") for item in protected_survivors)
                        changed = [*expected_added, *expected_removed]
                        retry_scan_valid = bool(
                            previous_path.is_file() and not previous_path.is_symlink()
                            and sha256_file(previous_path) == str(previous_ref.get("sha256") or "")
                            and previous_path.stat().st_size == int(previous_ref.get("bytes") or -1)
                            and previous_scan.get("schema") in {
                                "codex.chatgpt.parent-stop-final-tab-scan/v1",
                                "codex.chatgpt.parent-stop-final-tab-scan/v2",
                            }
                            and previous_scan.get("known_target_ids") == known_targets
                            and previous_scan.get("protected_survivors", []) == protected_survivors
                            and previous_scan.get("all_known_targets_absent") is True
                            and str(previous_scan.get("stop_epoch_nonce") or (previous_scan.get("parent_stop_scope") or {}).get("stop_epoch_nonce") or "")
                            == str((parent.get("parent_stop_scope") or {}).get("stop_epoch_nonce") or "")
                            and not (set(current_ids) & set(known_targets))
                            and not (set(previous_ids) & set(known_targets))
                            and drift.get("added") == expected_added
                            and drift.get("removed") == expected_removed
                            and all(str(row.get("targetId") or "") not in boundary_ids for row in changed)
                            and all(str(row.get("url") or "") not in boundary_urls for row in changed)
                        )
                    except (OSError, RuntimeError, TypeError, ValueError, StateError):
                        retry_scan_valid = False
                if (
                    not tabs_path.is_file() or tabs_path.is_symlink()
                    or sha256_file(tabs_path) != str(tabs_ref.get("sha256") or "")
                    or tabs_scan.get("schema") not in {
                        "codex.chatgpt.parent-stop-final-tab-scan/v1",
                        "codex.chatgpt.parent-stop-final-tab-scan/v2",
                    }
                    or tabs_scan.get("known_target_ids") != known_targets
                    or tabs_scan.get("protected_survivors", []) != protected_survivors
                    or any(str(item.get("target_id") or "") in known_targets for item in protected_survivors)
                    or tabs_scan.get("all_known_targets_absent") is not True
                    or tabs_scan.get("parent_stop_scope") != parent.get("parent_stop_scope")
                    or (
                        tabs_scan.get("schema") == "codex.chatgpt.parent-stop-final-tab-scan/v2"
                        and (
                            not isinstance(tabs_scan.get("previous_scan"), dict)
                            or (tabs_scan.get("external_inventory_drift") or {}).get("classification") not in {
                                "foreign-unowned-tab-inventory-drift",
                                "foreign-unowned-tab-inventory-drift-pre-drain",
                            }
                            or (tabs_scan.get("external_inventory_drift") or {}).get("mutation_commands") != []
                            or (tabs_scan.get("external_inventory_drift") or {}).get("ownership_adopted") is not False
                            or (tabs_scan.get("external_inventory_drift") or {}).get("close_authorized") is not False
                            or (
                                (tabs_scan.get("external_inventory_drift") or {}).get("classification")
                                == "foreign-unowned-tab-inventory-drift-pre-drain"
                                and (
                                    parent_phase != "USER_STOP_REQUESTED"
                                    or (tabs_scan.get("external_inventory_drift") or {}).get("prior_scan_attached_or_consumed") is not False
                                    or parent.get("user_stop_scan") not in (None, {})
                                    or lock.get("user_stop_scan") not in (None, {})
                                )
                            )
                            or not retry_scan_valid
                        )
                    )
                ):
                    parent["child_scan"] = summaries
                    parent["updated_at"] = utc_now()
                    write_json_atomic(parent_file, parent)
                    return parent
            scan_path = paths.run_dir / "user-stop" / "parent-scan.json"
            scan = {
                "schema": "codex.chatgpt.parent-user-stop-scan/v1",
                "parent_run_id": parent["run_id"],
                "project_root": parent["project_root"],
                "project_key": parent["project_key"],
                "workflow_id": parent["workflow_id"],
                "lease_nonce": parent["lease_nonce"],
                "children": summaries,
                "parent_stop_scope": parent.get("parent_stop_scope"),
                "tab_absence_evidence": tab_absence_evidence,
                "parent_state_sha256": sha256_file(parent_file),
                "lock_sha256": sha256_file(paths.lock_file),
            }
            if scan_path.exists():
                persisted_scan = read_json(scan_path)
                same_scan = bool(
                    persisted_scan.get("schema") == scan["schema"]
                    and persisted_scan.get("parent_run_id") == scan["parent_run_id"]
                    and persisted_scan.get("children") == summaries
                    and persisted_scan.get("parent_stop_scope") == scan["parent_stop_scope"]
                    and persisted_scan.get("tab_absence_evidence") == tab_absence_evidence
                )
                if same_scan:
                    descriptor = {"path": str(scan_path), "sha256": sha256_file(scan_path), "bytes": scan_path.stat().st_size}
                elif (
                    was_draining
                    and persisted_scan.get("schema") == scan["schema"]
                    and persisted_scan.get("parent_run_id") == scan["parent_run_id"]
                    and persisted_scan.get("children") == summaries
                    and persisted_scan.get("parent_stop_scope") == scan["parent_stop_scope"]
                    and tabs_scan.get("schema") == "codex.chatgpt.parent-stop-final-tab-scan/v2"
                ):
                    prior_descriptor = {"path": str(scan_path), "sha256": sha256_file(scan_path), "bytes": scan_path.stat().st_size}
                    retries = sorted(scan_path.parent.glob("parent-scan-retry-*.json"))
                    if retries:
                        latest_path = retries[-1]
                        latest = read_json(latest_path)
                        prior_descriptor = {"path": str(latest_path), "sha256": sha256_file(latest_path), "bytes": latest_path.stat().st_size}
                        if (
                            latest.get("schema") != "codex.chatgpt.parent-user-stop-scan/v2"
                            or latest.get("tab_absence_evidence") != tabs_scan.get("previous_scan")
                        ):
                            raise StateError("BLOCKED_OWNER_MISMATCH", "parent retry scan chain differs")
                    elif tabs_scan.get("previous_scan") != persisted_scan.get("tab_absence_evidence"):
                        raise StateError("BLOCKED_OWNER_MISMATCH", "parent retry scan does not extend the base scan")
                    retry_scan = {
                        **scan,
                        "schema": "codex.chatgpt.parent-user-stop-scan/v2",
                        "previous_scan": prior_descriptor,
                        "external_inventory_drift_only": True,
                    }
                    retry_path = scan_path.parent / f"parent-scan-retry-{len(retries) + 1:03d}.json"
                    descriptor = write_immutable_json_exclusive(retry_path, retry_scan)
                else:
                    raise StateError("BLOCKED_OWNER_MISMATCH", "published parent scan differs across retry")
            else:
                descriptor = write_immutable_json_exclusive(scan_path, scan)
                if (
                    sha256_file(paths.lock_file) != scan["lock_sha256"]
                    or sha256_file(parent_file) != scan["parent_state_sha256"]
                ):
                    raise StateError("BLOCKED_OWNER_MISMATCH", "parent or full project lock changed after user-stop scan publication")
            now = utc_now()
            if not was_draining:
                parent.setdefault("phase_events", []).append({"from": "USER_STOP_REQUESTED", "to": "PARENT_DRAINING", "at": now})
                parent.update({"phase": "PARENT_DRAINING", "phase_at": now, "updated_at": now, "owned_open_tabs": 0, "child_scan": summaries})
                lock.update({"phase": "PARENT_DRAINING", "heartbeat_at": now})
                write_json_atomic(parent_file, parent)
                write_json_atomic(paths.lock_file, lock)
            draining_parent_sha = sha256_file(parent_file); draining_lock_sha = sha256_file(paths.lock_file)
            if sha256_file(parent_file) != draining_parent_sha or sha256_file(paths.lock_file) != draining_lock_sha:
                raise StateError("BLOCKED_OWNER_MISMATCH", "draining state changed before terminal write")
            terminal_at = utc_now()
            parent_scope = parent.get("parent_stop_scope") if isinstance(parent.get("parent_stop_scope"), dict) else {}
            parent_tombstone = parent.get("user_stop_tombstone") if isinstance(parent.get("user_stop_tombstone"), dict) else {}
            terminal_stop_epoch = str(lock.get("stop_epoch_nonce") or parent_scope.get("stop_epoch_nonce") or parent_tombstone.get("stop_epoch_nonce") or uuid.uuid4().hex)
            parent.setdefault("phase_events", []).append({"from": "PARENT_DRAINING", "to": "PARENT_FAILED_CLOSED", "at": terminal_at})
            parent.update({"phase": "PARENT_FAILED_CLOSED", "phase_at": terminal_at, "updated_at": terminal_at, "user_stop_scan": descriptor, "result": None, "failure": {"code": "USER_STOPPED_LEGACY_WORKFLOW", "scan": descriptor}, "user_stop_tombstone": {**parent_tombstone, "permanent": True, "stop_epoch_nonce": terminal_stop_epoch, "terminal_scan": descriptor}})
            lock.update({"phase": "PARENT_FAILED_CLOSED", "heartbeat_at": terminal_at, "stop_epoch_nonce": terminal_stop_epoch, "user_stop_scan": descriptor, "user_stop_tombstone": parent["user_stop_tombstone"]})
            terminal_lock_bytes = (json.dumps(lock, ensure_ascii=False, indent=2) + "\n").replace("\n", os.linesep).encode("utf-8")
            parent["terminal_user_stop_lock"] = {
                "path": str(paths.lock_file),
                "resolved_path": str(paths.lock_file.resolve()),
                "sha256": sha256_bytes(terminal_lock_bytes),
                "bytes": len(terminal_lock_bytes),
                "regular_file": True,
                "symlink": False,
                "reparse_point": False,
            }
            write_json_atomic(parent_file, parent)
            write_json_atomic(paths.lock_file, lock)
            self._unlink_validated_terminal_user_stop_lock(paths.lock_file, parent)
            if os.name != "nt":
                directory_fd = os.open(str(paths.lock_file.parent), os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            return parent

    def finalize_parent(
        self,
        parent_run_dir: str | os.PathLike[str],
        phase: str,
        *,
        result: dict[str, Any] | None = None,
        failure: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if phase not in {"PARENT_COMPLETE", "PARENT_FAILED_CLOSED"}:
            raise StateError("PARENT_TERMINAL_PHASE_INVALID", "parent finalization requires a clean or failed-closed phase")
        state_file, initial = self.load(parent_run_dir)
        if str(initial.get("record_kind") or "") != "parent":
            raise StateError("PARENT_RECORD_REQUIRED", "finalization requires a parent record")
        if str(initial.get("phase") or "") == "USER_STOP_REQUESTED":
            raise StateError("PARENT_USER_STOP_CONFIRMATION_REQUIRED", "generic parent finalization is forbidden during user stop")
        root = canonical_project_root(initial["project_root"])
        paths = self.paths(root, str(initial["run_id"]))
        with exclusive_state_lock(paths.parent_transition_lock):
            record = read_json(state_file)
            lock = read_json(paths.lock_file)
            if (
                str(lock.get("parent_run_id") or lock.get("run_id") or "") != str(record["run_id"])
                or str(lock.get("lease_nonce") or "") != str(record["lease_nonce"])
                or str(lock.get("manifest_sha256") or "") != str(record["manifest_sha256"])
            ):
                raise StateError("BLOCKED_OWNER_MISMATCH", "parent finalization lease is not exact")
            current = str(record.get("phase") or "")
            if current not in {"PARENT_ACTIVE", "PARENT_RECOVERY_REQUIRED", "PARENT_DRAINING"}:
                raise StateError("PARENT_PHASE_INVALID", f"cannot finalize parent from {current}")
            now = utc_now()
            if current != "PARENT_DRAINING":
                record["phase_events"].append({"from": current, "to": "PARENT_DRAINING", "at": now})
                record["phase"] = "PARENT_DRAINING"
                record["phase_at"] = now
                record["updated_at"] = now
                lock["phase"] = "PARENT_DRAINING"
                lock["heartbeat_at"] = now
                write_json_atomic(state_file, record)
                write_json_atomic(paths.lock_file, lock)

            children = self._parent_children(paths.runs_dir, str(record["run_id"]))
            unresolved: list[dict[str, Any]] = []
            cleanup_pending: list[dict[str, Any]] = []
            failed: list[dict[str, Any]] = []
            summaries: list[dict[str, Any]] = []
            for child_state, child in children:
                child_phase = str(child.get("phase") or "")
                claim_exists = (child_state.parent / "send.claim").exists()
                identity_exists = bool(child.get("current_target_id") or child.get("conversation_url"))
                summary = {
                    "run_id": child.get("run_id"),
                    "stage_id": child.get("stage_id"),
                    "phase": child_phase,
                    "send_attempt_count": int(child.get("send_attempt_count") or 0),
                    "cleanup_pending": bool(child.get("cleanup_pending")),
                    "owned_open_tabs": int(child.get("owned_open_tabs") or 0),
                }
                summaries.append(summary)
                if (
                    child_phase in UNCERTAIN_OR_SUBMITTED_PHASES
                    or child_phase not in CHILD_SAFE_TERMINAL_PHASES
                    or (claim_exists and int(child.get("send_attempt_count") or 0) == 0)
                ):
                    unresolved.append(summary)
                    continue
                if child_phase == "COMPLETE" and identity_exists and str(child.get("owned_tab_state") or "") not in {"closed-and-absent", "already-absent"}:
                    cleanup_pending.append(summary)
                if bool(child.get("cleanup_pending")) or int(child.get("owned_open_tabs") or 0) != 0:
                    cleanup_pending.append(summary)
                if child_phase != "COMPLETE":
                    failed.append(summary)

            if unresolved or cleanup_pending:
                recovery_at = utc_now()
                record["phase_events"].append({"from": "PARENT_DRAINING", "to": "PARENT_RECOVERY_REQUIRED", "at": recovery_at})
                record["phase"] = "PARENT_RECOVERY_REQUIRED"
                record["phase_at"] = recovery_at
                record["updated_at"] = recovery_at
                record["child_scan"] = summaries
                record["failure"] = {
                    "code": "PARENT_CHILDREN_UNRESOLVED_OR_CLEANUP_PENDING",
                    "unresolved": unresolved,
                    "cleanup_pending": cleanup_pending,
                }
                lock["phase"] = "PARENT_RECOVERY_REQUIRED"
                lock["heartbeat_at"] = recovery_at
                write_json_atomic(state_file, record)
                write_json_atomic(paths.lock_file, lock)
                return record

            terminal_phase = phase
            if phase == "PARENT_COMPLETE" and failed:
                terminal_phase = "PARENT_FAILED_CLOSED"
                failure = failure or {"code": "PARENT_CHILD_STAGE_FAILED", "children": failed}
            if terminal_phase == "PARENT_COMPLETE" and result is None:
                raise StateError("PARENT_RESULT_MISSING", "clean parent completion requires a result descriptor")
            terminal_at = utc_now()
            record["phase_events"].append({"from": "PARENT_DRAINING", "to": terminal_phase, "at": terminal_at})
            record["phase"] = terminal_phase
            record["phase_at"] = terminal_at
            record["updated_at"] = terminal_at
            record["child_scan"] = summaries
            record["result"] = result
            record["failure"] = failure
            record["owned_open_tabs"] = 0
            lock["phase"] = terminal_phase
            lock["heartbeat_at"] = terminal_at
            write_json_atomic(state_file, record)
            write_json_atomic(paths.lock_file, lock)
            latest_lock = read_json(paths.lock_file)
            if (
                str(latest_lock.get("parent_run_id") or latest_lock.get("run_id") or "") == str(record["run_id"])
                and str(latest_lock.get("lease_nonce") or "") == str(record["lease_nonce"])
            ):
                paths.lock_file.unlink()
        return record

    def create_run(
        self,
        *,
        project_root: str | os.PathLike[str],
        manifest_path: str | os.PathLike[str],
        agbrowse_contract: dict[str, Any],
        owner_pid: int | None = None,
    ) -> dict[str, Any]:
        root = canonical_project_root(project_root)
        manifest_file = Path(manifest_path).expanduser().resolve()
        if not manifest_file.is_file():
            raise StateError("MANIFEST_MISSING", f"manifest file missing: {manifest_file}")
        manifest = load_manifest(manifest_file)
        manifest_hash = sha256_file(manifest_file)
        prompt = prompt_contract(manifest)
        prompt_hash = str(prompt["prompt_sha256"])
        run_id = uuid.uuid4().hex
        paths = self.paths(root, run_id)
        paths.project_dir.mkdir(parents=True, exist_ok=True)

        existing_lock = self._read_existing_lock(paths.lock_file)
        existing_records = self._active_or_uncertain_records(paths.runs_dir)
        if existing_lock or existing_records:
            diagnosis = self.reconcile_project_lock(root, apply_safe_pre_submission=False)
            code = str(diagnosis.get("state") or "SAME_PROJECT_ACTIVE_OR_UNCERTAIN")
            if code not in {
                "ACTIVE_PROJECT_OWNER",
                "STALE_PRE_SUBMISSION_SAFE_TO_CANCEL",
                "STALE_DUPLICATE_COMPLETE_OWNER_SAFE_TO_SETTLE",
                "STALE_OWNER_UNRESOLVED_SUBMISSION",
                "STALE_OWNER_PRE_SUBMIT_TARGET_PRESENT",
                "PROJECT_LOCK_STATE_AMBIGUOUS",
                "TERMINAL_ORPHAN_LOCK_DETECTED",
            }:
                code = "SAME_PROJECT_ACTIVE_OR_UNCERTAIN"
            raise StateError(
                code,
                "same project already has a verified active, uncertain, or ambiguous run",
                {
                    "diagnosis": diagnosis,
                    "supported_reconcile_command": (
                        f'python "{Path(__file__).resolve().with_name("chatgpt_agbrowse_run.py")}" '
                        f'--reconcile-project-lock "{root}"'
                        if code in {
                            "STALE_PRE_SUBMISSION_SAFE_TO_CANCEL",
                            "STALE_DUPLICATE_COMPLETE_OWNER_SAFE_TO_SETTLE",
                        }
                        else None
                    ),
                    "lock": existing_lock,
                    "records": existing_records[:10],
                },
            )

        identity = process_identity(owner_pid)
        nonce = uuid.uuid4().hex
        epoch = int(time.time_ns())
        created = utc_now()
        lock = {
            "schema": SCHEMA,
            "record_kind": "standalone",
            "run_id": run_id,
            "project_root": str(root),
            "project_key": project_key(root),
            "manifest_sha256": manifest_hash,
            "owner": {**identity, "nonce": nonce, "epoch": epoch},
            "phase": "CREATED",
            "session_id": None,
            "target_id": None,
            "conversation_url": None,
            "heartbeat_at": created,
        }
        try:
            fd = os.open(paths.lock_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(lock, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
        except FileExistsError as exc:
            raise StateError("SAME_PROJECT_ACTIVE_OR_UNCERTAIN", "project lock appeared during dispatch") from exc

        recovery_identity: dict[str, Any] | None = None
        if prompt.get("transport") == "file":
            source = Path(str(prompt["prompt_file"]))
            alias_name = recovery_prompt_alias_name(run_id, manifest)
            alias_path = paths.run_dir / alias_name
            alias_path.parent.mkdir(parents=True, exist_ok=True)
            prompt_bytes = source.read_bytes()
            alias_path.write_bytes(prompt_bytes)
            alias_hash = sha256_file(alias_path)
            if alias_hash != prompt_hash:
                try:
                    paths.lock_file.unlink()
                except OSError:
                    pass
                raise StateError(
                    "RECOVERY_PROMPT_ALIAS_HASH_MISMATCH",
                    "run-owned recovery prompt alias does not match the immutable prompt",
                    {"expected": prompt_hash, "actual": alias_hash},
                )
            recovery_identity = {
                "schema": "codex.chatgpt.recovery-identity/v1",
                "token": run_id,
                "attachment_name": alias_name,
                "attachment_path": str(alias_path),
                "attachment_sha256": alias_hash,
                "source_prompt_path": str(source),
            }

        record = {
            "schema": SCHEMA,
            "record_kind": "standalone",
            "run_id": run_id,
            "project_root": str(root),
            "project_key": project_key(root),
            "manifest_path": str(manifest_file),
            "manifest_sha256": manifest_hash,
            "prompt_sha256": prompt_hash,
            "recovery_identity": recovery_identity,
            "requested": _requested_contract(manifest),
            "agbrowse": dict(agbrowse_contract),
            "owner": {**identity, "nonce": nonce, "epoch": epoch},
            "created_at": created,
            "updated_at": created,
            "phase": "CREATED",
            "phase_at": created,
            "session_id": None,
            "current_target_id": None,
            "conversation_url": None,
            "submission_receipt": None,
            "result": None,
            "terminal_block_code": None,
            "recovery_count": 0,
            "phase_events": [{"from": None, "to": "CREATED", "at": created}],
            "target_rebind_events": [],
            "recovery_events": [],
            "app_evidence_refs": [],
            "selection_evidence_refs": [],
        }
        try:
            write_json_atomic(paths.state_file, record)
        except Exception:
            try:
                paths.lock_file.unlink()
            except OSError:
                pass
            raise
        return {**record, "run_dir": str(paths.run_dir), "state_file": str(paths.state_file)}

    def load(self, run_dir: str | os.PathLike[str]) -> tuple[Path, dict[str, Any]]:
        path = Path(run_dir).expanduser().resolve()
        state_file = path if path.name == "run.json" else path / "run.json"
        record = read_json(state_file)
        if record.get("schema") != SCHEMA or not REQUIRED_IMMUTABLE.issubset(record):
            raise StateError("STATE_SCHEMA_INVALID", f"invalid agbrowse run state: {state_file}")
        return state_file, record

    def verify_manifest(self, record: dict[str, Any]) -> None:
        path = Path(str(record["manifest_path"]))
        actual = sha256_file(path) if path.is_file() else None
        if actual != record["manifest_sha256"]:
            raise StateError(
                "BLOCKED_MANIFEST_MISMATCH",
                "manifest changed after run creation",
                {"expected": record["manifest_sha256"], "actual": actual, "path": str(path)},
            )
        manifest = load_manifest(path)
        if str(record.get("record_kind") or "standalone") == "parent":
            try:
                family = classify_parent_family(record)
                if family is None:
                    raise StateError("PARENT_FAMILY_INVALID", "parent family is not registered")
                validate_parent_family_manifest(family, manifest)
            except StateError as exc:
                raise StateError(
                    "BLOCKED_MANIFEST_MISMATCH",
                    "parent workflow manifest contract changed after creation",
                    {"cause": exc.code, **exc.evidence},
                ) from exc
            if str(manifest.get("workflow_id") or "") != str(record.get("workflow_id") or ""):
                raise StateError("BLOCKED_MANIFEST_MISMATCH", "parent workflow binding changed after creation")
            return
        try:
            contract = prompt_contract(manifest)
        except StateError as exc:
            raise StateError(
                "BLOCKED_MANIFEST_MISMATCH",
                "prompt file contract changed after run creation",
                {"cause": exc.code, **exc.evidence},
            ) from exc
        if contract["prompt_sha256"] != record["prompt_sha256"]:
            raise StateError(
                "BLOCKED_MANIFEST_MISMATCH",
                "prompt file changed after run creation",
                {
                    "expected": record["prompt_sha256"],
                    "actual": contract["prompt_sha256"],
                    "path": contract.get("prompt_file"),
                },
            )
        recovery_identity = record.get("recovery_identity")
        if recovery_identity:
            expected_names = accepted_recovery_prompt_alias_names(str(record["run_id"]), manifest)
            alias_path = Path(str(recovery_identity.get("attachment_path") or ""))
            try:
                alias_path = alias_path.expanduser().resolve(strict=True)
            except (OSError, RuntimeError, ValueError) as exc:
                raise StateError(
                    "BLOCKED_MANIFEST_MISMATCH",
                    "run-owned recovery prompt alias is unavailable",
                ) from exc
            if (
                recovery_identity.get("schema") != "codex.chatgpt.recovery-identity/v1"
                or str(recovery_identity.get("token") or "") != str(record["run_id"])
                or str(recovery_identity.get("attachment_name") or "") not in expected_names
                or alias_path.name not in expected_names
                or not alias_path.is_file()
                or alias_path.is_symlink()
                or sha256_file(alias_path) != record["prompt_sha256"]
                or str(recovery_identity.get("attachment_sha256") or "") != record["prompt_sha256"]
            ):
                raise StateError(
                    "BLOCKED_MANIFEST_MISMATCH",
                    "run-owned recovery prompt alias identity or bytes changed",
                    {"path": str(alias_path), "expected_sha256": record["prompt_sha256"]},
                )

    def _verify_lock(self, state_file: Path, record: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
        lock_file = state_file.parent.parent.parent / "active.lock"
        lock = read_json(lock_file)
        kind = str(record.get("record_kind") or "standalone")
        if kind == "child":
            if (
                str(lock.get("record_kind") or "") != "parent"
                or str(lock.get("parent_run_id") or lock.get("run_id") or "") != str(record.get("parent_run_id") or "")
                or str(lock.get("lease_nonce") or "") != str(record.get("parent_lease_nonce") or "")
                or str(lock.get("workflow_id") or "") != str(record.get("parent_workflow_id") or "")
            ):
                raise StateError("BLOCKED_OWNER_MISMATCH", "child does not belong to the exact active parent lease")
            return lock_file, lock
        if kind == "parent":
            if (
                str(lock.get("parent_run_id") or lock.get("run_id") or "") != str(record.get("run_id") or "")
                or str(lock.get("lease_nonce") or "") != str(record.get("lease_nonce") or "")
                or str(lock.get("workflow_id") or "") != str(record.get("workflow_id") or "")
            ):
                raise StateError("BLOCKED_OWNER_MISMATCH", "project parent lease does not match parent record")
            return lock_file, lock
        owner = record["owner"]
        if (
            lock.get("run_id") != record.get("run_id")
            or lock.get("manifest_sha256") != record.get("manifest_sha256")
            or lock.get("owner", {}).get("nonce") != owner.get("nonce")
            or lock.get("owner", {}).get("epoch") != owner.get("epoch")
        ):
            raise StateError("BLOCKED_OWNER_MISMATCH", "project lease does not match run owner")
        return lock_file, lock

    def transition(
        self,
        run_dir: str | os.PathLike[str],
        phase: str,
        *,
        session_id: str | None = None,
        target_id: str | None = None,
        conversation_url: str | None = None,
        submission_receipt: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
        block_code: str | None = None,
        recovery_event: dict[str, Any] | None = None,
        rebind_reason: str | None = None,
        app_evidence_ref: str | None = None,
        selection_evidence_ref: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if phase not in PHASES:
            raise StateError("PHASE_INVALID", f"unknown phase: {phase}")
        state_file, record = self.load(run_dir)
        self.verify_manifest(record)
        lock_file, lock = self._verify_lock(state_file, record)
        current = str(record["phase"])
        if phase != current and phase not in ALLOWED_TRANSITIONS.get(current, set()):
            raise StateError("PHASE_TRANSITION_INVALID", f"cannot transition {current} -> {phase}")
        if current in {"SUBMISSION_UNCERTAIN_IDENTITY_MISSING", "BLOCKED_RECOVERY_EXHAUSTED", "RECOVERING"} and phase == "SEND_REJECTED":
            event_kind = str((recovery_event or {}).get("kind") or "")
            user_attested = event_kind == "explicit-user-attested-no-submission"
            if record.get("session_id") or record.get("conversation_url") or (not user_attested and event_kind != "verified-mutation-disallowed-reclassification"):
                raise StateError(
                    "UNCERTAIN_RECLASSIFICATION_UNPROVEN",
                    "uncertain or exhausted recovery can be reclassified only with verified mutationAllowed=false evidence and no identity",
                )
            if user_attested:
                try:
                    evidence_path = Path(str((recovery_event or {}).get("evidence_path") or "")).expanduser().resolve(strict=True)
                    evidence_path.relative_to(state_file.parent)
                    evidence = read_json(evidence_path)
                except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
                    raise StateError("USER_ATTESTED_NO_SUBMISSION_EVIDENCE_INVALID", "settlement needs immutable in-run evidence") from exc
                if not ((recovery_event or {}).get("explicit_user_request") is True and str((recovery_event or {}).get("evidence_sha256") or "") == sha256_file(evidence_path) and evidence.get("schema") == "codex.chatgpt.user-attested-no-submission/v1" and evidence.get("explicit_user_request") is True and evidence.get("run_id") == record.get("run_id") and evidence.get("project_root") == record.get("project_root")):
                    raise StateError("USER_ATTESTED_NO_SUBMISSION_EVIDENCE_INVALID", "settlement evidence does not bind this exact run")
        if (
            current == "SEND_REJECTED"
            and phase == "PREFLIGHTED"
            and str(record.get("record_kind") or "standalone") == "child"
            and (state_file.parent / "send.claim").exists()
        ):
            authority = record.get("pre_submit_retry_authority")
            if not isinstance(authority, dict) or authority.get("eligible") is not True or authority.get("consumed_at") is not None:
                raise StateError(
                    "PRE_SUBMIT_RETRY_AUTHORITY_MISSING",
                    "claimed SEND_REJECTED child requires unconsumed exact retry authority",
                )
        if current == "RECOVERY_REQUIRED" and phase == "SEND_REJECTED":
            event = recovery_event or {}
            required = {
                "kind": "verified-mutation-disallowed-reclassification",
                "mutation_allowed": False,
                "send_click_status": "unresolved",
                "send_click_reason": "not-enabled",
                "assistant_count": 0,
                "session_id": record.get("session_id"),
                "target_id": record.get("current_target_id"),
            }
            mismatches = {
                key: {"actual": event.get(key), "expected": value}
                for key, value in required.items()
                if event.get(key) != value
            }
            session_status = str(event.get("session_status") or "")
            observed_url = str(event.get("observed_url") or "")
            uncommitted_sent_session = bool(
                session_status == "sent"
                and event.get("target_absent") is True
                and (
                    event.get("session_deadline_wait_required") is False
                    or event.get("session_deadline_expired") is True
                )
            )
            evidence_error: str | None = None
            try:
                evidence_path = Path(str(event.get("evidence_path") or "")).expanduser().resolve(strict=True)
                evidence_path.relative_to(state_file.parent)
                if not evidence_path.is_file() or evidence_path.is_symlink():
                    evidence_error = "evidence-not-regular"
                elif sha256_file(evidence_path) != str(event.get("evidence_sha256") or ""):
                    evidence_error = "evidence-hash-mismatch"
            except (OSError, RuntimeError, ValueError):
                evidence_error = "evidence-path-invalid"
            if (
                mismatches
                or (session_status not in {"complete", "timeout"} and not uncommitted_sent_session)
                or record.get("conversation_url")
                or not record.get("session_id")
                or not record.get("current_target_id")
                or not observed_url.startswith("https://chatgpt.com/")
                or CANONICAL_CHAT_RE.fullmatch(observed_url)
                or evidence_error is not None
            ):
                raise StateError(
                    "RECOVERY_RECLASSIFICATION_UNPROVEN",
                    "recovery-required run can be reclassified only when the exact session proves the send click never mutated",
                    {
                        "mismatches": mismatches,
                        "session_status": session_status,
                        "observed_url": observed_url,
                        "evidence_error": evidence_error,
                    },
                )
        if phase == "PROVIDER_FAILED_TERMINAL":
            event = recovery_event or {}
            evidence_error: str | None = None
            try:
                evidence_path = Path(str(event.get("answer_path") or "")).expanduser().resolve(strict=True)
                evidence_path.relative_to(state_file.parent)
                if not evidence_path.is_file() or evidence_path.is_symlink():
                    evidence_error = "evidence-not-regular"
                elif sha256_file(evidence_path) != str(event.get("answer_sha256") or ""):
                    evidence_error = "evidence-hash-mismatch"
                elif evidence_path.stat().st_size != int(event.get("answer_bytes") or -1):
                    evidence_error = "evidence-size-mismatch"
            except (OSError, RuntimeError, TypeError, ValueError):
                evidence_error = "evidence-path-invalid"
            if (
                str(event.get("kind") or "") != "provider-terminal-error-ui"
                or str(event.get("signature") or "") != "chatgpt-stream-error-retry-v1"
                or str(event.get("provider_status") or "").lower()
                not in {"complete", "completed", "done", "response_ready", "history-adjudicated-terminal"}
                or not record.get("session_id")
                or not record.get("current_target_id")
                or not record.get("conversation_url")
                or record.get("result") is not None
                or evidence_error is not None
            ):
                raise StateError(
                    "PROVIDER_TERMINAL_FAILURE_UNPROVEN",
                    "provider terminal failure requires exact identity and immutable provider-error answer evidence",
                    {"evidence_error": evidence_error},
                )

        now = utc_now()
        if session_id:
            existing_session = record.get("session_id")
            if existing_session and existing_session != session_id:
                raise StateError("SESSION_ID_IMMUTABLE", "session_id cannot change")
            record["session_id"] = session_id
        if conversation_url is not None:
            url = canonical_conversation_url(conversation_url)
            existing_url = record.get("conversation_url")
            if existing_url and existing_url != url:
                raise StateError("CONVERSATION_URL_IMMUTABLE", "canonical conversation URL cannot change")
            record["conversation_url"] = url
        if phase == "URL_BOUND" and not record.get("conversation_url"):
            raise StateError("CONVERSATION_IDENTITY_MISSING", "URL_BOUND requires canonical conversation URL")
        if phase in {"SUBMITTED", "URL_BOUND", "RESPONSE_IN_PROGRESS"} and not (session_id or record.get("session_id")):
            raise StateError("SESSION_ID_MISSING", f"{phase} requires session_id")

        if target_id:
            previous = record.get("current_target_id")
            if previous and previous != target_id:
                pre_submit_retry = bool(
                    current == "LEASED"
                    and not record.get("session_id")
                    and not record.get("conversation_url")
                    and rebind_reason == "pre-submit-composer-retry"
                )
                recovery_event_kind = str((recovery_event or {}).get("kind") or "")
                recovery_rebind = bool(
                    current == "RECOVERING"
                    and rebind_reason
                    and (
                        record.get("conversation_url")
                        or recovery_event_kind == "history-fingerprint-match"
                    )
                )
                if not (pre_submit_retry or recovery_rebind):
                    raise StateError(
                        "TARGET_REBIND_UNAUTHORIZED",
                        "target change requires a proven pre-submit retry or RECOVERING with exact URL and reason",
                    )
                record["target_rebind_events"].append(
                    {
                        "at": now,
                        "old_target_id": previous,
                        "new_target_id": target_id,
                        "conversation_url": record["conversation_url"],
                        "reason": rebind_reason,
                    }
                )
            record["current_target_id"] = target_id

        if submission_receipt is not None:
            if record.get("submission_receipt") not in (None, submission_receipt):
                raise StateError("SUBMISSION_RECEIPT_IMMUTABLE", "submission receipt cannot be rewritten")
            record["submission_receipt"] = submission_receipt
        if phase == "SUBMISSION_UNCERTAIN_IDENTITY_MISSING" and record.get("conversation_url"):
            raise StateError("UNCERTAIN_PHASE_INVALID", "identity-missing block cannot carry canonical URL")
        if recovery_event is not None:
            record["recovery_events"].append({"at": now, **recovery_event})
            record["recovery_count"] = int(record.get("recovery_count") or 0) + 1
        if app_evidence_ref:
            record["app_evidence_refs"].append({"at": now, "ref": app_evidence_ref})
        if selection_evidence_ref is not None:
            if not isinstance(selection_evidence_ref, dict):
                raise StateError("SELECTION_EVIDENCE_REF_INVALID", "selection evidence ref must be an object")
            path = Path(str(selection_evidence_ref.get("path") or ""))
            expected_sha256 = str(selection_evidence_ref.get("sha256") or "")
            try:
                resolved = path.expanduser().resolve(strict=True)
                resolved.relative_to(state_file.parent)
            except (OSError, RuntimeError, ValueError) as exc:
                raise StateError("SELECTION_EVIDENCE_REF_INVALID", "selection evidence path is outside the run") from exc
            if (
                not resolved.is_file()
                or resolved.is_symlink()
                or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)
                or sha256_file(resolved) != expected_sha256
            ):
                raise StateError("SELECTION_EVIDENCE_REF_INVALID", "selection evidence hash is invalid")
            refs = record.setdefault("selection_evidence_refs", [])
            candidate = {
                "at": now,
                "kind": str(selection_evidence_ref.get("kind") or "selection"),
                "path": str(resolved),
                "sha256": expected_sha256,
                "target_id": str(selection_evidence_ref.get("target_id") or record.get("current_target_id") or ""),
            }
            if any(
                str(item.get("path") or "") == candidate["path"]
                and str(item.get("sha256") or "") != candidate["sha256"]
                for item in refs
                if isinstance(item, dict)
            ):
                raise StateError("SELECTION_EVIDENCE_REF_CONFLICT", "selection evidence path changed hash")
            refs.append(candidate)
        if result is not None:
            if record.get("result") not in (None, result):
                raise StateError("RESULT_IMMUTABLE", "captured result descriptor cannot be rewritten")
            record["result"] = result
        if phase == "COMPLETE" and not record.get("result"):
            raise StateError("COMPLETION_EVIDENCE_MISSING", "COMPLETE requires result descriptor")

        if phase == "SEND_STARTED" and str(record.get("record_kind") or "standalone") == "child":
            claim_file = state_file.parent / "send.claim"
            if not claim_file.is_file() or claim_file.is_symlink():
                raise StateError("CHILD_SEND_CLAIM_MISSING", "child SEND_STARTED requires its durable O_EXCL send claim")
            claim = read_json(claim_file)
            parallel_child = str(record.get("parent_family") or "") == "parallel-implementation"
            expected_schema = "codex.chatgpt.child-send-claim/v2" if parallel_child else "codex.chatgpt.child-send-claim/v1"
            invalid_claim = bool(
                claim.get("schema") != expected_schema
                or str(claim.get("run_id") or "") != str(record.get("run_id") or "")
                or str(claim.get("parent_run_id") or "") != str(record.get("parent_run_id") or "")
                or str(claim.get("manifest_sha256") or "") != str(record.get("manifest_sha256") or "")
                or str(claim.get("prompt_sha256") or "") != str(record.get("prompt_sha256") or "")
                or not str(claim.get("claimed_at") or "")
            )
            if parallel_child:
                invalid_claim = invalid_claim or any(
                    str(claim.get(key) or "") != str(record.get(key) or "")
                    for key in ("component_id", "unit_id", "attempt_id", "input_base_oid", "topology_receipt_sha256")
                )
                digest_payload = {key: value for key, value in claim.items() if key != "send_claim_sha256"}
                expected_digest = sha256_bytes(
                    json.dumps(digest_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
                )
                invalid_claim = invalid_claim or str(claim.get("send_claim_sha256") or "") != expected_digest
            else:
                invalid_claim = invalid_claim or bool(
                    str(claim.get("parent_workflow_id") or "") != str(record.get("parent_workflow_id") or "")
                    or str(claim.get("parent_lease_nonce") or "") != str(record.get("parent_lease_nonce") or "")
                    or str(claim.get("project_root") or "") != str(record.get("project_root") or "")
                    or str(claim.get("project_key") or "") != str(record.get("project_key") or "")
                    or str(claim.get("stage_id") or "") != str(record.get("stage_id") or "")
                    or str(claim.get("role") or "") != str(record.get("role") or "")
                    or claim.get("lane") != record.get("lane")
                    or claim.get("iteration") != record.get("iteration")
                    or claim.get("send_limit") != record.get("send_limit")
                )
            if invalid_claim:
                raise StateError("CHILD_SEND_CLAIM_INVALID", "child send claim identity is not exact")
            authority = record.get("pre_submit_retry_authority")
            events = record.get("recovery_events") if isinstance(record.get("recovery_events"), list) else []
            latest = events[-1] if events and isinstance(events[-1], dict) else {}
            target_id = str(record.get("current_target_id") or "")
            retired_claim_reuse = bool(
                isinstance(authority, dict)
                and not authority.get("replacement_target_id")
                and str(authority.get("retired_replacement_target_id") or "") == target_id
                and str(latest.get("kind") or "") == "stale-pre-submit-retry-replacement-retired"
                and str(latest.get("target_id") or "") == target_id
                and str(latest.get("send_claim_sha256") or "") == str(authority.get("claim_sha256") or "")
                and not record.get("session_id") and not record.get("conversation_url")
                and record.get("submission_receipt") is None and record.get("result") is None
            )
            reuse_claim = bool(
                int(record.get("send_attempt_count") or 0) == 1
                and isinstance(authority, dict)
                and authority.get("eligible") is True
                and authority.get("consumed_at") is None
                and str(authority.get("claim_sha256") or "") == sha256_file(claim_file)
                and str(authority.get("run_id") or "") == str(record.get("run_id") or "")
                and str(authority.get("parent_run_id") or "") == str(record.get("parent_run_id") or "")
                and (
                    str(authority.get("replacement_target_id") or authority.get("cleanup_target_id") or "") == target_id
                    or retired_claim_reuse
                )
            )
            if reuse_claim:
                authority = dict(authority)
                authority["consumed_at"] = now
                record["pre_submit_retry_authority"] = authority
                record["pre_submit_retry_count"] = int(record.get("pre_submit_retry_count") or 0) + 1
            else:
                attempts = int(record.get("send_attempt_count") or 0) + 1
                if attempts > int(record.get("send_limit") or 1):
                    raise StateError("SEND_ALREADY_ATTEMPTED", "child send attempt exceeds its immutable limit")
                record["send_attempt_count"] = attempts
            record["send_claim"] = {
                "path": str(claim_file),
                "sha256": sha256_file(claim_file),
                "claimed_at": claim.get("claimed_at"),
            }

        if phase != current:
            record["phase_events"].append({"from": current, "to": phase, "at": now})
        record["phase"] = phase
        record["phase_at"] = now
        record["updated_at"] = now
        if phase.startswith("BLOCKED_") or phase in {
            "SUBMISSION_UNCERTAIN_IDENTITY_MISSING",
            "PROVIDER_FAILED_TERMINAL",
        }:
            record["terminal_block_code"] = block_code or phase
        else:
            record["terminal_block_code"] = None

        write_json_atomic(state_file, record)
        if str(record.get("record_kind") or "standalone") != "child":
            lock.update(
                {
                    "phase": phase,
                    "session_id": record.get("session_id"),
                    "target_id": record.get("current_target_id"),
                    "conversation_url": record.get("conversation_url"),
                    "heartbeat_at": now,
                }
            )
            write_json_atomic(lock_file, lock)
        if (
            str(record.get("record_kind") or "standalone") != "child"
            and phase in {
                "COMPLETE",
                "PROVIDER_FAILED_TERMINAL",
                "CANCELLED_PRE_SUBMISSION",
                "ABANDONED_UNCERTAIN",
            }
        ):
            current_lock = read_json(lock_file)
            if current_lock.get("run_id") == record["run_id"] and current_lock.get("owner", {}).get("nonce") == record["owner"]["nonce"]:
                lock_file.unlink()
        return record

    def list_project_runs(self, project_root: str | os.PathLike[str]) -> list[dict[str, Any]]:
        root = canonical_project_root(project_root)
        paths = self.paths(root, "unused")
        rows: list[dict[str, Any]] = []
        if not paths.runs_dir.exists():
            return rows
        for path in sorted(paths.runs_dir.glob("*/run.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            record = read_json(path)
            rows.append({"run_id": record.get("run_id"), "phase": record.get("phase"), "run_dir": str(path.parent)})
        return rows


def _json_arg(value: str | None) -> dict[str, Any] | None:
    if value is None:
        return None
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise StateError("ARGUMENT_INVALID", "JSON argument must be an object")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Immutable project/session state for the agbrowse ChatGPT bridge.")
    parser.add_argument("--state-root", type=Path)
    sub = parser.add_subparsers(dest="command", required=True)

    start = sub.add_parser("start")
    start.add_argument("--project-root", required=True)
    start.add_argument("--manifest", required=True)
    start.add_argument("--contract", required=True)

    show = sub.add_parser("show")
    show.add_argument("--run", required=True)

    trans = sub.add_parser("transition")
    trans.add_argument("--run", required=True)
    trans.add_argument("--phase", required=True, choices=sorted(PHASES))
    trans.add_argument("--session-id")
    trans.add_argument("--target-id")
    trans.add_argument("--conversation-url")
    trans.add_argument("--submission-receipt-json")
    trans.add_argument("--result-json")
    trans.add_argument("--block-code")
    trans.add_argument("--recovery-event-json")
    trans.add_argument("--rebind-reason")
    trans.add_argument("--app-evidence-ref")

    ls = sub.add_parser("list")
    ls.add_argument("--project-root", required=True)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    store = RunStore(args.state_root)
    try:
        if args.command == "start":
            contract = read_json(Path(args.contract))
            result = store.create_run(project_root=args.project_root, manifest_path=args.manifest, agbrowse_contract=contract)
        elif args.command == "show":
            _, result = store.load(args.run)
        elif args.command == "transition":
            result = store.transition(
                args.run,
                args.phase,
                session_id=args.session_id,
                target_id=args.target_id,
                conversation_url=args.conversation_url,
                submission_receipt=_json_arg(args.submission_receipt_json),
                result=_json_arg(args.result_json),
                block_code=args.block_code,
                recovery_event=_json_arg(args.recovery_event_json),
                rebind_reason=args.rebind_reason,
                app_evidence_ref=args.app_evidence_ref,
            )
        else:
            result = {"ok": True, "runs": store.list_project_runs(args.project_root)}
        print(json.dumps({"ok": True, "result": result}, ensure_ascii=False, indent=2))
        return 0
    except StateError as exc:
        print(json.dumps(exc.envelope(), ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
