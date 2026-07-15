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
| **worker** | A fire-and-forget autonomous Claude Code spawned by orca in an isolated git worktree, one per issue. Communicates only through GitHub (its PR, and issue comments). | Independent of the coordinator — never read by it. |

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
- **All durable state lives in GitHub**, so any fresh tick reconstructs the exact working set:
  `afk-claim/<n>` ref = claim (owned by a **fleet instance**) · PR (`Closes #n`) = result · issue
  comment = blocker · `afk-attempt/<n>` label = retry count · `afk-heartbeat/<id>` ref = owner
  liveness. Nothing is remembered between ticks.

## Modes

- `/afk-fleet` — **launcher** (default): bootstrap, then loop spawning ticks. The main entry.
- `/afk-fleet --plan` — **dry-run**: print the frontier selection + dispatch plan and exit. Spawns
  nothing, pushes nothing, needs no authorization.
- `/afk-fleet --tick` — **one reconciliation pass** and exit with a summary. This is what the
  launcher spawns each cycle (and what you'd run headless). It auto-merges only under the run
  authorization its launcher injects; invoked cold without it, it dispatches + gates but holds
  merges.

## Launcher (default mode)

### Bootstrap (once, with the human present)

1. **Load config** — the target repo's `docs/agents/afk-fleet.md` (schema:
   [references/config-template.md](references/config-template.md)). Missing → offer to create it from
   the template and stop; never run on guessed settings.
