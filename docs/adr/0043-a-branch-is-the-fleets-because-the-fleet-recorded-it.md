# ADR-0043 — A branch is the fleet's because the fleet recorded it

**Status:** accepted — refines the tier-2 signal of
[ADR-0011](0011-takeover-and-progress-preservation.md) and the discard of
[ADR-0017](0017-the-act-half-is-transitions.md)'s retry. The record is one more kind kept in a
comment ([ADR-0032](0032-records-kept-in-comments-share-the-encoding-of-records-on-refs.md)).

## Context

orca names a worker's branch ([ADR-0005](0005-orca-owns-the-worktree.md)); the claim ref records the
issue, not the branch. So when no worktree survived, the fleet found an issue's branch on the remote
by its shape: any `<prefix>/issue-<n>-<anything>`. Continuation read that set, and so did the discard
of a failed attempt — which deleted every branch in it and closed every PR that closed the issue from
one. A person's `hotfix/issue-1-my-manual-fix` has that shape. A test showed both: the branch
deleted, their PR closed. The rule stated beside that code was "the fleet closes only what the fleet
opened".

## Decision

1. **The fleet records each branch it has cut, on the issue.** When orca has cut a worktree for an
   issue, the fleet posts one comment on the issue carrying `<!--afk:branch name=<branch>-->`
   (`afk_decide.BRANCH_RECORD`), with the name orca reported. A recreated worktree gets a new name
   from orca and so a new record.
2. **An issue's own branches are the recorded ones, plus the branch of the worktree orca links to
   the issue on this machine.** That worktree was already the attempt's — tier 1 continues in it
   and a discard removes it — and counting its branch is what keeps an attempt started before this
   ADR discardable where it runs.
3. **Nothing is read off a branch's name.** Continuation (tier 2) picks among the issue's own
   branches; a discard deletes those and closes the PRs opened from them, and touches no other.

## Considered and rejected

- **A tighter pattern** (the exact slug of the issue's title, orca's `-<k>` suffix). Still a guess
  from a name anyone can give a branch, and it loses the fleet's own branch when the title is edited.
- **A ref per branch under `refs/afk/`.** A second layout for the `refs/heads` fallback namespace,
  and a lifetime to manage; the comment lives and dies with the issue and is rewritten by nobody.
- **A field of the status board's marker.** The board is rendered whole from the phase by every
  writer; each would have to read the names back first to carry them over.
- **The branch in the claim record.** The claim is won before the worktree exists, and rewriting it
  moves the sha every lease is taken against.

## Consequences

- An issue carries one short comment per worktree cut for it, beside its status board.
- `afk recovery` reads the issue, so it takes `--repo` like every subcommand that asks gh.
- A branch pushed by an attempt started before this ADR, whose worktree is no longer on the machine
  that looks, is nobody's: it is not continued from (the claim restarts from base) and not deleted,
  and a PR opened from it is not closed by a retry. A human deletes it, or records it by posting the
  marker on the issue.
