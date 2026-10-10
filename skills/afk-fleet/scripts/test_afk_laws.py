#!/usr/bin/env python3
"""
The laws of the afk-fleet decision core — pure, no git/gh/network.
Run: under pytest — the command is `gate.local_command` in docs/agents/afk-fleet.md

`test_afk_decide.py` pins each verdict with an example per case. These pin what
holds for EVERY input: each test states one law, checks it over a range that
is enumerated where the domain is small and drawn from a seeded generator
where it is not, and says in its docstring the precondition the law hides —
what must be true of the input for it to hold at all. Every test also checks
that its range reached the cases the law is about, so a generator that drifts
away from them fails instead of passing on nothing.
"""
import functools
import itertools
import json
import random
import typing

import afk_decide as d
from test_afk_decide import CALL, FACTS, GRACE, NOW, _did, _mine, _worker, _working_set

TTL = d.CLAIM_LEASE_TTL_SECONDS
SKEW = d.CLOCK_SKEW_TOLERANCE_SECONDS

# A stamp another clock wrote, at every place the grace reads differently: not
# known, one second inside it, exactly at it, long past it, ahead of this clock
# by the skew the fleet allows (read as "just now"), and ahead by one second more
# (no evidence of anything — `stamp_age`).
UNDER, AT, OLD, SKEWED, AHEAD = NOW - GRACE + 1, NOW - GRACE, NOW - 9000, NOW + SKEW, NOW + SKEW + 1
STAMPS = (None, UNDER, AT, OLD, SKEWED, AHEAD)
TERMINAL_IDLE = (None, 0, GRACE - 1, GRACE, 9000)


# --------------------------------------------------------------------------- #
# A stopped worker's classification                                            #
# --------------------------------------------------------------------------- #

def _progress(ahead=0, dirty=False, committed=None, touched=None):
    return {"commits_ahead": ahead, "dirty": dirty, "last_commit_ts": committed,
            "worktree_mtime_ts": touched}


def _said(phase, blocked_by=(), found=True):
    return {"found": found, "phase": phase, "blocked_by": list(blocked_by), "reason": None,
            "comment_url": "u" if found else None}


PROGRESS = (None, *(_progress(committed=c, touched=m) for c in STAMPS for m in STAMPS),
            _progress(ahead=None), _progress(ahead=2), _progress(dirty=True))
# (what the worker declared, the standing of each blocker it named)
DECLARED = (
    (None, {}), (_said(None, found=False), {}), (_said(None), {}), (_said("weird"), {}),
    *((_said(phase), {}) for phase in d.VERDICT_PHASES if phase != "blocked"),
    (_said("blocked"), {}), (_said("blocked", [1]), {}), (_said("blocked", [1]), {1: "closed"}),
    (_said("blocked", [1, 2]), {1: "closed", 2: "waiting"}),
    (_said("blocked", [1, 2]), {1: "waiting", 2: "unmet"}),
    (_said("blocked", [1]), {1: "no such standing"}))
NUDGED = (None, UNDER, AT, OLD, AHEAD)
# one PR's turn, and a marker that holds none: it only remembers a train abandoned
TURN_KINDS = ({}, {"released": True, "abandoned": 5})


def _turns(ats):
    return (None, *(d.next_turn(None, instance="fl-1", at=at, stopped=stopped,
                                head="abc" if stopped else None, restarted=restarted, **kind)
                    for at in ats for stopped in (None, *d.LAND_OUTCOMES)
                    for restarted in (None, OLD) for kind in TURN_KINDS))


@functools.lru_cache(maxsize=None)
def _stopped_workers():
    """Every stopped worker the laws are checked on → [(what was gathered about
    it, its classification)]. The words — what it declared, whether it can be
    nudged, the turn its PR carries and where that landing stopped — are
    enumerated whole against three timings (nothing known, everything long
    past, the terminal active within grace); the clocks, six stamps on each of
    five signs of life, are drawn from a seeded generator over the whole
    product."""
    core = itertools.product(
        (None, _progress(committed=OLD, touched=OLD), _progress(ahead=2)), (None, 9000, GRACE - 1),
        DECLARED, (None, OLD), (True, False), _turns((None, OLD)))
    rng, pools = random.Random(128), (PROGRESS, TERMINAL_IDLE, DECLARED, NUDGED, (True, False),
                                      _turns((None, UNDER, AT, OLD, AHEAD)))
    drawn = (tuple(rng.choice(pool) for pool in pools) for _ in range(30_000))
    out = []
    for progress, idle, (verdict, standings), nudged_at, can_nudge, turn in (*core, *drawn):
        gathered = {"progress": progress, "terminal_idle_seconds": idle, "worker_verdict": verdict,
                    "blocker_states": standings, "nudged_at": nudged_at, "can_nudge": can_nudge,
                    "turn": turn}
        out.append((gathered, d.classify_stopped(now=NOW, grace_seconds=GRACE, **gathered)))
    return out


