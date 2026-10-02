# Tools (`scripts/afk.py`) — the deterministic muscle

Disclosed reference for [`afk-fleet`](../SKILL.md): the full interface table for the deterministic
subcommands the launcher and tick call. Every deterministic step in the skill is one of these
subcommands; the tick (an LLM) orchestrates and judges, but calls the tool for the fixed mechanics
rather than re-deriving git/gh/orca incantations from prose each pass (ADR-0004). Every decision is a pure
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
- `afk …` in this table is shorthand for the executable `<skill>/scripts/afk.py …` — one word, no
  interpreter in front, so it survives being held in a shell variable under zsh.
- `afk <subcommand> --help` is the authoritative flag list; a test fails if this table or any
  `afk …` example in the skill names a subcommand or flag that does not exist.

| Subcommand | Does | Kind |
|---|---|---|
| **Bootstrap** | | |
| `afk config --file <path>` | parse + validate the repo config → **canonical JSON** (every key, defaults filled; unknown key → error). `--defaults` prints the one defaults table (ADR-0009) | pure (file read) |
| `afk probe` | the **claim namespace** that works (`refs/afk`, else the `refs/heads` fallback) folded into the returned **`config`**, which the launcher holds from then on → `{blocked, config}` (+ `detail`, the server's rejection, when `blocked`); in `gate.ci: local`, also the merge target's branch-`protection` verdict (ADR-0012) | effect + pure verdict |
| `afk worker-command [--check <cmd>]` | settle the string every worker is started with: ask-or-not (stock launcher → never asked) + the login shell's Claude-starting aliases to offer; `--check` resolves an answer's first word and flags a missing unattended flag (ADR-0010) | effect (login shell) + pure verdict |
| **The launcher's loop** | | |
| `afk cycle --instance <id> [--state <json>] [--summary <json>]` | one launcher cycle (ADR-0007, ADR-0017). **Top** (no `--summary`): digest observable state → `{action: tick\|skip, reason, state}`; on a skip it also refreshes the lease when the fleet holds claims (`heartbeat`) and returns `sleep_seconds`. **Bottom** (`--summary`, after a tick): fold the tick's summary (integer `in_flight` and `frontier_remaining` required) into the state → `{state, sleep_seconds}`, with the `ttl/2` cap. `state` is opaque — hand the last one back verbatim | effect gather + pure verdict |
| **Observation (read-only)** | | |
| `afk rebuild --instance <id>` | **one call → the whole working set**: frontier (dispatch+excluded), `mine` subclassified (`awaiting_merge` / `awaiting_ci` / `failure` / `handed_back` / `no_pr` / `closed`) with PR/checks, the `attempt` it is on, and the `board_phase` it renders as, `peer_live`, `stale` (with the sha reclaim needs), `free_slots`, fingerprint (ADR-0008). In `gate.ci: local` an open PR is `awaiting_merge` outright — no checks are read (ADR-0012). A PR whose sync conflict was handed back is `handed_back` until its head contains the target tip the hand-back named (ADR-0019) | effect gather + pure assembly |
| `afk no-pr --issue <n> --terminal <busy\|idle\|none> [--terminal-idle-seconds <s>] [--worktree <path>]` | **why a claim has no PR** — or, for a `handed_back` claim, whether its worker is still resolving — gathered and decided in one call: finds the issue's worktree through orca (`--worktree` overrides), reads its git progress against the remote base tip, the worker's latest `afk:verdict` marker, and the state of each issue it says it is blocked by → `{outcome, action, idle_seconds, open_blockers, worktree, progress, worker_verdict, nudged_at, handed_back_at}` (coding / idle_done / idle_blocked / idle_stalled / idle_failed / dead; `worker_verdict` is the marker the worker posted — an input, not the conclusion). The tick supplies only the terminal probe | effect gather + pure verdict |
| `afk recovery --issue <n>` | what `afk dispatch` would continue from, without acting: a **dead** claim's recoverable progress → the tiered **continuation** verdict `{tier, action, prompt, reason, worktree, branch}` (worktree still here? branch ahead of base?). A remote that cannot be read is an error, never tier 3 — ADR-0011 | effect gather + pure verdict |
| **Act — one call per transition (ADR-0017)** | | |
| `afk dispatch --issue <n> --instance <id> --worker-command <cmd> [--start auto\|fresh] [--ready-timeout <s>]` | **start a worker**: claim (or confirm the claim is mine) → continuation tier → orca worktree at the right commit (the remote base tip, the pushed branch tip, or the worktree still here) → agent started with the worker launch command → prompt filled, delivered and submitted → status board. `{started: true, claim: won\|held, tier, action, prompt, worktree, branch, terminal}`, or `{started: false, claim: lost, owner}`. `--start fresh` discards the previous attempt first | effect + pure verdicts |
| `afk merge --issue <n> --instance <id> [--verified <head>] [--allow-no-checks] [--gate-timeout <s>] [--excerpt-lines <k>]` | **land a PR**: sync (merge, never rebase) → push → the configured machine gate on that head → adversarial-verify check → `gh pr merge` pinned to the gated head → status board → release → worktree cleanup. `{outcome: merged \| conflict \| handed_back \| gate_red \| awaiting_ci \| no_checks \| needs_verify, pr, worktree, head, …}` | effect + pure verdicts |
| `afk hand-back --issue <n> --instance <id> --worker-command <cmd> [--ready-timeout <s>]` | **return a sync conflict to the worker that wrote the branch** (`merge` → `conflict`): abort the merge in the worktree → write the instruction (target + tip, conflicted files, merge-not-rebase, gate, push to the same PR) → record it as a marker comment on the PR → deliver it — one submitted line to the worker's terminal, or, when that is gone, a new worker started by continuation in the same worktree → status board. `{action: handed_back, pr, target, target_tip, files, delivery: terminal\|continuation, terminal, worktree, comment_id}`. Claim, PR, branch and worktree are kept; no attempt is spent (ADR-0019) | effect + pure render |
| `afk nudge --issue <n> --instance <id> [--worktree <path>]` | **tell a silent worker to carry on** (`no-pr` → `idle_stalled`): read the last screen of its terminal, type one line at it, record the nudge in the worktree's git dir → `{action: nudged, terminal, terminal_tail}`. Once per worker — a second call is refused; no attempt is spent, nothing is discarded (ADR-0018) | effect |
| `afk fail --issue <n> --instance <id> --worker-command <cmd> --reason <text>` | **the retry ladder**: read the attempt off the issue, then `retry` (swap the `afk-attempt/*` label, discard the failed attempt's PR + branch + worktree, start a fresh worker handed `--reason`) or `escalate` (as below) → `{action, attempt, …}`. After an unanswered nudge the worker's last screen is appended to the reason | effect + pure verdict |
| `afk escalate --issue <n> --instance <id> --reason <text>` | **hand a claim to a human**, in one order: status board → relabel (`escalate_label` on; `ready_label` and attempt labels off) → comment → release the claim last | effect |
| `afk close --issue <n> --instance <id>` | **close an issue that needed no change** (after the tick verified the empty diff): status board → close → release → worktree cleanup | effect |
| `afk status <n> --phase <phase> [--instance <id>] [--pr <pr>] [--attempt <k>]` | upsert the human-facing progress **status board** comment at a non-terminal phase, idempotently. `<phase>` is a `mine` row's `board_phase`; `<k>` is the row's `attempt`; `retry_max` and the gate's name come from config. (The terminal phases are written by `merge` / `escalate` / `close`.) | pure render + effect |
| **Claim refs** | | |
| `afk reclaim <n> --instance <id> --expect-sha <sha>` | `--force-with-lease` take of a stale claim → `{won}` | effect |
| `afk takeover --list --instance <my id>` / `--from <dead id> --instance <my id>` | the fleet instances GitHub remembers (claim markers + heartbeat refs) with heartbeat age / host / claim count — or force-take a dead one's claims: same atomic push as `reclaim`, staleness gate skipped, a fresh-heartbeat target held back until `--yes` (ADR-0011). `--instance` is always **my** id | effect + pure verdict |
| `afk release <n>` | delete a claim ref → `{released: true}` (idempotent: already gone counts). A delete that left the claim on the remote is an error. For the cases no transition covers: an orphan-release, a `closed` row, the drain | effect |
| `afk heartbeat --instance <id>` | refresh my heartbeat if due → `{refreshed}` | effect |
| `afk claim <n> --instance <id>` | low-level: atomic create-or-lose the claim ref → `{won}` (`won: false` names the `owner`; a push that failed for any other reason is an error). A tick never calls it — `dispatch` does | effect |
| `afk scan` | debug only: every claim + heartbeat ref as read from the remote (an unreadable remote is an error, not an empty list). A tick never calls it — `rebuild` does | effect |
| `afk classify-claims --instance <id>` | debug only: the mine / peer_live / stale partition on its own. A tick never calls it — `rebuild` does | effect gather + pure verdict |

Judgment stays with the tick and is **not** a tool: is the implementation correct (the gate),
adversarial verify, whether a sync conflict is mechanical enough to resolve rather than hand back, the orphan-vs-alive read of a liveness probe, whether a
recovered worktree is sane to build on, merging a PR that has no checks, the reason a failure or an
escalation is given, the human authorization. Each of those is exactly where a transition stops and
returns an `outcome`, or the one argument (`--reason`, `--verified`, `--start fresh`,
`--allow-no-checks`) it takes from you.
