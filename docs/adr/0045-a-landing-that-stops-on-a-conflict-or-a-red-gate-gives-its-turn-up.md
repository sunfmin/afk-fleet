# ADR-0045 — A landing that stops on a conflict or a red gate gives its turn up, once

**Amended by [ADR-0048](0048-finished-prs-join-a-landing-train-gated-whenever-the-gate-is-free.md) (#157):** where a landing train runs there is no turn to give up — a conflict is resolved against the train before joining, and a red gate is the train worker's to fix. This ADR stands where a PR lands on a turn (`gate.ci: required`, or an adversarial verify), and "a batch forms beside it" no longer applies anywhere.

**Status:** accepted — narrows what a **landing turn** covers to the bounded part of a landing.
Amends [ADR-0027](0027-a-worker-lands-its-own-pr-on-a-landing-turn.md) ("No outcome … gives the
turn up", and "each PR of a conflicting group is resolved once"),
[ADR-0035](0035-a-silent-landing-worker-is-restarted-onto-its-turn.md) (the ladder that bounds a
turn also bounds a fix off one) and
[ADR-0029](0029-a-merge-batch-lands-n-prs-behind-one-gate-run.md) (when a batch forms). The record
it adds is a field of the turn marker, in the encoding of
[ADR-0032](0032-records-kept-in-comments-share-the-encoding-of-records-on-refs.md). The invariant of
[ADR-0012](0012-local-completion-gate.md) is untouched. Amended by
[ADR-0047](0047-off-the-turn-a-landing-runs-no-local-gate.md): off the turn the local gate is no
longer run (items 3 and 4, and the fourth rejected alternative).

## Context

A PR kept its landing turn while its worker resolved a sync conflict or fixed a red gate in place
(ADR-0027). That work has no known length, and everything else waited behind it: every other ready
PR was `awaiting_turn`, and no merge batch forms while a turn is out (ADR-0029).

Seen on one run (fleet `fleet-789a05`, 2026-10-10, `gate.ci: local`, concurrency 10): a batch of
five landed two PRs and left three out. Those three then took single turns one after another —
20 min, 6 min and 11+ min, the last one stopped `gate_red` after its sync and fixed in place — while
seven finished PRs waited. Two PRs landed in 35 minutes.

A turn is the fleet's one-at-a-time resource. Sync, gate and merge are mechanical and bounded by
the gate's run time; a conflict resolution or a fix is neither.

## Decision

**A turn covers sync, gate and merge. A landing that stops with `conflict` or `gate_red` gives the
turn up — once per PR — and the PR is fixed off the turn, by the same worker, in the same
worktree.**

1. **`afk land` gives the turn up itself.** On `conflict` or `gate_red` (`LAND_FIXES`), on a turn
   whose PR never gave one up, the landing rewrites the turn marker to one that holds no turn:
   `released=1`, `given_up=<epoch>`, with `stopped` and `head` as before
   (`afk_decide.gives_turn_up`, `given_up_turn`). Its result says `"turn": "given_up"`. Nobody
   tells the worker anything: the outcome table it already acts on is the same — fix, commit, run
   the command again — plus one wake, sent at once and not waited on, so the next cycle opens now.
   No round trip through the tick, no attempt spent, the PR, the branch and the worktree kept.
2. **The claim is `fixing`.** `afk rebuild` reports a claim whose PR carries my given-up marker,
   still stopped on one of `LAND_FIXES`, as `fixing` — whatever its checks say, as for `landing`.
   It holds no turn (`held_turn`), is not in `merge_order`, and holds back neither a single turn
   nor a merge batch: the next cycle grants the turn to the head of the merge queue, or forms a
   batch.
3. **Off the turn `afk land` syncs and gates, and merges nothing.** The same command, under the
   given-up marker (`afk_decide.own_landing`): sync → push → the machine gate. It stops `conflict`
   or `gate_red` as often as it takes, each written on the marker. Green — or, in `required`,
   anything that is the tick's to settle before a grant — it writes `stopped=awaiting_turn`: the PR
   is **ready again**, the worker wakes the launcher and stops. Readiness is read from GitHub as
   always: the claim is `awaiting_turn` (or what its checks make it).
4. **Ready again, it goes ahead of PRs that never held a turn.** `turn_order`: the PR that holds a
   turn, then one that gave a turn up, then one that left a batch, then the lower PR number. It
   takes a **single** turn, and no batch forms while it waits for it — as for a PR that left a
   batch. That turn is usually short: the gate run made off the turn is on record for the tree
   (ADR-0030), so a target that did not move since costs a merge.
5. **Once.** `given_up` stays on every marker written for the PR from then on, whoever grants its
   next turn. On a turn whose marker carries it, `conflict` and `gate_red` keep the turn and are
   fixed with it held — ADR-0027's behaviour, and the marker comment says so. A PR therefore
   cannot be starved by the landings that pass it.
6. **A worker fixing off the turn is watched like one on it.** `asks_after` includes `fixing`
   rows, and `classify_stopped` puts both on the same rungs (`worker_at_landing`): nudge →
   restart (`afk turn --restart`, which for a `fixing` PR replaces the worker, writes `restarted`
   on the marker and grants nothing — outcome `fixing`) → escalate with the PR, the branch and the
   worktree kept. Giving the turn up clears `restarted`: a worker that got as far as `afk land` is
   not the one a restart replaced. A gone terminal is continued onto the landing brief
   (`afk dispatch`), unbounded. No silence of a `fixing` claim reaches `afk fail`.
7. **Where the next move is the fleet's, the turn is kept.** `awaiting_ci`, `needs_verify`,
   `no_checks` and `target_moved` are bounded waits or one more mechanical run; nothing changes for
   them.
8. **The status board says where the PR stands**: `fixing` (turn given up, being fixed) and
   `ready_again` (ready again, awaiting its turn).

A turn another fleet instance gave up is nobody's, like one it held: after a takeover the PR is
`awaiting_turn` and the new owner grants its own — on which, `given_up` being on the marker, a
conflict keeps the turn.

**The invariant is untouched: what lands on the target was gated in the form it lands.** Nothing
lands off a turn — the merge, and the re-read of the turn and the target before it (#126), are
reached only on a held turn.

## What this gives up

ADR-0027 chose that each PR of a conflicting group is resolved exactly once, against a target that
already holds everything landed before it. A PR that gave its turn up resolves against the target
as it was then; landings pass it while it is fixed, and its second turn may meet a second conflict.
The once-only bound keeps that cost at one extra resolution per PR, and that second one is made
with the turn held. Against it: a turn's length is now a gate run, not a debugging session.

## Consequences

- Throughput is no longer set by the slowest fix. A PR being fixed costs the queue nothing, and a
  batch forms beside it.
- A PR may take two turns, and the worker of a PR that gave its turn up sends one more wake.
- The turn marker grows a field, `afk rebuild` a status (`fixing`) and a row field (`given_up`),
  `afk land` an outcome (`awaiting_turn`) and a result field (`turn`), `afk turn` an outcome
  (`fixing`), and the status board two phases. A marker without the field reads as it always did.
- `released` on a marker no longer means only "left a batch": a marker holds no turn because the PR
  left a batch (`unbatched`) or gave its turn up (`given_up`).

## Considered and rejected

- **Give the turn up every time.** A PR in a hot area could be passed for ever. Once bounds it.
- **Have the tick take the turn back** when it sees `stopped=conflict`. A cycle later than the
  landing can do it itself, and a second writer of the marker racing the worker's next `afk land`.
- **Read "fixed" from the PR's head moving.** A worker pushes as it goes; a moved head is not a
  fixed PR. The worker says so by running the command it already runs.
- **Mark ready without syncing or gating off the turn.** The second turn would then hold the turn
  through the gate run of an unproven fix — the open-ended part again.
- **Let a ready-again PR join a merge batch.** It was promised a place ahead of the queue; a batch
  that leaves it out on a conflict would send it round a third time.
