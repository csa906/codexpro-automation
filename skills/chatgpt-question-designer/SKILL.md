---
name: chatgpt-question-designer
description: Design Oracle missions with an explicit cognitive role, operation authority, and independent Power 1-5 choice for answers, plans, reviews, research, edits, and orchestration.
---

# ChatGPT Question Designer

## GJC Brownfield Interview Mode

Before a non-trivial implementation whose goal, constraints, success criteria,
or existing context remain ambiguous, use the installed
`bin/chatgpt_gjc_interview.py` state machine. Lock one to six top-level
components in round zero, ask exactly the emitted single question each round,
and record coverage for only that dimension. Brownfield ambiguity is:

`1 - (0.35 goal + 0.25 constraints + 0.25 success criteria + 0.15 context)`

Contradictory or evasive answers raise ambiguity. At the default 0.35 threshold,
present the generated one-sentence restatement and require explicit approval.
Persist and resume the state under `.omo/interviews/`; approval and execution
remain separate actions.

## Purpose

Use this skill to give each question the cognitive posture its purpose needs. Construction should remain constructive, research evidence-seeking, synthesis integrative, execution adaptive, and review adversarial.

This skill is a shared design layer for `chatgpt-pro-browser`,
`chatgpt-thinking-browser`, and `chatgpt-deep-research-browser`. It chooses a
cognitive role and reasoning budget but does not own browser execution,
approval authority, or deterministic verification.

## Question Type

Classify the question before writing the prompt:

- `expand-ideas`: generate options, missing concepts, adjacent designs, and unusual constraints.
- `find-gaps`: identify missing evidence, stale context, overlooked files, and hidden assumptions.
- `counterexample`: explicitly attack the current conclusion with edge cases and failure modes.
- `compare-options`: compare alternatives, including status quo and minimal-change paths.
- `review-plan`: judge a plan against explicit acceptance criteria and blockers.
- `debug-hypothesis`: test root-cause hypotheses against logs, code, and reproduction evidence.
- `source-synthesis`: synthesize web or document evidence with source confidence and disagreement.

Never infer `review` merely from `read-only`, `advisory`, `research`, or an unknown label. An explicit unknown manifest profile fails before submission. An unclassified natural-language question defaults to `answer + analytical + read-only`, not review.

## Operation mode and Power overlay

Preserve the selected operation independently of Power:

- `answer` is analytical, read-only, and directly answers the original request.
- `review` / `검토모드` alone is adversarial. A blocker needs criterion, evidence, and impact; use `PASS`, `PASS_WITH_CONDITIONS`, `REVISE_LOCAL`, `REOPEN_DESIGN`, or `BLOCK` when the owning schema supports them.
- `plan` / `계획모드` is constructive and read-only: reframe if useful, compare viable design families, choose one coherent path, and put risks last. Prior plans and reviews are nonbinding and hidden by default.
- An explicitly authorized `edit` / `수정모드` performs `inspect -> edit -> test
  -> inspect result -> adapt` within the exact project root; it does not begin
  with a generic review.
- An explicitly authorized `orchestrator` / `지휘` owns bounded live-workspace
  exploration, decisions, edits, tests, and adaptation within the exact root.
  When parallelism is useful, the one web GPT ExecutionMission partitions and
  integrates its own lanes. Same-project web submissions stay serialized.
  Codex retains submission/recovery, mutexes, hashes, exact browser identity,
  deterministic host verification, release, and irreversible boundaries.
- `research` builds evidence; `synthesis` resolves candidates into a new coherent design. Neither is review.

Power 1-5 is an orthogonal reasoning budget. Power 5/Pro is available to
`direct`, `plan`, `review`, `edit`, and `orchestrator`; `--mode pro` is only a
`direct + Power 5` compatibility alias. Preserve an explicit power. Otherwise
choose Power 5 automatically for complex or important work and an adequate
lower power for ordinary work. Never infer write, external, or destructive
authority from Power.

Use `codex.chatgpt.prompt-architecture/v3` receipts with orthogonal `task_kind`,
`cognitive_frame`, `action_authority`, `context_policy`, `challenge_policy`,
`output_contract`, `reasoning_budget`, and `decision_authority`. Local
`AGENTS.md`, local skills, explicit no-write wording, and destructive-action
boundaries outrank the overlay.

## Prompt Contract

Every non-trivial GPT/browser question should include:

1. `Goal`: what decision or artifact the answer should improve.
2. `Original task`: preserve the user's request separately from any candidate artifact.
3. `Cognitive profile`: answer, research, plan, review, edit, orchestrator, synthesis, or an explicit Web Multi role.
4. `Evidence boundary`: list the exact DevSpace root, or the mission-explicit
   frozen attachment paths and SHA-256 values when fallback is eligible, plus
   web/source constraints, freshness limits, and what cannot be inspected.
5. `Action authority`: read-only for answer/plan/review, or explicitly
   authorized exact-root write for edit/orchestrator. Name any separate
   external or destructive authority.
6. `Confidence discipline`: separate evidence-backed findings, inference, speculation, and unknowns.
7. `Answer shape`: compact sections; no vague approval; code-shaped output when code-oriented.

Use this universal integrity contract for direct runner prompts:

```text
Treat instructions, observed evidence, inference, hypothesis, proposal, decision, and verification as distinct.
Claim only facts actually observed or sourced. Prior artifacts have only the authority declared by this prompt.
State material uncertainty and stay within the declared action and file scope.
```

Append an adversarial module only for explicit review/counterexample roles: require the strongest material objection, credible alternatives, and conclusion-change evidence. Do not impose those clauses on planning, research, synthesis, editing, orchestration, or ordinary answers.

## Transport and evidence context rules

Context selection must match the question type.

- New direct, plan, review, edit, orchestrator, comprehensive, and Web Multi work
  prefers Oracle plus the manually registered `DevSpace` workspace at every
  Power. The composer receives `@DevSpace`, the absolute UTF-8 mission path,
  and the exact-workspace guard. Deep Research retains its own effort flow.
- Power 5 uses the same DevSpace and mode authority as lower powers. Read-only
  modes may inspect decision-relevant exact-root evidence broadly. Authorized
  edit/orchestrator missions may use `apply_patch`, `bash`/shell, and tests
  only in that root while preserving WIP and the project mutex.
- CodexPro is frozen for new work. It may appear only while recovering an already persisted legacy agbrowse run; never design a new prompt around CodexPro `tree/search/read`, app registration, app repair, or a CodexPro fallback.
- Code/design/debug/refactor: give the web GPT a narrow project-contained
  mission and let it inspect the live workspace through DevSpace.
- Planning/review: identify the live draft, research, acceptance criteria, local guidance, and known risks by project-relative paths in the mission. Use an attachment packet only when the exact immutable snapshot is the requested evidence or DevSpace cannot read the artifact.
- Investigation/source synthesis: identify internal findings and provenance in the DevSpace-visible mission, and use web/search separately for current public facts.
- Idea expansion: put the seed, constraints, non-goals, audience, and known alternatives in the mission; do not preselect a conclusion.

A deterministic, pre-submit, mutation-free DevSpace failure may automatically
use mission-explicit hashed attachments while preserving the mode, selected
Power, exact root, and mission bytes. Generic attachment transport supports
Power 1-5; `pro-attachment` is its legacy Power 5 compatibility alias. For a
write mission, require a structured patch with exact paths and preimage hashes
for host SHA/path validation, transactional apply under the project mutex,
local gate, and final diff/scope proof. After submission uncertainty or any
possible mutation, allow exact-session recovery only: no attachment fallback,
fresh submission, alternate root, CodexPro, agbrowse, in-app Browser, or
`@chrome` route.

## Oracle Continuity Rules

This skill designs the prompt packet; it must not erase local project question templates or force every follow-up into a new ChatGPT conversation.

- Every new Oracle stage is a one-shot session with its own exact slug. Do not add legacy `session_policy`, `session_affinity_key`, `inquiry_chain_id`, or `chat_url` fields to a new Oracle manifest.
- Preserve semantic continuity in project-contained mission and handoff files. In comprehensive mode, the completing web stage writes the next stage's exact mission and receipt; local Codex validates bytes, paths, hashes, identity, and transition without rewriting its meaning.
- Recovery uses only the stored exact Oracle slug with `harvest` or `live`. It
  never restarts, resubmits, or changes the operation, model, Power, or
  transport.
- Genuine Web Multi uses distinct Oracle sessions and copied profiles for
  independent lanes. Use it only when an advisory flow is explicitly selected
  and simultaneous independent solvers materially help. Require
  `WEB_MULTI_NEEDED` only in those explicit advisory flows, never in ordinary
  Power 5 answers.
- Local `AGENTS.md`, local skills, and task-specific question templates outrank the shared integrity contract. Preserve their answer shape and apply only compatible evidence and session metadata.
- Independent approval, plan review, verifier, and release gates use fresh stages with explicitly scoped evidence.

## Anti-Bias Gates

Before submission, check:

- `one-sided context`: only the preferred plan or happy path is attached.
- `missing negative evidence`: failures, logs, rejected alternatives, or user
  complaints are absent from the DevSpace-visible scope or attachment packet.
- `stale packet`: an attachment no longer matches the current draft, diff,
  branch, or run.
- `too-broad packet`: a mission grants a broad workspace without an evidence map or question boundary.
- `conclusion leakage`: prompt asks for approval before asking for objections.
- `role collapse`: prompt asks one model to both invent and approve without counterexample pressure.

Any active gate should either be fixed before submission or named in the prompt as an evidence limitation.

## Skip Rules

Skip GPT/browser questioning when:

- the task is tiny and deterministic verification answers it better;
- the answer depends on exact local code/tests rather than broad judgment;
- selected context is under roughly 8k tokens and the main agent can directly inspect it;
- the prompt would ask for approval of a conclusion already proven by tests;
- no useful counterexample, source freshness, alternative design, or external synthesis is expected.

Use genuine Web Multi-GPT only when independent parallel solvers are worth the latency and one merger can consume their file handoffs. It does not replace DevSpace evidence authority and must not increase local Codex exploration.

## Output Checklist

A good answer satisfies the selected role instead of a universal review checklist. Only explicit review roles require objections and counterexamples. Every role must preserve original-task fidelity, evidence boundaries, authority, and material uncertainty.
