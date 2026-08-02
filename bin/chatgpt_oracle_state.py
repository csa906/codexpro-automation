from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

SCHEMA = "codex.chatgpt.oracle-run/v1"
DEVSPACE_APP_NAME = "DevSpace"
STATE_SCHEMA = "codex.chatgpt.oracle-run-state/v1"
STATUSES = {"prepared", "running", "complete", "failed", "attention_required", "abandoned"}
# One bounded lifecycle vocabulary.  The stored `status` values above remain the
# on-disk wire format for compatibility, but every consumer and report should
# reason about these four states instead of the historical five statuses times
# five authorities times terminal_harvested combinations.  That combinatorial
# space is what produced "nothing is running yet everything is locked".
LIFECYCLE_STATES = ("running", "complete", "needs_attention", "abandoned")
_STATUS_TO_LIFECYCLE = {
    "prepared": "running",
    "running": "running",
    "complete": "complete",
    "failed": "needs_attention",
    "attention_required": "needs_attention",
    "abandoned": "abandoned",
}
SESSION_AUTHORITY_RANK = {
    "pre_submit": 0,
    "submitted_unknown": 1,
    "live": 2,
    "terminal_observed": 3,
    "terminal": 4,
}
WAIT_OBJECT_0 = 0
WAIT_ABANDONED = 0x80
WAIT_TIMEOUT = 0x102
CREATE_NO_WINDOW = 0x08000000
BLOCKED_OPTIONS = {
    "-f", "--file", "--files", "--path", "--paths", "--include", "-p",
    "--prompt", "--message", "--write-output", "--slug", "-e", "--engine",
    "--mode", "--browser-model-strategy", "--browser-follow-up", "--followup",
    "--dry-run", "--render", "--render-markdown", "--copy",
}
BLOCKED_COMMANDS = {"restart", "session", "status", "serve", "tui"}
SAFE_ORACLE_SWITCHES = {
    "--no-notify",
    "--notify",
    "--no-notify-sound",
    "--notify-sound",
    "--verbose",
    "--browser-hide-window",
}
SAFE_ORACLE_VALUE_OPTIONS = {
    "--heartbeat",
    "--timeout",
    "--zombie-timeout",
    # Oracle 0.16.1 is compatibility-patched so this is one overall answer
    # budget, including fallback capture.  The host also enforces the same
    # wall-clock deadline with a short grace if CDP evaluation itself wedges.
    "--browser-timeout",
    "--browser-recheck-timeout",
}
# Overall answer budget for a heavy non-Pro run.
DEFAULT_BROWSER_ANSWER_TIMEOUT = "90m"
DEFAULT_BROWSER_ANSWER_CEILING_MINUTES = 90
HOST_WATCHDOG_GRACE_SECONDS = 30
ORACLE_DUPLICATE_PROMPT_RE = re.compile(
    r'A session with the same prompt is already running '
    r'\((?P<locator>oracle-[a-z0-9-]+)\)\.\s*'
    r'Reattach with "oracle session (?P=locator)" or rerun with --force to start another run\.',
    re.IGNORECASE,
)
ORACLE_NO_SESSION_RE = re.compile(
    r"No session found with ID\s+(?P<locator>oracle-[a-z0-9-]+)\.?",
    re.IGNORECASE,
)
ORACLE_PROMPT_NOT_OBSERVED_MARKER = (
    "Prompt did not appear in conversation before timeout (send may have failed)"
)
ORACLE_ATTACHMENT_SIZE_PREFLIGHT_MARKER = "The following files exceed the 1 MB limit:"
ORACLE_ATTACHMENT_SIZE_LIMIT_BYTES = 1024 * 1024
ORACLE_NO_LIVE_TAB_MARKER = "No live ChatGPT tab matched session"
ORACLE_NO_RECOVERABLE_URL_MARKER = (
    "session metadata has no recoverable ChatGPT conversation URL"
)
USER_CONFIRMED_NO_SUBMISSION = "user-confirmed-no-submission"
ORACLE_RECOVERY_STATE_RE = re.compile(r"(?im)^\s*State:\s*[a-z][a-z0-9_-]*\s*$")
# Upstream Oracle copies a signed-in browser profile with rsync.  On POSIX
# hosts without rsync the copy fails after launch, so feasibility is decided
# while loading the manifest instead of crashing mid-launch.  The pinned
# `oracle-compat/0.16.1/profileCopy.patch` replaces that spawn with Node's
# built-in recursive copy on Windows, so `nt` needs no external dependency.
# Checking PATH there would drop per-run profile isolation and block every
# parallel Web Multi lane, which is the exact failure this guard must avoid.
PROFILE_COPY_DEPENDENCY = "rsync"
PROFILE_COPY_NATIVE_PLATFORMS = ("nt",)


def profile_copy_is_supported(
    *, which_runner: Any = None, platform_name: str | None = None
) -> bool:
    """Report whether Oracle can actually copy a signed-in browser profile."""
    platform = os.name if platform_name is None else platform_name
    if platform in PROFILE_COPY_NATIVE_PLATFORMS:
        return True
    resolver = shutil.which if which_runner is None else which_runner
    return bool(resolver(PROFILE_COPY_DEPENDENCY))
APP_RE = re.compile(r"^[^\r\n]+$")
MODEL_RE = re.compile(r"^[a-zA-Z0-9._ -]+$")
PARENT_ID_RE = re.compile(r"^[a-f0-9]{32,64}$")
RUN_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{7,95}$")
_THREAD_MUTEXES: dict[str, threading.Lock] = {}
_THREAD_MUTEXES_GUARD = threading.Lock()