SETTLED_BY_STATE = {"working", "just_stopped", "gone"}


def test_a_stopped_worker_is_never_classified_with_a_cause_its_own_state_settles():
    """Whatever was gathered about a stopped worker, `classify_stopped` names
    none of the three causes `settled_by_worker_state` decides from the worker
    state alone — and between them the two name every cause there is.

    Hidden precondition: nothing ties the two calls together. `classify_stopped`
    is not told whether the worker state was asked first, so the law has to hold
    for every input, including a worker the state WOULD have settled (its
    terminal active within grace): that one is `within_grace`, never
    `just_stopped`."""
    readings = [{"terminal": terminal, "terminal_idle_seconds": idle, "state": None}
                for terminal in ("none", "busy", "idle") for idle in TERMINAL_IDLE]
    by_state = {seen["cause"] for reading in readings for nudged_at in NUDGED
                for seen in [d.settled_by_worker_state(reading, NOW, GRACE, nudged_at)] if seen}
    assert by_state == SETTLED_BY_STATE
    stopped = {seen["cause"] for _, seen in _stopped_workers()}
    assert not stopped & SETTLED_BY_STATE
    assert stopped == set(d.WORKER_CAUSES) - SETTLED_BY_STATE      # the range reached every row


def test_a_stopped_worker_is_within_grace_exactly_when_its_idle_is_under_the_grace():
    """`within_grace` ⟺ `idle_seconds` is known and below the grace, where
    `idle_seconds` is the age of the most recent sign of life that IS evidence.

    Hidden precondition: "idle" is not a clock reading — it is the least age
    among the signs that are known. An unknown idle (no sign at all, or every
    one stamped further ahead than the skew allowed) is never within grace, a
    sign ahead by no more than the skew is "just now", and an idle of exactly
    the grace is past it. Commits ahead and a dirty tree are not signs."""
    within = unknown = 0
    for gathered, seen in _stopped_workers():
        progress, turn = gathered["progress"], gathered["turn"]
        signs = (progress and progress["last_commit_ts"], progress and progress["worktree_mtime_ts"],
                 gathered["nudged_at"], turn and turn["at"])
        ages = [max(0, NOW - sign) for sign in signs if sign is not None and sign - NOW <= SKEW]
        if gathered["terminal_idle_seconds"] is not None:
            ages.append(gathered["terminal_idle_seconds"])
        idle = min(ages, default=None)
        assert seen["idle_seconds"] == idle, gathered
        assert (seen["cause"] == "within_grace") == (idle is not None and idle < GRACE), gathered
        within += seen["cause"] == "within_grace"
        unknown += idle is None
    assert within > 1000 and unknown > 1000


def test_a_held_landing_turn_with_no_verdict_never_spends_an_attempt():
    """A worker that stopped holding ONE PR's landing turn, having declared
    nothing, is never classified with a cause that fails the claim: it is left,
    nudged, restarted onto the turn or escalated (ADR-0035).

    Hidden precondition: the turn is one PR's and it is held — `at` set, not
    `released` (`single_turn_held`) — and the worker posted no verdict. A
    marker that holds no turn climbs the PR-less ladder, which does fail; a verdict is routed on what it declared, whatever turn is
    held."""
    reached = set()
    for gathered, seen in _stopped_workers():
        verdict = gathered["worker_verdict"]
        if not d.single_turn_held(gathered["turn"]) or (verdict and verdict["found"]):
            continue
        row = d.WORKER_CAUSES[seen["cause"]]
        assert row.step != "fail" and row.action != "next_attempt", (gathered, seen)
        reached.add(seen["cause"])
    assert reached == {"within_grace", "awaiting_tick", "silent", "silent_on_turn",
                       "silent_past_restart"}
    # …and the precondition is what holds it: the same silence on a turn that is
    # not one PR's held turn does fail
    for kind in TURN_KINDS[1:]:
        loose = d.next_turn(None, instance="fl-1", at=OLD, **kind)
        seen = d.classify_stopped(None, 9000, None, {}, NOW, GRACE, nudged_at=OLD, turn=loose)
        assert d.WORKER_CAUSES[seen["cause"]].step == "fail", kind


