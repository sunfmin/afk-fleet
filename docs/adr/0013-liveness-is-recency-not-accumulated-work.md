# ADR-0013 — Liveness is recency, not accumulated work

**Status:** accepted
**Supersedes:** the `classify_no_pr` "real progress beats idle+verdict" rule (unrecorded; it lived
only in `afk_decide.classify_no_pr`, its unit test, and the SKILL.md bullet).

## Context

A `no_pr` claim — I own the issue, the worker is running, no PR exists yet — is the one state a
binary liveness probe cannot resolve: a worker that ran to completion, concluded there was nothing to
PR, and went idle looks *identical* to one still coding. `classify_no_pr` therefore joins three
signals (git progress, the worker's `afk:verdict` marker, the orca liveness probe) into a five-way
verdict.

Its `coding` branch was:

```python
made_progress = commits_ahead > 0 or dirty
if terminal_idle is False or made_progress or within_grace:
    return coding / leave
```

with the comment *"any positive sign of life wins over the idle+verdict path"* and a unit test
asserting `commits_ahead=2, terminal idle, idle_seconds=9999` ⇒ `coding`. So the behaviour was
deliberate, not a slip.

**It is wrong, because `commits_ahead` and `dirty` are not signs of life.** They are *monotonic*:
once a worker lands one commit, `commits_ahead > 0` holds until the branch merges, and `dirty` holds
forever if the worker died mid-edit. Neither ever becomes false while the claim exists. So the
predicate short-circuited to `coding` on **every future tick**, making `idle_done`, `idle_blocked`
and `idle_failed` unreachable for any worker that had ever committed.

Observed live in `sunfmin/gaokaowiki` #139: `commits_ahead=4`, `dirty=false`, terminal connected but
`tui-idle`, **33 minutes** past its last commit and last worktree write, no PR, no `afk:verdict`
marker. Textbook `idle_failed` ("no verdict at all after grace"). It classified `coding`, and would
have on every subsequent tick — claim held indefinitely, retry ladder never entered, the issue
silently starved.

This is the mirror image of the `awaiting_ci` deadlock ADR-0012 removed: same outcome (a claim parked
forever on a state that cannot change), reached from the opposite direction.

## Decision

**Only live signals may produce `coding`:** a busy terminal, or observed activity inside
`worker_idle_grace_seconds`.

`idle_seconds` is already defined as the max-recency of `last_commit_ts` / `worktree_mtime_ts` /
terminal activity — so *recent* commits are still counted, via the grace window. What is dropped is
only the standing, recency-free part, which is exactly the bug.

`has_changes` (the renamed `made_progress`) survives for one narrow job: **refuting a verdict.** An
`already-satisfied` phase asserts nothing needed doing; work sitting on the branch contradicts that.
Trust the branch, not the claim — route it to `idle_failed` rather than closing the issue on a diff
the tick would then fail to verify as empty.

## Consequences

- A worker that commits and then dies or silently finishes now reaches the retry ladder after
  `worker_idle_grace_seconds` instead of holding its claim forever.
- The grace window becomes load-bearing where it previously was decorative for committing workers.
  Set it above a worker's longest plausible quiet stretch (thinking, a long build, a slow tool) or
  live work will be torn down mid-flight. The default 300s suits workers that commit incrementally;
  raise it for tickets whose workers go quiet for long stretches.
- A worker that commits, goes idle past grace, and *does* leave an honest `giving-up` or `blocked`
  verdict is routed on the verdict, as originally intended — that path was simply dead before.
- The retry keeps the claim and hands the fresh worker the branch as-is, so the accumulated commits
  are not lost; the new worker continues from them.

## Alternatives rejected

- **Require a verdict before any retry.** Would leave a silently-dead worker parked forever, which is
  the failure being fixed.
- **Treat `has_changes` as live but expire it by `last_commit_ts`.** That is precisely the grace
  window, expressed twice; `idle_seconds` already folds `last_commit_ts` in.
- **Shorten the grace window instead.** Does not help: the predicate never reached the grace test
  for a worker with commits.
