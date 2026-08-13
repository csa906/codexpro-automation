#!/usr/bin/env python3
"""Exercise the 75/80-minute harness policy with a compressed or real clock."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence


ROOT = Path(__file__).resolve().parents[1]


def _load_harness():
    path = ROOT / "bin" / "codexpro_harness.py"
    spec = importlib.util.spec_from_file_location("codexpro_harness_canary_runtime", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load harness: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _wait_until(started_monotonic: float, elapsed_seconds: float) -> None:
    while True:
        remaining = started_monotonic + elapsed_seconds - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(30.0, remaining))


def run_canary(*, state_root: Path, real_time: bool) -> dict[str, Any]:
    harness = _load_harness()
    canary_id = uuid.uuid4().hex
    workspace = state_root.expanduser().resolve() / "workspaces" / canary_id
    workspace.mkdir(parents=True, exist_ok=False)
    mission = workspace / "mission.md"
    mission.write_text("Dummy 85-minute checkpoint canary; never submit external work.\n", encoding="utf-8")
    started = datetime.now(timezone.utc)
    state = harness.start_run(
        workspace,
        mission,
        root=state_root,
        now=started,
        codex_session_id=f"canary-{canary_id}",
        owner_pid=0,
        next_instruction="Canary complete; do not resume this synthetic run.",
    )
    run_path = harness.run_dir_for(state_root, workspace, state["run_id"]) / "run.json"
    timeline: list[dict[str, Any]] = []
    monotonic_start = time.monotonic()

    def observe(elapsed: int, *, release: bool = False) -> None:
        if real_time:
            _wait_until(monotonic_start, float(elapsed))
            observed_at = datetime.now(timezone.utc)
        else:
            observed_at = started + timedelta(seconds=elapsed)
        if release:
            harness.release_owner(run_path, session_id=state["codex_session_id"])
        current = harness.evaluate(run_path, now=observed_at)
        timeline.append({"elapsed_seconds": elapsed, "phase": current["phase"], "fanout_locked": current["fanout_locked"]})

    observe(4499)
    observe(4500)
    observe(4800, release=True)
    if real_time:
        _wait_until(monotonic_start, 5100.0)
    final = json.loads(run_path.read_text(encoding="utf-8"))
    expected = ["RUNNING", "CHECKPOINT_DUE", "READY_NEXT_EPISODE"]
    actual = [item["phase"] for item in timeline]
    receipt = {
        "schema": "codex.codexpro.harness-canary/v1",
        "ok": actual == expected,
        "mode": "real-85-minute" if real_time else "compressed-clock",
        "canary_id": canary_id,
        "run_id": state["run_id"],
        "timeline": timeline,
        "run_path": str(run_path),
        "run_sha256": _sha256(run_path),
        "handoff_path": str(run_path.parent / "handoff.md"),
        "handoff_sha256": _sha256(run_path.parent / "handoff.md"),
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "final_phase": final["phase"],
    }
    receipt_path = run_path.parent / "canary-receipt.json"
    harness.write_json_atomic(receipt_path, receipt)
    receipt["receipt_path"] = str(receipt_path)
    return receipt


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-root", type=Path, default=Path.home() / ".codex" / "state" / "codexpro-harness-canary")
    parser.add_argument("--real-time", action="store_true", help="Wait a real 85 minutes; otherwise use a compressed clock.")
    args = parser.parse_args(argv)
    result = run_canary(state_root=args.state_root, real_time=args.real_time)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
