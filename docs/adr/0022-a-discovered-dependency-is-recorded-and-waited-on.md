# ADR-0022 — A discovered dependency is recorded and waited on

**Status:** accepted — splits the `idle_blocked` / `escalate` outcome of `classify_no_pr`
([ADR-0013](0013-liveness-is-recency-not-accumulated-work.md)) in two, and adds a transition beside
those of [ADR-0017](0017-the-act-half-is-transitions.md).

## Context

A worker that finds its issue needs something another issue has not built yet stops with a `blocked`
verdict naming that issue. If the blocker was still open, the tick escalated: `ready_label` swapped
for `escalate_label`, the claim released, a human left to wait for the blocker and re-add the label by
hand.

That is right when nothing will ever resolve the blocker. It is wrong when the blocker is ordinary,
workable backlog — and most visibly wrong when **this same fleet is already working it**. The run that
surfaced it: a fleet dispatched #135 and, over the next ticks, #136–#143 beside it, none of which
declared `blocked_by #135`. Within minutes six of their workers posted `blocked` naming #135 — open,
claimed by this fleet, a worker actively coding. All six were escalated with empty branches, and
needed manual relabelling after #135 merged, for a dependency the fleet had both discovered and was
already resolving.

The worker had told the fleet the one fact the backlog was missing: an undeclared dependency edge.
The frontier contract already knows what to do with a dependency edge — exclude the issue while the
blocker is open, dispatch it when the blocker closes. The only thing missing was writing the edge
down.

## Decision

1. **Each named blocker has a standing**, decided in code (`blocker_standings`):
   - `closed` — done; it no longer blocks.
   - `waiting` — open, not an epic, and the backlog will resolve it with no human: a claim ref exists
     for it, an open PR closes it, or it carries `ready_label` (dispatchable, or merely waiting on
     blockers of its own).
   - `unmet` — nothing will resolve it: it cannot be read, is a pull request, was closed as not
     planned or as a duplicate, is an epic, is open with no claim, no PR and no `ready_label` (which
     includes an escalated one), or already depends on the blocked issue — recording the edge would
     close a cycle neither side ever leaves. Each carries the reason a human is told.

   The label half of this — `ready_label`, epic labels — is not restated: the frontier and the
   standing both read `label_bars`, so a rule added to one is a rule of the other, and a test holds
   "an unclaimed, PR-less open issue is `waiting` exactly when it is dispatchable".
2. **`idle_blocked` has three routes** (`blocked_route`): every named blocker `closed` → `redispatch`,
   as before; every open one `waiting` → **`park`**; any `unmet`, or none named → `escalate`, as
   before. One `unmet` blocker escalates the issue whatever the others are.
3. **`afk park` is the transition**, in one order: a native `blocked_by` edge from the issue to each
   named blocker still open → status board (*waiting on #n*) → release the claim → remove the
   worktree, only when its branch holds no work (one with commits or a dirty tree is kept, and a later
   dispatch continues from it). The edge goes first for the reason `afk escalate` relabels first:
   released without it, an issue still carrying `ready_label` is straight back on the frontier.
4. **Nothing else is written.** `ready_label` stays, `afk-attempt/<n>` is neither read nor written,
   and there is no "parked" label, ref or record. The dependency lives in GitHub as the edge; the
   frontier — recomputed every tick from GitHub — is what does the waiting, for this fleet and for
   every peer.
5. **Any claim counts as "being worked"**, whoever owns it and whether or not its heartbeat is fresh:
   mine and a live peer's have a worker, and a stale one is reclaimed and continued
   ([ADR-0011](0011-takeover-and-progress-preservation.md)). `afk no-pr` therefore needs no instance
   id to decide.
6. **`afk park` decides for itself.** It re-reads the verdict and the standings through the same
   gatherer `afk no-pr` uses and refuses, touching nothing, a claim that is not parkable now
   ([ADR-0016](0016-the-seam-enforces-its-own-rules.md)) — naming the transition it needs instead
   (`park_refusal`).
7. **The row says `pending_blockers`, not `open_blockers`.** A blocker closed as not planned is
   not open, and is not done either; `open_blockers` stays the name of the frontier's count of
   open dependency edges, which is a different thing.

## Consequences

- A dependency the backlog forgot to declare costs the fleet one worker start and no human touch. The
  six issues of the run above would have waited on #135 and been dispatched the tick after it merged.
- The park cannot loop. A re-dispatched worker would report `blocked` on the same blocker only while
  that blocker is open — and while it is open the edge keeps the issue off the frontier.
- The fleet now writes dependency edges, so the backlog's DAG is corrected as a side effect: the edge
  outlives the run, visible in the GitHub UI, and a later fleet does not rediscover it.
- A parked issue holds no slot and no claim; its status board stays up, saying what it waits on, until
  the next dispatch overwrites it.
- A `waiting` blocker may itself end badly — escalated after its retries, say. The parked issue then
  stays excluded behind an open blocker a human has been handed: the same state as any issue whose
  *declared* blocker was escalated. The escalation of the blocker is the signal; nothing is lost.
- `afk no-pr` costs a stopped, `blocked` worker more reads than before (each blocker, the claim refs,
  the open blockers behind each open blocker). A busy worker still costs none
  ([ADR-0021](0021-worker-state-is-mechanics.md)).
- A blocker closed as not planned used to count as "closed" and re-dispatch the worker into the same
  wall; it is now `unmet`.

## Considered and rejected

- **Keep the claim and wait.** The fleet would hold a slot, a worktree and an idle worker for however
  long the blocker takes, and the wait would live in one instance's claims instead of in the backlog.
- **Park only when this instance holds the blocker.** The case that surfaced it, but the reasoning is
  the same for a peer's claim or a ready issue no one has reached yet: what matters is whether the
  blocker will be worked, not by whom.
- **A `parked` label.** A second record of what the edge already says, to be kept in step with it by
  hand — and the frontier would need a rule for it.
- **Follow the chain to see whether the blocker's own blockers are resolvable.** A blocker that is
  ready but waiting behind something unworkable is a gap in *its* dependencies, and surfaces there.
  The chain is walked only for the one thing that cannot surface anywhere else: a cycle.
- **Trust the worker's `blocked_by` without recording it.** The next tick would dispatch the issue
  again, to a worker that stops for the same reason.
