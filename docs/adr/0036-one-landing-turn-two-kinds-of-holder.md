# ADR-0036 — The landing turn is one concept; the PR and the merge batch that hold it stay two

**Status:** accepted — changes no behaviour. Records a question an architecture review raised (#73)
so that later reviews do not raise it again:
[ADR-0027](0027-a-worker-lands-its-own-pr-on-a-landing-turn.md) and
[ADR-0029](0029-a-merge-batch-lands-n-prs-behind-one-gate-run.md) both stand as they are. One
validator that accepted both vocabularies is split in two.

## Context

A **landing turn** is held by one PR or by one **merge batch**. The batch came in as a fork in four
subcommands — `afk turn`, `afk land`, `afk no-pr` and `afk nudge` each branch on it — and the review
asked whether "the thing that holds the landing turn" should be one concept with two forms: one
holder type, one grant, one landing, one ladder.

The part that really was the same has since been removed as duplication: where a worker is, what it
is doing, telling it, replacing it and removing it go through one worker module, asked by issue or
by batch (#68, #69). What is left is every place the code still asks "batch or single", listed here
as it stood when this was decided:

| Where | What forks | |
|---|---|---|
| `afk turn` (`cmd_turn` → `_grant_turn` / `_turn_batch` / `_abandon_batch`) | One PR's grant waits on the tick's judgments — its checks, a PR with no checks, the adversarial verify — and tells the worker that wrote the branch. A batch's grant has no judgment to wait on (batches form only where none is owed), marks every member PR and cuts a worktree of its own. Abandoning has no single-PR counterpart. | really different |
| `afk land` (`cmd_land` / `_land_batch`) | Sync, gate and `gh pr merge` in the author's worktree, against stack, one gate run and a fast-forward push by a worker that wrote none of it. | really different |
| `afk no-pr` (`_worker_outcome` / `_batch_worker`) | The reading and the ladder are shared (`_Worker.settled`, `classify_stopped`). What is gathered differs: a verdict, its blockers and one PR's turn, against the newest turn among the members and nothing else — a batch worker declares no verdict and names no blocker. | really different, on a shared core |
| `afk nudge` (`_nudge_worker`) | Whose it must be (`_require_mine` / `_require_my_batch`) and which worktree to look in. Everything after that is one path. | really different, down to a key |
| The worker module (`_Worktree.of_issue` / `of_batch`, `_Workers`) | Two keys into one module: a worktree linked to an issue, or one named after a batch and linked to none. | already one |
| The turn record (`single_turn` / `batch_turn` / `unbatched_turn`, `turn_comment`) | One record kind, one reader (`latest_turn`), one test of who holds it (`held_turn`). Three constructors set the fields that are each form's own: the judgments, where the landing stopped and the restart, against the batch, its members and its phase. | really different |
| The silence ladder (`classify_stopped`, `worker_step` / `batch_step`) | One PR's turn: nudge, restart, escalate with the PR kept (ADR-0035). A batch's: nudge, abandon — the members fall back to single turns, so nothing is lost and there is nothing to restart onto. | really different |
| The tick's plan (`_turn_plan`, `asks_after`, `_begin_worker`) | The one place that chooses which form gets the turn, and the guards that keep a member's own worker off a turn that is its batch worker's. | really different |
| Landing outcomes (`LAND_OUTCOMES` / `BATCH_OUTCOMES`) | Two vocabularies, each with its validator; only `gate_red` is in both. | really different |
| Turn outcomes (`turn_outcome`) | One validator accepted the union of `TURN_OUTCOMES` and `BATCH_TURN_OUTCOMES`, so one PR's turn could stop with `too_few` or `abandoned` and nothing refused it. | **accidentally merged** |
| Bootstrap (`protection_verdict(batch=…)`) | A batch lands by pushing, so the target must accept a direct push; a single PR lands through `gh pr merge`. | really different |

Ten of the eleven are differences the two ADRs decided on purpose. The eleventh was not
duplication but its opposite: two vocabularies checked as one.

## Decision

**The landing turn is one concept. Its two holders — one PR, and one merge batch — are not
generalized into one.**

- **What is one stays one.** There is one turn out at a time per fleet instance, one **merge
  queue** it is granted from, one record kind on the PR (`afk:turn`) with one reader, and one worker
  module that answers for whoever is on the turn. A merge batch *is* a landing turn — "one landing
  turn held by several finished PRs" — and none of that is duplicated.
- **What is two stays two**: who lands (the worker that wrote the branch, or a worker that wrote
  none of it), how (a merge of one PR pinned to its gated head, or a stack pushed as a
  fast-forward), what the tick must judge first (checks and a verify, or nothing), and how a silence
  ends (restart then escalate, or abandon). Each of these is the substance of ADR-0027, ADR-0029 or
  ADR-0035, not an accident of how the batch was added.
- **The fork is kept where it is: at the subcommand's door.** `afk turn`, `afk land`, `afk no-pr`
  and `afk nudge` each take `--issue` or `--batch` and hand off to one path at once. Past the door
  a path asks about the other form only to refuse it — a member PR's own worker is not on a turn
  that is its batch worker's — or to look a worktree up by the right key.
- **Each form keeps a closed vocabulary of its own.** `turn_outcome` now accepts `TURN_OUTCOMES`
  only, and `batch_turn_outcome` accepts `BATCH_TURN_OUTCOMES`: a turn cannot stop with a word that
  only the other form is routed on.

## Consequences

- An architecture review that finds the four forks finds this record with them. The question is
  reopened by a change in substance — a batch that needs a judgment before its grant, a single PR
  landed by someone other than its author — not by the forks being there.
- A third kind of holder would be the moment to look again: two forms are a fork, three are a
  pattern.
- Rejected: **a `TurnHolder` with two implementations.** Its interface would be the union of both
  — judgments one form never has, members and phases the other never has, two ways to end a silence
  — so every caller would still ask which one it holds. The fork would move from four doors into
  every method, and ADR-0027 and ADR-0029 would have to be rewritten to say the same things in
  terms of it.
- Rejected: **a batch of one as the only form.** A single PR would then land by a push from a
  worker that did not write it, in a worktree that is not its own — giving up exactly what ADR-0027
  decided: a conflict or a red gate at landing is fixed where the context is. It would also bring
  the direct-push requirement on the target to repos in `gate.ci: required`, which never batch.