class OracleStateError(RuntimeError):
    def __init__(self, code: str, message: str, evidence: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.evidence = evidence or {}

    def envelope(self) -> dict[str, Any]:
        return {"ok": False, "error": {"code": self.code, "message": str(self), "evidence": self.evidence}}


@dataclass(frozen=True)
class OracleConfig:
    project_root: Path
    mission_path: Path
    mission_sha256: str
    app_name: str | None
    mode: str
    transport: str
    attachments: tuple[Path, ...]
    attachment_sha256s: tuple[str, ...]
    run_root: Path
    oracle_command: tuple[str, ...]
    oracle_args: tuple[str, ...]
    submit_mutex_timeout_seconds: float
    model: str
    model_strategy: str
    thinking_time: str
    copy_profile: Path | None
    research: str
    archive: str
    task_outcome_contract: str
    parallel_parent_id: str | None
    requested_run_id: str | None


@dataclass(frozen=True)
class RunLayout:
    run_id: str
    slug: str
    run_dir: Path
    state_path: Path
    output_path: Path
    transcript_path: Path
    stdout_path: Path
    stderr_path: Path
    browser_temp_path: Path


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_utf8_strict(path: Path) -> str:
    try:
        return path.read_bytes().decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise OracleStateError("UTF8_REQUIRED", "file must be valid UTF-8", {"path": str(path), "offset": exc.start}) from exc
    except OSError as exc:
        raise OracleStateError("FILE_READ_FAILED", "file could not be read", {"path": str(path)}) from exc


def absolute_path(value: Any, *, label: str, must_exist: bool) -> Path:
    raw = Path(str(value or "")).expanduser()
    if not raw.is_absolute():
        raise OracleStateError(f"{label.upper()}_ABSOLUTE_REQUIRED", f"{label} must be an absolute path", {"path": str(raw)})
    try:
        return raw.resolve(strict=must_exist)
    except OSError as exc:
        raise OracleStateError(f"{label.upper()}_INVALID", f"{label} could not be resolved", {"path": str(raw)}) from exc


def exact_regular_file(value: Any, *, label: str) -> Path:
    raw = Path(str(value or "")).expanduser()
    code_prefix = label.upper()
    file_code = "MISSION_FILE_INVALID" if label == "mission_path" else f"{code_prefix}_FILE_INVALID"
    if not raw.is_absolute():
        raise OracleStateError(f"{code_prefix}_ABSOLUTE_REQUIRED", f"{label} must be an absolute path", {"path": str(raw)})
    if raw.is_symlink():
        raise OracleStateError(file_code, f"{label} must not be a symlink", {"path": str(raw)})
    path = absolute_path(raw, label=label, must_exist=True)
    if not path.is_file():
        raise OracleStateError(file_code, f"{label} must identify a regular file", {"path": str(path)})
    return path


def is_within(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def oracle_state_root() -> Path:
    override = str(os.environ.get("CODEX_ORACLE_STATE_ROOT") or "").strip()
    return Path(override).expanduser().resolve() if override else (Path.home() / ".codex" / "state" / "chatgpt-oracle").resolve()


def default_oracle_command(platform_name: str | None = None) -> tuple[str, ...]:
    platform = os.name if platform_name is None else platform_name
    return ("npx.cmd" if platform == "nt" else "npx", "-y", "@steipete/oracle")


def validate_oracle_command(values: Any) -> tuple[str, ...]:
    if not isinstance(values, list) or not values or not all(isinstance(item, str) and item for item in values):
        raise OracleStateError("ORACLE_COMMAND_INVALID", "oracle_command must be a nonempty list of strings")
    command = tuple(values)
    executable = Path(command[0]).name.casefold()
    if executable in {"oracle", "oracle.cmd", "oracle.exe"} and len(command) == 1:
        return command
    if executable in {"npx", "npx.cmd", "npx.exe"} and command[1:] in {
        ("-y", "@steipete/oracle"),
        ("--yes", "@steipete/oracle"),
        ("@steipete/oracle",),
    }:
        return command
    raise OracleStateError(
        "ORACLE_COMMAND_FORBIDDEN",
        "oracle_command must resolve directly to Oracle or npx @steipete/oracle",
        {"command": command_for_display(command)},
    )


def validate_oracle_args(values: Any) -> tuple[str, ...]:
    if values is None:
        return ()
    if not isinstance(values, list) or not all(isinstance(item, str) and item for item in values):
        raise OracleStateError("ORACLE_ARGS_INVALID", "oracle_args must be a list of nonempty strings")
    index = 0
    while index < len(values):
        item = values[index]
        option, separator, inline_value = item.partition("=")
        if option in SAFE_ORACLE_SWITCHES and not separator:
            index += 1
            continue
        if option in SAFE_ORACLE_VALUE_OPTIONS:
            if separator:
                if not inline_value:
                    raise OracleStateError("ORACLE_ARG_VALUE_MISSING", "safe Oracle option requires a value", {"argument": item})
                index += 1
                continue
            if index + 1 >= len(values) or values[index + 1].startswith("-"):
                raise OracleStateError("ORACLE_ARG_VALUE_MISSING", "safe Oracle option requires a value", {"argument": item})
            index += 2
            continue
        raise OracleStateError(
            "ORACLE_ARG_FORBIDDEN",
            "oracle_args accepts only bounded timing, heartbeat, verbosity, and notification options",
            {"argument": item},
        )
    return tuple(values)


def load_manifest(path: Path, *, platform_name: str | None = None) -> OracleConfig:
    manifest_path = absolute_path(path, label="manifest_path", must_exist=True)
    try:
        payload = json.loads(read_utf8_strict(manifest_path))
    except json.JSONDecodeError as exc:
        raise OracleStateError("MANIFEST_JSON_INVALID", "manifest must contain one JSON object", {"line": exc.lineno, "column": exc.colno}) from exc
    if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
        raise OracleStateError("MANIFEST_SCHEMA_INVALID", f"manifest schema must be {SCHEMA}")
    project_root = absolute_path(payload.get("project_root"), label="project_root", must_exist=True)
    if not project_root.is_dir():
        raise OracleStateError("PROJECT_ROOT_NOT_DIRECTORY", "project_root must identify a directory")
    mission_path = exact_regular_file(payload.get("mission_path"), label="mission_path")
    read_utf8_strict(mission_path)
    mode = str(payload.get("mode") or "browser").strip().casefold()
    if mode != "browser":
        raise OracleStateError("MODE_INVALID", "Oracle foundation runner supports mode=browser only")
    transport = str(payload.get("transport") or "devspace").strip().casefold()
    if transport not in {"devspace", "pro-attachment-only"}:
        raise OracleStateError("TRANSPORT_INVALID", "transport must be devspace or pro-attachment-only")
    app_name_raw = str(payload.get("app_name") or "").strip().lstrip("@").strip()
    if transport == "devspace":
        if not is_within(project_root, mission_path):
            raise OracleStateError("MISSION_OUTSIDE_PROJECT", "mission_path must stay inside project_root")
        if not app_name_raw or APP_RE.fullmatch(app_name_raw) is None:
            raise OracleStateError("APP_NAME_INVALID", "app_name must be one nonempty line")
        if app_name_raw != DEVSPACE_APP_NAME:
            raise OracleStateError(
                "DEVSPACE_APP_REQUIRED",
                f"new non-Pro Oracle runs require the exact app name {DEVSPACE_APP_NAME}",
                {"app_name": app_name_raw},
            )
        app_name: str | None = app_name_raw
        if payload.get("attachments"):
            raise OracleStateError("REGULAR_ATTACHMENTS_FORBIDDEN", "DevSpace runs must not attach files")
        attachments: tuple[Path, ...] = ()
    else:
        if app_name_raw:
            raise OracleStateError("PRO_APP_FORBIDDEN", "Pro attachment-only runs must not name an app")
        app_name = None
        raw_attachments = payload.get("attachments")
        if not isinstance(raw_attachments, list) or not raw_attachments:
            raise OracleStateError("PRO_ATTACHMENTS_REQUIRED", "Pro requires one or more exact attachment files")
        attachments = tuple(
            exact_regular_file(value, label=f"attachment_{index}")
            for index, value in enumerate(raw_attachments)
        )
        if len(set(attachments)) != len(attachments):
            raise OracleStateError("PRO_ATTACHMENTS_DUPLICATE", "Pro attachment paths must be unique")
        if mission_path not in attachments:
            raise OracleStateError("PRO_MISSION_ATTACHMENT_REQUIRED", "mission_path must be one of the Pro attachments")
    state_root = oracle_state_root()
    if is_within(project_root, state_root) or is_within(state_root, project_root):
        raise OracleStateError(
            "HOST_STATE_OVERLAPS_PROJECT",
            "Oracle host state must be disjoint from the DevSpace-writable project",
        )
    project_key = hashlib.sha256(str(project_root).casefold().encode("utf-8")).hexdigest()[:24]
    run_root = absolute_path(payload.get("run_root") or (state_root / "projects" / project_key / "runs"), label="run_root", must_exist=False)
    if not is_within(state_root, run_root):
        raise OracleStateError("RUN_ROOT_OUTSIDE_HOST_STATE", "run_root must stay inside the host-only Oracle state root")
    command_value = payload.get("oracle_command")
    if command_value is None:
        oracle_command = default_oracle_command(platform_name)
    else:
        oracle_command = validate_oracle_command(command_value)
    try:
        timeout = float(payload.get("submit_mutex_timeout_seconds", 30))
    except (TypeError, ValueError) as exc:
        raise OracleStateError("MUTEX_TIMEOUT_INVALID", "submit_mutex_timeout_seconds must be numeric") from exc
    if not 0 < timeout <= 300:
        raise OracleStateError("MUTEX_TIMEOUT_INVALID", "submit_mutex_timeout_seconds must be within 0..300")
    model = str(payload.get("model") or "gpt-5.6").strip()
    if not model or MODEL_RE.fullmatch(model) is None:
        raise OracleStateError("MODEL_INVALID", "model must be one safe Oracle browser model label")
    model_strategy = str(payload.get("model_strategy") or "select").strip().casefold()
    if model_strategy not in {"select", "current", "ignore"}:
        raise OracleStateError("MODEL_STRATEGY_INVALID", "model_strategy must be select, current, or ignore")
    thinking_time = str(payload.get("thinking_time") or "heavy").strip().casefold()
    if thinking_time not in {"light", "standard", "extended", "heavy"}:
        raise OracleStateError(
            "THINKING_TIME_INVALID",
            "thinking_time must be light, standard, extended, or heavy",
        )
    if transport == "pro-attachment-only":
        if model.casefold() != "gpt-5.5-pro":
            raise OracleStateError(
                "PRO_MODEL_INVALID",
                "Pro attachment-only runs require Oracle's current Pro alias gpt-5.5-pro; no downgrade is allowed",
                {"model": model},
            )
        if model_strategy != "select":
            raise OracleStateError("PRO_MODEL_STRATEGY_INVALID", "Pro requires explicit model selection")
        if thinking_time != "heavy":
            raise OracleStateError("PRO_THINKING_TIME_INVALID", "Pro requires heavy reasoning")
    copy_profile_raw = str(payload.get("copy_profile") or "").strip()
    if copy_profile_raw:
        copy_profile = absolute_path(copy_profile_raw, label="copy_profile", must_exist=True)
    else:
        # The manually signed-in Oracle profile is the immutable seed for a
        # throwaway per-run copy.  This prevents different projects from
        # sharing one Chrome process and closing each other's live work.
        profile_override = str(os.environ.get("ORACLE_BROWSER_PROFILE_DIR") or "").strip()
        default_profile = Path(profile_override).expanduser().resolve() if profile_override else (
            Path.home() / ".oracle" / "browser-profile"
        ).resolve()
        copy_profile = default_profile if default_profile.is_dir() else None
    if copy_profile is not None:
        if not copy_profile.is_dir():
            raise OracleStateError("COPY_PROFILE_NOT_DIRECTORY", "copy_profile must identify a directory")
        if is_within(project_root, copy_profile) or is_within(copy_profile, project_root):
            raise OracleStateError("COPY_PROFILE_OVERLAPS_PROJECT", "copy_profile must be outside the DevSpace project")
        if not profile_copy_is_supported(platform_name=platform_name):
            # Without the copy dependency Oracle aborts after launch, so every
            # run failed before reaching the composer.  Fall back to the
            # signed-in profile directly instead of forcing that failure.
            if copy_profile_raw:
                raise OracleStateError(
                    "COPY_PROFILE_DEPENDENCY_MISSING",
                    f"copy_profile requires {PROFILE_COPY_DEPENDENCY} on PATH; "
                    "install it or omit copy_profile to reuse the signed-in profile",
                    {"dependency": PROFILE_COPY_DEPENDENCY, "copy_profile": str(copy_profile)},
                )
            copy_profile = None
    research = str(payload.get("research") or "off").strip().casefold()
    if research not in {"off", "deep"}:
        raise OracleStateError("RESEARCH_INVALID", "research must be off or deep")
    if transport == "pro-attachment-only" and research != "off":
        raise OracleStateError("PRO_RESEARCH_FORBIDDEN", "Pro attachment-only runs do not enable research mode")
    archive = str(payload.get("archive") or "auto").strip().casefold()
    if archive not in {"auto", "always", "never"}:
        raise OracleStateError("ARCHIVE_INVALID", "archive must be auto, always, or never")
    task_outcome_contract = str(payload.get("task_outcome_contract") or "legacy").strip().casefold()
    if task_outcome_contract not in {"legacy", "v1"}:
        raise OracleStateError(
            "TASK_OUTCOME_CONTRACT_INVALID",
            "task_outcome_contract must be legacy or v1",
        )
    if transport == "pro-attachment-only" and task_outcome_contract != "legacy":
        raise OracleStateError(
            "PRO_TASK_OUTCOME_CONTRACT_FORBIDDEN",
            "Pro attachment-only output is not wrapped in the DevSpace task outcome contract",
        )
    parallel_parent_raw = str(payload.get("parallel_parent_id") or "").strip().casefold()
    parallel_parent_id = parallel_parent_raw or None
    if parallel_parent_id is not None and PARENT_ID_RE.fullmatch(parallel_parent_id) is None:
        raise OracleStateError("PARALLEL_PARENT_ID_INVALID", "parallel_parent_id must be 32-64 lowercase hex characters")
    requested_run_id = str(payload.get("run_id") or "").strip() or None
    if requested_run_id is not None and RUN_ID_RE.fullmatch(requested_run_id) is None:
        raise OracleStateError("RUN_ID_INVALID", "run_id must be a safe 8-96 character identifier")
    return OracleConfig(
        project_root,
        mission_path,
        sha256_file(mission_path),
        app_name,
        mode,
        transport,
        attachments,
        tuple(sha256_file(item) for item in attachments),
        run_root,
        oracle_command,
        validate_oracle_args(payload.get("oracle_args")),
        timeout,
        model,
        model_strategy,
        thinking_time,
        copy_profile,
        research,
        archive,
        task_outcome_contract,
        parallel_parent_id,
        requested_run_id,
    )


def composer_prompt(config: OracleConfig, mission_path: Path | None = None) -> str:
    if config.transport == "pro-attachment-only":
        identity_material = "\0".join((
            str(config.project_root).casefold(),
            config.mission_sha256,
            *config.attachment_sha256s,
        ))
        identity = hashlib.sha256(identity_material.encode("utf-8")).hexdigest()[:24]
        return (
            "Read the attached prompt/instructions and all attached files, then complete the task. "
            f"Task identity: oracle-pro-{identity}."
        )
    effective_path = config.mission_path if mission_path is None else mission_path
    # Keep the Windows npx.cmd prompt in one argument line. A literal newline
    # truncates the prompt after the app mention before Oracle receives it.
    return (
        f"@{config.app_name} {effective_path} 파일을 읽고 끝까지 수행하세요. "
        "그 파일에 기록된 정확한 프로젝트 루트만 사용하고 적용되는 AGENTS.md를 먼저 끝까지 읽으세요. "
        "작업공간 열기가 시간 초과되면 동일한 정확한 루트만 한 번 재시도하며 상위·하위·현재 활성 "
        "작업공간이나 셸 경계 우회로 대체하지 마세요."
        + (
            " 마지막 줄에 실제 작업 수행 결과를 TASK_OUTCOME: EXECUTED, "
            "TASK_OUTCOME: NOT_EXECUTED, TASK_OUTCOME: BLOCKED 중 하나로 정확히 기록하세요."
            if config.task_outcome_contract == "v1"
            else ""
        )
    )


def create_layout(config: OracleConfig, *, run_id: str | None = None) -> RunLayout:
    actual = run_id or f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:12]}"
    project_words = (re.findall(r"[a-z0-9]+", config.project_root.name.casefold()) or ["project"])[:3]
    project_token = "-".join(word[:10] for word in project_words)
    # Oracle accepts 3-5 words and normalizes every word to its first ten
    # characters. Generate that exact locator up front so recovery never
    # stores an alias that Oracle cannot resolve later.
    run_token = actual.rsplit("-", 1)[-1][:10]
    slug = f"oracle-{project_token}-{run_token}"
    run_dir = config.run_root / actual
    return RunLayout(
        actual,
        slug,
        run_dir,
        run_dir / "state.json",
        run_dir / "output.md",
        run_dir / "transcript.md",
        run_dir / "stdout.log",
        run_dir / "stderr.log",
        run_dir / "browser-temp",
    )


