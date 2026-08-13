from __future__ import annotations

import importlib.util
import json
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def harness(tmp_path: Path):
    module = load("codexpro_harness_test", ROOT / "bin" / "codexpro_harness.py")
    project = tmp_path / "project"
    project.mkdir()
    mission = project / "mission.md"
    mission.write_text("bounded mission\n", encoding="utf-8")
    started = datetime(2026, 8, 11, tzinfo=timezone.utc)
    state = module.start_run(project, mission, root=tmp_path / "state", now=started, codex_session_id="session-1", owner_pid=999999)
    path = module.run_dir_for(tmp_path / "state", project, state["run_id"]) / "run.json"
    return module, project, started, path


def test_75_80_state_machine_requires_explicit_owner_release(tmp_path: Path) -> None:
    module, _, started, path = harness(tmp_path)
    assert module.evaluate(path, now=started + timedelta(seconds=4499))["phase"] == "RUNNING"
    checkpoint = module.evaluate(path, now=started + timedelta(seconds=4500))
    assert checkpoint["phase"] == "CHECKPOINT_DUE" and checkpoint["fanout_locked"]
    assert module.evaluate(path, now=started + timedelta(seconds=4800))["phase"] == "HANDOFF_PENDING"

    module.release_owner(path, session_id="session-1")
    ready = module.evaluate(path, now=started + timedelta(seconds=4801))
    assert ready["phase"] == "READY_NEXT_EPISODE"
    assert (path.parent / "handoff.md").is_file()


def test_live_oracle_owns_exact_recovery_and_never_becomes_new_episode(tmp_path: Path) -> None:
    module, _, started, path = harness(tmp_path)
    oracle_dir = tmp_path / "oracle-run"
    oracle_dir.mkdir()
    module.register_oracle(path, oracle_run_dir=oracle_dir, slug="oracle-live", conversation_url="https://chatgpt.com/c/exact", terminal_observed=False)
    module.release_owner(path, session_id="session-1")

    state = module.evaluate(path, now=started + timedelta(seconds=4800))

    assert state["phase"] == "RECOVER_SAME_SESSION"
    assert state["recovery_targets"] == [{"run_dir": str(oracle_dir.resolve()), "slug": "oracle-live", "conversation_url": "https://chatgpt.com/c/exact"}]


def test_exact_recovery_finishes_before_next_episode_becomes_ready(tmp_path: Path) -> None:
    module, _, started, path = harness(tmp_path)
    oracle_dir = tmp_path / "oracle-run"
    oracle_dir.mkdir()
    module.register_oracle(path, oracle_run_dir=oracle_dir, slug="oracle-live", conversation_url="https://chatgpt.com/c/exact", terminal_observed=False)
    module.release_owner(path, session_id="session-1")
    state = module.evaluate(path, now=started + timedelta(seconds=4800))
    state["resume_attempt"] = {"generation": 1, "kind": "oracle_exact_recovery", "pid": 1234}
    path.write_text(json.dumps(state), encoding="utf-8")
    (oracle_dir / "run.json").write_text(json.dumps({"session_authority": "terminal", "transport_status": "complete"}), encoding="utf-8")

    recovered = module.evaluate(path, now=started + timedelta(seconds=4801))

    assert recovered["phase"] == "READY_NEXT_EPISODE"
    assert recovered["recovery_targets"] == []
    assert recovered["resume_attempt"] is None


def test_checkpoint_hook_denies_only_new_subagent_fanout(tmp_path: Path) -> None:
    module, project, started, path = harness(tmp_path)
    module.evaluate(path, now=started + timedelta(seconds=4500))
    payload = {"cwd": str(project), "session_id": "session-1", "tool_name": "spawn_agent"}
    output = json.loads(module.run_hook("pre-tool-use", payload, root=tmp_path / "state"))
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"
    payload["tool_name"] = "read_file"
    assert module.run_hook("pre-tool-use", payload, root=tmp_path / "state") == ""


def test_duplicate_active_run_is_rejected(tmp_path: Path) -> None:
    module, project, _, _ = harness(tmp_path)
    mission = project / "mission.md"
    try:
        module.start_run(project, mission, root=tmp_path / "state")
    except module.HarnessError as exc:
        assert "ACTIVE_RUN_EXISTS" in str(exc)
    else:
        raise AssertionError("duplicate active run was accepted")


def test_gjc_interview_asks_one_dimension_and_requires_all_context(tmp_path: Path) -> None:
    module = load("gjc_interview_test", ROOT / "bin" / "chatgpt_gjc_interview.py")
    project = tmp_path / "project"
    project.mkdir()
    path, state = module.start(project, [{"name": "runtime"}], threshold=0.35, restatement="macOS runtime을 적용한다.")
    assert state["question"]["dimension"] == "goal"
    for dimension in module.DIMENSIONS:
        state = module.answer(path, dimension=dimension, coverage=1.0, text=f"{dimension} locked", risk=None, restatement=None)
        if dimension != "context":
            assert state["status"] == "interviewing"
    assert state["status"] == "awaiting_approval" and state["ambiguity"] == 0.0
    assert module.approve(path, True)["status"] == "approved"


def test_gjc_contradiction_raises_ambiguity(tmp_path: Path) -> None:
    module = load("gjc_interview_penalty_test", ROOT / "bin" / "chatgpt_gjc_interview.py")
    project = tmp_path / "project"
    project.mkdir()
    path, _ = module.start(project, [{"name": "runtime"}], threshold=0.35, restatement=None)
    state = module.answer(path, dimension="goal", coverage=1.0, text="changed", risk="contradiction", restatement=None)
    assert state["ambiguity_penalty"] == 0.1
    assert state["ambiguity"] > 0.65


def test_harness_doctor_checks_policy_order_and_combined_limit(tmp_path: Path) -> None:
    module, _, _, path = harness(tmp_path)
    report = module.doctor(tmp_path / "state")
    assert report["ok"] is True
    assert report["runs"][0]["path"] == str(path)
    state = json.loads(path.read_text(encoding="utf-8"))
    state["policy"]["max_total_concurrency"] = 6
    path.write_text(json.dumps(state), encoding="utf-8")
    assert module.doctor(tmp_path / "state")["ok"] is False


def test_resume_spawn_is_serialized_and_never_duplicated(tmp_path: Path, monkeypatch) -> None:
    module, _, _, path = harness(tmp_path)
    state = json.loads(path.read_text(encoding="utf-8"))
    state["phase"] = "READY_NEXT_EPISODE"
    state["owner_released"] = True
    path.write_text(json.dumps(state), encoding="utf-8")
    entered = threading.Event()
    release = threading.Event()
    calls: list[list[str]] = []

    def fake_popen(command, **_kwargs):
        calls.append(command)
        entered.set()
        assert release.wait(timeout=5)
        return SimpleNamespace(pid=4321)

    monkeypatch.setattr(module.subprocess, "Popen", fake_popen)
    errors: list[Exception] = []

    def invoke() -> None:
        try:
            module.execute_resume(path)
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    first = threading.Thread(target=invoke)
    second = threading.Thread(target=invoke)
    first.start()
    assert entered.wait(timeout=5)
    second.start()
    time.sleep(0.05)
    assert len(calls) == 1
    release.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert errors == []
    assert len(calls) == 1
    assert json.loads(path.read_text(encoding="utf-8"))["phase"] == "RESUME_STARTED"
