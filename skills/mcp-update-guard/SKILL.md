---
name: mcp-update-guard
description: Part of the current Oracle automation path, safely update MCP servers, shared harness helpers, Oracle GPT runners, global skills, plugins, and related automation while preserving local customizations.
---

# MCP update guard

Use this skill for shared/global automation changes. Read the applicable
`AGENTS.md`, identify the authoritative source and installed deployment, and
preserve unrelated local customizations.

## Workflow

1. Classify the exact component and whether the work is an update,
   compatibility repair, policy refresh, or recovery fix.
2. Inspect source Git status and the installed file identity before editing.
   Never overwrite credentials, browser profiles, runtime state, or unrelated
   user changes.
3. For non-trivial GPT automation design or implementation, use the selected
   current GPT workflow only when the user asked for web delegation. Every new
   ChatGPT run uses Oracle:
   - direct, plan, review, edit, orchestrator, comprehensive stages, and Web
     Multi prefer the manually registered DevSpace app at every Power;
   - Power 5/Pro is a normal reasoning level, not an authority mode: answer,
     plan, and review remain read-only, while only an explicitly authorized
     edit/orchestrator mission may write within its exact root using
     `apply_patch`, `bash`/shell commands, and tests;
   - choose Power 5 automatically for complex or important work and preserve
     explicit or adequate lower powers for ordinary work;
   - only a deterministic pre-submit, mutation-free DevSpace failure may use
     mission-explicit hashed attachments while preserving mode and Power;
     `pro-attachment` is the legacy Power 5 attachment alias;
   - CodexPro/agbrowse may be used only for exact recovery of an already
     persisted legacy run and never as a fallback.
4. Prefer small compatibility changes over wholesale replacement. Preserve
   local ports, names, roots, tokens, routing, and hooks unless the task
   explicitly changes them.
5. Batch coherent edits, inspect the final diff once, run focused regression
   tests, then broader tests according to blast radius. A write mission is not
   complete until the host proves changed-path scope, preserved WIP, and the
   declared local gate. An attachment write result must be a structured patch
   whose paths and preimage hashes the host validates before transactional
   apply under the exact-project mutex.
6. Synchronize reusable GPT automation changes to the authoritative
   `codexpro-automation` source, install the verified bytes, commit with a
   descriptive message, push public-safe changes, and check CI.

## Single repair owner

Automation sources have exactly one repair owner. A project session that hits an
automation defect reports it and stops; it does not edit runners, state, patches,
or their tests. Cross-session patching previously produced duplicate fixes,
conflicting state rules, and repairs aimed at the layer that reported the symptom
instead of the layer that failed.

- Build the handover with
  `python "$env:USERPROFILE\.codex\bin\chatgpt_oracle_incident.py" report --run-dir <exact-run-dir>`.
  The packet carries the exact run directory, the classified bucket, the
  lifecycle verdict with its authority source, and existing evidence paths.
- Classify before repairing. Run
  `python "$env:USERPROFILE\.codex\bin\chatgpt_oracle_diagnose.py" --summary-only`
  and fix the largest bucket rather than the newest report. A `pre-submit-*`
  bucket proves no web submission occurred and is safe to retry; a
  `post-submit-*` bucket requires exact-slug recovery and never a replacement
  submission.
- Treat `safe_for_fresh_run: false` as binding. Do not resubmit, stop, or close
  another session's work while repairing code.

## Safety boundaries

- Do not delete or recreate credential-bearing state during a normal update.
- Do not use resource pressure as authority to block, terminate, downgrade, or
  duplicate user-visible work.
- Do not silently switch Oracle model, Power, root, or browser backend. The only
  automatic transport change is the guarded pre-submit, mutation-free hashed
  attachment fallback above; a post-submit uncertain or possibly mutated run
  permits exact-session recovery only.
- Do not create a new legacy agbrowse/CodexPro run while repairing recovery
  code.
- Stop and report exact dirty files when authoritative persistence, push, or CI
  cannot be completed.

## Report

Report updated components, preserved customizations, focused and broad
verification, installed/source synchronization, commit/push/CI state, rollback
evidence, and any remaining risk.
