from __future__ import annotations

import importlib.util
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

RUNNER_PATH = Path(__file__).resolve().parents[1] / "bin" / "chatgpt_oracle_run.py"


def load_runner():
    name = "chatgpt_oracle_run_test"
    spec = importlib.util.spec_from_file_location(name, RUNNER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def manifest(tmp_path: Path, **extra) -> Path:
    mission = tmp_path / "mission.md"
    mission.write_text("finish", encoding="utf-8")
    path = tmp_path / "job.json"
    payload = {
        "schema": "codex.chatgpt.oracle-run/v1",
        "project_root": str(tmp_path.resolve()),
        "mission_path": str(mission.resolve()),
        "app_name": "DevSpace",
        "mode": "browser",
        "run_root": str((tmp_path.parent / f"{tmp_path.name}-host-state" / "runs").resolve()),
        "oracle_command": ["oracle"],
    }
    payload.update(extra)
    path.write_text(json.dumps(payload), encoding="utf-8")
    os.environ["CODEX_ORACLE_STATE_ROOT"] = str((tmp_path.parent / f"{tmp_path.name}-host-state").resolve())
    return path.resolve()


def pro_manifest(tmp_path: Path, **extra) -> Path:
    prompt = tmp_path / "prompt.txt"
    packet = tmp_path / "packet.zip"
    prompt.write_text("pro instructions", encoding="utf-8")
    packet.write_bytes(b"PK\x03\x04packet")
    return manifest(
        tmp_path,
        transport="pro-attachment-only",
        app_name=None,
        model="gpt-5.5-pro",
        model_strategy="select",
        thinking_time="heavy",
        attachments=[str(prompt.resolve()), str(packet.resolve())],
        mission_path=str(prompt.resolve()),
        **extra,
    )


def version_runner(command, **kwargs):
    return subprocess.CompletedProcess(command, 0, stdout="oracle 0.13.0\n", stderr="")


def version_timeout_runner(command, **kwargs):
    raise subprocess.TimeoutExpired(command, kwargs.get("timeout", 30))


def execute_run(runner, *args, **kwargs):
    kwargs.setdefault("compat_factory", lambda version: {"ok": True, "version": version})
    kwargs.setdefault(
        "devspace_compat_factory",
        lambda: {"ok": True, "changed": [], "service_restart_required": False},
    )
    return runner.execute_run(*args, **kwargs)


class Process:
    def __init__(self, code: int, events: list[str]):
        self.code = code
        self.events = events
        self.pid = 1234
        self.wait_timeout = None

    def wait(self, timeout=None):
        self.wait_timeout = timeout
        self.events.append("wait")
        return self.code


def popen_for(code: int, output: bytes | None, captured: dict, events: list[str]):
    def popen(command, **kwargs):
        captured["command"] = list(command)
        captured["kwargs"] = kwargs
        events.append("popen")
        if output is not None:
            Path(command[command.index("--write-output") + 1]).write_bytes(output)
        kwargs["stdout"].write(b"stdout\n")
        kwargs["stdout"].flush()
        return Process(code, events)
    return popen


def duplicate_prompt_popen(command, **kwargs):
    kwargs["stdout"].write(
        b'oracle 0.16.1\nA session with the same prompt is already running '
        b'(oracle-global-agent-instructio-f39cc47ba5). Reattach with '
        b'"oracle session oracle-global-agent-instructio-f39cc47ba5" or rerun with '
        b'--force to start another run.\n'
    )
    kwargs["stdout"].flush()
    return Process(1, [])


def attachment_size_popen(command, **kwargs):
    kwargs["stdout"].write(b"oracle 0.16.1\n")
    kwargs["stdout"].flush()
    kwargs["stderr"].write(
        b"The following files exceed the 1 MB limit:\n- packet.zip (1.1 MB)\n"
    )
    kwargs["stderr"].flush()
    return Process(1, [])


def test_dry_run_never_executes_and_has_no_file_flag(tmp_path: Path) -> None:
    runner = load_runner()
    calls = []
    def forbidden(*args, **kwargs):
        calls.append(1)
        raise AssertionError
    result = execute_run(runner, manifest(tmp_path), dry_run=True, run_factory=forbidden, popen_factory=forbidden)
    assert result["ok"] is True
    assert result["prompt_first_line"].startswith("@DevSpace ")
    assert str((tmp_path / "mission.md").resolve()) in result["prompt_first_line"]
    assert result["mission_sha256"]
    assert Path(result["mission_path"]).is_absolute()
    assert str((tmp_path / "mission.md").resolve()) in result["argv"][result["argv"].index("--prompt") + 1]
    assert "--file" not in result["argv"]
    assert result["argv"][result["argv"].index("--browser-model-strategy") + 1] == "select"
    assert result["argv"][result["argv"].index("--browser-thinking-time") + 1] == "heavy"
    assert result["argv"].count("--browser-hide-window") == 1
    assert calls == []
    assert not (tmp_path / "runs").exists()


def test_copy_profile_is_first_class_and_outside_project(
    tmp_path: Path, monkeypatch
) -> None:
    runner = load_runner()
    profile = tmp_path.parent / f"{tmp_path.name}-oracle-profile"
    profile.mkdir()
    # Profile copying depends on rsync, which is absent on many Windows hosts.
    # Pin the dependency so this argv contract stays deterministic.
    monkeypatch.setattr(
        runner.STATE.shutil,
        "which",
        lambda name: "/usr/bin/rsync" if name == runner.STATE.PROFILE_COPY_DEPENDENCY else None,
    )
    result = execute_run(runner, manifest(tmp_path, copy_profile=str(profile.resolve())), dry_run=True)
    assert result["argv"][result["argv"].index("--copy-profile") + 1] == str(profile.resolve())


def test_default_signed_in_profile_is_copied_per_run_and_window_is_hidden(
    tmp_path: Path, monkeypatch
) -> None:
    runner = load_runner()
    profile = tmp_path.parent / f"{tmp_path.name}-signed-in-oracle-profile"
    profile.mkdir()
    monkeypatch.setenv("ORACLE_BROWSER_PROFILE_DIR", str(profile.resolve()))
    monkeypatch.setattr(
        runner.STATE.shutil,
        "which",
        lambda name: "/usr/bin/rsync" if name == runner.STATE.PROFILE_COPY_DEPENDENCY else None,
    )

    result = execute_run(runner, manifest(tmp_path), dry_run=True)

    assert result["argv"][result["argv"].index("--copy-profile") + 1] == str(profile.resolve())
    assert result["argv"].count("--browser-hide-window") == 1


def test_missing_posix_copy_dependency_still_launches_without_profile_copy(
    tmp_path: Path, monkeypatch
) -> None:
    runner = load_runner()
    profile = tmp_path.parent / f"{tmp_path.name}-signed-in-oracle-profile"
    profile.mkdir()
    monkeypatch.setenv("ORACLE_BROWSER_PROFILE_DIR", str(profile.resolve()))
    monkeypatch.setattr(runner.STATE.shutil, "which", lambda name: None)

    result = execute_run(
        runner, manifest(tmp_path), dry_run=True, platform_name="posix"
    )

    assert "--copy-profile" not in result["argv"]
    assert result["argv"].count("--browser-hide-window") == 1


def test_windows_lanes_keep_profile_isolation_without_rsync(
    tmp_path: Path, monkeypatch
) -> None:
    """Windows uses the pinned native profile copy, so lanes stay isolated.

    Probing PATH for rsync here dropped `--copy-profile` and blocked parallel
    Web Multi lanes before submission.
    """
    runner = load_runner()
    profile = tmp_path.parent / f"{tmp_path.name}-signed-in-oracle-profile"
    profile.mkdir()
    monkeypatch.setenv("ORACLE_BROWSER_PROFILE_DIR", str(profile.resolve()))
    monkeypatch.setattr(runner.STATE.shutil, "which", lambda name: None)

    result = execute_run(runner, manifest(tmp_path), dry_run=True, platform_name="nt")

    assert result["argv"][result["argv"].index("--copy-profile") + 1] == str(
        profile.resolve()
    )
    assert result["argv"].count("--browser-hide-window") == 1


def test_explicit_hide_window_arg_is_safe_and_not_duplicated(tmp_path: Path) -> None:
    runner = load_runner()
    result = execute_run(
        runner,
        manifest(tmp_path, oracle_args=["--browser-hide-window"]),
        dry_run=True,
    )
    assert result["argv"].count("--browser-hide-window") == 1


def test_regular_runs_raise_the_answer_timeout_above_the_upstream_default(
    tmp_path: Path,
) -> None:
    """Heavy Extra High lanes get one explicit overall answer budget."""
    runner = load_runner()

    result = execute_run(runner, manifest(tmp_path), dry_run=True)

    argv = result["argv"]
    assert argv.count("--browser-timeout") == 1
    assert argv[argv.index("--browser-timeout") + 1] == runner.STATE.DEFAULT_BROWSER_ANSWER_TIMEOUT
    assert runner.STATE.DEFAULT_BROWSER_ANSWER_TIMEOUT == "90m"
    assert runner.STATE.DEFAULT_BROWSER_ANSWER_CEILING_MINUTES == 90
    assert result["host_watchdog_timeout_seconds"] == 5430


def test_explicit_answer_timeout_is_honored_without_duplication(tmp_path: Path) -> None:
    runner = load_runner()

    result = execute_run(
        runner,
        manifest(tmp_path, oracle_args=["--browser-timeout", "70m"]),
        dry_run=True,
    )

    argv = result["argv"]
    assert argv.count("--browser-timeout") == 1
    assert argv[argv.index("--browser-timeout") + 1] == "70m"
    assert result["host_watchdog_timeout_seconds"] == 4230


@pytest.mark.parametrize("duration", ["9d", "999999999h", "9" * 400])
def test_answer_timeout_must_produce_a_finite_bounded_host_deadline(
    tmp_path: Path,
    duration: str,
) -> None:
    runner = load_runner()

    with pytest.raises(runner.OracleRunError) as exc:
        execute_run(
            runner,
            manifest(tmp_path, oracle_args=["--browser-timeout", duration]),
            dry_run=True,
        )

    assert exc.value.code in {"BROWSER_TIMEOUT_INVALID", "BROWSER_TIMEOUT_OUT_OF_RANGE"}


def test_pro_keeps_upstream_answer_timing(tmp_path: Path) -> None:
    runner = load_runner()

    result = execute_run(runner, pro_manifest(tmp_path), dry_run=True)

    assert "--browser-timeout" not in result["argv"]
    assert result["host_watchdog_timeout_seconds"] is None


def test_pro_dry_run_uses_oracle_attachments_and_no_app_mention(tmp_path: Path) -> None:
    runner = load_runner()
    result = execute_run(runner, pro_manifest(tmp_path), dry_run=True)
    argv = result["argv"]
    prompt = argv[argv.index("--prompt") + 1]
    attachments = [argv[index + 1] for index, value in enumerate(argv) if value == "--file"]
    assert result["transport"] == "pro-attachment-only"
    assert result["contains_file_flag"] is True
    assert argv[argv.index("--model") + 1] == "gpt-5.5-pro"
    assert argv[argv.index("--browser-attachments") + 1] == "always"
    assert attachments == [
        str((tmp_path / "prompt.txt").resolve()),
        str((tmp_path / "packet.zip").resolve()),
    ]
    assert prompt.startswith(
        "Read the attached prompt/instructions and all attached files, then complete the task. "
        "Task identity: oracle-pro-"
    )
    assert prompt.endswith(".")
    assert "@DevSpace" not in prompt
    assert all(item["sha256"] for item in result["attachments"])


def test_complete_requires_zero_exit_and_nonempty_output(tmp_path: Path) -> None:
    runner = load_runner()
    cases = [
        (0, b"answer", "complete", True),
        (0, b" \n", "attention_required", False),
        (3, b"answer", "attention_required", False),
    ]
    for index, (code, output, status, ok) in enumerate(cases):
        root = tmp_path / str(index)
        root.mkdir()
        captured, events = {}, []
        result = execute_run(runner, manifest(root), run_factory=version_runner, popen_factory=popen_for(code, output, captured, events))
        assert result["ok"] is ok
        assert result["result"]["status"] == status
        assert result["result"]["oracle"]["resolved_version"] == "oracle 0.13.0"
        assert "--file" not in captured["command"]
        assert events == ["popen", "wait"]
        assert Path(result["result"]["artifacts"]["transcript"]).is_file()


def test_v1_task_outcome_separates_transport_success_from_execution(
    tmp_path: Path,
) -> None:
    runner = load_runner()
    (tmp_path / "executed").mkdir()
    (tmp_path / "not-executed").mkdir()
    executed = execute_run(
        runner,
        manifest(
            tmp_path / "executed",
            task_outcome_contract="v1",
            run_id="e" * 32,
        ),
        run_factory=version_runner,
        popen_factory=popen_for(0, b"done\nTASK_OUTCOME: EXECUTED\n", {}, []),
    )
    not_executed = execute_run(
        runner,
        manifest(
            tmp_path / "not-executed",
            task_outcome_contract="v1",
            run_id="n" * 32,
        ),
        run_factory=version_runner,
        popen_factory=popen_for(
            0,
            b"workspace open timed out\nTASK_OUTCOME: NOT_EXECUTED\n",
            {},
            [],
        ),
    )

    assert executed["ok"] is True
    assert executed["result"]["status"] == "complete"
    assert executed["result"]["transport_status"] == "complete"
    assert executed["result"]["task_outcome"] == "executed"
    assert not_executed["ok"] is False
    assert not_executed["result"]["status"] == "attention_required"
    assert not_executed["result"]["transport_status"] == "complete"
    assert not_executed["result"]["task_outcome"] == "not_executed"
    assert not_executed["result"]["session_authority"] == "terminal"
    assert not_executed["result"]["terminal_harvested"] is True


def test_v1_missing_task_outcome_marker_never_claims_execution(tmp_path: Path) -> None:
    runner = load_runner()
    result = execute_run(
        runner,
        manifest(tmp_path, task_outcome_contract="v1"),
        run_factory=version_runner,
        popen_factory=popen_for(0, b"nonempty but semantically ambiguous", {}, []),
    )

    assert result["ok"] is False
    assert result["result"]["status"] == "attention_required"
    assert result["result"]["transport_status"] == "complete"
    assert result["result"]["task_outcome"] == "unknown"


def test_v1_task_outcome_marker_must_be_the_final_nonempty_line(tmp_path: Path) -> None:
    runner = load_runner()
    result = execute_run(
        runner,
        manifest(tmp_path, task_outcome_contract="v1"),
        run_factory=version_runner,
        popen_factory=popen_for(
            0,
            b"TASK_OUTCOME: EXECUTED\nActually no files were changed.\n",
            {},
            [],
        ),
    )

    assert result["ok"] is False
    assert result["result"]["task_outcome"] == "unknown"


def test_devspace_patch_change_blocks_before_submission_until_restart(
    tmp_path: Path,
) -> None:
    runner = load_runner()
    launched = []
    result = runner.execute_run(
        manifest(tmp_path),
        run_factory=version_runner,
        popen_factory=lambda *args, **kwargs: launched.append(True),
        compat_factory=lambda version: {"ok": True, "version": version},
        devspace_compat_factory=lambda: {
            "ok": True,
            "changed": ["dist/workspaces.js"],
            "package_roots": ["package"],
            "service_restart_required": True,
        },
    )

    assert result["ok"] is False
    assert result["result"]["status"] == "failed"
    assert launched == []
    stderr = Path(result["result"]["artifacts"]["stderr"]).read_text(encoding="utf-8")
    assert "DEVSPACE_SERVICE_RESTART_REQUIRED" in stderr


def test_exact_output_hash_adjudication_marks_legacy_task_not_executed(
    tmp_path: Path,
) -> None:
    runner = load_runner()
    completed = execute_run(
        runner,
        manifest(tmp_path),
        run_factory=version_runner,
        popen_factory=popen_for(0, b"workspace timeout; no files changed", {}, []),
    )
    run_dir = Path(completed["run_dir"])
    output = run_dir / "output.md"
    adjudicated = runner.adjudicate_task_outcome(
        run_dir,
        expected_output_sha256=runner.STATE.sha256_file(output),
        task_outcome="not_executed",
        reason="exact output proves workspace open timeout before file reads",
    )

    assert adjudicated["ok"] is False
    assert adjudicated["safe_for_fresh_retry"] is True
    assert adjudicated["task_outcome"] == "not_executed"
    assert adjudicated["result"]["status"] == "complete"
    assert adjudicated["result"]["transport_status"] == "complete"
    assert adjudicated["result"]["session_authority"] == "terminal"


def test_blocked_adjudication_never_authorizes_fresh_retry(tmp_path: Path) -> None:
    runner = load_runner()
    completed = execute_run(
        runner,
        manifest(tmp_path),
        run_factory=version_runner,
        popen_factory=popen_for(0, b"partial work then blocked", {}, []),
    )
    run_dir = Path(completed["run_dir"])
    output = run_dir / "output.md"
    adjudicated = runner.adjudicate_task_outcome(
        run_dir,
        expected_output_sha256=runner.STATE.sha256_file(output),
        task_outcome="blocked",
        reason="partial execution cannot authorize duplicate side effects",
    )

    assert adjudicated["safe_for_fresh_retry"] is False


def test_post_submit_nonzero_requires_exact_recovery_and_never_restarts(tmp_path: Path) -> None:
    runner = load_runner()
    calls = []
    def popen(command, **kwargs):
        calls.append(list(command))
        return Process(9, [])
    result = execute_run(runner, manifest(tmp_path), run_factory=version_runner, popen_factory=popen)
    assert result["result"]["status"] == "attention_required"
    assert result["result"]["session_authority"] == "submitted_unknown"
    assert len(calls) == 1
    assert "restart" not in calls[0]
    for action in ("harvest", "live"):
        recovery = runner.recover_run(Path(result["run_dir"]), action=action, dry_run=True, oracle_command=["oracle"])
        assert f"--{action}" in recovery["argv"]
        assert "--write-output" in recovery["argv"]
        assert "--no-recover" not in recovery["argv"]
        assert "restart" not in recovery["argv"]
        assert "--prompt" not in recovery["argv"]


@pytest.mark.parametrize("parallel_parent_id", [None, "d" * 64])
def test_post_submit_host_watchdog_preserves_exact_process_and_returns_attention(
    tmp_path: Path,
    parallel_parent_id: str | None,
) -> None:
    runner = load_runner()
    waits: list[float | None] = []
    process_actions: list[str] = []
    launches: list[list[str]] = []

    class HungProcess:
        pid = 4242

        def wait(self, timeout=None):
            waits.append(timeout)
            raise subprocess.TimeoutExpired("oracle", timeout)

        def terminate(self):
            process_actions.append("terminate")

        def kill(self):
            process_actions.append("kill")

    def hung_popen(command, **kwargs):
        launches.append(list(command))
        kwargs["stdout"].write(b"Session: exact\nprompt submitted; response streaming\n")
        kwargs["stdout"].flush()
        return HungProcess()

    extras = {
        "oracle_args": ["--browser-timeout", "1s"],
        "run_id": "4" * 32,
    }
    if parallel_parent_id is not None:
        extras["parallel_parent_id"] = parallel_parent_id
    result = execute_run(
        runner,
        manifest(tmp_path, **extras),
        run_factory=version_runner,
        popen_factory=hung_popen,
    )
    state = result["result"]

    assert result["ok"] is False
    assert result["status"] == "post_submit_watchdog_timeout"
    assert result["safe_for_fresh_run"] is False
    assert result["process_preserved"] is True
    assert result["oracle_process_pid"] == 4242
    assert waits == [31]
    assert process_actions == []
    assert len(launches) == 1
    assert state["status"] == "attention_required"
    assert state["exit_code"] is None
    assert state["session_authority"] == "submitted_unknown"
    assert state["terminal_harvested"] is False
    assert state["transport_status"] == "post_submit_watchdog_timeout"
    assert state["task_outcome_reason"] == "host-wall-clock-expired-process-preserved"
    assert state["host_watchdog"] == {
        "status": "expired",
        "timeout_seconds": 31,
        "oracle_process_pid": 4242,
        "process_action": "preserved",
        "next_action": "observe-or-recover-exact-session-only",
    }
    assert Path(state["artifacts"]["browser_temp"]).is_dir()
    assert not Path(state["artifacts"]["output"]).exists()
    assert not list(Path(result["run_dir"]).glob("recovery-*-stdout.log"))


def test_host_watchdog_deadline_race_accepts_only_a_process_that_already_exited(
    tmp_path: Path,
) -> None:
    runner = load_runner()
    output_path: Path | None = None

    class RacedExitProcess:
        pid = 4343

        def wait(self, timeout=None):
            assert timeout == 31
            assert output_path is not None
            output_path.write_text("durable answer\nTASK_OUTCOME: EXECUTED\n", encoding="utf-8")
            raise subprocess.TimeoutExpired("oracle", timeout)

        def poll(self):
            return 0

    def raced_popen(command, **kwargs):
        nonlocal output_path
        output_path = Path(command[command.index("--write-output") + 1])
        return RacedExitProcess()

    result = execute_run(
        runner,
        manifest(
            tmp_path,
            oracle_args=["--browser-timeout", "1s"],
            run_id="5" * 32,
            task_outcome_contract="v1",
        ),
        run_factory=version_runner,
        popen_factory=raced_popen,
    )

    assert result["ok"] is True
    assert result["result"]["status"] == "complete"
    assert result["result"]["session_authority"] == "terminal"
    assert result["result"]["terminal_harvested"] is True
    assert result["result"]["host_watchdog"]["status"] == "process-exited"


def test_pro_recovery_uses_exact_slug_without_attachments_or_resubmit(tmp_path: Path) -> None:
    runner = load_runner()
    result = execute_run(
        runner,
        pro_manifest(tmp_path),
        run_factory=version_runner,
        popen_factory=popen_for(4, None, {}, []),
    )
    state = runner.STATE.load_state(Path(result["run_dir"]) / "state.json")
    recovery = runner.recover_run(
        Path(result["run_dir"]),
        action="harvest",
        dry_run=True,
        oracle_command=["oracle"],
    )
    argv = recovery["argv"]
    assert argv[argv.index("session") + 1] == state["oracle"]["slug"]
    assert "--prompt" not in argv
    assert "--file" not in argv
    assert "--browser-attachments" not in argv
    assert "--no-recover" not in argv


def test_windows_launch_uses_no_window_and_waits(tmp_path: Path) -> None:
    runner = load_runner()
    captured, events = {}, []
    class Mutex:
        def __enter__(self):
            events.append("enter")
        def __exit__(self, *args):
            events.append("exit")
    runner.STATE.project_submit_mutex = lambda *args, **kwargs: Mutex()
    result = execute_run(
        runner,
        manifest(tmp_path),
        run_factory=version_runner,
        popen_factory=popen_for(0, b"answer", captured, events),
        platform_name="nt",
    )
    assert result["ok"] is True
    assert captured["kwargs"]["creationflags"] & runner.STATE.CREATE_NO_WINDOW
    assert Path(captured["kwargs"]["env"]["TEMP"]).name == "browser-temp"
    assert captured["kwargs"]["env"]["TMP"] == captured["kwargs"]["env"]["TEMP"]
    assert not Path(captured["kwargs"]["env"]["TEMP"]).exists()
    assert events == ["enter", "popen", "wait", "exit"]


def test_transport_mission_change_blocks_before_oracle_launch(tmp_path: Path) -> None:
    runner = load_runner()
    launched = []

    class MutatingMutex:
        def __enter__(self):
            transport = next((tmp_path / "runs").glob("*/mission.md"))
            transport.write_text("changed", encoding="utf-8")

        def __exit__(self, *args):
            return None

    runner.STATE.project_submit_mutex = lambda *args, **kwargs: MutatingMutex()

    def forbidden_popen(*args, **kwargs):
        launched.append(True)
        raise AssertionError("Oracle must not launch with changed mission bytes")

    result = execute_run(
        runner,
        manifest(tmp_path),
        run_factory=version_runner,
        popen_factory=forbidden_popen,
    )
    assert result["ok"] is False
    assert result["result"]["status"] == "failed"
    assert launched == []


def test_pro_attachment_change_blocks_before_submit(tmp_path: Path) -> None:
    runner = load_runner()
    launched = []

    class MutatingMutex:
        def __enter__(self):
            (tmp_path / "packet.zip").write_bytes(b"changed")

        def __exit__(self, *args):
            return None

    runner.STATE.project_submit_mutex = lambda *args, **kwargs: MutatingMutex()

    def forbidden_popen(*args, **kwargs):
        launched.append(True)
        raise AssertionError("Oracle must not launch with changed attachments")

    result = execute_run(
        runner,
        pro_manifest(tmp_path),
        run_factory=version_runner,
        popen_factory=forbidden_popen,
    )
    assert result["ok"] is False
    assert result["result"]["status"] == "failed"
    assert result["result"]["session_authority"] == "pre_submit"
    assert launched == []


def test_oracle_global_prompt_duplicate_is_proven_pre_submit_and_releases_project(tmp_path: Path) -> None:
    runner = load_runner()
    first = execute_run(
        runner,
        pro_manifest(tmp_path, run_id="a" * 32),
        run_factory=version_runner,
        popen_factory=duplicate_prompt_popen,
    )
    first_state = runner.STATE.load_state(Path(first["run_dir"]) / "state.json")
    assert first["status"] == "pre_submit_rejected"
    assert first["safe_for_fresh_run"] is True
    assert first_state["session_authority"] == "pre_submit"
    assert first_state["transport_status"] == "rejected_pre_submit"
    assert first_state["pre_submit_rejection"]["code"] == "ORACLE_GLOBAL_PROMPT_DUPLICATE"
    assert first_state["pre_submit_rejection"]["output_absent"] is True
    assert runner.STATE.unresolved_project_sessions(
        runner.STATE.load_manifest(pro_manifest(tmp_path)).run_root,
        tmp_path,
    ) == []

    launches: list[list[str]] = []
    second = execute_run(
        runner,
        pro_manifest(tmp_path, run_id="b" * 32),
        run_factory=version_runner,
        popen_factory=popen_for(0, b"answer", {}, launches),
    )
    assert second["ok"] is True
    assert launches


def test_attachment_size_preflight_is_proven_pre_submit_and_releases_project(tmp_path: Path) -> None:
    runner = load_runner()
    manifest_path = pro_manifest(tmp_path, run_id="c" * 32)
    (tmp_path / "packet.zip").write_bytes(
        b"x" * (runner.STATE.ORACLE_ATTACHMENT_SIZE_LIMIT_BYTES + 1)
    )

    result = execute_run(
        runner,
        manifest_path,
        run_factory=version_runner,
        popen_factory=attachment_size_popen,
    )
    run_dir = Path(result["run_dir"])
    state = runner.STATE.load_state(run_dir / "state.json")

    assert result["status"] == "pre_submit_failed"
    assert result["safe_for_fresh_run"] is True
    assert state["session_authority"] == "pre_submit"
    assert state["transport_status"] == "rejected_pre_submit"
    assert state["pre_submit_failure"]["code"] == "ORACLE_ATTACHMENT_SIZE_PREFLIGHT_REJECTED"
    assert runner.STATE.unresolved_project_sessions(
        runner.STATE.load_manifest(manifest_path).run_root,
        tmp_path,
    ) == []


def test_recovery_settles_legacy_duplicate_prompt_lock_without_oracle_call(tmp_path: Path) -> None:
    runner = load_runner()
    initial = execute_run(
        runner,
        pro_manifest(tmp_path, run_id="a" * 32),
        run_factory=version_runner,
        popen_factory=duplicate_prompt_popen,
    )
    run_dir = Path(initial["run_dir"])
    state_path = run_dir / "state.json"
    legacy = json.loads(state_path.read_text(encoding="utf-8"))
    legacy["session_authority"] = "submitted_unknown"
    legacy["transport_status"] = "incomplete"
    legacy.pop("pre_submit_rejection", None)
    state_path.write_text(json.dumps(legacy), encoding="utf-8")
    calls = []

    recovered = runner.recover_run(
        run_dir,
        action="harvest",
        oracle_command=["oracle"],
        popen_factory=lambda *args, **kwargs: calls.append(True),
    )
    settled = runner.STATE.load_state(state_path)
    assert recovered["status"] == "pre_submit_rejected"
    assert recovered["safe_for_fresh_run"] is True
    assert settled["session_authority"] == "pre_submit"
    assert calls == []


def test_version_resolution_timeout_is_proven_pre_submit_and_releases_project(tmp_path: Path) -> None:
    runner = load_runner()
    result = execute_run(
        runner,
        manifest(tmp_path, run_id="c" * 32),
        run_factory=version_timeout_runner,
        popen_factory=lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("Oracle must not launch after version timeout")
        ),
    )
    run_dir = Path(result["run_dir"])
    state = runner.STATE.load_state(run_dir / "state.json")

    assert result["status"] == "pre_submit_failed"
    assert result["safe_for_fresh_run"] is True
    assert state["session_authority"] == "pre_submit"
    assert state["transport_status"] == "failed_pre_submit"
    assert state["pre_submit_failure"]["code"] == "ORACLE_VERSION_RESOLUTION_PRELAUNCH_FAILED"
    assert state["pre_submit_failure"]["conversation_url_absent"] is True
    assert runner.STATE.unresolved_project_sessions(
        runner.STATE.load_manifest(manifest(tmp_path)).run_root,
        tmp_path,
    ) == []


