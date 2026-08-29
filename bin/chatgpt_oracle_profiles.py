from __future__ import annotations

"""Deterministic mode contracts for the Oracle + DevSpace ChatGPT path.

This module deliberately contains no browser, account, attachment, or app-settings
automation.  It only turns a requested mode into the small composer handoff that
the Oracle runner may send after the one-time DevSpace setup has been completed.
"""

import argparse
import importlib.util
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


REGULAR_REASONING_LEVELS = ("Pro", "Very High", "High", "Medium", "Low")
REGULAR_THINKING_TIME = {
    "Pro": "heavy",
    "Very High": "extra-high",
    "High": "extended",
    "Medium": "standard",
    "Low": "light",
}
_CONFIG_SPEC = importlib.util.spec_from_file_location(
    "chatgpt_oracle_profiles_workspace_config",
    Path(__file__).resolve().parent / "chatgpt_workspace_config.py",
)
if _CONFIG_SPEC is None or _CONFIG_SPEC.loader is None:
    raise RuntimeError("workspace app config module unavailable")
WORKSPACE_CONFIG = importlib.util.module_from_spec(_CONFIG_SPEC)
_CONFIG_SPEC.loader.exec_module(WORKSPACE_CONFIG)
DEVSPACE_APP_NAME = WORKSPACE_CONFIG.DEFAULT_APP_NAME
# Current ChatGPT exposes Pro as the maximum effort for GPT-5.6 Sol, not as a
# separate model row.  Oracle 0.17.1 verifies that Pro effort independently.
PRO_MODEL = "gpt-5.6-sol"
PRO_COMPOSER_PROMPT = "Read the attached prompt/instructions and all attached files, then complete the task."
POWER5_COMPATIBILITY_ALIASES = {
    "pro",
    "gpt-pro",
    "pro-readonly",
    "pro_readonly",
    "pro readonly",
}


