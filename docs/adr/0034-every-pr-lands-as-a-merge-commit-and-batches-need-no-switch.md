# ADR-0034 — Every PR lands as a merge commit, and merge batches need no switch

**Status:** accepted — removes two config keys, `merge.strategy` and `merge.batch`. Amends
[ADR-0029](0029-a-merge-batch-lands-n-prs-behind-one-gate-run.md): a batch is no longer opt-in, its
stack is made of merge commits instead of squash commits, and a batched PR reads *merged* on GitHub
instead of *closed*. The batch's turn, its worker, its one gate run, the fast-forward push as the
only lock, and everything about leaving, dissolving and abandoning a batch are unchanged.
[ADR-0027](0027-a-worker-lands-its-own-pr-on-a-landing-turn.md) is unchanged but for the flag
`afk land` passes to `gh pr merge`.

**Amended by [ADR-0046](0046-a-single-landing-stacks-on-the-target-like-a-batch-of-one.md) (#154):** where batches
form, a PR landing alone is stacked like a batch member, not merged with `gh pr merge`; "Alone:"
below holds where no batch forms.

## Context

Two things were options that did not need to be.

**`merge.batch` was off by default**, so a repo whose gate takes minutes landed its finished PRs
one gate run at a time unless someone knew to turn it on. On a live fleet with five workers and a
three-minute gate, a PR opened at 13:49 waited behind three others and got its landing turn half
an hour later. The reason it was opt-in — "what the gate proves is the stack, not each PR alone" —
is true of any landing order: a PR that lands alone is gated on a target that already holds
everything landed before it, never on the base it was written against.

**`merge.strategy` let a repo choose squash, merge or rebase**, and the batch chose squash for
everyone: one squash commit per PR keeps the target's history at one commit per PR. But a squash
commit is not the PR's head, so GitHub cannot tell the PR landed. A batched PR was therefore
*closed*, with a comment naming the commit, and its issue was closed by the command rather than by
`Closes #n`. A reader of the PR list saw finished work as closed PRs; whether a PR read *merged* or
*closed* depended on whether it happened to land alone.

Keeping squash and still getting *merged* was looked at: open a branch for the batch, point each
PR's base at it, and merge the PRs there through GitHub. It works, but a merge through GitHub cannot
be undone, so it must happen after the gate; the PR then reads "merged into `<batch branch>`", not
into the target; `Closes #n` does not fire off the default branch; and a PR that conflicts with the
stack must have the resolution pushed to its own branch first. Four mechanisms to keep one property
of the history.

## Decision

**A PR lands as a merge commit — alone or in a batch — and there is nothing to configure.**

- **Alone:** `afk land` runs `gh pr merge --merge`. `merge.strategy` is removed.
- **In a batch:** the batch worker stacks each member on the target's tip with one merge commit
  (`git merge --no-ff`), whose second parent is the PR's own head and whose message is the PR's
  title, `(#<pr>)` and `Closes #<issue>`. The stack is gated once and pushed to the target as a
  fast-forward, as before. Each PR's head is then on the target, so **GitHub marks the PR merged by
  itself**. The stack is read back along its first-parent line: merge commits are the members, and
  anything else on that line is a fix commit.
- **Batches form wherever they can** (`afk_decide.batches_form`): `gate.ci: local` with
  `gate.adversarial_verify` off. `merge.batch` is removed. With `required`, or with the adversarial
  verify on, no batch forms, as before — those were never a matter of the switch.
- **Both keys are refused at load time** with the reason and "delete the key"
  (`CONFIG_REMOVED`, ADR-0009): a config that still says `squash` or `batch: false` would otherwise
  describe a fleet that no longer exists.

### Finishing a batched PR

GitHub marks the PR merged a moment after the push, not in the push — about four seconds, measured
on a throwaway PR in this repo, with the pushed merge commit recorded as the PR's merge commit. So
the landing does two things
in order: it closes each member's issue itself (as before — that is what settles the claim), and it
**waits** for the PRs to leave the open list (`--merged-timeout`, 60 s) before deleting their
branches. A branch deleted first would close its PR instead. A PR still open when the wait runs out
is left with its branch; the next cycle's release closes it with a comment naming its commit if it
is open even then — which is also what happens to a PR whose head moved after it was stacked, since
that head is not on the target.

### What bootstrap checks

A batch lands by pushing, so a target that refuses a direct push — required pull request reviews,
push restrictions, a lock — is an error at bootstrap wherever batches form. That check used to
apply only with `merge.batch` on.

## Consequences

- A landed PR always reads *merged* on GitHub, into the target, and `Closes #n` works for it.
- **The target's history is longer.** Every PR contributes a merge commit plus its own commits and
  its sync merges. The per-PR view is `git log --first-parent`; a PR is reverted with
  `git revert -m 1 <merge>`; a bisect wants `--first-parent`, since a PR's own commits were never
  gated one by one.
- A repo in `gate.ci: local` whose target refuses a direct push, and which ran with the old default
  (`merge.batch: false`), is now stopped at bootstrap. It lifts the protection or uses
  `gate.ci: required`.
- A config file, or a `--set`, that carries either key fails loudly until the key is deleted. The
  canonical config a launcher already holds and passes back as `--config` is not read that
  strictly: a fleet running across the upgrade carries the two keys on, ignored, and from its next
  cycle lands by merge commit and forms batches.
- Rejected: **keeping `merge.strategy` with `merge` as the default.** A batch would then need a
  squash path and a merge path, and a batched PR would read closed again for anyone who chose
  squash — the difference this decision exists to remove.
