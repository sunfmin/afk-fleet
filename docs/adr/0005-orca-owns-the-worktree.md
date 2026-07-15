# Worker dispatch: orca owns the worktree; the tick never touches `git worktree`

**Status:** accepted — refines the dispatch step of [ADR-0002](0002-launcher-and-disposable-ticks.md).

## Decision

Creating and tearing down a worker's git worktree belongs to the **worker backend** (orca), not to the
**tick**. On dispatch, the tick claims the issue, fetches the base, then asks orca to create the
worktree *and* spawn the worker in one call; on merge/escalate it asks orca to remove it. The tick runs
no raw `git worktree add` / `git worktree remove`. orca also decides the branch name, and the tick
**reads it back** to fill the worker prompt rather than dictating it.

```bash
afk claim <n> --instance <id>                 # win the claim first (compare-and-swap)
git fetch origin <base_branch> --quiet        # guarantee the worker starts from latest base
orca worktree create --repo id:<repo-id> --name issue-<n>-<slug> --no-parent \
     --base-branch <base_branch> --issue <n> --agent claude --json
# → read result: the actual branch (e.g. sunfmin/issue-<n>-<slug>) and worktree path
# → fill worker-prompt.md with that real branch + path, deliver it to the spawned worker
...
orca worktree rm --worktree issue:<n> --force # cleanup on merge/escalate (issue:<n> selector)
```

orca is the **only** supported backend — there is no second implementation, so no generic
worktree-creation seam is maintained (Rule of Three: one caller does not earn an abstraction).

## Why

The prior dispatch step told the tick to `git worktree add ../wt-issue-<n> -b issue-<n>-<slug>
origin/<base>` and *then* hand the worktree to orca-cli to spawn a worker. A real run
(`gaokaowiki`, #36) exposed the contradiction: orca **cannot adopt an externally-created worktree** —
after the tick built the raw worktree, `orca worktree list` did not see it. The tick had to notice
this at runtime, `git worktree remove --force` + `git branch -D` its own work, and then let
`orca worktree create` build orca's own worktree (at `~/orca/workspaces/…`, branch
`sunfmin/issue-<n>-…`). The raw worktree was pure waste, and its cleanup relied on the tick being
clever enough to detect the mismatch — a dumber tick could have ended with two worktrees and a worker
running in the wrong one.

orca's own idiom is one atomic step — `orca worktree create --agent claude` makes the checkout, the
branch, and the agent terminal together — so worktree ownership has to sit with orca or not at all.
Passing `--issue <n>` links the worktree to the GitHub issue, making the issue number the key for
`orca worktree rm --worktree issue:<n>` at cleanup (no path/branch bookkeeping in the tick).

The branch name is orca's to set (it prefixes `<user>/`), and the tick never needs to dictate it: it
finds the PR by its `Closes #<n>` closing reference, and rebases/merges the PR's head branch as read
from `gh pr view` — never a branch name it assumed. So `branch_pattern` degrades to a *worktree-name
hint* passed to `--name`, not a promise about the branch.

`git fetch origin <base>` is kept explicitly before the create: orca is not assumed to fetch, and the
"worker starts from the latest base" invariant is cheap insurance against avoidable rebase conflicts
(the serialized rebase-before-merge is the backstop, not the first line of defence).

## Considered and rejected

- **Tick owns the worktree (raw `git worktree add`), orca only spawns into it.** The run proved orca
  does not adopt an external worktree, so this path does not work today; it would need an orca
  "adopt-this-checkout" feature that does not exist.
- **Keep a pluggable `worker` backend seam** with raw-git as the generic fallback. Rejected: only orca
  is implemented, and the whole spawn mechanism (`orca terminal wait` / `send`) is orca-specific.
  Maintaining a second, untested, currently-broken path is dead code, not flexibility.

## Consequences

- The tick's dispatch and the merge-cleanup step speak orca directly; SKILL.md carries no raw
  `git worktree add` / `git worktree remove`.
- `worktree_cleanup` is honoured via `orca worktree rm --worktree issue:<n> --force`.
- `branch_pattern` is documented as the `orca worktree create --name` hint; the real branch is
  orca's (`<user>/…`) and is read back from the create result into the worker prompt.
- The fleet stays coupled to orca as its worker backend. Supporting another backend later means
  re-introducing the seam deliberately (and earning it with a second real implementation), not keeping
  a speculative one alive now.
