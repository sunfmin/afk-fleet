# ADR-0030 — A gate run is recorded on the remote, under the tree it tested

**Status:** accepted. Supersedes the *where* and the *key* of
[ADR-0026](0026-a-recorded-gate-run-stands-in-for-the-merge-time-run.md)'s recorded gate run — its
decisions 2–4 and 7, its amendment's config key, and two of its rejections (*Record on GitHub*,
*Compare trees, not heads*). Removes the rule of
[ADR-0029](0029-a-merge-batch-lands-n-prs-behind-one-gate-run.md) that a merge batch never trusts a
recorded run. The invariant of [ADR-0012](0012-local-completion-gate.md) is unchanged: *what lands
on the target branch was tested in the form it lands.*

## Context

ADR-0026 let a landing skip its own run of the local gate when a green run was on record for the
exact head that would land. The record was a file in the worktree's git dir, keyed by commit. So it
was lost whenever the landing happened somewhere other than where the gate ran: a worktree recreated
from the pushed branch (continuation, tier 2), another machine (a takeover), another commit holding
the same content. Each of those paid one more run of a gate measured at 187 s serial on this repo,
on the one path that is strictly one at a time.

ADR-0026 kept the record local on purpose: on another machine "the environment the gate ran in is
not the one the merge would have used, which is the thing a `local` gate is a claim about."

## Decision

1. **The record is of a tree, not a commit.** A gate run tests file content; the commit sha is a
   proxy for it. A record says: *this command passed on this tree.* Any commit holding that tree —
   a reworded commit, a rebuilt stack — is as tested as the one that was gated.
2. **It lives on the remote, one ref per record, and the ref's name is the key:**
   `refs/afk/gate/<tree>-<hash of the command>` (`afk_decide.gate_record_ref`). The ref points at a
   parentless commit of the tested tree whose message carries `{tree, command, at}` (written as every record kept on a ref is:
   [ADR-0031](0031-records-kept-on-refs-share-one-encoding.md)). Asking "was this
   tested green?" is one fetch of one ref; writing is one push; two runs never contend for a ref.
   Nothing is kept in the worktree.
3. **A green run anywhere is believed everywhere.** The gate is a claim about content. Remote CI
   also runs on a machine that is not the one that merges, and nobody distrusts it for that.
4. **Only a green run on a committed tree is recorded** — by `afk gate`, by `afk land`, by a batch's
   landing alike. A run over uncommitted or untracked files tested a tree no commit holds.
5. **The latest run of a tree is the one believed.** A red or timed-out run on a committed tree
   deletes that tree's record: same content, same command, green then red is a flaky or
   environment-dependent gate, and void is the safe side.
6. **A record is believed for one day** (`afk_decide.GATE_RECORD_TTL`, not configurable). Age is
   checked when the record is read, so nothing depends on cleanup having run; the bootstrap probe
   sweeps the expired ones. A tree-keyed record belongs to no PR, so age is the only lifetime it
   can have — and it bounds how long an environment-dependent green is believed elsewhere.
7. **A record is always trusted; `gate.trust_recorded_run` is removed.** A config that still carries
   the key fails at load with a note, like a renamed key ([ADR-0009](0009-one-home-for-config.md)).
8. **A merge batch follows the same rule.** The stack's tree is looked up before the gate runs and
   recorded when it passes. It seldom hits — a re-stack on a moved target is a new tree — but one
   rule ("was this tree tested green by this command?") replaces a rule and its exception.
9. **A record that cannot be written is not an error.** `afk gate` answers green with
   `recorded: false` and the reason; the landing runs the gate itself. The bootstrap probe says so
   once, as a warning, when the remote refuses `refs/afk/gate/*` (`gate_records`).

## Why this keeps ADR-0012's invariant

The landing's sync still comes first. If it brings anything in, the tree that would land is a new
tree with no record, and the gate runs on the combined tree — the defense against two PRs that are
each green and break each other is intact. What is trusted is one run, of one command, on the tree
that lands, byte for byte.

## Amendment (2026-10-10) — a landing merges only on a run of the committed tree

Decision 4 kept an off-commit run out of the *record*; the landing that made the run still merged on
it, and "committed" was measured only before the run. Both holes let a tree no commit holds stand
behind a landing ([#109](https://github.com/sunfmin/afk-fleet/issues/109)). Now one rule, in one
place (`_run_and_record_gate`), decides both what is recorded and what a landing accepts:

- **A run is of the committed tree only when the worktree is exactly its commit before the run and
  after it** — nothing uncommitted, nothing untracked, HEAD where it was. A gate that rewrites a
  tracked file (a fixer), commits, or leaves an un-ignored artifact passed on what it left, not on
  what is committed.
- **`afk land` and `afk land --batch` refuse any other run** — an error naming the paths, nothing
  merged, nothing recorded, the turn kept, no attempt spent. With files lying around the gate is not
  run at all: the answer is known and the run is the lane's whole cost.
- **A record needs neither check.** It is of a tree; what lies around the commit in the worktree
  that reads it does not change what was tested.

A refusal rather than a `gate_red` outcome: the gate was not red, and the fix is not in the code.
It is the refusal an uncommitted change to a tracked file already got before the sync.

## What is given up

- **The second sample on another machine.** A gate that depends on a toolchain version, a local
  service or the OS can be green where it ran and would be red where the PR lands. Adopting
  `gate.ci: local` was already the repo's claim that its command is CI-equivalent (ADR-0012); this
  leans on that claim across machines, for at most a day.
- **The off switch.** A repo can no longer ask for the landing's own run on every head.
- **A clean remote.** Records accumulate under `refs/afk/gate/` until a launch sweeps them. They are
  not fetched by a default clone and fire no `on: push` workflow.

## Considered and rejected

- **A commit status** (`afk/gate`). Visible on the PR page, but stored per commit: finding "a commit
  with this tree that has the status" is not a lookup, so it falls back to a commit key.
- **A marker comment on the PR.** The worker gates before its PR exists, and the same tree under
  another PR would not find it.
- **Keep the local file beside the remote record.** Two homes for one fact, and a window where they
  disagree; the remote one costs a push at `afk gate` and a fetch at `afk land`, which already pushes.
- **Tag the record with the machine and trust it only there.** Solves only the recreated worktree.
- **Delete a record once its PR lands.** A tree has no owning PR, and the runs between a PR's first
  green and its last would be left behind anyway.
- **Lock a tree while it is being gated.** Two places gating one tree at once both run; the cost is
  one wasted run, rarely, against a lock that can be left held.
