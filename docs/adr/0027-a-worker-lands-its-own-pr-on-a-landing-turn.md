# ADR-0027 — A worker lands its own PR, on a landing turn the fleet grants one at a time

**Status:** accepted — moves the landing of a PR out of the tick. Supersedes the **tick-side**
merge-time gate run of [ADR-0012](0012-local-completion-gate.md) (its invariant, and merge-never-
rebase, are kept: the run moves to the worker's landing); the `merge` transition of
[ADR-0017](0017-the-act-half-is-transitions.md), replaced by `turn`;
[ADR-0019](0019-a-sync-conflict-is-handed-back-to-its-worker.md) (the hand-back) and
[ADR-0024](0024-merge-stays-out-of-a-busy-workers-worktree.md) (`worker_busy`), both in full; and
the overlap queue of [ADR-0025](0025-conflicting-prs-land-one-at-a-time.md) (`queued` / `behind`)
together with the `unblocked` list of
[ADR-0026](0026-a-recorded-gate-run-stands-in-for-the-merge-time-run.md) — one-at-a-time landing,
and ADR-0026's recorded gate run, are kept. Reuses the silent-worker ladder of
[ADR-0018](0018-nudge-a-silent-worker-before-failing-it.md) and the continuation tiers of
[ADR-0011](0011-takeover-and-progress-preservation.md) unchanged; enforced in the seam
([ADR-0016](0016-the-seam-enforces-its-own-rules.md)).

## Context

The tick used to land PRs: `afk merge` synced the branch with the target in the worker's worktree,
ran the gate there, and merged. Everything that could go wrong at that point was something only the
worker could fix, so each one became a round trip:

- **A sync conflict** stopped the merge, and the tick handed it back (ADR-0019): abort the merge,
  write an instruction, record the target tip on the PR, wait for a push that contained it, then
  merge again — at least two cycles, and a second sync and gate, for what the worker resolves in
  minutes with its context loaded.
- **The answer to a hand-back was not the worker's outcome.** A worker pushes the merge and gates it
  afterwards, so the tick had to be kept out of a worktree whose worker was still busy (ADR-0024).
- **Hand-backs raced each other.** Mutually conflicting PRs handed back together voided each other's
  resolutions, so a queue ordered them by hand-back rounds and held a PR behind a handed-back one
  whose conflicted files it changed (ADR-0025) — which needed every PR's file list, and a list of
  what a landing freed (ADR-0026) so a queued PR did not wait a cycle.
- **A red merge-time gate** failed the whole attempt: the PR was closed and a fresh worker redid the
  issue from base, when the worker that wrote the branch could have fixed the integration break.

Four statuses (`awaiting_merge`, `queued`, `handed_back`, and `worker_busy` as an outcome), two
marker formats and a file-overlap computation existed to shuttle work between two parties, one of
which — the tick — had none of the context the work needed. The tick was also running a
multi-minute gate inside what is meant to be a short, disposable pass.

## Decision

**A finished PR is landed by the worker that wrote it, on a landing turn the fleet grants one at a
time. The tick no longer merges: `afk merge` and `afk hand-back` are removed.**

### `afk turn` — the tick's half

`afk turn --issue <n> --instance <id>` gives one of my claims' PRs the **landing turn**. It records
the turn as ONE marker comment on the PR —

```
<!--afk:turn instance=<id> at=<epoch> [verified=<sha>] [allow_no_checks=1] [stopped=<outcome> head=<sha>]-->
```

— tells the worker, and upserts the status board. Recorded before delivered: a delivery that then
fails leaves a `landing` claim the existing paths repair.

- **One at a time.** While another claim of mine holds a turn, `afk turn` answers `waiting` and
  touches nothing. Turns go out in `merge_order`: a PR that already holds a turn first, then the
  lower PR number. Nothing else orders them: no rounds, no file overlap. A PR waiting for its turn is
  `awaiting_turn` — not synced, so never in conflict with a tip that is about to move — and each PR
  of a conflicting group is resolved once, against a target that holds everything landed before it.
- **The tick's judgments come before the grant** and travel on the marker, because a worker never
  judges its own work: in `gate.ci: required` the PR's checks must be green (`awaiting_ci` /
  `gate_red`), a PR with no checks needs `--allow-no-checks` (`no_checks`), and with
  `gate.adversarial_verify` the turn is refused without `--verified <head>` (`needs_verify`).
