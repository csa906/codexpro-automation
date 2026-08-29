# Global ChatGPT routing

The supported English names are `GPT`/`direct`, `plan`, `review`, `edit`,
`orchestrator`, `deep research`/`deep-research`, `Web Multi-GPT`,
`Local Multi-GPT`, `comprehensive mode`, `Ultra Economy Mode`/`ultra-economy`,
and `Pro`. Korean names documented in the main README map to the same runners;
language never selects a different backend.

Use this routing in the Codex global `AGENTS.md` after installing the package.

- New regular ChatGPT work, including direct, plan, review, edit,
  orchestrator, research, comprehensive, and Web Multi-GPT, uses Oracle plus
  the manually registered DevSpace app.
- Web work selects exact `GPT-5.6 Sol`. Power 1-5 map to Oracle `light`,
  `standard`, `extended`, `extra-high`, and `heavy`. Preserve an explicit
  choice; otherwise use Power 5 for complex or important work and an adequate
  lower Power for ordinary work. Power changes reasoning depth, not authority.
- The regular composer contains only `@DevSpace` and an absolute UTF-8 mission
  path. It does not attach the task body and does not inspect or mutate ChatGPT
  app settings per question.
- Power 5 uses the same operation authority as every other Power. `direct`,
  `plan`, and `review` remain read-only; explicitly authorized `edit` and
  `orchestrator` runs may edit and test only inside the exact project root.
  `--mode pro` is the `direct + Power 5` compatibility alias.
- All DevSpace output uses the v1 task-outcome marker. Exit zero and a durable
  answer do not count as execution when required tools or the exact root were
  unavailable. Provider prose never establishes mutation-free retry authority.
- Generic attachment transport preserves Power 1-5. `pro-attachment` is its
  Power 5 compatibility alias. The one-shot dispatcher may select it
  automatically only after a structured deterministic pre-submit DevSpace
  failure plus unchanged-workspace proof. Its immutable contract binds every
  evidence/edit path, SHA-256, and local gate.
- A write fallback returns a strict structured patch for host-side validation
  and transactional exact-root application. Direct DevSpace writes and
  host-applied patches both require actual diff/scope and a zero-exit local
  gate. Once submission or mutation is possible, only exact-slug recovery is
  allowed.
- Existing persisted agbrowse runs remain recovery-only. There is no new
  agbrowse submission path and no Oracle-to-agbrowse fallback.
- Comprehensive stages author the next semantic mission and a bound hash
  receipt. Local Codex owns transport, immutable identity, host safety, and one
  final deterministic gate rather than rewriting web output.
- An optional comprehensive Pro stage is read-only DevSpace by default. A
  plan-authored explicit `pro-attachment` contract selects attachment-only;
  either route returns one strict identity-bound JSON envelope whose output and
  next-mission strings the host materializes byte-for-byte.
- Genuine Web Multi-GPT uses distinct Oracle sessions. Windows lanes use
  independent throwaway copies of the signed-in Oracle profile, run in waves
  of at most five, and hand compact files to one merger.
- Local Multi-GPT is an optional, read-only PC-local advisory component. It is
  fixed to `gpt-5.6-luna` with `max` reasoning and is not a web transport or a
  release authority.
- Ultra Economy Mode keeps the local commander and native subagents on exact
  Luna Max while separate Oracle sessions own Pro design, review,
  implementation, and web verification. Its first request in each Codex task
  always produces one Luna/Max selection instruction; after user confirmation,
  that task never re-inspects the runtime or asks again.

## Standalone Pro versus comprehensive

`chatgpt-pro-browser` is the visible one-shot Power 5 skill. The selected mode
owns authority, so an explicit `edit` or `orchestrator` may write while
`direct`, `plan`, and `review` remain read-only. It saves one durable result and
stops; ordinary Power 5 runs do not require `WEB_MULTI_NEEDED`. That decision
appears only in an explicitly selected advisory Web Multi flow.

`chatgpt-pro-plan-handoff` owns comprehensive mode. Only that staged runner may
place an optional Pro decision between plan and review and continue afterward
to implementation and gates. Natural-language `Pro` or `GPT Pro` requests route
to the standalone skill; explicit comprehensive-mode requests route to the
handoff skill.

## Orchestrator versus comprehensive

These two are often confused because both let the web GPT own implementation.
They differ in structure, not in ambition.

| | `orchestrator` (지휘) | comprehensive (종합) |
|---|---|---|
| Runner | `chatgpt_oracle_dispatch.py --mode orchestrator` | `chatgpt_oracle_comprehensive.py` |
| Web submissions | one | several, one per stage |
| Stage receipts | none | hash-bound per workflow/stage/attempt/input |
| Independent review | no | yes, review repairs and finalizes the plan |
| Pro / Web Multi stage | not available | selectable |
| Completion | the answer itself | final web PASS plus zero-exit local gate |
| Recovery unit | one run | workflow plus stage identity |

Comprehensive mode runs orchestrator-equivalent work as its implementation
stage, so it contains that mode rather than competing with it.

Pick `orchestrator` when the goal and approach are settled and one authorized
pass should finish the work at the lowest local and web cost. Pick comprehensive
when the plan needs independent review, when Pro or Web Multi must participate,
or when completion must be proven by a deterministic local gate. Do not hand-chain
`orchestrator` submissions to imitate staging; same-project submissions stay
serialized and the workflow engine owns stage identity and recovery.

The package does not overwrite an existing user `AGENTS.md` automatically.
Apply this block deliberately so unrelated personal rules are preserved.
