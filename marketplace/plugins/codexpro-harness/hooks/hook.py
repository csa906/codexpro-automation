#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path


codex_home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser().resolve()
module_path = codex_home / "bin" / "codexpro_harness.py"
spec = importlib.util.spec_from_file_location("codexpro_harness_hook_runtime", module_path)
if spec is None or spec.loader is None:
    raise SystemExit(0)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
raise SystemExit(module.main(["hook", *sys.argv[1:]]))
