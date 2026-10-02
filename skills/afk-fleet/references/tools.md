# Tools (`scripts/afk.py`) — the deterministic muscle

Disclosed reference for [`afk-fleet`](../SKILL.md): the full interface table for the deterministic
subcommands the launcher and tick call. Every deterministic step in the skill is one of these
subcommands; the tick (an LLM) orchestrates and judges, but calls the tool for the fixed mechanics
rather than re-deriving git/gh incantations from prose each pass (ADR-0004). Every decision is a pure
function in `afk_decide.py` (fixture-tested); `afk.py` only gathers their inputs and applies effects.

**The contract every subcommand shares:**

- It prints **one JSON object**. Exit 0 = it ran, and the result is a real answer (a lost claim race
  is `{"won": false}`, still exit 0). Exit 3 = `{"error": …}` — it could not do its job (auth,
  network, a rejected push, an unreadable remote, bad input, a bad command line), never an outcome
  to act on. No failure is reported as an empty or negative exit-0 result.
- It **requires `--config '<json>'`** — the canonical config from `afk config`, as returned by
  `afk probe` — and, when it touches GitHub, **`--repo <owner/name>`**. Pass both on every call; the
  table omits them. Only `afk config` and `afk worker-command` take no config (they run before one
  exists). Resolution is one order everywhere: `--set` → `--config` → the defaults table for keys
  the JSON omits (ADR-0009), and the result is validated like the config file is.
- `--set <key>=<value>` (repeatable) overrides one config key for one call, by the config file's own
  key name — `--set claim_lease_ttl_seconds=60`, `--set gate.ci=local`. It is for tests and
  hand-debugging; a tick passes the run's `--config` and nothing else.
- `afk <subcommand> --help` is the authoritative flag list; a test fails if this table or any
  `afk …` example in the skill names a subcommand or flag that does not exist.

| Subcommand | Does | Kind |
|---|---|---|
| `afk config --file <path>` | parse + validate the repo config → **canonical JSON** (every key, defaults filled; unknown key → error). `--defaults` prints the one defaults table (ADR-0009) | pure (file read) |
| `afk probe` | the **claim namespace** that works (`refs/afk`, else the `refs/heads` fallback) folded into the returned **`config`**, which the launcher holds from then on → `{blocked, config}` (+ `detail`, the server's rejection, when `blocked`); in `gate.ci: local`, also the merge target's branch-`protection` verdict (ADR-0012) | effect + pure verdict |
| `afk worker-command [--check <cmd>]` | settle the string every worker is started with: ask-or-not (stock launcher → never asked) + the login shell's Claude-starting aliases to offer; `--check` resolves an answer's first word and flags a missing unattended flag (ADR-0010) | effect (login shell) + pure verdict |
| `afk rebuild --instance <id>` | **one read-only call → the whole working set**: frontier (dispatch+excluded), `mine` subclassified with PR/checks, the `attempt` it is on, and the `board_phase` it renders as, `peer_live`, `stale` (with the sha reclaim needs), fingerprint (ADR-0008). In `gate.ci: local` an open PR is `awaiting_merge` outright — no checks are read (ADR-0012) | effect gather + pure assembly |
| `afk no-pr --issue <n> --worktree <path> --terminal <busy\|idle\|none> [--terminal-idle-seconds <s>]` | **why a claim has no PR**, gathered and decided in one call: reads the worktree's git progress, the worker's latest `afk:verdict` marker, and the state of each issue it says it is blocked by → `{outcome, action, idle_seconds, open_blockers, progress, worker_verdict}` (coding / idle_done / idle_blocked / idle_failed / dead; `worker_verdict` is the marker the worker posted — an input, not the conclusion). The tick supplies only the terminal probe | effect gather + pure verdict |
| `afk recovery --issue <n>` | a **dead** claim's recoverable progress → the tiered **continuation** verdict `{tier, action, prompt, reason, worktree, branch}` (worktree still here? branch ahead of base?). A remote that cannot be read is an error, never tier 3 — ADR-0011 | effect gather + pure verdict |
| `afk claim <n> --instance <id>` | atomic create-or-lose the claim ref → `{won}` (`won: false` names the `owner`; a push that failed for any other reason is an error) | effect |
| `afk reclaim <n> --instance <id> --expect-sha <sha>` | `--force-with-lease` take of a stale claim → `{won}` | effect |
| `afk takeover --list --instance <my id>` / `--from <dead id> --instance <my id>` | the fleet instances GitHub remembers (claim markers + heartbeat refs) with heartbeat age / host / claim count — or force-take a dead one's claims: same atomic push as `reclaim`, staleness gate skipped, a fresh-heartbeat target held back until `--yes` (ADR-0011). `--instance` is always **my** id | effect + pure verdict |
| `afk release <n>` | delete a claim ref → `{released: true}` (idempotent: already gone counts). A delete that left the claim on the remote is an error | effect |
| `afk heartbeat --instance <id>` | refresh my heartbeat if due → `{refreshed}` | effect |
| `afk next-attempt --attempt <k>` | retry-or-escalate from the `mine` row's `attempt` → `{action, from_label, to_label}` (the `afk-attempt/*` labels to swap) | pure |
| `afk pace --summary <json>` | next launcher sleep, with the `ttl/2` cap | pure |
| `afk fingerprint --last <fp> --skips <k>` | digest observable state → skip-or-tick for the launcher's cycle gate (same gatherer as `rebuild`) | effect gather + pure verdict |
| `afk gate-run --worktree <p>` | run `gate.local_command` in a worktree → `{status, excerpt, exit_code, timed_out}` — the **merge-time completion gate** in `gate.ci: local`, mirroring the ephemeral CI-log sub-read (ADR-0012) | effect + pure verdict |
| `afk status <n> --phase <phase> [--instance <id>] [--pr <pr>] [--attempt <k>]` | upsert the human-facing progress **status board** comment, idempotently. `<phase>` is a `mine` row's `board_phase`, or `merged` / `escalated`; `<k>` is the row's `attempt`; `retry_max` and the gate's name come from config | pure render + effect |
| `afk scan` | debug only: every claim + heartbeat ref as read from the remote (an unreadable remote is an error, not an empty list). A tick never calls it — `rebuild` does | effect |
| `afk classify-claims --instance <id>` | debug only: the mine / peer_live / stale partition on its own. A tick never calls it — `rebuild` does | effect gather + pure verdict |

Judgment stays with the tick and is **not** a tool: is the implementation correct (the gate),
adversarial verify, resolving a sync conflict, the orphan-vs-alive read of a liveness probe, whether a
recovered worktree is sane to build on, wording an escalation, the human authorization.
