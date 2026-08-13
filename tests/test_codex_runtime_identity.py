from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


PATH = Path(__file__).resolve().parents[1] / "bin" / "codex_runtime_identity.py"


def load():
    spec = importlib.util.spec_from_file_location("codex_runtime_identity_test", PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write_rollout(home: Path, thread_id: str, *, model: str, effort: str) -> Path:
    path = home / "sessions" / "2026" / "08" / "12" / f"rollout-test-{thread_id}.jsonl"
    path.parent.mkdir(parents=True)
    events = [
        {"type": "session_meta", "payload": {"id": thread_id}},
        {"type": "turn_context", "payload": {
            "turn_id": "turn-1", "model": model, "effort": effort,
        }},
    ]
    path.write_text("".join(json.dumps(item) + "\n" for item in events), encoding="utf-8")
    return path


def test_reads_exact_task_bound_runtime_identity(tmp_path: Path) -> None:
    module = load()
    thread_id = "019ff05c-bad3-7770-a902-6b1b62588a7d"
    path = write_rollout(tmp_path, thread_id, model="gpt-5.6-luna", effort="max")
    result = module.current_runtime_identity(codex_home=tmp_path, thread_id=thread_id)
    assert result["model"] == "gpt-5.6-luna"
    assert result["reasoning_effort"] == "max"
    assert result["source"] == "codex-rollout-turn-context"
    assert result["rollout_path"] == str(path.resolve())
    assert len(result["turn_context_sha256"]) == 64


def test_rejects_missing_or_mismatched_runtime_evidence(tmp_path: Path) -> None:
    module = load()
    thread_id = "019ff05c-bad3-7770-a902-6b1b62588a7d"
    write_rollout(tmp_path, "119ff05c-bad3-7770-a902-6b1b62588a7d", model="gpt-5.6-luna", effort="max")
    with pytest.raises(module.RuntimeIdentityError, match="matching Codex runtime"):
        module.current_runtime_identity(codex_home=tmp_path, thread_id=thread_id)
    with pytest.raises(module.RuntimeIdentityError, match="task ID"):
        module.current_runtime_identity(codex_home=tmp_path, thread_id="not-a-task")