def test_the_train_worker_always_gets_a_train_step():
    """However the landing train's worker is read — its terminal, every clock,
    its nudge, whether it can be nudged at all — the cause it is classified with
    has a `train_step`, so `train_step` never raises. Exhaustive.

    Hidden precondition: it is classified the way `afk no-pr --train` does it —
    no verdict, no blocker, and no turn: when it was last told something is
    handed in as the `at` of a record that holds none. The table gives a train
    step to only some causes; the others need a verdict or one PR's held turn,
    which the train worker never has."""
    steps = set()
    for terminal, idle, nudged_at, progress, told, can_nudge in itertools.product(
            ("none", "busy", "idle"), TERMINAL_IDLE, NUDGED, PROGRESS, STAMPS, (True, False)):
        reading = {"terminal": terminal, "terminal_idle_seconds": idle, "state": None}
        seen = d.settled_by_worker_state(reading, NOW, GRACE, nudged_at) or d.classify_stopped(
            progress, idle, None, {}, NOW, GRACE, nudged_at=nudged_at, can_nudge=can_nudge,
            turn=d.next_turn(None, at=told, released=True) if told else None)
        steps.add(d.train_step({**_worker(seen["cause"]), "worker_state": None}))
    assert steps == set(typing.get_args(d.TrainStep))
    # …and the precondition is what holds it: classified as ONE PR's worker
    # would be, the same silence has no train step
    single = d.classify_stopped(None, 9000, None, {}, NOW, GRACE, nudged_at=OLD,
                                turn=d.next_turn(None, at=OLD))
    try:
        d.train_step({**_worker(single["cause"]), "worker_state": None})
        assert False, single
    except ValueError:
        pass


# --------------------------------------------------------------------------- #
# The cycle state machine                                                      #
# --------------------------------------------------------------------------- #

@functools.lru_cache(maxsize=None)
def _cycle_runs():
    """Seeded runs of a launcher's cycles → [[(event, the state it was given,
    what it was told, what it returned)...]...], an event being a `wake`, the
    `ticked` that follows a wake answered "tick", or a `drained`."""
    runs = []
    for seed in range(300):
        rng = random.Random(seed)
        count = lambda: rng.choice((0, 0, 1, 3))                            # noqa: E731
        state, run = d.cycle_state(None, **FACTS), []
        for _ in range(60):
            if rng.random() < 0.05:
                told = {"released": [9] * count(), "kept": list(range(count())), "errors": count()}
                out = d.cycle_drained(state, **told)
                run.append(("drained", state, told, out))
                state = out["state"]
                continue
            told = {"current_fp": rng.choice(("a", "a", "a", "a", "b")), "woke": rng.random() < 0.05}
            out = d.cycle_wake(state, **told)
            run.append(("wake", state, told, out))
            state = out["state"]
            if out["action"] == "tick":
                told = {"did": _did(dispatched=[1] * count(), in_flight=count(),
                                    frontier_remaining=count()),
                        "judgments": rng.choice((0, 0, 0, 1)), "errors": rng.choice((0, 0, 0, 1)),
                        "unseen": rng.choice((0, 0, 0, 1)), "left": rng.choice((None, "a", "c")),
                        "boards": rng.choice((None, {7: "0a1b2c3d"}))}
                out = d.cycle_ticked(state, **told)
                run.append(("ticked", state, told, out))
                state = out["state"]
        runs.append(run)
    return runs


def test_at_most_the_forced_tick_number_of_skips_between_two_ticks():
    """However long nothing moves, fewer than FORCE_TICK_AFTER_SKIPS cycles in a
    row are skipped: the one that would be that many ticks (ADR-0007).

    Hidden precondition: the count lives in the state, so the caller passes
    back the state each cycle returned, verbatim; the bound counts CYCLES, not
    time — a skipped cycle of an idle fleet sleeps the idle interval; and it is
    a bound on one run's cycles: a drain that met an error starts the count
    again, which costs nothing only because no cycle follows a drain."""
    longest = 0
    for run in _cycle_runs():
        skipped = 0
        for event, _, told, out in run:
            if event == "wake":
                skipped = skipped + 1 if out["action"] == "skip" else 0
            elif event == "drained" and told["errors"]:
                skipped = 0
            assert out["state"]["skips"] == skipped < d.FORCE_TICK_AFTER_SKIPS, out
            longest = max(longest, skipped)
    assert longest == d.FORCE_TICK_AFTER_SKIPS - 1         # the bound is reached, never passed


