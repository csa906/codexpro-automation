# Legacy prompt architecture reference

This filename is retained so older links do not break. Its former
CodexPro/agbrowse submission instructions are frozen and must not be used to
create new ChatGPT work.

All new modes use Oracle:

- regular direct, plan, review, edit, orchestrator, research, comprehensive,
  and Web Multi-GPT use Oracle plus the manually registered DevSpace app;
- Power 1-5 is independent of operation authority; Power 5 may run an
  authorized exact-root `edit` or `orchestrator`, while `--mode pro` remains
  the read-only `direct + Power 5` compatibility alias;
- generic attachment transport preserves the chosen Power and may be selected
  automatically only after deterministic pre-submit DevSpace failure plus
  unchanged-workspace proof; `pro-attachment` is its Power 5 alias;
- CodexPro and agbrowse may be used only for exact recovery of an already
  persisted legacy run.

See [GLOBAL_CHATGPT_ROUTING.md](GLOBAL_CHATGPT_ROUTING.md) and the current
mode skills for the authoritative prompt and transport contracts.

The exact frozen inventory and its boundary are listed in
[FROZEN_LEGACY.md](FROZEN_LEGACY.md).
