---
name: afk-fleet
description: >-
  Run an unattended, standing fleet that works a GitHub-issue backlog on its own. Use when the
  user wants to autonomously / AFK implement a repo's ready issues, orchestrate a fleet of
  agents/workers against GitHub issues (e.g. with orca), "launch workers to do the issues", or keep
  picking up and merging ready issues until stopped. A thin launcher spawns a fresh disposable
  reconciliation tick each cycle; each tick dispatches worktree-isolated Claude Code workers per
  ready issue, gates each on CI + optional independent adversarial verification, auto-merges green
  PRs to main, and retries-then-escalates failures — so it runs for days with context bounded by
  construction. Reads per-repo config and requires an explicit push+auto-merge authorization before
  running; supports --plan dry-run and --tick single-pass. NOT for decomposing a PRD/epic into
  issues, implementing a single named issue by hand, or reviewing a PR.
---

# afk-fleet

An **unattended fleet** that implements a decomposed GitHub-issue backlog by itself, and keeps
running for days **without any session's context growing without bound**. Three roles, each
context-bounded:

| Role | What it is | Lifetime |
|---|---|---|
| **launcher** | The interactive session you invoke `/afk-fleet` in. It authorizes once, then loops: spawn a tick → ingest a one-line summary → pace → repeat. | Long-lived, but only accumulates ~one compact summary per tick (auto-compaction keeps it flat). |
| **tick** | A **fresh-context [Agent] subagent** that does exactly **one reconciliation pass** against GitHub, then returns a compact structured summary and dies. | Short. Its bulky context is discarded on return. |
| **worker** | A fire-and-forget autonomous Claude Code spawned by orca — which creates its worktree, branch, and agent terminal in one step — one per issue. Communicates only through GitHub (its PR, and issue comments). | Independent of the coordinator — never read by it. |

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
   below). Probe the claim namespace with `afk probe`; if it reports `blocked` (an org ruleset forbids
   `refs/afk/*`), pass its fallback `--ns refs/heads` to every later `afk` call and **warn** that claim
   refs are then ordinary branches that may trigger `on: push` CI. See
   [Cooperative multi-fleet](#cooperative-multi-fleet).
3. **Preview** — spawn a **plan tick** (a `--tick` in plan mode) as an [Agent] subagent and show the
   dispatch plan it returns (which issues, order, concurrency, gate steps, merge target). The frontier
   is computed **inside the subagent, never in the launcher's own context**; the launcher only ingests
   the returned plan (ADR-0002).
4. **Authorize (the one gate)** — state plainly: *"I will push worker branches and **auto-merge**
   green PRs to `<target>` in `<repo>` unattended — this overrides the standing 'never push without
   asking' rule, for this repo, for this run. Confirm?"* Get an explicit yes. This authorization is
   **for the whole run**, held only in the launcher (never a config key); every tick inherits it via
   its spawn prompt, and it dies when you stop the launcher.

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
   only `{repo, config, authorized: true, instance_id}`. Constrain its return with a schema:
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
| `afk rebuild --repo <r> --instance <id> --config <json>` | **one read-only call → the whole working set**: frontier (dispatch+excluded), `mine` subclassified with PR/checks/attempt-labels, `peer_live`, `stale` (with the sha reclaim needs), fingerprint (ADR-0008) | effect gather + pure assembly |
| `afk worker-status --worktree <path> --base <branch>` | a `no_pr` worker's git **progress** in its worktree → `{commits_ahead, dirty, last_commit_ts, worktree_mtime_ts}` — the decisive coding-vs-finished signal, independent of terminal chrome (git only, no gh) | effect (git) |
| `afk verdict --repo <r> --issue <n>` | the LATEST parsed `afk:verdict` marker the worker left → `{found, phase, blocked_by, reason, comment_url}` — its machine-readable reason for opening no PR | effect gather + pure parse |
| `afk classify-no-pr --terminal <busy\|idle\|none> --progress <json> --verdict <json> --config <json>` | the **5-way `no_pr` verdict** from those signals → `{outcome, action}` (coding / idle_done / idle_blocked / idle_failed / dead) | pure |
| `afk claim <n> --instance <id>` | atomic create-or-lose the claim ref → `{won}` | effect |
| `afk reclaim <n> --instance <id> --expect-sha <sha>` | `--force-with-lease` takeover of a stale claim → `{won}` | effect |
| `afk release <n>` | delete a claim ref (idempotent) | effect |
| `afk heartbeat --instance <id> --config <json>` | refresh my heartbeat if due → `{refreshed}` | effect |
| `afk next-attempt --labels <csv> --config <json>` | retry-or-escalate from `afk-attempt/*` | pure |
| `afk pace --summary <json> --config <json>` | next launcher sleep, with the `ttl/2` cap | pure |
| `afk fingerprint --repo <r> --last <fp> --skips <k> --config <json>` | digest observable state → skip-or-tick for the launcher's cycle gate (same gatherer as `rebuild`) | effect gather + pure verdict |
| `afk status <n> --repo <r> --state <json>` | upsert the human-facing progress **status board** comment, idempotently | pure render + effect |

Every config-consuming subcommand takes the **same canonical `--config` JSON** the launcher got from
`afk config` — passed verbatim, never re-derived; explicit flags (`--ttl`, `--retry`, …) remain as
overrides for tests and hand-debugging. Resolution is one order everywhere:
flag → `--config` → the defaults table (ADR-0009).

(The verdicts `rebuild` absorbed — `frontier`, `scan`, `classify-claims`, `subclassify` — still exist
as undocumented debug surfaces over the same pure core; a tick never calls them.)

Judgment stays with the tick and is **not** a tool: is the implementation correct (the gate),
adversarial verify, resolving a rebase conflict, the orphan-vs-alive read of a liveness probe, wording
an escalation, the human authorization.

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
     *awaiting_ci* → leave; *failure* → failure handling; *no_pr* → **disambiguate finished-from-coding
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
       - **coding** (terminal busy, OR `commits_ahead>0`/dirty, OR activity within
         `worker_idle_grace_seconds`) → still implementing, **leave it**;
       - **idle_done** (idle + zero progress past grace + verdict `already-satisfied`) → **verify the
         empty diff vs base**, then close the issue and `afk release <n>`;
       - **idle_blocked** (verdict `blocked`) → re-check each `blocked_by` issue: all now closed/merged →
         **re-dispatch** (keep the claim; not a retry); any still open → **escalate the DAG gap** (add
         `escalate_label`, comment the unmet dependency — pass `--blocked-by-open`);
       - **idle_failed** (verdict `giving-up`, OR **no verdict at all** after grace) → **failure
         handling** (`afk next-attempt`: retry → escalate);
       - **dead** (no live worker/terminal at all) → **orphaned claim**: tear down any stale worktree
         (`orca worktree rm --worktree issue:<n> --force`) and re-dispatch, or `afk release <n>`.
     The liveness probe, the empty-diff verification, and the orphan-vs-alive read stay judgment —
     deliberately not inside `rebuild`.
   - **Stale peer claims** — **`stale`** (a peer owns it and its `afk-heartbeat/<id>` is expired past
     `claim_lease_ttl`) is the only foreign claim I may take: `afk reclaim <n> --instance <id>
     --expect-sha <the sha rebuild reported>` (atomic — fails if it moved), then treat as my own
     in-flight. **`peer_live`** is left strictly alone.
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
          --base-branch <base_branch> --issue <n> --agent claude --json
     ```
     Then read the create result for the **actual branch** (orca prefixes `<user>/…`) and the worktree
     path, fill [references/worker-prompt.md](references/worker-prompt.md) with that real branch + path,
     wait for the agent (`orca terminal wait --for tui-idle`), and deliver the prompt (`orca terminal
     send`). Do **not** wait for the worker. (`--name` comes from `branch_pattern` — a name hint only;
     orca sets the branch.)
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

All of the mechanics below are `afk.py` subcommands (see [Tools](#tools-scriptsafkpy)); the raw git
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
- **Release / cleanup → `afk release <n>`** (idempotent) on **merge**, **escalate**, and
  **orphan-release**. On **graceful stop**, the drain tick releases claims with **no PR yet** and
  **retains** those with an open PR (a peer inherits and merges the finished PR once the lease expires).
  The **open-PR guard** — an issue with an open linked PR is never in the frontier — is what makes
  releasing safe: a still-finishing orphan's PR is never re-dispatched, and a human's PR is left alone.
  A skipped delete is a **phantom lock** that silently starves an issue.
- **Namespace fallback** — if bootstrap's probe shows an org ruleset forbids `refs/afk/*`, fall back to
  `refs/heads/afk-claim/*` + `refs/heads/afk-heartbeat/*` and warn that `on: push` CI fires on claim
  churn.

## Completion gate

A PR may merge only when **all** configured gates are green:

- **CI machine gate** — wait for the PR's GitHub checks. Read the checks (and, on red, the failing-log
  excerpt) in an **ephemeral sub-read** that returns only `{status: green|red, reason}`; raw logs
  never enter the tick. Progressive: before CI exists, the gate is the issue's acceptance criteria +
  whatever local build/test exists.
- **Independent adversarial verification** (if `gate.adversarial_verify`) — a *separate* agent (not
  the author, doesn't see its reasoning) re-derives the result and tries to **refute** it (e.g.
  re-solve and assert `final == official answer:`, audit the derivation). Refute-first: any
  refutation blocks the merge, is **posted as a PR review comment** (durable, re-readable on retry),
  and feeds back as a retry reason.

## Merge (serialized)

Within a tick, merges are **strictly serialized** — one PR at a time — so parallel workers never
corrupt `main`:

1. Rebase the branch onto latest `merge.target` (`rebase_before_merge`). A conflict → failure handling.
2. Re-confirm the gate is still green after the rebase.
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

Per issue, on any of {worker failed, gate red, adversarial refute, unresolvable rebase conflict, a
`no_pr` claim classified **idle_failed** — a `giving-up` verdict, or a worker gone idle with **no
verdict at all** after grace}:

1. **Retry up to `retry` times** (default 2). The attempt count lives as an **`afk-attempt/<n>`
   label** on the issue (not in tick memory). `afk next-attempt --labels <the issue's labels>
   --config <config>` reads it and returns the verdict: on `{"action":"retry", "to_label":…}`, swap the label,
   tear down the worktree (`orca worktree rm --worktree issue:<n> --force`), and re-dispatch —
   **keeping the claim ref** (you still own the issue). The
   failure reason handed to the new worker is **re-read from where it already lives** — the PR's CI
   checks, the verifier's PR review comment, or the reproduced rebase conflict — never carried in context.
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
serialized rebase-before-merge and routed through failure handling. Early machinery issues that all
touch shared root config are naturally throttled by the DAG — chain them with `blocked_by`.

## Guardrails

- **Never** run the launcher without the bootstrap authorization; **never** let a cold `--tick`
  auto-merge without an injected run authorization.
- **Never** deploy, touch secrets, or push anywhere but worker branches + the merge to `merge.target`.
- **Never** dispatch an epic/PRD issue. If the frontier is all epics, report "nothing decomposed yet."
- **Never** read a worker's terminal for its result (only a bounded liveness probe) — and the probe
  **alone cannot tell finished-and-idle from still-coding**, so for a `no_pr` claim combine it with
  `afk worker-status` git progress and the `afk verdict` marker (see In-flight). Results are PRs; a
  not-going-to-PR outcome is the worker's `afk:verdict` marker comment; blockers are issue comments. A
  full transcript must never enter a tick or the launcher.
- **Claim before work** (create the `afk-claim/<n>` ref; if the create is rejected, a peer owns it —
  never proceed). **Release on every terminal transition** (merge, escalate, orphan-release) by
  deleting the ref — a leaked ref is a phantom lock. Reconcile only **your own** claims; take a peer's
  only when its heartbeat is expired (**stale claim**), never while it is fresh.
- **A human reserves an issue by removing `ready_label`**, not by assigning it — the fleet no longer
  reads the assignee. Keep the tracker honest so a peer fleet or a human never double-takes.
- Reserved: the fleet manages `afk-attempt/<n>` labels, **the `refs/afk/*` ref namespace**
  (`afk-claim/*`, `afk-heartbeat/*`), the single status-board comment tagged `<!--afk:status-->`, and
  the worker-authored `<!--afk:verdict …-->` marker comments (the fleet parses these) — don't
  hand-edit them or reuse those prefixes / those markers.
