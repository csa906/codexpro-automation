#!/usr/bin/env python3
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "bin"))
from codexpro_lifecycle import main

raise SystemExit(main(["doctor", *sys.argv[1:]]))