- **Delivery.** The worker's terminal is still there → one submitted line pointing at a landing
  brief. It is gone → a worker is started by continuation in the same worktree, on the same branch
  (or in one recreated at the PR's head — never from base), briefed **only** to land the PR.
  **There is no launcher-side merge fallback**: a PR nobody can be started to land is failed by the
  ladder below, not merged around.

### `afk land` — the worker's half

`afk land --issue <n>`, run by the worker in its own worktree, is the only way a PR lands. It exits
3, changing nothing, unless the PR's turn marker names the fleet instance that holds the issue's
claim (so a turn does not survive a takeover). Then: sync by merging the target in — never a rebase
— → push → the machine gate on that exact head (`local`: the gate run, or ADR-0026's recorded run;
`required`: the PR's checks) → the verify check → `gh pr merge --match-head-commit` → status board.
It stops with an `outcome`:

| outcome | the worker |
|---|---|
| `merged` | wakes the launcher and stops |
| `conflict` — the merge is left in progress, `files` unmerged | resolves in place, commits, lands again |
| `gate_red` — excerpt returned, and posted on the PR | fixes the code, commits, lands again |
| `awaiting_ci` / `needs_verify` / `no_checks` — the sync moved the head, and the next move is the tick's | wakes the launcher and stops; the turn is kept |

Every stop short of `merged` is written onto the turn marker (`stopped`, `head`), which is what
`afk rebuild` reports on the `landing` row. On the last three the tick re-runs `afk turn` — after CI
has spoken, with `--verified <new head>`, with `--allow-no-checks` — and that tells the worker to
land again. No outcome spends an attempt, closes the PR, or gives the turn up.

The invariant of ADR-0012 is unchanged — **what lands on the target was gated in the form it
lands** — and so is merge-never-rebase. What moved is who runs the gate at landing: the worker's
`afk land`, not the tick.

### What bounds a turn

The ladder that already exists. A `landing` claim's worker is asked after with `afk no-pr`, like a
PR-less one: busy or within grace of the turn it is left; silent past grace it is nudged once
(ADR-0018), then failed — `afk fail` closes the PR, which frees the turn, and the next PR gets it. A
worker with no terminal is an orphan, and its continuation (`afk dispatch`) is started on the turn.
A worker whose landing stopped *for the tick* is waiting, not silent, and is never nudged or failed
for it.

### What the landing does not do

`afk land` neither releases the claim nor removes the worktree: it runs inside that worktree, and a
worker holds no instance id. The next cycle sees a claim of its own whose issue is closed (the
`closed` row that already existed for a crashed merge) and `afk release` settles both — it now also
removes the worktree of a claim whose issue is closed.

### What the turn check is, and is not

It guards against a worker that **strays** — one that would run `afk land` early, or after its
claim was taken over. It is not a defence against a malicious worker: worker and launcher share one
`gh` credential, so a worker that decides to run `gh pr merge` itself is stopped by nothing here. A
real boundary would need separate credentials, which the fleet deliberately does not manage
([ADR-0010](0010-worker-launch-command.md)).

### Vocabulary

`afk rebuild`'s `mine` rows report `awaiting_turn` and `landing` (with `stopped`) in place of
`awaiting_merge`, `queued` and `handed_back`; the status board gains a waiting-for-turn phase and a
landing step. The worker prompt — fresh, continue, and the landing brief — names `afk land` as the
only way to land and carries its outcome table. The tick's summary counts `granted` where it counted
`merged`. The tick stays an Agent subagent, routing in prose.

## Consequences

- A conflict or a red gate at landing costs the worker minutes and the fleet nothing: no cycle, no
  second sync, no attempt. The file-overlap queue, the hand-back record, the busy-worktree guard and
  the freed-claims list are gone, with the code and tests that held them up.
- The tick is short again: it runs no gate and no merge.
- A landing needs a live worker. A finished worker's session is kept (or one is started by
  continuation) until its PR lands; the slot is held for that long, as it was while a PR awaited
  merge.
- Throughput is one landing at a time **per fleet instance**. A PR that is ready behind a slow
  landing waits even when it touches nothing the landing touches; merge batches (#40) are the place
  to recover that.
- The landing after a `merged` wake takes one more cycle to release the claim and the worktree.
- Two fleet instances on one repo each grant their own turns, so two landings can still race for
  the target: the later merge is pinned to the head it gated, but that head was not gated against
  what the other fleet landed in between — as it was with two ticks merging. Cross-instance turn
  coordination is not built.

## Considered and rejected

- **Keep the tick-side merge as a fallback when no worker can be reached.** Two ways to land is two
  sets of invariants; the continuation that already exists is the fallback.
- **Let the worker merge whenever its PR is green, with no turn.** That is the race ADR-0025 was
  written against: every worker syncs against a tip the others are about to move.
- **Have `afk land` release the claim and remove the worktree.** It would need the instance id in
  the worker's hands, and would remove the directory it is running in.
- **Let the worker run the adversarial verify.** The author verifying itself is what the gate
  exists to prevent.
- **Move the tick's routing into code** in the same change. Out of scope here.