def state_payload(config: OracleConfig, layout: RunLayout, *, status: str, resolved_version: str, exit_code: int | None = None) -> dict[str, Any]:
    return {
        "schema": STATE_SCHEMA, "run_id": layout.run_id, "project_root": str(config.project_root),
        "mode": config.mode, "transport": config.transport, "app_name": config.app_name,
        "profile": {
            "model": config.model,
            "model_strategy": config.model_strategy,
            "thinking_time": config.thinking_time,
            "copy_profile": str(config.copy_profile) if config.copy_profile else None,
            "research": config.research,
            "archive": config.archive,
        },
        "parallel_parent_id": config.parallel_parent_id,
        "transport_status": "prepared",
        "task_outcome_contract": config.task_outcome_contract,
        "task_outcome": "not_applicable" if config.transport == "pro-attachment-only" else "pending",
        "task_outcome_reason": None,
        "mission": {
            "path": str(config.mission_path),
            "transport_path": str(layout.run_dir / "mission.md"),
            "sha256": config.mission_sha256,
        },
        "attachments": [
            {"path": str(path), "sha256": digest, "size_bytes": path.stat().st_size}
            for path, digest in zip(config.attachments, config.attachment_sha256s, strict=True)
        ],
        "oracle": {
            "resolved_version": resolved_version,
            "command": list(config.oracle_command),
            "slug": layout.slug,
            "session_locator": layout.slug,
        },
        "artifacts": {
            "output": str(layout.output_path),
            "transcript": str(layout.transcript_path),
            "stdout": str(layout.stdout_path),
            "stderr": str(layout.stderr_path),
            "browser_temp": str(layout.browser_temp_path),
        },
        "status": status,
        "exit_code": exit_code,
        "session_authority": "pre_submit",
        "terminal_harvested": False,
        "artifact_sha256": None,
    }


