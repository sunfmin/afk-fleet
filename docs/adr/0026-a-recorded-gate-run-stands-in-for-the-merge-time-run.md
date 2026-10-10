# ADR-0026 — A recorded gate run stands in for the merge-time run, and a landing names what it freed

**Status:** accepted; the record's home, key and switch are superseded by [ADR-0030](0030-a-gate-run-is-recorded-on-the-remote-under-the-tree-it-tested.md) — it is now kept on the remote under the tree it tested, always trusted, and `gate.trust_recorded_run` is gone; what stands from here is that the record is `afk`'s own, made from an exit code it saw (decisions 1, 5, 6). Amended by [ADR-0027](0027-a-worker-lands-its-own-pr-on-a-landing-turn.md) — the recorded gate run stands, now trusted by the worker's `afk land` instead of `afk merge`; the `unblocked` list of the `merged` outcome is removed with the queue it served; and amended 2026-10-05 — `gate.trust_recorded_run` is **on by default** (see *Amendment* below). Originally: an opt-in relaxation of the merge-time re-run of
[ADR-0012](0012-local-completion-gate.md), whose invariant is unchanged; and one addition to the
`merged` outcome of the merge transition ([ADR-0017](0017-the-act-half-is-transitions.md)) for the
merge queue of [ADR-0025](0025-conflicting-prs-land-one-at-a-time.md). Both are enforced in the seam
([ADR-0016](0016-the-seam-enforces-its-own-rules.md)).

## Context

After the merge queue, two costs were left on the strictly serialized merge path.

**The local gate ran twice on one commit.** ADR-0012 has the worker run `gate.local_command` after
its pre-PR sync and `afk merge` run it again after the merge-time sync, because the two can test
different trees. Often they do not: when the target has not moved since the worker synced, the
merge-time sync is a no-op and the second run tests exactly the commit the first one did. On
`sunfmin/calcgrid` one run was measured at 353 s, so the merge lane landed about ten PRs an hour at
best, and every hand-back round paid the gate twice more.

**A queued PR waited a cycle after the PR ahead of it had landed.** `afk rebuild` reports a claim
`queued` at the top of a tick; the PR it waits behind then merges in that same tick; nothing told the
tick the queued claim was free, so it waited for the next cycle — the busy interval, 90 s by default
— before it was even synced. The skill asked the tick, in prose, to remember which claim waited
behind which PR and call `afk merge` on it again, and elsewhere told it never to merge a *queued*
row: bookkeeping in a context, and two instructions for one row.

## Decision

### A green run, on record, of the exact head that lands

1. **The worker gates through the tool.** `afk gate` runs `gate.local_command` in the worktree it is
   called from, streams the log to the worker's terminal, and answers
   `{status, exit_code, head, recorded, …}`. The worker prompt — the first brief and the hand-back —
   hands the worker that whole line (`afk_decide.gate_command`), in place of the bare command.
2. **A green run is recorded by `afk`, not reported by the worker.** On green, `afk gate` writes
   `{head, command, clean, at}` to the worktree's git dir (`afk_decide.gate_record`): the commit the
   worktree was at, the command that ran, and whether the tree had nothing uncommitted or untracked
   when the run started. The record is made from an exit code the tool saw. A worker typing "the
   gate is green" proves nothing and leaves none; a worker that runs the bare command leaves none.
3. **A red or timed-out run leaves no record.** The previous record is deleted before the run
   starts, so nothing can be read as a pass that did not happen — not even an earlier green.
4. **`gate.trust_recorded_run` (default `false`) lets `afk merge` skip its own run** when — and only
   when — `afk_decide.gate_record_void` finds nothing against the record: the head that would land,
   *after* the merge-time sync, is the recorded head; the recorded command is the
   `gate.local_command` configured now, verbatim; and the run was on a clean tree. Any sync that
   moved the head, any later commit, a changed command, a dirty tree, or no record voids it, and the
   merge runs the gate as ADR-0012 says. Void is always the safe side.
5. **The outcome says which happened.** A green merge carries
   `gate: {status, source, head, command}` — `source: "recorded"` with `recorded_at`, or
   `source: "run"`, with `not_trusted: <reason>` when the option was on and a record was not enough.
6. **`gate.adversarial_verify` is untouched.** It still comes after the machine gate and still pins
   to the head, whichever way the machine gate was satisfied.
7. **Only `gate.ci: local` has a merge-time run to skip.** The key with `gate.ci: required` is a
   load-time error — a key a human sets to no effect is a config that lies to its author (ADR-0009).

### A landing names what it freed

8. **`merged` carries `unblocked`**: the claims of this fleet instance whose PRs were queued behind
   the PR that just landed and now wait behind nothing (`afk_decide.freed_by`), in merge order. A
   claim that also waits behind another handed-back PR is not listed, and neither is a peer's — the
   peer's own merge names it.
