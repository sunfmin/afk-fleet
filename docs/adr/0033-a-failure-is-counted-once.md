# ADR-0033 — A failure is counted once: the count says whether its worker has started

**Status:** accepted. Sharpens the `afk fail` row of
[ADR-0017](0017-the-act-half-is-transitions.md); the order of that transition is unchanged.
Extended (#118) to the escalation the ladder ends in, which moves one step of the `afk escalate`
row: the comment now goes before the relabel.

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

The escalation at the end of the ladder had the same hole, the other way round. It relabelled —
stripping every attempt label — then commented, then released the claim. Refused at the comment or
the release, it left the claim held on an issue whose count was gone: the next `afk fail` read
attempt 0 and started the whole ladder over, discarding the PR of an issue already labelled for a
human; and where it did escalate again (`afk escalate`, which consults no count) it posted a
second comment, saying "without a retry".

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

5. **An escalation's comment records whose it is, and it is written before the relabel.** The
   comment leads with `<!--afk:escalation claim=<sha> attempt=<n>-->` (a record of
   [ADR-0032](0032-records-kept-in-comments-share-the-encoding-of-records-on-refs.md)): the sha
   the claim's ref is at, and the retries the issue had. The order is status board → comment →
   relabel → release. So wherever an escalation is cut, either nothing of it is on the issue but a
   status board — and the count is intact — or its comment is.
6. **A held claim whose escalation comment is on the issue is being escalated.** `afk fail` asks
   that before it reads the count (`escalation_begun`, an input of `next_attempt`) and escalates;
   it never retries. The escalation itself — from `afk fail` or `afk escalate` — posts no second
   comment, takes the attempt from the record instead of from labels that may be stripped, and
   does what is left: the status board, the relabel and the release were each already safe to
   repeat.
7. **Nothing new for the tick.** An escalation discards nothing, so what made the tick fail or
   escalate the claim — the red PR, the verdict, the silence — is still there on the next one,
   and it runs the same transition again.
8. **A config in which the relabel contradicts itself is refused** (`validate_config`):
   `escalate_label` equal to `ready_label`, which the one edit would both add and remove — and
   which would leave an escalated issue on the frontier — or under `afk-attempt/`, which the same
   edit strips and `current_attempt` may read as a count.

## Consequences

- One failure costs one attempt however many times `afk fail` runs to finish, by hand or by ticks.
- One escalation is one comment, and says the retries the issue really had, however many times it
  runs to finish. An issue escalated again later, under a new claim, gets a comment of its own:
  the earlier one names another claim.
- A count a hand-edit left a stray label beside still costs a counted failure no edit and no
  attempt: `retry_labels` compares the number, and the next count tidies the labels in the edit it
  makes anyway. A count spelled as the fleet never writes one (`afk-attempt/01`, `afk-attempt/²`)
  is not a count but one more stray label (`fleet_number`, #108): the issue reads as the attempt
  its other labels say — never retried, if it has none — and the next count strips it.
- An uninterrupted `afk fail` prints what it printed, and leaves the labels it left: the second
  label is gone again before it returns.
- One more reserved label under the `afk-attempt/` prefix; `current_attempt` reads past it.
- **What is not covered is the removal itself.** Refused after the worker has started, it leaves the
  label on a running attempt. That attempt's own failure is then retried without being counted —
  one retry more than configured, never one fewer — and if it merely goes quiet it is failed where
  it would have been nudged.

- **Nor is a cut escalation whose claim changes hands.** A peer that reclaims the claim (the
  fleet died mid-escalation and its lease ran out) re-stamps the ref: the comment names the old
  claim. Cut before the relabel, the peer's escalation posts a second comment; cut after it, the
  peer finds no count and no comment of its own and may retry an issue labelled for a human — the
  one case of #118 that remains, behind a fleet's death at one particular step.

## Considered and rejected

- **Recognise a cut escalation by its labels** (`escalate_label` on, `ready_label` off, a claim
  held). It costs no read and survives a reclaim, but it cannot say whether the comment was
  posted, nor after how many retries — the edit that makes the mark is the one that erases the
  count — so the comment would need recognising anyway.
- **Keep the comment after the relabel.** Then the count is gone before anything records it, and a
  comment posted on the second run could only say "without a retry".
- **Recognise the comment by its wording, the latest one on the issue.** An issue handed back and
  escalated again carries an earlier one; a cut before the comment would then post none.

- **Count last — discard, start, then raise the label.** A failed write then loses the count
  altogether, on a worker that is already running and gives no later tick a reason to come back.
- **A field on the claim record.** The count and "already counted" would live in two stores, written
  by two calls: the gap this closes, moved by one step.
- **Recognise the failure by what failed** (its PR, its verdict comment, its worktree). A worker
  that went silent with nothing pushed has none of them, and each kind of failure would need its
  own rule.
