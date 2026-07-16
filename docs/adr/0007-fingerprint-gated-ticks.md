# The launcher skips ticks code can prove are no-ops (the fingerprint gate)

**Status:** accepted — extends [ADR-0002](0002-launcher-and-disposable-ticks.md) (ticks stay
fresh-context LLM passes; this decides *whether one is spawned at all*) and applies the
[ADR-0004](0004-deterministic-mechanics-as-tools.md) bar (a deterministic verdict becomes an `afk`
tool).

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