9. **A *queued* row is routed one way.** The tick does not call `afk merge` on a row `afk rebuild`
   called *queued*. It merges what a `merged` outcome lists in `unblocked`, next, in that order. It
   keeps no note of who waited behind whom.
10. **It is read before the PR is merged**, from the queue as it stood: a read that failed after
    `gh pr merge` would report an error over a merge that happened. The files of a waiting PR are
    read only when the landing PR was handed back, so an ordinary merge pays nothing.

## Why this keeps ADR-0012's invariant

ADR-0012 rejected "trust the worker's pre-PR pass; merge with no post-sync verification", because two
PRs can each be green and conflict semantically, and the serialized re-gate is the only defense. That
defense is intact. The merge-time sync still happens first; if it brings anything in, the head moves,
the record is void, and the gate runs on the combined tree. What is trusted is narrower than "the
worker's pass": one run, of one command, on one commit — and only when that commit is, byte for byte,
what lands. *What lands on the target was tested in the form it lands* still holds; it was tested
once instead of twice.

What is given up is the second run's independence on an unchanged commit. A flaky test that passed
for the worker is not asked again; a gate that depends on something outside the tree (a service, the
date) is not re-sampled at merge time. ADR-0012 calls the merge-time run the only machine gate in
`gate.ci: local`, so a repo must choose this: the default is off, and with it off nothing changes.

## Considered and rejected

- **Trust on by default.** Halves the lane's cost for everyone, and silently relaxes a guarantee
  repos adopted `gate.ci: local` on.
- **The worker reports its own result** (a marker comment, a line in the PR body). That is the
  worker's say-so. The record has to be made by the code that observed the exit code.
- **Record on GitHub** (a commit status, a PR comment, a ref). It would survive a move to another
  machine — where the environment the gate ran in is not the one the merge would have used, which is
  the thing a `local` gate is a claim about. In the worktree's git dir the record dies with the
  worktree, is never staged by the worker's `git add -A`, and a worktree recreated at merge time has
  none: the merge gates.
- **Compare trees, not heads** — trust a run whose tree equals the landing tree though the commit
  differs. A merge-time sync that changes the head changes the tree; equal trees under different
  heads is not a case worth a second rule.
- **Count only tracked changes as dirty.** A new source file the worker forgot to `git add` makes
  the gate green on a tree no commit holds. Untracked, un-ignored files void the run; a gate that
  litters must have its artifacts ignored.
- **Let `afk rebuild` re-read the queue mid-tick**, or have the tick rebuild again after each merge.
  A second full gather per landing, to learn what the merge already knows.
- **Dispatch-time avoidance of conflicting issues.** The fleet cannot know which files an issue will
  change before a worker has done it, and a `queued` or `handed_back` claim already holds its slot,
  so a conflicting backlog throttles its own dispatch. Keeping conflicting issues apart belongs in
  the backlog (`blocked_by` edges) and in the target repo (union-merged or one-entry-per-file hot
  files).

## Consequences

- With the option on and a target that has not moved, a PR's gate runs once. A hand-back round runs
  it once — the worker's — instead of twice.
- `afk` gains one subcommand that is the worker's, not the tick's. It takes its config like every
  other (ADR-0016); the prompt carries it in the line, reduced to `gate.local_command`. A config
  that changes after a worker was briefed therefore records a command the merge no longer
  recognises: void, and the merge gates.
- The worker must finish on a run made on a committed tree. `afk gate` says so in its answer
  (`recorded: false`, with the uncommitted paths), and the prompt tells the worker to commit and
  run it again.
- The record is as trustworthy as the worker's worktree: a worker could write the file by hand, as
  it could push a commit that deletes the tests. The fleet's workers are its own; the record removes
  honest mistakes — the bare command, a forgotten commit, a stale green — not malice.
- With three PRs that all rewrite one file, the third is synced in the tick the second lands.
- `afk merge` on a handed-back PR reads the changed files of each of my PRs behind it once more
  before merging: a few GitHub reads, against a busy interval saved.

## Amendment (2026-10-05) — trust is the default

`gate.trust_recorded_run` now defaults to `true`; a repo that wants the landing's own run on every
head sets it to `false`. Decision 4's "(default `false`)", the consequence "the default is off", and
the rejection of *Trust on by default* above are superseded; every other decision stands, and so
does the void rule — a sync that moved the head, a later commit, a changed command, a dirty tree or
no record still makes the landing run the gate.

Why the default moved: landings are one at a time, so the gate's run time is the fleet's
throughput, and the run the default now skips re-tests a commit that was tested minutes earlier by
the same command on the same machine. Measured on this repo, one gate run is 187 s. The cost the
rejection named — a guarantee relaxed without the repo choosing it — is real and is accepted: what
is lost is only the second sample of a flaky or environment-dependent gate on an unchanged commit,
and one line of config buys it back.

The key keeps its name. With trust the default, the load-time error for setting it outside
`gate.ci: local` is removed — the default itself would trip it in `required` mode — and the key is
simply not read there.
