from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any


RUNTIME_IDENTITY_SCHEMA = "codex.runtime-identity/v1"
THREAD_ID_RE = re.compile(r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$", re.IGNORECASE)


class RuntimeIdentityError(RuntimeError):
    pass


def _codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".codex"


def _candidate_rollouts(home: Path, thread_id: str) -> list[Path]:
    suffix = f"-{thread_id}.jsonl"
    candidates: list[Path] = []
    for folder_name in ("sessions", "archived_sessions"):
        folder = home / folder_name
        if folder.is_dir():
            candidates.extend(path for path in folder.rglob(f"*{thread_id}.jsonl") if path.name.endswith(suffix))
    return sorted(set(path.resolve() for path in candidates), key=lambda path: path.stat().st_mtime_ns, reverse=True)


def _read_runtime_context(path: Path, thread_id: str) -> dict[str, Any] | None:
    session_matches = False
    latest: dict[str, Any] | None = None
    latest_line = b""
    with path.open("rb") as handle:
        for raw_line in handle:
            try:
                event = json.loads(raw_line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if event.get("type") == "session_meta":
                session_matches = str(event.get("payload", {}).get("id") or "").casefold() == thread_id.casefold()
            elif event.get("type") == "turn_context":
                payload = event.get("payload")
                if isinstance(payload, dict):
                    latest = payload
                    latest_line = raw_line
    if not session_matches or latest is None:
        return None
    model = str(latest.get("model") or "").strip().casefold()
    effort = str(latest.get("effort") or latest.get("reasoning_effort") or "").strip().casefold()
    turn_id = str(latest.get("turn_id") or "").strip()
    if not model or not effort or not turn_id:
        return None
    return {
        "schema": RUNTIME_IDENTITY_SCHEMA,
        "thread_id": thread_id,
        "turn_id": turn_id,
        "model": model,
        "reasoning_effort": effort,
        "source": "codex-rollout-turn-context",
        "rollout_path": str(path),
        "turn_context_sha256": hashlib.sha256(latest_line.rstrip(b"\r\n")).hexdigest(),
    }


def current_runtime_identity(*, codex_home: Path | None = None, thread_id: str | None = None) -> dict[str, Any]:
    resolved_thread_id = str(thread_id or os.environ.get("CODEX_THREAD_ID") or "").strip()
    if not THREAD_ID_RE.fullmatch(resolved_thread_id):
        raise RuntimeIdentityError("current Codex task ID is unavailable")
    home = (codex_home or _codex_home()).expanduser().resolve()
    candidates = _candidate_rollouts(home, resolved_thread_id)
    for candidate in candidates:
        identity = _read_runtime_context(candidate, resolved_thread_id)
        if identity is not None:
            return identity
    raise RuntimeIdentityError("matching Codex runtime turn context is unavailable")
