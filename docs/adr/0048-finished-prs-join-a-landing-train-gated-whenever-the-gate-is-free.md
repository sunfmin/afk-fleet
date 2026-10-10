# ADR-0048 — Finished PRs join a landing train, gated whenever the gate is free

**Status:** accepted (#157) — replaces how PRs land where the local gate is the completion gate and
no adversarial verify is owed (`gate.ci: local`, empty `gate.adversarial_verify_prompt`:
`afk_decide.train_runs`). There it supersedes
[ADR-0029](0029-a-merge-batch-lands-n-prs-behind-one-gate-run.md) (the merge batch, in full),
[ADR-0046](0046-a-single-landing-stacks-on-the-target-like-a-batch-of-one.md) (the stacked single
landing, in full) and the batch half of
[ADR-0036](0036-one-landing-turn-two-kinds-of-holder.md) (one kind of holder is left). Amends
[ADR-0027](0027-a-worker-lands-its-own-pr-on-a-landing-turn.md),
[ADR-0035](0035-a-silent-landing-worker-is-restarted-onto-its-turn.md),
[ADR-0045](0045-a-landing-that-stops-on-a-conflict-or-a-red-gate-gives-its-turn-up.md) and
[ADR-0047](0047-off-the-turn-a-landing-runs-no-local-gate.md): each now describes only the configs
where a PR still lands alone on a turn — `gate.ci: required`, and any config with the adversarial
verify on — which are unchanged. The invariant of [ADR-0012](0012-local-completion-gate.md) is
kept: the commit pushed to the target is the commit a green gate run tested, and a target that
moved refuses the push. Merge-never-rebase is kept, and extended: nothing on a train is ever
rewritten.

## Context

Throughput in local mode was bounded by one thing: the gate. A gate run takes minutes, one runs at
a time on a machine (it is the whole test suite, `-n auto`), and the landing turn made every PR
wait for a turn *before* it could even find out whether it conflicted. Three costs followed.

- **A turn per PR, or a batch that was fixed when it formed.** A merge batch (ADR-0029) took the
  PRs that were ready *at the moment the turn was free*; one that finished a minute into the batch's
  gate run waited for the whole run, then for a turn of its own or the next batch.
- **Conflicts were found late and resolved twice.** A PR that conflicted with a batch's stack was
  *left out* and resolved on a single turn — against a target that the batch then moved under it.
- **Two ways to land.** A batch, and a single landing made to look like a batch of one (ADR-0046),
  each with its own marker fields, phases, worker ladder and recovery.

What the gate actually needs is a commit to test. Deciding *which* PRs that commit holds can be done
at the last moment — when the gate is free — instead of at the first.

## Decision

**Where a landing train runs, a finished PR joins one append-only line of commits ahead of the
target — the train — and the train's own worker gates the line whenever the gate is free, and
lands what it gated.**

### The train

The train is one ref on the remote, `refs/afk/train/line/<target>` (`afk_decide.train_refs`; the
`refs/heads/afk-train/…` branch layout under the fallback namespace). Its commits ahead of the
target are the train. Each PR on it is **one merge commit** — first parent the train's tip before
it, second parent the PR's head — whose message is the PR's title and `Closes #<issue>`
(`afk_decide.join_message`): the same commit a PR's own merge would have made. A line the target
already holds is no train: the next PR joins onto the target's tip.

**Nothing is recorded anywhere else.** Which PRs are on the train, and at which head, is read off
the line (`read_joins`, `on_train`): a PR is on the train when the newest join commit that names it
has its *current* head as second parent. A PR whose worker pushed again after joining is therefore
not on the train any more, and joins again with the new head — no marker has to be retracted. What
a train landed is read off the target the same way.

### Joining takes no turn

`afk land --issue <n>` — the same one command a worker always lands with — joins the train
(`_join_train`). It pushes what the worker committed, makes the join commit in the object store
(`git merge-tree` + `git commit-tree`: no worktree is touched and the branch does not move), and
pushes it as the train's new tip. **That push is the lock**: the remote takes it only as a
fast-forward, so of two PRs joining at once one wins and the other merges again onto the new tip —
seconds, not a gate run. Join order is push order.

So no PR is given a landing turn here. `afk turn --issue` refuses with the reason, the tick grants
none (`_train_plan` replaces `_turn_plan`), and a claim with an open PR reads `joining` until its
head is on the train, then `joined`. The gate is not run to join: the worker ran it before opening
the PR (below), and the train worker's run is the one that lands.

### A conflict is resolved against the train, once

A PR that does not merge onto the train's tip gets the **train's tip merged into its branch**, left
in progress for its worker: `outcome: conflict`. The worker resolves, commits, and runs the command
again. Because the train is append-only, what it resolved against is still there when it joins, so
the resolution is not made a second time when the train lands — the merge of the PR's head onto the
train is clean by construction, however long the gate takes.

### The worker gates with the train already merged in

`afk gate --train` — the line the worker's brief hands it — merges what the PR will land behind into
the branch *before* running the gate (`_train_base`), by a merge, never a rebase. The conflict is
met before the run, not after; and a green run is recorded under the tree it tested (ADR-0030),
which is the tree of the join commit when nothing joined in between. The train worker's `afk land
--train` trusts that record: **a train of one lands on its own worker's gate run, with no second
run.**

A train that is known to be red is not merged into anyone's branch. When the train worker's gate
is red it records the commit under `refs/afk/train/red/<target>`; while that commit is on the line
and not on the target, `afk gate --train` merges in the newest commit of the line *below* it that
has a green run on record, else the target. A worker's gate is never red for a reason that is not
its own.

### One train worker per repo, gating whenever the gate is free

The train is landed by `afk land --train`, run by the **train worker** in the train's worktree
(`afk-train`, linked to no issue). Each run: take the train's tip (merging it in if this worktree
holds a fix commit of its own), merge the target in if it moved from outside, push that as the new
tip, gate it — or find its tree's run on record — and push it to the target as a fast-forward.
Then every PR that commit landed is finished: board, issue closed, branch deleted once GitHub shows
the PR merged. It answers `landed` and is run again at once; `idle` when nothing is on the train.

PRs that join *while the gate runs* are simply behind the tip being gated: they land with the next
run. Nothing waits for a wave to drain and nothing is speculatively gated — there is one gate run at
a time, always of everything that had joined when it started.

- **Red.** `gate_red` lands nothing. The train worker fixes **the train** — one more commit on top
  — and runs the command again. It does not look for the PR at fault, drops nothing and reverts
  nothing; every PR of that run gets the excerpt as a comment.
- **The target moved from outside** (a human pushed, a `required`-mode PR merged): before the run it
  is merged *into* the train; during the run the fast-forward is refused — `target_moved`, nothing
  landed, run again.
- **Cut after the push.** A train worker that died between the push and the finishing left its PRs
  on the target with their issues open. The next cycle finishes them from the target itself
  (`_cut_landing`): the join commit there that closes the issue, made no earlier than the claim.

The tick keeps the train worker on the train with `afk turn --train` while a PR of this fleet is
`joined`: it starts the worker, or tells the one that stopped that more joined (`granted`), and
otherwise touches nothing (`landing`, `stopped`, `idle`).

## The questions the issue left to settle

**Join order and fairness.** Push order; no queue, no priority. `merge_order` no longer decides who
lands first here — whoever is finished first is.

**What bounds a PR that keeps conflicting.** Each round resolves only against what joined *since
the last round* — what it resolved before is on the train, or on the target, and stays resolved —
so a PR loops only while other PRs keep landing in the same files faster than it resolves, and each
of those landings is progress for the fleet. Inside one command a join lost to a faster push is
retried 8 times (`_TRAIN_PUSH_TRIES`) and then reported as an error to run again. There is no
give-up counter: a worker that goes *silent* while joining is on the ladder every worker with a PR
is on (ADR-0035) — nudged once, restarted once onto the join brief, then escalated with PR, branch
and worktree kept. Silence is the bound; a worker that keeps resolving is working.

**A silent or dead train worker.** Gone, it is started again by the next `afk turn --train`, in the
train's worktree as it stands. Stopped with a tip it was already told about, it is asked after
(`afk no-pr --train`): left while it shows life, **nudged** once (`afk nudge --train`), and silent
again the train is **abandoned** (`afk turn --abandon`): the train's refs are deleted, its worktree
cleared, and each of this fleet's PRs that was on it is marked (`abandoned=` on its turn comment —
the one use of the marker here) and its worker told to join the next train. Nothing fails and no
attempt is spent. A PR taken off an abandoned train **for the second time** is escalated to a human
instead, everything kept.

**Takeover.** The train is the repo's, not a launch's. It carries no instance id: a new fleet
instance, or a peer's, finds the line on the remote and tends it — `afk turn --train` on its first
tick with a `joined` PR of its own. A dead fleet's `joined` claims are taken over like any claim;
their PRs stay on the train. PRs of several live fleets may ride one train; each fleet settles its
own claims from the target.

**The train's worktree.** One per repo, found by name, **kept**: across trains, across launches,
across abandonment (cleared, not removed). It is what the gate's build caches live in. It is never
swept as an orphan and holds no claim and no slot.

**A red train.** Fixed forward, as above. Never bisected, never unstacked: the fix is a commit, and
every PR that was on the red run lands with it.

**A branch that holds another PR's un-landed commits.** A worker that merged the train into its
branch (`afk gate --train`, or a conflict resolution) carries the commits of PRs that have not
landed. If that train lands, they are already on the target and the join is the same. If that train
is *abandoned*, nothing is taken off the branch: the PR joins the next train with those commits
aboard, and they are gated then — in the form they land, by a run of the train they are on. The
other PRs' issues are not closed by it (only a join commit that names a PR finishes it); their own
PRs join in turn and merge as no-ops. The gate is on what lands, so the invariant holds without
tracking whose commits are whose.

**Where no train runs.** `gate.ci: required` — the PR's own checks are the gate, and GitHub merges
it — and any config with an adversarial verify — the verify is of one PR's head — land exactly as
ADR-0027 decided: one PR, one landing turn. With `gate.ci: local` and a verify, that single landing
is again sync → push → gate → `gh pr merge` (ADR-0046's stacking is gone with the batch).

## How many gate runs

Five PRs, two of which conflict
(`test_five_prs_two_of_them_conflicting_land_behind_six_gate_runs_not_nine`): five worker runs,
plus **one** train run — six. The path this replaces took nine: five worker runs, one batch run for
the three that stacked, a single turn's run for each of the two left out, and a run for the second
resolution of the later one. A PR alone costs one run in total where it cost two.

## Consequences

- Removed: `afk turn --batch`, `afk land --batch`, `afk no-pr --batch`, the batch brief, the
  `batch` / `members` / `phase` / `unbatched` / `of` fields of the turn marker, the claim statuses
  that named a batch, the `afk-batch-…` branches and worktrees, `batch_candidates`.
- Added: `afk land --train`, `afk turn --train`, `afk gate --train`, `afk no-pr --train`,
  `afk nudge --train`; statuses `joining` and `joined`; the train brief and the join brief; the two
  train refs.
- The target's history in these configs is one merge commit per PR and nothing else — no sync
  merges on the target (what ADR-0046 was for), though a PR's branch may hold merges of the train.
- A direct push to the target must be allowed, as it was for a batch: `afk bootstrap` refuses a
  train config whose target would not take one.
- A red gate is paid by everyone on that run until the train worker fixes it. That is the price of
  one run for many; it is bounded by the train worker's ladder.

## Rejected

- **A turn to join.** Serialises what the remote already serialises, and makes a join cost a tick.
- **A marker on each PR saying it joined.** A second copy of what the line says, wrong the moment a
  worker pushes again.
- **Bisecting or dropping the PR at fault from a red train.** Rewrites the train — every resolution
  made against it is void — and costs gate runs to find what one fix commit removes.
- **Speculative gating of several prefixes at once.** One machine has one gate.
- **A new train worktree per train.** Throws the build cache away on every landing.
