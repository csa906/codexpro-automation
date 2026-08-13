# Legacy agbrowse architecture v2

Architecture v2 is retained only to identify and recover already persisted
CodexPro/agbrowse runs. It is not a valid source of new-submission commands.

All new regular and Pro ChatGPT work uses Oracle. Regular work uses DevSpace.
Qualified Pro uses `GPT-5.6 Sol` at the Pro effort through read-only DevSpace
at the exact project root; explicit `pro-attachment` is only for immutable or
external DevSpace-unreadable evidence. New comprehensive and Web Multi
workflows also use Oracle. There is no Oracle-to-agbrowse fallback.

See [GLOBAL_CHATGPT_ROUTING.md](GLOBAL_CHATGPT_ROUTING.md). Exact legacy
recovery remains implemented by the frozen runners and their immutable state;
new sends are rejected before browser mutation.

The exact frozen inventory and its boundary are listed in
[FROZEN_LEGACY.md](FROZEN_LEGACY.md).
