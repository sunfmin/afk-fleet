# ADR-0047 — Off the turn a landing runs no local gate

**Status:** accepted — amends
[ADR-0045](0045-a-landing-that-stops-on-a-conflict-or-a-red-gate-gives-its-turn-up.md) (item 3,
"off the turn `afk land` syncs and gates"; item 4, "that turn is usually short"; and its rejected
alternative "mark ready without syncing or gating off the turn"). The invariant of
[ADR-0012](0012-local-completion-gate.md) is untouched.

## Context

A PR that gave its turn up was synced **and gated** off the turn before it was ready again
(ADR-0045), on the reasoning that the run goes on record for the tree (ADR-0030), so the PR's next
turn "costs a merge".

That holds only while the target stands still, and a PR gives its turn up precisely so that the
target moves: the next PR is granted the turn at once. A gate run takes as long as the landing that
passes it, so by the time the run made off the turn is green, the tree it is on record for is
rarely the tree that would land.

Seen on one run (calcgrid PR #1309, fleet `fl-c13f62`, 2026-10-10, `gate.ci: local`, a gate of
about five minutes, concurrency 5): the run made off the turn was green at 22:20 on the target as
it stood at 22:05; two PRs had landed at 22:18 and 22:19; the next turn, at 22:23, found no record
for its tree and ran the gate again. Five minutes of a loaded machine bought nothing. Seven PRs
landed in those 45 minutes — a target at rest is the exception.

Counted out, with G one gate run:

| the target, between the fix and the next turn | gated off the turn | not gated off the turn |
|---|---|---|
| did not move | G off the turn, a merge on it | G on the turn |
| moved | G off the turn, G on it | G on the turn |

Not gating never costs a run and saves one whenever the target moved. What it costs is where a
wrong fix is found.

## Decision

**Off the turn, in `local` mode, `afk land` syncs and stops there: no gate runs. A PR that merges
with the target cleanly is ready again (`awaiting_turn`). The gate runs once, on the PR's next
turn, on what the target holds then.**

1. **Where batches form**, the PR's head is stacked on the target's tip as before (ADR-0046) — that
   is what says whether it conflicts — and the landing stops there, the stack discarded: no
   `commit`, no `gate` in the result. A PR that cannot be stacked is synced, and a conflict is
   `conflict`, as often as it takes.
2. **Elsewhere in `local` mode**, the target is merged into the branch and pushed; no conflict is
   `awaiting_turn`.
3. **`required` mode is unchanged.** The checks are GitHub's, run on the pushed head whether the
   landing waits for them or not; off the turn red checks are still `gate_red`.
4. **A red gate is proved fixed by its worker**, with the tests that were red — the landing brief
   says so — not by the whole gate. `gate_red` is therefore not an outcome of a `local` landing
   off the turn.
5. **The next turn may hold a red gate.** `given_up` being on the marker, a `gate_red` there keeps
   the turn and is fixed with it held (ADR-0045, item 5) — nothing new, but now reachable by a fix
   that was never gated.

## What this gives up

ADR-0045 rejected exactly this, because "the second turn would then hold the turn through the gate
run of an unproven fix — the open-ended part again". That cost is real and is accepted: a fix that
does not hold is found with the turn held, and the queue waits while it is fixed again. It is
bounded as before — once per PR, and watched by the same ladder (nudge, restart, escalate). Against
it, the gate run made off the turn proved the fix against a target that was about to move, so it
bounded that risk only when nothing else was landing — when holding the turn costs nobody anything.

A PR whose resolution broke something unrelated to the conflict is likewise found on its turn.

## Consequences

- A PR that gives its turn up costs one gate run from then on, not two.
- Fewer gate runs side by side on one machine — which is itself what makes a gate with time limits
  red (the two red runs of the same PR, at load 131, with no failing test).
- `afk land` off the turn answers in seconds.
- The off-turn result carries no `gate` and no `commit` in `local` mode.

## Considered and rejected

- **Gate off the turn only after a `gate_red`**, not after a `conflict`. Two rules for one state,
  and the run is as stale either way.
- **Gate off the turn only while nothing else waits for a turn.** The landing would have to read
  the queue, which is the tick's; and what waits changes during the run.
