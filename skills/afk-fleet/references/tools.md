# Tools (`scripts/afk.py`) — the deterministic muscle

Disclosed reference for [`afk-fleet`](../SKILL.md): the full interface table for the deterministic
subcommands the launcher and tick call. Every deterministic step in the skill is one of these
subcommands; the tick (an LLM) orchestrates and judges, but calls the tool for the fixed mechanics
rather than re-deriving git/gh incantations from prose each pass (ADR-0004). Each prints one JSON
object. Pure verdicts live in `afk_decide.py` (fixture-tested); effectful ops drive git refs / gh.

| Subcommand | Does | Kind |
|---|---|---|
| `afk config --file <path>` | parse + validate the repo config → **canonical JSON** (every key, defaults filled; unknown key → error). `--defaults` prints the one defaults table (ADR-0009) | pure (file read) |
| `afk worker-command [--check <cmd>]` | settle the string every worker is started with: ask-or-not (stock launcher → never asked) + the login shell's Claude-starting aliases to offer; `--check` resolves an answer's first word and flags a missing unattended flag (ADR-0010) | effect (login shell) + pure verdict |
| `afk rebuild --repo <r> --instance <id> --config <json>` | **one read-only call → the whole working set**: frontier (dispatch+excluded), `mine` subclassified with PR/checks/attempt-labels, `peer_live`, `stale` (with the sha reclaim needs), fingerprint (ADR-0008). In `gate.ci: local` an open PR is `awaiting_merge` outright — no checks are read (ADR-0012) | effect gather + pure assembly |
| `afk worker-status --worktree <path> --base <branch>` | a `no_pr` worker's git **progress** in its worktree → `{commits_ahead, dirty, last_commit_ts, worktree_mtime_ts}` — the decisive coding-vs-finished signal, independent of terminal chrome (git only, no gh) | effect (git) |
| `afk recovery --issue <n> --repo <r> --config <json>` | a **dead** claim's recoverable progress → the tiered **continuation** verdict `{tier, action, prompt, worktree, branch}` (worktree still here? branch ahead of base?) — ADR-0011 | effect gather + pure verdict |
| `afk verdict --repo <r> --issue <n>` | the LATEST parsed `afk:verdict` marker the worker left → `{found, phase, blocked_by, reason, comment_url}` — its machine-readable reason for opening no PR | effect gather + pure parse |
| `afk classify-no-pr --terminal <busy\|idle\|none> --progress <json> --verdict <json> --config <json>` | the **5-way `no_pr` verdict** from those signals → `{outcome, action}` (coding / idle_done / idle_blocked / idle_failed / dead) | pure |
| `afk claim <n> --instance <id>` | atomic create-or-lose the claim ref → `{won}` | effect |
| `afk reclaim <n> --instance <id> --expect-sha <sha>` | `--force-with-lease` takeover of a stale claim → `{won}` | effect |
| `afk takeover --list` / `--instance <dead id> --as <my id>` | the fleet instances GitHub remembers (claim markers + heartbeat refs) with heartbeat age / host / claim count — or force-take a dead one's claims: same atomic push as `reclaim`, staleness gate skipped, a fresh-heartbeat target held back until `--yes` (ADR-0011) | effect + pure verdict |
| `afk release <n>` | delete a claim ref (idempotent) | effect |
| `afk heartbeat --instance <id> --config <json>` | refresh my heartbeat if due → `{refreshed}` | effect |
| `afk next-attempt --labels <csv> --config <json>` | retry-or-escalate from `afk-attempt/*` | pure |
| `afk pace --summary <json> --config <json>` | next launcher sleep, with the `ttl/2` cap | pure |
| `afk fingerprint --repo <r> --last <fp> --skips <k> --config <json>` | digest observable state → skip-or-tick for the launcher's cycle gate (same gatherer as `rebuild`) | effect gather + pure verdict |
| `afk gate-run --worktree <p> --config <json>` | run `gate.local_command` in a worktree → `{status, excerpt, exit_code, timed_out}` — the **merge-time completion gate** in `gate.ci: local`, mirroring the ephemeral CI-log sub-read (ADR-0012) | effect + pure verdict |
| `afk status <n> --repo <r> --state <json>` | upsert the human-facing progress **status board** comment, idempotently | pure render + effect |

Every config-consuming subcommand takes the **same canonical `--config` JSON** the launcher got from
`afk config` — passed verbatim, never re-derived; explicit flags (`--ttl`, `--retry`, …) remain as
overrides for tests and hand-debugging. Resolution is one order everywhere:
flag → `--config` → the defaults table (ADR-0009).

(The verdicts `rebuild` absorbed — `frontier`, `scan`, `classify-claims`, `subclassify` — still exist
as undocumented debug surfaces over the same pure core; a tick never calls them.)

Judgment stays with the tick and is **not** a tool: is the implementation correct (the gate),
adversarial verify, resolving a sync conflict, the orphan-vs-alive read of a liveness probe, whether a
recovered worktree is sane to build on, wording an escalation, the human authorization.
