# ADR-0035 — A silent landing worker is restarted onto its turn, never failed for its silence alone

**Status:** accepted — adds one rung to the ladder of
[ADR-0018](0018-nudge-a-silent-worker-before-failing-it.md) where a claim holds a **landing turn**,
and amends "What bounds a turn" of
[ADR-0027](0027-a-worker-lands-its-own-pr-on-a-landing-turn.md). The merge batch's ladder
([ADR-0029](0029-a-merge-batch-lands-n-prs-behind-one-gate-run.md)) is unchanged. The record it
adds is a field of the turn marker, in the encoding of
[ADR-0032](0032-records-kept-in-comments-share-the-encoding-of-records-on-refs.md). While here, the
stale consequence of [ADR-0013](0013-liveness-is-recency-not-accumulated-work.md) is marked
superseded by the retry of [ADR-0017](0017-the-act-half-is-transitions.md).

## Context

A claim whose PR holds the landing turn was watched like a PR-less one: silent past grace it was
nudged once, and silent again it was failed — `afk fail` closed the PR, deleted the branch, removed
the worktree, spent an attempt and started a fresh worker from base.

That put a *landing* failure on the ladder built for *failed work* — red checks, a refuted verify,
a `giving-up` verdict. A PR on the turn had already been judged ready by the tick; what did not
happen was the landing, and the usual reason is delivery: the one line pointing at the landing
brief was not acted on by a session that had gone idle. ADR-0027 already keeps the work when the
worker's terminal is *gone* — a worker is started by continuation in the same worktree, on the same
branch, briefed only to land — so a worker that closed its session kept its PR while one that sat
idle lost it.

Seen live: issue #562's worker went silent on the turn of PR #578; the PR was closed, the branch
and worktree deleted, attempt 2 spent, and a fresh worker redid the issue from base.

## Decision

**A `landing` claim whose worker is still silent after its one nudge gets its worker restarted
onto the turn, once. Only the restarted worker's own unanswered nudge fails the attempt.**

1. **`afk no-pr` names it.** No verdict, silent after a nudge, on one PR's landing turn that has
   not been restarted: the cause is `silent_on_turn`, printed `idle_stalled` / `restart` — never
   `idle_failed` / `next_attempt`. Decided in `classify_stopped` from the turn record it already
   reads (`restartable_turn`): a turn that is held, is no merge batch's, and carries no
   `restarted`. A `no_pr` claim has no turn, so nothing changes for it; a landing that stopped
   for the tick (`awaiting_tick`) is still waiting, not silent.
2. **`afk turn --restart` is the transition.** It is the delivery `afk turn` already makes for a
   gone terminal, applied to an idle one: the idle session is closed (what starting a worker in a
   worktree already does), and a worker is started by continuation in the same worktree, on the
   same branch — or in one recreated at the PR's head, never from base — briefed only to land.
   Refused unless the PR holds this fleet's turn, and refused when the turn was already restarted.
   The tick's judgments carried on the marker (`verified`, `allow_no_checks`) stand; the gate check
   runs as on any re-delivery, and a result that is not `granted` is routed as any `afk turn`
   result — a judgment whose *yes* is the restart.
3. **Nothing is closed, deleted or counted.** The PR stays open, the branch and the worktree stay,
   no `afk-attempt/<n>` label moves, the turn stays with the PR, and `afk rebuild` and the status
   board say `landing` throughout. The nudge record is cleared by the start, as it is for any new
   worker; `at` moves, so the restarted worker gets a whole grace period to begin.
4. **The restart is recorded on the turn marker** — `restarted=<epoch>`, a field of
   `TURN_RECORD` — so the tick can see a turn that already had its restart, from GitHub, on any
   machine. A re-delivery of the same turn (`afk turn` after `awaiting_ci` / `needs_verify` /
   `no_checks`) carries it over; a turn granted anew — by another instance after a takeover, or to
   a PR that left a batch — carries none, as it is named on every grant.
5. **One restart per turn.** A restarted worker that goes silent again, after its own nudge, is
   `silent_after_nudge` and takes the path that existed before: `afk fail`, with the turn's
   reason. (A follow-up replaces that fallback with an escalation that keeps the PR.)
6. **A merge batch's worker is unchanged**: its second silence abandons the batch with nothing
   landed, which fails no PR (ADR-0029). `classify_stopped` is told the turn is a batch's, and
   `batch_step` has no restart.

The pass runs the restart in its nudge / fail stage (`tick_plan`), after the turn stage — which
grants nothing while the PR holds the turn — and counts it as `restarted`.

## Consequences

- A landing worker that merely stopped on its brief costs one more grace period and one session,
  not an attempt and a PR. The PR a human may already have looked at is the PR that lands.
- A worker that genuinely cannot land — a conflict it will not resolve, a gate it cannot turn
  green — is failed one grace period plus one restart later than before.
- The turn marker grows a field; a marker without it reads as it always did.
- `afk turn --restart` on a PR that holds no turn of mine is an error, not `waiting`: there is no
  worker to restart onto a turn nobody holds.

## Considered and rejected

- **Make the restart the orphan continuation (`afk dispatch`) by closing the terminal first.**
  Two calls for one transition, and nothing to record the restart with: an orphan is continued
  without bound, as it should be.
- **A flag rather than a time on the marker.** A time says the same thing to the tick and more to
  a human reading the PR.
- **Restart without bound.** A worker that ignores two briefs will ignore the third; an unbounded
  restart is a turn held forever — the parked queue ADR-0027's ladder exists to prevent.
- **Nudge twice instead.** ADR-0018 rejected it: a session that ignored one instruction to stop
  asking ignores the second. A new session reads the brief fresh.
