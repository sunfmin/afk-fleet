# ADR-0019 — A sync conflict is handed back to the worker that wrote the branch

**Status:** accepted — adds a transition to [ADR-0017](0017-the-act-half-is-transitions.md) and
reverses one of its "considered and rejected" entries (*have `merge` resolve conflicts by
re-dispatching a worker*: the objection was the cost of a retry, and a hand-back costs none). Reuses
the silent-worker path of [ADR-0018](0018-nudge-a-silent-worker-before-failing-it.md) and the
continuation tiers of [ADR-0011](0011-takeover-and-progress-preservation.md) unchanged.

## Context

`afk merge` syncs a PR's branch with the merge target before gating it (ADR-0012). When that sync
conflicted, the merge stopped with `outcome: conflict` and left the tick two moves: resolve it itself,
or `afk fail`. A tick is a fresh context with none of the worker's — it did not write either side — so
anything beyond a purely mechanical conflict went to `afk fail`, which closes the PR, deletes the
branch, removes the worktree and starts a fresh worker from base, spending an attempt.

That is the retry ladder answering a question nobody asked. The ladder exists for work that is
*failing*; here the work is finished and gate-green, and the only thing that happened is that a
sibling PR landed first. Observed on one run (calcgrid, 2026-10-02): two issues each lost about an
hour of completed work this way — one PR conflicted with a sibling that rewrote the same file, another
in 8 files / 18 hunks with a sibling that had landed minutes earlier. Both were redone from scratch.

The worker prompt already says where integration conflicts belong: *"you are the author, your context
is loaded, and the fix is cheap."* The same is true after the PR is open.

## Decision

1. **`afk hand-back` is the transition for a `conflict` outcome.** One call, in this order: abort the
   merge the sync left in the worktree → write the instruction as the worktree's brief → record the
   hand-back on the PR → deliver it → status board. The claim, the PR, the branch and the worktree are
   kept. `afk-attempt/<n>` is neither read nor written: a sync conflict is not a failure of the work.
2. **The instruction is one template block.** `worker-prompt.md`'s `handback` block names the target
   and the tip that conflicted, the conflicted files, and the steps: fetch, **merge** the target in
   (never rebase), resolve, run `gate.local_command` until green, push to the PR's existing branch,
   stop. Every worker prompt also says, at its last step, that the PR may come back this way.
3. **Delivery follows the worker, not the other way round.** Its terminal still there → the block
   alone becomes the brief and one submitted line points at it (the same paste-avoiding pointer the
   prompt and the nudge use). Its terminal gone — it finished and closed, the machine restarted, the
   claim was taken over from another machine — → a new terminal in the same worktree, a new worker
   started with the worker launch command by **continuation**, on the continue-mode prompt with the
   block appended. Never from base. A plain `afk dispatch` of a claim whose PR carries an open
   hand-back does the same, so a hand-back survives its worker dying mid-resolution.
4. **The record is a marker comment on the PR**: `<!--afk:handback target=<branch> tip=<sha>
   head=<sha> at=<epoch>-->`, followed by the same facts for a human. It lives where it concerns and
   dies with it — a retry closes the PR, so a fresh attempt starts with no hand-back to answer. One
   comment per conflicting head: handing the same head back again rewrites it.
5. **A hand-back is open until the PR head contains the tip it named.** While it is open `afk rebuild`
   reports the claim as `handed_back` (board phase `handed_back`), whatever its checks say, and
   `afk merge` returns `outcome: handed_back` without touching the worktree. A push that does not
   bring the tip in — a checkpoint, a half-done merge — does not close it. Containment is asked of
   GitHub (one compare), and only once the head has moved, so `rebuild` stays machine-independent
   (ADR-0008); the extra read is paid only by claims of mine that have a PR.
6. **A handed-back worker is watched like a PR-less one.** The tick probes its terminal and calls
   `afk no-pr`; the hand-back's timestamp is a sign of life like the nudge's, worth one grace period.
   After that: idle with the head unmoved → `nudge` (the nudge points at the brief, which is now the
   hand-back) → still silent → `idle_failed` → `afk fail`. **Only an unanswered hand-back enters the
   retry ladder**, which is what stops one from parking a claim forever. No terminal → `orphan` →
   `afk dispatch`, per point 3.
7. **Recorded before delivered.** The other order has a window in which the worker is resolving and
   `rebuild` still says `awaiting_merge`, so the next tick re-runs the merge inside the worktree the
   worker is in. With the record first, a delivery that fails (orca down, an agent that never became
   ready) leaves a `handed_back` claim that the existing paths repair: the nudge delivers the brief,
   or the dead worker's continuation is started on it.
8. **A second conflict is handed back again.** After a successful re-sync the target may have moved
   once more; that is a new round with a new comment, and each round merges a newer tip, so the
   sequence converges without a counter.
9. **A tick may still resolve a purely mechanical conflict itself.** This keeps what ADR-0017 kept:
   both sides added independent adjacent lines and the resolution is *keep both* — resolve, commit,
   re-run `afk merge`. The default flips, though: anything that needs to know what the code is for is
   handed back, and `afk fail` is no longer a response to a conflict at all.

## Consequences

- A conflict costs one worker round trip (minutes, with its context already loaded) instead of one
  attempt and the whole implementation.
- `afk rebuild` makes one more read per in-flight PR of mine (its comments), and one compare for a
  handed-back PR whose head has moved. Bounded by `concurrency`.
- `mine` rows have a seventh status and the board an eighth phase. `afk no-pr` now also answers for a
  claim that *has* a PR; its name is kept, since what it reads — is the worker still working — is the
  same question.
- The fleet writes to a worker's terminal a second way (after the nudge). It is still one line, and a
  worker's result is still only its PR or its verdict marker: the answer to a hand-back is the PR's
  head, read from GitHub.
- The marker comment joins the reserved surfaces (`<!--afk:handback …-->`).
- A worker that can no longer act on a line typed at it (its agent exited to a bare shell while the
  terminal stayed open) answers nothing, and reaches a retry two grace periods later — the same bound
  ADR-0018 gives any silence.

## Considered and rejected

- **Have `afk merge` hand back on its own.** The conflict is where the tick's judgment sits (point 9),
  and a transition stops where judgment starts. The call is one line away.
- **Record the hand-back in the claim ref or under a new `refs/afk/handback/*`.** A third ref layout
  that no probe, fallback or doc describes (ADR-0003), re-stamped away by every reclaim and takeover —
  for a fact about a PR.
- **Record it on the issue.** It would outlive the PR it describes, and a retry's new PR would have
  to be told apart from the one that conflicted.
- **Treat "the head moved" as the answer.** A worker checkpoints before it merges; the claim would
  bounce back to `awaiting_merge`, conflict, and be handed back again while the worker is mid-way.
- **Count hand-backs and fail after N.** Each round merges a strictly newer tip, and silence is
  already bounded by the nudge path; a counter would only fail work that is converging.
- **Rebase the branch instead.** ADR-0012: it drops the merge commits and re-ignites the conflicts
  resolved inside them.
