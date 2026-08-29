---
name: chatgpt-pro-plan-handoff
description: Run staged Oracle work with operation-scoped authority, independent Power 1-5 selection, preferred DevSpace access, safe hashed-attachment fallback, and host completion proof.
---

# Comprehensive Oracle handoff

New comprehensive work uses `bin/chatgpt_oracle_comprehensive.py` with schema
`codex.chatgpt.oracle-comprehensive/v1`:

```text
plan -> optional Power 5 or explicit Oracle Web Multi advisory -> review
     -> implementation -> final web gate -> one local deterministic gate
```

Power 5/Pro is a normal reasoning level for the same operation modes, not a
read-only route or separate permission. `--mode pro` is the legacy `direct +
Power 5` alias. CodexPro and agbrowse remain exact persisted-run recovery only.

The optional `ultra-economy` profile starts with a Power 5 `plan` mission, then
uses separate web review, implementation, and final-gate sessions. Follow
`skills/ultra-economy-mode/SKILL.md` for its Luna Max activation and commander
contract.

## Stage authority and Power

- Plan and review stages are read-only at every Power.
- An explicitly authorized implementation `edit` or `orchestrator` stage may
  write only within the exact project root using `apply_patch`, `bash`/shell
  commands, and tests.
- External actions, publishing, destructive changes, credentials, and
  out-of-root writes require separate explicit authority.

Preserve a user- or workflow-selected Power. Otherwise choose Power 5
automatically for complex or important stages and retain an adequate lower
Power for ordinary stages. Power never expands stage authority.

Comprehensive mode is a staged workflow, not a prompt variant. Its
implementation stage carries the one-submission orchestrator ownership
contract, while the surrounding stages author hash-bound missions and receipts.
Use single-submission `orchestrator` when the goal and approach are settled;
use comprehensive mode when independent planning/review, an explicit advisory
branch, or deterministic final proof materially helps.

## Workflow bindings

The manifest binds absolute `project_root`, `workflow_dir`,
`initial_mission_path`, stable `workflow_id`, stage operation and Power, and a
nonempty `local_gate_command`. Every web stage writes its next mission and a
`codex.chatgpt.oracle-stage-result/v1` receipt. The host validates
workflow/stage/attempt/input identities, UTF-8 paths, hashes, transition, and
status; it never rewrites the semantic prompt.

The review GPT owns plan repair and finalization. It fixes every locally
resolvable defect in its returned artifact and authors the implementation
mission. `PASS` and `PASS_WITH_NOTES` proceed; `FAIL` is reserved for a concrete
external input, authority, safety, or execution blocker. New work does not loop
review back to plan.

## DevSpace and attachments

DevSpace is preferred at every Power and stage. Each stage binds the same exact
root and exact input mission, reads applicable `AGENTS.md`, and may retry only
that root once. Parent, child, similarly named, active-workspace, and
shell-boundary substitutions are forbidden. Authorized writers preserve
unrelated WIP, declared exclusions, and the exact-project mutex; they never
broadly stage, reset, stash, clean, or overwrite another writer's changes.

The generic one-shot dispatcher can automatically use attachment transport
only after a deterministic, pre-submit, mutation-free DevSpace failure. The
current comprehensive v1 runner does not replace a failed stage with a new
transport: it preserves the exact workflow/stage record as
attention-required. An explicit plan-authored frozen-evidence attachment for
the optional advisory Power 5 stage remains supported. Never infer or scrape a
packet from prose.

A one-shot read-only attachment fallback returns its durable answer. A one-shot
write fallback returns a structured patch with exact relative paths and
preimage hashes; the dispatcher validates and applies it transactionally,
runs the local gate, and records the resulting diff. The web session must not
claim that it directly mutated DevSpace.

After prompt submission, any uncertainty or observed/possible mutation permits
only exact-session recovery. It never authorizes attachment fallback, a fresh
stage submission, or a replacement workflow.

Require `WEB_MULTI_NEEDED` only when the workflow explicitly asks a stage to
decide an advisory Web Multi branch. Ordinary Power 5 stages do not emit or
trigger it automatically.

## Run, completion, and recovery

Preview the workflow before launch:

```powershell
python "$env:USERPROFILE\.codex\bin\chatgpt_oracle_comprehensive.py" --manifest C:\project\workflow.json --dry-run
```

Transport and runner recovery retain the exact workflow, stage, attempt, input
hash, model, Power, and project mutex. Missing output/receipt, a nonzero
post-submit exit, watchdog timeout, or ambiguous prompt submission returns
attention-required. Recovery observes or harvests only the stored slug; it
never kills, restarts, replaces, or resubmits the session.

Only a final web PASS plus current host proof can complete. For a write
workflow, host proof includes the actual diff, every changed path within the
declared scope, preserved unrelated WIP, and a zero-exit local deterministic
gate. A provider receipt, direct-write claim, or generated patch alone is not
completion.

If a prompt-not-observed run has hash-valid recovery evidence proving the same
binding but remains submission-uncertain, only explicit user confirmation may
authorize the existing `settle-no-submission` command. Settlement does not
itself authorize a fresh submission. Any possible mutation keeps exact-session
ownership.

Existing v1-v4 agbrowse comprehensive state and v3 parallel implementation are
legacy recovery-only. Their files remain installed solely to recover the exact
persisted run.