def test_a_cycle_is_never_skipped_while_unsettled_woken_or_with_a_moved_digest():
    """A cycle is skipped only when the last tick left nothing unsettled, no
    wake arrived, and the digest is the one the state keeps.

    Hidden precondition: "moved" is against the digest the STATE keeps — the
    fleet as the last tick left it (`left`), not as the cycle opened — and a
    state that keeps none (the first cycle) has always moved."""
    reasons = set()
    for run in _cycle_runs():
        for event, state, told, out in run:
            if event != "wake":
                continue
            reasons.add(out["reason"])
            if out["action"] == "skip":
                assert not state["unsettled"] and not told["woke"], (state, told)
                assert told["current_fp"] == state["fingerprint"] != "", (state, told)
    assert reasons == {"first", "changed", "forced", "unchanged", "unsettled", "wake"}


def test_a_cycle_holding_claims_never_sleeps_the_idle_interval():
    """A cycle that ends with claims in flight — ticked or skipped — sleeps at
    most the busy interval, and a skipped one refreshes the lease, so a held
    claim's heartbeat cannot lapse (ADR-0003).

    Hidden precondition: `in_flight` is the count the last tick reported. A
    skipped cycle reads it from the state, so a claim taken outside a tick is
    not held, as far as the pace goes, until a tick has counted it."""
    for did_work, in_flight, streak in itertools.product((True, False), range(1, 6), range(50)):
        assert d.pace(did_work, in_flight, streak) == d.BUSY_INTERVAL_SECONDS
    holding, sleeps = 0, set()
    for run in _cycle_runs():
        for event, _, _, out in run:
            sleep = out.get("sleep_seconds")
            sleeps.add(sleep)
            if event == "drained" or out.get("action") == "tick" or not out["state"]["in_flight"]:
                continue
            holding += 1
            assert sleep in (0, d.BUSY_INTERVAL_SECONDS), out
            if event == "wake":
                assert sleep == d.BUSY_INTERVAL_SECONDS and out["heartbeat"] is True, out
    assert holding > 1000 and d.IDLE_INTERVAL_SECONDS in sleeps    # the runs did go idle, too


def test_every_state_a_cycle_leaves_survives_its_own_json():
    """Whatever run of wakes, ticks and drains a fleet goes through, the state
    each one returns — through the JSON a launcher carries it in — is taken
    back unchanged.

    Hidden precondition: JSON has only string keys, so the law holds because
    `cycle_ticked` writes `boards` under strings whatever it was handed; and
    "taken back" means by `cycle_state`, which refuses a state no cycle leaves."""
    states = 0
    for run in _cycle_runs():
        for _, _, _, out in run:
            carried = json.loads(json.dumps(out["state"]))
            assert carried == out["state"] and d.cycle_state(carried) == out["state"], out
            states += 1
    assert states > 10_000


# --------------------------------------------------------------------------- #
# The tick's books                                                             #
# --------------------------------------------------------------------------- #

CONFIGS = tuple(d.resolve_config({"concurrency": concurrency, "gate": {"ci": ci}})
                for concurrency in range(6) for ci in ("required", "local"))
# the causes a claim's worker can be classified with, by what the claim holds
NO_PR_CAUSES = tuple(c for c in d.WORKER_CAUSES
                     if c not in ("awaiting_tick", "silent_on_turn", "silent_past_restart"))
LANDING_CAUSES = tuple(c for c in d.WORKER_CAUSES
                       if c not in ("silent_after_nudge", "silent_unnudgeable"))
# a PR that is to join the landing train holds no turn, and its silence after the
# nudge escalates it (`classify_stopped(joining=True)`)
JOINING_CAUSES = tuple(c for c in NO_PR_CAUSES if c not in ("silent_after_nudge",
                                                            "silent_unnudgeable"))
TRAIN_CAUSES = tuple(c for c, row in d.WORKER_CAUSES.items() if row.train_step)
HAS_PR = ("awaiting_turn", "awaiting_ci", "failure", "joining", "joined")


