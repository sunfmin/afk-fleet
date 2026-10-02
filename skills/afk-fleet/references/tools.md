# Tools (`scripts/afk.py`) — the deterministic muscle

Disclosed reference for [`afk-fleet`](../SKILL.md): the full interface table for the deterministic
subcommands the launcher and tick call. Every deterministic step in the skill is one of these
subcommands; the tick (an LLM) orchestrates and judges, but calls the tool for the fixed mechanics
rather than re-deriving git/gh incantations from prose each pass (ADR-0004). Every decision is a pure
function in `afk_decide.py` (fixture-tested); `afk.py` only gathers their inputs and applies effects.

**The contract every subcommand shares:**

- It prints **one JSON object**. Exit 0 = it ran (a lost claim race is `{"won": false}`, still exit
  0). Exit 3 = `{"error": …}` — an operational failure (auth, network, a rejected push, bad input),
  never an outcome to act on.
- It takes **`--config '<json>'`** — the canonical config from `afk config`, as returned by
  `afk probe` — and, when it touches GitHub, **`--repo <owner/name>`**. Pass both on every call; the
  table omits them. Resolution is one order everywhere: override flag → `--config` → the defaults
  table (ADR-0009). The override flags (`--ttl`, `--retry`, `--base`, …) are for hand-debugging.
- `afk <subcommand> --help` is the authoritative flag list; a test fails if this table or any
  `afk …` example in the skill names a subcommand or flag that does not exist.

| Subcommand | Does | Kind |
|---|---|---|
| `afk config --file <path>` | parse + validate the repo config → **canonical JSON** (every key, defaults filled; unknown key → error). `--defaults` prints the one defaults table (ADR-0009) | pure (file read) |
| `afk probe` | the **claim namespace** that works (`refs/afk`, else the `refs/heads` fallback — `blocked`, `ci_on_push`) folded into the returned **`config`**, which the launcher holds from then on; in `gate.ci: local`, also the merge target's branch-`protection` verdict (ADR-0012) | effect + pure verdict |
| `afk worker-command [--check <cmd>]` | settle the string every worker is started with: ask-or-not (stock launcher → never asked) + the login shell's Claude-starting aliases to offer; `--check` resolves an answer's first word and flags a missing unattended flag (ADR-0010) | effect (login shell) + pure verdict |
| `afk rebuild --instance <id>` | **one read-only call → the whole working set**: frontier (dispatch+excluded), `mine` subclassified with PR/checks/attempt-labels and the `board_phase` each renders as, `peer_live`, `stale` (with the sha reclaim needs), fingerprint (ADR-0008). In `gate.ci: local` an open PR is `awaiting_merge` outright — no checks are read (ADR-0012) | effect gather + pure assembly |
| `afk no-pr --issue <n> --worktree <path> --terminal <busy\|idle\|none> [--terminal-idle-seconds <s>]` | **why a claim has no PR**, gathered and decided in one call: reads the worktree's git progress, the worker's latest `afk:verdict` marker, and the state of each issue it says it is blocked by → `{outcome, action, idle_seconds, open_blockers, progress, verdict}` (coding / idle_done / idle_blocked / idle_failed / dead). The tick supplies only the terminal probe | effect gather + pure verdict |
| `afk recovery --issue <n>` | a **dead** claim's recoverable progress → the tiered **continuation** verdict `{tier, action, prompt, worktree, branch}` (worktree still here? branch ahead of base?) — ADR-0011 | effect gather + pure verdict |
| `afk claim <n> --instance <id>` | atomic create-or-lose the claim ref → `{won}` (`won: false` names the `owner`; a push that failed for any other reason is an error) | effect |
| `afk reclaim <n> --instance <id> --expect-sha <sha>` | `--force-with-lease` take of a stale claim → `{won}` | effect |
| `afk takeover --list --instance <my id>` / `--from <dead id> --instance <my id>` | the fleet instances GitHub remembers (claim markers + heartbeat refs) with heartbeat age / host / claim count — or force-take a dead one's claims: same atomic push as `reclaim`, staleness gate skipped, a fresh-heartbeat target held back until `--yes` (ADR-0011). `--instance` is always **my** id | effect + pure verdict |
| `afk release <n>` | delete a claim ref (idempotent) | effect |
| `afk heartbeat --instance <id>` | refresh my heartbeat if due → `{refreshed}` | effect |
| `afk next-attempt --labels <csv>` | retry-or-escalate from `afk-attempt/*` | pure |
| `afk pace --summary <json>` | next launcher sleep, with the `ttl/2` cap | pure |
| `afk fingerprint --last <fp> --skips <k>` | digest observable state → skip-or-tick for the launcher's cycle gate (same gatherer as `rebuild`) | effect gather + pure verdict |
| `afk gate-run --worktree <p>` | run `gate.local_command` in a worktree → `{status, excerpt, exit_code, timed_out}` — the **merge-time completion gate** in `gate.ci: local`, mirroring the ephemeral CI-log sub-read (ADR-0012) | effect + pure verdict |
| `afk status <n> --phase <phase> [--instance <id>] [--pr <pr>] [--attempt <k>]` | upsert the human-facing progress **status board** comment, idempotently. `<phase>` is a `mine` row's `board_phase`, or `merged` / `escalated`; `retry_max` and the gate's name come from config | pure render + effect |
| `afk scan` | debug only: every claim + heartbeat ref as read from the remote. A tick never calls it — `rebuild` does | effect |
| `afk classify-claims --instance <id>` | debug only: the mine / peer_live / stale partition on its own. A tick never calls it — `rebuild` does | effect gather + pure verdict |

Judgment stays with the tick and is **not** a tool: is the implementation correct (the gate),
adversarial verify, resolving a sync conflict, the orphan-vs-alive read of a liveness probe, whether a
recovered worktree is sane to build on, wording an escalation, the human authorization.
