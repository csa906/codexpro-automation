<!-- BEGIN CODEX WEB GPT SUBAGENT POLICY -->
## Codex native subagent policy

- The primary commander uses GPT-5.6 Sol at high reasoning. Default subagents use GPT-5.6 Terra at medium reasoning; role files may narrow this further.
- Use subagents actively when the user, applicable repository rules, or a selected skill asks for delegation and the work is independently bounded.
- Do not blanket-fan-out. Start with no more than two concurrent workers in normal operation; the global hard cap is three spawned threads.
- Prefer `scout` for narrow repetitive read-only discovery, `implementer` only when the parent supplies an explicit non-overlapping file list, and `verifier` for independent read-only validation.
- Never assign overlapping write ownership. The primary agent integrates results and remains responsible for final deterministic verification.
- Keep `multi_agent_v2` disabled while it is unstable; the supported `[agents]` settings and standalone role files are sufficient.

## Filesystem hygiene

- Never create test output, temporary directories, logs, downloaded archives, or dependency checkouts directly under a drive root such as `C:\` or `D:\`.
- Use the operating-system temp directory under a task-specific `Codex` child first. If Windows path length requires a shorter location, use the active repository's gitignored `.codex-tmp\<task>` directory, never `D:\pytest-*` or another drive-root scratch path.
- Put reusable third-party source checkouts under `%LOCALAPPDATA%\Codex\Sources`. Keep explicit user project roots separate and never repurpose them as scratch space.
- Before cleanup, verify ownership and active references. Preserve user projects, system folders, credentials, and ambiguous items; move confirmed automation artifacts to a recoverable archive instead of deleting them.
<!-- END CODEX WEB GPT SUBAGENT POLICY -->
