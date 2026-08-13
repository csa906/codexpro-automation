# Release checklist

New submissions use Oracle only. CodexPro and agbrowse checks below apply only
to packaging integrity and exact recovery of already-persisted legacy runs;
they are not active routing prerequisites.

The default installer must leave both frozen dependencies untouched.
`-InstallLegacyRecoveryDependency` is the only opt-in that may install or
contract-validate agbrowse for an old persisted run.

## Version and presentation

- Choose the SemVer impact using [VERSIONING.md](VERSIONING.md).
- Require the same version in `package.json`, the root `package-lock.json`
  entries, `install-manifest.json`, and the newest changelog heading.
- Validate every relative Markdown link and every committed SVG/PNG asset.
- Keep Korean and English README mode tables, safety claims, requirements, and
  documentation maps semantically equivalent.
- Confirm the repository description, topics, issue templates, PR template,
  release badge, and social-preview asset match the current product name.
- Create an annotated `vMAJOR.MINOR.PATCH` tag and GitHub Release only after the
  exact commit passes both Windows and macOS CI. Never move a published tag.

- Run `python scripts/check_portability.py --root .`, `python scripts/run_v4_contract_tests.py --focused`, `python scripts/run_v3_contract_tests.py`, and `python scripts/run_v4_contract_tests.py --full`.
- Confirm `install-manifest.json` and `package.json` inventory every shipped runtime/schema file, the v4 runner, and both v7/v8 quiescent app-trace incident fixtures.
- Confirm MIT copyright is `2026 ventianima-lab` and third-party notices retain the multi-gpt commit/hash attribution.
- Do not vendor agbrowse, Codex, CodexPro, browser binaries, or account data.
- Verify no workflow has `schedule`; CI must use Windows and macOS with mocked/offline lifecycle checks.
- Treat agbrowse update as an explicit, reviewed agent action. There is no background checker, scheduled updater, candidate slot, or promotion pointer.
- Exercise `install.ps1`, `doctor.ps1`, `uninstall.ps1`, and `rollback.ps1` with a temporary `CODEX_HOME`; never require Git to bootstrap or verify a release.
- Before a normal install, verify its read-only dependency preflight completes before any managed file mutation. The returned token binds selected version/integrity, prior dependency identity, and observed unlocked state; the subsequent update must reacquire the lock and reject drift. Before an explicit update, confirm no active or uncertain run state exists. The update receipt must preserve the prior npm version/integrity, executable and contract hashes, then capture and validate the reviewed public-command contract before replacing it.
- Future agbrowse versions must be explicit resolved semvers. Pass their exact registry integrity to contract capture/validation, retain 0.1.18 only as the tested baseline, and require the invoking agent/workflow to select the resulting versioned contract explicitly.
- Exercise both file-only install rollback and mocked normal install rollback. Receipt v3 must restore the prior agbrowse package, selected contract bytes, and prior update receipt; the exact inverse must prove registry integrity, installed version, and executable SHA-256 after npm reports success. Dependency drift must fail in preflight before installed files change, and any late inverse failure must report `PARTIAL`.
- Verify install WAL behavior: per file, durable `INTENT` precedes mutation; the file is flushed, `replacement.json` is written, hashes are verified, and only then is the entry `COMPLETE`. A later install resumes an interrupted WAL by restoring only receipt-owned bytes; a modified destination remains a conflict.

## macOS Ultrawork 1.7

- Exercise the portable lifecycle in a temporary `CODEX_HOME`, including update rollback and the original install uninstall.
- Verify OMO Codex Light only, `features.multi_agent_v2.max_concurrent_threads_per_session = 5`, telemetry opt-out, and direct smoke calls for ultrawork/ulw-loop/start-work/reviewer hooks.
- Run `python3 scripts/run_harness_canary.py`; use `--real-time` for the release-host 85-minute canary and retain its SHA-256 receipt.
- Validate all three managed plists with `plutil`; force-restart the supervisor and verify only `com.ventianima.codexpro-automation.*` labels are touched.
- Require exactly one persisted DevSpace allowed root. Tailscale login, Funnel approval, macOS security approval, ChatGPT Developer Mode registration, and Owner approval remain manual gates.

## Parallel implementation v3

- Confirm all eight v3 schemas parse as draft 2020-12 and retain `additionalProperties: false`, bounded IDs/paths, and registered test IDs rather than free shell strings.
- Verify a missing manifest gate or environment gate creates no lease, parent run, staging repository, exact-unit app, tunnel, or browser send.
- Exercise fixed topology, logical/final overlap, drive/home equality, reparse escape, singleton allowed roots, and sibling isolation tests.
- Verify staging uses `--no-local --no-hardlinks --no-checkout`, has no alternates/reference/shared object store, and detects worker mutation of common Git metadata.
- Verify every dependency and path-conflict edge is unioned into one component, only one unit per component is active, and independent components may continue when another component requires exact-session recovery.
- Verify `send.claim` v2 is immutable and authority-bound; post-send uncertainty must never create a second provider submission.
- Verify exact-unit app identity includes singleton roots, bash off, workspace write, full tool mode, actual listener identity, and separate Cloudflare tunnel identity immediately before send.
- Run `python scripts/run_v3_contract_tests.py`; opt into the live Windows exact-unit integration only in an isolated release environment with test credentials.
- Verify full registered tests and canonical baseline/config/submodule/filesystem revalidation occur before temporary-ref import and ff-only apply. A forced conflict or test failure must leave canonical source unchanged.

## Release lifecycle safety

- `install.ps1` only manages manifest-owned files. By default it neither
  installs nor updates CodexPro/agbrowse because those dependencies are frozen
  for new work. `-InstallLegacyRecoveryDependency` is an explicit opt-in used
  only when an existing persisted legacy run requires that exact recovery
  runtime; `-SkipDependencyInstall` suppresses dependency mutation entirely.
- Retain the unique receipt and backup directory. `uninstall.ps1` is a safe inverse: it removes only unchanged created files and restores only unchanged overwritten files; modified destinations are reported as conflicts.
- Run `doctor.ps1` before an explicit `update.ps1 -AgbrowseVersion <version>`. Updates defer while bridge state is active or uncertain and never terminate it.
