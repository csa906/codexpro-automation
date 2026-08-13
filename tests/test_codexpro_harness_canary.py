from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_module():
    path = ROOT / "scripts" / "run_harness_canary.py"
    spec = importlib.util.spec_from_file_location("run_harness_canary_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_compressed_canary_writes_75_80_receipt_without_resume(tmp_path: Path) -> None:
    module = load_module()
    result = module.run_canary(state_root=tmp_path / "state", real_time=False)

    assert result["ok"] is True
    assert [item["phase"] for item in result["timeline"]] == ["RUNNING", "CHECKPOINT_DUE", "READY_NEXT_EPISODE"]
    receipt = json.loads(Path(result["receipt_path"]).read_text(encoding="utf-8"))
    assert receipt["handoff_sha256"] == result["handoff_sha256"]
    assert receipt["final_phase"] == "READY_NEXT_EPISODE"