class OracleProfileError(ValueError):
    def __init__(self, code: str, message: str, evidence: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.evidence = evidence or {}

    def envelope(self) -> dict[str, Any]:
        return {"ok": False, "error": {"code": self.code, "message": str(self), "evidence": self.evidence}}


@dataclass(frozen=True)
class OracleModeProfile:
    mode: str
    task_kind: str
    oracle_launch: bool
    devspace_required: bool
    research: bool = False
    legacy_route: str | None = None


_PROFILES = {
    "direct": OracleModeProfile("direct", "direct", True, True),
    "plan": OracleModeProfile("plan", "plan", True, True),
    "review": OracleModeProfile("review", "review", True, True),
    "edit": OracleModeProfile("edit", "edit", True, True),
    "orchestrator": OracleModeProfile("orchestrator", "orchestrator", True, True),
    "deep-research": OracleModeProfile("deep-research", "deep-research", True, True, research=True),
    "manual": OracleModeProfile("manual", "manual", False, False),
    "attachment": OracleModeProfile("attachment", "direct", True, False),
    "pro-attachment": OracleModeProfile("pro-attachment", "direct", True, False),
}
_ALIASES = {
    "deep_research": "deep-research",
    "deep research": "deep-research",
    "pro_attachment": "pro-attachment",
    "pro attachment": "pro-attachment",
}


def _normalize_mode(value: str) -> str:
    requested = str(value or "").strip().casefold()
    normalized = "direct" if requested in POWER5_COMPATIBILITY_ALIASES else _ALIASES.get(requested, requested)
    if normalized not in _PROFILES:
        raise OracleProfileError("MODE_UNSUPPORTED", "Oracle mode is not supported", {"requested": value, "supported": list(_PROFILES)})
    return normalized


def resolve_profile(mode: str) -> OracleModeProfile:
    """Return a named mode profile without starting a browser or process."""
    return _PROFILES[_normalize_mode(mode)]


def _absolute_mission_path(value: str | Path | None) -> Path:
    if value is None or not str(value).strip():
        raise OracleProfileError("MISSION_PATH_REQUIRED", "Oracle launch modes require an absolute mission path")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise OracleProfileError("MISSION_PATH_ABSOLUTE_REQUIRED", "mission path must be absolute", {"mission_path": str(path)})
    return path.resolve(strict=False)


def _resolve_reasoning(requested: str | None, *, default: str = "Very High") -> str:
    if requested is None or not str(requested).strip():
        return default
    normalized = str(requested).strip().casefold()
    if normalized in {"pro", "power 5", "power-5", "power5", "최고", "프로"}:
        return "Pro"
    if normalized in {"very high", "very-high", "extra high", "extra-high", "xhigh", "매우 높음"}:
        return "Very High"
    if normalized in {"high", "높음"}:
        return "High"
    if normalized in {"medium", "중간"}:
        return "Medium"
    if normalized in {"low", "light", "instant", "낮음"}:
        return "Low"
    raise OracleProfileError(
        "REGULAR_REASONING_UNAVAILABLE",
        "requested regular reasoning level is unavailable; no downgrade was made",
        {"requested": str(requested), "supported": list(REGULAR_REASONING_LEVELS)},
    )


def composer_handoff(mission_path: str | Path, app_name: str | None = None) -> str:
    """The only regular-GPT composer text: app mention plus the absolute mission."""
    mission = _absolute_mission_path(mission_path)
    return (
        f"@{WORKSPACE_CONFIG.normalize_app_name(app_name or WORKSPACE_CONFIG.configured_app_name())} Read and execute the mission file: {mission}. "
        "Use only the exact project root recorded there; read the mission and applicable AGENTS.md fully first. "
        "If workspace opening times out, retry that same exact root once; never substitute a parent, child, active "
        "workspace, or shell boundary workaround."
    )


def _attachment_paths(values: list[str | Path] | tuple[str | Path, ...] | None) -> list[Path]:
    result: list[Path] = []
    for value in values or ():
        path = Path(value).expanduser()
        if not path.is_absolute():
            raise OracleProfileError(
                "ATTACHMENT_PATH_ABSOLUTE_REQUIRED",
                "Pro attachment paths must be absolute",
                {"attachment_path": str(path)},
            )
        result.append(path.resolve(strict=False))
    return result


def build_launch_contract(
    mode: str,
    *,
    mission_path: str | Path | None = None,
    reasoning_level: str | None = None,
    attachment_paths: list[str | Path] | tuple[str | Path, ...] | None = None,
    app_name: str | None = None,
) -> dict[str, Any]:
    """Build an immutable, browser-agnostic launch contract for parent runners.

    `manual` intentionally produces a non-launch contract. Power is orthogonal
    to the operation mode: Pro/Power 5 uses the same DevSpace and mission
    authority as lower powers. Attachment-only is a transport fallback that
    preserves the selected power; `pro-attachment` is its Power 5 alias.
    """
    requested_mode = str(mode or "").strip().casefold()
    power5_compatibility_alias = requested_mode in POWER5_COMPATIBILITY_ALIASES
    profile = resolve_profile(mode)
    resolved_app_name = WORKSPACE_CONFIG.normalize_app_name(
        app_name or WORKSPACE_CONFIG.configured_app_name()
    )
    result: dict[str, Any] = {
        "schema": "codex.chatgpt.oracle-mode-profile/v1",
        "mode": profile.mode,
        "task_kind": profile.task_kind,
        "oracle_launch": profile.oracle_launch,
        "devspace_required": profile.devspace_required,
        "research": profile.research,
        "attachments": [],
        "app_picker": False,
        "app_settings_automation": False,
    }
    if not profile.oracle_launch:
        result.update({
            "route": "manual-no-launch",
            "app_policy": "not-applicable",
            "reasoning_level": None,
            "composer_prompt": None,
            "mission_path": None,
        })
        return result
    mission = _absolute_mission_path(mission_path)
    if profile.mode in {"attachment", "pro-attachment"}:
        attachments = _attachment_paths(attachment_paths)
        if mission not in attachments:
            attachments.insert(0, mission)
        if not attachments:
            raise OracleProfileError("PRO_ATTACHMENTS_REQUIRED", "Pro requires at least one exact attachment")
        reasoning = _resolve_reasoning(
            "Pro" if profile.mode == "pro-attachment" else reasoning_level,
        )
        result.update({
            "route": "oracle-attachment-only",
            "app_policy": "forbidden",
            "attachment_policy": "always",
            "attachments": [str(path) for path in attachments],
            "model": PRO_MODEL,
            "model_strategy": "select",
            "reasoning_level": reasoning,
            "thinking_time": REGULAR_THINKING_TIME[reasoning],
            "mission_path": str(mission),
            "composer_prompt": PRO_COMPOSER_PROMPT,
            "compatibility_alias": "pro-attachment" if profile.mode == "pro-attachment" else None,
        })
        return result
    if attachment_paths:
        raise OracleProfileError(
            "REGULAR_ATTACHMENTS_FORBIDDEN",
            "non-Pro Oracle modes use DevSpace and must not attach files",
        )
    default_reasoning = "Pro" if profile.mode == "orchestrator" else "Very High"
    reasoning = _resolve_reasoning(
        "Pro" if power5_compatibility_alias else reasoning_level,
        default=default_reasoning,
    )
    if profile.research and reasoning == "Pro":
        raise OracleProfileError(
            "DEEP_RESEARCH_POWER_UNAVAILABLE",
            "Deep Research owns its effort flow and cannot claim a Power 5 slider selection",
        )
    result.update({
        "route": "oracle-devspace",
        "app_policy": "prompt-mention-only",
        "app_name": resolved_app_name,
        "model": PRO_MODEL,
        "model_strategy": "select",
        "reasoning_level": reasoning,
        "thinking_time": REGULAR_THINKING_TIME[reasoning],
        "mission_path": str(mission),
        "composer_prompt": composer_handoff(mission, resolved_app_name),
        "compatibility_alias": "pro" if power5_compatibility_alias else None,
    })
    return result


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Resolve Oracle + DevSpace mode profiles without launching ChatGPT.")
    parser.add_argument("command", choices=("resolve", "list"))
    parser.add_argument("--mode")
    parser.add_argument("--mission-path")
    parser.add_argument("--reasoning-level")
    parser.add_argument("--attachment", action="append", default=[])
    parser.add_argument("--app-name")
    args = parser.parse_args(argv)
    try:
        if args.command == "list":
            result: dict[str, Any] = {"ok": True, "profiles": [asdict(item) for item in _PROFILES.values()]}
        else:
            if not args.mode:
                raise OracleProfileError("MODE_REQUIRED", "--mode is required for resolve")
            result = {
                "ok": True,
                "contract": build_launch_contract(
                    args.mode,
                    mission_path=args.mission_path,
                    reasoning_level=args.reasoning_level,
                    attachment_paths=args.attachment,
                    app_name=args.app_name,
                ),
            }
    except OracleProfileError as exc:
        print(json.dumps(exc.envelope(), ensure_ascii=False, sort_keys=True))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