def _tick_world(rng, train):
    """One working set as a rebuild hands it over: claims of mine in every
    status — where a landing train runs (`train`), PRs that are to join it and
    PRs on it, and no turn anywhere; elsewhere at most ONE landing turn out —
    and stale claims, phantom locks and a frontier, no issue in two of those
    lists."""
    numbers = rng.sample(range(1, 60), 16)
    ready = ("joining", "joining", "joined") if train else ("awaiting_turn", "awaiting_turn",
                                                           "awaiting_ci", "failure")
    mine = []
    for n in numbers[:rng.randrange(7)]:
        status = rng.choice(("no_pr", "no_pr", *ready, "closed", "landed"))
        mine.append(_mine(n, status, pr=n * 10 if status in HAS_PR else None,
                          board_phase=d.BOARD_PHASE_OF[status],
                          starting=status == "no_pr" and rng.random() < 0.15))
    waiting = [r for r in mine if r["status"] == "awaiting_turn"]
    if waiting and rng.random() < 0.3:
        waiting[0].update(status="landing", stopped=rng.choice(
            (None, None, None, *(s for s in d.LAND_OUTCOMES if s not in ("merged", "joined")))))
    rest = iter(numbers[7:])
    stale, stale_closed, frontier = (sorted(itertools.islice(rest, rng.randrange(size)))
                                     for size in (4, 3, 5))
    return _working_set(mine, frontier, stale, stale_closed)


def _carried_out(ws, config, rng):
    """Carry one tick's plan out against a world where every step may end any
    way it can → ([(the step, its result, what it raised)...], what the plan
    returned)."""
    rows = {r["number"]: r for r in ws["mine"]}
    joined = [r["number"] for r in ws["mine"] if r["status"] == "joined"]
    log = []

    def cause(asked):
        return rng.choice({"landing": LANDING_CAUSES, "joining": JOINING_CAUSES}.get(
            rows[asked]["status"], NO_PR_CAUSES))

    def abandoned():
        twice = [n for n in joined if rng.random() < 0.3]
        return {"outcome": "abandoned", "issues": [n for n in joined if n not in twice],
                "escalated": twice}

    def answer(step):
        do, n = step["do"], step.get("issue")
        if do == "finish" and rng.random() > 0.05:
            return [(None, "not ready") if rng.random() < 0.15 else ({"ok": True}, None)
                    for _ in step["issues"]]
        if rng.random() < 0.12:
            raise RuntimeError(f"{do} failed")
        if do == "no-pr" and step.get("train"):
            return {"workers": [_worker(rng.choice(TRAIN_CAUSES))]}
        if do == "no-pr":
            return {"workers": [_worker(cause(asked), issue=asked) for asked in step["issues"]]}
        if do in ("turn", "restart"):
            return {"issue": n, "pr": n * 10, "head": "abc",
                    "outcome": rng.choice(("granted", "granted", *d.TURN_OUTCOMES))}
        if do == "train":
            return {"outcome": rng.choice(("granted", "landing", "idle", "stopped", "stopped"))}
        return {"abandon": abandoned,
                "fail": lambda: {"action": rng.choice(("retry", "retry", "escalate"))},
                "escalate": lambda: {"action": "escalate"},
                "reclaim": lambda: {"won": rng.random() < 0.7},
                "begin": lambda: rng.choice((d.BEGUN, d.BEGUN, d.BEGUN, d.LOST)),
                }.get(do, lambda: {"ok": True})()

    def carry_out(step):
        assert step["do"] in d.TICK_STEPS, step
        try:
            result, error = answer(step), None
        except RuntimeError as e:
            result, error = None, str(e)
        log.append((step, result, error))
        return result, error

    return log, d.follow(d.tick_plan(ws, CALL, config), carry_out)


@functools.lru_cache(maxsize=None)
def _ticks():
    """Seeded ticks → [(the working set, the steps carried out with how each
    ended, what the tick returned)...]."""
    ticks = []
    for seed in range(3000):
        rng = random.Random(seed)
        config = rng.choice(CONFIGS)
        ws = _tick_world(rng, d.train_runs(config))
        ticks.append((ws, *_carried_out(ws, config, rng)))
    return ticks


# The steps that move ONE issue's claim. `reclaim` is the first half of a stale
# claim's start (its `begin` is the second); `status` writes a board and moves
# nothing.
MOVES = ("turn", "restart", "nudge", "park", "fail", "escalate", "release", "begin")


