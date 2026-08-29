from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
THINKING = ROOT / "skills" / "chatgpt-thinking-browser" / "SKILL.md"
PRO = ROOT / "skills" / "chatgpt-pro-browser" / "SKILL.md"
HANDOFF = ROOT / "skills" / "chatgpt-pro-plan-handoff" / "SKILL.md"
MULTI = ROOT / "skills" / "web-multi-gpt" / "SKILL.md"
RESEARCH = ROOT / "skills" / "chatgpt-deep-research-browser" / "SKILL.md"
ORACLE = ROOT / "skills" / "chatgpt-oracle-runtime" / "SKILL.md"
DESIGNER = ROOT / "skills" / "chatgpt-question-designer" / "SKILL.md"


def text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_new_regular_modes_route_only_to_oracle_devspace() -> None:
    value = text(THINKING)
    assert "chatgpt_oracle_dispatch.py" in value
    assert "@DevSpace" in value
    assert "Only a deterministic, pre-submit, mutation-free DevSpace failure may" in value
    assert "mission-explicit attachment transport" in value
    assert "Never create a new legacy run." in value
    assert "does not automate the\napp picker or settings" in value


def test_power5_uses_mode_authority_and_guarded_attachments() -> None:
    value = text(PRO)
    flat = " ".join(value.split())
    assert "Power 5/Pro is the maximum reasoning level for the same `GPT-5.6 Sol` model" in flat
    assert "available to `direct`, `plan`, `review`, `edit`, and `orchestrator`" in flat
    assert "`direct`/answer, `plan`, and `review` are read-only" in flat
    assert "An explicitly authorized `edit` or `orchestrator` mission may write only" in flat
    assert "`--mode pro` is a compatibility alias for `--mode direct --reasoning-level Pro`" in flat
    assert "`pro-attachment` is the legacy Power 5 compatibility alias for the generic attachment transport" in flat
    assert "Attachment transport may preserve any selected Power 1-5" in flat
    assert "A deterministic DevSpace failure may automatically use attachment transport" in flat
    assert "After prompt submission, any uncertainty or observed/possible mutation permits only exact-session recovery" in flat
    handoff = text(HANDOFF)
    handoff_flat = " ".join(handoff.split())
    assert "Power 5/Pro is a normal reasoning level for the same operation modes" in handoff_flat
    assert "An explicitly authorized implementation `edit` or `orchestrator` stage may write only" in handoff_flat
    assert "The generic one-shot dispatcher can automatically use attachment transport only after a deterministic, pre-submit, mutation-free DevSpace failure" in handoff_flat


def test_qualified_pro_permits_broad_adaptive_read_only_project_context() -> None:
    value = text(PRO)
    flat = " ".join(value.split())
    assert "begin with the `read('.')` directory-list compatibility call" in flat
    assert "Read-only missions may inspect decision-relevant material broadly" in flat
    assert "read the mission and applicable `AGENTS.md` chain completely" in flat
    assert "Authorized write missions preserve unrelated WIP" in flat
    assert "must not broadly stage, reset, stash, clean, or overwrite another writer's changes" in flat


def test_qualified_pro_fails_closed_when_devspace_tools_are_not_exposed() -> None:
    value = text(ORACLE)
    flat = " ".join(value.split())
    assert "`TASK_OUTCOME: EXECUTED` must be the final semantic marker" in flat
    assert "provider transport success alone is not execution proof" in flat
    assert "Once a prompt may have been submitted, or any mutation is observed or possible, fallback and fresh submission are forbidden" in flat
    handoff = " ".join(text(HANDOFF).split())
    assert "Missing output/receipt, a nonzero post-submit exit, watchdog timeout, or ambiguous prompt submission returns attention-required" in handoff


def test_web_multi_decision_is_explicit_advisory_only() -> None:
    value = text(PRO)
    flat = " ".join(value.split())
    assert "Do not require `WEB_MULTI_NEEDED` in ordinary Power 5 answers" in flat
    assert "Include that decision block only when the user or owning workflow explicitly requests a Web Multi advisory decision" in flat
    handoff = " ".join(text(HANDOFF).split())
    assert "Require `WEB_MULTI_NEEDED` only when the workflow explicitly asks a stage to decide an advisory Web Multi branch" in handoff
    assert "Ordinary Power 5 stages do not emit or trigger it automatically" in handoff