def host_uptime_ms(*, platform_name: str | None = None) -> int:
    platform = os.name if platform_name is None else platform_name
    if platform == "nt":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetTickCount64.restype = ctypes.c_ulonglong
        return int(kernel32.GetTickCount64())
    return int(time.monotonic() * 1000)


def browser_temp_environment(
    browser_temp_path: Path,
    *,
    platform_name: str | None = None,
    base_env: dict[str, str] | None = None,
) -> dict[str, str]:
    root = browser_temp_path.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    marker = {
        "schema": "codex.chatgpt.oracle-browser-temp-owner/v1",
        "controller_pid": os.getpid(),
        "host_uptime_ms": host_uptime_ms(platform_name=platform_name),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    write_json_atomic(root / ".owner.json", marker)
    env = dict(os.environ if base_env is None else base_env)
    value = str(root)
    env.update({"TEMP": value, "TMP": value, "TMPDIR": value})
    return env


def cleanup_owned_browser_temp(browser_temp_path: Path) -> bool:
    root = browser_temp_path.expanduser().resolve()
    if not root.exists():
        return True
    marker = root / ".owner.json"
    if not marker.is_file():
        return False
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if payload.get("schema") != "codex.chatgpt.oracle-browser-temp-owner/v1":
        return False
    try:
        shutil.rmtree(root)
    except OSError:
        return False
    return not root.exists()


def cleanup_prior_boot_browser_temps(
    run_root: Path,
    *,
    platform_name: str | None = None,
    current_uptime_ms: int | None = None,
) -> list[str]:
    root = run_root.expanduser().resolve()
    if not root.is_dir():
        return []
    now_uptime = host_uptime_ms(platform_name=platform_name) if current_uptime_ms is None else int(current_uptime_ms)
    cleaned: list[str] = []
    for run_dir in sorted((item for item in root.iterdir() if item.is_dir()), key=lambda item: item.name):
        browser_temp = run_dir / "browser-temp"
        marker = browser_temp / ".owner.json"
        if not marker.is_file():
            continue
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
            owner_uptime = int(payload["host_uptime_ms"])
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            continue
        # GetTickCount/monotonic reset on reboot. Only a prior-boot owner is
        # eligible here; same-boot crashes remain preserved for exact recovery.
        if now_uptime >= owner_uptime:
            continue
        if cleanup_owned_browser_temp(browser_temp):
            cleaned.append(str(browser_temp))
    return cleaned


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def load_state(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(read_utf8_strict(absolute_path(path, label="state_path", must_exist=True)))
    except json.JSONDecodeError as exc:
        raise OracleStateError("STATE_JSON_INVALID", "state file is not valid JSON") from exc
    if not isinstance(payload, dict) or payload.get("schema") != STATE_SCHEMA:
        raise OracleStateError("STATE_SCHEMA_INVALID", f"state schema must be {STATE_SCHEMA}")
    return payload


def update_state(
    state_path: Path,
    *,
    status: str,
    resolved_version: str | None = None,
    exit_code: int | None = None,
    session_authority: str | None = None,
    terminal_harvested: bool | None = None,
    artifact_sha256: str | None = None,
    transport_status: str | None = None,
    task_outcome: str | None = None,
    task_outcome_reason: str | None = None,
    host_watchdog: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if status not in STATUSES:
        raise OracleStateError("STATUS_INVALID", "invalid Oracle run status")
    payload = load_state(state_path)
    payload["status"] = status
    payload["exit_code"] = exit_code
    if resolved_version is not None:
        payload["oracle"]["resolved_version"] = resolved_version
    if session_authority is not None:
        current_authority = str(payload.get("session_authority") or "")
        current_rank = SESSION_AUTHORITY_RANK.get(current_authority, -1)
        requested_rank = SESSION_AUTHORITY_RANK.get(session_authority, -1)
        payload["session_authority"] = (
            current_authority if current_rank > requested_rank else session_authority
        )
        if current_rank > requested_rank and status == "running":
            payload["status"] = (
                "complete"
                if current_authority == "terminal" and payload.get("terminal_harvested") is True
                else "attention_required"
            )
    if terminal_harvested is not None:
        payload["terminal_harvested"] = terminal_harvested
    if artifact_sha256 is not None:
        payload["artifact_sha256"] = artifact_sha256
    if transport_status is not None:
        payload["transport_status"] = transport_status
    if task_outcome is not None:
        payload["task_outcome"] = task_outcome
    if task_outcome_reason is not None:
        payload["task_outcome_reason"] = task_outcome_reason
    if host_watchdog is not None:
        payload["host_watchdog"] = host_watchdog
    write_json_atomic(state_path, payload)
    return payload


def output_is_nonempty(path: Path) -> bool:
    try:
        return bool(path.read_bytes().strip())
    except OSError:
        return False


def _state_has_conversation_url(state: dict[str, Any]) -> bool:
    """Recognize only explicit persisted conversation URL fields."""
    url_keys = {"conversation_url", "conversationUrl", "canonical_url", "canonicalUrl"}

    def walk(value: Any) -> bool:
        if isinstance(value, dict):
            for key, nested in value.items():
                if key in url_keys and str(nested or "").strip():
                    return True
                if isinstance(nested, (dict, list)) and walk(nested):
                    return True
        elif isinstance(value, list):
            return any(walk(item) for item in value)
        return False

    return walk(state)


def _artifact_bytes(state: dict[str, Any], name: str) -> tuple[Path, bytes] | None:
    artifacts = state.get("artifacts") if isinstance(state.get("artifacts"), dict) else {}
    raw = str(artifacts.get(name) or "").strip()
    if not raw:
        return None
    path = Path(raw)
    try:
        return path, path.read_bytes()
    except OSError:
        return None


def _user_confirmable_no_submission_evidence(state_path: Path) -> dict[str, Any] | None:
    """Return exact evidence for a user-adjudicable Oracle composer timeout.

    The Oracle message is not mechanical proof of non-submission.  This helper
    only proves that the run is eligible for an explicit user adjudication: no
    output or conversation URL exists, Oracle reported that the prompt was not
    observed, and exact recovery has neither a live tab nor a saved URL.
    """
    state = load_state(state_path)
    authority = str(state.get("session_authority") or "")
    if authority not in {"submitted_unknown", "pre_submit"}:
        return None
    if state.get("terminal_harvested") is True or _state_has_conversation_url(state):
        return None
    run_dir = state_path.parent
    artifacts = state.get("artifacts") if isinstance(state.get("artifacts"), dict) else {}
    output = Path(str(artifacts.get("output") or ""))
    if output.resolve() != (run_dir / "output.md").resolve() or output.is_symlink():
        return None
    if str(output) and output_is_nonempty(output):
        return None
    stdout_record = _artifact_bytes(state, "stdout")
    stderr_record = _artifact_bytes(state, "stderr")
    if stdout_record is None or stderr_record is None:
        return None
    stdout_path, stdout_bytes = stdout_record
    stderr_path, stderr_bytes = stderr_record
    if (
        stdout_path.resolve() != (run_dir / "stdout.log").resolve()
        or stderr_path.resolve() != (run_dir / "stderr.log").resolve()
        or stdout_path.is_symlink()
        or stderr_path.is_symlink()
    ):
        return None
    try:
        stdout_text = stdout_bytes.decode("utf-8", errors="strict")
        stderr_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return None
    oracle = state.get("oracle") if isinstance(state.get("oracle"), dict) else {}
    locator = str(oracle.get("session_locator") or oracle.get("slug") or "").strip()
    if not locator or ORACLE_PROMPT_NOT_OBSERVED_MARKER not in stdout_text:
        return None
    if f"Session: {locator}" not in stdout_text:
        return None
    mission = state.get("mission") if isinstance(state.get("mission"), dict) else {}
    transport_path = Path(str(mission.get("transport_path") or ""))
    if transport_path.resolve() != (run_dir / "mission.md").resolve() or transport_path.is_symlink():
        return None
    try:
        mission_bytes = transport_path.read_bytes()
        mission_text = mission_bytes.decode("utf-8", errors="strict")
    except (OSError, UnicodeDecodeError):
        return None
    mission_sha256 = hashlib.sha256(mission_bytes).hexdigest()
    if mission_sha256 != str(mission.get("sha256") or ""):
        return None
    host_marker = "[HOST_STAGE_CONTRACT]"
    workspace_marker = "[DEVSPACE_WORKSPACE_ENTRY_CONTRACT]"
    if mission_text.count(host_marker) != 1 or mission_text.count(workspace_marker) != 1:
        return None
    host_start = mission_text.index(host_marker) + len(host_marker)
    workspace_start = mission_text.index(workspace_marker)
    if workspace_start <= host_start:
        return None
    host_contract = mission_text[host_start:workspace_start]
    binding: dict[str, str] = {}
    for key, pattern in {
        "workflow_id": (
            r"(?m)^workflow_id=((?:[a-f0-9]{32,64}|"
            r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}))\r?$"
        ),
        "stage": r"(?m)^stage=([a-z][a-z0-9-]*)\r?$",
        "attempt_id": r"(?m)^attempt_id=([a-f0-9]{32,64})\r?$",
        "input_mission_sha256": r"(?m)^input_mission_sha256=([a-f0-9]{64})\r?$",
    }.items():
        matches = re.findall(pattern, host_contract)
        if len(matches) != 1:
            return None
        binding[key] = matches[0]
    if binding["attempt_id"] != str(state.get("run_id") or ""):
        return None
    expected_parent = hashlib.sha256(binding["workflow_id"].encode("utf-8")).hexdigest()
    if str(state.get("parallel_parent_id") or "") != expected_parent:
        return None
    contract_paths: dict[str, str] = {}
    for key, pattern in {
        "project_root": r"(?m)^exact_project_root=([^\r\n]+)\r?$",
        "input_mission": r"(?m)^exact_input_mission_path=([^\r\n]+)\r?$",
        "receipt": r"(?m)^Write the small UTF-8 stage receipt to: ([^\r\n]+)\r?$",
    }.items():
        matches = re.findall(pattern, host_contract)
        if len(matches) != 1:
            return None
        contract_paths[key] = matches[0]
    try:
        project_root = Path(str(state.get("project_root") or ""))
        contract_project_root = Path(contract_paths["project_root"])
        if (
            not project_root.is_absolute()
            or not contract_project_root.is_absolute()
            or project_root.resolve(strict=True) != contract_project_root.resolve(strict=True)
            or not project_root.resolve(strict=True).is_dir()
        ):
            return None
        canonical_root = project_root.resolve(strict=True)
        source_mission = Path(str(mission.get("path") or ""))
        input_mission = Path(contract_paths["input_mission"])
        receipt_path = Path(contract_paths["receipt"])
        if (
            not source_mission.is_absolute()
            or source_mission.is_symlink()
            or not input_mission.is_absolute()
            or input_mission.is_symlink()
            or not receipt_path.is_absolute()
            or receipt_path.is_symlink()
        ):
            return None
        source_mission = source_mission.resolve(strict=True)
        input_mission = input_mission.resolve(strict=True)
        receipt_path = receipt_path.resolve(strict=False)
        if (
            not source_mission.is_file()
            or not input_mission.is_file()
            or not is_within(canonical_root, source_mission)
            or not is_within(canonical_root, input_mission)
            or not is_within(canonical_root, receipt_path)
            or receipt_path != source_mission.parent / "stage-result.json"
            or source_mission.read_bytes() != mission_bytes
            or sha256_file(input_mission) != binding["input_mission_sha256"]
        ):
            return None
    except OSError:
        return None
    recovery_records: list[dict[str, str]] = []
    for recovery_stdout in sorted(run_dir.glob("recovery-*-stdout.log"), key=lambda item: item.name):
        recovery_stderr = recovery_stdout.with_name(
            recovery_stdout.name.replace("-stdout.log", "-stderr.log")
        )
        try:
            if recovery_stdout.is_symlink() or recovery_stderr.is_symlink():
                continue
            recovery_stdout_bytes = recovery_stdout.read_bytes()
            recovery_stderr_bytes = recovery_stderr.read_bytes()
            combined = b"\n".join((recovery_stdout_bytes, recovery_stderr_bytes))
            recovery_text = combined.decode("utf-8", errors="strict")
        except (OSError, UnicodeDecodeError):
            return None
        if ORACLE_RECOVERY_STATE_RE.search(recovery_text):
            return None
        if (
            ORACLE_NO_LIVE_TAB_MARKER not in recovery_text
            or f'"{locator}"' not in recovery_text
            or ORACLE_NO_RECOVERABLE_URL_MARKER not in recovery_text
        ):
            return None
        recovery_records.append({
            "stdout_name": recovery_stdout.name,
            "stdout_sha256": hashlib.sha256(recovery_stdout_bytes).hexdigest(),
            "stderr_name": recovery_stderr.name,
            "stderr_sha256": hashlib.sha256(recovery_stderr_bytes).hexdigest(),
        })
    if not recovery_records:
        return None
    return {
        "project_root": str(state.get("project_root") or ""),
        "run_id": str(state.get("run_id") or ""),
        **binding,
        "mission_sha256": mission_sha256,
        "oracle_locator": locator,
        "stdout_sha256": hashlib.sha256(stdout_bytes).hexdigest(),
        "stderr_sha256": hashlib.sha256(stderr_bytes).hexdigest(),
        "recovery_evidence": recovery_records,
        "output_absent": True,
        "conversation_url_absent": True,
        "_augmented_mission_path": str(source_mission),
        "_input_mission_path": str(input_mission),
        "_receipt_path": str(receipt_path),
    }