def test_a_tick_hands_each_issue_at_most_one_transition_step():
    """No issue is handed two of the steps that move its claim in one tick, nor
    two judgments, nor two status boards.

    Hidden precondition: the working set names each issue ONCE — one `mine`
    row, and no issue in two of mine / stale / phantom locks / frontier
    (`assemble_working_set`'s partition) — at most one landing turn is out as
    the tick begins, a nudge of the train's worker is no issue's step, and `afk no-pr` answers one row per claim asked after. A
    stale claim's `reclaim` and `begin` are one start, in that order."""
    twice = 0
    for ws, log, done in _ticks():
        for kinds in (MOVES, ("reclaim",), ("status",)):
            moved = [step["issue"] for step, _, _ in log if step["do"] in kinds and "issue" in step]
            assert len(moved) == len(set(moved)), (kinds, moved)
        judged = [j["issue"] for j in done["judgments"]]
        assert len(judged) == len(set(judged)), judged
        steps = [(step["do"], step.get("issue")) for step, _, _ in log]
        for n in (row["number"] for row in ws["stale"]):
            if ("begin", n) in steps:
                assert steps.index(("reclaim", n)) < steps.index(("begin", n))
                twice += 1
    assert twice > 300


def test_a_tick_hands_out_at_most_one_landing_turn():
    """At most one of a tick's steps is answered with a landing turn granted —
    to one PR, or as a restart onto the turn already held (ADR-0029) — and the
    issues the tick reports granted or restarted are that one grant's. Where a
    landing train runs no PR is granted a turn at all: the tick asks for none,
    and at most once puts the train's worker on what joined (ADR-0048).

    Hidden precondition: at most one turn is out as the tick begins, and none
    where a train runs — a PR there is `joining` or `joined`, never waiting
    for a turn or holding one. The tick grants against the working set it was
    handed and never re-reads who holds the turn."""
    kinds, trains = set(), 0
    for ws, log, done in _ticks():
        grants = [(step, result) for step, result, error in log
                  if step["do"] in ("turn", "restart") and error is None
                  and result["outcome"] == "granted"]
        assert len(grants) <= 1, grants
        told = [step["issue"] for step, _ in grants]
        assert done["did"]["granted"] + done["did"]["restarted"] == told
        kinds |= {step["do"] for step, _ in grants}
        does = [step["do"] for step, _, _ in log]
        on_train = [r for r in ws["mine"] if r["status"] in ("joining", "joined")]
        assert does.count("train") <= 1 and does.count("abandon") <= 1
        if on_train:
            assert not {"turn", "restart"} & set(does), does
            assert ("train" in does) == any(r["status"] == "joined" for r in on_train)
            trains += "train" in does
        else:
            assert not {"train", "abandon"} & set(does), does
    assert kinds == {"turn", "restart"} and trains > 100


def test_a_tick_never_both_settles_and_starts_an_issue():
    """No issue is both settled by a tick — parked, escalated or cleared — and
    started by it: none is handed a `begin`, and none is reported dispatched or
    reclaimed.

    Hidden precondition: the same partition — an issue is one row of one list.
    A retry is neither: `afk fail` starts its own fresh worker and is counted
    `retried`, and the claim it keeps holds its slot."""
    settled_some = started_some = 0
    for ws, log, done in _ticks():
        did = done["did"]
        settled = {n for k in d.TickBooks.SETTLES for n in did[k]}
        started = {step["issue"] for step, _, _ in log if step["do"] in ("begin", "reclaim")}
        assert not settled & (started | set(did["dispatched"]) | set(did["reclaimed"])), did
        assert not settled & done["held"]
        settled_some += bool(settled)
        started_some += bool(settled and started)
    assert settled_some > 1000 and started_some > 500


