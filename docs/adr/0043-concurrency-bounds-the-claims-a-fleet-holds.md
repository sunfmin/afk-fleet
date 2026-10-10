# ADR-0043 — `concurrency` bounds the claims a fleet holds, stale reclaims included

**Status:** accepted — settles what `concurrency` counts, which
[ADR-0009](0009-one-home-for-config.md) gave a home but no rule; narrows the unattended reclaim of
[ADR-0003](0003-cooperative-multi-fleet-claims.md) and [ADR-0011](0011-takeover-and-progress-preservation.md)
to the free slots.

## Context

`concurrency` was documented as "max workers running at once", and the frontier was dispatched into
the free slots it leaves. Stale claims were not: a tick reclaimed and started **every** stale peer
claim, whatever the slots, so one dead fleet's backlog could start any number of workers in a single
tick. And `free_slots` was clamped at zero when the working set was assembled, so a fleet already
over the bound got a frontier slot back for each claim it settled while still over it. SKILL.md's
tick description ("a stale claim taken uses one") disagreed with its config section. A theorem hunt
found it: 5,968 of 30,000 random ticks ended above the bound.

## Decision

1. **The bound stands, and it counts claims held.** A claim holds its slot from the moment it is
   taken until it is released, whatever its worker is doing (as a PR waiting for its landing turn
   already did, ADR-0025). The free slots are `concurrency` minus the claims held, never below zero
   — one function, `afk_decide.free_slots`, read by the rebuild and by every take of a tick.
2. **A tick takes a claim it does not hold only into a free slot.** Stale claims first, lowest issue
   number first, then the frontier in its order. A stale claim past the slots is not tried: it stays
   stale, for a later tick or for another fleet.
3. **A fleet over the bound takes nothing until it is back under.** A takeover, or lowering the key,
   can leave a fleet holding more claims than `concurrency`; a claim it settles then frees no slot
   until the count is under the bound.
4. **Continuation takes no slot.** Restarting the worker of a claim already held — an orphaned
   claim, one whose blockers closed — adds no claim, and an orphaned claim is still never released
   back (ADR-0011).

So after any tick, claims held ≤ max(`concurrency`, claims held before it).

## Consequences

- A dead fleet's backlog is drained at the survivor's pace, not all at once. Its claims past the
  slots stay stale on the remote, visible to every fleet, and are taken as slots free: a claim
  settled in a tick frees its slot before that tick's starts.
- Stale claims are served before the frontier, so a long dead backlog holds new issues back until
  it is worked off. That is the order the tick already had; in-flight work is finished first.
- A human-authorized takeover is not bounded: it takes every claim of the chosen instance at once
  (ADR-0011). The bound then stops the fleet taking more.