def test_recovery_repairs_legacy_version_timeout_authority_without_oracle_call(tmp_path: Path) -> None:
    runner = load_runner()
    initial = execute_run(
        runner,
        manifest(tmp_path, run_id="d" * 32),
        run_factory=version_timeout_runner,
    )
    run_dir = Path(initial["run_dir"])
    state_path = run_dir / "state.json"
    legacy = json.loads(state_path.read_text(encoding="utf-8"))
    legacy["session_authority"] = "submitted_unknown"
    legacy["transport_status"] = "incomplete"
    legacy.pop("pre_submit_failure", None)
    state_path.write_text(json.dumps(legacy), encoding="utf-8")
    calls: list[bool] = []

    recovered = runner.recover_run(
        run_dir,
        action="harvest",
        oracle_command=["oracle"],
        popen_factory=lambda *args, **kwargs: calls.append(True),
    )
    settled = runner.STATE.load_state(state_path)

    assert recovered["status"] == "pre_submit_failed"
    assert recovered["safe_for_fresh_run"] is True
    assert settled["session_authority"] == "pre_submit"
    assert calls == []


def test_recovery_no_session_keeps_pre_submit_authority_and_allows_fresh_attempt(tmp_path: Path) -> None:
    runner = load_runner()
    config = runner.STATE.load_manifest(manifest(tmp_path, run_id="e" * 32))
    layout = runner.STATE.create_layout(config, run_id=config.requested_run_id)
    layout.run_dir.mkdir(parents=True)
    runner.STATE.write_json_atomic(
        layout.state_path,
        runner.STATE.state_payload(config, layout, status="failed", resolved_version="oracle 0.16.1"),
    )
    for path in (layout.stdout_path, layout.stderr_path):
        path.touch()

    def no_session(command, **kwargs):
        kwargs["stderr"].write(f"No session found with ID {layout.slug}.\n".encode())
        kwargs["stderr"].flush()
        return Process(1, [])

    recovered = runner.recover_run(
        layout.run_dir,
        action="harvest",
        oracle_command=["oracle"],
        popen_factory=no_session,
    )
    settled = runner.STATE.load_state(layout.state_path)

    assert recovered["status"] == "pre_submit_session_absent"
    assert recovered["safe_for_fresh_run"] is True
    assert settled["session_authority"] == "pre_submit"
    assert settled["pre_submit_session_absence"]["oracle_locator"] == layout.slug


