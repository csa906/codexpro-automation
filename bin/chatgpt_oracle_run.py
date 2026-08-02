from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Sequence

STATE_PATH = Path(__file__).resolve().with_name("chatgpt_oracle_state.py")
COMPAT_PATH = Path(__file__).resolve().with_name("chatgpt_oracle_compat.py")
DEVSPACE_COMPAT_PATH = Path(__file__).resolve().with_name("chatgpt_devspace_compat.py")


def load_state_module():
    spec = importlib.util.spec_from_file_location("chatgpt_oracle_state_runtime", STATE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Oracle state module unavailable: {STATE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


STATE = load_state_module()


def load_compat_module():
    spec = importlib.util.spec_from_file_location("chatgpt_oracle_compat_runtime", COMPAT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Oracle compatibility module unavailable: {COMPAT_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


COMPAT = load_compat_module()


def load_devspace_compat_module():
    spec = importlib.util.spec_from_file_location(
        "chatgpt_devspace_compat_runtime",
        DEVSPACE_COMPAT_PATH,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"DevSpace compatibility module unavailable: {DEVSPACE_COMPAT_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


DEVSPACE_COMPAT = load_devspace_compat_module()


class OracleRunError(RuntimeError):
    def __init__(self, code: str, message: str, evidence: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.evidence = evidence or {}

    def envelope(self) -> dict[str, Any]:
        return {"ok": False, "error": {"code": self.code, "message": str(self), "evidence": self.evidence}}


def build_oracle_argv(config, layout, prompt: str) -> list[str]:
    lifecycle_args = [] if "--browser-hide-window" in config.oracle_args else ["--browser-hide-window"]
    # Upstream waits `--browser-timeout` for the answer and then gives its
    # recovery pass the same budget, so the effective ceiling is twice this
    # value.  Oracle's 20m default cut heavy Extra High DevSpace lanes at
    # exactly 40m while they were still streaming.  Pro keeps upstream timing.
    answer_timeout_args = (
        []
        if config.transport == "pro-attachment-only"
        or any(
            item == "--browser-timeout" or item.startswith("--browser-timeout=")
            for item in config.oracle_args
        )
        else ["--browser-timeout", STATE.DEFAULT_BROWSER_ANSWER_TIMEOUT]
    )
    command = [
        *config.oracle_command,
        "--engine", "browser",
        "--model", config.model,
        "--browser-model-strategy", config.model_strategy,
        "--browser-thinking-time", config.thinking_time,
        "--browser-research", config.research,
        "--browser-archive", config.archive,
        *lifecycle_args,
        *answer_timeout_args,
        *config.oracle_args,
        "--slug", layout.slug,
        "--prompt", prompt,
        "--write-output", str(layout.output_path),
    ]
    if config.transport == "pro-attachment-only":
        attachment_args: list[str] = []
        for path in config.attachments:
            attachment_args.extend(["--file", str(path)])
        command[command.index("--slug"):command.index("--slug")] = [
            "--browser-attachments", "always", *attachment_args,
        ]
    if config.copy_profile is not None:
        command[command.index("--slug"):command.index("--slug")] = ["--copy-profile", str(config.copy_profile)]
    if config.transport != "pro-attachment-only" and any(
        item == "--file" or item.startswith("--file=") or item == "-f" for item in command
    ):
        raise OracleRunError("FILE_TRANSPORT_FORBIDDEN", "general GPT browser runs must not use --file")
    return command


_BROWSER_TIMEOUT_RE = re.compile(r"^(?P<value>[0-9]+(?:\.[0-9]+)?)(?P<unit>ms|s|m|h)?$", re.IGNORECASE)
MAX_HOST_WATCHDOG_SECONDS = 7 * 24 * 60 * 60


def host_watchdog_timeout_seconds(config, argv: Sequence[str]) -> float | None:
    """Return one host wall-clock ceiling without changing Pro timing.

    Oracle 0.16.1 can remain inside a blocked CDP evaluation after its own
    browser deadline.  The host deadline is therefore independent and only
    releases the caller; it never terminates the submitted Oracle process.
    """
    if config.transport == "pro-attachment-only":
        return None
    values: list[str] = []
    for index, item in enumerate(argv):
        if item == "--browser-timeout":
            if index + 1 >= len(argv):
                raise OracleRunError("BROWSER_TIMEOUT_INVALID", "--browser-timeout requires a value")
            values.append(str(argv[index + 1]))
        elif item.startswith("--browser-timeout="):
            values.append(item.split("=", 1)[1])
    if len(values) != 1:
        raise OracleRunError(
            "BROWSER_TIMEOUT_INVALID",
            "regular Oracle runs require exactly one browser timeout",
            {"values": values},
        )
    match = _BROWSER_TIMEOUT_RE.fullmatch(values[0].strip())
    if match is None:
        raise OracleRunError(
            "BROWSER_TIMEOUT_INVALID",
            "browser timeout must be a positive ms/s/m/h duration",
            {"value": values[0]},
        )
    value = float(match.group("value"))
    unit = (match.group("unit") or "ms").casefold()
    multiplier = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}[unit]
    answer_seconds = value * multiplier
    watchdog_seconds = answer_seconds + STATE.HOST_WATCHDOG_GRACE_SECONDS
    if (
        not math.isfinite(value)
        or not math.isfinite(answer_seconds)
        or not math.isfinite(watchdog_seconds)
        or answer_seconds <= 0
        or watchdog_seconds > MAX_HOST_WATCHDOG_SECONDS
    ):
        raise OracleRunError(
            "BROWSER_TIMEOUT_OUT_OF_RANGE",
            "browser timeout must produce a finite host deadline of at most seven days",
            {"value": values[0]},
        )
    return watchdog_seconds


def wait_for_oracle_process(process: Any, watchdog_timeout_seconds: float | None) -> tuple[int | None, bool]:
    if watchdog_timeout_seconds is None:
        return int(process.wait()), False
    try:
        return int(process.wait(timeout=watchdog_timeout_seconds)), False
    except subprocess.TimeoutExpired:
        poll = getattr(process, "poll", None)
        if callable(poll):
            raced_exit_code = poll()
            if raced_exit_code is not None:
                return int(raced_exit_code), False
        return None, True


def isolated_oracle_environment(
    base_env: dict[str, str],
    command: Sequence[str],
    *,
    npm_prefix: Path,
) -> dict[str, str]:
    """Keep npx Oracle resolution independent of an installed global package."""
    env = dict(base_env)
    executable = Path(command[0]).name.casefold() if command else ""
    if executable not in {"npx", "npx.cmd", "npx.exe"}:
        return env
    prefix = npm_prefix.expanduser().resolve()
    state_root = STATE.oracle_state_root()
    if not STATE.is_within(state_root, prefix):
        raise OracleRunError(
            "ORACLE_NPM_PREFIX_OUTSIDE_HOST_STATE",
            "isolated Oracle npm prefix must remain inside host-only Oracle state",
            {"prefix": str(prefix), "state_root": str(state_root)},
        )
    prefix.mkdir(parents=True, exist_ok=True)
    for key in tuple(env):
        if key.casefold() == "npm_config_prefix":
            del env[key]
    env["npm_config_prefix"] = str(prefix)
    return env


def resolve_oracle_version(
    command: Sequence[str],
    *,
    run_factory=subprocess.run,
    platform_name: str | None = None,
    env: dict[str, str] | None = None,
) -> str:
    completed = run_factory(
        [*command, "--version"],
        cwd=None,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        **STATE.windows_subprocess_kwargs(platform_name=platform_name),
    )
    if completed.returncode != 0:
        raise OracleRunError("ORACLE_VERSION_FAILED", "Oracle version could not be resolved", {"exit_code": completed.returncode})
    lines = [line.strip() for line in f"{completed.stdout or ''}\n{completed.stderr or ''}".splitlines() if line.strip()]
    if not lines:
        raise OracleRunError("ORACLE_VERSION_EMPTY", "Oracle version command returned no version")
    return lines[0]


def dry_run_payload(config, layout, argv: Sequence[str], prompt: str) -> dict[str, Any]:
    return {
        "ok": True,
        "status": "dry-run",
        "run_id": layout.run_id,
        "run_dir": str(layout.run_dir),
        "argv": STATE.command_for_display(argv),
        "prompt_first_line": prompt.splitlines()[0],
        "mission_path": str(config.mission_path),
        "mission_sha256": config.mission_sha256,
        "transport": config.transport,
        "attachments": [
            {"path": str(path), "sha256": digest}
            for path, digest in zip(config.attachments, config.attachment_sha256s, strict=True)
        ],
        "output_path": str(layout.output_path),
        "transcript_path": str(layout.transcript_path),
        "stdout_path": str(layout.stdout_path),
        "stderr_path": str(layout.stderr_path),
        "contains_file_flag": "--file" in argv,
        "host_watchdog_timeout_seconds": host_watchdog_timeout_seconds(config, argv),
    }


def append_error(path: Path, message: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as handle:
        handle.write((message.rstrip() + "\n").encode("utf-8", errors="replace"))


SESSION_STATE_RE = re.compile(r"(?im)^\s*State:\s*([a-z][a-z0-9_-]*)\s*$")
LIVE_SESSION_STATES = {"running", "streaming", "thinking", "active"}
TERMINAL_SESSION_STATES = {
    "complete", "completed", "done", "finished", "failed", "error", "cancelled", "canceled",
}
RECOVERY_BINDING_UNAVAILABLE_MARKERS = (
    'No live ChatGPT tab matched session',
    'session metadata has no recoverable ChatGPT conversation URL',
)


def exact_session_state(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    matches = SESSION_STATE_RE.findall(text)
    return matches[-1].casefold() if matches else None


def exact_recovery_binding_unavailable(*paths: Path) -> bool:
    """Return true only for Oracle's exact no-live-tab plus no-saved-URL proof.

    Oracle 0.16.1 writes the no-live-tab line to stdout and the missing-URL
    detail to stderr.  Both streams belong to one exact recovery attempt.
    """
    chunks: list[str] = []
    for path in paths:
        try:
            chunks.append(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            chunks.append("")
    value = "\n".join(chunks)
    return all(marker in value for marker in RECOVERY_BINDING_UNAVAILABLE_MARKERS)


def historical_session_authority(run_dir: Path, state: dict[str, Any]) -> str:
    """Recover the strongest exact-session authority from durable observer logs."""
    current = str(state.get("session_authority") or "submitted_unknown")
    if (
        current == "terminal"
        and state.get("terminal_harvested") is True
        and STATE.output_is_nonempty(Path(str(state["artifacts"]["output"])))
    ):
        return "terminal"
    strongest = current
    for path in sorted(run_dir.glob("recovery-*-stdout.log"), key=lambda item: item.name):
        observed = exact_session_state(path)
        if observed in TERMINAL_SESSION_STATES:
            strongest = "terminal_observed"
            break
    return strongest


def execute_run(
    manifest_path: Path,
    *,
    dry_run: bool = False,
    run_factory: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    popen_factory: Callable[..., Any] = subprocess.Popen,
    platform_name: str | None = None,
    compat_factory: Callable[[str], dict[str, Any]] = COMPAT.ensure_oracle_compatibility,
    devspace_compat_factory: Callable[[], dict[str, Any]] = (
        DEVSPACE_COMPAT.ensure_devspace_compatibility
    ),
) -> dict[str, Any]:
    config = STATE.load_manifest(manifest_path, platform_name=platform_name)
    layout = STATE.create_layout(config, run_id=config.requested_run_id)
    transport_mission_path = layout.run_dir / "mission.md"
    # The app reads the project mission. The copied bytes below are host-only
    # immutable evidence and are never exposed as the workspace handoff path.
    prompt = STATE.composer_prompt(config, config.mission_path)
    argv = build_oracle_argv(config, layout, prompt)
    if dry_run:
        return dry_run_payload(config, layout, argv, prompt)

    STATE.cleanup_prior_boot_browser_temps(config.run_root, platform_name=platform_name)
    watchdog_timeout_seconds = host_watchdog_timeout_seconds(config, argv)
    mission_bytes = config.mission_path.read_bytes()
    actual_mission_sha256 = hashlib.sha256(mission_bytes).hexdigest()
    if actual_mission_sha256 != config.mission_sha256:
        raise OracleRunError(
            "MISSION_CHANGED_BEFORE_PREPARE",
            "mission bytes changed after manifest validation",
            {"expected": config.mission_sha256, "actual": actual_mission_sha256},
        )
    for attachment, expected in zip(config.attachments, config.attachment_sha256s, strict=True):
        actual = STATE.sha256_file(attachment)
        if actual != expected:
            raise OracleRunError(
                "ATTACHMENT_CHANGED_BEFORE_PREPARE",
                "attachment bytes changed after manifest validation",
                {"path": str(attachment), "expected": expected, "actual": actual},
            )
    layout.run_dir.mkdir(parents=True, exist_ok=False)
    transport_mission_path.write_bytes(mission_bytes)
    STATE.write_json_atomic(layout.state_path, STATE.state_payload(config, layout, status="prepared", resolved_version="unresolved"))
    layout.stdout_path.touch()
    layout.stderr_path.touch()
    oracle_env = isolated_oracle_environment(
        STATE.browser_temp_environment(layout.browser_temp_path, platform_name=platform_name),
        config.oracle_command,
        npm_prefix=layout.run_dir / "npm-prefix",
    )
    exit_code: int | None = None
    watchdog_expired = False
    oracle_process_pid: int | None = None
    try:
        version = resolve_oracle_version(
            config.oracle_command,
            run_factory=run_factory,
            platform_name=platform_name,
            env=oracle_env,
        )
        compat_factory(version)
        if config.transport == "devspace":
            devspace_compat = devspace_compat_factory()
            if devspace_compat.get("service_restart_required"):
                raise OracleRunError(
                    "DEVSPACE_SERVICE_RESTART_REQUIRED",
                    "DevSpace was safely patched before submission and must be restarted once",
                    {"package_roots": devspace_compat.get("package_roots", [])},
                )
        STATE.update_state(layout.state_path, status="prepared", resolved_version=version)
    except Exception as exc:
        code = (
            f"{exc.code}: "
            if isinstance(exc, OracleRunError)
            else "ORACLE_VERSION_TIMEOUT: " if isinstance(exc, subprocess.TimeoutExpired) else ""
        )
        append_error(layout.stderr_path, f"version resolution failed: {code}{exc}")
        STATE.write_transcript(layout)
        failed = STATE.update_state(layout.state_path, status="failed")
        settled = STATE.settle_proven_pre_submit_failure(layout.state_path)
        if settled is not None:
            STATE.cleanup_owned_browser_temp(layout.browser_temp_path)
            return {
                "ok": False,
                "status": "pre_submit_failed",
                "safe_for_fresh_run": True,
                "run_dir": str(layout.run_dir),
                "result": settled,
            }
        return {
            "ok": False,
            "run_dir": str(layout.run_dir),
            "result": failed,
        }

    try:
        with layout.stdout_path.open("wb") as stdout_handle, layout.stderr_path.open("wb") as stderr_handle:
            mutex_root = (
                config.project_root / ".oracle-parallel-submit" / str(config.parallel_parent_id)
                if config.parallel_parent_id
                else config.project_root
            )
            with STATE.project_submit_mutex(mutex_root, timeout_seconds=config.submit_mutex_timeout_seconds, platform_name=platform_name):
                owners = STATE.unresolved_project_sessions(
                    config.run_root,
                    config.project_root,
                    parallel_parent_id=config.parallel_parent_id,
                    exclude_run_id=layout.run_id,
                )
                if owners:
                    raise OracleRunError(
                        "PROJECT_SESSION_STILL_LIVE",
                        "an exact Oracle session still owns this project; recover it before submitting",
                        {"owners": owners},
                    )
                original_mission_sha256 = STATE.sha256_file(config.mission_path)
                current_mission_sha256 = STATE.sha256_file(transport_mission_path)
                if original_mission_sha256 != config.mission_sha256 or current_mission_sha256 != config.mission_sha256:
                    raise OracleRunError(
                        "MISSION_CHANGED_BEFORE_SUBMIT",
                        "mission bytes changed after manifest validation",
                        {
                            "expected": config.mission_sha256,
                            "original_actual": original_mission_sha256,
                            "evidence_actual": current_mission_sha256,
                        },
                    )
                for attachment, expected in zip(config.attachments, config.attachment_sha256s, strict=True):
                    actual = STATE.sha256_file(attachment)
                    if actual != expected:
                        raise OracleRunError(
                            "ATTACHMENT_CHANGED_BEFORE_SUBMIT",
                            "attachment bytes changed after manifest validation",
                            {"path": str(attachment), "expected": expected, "actual": actual},
                        )
                process = popen_factory(
                    argv,
                    cwd=str(config.project_root),
                    env=oracle_env,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_handle,
                    stderr=stderr_handle,
                    shell=False,
                    **STATE.windows_subprocess_kwargs(platform_name=platform_name),
                )
                raw_pid = getattr(process, "pid", None)
                oracle_process_pid = int(raw_pid) if isinstance(raw_pid, int) else None
                STATE.update_state(
                    layout.state_path,
                    status="running",
                    resolved_version=version,
                    session_authority="submitted_unknown",
                    host_watchdog=(
                        {
                            "status": "armed",
                            "timeout_seconds": watchdog_timeout_seconds,
                            "oracle_process_pid": oracle_process_pid,
                            "process_action": "preserve",
                        }
                        if watchdog_timeout_seconds is not None
                        else {"status": "disabled-for-pro"}
                    ),
                )
                if not config.parallel_parent_id:
                    exit_code, watchdog_expired = wait_for_oracle_process(
                        process, watchdog_timeout_seconds
                    )
            if config.parallel_parent_id:
                exit_code, watchdog_expired = wait_for_oracle_process(
                    process, watchdog_timeout_seconds
                )
    except Exception as exc:
        code = f"{exc.code}: " if isinstance(exc, OracleRunError) else ""
        append_error(layout.stderr_path, f"Oracle launch/run failed: {code}{exc}")
        STATE.write_transcript(layout)
        latest = STATE.load_state(layout.state_path)
        if latest.get("session_authority") == "pre_submit":
            STATE.cleanup_owned_browser_temp(layout.browser_temp_path)
        return {"ok": False, "run_dir": str(layout.run_dir), "result": STATE.update_state(layout.state_path, status="failed")}
    STATE.write_transcript(layout)
    if watchdog_expired:
        state = STATE.update_state(
            layout.state_path,
            status="attention_required",
            exit_code=None,
            session_authority="submitted_unknown",
            transport_status="post_submit_watchdog_timeout",
            task_outcome="pending",
            task_outcome_reason="host-wall-clock-expired-process-preserved",
            host_watchdog={
                "status": "expired",
                "timeout_seconds": watchdog_timeout_seconds,
                "oracle_process_pid": oracle_process_pid,
                "process_action": "preserved",
                "next_action": "observe-or-recover-exact-session-only",
            },
        )
        return {
            "ok": False,
            "status": "post_submit_watchdog_timeout",
            "safe_for_fresh_run": False,
            "process_preserved": True,
            "oracle_process_pid": oracle_process_pid,
            "host_watchdog_timeout_seconds": watchdog_timeout_seconds,
            "next_action": "observe the original process or recover the exact slug; never replace or resubmit",
            "run_dir": str(layout.run_dir),
            "result": state,
        }
    pre_submit_failure = STATE.settle_proven_pre_submit_failure(layout.state_path)
    if pre_submit_failure is not None:
        STATE.cleanup_owned_browser_temp(layout.browser_temp_path)
        status = "pre_submit_rejected" if pre_submit_failure.get("pre_submit_rejection") else "pre_submit_failed"
        return {
            "ok": False,
            "status": status,
            "safe_for_fresh_run": True,
            "run_dir": str(layout.run_dir),
            "result": pre_submit_failure,
        }
    # Once Oracle has been launched, a nonzero local exit (including the
    # browser response timeout) does not prove that the exact web session
    # failed or stopped. Preserve same-project ownership and require exact-slug
    # recovery instead of presenting a terminal local failure.
    transport_complete = exit_code == 0 and STATE.output_is_nonempty(layout.output_path)
    task_outcome = (
        STATE.classify_task_outcome(
            layout.output_path,
            contract=config.task_outcome_contract,
            transport=config.transport,
        )
        if transport_complete
        else "pending"
    )
    semantic_complete = task_outcome in {
        "executed",
        "not_applicable",
        "legacy_unclassified",
    }
    status = "complete" if transport_complete and semantic_complete else "attention_required"
    if transport_complete:
        state = STATE.update_state(
            layout.state_path,
            status=status,
            exit_code=exit_code,
            session_authority="terminal",
            terminal_harvested=True,
            artifact_sha256=STATE.sha256_file(layout.output_path),
            transport_status="complete",
            task_outcome=task_outcome,
            task_outcome_reason=(
                "explicit-output-marker"
                if task_outcome in {"executed", "not_executed", "blocked"}
                else task_outcome
            ),
            host_watchdog={
                "status": "process-exited",
                "timeout_seconds": watchdog_timeout_seconds,
                "oracle_process_pid": oracle_process_pid,
                "process_action": "none",
            },
        )
        STATE.cleanup_owned_browser_temp(layout.browser_temp_path)
    else:
        state = STATE.update_state(
            layout.state_path,
            status=status,
            exit_code=exit_code,
            session_authority="submitted_unknown",
            transport_status="failed" if exit_code else "incomplete",
            task_outcome=task_outcome,
            host_watchdog={
                "status": "process-exited",
                "timeout_seconds": watchdog_timeout_seconds,
                "oracle_process_pid": oracle_process_pid,
                "process_action": "none",
            },
        )
    return {"ok": status == "complete", "run_dir": str(layout.run_dir), "result": state}


def recovery_argv(command: Sequence[str], locator: str, action: str, output_path: Path) -> list[str]:
    if action not in {"harvest", "live"}:
        raise OracleRunError("RECOVERY_ACTION_INVALID", "recovery action must be harvest or live")
    # Oracle's bounded browser recovery reopens only the exact conversation URL
    # persisted under this slug.  Do not pass --no-recover here: it disables
    # that safe harvest path and leaves a dead CDP endpoint as ECONNREFUSED.
    argv = [*command, "session", locator, f"--{action}", "--write-output", str(output_path)]
    if "restart" in argv or "--prompt" in argv or "-p" in argv:
        raise OracleRunError("RECOVERY_COMMAND_UNSAFE", "recovery must not restart or submit a new prompt")
    return argv


def _recover_run_locked(
    run_dir: Path,
    *,
    action: str,
    dry_run: bool = False,
    oracle_command: Sequence[str] | None = None,
    popen_factory: Callable[..., Any] = subprocess.Popen,
    platform_name: str | None = None,
) -> dict[str, Any]:
    directory = run_dir.expanduser().resolve(strict=True)
    state = STATE.load_state(directory / "state.json")
    pre_submit_failure = STATE.settle_proven_pre_submit_failure(directory / "state.json")
    if pre_submit_failure is not None:
        STATE.cleanup_owned_browser_temp(Path(str(pre_submit_failure["artifacts"]["browser_temp"])))
        status = "pre_submit_rejected" if pre_submit_failure.get("pre_submit_rejection") else "pre_submit_failed"
        return {
            "ok": False,
            "status": status,
            "safe_for_fresh_run": True,
            "run_dir": str(directory),
            "action": "none",
            "result": pre_submit_failure,
        }
    historical_authority = historical_session_authority(directory, state)
    if (
        STATE.SESSION_AUTHORITY_RANK.get(historical_authority, -1)
        > STATE.SESSION_AUTHORITY_RANK.get(str(state.get("session_authority") or ""), -1)
    ):
        state = STATE.update_state(
            directory / "state.json",
            status="attention_required",
            exit_code=state.get("exit_code"),
            session_authority=historical_authority,
        )
    if (
        state.get("status") == "complete"
        and state.get("session_authority") == "terminal"
        and state.get("terminal_harvested") is True
        and STATE.output_is_nonempty(Path(str(state["artifacts"]["output"])))
    ):
        outcome = str(state.get("task_outcome") or "legacy_unclassified")
        return {
            "ok": outcome in {"executed", "not_applicable", "legacy_unclassified"},
            "status": "complete",
            "run_dir": str(directory),
            "action": "none",
            "result": state,
            "output_path": str(state["artifacts"]["output"]),
            "monotonic_noop": True,
        }
    oracle = state.get("oracle") if isinstance(state.get("oracle"), dict) else {}
    locator = str(oracle.get("session_locator") or oracle.get("slug") or "").strip()
    if not locator:
        raise OracleRunError("SESSION_LOCATOR_MISSING", "run state has no Oracle session locator")
    artifacts = state.get("artifacts") if isinstance(state.get("artifacts"), dict) else {}
    output_path = Path(str(artifacts.get("output") or (directory / "output.md"))).expanduser().resolve()
    if not STATE.is_within(STATE.oracle_state_root(), output_path):
        raise OracleRunError("RECOVERY_OUTPUT_OUTSIDE_HOST_STATE", "recovery output must remain inside host-only Oracle state")
    stored_command = oracle.get("command")
    command = STATE.validate_oracle_command(list(oracle_command) if oracle_command is not None else stored_command)
    argv_output = directory / f"recovery-{action}-candidate.md"
    argv = recovery_argv(command, locator, action, argv_output)
    if dry_run:
        return {"ok": True, "status": "dry-run", "run_dir": str(directory), "action": action, "argv": STATE.command_for_display(argv)}
    stdout_path = directory / f"recovery-{action}-stdout.log"
    stderr_path = directory / f"recovery-{action}-stderr.log"
    recovery_browser_temp = directory / f"recovery-{action}-browser-temp"
    recovery_env = isolated_oracle_environment(
        STATE.browser_temp_environment(recovery_browser_temp, platform_name=platform_name),
        command,
        npm_prefix=directory / "npm-prefix",
    )
    try:
        with stdout_path.open("wb") as stdout_handle, stderr_path.open("wb") as stderr_handle:
            process = popen_factory(
                argv,
                cwd=str(state["project_root"]),
                env=recovery_env,
                stdin=subprocess.DEVNULL,
                stdout=stdout_handle,
                stderr=stderr_handle,
                shell=False,
                **STATE.windows_subprocess_kwargs(platform_name=platform_name),
            )
            exit_code = int(process.wait())
    finally:
        STATE.cleanup_owned_browser_temp(recovery_browser_temp)
    pre_submit_absence = STATE.settle_pre_submit_session_absent(
        directory / "state.json",
        locator=locator,
        recovery_stdout=stdout_path,
        recovery_stderr=stderr_path,
    )
    if pre_submit_absence is not None:
        if argv_output.exists():
            argv_output.unlink()
        return {
            "ok": False,
            "status": "pre_submit_session_absent",
            "safe_for_fresh_run": True,
            "run_dir": str(directory),
            "action": action,
            "exit_code": exit_code,
            "result": pre_submit_absence,
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
        }
    observed_session_state = exact_session_state(stdout_path)
    if exact_recovery_binding_unavailable(stdout_path, stderr_path):
        if argv_output.exists():
            argv_output.unlink()
        updated = STATE.update_state(
            directory / "state.json",
            status="attention_required",
            exit_code=exit_code,
            session_authority="submitted_unknown",
        )
        return {
            "ok": False,
            "status": "recovery_binding_unavailable",
            "run_dir": str(directory),
            "action": action,
            "exit_code": exit_code,
            "exact_session_state": observed_session_state,
            "result": updated,
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
            "next_action": (
                "restore the exact persisted ChatGPT conversation URL for this slug, "
                "then resume exact-slug recovery; never replace or resubmit"
            ),
        }
    if observed_session_state in LIVE_SESSION_STATES:
        if argv_output.exists():
            argv_output.unlink()
        prior_authority = str(state.get("session_authority") or "")
        updated = STATE.update_state(
            directory / "state.json",
            status="running",
            exit_code=exit_code,
            session_authority="live",
        )
        settle_disagreement = str(updated.get("session_authority") or "") in {
            "terminal_observed", "terminal",
        }
        return {
            "ok": False,
            "status": "terminal_settle_disagreement" if settle_disagreement else "session_live",
            "run_dir": str(directory),
            "action": action,
            "exit_code": exit_code,
            "exact_session_state": observed_session_state,
            "prior_session_authority": prior_authority,
            "session_authority": updated.get("session_authority"),
            "result": updated,
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
        }
    if action == "live":
        if argv_output.exists():
            argv_output.unlink()
        authority = "terminal_observed" if observed_session_state in TERMINAL_SESSION_STATES else "submitted_unknown"
        updated = STATE.update_state(
            directory / "state.json",
            status="attention_required",
            exit_code=exit_code,
            session_authority=authority,
        )
        return {
            "ok": False,
            "status": "terminal_observed" if authority == "terminal_observed" else "attention_required",
            "run_dir": str(directory),
            "action": action,
            "exit_code": exit_code,
            "exact_session_state": observed_session_state,
            "result": updated,
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
        }
    if (
        exit_code == 0
        and observed_session_state in TERMINAL_SESSION_STATES
        and STATE.output_is_nonempty(argv_output)
    ):
        os.replace(argv_output, output_path)
    layout = STATE.RunLayout(
        str(state["run_id"]),
        str(oracle.get("slug") or locator),
        directory,
        directory / "state.json",
        output_path,
        Path(str(artifacts.get("transcript") or (directory / "transcript.md"))),
        Path(str(artifacts.get("stdout") or (directory / "stdout.log"))),
        Path(str(artifacts.get("stderr") or (directory / "stderr.log"))),
        Path(str(artifacts.get("browser_temp") or (directory / "browser-temp"))).resolve(),
    )
    STATE.write_transcript(layout)
    harvested = (
        exit_code == 0
        and observed_session_state in TERMINAL_SESSION_STATES
        and STATE.output_is_nonempty(output_path)
    )
    # A failed recovery process is also not web-terminal evidence. Only an
    # exact terminal observation plus a nonempty durable output may complete.
    contract = str(state.get("task_outcome_contract") or "legacy")
    transport = str(state.get("transport") or "devspace")
    task_outcome = (
        STATE.classify_task_outcome(output_path, contract=contract, transport=transport)
        if harvested
        else "pending"
    )
    semantic_complete = task_outcome in {
        "executed",
        "not_applicable",
        "legacy_unclassified",
    }
    status = "complete" if harvested and semantic_complete else "attention_required"
    latest = STATE.load_state(layout.state_path)
    latest_output = Path(str(latest.get("artifacts", {}).get("output") or output_path))
    if latest.get("status") == "complete" and STATE.output_is_nonempty(latest_output):
        return {
            "ok": True,
            "status": "complete",
            "run_dir": str(directory),
            "action": action,
            "exit_code": exit_code,
            "result": latest,
            "output_path": str(latest_output),
            "monotonic_race_preserved": True,
        }
    updated = STATE.update_state(
        layout.state_path,
        status=status,
        exit_code=exit_code,
        session_authority="terminal" if harvested else (
            "terminal_observed" if observed_session_state in TERMINAL_SESSION_STATES else "submitted_unknown"
        ),
        terminal_harvested=harvested,
        artifact_sha256=STATE.sha256_file(output_path) if harvested else None,
        transport_status="complete" if harvested else "incomplete",
        task_outcome=task_outcome,
        task_outcome_reason=(
            "explicit-output-marker"
            if task_outcome in {"executed", "not_executed", "blocked"}
            else task_outcome
        ),
    )
    if harvested:
        STATE.cleanup_owned_browser_temp(layout.browser_temp_path)
    return {
        "ok": status == "complete",
        "status": status,
        "run_dir": str(directory),
        "action": action,
        "exit_code": exit_code,
        "result": updated,
        "output_path": str(output_path),
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
    }


def adjudicate_task_outcome(
    run_dir: Path,
    *,
    expected_output_sha256: str,
    task_outcome: str,
    reason: str,
) -> dict[str, Any]:
    directory = run_dir.expanduser().resolve(strict=True)
    state_path = directory / "state.json"
    state = STATE.load_state(state_path)
    output_path = Path(str((state.get("artifacts") or {}).get("output") or ""))
    if not output_path.is_file() or not STATE.is_within(STATE.oracle_state_root(), output_path.resolve()):
        raise OracleRunError(
            "ADJUDICATION_OUTPUT_INVALID",
            "exact run output is unavailable or outside host state",
        )
    actual = STATE.sha256_file(output_path)
    if actual != expected_output_sha256.strip().casefold():
        raise OracleRunError(
            "ADJUDICATION_OUTPUT_HASH_MISMATCH",
            "exact output changed before task outcome adjudication",
            {"expected": expected_output_sha256, "actual": actual},
        )
    normalized = task_outcome.strip().casefold()
    if normalized not in {"executed", "not_executed", "blocked", "unknown"}:
        raise OracleRunError(
            "ADJUDICATION_TASK_OUTCOME_INVALID",
            "task outcome must be executed, not_executed, blocked, or unknown",
        )
    if (
        str(state.get("session_authority") or "") != "terminal"
        or state.get("terminal_harvested") is not True
    ):
        raise OracleRunError(
            "ADJUDICATION_TERMINAL_REQUIRED",
            "only a durably harvested terminal run may be adjudicated",
        )
    updated = STATE.update_state(
        state_path,
        status=str(state.get("status") or "complete"),
        exit_code=state.get("exit_code"),
        transport_status="complete",
        task_outcome=normalized,
        task_outcome_reason=reason.strip() or "explicit-exact-output-adjudication",
    )
    return {
        "ok": normalized == "executed",
        "status": "task_outcome_adjudicated",
        "run_dir": str(directory),
        "output_path": str(output_path),
        "output_sha256": actual,
        "task_outcome": normalized,
        "safe_for_fresh_retry": normalized == "not_executed",
        "result": updated,
    }


def settle_user_confirmed_no_submission(
    run_dir: Path,
    *,
    confirmation: str,
    reason: str,
    platform_name: str | None = None,
) -> dict[str, Any]:
    """Settle one exact ambiguous send without launching or recovering Oracle."""
    directory = run_dir.expanduser().resolve(strict=True)
    state_path = directory / "state.json"
    stored = STATE.load_state(state_path)
    project_root = Path(str(stored.get("project_root") or "")).expanduser().resolve(strict=True)
    parallel_parent_id = str(stored.get("parallel_parent_id") or "").strip().casefold()
    if parallel_parent_id and STATE.PARENT_ID_RE.fullmatch(parallel_parent_id) is None:
        raise OracleRunError(
            "SETTLEMENT_PARALLEL_PARENT_ID_INVALID",
            "stored parallel parent id is invalid",
            {"parallel_parent_id": parallel_parent_id},
        )
    mutex_root = (
        project_root / ".oracle-parallel-submit" / parallel_parent_id
        if parallel_parent_id
        else project_root
    )
    with STATE.project_submit_mutex(mutex_root, timeout_seconds=30, platform_name=platform_name):
        settled = STATE.settle_user_confirmed_no_submission(
            state_path,
            confirmation=confirmation,
            reason=reason,
        )
        owners = STATE.unresolved_project_sessions(
            directory.parent,
            project_root,
            exclude_run_id=str(settled.get("run_id") or ""),
        )
    return {
        "ok": True,
        "status": "pre_submit_user_confirmed",
        "safe_for_fresh_run": not owners,
        "unresolved_owners": owners,
        "run_dir": str(directory),
        "result": settled,
    }


def recover_run(
    run_dir: Path,
    *,
    action: str,
    dry_run: bool = False,
    oracle_command: Sequence[str] | None = None,
    popen_factory: Callable[..., Any] = subprocess.Popen,
    platform_name: str | None = None,
    settle_timeout_seconds: float = 0,
    settle_interval_seconds: float = 15,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    directory = run_dir.expanduser().resolve(strict=True)
    stored = STATE.load_state(directory / "state.json")
    project_root = Path(str(stored.get("project_root") or "")).expanduser().resolve(strict=True)
    parallel_parent_id = str(stored.get("parallel_parent_id") or "").strip().casefold()
    if parallel_parent_id and STATE.PARENT_ID_RE.fullmatch(parallel_parent_id) is None:
        raise OracleRunError(
            "RECOVERY_PARALLEL_PARENT_ID_INVALID",
            "stored parallel parent id is invalid",
            {"parallel_parent_id": parallel_parent_id},
        )
    mutex_root = (
        project_root / ".oracle-parallel-submit" / parallel_parent_id
        if parallel_parent_id
        else project_root
    )
    with STATE.project_submit_mutex(
        mutex_root,
        timeout_seconds=30,
        platform_name=platform_name,
    ):
        result = _recover_run_locked(
            directory,
            action=action,
            dry_run=dry_run,
            oracle_command=oracle_command,
            popen_factory=popen_factory,
            platform_name=platform_name,
        )
        if dry_run or action != "live" or settle_timeout_seconds <= 0:
            return result
        deadline = time.monotonic() + settle_timeout_seconds
        while True:
            if result.get("ok"):
                return result
            if result.get("status") == "recovery_binding_unavailable":
                return result
            if result.get("status") == "terminal_observed":
                return _recover_run_locked(
                    directory,
                    action="harvest",
                    dry_run=False,
                    oracle_command=oracle_command,
                    popen_factory=popen_factory,
                    platform_name=platform_name,
                )
            current = result.get("result") if isinstance(result.get("result"), dict) else {}
            authority = str(current.get("session_authority") or "")
            exact_state = str(result.get("exact_session_state") or "").casefold()
            still_live_or_unsettled = (
                result.get("status") in {"session_live", "terminal_settle_disagreement"}
                or authority in {"live", "submitted_unknown"}
                and exact_state in {"", "active", "running", "streaming", "thinking", "stalled"}
            )
            if not still_live_or_unsettled:
                return result
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {
                    **result,
                    "ok": False,
                    "status": "live_settle_timeout",
                    "settle_timeout_seconds": settle_timeout_seconds,
                    "next_action": "resume the same exact-slug live recovery; never replace or resubmit",
                }
            sleep(min(settle_interval_seconds, remaining))
            result = _recover_run_locked(
                directory,
                action="live",
                dry_run=False,
                oracle_command=oracle_command,
                popen_factory=popen_factory,
                platform_name=platform_name,
            )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run additive Oracle browser missions without modifying agbrowse routing.")
    commands = parser.add_subparsers(dest="command", required=True)
    run_parser = commands.add_parser("run")
    run_parser.add_argument("--manifest", type=Path, required=True)
    run_parser.add_argument("--dry-run", action="store_true")
    recover_parser = commands.add_parser("recover")
    recover_parser.add_argument("--run-dir", type=Path, required=True)
    recover_parser.add_argument("--action", choices=("harvest", "live"), required=True)
    recover_parser.add_argument("--oracle-command", nargs="+")
    recover_parser.add_argument("--dry-run", action="store_true")
    recover_parser.add_argument(
        "--settle-timeout-seconds",
        type=float,
        default=5400,
        help="For live recovery, keep the exact slug in one process until terminal or this bounded deadline.",
    )
    recover_parser.add_argument(
        "--settle-interval-seconds",
        type=float,
        default=15,
    )
    adjudicate_parser = commands.add_parser("adjudicate")
    adjudicate_parser.add_argument("--run-dir", type=Path, required=True)
    adjudicate_parser.add_argument("--expected-output-sha256", required=True)
    adjudicate_parser.add_argument(
        "--task-outcome",
        choices=("executed", "not_executed", "blocked", "unknown"),
        required=True,
    )
    adjudicate_parser.add_argument("--reason", required=True)
    settle_parser = commands.add_parser("settle-no-submission")
    settle_parser.add_argument("--run-dir", type=Path, required=True)
    settle_parser.add_argument(
        "--confirmation",
        choices=(STATE.USER_CONFIRMED_NO_SUBMISSION,),
        required=True,
    )
    settle_parser.add_argument("--reason", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "run":
            payload = execute_run(args.manifest, dry_run=args.dry_run)
        elif args.command == "recover":
            payload = recover_run(
                args.run_dir,
                action=args.action,
                dry_run=args.dry_run,
                oracle_command=args.oracle_command,
                settle_timeout_seconds=args.settle_timeout_seconds,
                settle_interval_seconds=args.settle_interval_seconds,
            )
        elif args.command == "adjudicate":
            payload = adjudicate_task_outcome(
                args.run_dir,
                expected_output_sha256=args.expected_output_sha256,
                task_outcome=args.task_outcome,
                reason=args.reason,
            )
        else:
            payload = settle_user_confirmed_no_submission(
                args.run_dir,
                confirmation=args.confirmation,
                reason=args.reason,
            )
    except STATE.OracleStateError as exc:
        payload = exc.envelope()
    except OracleRunError as exc:
        payload = exc.envelope()
    except Exception as exc:
        payload = OracleRunError("ORACLE_RUN_FAILED", str(exc)).envelope()
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
