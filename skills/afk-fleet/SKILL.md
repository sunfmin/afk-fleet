---
name: afk-fleet
description: >-
  Run an unattended, standing fleet that works a GitHub-issue backlog on its own. Use when the
  user wants to autonomously / AFK implement a repo's ready issues, orchestrate a fleet of
  agents/workers against GitHub issues (e.g. with orca), "launch workers to do the issues", or keep
  picking up and merging ready issues until stopped. A thin launcher spawns a fresh disposable
  reconciliation tick each cycle; each tick dispatches worktree-isolated coding-agent workers per
  ready issue, gates each on CI + optional independent adversarial verification, auto-merges green
  PRs to main, and retries-then-escalates failures — so it runs for days with context bounded by
  construction. Reads per-repo config and requires an explicit push+auto-merge authorization before
  running; supports --plan dry-run, --tick single-pass, and --takeover to inherit and continue a
  dead fleet's claims when a run hard-stopped (quota) and you don't want to wait out its lease.
  NOT for decomposing a PRD/epic into issues, implementing a single named issue by hand, or
  reviewing a PR.
---

# afk-fleet

An **unattended fleet** that implements a decomposed GitHub-issue backlog by itself, and keeps
running for days **without any session's context growing without bound**. Three roles, each
context-bounded:

| Role | What it is | Lifetime |
|---|---|---|
| **launcher** | The interactive session you invoke `/afk-fleet` in. It authorizes once, then loops: spawn a tick → ingest a one-line summary → pace → repeat. | Long-lived, but only accumulates ~one compact summary per tick (auto-compaction keeps it flat). |
| **tick** | A **fresh-context [Agent] subagent** that does exactly **one reconciliation pass** against GitHub, then returns a compact structured summary and dies. | Short. Its bulky context is discarded on return. |
| **worker** | A fire-and-forget autonomous coding agent (Claude Code or qoderclicn — the run's **runtime**), one per issue: orca creates its worktree + branch, then starts it with the run's **worker launch command** so it runs on the same runtime as the launcher. Communicates only through GitHub (its PR, and issue comments). | Independent of the coordinator — never read by it. |

**This skill only *consumes* a backlog.** It does not decompose a PRD/epic into issues — that is
upstream work, and epics are explicitly excluded from dispatch. Assume the issues already exist,
worker-sized, labelled, and dependency-ordered.

## Why it runs forever (bounded by construction)

Permanent runtime is achieved by a **succession of disposable ticks**, not by one long-lived
coordinator staying disciplined. No coordinator context is ever alive long enough to fill up:

- Each **tick is a fresh subagent context**, dropped on return — the reset is structural, not a
  hoped-for compaction.
- The **launcher** does no coordination itself; it only ingests a compact per-tick summary, so its
  own growth is a tiny constant per cycle (and auto-compaction is the safety net).
- **A cycle whose observable state is unchanged spawns no tick at all** — the launcher's
  `afk fingerprint` gate (pure code, zero LLM tokens) proves the no-op before any LLM context is
  created, and a forced full tick every `force_tick_after_skips` cycles backstops what a state hash
  can't see (ADR-0007). Idle days cost tool calls, not contexts.
- **All durable state lives in GitHub**, so any fresh tick reconstructs the exact working set:
  `afk-claim/<n>` ref = claim (owned by a **fleet instance**) · PR (`Closes #n`) = result · an
  `afk:verdict` marker comment = a worker's machine-readable reason for opening **no** PR
  (already-satisfied / blocked / giving-up) · `afk-attempt/<n>` label = retry count ·
  `afk-heartbeat/<id>` ref = owner liveness. Nothing is remembered between ticks. (The human-facing **status board** comment is a
  *derived projection* of this state onto the issue surface, re-rendered each tick — never itself a
  source of truth, and never read back by a tick.)

## Modes

- `/afk-fleet` — **launcher** (default): bootstrap, then loop spawning ticks. The main entry.
- `/afk-fleet --plan` — **dry-run**: a **tick short-circuited before the Act phase**. It does the full
  rebuild (frontier + in-flight + stale classification), prints the dispatch plan, and exits —
  merges/dispatches/reclaims **nothing**, needs no authorization. Same rebuild code path as `--tick`,
  so the plan can't drift from what a live tick would do (ADR-0002).
- `/afk-fleet --tick` — **one reconciliation pass** and exit with a summary. This is what the
  launcher spawns each cycle (and what you'd run headless). It auto-merges only under the run
  authorization its launcher injects; invoked cold without it, it dispatches + gates but holds
  merges.
- `/afk-fleet --takeover` — a **launcher bootstrap variant** for when a fleet hard-stopped (quota) and
  you will not wait ~75 min for its lease to lapse: the *full* bootstrap, then the opening working set
  is seeded from a dead peer's claims instead of the frontier alone. Thereafter an ordinary standing
  fleet. See [Takeover mode](#takeover-mode---takeover).

## Launcher (default mode)

### Bootstrap (once, with the human present)

1. **Load config** — `afk config --file <target repo>/docs/agents/afk-fleet.md` parses + validates
   the file against the one schema (unknown key or wrong shape → **error, with you present — fix the
   file, don't guess**) and returns the **canonical config JSON**: every key present, defaults
   filled (ADR-0009). That JSON is what the launcher holds and injects into every tick — nothing
   downstream re-parses YAML or re-applies defaults. Missing file → offer to create it from the
   template ([references/config-template.md](references/config-template.md)) and stop; never run on
   guessed settings.
2. **Establish this fleet instance** — mint a short unique **instance id** (this launcher run's
   identity, held only in the launcher and injected into every tick, exactly like the authorization
   below). Then `afk probe --repo <repo> --config '<config>'`, which answers two compatibility questions:
   - **Claim namespace** — if it reports `blocked` (an org ruleset forbids `refs/afk/*`), pass its
     fallback `--ns refs/heads` to every later `afk` call and **warn** that claim refs are then ordinary
     branches that may trigger `on: push` CI. See [Cooperative multi-fleet](#cooperative-multi-fleet).
   - **Branch protection** (only when `gate.ci: local`) — `protection.verdict == "error"` means
     `merge.target` **requires status checks**, so `gh pr merge` would be rejected however green the
     local gate is: **stop here, with the human present** — drop the required checks on that branch or
     switch to `gate.ci: required`. (`gh pr merge --admin` is not an option: it bypasses human review
     too.) A `"warn"` verdict (the read was inconclusive — no admin rights) is reported and continues.
3. **Settle the worker launch command** — `afk worker-command`. The tool first detects the
   **runtime** (ADR-0014): `QODERCN_CLI=1` in the environment → `qoderclicn` (always stock — no
   custom provider, no wrapping — returns its default and never asks); otherwise → `claude`, and
   the existing provider-parity flow applies. Workers start in a *fresh login shell*
   that inherits none of this session's environment, so a Claude launcher running on a custom provider
   (`ckimi`, `csk`, a direnv, a wrapper script) would otherwise dispatch workers that silently fall
   back to stock Anthropic and stay there for days (ADR-0010). For the Claude runtime, the tool reports:
   - `"status": "stock"` — no `ANTHROPIC_BASE_URL`; take its `command` and **ask nothing**.
   - `"status": "ask"` — a custom provider. Show the `base_url` and the `candidates` it found (the
     login shell's Claude-starting aliases, `wraps_env: true` marking the ones that carry a provider),
     ask *"which command should workers start with?"*, then **verify the answer**:
     `afk worker-command --check "<their answer>"`. `unresolved` → say what didn't resolve and re-ask
     (an unresolvable command starts no worker at all: the claim goes PR-less into the retry ladder
     and escalates, on a typo). `confirmed` with `"yolo": false` → warn that it carries no unattended
     flag, so a worker will park on a permission prompt — indistinguishable to the fleet from one that
     finished. `"yolo": null` just means the resolution couldn't show it.

   Hold the resulting `command` **verbatim** and inject it into every tick. It is **opaque** — never
   parse it, never compose one yourself, never append flags to it (appending to an alias that expands
   to a subshell isn't even valid syntax). This is what keeps every credential inside the wrapper the
   human already trusts: the fleet copies no environment, writes no file, and puts no key on any
   command line.
4. **Preview** — spawn a **plan tick** (a `--tick` in plan mode) as an [Agent] subagent and show the
   dispatch plan it returns (which issues, order, concurrency, gate steps, merge target). The frontier
   is computed **inside the subagent, never in the launcher's own context**; the launcher only ingests
   the returned plan (ADR-0002).
5. **Authorize (the one gate)** — state plainly: *"I will push worker branches and **auto-merge**
   green PRs to `<target>` in `<repo>` unattended — this overrides the standing 'never push without
   asking' rule, for this repo, for this run. Confirm?"* Get an explicit yes. This authorization is
   **for the whole run**, held only in the launcher (never a config key); every tick inherits it via
   its spawn prompt, and it dies when you stop the launcher.

The instance id, the run authorization, and the worker launch command are the run's **three
launcher-held facts**: settled once with you present, carried in every tick's spawn prompt, never
written to a file, gone when the launcher stops.

### Takeover mode (`--takeover`)

For when a fleet **hard-stopped** — its provider quota ran out, its process was killed — and you are
standing right there. The lease will hand its claims to a peer, but only after `claim_lease_ttl`
(~75 min), because a heartbeat is the only *machine-visible* line between "dead" and "alive but slow".
The present human is the oracle that knows *now*; the dying fleet cannot help, since a hard stop runs no
code at all (no drain, no release) — [ADR-0011](../../docs/adr/0011-takeover-and-progress-preservation.md).

Run the **full** [Bootstrap](#bootstrap-once-with-the-human-present) above — config, a *new* instance id,
the worker launch command, the one push+auto-merge authorization — so this is a real fleet instance. Only
the opening working set differs:

1. **List what GitHub still remembers.** The dead launcher forgot its own id; the claim markers
   (`instance=<id> host=<host>`) and heartbeat refs did not:
   ```bash
   python3 <skill>/scripts/afk.py takeover --list --repo <repo> --as <my instance id> --config '<config json>'
   ```
   Show the human each instance's id, host, claim count and heartbeat age, and ask which to take. Rows
   are flagged so the wrong answer is visible: `fresh: true` (looks alive), `claim_count: 0` (drained
   cleanly — nothing to take), `is_me: true` (this run).
2. **Force-take the selection:**
   ```bash
   python3 <skill>/scripts/afk.py takeover --instance <dead id> --as <my instance id> --repo <repo> --config '<config json>'
   ```
   The *same* atomic `--force-with-lease` push a stale reclaim uses, only skipping the staleness gate —
   so a fleet that is not actually dead still wins the race and the result reports it under `lost`.
   (Careful: here `--instance` is the instance taken **from**; yours is `--as`.) On
   `"action": "confirm"` — the target's heartbeat is still fresh — relay the warning **verbatim**, get an
   explicit yes, then re-run with `--yes`; on `"error"`/`"none"`, show it and continue as an ordinary
   launcher run.
3. **Then it is an ordinary standing fleet.** Enter the [Loop](#loop) unchanged: the first tick sees the
   taken claims as `mine` and recovers each by
   [continuation](#recovery-by-continuation-a-dead-claim-is-continued-never-restarted) — tier 1 when the
   dead fleet ran on *this* box, since its worktrees are still here — **and** works the frontier up to
   `concurrency`, until you stop it.

A takeover **is not a retry** (it never reads or increments `afk-attempt/<n>`) and **does not shorten the
lease** — the unattended safety net stays exactly as wide; this is only the human-gated fast path across
it. Because continuation makes each take start further along, repeatedly taking one claim converges
rather than loops.

### Loop

Repeat until you stop it:

1. **Gate the cycle** (if `fingerprint_gate`) — run `afk fingerprint --repo <repo> --last
   <last_fingerprint> --skips <skips> --config <config>`. It gathers what a
   tick's Rebuild would observe (issues+labels, PRs+checks, claim refs) **inside the tool** — the
   raw JSON never enters the launcher — and returns only `{fingerprint, action, reason, skips}`.
   On `"action": "skip"`: spawn nothing this cycle; if the last summary shows `in_flight > 0`,
   refresh the lease directly with `afk heartbeat --instance <id> --config <config>` (the one
   coordination-adjacent call the launcher makes itself, precisely so a skipped cycle can never
   lapse a lease), then go to **Pace**. On `"action": "tick"` (changed / forced / first), continue.
2. **Spawn a tick** — call the [Agent] tool (fresh context) to run one reconciliation pass, passing
   only `{repo, config, authorized: true, instance_id, worker_command}`. Constrain its return with a schema:
   `{merged:[…], escalated:[…], dispatched:[…], reclaimed:[…], in_flight:N, frontier_remaining:N, note}`.
3. **Ingest the summary** — keep that one line; discard everything else. Surface a short progress
   line to the user. The launcher's whole inter-cycle state is three small values: the last summary,
   the last `fingerprint`, and the `skips` streak.
4. **Pace adaptively** (`ScheduleWakeup`): if the last tick merged/dispatched/reclaimed anything or has
   in-flight PRs pending, wake again in `busy_interval` (~1–2 min) so green PRs merge promptly; if
   idle, `idle_interval` (~25 min). After `idle_ticks_before_sleep` consecutive empty ticks (frontier
   empty **and** no in-flight), drop to the long idle cadence. **While the fleet holds any claim
   (`in_flight > 0`), never sleep past `claim_lease_ttl`/2** — the heartbeat (refreshed inside the
   tick) must not lapse, or a peer will reclaim live work. `afk pace --summary <last summary> --config
   <config>` encodes exactly these rules (including the `ttl/2` cap) and returns the seconds.
   A skipped cycle paces off the **previous** summary — the cadence question ("busy or idle?") is
   unchanged by a cycle that proved nothing moved.
5. **Stop** on the user's word: run one final **drain** tick that `afk release <n>`s claims with no PR
   yet and retains those with an open PR (see [Cooperative multi-fleet](#cooperative-multi-fleet)), then
   spawn no more ticks. In-flight workers finish on their own; their PRs are inherited and merged by a
   peer (or a later run) once the lease expires; escalated issues stay labelled for the human.

The launcher never dispatches, merges, or reads a worker itself, never computes the frontier in its own
context, and never reads the tick's files (the `afk.py`/`afk_decide.py` source, `worker-prompt.md`) — it
reads only the repo config, calls `afk` subcommands, and spawns ticks. All coordination happens inside a
tick; even the bootstrap preview is a plan-tick subagent. This keeps the launcher thin *by construction*
(ADR-0002), not by later compaction.

## Tools (`scripts/afk.py`) — the deterministic muscle

Every **deterministic** step below is a subcommand of `afk.py`; the tick (an LLM) orchestrates and
judges, but calls the tool for the fixed mechanics rather than re-deriving git/gh incantations from
prose each pass (ADR-0004). Each prints one JSON object. Pure verdicts live in `afk_decide.py`
(fixture-tested); effectful ops drive git refs / gh.

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

## A tick (`--tick`) — one reconciliation pass

A tick is stateless: it rebuilds from GitHub, acts, summarizes, and exits. It never waits for the
workers it dispatches. In `--plan` mode it stops after step 1 (**Rebuild**) and returns the plan
instead of acting — same rebuild, zero side effects (this is what the launcher's bootstrap preview
spawns).

1. **Rebuild the working set from GitHub** (never from memory) — **one read-only call** (ADR-0008):
   ```bash
   python3 <skill>/scripts/afk.py rebuild --repo <repo> --instance <id> --config '<config json>'
   ```
   It gathers issues + PRs + claim/heartbeat refs once (the same gatherer the launcher's fingerprint
   gate reads through — the raw 200-issue JSON lives and dies inside the tool) and returns the whole
   working set: `{frontier: {dispatch, excluded}, mine: [{number, status, pr, checks,
   attempt_labels}…], peer_live, stale: [{number, sha}…], fingerprint, now}`. Then act on it:
   - **Frontier** — `frontier.dispatch` is the dispatchable set (`open` + `ready_label` + no
     `epic_labels` + **unclaimed** + **no open linked PR** + zero open `blocked_by`) — the published
     contract; `--plan` and live agree because both are this one code path.
   - **In-flight** — each of **`mine`** arrives subclassified: *awaiting_merge* → merge;
     *awaiting_ci* → leave; *failure* → failure handling; *no_pr* → see below. (In `gate.ci: local`
     only *awaiting_merge* and *no_pr* occur — no checks are read, and the gate runs inside the merge
     sequence instead; ADR-0012.) For *no_pr*, **disambiguate finished-from-coding
     with three signals, never terminal chrome alone.** A worker that ran to completion, concluded there
     was no PR to open, posted its reason, and went idle looks *identical* to one still coding — both
     are "a connected terminal with a title" — so a binary liveness probe parks the claim forever.
     `rebuild` stays git+gh-only and machine-independent (ADR-0008), so the tick gathers these per
     `no_pr` claim itself (the narrow set only): **(a)** git **progress** — `afk worker-status
     --worktree <path> --base <base_branch>` (the worktree path is orca's — from `orca worktree list`
     or the create result); **(b)** the worker's declared reason — `afk verdict --repo <repo> --issue
     <n>`; **(c)** the orca **liveness** probe (bounded, never a transcript read) for terminal
     busy / idle / none. Feed all three to `afk classify-no-pr --terminal <busy|idle|none> --idle-seconds
     <s> --progress <…> --verdict <…> [--blocked-by-open] --config <config>`, which returns one of five
     `{outcome, action}`:
       - **coding** (terminal busy, OR activity within `worker_idle_grace_seconds` — `idle_seconds`
         is the max-recency of `last_commit_ts` / `worktree_mtime_ts` / terminal activity, so recent
         commits count here) → still implementing, **leave it**. Note what is **not** in this list:
         `commits_ahead>0`/`dirty`. Those are **standing** facts, not signs of life — they stay true
         until the branch merges — and including them made every idle+verdict outcome unreachable for
         any worker that had ever committed, holding its claim forever (ADR-0013);
       - **idle_done** (idle past grace + verdict `already-satisfied` + **no** changes on the branch)
         → **verify the empty diff vs base**, then close the issue and `afk release <n>`. Changes on
         the branch refute an `already-satisfied` claim, so that combination routes to `idle_failed`
         instead of closing the issue;
       - **idle_blocked** (verdict `blocked`) → re-check each `blocked_by` issue: all now closed/merged →
         **re-dispatch** (keep the claim; not a retry); any still open → **escalate the DAG gap** (add
         `escalate_label`, comment the unmet dependency — pass `--blocked-by-open`);
       - **idle_failed** (verdict `giving-up`, OR **no verdict at all** after grace) → **failure
         handling** (`afk next-attempt`: retry → escalate);
       - **dead** (no live worker/terminal at all) → **orphaned claim**: recover it by
         **continuation** — `afk recovery --issue <n>` returns tier 1/2/3 and only tier 3 tears the
         worktree down (see [Recovery by continuation](#recovery-by-continuation-a-dead-claim-is-continued-never-restarted)).
         Keep the claim; or `afk release <n>` if the issue should go back to the frontier instead.
     The liveness probe, the empty-diff verification, and the orphan-vs-alive read stay judgment —
     deliberately not inside `rebuild`.
   - **Stale peer claims** — **`stale`** (a peer owns it and its `afk-heartbeat/<id>` is expired past
     `claim_lease_ttl`) is the only foreign claim I may take *unattended*: `afk reclaim <n> --instance
     <id> --expect-sha <the sha rebuild reported>` (atomic — fails if it moved), then treat as my own
     in-flight and recover it by **continuation** — a reclaimed claim's worker is dead by definition, so
     it goes straight through the tiers (tier 1 applies when the dead peer ran on *this* box).
     **`peer_live`** is left strictly alone. The human-gated, lease-skipping sibling of this reclaim is
     [`--takeover`](#takeover-mode---takeover).
2. **Act**, in this order:
   - **Merge** every green in-flight PR (serialized — see below). `afk release <n>` on each merged issue.
   - **Escalate** any retry-exhausted issue (see failure handling).
   - **Dispatch** to fill free slots up to `concurrency`: `afk claim <n> --instance <id>` — if it
     returns `{"won": false}`, a peer won the race, so skip it. On `{"won": true}`, hand the worktree to
     **orca** — it owns worktree + branch + spawn in one step; the tick never runs raw `git worktree`
     ([ADR-0005](../../docs/adr/0005-orca-owns-the-worktree.md)):
     ```bash
     git fetch origin <base_branch> --quiet     # the worker must start from the latest base
     orca worktree create --repo id:<repo-id> --name issue-<n>-<slug> --no-parent \
          --base-branch <base_branch> --issue <n> --json          # NO --agent
     orca terminal create --worktree issue:<n> --command "<worker_command>" --json
     ```
     `--agent` is deliberately **not** used: the worker must start with the run's **worker launch
     command** so it runs on the same runtime as this fleet (ADR-0010, ADR-0014). Pass that string **verbatim**
     — it is opaque; never rebuild it, never append flags. (Cost of dropping `--agent`: orca's
     unattended-flag default goes with it, which is why bootstrap warns on a command with no such flag.)

     Then read the create result for the **actual branch** (orca prefixes `<user>/…`) and the worktree
     path, fill [references/worker-prompt.md](references/worker-prompt.md) with that real branch + path,
     wait for the agent on the handle `terminal create` returned (`orca terminal wait --for tui-idle`),
     and deliver the prompt (`orca terminal send`). Do **not** wait for the worker. (`--name` comes from
     `branch_pattern` — a name hint only; orca sets the branch.)
   - **Heartbeat** — `afk heartbeat --instance <id> --config <config>`; it refreshes only if due
     and only matters while I hold ≥1 claim. Cheap, stateless (it reads the old ts from the ref itself).
   - **Render progress** (if `progress_comment`) — for each of my claims, upsert the human-facing
     **status board** so a person reading the issue sees how far along it is (esp. the otherwise-invisible
     "claimed, coding, no PR yet" phase — the claim lives in the hidden `refs/afk/*` and the assignee is
     unused). `afk status <n> --repo <repo> --state <json>` renders a progress checklist and writes the
     one marker-tagged comment **only when it changed** (idempotent — re-entrant ticks and retries never
     spam). The `phase` is *derived from state this pass already computed*, never a new fact:
     `no_pr` + live worker → `claimed`; `awaiting_ci` → `pr_open`; `failure` heading to retry →
     `ci_failed` (pass `attempt`/`retry_max`); `awaiting_merge` → `awaiting_merge`. The two **terminal**
     phases are upserted **before the claim is released**: `merged` in the merge sequence, `escalated` in
     the escalate step. The board is human-read only — no tick ever parses it back (ADR-0006).
3. **Return** the compact summary and **exit**. Freshly-dispatched workers' PRs are picked up by a
   later tick.

## Cooperative multi-fleet

Several fleet instances — on several machines, even under one shared GitHub account — may work the
same repo at once. The assignee can't arbitrate them (under a shared account it can't say *who* owns
an issue), so ownership lives in atomic **git refs** under the hidden `refs/afk/*` namespace and
liveness in a **per-instance lease**. See
[ADR-0003](../../docs/adr/0003-cooperative-multi-fleet-claims.md).

All of the mechanics below are `afk.py` subcommands (see [Tools](#tools-scriptsafkpy--the-deterministic-muscle)); the raw git
each one runs is shown so the mechanism is legible, but the tick calls the tool.

- **Instance id** — minted once per launcher run at bootstrap, injected into every tick. It stamps
  every claim this fleet makes (`--instance <id>`) and names this fleet's heartbeat.
- **Claim → `afk claim <n> --instance <id>`.** Internally it creates `afk-claim/<n>` pointing at a
  marker commit carrying `instance=<id> host=<host>`; the ref name is the issue number *only*. Creating
  a ref that already exists is **rejected by the server** — that rejection *is* the compare-and-swap.
  `{"won": true}` → proceed; `{"won": false}` (with the current `owner`) → a peer has it, skip. The
  claim ref is immutable after creation.
  ```bash
  sha=$(git commit-tree $(git hash-object -t tree /dev/null) -m "afk-claim instance=$ID host=$(hostname)")
  git push origin "$sha:refs/afk/claim/$n"    # nonzero exit ⇒ lost the race, back off
  ```
- **Owner check → rides in `afk rebuild`.** The ref scan reads every `afk-claim/*` marker, and the
  working set arrives already partitioned into `mine` / `peer_live` / `stale` (in-flight = `mine`).
  (`afk scan` / `afk classify-claims` remain as standalone debug surfaces over the same core.)
- **Heartbeat (the lease) → `afk heartbeat --instance <id> --ttl <s>`.** One ref `afk-heartbeat/<id>`
  carries a timestamp; the tool refreshes it **only if due** (`now - ts > ttl/3`) by force-pushing a
  new marker (it reads the old ts itself, so this stays stateless). **Per instance, not per claim**
  (claim refs never churn); a fleet holding no claims never beats.
- **Reclaim a stale peer claim → `afk reclaim <n> --instance <id> --expect-sha <sha>`.** Only the
  `stale` list is reclaimable. The takeover is atomic (two reclaimers can't both win):
  ```bash
  git push origin --force-with-lease="refs/afk/claim/$n:$sha_i_read" "$my_sha:refs/afk/claim/$n"
  ```
  A peer with a *fresh* heartbeat is left strictly alone — it reconciles its own dead workers locally.
  A reclaimed claim is then recovered by **continuation**, not restarted (ADR-0011). The
  lease-skipping, human-authorized sibling is [`--takeover`](#takeover-mode---takeover).
- **Release / cleanup → `afk release <n>`** (idempotent) on **merge**, **escalate**, and
  **orphan-release**. On **graceful stop**, the drain tick releases claims with **no PR yet** and
  **retains** those with an open PR (a peer inherits it once the lease expires — merging it if it is
  finished, **continuing** it if it is not).
  The **open-PR guard** — an issue with an open linked PR is never in the frontier — is what makes
  releasing safe: a still-finishing orphan's PR is never re-dispatched, and a human's PR is left alone.
  A skipped delete is a **phantom lock** that silently starves an issue.
- **Namespace fallback** — if bootstrap's probe shows an org ruleset forbids `refs/afk/*`, fall back to
  `refs/heads/afk-claim/*` + `refs/heads/afk-heartbeat/*` and warn that `on: push` CI fires on claim
  churn.

## Recovery by continuation (a dead claim is continued, never restarted)

A claim whose worker died — an **orphaned claim** of mine, a **stale claim** reclaimed from a dead
peer, or one inherited through a **takeover** — is recovered *from its durable progress*, never
re-dispatched from base while progress exists. Workers push after every completed step (see
[worker-prompt](references/worker-prompt.md)), so that progress is real and reachable: the local
worktree if it is still on this machine, else the branch tip on GitHub ([ADR-0011](../../docs/adr/0011-takeover-and-progress-preservation.md)).

One call decides which, per dead claim:

```bash
python3 <skill>/scripts/afk.py recovery --issue <n> --repo <repo> --config '<config json>'
```

It asks `orca worktree list` whether a worktree for this issue is still here, recognises the issue's
branch on the remote from `branch_pattern` (the claim ref records the issue, not the branch), compares
it against `base_branch`, and returns `{tier, action, prompt, worktree, branch, reason}`. It is
deliberately a **separate call, not part of `rebuild`**: `rebuild` is the one machine-independent
observation the launcher's fingerprint gate shares (ADR-0008), while this asks *this machine* what it
still has — and only a dead claim ever needs asking.

| tier | action | what the tick does | prompt |
|---|---|---|---|
| **1** | `reuse_worktree` | The worktree is still on this machine: **do not `orca worktree rm` it.** Start a new worker *inside it*, on the same branch — `orca terminal create --worktree issue:<n> --command "<worker_command>"`. Lossless: even uncommitted work survives. | continue |
| **2** | `recreate_at_tip` | No local worktree, but the branch is ahead of base: recreate one at the **branch tip** (`orca worktree create … --base-branch <that branch>`) and continue there. Loss is bounded to "since the last push". | continue |
| **3** | `dispatch_fresh` | Nothing survived: today's behaviour — tear down any leftover (`orca worktree rm --worktree issue:<n> --force`) and dispatch from base. **The only tier that tears anything down.** | fresh |

`prompt` names the [worker-prompt](references/worker-prompt.md) variant to deliver: its
**continue-mode variant** (inspect the existing progress first, treat it as partial work toward the
*same* acceptance criteria) or the fresh one. A surviving worktree with provably nothing in it gets the
fresh prompt — tier 1 is about never destroying a worktree, not about pretending there is progress.

**Keep the claim** throughout (you already own the issue), and note that the `afk-attempt/<n>` counter
is neither read nor incremented: continuation answers *"did the **worker** die?"*, the retry ladder
answers *"is this **work** failing?"* — different axes (ADR-0011). Because progress accumulates across
continuations, a claim recovered repeatedly *converges* instead of looping.

**Judgment stays with the tick.** The tier selection is mechanics; whether the recovered state is sane
to build on is your read, exactly like orphan-vs-alive. If it plainly is not (a wrecked tree, a branch
carrying a wrong approach), fall back to tier 3 by hand.

**This is not the retry path.** A red gate / adversarial refute / `giving-up` verdict still tears the
worktree down and re-dispatches *fresh* with the failure reason (see
[Failure handling](#failure-handling--bounded-retry--escalate-never-silently-drop)) — there the previous
attempt is precisely the thing that failed, so starting from base is deliberate.

## Completion gate

A PR may merge only when **all** configured gates are green. Which **machine gate** applies is
`gate.ci` ([ADR-0012](../../docs/adr/0012-local-completion-gate.md)):

- **`required` (default) — the CI machine gate.** Wait for the PR's GitHub checks. Read the checks
  (and, on red, the failing-log excerpt) in an **ephemeral sub-read** that returns only
  `{status: green|red, reason}`; raw logs never enter the tick. Progressive: before CI exists, the gate
  is the issue's acceptance criteria + whatever local build/test exists.
- **`local` — `gate.local_command` *is* the completion gate.** GitHub checks are **never read** in this
  mode (`rebuild` reports every open PR as `awaiting_merge`: gating is an **action taken at merge
  time**, not an observation waited on). It runs twice in a PR's life — the **worker** runs it after its
  pre-PR sync, and the **tick re-runs it at merge time** in the branch's worktree — because the worker's
  pass tested pre-sync code, and two PRs can each be locally green yet conflict semantically. The
  invariant both runs serve: *what lands on the target branch was tested in the form it lands.* One
  call, deliberately the same compact shape as the CI sub-read, so a raw log never enters the tick:
  ```bash
  python3 <skill>/scripts/afk.py gate-run --worktree <path> --config '<config json>'
  # → {status: green|red, exit_code, excerpt, omitted_lines, timed_out}
  ```
  A red run's `excerpt` is **posted as a PR comment** before the retry ladder, so the next attempt
  re-reads the failure from where it lives rather than from a dead tick's context. Adopting this mode is
  the repo's claim that its command is CI-equivalent, and it is expected to scope remote CI away from
  worker branches; bootstrap **hard-errors** when `merge.target` requires status checks (see
  [Bootstrap](#bootstrap-once-with-the-human-present) step 2).
- **Independent adversarial verification** (if `gate.adversarial_verify`) — a *separate* agent (not
  the author, doesn't see its reasoning) re-derives the result and tries to **refute** it (e.g.
  re-solve and assert `final == official answer:`, audit the derivation). Refute-first: any
  refutation blocks the merge, is **posted as a PR review comment** (durable, re-readable on retry),
  and feeds back as a retry reason.

## Merge (serialized)

Within a tick, merges are **strictly serialized** — one PR at a time — so parallel workers never
corrupt the target branch:

1. **Sync** the branch up to latest `merge.target` (`sync_before_merge`) — by **merging, never
   rebasing**:
   ```bash
   git -C <worktree> fetch origin <target>
   git -C <worktree> merge origin/<target>      # NOT rebase
   git -C <worktree> push origin HEAD
   ```
   One verb at both ends of a PR's life (the worker syncs the same way pre-PR). Rebase is **retired from
   the merge path**: it drops the merge commits the worker's own sync and checkpoints created, re-igniting
   the conflicts already resolved inside them — while squash-merge makes the target-branch history
   identical either way (ADR-0012). An unresolvable conflict → failure handling.
   **No worktree here?** (a `--takeover` from another machine, a stray cleanup.) Recreate one at the
   pushed branch tip — the [continuation](#recovery-by-continuation-a-dead-claim-is-continued-never-restarted)
   tier-2 move — and dispose of it after the merge.
2. **Re-confirm the gate after the sync**, against the exact tree that will land:
   - `gate.ci: required` → the PR's checks are green again;
   - `gate.ci: local` → `afk gate-run --worktree <path> --config '<config>'`. On red, post the `excerpt`
     as a PR comment and route to failure handling as **gate red** — the existing category, not a new one.
3. `gh pr merge <n> --squash --delete-branch` (per `merge.strategy`). The issue auto-closes via
   `Closes #<n>`.
   If `progress_comment`, upsert the terminal board now (`afk status <n> --repo <repo> --state
   '{"phase":"merged",…}'`) — **before** the release below, while the issue is still one of my claims.
4. **Delete the claim** — `afk release <n>` (a *different* ref from the work branch that
   `--delete-branch` removed). Then remove the worktree if `worktree_cleanup`
   (`orca worktree rm --worktree issue:<n> --force`, since orca owns it — ADR-0005) and free the slot.
   A skipped claim-ref delete is a phantom lock that silently starves the issue.

The fleet's mandate **ends at a green merge to `merge.target`.** Deploying is a separate,
human-gated step — never done here.

## Failure handling — bounded retry → escalate, never silently drop

Per issue, on any of {worker failed, gate red — the PR's CI checks *or* a red merge-time
`afk gate-run` — adversarial refute, unresolvable **sync** conflict, a `no_pr` claim classified
**idle_failed** — a `giving-up` verdict, or a worker gone idle with **no verdict at all** after grace}:

1. **Retry up to `retry` times** (default 2). The attempt count lives as an **`afk-attempt/<n>`
   label** on the issue (not in tick memory). `afk next-attempt --labels <the issue's labels>
   --config <config>` reads it and returns the verdict: on `{"action":"retry", "to_label":…}`, swap the label,
   tear down the worktree (`orca worktree rm --worktree issue:<n> --force`), and re-dispatch —
   **keeping the claim ref** (you still own the issue). The
   failure reason handed to the new worker is **re-read from where it already lives** — the PR's CI
   checks, the merge-time gate excerpt posted as a PR comment, the verifier's PR review comment, or the
   reproduced sync conflict — never carried in context.
2. **On `{"action":"escalate"}`:** (if `progress_comment`, upsert the terminal board
   `--state '{"phase":"escalated",…}'` **before** releasing, while it's still my claim), then `afk
   release <n>` (delete the claim), remove
   `ready_label` and any `afk-attempt/*` label, add `escalate_label` (`ready-for-human`), and (if
   `escalate_comment`) comment the stuck-point with PR + log links — the status board points a reader
   here. Then move on — never silently drop or silently merge bad work.

**`no_pr` idle routing (not all of it is a failure).** Only **idle_failed** enters the retry ladder
above. A **idle_blocked** claim (a `blocked` verdict) skips retry accounting entirely: if its
`blocked_by` issues have all resolved it is simply **re-dispatched** (a transient DAG-ordering miss,
not a failure); if any remain open it is **escalated as a DAG gap** — comment the unmet dependency and
add `escalate_label`, a decomposition error only a human can fix. An **idle_done** claim
(`already-satisfied`) is not a failure either: verify the empty diff vs base, close the issue, `afk
release <n>`.

## Concurrency

`concurrency` (default 3) bounds parallel workers. Semantic ordering is the backlog's dependency DAG
(your responsibility when decomposing); textual conflicts between parallel PRs are caught by the
serialized sync-before-merge and routed through failure handling. Early machinery issues that all
touch shared root config are naturally throttled by the DAG — chain them with `blocked_by`.

## Guardrails

- **Never** run the launcher without the bootstrap authorization; **never** let a cold `--tick`
  auto-merge without an injected run authorization.
- **Never** deploy, touch secrets, or push anywhere but worker branches + the merge to `merge.target`.
  In particular, **never carry a credential to a worker**: don't copy `ANTHROPIC_*` (or any env) into a
  spawn command, don't write an env file, don't read a token out of a secret manager. The **worker
  launch command** is opaque precisely so the wrapper the human named does that in the worker's own
  shell (ADR-0010). Copying the launcher's env would also break the fleet outright — `ORCA_TERMINAL_HANDLE`
  and friends would make every worker report status as the launcher's terminal, collapsing the liveness probe.
- **Never** dispatch an epic/PRD issue. If the frontier is all epics, report "nothing decomposed yet."
- **Never** read a worker's terminal for its result (only a bounded liveness probe) — and the probe
  **alone cannot tell finished-and-idle from still-coding**, so for a `no_pr` claim combine it with
  `afk worker-status` git progress and the `afk verdict` marker (see In-flight). Results are PRs; a
  not-going-to-PR outcome is the worker's `afk:verdict` marker comment; blockers are issue comments. A
  full transcript must never enter a tick or the launcher.
- **Claim before work** (create the `afk-claim/<n>` ref; if the create is rejected, a peer owns it —
  never proceed). **Release on every terminal transition** (merge, escalate, orphan-release) by
  deleting the ref — a leaked ref is a phantom lock. Reconcile only **your own** claims; take a peer's
  only when its heartbeat is expired (**stale claim**), never while it is fresh — the one exception
  being an explicit human [`--takeover`](#takeover-mode---takeover).
- **Never discard a dead worker's progress.** Recover a dead claim by **continuation** (`afk recovery`
  → tier 1/2), and tear a worktree down only when the tool says tier 3. An `orca worktree rm` on a
  worktree that still holds work is unrecoverable — nothing else in the fleet is.
- **Never `afk takeover --instance` on your own initiative.** It is the one operation that can take a
  claim from a fleet whose lease has *not* lapsed, so it runs only on a human's explicit selection from
  `--list`, and `--yes` only after relaying the fresh-heartbeat warning and getting an explicit yes.
  Unattended, the lease is the only path — never `--yes` to make a tick's life easier.
- **A human reserves an issue by removing `ready_label`**, not by assigning it — the fleet no longer
  reads the assignee. Keep the tracker honest so a peer fleet or a human never double-takes.
- Reserved: the fleet manages `afk-attempt/<n>` labels, **the `refs/afk/*` ref namespace**
  (`afk-claim/*`, `afk-heartbeat/*`), the single status-board comment tagged `<!--afk:status-->`, and
  the worker-authored `<!--afk:verdict …-->` marker comments (the fleet parses these) — don't
  hand-edit them or reuse those prefixes / those markers.