def proven_user_confirmed_no_submission(state_path: Path) -> dict[str, Any] | None:
    """Revalidate a persisted user confirmation against immutable run artifacts."""
    state = load_state(state_path)
    reference = state.get("user_confirmed_no_submission")
    if not isinstance(reference, dict):
        return None
    expected_path = state_path.parent / "user-confirmed-no-submission.json"
    if (
        reference.get("schema") != "codex.chatgpt.oracle-settlement-reference/v1"
        or Path(str(reference.get("path") or "")).resolve() != expected_path.resolve()
        or expected_path.is_symlink()
    ):
        return None
    try:
        artifact_bytes = expected_path.read_bytes()
        if hashlib.sha256(artifact_bytes).hexdigest() != str(reference.get("sha256") or ""):
            return None
        recorded = json.loads(artifact_bytes.decode("utf-8", errors="strict"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if (
        recorded.get("schema") != "codex.chatgpt.oracle-user-confirmed-no-submission/v1"
        or recorded.get("code") != "ORACLE_USER_CONFIRMED_NO_SUBMISSION"
        or recorded.get("confirmation") != USER_CONFIRMED_NO_SUBMISSION
        or not str(recorded.get("reason") or "").strip()
    ):
        return None
    current = _user_confirmable_no_submission_evidence(state_path)
    if current is None:
        return None
    for key in (
        "project_root",
        "run_id",
        "workflow_id",
        "stage",
        "attempt_id",
        "input_mission_sha256",
        "mission_sha256",
        "oracle_locator",
        "stdout_sha256",
        "stderr_sha256",
        "recovery_evidence",
        "output_absent",
        "conversation_url_absent",
    ):
        if recorded.get(key) != current.get(key):
            return None
    return {
        **recorded,
        "_augmented_mission_path": current["_augmented_mission_path"],
        "_input_mission_path": current["_input_mission_path"],
        "_receipt_path": current["_receipt_path"],
    }


def settle_user_confirmed_no_submission(
    state_path: Path,
    *,
    confirmation: str,
    reason: str,
) -> dict[str, Any]:
    """Release one ambiguous send only after explicit user adjudication.

    Mechanical evidence remains fail-closed: it merely makes the run eligible.
    The exact confirmation token is the authority that resolves non-submission.
    """
    if confirmation.strip().casefold() != USER_CONFIRMED_NO_SUBMISSION:
        raise OracleStateError(
            "NO_SUBMISSION_CONFIRMATION_REQUIRED",
            f"confirmation must be exactly {USER_CONFIRMED_NO_SUBMISSION}",
        )
    normalized_reason = reason.strip()
    if not normalized_reason:
        raise OracleStateError("NO_SUBMISSION_REASON_REQUIRED", "confirmation reason is required")
    payload = load_state(state_path)
    existing = proven_user_confirmed_no_submission(state_path)
    if existing is not None:
        return payload
    if str(payload.get("session_authority") or "") != "submitted_unknown":
        raise OracleStateError(
            "NO_SUBMISSION_AUTHORITY_INVALID",
            "only a submitted_unknown run may be adjudicated as not submitted",
        )
    evidence = _user_confirmable_no_submission_evidence(state_path)
    if evidence is None:
        raise OracleStateError(
            "NO_SUBMISSION_EVIDENCE_INCOMPLETE",
            "run lacks the exact prompt-timeout and recovery-binding evidence required for user adjudication",
        )
    recorded = {
        "schema": "codex.chatgpt.oracle-user-confirmed-no-submission/v1",
        "code": "ORACLE_USER_CONFIRMED_NO_SUBMISSION",
        "confirmation": USER_CONFIRMED_NO_SUBMISSION,
        "reason": normalized_reason,
        **{key: value for key, value in evidence.items() if not key.startswith("_")},
    }
    settlement_path = state_path.parent / "user-confirmed-no-submission.json"
    write_json_atomic(settlement_path, recorded)
    settlement_sha256 = sha256_file(settlement_path)
    payload.update({
        "status": "attention_required",
        "exit_code": int(payload.get("exit_code") or 1),
        "session_authority": "pre_submit",
        "terminal_harvested": False,
        "artifact_sha256": None,
        "transport_status": "not_submitted_user_confirmed",
        "task_outcome": "pending",
        "task_outcome_reason": "user-confirmed-no-submission-after-prompt-timeout",
        "user_confirmed_no_submission": {
            "schema": "codex.chatgpt.oracle-settlement-reference/v1",
            "path": str(settlement_path),
            "sha256": settlement_sha256,
        },
    })
    write_json_atomic(state_path, payload)
    return payload


def proven_pre_submit_rejection(state_path: Path) -> dict[str, Any] | None:
    """Return immutable evidence only for Oracle's own pre-submit prompt dedup rejection."""
    state = load_state(state_path)
    artifacts = state.get("artifacts") if isinstance(state.get("artifacts"), dict) else {}
    output = Path(str(artifacts.get("output") or ""))
    if str(output) and output_is_nonempty(output):
        return None
    stdout = Path(str(artifacts.get("stdout") or ""))
    try:
        stdout_bytes = stdout.read_bytes()
        stdout_text = stdout_bytes.decode("utf-8", errors="strict")
    except (OSError, UnicodeDecodeError):
        return None
    match = ORACLE_DUPLICATE_PROMPT_RE.search(stdout_text)
    if match is None:
        return None
    return {
        "schema": "codex.chatgpt.oracle-pre-submit-rejection/v1",
        "code": "ORACLE_GLOBAL_PROMPT_DUPLICATE",
        "oracle_locator": match.group("locator"),
        "stdout_sha256": hashlib.sha256(stdout_bytes).hexdigest(),
        "output_absent": True,
    }


def proven_pre_submit_host_failure(state_path: Path) -> dict[str, Any] | None:
    """Prove a host failure happened before Oracle/browser launch.

    `execute_run` emits the version-resolution prefix itself before the Oracle
    process is created.  The additional immutable-state checks keep this from
    reclassifying a real submitted or live session.
    """
    state = load_state(state_path)
    authority = str(state.get("session_authority") or "")
    if authority not in {"pre_submit", "submitted_unknown"}:
        return None
    oracle = state.get("oracle") if isinstance(state.get("oracle"), dict) else {}
    if str(oracle.get("resolved_version") or "") != "unresolved":
        return None
    if _state_has_conversation_url(state):
        return None
    artifacts = state.get("artifacts") if isinstance(state.get("artifacts"), dict) else {}
    output = Path(str(artifacts.get("output") or ""))
    if str(output) and output_is_nonempty(output):
        return None
    stdout_record = _artifact_bytes(state, "stdout")
    stderr_record = _artifact_bytes(state, "stderr")
    if stdout_record is None or stderr_record is None:
        return None
    _, stdout_bytes = stdout_record
    _, stderr_bytes = stderr_record
    if stdout_bytes.strip():
        return None
    try:
        stderr_text = stderr_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return None
    normalized_error = stderr_text.lstrip()
    if not normalized_error.startswith("version resolution failed:"):
        return None
    if not (
        "ORACLE_VERSION_TIMEOUT:" in normalized_error
        or ("--version" in normalized_error and "timed out after 30 seconds" in normalized_error)
    ):
        return None
    return {
        "schema": "codex.chatgpt.oracle-pre-submit-host-failure/v1",
        "code": "ORACLE_VERSION_RESOLUTION_PRELAUNCH_FAILED",
        "stdout_sha256": hashlib.sha256(stdout_bytes).hexdigest(),
        "stderr_sha256": hashlib.sha256(stderr_bytes).hexdigest(),
        "output_absent": True,
        "conversation_url_absent": True,
        "resolved_version": "unresolved",
    }


def proven_pre_submit_attachment_size_failure(state_path: Path) -> dict[str, Any] | None:
    """Prove Oracle rejected an oversized attachment before browser submission."""
    state = load_state(state_path)
    authority = str(state.get("session_authority") or "")
    if authority not in {"pre_submit", "submitted_unknown"}:
        return None
    if _state_has_conversation_url(state):
        return None
    artifacts = state.get("artifacts") if isinstance(state.get("artifacts"), dict) else {}
    output = Path(str(artifacts.get("output") or ""))
    if str(output) and output_is_nonempty(output):
        return None
    stderr_record = _artifact_bytes(state, "stderr")
    if stderr_record is None:
        return None
    _, stderr_bytes = stderr_record
    try:
        stderr_text = stderr_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return None
    if ORACLE_ATTACHMENT_SIZE_PREFLIGHT_MARKER not in stderr_text:
        return None
    attachments = state.get("attachments") if isinstance(state.get("attachments"), list) else []
    oversized: list[dict[str, Any]] = []
    for item in attachments:
        if not isinstance(item, dict):
            continue
        try:
            size_bytes = int(item.get("size_bytes") or 0)
        except (TypeError, ValueError):
            continue
        path = str(item.get("path") or "")
        if (
            size_bytes > ORACLE_ATTACHMENT_SIZE_LIMIT_BYTES
            and path
            and Path(path).name in stderr_text
        ):
            oversized.append({
                "path": path,
                "sha256": str(item.get("sha256") or ""),
                "size_bytes": size_bytes,
            })
    if not oversized:
        return None
    return {
        "schema": "codex.chatgpt.oracle-pre-submit-attachment-size-failure/v1",
        "code": "ORACLE_ATTACHMENT_SIZE_PREFLIGHT_REJECTED",
        "stderr_sha256": hashlib.sha256(stderr_bytes).hexdigest(),
        "output_absent": True,
        "conversation_url_absent": True,
        "limit_bytes": ORACLE_ATTACHMENT_SIZE_LIMIT_BYTES,
        "oversized_attachments": oversized,
    }


def proven_pre_submit_failure(state_path: Path) -> dict[str, Any] | None:
    return (
        proven_pre_submit_rejection(state_path)
        or proven_pre_submit_attachment_size_failure(state_path)
        or proven_pre_submit_host_failure(state_path)
        or proven_user_confirmed_no_submission(state_path)
    )


def settle_proven_pre_submit_rejection(state_path: Path) -> dict[str, Any] | None:
    """Correct submitted_unknown only when exact Oracle stdout proves no send occurred."""
    evidence = proven_pre_submit_rejection(state_path)
    if evidence is None:
        return None
    payload = load_state(state_path)
    payload.update({
        "status": "attention_required",
        "exit_code": int(payload.get("exit_code") or 1),
        "session_authority": "pre_submit",
        "terminal_harvested": False,
        "artifact_sha256": None,
        "transport_status": "rejected_pre_submit",
        "task_outcome": "pending",
        "task_outcome_reason": "oracle-global-prompt-duplicate",
        "pre_submit_rejection": evidence,
    })
    write_json_atomic(state_path, payload)
    return payload


def settle_proven_pre_submit_failure(state_path: Path) -> dict[str, Any] | None:
    """Settle either supported immutable proof without preserving a false lock."""
    rejection = proven_pre_submit_rejection(state_path)
    if rejection is not None:
        return settle_proven_pre_submit_rejection(state_path)
    confirmed = proven_user_confirmed_no_submission(state_path)
    if confirmed is not None:
        return load_state(state_path)
    evidence = (
        proven_pre_submit_attachment_size_failure(state_path)
        or proven_pre_submit_host_failure(state_path)
    )
    if evidence is None:
        return None
    attachment_size_rejection = (
        evidence.get("code") == "ORACLE_ATTACHMENT_SIZE_PREFLIGHT_REJECTED"
    )
    payload = load_state(state_path)
    payload.update({
        "status": "attention_required",
        "exit_code": int(payload.get("exit_code") or 1),
        "session_authority": "pre_submit",
        "terminal_harvested": False,
        "artifact_sha256": None,
        "transport_status": (
            "rejected_pre_submit" if attachment_size_rejection else "failed_pre_submit"
        ),
        "task_outcome": "pending",
        "task_outcome_reason": (
            "oracle-attachment-size-preflight-rejected"
            if attachment_size_rejection
            else "prelaunch-host-failure"
        ),
        "pre_submit_failure": evidence,
    })
    write_json_atomic(state_path, payload)
    return payload


def settle_pre_submit_session_absent(
    state_path: Path,
    *,
    locator: str,
    recovery_stdout: Path,
    recovery_stderr: Path,
) -> dict[str, Any] | None:
    """Keep pre-submit authority when exact recovery proves no Oracle session exists."""
    payload = load_state(state_path)
    if str(payload.get("session_authority") or "") != "pre_submit":
        return None
    if _state_has_conversation_url(payload):
        return None
    artifacts = payload.get("artifacts") if isinstance(payload.get("artifacts"), dict) else {}
    output = Path(str(artifacts.get("output") or ""))
    if str(output) and output_is_nonempty(output):
        return None
    chunks: list[bytes] = []
    for path in (recovery_stdout, recovery_stderr):
        try:
            chunks.append(path.read_bytes())
        except OSError:
            chunks.append(b"")
    combined = b"\n".join(chunks)
    try:
        text = combined.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return None
    matches = [match.group("locator") for match in ORACLE_NO_SESSION_RE.finditer(text)]
    if matches != [locator]:
        return None
    evidence = {
        "schema": "codex.chatgpt.oracle-pre-submit-session-absence/v1",
        "code": "ORACLE_EXACT_SESSION_NOT_FOUND",
        "oracle_locator": locator,
        "recovery_sha256": hashlib.sha256(combined).hexdigest(),
        "output_absent": True,
        "conversation_url_absent": True,
    }
    payload.update({
        "status": "attention_required",
        "exit_code": int(payload.get("exit_code") or 1),
        "session_authority": "pre_submit",
        "terminal_harvested": False,
        "artifact_sha256": None,
        "transport_status": "not_submitted",
        "task_outcome": "pending",
        "task_outcome_reason": "exact-session-absent-before-submit",
        "pre_submit_session_absence": evidence,
    })
    write_json_atomic(state_path, payload)
    return payload


def resolve_lifecycle(state: dict[str, Any], *, output_is_present: bool | None = None) -> dict[str, Any]:
    """Collapse the stored run record into one bounded lifecycle verdict.

    Authority order is fixed and single-sourced: exact terminal web evidence
    outranks a durable stored artifact, which outranks the local ledger.  PIDs,
    heartbeats, locks and poll results are diagnostics and never appear here.
    """
    status = str(state.get("status") or "")
    authority = str(state.get("session_authority") or "")
    harvested = state.get("terminal_harvested") is True
    outcome = str(state.get("task_outcome") or "")
    if output_is_present is None:
        artifacts = state.get("artifacts") if isinstance(state.get("artifacts"), dict) else {}
        output_path = Path(str(artifacts.get("output") or ""))
        has_output = bool(str(output_path)) and output_is_nonempty(output_path)
    else:
        has_output = bool(output_is_present)

    if status == "abandoned":
        return {"lifecycle": "abandoned", "authority_source": "explicit-abandonment"}
    # 1. Exact terminal web evidence.
    if authority == "terminal" and harvested and has_output:
        if outcome == "not_executed":
            return {"lifecycle": "needs_attention", "authority_source": "exact-terminal-evidence"}
        return {"lifecycle": "complete", "authority_source": "exact-terminal-evidence"}
    # 2. Durable stored artifact, including ledgers written before authority
    #    tracking existed.  A finished answer on disk is not a defect.
    if has_output and status == "complete":
        if outcome == "not_executed":
            return {"lifecycle": "needs_attention", "authority_source": "durable-artifact"}
        return {"lifecycle": "complete", "authority_source": "durable-artifact"}
    # 3. An owned session that is still live keeps running regardless of a
    #    local nonzero exit; only web state may end it.
    if authority in {"live", "submitted_unknown", "terminal_observed"}:
        return {"lifecycle": "running", "authority_source": "exact-session-ownership"}
    # 4. Local ledger, lowest authority.
    if status == "complete":
        # A ledger that claims completion without a durable artifact has not
        # proven anything.  Never let the weakest authority assert completion.
        return {"lifecycle": "needs_attention", "authority_source": "local-ledger"}
    return {
        "lifecycle": _STATUS_TO_LIFECYCLE.get(status, "needs_attention"),
        "authority_source": "local-ledger",
    }


TASK_OUTCOME_RE = re.compile(
    r"TASK_OUTCOME:\s*(EXECUTED|NOT_EXECUTED|BLOCKED)",
    re.IGNORECASE,
)


def classify_task_outcome(path: Path, *, contract: str, transport: str) -> str:
    if transport == "pro-attachment-only":
        return "not_applicable"
    try:
        text = path.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeDecodeError):
        return "unknown"
    final_line = next((line.strip() for line in reversed(text.splitlines()) if line.strip()), "")
    marker = TASK_OUTCOME_RE.fullmatch(final_line)
    if marker:
        return marker.group(1).casefold()
    return "unknown" if contract == "v1" else "legacy_unclassified"


def unresolved_project_sessions(
    run_root: Path,
    project_root: Path,
    *,
    parallel_parent_id: str | None = None,
    exclude_run_id: str | None = None,
) -> list[dict[str, str]]:
    """Return exact submitted sessions that still own this project.

    A local Oracle exit is not web-terminal authority.  Ownership therefore
    survives ``running``/``attention_required`` host states until exact-session
    recovery records terminal completion.  Parallel children from the same
    persisted parent are allowed to coexist; a different parent is not.
    """
    root = run_root.expanduser().resolve()
    expected_project = str(project_root.expanduser().resolve()).casefold()
    expected_parent = str(parallel_parent_id or "").strip().casefold()
    active_authorities = {"submitted_unknown", "live", "terminal_observed"}
    owners: list[dict[str, str]] = []
    if not root.is_dir():
        return owners
    for candidate in sorted(root.glob("*/state.json"), key=lambda item: str(item)):
        try:
            payload = load_state(candidate)
        except (OSError, OracleStateError):
            continue
        run_id = str(payload.get("run_id") or "")
        if run_id == exclude_run_id or str(payload.get("project_root") or "").casefold() != expected_project:
            continue
        authority = str(payload.get("session_authority") or "").strip().casefold()
        settlement_artifact = candidate.parent / "user-confirmed-no-submission.json"
        settlement_derived = (
            "user_confirmed_no_submission" in payload
            or str(payload.get("transport_status") or "") == "not_submitted_user_confirmed"
            or str(payload.get("task_outcome_reason") or "")
            == "user-confirmed-no-submission-after-prompt-timeout"
            or settlement_artifact.exists()
        )
        invalid_settlement = False
        if (
            authority == "pre_submit"
            and settlement_derived
            and proven_user_confirmed_no_submission(candidate) is None
        ):
            # A missing or changed settlement artifact revokes the release and
            # restores fail-closed ownership before any new submission.
            authority = "submitted_unknown"
            invalid_settlement = True
        # Legacy running records fail closed because the provider may still be
        # active. Legacy attention-required records predate explicit session
        # authority and must not become permanent project locks; new runs
        # persist submitted_unknown/live explicitly before reaching attention.
        if not authority and str(payload.get("status") or "").casefold() == "running":
            authority = "submitted_unknown"
        if authority not in active_authorities:
            continue
        owner_parent = str(payload.get("parallel_parent_id") or "").strip().casefold()
        if expected_parent and owner_parent == expected_parent and not invalid_settlement:
            continue
        owners.append({
            "run_id": run_id,
            "session_locator": str((payload.get("oracle") or {}).get("session_locator") or ""),
            "session_authority": authority,
            "state_path": str(candidate),
        })
    return owners


def write_transcript(layout: RunLayout) -> None:
    chunks = []
    for source in (layout.stdout_path, layout.stderr_path):
        try:
            data = source.read_bytes()
        except OSError:
            data = b""
        if data:
            chunks.append(data.rstrip() + b"\n")
    if layout.output_path.is_file():
        data = layout.output_path.read_bytes()
        if data:
            chunks.append(data.rstrip() + b"\n")
    layout.transcript_path.write_bytes(b"".join(chunks))


def windows_subprocess_kwargs(*, platform_name: str | None = None) -> dict[str, Any]:
    if (os.name if platform_name is None else platform_name) != "nt":
        return {}
    kwargs: dict[str, Any] = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", CREATE_NO_WINDOW)}
    startupinfo_type = getattr(subprocess, "STARTUPINFO", None)
    if startupinfo_type is not None:
        startupinfo = startupinfo_type()
        startupinfo.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 1)
        startupinfo.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
        kwargs["startupinfo"] = startupinfo
    return kwargs


