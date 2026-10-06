# ADR-0018 — A silent worker is nudged once before it is failed

**Status:** accepted — narrows one clause of the "read workers through GitHub, never their
transcripts" guardrail ([ADR-0001](0001-disposable-coordinator-context.md)), and adds an outcome to
`classify_no_pr` beside [ADR-0013](0013-liveness-is-recency-not-accumulated-work.md). The "liveness
probe" it mentions is read in code since [ADR-0021](0021-worker-state-is-mechanics.md). Where the
claim holds a **landing turn**, the silence past the nudge climbs the ladder of
[ADR-0035](0035-a-silent-landing-worker-is-restarted-onto-its-turn.md) instead — a restart, then
an escalation that keeps the PR — and never reaches `afk fail`; the failure below is a PR-less
claim's.

## Context

A `no_pr` claim whose worker is idle past `worker_idle_grace_seconds` with **no `afk:verdict` marker**
was classified `idle_failed` and sent to the retry ladder: the attempt counted, its branch and
worktree discarded, a fresh worker started.

That treats two different things as one. A worker that *declared* `giving-up` has failed. A worker
that declared nothing has only **stopped** — and in practice it stopped to ask a question. The case
that surfaced it: the whole worker prompt was delivered as one terminal paste, the worker read it as
quoted material with no instruction attached, and replied "confirm and I will start". It then sat
idle. Every tick saw it — the liveness probe said `idle` — and the fleet's answer was to throw the
worktree away and start a fresh worker, which asked the same question. Two retries later a human got
an escalation saying only "idle with no outcome".

The prompt delivery was fixed separately (a brief file plus a one-line pointer). The gap that
remains is general: any question a worker stops on — a permission it wants confirmed, an ambiguity in
the issue — is invisible to the fleet, costs a retry it cannot win, and reaches the human with its
actual content lost.

## Decision

1. **`idle_stalled` / `nudge` is its own outcome.** Idle past grace, no verdict found, never nudged →
   `afk no-pr` returns `idle_stalled`. A declared verdict is never overridden by this: `giving-up`,
   an unknown phase, a refuted `already-satisfied` still fail at once, and a terminal that is gone is
   still `dead`.
2. **`afk nudge` is the transition.** It reads the tail of the worker's rendered screen, types one
   short line at it — *nobody is watching this terminal; do not wait for an answer; continue the brief
   and end with a PR or a verdict marker* — and records `{at, tail}` in the worktree's git dir. No
   attempt label is touched, nothing is discarded.
3. **A nudge is spent once per worker.** The record makes the next `afk no-pr` count the nudge as a
   sign of life (one full grace period to answer it) and then, if the worker is silent again, return
   `idle_failed`; a second `afk nudge` is refused. Starting any new worker in the worktree clears the
   record. With no worktree on this machine there is nowhere to record a nudge, so the silence fails
   at once, as before.
4. **The failure says where the worker stopped.** When `afk fail` follows an unanswered nudge it
   appends the worker's last screen (as it is now, else as it was at the nudge) to `--reason` — so the
   retry's worker, or the human an exhausted ladder escalates to, reads the question that was asked.
5. **The terminal is read for a stall, never for a result.** A bounded tail (30 non-blank lines) of
   one screen, only for a worker already classified silent, only by `afk nudge` / `afk fail`. A
   worker's *result* remains its PR or its verdict marker, and a busy worker's terminal is still never
   read.

## Consequences

- A stalled worker costs one extra grace period instead of one attempt, and keeps its worktree.
- The fleet now writes to a worker's terminal after dispatch. The nudge is one line for the same
  reason the prompt pointer is: long text arrives as a paste the worker asks to have confirmed.
- The nudge record is local to the machine and the worktree, like the brief file. A claim taken over
  on another machine has no live worker to nudge, so nothing is lost by that.
- A worker that genuinely cannot proceed is failed one grace period later than before.

## Considered and rejected

- **Read every worker's terminal every tick.** It would put transcripts into every tick's context to
  catch a case the existing probe already isolates (idle + no verdict).
- **Nudge repeatedly.** A worker that ignores one instruction to stop asking will ignore the second;
  an unbounded nudge is a claim parked forever, the failure ADR-0013 removed.
- **Let the tick answer the worker's question.** That makes the tick a participant in the worker's
  session and its judgment depend on a transcript. The question goes to the retry or to a human.
