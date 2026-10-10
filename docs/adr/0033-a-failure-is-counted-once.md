# ADR-0033 — A failure is counted once: the count says whether its worker has started

**Status:** accepted. Sharpens the `afk fail` row of
[ADR-0017](0017-the-act-half-is-transitions.md); the order of that transition is unchanged.

## Context

`afk fail` is the one writer of the attempt count, the issue's `afk-attempt/<n>` label. A retry
reads it, writes it one higher, and only then discards the failed attempt (closes its PR, deletes
its branches, removes its worktree) and starts a fresh worker. Each of those later steps can be
refused — by GitHub, by the remote, by orca — and ADR-0017 is deliberate about what that leaves: the
claim still held and nothing reported as done.

But the count was already written, and what failed was still there. The next tick found the same
red PR, the same `giving-up` verdict, the same silent worker, and failed it again; so did a human
running the command a second time. Read → add one → write is not something that can be done twice:
one failure spent two attempts, and the issue reached a human a retry early. A test now cuts the
transition short at each of the three steps and shows it.

## Decision

1. **The count carries whether it has been acted on.** The edit that raises `afk-attempt/<n>` also
   adds `afk-attempt/starting`, in the same `gh issue edit`: *this failure is counted, and the fresh
   worker of that attempt has not started.* They are written together or not at all.
2. **Starting a worker on the issue removes it** — any start, the retry's own or a later
   `afk dispatch`, once the worker has its prompt.
3. **`afk fail` on an issue that carries it adds nothing.** It is the same failure: the transition
   keeps the attempt it already made and does whatever is left of the discard and the start, each of
   which was already safe to repeat. Without the label a failure is a new one and is counted;
   `retry` exhausted escalates as before, and an escalation strips the label with the rest.
4. **The tick finishes such a retry by itself.** `afk rebuild` puts the label on the `mine` row as
   `starting`. A `starting` claim whose worker is not at work is failed again whatever else is true
   of it — it is never continued, nudged or parked, because what is in its worktree is the attempt
   that was being discarded. A PR that is still open and red is asked about as it always was, and
   the answer runs the same transition.

## Consequences

- One failure costs one attempt however many times `afk fail` runs to finish, by hand or by ticks.
- An uninterrupted `afk fail` prints what it printed, and leaves the labels it left: the second
  label is gone again before it returns.
- One more reserved label under the `afk-attempt/` prefix; `current_attempt` reads past it.
- **What is not covered is the removal itself.** Refused after the worker has started, it leaves the
  label on a running attempt. That attempt's own failure is then retried without being counted —
  one retry more than configured, never one fewer — and if it merely goes quiet it is failed where
  it would have been nudged.

## Considered and rejected

- **Count last — discard, start, then raise the label.** A failed write then loses the count
  altogether, on a worker that is already running and gives no later tick a reason to come back.
- **A field on the claim record.** The count and "already counted" would live in two stores, written
  by two calls: the gap this closes, moved by one step.
- **Recognise the failure by what failed** (its PR, its verdict comment, its worktree). A worker
  that went silent with nothing pushed has none of them, and each kind of failure would need its
  own rule.