def test_recovery_no_session_never_releases_submitted_unknown_run(tmp_path: Path) -> None:
    runner = load_runner()
    config = runner.STATE.load_manifest(manifest(tmp_path, run_id="f" * 32))
    layout = runner.STATE.create_layout(config, run_id=config.requested_run_id)
    layout.run_dir.mkdir(parents=True)
    state = runner.STATE.state_payload(config, layout, status="attention_required", resolved_version="oracle 0.16.1")
    state["session_authority"] = "submitted_unknown"
    runner.STATE.write_json_atomic(layout.state_path, state)
    for path in (layout.stdout_path, layout.stderr_path):
        path.touch()

    def no_session(command, **kwargs):
        kwargs["stderr"].write(f"No session found with ID {layout.slug}.\n".encode())
        kwargs["stderr"].flush()
        return Process(1, [])

    recovered = runner.recover_run(
        layout.run_dir,
        action="live",
        oracle_command=["oracle"],
        popen_factory=no_session,
    )
    settled = runner.STATE.load_state(layout.state_path)

    assert recovered["status"] == "attention_required"
    assert recovered.get("safe_for_fresh_run") is not True
    assert settled["session_authority"] == "submitted_unknown"


def test_user_confirmed_no_submission_is_hash_bound_idempotent_and_fail_closed(tmp_path: Path) -> None:
    runner = load_runner()
    run_id = "a" * 32
    workflow_id = "b4362f04-3cf2-4f5e-b6a2-8d9443175298"
    parallel_parent_id = hashlib.sha256(workflow_id.encode("utf-8")).hexdigest()
    manifest_path = manifest(
        tmp_path,
        run_id=run_id,
        parallel_parent_id=parallel_parent_id,
    )
    input_mission = tmp_path / "input.md"
    input_mission.write_text("bound input", encoding="utf-8")
    input_sha = hashlib.sha256(input_mission.read_bytes()).hexdigest()
    (tmp_path / "mission.md").write_text(
        "\n".join((
            "mission body",
            "",
            "[HOST_STAGE_CONTRACT]",
            f"workflow_id={workflow_id}",
            "stage=implementation",
            f"attempt_id={run_id}",
            f"input_mission_sha256={input_sha}",
            f"exact_project_root={tmp_path.resolve()}",
            f"exact_input_mission_path={input_mission.resolve()}",
            f"Write the small UTF-8 stage receipt to: {(tmp_path / 'stage-result.json').resolve()}",
            "",
            "[DEVSPACE_WORKSPACE_ENTRY_CONTRACT]",
            "workspace body",
            "",
        )),
        encoding="utf-8",
    )

    def prompt_not_observed(command, **kwargs):
        slug = command[command.index("--slug") + 1]
        kwargs["stdout"].write(
            (
                f"Session: {slug}\n"
                "ERROR: Prompt did not appear in conversation before timeout (send may have failed)\n"
            ).encode()
        )
        kwargs["stdout"].flush()
        return Process(1, [])

    failed = execute_run(
        runner,
        manifest_path,
        run_factory=version_runner,
        popen_factory=prompt_not_observed,
    )
    run_dir = Path(failed["run_dir"])
    state_path = run_dir / "state.json"
    state = runner.STATE.load_state(state_path)
    slug = state["oracle"]["slug"]
    recovery_stdout = run_dir / "recovery-harvest-stdout.log"
    recovery_stderr = run_dir / "recovery-harvest-stderr.log"
    recovery_stdout.write_text(
        f'No live ChatGPT tab matched session "{slug}". Attempting recovery.\n',
        encoding="utf-8",
    )
    recovery_stderr.write_text(
        "Cannot recover conversation: session metadata has no recoverable ChatGPT conversation URL.\n",
        encoding="utf-8",
    )

    assert runner.exact_recovery_binding_unavailable(recovery_stdout, recovery_stderr) is True
    settled = runner.settle_user_confirmed_no_submission(
        run_dir,
        confirmation=runner.STATE.USER_CONFIRMED_NO_SUBMISSION,
        reason="user inspected the exact ChatGPT state and confirmed no submission",
    )
    settlement_path = run_dir / "user-confirmed-no-submission.json"
    proof = runner.STATE.proven_user_confirmed_no_submission(state_path)

    assert settled["ok"] is True
    assert settled["safe_for_fresh_run"] is True
    assert settled["result"]["session_authority"] == "pre_submit"
    assert proof is not None
    assert proof["workflow_id"] == workflow_id
    assert proof["stage"] == "implementation"
    assert proof["attempt_id"] == run_id
    assert proof["input_mission_sha256"] == input_sha
    assert settlement_path.is_file()
    # Repeating the exact adjudication is idempotent and launches nothing.
    repeated = runner.settle_user_confirmed_no_submission(
        run_dir,
        confirmation=runner.STATE.USER_CONFIRMED_NO_SUBMISSION,
        reason="user inspected the exact ChatGPT state and confirmed no submission",
    )
    assert repeated["result"] == settled["result"]
    other_run_id = "9" * 32
    other_state_path = run_dir.parent / other_run_id / "state.json"
    other_state_path.parent.mkdir()
    other_state = {
        "schema": "codex.chatgpt.oracle-run-state/v1",
        "run_id": other_run_id,
        "project_root": str(tmp_path.resolve()),
        "status": "running",
        "session_authority": "submitted_unknown",
        "oracle": {"session_locator": "oracle-project-other"},
    }
    runner.STATE.write_json_atomic(other_state_path, other_state)
    blocked = runner.settle_user_confirmed_no_submission(
        run_dir,
        confirmation=runner.STATE.USER_CONFIRMED_NO_SUBMISSION,
        reason="user inspected the exact ChatGPT state and confirmed no submission",
    )
    assert blocked["safe_for_fresh_run"] is False
    assert [owner["run_id"] for owner in blocked["unresolved_owners"]] == [other_run_id]
    other_state.update({"status": "attention_required", "session_authority": "pre_submit"})
    runner.STATE.write_json_atomic(other_state_path, other_state)
    assert runner.STATE.unresolved_project_sessions(
        runner.STATE.load_manifest(manifest_path).run_root,
        tmp_path,
        parallel_parent_id="e" * 64,
    ) == []

    reference = settled["result"]["user_confirmed_no_submission"]
    missing_reference_state = runner.STATE.load_state(state_path)
    missing_reference_state.pop("user_confirmed_no_submission")
    runner.STATE.write_json_atomic(state_path, missing_reference_state)
    owners = runner.STATE.unresolved_project_sessions(
        runner.STATE.load_manifest(manifest_path).run_root,
        tmp_path,
        parallel_parent_id=parallel_parent_id,
    )
    assert owners[0]["run_id"] == run_id
    restored = runner.STATE.load_state(state_path)
    restored["user_confirmed_no_submission"] = reference
    runner.STATE.write_json_atomic(state_path, restored)

    # Any contradictory later recovery revokes the release even though the
    # original no-tab/no-URL recovery still exists.
    (run_dir / "recovery-live-stdout.log").write_text(
        "State: running\n",
        encoding="utf-8",
    )
    (run_dir / "recovery-live-stderr.log").write_text("", encoding="utf-8")
    assert runner.STATE.proven_user_confirmed_no_submission(state_path) is None
    owners = runner.STATE.unresolved_project_sessions(
        runner.STATE.load_manifest(manifest_path).run_root,
        tmp_path,
        parallel_parent_id="e" * 64,
    )
    assert owners[0]["run_id"] == run_id


