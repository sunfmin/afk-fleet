# ADR-0024 — `afk merge` stays out of a worktree whose worker is busy

**Status:** superseded by [ADR-0027](0027-a-worker-lands-its-own-pr-on-a-landing-turn.md) — there is no tick-side merge to keep out of a worker's worktree: the worker is the one landing. `worker_busy` is removed. Originally: accepted — adds one stop to the merge transition
([ADR-0017](0017-the-act-half-is-transitions.md)), enforced in the seam
([ADR-0016](0016-the-seam-enforces-its-own-rules.md)) from the worker state
[ADR-0021](0021-worker-state-is-mechanics.md) already reads. The hand-back record of
[ADR-0019](0019-a-sync-conflict-is-handed-back-to-its-worker.md) is unchanged.

## Context

ADR-0019 keeps a merge out of a worktree the worker is resolving a conflict in by reporting the claim
`handed_back` until the PR head contains the target tip the hand-back named. That record is correct,
and it was read correctly, in the run that produced issue #32 (`sunfmin/calcgrid`, `gate.ci: local`):

| PR | hand-back recorded | worker's merge commit of the named tip | `afk rebuild` ~30 s after the hand-back |
|---|---|---|---|
| #292 (3rd hand-back, tip `af8ed3e`) | 06:39:39Z | `93f79ab`, 06:40:01Z — 22 s later | `awaiting_merge` |
| #297 (1st hand-back, tip `0c55652`) | 07:27:31Z | `64a0ad4`, 07:28:01Z — 30 s later | `awaiting_merge` |

The worker merged the target in and **pushed within half a minute**, then went on to run the gate
(353 s on that repo) — the order its first brief had taught it: sync, push, then gate. So the
hand-back really was answered, `awaiting_merge` was true, and the worker was still at work in the
worktree. `afk no-pr` said `working`, `idle_seconds: 0`, and `handed_back_at: null` — null because a
busy worker is settled from orca with no GitHub read (ADR-0021), which read as "the record is gone".

A tick following the skill then calls `afk merge` on that row: a sync and a second gate run in the
worktree where the worker is running its own, and a worker that fixes something after its gate finds
the branch moved under it. No bad merge happened only because that launcher improvised a guard.

"The PR head contains the tip" answers *is the conflict resolved*. It does not answer *is the worker
done*. Before a PR exists the two coincide — the PR is opened last. After a hand-back they do not.

## Decision

1. **`afk merge` never enters a worktree whose worker is busy.** Before touching the worktree it
   reads the worker state (one `orca worktree ps`, ~0.1 s) and, if the worker is busy — ADR-0021's
   two-signal busy — stops with outcome **`worker_busy`**: nothing synced, gated, pushed or merged.
   A later tick lands it. This holds for every claim, handed back or not.
2. **The rule lives in the transition, not in the tick's prose and not in the claim's status.**
   `afk rebuild` stays a GitHub-only read; a tick calls `afk merge` on every `awaiting_merge` row
   and routes on the outcome. A launcher needs no guard of its own.
3. **A lost stop report does not park the merge.** Busy needs fresh terminal output, so a worker
   that died while reporting `working` blocks the merge for at most `worker_idle_grace_seconds`.
4. **`handed_back_at: null` on a busy or gone worker means "not read".** It stays unread — reading
   it would put back the GitHub round trip ADR-0021 removed — and the docs say so.

## Consequences

- A worker may push its answer before gating it; the checkpoint is kept and costs nothing.
- A worker that opens its PR and wakes the launcher is still `working` for the few seconds it takes
  to end its turn. A tick that reaches `afk merge` inside that window gets `worker_busy` and the
  merge waits one busy interval. Accepted: a tick takes longer than that to start, and the
  alternative is merging beside a live worker.
- The merge transition makes one more orca call per run (two more, plus an idle probe, for a runtime
  that reports no state).
