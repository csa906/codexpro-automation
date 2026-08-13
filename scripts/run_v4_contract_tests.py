#!/usr/bin/env python
"""Offline release-contract runner for the install WAL and package inventory."""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FOCUSED = [
    "tests/test_global_gpt_browser_policy.py",
    "tests/test_release_packaging.py",
    "tests/test_install_lifecycle.py",
]


def main() -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--focused", action="store_true")
    mode.add_argument("--full", action="store_true")
    args = parser.parse_args()
    targets = FOCUSED if args.focused else ["tests"]
    environment = dict(os.environ)
    stable_temp = Path(tempfile.gettempdir()).resolve()
    if os.name == "nt" and environment.get("LOCALAPPDATA"):
        stable_temp = (Path(environment["LOCALAPPDATA"]) / "Temp").resolve()
        stable_temp.mkdir(parents=True, exist_ok=True)
        environment["TEMP"] = str(stable_temp)
        environment["TMP"] = str(stable_temp)
        environment["TMPDIR"] = str(stable_temp)
    with tempfile.TemporaryDirectory(prefix="codexpro-v4-pytest-", dir=stable_temp) as basetemp:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", *targets, "--basetemp", basetemp],
            cwd=ROOT,
            env=environment,
        )
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