def test_user_confirmation_rejects_bare_bindings_without_host_contract(tmp_path: Path) -> None:
    runner = load_runner()
    run_id = "e" * 32
    workflow_id = "b4362f04-3cf2-4f5e-b6a2-8d9443175298"
    parent_id = hashlib.sha256(workflow_id.encode("utf-8")).hexdigest()
    manifest_path = manifest(tmp_path, run_id=run_id, parallel_parent_id=parent_id)
    (tmp_path / "mission.md").write_text(
        "\n".join((
            f"workflow_id={workflow_id}",
            "stage=implementation",
            f"attempt_id={run_id}",
            f"input_mission_sha256={'f' * 64}",
            "",
        )),
        encoding="utf-8",
    )

    def prompt_not_observed(command, **kwargs):
        slug = command[command.index("--slug") + 1]
        kwargs["stdout"].write(
            (
                f"Session: {slug}\n"
                "ERROR: Prompt did not appear in conversation before timeout (send may have failed)\n"
            ).encode()
        )
        kwargs["stdout"].flush()
        return Process(1, [])

    failed = execute_run(
        runner,
        manifest_path,
        run_factory=version_runner,
        popen_factory=prompt_not_observed,
    )
    run_dir = Path(failed["run_dir"])
    state = runner.STATE.load_state(run_dir / "state.json")
    slug = state["oracle"]["slug"]
    (run_dir / "recovery-harvest-stdout.log").write_text(
        f'No live ChatGPT tab matched session "{slug}". Attempting recovery.\n',
        encoding="utf-8",
    )
    (run_dir / "recovery-harvest-stderr.log").write_text(
        "Cannot recover conversation: session metadata has no recoverable ChatGPT conversation URL.\n",
        encoding="utf-8",
    )

    with pytest.raises(runner.STATE.OracleStateError) as exc:
        runner.settle_user_confirmed_no_submission(
            run_dir,
            confirmation=runner.STATE.USER_CONFIRMED_NO_SUBMISSION,
            reason="user said no submission",
        )
    assert exc.value.code == "NO_SUBMISSION_EVIDENCE_INCOMPLETE"


