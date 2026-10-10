# ADR-0041 — A decision only the issue's owner can make is escalated without a retry

**Status:** accepted — adds a fourth phase to the worker's verdict and a row to the cause table of
[ADR-0017](0017-the-act-half-is-transitions.md); narrows the clause of
[ADR-0018](0018-nudge-a-silent-worker-before-failing-it.md) that a declared verdict other than
`blocked` "fails at once". Extends to `afk escalate` the worktree rule `afk park` has had since
[ADR-0022](0022-a-discovered-dependency-is-recorded-and-waited-on.md).

## Context

A worker that opens no PR declares why, and there were three words for it: `already-satisfied`,
`blocked` (it needs something another issue owns) and `giving-up` (it could not do the work or make
the gate pass). `giving-up` is a failure: the attempt is counted, discarded, and a fresh worker
starts from the base, told why the last one stopped. That is right when the *worker* fell short —
another one may not.

It is wrong when the *issue* is what falls short. The case that surfaced it: an issue asked for a
group of files to be split out of a core module, on the premise that nothing below the new module
used them. The first worker found the premise false and two acceptance criteria mutually exclusive,
wrote the evidence and three ways forward on the issue, and — having no other word — declared
`giving-up`. The fleet retried twice. Each fresh worker re-derived the same evidence and said so
("retrying before you decide will end in this same comment"). Thirty minutes and three workers
later the human got an escalation whose reason was a slug, under three near-identical comments,
with an idle worker still sitting in an empty worktree.

Nothing in that issue could change between attempts: not the code, not the issue text. What was
missing was a decision, and only its owner could make it.

## Decision

1. **`needs-decision` is a verdict phase.** A worker declares it when the issue as written cannot
   be done by anyone — a premise that does not hold in the code, criteria that contradict each
   other, two readings that lead to different work. The phases are told apart by who can supply
   what is missing: nobody needs to (`already-satisfied`), the backlog (`blocked`), the next worker
   (`giving-up`), the issue's owner (`needs-decision`).
2. **It is escalated at once and spends no attempt.** `classify_stopped` names the cause
   `needs_decision`; its row is `idle_undecided` / `escalate`. The tick runs `afk escalate` itself:
   the reason is always on record, because it is the worker's comment — the escalation quotes the
   marker's `reason=` and links the comment, and asks the launcher for no wording.
3. **The name carries its own burden of proof.** The worker prompt requires the comment to state
   the evidence, the decision to make, the options and the one the worker would pick, and to leave
   nothing on the branch. A worker that cannot name the decision has not found one — it is stuck,
   which is `giving-up`. That is the guard against the phase becoming a way out of hard work; it
   costs less than the alternative, since `giving-up` was already available for that and cost two
   more workers.
4. **An escalation removes a worktree whose branch holds no work.** `afk escalate` ends, after the
   release, by removing the issue's worktree when nothing is committed past the base and nothing
   is uncommitted — the rule `afk park` already applies, now shared (`_workless_worktree`). This
   holds for every escalation, not only this phase: a worktree is kept as evidence for the human,
   and an empty one is evidence of nothing — only a slot's worth of idle worker. A worktree with a
   PR, commits or a dirty tree stays exactly as before.

## Consequences

- An ill-posed issue costs one worker and reaches its owner with one comment to answer, instead of
  three workers and three comments.
- Whether a `needs-decision` is *right* is not judged by the fleet. A worker that declares it
  wrongly hands a human an issue an agent could have done; the human relabels it `ready_label` and
  it is dispatched again. A wrong `giving-up` was never judged either.
- The escalated issue is not re-dispatched by the owner's reply: the fleet reads labels, not
  answers. The owner edits the issue (or opens the ones the decision calls for) and puts
  `ready_label` back.
- `afk escalate` now removes something. It removes only what `afk park` would, after the claim is
  released, and reports it as `cleanup`.