def test_deep_research_uses_oracle_deep_without_silent_fallback() -> None:
    value = text(RESEARCH)
    flat = " ".join(value.split())
    assert "chatgpt_oracle_dispatch.py" in value
    assert "--mode deep-research" in value
    assert "--browser-research deep" in value
    assert '--reasoning-level "Very High"' in value
    assert "Oracle `extra-high`" in value
    assert "skips generic thinking-time selection" in value
    assert "Deep Research owns its effort flow" in flat
    assert "do not claim a visible `Extra High` selection or verification" in flat
    assert "Do not silently replace Deep Research" in value


def test_power_levels_are_orthogonal_to_operation_authority() -> None:
    runtime = text(ORACLE)
    routing = text(ROOT / "docs" / "GLOBAL_CHATGPT_ROUTING.md")
    runtime_flat = " ".join(runtime.split())
    routing_flat = " ".join(routing.split())
    assert "For `GPT-5.6 Sol`, Power 1-5 correspond to Low, Medium, High, Very High, and Pro" in runtime_flat
    assert "never use power selection to expand authority" in runtime_flat
    assert "Power 1-5 map to Oracle `light`, `standard`, `extended`, `extra-high`, and `heavy`" in routing_flat
    assert "Power changes reasoning depth, not authority" in routing_flat
    assert "Power 5 uses the same operation authority as every other Power" in routing_flat
    assert "`heavy` is a compatibility token reserved for" not in runtime
    assert "`heavy` is a compatibility token reserved for" not in routing


def test_web_multi_is_genuine_sessions_with_wave_cap_and_worktrees() -> None:
    value = text(MULTI)
    assert "chatgpt_oracle_multi.py" in value
    assert "waves of at most five" in value
    assert "worktree-write" in value
    assert "distinct pre-created worktree" in value
    assert "single-GPT role simulation" in value


def test_comprehensive_is_web_native_relay_with_one_local_gate() -> None:
    value = text(HANDOFF)
    assert "chatgpt_oracle_comprehensive.py" in value
    assert "plan -> optional Power 5 or explicit Oracle Web Multi advisory -> review" in value
    assert "Only a final web PASS plus current host proof can complete" in value
    assert "and a zero-exit local deterministic\ngate" in value
    assert "The host validates" in value
    assert "never rewrites the semantic prompt" in value


def test_host_control_state_is_outside_devspace_project() -> None:
    value = text(ORACLE)
    assert "%USERPROFILE%\\.codex\\state\\chatgpt-oracle" in value
    source = text(ROOT / "bin" / "chatgpt_oracle_state.py")
    assert "HOST_STATE_OVERLAPS_PROJECT" in source


def test_oracle_recovery_is_exact_slug_no_restart_and_monotonic() -> None:
    value = text(THINKING)
    compact = " ".join(value.split())
    assert "stored slug" in value
    assert "never restarts/resubmits" in value
    assert "or downgrades a\ndurable COMPLETE result" in value
    assert "A later `running` observation cannot erase terminal evidence" in compact
    assert "exact persisted" in value
    assert "recover the exact session only" in compact
    runtime = text(ORACLE)
    runtime_compact = " ".join(runtime.split())
    assert "Recover only the stored Oracle slug" in runtime
    assert "never restarts, resubmits, changes mode/model/Power/transport" in runtime_compact
    assert "or creates a replacement conversation" in runtime_compact
    assert "Observer disagreement" in runtime
    assert "remains attention-required under the same project lock" in runtime


def test_oracle_runs_use_isolated_profile_copies_and_owned_hidden_windows() -> None:
    value = text(THINKING)
    assert "throwaway" in value
    assert "throwaway copy of the manually signed-in Oracle profile" in value
    assert "Oracle-owned hidden window" in value


