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
**Extended, opt-in, by [ADR-0029](0029-a-merge-batch-lands-n-prs-behind-one-gate-run.md):** with
`merge.batch` one landing turn may be held by several PRs — a merge batch — and landed by a batch
worker behind a single gate run.
**Amended (#51):** in `gate.ci: required` a landing waits for its checks itself — see
[the amendment](#amendment-51--a-landing-waits-for-its-own-checks) at the end. Where the text below
has `awaiting_ci` send the worker round the launcher, read it as what happens only once that wait
has run out.
**Amended by [ADR-0035](0035-a-silent-landing-worker-is-restarted-onto-its-turn.md) (#90, #91):** a
worker still silent after its nudge on the turn is restarted onto the turn once, not failed, and
silent again past that restart it is escalated with the PR kept — see
[What bounds a turn](#what-bounds-a-turn).
**Amended by [ADR-0045](0045-a-landing-that-stops-on-a-conflict-or-a-red-gate-gives-its-turn-up.md) (#151):** a
turn covers only sync, gate and merge. The first time a PR's landing stops with `conflict` or
`gate_red` it **gives its turn up** and is fixed off the turn; the turn goes to the next PR. Where
the text below says such a stop keeps the turn, that no outcome gives the turn up, or that each PR
of a conflicting group is resolved exactly once, read ADR-0045: that holds only from a PR's second
turn on.
**Amended by [ADR-0046](0046-a-single-landing-stacks-on-the-target-like-a-batch-of-one.md) (#154):** where merge
batches form — `gate.ci: local`, no adversarial verify — a PR landing alone is not synced: it is
stacked on the target's tip with one merge commit, gated there, and pushed as a fast-forward. Read
"sync → push → gate → `gh pr merge`" below as what happens in `required`, with the verify on, and
for a PR that conflicts with the target's tip.
**Amended (#126):** a landing reads its turn and the target's tip again right before it merges — see
[the amendment](#amendment-126--a-landing-reads-its-turn-and-the-target-again-before-it-merges) at
the end, which also states the window that remains.

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
| `target_moved` — the target moved while the gate ran; nothing merged (#126) | lands again: synced with the new tip, gated on it |
| `awaiting_ci` / `needs_verify` / `no_checks` — the sync moved the head, and the next move is the tick's | wakes the launcher and stops; the turn is kept |

Every stop short of `merged` is written onto the turn marker (`stopped`, `head`), which is what
`afk rebuild` reports on the `landing` row. On the last three the tick re-runs `afk turn` — after CI
has spoken, with `--verified <new head>`, with `--allow-no-checks` — and that tells the worker to
land again. No outcome spends an attempt, closes the PR, or gives the turn up. *(As first
written. Since ADR-0045 the first `conflict` or `gate_red` of a PR gives the turn up; the worker
fixes off the turn, and `afk land` there ends with `awaiting_turn`.)*

The invariant of ADR-0012 is unchanged — **what lands on the target was gated in the form it
lands** — and so is merge-never-rebase. What moved is who runs the gate at landing: the worker's
`afk land`, not the tick.

### What bounds a turn

The ladder that already exists, with one rung added by
[ADR-0035](0035-a-silent-landing-worker-is-restarted-onto-its-turn.md). A `landing` claim's worker
is asked after with `afk no-pr`, like a PR-less one: busy or within grace of the turn it is left;
silent past grace it is nudged once (ADR-0018); silent again it is **restarted onto the turn** —
`afk turn --restart`: the delivery below for a gone terminal, applied to an idle one, recorded on
the turn marker and made once per turn; and silent again after the restarted worker's own nudge it
is **escalated** (ADR-0035 as amended by #91) — `afk escalate` keeps the PR, the branch and the
worktree, spends no attempt and releases the claim, which frees the turn: the next PR gets it. A
landing turn's silence never reaches `afk fail`; `afk fail` on a landing claim is the tick's own
judgment (red checks in `required`, a refuted verify), and closing the PR is what frees the turn
then. (As first written, the unanswered nudge failed the attempt outright: a PR the tick had judged
ready was closed and redone from base because one line to an idle session was not acted on; the
first form of ADR-0035 moved that failure one restart later.) A worker with no terminal is an
orphan, and its continuation (`afk dispatch`) is started on the turn. A worker whose landing
stopped *for the tick* is waiting, not silent, and is never nudged, restarted or escalated for it.

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
landing step. The **landing brief** names `afk land` as the only way to land and carries its outcome
table; the worker prompt a worker is started with (fresh, continue) says only that it does not
merge its PR and is told when to land it. (As first written every variant carried the command
and the table. The copy in the starting prompt was never acted on — the brief is rewritten with both
when the turn is granted — so it was cut, with the rest of what a worker read and did not act on.)
The tick's summary counts `granted` where it counted
`merged`. The tick stays an Agent subagent, routing in prose.

## Consequences

- A conflict or a red gate at landing costs the worker minutes and the fleet nothing: no cycle, no
  second sync, no attempt. *(Minutes it held the turn for, with every ready PR behind it — which is
  what ADR-0045 ends.)* The file-overlap queue, the hand-back record, the busy-worktree guard and
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
  the target. As first written the later merge went through — pinned to the head it gated, a head
  not gated against what the other fleet landed in between. Since #126 the landing reads the
  target's tip again before it merges and refuses when it moved, which leaves only
  [the instant after that read](#amendment-126--a-landing-reads-its-turn-and-the-target-again-before-it-merges).
  Cross-instance turn coordination is not built.

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

## Amendment (#51) — a landing waits for its own checks

As first decided, a landing in `gate.ci: required` whose sync pushed a new head stopped with
`awaiting_ci`: the worker sent its wake and stopped, a cycle ran `afk turn` again once CI was
green, the worker was told again, and ran `afk land` again. Nothing in that loop is a judgment —
it is waiting, done by four parties in turn, and it cost at least one cycle per landing on any
repo whose base moves.

**`afk land` now waits for the checks of the head that would land**, in the same run: after the
push (and equally when the checks of an unmoved head are still running), it re-reads the PR every
`--checks-poll` seconds (default 15) until those checks say `green` or `red`, for at most
`--checks-timeout` seconds (default 1800). What it waits out is decided in one pure place
(`afk_decide.checks_owed`): GitHub still showing the head from before the push, a check still
running, and a just-pushed head that shows no checks although the PR had them — they are not
registered yet, which is not a repo without CI.

| the wait ends with | the landing |
|---|---|
| green | goes on: the verify check, then the merge pinned to that head |
| red | `gate_red` — the worker's to fix, as before |
| the bound | `awaiting_ci` — and the path above takes over unchanged: wake, the tick's `afk turn` once CI has spoken, land again |

`needs_verify` and `no_checks` are untouched: those are the tick's judgments, and a worker still
never answers them. The turn check, the pin of the merge to the gated head, and "what lands was
gated in the form it lands" are as they were; the only thing that moved is who does the waiting.
A landing can therefore run for as long as CI does, inside the worker — the same place a long
local gate run already happens.

## Amendment (#126) — a landing reads its turn and the target again before it merges

`afk land` checked its turn once, at the start, and then ran the gate or waited for checks — up to
half an hour each — and merged without looking again. Two things it rested on could have moved by
then:

- **The target.** `gh pr merge --match-head-commit` pins the merge to the PR's head and to nothing
  on the target. A target that moved after the sync makes the merge commit a merge of the gated head
  with a tip it never met: a tree no gate run saw, which is exactly what "what lands on the target
  was gated in the form it lands" forbids.
- **The turn.** An escalation releases the claim and frees the turn; a takeover restamps the claim;
  `afk turn --restart` grants the turn afresh to a second worker. In each the next landing may
  already be under way while the first one is still gating.

The batch path already asked again before its push (ADR-0029), and its push is a fast-forward the
remote refuses on a moved target. The single path had neither.

**Right before the merge, the landing reads again what the merge rests on**, and merges only if
none of it moved:

| read again | moved means | the landing |
|---|---|---|
| the claim's owner and the PR's turn marker | not the turn this landing started on — released, taken over, or granted afresh | exit 3, as for a turn never held: nothing merged, **nothing written** (the marker is no longer its to write on) |
| the target's tip | the gated head does not contain it | `target_moved`, written on the turn marker — the worker's own to act on: the same command again syncs with the new tip and gates that tree |
| in `required`, the head the checks belong to | the PR as first read was at another head than the one that would merge | waited out like a head just pushed (the #51 wait), so the checks judged are always those of the head merged |

"Contains", not "equals": a head that holds the target's tip merges to its own tree, whatever the
tip is, and that tree is the one the gate passed on. An undisturbed landing costs one `ls-remote`,
one read of the claims and one of the PR's comments more than before, and behaves as it did —
recorded gate run included.

### The window that remains

The check and the merge are two calls, and GitHub offers no merge pinned to the target's tip. So
between the landing's read of the target and GitHub making the merge commit — seconds, where it was
the length of a gate run — the target can still move, and a turn can still be revoked, and the
merge goes through. Within one fleet instance nothing moves the target in that instant: turns go
out one at a time, and the instance's only other landing is one this check has just refused. What
can is outside its turns — a peer fleet instance's landing, or a person pushing or merging by hand.
The merge commit is then of a tree no gate ran on, and nothing here detects it afterwards. Closing
it needs what the batch path has, a fast-forward the remote itself refuses; a single PR lands
through `gh pr merge` on purpose (ADR-0036: `required` repos never accept a direct push), so the
window is narrowed, not closed.
