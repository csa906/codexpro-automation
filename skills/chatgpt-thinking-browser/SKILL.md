---
name: chatgpt-thinking-browser
description: Run new ChatGPT direct, plan, review, edit, and orchestrator missions through Oracle with independent Power 1-5 selection, preferred DevSpace access, and guarded attachment fallback.
---

# ChatGPT through Oracle

Read `chatgpt-question-designer` before shaping a new non-trivial mission. Put
one absolute UTF-8 mission file inside the exact project root, then preview:

```powershell
python "$env:USERPROFILE\.codex\bin\chatgpt_oracle_dispatch.py" --mode <direct|plan|review|edit|orchestrator> --project-root C:\project --mission-path C:\project\mission.md --manifest-output C:\project\.ai-bridge\oracle.json --reasoning-level <Pro|Very-High|High|Medium|Low> --dry-run
```

For `edit` or `orchestrator`, also pass `--fallback-contract` with a strict
`codex.chatgpt.oracle-attachment-fallback/v1` JSON file. It must bind the same
root, mission SHA-256, authority, and Power, enumerate every allowed edit path
and operation with its preimage hash, and name one shell-free local gate whose
first argv entry is the regular absolute executable (not a Store alias,
symlink, or reparse point). The runtime binds that executable's SHA-256 and
forbids editing a gate input.

Remove `--dry-run` only for an explicitly authorized live web run. Oracle
selects `GPT-5.6 Sol` and proves the requested visible Power before prompt send.
Power 5/Pro is available to every listed operation mode; `--mode pro` remains a
compatibility alias for `direct` plus Power 5. Choose Power 5 automatically for
complex or important work, while preserving explicit or adequate lower powers
for ordinary work. Power never grants authority.

## Operation authority

- `direct`/answer, `plan`, and `review` are read-only regardless of Power.
- An explicitly authorized `edit` or `orchestrator` mission may inspect, write
  within the exact root, use `apply_patch` and `bash`/shell commands, and run
  tests.
- External, publishing, destructive, credential, or out-of-root actions require
  separate explicit authority.

`orchestrator` is one web submission that owns its mission's bounded
exploration, decisions, edits, tests, and adaptation. Comprehensive mode is a
separate multi-stage workflow owned by `chatgpt-pro-plan-handoff`; use it when
the plan or implementation needs independent stages and a final local gate.

Authorized writers hold the exact-project mutex for the mission, preserve
unrelated WIP and declared exclusions, and never broadly stage, reset, stash,
clean, or overwrite another writer's changes. Completion requires host proof
of the actual diff, changed-path scope, preserved WIP, and the declared local
gate, not only a web answer.

## DevSpace and attachment fallback

DevSpace is preferred at every Power. The runtime sends `@DevSpace`, the
absolute mission path, and the exact-workspace guard. It does not automate the
app picker or settings and never substitutes another root. CodexPro and
agbrowse are frozen for exact persisted-run recovery only.

Only a deterministic, pre-submit, mutation-free DevSpace failure may
automatically use mission-explicit attachment transport. Freeze every attached
file and SHA-256, preserve the selected operation and Power, and never infer a
project packet from prose. `pro-attachment` is the legacy Power 5 alias; generic
attachment transport may preserve Power 1-5.

For a write mission, the attachment result is a structured patch. The host
validates its exact paths and preimage hashes, applies it transactionally under
the project mutex, runs the local gate, and checks the resulting diff/scope.
After submission uncertainty or any possible mutation, do not fall back or
submit again; recover the exact session only.

## Session and recovery

Every new run uses a throwaway copy of the manually signed-in Oracle profile
and an Oracle-owned hidden window. Control state and output stay host-only under
`%USERPROFILE%\.codex\state\chatgpt-oracle`.

Recover using the stored slug:

```powershell
python "$env:USERPROFILE\.codex\bin\chatgpt_oracle_run.py" recover --run-dir C:\exact\host-run --action harvest
python "$env:USERPROFILE\.codex\bin\chatgpt_oracle_dispatch.py" --resume-run C:\exact\host-run
```

Recovery never restarts/resubmits, changes Power or transport, or downgrades a
durable COMPLETE result. If needed, a bounded recovery browser may open only
that slug's persisted conversation URL. A later `running` observation cannot
erase terminal evidence; disagreement remains attention-required until a fresh
exact terminal harvest settles it. For dispatcher-owned runs, the second
command resumes only the persisted host diff/gate or patch-apply phase from its
hash-bound baseline; it never creates a browser submission.

For an already persisted agbrowse/CodexPro run only, use its exact legacy
recovery command. Never create a new legacy run.
