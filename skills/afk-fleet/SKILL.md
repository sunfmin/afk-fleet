---
name: afk-fleet
description: >-
  Run an unattended, standing fleet that autonomously implements a repo's ready GitHub-issue backlog —
  dispatching worktree-isolated workers, gating each on CI, and auto-merging green PRs until stopped.
  Use when the user wants to go AFK on the issues ("launch workers to do the issues", "run the fleet",
  orchestrate agents against GitHub issues with orca), or asks for one of its modes: --plan (dry-run),
  --tick (single pass), --takeover (inherit a dead fleet's claims). Another skill can invoke it to
  staff an already-decomposed backlog. NOT for decomposing a PRD/epic into issues, implementing a
  single named issue by hand, or reviewing a PR.
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
     branches that may trigger `on: push` CI. See [Cooperative multi-fleet](references/cooperative-multi-fleet.md).
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
   [continuation](references/recovery.md) — tier 1 when the
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
   yet and retains those with an open PR (see [Cooperative multi-fleet](references/cooperative-multi-fleet.md)), then
   spawn no more ticks. In-flight workers finish on their own; their PRs are inherited and merged by a
   peer (or a later run) once the lease expires; escalated issues stay labelled for the human.

The launcher never dispatches, merges, or reads a worker itself, never computes the frontier in its own
context, and never reads the tick's files (the `afk.py`/`afk_decide.py` source, `worker-prompt.md`) — it
reads only the repo config, calls `afk` subcommands, and spawns ticks. All coordination happens inside a
tick; even the bootstrap preview is a plan-tick subagent. This keeps the launcher thin *by construction*
(ADR-0002), not by later compaction.

## Tools (`scripts/afk.py`) — the deterministic muscle

Every **deterministic** step the skill runs is a subcommand of `afk.py`, each printing one JSON object:
the tick orchestrates and judges, but calls the tool for the fixed mechanics rather than re-deriving
git/gh incantations from prose each pass (ADR-0004). The full interface table — every subcommand with
its arguments and return shape — is disclosed in
[references/tools.md](references/tools.md); read it when you need a signature not already shown inline
at its call site.

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
         worktree down (see [Recovery by continuation](references/recovery.md)).
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
     # Fast-forward the LOCAL base branch, not just origin/<base>. `orca --base-branch <base>`
     # resolves the **local** ref, and a plain `git fetch origin <base>` never moves it — so a
     # bare fetch silently dispatches the worker from a stale base (see the assertion below).
     git fetch origin <base_branch>:<base_branch> --quiet
     orca worktree create --repo id:<repo-id> --name issue-<n>-<slug> --no-parent \
          --base-branch <base_branch> --issue <n> --json          # NO --agent
     # The worker must start from the latest base — assert it, don't assume it.
     git -C <worktree> merge-base --is-ancestor origin/<base_branch> HEAD \
       || git -C <worktree> merge --ff-only origin/<base_branch>
     orca terminal create --worktree issue:<n> --command "<worker_command>" --json
     ```
     **Why the refspec and the assertion, not just a fetch.** `git fetch origin <base>` updates only
     `refs/remotes/origin/<base>`; the local `refs/heads/<base>` stays where it was. orca creates the
     worktree from the **local** branch, so right after a merge to the target the very next dispatch
     starts its worker one or more commits behind — the worker then does its whole pass against a stale
     tree and only discovers it at merge-time sync. The `<base>:<base>` refspec moves the local branch;
     the `merge-base --is-ancestor` line makes the guarantee **mechanism-independent** (it holds however
     orca resolves the ref, and self-heals if the refspec fetch was refused). If the refspec fetch fails
     with *"refusing to fetch into branch … checked out at …"*, that is the base branch being checked out
     in some worktree: leave it alone and let the assertion's `--ff-only` do the work.

     `--agent` is deliberately **not** used: the worker must start with the run's **worker launch
     command** so it runs on the same runtime as this fleet (ADR-0010, ADR-0014). Pass that string
     **verbatim** — the opaque command settled at bootstrap step 3. (Cost of dropping `--agent`: orca's
     unattended-flag default goes with it, which is why bootstrap warns on a command with no such flag.)

     Then read the create result for the **actual branch** (orca prefixes `<user>/…`) and the worktree
     path, fill [references/worker-prompt.md](references/worker-prompt.md) with that real branch + path,
     wait for the agent on the handle `terminal create` returned (`orca terminal wait --for tui-idle`),
     and deliver the prompt — **`--enter` is mandatory**:
     ```bash
     orca terminal send --terminal <handle> --text "<filled worker prompt>" --enter
     ```
     Without `--enter`, orca types the prompt into the worker TUI's input box but never submits it: the
     worker then sits idle forever with the prompt unsubmitted — indistinguishable, to the liveness probe,
     from one that finished. Do **not** wait for the worker after sending. (`--name` comes from
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
same repo at once: ownership lives in atomic git refs under the hidden `refs/afk/*` namespace,
liveness in a per-instance lease ([ADR-0003](../../docs/adr/0003-cooperative-multi-fleet-claims.md)).
The operative rules (claim before work, release on every terminal transition, reclaim only stale) are
in [Guardrails](#guardrails); the mechanism and the raw git each subcommand runs are disclosed in
[references/cooperative-multi-fleet.md](references/cooperative-multi-fleet.md) — read it when you need
to understand *how* a claim, heartbeat, or reclaim works under the hood. It also defines the
**phantom lock** failure a skipped claim-ref delete causes.

## Recovery by continuation (a dead claim is continued, never restarted)

A claim whose worker died — an **orphaned claim** of mine, a **stale claim** reclaimed from a dead
peer, or one inherited through a **takeover** — is recovered *from its durable progress*, never
re-dispatched from base while progress exists. On any dead claim, read
[references/recovery.md](references/recovery.md) **before acting**: one call —
`afk recovery --issue <n> --repo <repo> --config '<config json>'` — returns the tier, and that file
carries the per-tier action table (tier 1 reuse the worktree / tier 2 recreate at the branch tip /
tier 3 dispatch fresh — **only tier 3 tears anything down**) and the continue-vs-fresh prompt choice.
**Keep the claim** throughout; the `afk-attempt/<n>` counter is neither read nor incremented
(continuation answers *"did the worker die?"*, the retry ladder *"is the work failing?"*). This is
**not the retry path** — a red gate / adversarial refute / `giving-up` verdict re-dispatches *fresh*
by design (see
[Failure handling](#failure-handling--bounded-retry--escalate-never-silently-drop)).

## Completion gate

A PR may merge only when **all** configured gates are green. Which **machine gate** applies is
`gate.ci` ([ADR-0012](../../docs/adr/0012-local-completion-gate.md)): `required` (default) waits for
the PR's GitHub checks; `local` makes `gate.local_command` the gate, re-run at merge time, and never
reads checks. The executable per-mode actions are in [Merge](#merge-serialized) step 2; the invariants
behind them — the local gate's two-run rule, the ephemeral CI sub-read, and the adversarial-verify
procedure when `gate.adversarial_verify` is on — are disclosed in
[references/completion-gate.md](references/completion-gate.md). Read it before running an adversarial
verify or switching a repo to `gate.ci: local`.

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
   pushed branch tip — the [continuation](references/recovery.md)
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
   A skipped claim-ref delete is a **phantom lock** (see [Cooperative multi-fleet](references/cooperative-multi-fleet.md)).

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

**`no_pr` idle routing (not all of it is a failure).** Of the five `no_pr` outcomes (defined in the
tick's In-flight list), only **idle_failed** enters the retry ladder above. **idle_blocked** skips
retry accounting entirely — re-dispatched when its `blocked_by` issues resolve, escalated as a DAG gap
when they don't — and **idle_done** closes the issue after an empty-diff check; neither is a failure.

## Concurrency

`concurrency` (default 3) bounds parallel workers. Semantic ordering is the backlog's dependency DAG
(your responsibility when decomposing); textual conflicts between parallel PRs are caught by the
serialized sync-before-merge and routed through failure handling. Early machinery issues that all
touch shared root config are naturally throttled by the DAG — chain them with `blocked_by`.

## Guardrails

- **Authorize before any push or merge.** The launcher runs only on the bootstrap authorization, and a
  cold `--tick` auto-merges only with an injected run authorization — without one it dispatches and
  gates but holds merges.
- **Keep every credential inside the worker's own shell.** Push only to worker branches and the merge
  to `merge.target`; deploying, secrets, and every other remote stay out of scope. Carry no credential
  to a worker — no copied `ANTHROPIC_*` (or any) env, no env file, no token from a secret manager: the
  opaque **worker launch command** exists so the wrapper the human named does this itself (ADR-0010).
  Copying the launcher's env would also break the fleet outright — `ORCA_TERMINAL_HANDLE` and friends
  would make every worker report as the launcher's terminal, collapsing the liveness probe.
- **Dispatch worker-sized issues only.** Epics/PRDs stay upstream; if the frontier is all epics, report
  "nothing decomposed yet."
- **Read workers through GitHub, never their transcripts.** A worker's result is its PR (`Closes #n`);
  a not-going-to-PR outcome is its `afk:verdict` marker comment; blockers are issue comments. Liveness
  is a bounded probe combined, for a `no_pr` claim, with `afk worker-status` git progress and the
  `afk verdict` marker (see In-flight — the probe alone is never a finished/coding verdict). A full
  transcript never enters a tick or the launcher.
- **Claim before work; release on every terminal transition.** Create the `afk-claim/<n>` ref first —
  if the create is rejected, a peer owns it, so stop. Delete the ref on merge, escalate, and
  orphan-release; a leaked ref is a phantom lock. Reconcile only your own claims, and take a peer's
  only when its heartbeat is expired (a **stale claim**) — the single exception is an explicit human
  [`--takeover`](#takeover-mode---takeover).
- **Preserve a dead worker's progress.** Recover a dead claim by **continuation** (`afk recovery` →
  tier 1/2), and tear a worktree down only when the tool says tier 3 — an `orca worktree rm` on a
  worktree that still holds work is the one unrecoverable act in the fleet.
- **Take a live lease only on a human's word.** `afk takeover --instance` runs only on the human's
  explicit selection from `--list`, with `--yes` only after relaying the fresh-heartbeat warning and
  getting an explicit yes; unattended, the lease is the only path (never `--yes` to ease a tick).
- **Respect human reservation.** A human reserves an issue by removing `ready_label` (the fleet no
  longer reads the assignee); keep the tracker honest so a peer fleet or a human never double-takes.
- **Stay off the reserved namespaces.** The fleet manages the `afk-attempt/<n>` labels, the
  `refs/afk/*` ref namespace (`afk-claim/*`, `afk-heartbeat/*`), the single status-board comment tagged
  `<!--afk:status-->`, and the worker-authored `<!--afk:verdict …-->` markers (which it parses) — leave
  them to the fleet, and reuse those prefixes / markers for nothing else.
