# ADR-0037 — A settled landing is fast-forwarded into the fleet's own checkout

**Status:** accepted.

## Context

A worker lands its PR on the remote ([ADR-0027](0027-a-worker-lands-its-own-pr-on-a-landing-turn.md)),
and every worktree is cut from the remote's tip, so the fleet itself never needed the local
`merge.target` branch to be current. The human did: after a run, the checkout the launcher was
started in — the one they read, build and install from — was as many merges behind as the fleet had
landed, and "pull the target" was a step somebody had to remember after every PR.

## Decision

When the tick settles a landed claim (`_settle_landed`: the release of a claim whose issue is
closed), it also fast-forwards the local `merge.target` branch of the checkout orca cuts worktrees
from to the remote's tip (`_sync_checkout`), and reports it as `synced` on the release.

- **Only ever a fast-forward.** Checked out, the branch is merged `--ff-only`; not checked out, its
  ref is moved by a fetch, which refuses anything else. Uncommitted changes the landing does not
  touch stay as they are.
- **Soft.** A local branch that diverged, changes in the way, no local branch of that name, no orca:
  each is `{"skipped": why}` and nothing is changed. A checkout that cannot follow never fails the
  release it rides on.
- **The remote-tracking ref follows too**, for every remote of the checkout that is the target repo
  (`afk_decide.remotes_of`) — the fleet fetches by URL, which would otherwise leave the branch
  reading as "ahead of origin".
- **No config key.** It is what the fleet does: a fast-forward that is skipped whenever it could
  cost anything leaves nothing for a switch to protect.

## Consequences

- It happens once per settled landing, in the cycle after the merge — not at the merge, which runs
  in a worker's worktree and holds no instance id.
- The members of a merge batch are settled one by one; the first sync brings all of them and the
  rest find nothing to move.
- Nothing downstream of the pull is run: installing or deploying what landed stays a human step.
