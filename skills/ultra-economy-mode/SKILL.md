---
name: ultra-economy-mode
description: Run expensive or long tasks with a Luna Max local commander, a Power 5 plan, and separate Oracle implementation and verification. Use when the user says 초절약모드, Ultra Economy Mode, or explicitly requests Luna Max web-first execution.
---

# Ultra Economy Mode

Minimize local model cost without treating the small local model as the main
reasoning surface. Use the existing Oracle comprehensive engine with the
`ultra-economy` profile.

## Activation gate

1. On the **first** Ultra Economy Mode request in a Codex task, always stop
   before creating a subagent, browser, Oracle, Pro, or web session and give
   exactly one concise instruction: select `GPT-5.6 Luna` and reasoning effort
   `Max`, then confirm completion.
2. Give that instruction even when the user says the model is already selected.
   Do not inspect, infer, or verify the current model or reasoning effort from
   runtime metadata, screenshots, `~/.codex/config.toml`, role files, prompts,
   tool output, or previous tasks.
3. After the user confirms the selection, treat the activation handshake as
   satisfied for the rest of the same Codex task. Continue the workflow without
   asking again, including after compaction, recovery, stage transitions, or
   follow-up requests in that task.
4. Ask once again only for a new Codex task's first Ultra Economy Mode request.
   Never rewrite the user's global model defaults to activate this mode.

## Local commander contract

- Keep the commander to routing, compact mission creation, durable receipt
  reading, exact-session monitoring, hash checks, and one deterministic gate.
- For every substantive local semantic task, spawn one fresh `default`
  subagent with explicit model `gpt-5.6-luna`, reasoning effort `max`, and a
  minimal history fork. Do not use the globally configured scout,
  implementer, or verifier roles because their model contracts may differ.
- Give a subagent only the bounded objective, exact artifact paths, current
  stage receipt, authority boundary, and success criteria. Never forward the
  full conversation or a growing transcript.
- Prefer one worker at a time. Use at most two only for genuinely independent
  read-only work; never exceed the global cap of three spawned threads.
- Deterministic host scripts and simple status polling remain commander work;
  they do not require model delegation.

## Web-first stage graph

Run separate sessions so each semantic boundary can inspect the prior durable
artifact:

```text
one-time exact-root qualification
  -> Power 5 plan/design (read-only)
  -> regular web design review and implementation-mission authoring
  -> regular web implementation and project tests
  -> separate regular web final verification or repair handoff
  -> one local deterministic gate
```

Use `bin/chatgpt_oracle_comprehensive.py` with these manifest fields:

```json
{
  "schema": "codex.chatgpt.oracle-comprehensive/v1",
  "workflow_profile": "ultra-economy",
  "initial_stage": "pro"
}
```

Add the normal absolute project, workflow, mission, app, and local gate fields.
The local commander owns the one-time conversational activation handshake; the
engine does not re-read or re-verify the task model at later stages. A manifest
self-declaration is not a substitute for the handshake.

The engine must fail closed before submission when the profile, Power-5
plan-first stage, exact root qualification, or minimum four-stage budget is
missing. Do not use Power 5 as the first connector-health probe.

Power is independent of stage authority. The Power 5 design stage remains
read-only because it is `plan`; review is also read-only. Only an explicitly
authorized implementation `edit`/`orchestrator` stage may write within the
exact root using `apply_patch`, `bash`/shell commands, and tests. Preserve an
explicit lower Power on ordinary later stages, and choose Power 5 automatically
only when their complexity or importance warrants it. External or destructive
actions still require separate authority.

DevSpace is preferred at every stage. A deterministic, pre-submit,
mutation-free DevSpace failure may automatically use mission-explicit hashed
attachments while preserving the stage operation and selected Power. Generic
attachment transport supports Power 1-5; `pro-attachment` is the legacy Power
5 alias. For a write stage, require a structured patch for host SHA/path/scope
validation, transactional apply under the exact-project mutex, local gate, and
diff proof. After submission uncertainty or possible mutation, recover only
the exact session; never fall back or submit a replacement.

## Failure and residual work

- Recover only the exact persisted Oracle stage. Never create a replacement
  submission from an ambiguous, possibly submitted, or possibly mutated
  failure.
- If web work reaches a genuine local-only boundary, give that one bounded
  residual task to a fresh Luna Max subagent, then return to a separate web
  verification stage when semantic review is still needed.
- Do not repeat app/settings checks or endpoint probes after the project's
  exact-root qualification while the DevSpace config hash is unchanged.
- Completion requires the final web PASS receipt plus host proof of the actual
  diff, changed-path scope, preserved unrelated WIP, and a zero-exit local
  deterministic gate. Local Luna judgment or a web patch alone is not release
  authority.
