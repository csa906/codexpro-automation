---
name: chatgpt-oracle-runtime
description: "Current Oracle runtime for mission-scoped ChatGPT work: Power 1-5 is independent of operation authority, DevSpace is preferred, and safe pre-submit failures may use hashed attachments."
---

# ChatGPT Oracle Runtime

This is the only active browser path for new GPT work. CodexPro and agbrowse
are frozen for exact persisted-run recovery only.

## Modes, Power, and authority

The dispatcher supports `direct`, `plan`, `review`, `edit`, `orchestrator`,
`deep-research`, `manual`, and `attachment`. `answer` is the prompt-design alias
for `direct`. `--mode pro` is a compatibility alias for `direct` at Power 5;
`pro-attachment` is the Power 5 compatibility alias for `attachment`. Neither
alias creates a separate permission mode.

For `GPT-5.6 Sol`, Power 1-5 correspond to Low, Medium, High, Very High, and
Pro. Preserve an explicit user-selected power. Otherwise choose Power 5
automatically for complex or important work and retain a lower power for
ordinary work; never use power selection to expand authority. Deep Research
owns its separate effort flow.

Authority comes only from the operation and mission:

- `direct`/answer, `plan`, and `review` are read-only.
- An explicitly authorized `edit` or `orchestrator` mission may write in its
  exact project root using `apply_patch`, `bash`/shell commands, and tests.
- External actions, publishing, destructive changes, credentials, and writes
  outside that root require separate explicit authority.

Authorized writers must hold the normalized exact-project mutex, preserve
unrelated WIP and declared exclusions, and never broadly stage, reset, stash,
clean, or overwrite another writer's changes.
Snapshot/patch proof fails closed on symlinks and reparse points. The mutex and
WAL guard ordinary local concurrency and crashes; they are not an OS sandbox
against a malicious same-account process racing filesystem operations.

## Transport selection

DevSpace is preferred at every Power. Send only `@DevSpace`, the absolute
mission path, and the exact-workspace guard. The web GPT reads the mission and
applicable `AGENTS.md` chain completely, uses only that normalized root, and
may retry opening that same root once. A parent, child, similarly named, active,
or shell-boundary workspace is not a substitute.

Qualify exact equality against DevSpace `allowedRoots` before the first run and
cache the result by config hash. Do not repeatedly automate ChatGPT app/settings
state. A deterministic DevSpace failure may automatically fall back to
attachment transport only when it is proven pre-submit and mutation-free.

Attachment fallback preserves the operation mode, selected Power 1-5, exact
root, and immutable mission bytes. The mission explicitly enumerates regular
non-symlink attachments and their SHA-256 values; the runtime must not discover
files from prose or scrape the project. `pro-attachment` is only a legacy
Power-5 alias for this generic transport.

For authorized write missions, attachment transport returns a structured patch
instead of claiming workspace mutation. The host validates every relative path
and preimage hash, applies the patch transactionally under the same project
mutex, runs the mission's local gate, and verifies the final scope and diff.

Once a prompt may have been submitted, or any mutation is observed or possible,
fallback and fresh submission are forbidden. Only exact-session recovery may
continue.

## Manifest and run

Use schema `codex.chatgpt.oracle-run/v1`. Bind the absolute existing
`project_root`, project-contained UTF-8 `mission_path`, operation `task_kind`,
selected model/Power, transport, host-only output paths, and mutex timeout.
Attachment manifests additionally bind the ordered attachment paths and hashes.

For a new one-shot run, use `chatgpt_oracle_dispatch.py`. Read-only modes get a
minimal mission-only fallback contract automatically. `edit` and
`orchestrator` must pass `--fallback-contract` with schema
`codex.chatgpt.oracle-attachment-fallback/v1`, exact root/mission/Power,
evidence hashes, edit paths/operations/preimages, and one local gate.

Preview with the wrapper, which shows final argv, prompt, mission SHA-256, and
artifact paths without launching Oracle:

```powershell
python skills/chatgpt-oracle-runtime/scripts/run_chatgpt_oracle.py run --manifest C:\absolute\oracle-job.json --dry-run
```

Execute only after an explicit live-run request:

```powershell
python skills/chatgpt-oracle-runtime/scripts/run_chatgpt_oracle.py run --manifest C:\absolute\oracle-job.json
```

Do not substitute Oracle's own browser `--dry-run`; version-specific browser
preflight may still run. Require the hash-gated Oracle compatibility contract,
isolated copied profile, owned hidden window, exact model, and visible selected
Power evidence before accepting a send.

## Completion and recovery

All missions require exit zero, fresh nonempty host-only output, immutable run
identity, and a refreshed transcript. When the v1 outcome contract applies,
`TASK_OUTCOME: EXECUTED` must be the final semantic marker; provider transport
success alone is not execution proof.

An authorized write mission is complete only after the host proves the actual
diff, every changed path is within declared scope, unrelated WIP is preserved,
and the declared local gate exits zero. This applies to direct DevSpace writes
and host-applied attachment patches. Provider claims or patch generation alone
are insufficient.

Recover only the stored Oracle slug:

```powershell
python skills/chatgpt-oracle-runtime/scripts/run_chatgpt_oracle.py recover --run-dir C:\absolute\run --action harvest
```

Use `live` only to follow that same session. Recovery never restarts, resubmits,
changes mode/model/Power/transport, or creates a replacement conversation.
Observer disagreement, prompt-not-observed timeout, nonzero post-submit exit,
or a missing live tab remains attention-required under the same project lock
until exact-session evidence settles it. A no-submission settlement requires
the existing hash-bound evidence and explicit user confirmation; it does not
itself authorize a new run.

One-shot dispatcher runs persist their contract and whole-workspace baseline
before submission. After exact recovery reaches terminal output, resume the
host-only acceptance/apply phase with:

```powershell
python "$env:USERPROFILE\.codex\bin\chatgpt_oracle_dispatch.py" --resume-run C:\absolute\run
```

The deterministic episode claim makes a repeated new-run command resolve to
that same episode rather than submit again.

Control state, Oracle output, and transcripts remain under
`%USERPROFILE%\.codex\state\chatgpt-oracle`, outside the project. Use
`chatgpt_oracle_comprehensive.py` for staged work and
`chatgpt_oracle_multi.py` only for explicitly selected independent advisory
sessions.