def test_user_confirmation_cannot_replace_missing_recovery_evidence(tmp_path: Path) -> None:
    runner = load_runner()
    config = runner.STATE.load_manifest(manifest(tmp_path, run_id="f" * 32))
    layout = runner.STATE.create_layout(config, run_id=config.requested_run_id)
    layout.run_dir.mkdir(parents=True)
    state = runner.STATE.state_payload(config, layout, status="attention_required", resolved_version="0.16.1")
    state["session_authority"] = "submitted_unknown"
    runner.STATE.write_json_atomic(layout.state_path, state)
    for path in (layout.stdout_path, layout.stderr_path):
        path.touch()

    with pytest.raises(runner.STATE.OracleStateError) as exc:
        runner.settle_user_confirmed_no_submission(
            layout.run_dir,
            confirmation=runner.STATE.USER_CONFIRMED_NO_SUBMISSION,
            reason="user said no submission",
        )
    assert exc.value.code == "NO_SUBMISSION_EVIDENCE_INCOMPLETE"


def test_recovery_captures_output_and_updates_state(tmp_path: Path) -> None:
    runner = load_runner()
    result = execute_run(
        runner,
        manifest(tmp_path),
        run_factory=version_runner,
        popen_factory=popen_for(4, None, {}, []),
    )
    run_dir = Path(result["run_dir"])

    def recovery_popen(command, **kwargs):
        captured_env.update(kwargs["env"])
        output = Path(command[command.index("--write-output") + 1])
        output.write_text("recovered answer", encoding="utf-8")
        kwargs["stdout"].write(b"State: complete\n")
        kwargs["stdout"].flush()
        return Process(0, [])

    captured_env = {}
    recovered = runner.recover_run(
        run_dir,
        action="harvest",
        oracle_command=["oracle"],
        popen_factory=recovery_popen,
    )
    assert recovered["ok"] is True
    assert recovered["status"] == "complete"
    assert Path(recovered["output_path"]).read_text(encoding="utf-8") == "recovered answer"
    assert recovered["result"]["status"] == "complete"
    assert Path(captured_env["TEMP"]).name == "recovery-harvest-browser-temp"
    assert not Path(captured_env["TEMP"]).exists()
    transcript = Path(recovered["result"]["artifacts"]["transcript"]).read_text(encoding="utf-8")
    assert "recovered answer" in transcript