2. **Establish this fleet instance** — mint a short unique **instance id** (this launcher run's
   identity, held only in the launcher and injected into every tick, exactly like the authorization
   below). Probe the claim namespace: push one throwaway ref to `refs/afk/probe` and delete it; if an
   org ruleset rejects it, fall back to `refs/heads/afk-claim/*` and **warn** that claim refs are then
   ordinary branches and may trigger `on: push` CI. See [Cooperative multi-fleet](#cooperative-multi-fleet).
3. **Preview** — run one `--plan` and show the dispatch plan (which issues, order, concurrency, gate
   steps, merge target).
4. **Authorize (the one gate)** — state plainly: *"I will push worker branches and **auto-merge**
   green PRs to `<target>` in `<repo>` unattended — this overrides the standing 'never push without
   asking' rule, for this repo, for this run. Confirm?"* Get an explicit yes. This authorization is
   **for the whole run**, held only in the launcher (never a config key); every tick inherits it via
   its spawn prompt, and it dies when you stop the launcher.

### Loop

Repeat until you stop it:

1. **Spawn a tick** — call the [Agent] tool (fresh context) to run one reconciliation pass, passing
   only `{repo, config, authorized: true, instance_id}`. Constrain its return with a schema:
   `{merged:[…], escalated:[…], dispatched:[…], reclaimed:[…], in_flight:N, frontier_remaining:N, note}`.
2. **Ingest the summary** — keep that one line; discard everything else. Surface a short progress
   line to the user.
3. **Pace adaptively** (`ScheduleWakeup`): if the last tick merged/dispatched/reclaimed anything or has
   in-flight PRs pending, wake again in `busy_interval` (~1–2 min) so green PRs merge promptly; if
   idle, `idle_interval` (~25 min). After `idle_ticks_before_sleep` consecutive empty ticks (frontier
   empty **and** no in-flight), drop to the long idle cadence. **While the fleet holds any claim
   (`in_flight > 0`), never sleep past `claim_lease_ttl`/2** — the heartbeat (refreshed inside the
   tick) must not lapse, or a peer will reclaim live work.
4. **Stop** on the user's word: run one final **drain** tick that releases claims with no PR yet and
   retains those with an open PR (see [Cooperative multi-fleet](#cooperative-multi-fleet)), then spawn
   no more ticks. In-flight workers finish on their own; their PRs are inherited and merged by a peer
   (or a later run) once the lease expires; escalated issues stay labelled for the human.

The launcher never dispatches, merges, or reads a worker itself — all of that happens inside a tick.

## A tick (`--tick`) — one reconciliation pass

A tick is stateless: it rebuilds from GitHub, acts, summarizes, and exits. It never waits for the
workers it dispatches.

1. **Rebuild the working set from GitHub** (never from memory):
   - **Frontier** — the dispatchable set: `open` + `ready_label` + no `epic_labels` + **unclaimed** +
     **no open linked PR** + zero open `blocked_by`. Run the query + selector in an **ephemeral
     sub-read** that returns only `{dispatch:[…], excluded:[…]}` (the 200-issue JSON stays there):
     ```bash
     gh issue list --repo <repo> --state open --limit 200 \
       --json number,title,labels | python3 <skill>/scripts/select_frontier.py --stdin \
         --ready-label "<ready_label>" --epic-labels "<epic_labels csv>"
     ```
     Three fields aren't in `gh issue list`; set each on the candidates before selecting:
     - `claimed` ← the claimed-set from one `git ls-remote origin 'refs/afk/claim/*'` (the issue number
       is in the ref name). This — not the assignee — is "already taken."
     - `has_open_pr` ← issues named by open PRs' closing refs:
       `gh pr list --repo <repo> --state open --json number,closingIssuesReferences`.
     - `open_blockers` ← for each candidate `gh api repos/<owner>/<repo>/issues/<n> --jq
       '.issue_dependencies_summary.blocked_by'` (native `blocked_by`, open only).
   - **In-flight** — the issues *I* own: each `afk-claim/<n>` ref whose marker names **my**
     `instance_id`. Sub-classify purely from each one's PR + checks: PR green → *awaiting merge*; PR
     pending → *awaiting CI*; PR red → *failure handling*; no PR yet → probe the worker's **liveness**
     via orca-cli (bounded, never a transcript read) — alive → still implementing, leave it; no live
     worker → **orphaned claim**, reconcile (tear down any stale worktree and re-dispatch, or release
     the claim by deleting its ref).
   - **Stale peer claims** — `afk-claim/<n>` refs owned by *another* instance whose `afk-heartbeat/<id>`
     is expired past `claim_lease_ttl`. These are the only foreign claims I may take: reclaim by an
     atomic `--force-with-lease` takeover (see [Cooperative multi-fleet](#cooperative-multi-fleet)),
     then treat as my own in-flight. A peer whose heartbeat is *fresh* is left strictly alone.
2. **Act**, in this order:
   - **Merge** every green in-flight PR (serialized — see below). Delete each merged issue's claim ref.
   - **Escalate** any retry-exhausted issue (see failure handling).
   - **Dispatch** to fill free slots up to `concurrency`: **claim atomically** by creating the lock ref
     `afk-claim/<n>` with my instance marker — if the push is **rejected** (`already exists`), a peer
     won the race, so skip it. On a won claim, create a worktree off latest `base_branch` (`git
     worktree add ../wt-issue-<n> -b issue-<n>-<slug> origin/<base>`) and use **orca-cli** to spawn a
     Claude Code worker with [references/worker-prompt.md](references/worker-prompt.md). Do **not** wait
     for it. (Claim mechanics: [Cooperative multi-fleet](#cooperative-multi-fleet).)
   - **Heartbeat** — if I hold ≥1 claim and my `afk-heartbeat/<id>` is older than `claim_lease_ttl`/3,
     force-push a fresh timestamp. Cheap and stateless: read the old ts from the ref itself.
3. **Return** the compact summary and **exit**. Freshly-dispatched workers' PRs are picked up by a
   later tick.

## Cooperative multi-fleet

Several fleet instances — on several machines, even under one shared GitHub account — may work the
same repo at once. The assignee can't arbitrate them (under a shared account it can't say *who* owns
an issue), so ownership lives in atomic **git refs** under the hidden `refs/afk/*` namespace and
liveness in a **per-instance lease**. See
[ADR-0003](../../docs/adr/0003-cooperative-multi-fleet-claims.md).

- **Instance id** — minted once per launcher run at bootstrap, injected into every tick. It stamps
  every claim this fleet makes and names this fleet's heartbeat.
- **Claim = create `afk-claim/<n>`** pointing at a marker commit whose message carries
  `instance=<id> host=<host>`; the ref name is the issue number *only*. Creating a ref that already
  exists is **rejected by the server** — that rejection *is* the compare-and-swap. Won → proceed;
  rejected → a peer owns it, skip. The claim ref is immutable after creation.
  ```bash
  sha=$(git commit-tree $(git hash-object -t tree /dev/null) -m "afk-claim instance=$ID host=$(hostname)")
  git push origin "$sha:refs/afk/claim/$n"    # nonzero exit ⇒ lost the race, back off
  ```
- **Owner check** — on rebuild, `git ls-remote origin 'refs/afk/claim/*'` lists every claimed issue;
  read a ref's marker (fetch it, then `git log -1 --format=%s`) to see whose it is. In-flight = claims
  stamped with *my* id; everything else is a peer's.
- **Heartbeat (the lease)** — one ref `afk-heartbeat/<id>` carries a timestamp. Any tick holding ≥1
  claim refreshes it **if due** (`now - ts > claim_lease_ttl/3`) by force-pushing a new marker — read
  the old ts from the ref, so this stays stateless. **Per instance, not per claim** (claim refs never
  churn); idle fleets don't heartbeat at all.
- **Reclaim a stale peer claim** — a foreign claim is reclaimable *only* once its owner's heartbeat is
  expired past `claim_lease_ttl`. Take it over atomically so two reclaimers can't both win:
  ```bash
  git push origin --force-with-lease="refs/afk/claim/$n:$sha_i_read" "$my_sha:refs/afk/claim/$n"
  ```
  A peer with a *fresh* heartbeat is left strictly alone — it reconciles its own dead workers locally.
- **Release / cleanup** — delete the claim ref (`git push origin --delete afk-claim/<n>`) on **merge**,
  **escalate**, and **orphan-release**. On **graceful stop**, the drain tick releases claims with **no
  PR yet** and **retains** those with an open PR (a peer inherits and merges the finished PR once the
  lease expires). The **open-PR guard** — an issue with an open linked PR is never in the frontier — is
  what makes releasing safe: a still-finishing orphan's PR is never re-dispatched, and a human's PR is
  left alone. A skipped delete is a **phantom lock** that silently starves an issue.
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
4. **Delete the claim** — `git push origin --delete afk-claim/<n>` (a *different* ref from the work
   branch that `--delete-branch` removed). Then remove the worktree (`worktree_cleanup`) and free the
   slot. A skipped claim-ref delete is a phantom lock that silently starves the issue.

The fleet's mandate **ends at a green merge to `merge.target`.** Deploying is a separate,
human-gated step — never done here.

## Failure handling — bounded retry → escalate, never silently drop

Per issue, on any of {worker failed, gate red, adversarial refute, unresolvable rebase conflict}:

1. **Retry up to `retry` times** (default 2). The attempt count lives as an **`afk-attempt/<n>`
   label** on the issue (not in tick memory): read it, and while `n < retry` swap it to
   `afk-attempt/<n+1>`, tear down the worktree, and re-dispatch — **keeping the claim ref** (you still
   own the issue). The failure reason handed to the new worker is **re-read from where it already
   lives** — the PR's CI checks, the verifier's PR review comment, or the reproduced rebase conflict —
   never carried in context.
2. **Still failing → escalate:** **delete the claim** (`git push origin --delete afk-claim/<n>`),
   remove `ready_label` and any `afk-attempt/*` label, add `escalate_label` (`ready-for-human`), and
   (if `escalate_comment`) comment the stuck-point with PR + log links. Then move on — never silently
   drop or silently merge bad work.

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
- **Never** read a worker's terminal for its result (only a bounded liveness probe); results are PRs,
  blockers are issue comments. A full transcript must never enter a tick or the launcher.
- **Claim before work** (create the `afk-claim/<n>` ref; if the create is rejected, a peer owns it —
  never proceed). **Release on every terminal transition** (merge, escalate, orphan-release) by
  deleting the ref — a leaked ref is a phantom lock. Reconcile only **your own** claims; take a peer's
  only when its heartbeat is expired (**stale claim**), never while it is fresh.
- **A human reserves an issue by removing `ready_label`**, not by assigning it — the fleet no longer
  reads the assignee. Keep the tracker honest so a peer fleet or a human never double-takes.
- Reserved: the fleet manages `afk-attempt/<n>` labels **and the `refs/afk/*` ref namespace**
  (`afk-claim/*`, `afk-heartbeat/*`) itself — don't hand-edit them or reuse those prefixes.
