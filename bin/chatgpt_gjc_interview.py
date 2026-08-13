#!/usr/bin/env python3
"""GJC-inspired, one-question-per-round brownfield interview state machine."""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence


SCHEMA = "codex.gjc.interview/v1"
DIMENSIONS = ("goal", "constraints", "success_criteria", "context")
WEIGHTS = {"goal": 0.35, "constraints": 0.25, "success_criteria": 0.25, "context": 0.15}
QUESTIONS = {
    "goal": "이 작업이 완료됐을 때 사용자가 실제로 얻게 되는 결과를 한 문장으로 확정해 주세요.",
    "constraints": "반드시 지켜야 할 기술·운영·권한 제약 중 아직 명시하지 않은 가장 중요한 하나는 무엇인가요?",
    "success_criteria": "완료 여부를 실제 사용 표면에서 판정할 수 있는 가장 중요한 성공 시나리오는 무엇인가요?",
    "context": "현재 구현이나 운영 상태에서 새 설계를 바꿀 수 있는 핵심 사실 하나는 무엇인가요?",
}


class InterviewError(RuntimeError):
    pass


def now_text() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InterviewError("INTERVIEW_STATE_UNREADABLE") from exc
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        raise InterviewError("INTERVIEW_SCHEMA_INVALID")
    return value


def ambiguity(state: dict[str, Any]) -> float:
    coverage = state["coverage"]
    confidence = sum(WEIGHTS[key] * float(coverage[key]) for key in DIMENSIONS)
    return round(min(1.0, max(0.0, 1.0 - confidence + float(state.get("ambiguity_penalty") or 0.0))), 4)


def weakest_dimension(state: dict[str, Any]) -> str:
    return min(DIMENSIONS, key=lambda key: (float(state["coverage"][key]), -WEIGHTS[key], DIMENSIONS.index(key)))


def response(state: dict[str, Any]) -> dict[str, Any]:
    current = ambiguity(state)
    state["ambiguity"] = current
    if state.get("status") == "approved":
        return state
    if current <= float(state["threshold"]) and all(float(state["coverage"][key]) > 0.0 for key in DIMENSIONS):
        state["status"] = "awaiting_approval"
        state["question"] = None
        state["approval_prompt"] = f"요구사항을 다음과 같이 확정합니다: {state.get('restatement') or '목표·제약·성공 기준·현재 맥락에 따라 구현한다.'} 이 내용으로 실행을 승인하시나요?"
    else:
        dimension = weakest_dimension(state)
        state["status"] = "interviewing"
        state["question"] = {"round": len(state["rounds"]) + 1, "dimension": dimension, "text": QUESTIONS[dimension]}
        state["approval_prompt"] = None
    return state


def start(project_root: Path, components: list[dict[str, Any]], *, threshold: float, restatement: str | None) -> tuple[Path, dict[str, Any]]:
    project_root = project_root.expanduser().resolve()
    if not project_root.is_dir():
        raise InterviewError("PROJECT_ROOT_INVALID")
    if not 1 <= len(components) <= 6:
        raise InterviewError("TOPOLOGY_COMPONENT_COUNT_MUST_BE_1_TO_6")
    for component in components:
        if not isinstance(component, dict) or not str(component.get("name") or "").strip():
            raise InterviewError("TOPOLOGY_COMPONENT_NAME_REQUIRED")
    if not 0.0 <= threshold <= 1.0:
        raise InterviewError("THRESHOLD_OUT_OF_RANGE")
    interview_id = uuid.uuid4().hex
    path = project_root / ".omo" / "interviews" / f"{interview_id}.json"
    state: dict[str, Any] = {
        "schema": SCHEMA,
        "interview_id": interview_id,
        "project_root": str(project_root),
        "mode": "brownfield",
        "threshold": threshold,
        "topology_locked": True,
        "components": components,
        "coverage": {key: 0.0 for key in DIMENSIONS},
        "ambiguity_penalty": 0.0,
        "ambiguity": 1.0,
        "rounds": [],
        "restatement": restatement,
        "status": "interviewing",
        "created_at": now_text(),
        "updated_at": now_text(),
    }
    response(state)
    _write(path, state)
    return path, state