def test_running_exact_session_cannot_publish_partial_harvest(tmp_path: Path) -> None:
    runner = load_runner()
    result = execute_run(
        runner,
        manifest(tmp_path),
        run_factory=version_runner,
        popen_factory=popen_for(0, None, {}, []),
    )
    run_dir = Path(result["run_dir"])

    def live_harvest(command, **kwargs):
        candidate = Path(command[command.index("--write-output") + 1])
        candidate.write_text("partial answer still flushing", encoding="utf-8")
        kwargs["stdout"].write(b"State: running\nSignals: stop=yes send=no\n")
        kwargs["stdout"].flush()
        return Process(0, [])

    recovered = runner.recover_run(
        run_dir,
        action="harvest",
        oracle_command=["oracle"],
        popen_factory=live_harvest,
    )

    state = runner.STATE.load_state(run_dir / "state.json")
    assert recovered["status"] == "session_live"
    assert recovered["ok"] is False
    assert state["session_authority"] == "live"
    assert state["terminal_harvested"] is False
    assert not Path(state["artifacts"]["output"]).exists()
    assert not (run_dir / "recovery-harvest-candidate.md").exists()


def test_terminal_observation_cannot_regress_to_live_and_later_harvest_settles(tmp_path: Path) -> None:
    runner = load_runner()
    result = execute_run(
        runner,
        manifest(tmp_path),
        run_factory=version_runner,
        popen_factory=popen_for(0, None, {}, []),
    )
    run_dir = Path(result["run_dir"])

    def observation(state: str, answer: str | None = None):
        def popen(command, **kwargs):
            if answer is not None:
                Path(command[command.index("--write-output") + 1]).write_text(answer, encoding="utf-8")
            kwargs["stdout"].write(f"State: {state}\n".encode())
            kwargs["stdout"].flush()
            return Process(0, [])
        return popen

    terminal = runner.recover_run(
        run_dir,
        action="live",
        oracle_command=["oracle"],
        popen_factory=observation("completed"),
    )
    # Reproduce state already regressed by the previously installed runner;
    # the durable exact live-observer log must restore terminal authority.
    regressed = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    regressed["status"] = "running"
    regressed["session_authority"] = "live"
    (run_dir / "state.json").write_text(json.dumps(regressed), encoding="utf-8")
    disagreement = runner.recover_run(
        run_dir,
        action="harvest",
        oracle_command=["oracle"],
        popen_factory=observation("running", "partial"),
    )
    output_absent_during_disagreement = not Path(
        disagreement["result"]["artifacts"]["output"]
    ).exists()
    duplicate_launches: list[list[str]] = []
    blocked_duplicate = execute_run(
        runner,
        manifest(tmp_path, run_id="b" * 32),
        run_factory=version_runner,
        popen_factory=popen_for(0, b"duplicate", {}, duplicate_launches),
    )
    settled = runner.recover_run(
        run_dir,
        action="harvest",
        oracle_command=["oracle"],
        popen_factory=observation("completed", "durable answer"),
    )

    assert terminal["status"] == "terminal_observed"
    assert terminal["result"]["session_authority"] == "terminal_observed"
    assert disagreement["status"] == "terminal_settle_disagreement"
    assert disagreement["result"]["status"] == "attention_required"
    assert disagreement["result"]["session_authority"] == "terminal_observed"
    assert disagreement["result"]["terminal_harvested"] is False
    assert output_absent_during_disagreement
    assert blocked_duplicate["ok"] is False
    assert duplicate_launches == []
    assert "still owns this project" in Path(
        blocked_duplicate["result"]["artifacts"]["stderr"]
    ).read_text(encoding="utf-8")
    assert settled["ok"] is True
    assert settled["status"] == "complete"
    assert settled["result"]["session_authority"] == "terminal"
    assert Path(settled["output_path"]).read_text(encoding="utf-8") == "durable answer"


