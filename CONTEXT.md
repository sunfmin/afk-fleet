# afk-fleet

An unattended fleet that works a GitHub-issue backlog on its own, for days, without any session's
context growing without bound: a thin **launcher** spawns a fresh disposable **tick** each cycle;
each tick dispatches worktree-isolated **workers** per ready issue, gates them, and auto-merges green
PRs to the target branch.

## Language

**Launcher**:
The interactive session `/afk-fleet` is invoked in. It authorizes once, mints a **fleet-instance**
id, then loops: spawn a **tick**, ingest its one-line summary, heartbeat + pace, repeat. It does no
coordination itself, so its context stays flat over a multi-day run. Several launchers — on several
machines, even under one GitHub account — may run against the same repo at once; they cooperate only
through **claims**, never a central coordinator.
_Avoid_: coordinator (there is no single long-lived coordinator; a tick coordinates one pass),
orchestrator, manager, main agent

**Fleet instance**:
One launcher run and everything it owns — the ticks it spawns, the workers they dispatch, and the
**claims** it holds — identified by an id minted at bootstrap and injected into every tick (like the
run authorization). Its liveness is published as a **heartbeat**; when it stops or dies its claims
are released or reclaimed. Distinct instances (even on one GitHub account) are the unit of
cooperative concurrency across machines.
_Avoid_: node, worker (that is the per-issue Claude Code), coordinator

**Tick**:
One fresh-context, disposable reconciliation pass, run as an Agent subagent. It rebuilds the working
set from fleet state, acts once (merge green PRs, escalate exhausted, dispatch to free slots), returns
a compact summary, and dies — without waiting for the workers it dispatched. Runtime is bounded
because ticks are disposable, not because one session stays disciplined.
_Avoid_: batch (implies draining a whole wave), poll (a tick acts, not just observes), coordinator

**Worker**:
A fire-and-forget, ephemeral Claude Code session, isolated in one git worktree, that owns exactly one
issue, opens a PR, and reports done via GitHub. It never merges, and its terminal is never read for
its result.
_Avoid_: agent (too generic), subagent, child

**Fleet state**:
The authoritative record of the fleet's progress — what is claimed, in-flight, gated, merged,
escalated, and retried. It lives *outside* any context, in GitHub (claim/heartbeat refs, labels, PR
state, branches). It is the single source of truth; no tick holds it in memory.
_Avoid_: progress, run state, memory

**Working set**:
A tick's disposable, in-context view of the fleet at one moment. It is derived from fleet state and
is dropped when the tick returns.
_Avoid_: coordinator memory, session state

**Frontier**:
The set of currently-dispatchable issues — `open` + `ready_label` + not an epic + **unclaimed** (no
`afk-claim` ref) + **no open linked PR** + zero open `blocked_by`. Recomputed from GitHub every tick.
_Avoid_: queue, backlog (the backlog is the whole issue set; the frontier is only the ready edge)

**Claim**:
The atomic lock that marks one issue as owned by one **fleet instance**: a git ref `afk-claim/<n>` in
the hidden `refs/afk/*` namespace, whose creation the server accepts for exactly one fleet and rejects
for every other (that rejection is the compare-and-swap). Its marker commit names the owning instance.
It is the single source of truth for "taken" — replacing the assignee, which under a shared account
cannot say *who* owns an issue. Deleted at every terminal transition; a leaked claim is a phantom lock
that silently starves an issue.
_Avoid_: assignee (dropped as a claim signal), assignment, lock (too generic)

**Heartbeat** (and its lease):
The liveness signal a **fleet instance** publishes for itself — one ref `afk-heartbeat/<id>` carrying
a timestamp, refreshed while it holds any claim (per instance, not per claim; roughly once per
`claim_lease_ttl`/3, not once per tick). A claim is leased-live while its owner's heartbeat is within
`claim_lease_ttl`; its freshness is the only thing that lets a peer tell a live owner from a dead one.
_Avoid_: ping, keepalive, liveness probe (that name is the local orca-cli worker check — a different
thing, at a different granularity)

**Rebuild**:
The bounded pass, run at the top of every tick, that re-derives the whole working set from fleet
state: recompute the frontier, and reconstruct the in-flight set (the `afk-claim/<n>` refs owned by
this instance, sub-classified from each issue's PR + checks). Any tick — a fresh one or a later one — produces the
same working set from the same GitHub; this equivalence is the re-entrancy invariant that makes
disposable ticks safe.
_Avoid_: refresh, resync, reload

**Orphaned claim**:
One of **my own** claims (an `afk-claim/<n>` owned by this instance) with no PR and no live worker — a
claim whose worker crashed or never started. Reconciled *locally* on every rebuild (torn down and
re-dispatched, or released), never assumed still-running. Contrast **Stale claim**, which is a peer's.
_Avoid_: stuck issue, dead worker, zombie

**Stale claim**:
A **peer's** claim whose owner's **heartbeat** has expired past `claim_lease_ttl` — evidence the owning
instance died mid-flight. It is the only claim a fleet may take from another: reclaimed by an atomic
`git push --force-with-lease` takeover of the ref, and only then. A live peer's claim is never touched
— that is what keeps cooperating fleets from cannibalising each other's in-flight work.
_Avoid_: dead claim, abandoned claim, orphaned claim (that is one's *own* worker-less claim)