def test_a_ticks_counts_read_back_from_its_steps():
    """Everything a tick reports — the issues per TICK_DID key, the claims in
    flight, the frontier left, the boards still held — is what the steps it
    handed out, and how each ended, come to.

    Hidden precondition: only a step that was carried out counts — one that
    raised settles nothing and starts nothing; a claim is in flight from the
    moment it is taken (a stale claim won, whether or not its worker then
    started) but a frontier issue only once its worker runs; and a frontier
    issue a peer won is off the frontier without being held."""
    in_flight = set()
    for ws, log, done in _ticks():
        did = {k: [] for k in d.TICK_DID}
        mine = {r["number"] for r in ws["mine"]}
        frontier = {i["number"] for i in ws["frontier"]["dispatch"]}
        joined = [r["number"] for r in ws["mine"] if r["status"] == "joined"]
        took, off_frontier = set(), set()
        for step, result, error in log:
            do, n = step["do"], step.get("issue")
            if error is not None:
                continue
            if do == "turn" and result["outcome"] == "granted":
                did["granted"].append(n)
            elif do == "restart" and result["outcome"] == "granted":
                did["restarted"].append(n)
            elif do == "abandon":
                did["abandoned"] += result["issues"]
                did["escalated"] += result["escalated"]
            elif do == "nudge":
                did["nudged"] += joined if n is None else [n]
            elif do in ("fail", "escalate"):
                did["escalated" if result["action"] == "escalate" else "retried"].append(n)
            elif do in ("park", "release"):
                did["parked" if do == "park" else "cleared"].append(n)
            elif do == "reclaim" and result["won"]:
                took.add(n)
            elif do == "begin" and n in frontier:
                off_frontier.add(n)                 # begun, or lost to a peer
            elif do == "finish":
                for started, (worker, failed) in zip(step["issues"], result):
                    if failed is None:
                        did["reclaimed" if started in took else "dispatched"].append(started)
        settled = {n for k in d.TickBooks.SETTLES for n in did[k]} & mine
        counts = {"in_flight": len(mine - settled) + len(took) + len(frontier & set(did["dispatched"])),
                  "frontier_remaining": len(frontier - off_frontier)}
        assert done["did"] == {**did, **counts}, log
        assert done["held"] == (mine - settled) | set(did["dispatched"]) | set(did["reclaimed"])
        assert len(done["errors"]) >= sum(error is not None for _, _, error in log)
        in_flight.add(counts["in_flight"])
    assert in_flight >= set(range(7))


# --------------------------------------------------------------------------- #
# Claims                                                                       #
# --------------------------------------------------------------------------- #

def test_every_claim_lands_in_exactly_one_of_mine_peer_live_stale_whatever_the_order():
    """`classify_claims` puts each claim in exactly one of its three lists —
    the one its owner and that owner's heartbeat alone decide — and the lists
    do not depend on the order the claims were read in.

    Hidden precondition: one claim per issue (a claim is a ref, and its number
    is the ref's name), and one heartbeat per instance, so two claims of one
    owner are never split between live and stale. Mine is mine whatever MY
    heartbeat says; a marker naming no instance is nobody's, and stale."""
    now = 100_000
    beats = {"none": None, "fresh": now - 10, "at-ttl": now - TTL, "past-ttl": now - TTL - 1,
             "skewed": now + SKEW, "ahead": now + SKEW + 1}
    live = {"fresh", "at-ttl", "skewed"}
    owners = ("me", None, *(f"peer-{beat}" for beat in beats))
    rng, reached = random.Random(128), set()
    for _ in range(1500):
        heartbeats = {f"peer-{beat}": at for beat, at in beats.items() if at is not None}
        mine_beats = rng.choice(tuple(beats.values()))
        if mine_beats is not None:
            heartbeats["me"] = mine_beats
        claims = [{"number": n, "instance": rng.choice(owners), "host": None, "ts": None, "sha": ""}
                  for n in rng.sample(range(1, 200), rng.randrange(14))]
        want = {"mine": [], "peer_live": [], "stale": []}
        for claim in claims:
            owner = claim["instance"]
            where = ("mine" if owner == "me" else
                     "peer_live" if owner and owner.partition("-")[2] in live else "stale")
            want[where].append(claim["number"])
            reached.add((owner, where))
        got = d.classify_claims(claims, heartbeats, "me", now, TTL)
        assert got == {where: sorted(numbers) for where, numbers in want.items()}, claims
        assert sorted(n for numbers in got.values() for n in numbers) == \
            sorted(c["number"] for c in claims)
        for _ in range(3):
            rng.shuffle(claims)
            assert d.classify_claims(claims, heartbeats, "me", now, TTL) == got
    assert len(reached) == len(owners)          # every owner, each always in its one list


# --------------------------------------------------------------------------- #
# The checks rule                                                              #
# --------------------------------------------------------------------------- #

