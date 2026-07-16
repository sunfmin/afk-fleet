# afk-fleet

An unattended fleet that works a GitHub-issue backlog on its own, for days, without any session's
context growing without bound: a thin **launcher** spawns a fresh disposable **tick** each cycle;
each tick dispatches worktree-isolated **workers** per ready issue, gates them, and auto-merges green
PRs to the target branch.

## Language

**Launcher**:
The interactive session `/afk-fleet` is invoked in. It authorizes once, mints a **fleet-instance**
id, then loops: spawn a **tick**, ingest its one-line summary, pace, repeat. It does no coordination
itself: it never computes the **frontier**, never reads tick-only files (the `afk.py`/`afk_decide.py`
source, `worker-prompt.md`), and delegates even the bootstrap **preview** to a **plan tick**. It is
thin *by construction* from its first action — it only ever spawns subagents and ingests their compact
summaries — so its context stays flat over a multi-day run. Several launchers — on several machines,
even under one GitHub account — may run against the same repo at once; they cooperate only through
**claims**, never a central coordinator.
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

**Plan tick**:
A **tick** run in dry-run mode (`--plan`): it does the full **rebuild** (recompute the **frontier**,
classify claims into mine/peer-live/stale) and then **stops before the Act phase**, returning the
dispatch plan instead of merging/dispatching/reclaiming. It is the *same* procedure as an acting tick,
short-circuited — so the plan a human authorizes against at bootstrap cannot drift from what a live
tick will actually do. Used both for standalone `/afk-fleet --plan` and for the launcher's bootstrap
preview (spawned there as a subagent, so the launcher never computes a frontier in its own context).
_Avoid_: dry run (that is its mode, not its name), preview pass

**Worker**:
A fire-and-forget, ephemeral Claude Code session, isolated in one git worktree, that owns exactly one
issue, opens a PR, and reports done via GitHub. It never merges, and its terminal is never read for
its result. Its worktree is created and later torn down by the **worker backend** — orca (`orca
worktree create` / `orca worktree rm`), the only supported backend — never by the tick with raw `git
worktree`; orca also names the branch (a `<user>/…` prefix), and the tick **reads that back** rather
than dictating it (ADR-0005).
_Avoid_: agent (too generic), subagent, child

**Mechanics vs judgment**:
The line that divides the fleet's work into what code owns and what the LLM owns. **Mechanics** are
the deterministic steps whose inputs uniquely fix the correct action, so a wrong result is a defect,
not a difference of opinion — selecting the **frontier**, claiming/reclaiming/releasing, the
mine/live-peer/stale partition, lease arithmetic, retry accounting, pacing. They are extracted into
tested **tools**. **Judgment** is everything that must read context and can be reasonably contested —
whether an implementation is correct (the gate), whether a refutation holds, whether a PR-less claim
is an **orphaned claim** or a live worker still coding, how to word an escalation, granting the run
authorization. It stays with the **tick** (an LLM). "Extract mechanics to code, keep judgment in the
LLM" is the fleet's core build rule.
_Avoid_: automation vs decision, deterministic vs heuristic (near, but this is specifically the
code/LLM ownership split), script vs agent (the tick is not a script)

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
disposable ticks safe. Its deterministic half is one read-only tool call — `afk rebuild`, which
gathers and assembles the working set (plus its fingerprint digest) in code; only the
orphan-vs-alive reconciliation of `no_pr` claims (the liveness probe) stays tick judgment (ADR-0008).
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

**Status board** (a.k.a. progress comment):
The human-facing projection of an issue's lifecycle onto the issue surface: a **single** comment the
owning **fleet instance**'s **tick** upserts each **rebuild**, rendering a milestone checklist (claimed
→ PR open → gate green → merged, with the *ci-failed* and *escalated* off-ramps) **derived** from
**fleet state**. It exists because the **claim** lives in a hidden ref namespace and the assignee is
unused, so the "claimed but no PR yet" phase is otherwise invisible to a reader. It is a *rendering* of
existing state, **never a source of truth** and **never read back by a tick**; it is edited in place
(idempotent — identical state renders identical text, so re-entrant ticks don't churn it), never
appended. Contrast the **escalation comment**, which is a durable, appended, re-readable handoff record
the board merely points to (ADR-0006).
_Avoid_: progress log (it is upserted, not appended), worklog, status label (a label is machine state;
this is human narration), progress ref
