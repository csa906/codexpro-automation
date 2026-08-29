---
name: chatgpt-pro-browser
description: Use for one-shot Oracle work at Power 5/Pro. Power is reasoning effort, while the selected direct, plan, review, edit, or orchestrator mode owns authority and completion.
---

# ChatGPT Power 5 through Oracle

## Standalone scope

This is the one-shot Power 5 route. It returns one durable result and stops; it
does not start comprehensive staging or implementation on its own. Use
`chatgpt-pro-plan-handoff` only when the user requests the staged workflow.

Oracle is the only backend for a new run. CodexPro and agbrowse are exact
persisted-run recovery paths only; do not substitute another model or browser.

## Power and authority

Power 5/Pro is the maximum reasoning level for the same `GPT-5.6 Sol` model,
not a separate operation or permission mode. It is available to `direct`,
`plan`, `review`, `edit`, and `orchestrator`:

- `direct`/answer, `plan`, and `review` are read-only.
- An explicitly authorized `edit` or `orchestrator` mission may write only in
  its exact project root and may use `apply_patch`, `bash`/shell commands, and
  tests.
- External side effects, publishing, destructive actions, and writes outside
  the exact root require separate explicit authority.

`--mode pro` is a compatibility alias for `--mode direct --reasoning-level
Pro`; it does not grant write authority. Prefer an explicit operation mode plus
Power 5 when the distinction matters.

## Preferred DevSpace route

DevSpace is preferred at every power. Bind one exact absolute project root,
read the mission and applicable `AGENTS.md` chain completely, and begin with
the `read('.')` directory-list compatibility call. Read-only missions may
inspect decision-relevant material broadly. Authorized write missions preserve
unrelated WIP, the exact-project mutex, and the declared write scope; they must
not broadly stage, reset, stash, clean, or overwrite another writer's changes.

Before the first DevSpace submission for a root, verify exact equality against
`allowedRoots` and cache it by config hash. Do not substitute a parent, child,
similarly named, or active workspace, and do not repeat app/settings checks
while the cached config is unchanged.

## Attachment compatibility and fallback

`pro-attachment` is the legacy Power 5 compatibility alias for the generic
attachment transport. Attachment transport may preserve any selected Power
1-5; it is not inherently Pro and grants no extra authority.

A deterministic DevSpace failure may automatically use attachment transport
only when it is proven pre-submit and mutation-free. The mission must explicitly
name every regular non-symlink attachment and freeze its SHA-256; use the
repository context-packet helper rather than an ad-hoc project scrape. Preserve
the selected operation mode, power, exact root, and mission bytes.

For a read-only mission, the attachment result is the answer. For an authorized
write mission, the web session returns a structured patch with exact relative
paths and preimage hashes. The host validates SHA/path/scope, applies it
transactionally inside the exact root, runs the declared local gate, and
rechecks the resulting diff. The attachment session never claims it wrote the
workspace.

After prompt submission, any uncertainty or observed/possible mutation permits
only exact-session recovery. It never authorizes attachment fallback or a
fresh submission.

## Run and completion

Preview the explicit mode and Power 5 contract before a live run:

```powershell
python "$env:USERPROFILE\.codex\bin\chatgpt_oracle_dispatch.py" --mode direct --reasoning-level Pro --project-root <ROOT> --mission-path <MISSION> --manifest-output <MANIFEST> --dry-run
python "$env:USERPROFILE\.codex\bin\chatgpt_oracle_dispatch.py" --mode pro --project-root <ROOT> --mission-path <MISSION> --manifest-output <MANIFEST> --dry-run
python "$env:USERPROFILE\.codex\bin\chatgpt_oracle_dispatch.py" --mode edit --reasoning-level Pro --project-root <ROOT> --mission-path <MISSION> --fallback-contract <FALLBACK_CONTRACT_JSON> --manifest-output <MANIFEST> --dry-run
python "$env:USERPROFILE\.codex\bin\chatgpt_oracle_dispatch.py" --mode pro-attachment --project-root <ROOT> --mission-path <MISSION> --attachment <PACKET> --manifest-output <MANIFEST> --dry-run
```

Remove `--dry-run` only after validating the manifest, immutable mission,
selected model/power, exact-project mutex, and compatibility hashes. Completion
always requires a durable nonempty host output and exact model/power evidence.
An authorized write mission additionally requires host-side diff, path/scope,
and declared local-gate proof; a provider success marker alone is insufficient.
The write fallback contract is mandatory and uses schema
`codex.chatgpt.oracle-attachment-fallback/v1`; it explicitly binds allowed
paths/operations, preimage hashes, evidence hashes, and the shell-free gate.

Recover only the stored exact Oracle run directory and slug. `live` and
`harvest` may observe or collect that session; they never restart, resubmit,
change route/model/power, or create a replacement conversation. Already
persisted legacy agbrowse/CodexPro runs retain only their exact recovery tools.
After a dispatcher run becomes terminal, run `chatgpt_oracle_dispatch.py
--resume-run <RUN_DIR>` to resume its persisted host-only diff/gate or patch
phase. That command cannot submit a prompt.

Do not require `WEB_MULTI_NEEDED` in ordinary Power 5 answers. Include that
decision block only when the user or owning workflow explicitly requests a Web
Multi advisory decision.
