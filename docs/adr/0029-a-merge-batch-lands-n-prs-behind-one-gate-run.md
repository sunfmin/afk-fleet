# ADR-0029 — A merge batch lands N ready PRs behind one gate run

**Status:** accepted — opt-in (`merge.batch`, default off, `gate.ci: local` only). Relaxes, per
batch, the reading of [ADR-0012](0012-local-completion-gate.md)'s invariant that the gate proves
*a PR*: with the option on it proves *a stack*. Extends
[ADR-0027](0027-a-worker-lands-its-own-pr-on-a-landing-turn.md): the landing turn is still one at a
time, but it may now be held by several PRs at once, landed by a worker that wrote none of them.
With the option off, or with one eligible PR, ADR-0027 is unchanged. Reuses the silent-worker ladder
of [ADR-0018](0018-nudge-a-silent-worker-before-failing-it.md), the continuation tiers of
[ADR-0011](0011-takeover-and-progress-preservation.md) and the recorded-nothing rule of
[ADR-0026](0026-a-recorded-gate-run-stands-in-for-the-merge-time-run.md) (a batch never trusts a
recorded run: no record is of a stack).

## Context

Landing turns go out one at a time, and each landing runs the gate on its own synced head. So the
gate's run time is the fleet's landing throughput: N finished PRs cost N gate runs, end to end, and
with a gate of several minutes the PRs a fleet finishes in parallel queue up behind it. ADR-0026
removed the second run *of an unchanged commit*; it does nothing for the common case, where every
landing after the first has a head its sync just moved.

Most of those runs prove the same thing. PRs that do not conflict could have been tested together:
if the target with all of them applied is green, each of them may land.

## Decision

**With `merge.batch` on, when two or more finished PRs wait for the landing turn, the turn goes to a
merge batch: one batch worker stacks them on the target, gates the stack once, and pushes the stack
to the target.**

### What a batch is

A **merge batch** is one landing turn held by several PRs of one fleet instance. Its record is the
turn marker, the same single comment, on **every** member PR:

```
<!--afk:turn instance=<id> at=<epoch> batch=<batch> members=<issue>:<pr>,… phase=<stacking|gating|fixing>-->
```

There is no other record. `afk rebuild` reads a batch back from those markers: each member's row is
`landing` with `batch: {id, members, phase}`, and `batches` lists every batch a PR of my claims is
in. A member's own branch, worktree and worker are not touched, and its worker is told nothing.

### Who is batched — the cycle's decision, in code

`afk_decide.batch_candidates` is the whole batch-or-single decision, and fixture tests cover it; no
skill prose re-derives it. A batch is formed only when