def mutex_wait_succeeded(wait_result: int) -> bool:
    return wait_result in {WAIT_OBJECT_0, WAIT_ABANDONED}


def submit_mutex_name(project_root: Path) -> str:
    digest = hashlib.sha256(str(project_root).casefold().encode("utf-8")).hexdigest()[:32]
    return f"Local\\codexpro-oracle-submit-{digest}"


class WindowsSubmitMutex(AbstractContextManager["WindowsSubmitMutex"]):
    def __init__(self, name: str, timeout_seconds: float):
        self.name, self.timeout_seconds, self.handle, self.acquired = name, timeout_seconds, None, False

    def __enter__(self) -> "WindowsSubmitMutex":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        handle = kernel32.CreateMutexW(None, False, self.name)
        if not handle:
            raise OracleStateError("SUBMIT_MUTEX_CREATE_FAILED", "Windows submit mutex could not be created")
        self.handle = int(handle)
        result = int(kernel32.WaitForSingleObject(handle, max(1, int(self.timeout_seconds * 1000))))
        if not mutex_wait_succeeded(result):
            kernel32.CloseHandle(handle)
            self.handle = None
            raise OracleStateError("SUBMIT_MUTEX_TIMEOUT" if result == WAIT_TIMEOUT else "SUBMIT_MUTEX_WAIT_FAILED", "project submit mutex could not be acquired")
        self.acquired = True
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.handle is not None:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            if self.acquired:
                kernel32.ReleaseMutex(ctypes.c_void_p(self.handle))
            kernel32.CloseHandle(ctypes.c_void_p(self.handle))
        self.handle, self.acquired = None, False
        return None


