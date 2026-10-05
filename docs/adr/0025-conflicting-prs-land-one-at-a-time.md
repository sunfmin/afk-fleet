# ADR-0025 — Conflicting PRs land one at a time: a merge queue

**Status:** accepted — orders the merge transition of [ADR-0017](0017-the-act-half-is-transitions.md)
and adds a waiting status beside the hand-back of
[ADR-0019](0019-a-sync-conflict-is-handed-back-to-its-worker.md), whose record and unanswered ladder
are unchanged. Enforced in the seam ([ADR-0016](0016-the-seam-enforces-its-own-rules.md)).

## Context

A hand-back names one target tip. When several finished PRs conflict with each other, a tick merged
them in whatever order it listed them and handed every conflict back at once, all against the same
tip. Whichever worker answered first was merged; that moved the target, so the answers the others
were still producing were stale and each was handed back again. For *n* mutually conflicting PRs that
is up to n(n-1)/2 resolutions — each a worker resolve plus a local gate run, then another gate run
inside the merge — where n-1 would do. A PR that had never been handed back could also land ahead of
one whose worker was mid-resolution, with the same effect.

Observed on `sunfmin/calcgrid` (concurrency 5, local gate about 5 min, issues that all edit the same
glossary, ADR and help-text lines): one tick ran four merges, landed one and handed three back against
the same tip; the next ran five, landed three and handed two back — the third landing *after* those
two hand-backs. One PR was handed back three times, another twice. Asking the tick, in prose, to merge
answered hand-backs first raised a tick's landed merges from 1 of 4 to 3 of 5: an ordering that
belongs in the tool.

## Decision

1. **There is one merge order, and the tool owns it.** A PR that was handed back goes before one that
   never was; among those, the most rounds first, then the oldest latest hand-back, then the lower PR
   number — which is the whole order among PRs never handed back
   (`afk_decide.queue_rank`). `afk rebuild` returns the `awaiting_merge` rows in that order as
   `merge_order`; the tick merges in it and adds none of its own.
2. **A PR waits behind a handed-back PR it would collide with.** A PR is `queued` while a handed-back
   PR *ahead of it in that order* conflicted — in its latest hand-back — in a file the PR also changes
   (`afk_decide.waits_behind`). `afk rebuild` reports `status: queued` with `behind: <pr>`, and
   `afk merge` on it stops with outcome `queued` before the worktree is touched: no sync, so no
   conflict and no hand-back. Only one PR of an overlapping group is with its worker at a time.
3. **"Handed back" holds the queue until it merges, not until it is answered.** An answered hand-back
   still has to land: its worker may be gating (ADR-0024), its merge-time gate may run for minutes. A
   PR behind it that synced in that window would resolve against a tip about to move — the very round
   trip this removes. The PR ahead stops holding when it is no longer an open PR of a claim: merged,
   failed (closed), or its claim released.
4. **Overlap is by file, against the conflicted files of the hand-back.** They are on the PR already —
   the marker comment lists them — and the waiting PR's changed files are one read of
   `pulls/<n>/files`, paid only when a handed-back PR is ahead of it. With no hand-back anywhere the
   queue costs nothing beyond the comments read `afk rebuild` already made. A PR that changes none of
   those files is never held up.
5. **The queue is every claim's open PR**, a peer fleet's included — the record is on GitHub, so two
   fleets compute the same order. A PR whose claim was released (escalated, parked) is not in it.
6. **Waiting is bounded by the ladder that exists.** An unanswered hand-back is nudged and then failed
   (ADR-0018, ADR-0019); `afk fail` closes its PR and the queue advances. A queued claim is not
   watched, not nudged, and spends no attempt. It keeps its claim, so it counts toward `concurrency`
   and `in_flight`, and the launcher keeps the busy interval.
7. **The status board shows it.** A new phase `queued` — "waiting to merge, behind PR #n" — written by
   `afk merge` when it answers `queued`, and by the tick's render pass (`afk status --behind`).

## Considered and rejected

- **Order only, no waiting.** Fixes the PR that lands ahead of an answered hand-back; leaves every
  conflicting PR handed back at once against one tip, which is most of the cost.
- **Hold on an *open* hand-back only.** The window between answered and merged is exactly when the
  next PR would be synced against a stale tip.
- **Predict conflicts by merging PRs against each other.** A trial merge per pair, per tick, in a
  worktree — to learn what the first real hand-back states for free. Until one PR of a group has
  conflicted, nothing is known and nothing is held: the first hand-back is the signal.
- **A lock or a queue stored somewhere.** The order is a pure function of what is on GitHub, recomputed
  by every call; nothing can be left held by a tick that died.

## Consequences

- With three PRs that all rewrite one file: the first merges, the second is handed back, the third
  waits; the second lands, then the third is synced, handed back once, and lands. Two resolutions.
- A group's PRs land over more ticks than before — each waits for the one ahead — in exchange for not
  redoing work. Unrelated PRs are unaffected.
- File overlap is a heuristic. Two PRs that conflict in a file the hand-back did not name are not
  held apart, and a PR that changes a named file in a non-conflicting place waits one turn it did not
  need. Both err toward what happened before, or a short wait — never a lost resolution.
- `afk merge` reads the claim refs and each queued PR's comments on every call: a few GitHub reads
  beside a gate run measured in minutes.
- A stale peer's handed-back PR holds overlapping PRs until that claim is reclaimed or released.