- `merge.batch` is on and `gate.ci` is `local` (a config error otherwise — `required` has no gate
  run to share, and GitHub's own merge queue is the tool there);
- no turn is out — while one PR holds the turn no batch forms, and while a batch holds it nothing
  else is granted;
- `gate.adversarial_verify` is off — with it every PR owes a verify of its own head, so none is
  eligible;
- no PR that already left a batch is waiting (those go first, on single turns);
- and at least two of my claims' ready PRs remain after dropping the ones whose own worker is still
  working (its PR may yet move). A peer fleet's PR is never mine to batch.

Otherwise the turn goes to one PR, exactly as in ADR-0027.

### The batch worker, and why stacking and gating are its and not the cycle's

`afk turn --batch` records the markers, has orca create a worktree **of the batch's own** at the
target's tip (named `afk-batch-<id>`, linked to no issue) and starts a **batch worker** in it on a
batch brief. The batch worker holds no claim and takes no dispatch slot.

Stacking and gating belong to a worker for the reason landing did in ADR-0027: a gate run takes
minutes and a red one needs someone with a worktree and a context to repair it. A cycle is a short
pass, run in code, that runs no gate and holds no worktree; putting the batch's gate in it would bring
back the multi-minute tick ADR-0027 removed, and leave a red stack with nobody to fix it.

### `afk land --batch <batch>` — one command, the same every time

Run by the batch worker in the batch's worktree. Exit 3, nothing changed, unless every member PR
carries the batch's marker naming the fleet instance that holds its issue's claim. Then:

1. **Stack.** Reset to the target's tip and put each member on it in merge order, **one squash
   commit per PR** — subject `<PR title> (#<pr>)`, body `Closes #<issue>`, the PR's last author. A
   PR that conflicts with the stack so far is **left out**. Fix commits the worktree already
   carried are put back on top.
2. **Push** the stack to the batch's own branch — durable progress, so a batch can be continued on
   another machine.
3. **Gate** — `gate.local_command`, **once**, on the stack.
4. **Land** — check the markers again (a gate run is long), then push the stack to the target as a
   **fast-forward**.
5. **Finish** each member: status board, issue closed, PR closed with a comment naming the commit
   that landed it; the batch's branch deleted.

| outcome | the batch worker |
|---|---|
| `landed` — `landed`, `left_out`, `gate_runs`, `fix_commits` | wakes the launcher and stops |
| `gate_red` — nothing landed | fixes the stack with one more commit on top, runs it again |
| `target_moved` — the push was refused, nothing landed | runs it again: re-stacked, re-gated |
| `too_small` — fewer than two PRs stacked; the batch is dissolved | wakes the launcher and stops |

The stack is rebuilt from the target's tip on every run, so the run after a red gate and the run
after the target moved are the same run. The claims and the worktrees — the members' and the
batch's — are settled by the next cycle, from the claims whose issues are now closed.

### The gate proves a stack, not a PR

ADR-0012's invariant is *what lands on the target was gated in the form it lands.* A batch keeps it
literally: the commit the target is moved to is the commit the gate passed on. What it gives up is
the finer claim that each PR was gated **alone** on the target: the intermediate squash commits of
a stack were never gated individually, so a `git bisect` may land on one that is red by itself.
That is the trade the option buys — one run instead of N — and why it is opt-in, off by default,
and decided per repo.

### Squash commits, and why a batched PR reads closed

Each PR lands as one squash commit, shaped like the one `gh pr merge --squash` writes, so the
target's history is the same with or without batching: one commit per PR, in merge order, each
naming its PR. But the commit is **pushed**, not merged through GitHub, and GitHub marks a PR
*merged* only when its own head (or its own merge) reaches the target. A squash commit is neither,
so a batched PR is **closed**, with a comment naming the commit that landed it, and its issue is
closed by the command rather than by `Closes #n`. The alternative — pushing each PR's real head, so
GitHub shows "merged" — would put every PR's work-in-progress commits and sync merges on the target
and make the history depend on whether batching was on.

`afk rebuild` therefore reports a batched PR like any landed one: its issue is closed, so its claim
is a `closed` row; none of the batch's PRs is open, failed or abandoned.

### The fast-forward push is the lock

Nothing is held on the target while the batch gates. The push in step 4 names the stack's head and
is a plain, non-forced push: git accepts it only if the target is still the tip the stack was built
on. If anything landed meanwhile the push is refused, nothing lands (`target_moved`), and the next
run stacks on the new tip and gates again. So the target is never at a commit the gate did not pass
on, with no lock to leak and nothing for a crashed worker to release.

It also means the target must accept a direct push from the fleet's credential. `afk probe`
**hard-errors at bootstrap** when `merge.batch` is on and the target's protection requires pull
request reviews, restricts who may push, or is locked — after a batch has spent its gate run is too
late to find out.

### A red batch is repaired, not bisected

When the gate is red on the stack the batch worker fixes the stack with **one more commit on top**
and runs the command again; the batch lands with the fix commit. It does not hunt for the PR at
fault. Bisecting costs log₂N further gate runs to name a culprit — and then the culprit still has
to be fixed, by a worker that must reload the context the batch worker already has in front of it:
the red log, and the combined tree it is red on. Most red stacks are integration breaks between two
PRs that are each fine alone, where "the PR at fault" has no answer at all. One fix commit and one
more gate run is the cheapest path to a green target, and the fix commit records what the
combination needed.

### A PR that leaves a batch is never batched again

Two ways out, both recorded by replacing the PR's marker with one that holds no turn:

```
<!--afk:turn instance=<id> at=<epoch> unbatched=<left_out|abandoned|dissolved> of=<batch> released=1-->
```

- **Left out** — it conflicted with the stack. The rest of the batch lands without it.
- **Abandoned** / **dissolved** — the batch itself ended with nothing landed: its worker stayed
  silent after its nudge (`afk turn --abandon`), or fewer than two PRs could be stacked.

Such a PR goes to the head of the merge queue, takes a **single** landing turn, and its own worker
lands it — resolving its conflict once, against a target that already holds what it conflicted with.
`unbatched` stays on its later markers, and `batch_candidates` forms no batch while one waits. Never
batching it again is what bounds the worst case: a PR that conflicts with whatever it is stacked on,
or a batch brief no worker gets through, would otherwise go round forever, spending a gate run each
time. One failed batching costs a PR at most one wasted turn; after that it is on the path that is
known to terminate.

### What bounds a batch

The ladder that already exists, asked of the batch's worktree (`afk no-pr --batch`): busy, or within
grace of its turn or of its last `afk land --batch`, it is left; with no terminal it is
**continued** — a new batch worker in the batch's worktree if it is on this machine, else in one cut
from the batch's pushed branch; silent past grace it is nudged once; silent again the batch is
abandoned. No attempt is spent by any of this, and no PR is failed: abandoning a batch is not a
failure of the PRs in it.

A batch recorded by a fleet that died is not continued by the fleet that takes its claims — the
markers name the dead instance, so `afk land --batch` refuses — it is abandoned, and the PRs land on
the new owner's single turns.

## Consequences

- N non-conflicting PRs land behind one gate run. Five PRs of which three rewrite one file cost
  three runs (one batch of three, two single turns) instead of five, with the same two conflict
  resolutions.
- The target's history is unchanged in shape; batched PRs read *closed*, not *merged*, on GitHub.
- The intermediate commits of a stack are not individually gated.
- The target must accept the fleet's direct push; repos that require PR reviews on the target cannot
  turn the option on.
- A second kind of worker exists — one with no issue, no claim and no slot — and a second worktree
  name the tick sweeps. Both are opt-in with the option.
- `gate_runs` in the `landed` outcome counts the runs made in the batch's current worktree; a batch
  continued on another machine starts that count again.
