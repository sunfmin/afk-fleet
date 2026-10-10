# ADR-0046 — Where merge batches form, a PR that lands alone is stacked on the target like a batch of one

**Status:** accepted — changes how one PR lands in `gate.ci: local` with no adversarial verify, the
configs where merge batches form (`afk_decide.batches_form`). Amends
[ADR-0027](0027-a-worker-lands-its-own-pr-on-a-landing-turn.md) (there the landing's order is
"sync → push → gate → `gh pr merge`": in these configs it is stack → gate → fast-forward push, and
the window #126 left open is closed) and
[ADR-0034](0034-every-pr-lands-as-a-merge-commit-and-batches-need-no-switch.md) ("Alone: `afk land`
runs `gh pr merge --merge`": only where no batch forms). Narrows two entries of
[ADR-0036](0036-one-landing-turn-two-kinds-of-holder.md): *how* a single PR lands is no longer
different from a batch member in these configs, while *who* lands it, and where, stay as ADR-0027
decided. [ADR-0045](0045-a-landing-that-stops-on-a-conflict-or-a-red-gate-gives-its-turn-up.md) is
unchanged: the same two stops give the turn up, once. The invariant of
[ADR-0012](0012-local-completion-gate.md) is kept, and for these landings by construction.
`gate.ci: required`, and any config with the adversarial verify on, land exactly as before.

## Context

A PR landing alone synced first: `afk land` merged the target **into the PR's branch**, pushed that,
gated the head, and ran `gh pr merge --merge`. So every landing whose target had moved put two merge
commits on the target's history — the sync merge (`Merge commit '<sha>' into <branch>`) and the PR's
own — and a landing that was repeated (`target_moved`, or a turn given up and taken again,
ADR-0045) left one sync merge per attempt. With several workers landing one after another the
target moves on every landing, so nearly every PR paid at least one.

Seen on a live fleet (sunfmin/calcgrid, 2026-10-10, concurrency 5): of 12 consecutive commits on
`main`, 4 were `Merge pull request`, 6 were sync merges (3 `Merge commit '<sha>'`, 3
`Merge remote-tracking branch 'origin/main'`) and 2 were work. The person reading the history asked
why there were so many merge commits.

A merge batch never needed a sync merge: its stack — each member merged onto the target's tip — *is*
the sync (ADR-0029, ADR-0034). A PR landing alone is the same thing with one member.

## Decision

**Where merge batches form, `afk land` stacks the PR on the target's tip instead of merging the
target into the PR's branch.** In the worker's own worktree, on its turn (`_land_stacked`):

1. **Push** whatever the worker committed since the PR's head on the remote — a resolution, a fix.
2. **Stack.** The target's tip is checked out detached and the PR's head merged onto it with ONE
   merge commit (`git merge --no-ff`), written the way a batch member's is
   (`afk_decide.stack_message`): the PR's title, `(#<pr>)`, `Closes #<issue>`; second parent the
   PR's own head. The branch is not touched.
3. **Gate** that commit — `gate.local_command`, or the green run on record for its tree (ADR-0030).
4. **Land.** The turn is read again (#126), and the commit is pushed to the target as a
   **fast-forward**. That push is the only lock: a target that moved refuses it, nothing lands
   (`target_moved`), and the next run stacks on the new tip and gates that.
5. **Finish**, as for a batch member: status board, the issue closed, and — once GitHub shows the PR
   merged, which it does by itself because the PR's head is on the target — its branch deleted
   (`--merged-timeout`).

The worktree is put back on its branch whenever the command returns or fails; one a killed run left
on its stack is put back by the next run before anything else.

### What stays the worker's, in place

- **A conflict.** A PR whose head does not merge cleanly onto the target's tip cannot be stacked,
  and the landing falls through to what it did before: the target is merged into the branch, the
  conflict is left in progress, the outcome is `conflict`, and the first one gives the turn up
  (ADR-0045). The worker resolves and commits; the next run stacks that head, which now holds the
  target's tip. **This is the sync merge that remains** on the target's history, and only a PR that
  really conflicted leaves it.
- **A red gate.** The gate ran on the stack, a tree the branch does not hold, and a worker cannot
  fix what it cannot reproduce. So before `gate_red` is returned the tip the stack was made on is
  merged into the branch and pushed: the tree in front of the worker is the one that was red. A PR
  that is red only *with* the target is a conflict git could not see, and it costs the same one
  sync merge; a PR that is red by itself would have been red before its PR was opened.
- **Off the turn** (a turn given up, ADR-0045) the same command pushes, stacks and gates, and pushes
  nothing to the target: green is `awaiting_turn`. The run is on record for the stack's tree, so on
  the PR's next turn a target that has not moved costs a push.

### What it keeps

- **What lands was gated in the form it lands** — now literally: the commit pushed is the commit
  the gate passed on. The window ADR-0027's #126 amendment could only narrow (between reading the
  target's tip and GitHub making the merge commit) does not exist for these landings.
- **A landed PR reads *merged* on GitHub**, into the target, and `Closes #n` holds (ADR-0034).
- **Recorded gate runs** (ADR-0030). A record is of a tree: the worker's pre-PR run on a branch
  that held the target's tip is a run of the tree the stack has, and the landing does not run again.
- **Who lands, and where** (ADR-0027, ADR-0036): the worker that wrote the PR, in its own worktree,
  with its context. No batch worker, no worktree of the landing's own, no marker fields: the turn
  is the single turn it was.
- **No new requirement on the target.** A stacked landing pushes to the target directly; wherever
  batches form, bootstrap already refuses a target that would not take that push (ADR-0034).

### Settling a landing cut after its push

A stacked landing can be cut where a batch's can — the target moved, the issue not yet closed, or
GitHub never showing the PR merged — and is settled by the same reads of the target, which no
longer ask whether the turn was a batch's: `afk_decide.landed_under` holds for any turn the PR
still holds, and the next cycle's release closes a PR GitHub left open with a comment naming its
commit (`afk_decide.landed_comment`).

## Consequences

- A PR that merges cleanly puts exactly one merge commit on the target's own line, and nothing on
  its branch, however often its landing is repeated. Three PRs landed one after another leave three
  merge commits and no sync merge — a test pins it.
- There is one fewer way to land where batches form: `gh pr merge` is reached there only by a PR
  that changes nothing against the target, which cannot be stacked.
- The worker's worktree is on a detached HEAD while the gate runs. A gate that reads the branch
  name would see none.
- The result of `afk land` gains `commit` (the merge commit gated and pushed); `head` stays the
  PR's own head, and `gate.head` is the commit.
- `required` and adversarial-verify landings are untouched, sync merge included: checks and
  verifications are of the PR's head, so the in-branch sync is what they need.

## Out of scope

The worker's **pre-PR** sync (ADR-0012) also merges the target into the branch, and is where the
`Merge remote-tracking branch 'origin/<base>' into <branch>` commits come from. It is what surfaces
an integration conflict inside the worker's session and what makes its pre-PR gate run a run of
"my code + current base"; this decision does not change it.

## Considered and rejected

- **Hand a single PR to a batch worker** (a real batch of one). ADR-0036 rejected it and the reason
  stands: a conflict or a red gate would be met by a worker that wrote none of the code.
- **Rebase or squash the sync merges away at landing.** The PR's head would no longer be on the
  target, so GitHub would not show it merged (ADR-0034), and a rebase re-ignites conflicts already
  resolved inside merge commits (ADR-0012).
- **Stack in a worktree of the landing's own.** The gate would run without whatever the worker's
  worktree has built up, and a second worktree per landing is one more thing to sweep.
- **Leave a red stack's branch unsynced and tell the worker to merge the target itself.** That is a
  mechanical step with one right answer, so it is code's (the build rule in `CONTEXT.md`).
