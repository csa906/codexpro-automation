# Codex Web GPT Automation Repository Rules

## GPT Automation Change Persistence

- Any durable change to GPT or ChatGPT skills, CodexPro/agbrowse bridges, browser runners, prompt or mode routing, recovery, state, locks, tabs, app registration, Web Multi-GPT, or their tests must include focused verification and a descriptive Git commit before the work is reported complete.
- The installed files under `%USERPROFILE%\.codex` are deployment copies, not the sole source of truth. Synchronize reusable fixes back into this repository instead of leaving them only in the global installation.
- Public-safe reusable changes must be committed to the clean public `main`, pushed, and checked in CI. Never copy credentials, host-only values, sensitive artifacts, or private Git history into this repository.
- Never push a private-history development branch to the public repository. If commit, push, or CI verification is blocked, report the exact dirty files and blocker and do not claim completion.

## Filesystem hygiene

- Never create test output, temporary directories, logs, downloaded archives, or dependency checkouts directly under a drive root such as `C:\` or `D:\`.
- Use the operating-system temp directory under a task-specific `Codex` child first. When a shorter Windows path is genuinely required, use the authoritative repository's gitignored `.codex-tmp\<task>` directory.
- Put reusable third-party source checkouts under `%LOCALAPPDATA%\Codex\Sources`, not a drive root. An explicitly user-approved project root is not temporary storage and must not be repurposed.
- Before cleaning an existing drive-root item, classify its ownership and active references. Preserve user projects, system folders, credentials, and ambiguous items; move confirmed automation artifacts to a recoverable archive instead of deleting them.

## Comprehensive-mode ownership

- macOS new-work support uses the portable Python lifecycle, POSIX identity,
  DevSpace/Tailscale Funnel, and `com.ventianima.codexpro-automation.*`
  LaunchAgents. It must never reuse, overwrite, or stop `com.openclaw.codexpro*`
  services or mutate `~/.codexpro`.
- Harness episodes use a 4,200-second web answer budget, 4,500-second soft
  checkpoint, and 4,800-second handoff boundary. A live or uncertain Oracle
  slug always owns the mission; recovery may harvest that exact session but
  must not resubmit it.
- Do not blanket-fan-out Codex native subagents. Normal operation starts with
  at most two concurrent workers and the global hard cap is three spawned
  threads. Concurrent writers require explicit, non-overlapping file lists or
  distinct worktrees. Oracle Web Multi remains separately bounded to five
  provider sessions, and local-subagent and Oracle-web phases do not overlap.

- Every new ChatGPT submission uses Oracle. Regular GPT uses the manually registered DevSpace app. Qualified Pro uses `GPT-5.6 Sol` at the Pro effort through the same app in a read-only DevSpace route; explicit `pro-attachment` is reserved for immutable or external evidence that DevSpace cannot read and is never an automatic fallback.
- New GPT comprehensive workflows use `codex.chatgpt.oracle-comprehensive/v1`. Existing CodexPro/agbrowse comprehensive v1-v4 state remains exact recovery-only.
- The completing web GPT stage authors the next stage's semantic prompt. Local Codex may validate UTF-8, hashes, stage identity, immutable bindings, transport, recovery, and deterministic final tests, but must not rewrite the next prompt or take over expensive exploration/implementation.
- A selected Web Multi advisory uses genuine independent Oracle sessions. Provider generation is limited to at most five concurrent children; larger accepted topologies run in capacity waves without reducing their logical lane count.
- Comprehensive review owns plan repair and finalization. It fixes every locally resolvable defect inline, writes the corrected final plan and implementation mission, then returns PASS or PASS_WITH_NOTES. New work never loops review back to plan; legacy REVISE is terminal compatibility only, and FAIL requires a concrete external blocker.
- Every regular Oracle stage is bound to one exact project root and one exact mission path. DevSpace may retry that same root once after listing registered workspaces, but must never substitute a parent, child, similarly named, active workspace, or shell boundary workaround.
- A Pro DevSpace stage has the same exact-root binding. It may broadly and adaptively inspect decision-relevant project material, beginning with the `read('.')` directory-list compatibility path, but it must not write, edit, invoke a shell, or run commands. Once the one-time DevSpace qualification is complete, do not re-check app/settings state per run.
- Before the first DevSpace-backed Oracle submission for a new exact project root, verify exact equality against the current local DevSpace `allowedRoots`. Cache that qualification against the config hash; revalidate only when the config changes. Missing, parent, child, or similarly named roots fail before Oracle/browser creation. This lightweight root guard must not automate or repeatedly inspect ChatGPT app/settings state.
- Pro DevSpace completion requires a v1 `TASK_OUTCOME` marker as the final nonempty line, with citations and reference definitions before it. A bounded provider-rendering exception may accept exactly one marker followed only by single-line HTTP(S) Markdown reference definitions; ordinary prose or another marker remains unknown. Exit zero plus a durable answer is not successful execution when the session exposed no callable DevSpace tools or could not read the exact mission/root. A durably terminal `NOT_EXECUTED` run may release the project lock for one fresh retry with identical mission bytes and SHA-256; a repeated tool-exposure failure is `attention_required`, with no automatic app-settings manipulation or attachment fallback.
- Transport and runner recovery retain the exact workflow/stage identity. They must not create a replacement workflow or reset the semantic revision budget.
- CodexPro and agbrowse are frozen for new work. Their code may be invoked only to recover an exact persisted legacy run, never as an Oracle fallback.
- Every new Oracle run must use a throwaway copy of the manually signed-in profile and an Oracle-owned hidden window. Never share the manual-login Chrome process across concurrent projects.
- Exact-slug recovery may relaunch a bounded recovery browser from the persisted profile seed and open only the recorded conversation URL. It must never restart, resubmit, or create a replacement conversation.
- A nonzero Oracle exit after submission, including a browser response timeout, is attention-required rather than web-terminal failure. It retains exact-session ownership and allows only exact-slug live/harvest recovery.
- Exact session authority is monotonic. `terminal_observed` cannot regress to `live`; observer disagreement remains attention-required under the same project lock until a later exact terminal harvest produces fresh nonempty durable output.
