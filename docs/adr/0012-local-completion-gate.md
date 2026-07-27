# Local completion gate: CI is deferred to a merge-time local re-run

**Status:** accepted — adds a `local` mode to the completion gate (previously: wait for GitHub
checks). Composes with [ADR-0011](0011-takeover-and-progress-preservation.md)'s incremental-push
progress preservation and reuses its continuation tier-2 move for merge-time worktree recreation.

## Context

Two forces made remote CI the fleet's bottleneck:

- **CI runs far too often.** ADR-0011 has every worker push its branch after each completed step —
  progress preservation, not a CI signal. But the target repo's `on: push` / `on: pull_request`
  workflows fire on every one of those pushes: a worker may push a dozen times per issue, and the
  fleet reads only the final run's result. The rest burn queue capacity — and with heavy CI, that
  congestion is exactly what makes the *gate-relevant* run slow when the tick finally waits for it.
- **A full remote run sits on the serialized merge path.** The merge procedure was: rebase onto
  latest target → force-push → *wait for a fresh CI run* → merge. One heavy CI run per merge,
  serialized, with the fleet idle-watching.

Meanwhile `gate.local_command` already existed as the worker's optional pre-PR filter, and the whole
fleet — launcher, ticks, worktrees — runs on machines that can execute it. When the local command is
CI-equivalent, the remote run verifies nothing the local run didn't.

## Decision

1. **`gate.ci: local` — the local command *is* the completion gate.** New enum value alongside
   `required` (the default; existing repos see zero change). In `local` mode GitHub checks are never
   read: the **worker** syncs its base into the branch, pushes, runs the local gate, and only then
   opens the PR. Config validation rejects `ci: local` with an empty `local_command` at load time —
   the illegal state is unrepresentable.

2. **Merge-time re-run preserves the invariant.** The tick's merge procedure becomes: **sync** →
   push → re-run the local gate in the branch's worktree → merge. The worker's pre-PR pass tested
   pre-sync code; parallel PRs can each be locally green yet conflict semantically, so the gate
   must cover the exact tree that lands: *what lands on the target branch was tested in the form it
   lands.* If no worktree survives locally at merge time (a **takeover** from another machine,
   stray cleanup), the tick recreates one from the pushed branch tip — the continuation tier-2 move
   — and disposes of it after. A red merge-time run posts its log excerpt as a PR comment, keeping
   the rule that a retry's failure reason is re-read from where it lives.

3. **Sync replaces rebase on the merge path — one verb, both ends.** Once workers merge base into
   their branches pre-PR, rebase is disqualified: it drops merge commits and replays the branch's
   own commits, re-igniting conflicts whose resolutions lived only in the dropped merge. Merge
   commits are harmless under squash-merge — the target-branch history is identical. So both the
   worker's pre-PR catch-up and the tick's merge-time catch-up are `git merge origin/<base>`, and
   `rebase_before_merge` becomes `sync_before_merge` (renamed key; old files fail schema validation
   loudly, with a migration note).

4. **Incompatible branch protection is a bootstrap error, not a runtime surprise.** A target branch
   whose protection *requires status checks* rejects `gh pr merge` no matter how green the local
   gate is. The bootstrap probe checks protection when `ci: local` is configured: required checks
   present → hard error with the human present (the ADR-0009 tradition: fix it here, don't guess);
   probe itself inconclusive → warn and continue. Repos adopting this mode are expected to scope
   remote CI away from worker branches (e.g. `on: push` to the target only) — the fleet can't edit
   their workflows, so this lives in the config template's guidance.

## Why the worker syncs before opening the PR

Integration conflicts have two possible venues: the worker's own session (the author is present,
context is loaded, the fix is cheap) or the tick's serialized merge point (the author is gone, the
queue is blocked, and a failure enters the retry ladder — the fleet's most expensive event). The
pre-PR sync forces the cheap venue for everything that has drifted during the worker's session; the
merge-time sync remains the unavoidable venue for whatever drifted after. The same merge verb at
both ends keeps one code path.

## Considered and rejected

- **Trust the worker's pre-PR pass; merge with no post-sync verification.** Fastest, but semantic
  conflicts between parallel green PRs would land on the target silently — the serialized re-gate is
  the fleet's only defense against them, and the mandate ends at merge, so nobody is left to notice.
- **Trigger remote CI at gate time only** (`workflow_dispatch`, draft-PR-then-ready tricks). Halves
  the waste but keeps a full remote run on the serialized merge path; a local re-run is strictly
  faster whenever the environments agree, and when they don't the repo should stay on `required`.
- **Keep remote CI on the target branch as a post-merge backstop.** The fleet's mandate ends at a
  green merge; a red target branch afterward has no owner. Repos remain free to run `on: push` CI on
  their main branch for their own purposes — it is simply not the fleet's gate.
- **`gh pr merge --admin` to bypass required checks.** `--admin` bypasses *all* protections,
  including human-review requirements — far too much power for an unattended fleet. Rejected in
  favor of the bootstrap-time hard error.
- **Rebase for `required` mode, merge for `local` mode.** Two code paths for one verb, and the
  rebase path is already broken for any branch carrying merge commits — which the worker prompt now
  produces regardless of mode.

## Consequences

- **Environment parity moves to the repo.** `ci: local` is a claim by the repo that its
  `local_command` is CI-equivalent. Repos whose CI is environment-sensitive (Linux-only toolchains,
  service containers, secrets) should stay on `required`.
- **Worker prompt:** the pre-PR step becomes sync → push → local gate → open PR. Incremental pushes
  (ADR-0011) are unchanged — they were never for CI.
- **`afk` tool:** the merge-time gate run is mechanics (ADR-0004) — a small tool that runs the
  configured command in a worktree and returns `{status, excerpt}`, mirroring the ephemeral
  sub-read that reads CI logs in `required` mode. The bootstrap probe gains a branch-protection
  check.
- **Rebuild classification:** in `local` mode there are no PR checks to subclassify; gating is an
  *action* the tick takes at merge time, not an observation it waits on.
- **Retry ladder unchanged:** a red merge-time local gate is "gate red," the existing category.