def test_the_checks_rule_agrees_in_its_three_places_on_every_checks_state():
    """What a PR's checks come to is read in three places — `claim_status`
    (which row the claim is), `checks_gate` (may it land; `turn_gate` asks it)
    and `checks_owed` (is a landing still waiting) — and on each of the four
    checks states they say the same thing: red fails in all, pending waits in
    all, green and no-checks-at-all go on to the turn.

    Hidden precondition: all three read the SAME head, with gate.ci `required`,
    of a PR that does not hold the turn. A landing claim's checks are the
    landing's own business, `local` reads none of them, a read of a head
    GitHub has not caught up with is owed whatever it says, and a head just
    pushed that shows no checks where the PR had them is owed, not checkless."""
    states = (*typing.get_args(d.ChecksState), None)
    verdicts = {"SUCCESS": "ok", "NEUTRAL": "ok", "SKIPPED": "ok", "FAILURE": "red",
                "CANCELLED": "red", "PENDING": "pending", "STALE": "pending", "": "pending"}
    produced = {d.pr_checks_state([{"conclusion": c} for c in rollup])
                for size in range(4) for rollup in itertools.product(verdicts, repeat=size)}
    assert produced == set(states)              # the four states are every state there is
    for state in states:
        status = d.claim_status(True, state, "required")
        gate = d.checks_gate(state)
        owed = d.checks_owed(state, at_head=True, had_checks=False)
        assert (status == "awaiting_ci") == (gate == "awaiting_ci") == owed, state
        assert (status == "failure") == (gate == "gate_red"), state
        assert (status == "awaiting_turn") == (gate in ("green", "no_checks")), state
        for allow, verify, verified in itertools.product((False, True), (False, True),
                                                         (None, "old", "head")):
            gate = d.checks_gate(state, allow)
            assert (gate == "no_checks") == (state is None and not allow)
            asked = "needs_verify" if verify and verified != "head" else "ready"
            assert d.turn_gate("required", state, allow, verify, verified, "head") == \
                (asked if gate == "green" else gate), (state, allow, verify, verified)
            # outside the precondition nothing is read from the checks at all
            assert d.turn_gate("local", state, allow, verify, verified, "head") == asked
        assert d.claim_status(True, state, "local") == "awaiting_turn"
        for closed, landing, landed in itertools.product((False, True), repeat=3):
            for has_pr in (False, True):
                if closed or landing or not has_pr:
                    assert d.claim_status(has_pr, state, "required", closed, landing, landed) == \
                        d.claim_status(has_pr, "green", "required", closed, landing, landed)
        assert d.checks_owed(state, at_head=False, had_checks=False) is True
        assert d.checks_owed(state, at_head=True, had_checks=True) is (state in ("pending", None))


# --------------------------------------------------------------------------- #
# Blocker chains                                                               #
# --------------------------------------------------------------------------- #

def _reaches(edges, nodes):
    """Reachability as a fixpoint — {n: every issue a chain of blockers leads
    from n to, n itself among them} — not as a walk."""
    reach = {n: {n, *(edges.get(n) or [])} for n in nodes}
    grew = True
    while grew:
        grew = False
        for n in nodes:
            wider = set().union(*(reach[m] for m in reach[n]))
            if not wider <= reach[n]:
                reach[n] |= wider
                grew = True
    return reach


def test_a_blocker_chain_terminates_and_is_reachability_even_through_cycles():
    """`depends_on(start, target, edges)` returns on every graph — with cycles,
    self-loops and blockers nobody listed — and says exactly whether a chain of
    blockers leads from `start` to `target`.

    Hidden precondition: a chain may be EMPTY — every issue depends on itself,
    listed or not — and `edges` is all there is: an issue with no entry has no
    blockers, so a chain that leaves what was walked ends there."""
    def holds(edges, nodes):
        reach = _reaches(edges, nodes)
        for start, target in itertools.product(nodes, repeat=2):
            assert d.depends_on(start, target, edges) == (target in reach[start]), (edges, start, target)

    # every graph on three issues: 512 of them, most with a cycle
    pairs = list(itertools.product(range(3), repeat=2))
    for drawn in itertools.product((False, True), repeat=len(pairs)):
        holds({n: [b for (a, b), there in zip(pairs, drawn) if there and a == n] for n in range(3)},
              range(3))
    rng, cyclic = random.Random(128), 0
    for _ in range(600):
        size = rng.randrange(1, 9)
        nodes = range(size + 2)             # the last two are named as blockers, never listed
        edges = {n: [rng.randrange(size + 2) for _ in range(rng.randrange(4))]
                 for n in range(size) if rng.random() < 0.9}
        holds(edges, nodes)
        reach = _reaches(edges, nodes)
        cyclic += any(a != b and a in reach[b] and b in reach[a] for a in nodes for b in nodes)
    assert cyclic > 100