def test_live_recovery_settles_stalled_inside_one_exact_slug_process(
    tmp_path: Path,
) -> None:
    runner = load_runner()
    initial = execute_run(
        runner,
        manifest(tmp_path),
        run_factory=version_runner,
        popen_factory=popen_for(7, None, {}, []),
    )
    run_dir = Path(initial["run_dir"])
    runner.STATE.update_state(
        run_dir / "state.json",
        status="running",
        exit_code=7,
        session_authority="submitted_unknown",
    )
    calls: list[str] = []

    def recovery(command, **kwargs):
        action = "harvest" if "--harvest" in command else "live"
        calls.append(action)
        if calls == ["live"]:
            kwargs["stdout"].write(b"State: stalled\n")
        elif action == "live":
            kwargs["stdout"].write(b"State: completed\n")
        else:
            candidate = Path(command[command.index("--write-output") + 1])
            candidate.write_text("durable exact answer", encoding="utf-8")
            kwargs["stdout"].write(b"State: completed\n")
        kwargs["stdout"].flush()
        return Process(0, [])

    settled = runner.recover_run(
        run_dir,
        action="live",
        oracle_command=["oracle"],
        popen_factory=recovery,
        settle_timeout_seconds=5,
        settle_interval_seconds=0,
        sleep=lambda _: None,
    )

    assert calls == ["live", "live", "harvest"]
    assert settled["ok"] is True
    assert settled["status"] == "complete"
    assert settled["result"]["session_authority"] == "terminal"
    assert settled["result"]["terminal_harvested"] is True
    assert Path(settled["output_path"]).read_text(encoding="utf-8") == "durable exact answer"


