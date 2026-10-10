# The launcher skips ticks code can prove are no-ops (the fingerprint gate)

**Status:** accepted — extends [ADR-0002](0002-launcher-and-disposable-ticks.md) (ticks stay
fresh-context LLM passes; this decides *whether one is spawned at all*) and applies the
[ADR-0004](0004-deterministic-mechanics-as-tools.md) bar (a deterministic verdict becomes an `afk`
tool).
**Note ([ADR-0028](0028-the-launcher-runs-each-cycle-itself.md)):** the gate stays; one of its
reasons is gone. A tick is no longer a spawned context, so "the expensive part is spawning the fresh
context" (the rejection of *fingerprint inside the tick*) no longer applies — the gate now runs
inside `afk cycle`, in front of the pass, and what a skip saves is the pass's GitHub reads and
writes, not a cold start.
**Note (#51) — what the digest holds, and which digest is kept.** Three things changed; the gate and
its two invariants did not.

- **A PR's checks enter as the one word a tick acts on** — `green` / `red` / `pending` / none — not
  each check's own status and conclusion. A check going queued → in progress, or the first of
  several finishing green, used to move the digest and run a tick that concluded the claim was
  still `awaiting_ci`; now only the verdict arriving (pending → green, pending → red) does.
- **An issue's open-blocker count is in its row.** The issue list carries the count, so the
  frontier reads it from there instead of once per candidate; a blocker recorded or closed moves
  the digest directly rather than through an `updatedAt`.
- **The cycle state keeps the digest of the fleet as the tick LEFT it**, gathered again after the
  tick's last write, not the one taken before the tick. A tick writes status boards, claim refs,
  turn markers and labels, each of which moves an `updatedAt` or a ref; with the opening digest
  kept, every tick was followed by a second one that found nothing. What that closing digest
  would swallow — something that changed on GitHub while the tick ran — is covered twice: a
  **wake** that arrived during a cycle is passed to the next one (`afk cycle --wake`), which then
  ticks whatever the digest says (`reason: wake`), and the forced tick remains the backstop for a
  change nobody announced.

**Note — a PR that opens behind a tick's back.** The closing digest swallowed more than that note
allowed for. A worker opens its PR and wakes the launcher within seconds; the tick that wake
starts reads GitHub once, at its top, and GitHub may not yet list the PR — or lists it before it
lists the issue the PR closes, which is the only thing that makes it a claim's PR. The tick saw a
claim with no PR and did nothing; its closing digest held the PR; the next cycle read `unchanged`
and skipped. No wake was pending (the one that came had been spent on the tick that looked too
early), so the PR waited for the forced tick or for some unrelated change — measured on a live
fleet, 2 min 23 s and 4 min 42 s with the landing turn free the whole time. Two changes:

- **The issues a PR closes are in its row of the digest.** The link arriving after the PR now
  reads as a change.
- **A tick says which PRs it did not act on.** The closing gather is compared with the working set
  the tick worked from (`unseen_prs`): a claim whose closing PR is not the one its row named leaves
  the cycle state `unsettled`, and the cycle returns `sleep_seconds: 0`. The launcher opens the
  next cycle at once; it ticks (`reason: unsettled`) and grants the turn. It costs no read — the
  closing gather is the one the digest is taken from.

**Note (#120) — one digest, one working set.** The gate skips a tick on the ruling that the same
digest means the same working set, apart from what time alone changes. Two things broke it, and
neither was a field going stale:

- **The frontier followed the order its rows arrived in**, which the digest — sorted — cannot see:
  two gathers with one digest filled the free slots with different issues. The frontier is now in
  issue-number order, lowest first, whatever order the issue list came in.
- **The working set read more than the digest held.** An issue's title and a claim's owning
  instance are now in their rows of the digest. What is left outside it is a closed list — the
  section *What the working set reads outside the digest* below — where it used to be whatever the
  assembly happened to take.

A test generates fleets and holds both: any shuffle of every input list assembles the same working
set, and a change to a gathered row that leaves the digest alone leaves the working set alone.

## Context

ADR-0001/0002 bound the fleet's context growth — tokens *per call* — but every launcher wake-up
still spawns a full tick: a fresh LLM context that loads the skill and config, runs a dozen `gh`
calls, and, most of the time, concludes "still waiting." At the idle cadence (~25 min) an empty
backlog burns ~58 no-op ticks a day, for days — that is the fleet's dominant steady-state token
spend. While busy (~90 s cadence), a 10-minute CI run eats another half-dozen ticks that merge
nothing. Whether anything observable changed since the last cycle is a **deterministic** question,
and per ADR-0004 a deterministic verdict re-derived by an LLM every cycle belongs in code.

## Decision

Add `afk fingerprint`: gather what a tick's Rebuild would observe — open issues (number, labels,
`updatedAt`), open PRs (number, head sha, `updatedAt`, per-check status/conclusion), and claim refs
(number, sha) — **inside the tool process**, collapse it to a 16-hex digest, and return a
skip-or-tick verdict (`afk_decide.fingerprint` + `fingerprint_gate`, fixture-tested). The launcher
runs it each wake-up and spawns a tick only on `tick` (changed / forced / first); on `skip` it
spawns nothing. Two invariants make skipping safe:

- **Forced full tick every `force_tick_after_skips` cycles** (default 6). Time-driven transitions —
  a peer's lease expiring, a worker dying without a trace — are invisible to any state hash; the
  forced tick bounds their staleness. Correctness therefore never depends on the gate: a false
  "changed" costs one tick (today's behaviour), a missed change waits at most N cycles.
- **Heartbeats are excluded from the digest, and skipped cycles still beat.** Including them would
  be self-defeating (the launcher's own refresh would move the digest every cycle); instead, while
  the last summary shows `in_flight > 0`, the launcher calls `afk heartbeat` directly on a skip —
  an `afk` subcommand it is already permitted to run — so a skipped cycle can never lapse a lease.

The launcher's inter-cycle state stays tiny and constant: last summary + last fingerprint + skip
streak. Pacing is unchanged and paces off the previous summary — the gate decides *whether* a tick
runs, never *when* the next wake-up is.

## What the working set reads outside the digest

The digest holds everything the working set reads of the three gathered lists — open issues, open
PRs, claim refs. These are its other inputs, by the name `assemble_working_set` takes each under
(`afk_decide.OUTSIDE_THE_DIGEST`; a test holds this table to it). With these equal, one digest is
one working set.

| Input | What it is | Why a skipped cycle may go without it |
|---|---|---|
| `now` | the clock | Time alone moves it. What it decides — a lease lapsing — is the forced tick's to see. |
| `heartbeats` | every instance's last beat | See the second invariant above: this instance's own beat on a skipped cycle would move the digest every cycle. A peer's beat going stale is time passing — the forced tick. |
| `turns` | the landing-turn marker on the PR of each claim of mine | A comment on the PR, read once per claim that has a PR — a read the gate does not make. Writing one moves that PR's `updatedAt`, which the digest holds: the digest sees *that* a marker was written, not what it says. Observed for a new comment; for a marker rewritten in place it rests on GitHub moving `updatedAt` then too, and on the forced tick if it does not. |
| `closed` | which claims are on a closed issue | Asked only of a claim whose issue is missing from the open list. The issue leaving that list moved the digest; why it left is one read, and a read that failed is asked again by the forced tick. |
| `landed` | which claims of mine the landing train already landed on the target | Asked only of a claim of mine on an open issue — the target is fetched once and asked about each. It is true only once what lands the claim was pushed to the target: the train that carried it there moved the digest (its tip is in it), and so did GitHub taking the merged PR off the open list. A read that failed is asked again by the forced tick. |
| `joined` | which claims' open PR is on the landing train, its head as it is now (ADR-0048) | Read off the train's own commits and the open PRs' heads, and the digest holds both: the train's tip — fetched with the claim refs, in the same one call — and every PR's head. |
| `me` | this instance's id | The run's own: no cycle changes it. |
| `config` | the resolved config | The run's own: no cycle changes it. |

## Considered and rejected

- **Longer idle intervals instead.** Free, but it trades latency for cost linearly and does nothing
  for the busy-cadence no-ops (awaiting CI at 90 s). The gate makes the no-op itself nearly free, so
  cadence can stay responsive. (Operators can still raise `idle_interval_seconds` on top.)
- **The launcher eyeballs `gh` output itself and decides.** Puts raw issue/PR lists into the
  launcher's context every cycle — exactly what ADR-0002 forbids (the launcher stays thin by
  construction) — and makes the skip verdict LLM judgment, which drifts (ADR-0004).
- **Fingerprint inside the tick** (tick starts, checks, exits early). Too late — the expensive part
  is spawning the fresh context at all, not the tick's late phases.
- **Include heartbeats in the digest.** Self-defeating churn; see above.
- **Event-driven (webhooks) instead of polling.** Strictly better signal, but it needs a listening
  endpoint and repo-admin setup — infrastructure a CLI fleet on a laptop doesn't have. The
  fingerprint keeps the poll model and removes its cost.

## Consequences

- New: `afk_decide.fingerprint` / `fingerprint_gate` (pure, fixture-tested) and the `afk
  fingerprint` subcommand (gather half effectful, verdict half pure — same split as
  `classify-claims`). Config gains `fingerprint_gate: true` and `force_tick_after_skips: 6`
  (`1` disables skipping; `fingerprint_gate: false` removes the gate call entirely).
- The launcher loop gains a step 1 (gate) and, on skips with claims held, a direct `afk heartbeat`
  call — the one coordination-adjacent action the launcher performs itself, justified because it is
  a deterministic tool call, not coordination judgment.
- Reaction latency to hash-invisible events is bounded by `force_tick_after_skips ×` the current
  interval (defaults: ≤ 6 × 25 min idle). Hash-visible events (label edits, CI finishing, pushes,
  claim churn, new issues/PRs, blocker comments via `updatedAt`) are caught on the next wake-up.
- `updatedAt` makes the digest conservative: any issue/PR activity (including human comments)
  triggers a tick. On chatty repos the gate saves less — it never costs more than today's
  tick-every-cycle behaviour.