def test_install_inventory_contains_new_active_runtime_and_keeps_legacy_recovery() -> None:
    manifest = json.loads((ROOT / "install-manifest.json").read_text(encoding="utf-8"))
    include = set(manifest["include"])
    for path in (
        "bin/chatgpt_oracle_dispatch.py",
        "bin/chatgpt_oracle_fallback.py",
        "bin/chatgpt_oracle_multi.py",
        "bin/chatgpt_oracle_comprehensive.py",
        "bin/devspace-compat/1.0.4/directory-read.patch",
        "skills/chatgpt-workspace-setup/SKILL.md",
    ):
        assert path in include
    assert "bin/chatgpt_agbrowse_run.py" in include
    assert manifest["routing"] == {
        "new_work_engine": "oracle",
        "regular_workspace_transport": "devspace",
        "power_levels": "gpt-5.6-sol-power-1-through-5",
        "pro_transport": "oracle-devspace-mode-authority",
        "attachment_transport": "oracle-attachment-only-deterministic-fallback",
        "pro_attachment_transport": "oracle-attachment-only-power5-compatibility-alias",
        "agbrowse": "persisted-run-recovery-only",
        "codexpro": "persisted-run-recovery-only",
    }
    assert manifest["external"]["oracle"]["license"] == "MIT"
    assert manifest["external"]["devspace"]["license"] == "MIT"
    assert manifest["external"]["agbrowse"]["role"] == "persisted-run-recovery-only"
    assert manifest["external"]["agbrowse"]["default_install"] is False
    assert manifest["external"]["codexpro"]["frozen"] is True


def test_no_new_skill_routes_to_chrome_playwright_or_in_app_fallback() -> None:
    combined = "\n".join(text(path) for path in (THINKING, HANDOFF, MULTI, RESEARCH)).casefold()
    compact = " ".join(combined.split())
    assert "@chrome" not in combined
    assert "codexpro and agbrowse are frozen" in compact
    assert "never create a new agbrowse" in compact
    assert "codexpro is frozen and is not a fallback" in compact


def test_readme_declares_manual_one_time_registration_not_ui_automation() -> None:
    value = text(ROOT / "README.md")
    assert "최초 한 번 수동 등록" in value
    assert "ChatGPT 설정·앱 목록·권한·삭제·선택 UI를 자동화하지 않습니다" in value
    assert "실행 신원으로 정확히 복구" in value
    assert "최초 설치 가이드" in value
    assert "ChatGPT 앱 `codex` 등록" in value


def test_english_readme_maps_modes_to_the_same_oracle_routes() -> None:
    value = text(ROOT / "README.en.md")
    assert "Oracle + DevSpace" in value
    assert "| `orchestrator` | Single web session |" in value
    assert "| Public-source investigation | `deep-research` | Oracle Deep Research |" in value
    assert "comprehensive mode" in value
    assert "Web Multi-GPT" in value
    assert "| One-shot Power 5 work | `pro` alias or explicit mode + `Pro` | Mode authority + GPT-5.6 Sol Power 5 |" in value
    assert "Power 1-5 controls reasoning, not authority" in value
    assert "same-Power, hash-bound attachment fallback" in value
    assert "never resubmits the task" in value


def test_question_designer_cannot_route_new_work_through_codexpro_or_legacy_sessions() -> None:
    value = text(DESIGNER)
    assert "CodexPro is frozen for new work" in value
    assert "never design a new prompt around CodexPro" in value
    assert "Every new Oracle stage is a one-shot session" in value
    assert "Do not add legacy `session_policy`" in value
    assert "verified CodexPro live connector context remains the default" not in value


def test_agent_metadata_exposes_oracle_active_routes() -> None:
    thinking = text(ROOT / "skills" / "chatgpt-thinking-browser" / "agents" / "openai.yaml")
    multi = text(ROOT / "skills" / "web-multi-gpt" / "agents" / "openai.yaml")
    pro = text(ROOT / "skills" / "chatgpt-pro-browser" / "agents" / "openai.yaml")
    assert "Run scoped Oracle missions at the appropriate Power" in thinking
    assert "Run genuine parallel Oracle GPT sessions" in multi
    assert "Run one-shot Oracle work at Power 5 with scoped authority" in pro
    assert "allow_implicit_invocation: true" in pro


def test_standalone_pro_never_transitions_into_comprehensive_implementation() -> None:
    pro = text(PRO)
    compact = " ".join(pro.split())
    assert "This is the one-shot Power 5 route" in pro
    assert "returns one durable result and stops" in pro
    assert "does not start comprehensive staging or implementation on its own" in pro
    assert "Use `chatgpt-pro-plan-handoff` only when the user requests the staged workflow" in compact