def test_live_recovery_cli_defaults_to_one_ninety_minute_settle_process() -> None:
    runner = load_runner()
    args = runner.build_parser().parse_args([
        "recover", "--run-dir", r"C:\host-state\exact-run", "--action", "live",
    ])
    assert args.settle_timeout_seconds == 5400
    assert args.settle_interval_seconds == 15


def test_live_recovery_returns_once_when_exact_binding_is_unavailable(
    tmp_path: Path,
) -> None:
    runner = load_runner()
    initial = execute_run(
        runner,
        manifest(tmp_path),
        run_factory=version_runner,
        popen_factory=popen_for(7, None, {}, []),
    )
    run_dir = Path(initial["run_dir"])
    calls: list[str] = []
    sleeps: list[float] = []

    def no_binding(command, **kwargs):
        calls.append("live")
        kwargs["stdout"].write(
            b'No live ChatGPT tab matched session "exact". Attempting recovery by reopening the saved conversation URL.\n'
            b'Cannot recover conversation: session metadata has no recoverable ChatGPT conversation URL.\n'
        )
        kwargs["stdout"].flush()
        return Process(1, [])

    result = runner.recover_run(
        run_dir,
        action="live",
        oracle_command=["oracle"],
        popen_factory=no_binding,
        settle_timeout_seconds=5400,
        settle_interval_seconds=15,
        sleep=sleeps.append,
    )

    assert calls == ["live"]
    assert sleeps == []
    assert result["ok"] is False
    assert result["status"] == "recovery_binding_unavailable"
    assert result["exact_session_state"] is None
    assert "never replace or resubmit" in result["next_action"]
    assert result["result"]["status"] == "attention_required"
    assert result["result"]["session_authority"] == "submitted_unknown"
    assert result["result"]["terminal_harvested"] is False
    assert not (run_dir / "recovery-live-candidate.md").exists()


def test_unresolved_exact_session_blocks_different_parent_submission(tmp_path: Path) -> None:
    runner = load_runner()
    first_parent = "a" * 64
    second_parent = "b" * 64
    first = execute_run(
        runner,
        manifest(tmp_path, run_id="a" * 32, parallel_parent_id=first_parent),
        run_factory=version_runner,
        popen_factory=popen_for(0, None, {}, []),
    )
    launches: list[list[str]] = []

    def forbidden_launch(command, **kwargs):
        launches.append(list(command))
        raise AssertionError("a different workflow must not submit while the exact session owns the project")

    second = execute_run(
        runner,
        manifest(tmp_path, run_id="b" * 32, parallel_parent_id=second_parent),
        run_factory=version_runner,
        popen_factory=forbidden_launch,
    )

    assert first["result"]["session_authority"] == "submitted_unknown"
    assert second["ok"] is False
    assert second["result"]["status"] == "failed"
    assert launches == []
    assert "still owns this project" in Path(second["result"]["artifacts"]["stderr"]).read_text(encoding="utf-8")


def test_legacy_attention_without_session_authority_is_not_a_permanent_project_lock(tmp_path: Path) -> None:
    runner = load_runner()
    first = execute_run(
        runner,
        manifest(tmp_path, run_id="a" * 32),
        run_factory=version_runner,
        popen_factory=popen_for(0, None, {}, []),
    )
    first_state_path = Path(first["run_dir"]) / "state.json"
    first_state = json.loads(first_state_path.read_text(encoding="utf-8"))
    first_state["status"] = "attention_required"
    first_state.pop("session_authority", None)
    first_state_path.write_text(json.dumps(first_state), encoding="utf-8")

    launches: list[list[str]] = []
    second = execute_run(
        runner,
        manifest(tmp_path, run_id="b" * 32),
        run_factory=version_runner,
        popen_factory=popen_for(0, b"answer", {}, launches),
    )

    assert second["ok"] is True
    assert launches


def test_recovery_never_downgrades_durable_complete(tmp_path: Path) -> None:
    runner = load_runner()
    result = execute_run(
        runner,
        manifest(tmp_path),
        run_factory=version_runner,
        popen_factory=popen_for(0, b"answer", {}, []),
    )
    calls = []
    recovered = runner.recover_run(
        Path(result["run_dir"]),
        action="harvest",
        oracle_command=["oracle"],
        popen_factory=lambda *args, **kwargs: calls.append(True),
    )
    assert recovered["ok"] is True
    assert recovered["monotonic_noop"] is True
    assert calls == []


def test_parallel_recovery_reuses_the_parent_scoped_submit_mutex(tmp_path: Path) -> None:
    runner = load_runner()
    parent_id = "a" * 32
    roots: list[Path] = []

    class Mutex:
        def __init__(self, root: Path):
            self.root = root

        def __enter__(self):
            roots.append(self.root)

        def __exit__(self, *args):
            return None

    runner.STATE.project_submit_mutex = lambda root, **kwargs: Mutex(root)
    result = execute_run(
        runner,
        manifest(tmp_path, parallel_parent_id=parent_id),
        run_factory=version_runner,
        popen_factory=popen_for(4, None, {}, []),
    )
    recovered = runner.recover_run(Path(result["run_dir"]), action="harvest", dry_run=True, oracle_command=["oracle"])
    expected = tmp_path.resolve() / ".oracle-parallel-submit" / parent_id
    assert result["result"]["status"] == "attention_required"
    assert recovered["status"] == "dry-run"
    assert roots == [expected, expected]