class ThreadSubmitMutex(AbstractContextManager["ThreadSubmitMutex"]):
    def __init__(self, name: str, timeout_seconds: float):
        self.name, self.timeout_seconds, self.lock = name, timeout_seconds, None

    def __enter__(self) -> "ThreadSubmitMutex":
        with _THREAD_MUTEXES_GUARD:
            lock = _THREAD_MUTEXES.setdefault(self.name, threading.Lock())
        if not lock.acquire(timeout=self.timeout_seconds):
            raise OracleStateError("SUBMIT_MUTEX_TIMEOUT", "project submit mutex could not be acquired")
        self.lock = lock
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.lock is not None:
            self.lock.release()
        self.lock = None
        return None


class FileSubmitMutex(AbstractContextManager["FileSubmitMutex"]):
    def __init__(self, name: str, timeout_seconds: float):
        digest = hashlib.sha256(name.encode("utf-8")).hexdigest()
        self.path = Path(tempfile.gettempdir()) / f"codexpro-oracle-submit-{digest}.lock"
        self.timeout_seconds = timeout_seconds
        self.handle = None

    def __enter__(self) -> "FileSubmitMutex":
        import fcntl

        self.handle = self.path.open("a+b")
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            try:
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    self.handle.close()
                    self.handle = None
                    raise OracleStateError("SUBMIT_MUTEX_TIMEOUT", "project submit mutex could not be acquired")
                time.sleep(0.05)

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.handle is not None:
            import fcntl

            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()
        self.handle = None
        return None


def project_submit_mutex(
    project_root: Path,
    *,
    timeout_seconds: float,
    platform_name: str | None = None,
) -> AbstractContextManager[Any]:
    name = submit_mutex_name(project_root)
    platform = os.name if platform_name is None else platform_name
    if platform == "nt":
        return WindowsSubmitMutex(name, timeout_seconds)
    return FileSubmitMutex(name, timeout_seconds)


def command_for_display(command: Sequence[str]) -> list[str]:
    return [str(item) for item in command]