def answer(path: Path, *, dimension: str, coverage: float, text: str, risk: str | None, restatement: str | None) -> dict[str, Any]:
    state = _load(path)
    if state["status"] != "interviewing":
        raise InterviewError("INTERVIEW_NOT_ACCEPTING_ANSWERS")
    question = state.get("question") or {}
    if question.get("dimension") != dimension:
        raise InterviewError(f"ANSWER_MUST_TARGET_CURRENT_DIMENSION: {question.get('dimension')}")
    if not 0.0 <= coverage <= 1.0 or not text.strip():
        raise InterviewError("ANSWER_AND_COVERAGE_REQUIRED")
    penalty = {None: 0.0, "contradiction": 0.10, "evasive": 0.10, "scope-expansion": 0.05}[risk]
    state["coverage"][dimension] = max(float(state["coverage"][dimension]), coverage)
    state["ambiguity_penalty"] = min(0.5, float(state["ambiguity_penalty"]) + penalty)
    state["rounds"].append({
        "round": question["round"],
        "dimension": dimension,
        "answer": text,
        "coverage": coverage,
        "risk": risk,
        "at": now_text(),
    })
    if restatement:
        state["restatement"] = restatement
    state["updated_at"] = now_text()
    response(state)
    _write(path, state)
    return state


def approve(path: Path, approved: bool) -> dict[str, Any]:
    state = _load(path)
    if state["status"] != "awaiting_approval":
        raise InterviewError("APPROVAL_NOT_READY")
    state["status"] = "approved" if approved else "interviewing"
    state["approved_at"] = now_text() if approved else None
    if not approved:
        state["ambiguity_penalty"] = min(0.5, float(state["ambiguity_penalty"]) + 0.10)
        response(state)
    state["updated_at"] = now_text()
    _write(path, state)
    return state


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    start_parser = commands.add_parser("start")
    start_parser.add_argument("--project-root", type=Path, required=True)
    start_parser.add_argument("--components-json", required=True)
    start_parser.add_argument("--threshold", type=float, default=0.35)
    start_parser.add_argument("--restatement")
    for name in ("status", "approve"):
        value = commands.add_parser(name)
        value.add_argument("--state", type=Path, required=True)
        if name == "approve":
            choice = value.add_mutually_exclusive_group(required=True)
            choice.add_argument("--yes", action="store_true")
            choice.add_argument("--no", action="store_true")
    answer_parser = commands.add_parser("answer")
    answer_parser.add_argument("--state", type=Path, required=True)
    answer_parser.add_argument("--dimension", choices=DIMENSIONS, required=True)
    answer_parser.add_argument("--coverage", type=float, required=True)
    answer_parser.add_argument("--answer", required=True)
    answer_parser.add_argument("--risk", choices=("contradiction", "evasive", "scope-expansion"))
    answer_parser.add_argument("--restatement")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "start":
            try:
                components = json.loads(args.components_json)
            except json.JSONDecodeError as exc:
                raise InterviewError("COMPONENTS_JSON_INVALID") from exc
            if not isinstance(components, list):
                raise InterviewError("COMPONENTS_JSON_ARRAY_REQUIRED")
            path, state = start(args.project_root, components, threshold=args.threshold, restatement=args.restatement)
            result: Any = {"ok": True, "state_path": str(path), "interview": state}
        elif args.command == "answer":
            result = answer(args.state, dimension=args.dimension, coverage=args.coverage, text=args.answer, risk=args.risk, restatement=args.restatement)
        elif args.command == "approve":
            result = approve(args.state, args.yes)
        else:
            result = response(_load(args.state))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except InterviewError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
