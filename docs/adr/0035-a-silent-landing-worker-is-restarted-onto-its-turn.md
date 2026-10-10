# ADR-0035 — A silent landing worker is restarted onto its turn, never failed for its silence alone

**Amended by [ADR-0048](0048-finished-prs-join-a-landing-train-gated-whenever-the-gate-is-free.md) (#157):** where a landing train runs the same ladder bounds a worker that is *joining* (nudged, restarted onto the join brief, escalated); the train's own worker is nudged once and then the train is abandoned.

**Status:** accepted — adds one rung to the ladder of
[ADR-0018](0018-nudge-a-silent-worker-before-failing-it.md) where a claim holds a **landing turn**,
and amends "What bounds a turn" of
[ADR-0027](0027-a-worker-lands-its-own-pr-on-a-landing-turn.md). The merge batch's ladder
([ADR-0029](0029-a-merge-batch-lands-n-prs-behind-one-gate-run.md)) is unchanged. The record it
adds is a field of the turn marker, in the encoding of
[ADR-0032](0032-records-kept-in-comments-share-the-encoding-of-records-on-refs.md). While here, the
stale consequence of [ADR-0013](0013-liveness-is-recency-not-accumulated-work.md) is marked
superseded by the retry of [ADR-0017](0017-the-act-half-is-transitions.md).
**Amended by [ADR-0045](0045-a-landing-that-stops-on-a-conflict-or-a-red-gate-gives-its-turn-up.md) (#151):** the
same ladder bounds a worker fixing **off** a turn its PR gave up — nudge, one restart (`afk turn
--restart`, which grants no turn there), then the escalation that keeps everything. Giving the turn
up clears `restarted`, and the PR's next turn has a restart of its own.
**Amended (#91):** the rung past the restart is an **escalation** that keeps the PR, not `afk
fail` — see [the amendment](#amendment-91--a-landing-that-outlives-its-restart-is-escalated-with-everything-kept)
at the end. Where decision 5 and the consequences below say the restarted worker's second silence
fails the attempt, read the amendment: a landing turn's silence never reaches `afk fail`.

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
   reason. *(As first written. Superseded by the amendment below: that silence is
   `silent_past_restart`, and is escalated with the PR kept.)*
6. **A merge batch's worker is unchanged**: its second silence abandons the batch with nothing
   landed, which fails no PR (ADR-0029). `classify_stopped` is told the turn is a batch's, and
   `batch_step` has no restart.

The pass runs the restart in its nudge / fail stage (`tick_plan`), after the turn stage — which
grants nothing while the PR holds the turn — and counts it as `restarted`.

## Consequences

- A landing worker that merely stopped on its brief costs one more grace period and one session,
  not an attempt and a PR. The PR a human may already have looked at is the PR that lands.
- A worker that genuinely cannot land — a conflict it will not resolve, a gate it cannot turn
  green — is failed one grace period plus one restart later than before. *(Since #91: is
  escalated, with the PR kept, at that point — never failed.)*
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

## Amendment (#91) — a landing that outlives its restart is escalated, with everything kept

### Context

Decision 5 bounded the turn with the ladder built for *failed work*: the restarted worker's own
unanswered nudge was `afk fail` — close the PR, delete the branch, remove the worktree, spend an
attempt, start over from base. For a PR the tick had already judged ready, that threw finished work
away to recover from a worker that would not run one command, and did so one restart later than
before rather than never.

The escalation transition of [ADR-0017](0017-the-act-half-is-transitions.md) already leaves the PR
and the worktree for the human (status board → relabel → comment → release last). It is the right
end of a landing nobody could get a worker to perform.

### Decision

**A restarted landing worker that is silent again after its own nudge is escalated, not failed. A
landing turn never spends an attempt, and no silence of a landing claim reaches `afk fail`.**

1. **`afk no-pr` names it.** No verdict, silent after a nudge, on one PR's landing turn that
   already carries `restarted`: the cause is `silent_past_restart`, printed `idle_stalled` /
   `escalate` — never `idle_failed` / `next_attempt`. Decided in `classify_stopped` beside
   `silent_on_turn`, from the same turn record: a turn that is held and is no merge batch's
   (`single_turn_held`) climbs its own ladder — restart while it has not been restarted, escalate
   after. With no worktree on this machine to record a nudge in, the turn takes its next rung at
   once (restart, then escalate), where a PR-less claim fails at once (ADR-0018).
2. **`afk escalate` is the transition, as it is today.** The PR stays open, the branch and the
   worktree stay, `escalate_label` goes on and `ready_label` comes off, the reason is commented
   when `escalate_comment` is on, and the claim is released last. The reason says the PR was judged
   ready, the landing was restarted once, and where the worker stopped: `afk escalate` now appends
   the worker's last screen after an unanswered nudge, exactly as `afk fail` does. No
   `afk-attempt/<n>` label goes up; the hand-off reports the attempts the issue had, which the turn
   did not add to.
3. **The release frees the turn.** A turn marker names the instance whose claim held it, and a
   released claim holds no turn: `afk turn` reads turns through the claims (`_claim_turns`), so the
   next cycle's `afk turn` for the next ready PR of the same instance answers `granted`, not
   `waiting`. The marker stays on the escalated PR for the human to read, and `afk land` on it is
   refused — the claim names nobody.
4. **A landing claim's silence has no failure route.** `worker_step` routes `silent_past_restart`
   to `afk escalate`; a `landing` row classified `silent_after_nudge` or `silent_unnudgeable` is an
   error of the tick, not a failure — such a row holds no turn of one PR, which `asks_after` never
   produces. `afk fail` reaches a landing claim only by the tick's own judgments: red checks in
   `gate.ci: required`, a refuted adversarial verify.
5. **The merge batch's ladder is unchanged**: its second silence abandons the batch with nothing
   landed (ADR-0029).

### The whole bound of a landing turn

told → grace → nudge → grace → restart → grace → nudge → grace → escalate — with nothing closed,
deleted or counted at any step. A worker whose terminal is gone is continued onto the turn at any
step, unbounded; a worker whose landing stopped for the tick is waiting, not silent.

### Consequences

- A PR the tick judged ready is never thrown away because its worker would not land it: the human
  gets the PR, the branch, the worktree, the turn marker, the reason and the last screen.
- A landing that genuinely cannot complete — a conflict nobody resolves, a gate nobody turns green
  — reaches the human one escalation later than a failure would have, and with the evidence intact
  rather than redone from base.
- `afk fail` on a landing claim is a tick judgment, never a silence; the retry ladder is for failed
  work.

### Considered and rejected

- **Keep `afk fail` past the restart, but without discarding.** A retry that keeps the PR is not a
  retry — the fresh worker would be a third restart — and a failure that keeps everything is an
  escalation by another name.
- **A second restart.** Rejected above: a worker that ignores two briefs will ignore the third.
