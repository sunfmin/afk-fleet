#!/usr/bin/env python3
"""
Fixture tests for the afk-fleet decision core — pure, no git/gh/network.
Run: under pytest — the command is `gate.local_command` in docs/agents/afk-fleet.md

These cover the correctness-critical verdicts (esp. classify_claims: the
mine/peer_live/stale partition whose wrong answer silently corrupts state).
"""
import itertools
import json
import os
import random
import re
import shlex
import typing

import afk_decide as d

TTL = d.CLAIM_LEASE_TTL_SECONDS

# The rows gh and the ref scan hand the core are whole (`afk_decide.Issue`,
# `PullRequest`, `Claim`): a fixture names what its test is about, and these
# say nothing for the rest.
_ISSUE = {"id": 0, "title": "", "labels": [], "updatedAt": "", "blocked_by": 0}
_ELIGIBLE = {**_ISSUE, "claimed": False, "has_open_pr": False, "open_blockers": 0}
_PR = {"title": "", "headRefName": "", "headRefOid": "", "updatedAt": "",
       "statusCheckRollup": None, "closingIssuesReferences": []}
_CLAIM = {"instance": None, "host": None, "ts": None, "sha": ""}


def _whole(blank, rows):
    return [{**blank, **row} for row in rows]


def _gathered(issues, prs, claims):
    return _whole(_ISSUE, issues), _whole(_PR, prs), _whole(_CLAIM, claims)


def test_select_frontier():
    issues = [
        {"number": 101, "labels": ["ready-for-agent"], "claimed": False, "has_open_pr": False, "open_blockers": 0},
        {"number": 102, "labels": ["ready-for-agent"], "open_blockers": 1},
        {"number": 103, "labels": ["ready-for-agent", "epic"], "open_blockers": 0},
        {"number": 104, "labels": ["ready-for-agent"], "claimed": True},
        {"number": 105, "labels": ["ready-for-agent"], "has_open_pr": True},
        {"number": 107, "labels": []},
    ]
    issues = _whole(_ELIGIBLE, issues)
    r = d.select_frontier(issues, "ready-for-agent", ["epic", "prd"])
    assert r["dispatch"] == [101], r
    reasons = {e["number"]: e["reason"] for e in r["excluded"]}
    assert "1 open blocker" in reasons[102]
    assert "epic label (epic)" == reasons[103]
    assert "already claimed" in reasons[104]
    assert "open linked PR" in reasons[105]
    assert "no ready-for-agent label" == reasons[107]
    # every issue lands in exactly one of the two lists
    assert len(r["dispatch"]) + len(r["excluded"]) == len(issues)


def test_the_frontier_reads_each_issues_own_blocker_count():
    # the open-blocker count rides on the issue row — the list read carries it —
    # so the frontier needs no read of its own, and no pre-pass to ration one
    issues = [{"number": n, "labels": ["ready-for-agent"]} for n in (1, 2, 3, 4)]
    issues.append({"number": 5, "labels": ["ready-for-agent", "epic"]})
    prs = [{"number": 30, "closingIssuesReferences": [{"number": 3}]}]
    claims = [{"number": 2, "instance": "peer"}]
    cfg = d.resolve_config({"epic_labels": ["epic"]})
    issues, prs, claims = _gathered(issues, prs, claims)
    ws = d.assemble_working_set(issues, prs, claims, {}, "me", 0, cfg)
    # 2 claimed, 3 has an open PR, 5 is an epic; a row with no count is unblocked
    assert [i["number"] for i in ws["frontier"]["dispatch"]] == [1, 4]
    issues[3]["blocked_by"] = 2
    ws = d.assemble_working_set(issues, prs, claims, {}, "me", 0, cfg)
    assert [i["number"] for i in ws["frontier"]["dispatch"]] == [1]
    assert {"number": 4, "reason": "2 open blocker(s)"} in ws["frontier"]["excluded"]


def test_is_stale_and_due():
    assert d.is_stale(None, 1000, TTL) is True          # never beat → dead
    assert d.is_stale(1000, 1000 + TTL, TTL) is False   # exactly ttl → still live
    assert d.is_stale(1000, 1000 + TTL + 1, TTL) is True
    # the one lease comparison: the takeover picker reads a heartbeat exactly at
    # the lease as live, as the partition of claims does
    at_lease = {"claims": [{"number": 1, "instance": "peer", "host": "h", "sha": "s"}],
                "heartbeats": {"peer": 1000}, "me": "me", "now": 1000 + TTL, "ttl": TTL}
    assert d.classify_claims(**at_lease)["peer_live"] == [1]
    assert d.group_instances(**at_lease)[0]["fresh"] is True
    assert d.heartbeat_due(None, 1000, TTL) is True
    assert d.heartbeat_due(1000, 1000 + TTL // 3, TTL) is False
    assert d.heartbeat_due(1000, 1000 + TTL // 3 + 2, TTL) is True


def test_a_stamp_from_the_future_is_not_evidence():
    """Age is `now − written`, and the stamp is the writing host's clock: beyond
    the one tolerance allowed for skew, a stamp ahead of the reader is read as
    missing — wherever an age is acted on."""
    now, skew = 1_000_000, d.CLOCK_SKEW_TOLERANCE_SECONDS
    # the one reading: within the tolerance "just now", past it nothing at all
    assert d.stamp_age(now - 40, now) == 40
    assert d.stamp_age(now + skew, now) == 0
    assert d.stamp_age(now + skew + 1, now) is None
    assert d.stamp_age(None, now) is None

    # a heartbeat a day ahead does not keep a dead fleet's claims live for a day
    # past the lease — and its own fleet beats again rather than wait for it
    assert d.is_stale(now + skew, now, TTL) is False
    assert d.is_stale(now + skew + 1, now, TTL) is True
    assert d.is_stale(now + 86400, now, TTL) is True
    assert d.heartbeat_due(now + skew, now, TTL) is False
    assert d.heartbeat_due(now + 86400, now, TTL) is True
    part = d.classify_claims([{"number": 7, "instance": "peer"}], {"peer": now + 86400},
                             "me", now, TTL)
    assert part == {"mine": [], "peer_live": [], "stale": [7]}
    assert d.group_instances([], {"peer": now + 86400}, "me", now, TTL)[0]["fresh"] is False

    # a recorded gate run dated ten days ahead is void now, not trusted for eleven
    ahead = lambda s: d.gate_record("abc123", "make test", now + s)   # noqa: E731
    assert d.gate_record_void(ahead(skew), now) is None
    assert "ahead of this clock" in d.gate_record_void(ahead(skew + 1), now)
    assert "ahead of this clock" in d.gate_record_void(ahead(10 * 86400), now)


def test_classify_claims():
    now = 100_000
    claims = [
        {"number": 1, "instance": "me"},                 # mine
        {"number": 2, "instance": "peerA"},              # peer, fresh hb → live
        {"number": 3, "instance": "peerB"},              # peer, stale hb → reclaimable
        {"number": 4, "instance": "peerC"},              # peer, no hb at all → reclaimable
        {"number": 5, "instance": None},                 # malformed marker → reclaimable
    ]
    heartbeats = {
        "me": now - 10,
        "peerA": now - 100,           # well within ttl
        "peerB": now - TTL - 50,      # expired
    }
    r = d.classify_claims(claims, heartbeats, "me", now, TTL)
    assert r["mine"] == [1], r
    assert r["peer_live"] == [2], r
    assert r["stale"] == [3, 4, 5], r


def test_classify_claims_my_own_expired_stays_mine():
    # My own claim is mine even if MY heartbeat lapsed — I reconcile it locally,
    # a peer would see it stale. (Guards against a fleet abandoning its own work.)
    now = 100_000
    r = d.classify_claims([{"number": 9, "instance": "me"}],
                          {"me": now - TTL - 1}, "me", now, TTL)
    assert r == {"mine": [9], "peer_live": [], "stale": []}, r


def test_claim_status():
    # → the status: what the tick does next. What the board shows is BOARD_PHASE_OF's
    assert d.claim_status(False, None, "required") == "no_pr"
    assert d.claim_status(True, "green", "required") == "awaiting_turn"
    assert d.claim_status(True, "red", "required") == "failure"
    assert d.claim_status(True, "pending", "required") == "awaiting_ci"
    # no checks at all is not "checks pending": nothing is running, so waiting
    # would park the claim forever in a repo with no CI. It goes to `afk turn`,
    # whose `no_checks` outcome asks the tick
    assert d.claim_status(True, None, "required") == "awaiting_turn"
    # with no PR the checks are nobody's: stale rollup data cannot invent a status
    assert d.claim_status(False, "green", "required") == "no_pr"

    # gate.ci: local — there are no checks to WAIT on, because gating is an action
    # the landing takes (ADR-0012). Any open PR is awaiting its turn, and a red
    # remote run (the repo's own on:push CI, which the fleet does not gate on)
    # must never park the claim in `failure` forever.
    for checks in ("green", "red", "pending", None):
        assert d.claim_status(True, checks, "local") == "awaiting_turn", checks
    assert d.claim_status(False, None, "local") == "no_pr"

    # every board phase a status is shown as is one the board renders — the tick
    # never translates — and only a row the tick releases has none: its board is
    # final already (`closed`), or the release writes it (`landed`)
    assert {st for st, phase in d.BOARD_PHASE_OF.items() if phase is None} == {"closed", "landed"}
    assert {p for p in d.BOARD_PHASE_OF.values() if p} <= set(d.STATUS_PHASES)
    assert (d.BOARD_PHASE_OF["failure"], d.BOARD_PHASE_OF["awaiting_ci"],
            d.BOARD_PHASE_OF["no_pr"]) == ("ci_failed", "pr_open", "claimed")

    # CLAIM_STATUSES is exactly what it can return: no status the docs were never
    # held to, and none listed that cannot happen
    seen = {d.claim_status(has_pr, checks, ci, closed=closed, landing=landing, landed=landed,
                           fixing=fixing)
            for ci in d.GATE_CI_MODES for has_pr in (True, False)
            for checks in ("green", "red", "pending", None)
            for closed in (True, False) for landing in (True, False)
            for landed in (True, False) for fixing in (True, False)}
    assert seen == set(d.CLAIM_STATUSES)

    # a PR that gave its turn up and is being fixed off it (ADR-0045): `fixing`
    # whatever its checks say — the red run is what its worker is fixing, never a
    # route into `afk fail` — and only while there is a PR on an open issue
    for ci in d.GATE_CI_MODES:
        for checks in ("green", "red", "pending", None):
            assert d.claim_status(True, checks, ci, fixing=True) == "fixing", (ci, checks)
        assert d.claim_status(True, "red", ci, closed=True, fixing=True) == "closed"
        assert d.claim_status(False, None, ci, fixing=True) == "no_pr"
    assert d.BOARD_PHASE_OF["fixing"] == "fixing"

    # no open PR, the issue open, and its landing on the target: a merge batch
    # pushed it and was cut before it closed the issue. Never `no_pr` — nobody is
    # asked why work that landed has no PR. An open PR is still landing, and a
    # closed issue is closed
    for ci in d.GATE_CI_MODES:
        assert d.claim_status(False, None, ci, landed=True) == "landed"
        assert d.claim_status(False, None, ci, closed=True, landed=True) == "closed"
        assert d.claim_status(True, "green", ci, landed=True) == "awaiting_turn"
        assert d.claim_status(True, "green", ci, landing=True, landed=True) == "landing"

    # the issue is CLOSED but the claim is still mine — its worker landed the PR, or
    # an `afk close` crashed before releasing. Nothing else about it matters, and
    # there is no board to write: the only thing left to do is release.
    for ci in d.GATE_CI_MODES:
        for has_pr in (True, False):
            assert d.claim_status(has_pr, "red", ci, closed=True) == "closed"

    # a PR that holds the landing turn: whatever the checks say, in either mode, the
    # claim is `landing` — its worker is at it, and red or pending checks on a head
    # the sync just pushed are the landing's own to wait out (ADR-0027)
    for ci in d.GATE_CI_MODES:
        for checks in ("green", "red", "pending", None):
            assert d.claim_status(True, checks, ci, landing=True) == "landing", (ci, checks)
        assert d.claim_status(True, "green", ci, closed=True, landing=True) == "closed"
        assert d.claim_status(False, None, ci, landing=True) == "no_pr"
    assert {"landing", "awaiting_turn"} <= set(d.STATUS_PHASES)


def test_turn_record_round_trips_and_is_held_only_by_the_claims_owner():
    body = d.turn_comment(d.single_turn(None, "fl-1", 1234))
    # the marker leads, the same facts follow for a human
    assert body.startswith("<!--afk:turn instance=fl-1 at=1234-->\n")
    assert "holds the landing turn" in body and "`fl-1`" in body and "`afk land`" in body
    rec = d.latest_turn([{"id": 7, "body": "a human note"}, {"id": 8, "body": body}])
    single = {"restarted": None, "given_up": None, "batch": None, "members": [], "phase": None,
              "unbatched": None, "of": None, "released": False}
    assert rec == {"instance": "fl-1", "at": 1234, "verified": None, "allow_no_checks": False,
                   "stopped": None, "head": None, "comment_id": 8, **single}
    assert d.latest_turn([]) is None and d.latest_turn([{"id": 1, "body": "x"}]) is None
    assert d.latest_turn([{"id": 1, "body": None}]) is None and d.latest_turn(None) is None

    # the tick's two judgments travel on the marker, and so does where the landing stopped
    full = d.turn_comment(d.next_turn(
        d.single_turn(None, "fl-1", 2000.9, verified="v" * 40, allow_no_checks=True),
        stopped="awaiting_ci", head="h" * 40))
    assert full.startswith(f"<!--afk:turn instance=fl-1 at=2000 verified={'v' * 40} "
                           f"allow_no_checks=1 stopped=awaiting_ci head={'h' * 40}-->\n")
    assert "stopped with `awaiting_ci` on `hhhhhhhhhhhh`" in full
    rec2 = d.latest_turn([{"id": 8, "body": body}, {"id": 9, "body": full},
                          {"id": 10, "body": "<!--afk:turn at=3-->"}])       # names nobody: no record
    assert rec2 == {"instance": "fl-1", "at": 2000, "verified": "v" * 40, "allow_no_checks": True,
                    "stopped": "awaiting_ci", "head": "h" * 40, "comment_id": 9, **single}
    # a `stopped` word the code does not know is not a stop
    odd = d.latest_turn([{"id": 1, "body": "<!--afk:turn instance=x at=5 stopped=bogus head=abc-->"}])
    assert (odd["stopped"], odd["head"]) == (None, None)

    # held only by the instance that holds the claim: a turn granted by the fleet a
    # claim was taken over from is nobody's — and no claim, no turn
    assert d.held_turn(rec, "fl-1") is rec
    assert d.held_turn(rec, "fl-2") is None and d.held_turn(rec, None) is None
    assert d.held_turn(rec, "") is None and d.held_turn(None, "fl-1") is None

    # each vocabulary is closed: neither subcommand can stop with a word nobody routes
    assert all(d.land_outcome(o) == o for o in d.LAND_OUTCOMES)
    assert all(d.turn_outcome(o) == o for o in d.TURN_OUTCOMES)
    assert all(d.batch_turn_outcome(o) == o for o in d.BATCH_TURN_OUTCOMES)
    assert set(d.LAND_WAITS) < set(d.LAND_OUTCOMES) and "merged" not in d.LAND_WAITS
    # one PR's turn and a merge batch's are two holders with a vocabulary each
    # (ADR-0036): neither stops with a word only the other is routed on
    for check, word in ((d.land_outcome, "granted"), (d.turn_outcome, "merged"),
                        (d.land_outcome, "handed_back"),
                        (d.turn_outcome, "too_few"), (d.turn_outcome, "abandoned"),
                        (d.batch_turn_outcome, "gate_red"),
                        (d.batch_turn_outcome, "needs_verify"),
                        (d.batch_turn_outcome, "merged")):
        try:
            check(word)
            raise AssertionError(word)
        except ValueError:
            pass


def test_a_batchs_turn_is_one_marker_on_every_member_and_leaving_it_is_remembered():
    """ADR-0029: a merge batch's turn is the turn marker, on every member PR,
    naming the batch, its members and its phase. A PR that leaves a batch
    without landing holds no turn — and says so on every marker written for it
    afterwards, so it is never batched again."""
    members = [{"issue": 1, "pr": 10}, {"issue": 2, "pr": 20}, {"issue": 3, "pr": 30}]
    body = d.turn_comment(d.batch_turn(None, "fl-1", 500, "fl-1-500", members, "gating"))
    assert body.startswith("<!--afk:turn instance=fl-1 at=500 batch=fl-1-500 "
                           "members=1:10,2:20,3:30 phase=gating-->\n")
    assert "#10, #20, #30" in body and "one merge commit each" in body
    rec = d.latest_turn([{"id": 4, "body": body}])
    assert (rec["batch"], rec["members"], rec["phase"]) == ("fl-1-500", members, "gating")
    assert (rec["unbatched"], rec["released"], rec["stopped"]) == (None, False, None)
    # held like any turn: by the instance that holds the claim, and by no other
    assert d.held_turn(rec, "fl-1") is rec and d.held_turn(rec, "fl-2") is None
    # the PR landed its issue under a claim made before the batch's turn, by the
    # instance that granted it — not one made since (the issue was reopened), not
    # another instance's, not a claim that names nobody
    claim = {"number": 1, "instance": "fl-1", "host": "h", "ts": 500, "sha": "c"}
    assert d.landed_under(rec, claim) and d.landed_under(rec, {**claim, "ts": 100})
    for other in ({"ts": 501}, {"instance": "fl-2"}, {"instance": None, "ts": None}):
        assert not d.landed_under(rec, {**claim, **other}), other
    assert not d.landed_under(None, claim)
    assert not d.landed_under(d.single_turn(None, "fl-1", 600), claim)     # no batch landed it

    # leaving: the marker holds NO turn, whoever reads it — and remembers why
    for why in d.UNBATCHED:
        left = d.latest_turn([{"id": 4, "body": body},
                              {"id": 5, "body": d.turn_comment(d.unbatched_turn(None, "fl-1", 600, "fl-1-500", why))}])
        assert (left["unbatched"], left["released"], left["batch"]) == (why, True, None)
        assert d.held_turn(left, "fl-1") is None
    # a single turn granted afterwards carries the fact along, and IS held
    later = d.latest_turn([{"id": 6, "body": d.turn_comment(d.single_turn(left, "fl-1", 700))}])
    assert (later["unbatched"], later["of"], later["released"]) == (left["unbatched"], "fl-1-500",
                                                                    False)
    assert d.held_turn(later, "fl-1") is later

    # closed vocabularies; a phase or a reason nobody knows is not written, nor read
    assert all(d.batch_outcome(o) == o for o in d.BATCH_OUTCOMES)
    for bad in (lambda: d.batch_outcome("merged"),
                lambda: d.turn_comment(d.batch_turn(None, "fl-1", 1, "b", members, "bisecting")),
                lambda: d.turn_comment(d.unbatched_turn(None, "fl-1", 1, "b", "bored")),
                lambda: d.next_turn(None, at=1, bogus=1)):
        try:
            bad()
            raise AssertionError("accepted")
        except ValueError:
            pass
    odd = d.latest_turn([{"id": 1, "body": "<!--afk:turn instance=x at=5 batch=b members=1:10,zz,3 "
                                           "phase=bogus unbatched=bored-->"}])
    assert (odd["members"], odd["phase"], odd["unbatched"]) == ([{"issue": 1, "pr": 10}], None, None)


def _turn_shapes():
    """One record of every shape a turn marker takes, as a rewrite makes them."""
    members = [{"issue": 1, "pr": 10}, {"issue": 2, "pr": 20}]
    single = d.single_turn(None, "fl-1", 100)
    judged = d.single_turn(None, "fl-1", 100, verified="v" * 40, allow_no_checks=True)
    left = d.unbatched_turn(d.batch_turn(None, "fl-1", 200, "fl-1-200", members, "stacking"),
                            "fl-1", 300, "fl-1-200", "left_out")
    return {"single": single,
            "single, judged and stopped": d.next_turn(judged, at=150, stopped="needs_verify",
                                                      head="h" * 40),
            "single, restarted": d.single_turn(judged, "fl-1", 160, verified="v" * 40,
                                               allow_no_checks=True, restarted=160),
            "batch": d.batch_turn(None, "fl-1", 200, "fl-1-200", members, "gating"),
            "left a batch": left,
            "single, after leaving a batch": d.single_turn(left, "fl-1", 400, verified="v" * 40)}


def test_a_turn_marker_round_trips_in_every_shape():
    """Render then parse gives the record back — one PR's turn, a merge batch's,
    and the marker of a PR that left a batch — so a marker rewritten from the
    record it was read as loses nothing."""
    wording = {"single": "holds the landing turn", "single, judged and stopped": "stopped with",
               "single, restarted": "a new one was started onto it",
               "batch": "is in a merge batch", "left a batch": "was in merge batch `fl-1-200`",
               "single, after leaving a batch": "holds the landing turn"}
    for shape, record in _turn_shapes().items():
        record = {**record, "comment_id": 7}
        body = d.turn_comment(record)
        assert d.latest_turn([{"id": 7, "body": body}]) == record, shape
        assert wording[shape] in body, shape
        # …and a rewrite that changes nothing writes the same comment
        assert d.turn_comment(d.next_turn(d.latest_turn([{"id": 7, "body": body}]))) == body, shape


def test_a_field_no_rewrite_names_survives_it():
    """A marker is rewritten as the record read plus what changed: that a PR left
    a batch survives a landing that stops and a turn granted again, and the
    tick's two judgments survive a landing that stops — by construction, not
    because each rewrite remembers to pass them along."""
    def read(record):
        return d.latest_turn([{"id": 9, "body": d.turn_comment({**record, "comment_id": 9})}])

    members = [{"issue": 1, "pr": 10}, {"issue": 2, "pr": 20}]
    left = read(d.unbatched_turn(read(d.batch_turn(None, "fl-1", 1, "fl-1-1", members, "gating")),
                                 "fl-1", 2, "fl-1-1", "dissolved"))
    assert (left["unbatched"], left["of"], left["released"], left["batch"]) == \
        ("dissolved", "fl-1-1", True, None)
    granted = read(d.single_turn(left, "fl-1", 3, verified="v" * 40, allow_no_checks=True))
    # a landing that stops names only when, with what, and on which head
    stopped = read(d.next_turn(granted, at=4, stopped="awaiting_ci", head="h" * 40))
    assert stopped == {**granted, "at": 4, "stopped": "awaiting_ci", "head": "h" * 40}
    assert (stopped["unbatched"], stopped["of"]) == ("dissolved", "fl-1-1")
    assert (stopped["verified"], stopped["allow_no_checks"]) == ("v" * 40, True)
    # a turn granted again names the instance, when, and the judgments it was granted on
    again = read(d.single_turn(stopped, "fl-1", 5, verified="w" * 40))
    assert (again["unbatched"], again["of"], again["released"]) == ("dissolved", "fl-1-1", False)
    assert (again["stopped"], again["head"], again["verified"], again["allow_no_checks"]) == \
        (None, None, "w" * 40, False)
    # the comment a record was read from is the one its rewrite replaces
    assert again["comment_id"] == d.next_turn(again, at=6)["comment_id"] == 9
    assert d.next_turn(None, instance="x", at=6.9)["at"] == 6
    # a restart (ADR-0035) is named by the turn that carries it over, so a landing
    # that stops keeps it, and a re-delivery of the same turn passes it on — while
    # a turn granted anew says none: another instance's restart does not outlive it
    restarted = read(d.single_turn(again, "fl-1", 7, restarted=7))
    assert (restarted["restarted"], restarted["at"], restarted["stopped"]) == (7, 7, None)
    stopped = read(d.next_turn(restarted, at=8, stopped="conflict", head="c" * 40))
    assert stopped["restarted"] == 7
    assert read(d.single_turn(stopped, "fl-1", 9, restarted=stopped["restarted"]))["restarted"] == 7
    assert read(d.single_turn(stopped, "fl-2", 9))["restarted"] is None
    # and nothing of a turn says a batch's worker was restarted: a batch is abandoned
    assert d.batch_turn(stopped, "fl-1", 10, "fl-1-10", members, "stacking")["restarted"] == 7
    assert d.restartable_turn(d.batch_turn(None, "fl-1", 10, "fl-1-10", members, "stacking")) is False


def test_a_batch_is_known_by_its_id_wherever_orca_puts_its_branch():
    batch = d.batch_id("fl-1-x", 1700000000)
    assert batch == "fl-1-x-t1700000000" and d.batch_name(batch) == "afk-batch-fl-1-x-t1700000000"
    heads = ["main", "felix/afk-batch-fl-1-t170", "afk-batch-fl-1-t170-2", "felix/afk-batch-fl-1-t1700",
             "felix/afk-batch-fl-2-t170", "felix/issue-3-x", "felix/afk-batch-fl-1-t170-x"]
    assert d.batch_branches(heads, "fl-1-t170") == ["afk-batch-fl-1-t170-2", "felix/afk-batch-fl-1-t170"]
    # a batch's id says which instance formed it — and only that one
    assert d.batch_formed_by(batch, "fl-1-x") and not d.batch_formed_by(batch, "fl-1")
    assert not d.batch_formed_by(batch, "") and not d.batch_formed_by(None, "fl-1-x")
    # the PR a stacked merge commit's subject names
    subject = d.stack_message("a title", 12, 3).splitlines()[0]
    assert d.stacked_pr("p1 p2", subject) == 12
    assert d.stacked_pr("p1 p2", "work: fix.txt") is None and d.stacked_pr("p1 p2", None) is None
    # …and only a merge commit's: the same subject on a commit with one parent, or none, names no PR
    assert d.stacked_pr("p1", subject) is None and d.stacked_pr("", subject) is None
    # the subject ENDS with it: a revert quotes it, and names no PR
    assert d.stacked_pr("p1 p2", f'Revert "{subject}"') is None
    assert d.batch_branches(heads, "fl-9-t1") == [] and d.batch_branches(None, "fl-1-t170") == []

    def wt(branch, at=1, **more):
        return {"path": f"/wt/{branch}", "branch": f"refs/heads/{branch}",
                "projectId": "github:acme/widgets", "lastActivityAt": at, **more}

    rows = [wt("felix/afk-batch-fl-1-t170"), wt("felix/afk-batch-fl-1-t170-2", at=5),
            wt("felix/afk-batch-fl-1-t200"), wt("felix/afk-batch-fl-2-t170"),
            wt("felix/afk-batch-fl-1-t300", isArchived=True), wt("felix/issue-3-x", linkedIssue=3),
            wt("felix/afk-batch-fl-1-t400", projectId="github:other/repo")]
    # one batch's worktrees, the most recently active first
    assert [w["path"] for w in d.batch_worktrees(rows, "acme/widgets", batch="fl-1-t170")] == \
        ["/wt/felix/afk-batch-fl-1-t170-2", "/wt/felix/afk-batch-fl-1-t170"]
    # every batch of one instance, each under its own id — never another fleet's, another repo's
    mine = d.batch_worktrees(rows, "acme/widgets", instance="fl-1")
    assert sorted({w["batch"] for w in mine}) == ["fl-1-t170", "fl-1-t200"]
    assert d.batch_worktrees(rows, "acme/widgets", instance="fl-3") == []
    # orca lower-cases the project id: a repo with a capital still owns its worktrees
    assert d.batch_worktrees(rows, "Acme/Widgets", batch="fl-1-t170") == \
        d.batch_worktrees(rows, "acme/widgets", batch="fl-1-t170")


def test_an_instance_id_has_one_grammar():
    for ok in ("fl", "fl-1", "fleet-789a05", "7", "a--b-", "a" * 40):
        assert d.instance_id(ok) == ok
    # one ref path segment, one bare token of a branch name, never two that differ by case
    for bad in ("", "-fl", "fl/1", "fl 1", "fl.1", "fl_1", "Fl", "fl\n", "é", "a" * 41):
        try:
            d.instance_id(bad)
            raise AssertionError(bad)
        except ValueError as e:
            assert "is not an instance id" in str(e) and d.INSTANCE_ID_GRAMMAR in str(e)


def _some_instance_ids():
    """Every instance id of up to four characters over an alphabet that holds each
    kind the grammar has — a letter, the `t` of a batch id, a digit, the `-` —
    and the prefix pairs by name."""
    short = ["".join(c) for n in range(1, 5) for c in itertools.product("at1-", repeat=n)]
    named = ["fl", "fl-1", "fl-t1", "fl-1-t1", "felix", "felix-2", "felix-2-2", "fl-t170-2"]
    ids = [i for i in dict.fromkeys(short + named) if re.fullmatch(d.INSTANCE_ID_GRAMMAR, i)]
    assert len(ids) > 200 and set(named) <= set(ids)
    return ids


def test_a_batch_branch_is_one_instances_and_never_another_s():
    """Whatever second a batch was formed in and whatever continuation suffix orca
    put behind its branch, the branch matches the batches of the instance that
    formed it and of no other — so a sweep never deletes a peer's — and distinct
    instances never form one batch id."""
    ids = _some_instance_ids()
    owners = {}             # branch → the one instance it is a batch branch of
    for instance in ids:
        for now in (1, 170, 1700000000):
            batch = d.batch_id(instance, now)
            assert d.batch_formed_by(batch, instance)
            for suffix in ("", "-1", "-2", "-170"):
                for user in ("", "felix/"):
                    branch = f"{user}{d.batch_name(batch)}{suffix}"
                    assert owners.setdefault(branch, instance) == instance, branch
                    assert d.batch_branch_regex(batch).match(branch)
    for instance in ids:
        rx = d.batch_branch_regex(instance=instance)
        claimed = {branch for branch in owners if rx.match(branch)}
        assert claimed == {b for b, owner in owners.items() if owner == instance}, instance
        # and the id it reads off a branch is the batch's own
        assert all(d.batch_formed_by(rx.match(b).group(1), instance) for b in claimed)


def test_the_cycle_forms_a_batch_only_from_two_or_more_eligible_prs():
    """The batch-or-single decision, whole (ADR-0029) — a table lookup, so it is
    this function and not a paragraph."""
    on = d.resolve_config({"gate": {"ci": "local", "local_command": "make test"}})
    off = d.resolve_config({})                    # `required`: the gate is each PR's own checks

    def rows(*specs):
        mine = [_mine(n, status, pr=n * 10, **more) for n, status, more in specs]
        return mine, d.turn_order(mine)

    def picked(specs, cfg=on, busy=()):
        return d.batch_candidates(*rows(*specs), cfg, busy=busy)

    waiting = [(n, "awaiting_turn", {}) for n in (3, 1, 2)]
    assert picked(waiting) == [1, 2, 3]                            # in merge order
    assert picked(waiting, cfg=off) == []                          # only the local gate
    assert picked(waiting[:1]) == []                               # one PR is a single turn
    assert picked(waiting[:2]) == [1, 3]
    # a claim with no PR, a failed one, one still waiting on CI are not in the queue at all
    assert picked([*waiting[:2], (4, "no_pr", {}), (5, "failure", {}), (6, "closed", {})]) == [1, 3]
    # a worker still working may yet move its PR: it is not stacked
    assert picked(waiting, busy=[2]) == [1, 3] and picked(waiting, busy=[1, 2]) == []
    # one turn out at a time: while a PR — or a batch — holds it, no batch is formed
    assert picked([*waiting, (4, "landing", {})]) == []
    assert picked([*waiting, (4, "landing", {"batch": {"id": "b", "members": [4, 5], "phase": "gating"}})]) == []
    # a PR that left a batch goes first, on a single turn, and is never batched again
    left = [*waiting, (4, "awaiting_turn", {"unbatched": "left_out"})]
    assert picked(left) == [] and d.turn_order(rows(*left)[0])[0] == 4
    assert d.turn_due(*rows(*left)) == 4
    # so does a PR that gave its turn up and is ready again (ADR-0045) — while one
    # still being fixed off that turn holds nothing back: the batch forms beside it
    again = [*waiting, (4, "awaiting_turn", {"given_up": True})]
    assert picked(again) == [] and d.turn_due(*rows(*again)) == 4
    fixing = [*waiting, (4, "fixing", {"given_up": True, "stopped": "conflict"})]
    assert picked(fixing) == [1, 2, 3]
    assert d.turn_due(*rows(waiting[0], fixing[-1])) == 3         # …and so does a single turn
    # every PR owes its own adversarial verify before its turn: none is eligible
    verify = d.resolve_config({"gate": {"ci": "local", "local_command": "x",
                                        "adversarial_verify_prompt": "re-derive it"}})
    assert picked(waiting, cfg=verify) == []
    assert [d.batches_form(c) for c in (on, off, verify)] == [True, False, False]

    # a row in a batch is not asked after as its own worker's: the batch's worker is
    mine = [_mine(1, "landing", pr=10, batch={"id": "b", "members": [1, 2], "phase": "gating"}),
            _mine(2, "landing", pr=20, batch={"id": "b", "members": [1, 2], "phase": "gating"}),
            _mine(3, "landing", pr=30), _mine(4)]
    assert d.asks_after(mine) == [3, 4]
    assert d.turn_due(mine[:2], [1, 2]) is None
    # its worker's reading routes the batch: left, continued, nudged once, abandoned
    assert [d.batch_step({"cause": c}) for c in ("working", "gone", "silent", "silent_after_nudge")] == \
        ["leave", "continue", "nudge", "abandon"]


def test_a_stack_is_read_back_from_its_commits():
    assert d.stack_message("Add the thing", 12, 7) == "Add the thing (#12)\n\nCloses #7\n"
    assert d.stack_message("  ", 12, 7).startswith("PR 12 (#12)\n")
    m, fix = "p1 p2", "p1"                        # a member is a merge commit; a fix has one parent
    log = [("a1", m, "Add the thing (#12)"), ("b2", m, "Fix a typo (#13)"),
           ("c3", fix, "make the stack green"), ("d4", m, "refs issue (#99)"),
           ("e5", m, "Add the thing (#12)"), ("f6", fix, "repair the other thing (#14)")]
    stacked, fixes = d.read_stack(log, {12, 13, 14})
    # a fix titled like member 14's commit is still a fix: 14 is not on this stack
    assert stacked == {12: "a1", 13: "b2"} and fixes == ["c3", "d4", "e5", "f6"]
    assert d.read_stack([], {12}) == ({}, [])
    said = d.batch_landed_comment("abc123", "main", "fl-1-5", [12, 13])
    assert "landed on `main` as abc123" in said and "#12, #13" in said and "did not mark this PR merged" in said


def test_the_ticks_judgments_are_settled_before_a_turn_is_granted():
    def gate(ci, checks, allow=False, verify=False, verified=None, head="h1"):
        return d.turn_gate(ci, checks, allow, verify, verified, head)

    # local: the machine gate is the landing's to run — nothing to wait for here
    for checks in ("green", "red", "pending", None):
        assert gate("local", checks) == "ready", checks
    # required: the PR's checks speak first; absent ones are the tick's call
    assert gate("required", "green") == "ready"
    assert gate("required", "pending") == "awaiting_ci"
    assert gate("required", "red") == "gate_red"
    assert gate("required", None) == "no_checks" and gate("required", None, allow=True) == "ready"
    assert gate("required", "red", allow=True) == "gate_red"       # waives ABSENT checks only
    # the adversarial verify comes after the machine gate, pinned to the head
    assert gate("local", None, verify=True) == "needs_verify"
    assert gate("local", None, verify=True, verified="h0") == "needs_verify"
    assert gate("local", None, verify=True, verified="h1") == "ready"
    assert gate("required", "pending", verify=True, verified="h1") == "awaiting_ci"
    # every refusal is a word `afk turn` may stop with
    assert {gate("required", c, verify=True) for c in ("green", "red", "pending", None)} \
        <= set(d.TURN_OUTCOMES)


def test_turns_are_granted_in_one_order_a_held_turn_first_then_pr_number():
    """The merge queue (ADR-0027): the ready PRs of mine, the one that already
    holds a turn first, then the lower PR number — never the order claims were
    scanned in."""
    def row(number, status, pr):
        return _mine(number, status, pr=pr)

    rows = [row(1, "awaiting_turn", 30), row(2, "no_pr", None), row(3, "awaiting_turn", 10),
            row(4, "landing", 40), row(5, "awaiting_ci", 5), row(6, "failure", 6),
            row(7, "closed", None)]
    assert d.turn_order(rows) == [4, 3, 1]
    assert d.turn_order(reversed(rows)) == [4, 3, 1]                # not input order
    # one PR closing several issues: its rows share everything but the issue number,
    # and still come out one way under every order they could be scanned in
    shared = [row(9, "awaiting_turn", 10), row(8, "awaiting_turn", 10), row(3, "awaiting_turn", 10),
              row(6, "landing", 40), row(4, "landing", 40), row(1, "awaiting_turn", 30),
              _mine(2, "awaiting_turn", pr=50, unbatched="left_out"),
              _mine(5, "awaiting_turn", pr=50, unbatched="left_out")]
    assert {tuple(d.turn_order(list(p))) for p in itertools.permutations(shared)} == \
        {(4, 6, 2, 5, 3, 8, 9, 1)}
    assert d.turn_order([]) == [] and d.turn_order(rows[1:2]) == []
    # a PR that gave its turn up (ADR-0045): out of the queue while it is being
    # fixed, and ready again it goes behind a held turn only — ahead of a PR that
    # left a batch and of every PR that never held a turn
    queue = [row(1, "awaiting_turn", 10), _mine(2, "awaiting_turn", pr=20, unbatched="left_out"),
             _mine(3, "awaiting_turn", pr=90, given_up=True), row(4, "landing", 40),
             _mine(5, "fixing", pr=5, given_up=True, stopped="gate_red")]
    assert {tuple(d.turn_order(list(p))) for p in itertools.permutations(queue)} == {(4, 3, 2, 1)}


def test_an_escalate_label_the_escalation_edit_would_also_remove_is_refused():
    """An escalation adds `escalate_label` and removes `ready_label` and every
    attempt label in ONE edit: a label on both sides of it is a config that
    cannot mean anything."""
    d.validate_config(d.resolve_config({"ready_label": "go", "escalate_label": "human"}))
    for bad, says in (({"escalate_label": "ready-for-agent"}, "is ready_label too"),
                      ({"ready_label": "x", "escalate_label": "x"}, "is ready_label too"),
                      ({"escalate_label": "afk-attempt/human"}, "attempt labels"),
                      ({"escalate_label": "afk-attempt/3"}, "attempt labels"),
                      ({"escalate_label": d.ATTEMPT_STARTING}, "attempt labels")):
        try:
            d.validate_config(d.resolve_config(bad))
            assert False, f"expected ValueError for {bad}"
        except ValueError as e:
            assert "escalate_label" in str(e) and says in str(e)


def test_validate_config():
    ok = d.resolve_config({})
    assert d.validate_config(ok) is ok                    # returns it, unchanged

    # local mode with a real command is fine
    d.validate_config(d.resolve_config({"gate": {"ci": "local", "local_command": "make test"}}))

    # the illegal state made unrepresentable: local mode IS the local command, so an
    # empty one would merge every PR unverified
    for bad_gate in ({"ci": "local"},                       # local_command defaults to ""
                     {"ci": "local", "local_command": "   "}):
        try:
            d.validate_config(d.resolve_config({"gate": bad_gate}))
            assert False, f"expected ValueError for {bad_gate}"
        except ValueError as e:
            assert "local_command" in str(e)

    # the claim namespace is one of exactly two layouts. Anything else — even a
    # well-formed prefix — would be a third one no probe or warning describes:
    # `refs/heads/afk` builds ordinary branches that `on: push` CI fires on
    for ns in d.CLAIM_NAMESPACES:
        d.validate_config(d.resolve_config({"claim_namespace": ns}))
    assert set(d.CLAIM_NAMESPACES) == {"refs/afk", "refs/heads"}
    assert d.BRANCH_NAMESPACE in d.CLAIM_NAMESPACES
    assert d.CONFIG_SETTLED["claim_namespace"] in d.CLAIM_NAMESPACES
    for bad_ns in ("afk", "heads/afk", "refs/afk/", "", "refs/heads/afk", "refs/x", None):
        try:
            d.validate_config(d.resolve_config({"claim_namespace": bad_ns}))
            assert False, f"expected ValueError for claim_namespace {bad_ns!r}"
        except ValueError as e:
            assert "claim_namespace" in str(e)

    # an unknown mode is refused, never treated as "required"
    try:
        d.validate_config(d.resolve_config({"gate": {"ci": "optional"}}))
        assert False, "expected ValueError for an unknown gate.ci"
    except ValueError as e:
        assert "gate.ci" in str(e) and "required" in str(e)

    # how a PR lands is not a choice (ADR-0034): a config that still names a
    # strategy, or switches batches on or off, is refused with the reason
    for key, value in (("strategy", "squash"), ("strategy", "merge"), ("batch", "true"),
                       ("batch", "false")):
        for load in (lambda: d.parse_config_yaml(f"merge:\n  {key}: {value}"),
                     lambda: d.override_config(d.resolve_config({}), [f"merge.{key}={value}"])):
            try:
                load()
                assert False, f"expected ValueError for merge.{key}"
            except ValueError as e:
                assert f"merge.{key}" in str(e) and "removed" in str(e) and "Delete the key" in str(e)
    assert "merge" not in d.CONFIG_DEFAULTS


def test_gate_verdict():
    log = "\n".join(f"line {i}" for i in range(1, 101))

    # ONLY exit 0 is green — the one thing that could quietly poison a merge
    assert d.gate_verdict(0, "all good")["status"] == "green"
    for rc in (1, 2, 127, -9):
        assert d.gate_verdict(rc, "boom")["status"] == "red", rc

    # the excerpt is the TAIL (where runners put the failure summary), bounded
    r = d.gate_verdict(1, log, max_lines=10)
    assert r["excerpt"].splitlines() == [f"line {i}" for i in range(91, 101)]
    assert r["omitted_lines"] == 90 and r["exit_code"] == 1
    # a short log is kept whole, and nothing is reported as omitted
    r = d.gate_verdict(0, "one\ntwo\n")
    assert r["excerpt"] == "one\ntwo" and r["omitted_lines"] == 0
    assert d.gate_verdict(0, "")["excerpt"] == ""
    assert d.gate_verdict(0, None)["excerpt"] == ""
    # an excerpt of no lines is none of the log, all of it reported as omitted
    r = d.gate_verdict(1, log, max_lines=0)
    assert (r["excerpt"], r["omitted_lines"], r["status"]) == ("", 100, "red")

    # a timed-out run is RED, never green-by-default, whatever it exited with
    r = d.gate_verdict(0, "hung", timed_out=True)
    assert r["status"] == "red" and r["timed_out"] is True


def test_records_kept_in_comments_share_the_encoding_of_records_on_refs():
    """ADR-0032. The landing turn, a worker's verdict and the status board each
    declare a word and their fields; the pair that writes and reads a record on a
    ref writes and reads them, with a comment's marker as the carrier."""
    members = [{"issue": 1, "pr": 10}, {"issue": 2, "pr": 20}]
    kinds = [
        (d.TURN_RECORD, [
            ({"instance": "fl-1", "at": 100}, "<!--afk:turn instance=fl-1 at=100-->"),
            ({"instance": "fl-1", "at": 150, "verified": "v" * 40, "allow_no_checks": True,
              "stopped": "needs_verify", "head": "h" * 40},
             f"<!--afk:turn instance=fl-1 at=150 verified={'v' * 40} allow_no_checks=1 "
             f"stopped=needs_verify head={'h' * 40}-->"),
            ({"instance": "fl-1", "at": 200, "batch": "fl-1-200", "members": members,
              "phase": "gating"},
             "<!--afk:turn instance=fl-1 at=200 batch=fl-1-200 members=1:10,2:20 phase=gating-->"),
            ({"instance": "fl-1", "at": 300, "unbatched": "left_out", "of": "fl-1-200",
              "released": True},
             "<!--afk:turn instance=fl-1 at=300 unbatched=left_out of=fl-1-200 released=1-->"),
            ({"instance": "fl-1", "at": 400, "restarted": 400},
             "<!--afk:turn instance=fl-1 at=400 restarted=400-->")]),
        (d.VERDICT_RECORD, [
            ({"n": 7, "phase": "already-satisfied"}, "<!--afk:verdict n=7 phase=already-satisfied-->"),
            ({"n": 12, "phase": "blocked", "blocked_by": [3, 4], "reason": "needs pages from #3"},
             "<!--afk:verdict n=12 phase=blocked blocked_by=3,4 reason=needs pages from #3-->"),
            ({"n": 9, "phase": "giving-up", "reason": "the gate is red: k=v, 100% of runs"},
             "<!--afk:verdict n=9 phase=giving-up reason=the gate is red: k=v, 100% of runs-->")]),
        (d.STATUS_RECORD, [({}, "<!--afk:status-->")]),
    ]
    for kind, cases in kinds:
        for record, marker in cases:
            # a literal of each marker in use today is what is written, and reads back
            assert d.record_marker(kind, record) == marker
            body = d.record_comment(kind, record, "**worded for a human**\n\nmore")
            assert body == f"{marker}\n**worded for a human**\n\nmore"
            assert d.read_marker(kind, body) == record, marker
            # the comment a record is read from is the one its rewrite replaces
            comments = [{"id": 1, "body": "a human note"}, {"id": 2, "body": body, "url": "u2"}]
            assert d.latest_record(kind, comments) == (record, comments[1]), marker
        assert d.read_marker(kind, "a human note") is None and d.read_marker(kind, None) is None
        assert d.latest_record(kind, []) == d.latest_record(kind, None) == (None, None)

    # the latest marker wins, for every kind: stated once, in `latest_record`
    for kind, cases in kinds:
        old, new = cases[0][0], cases[-1][0]
        comments = [{"id": 1, "body": d.record_comment(kind, old, "then")},
                    {"id": 2, "body": "chatter"},
                    {"id": 3, "body": d.record_comment(kind, new, "now")}]
        assert d.latest_record(kind, comments) == (new, comments[2])
    # …and a marker that is not a record is passed over, not read as the latest: a
    # turn that names no instance is nobody's (`TURN_RECORD` requires it)
    held = {"id": 1, "body": "<!--afk:turn instance=fl-1 at=1-->"}
    assert d.latest_record(d.TURN_RECORD, [held, {"id": 2, "body": "<!--afk:turn at=3-->"}]) == \
        ({"instance": "fl-1", "at": 1}, held)
    assert d.TURN_RECORD.required == ("instance",)
    # a verdict is written by hand and requires nothing: one with no phase is still one
    assert d.read_marker(d.VERDICT_RECORD, "<!--afk:verdict n=9-->") == {"n": 9}

    # read like a commit's subject: an unknown field is read past, a value not of
    # its type is a field that is missing, and spacing is free
    assert d.read_marker(d.TURN_RECORD, "<!--  afk:turn instance=x at=soon pid=7 stopped=bogus "
                                        "phase=bisecting released=yes\n members=zz,3:30 -->") == \
        {"instance": "x", "members": [{"issue": 3, "pr": 30}]}
    assert d.read_marker(d.VERDICT_RECORD, "<!--afk:verdict blocked_by=3, x, 4 n=5 reason= -->") == \
        {"n": 5, "blocked_by": [3, 4]}
    # the first marker of a body is the one read, and another kind's is not this kind's
    both = "<!--afk:verdict n=1 phase=blocked-->\n<!--afk:verdict n=1 phase=giving-up-->"
    assert d.read_marker(d.VERDICT_RECORD, both)["phase"] == "blocked"
    assert d.read_marker(d.TURN_RECORD, both) is None and d.read_marker(d.STATUS_RECORD, both) is None
    # a word outside a closed vocabulary is a defect in the writer
    for bad in ({"instance": "x", "stopped": "bogus"}, {"instance": "x", "pid": 7}, {"at": 1}):
        try:
            d.record_marker(d.TURN_RECORD, bad)
            raise AssertionError(bad)
        except ValueError:
            pass
    # the two carriers are one line: a marker is the commit message between `<!--` and `-->`
    assert d.record_marker(d.CLAIM_RECORD, {"instance": "a", "ts": 1}) == "<!--afk-claim instance=a ts=1-->"
    assert d.read_record(d.TURN_RECORD, "afk:turn instance=a at=1 released=1") == \
        {"instance": "a", "at": 1, "released": True}


# The pieces a generated value is made of: everything that separates, closes or
# encodes something in a record, and text that needs none of it.
_VALUE_PIECES = (",", "-->", "--", ">", "<!--", "%", "%2C", "%4", "=", " ", "\t", "\n", "\r\n",
                 "\u3000", "\u2028", "\x00", "#", "é", "进度", "reason=", "ts=5", "a", "Z", "9", "-")


def _generated_text(rng):
    return "".join(rng.choice(_VALUE_PIECES) for _ in range(rng.randint(1, 8)))


def _generated_members(rng):
    return [{"issue": rng.randrange(10**6), "pr": rng.randrange(10**6)}
            for _ in range(rng.randint(0, 4))]


# One generator per way a field's value is spelled. A kind that declares a type
# with none here fails the test below until it is given one.
_GENERATED = {
    str: _generated_text,
    int: lambda rng: rng.randrange(10**12),
    d.FLAG: lambda rng: rng.random() < 0.5,
    d.INTS: lambda rng: [rng.randrange(10**6) for _ in range(rng.randint(0, 4))],
    d.TURN_RECORD.fields["members"]: _generated_members,
    d.TURN_RECORD.fields["stopped"]: lambda rng: rng.choice(d.LAND_OUTCOMES),
    d.TURN_RECORD.fields["phase"]: lambda rng: rng.choice(d.BATCH_PHASES),
    d.TURN_RECORD.fields["unbatched"]: lambda rng: rng.choice(d.UNBATCHED),
}


def test_every_record_value_round_trips_on_both_carriers():
    """Writing a record and reading it back is the identity — for every declared
    kind, on a ref and in a comment, over values made of what the encoding itself
    uses (`,`, `-->`, `%`, `=`, whitespace, line breaks) and of other scripts. A
    field left out reads as its type's `empty`, which is what writing nothing
    says."""
    kinds = {name: kind for name, kind in vars(d).items() if isinstance(kind, d.RecordKind)}
    assert {"CLAIM_RECORD", "GATE_RUN_RECORD", "BASE_RECORD", "TURN_RECORD", "VERDICT_RECORD",
            "ESCALATION_RECORD"} <= set(kinds)
    rng = random.Random(114)
    for name, kind in kinds.items():
        blank = d.blank_record(kind)
        for _ in range(400):
            record = {field: _GENERATED[declared](rng) for field, declared in kind.fields.items()
                      if field in kind.required or rng.random() < 0.7}
            said = {**blank, **record}
            message = d.record_message(kind, record)
            assert "\n" not in message and "\r" not in message, (name, record)
            on_ref = d.read_record(kind, message + "\n\nany body at all")
            assert on_ref is not None and {**blank, **on_ref} == said, (name, record, message)
            body = d.record_comment(kind, record, "worded for a human --> a=1, b=2")
            in_comment = d.read_marker(kind, body)
            assert in_comment is not None and {**blank, **in_comment} == said, (name, record, body)

    # the two values that did not (#114), by name: a trailing comma took the next
    # field with it, and `-->` in a value closed the marker
    base = {"branch": "release,", "ts": 5}
    assert d.read_record(d.BASE_RECORD, d.record_message(d.BASE_RECORD, base)) == base
    gate = d.gate_record("abc123", "make test,", 7)
    assert d.read_record(d.GATE_RUN_RECORD, d.record_message(d.GATE_RUN_RECORD, gate)) == gate
    marker = d.verdict_marker(9, "giving-up", reason="it --> broke")
    assert marker.count("-->") == 1 and _verdict_in(marker)["reason"] == "it --> broke"
    turn = {"instance": "fl-1", "of": "a-->b,"}
    assert d.read_marker(d.TURN_RECORD, d.record_marker(d.TURN_RECORD, turn)) == turn


def test_a_hand_written_blocked_by_reads_as_the_issues_it_names():
    """A worker types its verdict, and names issues the way issues are named: a
    list spelled with `#`, or spread over words, is still those issues — never
    a `blocked` verdict that names nobody, which would be escalated, not parked."""
    for spelled in ("3,4", "#3,#4", "3 4", "#3 #4", "3, 4", "#3, #4", "#3 and #4", "3 ,4"):
        for rest in ("", " reason=needs pages from #3"):
            body = f"<!--afk:verdict n=12 phase=blocked blocked_by={spelled}{rest}-->"
            assert _verdict_in(body)["blocked_by"] == [3, 4], body
    # only a list runs over words: a stray word after any other value is read past
    assert d.read_marker(d.VERDICT_RECORD, "<!--afk:verdict n=5 6 phase=blocked now-->") == \
        {"n": 5, "phase": "blocked"}
    assert d.read_record(d.BASE_RECORD, "afk-base branch=release, ts=5") == \
        {"branch": "release,", "ts": 5}
    # a batch's members are a list too
    assert d.read_marker(d.TURN_RECORD, "<!--afk:turn instance=x members=1:10, 2:20 at=3-->") == \
        {"instance": "x", "members": [{"issue": 1, "pr": 10}, {"issue": 2, "pr": 20}], "at": 3}


def test_each_record_in_a_comment_round_trips_through_its_own_reader():
    """What the fleet's own writers put in a comment — a turn in each of its
    shapes, a verdict, a status board — is found again by the kind it was
    written as, and a literal of each marker already on a PR or an issue reads
    as the record it always did."""
    for shape, turn in _turn_shapes().items():
        body = d.turn_comment(turn)
        record, comment = d.latest_record(d.TURN_RECORD, [{"id": 7, "body": body}])
        assert comment["id"] == 7 and d.record_marker(d.TURN_RECORD, record) == body.split("\n")[0], shape
    board = d.render_status_board("pr_open", "local", 2, instance="fl-1", pr=9)
    assert board.split("\n")[:2] == ["<!--afk:status-->", "**afk-fleet 进度** · 认领方 `fl-1`"]
    assert d.latest_record(d.STATUS_RECORD, [{"id": 1, "body": "x"}, {"id": 2, "body": board}]) == \
        ({}, {"id": 2, "body": board})
    posted = d.verdict_marker(12, "blocked", [3, 4], "needs #3") + "\nBlocked on #3 and #4."
    assert d.latest_verdict([{"body": posted, "url": "u"}]) == {
        "found": True, "phase": "blocked", "blocked_by": [3, 4], "reason": "needs #3",
        "comment_url": "u"}

    no_turn = {"verified": None, "allow_no_checks": False, "stopped": None, "head": None,
               "restarted": None, "given_up": None, "batch": None, "members": [], "phase": None,
               "unbatched": None, "of": None, "released": False, "comment_id": 5}
    literals = {
        "<!--afk:turn instance=fl-7fbd5e at=1759676212-->\n**afk-fleet: this PR holds the landing turn**":
            {"instance": "fl-7fbd5e", "at": 1759676212},
        "<!--afk:turn instance=fl-7fbd5e at=1759676300 verified=0af4df0 allow_no_checks=1 "
        "stopped=awaiting_ci head=8ca0ee2-->\n…":
            {"instance": "fl-7fbd5e", "at": 1759676300, "verified": "0af4df0",
             "allow_no_checks": True, "stopped": "awaiting_ci", "head": "8ca0ee2"},
        "<!--afk:turn instance=fl-7fbd5e at=1759676400 batch=fl-7fbd5e-1759676400 "
        "members=59:66,60:67 phase=stacking-->\n…":
            {"instance": "fl-7fbd5e", "at": 1759676400, "batch": "fl-7fbd5e-1759676400",
             "members": [{"issue": 59, "pr": 66}, {"issue": 60, "pr": 67}], "phase": "stacking"},
        "<!--afk:turn instance=fl-7fbd5e at=1759676500 unbatched=dissolved "
        "of=fl-7fbd5e-1759676400 released=1-->\n…":
            {"instance": "fl-7fbd5e", "at": 1759676500, "unbatched": "dissolved",
             "of": "fl-7fbd5e-1759676400", "released": True},
        "<!--afk:turn instance=fl-7fbd5e at=1759676600 restarted=1759676600-->\n…":
            {"instance": "fl-7fbd5e", "at": 1759676600, "restarted": 1759676600},
        "<!--afk:turn instance=fl-7fbd5e at=1759676700 stopped=conflict head=abc "
        "given_up=1759676700 released=1-->\n…":
            {"instance": "fl-7fbd5e", "at": 1759676700, "stopped": "conflict", "head": "abc",
             "given_up": 1759676700, "released": True},
    }
    for body, said in literals.items():
        assert d.latest_turn([{"id": 5, "body": body}]) == {**no_turn, **said}, body
    verdicts = {
        "<!--afk:verdict n=72 phase=already-satisfied-->\nEmpty diff.": ("already-satisfied", [], None),
        "<!--afk:verdict n=72 phase=blocked blocked_by=71,55-->\nNeeds both.": ("blocked", [71, 55], None),
        "<!--afk:verdict n=72 phase=giving-up reason=the gate stays red on master-->\n…":
            ("giving-up", [], "the gate stays red on master"),
        "<!--afk:verdict n=72 phase=blocked blocked_by=71 reason=no record mechanism yet-->":
            ("blocked", [71], "no record mechanism yet"),
    }
    for body, (phase, blocked_by, reason) in verdicts.items():
        assert _verdict_in(body) == {"found": True, "n": 72, "phase": phase,
                                                "blocked_by": blocked_by, "reason": reason}, body
    assert d.STATUS_MARKER == "<!--afk:status-->"


def test_records_kept_on_refs_share_one_encoding():
    """ADR-0031. A claim, a heartbeat and a recorded gate run each declare a word
    and their fields; one pair of functions writes and reads them all, with one
    rule each for an unknown field, a missing one, and a commit that is no record."""
    claim = {"instance": "fl-1", "host": "mac.local", "ts": 1700000000}
    assert d.record_message(d.CLAIM_RECORD, claim) == "afk-claim instance=fl-1 host=mac.local ts=1700000000"
    assert d.record_message(d.HEARTBEAT_RECORD, {"instance": "fl-1", "ts": 5.9}) == \
        "afk-heartbeat instance=fl-1 ts=5"
    for kind, record in ((d.CLAIM_RECORD, claim), (d.HEARTBEAT_RECORD, {"instance": "fl-1", "ts": 5}),
                         (d.GATE_RUN_RECORD, d.gate_record("abc123", "make  test\n# 100% é k=v", 7))):
        message = d.record_message(kind, record)
        assert "\n" not in message and d.read_record(kind, message) == record
        assert d.read_record(kind, message + "\n\nany body at all") == record

    # the formats on remotes today, pinned: what a fleet wrote before it updated
    assert d.read_record(d.CLAIM_RECORD, "afk-claim instance=fl-7fbd5e host=Felixs-MacBook-Pro.local "
                                         "ts=1000000") == \
        {"instance": "fl-7fbd5e", "host": "Felixs-MacBook-Pro.local", "ts": 1000000}
    assert d.read_record(d.CLAIM_RECORD, "afk-claim instance=by-hand host=box") == \
        {"instance": "by-hand", "host": "box"}                      # the documented by-hand claim
    assert d.read_record(d.HEARTBEAT_RECORD, "afk-heartbeat instance=fl-7fbd5e ts=1000000") == \
        {"instance": "fl-7fbd5e", "ts": 1000000}

    # a field left out is not written, and an optional one not stated is absent
    assert d.record_message(d.CLAIM_RECORD, {"instance": "a", "host": None, "ts": 1}) == \
        d.record_message(d.CLAIM_RECORD, {"instance": "a", "host": "", "ts": 1}) == "afk-claim instance=a ts=1"
    assert d.read_record(d.CLAIM_RECORD, "afk-claim instance=a host= ts=soon") == {"instance": "a"}
    # an unknown field is read past
    assert d.read_record(d.CLAIM_RECORD, "afk-claim instance=a pid=7 loose ts=1") == {"instance": "a", "ts": 1}
    # not a record: another word, no word, a required field missing or ill-typed
    for kind, message in ((d.CLAIM_RECORD, "afk-heartbeat instance=a ts=1"), (d.CLAIM_RECORD, ""),
                          (d.CLAIM_RECORD, None), (d.CLAIM_RECORD, "not a marker at all"),
                          (d.CLAIM_RECORD, "afk-claim host=mac ts=1"), (d.CLAIM_RECORD, "afk-claim instance="),
                          (d.HEARTBEAT_RECORD, "afk-heartbeat instance=a"),
                          (d.HEARTBEAT_RECORD, "afk-heartbeat ts=-1"),
                          (d.GATE_RUN_RECORD, "afk-gate green"),
                          (d.GATE_RUN_RECORD, 'afk-gate green\n\n{"tree": "t", "command": "c", "at": 1}'),
                          (d.GATE_RUN_RECORD, "afk-gate tree=t command=c at=yesterday")):
        assert d.read_record(kind, message) is None, message
    # writing a record that is not one is the caller's defect, not a quiet hole
    for kind, record in ((d.CLAIM_RECORD, {"ts": 1}), (d.CLAIM_RECORD, {"instance": ""}),
                         (d.HEARTBEAT_RECORD, {"instance": "a", "pid": 7, "ts": 1})):
        try:
            d.record_message(kind, record)
        except ValueError:
            continue
        raise AssertionError(record)


def test_a_recorded_gate_run_counts_only_for_the_tree_and_command_it_ran():
    """ADR-0030. A landing skips its own run of the local gate on ONE proof: a green
    run of the command configured now, on the tree that would land, no more than a
    day old. Everything else is void — and void means the landing gates."""
    now = 1700000000
    rec = d.gate_record("abc123", "make test", now + 0.9)
    assert rec == {"tree": "abc123", "command": "make test", "at": now}
    assert d.gate_record_void(rec, now) is None
    assert d.gate_record_void(rec, now + d.GATE_RECORD_TTL) is None
    assert d.GATE_RECORD_TTL == 24 * 3600

    # which tree and which command a record is of is settled by the name it was
    # asked for under (below): all that is left to decide is whether there is
    # one, and how old it is
    assert "no green run" in d.gate_record_void(None, now)
    assert "old" in d.gate_record_void(rec, now + d.GATE_RECORD_TTL + 1)

    # the ref's name is the key: one per tree and command, and nothing else in it
    ref = d.gate_record_ref("abc123", "make test")
    assert ref.startswith("refs/afk/gate/abc123-") and ref == d.gate_record_ref("abc123", "make test")
    assert len({ref, d.gate_record_ref("def456", "make test"),
                d.gate_record_ref("abc123", "make test ")}) == 3

    # a recorded run is always trusted: the key that turned that off is gone, and
    # a file still carrying it is told so rather than silently ignored
    assert "trust_recorded_run" not in d.CONFIG_DEFAULTS["gate"]
    try:
        d.parse_config_yaml("gate:\n  trust_recorded_run: false")
        assert False, "expected ValueError for the removed trust_recorded_run key"
    except ValueError as e:
        assert "was removed" in str(e) and "ADR-0030" in str(e)

    # the line a worker gates with: the tool, carrying the command — quoted for a shell
    assert d.gate_command("/s/afk.py", " make test ") == \
        """/s/afk.py gate --config '{"gate": {"local_command": "make test"}}'"""
    import shlex
    odd = d.gate_command("/my skills/afk.py", """pnpm test -- --grep 'a "b"'""")
    argv = shlex.split(odd)
    assert argv[:3] == ["/my skills/afk.py", "gate", "--config"] and len(argv) == 4
    assert json.loads(argv[3]) == {"gate": {"local_command": """pnpm test -- --grep 'a "b"'"""}}
    assert "no gate.local_command configured" in d.gate_command("/s/afk.py", "  ")


def test_a_merge_batch_needs_the_local_gate_and_a_target_that_takes_a_push():
    """What bootstrap refuses (ADR-0029). A batch is gated by ONE run of
    `gate.local_command` on the stack, and lands by pushing to the target."""
    refusing = ({"required_pull_request_reviews": {"required_approving_review_count": 1}},
                {"restrictions": {"users": ["a"], "teams": []}},
                {"lock_branch": {"enabled": True}})
    for prot in refusing:
        r = d.protection_verdict("local", prot, batch=True)
        assert r["verdict"] == "error" and "merge batch" in r["detail"], r
        assert d.protection_verdict("local", prot)["verdict"] == "ok"      # only where batches form
    for fine in (None, {}, {"lock_branch": {"enabled": False}}, {"restrictions": None},
                 {"enforce_admins": {"enabled": True}}):
        assert d.protection_verdict("local", fine, batch=True)["verdict"] == "ok", fine
    # required checks stay the error they were; an unreadable protection still warns
    checks = {"required_status_checks": {"contexts": ["ci"]}}
    assert d.protection_verdict("local", checks, batch=True)["required_checks"] == ["ci"]
    assert d.protection_verdict("local", None, "HTTP 403", batch=True)["verdict"] == "warn"


def test_a_batch_shows_on_the_board_in_the_brief_and_in_the_working_set():
    # --- the status board of every member: the batch's PRs, and what is being done ---
    for phase, word in zip(d.BATCH_PHASES, ("stacking", "gating", "being fixed")):
        board = d.render_status_board("landing", "local", 2, instance="me", pr=20,
                                      batch={"prs": [10, 20, 30], "phase": phase})
        assert "#10、#20、#30" in board and word in board and "merge batch" in board, board
    plain = d.render_status_board("landing", "local", 2, instance="me", pr=20)
    assert "merge batch" not in plain

    # --- the batch brief: the one command, the members in stack order, its own wake ---
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "..", "references", "worker-prompt.md")) as f:
        template = f.read()
    fields = {"batch": "me-t100", "repo": "acme/widgets", "target": "main",
              "branch": "u/afk-batch-me-t100", "worktree_path": "/w/batch",
              "members": [{"issue": 1, "pr": 10, "title": "one {braces}"},
                          {"issue": 2, "pr": 20, "title": "two"}],
              "afk_path": "/s/afk.py", "config": '{"retry": 2}',
              "launcher_terminal": "term_1"}
    brief = d.render_batch_brief(template, fields)
    assert "/s/afk.py land --batch me-t100 --repo acme/widgets --config " in brief
    assert brief.index("PR #10 — closes #1 — one {braces}") < brief.index("PR #20 — closes #2 — two")
    assert d.wake_command("term_1", "batch-me-t100") in brief and "/w/batch" in brief
    for outcome in d.BATCH_OUTCOMES:
        assert f"`{outcome}`" in brief
    try:
        d.render_batch_brief(template, {k: v for k, v in fields.items() if k != "target"})
    except ValueError as e:
        assert "target" in str(e)
    else:
        raise AssertionError("a brief with a field missing was rendered")

    # --- what the tick does about the batch's worker ---
    assert [d.batch_step({"cause": c}) for c in ("working", "gone", "silent", "silent_after_nudge")] == \
        ["leave", "continue", "nudge", "abandon"]

    # --- the working set: a batch's members are `landing` rows that name it ---
    cfg = d.resolve_config({"gate": {"ci": "local", "local_command": "make test"}})
    issues = [{"number": n, "title": f"i{n}", "labels": ["ready-for-agent"], "updatedAt": "t"}
              for n in (1, 2, 3)]
    prs = [{"number": n * 10, "headRefOid": f"h{n}", "statusCheckRollup": [],
            "closingIssuesReferences": [{"number": n}]} for n in (1, 2, 3)]
    claims = [{"number": n, "instance": "me", "sha": f"s{n}"} for n in (1, 2, 3)]
    beats = [{"instance": "me", "ts": 1000}]
    members = [{"issue": 1, "pr": 10}, {"issue": 2, "pr": 20}]

    def turn(body):
        return d.latest_turn([{"id": 1, "body": body}])

    mine = turn(d.turn_comment(d.batch_turn(None, "me", 1000, "me-t100", members, "gating")))
    issues, prs, claims = _gathered(issues, prs, claims)
    ws = d.assemble_working_set(issues, prs, claims, beats, "me", 1000, cfg,
                                turns={1: mine, 2: mine,
                                       3: turn(d.turn_comment(d.unbatched_turn(None, "me", 1000, "me-t100", "left_out")))})
    rows = {m["number"]: (m["status"], m["board_phase"], m["batch"], m["unbatched"]) for m in ws["mine"]}
    in_batch = {"id": "me-t100", "members": [1, 2], "phase": "gating"}
    # (a batch row has no board phase: its board is the batch's to write, not the tick's)
    assert rows == {1: ("landing", None, in_batch, None), 2: ("landing", None, in_batch, None),
                    3: ("awaiting_turn", "awaiting_turn", None, "left_out")}
    assert ws["batches"] == [{"id": "me-t100", "instance": "me", "members": members,
                              "phase": "gating", "at": 1000}]
    assert ws["merge_order"] == [1, 2, 3]
    assert d.asks_after(ws["mine"]) == []            # the batch's worker is asked after, not theirs
    assert d.batch_candidates(ws["mine"], ws["merge_order"], cfg) == []
    assert d.turn_due(ws["mine"], ws["merge_order"]) is None

    # a batch a DEAD fleet recorded on claims I took holds no turn of mine — and is
    # listed, so the tick abandons it before it grants anything
    theirs = turn(d.turn_comment(d.batch_turn(None, "old", 900, "old-90", members, "stacking")))
    ws = d.assemble_working_set(issues, prs, claims, beats, "me", 1000, cfg,
                                turns={1: theirs, 2: theirs})
    assert {m["number"]: (m["status"], m["batch"]) for m in ws["mine"]} == \
        {n: ("awaiting_turn", None) for n in (1, 2, 3)}
    assert [(b["id"], b["instance"]) for b in ws["batches"]] == [("old-90", "old")]


def test_a_landing_that_stops_on_a_fix_gives_its_turn_up_once():
    """ADR-0045, as the turn record says it: a held turn whose landing stops on a
    conflict or a red gate becomes a marker that holds no turn; granted again,
    the PR keeps that second turn through the same stop."""
    held = d.single_turn(None, "me", 100, verified="v1", restarted=90)
    for outcome in d.LAND_OUTCOMES:
        assert d.gives_turn_up(held, outcome) == (outcome in ("conflict", "gate_red")), outcome
    assert set(d.LAND_FIXES) == {"conflict", "gate_red"} and not set(d.LAND_FIXES) & set(d.LAND_WAITS)

    gone = d.given_up_turn(held, 200.7, "conflict", "abc123")
    assert (gone["released"], gone["given_up"], gone["stopped"], gone["head"], gone["at"]) == \
        (True, 200, "conflict", "abc123", 200)
    # the worker that got as far as `afk land` is not the one a restart replaced
    assert gone["restarted"] is None and gone["verified"] == "v1"
    body = d.turn_comment(gone)
    assert body.startswith("<!--afk:turn instance=me at=200 verified=v1 stopped=conflict "
                           "head=abc123 given_up=200 released=1-->\n")
    assert "gave its landing turn up" in body and "fixing that in place, off the turn" in body
    read = d.latest_turn([{"id": 3, "body": body}])
    assert read == {**gone, "comment_id": 3}

    # it holds no turn — the next PR is granted one — but the PR's own worker still
    # lands under it, off the turn; and its silence climbs the landing ladder
    assert d.held_turn(read, "me") is None and d.own_landing(read, "me") is read
    assert d.own_landing(read, "other") is None and d.own_landing(read, None) is None
    assert d.turn_given_up(read) and d.fixing_off_turn(read) and d.worker_at_landing(read)
    assert not d.single_turn_held(read) and d.restartable_turn(read)
    assert not d.restartable_turn(d.next_turn(read, restarted=250))

    # fixed: `afk land`, off the turn, says the PR is ready again
    ready = d.next_turn(read, at=300, stopped="awaiting_turn", head="def456")
    assert d.turn_given_up(ready) and not d.fixing_off_turn(ready)
    assert not d.worker_at_landing(ready) and d.own_landing(ready, "me") is ready
    assert "ready again" in d.turn_comment(ready) and "ahead of PRs that never held one" in \
        d.turn_comment(ready)

    # its next turn is an ordinary held one that remembers — whoever grants it —
    # and on it the same stop keeps the turn
    for instance in ("me", "heir"):
        second = d.single_turn(ready, instance, 400)
        assert (second["released"], second["stopped"], second["given_up"]) == (False, None, 200)
        assert d.held_turn(second, instance) is second and d.own_landing(second, instance) is second
        assert not any(d.gives_turn_up(second, outcome) for outcome in d.LAND_OUTCOMES)
        assert "gave a turn up once already" in d.turn_comment(second)
        try:
            d.given_up_turn(second, 500, "conflict", "abc")
        except ValueError as e:
            assert "gave one up already" in str(e)
        else:
            raise AssertionError("a PR gave its turn up twice")
    # a stop that is the fleet's to move on never gives a turn up
    for outcome in (*d.LAND_WAITS, "target_moved", "merged"):
        try:
            d.given_up_turn(held, 200, outcome, "abc")
        except ValueError as e:
            assert "keeps its turn" in str(e)
        else:
            raise AssertionError(f"{outcome} gave the turn up")
    # a merge batch's turn is its batch worker's, and a PR that only left a batch
    # gave nothing up
    members = [{"issue": 1, "pr": 10}, {"issue": 2, "pr": 20}]
    assert d.own_landing(d.batch_turn(None, "me", 10, "me-10", members, "gating"), "me") is None
    left = d.unbatched_turn(None, "me", 10, "me-10", "left_out")
    assert d.own_landing(left, "me") is None and not d.turn_given_up(left)
    # …and one that left a batch, took a single turn and gave THAT up says so
    both = d.given_up_turn(d.single_turn(left, "me", 20), 30, "gate_red", "abc")
    assert "gave its landing turn up" in d.turn_comment(both) and both["unbatched"] == "left_out"


def test_the_working_set_keeps_a_pr_being_fixed_off_its_turn_out_of_the_queue():
    """ADR-0045, as `afk rebuild` reports it: `fixing` while the worker fixes —
    no turn out, not in the merge queue — then `awaiting_turn` at its head."""
    cfg = d.resolve_config({"gate": {"ci": "local", "local_command": "make test"}})
    issues = [{"number": n, "title": f"i{n}", "labels": ["ready-for-agent"], "updatedAt": "t"}
              for n in (1, 2, 3)]
    prs = [{"number": n * 10, "headRefOid": f"h{n}", "statusCheckRollup": [],
            "closingIssuesReferences": [{"number": n}]} for n in (1, 2, 3)]
    claims = [{"number": n, "instance": "me", "sha": f"s{n}"} for n in (1, 2, 3)]
    beats = [{"instance": "me", "ts": 1000}]
    issues, prs, claims = _gathered(issues, prs, claims)

    def rows(turn, config=cfg):
        ws = d.assemble_working_set(issues, prs, claims, beats, "me", 1000, config, turns={3: turn})
        return ws, {m["number"]: (m["status"], m["board_phase"], m["stopped"], m["given_up"])
                    for m in ws["mine"]}

    given_up = d.given_up_turn(d.single_turn(None, "me", 900), 950, "conflict", "h3")
    ws, said = rows(given_up)
    assert said[3] == ("fixing", "fixing", "conflict", True)
    assert said[1] == ("awaiting_turn", "awaiting_turn", None, False)
    assert ws["merge_order"] == [1, 2] and d.asks_after(ws["mine"]) == [3]
    assert d.turn_holder(ws, "me") == (None, None)                 # no turn is out
    assert d.batch_candidates(ws["mine"], ws["merge_order"], cfg) == [1, 2]
    # in `required` its red checks are what it is fixing, never a `failure`
    required = d.resolve_config({})
    red = [{**p, "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "FAILURE"}]}
           for p in prs]
    ws_red = d.assemble_working_set(issues, red, claims, beats, "me", 1000, required,
                                    turns={3: given_up})
    assert {m["number"]: m["status"] for m in ws_red["mine"]} == \
        {1: "failure", 2: "failure", 3: "fixing"}

    # ready again: at the head of the queue, on a single turn, its board saying so
    ws, said = rows(d.next_turn(given_up, at=980, stopped="awaiting_turn"))
    assert said[3] == ("awaiting_turn", "ready_again", None, True)
    assert ws["merge_order"] == [3, 1, 2] and d.turn_due(ws["mine"], ws["merge_order"]) == 3
    assert d.asks_after(ws["mine"]) == [] and d.batch_candidates(ws["mine"], ws["merge_order"], cfg) == []
    # on its second turn it is an ordinary landing row that remembers
    ws, said = rows(d.next_turn(d.single_turn(given_up, "me", 990), stopped="conflict", head="h3"))
    assert said[3] == ("landing", "landing", "conflict", True) and ws["merge_order"] == [3, 1, 2]
    assert d.turn_due(ws["mine"], ws["merge_order"]) is None        # fixed with the turn held
    # a turn a DEAD fleet gave up is nobody's: the PR waits for a turn of mine, on
    # which — `given_up` staying on the marker — a conflict keeps the turn
    ws, said = rows(d.given_up_turn(d.single_turn(None, "old", 900), 950, "conflict", "h3"))
    assert said[3] == ("awaiting_turn", "awaiting_turn", None, True)
    assert ws["merge_order"] == [3, 1, 2]

    for phase, words in (("fixing", ("turn given up", "being fixed")),
                         ("ready_again", ("ready again", "awaiting its turn"))):
        board = d.render_status_board(phase, "local", 2, instance="me", pr=30)
        assert all(w in board for w in words) and "- [x] PR 已开 (#30)" in board, board
        assert "- [ ] 轮到落地" in board


def test_protection_verdict():
    checks_required = {"required_status_checks": {"strict": True, "contexts": ["ci/build"]}}

    # gate.ci: local + a target that requires checks → merges would be rejected
    # outright, and --admin (the only bypass) would override human review too.
    r = d.protection_verdict("local", checks_required)
    assert r["verdict"] == "error" and r["required_checks"] == ["ci/build"]
    assert "gate.ci: required" in r["detail"]
    # the newer `checks: [{context}]` shape is read too, and de-duplicated
    r = d.protection_verdict("local", {"required_status_checks": {
        "contexts": ["ci/build"], "checks": [{"context": "ci/build"}, {"context": "ci/lint"}]}})
    assert r["required_checks"] == ["ci/build", "ci/lint"]

    # local mode is fine when nothing is required, protected or not
    assert d.protection_verdict("local", None)["verdict"] == "ok"
    assert d.protection_verdict("local", {"required_status_checks": {"contexts": []}})["verdict"] == "ok"
    assert d.protection_verdict("local", {"required_pull_request_reviews": {}})["verdict"] == "ok"

    # an inconclusive read WARNS — never a guess in either direction
    r = d.protection_verdict("local", None, unavailable="HTTP 403: needs admin rights")
    assert r["verdict"] == "warn" and "403" in r["detail"]

    # in required mode, required checks are the gate itself — never an obstacle
    assert d.protection_verdict("required", checks_required)["verdict"] == "ok"
    assert d.protection_verdict("required", None, unavailable="boom")["verdict"] == "ok"


def _verdict_in(body):
    """The verdict one comment body carries, blanks filled in, or None."""
    record = d.read_marker(d.VERDICT_RECORD, body)
    return None if record is None else {"found": True, **d.blank_record(d.VERDICT_RECORD), **record}


def test_the_verdict_marker_round_trips_through_its_reader():
    # one writer, one reader: whatever `verdict_marker` spells, the parser reads back
    for phase in d.VERDICT_PHASES:
        got = _verdict_in(d.verdict_marker(12, phase, [3, 4], "needs pages from #3"))
        assert got == {"found": True, "n": 12, "phase": phase, "blocked_by": [3, 4],
                       "reason": "needs pages from #3"}, phase
        bare = _verdict_in(d.verdict_marker(7, phase))
        assert (bare["n"], bare["phase"], bare["blocked_by"], bare["reason"]) == (7, phase, [], None)
    # what the worker is shown is that same spelling, with placeholders
    shown = d.verdict_marker_format(31)
    assert shown == ("<!--afk:verdict n=31 phase=<already-satisfied|blocked|giving-up|needs-decision> "
                     "[blocked_by=<csv of issue numbers>] [reason=<short>]-->")


def test_a_verdict_marker_is_read_leniently():
    # valid, every field; reason (last) keeps its spaces
    body = ("<!--afk:verdict n=12 phase=blocked blocked_by=3,4 reason=needs pages from #3-->\n"
            "Blocked on #3 and #4 — no PRs there yet.")
    p = _verdict_in(body)
    assert p["found"] is True and p["n"] == 12 and p["phase"] == "blocked"
    assert p["blocked_by"] == [3, 4]
    assert p["reason"] == "needs pages from #3"

    # already-satisfied, no blocked_by / reason
    p = _verdict_in("<!--afk:verdict n=7 phase=already-satisfied-->\nEmpty diff vs base.")
    assert p["phase"] == "already-satisfied" and p["blocked_by"] == [] and p["reason"] is None

    # giving-up, tolerant of extra whitespace around the marker + tokens
    assert _verdict_in("<!--  afk:verdict   phase=giving-up  -->")["phase"] == "giving-up"

    # missing marker → None (a plain human comment is not a verdict)
    assert _verdict_in("just a normal comment, no marker") is None
    assert _verdict_in("") is None
    assert _verdict_in(None) is None

    # malformed: marker present but no phase → found True, phase None. Parse is
    # lenient by design; classify_stopped treats a None/unknown phase as failed, and
    # whether to trust the marker at all stays the tick's call.
    p = _verdict_in("<!--afk:verdict n=9-->")
    assert p["found"] is True and p["phase"] is None and p["blocked_by"] == []


def test_latest_verdict():
    empty = {"found": False, "phase": None, "blocked_by": [], "reason": None, "comment_url": None}
    assert d.latest_verdict([]) == empty
    assert d.latest_verdict(None) == empty
    assert d.latest_verdict([{"body": "hello", "url": "u"}, {"body": None, "url": "u"}]) == empty

    # multiple markers across comments → the LAST (chronological, gh's default order) wins,
    # and its comment url rides along
    comments = [
        {"body": "<!--afk:verdict n=5 phase=blocked blocked_by=2-->", "url": "u1"},
        {"body": "some human chatter in between", "url": "u1b"},
        {"body": "<!--afk:verdict n=5 phase=giving-up-->", "url": "u2"},
    ]
    r = d.latest_verdict(comments)
    assert r["found"] is True and r["phase"] == "giving-up" and r["comment_url"] == "u2"
    assert r["blocked_by"] == []          # the superseded marker contributes nothing


NOW = 1_000_000
GRACE = 300
ZERO = {"commits_ahead": 0, "dirty": False, "last_commit_ts": None, "worktree_mtime_ts": None}


def _verdict(phase, blocked_by=None):
    return {"found": True, "phase": phase, "blocked_by": blocked_by or [],
            "reason": None, "comment_url": "u"}


def _classify(progress, terminal, idle, verdict=None, blockers=None, **more):
    """One worker's classification, the way `afk no-pr` reaches it: is it settled
    by its worker state alone — and only if not, as a stopped worker."""
    reading = {"terminal": terminal, "terminal_idle_seconds": idle, "state": None}
    if more.get("turn"):                    # a whole turn record, saying what the test names
        more["turn"] = d.next_turn(None, **more["turn"])
    return d.settled_by_worker_state(reading, NOW, GRACE, more.get("nudged_at")) or \
        d.classify_stopped(progress, idle, verdict, blockers or {}, NOW, GRACE, **more)


def _no_pr(progress, terminal, idle, verdict=None, blockers=None):
    """`_classify` with the terminal as the only recency signal, `idle` s ago."""
    r = _classify(progress, terminal, idle, verdict, blockers)
    return r["outcome"], r["action"]


def _ps(state=None, started=None, output=None, terminals=1, parent=None):
    """One `orca worktree ps` row: an agent in `state` since `started` s ago, the
    terminal's last output `output` s ago (orca speaks milliseconds)."""
    ms = lambda ago: (NOW - ago) * 1000 if ago is not None else None   # noqa: E731
    agents = [{"state": state, "stateStartedAt": ms(started), "parentPaneKey": parent}] \
        if state else []
    return {"liveTerminalCount": terminals, "lastOutputAt": ms(output), "agents": agents}


def _reading(row, tui_idle=None):
    r = d.read_worker_state(row, NOW, GRACE, tui_idle)
    return r["terminal"], r["terminal_idle_seconds"], r["state"]


def test_read_worker_state_takes_the_runtimes_own_report():
    """ADR-0021: whether a worker is busy is read from what its runtime reported to
    orca — never from what its screen looks like — and checked against the
    terminal's output, so a lost stop report cannot read `working` forever."""
    # working, and the terminal is still producing output: busy
    assert _reading(_ps("working", 900, 5)) == ("busy", 5, "working")
    # working, but nothing on the terminal for a whole grace period: a stop report
    # the runtime lost. Not busy; the silence is timed from the last output
    assert _reading(_ps("working", 900, GRACE)) == ("idle", GRACE, "working")
    assert _reading(_ps("working", 900, None)) == ("idle", None, "working")
    # stopped — its turn ended, or it is waiting on a question — is timed from WHEN
    # it stopped, whatever the terminal redraws after
    assert _reading(_ps("done", 700, 3)) == ("idle", 700, "done")
    assert _reading(_ps("waiting", 40, 3)) == ("idle", 40, "waiting")
    assert _reading(_ps("blocked", 40, 3)) == ("idle", 40, "blocked")
    # a runtime that reports nothing (qoderclicn, hooks not installed): orca's own
    # idle detection for that terminal decides. Its output is no clock — an idle
    # qoderclicn redraws every minute, and would read as alive forever
    assert _reading(_ps(None, None, 12), tui_idle=True) == ("idle", None, None)
    assert _reading(_ps(None, None, 12), tui_idle=False) == ("busy", None, None)
    assert _reading(_ps(None, None, 12)) == ("idle", None, None)
    # a runtime that DOES report is never second-guessed by it
    assert _reading(_ps("done", 700, 3), tui_idle=False) == ("idle", 700, "done")
    # no live terminal, no row at all: the worker is gone
    assert _reading(_ps("working", 1, 1, terminals=0)) == ("none", None, None)
    assert _reading(None) == ("none", None, None)

    # several agents in one worktree (a restarted worker beside a dead pane's last
    # report, a subagent): the top-level one that changed state LAST speaks
    row = _ps("working", 9000, 5)
    row["agents"] += [{"state": "done", "stateStartedAt": (NOW - 60) * 1000, "parentPaneKey": None},
                      {"state": "working", "stateStartedAt": (NOW - 1) * 1000, "parentPaneKey": "p"}]
    assert _reading(row) == ("idle", 60, "done")

    # and the reading is what the classification takes: busy → coding, a stop within
    # grace → coding, a stop past grace → routed on the verdict
    for row, outcome in ((_ps("working", 900, 5), "coding"), (_ps("done", 10, 10), "coding"),
                         (_ps("done", GRACE, 1), "idle_stalled"), (None, "dead")):
        t, idle, _ = _reading(row)
        assert _no_pr(ZERO, t, idle)[0] == outcome, row


def test_classification_coding_needs_a_live_signal():
    # terminal busy: left alone even with a giving-up verdict + zero progress
    assert _no_pr(ZERO, "busy", 9999, _verdict("giving-up")) == ("coding", "leave")
    # idle, but activity within the grace window (a worker between steps)
    assert _no_pr(ZERO, "idle", 120) == ("coding", "leave")
    assert _no_pr({**ZERO, "commits_ahead": 2}, "idle", 120) == ("coding", "leave")
    # the boundary: exactly `grace` seconds idle is no longer within grace
    assert _no_pr(ZERO, "idle", GRACE - 1) == ("coding", "leave")
    assert _no_pr(ZERO, "idle", GRACE) == ("idle_stalled", "nudge")

    # ADR-0013: commits ahead / a dirty tree are STANDING facts, not signs of life.
    # A worker that committed 4×, went idle 33 min ago, and left no PR and no verdict
    # must leave `coding` (a nudge first, then failure handling — see the stalled
    # test) — not be re-read as `coding` on every tick forever.
    assert _no_pr({**ZERO, "commits_ahead": 4}, "idle", 1992) == ("idle_stalled", "nudge")
    assert _no_pr({**ZERO, "dirty": True}, "idle", 9999) == ("idle_stalled", "nudge")
    # …while a busy terminal still wins regardless of what is on the branch
    assert _no_pr({**ZERO, "commits_ahead": 4}, "busy", 9999) == ("coding", "leave")


def test_classification_idle_seconds_is_the_most_recent_sign_of_life():
    def idle(progress, terminal_idle):
        return _classify(progress, "idle", terminal_idle)

    # three clocks, the freshest wins — whichever one it is
    r = idle({**ZERO, "last_commit_ts": NOW - 5000, "worktree_mtime_ts": NOW - 40}, 9000)
    assert r["idle_seconds"] == 40 and r["outcome"] == "coding"       # an uncommitted edit
    r = idle({**ZERO, "last_commit_ts": NOW - 60, "worktree_mtime_ts": NOW - 5000}, 9000)
    assert r["idle_seconds"] == 60 and r["outcome"] == "coding"       # a fresh commit
    r = idle({**ZERO, "last_commit_ts": NOW - 5000, "worktree_mtime_ts": NOW - 5000}, 10)
    assert r["idle_seconds"] == 10 and r["outcome"] == "coding"       # terminal activity
    # all three stale → idle past grace
    r = idle({**ZERO, "last_commit_ts": NOW - 5000, "worktree_mtime_ts": NOW - 4000}, 3000)
    assert r["idle_seconds"] == 3000 and r["outcome"] == "idle_stalled"

    # nothing known at all is NOT "within grace": unknown never keeps a claim parked
    r = idle(ZERO, None)
    assert r["idle_seconds"] is None and r["outcome"] == "idle_stalled"
    r = idle(None, None)                                              # unreadable worktree
    assert r["idle_seconds"] is None and r["outcome"] == "idle_stalled"
    # a clock skewed into the future reads as "just now", never a negative age
    assert idle({**ZERO, "worktree_mtime_ts": NOW + 30}, None)["idle_seconds"] == 0
    skew = d.CLOCK_SKEW_TOLERANCE_SECONDS
    assert idle({**ZERO, "worktree_mtime_ts": NOW + skew}, None)["idle_seconds"] == 0
    # …but only that far: a file dated next week is no sign of life, so it neither
    # keeps a stopped worker within grace nor hides the signs that are real
    r = idle({**ZERO, "worktree_mtime_ts": NOW + skew + 1}, None)
    assert r["idle_seconds"] is None and r["outcome"] == "idle_stalled"
    r = idle({**ZERO, "last_commit_ts": NOW - 5000, "worktree_mtime_ts": NOW + 7 * 86400}, 3000)
    assert r["idle_seconds"] == 3000 and r["outcome"] == "idle_stalled"
    # the same for the clocks orca reports: output dated ahead is not output now
    assert _reading(_ps("working", 900, -30)) == ("busy", 0, "working")
    assert _reading(_ps("working", 900, -skew - 1)) == ("idle", None, "working")


def test_classification_routes_idle_workers_on_their_verdict():
    # already-satisfied + a truly empty branch → close + release
    assert _no_pr(ZERO, "idle", 600, _verdict("already-satisfied")) == ("idle_done", "close_release")
    # …but work on the branch REFUTES it → failure handling, not a closed issue
    assert _no_pr({**ZERO, "commits_ahead": 2}, "idle", 9999, _verdict("already-satisfied")) == \
        ("idle_failed", "next_attempt")
    assert _no_pr({**ZERO, "dirty": True}, "idle", 9999, _verdict("already-satisfied")) == \
        ("idle_failed", "next_attempt")

    # giving-up, a garbage phase → failed: the worker DECLARED something
    assert _no_pr(ZERO, "idle", 600, _verdict("giving-up")) == ("idle_failed", "next_attempt")
    assert _no_pr(ZERO, "idle", 600, _verdict("weird-phase")) == ("idle_failed", "next_attempt")
    # needs-decision → escalated, never failed: the issue's owner is the only one who
    # can supply what is missing, so no attempt is spent on it — work on the branch or not
    assert _no_pr(ZERO, "idle", 600, _verdict("needs-decision")) == ("idle_undecided", "escalate")
    assert _no_pr({**ZERO, "commits_ahead": 2}, "idle", 9999, _verdict("needs-decision")) == \
        ("idle_undecided", "escalate")
    # no verdict at all, a not-found verdict → it declared nothing: stalled, not failed
    assert _no_pr(ZERO, "idle", 600, None) == ("idle_stalled", "nudge")
    assert _no_pr(ZERO, "idle", 600, {"found": False}) == ("idle_stalled", "nudge")

    # dead: no live worker/terminal at all → orphan path, whatever it left behind
    assert _no_pr({**ZERO, "commits_ahead": 3}, "none", 5, _verdict("blocked", [1])) == \
        ("dead", "orphan")


def test_classification_nudges_a_silent_worker_once_before_failing_it():
    """A worker idle past grace with NO verdict stopped without an outcome — most
    often it is waiting on a question nobody will answer. Failing it discards its
    work and sends a fresh worker into the same wall, so it is nudged first; a
    nudge is spent once (ADR-0018)."""
    def silent(idle, **nudge):
        r = _classify(ZERO, "idle", idle, **nudge)
        return r["outcome"], r["action"], r["idle_seconds"]

    assert silent(600) == ("idle_stalled", "nudge", 600)
    # the nudge is a sign of life: the worker gets a whole grace period to answer it
    assert silent(600, nudged_at=NOW - 10) == ("coding", "leave", 10)
    assert silent(600, nudged_at=NOW - GRACE + 1)[:2] == ("coding", "leave")
    # …and silence after that is a failure, never a second nudge
    assert silent(9000, nudged_at=NOW - GRACE) == ("idle_failed", "next_attempt", GRACE)
    # nowhere to record a nudge (no worktree on this machine) → it fails at once
    assert silent(600, can_nudge=False)[:2] == ("idle_failed", "next_attempt")

    # a nudge never overrides what the worker DECLARED, nor a terminal that is gone
    def routed(verdict, terminal="idle", **nudge):
        r = _classify(ZERO, terminal, 600, verdict, **nudge)
        return r["outcome"], r["action"]
    assert routed(_verdict("giving-up")) == ("idle_failed", "next_attempt")
    assert routed(_verdict("already-satisfied"), nudged_at=NOW - 9000) == ("idle_done", "close_release")
    assert routed(None, terminal="none") == ("dead", "orphan")

    # a landing turn is the same kind of sign of life (ADR-0027): one grace period
    # to start on it, then the same nudge — and silent after THAT, the worker is
    # restarted onto the turn, not failed (ADR-0035): the PR was judged ready, and
    # what did not happen is the landing. Never a parked queue either way
    def turn(idle, at, stopped=None, restarted=None, **more):
        r = _classify({**ZERO, "commits_ahead": 3}, "idle", idle,
                      turn={"at": at, "stopped": stopped, "restarted": restarted}, **more)
        return r["outcome"], r["action"], r["idle_seconds"]
    assert turn(9000, NOW - 10) == ("coding", "leave", 10)
    assert turn(9000, NOW - GRACE) == ("idle_stalled", "nudge", GRACE)
    assert turn(9000, NOW - 2 * GRACE, nudged_at=NOW - GRACE) == \
        ("idle_stalled", "restart", GRACE)
    assert _classify({**ZERO, "commits_ahead": 3}, "idle", 9000, nudged_at=NOW - GRACE,
                     turn={"at": NOW - 2 * GRACE, "stopped": None})["cause"] == "silent_on_turn"
    # ONE restart per turn: the restarted worker is told anew (`at` moves), nudged
    # once like any worker, and silent again after that it is ESCALATED with its
    # PR, branch and worktree kept — never `next_attempt`: no silence of a landing
    # turn reaches `afk fail` (#91)
    assert turn(9000, NOW - 10, restarted=NOW - 10) == ("coding", "leave", 10)
    assert turn(9000, NOW - GRACE, restarted=NOW - GRACE) == ("idle_stalled", "nudge", GRACE)
    assert turn(9000, NOW - 2 * GRACE, restarted=NOW - 2 * GRACE, nudged_at=NOW - GRACE) == \
        ("idle_stalled", "escalate", GRACE)
    assert _classify(ZERO, "idle", 9000, nudged_at=NOW - GRACE,
                     turn={"at": NOW - 2 * GRACE, "restarted": NOW - 2 * GRACE})["cause"] == \
        "silent_past_restart"
    # with nowhere to record a nudge the turn takes its next rung at once — the
    # restart, then the escalation — where a PR-less claim fails at once
    assert _classify(ZERO, "idle", 9000, can_nudge=False,
                     turn={"at": NOW - GRACE})["action"] == "restart"
    assert _classify(ZERO, "idle", 9000, can_nudge=False,
                     turn={"at": NOW - GRACE, "restarted": NOW - GRACE})["action"] == "escalate"
    assert _classify(ZERO, "idle", 9000, can_nudge=False)["cause"] == "silent_unnudgeable"
    # a turn that is not one PR's — a merge batch's, or a marker that holds no turn
    # — is never restarted or escalated for its silence; nor is a claim that has no
    # turn (a no_pr claim)
    for held in ({"at": NOW - 2 * GRACE, "batch": "b1"}, {"at": NOW - 2 * GRACE, "released": True},
                 {}, None):
        r = _classify(ZERO, "idle", 9000, nudged_at=NOW - GRACE, turn=held)
        assert (r["cause"], r["action"]) == ("silent_after_nudge", "next_attempt"), held
        r = _classify(ZERO, "idle", 9000, can_nudge=False, turn=held)
        assert (r["cause"], r["action"]) == ("silent_unnudgeable", "next_attempt"), held
    # a PR being fixed OFF the turn it gave up is on those same rungs (ADR-0045):
    # grace from its last `afk land`, a nudge, one restart, then the escalation —
    # and once it said it is ready again its worker has nothing left to do
    off = {"released": True, "given_up": NOW - 9 * GRACE}
    assert turn(9000, NOW - 10, stopped="conflict", **{}) == ("coding", "leave", 10)
    for stopped in d.LAND_FIXES:
        fixing = {**off, "stopped": stopped}
        r = lambda **more: (lambda c: (c["cause"], c["action"]))(
            _classify({**ZERO, "commits_ahead": 3}, "idle", 9000, **more))
        assert r(turn={**fixing, "at": NOW - 10}) == ("within_grace", "leave")
        assert r(turn={**fixing, "at": NOW - GRACE}) == ("silent", "nudge")
        assert r(turn={**fixing, "at": NOW - 2 * GRACE}, nudged_at=NOW - GRACE) == \
            ("silent_on_turn", "restart")
        assert r(turn={**fixing, "at": NOW - 2 * GRACE, "restarted": NOW - 2 * GRACE},
                 nudged_at=NOW - GRACE) == ("silent_past_restart", "escalate")
        assert r(turn={**fixing, "at": NOW - GRACE}, can_nudge=False) == ("silent_on_turn", "restart")
    assert _classify(ZERO, "idle", 9000, nudged_at=NOW - GRACE,
                     turn={**off, "at": NOW - 2 * GRACE, "stopped": "awaiting_turn"})["cause"] == \
        "silent_after_nudge"
    a = lambda **said: d.next_turn(None, **said)
    assert d.restartable_turn(a(at=1)) and not d.restartable_turn(a(at=1, restarted=2))
    assert d.single_turn_held(a(at=1, restarted=2)) and not d.single_turn_held(a(at=1, batch="b"))
    r = _classify(ZERO, "none", None, turn={"at": NOW - 10})
    assert (r["outcome"], r["action"]) == ("dead", "orphan")      # a gone terminal is still dead
    # a worker whose landing stopped FOR THE TICK (CI, a verify, absent checks) is
    # waiting on the tick, not silent: never nudged, never failed, however long ago
    for stopped in d.LAND_WAITS:
        assert turn(9000, NOW - 5 * GRACE, stopped=stopped)[:2] == ("coding", "leave"), stopped
        assert turn(9000, NOW - 5 * GRACE, stopped=stopped, nudged_at=NOW - 3 * GRACE)[:2] == \
            ("coding", "leave")
    # …while one that stopped on something that is ITS to fix is silent like any other,
    # and is restarted onto the turn the same way when the nudge goes unanswered
    for stopped in ("conflict", "gate_red"):
        assert turn(9000, NOW - GRACE, stopped=stopped)[:2] == ("idle_stalled", "nudge")
        assert turn(9000, NOW - 2 * GRACE, stopped=stopped, nudged_at=NOW - GRACE)[:2] == \
            ("idle_stalled", "restart")
    # what the worker declared still wins, and so does a gone terminal
    r = _classify(ZERO, "idle", 9000, _verdict("giving-up"),
                  turn={"at": NOW - 9000, "stopped": "awaiting_ci"})
    assert (r["outcome"], r["action"]) == ("idle_failed", "next_attempt")
    r = _classify(ZERO, "none", None, turn={"stopped": "awaiting_ci"})
    assert (r["outcome"], r["action"]) == ("dead", "orphan")


def test_stall_reason_carries_where_the_worker_stopped():
    screen = ["", "● 要我按这段说明把 #41 从头做到开 PR 吗？", "   ", "x" * 500, "❯ "]
    assert d.stall_tail(screen) == ["● 要我按这段说明把 #41 从头做到开 PR 吗？", "x" * 200, "❯"]
    assert d.stall_tail([str(i) for i in range(100)], limit=3) == ["97", "98", "99"]
    assert d.stall_tail(None) == []
    reason = d.stall_reason("idle with no outcome\n", screen)
    assert reason.startswith("idle with no outcome\n\nThe worker stopped")
    assert "```\n● 要我按这段说明把 #41 从头做到开 PR 吗？\n" in reason and reason.endswith("❯\n```")
    assert d.stall_reason("idle with no outcome", []) == "idle with no outcome"   # nothing to add
    # the nudge is one short line — a long one is the very paste it is sent to break
    for text in (d.nudge_text(), d.nudge_text("/w/.git/afk-worker-prompt.md")):
        assert "\n" not in text and len(text) < 400 and "afk:verdict" in text
    assert "/w/.git/afk-worker-prompt.md" in d.nudge_text("/w/.git/afk-worker-prompt.md")


def test_classification_blocked_routes_on_the_blockers_real_state():
    def blocked(named, states, progress=ZERO):
        return _classify(progress, "idle", 600, _verdict("blocked", named), states)

    # every named blocker closed → the DAG cleared: re-dispatch (keep the claim)
    r = blocked([42, 43], {42: "closed", 43: "closed"})
    assert (r["outcome"], r["action"], r["pending_blockers"]) == ("idle_blocked", "redispatch", [])
    # one still open that the backlog will resolve → park on it, and say which (ADR-0022)
    r = blocked([42, 43], {42: "closed", 43: "waiting"})
    assert (r["outcome"], r["action"], r["pending_blockers"]) == ("idle_blocked", "park", [43])
    assert blocked([42, 43], {42: "waiting", 43: "waiting"})["pending_blockers"] == [42, 43]
    # one that nothing will resolve → a real DAG gap: escalate, whatever the others are
    r = blocked([42, 43], {42: "waiting", 43: "unmet"})
    assert (r["outcome"], r["action"], r["pending_blockers"]) == ("idle_blocked", "escalate", [42, 43])
    r = blocked([42, 43], {42: "closed", 43: "open"})                 # not a standing: unmet
    assert (r["outcome"], r["action"], r["pending_blockers"]) == ("idle_blocked", "escalate", [43])
    # a blocker whose state could not be read is NOT provably closed → still open
    r = blocked([42, 43], {42: "closed", 43: None})
    assert (r["action"], r["pending_blockers"]) == ("escalate", [43])
    assert blocked([42], {})["pending_blockers"] == [42]
    # a `blocked` verdict naming NO blocker can never clear — re-dispatching it
    # would loop forever outside the retry ladder, so it escalates
    r = blocked([], {})
    assert (r["outcome"], r["action"], r["pending_blockers"]) == ("idle_blocked", "escalate", [])
    # blocked routes on the DAG even with work on the branch
    assert blocked([42], {42: "open"}, {**ZERO, "commits_ahead": 4})["action"] == "escalate"
    assert blocked([42], {42: "waiting"}, {**ZERO, "commits_ahead": 4})["action"] == "park"
    # every route it can return is one the docs are held to
    assert {("idle_blocked", a) for a in ("redispatch", "park", "escalate")} <= set(d.NO_PR_ROUTES)

    # pending_blockers is reported only for a blocked verdict
    assert _classify(ZERO, "idle", 600, _verdict("giving-up", [42]),
                     {42: "open"})["pending_blockers"] == []


def test_blocker_standings_tell_a_dependency_the_backlog_resolves_from_one_nothing_will():
    """A worker's `blocked` verdict is the one fact the backlog was missing: an
    undeclared dependency. Whether to wait on it or hand it to a human turns on
    whether anything will ever close the blocker (ADR-0022)."""
    ready = {"state": "open", "state_reason": None, "labels": ["ready-for-agent"],
             "pull_request": False}
    bare = {**ready, "labels": ["bug"]}
    cfg = {"ready_label": "ready-for-agent", "epic_labels": ["epic", "prd"]}

    def stand(named, blockers, claimed=(), open_pr=(), edges=None, number=7):
        rows = d.blocker_standings(number, named, cfg, blockers=blockers, claimed=set(claimed),
                                   open_pr=set(open_pr), edges=edges or {})
        assert [r["number"] for r in rows] == list(named)            # one row each, in order
        assert all(r["standing"] in d.BLOCKER_STANDINGS for r in rows)
        assert all((r["reason"] is None) == (r["standing"] != "unmet") for r in rows)
        return {r["number"]: r["standing"] for r in rows}, {r["number"]: r["reason"] for r in rows}

    # waiting: a fleet holds it (any owner — a stale claim is reclaimed and
    # continued), a PR is open for it, or it is merely ready
    assert stand([1], {1: bare}, claimed=[1])[0] == {1: "waiting"}
    assert stand([1], {1: bare}, open_pr=[1])[0] == {1: "waiting"}
    assert stand([1], {1: ready})[0] == {1: "waiting"}
    # …including one that is itself waiting on blockers of its own
    assert stand([1], {1: ready}, edges={1: [2], 2: []})[0] == {1: "waiting"}
    # closed and done: no longer a blocker
    done = {**bare, "state": "closed", "state_reason": "completed"}
    assert stand([1], {1: done})[0] == {1: "closed"}
    assert stand([1], {1: {**done, "state_reason": None}})[0] == {1: "closed"}

    # unmet — each with the reason a human is told
    for blocker, kw, why in (
            (None, {}, "could not be read"),
            (bare, {}, "no ready-for-agent label"),
            ({**ready, "labels": ["ready-for-agent", "prd"]}, {"claimed": [1]}, "epic label (prd)"),
            ({**done, "state_reason": "not_planned"}, {}, "not planned"),
            ({**done, "state_reason": "duplicate"}, {}, "duplicate"),
            ({**ready, "pull_request": True}, {}, "pull request")):
        standings, reasons = stand([1], {1: blocker}, **kw)
        assert standings == {1: "unmet"} and why in reasons[1], (blocker, reasons)
    assert stand([1], {})[0] == {1: "unmet"}                          # never read at all

    # a cycle: the blocker already depends on the issue — directly, or down a chain
    standings, reasons = stand([1], {1: ready}, edges={1: [7]})
    assert standings == {1: "unmet"} and "cycle" in reasons[1]
    assert stand([1], {1: ready}, edges={1: [2], 2: [3], 3: [7]})[0] == {1: "unmet"}
    assert stand([7], {7: ready})[0] == {7: "unmet"}                  # names itself
    assert stand([1], {1: ready}, edges={1: [2], 2: [1]})[0] == {1: "waiting"}   # a loop elsewhere
    # each named blocker stands on its own
    assert stand([1, 2, 3], {1: done, 2: ready, 3: bare})[0] == \
        {1: "closed", 2: "waiting", 3: "unmet"}

    assert d.depends_on(1, 3, {1: [2], 2: [3]}) and not d.depends_on(3, 1, {1: [2], 2: [3]})
    assert d.depends_on(1, 1, {})

    # and the route those standings come to
    assert d.blocked_route([1, 2], {1: "closed", 2: "closed"}) == \
        {"action": "redispatch", "pending_blockers": []}
    assert d.blocked_route([1, 2], {1: "closed", 2: "waiting"}) == \
        {"action": "park", "pending_blockers": [2]}
    assert d.blocked_route([1, 2], {1: "unmet", 2: "waiting"}) == \
        {"action": "escalate", "pending_blockers": [1, 2]}
    assert d.blocked_route([], {}) == {"action": "escalate", "pending_blockers": []}
    assert d.blocked_route([1], None) == {"action": "escalate", "pending_blockers": [1]}

    # `afk park`'s own check: None when parkable, else what to do instead
    def refusal(named, blockers, phase="blocked", **kw):
        rows = d.blocker_standings(7, named, cfg, blockers=blockers, claimed=set(),
                                   open_pr=set(), edges={}, **kw)
        return d.park_refusal(_verdict(phase, named), rows)

    assert refusal([1, 2], {1: done, 2: ready}) is None
    assert "not `blocked`" in refusal([1], {1: ready}, phase="giving-up")
    assert "not `blocked`" in d.park_refusal({"found": False, "phase": None, "blocked_by": []}, [])
    assert "dispatch it again" in refusal([1], {1: done})
    assert "names no blocker" in refusal([], {})
    why = refusal([1, 2, 3], {1: ready, 2: bare, 3: None})
    assert "#2 is open but no fleet" in why and "#3 could not be read" in why
    assert "#1" not in why and "escalate it" in why                  # only the unmet ones


def test_the_frontier_and_a_blockers_standing_read_one_set_of_label_rules():
    """What keeps an issue off the frontier and what makes a blocker `unmet` are
    the same facts about its labels. Two copies would drift: a rule added to the
    frontier alone leaves parked issues waiting on a blocker no fleet will ever
    dispatch."""
    assert d.label_bars(["ready-for-agent", "bug"], "ready-for-agent", ["epic"]) == {}
    assert d.label_bars(None, "go", []) == {"not_ready": "no go label"}
    bars = d.label_bars(["epic", "prd"], "ready-for-agent", ["epic", " prd ", ""])
    assert list(bars) == ["not_ready", "epic"] and bars["epic"] == "epic label (epic, prd)"

    cfg = {"ready_label": "ready-for-agent", "epic_labels": ["epic"]}
    for labels in (["ready-for-agent"], ["bug"], ["ready-for-agent", "epic"], ["epic"], []):
        row = {**_ELIGIBLE, "number": 1, "labels": labels}
        front = d.select_frontier([row], cfg["ready_label"], cfg["epic_labels"])
        blocker = {"state": "open", "state_reason": None, "labels": labels, "pull_request": False}
        (standing,) = d.blocker_standings(7, [1], cfg, blockers={1: blocker}, claimed=set(),
                                          open_pr=set(), edges={})
        # an unclaimed, PR-less open issue is `waiting` exactly when it is dispatchable
        assert (standing["standing"] == "waiting") == (front["dispatch"] == [1]), labels
        if front["excluded"]:                    # …and is told off for the frontier's own reason
            assert front["excluded"][0]["reason"] in standing["reason"], labels


def _takeover_state(now):
    claims = [
        {"number": 3, "instance": "dead-1", "host": "macbook", "sha": "s3"},
        {"number": 1, "instance": "dead-1", "host": "macbook", "sha": "s1"},
        {"number": 7, "instance": "live-2", "host": "studio", "sha": "s7"},
        {"number": 9, "instance": "me", "host": "macbook", "sha": "s9"},
        {"number": 11, "instance": None, "host": None, "sha": "s11"},   # malformed marker
    ]
    heartbeats = {"dead-1": now - TTL - 600,   # long expired — the quota hard stop
                  "live-2": now - 30,          # beating
                  "me": now - 5,
                  "drained-3": now - 60}       # alive, holds nothing (stopped cleanly)
    return _whole(_CLAIM, claims), heartbeats


def test_group_instances():
    now = 1_000_000
    claims, heartbeats = _takeover_state(now)
    rows = d.group_instances(claims, heartbeats, "me", now, TTL)
    by = {r["instance"]: r for r in rows}

    # the dead fleet is discoverable from its markers alone, with what it holds
    assert by["dead-1"]["claims"] == [1, 3] and by["dead-1"]["claim_count"] == 2
    assert by["dead-1"]["host"] == "macbook"
    assert by["dead-1"]["fresh"] is False and by["dead-1"]["heartbeat_age"] == TTL + 600
    # a live peer and my own fleet are both marked, never takeover candidates by accident
    assert by["live-2"]["fresh"] is True and by["live-2"]["is_me"] is False
    assert by["me"]["is_me"] is True
    # an instance with a heartbeat but no claims is still listed (drained ≠ died mid-flight)
    assert by["drained-3"]["claims"] == [] and by["drained-3"]["fresh"] is True
    # a claim whose marker names nobody shows up as null — already reclaimable as stale
    assert by[None]["claims"] == [11] and by[None]["heartbeat_ts"] is None
    # ordering is stable and useful: most claims first, then by id
    assert [r["instance"] for r in rows] == ["dead-1", "live-2", "me", None, "drained-3"], rows
    assert d.group_instances([], {}, "me", now, TTL) == []


def test_plan_takeover():
    now = 1_000_000
    claims, heartbeats = _takeover_state(now)

    # the ordinary case: a dead fleet's claims, with the sha each push needs
    r = d.plan_takeover(claims, heartbeats, "dead-1", "me", now, TTL)
    assert r["action"] == "take" and r["fresh"] is False
    assert r["claims"] == [{"number": 1, "sha": "s1", "host": "macbook"},
                           {"number": 3, "sha": "s3", "host": "macbook"}], r

    # a FRESH heartbeat is a warning, not a refusal: confirm first, take nothing yet
    r = d.plan_takeover(claims, heartbeats, "live-2", "me", now, TTL)
    assert r["action"] == "confirm" and r["fresh"] is True
    assert "looks ALIVE" in r["detail"] and r["claims"] == [{"number": 7, "sha": "s7", "host": "studio"}]
    # …and an informed human may override it — atomic underneath either way
    assert d.plan_takeover(claims, heartbeats, "live-2", "me", now, TTL, confirmed=True)["action"] == "take"
    # confirmation is irrelevant when the target is already dead
    assert d.plan_takeover(claims, heartbeats, "dead-1", "me", now, TTL, confirmed=True)["action"] == "take"

    # never take from myself — my own claims are reconciled, not taken
    r = d.plan_takeover(claims, heartbeats, "me", "me", now, TTL, confirmed=True)
    assert r["action"] == "error" and "own instance id" in r["detail"]

    # nothing to take: an unknown id, and a live-but-claimless instance
    assert d.plan_takeover(claims, heartbeats, "typo-9", "me", now, TTL)["action"] == "none"
    r = d.plan_takeover(claims, heartbeats, "drained-3", "me", now, TTL)
    assert r["action"] == "none" and "holds no claims" in r["detail"]

    # a dead instance that never beat at all is takeable (missing heartbeat = stale)
    r = d.plan_takeover([{**_CLAIM, "number": 4, "instance": "ghost", "sha": "s4"}], {}, "ghost-target",
                        "me", now, TTL)
    assert r["action"] == "none"                     # …but only under its real id
    r = d.plan_takeover([{**_CLAIM, "number": 4, "instance": "ghost", "sha": "s4"}], {}, "ghost",
                        "me", now, TTL)
    assert r["action"] == "take" and r["fresh"] is False and r["heartbeat_age"] is None


def test_a_branch_is_the_fleets_by_its_record_never_by_its_name():
    name, again = "sunfmin/issue-9-continuation", "sunfmin/issue-9-continuation-2"
    comments = [{"id": 1, "body": "looks like <!--afk:status-->", "url": "u"},
                {"id": 2, "body": d.branch_comment(name), "url": "u"},
                {"id": 3, "body": "I pushed hotfix/issue-9-my-manual-fix", "url": "u"},
                {"id": 4, "body": d.branch_comment(again), "url": "u"},      # a continuation's
                {"id": 5, "body": d.branch_comment(name), "url": "u"},       # said twice: one name
                {"id": 6, "body": "<!--afk:branch-->", "url": "u"}]          # names nothing
    assert d.branch_comment(name).startswith(f"<!--afk:branch name={name}-->\n")
    assert d.recorded_branches(comments) == [name, again]
    assert d.recorded_branches(None) == []

    heads = ["master", again, name,
             "hotfix/issue-9-my-manual-fix",          # a person's, shaped like orca's
             "sunfmin/issue-9-continuation-3"]        # ... down to the suffix
    assert d.own_branches(heads, [name, again]) == [name, again]
    # a recorded branch the remote no longer has is no branch; None is no name
    assert d.own_branches(heads, ["sunfmin/issue-9-gone", None, name]) == [name]
    assert d.own_branches(heads, []) == [] and d.own_branches(None, [name]) == []


def test_find_orca_worktree():
    rows = [
        {"linkedIssue": None, "path": "/main", "branch": "refs/heads/master",
         "projectId": "github:o/r", "isMainWorktree": True, "lastActivityAt": 99},
        {"linkedIssue": 9, "path": "/wt/old-9", "branch": "refs/heads/sunfmin/issue-9-a",
         "projectId": "github:o/r", "isMainWorktree": False, "lastActivityAt": 100},
        {"linkedIssue": 9, "path": "/wt/new-9", "branch": "refs/heads/sunfmin/issue-9-b",
         "projectId": "github:o/r", "isMainWorktree": False, "lastActivityAt": 200},
        {"linkedIssue": 9, "path": "/wt/other-repo-9", "branch": "refs/heads/x",
         "projectId": "github:o/other", "isMainWorktree": False, "lastActivityAt": 999},
        {"linkedIssue": 7, "path": "/wt/7", "branch": "refs/heads/sunfmin/issue-7",
         "projectId": "github:o/r", "isMainWorktree": False, "lastActivityAt": 300},
    ]
    # several worktrees for one issue → the most recently active; refs/heads/ stripped
    r = d.find_orca_worktree(rows, 9, "o/r")
    assert r == {"found": True, "path": "/wt/new-9", "branch": "sunfmin/issue-9-b"}, r

    # a same-numbered issue in ANOTHER repo is never mistaken for this one
    assert d.find_orca_worktree(rows, 9, "o/other")["path"] == "/wt/other-repo-9"
    # orca lower-cases the project id whatever the repo's casing: compared exactly, a
    # repo with a capital owns no worktree and every live worker is dispatched again
    assert d.find_orca_worktree(rows, 9, "O/R")["path"] == "/wt/new-9"
    assert d.find_orca_worktree([{**rows[1], "projectId": None}], 9, "o/r")["found"] is False
    # no repo filter → any project may match (single-repo machines)
    assert d.find_orca_worktree(rows, 7)["path"] == "/wt/7"
    # the main worktree is never a worker's, and a missing issue is simply not found
    assert d.find_orca_worktree(rows, 42, "o/r") == {"found": False, "path": None, "branch": None}
    assert d.find_orca_worktree([], 9)["found"] is False
    assert d.find_orca_worktree(None, 9)["found"] is False
    # archived leftovers are not recoverable progress
    assert d.find_orca_worktree([{**rows[1], "isArchived": True}], 9, "o/r")["found"] is False
    # the issue number is compared numerically, not by identity: a str/int drift at
    # orca's JSON boundary would silently downgrade every tier-1 recovery
    assert d.find_orca_worktree([{**rows[1], "linkedIssue": "9"}], 9, "o/r")["path"] == "/wt/old-9"
    assert d.find_orca_worktree([{**rows[1], "linkedIssue": "nine"}], 9, "o/r")["found"] is False


def test_furthest_ahead():
    # several branches can match one issue (an earlier attempt left one behind)
    assert d.furthest_ahead({"a/issue-9-x": 1, "b/issue-9-y": 3}) == "b/issue-9-y"
    # ties go to the first by name, whatever order they were measured in
    assert d.furthest_ahead({"z": 2, "m": 2, "a": 1}) == "m"
    # an unmeasurable branch (None) never beats a measured one, but is still a
    # candidate when it is all there is
    assert d.furthest_ahead({"a": None, "b": 1}) == "b"
    assert d.furthest_ahead({"b": None, "a": None}) == "a"
    assert d.furthest_ahead({"only": 0}) == "only"
    assert d.furthest_ahead({}) is None


def test_select_recovery():
    # tier 1 — a worktree is still HERE: reuse it, continue-mode prompt. Never torn down.
    r = d.select_recovery({"present": True, "commits_ahead": 3, "dirty": False},
                          {"name": "b", "commits_ahead": 3})
    assert (r["tier"], r["action"], r["prompt"]) == (1, "reuse_worktree", "continue")
    # tier 1 — uncommitted-only work still counts (this is what makes tier 1 lossless)
    r = d.select_recovery({"present": True, "commits_ahead": 0, "dirty": True}, None)
    assert (r["tier"], r["prompt"]) == (1, "continue")
    # tier 1 — provably pristine: reuse the worktree, but the FRESH prompt (nothing to continue)
    r = d.select_recovery({"present": True, "commits_ahead": 0, "dirty": False},
                          {"name": "b", "commits_ahead": 0})
    assert (r["tier"], r["action"], r["prompt"]) == (1, "reuse_worktree", "fresh")
    # tier 1 — clean worktree but the branch carries pushed commits → still continue
    r = d.select_recovery({"present": True, "commits_ahead": 0, "dirty": False},
                          {"name": "b", "commits_ahead": 2})
    assert (r["tier"], r["prompt"]) == (1, "continue") and "pushed" in r["reason"]
    # tier 1 — unreadable progress is NOT pristine: never hand a fresh prompt over it
    r = d.select_recovery({"present": True, "commits_ahead": None, "dirty": False}, None)
    assert (r["tier"], r["prompt"]) == (1, "continue")

    # tier 2 — no worktree, but the dead worker pushed → recreate at the branch tip
    r = d.select_recovery({"present": False}, {"name": "sunfmin/issue-9-a", "commits_ahead": 4})
    assert (r["tier"], r["action"], r["prompt"]) == (2, "recreate_at_tip", "continue")
    assert "sunfmin/issue-9-a" in r["reason"] and "4 commit" in r["reason"]

    # tier 3 — nothing survived: the old tear-down-and-re-dispatch, now the fallback only
    for wt, br in (({"present": False}, {"name": "b", "commits_ahead": 0}),
                   ({"present": False}, {"name": None, "commits_ahead": None}),
                   (None, None),
                   ({}, {})):
        r = d.select_recovery(wt, br)
        assert (r["tier"], r["action"], r["prompt"]) == (3, "dispatch_fresh", "fresh"), (wt, br, r)

    # a branch we could not measure is not evidence of progress (never a silent tier 2)
    assert d.select_recovery({"present": False}, {"name": "b", "commits_ahead": None})["tier"] == 3


def test_select_recovery_knows_a_retry_and_a_held_landing_turn():
    here = {"present": True, "commits_ahead": 3, "dirty": False}
    pushed = {"name": "b", "commits_ahead": 4}
    # a retry discarded the attempt: from base, whatever is still lying around
    r = d.select_recovery(here, pushed, fresh=True)
    assert (r["tier"], r["action"], r["prompt"]) == (3, "dispatch_fresh", "fresh")
    assert "discarded" in r["reason"]
    # a PR that holds the turn: its worker is started on the landing brief …
    r = d.select_recovery(here, pushed, landing_pr=30)
    assert (r["tier"], r["action"], r["prompt"]) == (1, "reuse_worktree", "landing")
    # … and with no worktree here, at the PR's head — never from base, pushed or not
    for br in (pushed, {"name": "b", "commits_ahead": 0}, None):
        r = d.select_recovery({"present": False}, br, landing_pr=30)
        assert (r["tier"], r["action"], r["prompt"]) == (2, "recreate_at_tip", "landing"), br
        assert "PR #30" in r["reason"]


def test_remotes_of_names_the_remotes_that_are_the_target_repo():
    urls = {"origin": "git@github.com:Acme/Widgets.git", "https": "https://github.com/acme/widgets",
            "fork": "https://github.com/me/widgets.git", "other": "https://github.com/acme/widgets-old",
            "mirror": "/srv/git/widgets.git"}
    assert d.remotes_of(urls, "acme/widgets") == ["https", "origin"]
    assert d.remotes_of({}, "acme/widgets") == d.remotes_of(None, "acme/widgets") == []


def test_turn_holder_reads_one_precedence():
    mine_b = {"id": "m", "instance": "me", "members": [], "phase": None}
    dead_b = {"id": "x", "instance": "gone", "members": [], "phase": None}
    rows = [_mine(1, "landing", pr=10), _mine(2, "awaiting_turn", pr=20)]
    ws = lambda batches, mine=(): {"batches": batches, "mine": list(mine)}
    assert d.turn_holder(ws([mine_b, dead_b], rows), "me") == ("dead", [dead_b])
    assert d.turn_holder(ws([mine_b], rows), "me") == ("mine", mine_b)
    assert d.turn_holder(ws([], rows), "me") == ("single", 1)
    assert d.turn_holder(ws([], rows[1:]), "me") == (None, None)


def test_current_attempt_is_the_one_reader_of_the_label():
    assert d.current_attempt([]) == 0 and d.current_attempt(None) == 0
    assert d.current_attempt(["ready-for-agent"]) == 0          # never retried
    assert d.current_attempt(["afk-attempt/1", "ready-for-agent"]) == 1
    # highest label wins even if out of order; junk suffixes are not attempts
    assert d.current_attempt(["afk-attempt/x", "afk-attempt/1", "afk-attempt/3"]) == 3
    assert d.current_attempt(["afk-attempt/", "afk-attempt/-2", "afk-attempt/1.5", 7]) == 0
    assert d.current_attempt(["xafk-attempt/9", "afk-attempt/2"]) == 2


def test_next_attempt():
    assert d.next_attempt(0, 2) == {"action": "retry", "attempt": 1, "to_label": "afk-attempt/1"}
    assert d.next_attempt(1, 2) == {"action": "retry", "attempt": 2, "to_label": "afk-attempt/2"}
    assert d.next_attempt(2, 2) == {"action": "escalate", "attempt": 2}
    assert d.next_attempt(3, 2) == {"action": "escalate", "attempt": 3}
    # retry: 0 escalates the first failure
    assert d.next_attempt(0, 0) == {"action": "escalate", "attempt": 0}
    # the label it hands back is one current_attempt reads as the next number —
    # the two are a round trip, so the ladder cannot stall on its own output
    n = 0
    for _ in range(3):
        step = d.next_attempt(n, 9)
        n = d.current_attempt([step["to_label"]])
        assert n == step["attempt"]
    assert n == 3


def test_a_failure_already_counted_is_retried_without_being_counted_again():
    one, starting = "afk-attempt/1", d.ATTEMPT_STARTING
    # counting a failure: the number and "its worker has not started" in ONE edit
    assert d.retry_labels(["ready-for-agent"], one) == ([one, starting], [])
    assert d.retry_labels(["bug", one], "afk-attempt/2") == (["afk-attempt/2", starting], [one])
    assert d.attempt_starting(["bug", one, starting]) and d.current_attempt([one, starting]) == 1
    assert not d.attempt_starting(["bug", one]) and not d.attempt_starting(None)
    assert not d.attempt_starting([starting])           # a hand-edit: no attempt it could mean

    # the same failure again: the attempt it already made, and no edit to make
    again = d.next_attempt(1, 2, counted=True)
    assert again == {"action": "retry", "attempt": 1, "to_label": one}
    assert d.retry_labels(["bug", one, starting], again["to_label"]) == ([], [])
    # …even when it was the last retry: what is cut short is that retry, not an escalation
    assert d.next_attempt(2, 2, counted=True)["action"] == "retry"
    # a worker started ends it (`afk` removes the label), so the next failure is counted
    assert d.next_attempt(1, 2, counted=d.attempt_starting(["bug", one]))["attempt"] == 2
    # an escalation strips it with the rest
    assert d.escalation_labels([one, starting], d.resolve_config({}))[1] == [one, starting]


def test_attempt_and_escalation_labels():
    labels = ["ready-for-agent", "afk-attempt/2", "bug", "afk-attempt/1", "xafk-attempt/9", 7]
    # every attempt label the issue carries — a hand-edit can leave more than one
    assert d.attempt_labels(labels) == ["afk-attempt/1", "afk-attempt/2"]
    assert d.attempt_labels(None) == []

    cfg = d.resolve_config({})
    add, remove = d.escalation_labels(labels, cfg)
    assert add == ["ready-for-human"]
    assert remove == ["afk-attempt/1", "afk-attempt/2", "ready-for-agent"]   # never "bug"
    # only what the issue actually carries is removed: gh refuses an absent label
    assert d.escalation_labels(["bug"], cfg) == (["ready-for-human"], [])
    add, remove = d.escalation_labels(["go"], {**cfg, "ready_label": "go", "escalate_label": "human"})
    assert (add, remove) == (["human"], ["go"])

    body = d.escalation_comment("  the gate needs a secret CI has and I do not  ", 2, "c1", pr=31)
    assert "escalated to a human" in body and "after 2 retries" in body and "#31" in body
    assert body.endswith("the gate needs a secret CI has and I do not")
    assert "after 1 retry)" in d.escalation_comment("x", 1, "c1")
    assert "without a retry" in d.escalation_comment("x", 0, "c1")
    assert "PR" not in d.escalation_comment("x", 0, "c1")


def test_an_escalation_is_recorded_in_its_comment_as_the_claims():
    """The relabel of an escalation strips the attempt count, so what says "this
    claim is already being escalated, after n retries" is the comment (ADR-0033)."""
    def comments(*bodies):
        return [{"id": 100 + i, "body": b, "url": "u"} for i, b in enumerate(bodies)]

    mine = d.escalation_comment("stuck", 2, "c2", pr=31)
    assert mine.startswith("<!--afk:escalation claim=c2 attempt=2-->\n**afk-fleet: escalated")
    assert d.escalation_begun(comments("chat", mine, "more chat"), "c2") == \
        {"attempt": 2, "comment_id": 101}
    # an escalation without a retry is one too: its count is 0, not missing
    assert d.escalation_begun(comments(d.escalation_comment("x", 0, "c2")), "c2") == \
        {"attempt": 0, "comment_id": 100}
    # nothing begun: no comment, one a human wrote, one from before the record existed
    assert d.escalation_begun(None, "c2") is None
    assert d.escalation_begun(comments("**afk-fleet: escalated to a human** (x)"), "c2") is None
    # an earlier escalation of the issue was of another claim — it ended in a release
    earlier = d.escalation_comment("first time", 2, "c1")
    assert d.escalation_begun(comments(earlier), "c2") is None
    assert d.escalation_begun(comments(earlier, mine), "c2")["comment_id"] == 101
    assert d.escalation_begun(comments("<!--afk:escalation claim=c2-->"), "c2") is None

    # a failure being escalated escalates, whatever the labels still say: stripped,
    # they read as attempt 0 — which would start the whole ladder over
    begun = d.escalation_begun(comments(mine), "c2")
    assert d.next_attempt(0, 2, escalation=begun) == {"action": "escalate", "attempt": 2}
    assert d.next_attempt(1, 2, counted=True, escalation=begun)["action"] == "escalate"
    assert d.next_attempt(0, 2, escalation=None)["action"] == "retry"


def test_a_hand_edited_attempt_label_costs_no_edit_and_no_attempt():
    """`afk-attempt/<n>` is the fleet's to write, but a human can: a count spelled
    another way, or a label under the prefix that is no count at all."""
    starting = d.ATTEMPT_STARTING
    # neither is an attempt (`fleet_number`): the issue reads as never retried
    assert d.current_attempt(["afk-attempt/01"]) == 0
    assert d.current_attempt(["afk-attempt/²", "afk-attempt/x"]) == 0     # a digit, not a number
    # the odd spellings go out in the edit that writes the first count
    step = d.next_attempt(d.current_attempt(["afk-attempt/01", "afk-attempt/x"]), 2)
    assert step == {"action": "retry", "attempt": 1, "to_label": "afk-attempt/1"}
    assert d.retry_labels(["afk-attempt/01", "afk-attempt/x"], step["to_label"]) == \
        (["afk-attempt/1", starting], ["afk-attempt/01", "afk-attempt/x"])
    # the same failure again: the attempt it already made, and no edit — the count is
    # the number `to_label` says, whatever sits beside it
    for labels in (["afk-attempt/1", starting], ["afk-attempt/1", starting, "afk-attempt/x"]):
        again = d.next_attempt(d.current_attempt(labels), 2, counted=d.attempt_starting(labels))
        assert (again["action"], again["attempt"]) == ("retry", 1), labels
        assert d.retry_labels(labels, again["to_label"]) == ([], []), labels
    # an escalation strips them all in its one edit
    cfg = d.resolve_config({})
    assert d.escalation_labels(["afk-attempt/01", "afk-attempt/x", starting], cfg)[1] == \
        ["afk-attempt/01", starting, "afk-attempt/x"]


# Text that is not a number the fleet wrote, though a digit test or `int()` takes
# each for one: too long (19 digits, and past the length `int()` itself refuses),
# spelled with leading zeros, in digits outside ASCII, or dressed as a number.
NOT_FLEET_NUMBERS = ["9" * 19, "1" + "0" * 200, "7" * 4301, "007", "00", "٧", "١٢", "７", "²", "1²",
                     "৩", "+7", "-7", "1_0", "1.5", "1e3", "0x7", "x", "7x"]


def test_a_number_is_one_only_as_the_fleet_writes_it():
    rng = random.Random(108)
    for n in [0, 1, 7, 10, 10 ** 17, 10 ** 18 - 1, *(rng.randrange(10 ** rng.randint(1, 18))
                                                    for _ in range(200))]:
        assert d.fleet_number(str(n)) == n
    for junk in [*NOT_FLEET_NUMBERS, "", " 7", "7 ", "7\n", "\n7"]:
        assert d.fleet_number(junk) is None, junk
    # any text at all is answered, never raised on
    alphabet = "0123456789٧７²৩+-_ .ex\n"
    for _ in range(500):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 24)))
        n = d.fleet_number(text)
        assert n is None or str(n) == text, text


def test_no_number_from_outside_the_fleet_takes_a_record_reader_down():
    """A comment, a commit subject and a label are anyone's to write. A number
    the fleet did not write is a field that is missing — which is no record when
    the kind requires it — and never an exception or another number's alias."""
    for junk in NOT_FLEET_NUMBERS:
        # on a ref: required → not a record; optional → absent
        assert d.read_record(d.HEARTBEAT_RECORD, f"afk-heartbeat instance=a ts={junk}") is None, junk
        assert d.read_record(d.CLAIM_RECORD, f"afk-claim instance=a host=h ts={junk}") == \
            {"instance": "a", "host": "h"}, junk
        # in a comment, and the valid record beside it is still the record — whichever came last
        bad = {"id": 2, "body": f"<!--afk:escalation claim=c2 attempt={junk}-->"}
        good = {"id": 3, "body": "<!--afk:escalation claim=c1 attempt=2-->"}
        assert d.read_marker(d.ESCALATION_RECORD, bad["body"]) is None, junk
        for comments in ([good, bad], [bad, good]):
            assert d.latest_record(d.ESCALATION_RECORD, comments) == \
                ({"claim": "c1", "attempt": 2}, good), junk
        assert d.read_marker(d.VERDICT_RECORD, f"<!--afk:verdict n={junk} phase=blocked-->") == \
            {"phase": "blocked"}, junk
        # in a list: dropped, and the numbers around it kept
        assert d.read_marker(d.VERDICT_RECORD, f"<!--afk:verdict n=5 blocked_by=3,{junk},4-->") == \
            {"n": 5, "blocked_by": [3, 4]}, junk
        assert "blocked_by" not in d.read_marker(d.VERDICT_RECORD,
                                                 f"<!--afk:verdict n=5 blocked_by={junk}-->"), junk
        # a batch's members: a pair with such a number on either side is no member
        turn = d.read_marker(d.TURN_RECORD, "<!--afk:turn instance=a at=1 batch=a-1 "
                                            f"members=1:10,{junk}:20,3:{junk},4:40-->")
        assert turn["members"] == [{"issue": 1, "pr": 10}, {"issue": 4, "pr": 40}], junk
        # an attempt label: not an attempt
        assert d.current_attempt([f"afk-attempt/{junk}"]) == 0, junk
        assert d.current_attempt([f"afk-attempt/{junk}", "afk-attempt/2"]) == 2, junk
        # a commit subject and a PR body: no PR stacked, no issue closed
        assert d.stacked_pr("p1 p2", f"feature (#{junk})") is None, junk
    for junk in ("9" * 19, "7" * 4301, "٧", "１２", "²"):
        assert d.issues_closed_by(f"Closes #{junk}", None, "o/r") == [], junk


def test_render_status_board():
    # ci_failed: PR opened, gate red, retrying — the two happy steps ticked, the
    # rest open, and the current-line names the attempt count.
    body = d.render_status_board("ci_failed", "required", 2, instance="fl-abc", pr=123, attempt=2)
    assert body.startswith(d.STATUS_MARKER)          # marker leads → find-or-create anchor
    assert "认领方 `fl-abc`" in body
    assert "- [x] 已认领 · worker 实现中" in body
    assert "- [x] PR 已开 (#123) · 等 CI" in body
    assert "- [ ] 轮到落地 · worker 同步、过门、合并" in body
    assert "- [ ] 已合并" in body
    assert "CI 失败,修复重试中(2/2)" in body
    # retry_max is the caller's (config `retry`), never a number of the renderer's own
    assert "(1/7)" in d.render_status_board("ci_failed", "required", 7, attempt=1)

    # claimed: the invisible phase this whole feature exists to surface.
    claimed = d.render_status_board("claimed", "required", 2, instance="x")
    assert claimed.count("- [x]") == 1 and "尚无 PR" in claimed
    assert "认领方" not in d.render_status_board("claimed", "required", 2)   # no instance, no header tail

    # merged: every step ticked, none open.
    merged = d.render_status_board("merged", "required", 2, pr=7)
    assert merged.count("- [x]") == 4 and "- [ ]" not in merged
    assert "已合并,完成" in merged

    # escalated: terminal give-up — only what truly happened stays ticked.
    esc = d.render_status_board("escalated", "required", 2, pr=9)
    assert esc.count("- [x]") == 2 and "已升级给人处理" in esc      # 认领 + PR
    assert d.render_status_board("escalated", "required", 2).count("- [x]") == 1  # no PR → only 认领

    # closed: the worker found the base already satisfies the issue — no PR ever
    # existed, so nothing past 认领 is ticked and the line says why it is closed
    closed = d.render_status_board("closed", "required", 2, instance="x")
    assert closed.count("- [x]") == 1 and "无需改动" in closed and "已关闭" in closed

    # parked: the claim is released until the blockers close — nothing is in
    # progress, so nothing is ticked, and the line names what it waits on
    parked = d.render_status_board("parked", "required", 2, blocked_by=[135, 140])
    assert "- [x]" not in parked and "等待依赖 #135、#140 关闭" in parked
    assert "认领方" not in parked and "无需人工处理" in parked
    # the key the cycle state keeps for a board is of its body, whitespace aside
    assert d.board_key(parked) == d.board_key(parked + "\n") != d.board_key(
        d.render_status_board("parked", "required", 2, blocked_by=[135]))
    assert len(d.board_key(parked)) == 8

    # awaiting_turn: the PR is open and ready; the line says what it waits for
    waiting = d.render_status_board("awaiting_turn", "required", 2, instance="x", pr=9)
    assert waiting.count("- [x]") == 2 and "- [x] PR 已开 (#9)" in waiting
    assert "排队等落地轮次" in waiting and "失败" not in waiting
    # landing: the turn is its own step, ticked while the worker lands the PR
    landing = d.render_status_board("landing", "local", 2, instance="x", pr=9)
    assert landing.count("- [x]") == 3 and "- [x] 轮到落地" in landing and "- [ ] 已合并" in landing
    assert "已轮到落地" in landing and "worker" in landing

    # gate.ci: local — the board names the gate actually being waited on, and a
    # failed one is not blamed on a CI the fleet never read (ADR-0012)
    local = d.render_status_board("pr_open", "local", 2, pr=5)
    assert "- [x] PR 已开 (#5) · 等 本地门" in local and "▸ 当前:等 本地门" in local
    assert "CI" not in local
    assert "本地门 失败,修复重试中(1/2)" in d.render_status_board("ci_failed", "local", 2, pr=5, attempt=1)
    assert "- [x] PR 已开 · 等 CI" in d.render_status_board("pr_open", "required", 2)   # no PR number

    # determinism: identical state → identical body (write-only-on-change relies on it).
    assert d.render_status_board("pr_open", "required", 2, pr=5) == \
        d.render_status_board("pr_open", "required", 2, pr=5)
    # every phase renders under every gate mode
    for phase in d.STATUS_PHASES:
        for ci in d.GATE_CI_MODES:
            assert d.render_status_board(phase, ci, 2).startswith(d.STATUS_MARKER)

    # an unknown phase or gate mode is rejected, not silently rendered as the default
    for bad in (("bogus", "required", 2), ("claimed", None, 2), ("claimed", "optional", 2)):
        try:
            d.render_status_board(*bad)
            assert False, f"expected an error for {bad}"
        except (ValueError, KeyError):
            pass


def test_pace():
    # did work → busy
    assert d.pace(True, 0, 0) == 90
    # in-flight → busy, however long the streak
    assert d.pace(False, 2, 9) == 90
    # idle but recently active (streak < threshold) → stay busy for stragglers
    assert d.pace(False, 0, 2) == 90
    # idle past threshold → idle interval
    assert d.pace(False, 0, 3) == 1500
    # the sleep while a claim is held can never let its lease lapse: the heartbeat
    # a skipped cycle sends is due well inside it (ADR-0003)
    assert d.BUSY_INTERVAL_SECONDS <= d.CLAIM_LEASE_TTL_SECONDS // 2
    assert d.CLAIM_LEASE_TTL_SECONDS == 3 * d.IDLE_INTERVAL_SECONDS


FACTS = {"instance": "fl-1", "worker_command": "ckimi --yolo"}


def test_cycle_state_is_validated_not_guessed():
    # the first cycle is handed the run's two facts, and the state carries them from then on
    first = d.cycle_state(None, **FACTS)
    assert first == {**d.CYCLE_START, **FACTS} == d.cycle_state("", **FACTS)
    assert d.cycle_state(first) == first                      # …so a later cycle passes neither
    assert d.cycle_state(first, **FACTS) == first             # the same ones again are harmless
    st = {"fingerprint": "abc", "skips": 2, "empty_streak": 0, "in_flight": 1,
          "frontier_remaining": 4, "unsettled": False, "boards": {"7": "0a1b2c3d"}, **FACTS}
    assert d.cycle_state(st) == st
    assert d.cycle_state(None, **FACTS)["boards"] is not d.CYCLE_START["boards"]   # never shared
    # a caller that mangled the state must hear so — run on zeros, a fleet holding
    # claims would be paced as if it held none; without the facts it could start no worker
    no_facts = {k: v for k, v in st.items() if k not in FACTS}
    for bad in ({"fingerprint": "abc"}, {**st, "extra": 1}, [], "abc", {**st, "skips": "x"},
                no_facts, {**no_facts, "instance": "fl-1"}, {**st, "worker_command": ""},
                {**st, "instance": None},
                # a field of the wrong type — never coerced into one of the right type
                {**st, "skips": "2"}, {**st, "skips": [2]}, {**st, "skips": 2.0},
                {**st, "skips": True}, {**st, "in_flight": None}, {**st, "empty_streak": {}},
                {**st, "unsettled": 0}, {**st, "fingerprint": None}, {**st, "boards": []},
                {**st, "boards": {"7": 1}},
                # a count no cycle leaves: negative (it would put the forced tick
                # off by that many cycles), or at the forced tick and past it
                {**st, "skips": -39}, {**st, "in_flight": -1}, {**st, "frontier_remaining": -1},
                {**st, "empty_streak": -1}, {**st, "skips": d.FORCE_TICK_AFTER_SKIPS},
                # counts that contradict each other
                {**st, "unsettled": True}, {**st, "empty_streak": 1},
                {**st, "skips": 0, "in_flight": 0, "empty_streak": 1},
                {**st, "skips": 0, "in_flight": 0, "frontier_remaining": 0, "empty_streak": 1,
                 "unsettled": True}):
        try:
            d.cycle_state(bad)
            assert False, f"expected ValueError for {bad!r}"
        except ValueError:
            pass
    assert d.cycle_state({**st, "skips": 0, "unsettled": True})["unsettled"] is True
    assert d.cycle_state({**st, "skips": 0, "in_flight": 0, "frontier_remaining": 0,
                          "empty_streak": 9})["empty_streak"] == 9
    # a first cycle without them, and a fact that disagrees with the state's
    for raw, given in ((None, {}), (None, {"instance": "fl-1"}), ("", {"worker_command": "x"}),
                       (st, {"instance": "fl-2"}), (st, {"worker_command": "claude"})):
        try:
            d.cycle_state(raw, **given)
            assert False, f"expected ValueError for {given!r}"
        except ValueError as e:
            assert "--instance" in str(e) or "--worker-command" in str(e)


def _did(**did):
    return {**{k: [] for k in d.TICK_DID}, "in_flight": 0, "frontier_remaining": 0, **did}


def test_cycle_ticked_folds_what_the_tick_did_and_counts_empty_ticks():
    st = d.cycle_state(None, **FACTS)

    def ticked(state, **did):
        return d.cycle_ticked(state, _did(**did))

    # an EMPTY tick: nothing done, nothing in flight, nothing left to dispatch
    r = ticked(st)
    assert r["state"]["empty_streak"] == 1 and r["sleep_seconds"] == 90
    assert r["progress"] == "0 in flight, 0 left on the frontier"
    r = ticked(r["state"])
    r = ticked(r["state"])
    assert r["state"]["empty_streak"] == 3 and r["sleep_seconds"] == 1500     # idle at last
    # anything that is not empty resets the streak — work done, a claim held, or
    # frontier the tick could not take (e.g. no free slot)
    for did in ({"granted": [3]}, {"escalated": [4]}, {"dispatched": [1]}, {"reclaimed": [6]},
                {"parked": [5]}, {"cleared": [7]},
                {"in_flight": 2}, {"frontier_remaining": 5}):
        back = ticked(r["state"], **did)
        assert back["state"]["empty_streak"] == 0 and back["sleep_seconds"] == 90, did
    # a nudge or a retry is told to the human but is not what keeps the fleet busy:
    # the claim it is about is (pacing is what it was when a tick summarised itself)
    assert ticked(r["state"], nudged=[4])["sleep_seconds"] == 1500
    # what the next skipped cycle paces and beats on is carried in the state
    held = ticked(st, in_flight=2, frontier_remaining=7)["state"]
    assert (held["in_flight"], held["frontier_remaining"]) == (2, 7)
    kept = ticked({**st, "fingerprint": "abc", "skips": 4})["state"]
    assert kept["fingerprint"] == "abc" and {k: kept[k] for k in FACTS} == FACTS
    # the digest the state keeps is of the fleet as the tick LEFT it, so what the
    # tick itself wrote is not a change next cycle; with it, the board each claim
    # still held was left with
    left = d.cycle_ticked({**st, "fingerprint": "abc", "boards": {"9": "old"}}, _did(in_flight=1),
                          left="def", boards={4: "0a1b2c3d"})["state"]
    assert (left["fingerprint"], left["boards"]) == ("def", {"4": "0a1b2c3d"})
    assert d.cycle_state(left) == left
    assert ticked({**st, "boards": {"9": "old"}})["state"]["boards"] == {"9": "old"}

    # the progress line: every list the tick filled, then where the fleet stands
    line = ticked(st, cleared=[1], granted=[2], dispatched=[3, 4], retried=[5], nudged=[6],
                  in_flight=4, frontier_remaining=1)["progress"]
    assert line == ("landing turn to #2; dispatched #3, #4; cleared #1; retried #5; nudged #6; "
                    "4 in flight, 1 left on the frontier")

    # a tick that returned a judgment sleeps 0 — the caller answers and opens the
    # next cycle at once — and one that left a judgment or an error is unsettled:
    # never empty, and the next cycle ticks whatever the digest says
    asked = d.cycle_ticked(r["state"], _did(), judgments=2)
    assert (asked["sleep_seconds"], asked["state"]["unsettled"]) == (0, True)
    assert asked["state"]["empty_streak"] == 0 and "2 judgments open" in asked["progress"]
    failed = d.cycle_ticked(r["state"], _did(), errors=1)
    assert (failed["sleep_seconds"], failed["state"]["unsettled"]) == (90, True)
    # a PR that opened while the tick ran is in the digest it keeps, unseen: the
    # next cycle is opened at once, and ticks
    missed = d.cycle_ticked(r["state"], _did(in_flight=2), unseen=1)
    assert (missed["sleep_seconds"], missed["state"]["unsettled"]) == (0, True)
    assert missed["progress"] == "1 PR opened meanwhile; 2 in flight, 0 left on the frontier"
    assert "1 error;" in failed["progress"]
    assert ticked(asked["state"])["state"]["unsettled"] is False     # a clean tick settles it


def test_cycle_drained_folds_the_stop_and_schedules_nothing():
    st = d.cycle_ticked(d.cycle_state(None, **FACTS), _did(in_flight=3))["state"]
    r = d.cycle_drained(st, [1, 2], [7])
    assert r["progress"] == "drained; released #1, #2; kept #7"
    assert r["sleep_seconds"] is None                          # no cycle follows a drain
    assert (r["state"]["in_flight"], r["state"]["unsettled"]) == (1, False)
    assert d.cycle_state(r["state"]) == r["state"]             # still a state the code takes back
    assert d.cycle_drained(st, [], [])["progress"] == "drained"
    # a release that failed left its claim held: said, and never read as settled
    failed = d.cycle_drained(st, [1], [2, 7], errors=1)
    assert failed["progress"] == "drained; released #1; kept #2, #7; 1 error"
    assert failed["state"]["unsettled"] is True
    # claims found held by a fleet that thought itself idle (a takeover's): no longer empty
    idle = {**d.cycle_state(None, **FACTS), "fingerprint": "abc", "skips": 2, "empty_streak": 5}
    assert d.cycle_drained(idle, [], [])["state"] == idle
    kept = d.cycle_drained(idle, [], [7])["state"]
    assert (kept["empty_streak"], kept["skips"], kept["in_flight"]) == (0, 2, 1)
    failed = d.cycle_drained(idle, [], [], errors=1)["state"]
    assert (failed["empty_streak"], failed["skips"], failed["unsettled"]) == (0, 0, True)
    assert d.cycle_state(kept) == kept and d.cycle_state(failed) == failed


def test_cycle_wake_gates_beats_and_paces_a_skipped_cycle():
    st = d.cycle_state(None, **FACTS)
    first = d.cycle_wake(st, "aaa")
    assert (first["action"], first["reason"]) == ("tick", "first")
    assert first["state"]["fingerprint"] == "aaa"
    # a tick owes its sleep to cycle_ticked, not to the gate
    assert set(first) == {"action", "reason", "state"}

    # unchanged + idle fleet → skip; each such skip is itself an empty cycle
    idle = d.cycle_ticked(first["state"], _did())["state"]
    s1 = d.cycle_wake(idle, "aaa")
    assert (s1["action"], s1["reason"], s1["heartbeat"]) == ("skip", "unchanged", False)
    assert (s1["state"]["skips"], s1["state"]["empty_streak"], s1["sleep_seconds"]) == (1, 2, 90)
    assert s1["progress"] == "nothing moved; 0 in flight, 0 left on the frontier"
    s2 = d.cycle_wake(s1["state"], "aaa")
    assert (s2["state"]["empty_streak"], s2["sleep_seconds"]) == (3, 1500)

    # unchanged while HOLDING claims → skip, but beat, stay busy, and never count as empty
    held = d.cycle_ticked(first["state"], _did(in_flight=2))["state"]
    h = d.cycle_wake(held, "aaa")
    assert (h["action"], h["heartbeat"], h["sleep_seconds"]) == ("skip", True, 90)
    assert h["state"]["empty_streak"] == 0
    # frontier left over (the tick had no free slot) is not empty either
    waiting = d.cycle_ticked(first["state"], _did(frontier_remaining=3))["state"]
    assert d.cycle_wake(waiting, "aaa")["state"]["empty_streak"] == 0

    # changed → tick; the Nth consecutive skip → a forced tick
    moved = d.cycle_wake(s2["state"], "bbb")
    assert (moved["action"], moved["reason"], moved["state"]["skips"]) == ("tick", "changed", 0)
    forced = d.cycle_wake({**idle, "skips": 5}, "aaa")
    assert (forced["action"], forced["reason"], forced["state"]["skips"]) == ("tick", "forced", 0)
    # the streak survives a tick decision: only cycle_ticked resets it
    assert moved["state"]["empty_streak"] == 3

    # the last tick left a judgment open or met an error: unchanged is not a skip
    owed = d.cycle_wake({**idle, "unsettled": True, "skips": 2}, "aaa")
    assert (owed["action"], owed["reason"], owed["state"]["skips"]) == ("tick", "unsettled", 0)

    # a wake that arrived while the last cycle was running: the digest that cycle
    # kept may already hold what the wake announced, so unchanged is not a skip
    woke = d.cycle_wake({**idle, "skips": 2}, "aaa", woke=True)
    assert (woke["action"], woke["reason"], woke["state"]["skips"]) == ("tick", "wake", 0)
    assert d.cycle_wake(idle, "bbb", woke=True)["reason"] == "changed"


CALL = {"afk_path": "/skill/scripts/afk.py", "repo": "acme/widgets", "instance": "fl-1",
        "worker_command": "ckimi --yolo", "config": '{"retry": 2}'}


def _argv(command):
    """A judgment's command as the shell would split it: (subcommand, argv)."""
    argv = shlex.split(command)
    assert argv[0] == CALL["afk_path"]
    for flag, value in (("--repo", CALL["repo"]), ("--config", CALL["config"]),
                        ("--instance", CALL["instance"])):
        assert argv[argv.index(flag) + 1] == value, command
    return argv[1], argv


def _worker(cause, **more):
    """A whole `afk no-pr` row (`afk_decide.WorkerRow`) for a worker classified `cause`."""
    row = d.WORKER_CAUSES.get(cause)
    return {"cause": cause, "outcome": row and row.outcome, "action": row and row.action,
            "idle_seconds": None, "pending_blockers": [], "worktree": None, "progress": None,
            "worker_verdict": None, "blockers": [], "nudged_at": None, "turn_at": None, **more}


def _mine(n, status="no_pr", **more):
    """A whole `mine` row (`afk_decide.MineRow`), saying nothing but what is named."""
    return {"number": n, "title": None, "status": status, "board_phase": None, "pr": None,
            "checks": None, "attempt": 0, "starting": False, "stopped": None, "batch": None,
            "unbatched": None, "given_up": False, **more}


def test_a_tick_asks_after_waiting_workers_and_grants_one_turn():
    mine = [_mine(1), _mine(2, "awaiting_turn", pr=22), _mine(3, "landing", pr=21),
            _mine(4, "landing", pr=20, stopped="awaiting_ci"), _mine(5, "awaiting_ci", pr=23),
            _mine(6, "failure", pr=24), _mine(7, "closed"),
            _mine(8, "landing", pr=19, stopped="gate_red")]
    # `afk no-pr` is asked about a PR-less claim, and a landing one that did not stop for the tick
    assert d.asks_after(mine) == [1, 3, 8]
    # …and about one being fixed off the turn its PR gave up (ADR-0045) — which is
    # no turn out: the head of the queue is granted its own beside it
    fixing = _mine(9, "fixing", pr=18, stopped="conflict", given_up=True)
    assert d.asks_after([*mine, fixing]) == [1, 3, 8, 9]
    assert d.turn_due([fixing, mine[1]], d.turn_order([fixing, mine[1]])) == 2

    # the turn goes to the head of the merge queue, and only when its worker is not at it
    assert d.turn_due(mine, []) is None
    assert d.turn_due(mine, [2]) == 2                             # awaiting its turn
    assert d.turn_due(mine, [4, 2]) == 4                          # stopped for the tick: again
    assert d.turn_due(mine, [3, 2]) is None                       # landing: leave it, and #2 waits
    assert d.turn_due(mine, [8, 2]) is None                       # fixing a red gate in place


def test_turn_step_routes_every_outcome_or_returns_the_judgment():
    cfg = d.resolve_config({})
    verify = d.resolve_config({"gate": {"adversarial_verify_prompt": "be harsh"}})

    def step(outcome, config=cfg):
        return d.turn_step(CALL, {"issue": 4, "pr": 30, "head": "abc123", "outcome": outcome},
                           config)

    assert step("granted") == ("granted", None)
    for outcome in ("waiting", "landing", "fixing", "awaiting_ci"):
        assert step(outcome) == ("leave", None), outcome
    # `afk turn --restart` on a PR that gave its turn up grants none: the worker
    # it started is what is counted (ADR-0045)
    assert d.turn_step(CALL, {"issue": 4, "pr": 30, "head": "abc123", "outcome": "fixing",
                              "restarted": NOW}, cfg, restart=True) == ("granted", None)
    routed = {"granted", "waiting", "landing", "fixing", "awaiting_ci", "gate_red", "no_checks",
              "needs_verify"}
    assert routed == set(d.TURN_OUTCOMES)             # a new outcome needs a route here

    do, j = step("no_checks")
    assert (do, j["kind"], j["issue"], j.get("bulky")) == ("judge", "no_checks", 4, None)
    assert j["context"] == {"pr": 30, "head": "abc123"} and "acceptance criteria" in j["question"]
    sub, argv = _argv(j["if_yes"])
    assert sub == "turn" and "--allow-no-checks" in argv and "--verified" not in argv
    assert argv[argv.index("--worker-command") + 1] == CALL["worker_command"]
    sub, argv = _argv(j["if_no"])
    assert sub == "fail" and argv[-2] == "--reason" and "PR #30" in argv[-1]

    do, j = step("needs_verify", verify)
    assert (do, j["kind"], j["bulky"]) == ("judge", "adversarial_verify", True)
    assert j["context"]["prompt"] == "be harsh" and "abc123" in j["question"]
    sub, argv = _argv(j["if_yes"])
    assert sub == "turn" and argv[-2:] == ["--verified", "abc123"]
    assert "--allow-no-checks" not in argv
    assert _argv(j["if_no"])[0] == "fail"
    # no checks AND a verify owed: `afk turn` records neither judgment until both are
    # in, so they are asked as one — never a `no_checks` whose yes changes nothing
    do, j = step("no_checks", verify)
    assert j["kind"] == "adversarial_verify" and "only gate" in j["question"]
    assert _argv(j["if_yes"])[1][-3:] == ["--allow-no-checks", "--verified", "abc123"]

    # a restart's result (ADR-0035): a judgment's yes restarts, so an answered
    # verify or a waived check does not fall back to `landing` and change nothing
    told = d.turn_step(CALL, {"issue": 4, "pr": 30, "head": "abc123", "outcome": "needs_verify"},
                       verify, restart=True)
    assert told[0] == "judge" and _argv(told[1]["if_yes"])[1][-3:] == ["--restart", "--verified",
                                                                        "abc123"]
    told = d.turn_step(CALL, {"issue": 4, "pr": 30, "head": "abc123", "outcome": "no_checks"},
                       cfg, restart=True)
    assert _argv(told[1]["if_yes"])[1][-2:] == ["--restart", "--allow-no-checks"]
    assert "--restart" not in _argv(told[1]["if_no"])[1]
    assert d.turn_step(CALL, {"issue": 4, "pr": 30, "head": "abc123", "outcome": "granted"},
                       cfg, restart=True) == ("granted", None)

    # red checks: the transition is fixed, its wording is not — and lives in a CI log
    do, j = step("gate_red")
    assert (do, j["kind"], j["bulky"]) == ("judge", "reason", True)
    assert j["if_yes"] == j["if_no"] and _argv(j["if_yes"])[0] == "fail"
    assert _argv(j["if_yes"])[1][-2] == "--reason"
    assert d.failure_judgment(CALL, _mine(6, "failure", pr=24))["if_yes"] == \
        d.afk_command(CALL, "fail", 6, "--reason", "the checks of PR #24 are red")


def _declared(phase=None, reason=None, blocked_by=()):
    return {"found": phase is not None, "phase": phase, "blocked_by": list(blocked_by),
            "reason": reason, "comment_url": "https://gh/c/7" if phase else None}


def test_worker_step_routes_every_cause_or_returns_the_judgment():
    cfg = d.resolve_config({"base_branch": "main"})

    def step(cause, row=None, **worker):
        return d.worker_step(CALL, row or _mine(4), _worker(cause, **worker), cfg)

    for cause in ("working", "just_stopped", "within_grace", "awaiting_tick"):
        assert step(cause) == ("leave", None), cause
    # a retry cut short (its failure counted, no fresh worker started) is FINISHED,
    # whatever is left of the attempt it was discarding — never continued, nudged
    # or parked — and a worker still at work is left alone
    cut = _mine(4, starting=True)
    for cause in d.WORKER_CAUSES:
        do, reason = step(cause, cut, worker_verdict=_declared("giving-up", "no fixture"))
        if d.WORKER_CAUSES[cause][1] == "leave":
            assert do == "leave", cause
        else:
            assert do == "fail", cause
            assert ("cut short" in reason) == (d.WORKER_CAUSES[cause][1] != "next_attempt"), cause
    assert step("blockers_waiting") == ("park", None) and step("silent") == ("nudge", None)
    # a landing worker silent after its nudge is RESTARTED onto its turn — `afk turn
    # --restart` — not failed: nothing is closed, deleted or counted (ADR-0035)
    assert step("silent_on_turn", _mine(4, "landing", pr=30)) == ("restart", None)
    # …and silent again past that one restart it is ESCALATED with its PR, branch
    # and worktree kept, the reason saying the PR was ready and the landing was
    # restarted once (#91); a landing row's silence has NO failure route at all
    do, reason = step("silent_past_restart", _mine(4, "landing", pr=30, stopped="conflict"))
    assert (do, reason) == ("escalate", _KEPT.format(pr=30, stopped="conflict"))
    # a worker fixing OFF the turn its PR gave up climbs the same two rungs, and
    # its silence has no failure route either (ADR-0045)
    fixing = _mine(4, "fixing", pr=30, stopped="gate_red", given_up=True)
    assert step("silent_on_turn", fixing) == ("restart", None)
    do, reason = step("silent_past_restart", fixing)
    assert do == "escalate" and "gave it up" in reason and "`gate_red`" in reason
    assert "kept as they are" in reason
    for cause in ("silent_after_nudge", "silent_unnudgeable"):
        try:
            step(cause, fixing)
        except ValueError as e:
            assert "never a failure" in str(e)
        else:
            raise AssertionError("a fixing claim's silence reached `afk fail`")
    for cause in ("silent_after_nudge", "silent_unnudgeable"):
        try:
            step(cause, _mine(4, "landing", pr=30))
        except ValueError as e:
            assert "never a failure" in str(e), cause
        else:
            raise AssertionError(f"a landing claim's {cause} was routed")
    # a claim with no worker left is CONTINUED, with no judgment asked: an unattended
    # run never releases it back to the frontier
    assert step("gone") == step("blockers_closed") == ("dispatch", None)

    # already-satisfied: whether the diff really is empty stays a judgment
    do, j = step("satisfied", worktree="/wt/4", worker_verdict=_declared("already-satisfied"))
    assert (do, j["kind"], j.get("bulky")) == ("judge", "empty_diff", None)
    assert j["context"] == {"worktree": "/wt/4", "base_branch": "main", "verdict": "https://gh/c/7"}
    sub, argv = _argv(j["if_yes"])
    assert sub == "close" and "--worker-command" not in argv
    assert _argv(j["if_no"])[0] == "fail"

    # an escalation or a failure whose reason is NOT on record is asked for
    do, j = step("no_blocker_named", blockers=[], worker_verdict=_declared("blocked"))
    assert (do, j["kind"], j.get("bulky")) == ("judge", "reason", None)
    assert j["if_yes"] == j["if_no"] and _argv(j["if_yes"])[0] == "escalate"
    assert j["context"]["verdict"] == "https://gh/c/7"
    do, j = step("gave_up", worker_verdict=_declared("giving-up"))
    assert (do, j["kind"]) == ("judge", "reason") and _argv(j["if_yes"])[0] == "fail"

    # every cause the classification can name has a route, and one it cannot is an
    # error — never a reason worded for some other cause
    for cause in d.WORKER_CAUSES:
        assert step(cause, worker_verdict=_declared())[0] in \
            ("leave", "dispatch", "park", "nudge", "restart", "escalate", "fail", "judge"), cause
    for route in (lambda: step("on-holiday"), lambda: d.batch_step({"cause": "gave_up"})):
        try:
            route()
        except ValueError as e:
            assert "classified" in str(e)
        else:
            raise AssertionError("a cause with no route was routed")
    assert {c for c, row in d.WORKER_CAUSES.items() if row.batch_step is None} == {
        "satisfied", "satisfied_refuted", "blockers_closed", "blockers_waiting", "blocker_unmet",
        "no_blocker_named", "gave_up", "needs_decision",        # a batch worker declares nothing
        "unknown_phase",
        "silent_on_turn", "silent_past_restart"}                # …and is abandoned, never restarted


# Every failure and escalation reason a tick words by itself, pinned to the cause
# it is worded for: (cause, what was gathered, the claim's status → the reason).
_UNMET = [{"number": 9, "standing": "unmet", "reason": "was closed as not planned"},
          {"number": 8, "standing": "waiting", "reason": None}]
# the reason a landing turn is escalated with past its one restart (#91)
_KEPT = ("PR #{pr} was judged ready and given the landing turn, its worker was restarted onto "
         "the turn once, and the landing still did not happen: {stopped}. The PR, its branch "
         "and its worktree are kept as they are")
_GONE_QUIET = {"progress": ZERO, "idle": 9000}
# the reason a `needs-decision` verdict is escalated with (ADR-0041)
_ASKS = ("its worker found that the issue as written needs a decision from its owner{asks}. A "
         "retry would stop at the same question, so none was spent — the decision to make is "
         "in the worker's comment: https://gh/c/7")
REASONS = (
    ("blocker_unmet", "escalate",
     {"verdict": _declared("blocked", blocked_by=[9, 8]), "blockers": _UNMET}, "no_pr",
     "blocked by a dependency nothing will resolve: #9 was closed as not planned"),
    ("no_blocker_named", "escalate",
     {"verdict": _declared("blocked", reason="no design yet")}, "no_pr",
     "its worker reported blocked, naming no blocker: no design yet"),
    ("satisfied_refuted", "fail",
     {"verdict": _declared("already-satisfied"), "progress": {**ZERO, "commits_ahead": 2}}, "no_pr",
     "its worker declared `already-satisfied`, but the branch holds changes"),
    ("gave_up", "fail", {"verdict": _declared("giving-up", reason="flaky build")}, "no_pr",
     "its worker gave up: flaky build"),
    # a decision only the issue's owner can make is escalated with the worker's own
    # words and a pointer at its comment — and is on record with no `reason=` at all
    ("needs_decision", "escalate",
     {"verdict": _declared("needs-decision", reason="split or keep in Core")}, "no_pr",
     _ASKS.format(asks=": split or keep in Core")),
    ("needs_decision", "escalate", {"verdict": _declared("needs-decision")}, "no_pr",
     _ASKS.format(asks="")),
    ("unknown_phase", "fail", {"verdict": _declared("on-holiday")}, "no_pr",
     "its worker's verdict names no phase the fleet knows ('on-holiday')"),
    ("unknown_phase", "fail", {"verdict": {**_declared("x"), "phase": None}}, "no_pr",
     "its worker's verdict names no phase the fleet knows (None)"),
    ("silent_after_nudge", "fail", {"nudged_at": NOW - GRACE}, "no_pr",
     "idle with no PR and no verdict a grace period after its nudge"),
    ("silent_unnudgeable", "fail", {"can_nudge": False}, "no_pr",
     "idle with no PR and no verdict, and no worktree here to nudge it in"),
    # a landing worker silent after its nudge is restarted once (ADR-0035), and
    # silent again past that restart it is ESCALATED with its PR, branch and
    # worktree kept — the only end a landing claim's silence has, and never `afk
    # fail` (#91); with no worktree here to nudge in, the same rung at once
    ("silent_past_restart", "escalate",
     {"nudged_at": NOW - GRACE, "stopped": "conflict", "restarted": NOW - 2 * GRACE}, "landing",
     _KEPT.format(pr=30, stopped="conflict")),
    ("silent_past_restart", "escalate", {"can_nudge": False, "restarted": NOW - 2 * GRACE},
     "landing", _KEPT.format(pr=30, stopped="no `afk land` outcome")),
    # what the worker declared still words the failure of a claim that holds the turn
    ("gave_up", "fail", {"verdict": _declared("giving-up", reason="flaky build")}, "landing",
     "its worker gave up: flaky build"),
)


def test_every_reason_a_tick_words_is_pinned_to_the_cause_it_is_worded_for():
    """The classification names the cause once; `worker_step` maps it to the
    reason and re-derives nothing — so the text below is reached from the raw
    signals through the cause in the first column, and through no other."""
    cfg = d.resolve_config({})
    for cause, do, got, status, reason in REASONS:
        verdict, blockers = got.get("verdict", _declared()), got.get("blockers", [])
        turn = d.next_turn(None, at=NOW - 9000, stopped=got.get("stopped"),
                           restarted=got.get("restarted")) if status == "landing" else None
        seen = d.classify_stopped(got.get("progress", ZERO), 9000, verdict,
                                  {b["number"]: b["standing"] for b in blockers}, NOW, GRACE,
                                  nudged_at=got.get("nudged_at"),
                                  can_nudge=got.get("can_nudge", True), turn=turn)
        assert seen["cause"] == cause, (seen, reason)
        row = _mine(4, status, pr=30, stopped=got.get("stopped")) if status == "landing" else _mine(4)
        worker = {**_worker(seen["cause"]), **seen, "worker_verdict": verdict,
                  "blockers": blockers, "nudged_at": got.get("nudged_at")}
        assert d.worker_step(CALL, row, worker, cfg) == (do, reason), cause
        # the cause alone carries the decision: with the row's words for it
        # scrambled, the reason is the same
        assert d.worker_step(CALL, row, {**worker, "outcome": "coding", "action": "leave"},
                             cfg) == (do, reason), cause
    # every cause that ends in a failure or an escalation is pinned above
    assert {c for c, row in d.WORKER_CAUSES.items()
            if row.step in ("fail", "escalate")} == {r[0] for r in REASONS}


def test_a_worker_state_settles_a_busy_or_gone_worker_and_nothing_else():
    """ADR-0021: busy, gone, or stopped within grace is decided from the reading
    alone — the one decision both `afk no-pr` callers ask before gathering."""
    def settled(row, nudged_at=None):
        seen = d.settled_by_worker_state(d.read_worker_state(row, NOW, GRACE), NOW, GRACE, nudged_at)
        return seen and (seen["cause"], seen["outcome"], seen["action"], seen["idle_seconds"])

    assert settled(_ps("working", 900, 5)) == ("working", "coding", "leave", 5)
    assert settled(None) == ("gone", "dead", "orphan", None)
    assert settled(_ps("working", 1, 1, terminals=0), NOW - 10) == ("gone", "dead", "orphan", 10)
    assert settled(_ps("done", 10, 10)) == ("just_stopped", "coding", "leave", 10)
    assert settled(_ps("done", 9000, 1), NOW - 10) == ("just_stopped", "coding", "leave", 10)
    # stopped past grace: not settled — its reasons are gathered, then classified
    assert settled(_ps("done", GRACE, 1)) is None
    assert settled(_ps("working", 900, GRACE)) is None                # a lost stop report
    assert settled(_ps("done", 9000, 1), NOW - GRACE) is None
    # every cause comes to a pair `afk no-pr` may print
    assert {(row.outcome, row.action) for row in d.WORKER_CAUSES.values()} == set(d.NO_PR_ROUTES)


def test_checks_gate_and_gate_comment():
    # required mode: only green on the head that lands merges
    assert d.checks_gate("green") == "green"
    assert d.checks_gate("red") == "gate_red"
    assert d.checks_gate("pending") == "awaiting_ci"
    # no checks at all is the tick's judgment, never a default
    assert d.checks_gate(None) == "no_checks"
    assert d.checks_gate(None, allow_no_checks=True) == "green"
    assert d.checks_gate("red", allow_no_checks=True) == "gate_red"   # not a bypass

    # a landing waits for the checks of the head that would land (ADR-0027): while
    # GitHub still shows the head before its push, whatever those checks say…
    for state in ("green", "red", "pending", None):
        assert d.checks_owed(state, at_head=False, had_checks=True) is True
    # …while one is running, and while a head just pushed shows none although the
    # PR had them — they are not registered yet, which is not a repo with no CI
    assert d.checks_owed("pending", at_head=True, had_checks=True) is True
    assert d.checks_owed(None, at_head=True, had_checks=True) is True
    # a verdict ends the wait, and so does a PR that never had a check
    assert d.checks_owed("green", at_head=True, had_checks=True) is False
    assert d.checks_owed("red", at_head=True, had_checks=True) is False
    assert d.checks_owed(None, at_head=True, had_checks=False) is False

    red = d.gate_verdict(7, "\n".join(f"line {i}" for i in range(50)), max_lines=3)
    body = d.gate_comment(red, "make test")
    assert "`make test`" in body and "exit 7" in body and "line 49" in body
    assert "47 earlier line(s) omitted" in body and "line 1\n" not in body
    hung = d.gate_comment(d.gate_verdict(124, "x", timed_out=True), "make test")
    assert "timed out" in hung and "omitted" not in hung


def test_find_orca_repo_and_worktree_name():
    ident = lambda key: {"gitRemoteIdentity": {"canonicalKey": key}}
    repos = [{"id": "r-other", "path": "/o", **ident("github.com/acme/other")},
             {"id": "r-folder", "path": "/f", "kind": "folder"},         # no git identity at all
             {"id": "", "path": "/x", **ident("github.com/acme/widgets")},
             {"id": "r-1", "path": "/src/widgets", "displayName": "w", **ident("github.com/Acme/Widgets")}]
    # case-insensitive, as GitHub is; only the two fields a caller needs
    assert d.find_orca_repo(repos, "acme/widgets") == {"id": "r-1", "path": "/src/widgets"}
    assert d.find_orca_repo(repos, "acme/widget") is None             # never a prefix match
    assert d.find_orca_repo(None, "acme/widgets") is None

    name = d.worktree_name(31, "Fix the  Names inspector: tab (v2)!")
    assert name == "issue-31-fix-the-names-inspector-tab-v2"
    assert d.worktree_name(4, "中文标题") == "issue-4-work"
    assert d.worktree_name(4, None) == "issue-4-work"
    long = d.worktree_name(4, "word " * 40)
    assert len(long) <= len("issue-4-") + 40 and not long.endswith("-")


def test_closing_pr_and_superseded_prs():
    prs = [{"number": 30, "headRefName": "sunfmin/issue-3-x",
            "closingIssuesReferences": [{"number": 3}]},
           {"number": 31, "headRefName": "sunfmin/issue-3-x-2",
            "closingIssuesReferences": [{"number": 3}]},
           {"number": 32, "headRefName": "alice/hotfix",                 # a human's PR
            "closingIssuesReferences": [{"number": 3}, {"number": 4}]},
           {"number": 33, "headRefName": "sunfmin/issue-3-y", "closingIssuesReferences": []},
           {"number": 34, "headRefName": "sunfmin/issue-30-x",
            "closingIssuesReferences": [{"number": 30}]}]
    assert d.closing_pr(prs, 3)["number"] == 32                          # the latest closes it
    assert d.closing_pr(prs, 4)["number"] == 32 and d.closing_pr(prs, 99) is None
    # a fresh start closes only what the FLEET opened for THIS issue: from a branch
    # that is the fleet's own AND closing the issue — never a human's PR, never
    # issue 30's, and not #31 either while its fleet-shaped branch is not the fleet's
    own = ["sunfmin/issue-3-x", "sunfmin/issue-3-y", "sunfmin/issue-30-x", None]
    assert [p["number"] for p in d.superseded_prs(prs, 3, own)] == [30]
    assert [p["number"] for p in d.superseded_prs(prs, 3, [*own, "sunfmin/issue-3-x-2"])] == [30, 31]
    assert d.superseded_prs(prs, 3, []) == [] and d.superseded_prs(prs, 4, own) == []
    assert d.superseded_prs(None, 3, own) == []


# --------------------------------------------------------------------------- #
# The tick's plan — the order of a tick, its slots and its figures, no process #
# --------------------------------------------------------------------------- #

class _Raises(str):
    """A scripted answer: the step raised, saying this."""


_row = _mine


def _working_set(mine=(), frontier=(), stale=(), stale_closed=(), batches=()):
    mine = list(mine)
    return {"mine": mine, "merge_order": d.turn_order(mine), "batches": list(batches),
            "frontier": {"dispatch": [{"number": n, "title": f"issue {n}"} for n in frontier]},
            "stale": [{"number": n, "instance": "dead", "sha": f"sha{n}"} for n in stale],
            "stale_closed": [{"number": n, "instance": "dead", "sha": f"sha{n}"}
                             for n in stale_closed]}


def _batch(members, instance="fl-1", batch="b1"):
    return {"id": batch, "instance": instance, "phase": "stacking", "at": NOW,
            "members": [{"issue": n, "pr": n * 10} for n in members]}


def _play(ws, answers=None, causes=None, config=None, concurrency=3):
    """Carry a tick's plan out against a scripted world — the way `afk._tick`
    does, with no process → (the steps it handed out, what it returned).

      answers: {(do, issue or batch) | do: what that step's transition returns,
                or `_Raises`}; a step with no answer here succeeds
      causes:  {issue or batch id: the cause `afk no-pr` classifies its worker with}
      concurrency: the config's, where no whole `config` is given
    """
    answers, causes, steps = answers or {}, causes or {}, []
    stock = {"begin": d.BEGUN, "reclaim": {"won": True}, "fail": {"action": "retry"},
             "escalate": {"action": "escalate"}}

    def said(do, key):
        answer = answers.get((do, key), answers.get(do, stock.get(do, {"ok": True})))
        return (None, str(answer)) if isinstance(answer, _Raises) else (answer, None)

    def carry_out(step):
        steps.append(step)
        do = step["do"]
        assert do in d.TICK_STEPS, step
        if do == "finish":
            return [said(do, n) for n in step["issues"]], None
        key = step.get("issue", step.get("batch"))
        if do == "no-pr" and ("no-pr", key) not in answers and "no-pr" not in answers:
            asked = step.get("issues") or [step["batch"]]
            return {"workers": [_worker(causes.get(n, "working"), issue=n)
                                for n in asked]}, None
        if do == "turn" and ("turn", key) not in answers and "turn" not in answers:
            return {"issue": key, "pr": key * 10, "head": "abc", "outcome": "granted"}, None
        return said(do, key)

    plan = d.tick_plan(ws, CALL, config or d.resolve_config({"concurrency": concurrency}))
    return steps, d.follow(plan, carry_out)


def _brief(steps):
    """The steps as (do, the issue / issues / batch it is about)."""
    return [(s["do"], *(s[k] for k in ("issue", "issues", "batch") if k in s)) for s in steps]


def _nothing_done(**did):
    return {**{k: [] for k in d.TICK_DID}, "in_flight": 0, "frontier_remaining": 0, **did}


def test_a_tick_fills_its_free_slots_at_once_and_reports_what_it_holds():
    # three free slots, five ready issues: three starts begun in frontier order,
    # then ALL of them finished in one step — and nothing begun past the slots
    steps, done = _play(_working_set(frontier=[1, 2, 3, 4, 5]))
    assert _brief(steps) == [("begin", 1), ("begin", 2), ("begin", 3), ("finish", [1, 2, 3]),
                             ("heartbeat",)]
    assert done == {"did": _nothing_done(dispatched=[1, 2, 3], in_flight=3, frontier_remaining=2),
                    "judgments": [], "errors": [], "held": {1, 2, 3}}
    # a claim already held takes its slot: two held, one free
    steps, done = _play(_working_set(mine=[_row(8), _row(9)], frontier=[1, 2]))
    assert _brief(steps) == [("no-pr", [8, 9]), ("begin", 1), ("finish", [1]), ("heartbeat",)]
    assert (done["did"]["in_flight"], done["did"]["frontier_remaining"]) == (3, 1)
    # nothing to do, nothing held: not one step, and no lease to refresh
    assert _play(_working_set()) == ([], {"did": _nothing_done(), "judgments": [], "errors": [],
                                          "held": set()})


def test_a_start_that_fails_to_begin_ends_the_starting_for_the_tick():
    # #2 cannot begin: #3 is not even tried — it would take a claim nobody can
    # staff — and #1, begun before, is finished all the same
    steps, done = _play(_working_set(frontier=[1, 2, 3]),
                        {("begin", 2): _Raises("orca worktree create failed")})
    assert _brief(steps) == [("begin", 1), ("begin", 2), ("finish", [1]), ("heartbeat",)]
    assert done["errors"] == [{"step": "dispatch", "issue": 2,
                               "error": "orca worktree create failed"}]
    assert done["did"] == _nothing_done(dispatched=[1], in_flight=1, frontier_remaining=2)
    # it ends EVERY kind of start: a continuation that cannot begin leaves the dead
    # peer's claim untaken and the frontier untouched
    ws = _working_set(mine=[_row(7)], stale=[6], frontier=[1])
    steps, done = _play(ws, {("begin", 7): _Raises("no orca")}, causes={7: "gone"})
    assert _brief(steps) == [("no-pr", [7]), ("begin", 7), ("heartbeat",)]
    assert done["did"] == _nothing_done(in_flight=1, frontier_remaining=1)
    # a start whose agent never comes up is an error of ITS start only: the others ran
    steps, done = _play(_working_set(frontier=[1, 2]), {("finish", 1): _Raises("not ready")})
    assert done["errors"] == [{"step": "dispatch", "issue": 1, "error": "not ready"}]
    assert done["did"] == _nothing_done(dispatched=[2], in_flight=1)
    assert done["held"] == {2}


def test_a_claim_a_peer_won_is_off_the_frontier_and_takes_no_slot():
    # a peer took #1 after the rebuild: neither a start nor a failure — the slot it
    # would have filled goes to the next issue, and the starting goes on
    steps, done = _play(_working_set(frontier=[1, 2, 3, 4, 5]), {("begin", 1): d.LOST})
    assert _brief(steps) == [("begin", 1), ("begin", 2), ("begin", 3), ("begin", 4),
                             ("finish", [2, 3, 4]), ("heartbeat",)]
    assert done["errors"] == []
    assert done["did"] == _nothing_done(dispatched=[2, 3, 4], in_flight=3, frontier_remaining=1)


def test_a_claim_settled_this_tick_frees_its_slot_for_a_dispatch_in_the_same_tick():
    # three claims fill the fleet. One is parked, one escalated, one outlived its
    # issue: three slots, filled from the frontier before the tick ends — and a
    # dead peer's phantom lock, which was never a slot of mine, frees none
    for settled in ("closed", "landed"):        # a `landed` row is released like a `closed` one
        ws = _working_set(mine=[_row(3, settled)])
        steps, done = _play(ws)
        assert _brief(steps) == [("release", 3)] and done["did"]["cleared"] == [3], settled
        assert d.asks_after(ws["mine"]) == [] and d.turn_order(ws["mine"]) == []
    mine = [_row(1), _row(2), _row(3, "closed")]
    ws = _working_set(mine=mine, frontier=[11, 12, 13, 14], stale_closed=[9])
    blocked = {1: "blockers_waiting", 2: "blocker_unmet"}
    steps, done = _play(ws, causes=blocked)
    assert _brief(steps) == [
        ("no-pr", [1, 2]), ("park", 1), ("escalate", 2), ("release", 3), ("release", 9),
        ("begin", 11), ("begin", 12), ("begin", 13), ("finish", [11, 12, 13]), ("heartbeat",)]
    assert steps[4] == {"do": "release", "issue": 9, "expect_sha": "sha9"}
    assert done["did"] == _nothing_done(parked=[1], escalated=[2], cleared=[3, 9],
                                        dispatched=[11, 12, 13], in_flight=3,
                                        frontier_remaining=1)
    assert done["held"] == {11, 12, 13}
    # a retry keeps its claim, and so its slot; so does a continuation; a stale
    # claim taken from a dead peer USES one, whether or not it could be started
    ws = _working_set(mine=[_row(1), _row(2)], stale=[6], frontier=[11])
    steps, done = _play(ws, causes={1: "satisfied_refuted", 2: "gone"})
    assert _brief(steps)[:5] == [("no-pr", [1, 2]), ("fail", 1), ("begin", 2), ("reclaim", 6),
                                 ("begin", 6)]
    assert steps[3] == {"do": "reclaim", "issue": 6, "sha": "sha6"}
    assert _brief(steps)[5:] == [("finish", [2, 6]), ("heartbeat",)]
    assert done["did"] == _nothing_done(retried=[1], dispatched=[2], reclaimed=[6], in_flight=3,
                                        frontier_remaining=1)
    # a reclaim a peer beat me to is no claim of mine: its slot goes to the frontier
    steps, done = _play(_working_set(mine=[_row(1), _row(2)], stale=[6], frontier=[11]),
                        {("reclaim", 6): {"won": False}})
    assert ("begin", 6) not in _brief(steps) and ("begin", 11) in _brief(steps)
    assert done["did"] == _nothing_done(dispatched=[11], in_flight=3)


def test_a_stale_claim_is_taken_into_a_free_slot_only_and_the_rest_wait_for_a_later_tick():
    # one slot free, three dead peer's claims: the lowest is taken, the other two
    # are not even tried, and the frontier — behind the stale claims — gets nothing
    ws = _working_set(mine=[_row(1)], stale=[6, 7, 8], frontier=[11])
    steps, done = _play(ws, concurrency=2)
    assert _brief(steps) == [("no-pr", [1]), ("reclaim", 6), ("begin", 6), ("finish", [6]),
                             ("heartbeat",)]
    assert done["did"] == _nothing_done(reclaimed=[6], in_flight=2, frontier_remaining=1)
    # a reclaim a peer won, or one that raised, used no slot: the next one is tried
    steps, done = _play(ws, {("reclaim", 6): {"won": False}, ("reclaim", 7): _Raises("push")},
                        concurrency=2)
    assert [s for s in _brief(steps) if s[0] in ("reclaim", "begin")] == [
        ("reclaim", 6), ("reclaim", 7), ("reclaim", 8), ("begin", 8)]
    # the ones left are taken by later ticks, in order, as claims settle: here one
    # claim of mine closes before each tick, and each tick takes exactly one
    mine, stale, taken = [1, 2], [5, 6, 7, 8, 9], []
    while stale:
        ws = _working_set(mine=[_row(mine[0], "closed"), *map(_row, mine[1:])], stale=stale)
        steps, done = _play(ws, concurrency=2)
        assert done["did"]["in_flight"] == 2
        taken += done["did"]["reclaimed"]
        mine, stale = sorted(done["held"]), [n for n in stale if n not in taken]
    assert taken == [5, 6, 7, 8, 9]
    # a fleet over the bound takes nothing — no stale claim, no frontier issue, not
    # even into the slot of the claim it settles — until it is back under; a claim
    # it already holds is still continued, which takes no slot
    mine = [_row(1), _row(2), _row(3), _row(4, "closed")]
    steps, done = _play(_working_set(mine=mine, stale=[6], frontier=[11]), causes={1: "gone"},
                        concurrency=2)
    assert _brief(steps) == [("no-pr", [1, 2, 3]), ("release", 4), ("begin", 1), ("finish", [1]),
                             ("heartbeat",)]
    assert done["did"]["in_flight"] == 3 and done["held"] == {1, 2, 3}


def test_no_tick_ends_holding_more_claims_than_the_bound_or_than_it_began_with():
    """Whatever the working set — stale claims, more claims than slots — and
    however each step ends, the claims held never pass max(`concurrency`, the
    claims held as the tick began): a tick that starts at or under the bound
    stays there, and one that starts over it takes nothing until it is back
    under."""
    causes = ("working", "gone", "silent", "blockers_waiting", "blocker_unmet",
              "satisfied_refuted")
    ends = {"reclaim": ({"won": True}, {"won": True}, {"won": False}, _Raises("push refused")),
            "begin": (d.BEGUN, d.BEGUN, d.BEGUN, d.LOST, _Raises("no orca")),
            "fail": ({"action": "retry"}, {"action": "escalate"}),
            "finish": ({"ok": True}, {"ok": True}, _Raises("not ready"))}
    releases = {"park", "escalate", "release"}
    over = 0
    for seed in range(3000):
        rng = random.Random(seed)
        concurrency = rng.randrange(5)
        numbers = rng.sample(range(1, 40), rng.randrange(8) + rng.randrange(8) + rng.randrange(6))
        mine, rest = numbers[:rng.randrange(8)], numbers[8:]
        stale = sorted(rest[:rng.randrange(8)])
        frontier = sorted(set(rest) - set(stale))
        ws = _working_set(mine=[_row(n, rng.choice(("no_pr", "no_pr", "closed"))) for n in mine],
                          stale=stale, frontier=frontier)
        answers = {(do, n): rng.choice(how) for do, how in ends.items() for n in numbers}
        steps, done = _play(ws, answers, causes={n: rng.choice(causes) for n in mine},
                            concurrency=concurrency)
        held, bound = set(mine), max(concurrency, len(mine))
        over += len(mine) > concurrency
        for step in steps:
            do, n = step["do"], step.get("issue")
            answer = answers.get((do, n))
            if isinstance(answer, _Raises):
                continue
            if do in releases and "expect_sha" not in step or answer == {"action": "escalate"}:
                held.discard(n)
            elif answer in ({"won": True}, d.BEGUN) and n not in held:
                # a claim is taken only into a free slot: over the bound, none is
                assert len(held) < concurrency, (seed, step, held, concurrency)
                held.add(n)
            assert len(held) <= bound, (seed, step, held, concurrency)
        assert done["did"]["in_flight"] <= len(held) <= bound, seed
        # the stale claims tried are the first ones, in order: none is skipped
        tried = [s["issue"] for s in steps if s["do"] == "reclaim"]
        assert tried == stale[:len(tried)], (seed, tried)
    assert over > 300       # it did meet fleets holding more claims than slots


def test_a_transition_that_fails_settles_nothing_and_the_rest_of_the_tick_runs():
    mine = [_row(1), _row(2), _row(3), _row(4, "closed"), _row(5, "awaiting_turn", pr=50)]
    ws = _working_set(mine=mine, frontier=[11])
    causes = {1: "blocker_unmet", 2: "satisfied_refuted", 3: "blockers_waiting"}
    broken = {"escalate": _Raises("gh issue edit failed"), "park": _Raises("TypeError: null"),
              "release": _Raises("push refused"), "turn": _Raises("gh is down"),
              "heartbeat": _Raises("push refused")}
    steps, done = _play(ws, broken, causes=causes, concurrency=5)
    # every step is still handed out, in order, whatever the one before it did
    assert _brief(steps) == [("no-pr", [1, 2, 3]), ("turn", 5), ("escalate", 1), ("fail", 2),
                             ("park", 3), ("release", 4), ("heartbeat",)]
    assert [(e["step"], e.get("issue")) for e in done["errors"]] == [
        ("turn", 5), ("escalate", 1), ("park", 3), ("release", 4), ("heartbeat", None)]
    assert done["errors"][1]["error"] == "gh issue edit failed"
    # nothing settled: five claims still held, no slot freed, the frontier untouched
    assert done["did"] == _nothing_done(retried=[2], in_flight=5, frontier_remaining=1)
    assert done["held"] == {1, 2, 3, 4, 5}
    # the workers could not even be asked after: no route, and the rest still runs
    steps, done = _play(_working_set(mine=[_row(1)], frontier=[11]),
                        {"no-pr": _Raises("orca is down")})
    assert _brief(steps) == [("no-pr", [1]), ("begin", 11), ("finish", [11]), ("heartbeat",)]
    assert done["errors"] == [{"step": "no-pr", "error": "orca is down"}]


def test_a_tick_grants_at_most_one_landing_turn():
    # three PRs wait: the head of the merge queue is granted, the others are not asked
    waiting = [_row(n, "awaiting_turn", pr=n * 10) for n in (2, 1, 3)]
    steps, done = _play(_working_set(mine=waiting))
    assert [s for s in _brief(steps) if s[0] == "turn"] == [("turn", 1)]
    assert done["did"]["granted"] == [1] and done["judgments"] == []
    # a PR holds the turn and its worker is at it: nobody is granted anything
    landing = [_row(1, "landing", pr=10), _row(2, "awaiting_turn", pr=20)]
    steps, done = _play(_working_set(mine=landing))
    assert _brief(steps) == [("no-pr", [1]), ("heartbeat",)] and done["did"]["granted"] == []
    # …and one that stopped for the tick is told again — still the one turn
    landing[0]["stopped"] = "awaiting_ci"
    steps, done = _play(_working_set(mine=landing))
    assert _brief(steps) == [("turn", 1), ("heartbeat",)] and done["did"]["granted"] == [1]
    # a turn that could not be decided is a judgment, and the next PR still waits
    steps, done = _play(_working_set(mine=waiting),
                        {"turn": {"issue": 1, "pr": 10, "head": "abc", "outcome": "no_checks"}})
    assert [s for s in _brief(steps) if s[0] == "turn"] == [("turn", 1)]
    assert [(j["issue"], j["kind"]) for j in done["judgments"]] == [(1, "no_checks")]
    assert done["did"]["granted"] == []
    # a merge batch is that one turn: formed from the PRs free to land together,
    # and no single turn beside it
    batching = d.resolve_config({"gate": {"ci": "local"}})
    formed = {"outcome": "granted", "batch": "b9", "issues": [1, 2, 3]}
    steps, done = _play(_working_set(mine=waiting), {"batch-turn": formed}, config=batching)
    assert _brief(steps) == [("batch-turn",), ("heartbeat",), ("sweep",)]
    assert steps[-1]["live"] == ["b9"] and done["did"]["granted"] == [1, 2, 3]
    # too few of them were free after all: the turn goes to one PR, as before
    steps, done = _play(_working_set(mine=waiting), {"batch-turn": {"outcome": "too_few"}},
                        config=batching)
    assert _brief(steps)[:2] == [("batch-turn",), ("turn", 1)] and steps[-1]["live"] == []
    assert done["did"]["granted"] == [1]
    # forming it failed: an error, and no turn is granted behind its back
    steps, done = _play(_working_set(mine=waiting), {"batch-turn": _Raises("orca is down")},
                        config=batching)
    assert not [s for s in steps if s["do"] == "turn"] and done["did"]["granted"] == []
    assert done["errors"] == [{"step": "turn", "error": "orca is down"}]


def test_a_dead_fleets_batch_is_abandoned_before_anything_is_granted():
    # claims I took carry a dead fleet's batch: it is abandoned, and no turn — a
    # batch's or a single PR's — is granted until the next cycle reads the result
    mine = [_row(n, "awaiting_turn", pr=n * 10) for n in (1, 2)]
    ws = _working_set(mine=mine, batches=[_batch([1, 2], instance="dead", batch="old")])
    steps, done = _play(ws, {"abandon": {"issues": [1, 2]}})
    assert _brief(steps) == [("abandon", "old"), ("heartbeat",), ("sweep",)]
    assert steps[-1]["live"] == [] and done["did"]["abandoned"] == [1, 2]
    assert done["did"]["granted"] == [] and done["did"]["in_flight"] == 2
    # the abandon failed: the batch still holds its turn, and its worktree is not swept
    steps, done = _play(ws, {"abandon": _Raises("gh is down")})
    assert _brief(steps) == [("abandon", "old"), ("heartbeat",), ("sweep",)]
    assert steps[-1]["live"] == ["old"] and done["did"]["abandoned"] == []
    assert done["errors"] == [{"step": "turn", "error": "gh is down"}]
    # it comes before my own batch's worker is asked after, too
    both = _working_set(mine=mine, batches=[_batch([1], instance="dead", batch="old"),
                                            _batch([2], batch="new")])
    steps, done = _play(both, {"abandon": {"issues": [1]}})
    assert _brief(steps) == [("abandon", "old"), ("heartbeat",), ("sweep",)]
    assert steps[-1]["live"] == ["new"]


def test_a_batch_of_mine_is_left_continued_nudged_or_abandoned():
    held = {"id": "b1", "members": [1, 2], "phase": "stacking"}
    mine = [_row(n, "landing", pr=n * 10, batch=held) for n in (1, 2)]
    ws = _working_set(mine=mine + [_row(3, "awaiting_turn", pr=30)], batches=[_batch([1, 2])])

    def played(cause, **answers):
        steps, done = _play(ws, answers, causes={"b1": cause})
        # the batch's worker is asked after once, for the batch — never its members' own
        assert _brief(steps)[0] == ("no-pr", "b1") and steps[0] == {"do": "no-pr", "batch": "b1"}
        # the turn is out: #3 waits, whatever becomes of the batch this tick
        assert not [s for s in steps if s["do"] == "turn"]
        return _brief(steps)[1:-2], steps[-1]["live"], done

    # its worker is at it: nothing
    for cause in ("working", "just_stopped", "within_grace", "awaiting_tick"):
        acted, live, done = played(cause)
        assert (acted, live) == ([], ["b1"]) and done["did"] == _nothing_done(in_flight=3)
    # its terminal is gone: continued, which is the turn told again
    again = {"outcome": "granted", "batch": "b1", "issues": [1, 2]}
    acted, live, done = played("gone", **{"batch-turn": again})
    assert (acted, live, done["did"]["granted"]) == ([("batch-turn",)], ["b1"], [1, 2])
    acted, live, done = played("gone", **{"batch-turn": {"outcome": "landing"}})
    assert (acted, live, done["did"]["granted"]) == ([("batch-turn",)], ["b1"], [])
    # silent past grace: nudged once, for every PR it carries
    acted, live, done = played("silent")
    assert (acted, live, done["did"]["nudged"]) == ([("nudge", "b1")], ["b1"], [1, 2])
    # silent again: abandoned — nothing is live, and its worktree is swept this tick
    for cause in ("silent_after_nudge", "silent_unnudgeable"):
        acted, live, done = played(cause, abandon={"issues": [1, 2]})
        assert (acted, live, done["did"]["abandoned"]) == ([("abandon", "b1")], [], [1, 2])
    # an abandon that failed leaves the batch holding its turn
    acted, live, done = played("silent_after_nudge", abandon=_Raises("gh is down"))
    assert live == ["b1"] and done["did"]["abandoned"] == []
    # its worker could not be read: left, and reported
    steps, done = _play(ws, {"no-pr": _Raises("orca is down")})
    assert _brief(steps) == [("no-pr", "b1"), ("heartbeat",), ("sweep",)]
    assert done["errors"] == [{"step": "no-pr", "error": "orca is down"}]


def test_a_tick_runs_its_stages_in_one_order_and_writes_each_board_once():
    # every stage at once: observe → the turn → nudge / fail / park / escalate →
    # releases → continuations → reclaims → the frontier → finish → the lease →
    # the boards no transition of this tick already wrote
    mine = [_row(1, board_phase="claimed"), _row(2, board_phase="claimed"),
            _row(3, board_phase="claimed"), _row(4, board_phase="claimed"),
            _row(5, "awaiting_turn", pr=50, board_phase="awaiting_turn"),
            _row(6, "failure", pr=60, board_phase="ci_failed", attempt=1),
            _row(7, "closed"), _row(8, "awaiting_ci", pr=80, board_phase="pr_open"),
            _row(9, board_phase="claimed")]
    ws = _working_set(mine=mine, stale=[20], stale_closed=[21], frontier=[30, 31, 32])
    causes = {1: "silent", 2: "gone", 3: "blockers_waiting", 4: "satisfied", 9: "satisfied_refuted"}
    steps, done = _play(ws, causes=causes, concurrency=11)
    assert _brief(steps) == [
        ("no-pr", [1, 2, 3, 4, 9]), ("turn", 5), ("nudge", 1), ("park", 3), ("fail", 9),
        ("release", 7), ("release", 21), ("begin", 2), ("reclaim", 20), ("begin", 20),
        ("begin", 30), ("begin", 31), ("begin", 32), ("finish", [2, 20, 30, 31, 32]),
        ("heartbeat",), ("status", 1), ("status", 4), ("status", 6), ("status", 8)]
    # free slots: 11 − 9 held + the two settled (#3, #7) − the one reclaimed = 3
    assert done["did"] == {
        "granted": [5], "dispatched": [2, 30, 31, 32], "reclaimed": [20], "cleared": [7, 21],
        "escalated": [], "parked": [3], "abandoned": [], "retried": [9], "nudged": [1],
        "restarted": [], "in_flight": 11, "frontier_remaining": 0}
    # a board is written for a claim only where no transition of this tick wrote it
    assert steps[-2] == {"do": "status", "issue": 6, "phase": "ci_failed", "pr": 60, "attempt": 1}
    # the judgments, in the order the tick met them: red checks, then the workers'
    assert [(j["issue"], j["kind"]) for j in done["judgments"]] == [(6, "reason"), (4, "empty_diff")]
    # the boards still mine to remember: every claim held as the tick ends
    assert done["held"] == {1, 2, 4, 5, 6, 8, 9, 20, 30, 31, 32}


def test_a_landing_worker_silent_after_its_nudge_is_restarted_onto_its_turn():
    """ADR-0035: the pass restarts the worker (`afk turn --restart`) where it used
    to fail the claim — in the nudge / fail stage, after the turn stage, which
    grants nothing while the PR holds the turn. The restart writes the board
    (it starts a worker), spends nothing, and a result that is not `granted` is
    routed as any `afk turn` result: a judgment whose yes restarts."""
    row = _row(5, "landing", pr=50, stopped="conflict", board_phase="landing")
    ws = _working_set(mine=[row, _row(6, "awaiting_turn", pr=60, board_phase="awaiting_turn")])
    granted = {"issue": 5, "pr": 50, "head": "abc", "outcome": "granted",
               "delivery": "continuation", "restarted": NOW}
    steps, done = _play(ws, {("restart", 5): granted}, causes={5: "silent_on_turn"})
    assert _brief(steps) == [("no-pr", [5]), ("restart", 5), ("heartbeat",), ("status", 6)]
    assert done["did"] == _nothing_done(restarted=[5], in_flight=2)
    assert done["judgments"] == [] and done["held"] == {5, 6}
    # the restart answered with a judgment: asked, with the restart as its yes
    owed = {"issue": 5, "pr": 50, "head": "abc", "outcome": "no_checks"}
    steps, done = _play(ws, {("restart", 5): owed}, causes={5: "silent_on_turn"})
    assert done["did"]["restarted"] == [] and [j["kind"] for j in done["judgments"]] == ["no_checks"]
    assert _argv(done["judgments"][0]["if_yes"])[1][-2:] == ["--restart", "--allow-no-checks"]
    # the restart failed: an error of the turn step, the claim still held, the board written
    steps, done = _play(ws, {("restart", 5): _Raises("orca is down")}, causes={5: "silent_on_turn"})
    assert done["errors"] == [{"step": "turn", "issue": 5, "error": "orca is down"}]
    assert ("status", 5) in _brief(steps) and done["held"] == {5, 6}
    # past its one restart the same silence is an ESCALATION with the turn's reason
    # (#91): the claim is settled — its slot freed, its board the escalation's —
    # and nothing is retried; `afk fail` is never a step of it
    steps, done = _play(ws, causes={5: "silent_past_restart"})
    assert _brief(steps) == [("no-pr", [5]), ("escalate", 5), ("heartbeat",), ("status", 6)]
    assert done["did"] == _nothing_done(escalated=[5], in_flight=1)
    assert done["held"] == {6} and done["judgments"] == []
    assert steps[1]["reason"] == _KEPT.format(pr=50, stopped="conflict")
    # a landing row's silence is never routed to `afk fail`: the plan refuses it
    try:
        _play(ws, causes={5: "silent_after_nudge"})
    except ValueError as e:
        assert "never a failure" in str(e)
    else:
        raise AssertionError("a landing claim's silence reached `afk fail`")


def test_a_tick_grants_the_turn_elsewhere_while_a_pr_is_fixed_off_the_one_it_gave_up():
    """ADR-0045: a `fixing` claim holds no turn — the head of the queue gets one,
    or a batch forms — while its worker is watched on the landing's own ladder."""
    fixing = _row(5, "fixing", pr=50, stopped="conflict", given_up=True, board_phase="fixing")
    waiting = [_row(n, "awaiting_turn", pr=n * 10, board_phase="awaiting_turn") for n in (6, 7)]
    granted = {"issue": 6, "pr": 60, "head": "abc", "outcome": "granted", "delivery": "terminal"}
    # one PR waits: it is granted its single turn although #5 is still being fixed
    steps, done = _play(_working_set(mine=[fixing, waiting[0]]), {("turn", 6): granted},
                        causes={5: "working"})
    assert _brief(steps) == [("no-pr", [5]), ("turn", 6), ("heartbeat",), ("status", 5)]
    assert done["did"] == _nothing_done(granted=[6], in_flight=2)
    # two wait: they form a merge batch beside it
    local = d.resolve_config({"gate": {"ci": "local", "local_command": "make test"}})
    formed = {"outcome": "granted", "batch": "fl-1-9", "issues": [6, 7], "prs": [60, 70]}
    steps, done = _play(_working_set(mine=[fixing, *waiting]), {"batch-turn": formed},
                        causes={5: "working"}, config=local)
    assert _brief(steps)[:2] == [("no-pr", [5]), ("batch-turn",)]
    assert done["did"]["granted"] == [6, 7]
    # its worker went silent after its nudge: restarted to keep fixing — no turn
    # is granted by that, and the queue's head still gets its own in the same tick
    kept = {"issue": 5, "pr": 50, "head": "abc", "outcome": "fixing", "restarted": NOW,
            "delivery": "continuation"}
    steps, done = _play(_working_set(mine=[fixing, waiting[0]]),
                        {("turn", 6): granted, ("restart", 5): kept}, causes={5: "silent_on_turn"})
    assert _brief(steps) == [("no-pr", [5]), ("turn", 6), ("restart", 5), ("heartbeat",)]
    assert done["did"] == _nothing_done(granted=[6], restarted=[5], in_flight=2)
    # …and silent again past that restart it is escalated with everything kept
    steps, done = _play(_working_set(mine=[fixing, waiting[0]]), {("turn", 6): granted},
                        causes={5: "silent_past_restart"})
    assert _brief(steps) == [("no-pr", [5]), ("turn", 6), ("escalate", 5), ("heartbeat",)]
    assert done["did"] == _nothing_done(granted=[6], escalated=[5], in_flight=1)
    assert "gave it up" in steps[2]["reason"] and "kept as they are" in steps[2]["reason"]
    try:
        _play(_working_set(mine=[fixing]), causes={5: "silent_after_nudge"})
    except ValueError as e:
        assert "never a failure" in str(e)
    else:
        raise AssertionError("a fixing claim's silence reached `afk fail`")


def test_a_plan_names_only_steps_the_table_lists_and_refuses_an_unknown_cause():
    assert set(d.TICK_STEPS.values()) == {"no-pr", "turn", "nudge", "park", "fail", "escalate",
                                          "release", "reclaim", "dispatch", "heartbeat",
                                          "status", "sweep"}
    # a cause the routing does not know is an error of the tick, never a step
    try:
        _play(_working_set(mine=[_row(1)]), causes={1: "levitating"})
        assert False, "expected ValueError"
    except ValueError as e:
        assert "no route for a worker classified 'levitating'" in str(e)


def _prompt_template():
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "..", "references", "worker-prompt.md")) as f:
        return f.read()


PROMPT_FIELDS = {"n": 31, "title": "Names inspector tab", "repo": "acme/widgets",
                 "base_branch": "main", "local_command": "make test",
                 "afk_path": "/skills/afk fleet/scripts/afk.py",
                 "config": '{"base_branch": "main"}',
                 "branch": "sunfmin/issue-31-names", "worktree_path": "/wt/issue-31",
                 "launcher_terminal": "term_launcher-1"}


def test_render_worker_prompt_fills_the_shipped_template():
    t = _prompt_template()
    fresh = d.render_worker_prompt(t, "fresh", PROMPT_FIELDS)
    cont = d.render_worker_prompt(t, "continue", PROMPT_FIELDS)

    for body in (fresh, cont):
        # every field landed, and nothing template-ish is left for a worker to read
        assert "`acme/widgets#31` — Names inspector tab" in body
        assert "`sunfmin/issue-31-names`" in body and "`/wt/issue-31`" in body
        assert "Closes #31" in body and "gh issue view 31 --repo acme/widgets" in body
        assert "make test" in body and "git merge origin/main" in body
        # the gate is run THROUGH the tool, so a green run is on record (ADR-0026)
        assert d.gate_command(PROMPT_FIELDS["afk_path"], "make test") in body
        assert "\n   make test" not in body                    # never the bare command to run
        assert not re.search(r"\{[a-z_0-9.]+\}", body), re.findall(r"\{[a-z_0-9.]+\}", body)
        assert "afk:block" not in body and "Worker prompt template" not in body
        # the single-outcome rule and the checkpoint rule are in both variants
        assert "<!--afk:verdict n=31 phase=" in body and "Publish progress as you go" in body
        assert "Why the previous attempt failed" not in body          # not a retry

    # the variants differ ONLY in the opening and in step 1
    assert fresh.startswith("You are an afk-fleet worker. You own")
    assert cont.startswith("You are an afk-fleet worker **continuing**")
    assert "1. **Read the ground truth first.**" in fresh and "Inspect the existing" not in fresh
    assert "1. **Inspect the existing progress first.**" in cont
    assert "git log origin/main..HEAD" in cont
    strip = lambda b: b[b.index("**Your issue:**"):b.index("## Steps")] + b[b.index("\n2. **Implement**"):]
    assert strip(fresh) == strip(cont)

    # a retry carries the reason the previous attempt failed, last
    retry = d.render_worker_prompt(t, "fresh", PROMPT_FIELDS, reason="  gate red: 2 tests fail in x_test.go  ")
    assert retry.startswith(fresh.rstrip("\n")) and retry.rstrip().endswith("gate red: 2 tests fail in x_test.go")
    assert "## Why the previous attempt failed" in retry

    # no local gate configured: a no-op with a note, never an empty command line
    none = d.render_worker_prompt(t, "fresh", {**PROMPT_FIELDS, "local_command": "  "})
    assert "no gate.local_command configured" in none and " gate --config " not in none


def test_a_worker_is_told_how_to_wake_the_launcher_and_nothing_else():
    t = _prompt_template()
    wake = 'orca terminal send --terminal term_launcher-1 --text "afk-wake #31" --enter'
    assert d.wake_command("term_launcher-1", 31) == wake and d.wake_line(31) == "afk-wake #31"

    # every way a worker is instructed carries the same one line: a fresh start, a
    # continuation, and the landing brief a worker is given with its turn
    for body in (d.render_worker_prompt(t, "fresh", PROMPT_FIELDS),
                 d.render_worker_prompt(t, "continue", PROMPT_FIELDS),
                 d.render_landing(t, PROMPT_FIELDS, LANDING)):
        assert wake in body and "launcher_terminal" not in body and "{wake_command}" not in body

    # no launcher terminal (a headless tick): a no-op with a note, never a broken
    # command — and the same for anything that is not a bare handle, because the
    # worker runs this string in its shell
    for handle in ("", None, "  ", "term_1; rm -rf ~", "$(whoami)", "a b"):
        cmd = d.wake_command(handle, 31)
        assert cmd.startswith("true ") and "orca" not in cmd, (handle, cmd)
    headless = d.render_worker_prompt(t, "fresh", {**PROMPT_FIELDS, "launcher_terminal": ""})
    assert "orca terminal send" not in headless and "no coordinator terminal to wake" in headless


LANDING = {"pr": 77, "pr_branch": "sunfmin/issue-31-names", "target": "main"}


def test_a_worker_reads_the_one_way_its_pr_lands_when_it_holds_the_turn():
    """ADR-0027. `afk land` is the only way a PR lands, and the landing brief is
    the only place it is spelled: the command, and what to do on each outcome it
    can stop with. A worker with a PR still to open is told only that it lands
    when it is told to, and never by hand."""
    t = _prompt_template()
    # the config travels whole, quoted for the worker's shell — and it is free text
    # to the template: a `{branch}` or an apostrophe inside it arrives verbatim
    config = json.dumps({"base_branch": "main", "note": "it's {branch} {title} {pr}"})
    fields = {**PROMPT_FIELDS, "config": config}
    land = d.land_command(fields["afk_path"], 31, "acme/widgets", config)
    assert land == ("'/skills/afk fleet/scripts/afk.py' land --issue 31 --repo acme/widgets "
                    "--config " + shlex.quote(config))
    assert shlex.split(land)[-1] == config
    fresh = d.render_worker_prompt(t, "fresh", fields)
    cont = d.render_worker_prompt(t, "continue", fields)
    alone = d.render_landing(t, fields, LANDING)
    assert alone.count(land) == 1, alone.count(land)
    for outcome in d.LAND_OUTCOMES:
        assert f"| `{outcome}` |" in alone, outcome
    for body in (fresh, cont, alone):
        assert "gh pr merge" in body and "afk:block" not in body         # named only to forbid it
    assert "landing turn" in alone
    # a worker with a PR to open is told it does NOT merge it, and that it is told
    # when to land it — and is not handed the command, or its outcomes, or how the
    # fleet takes turns, before it can act on any of them
    for body in (fresh, cont):
        assert "Do not merge the PR" in body and "you are told\n   here when to land it" in body
        assert " land --issue " not in body and "| `outcome` |" not in body
        assert "landing turn" not in body

    # the landing brief is the whole instruction, alone: which PR, where it lands,
    # the command, the wake — and nothing about implementing or opening a PR
    assert alone.startswith("## Your PR holds the landing turn")
    for needle in ("PR #77", "`sunfmin/issue-31-names`", "landing on `main`", "`/wt/issue-31`",
                   "`acme/widgets#31` — Names inspector tab", "phase=giving-up",
                   "<!--afk:verdict n=31 phase="):
        assert needle in alone, needle
    assert "Closes #31" not in alone and "## Steps" not in alone
    assert not re.search(r"\{[a-z_0-9.]+\}", alone.replace(land, ""))

    for bad in ({k: v for k, v in LANDING.items() if k != "pr_branch"},):
        try:
            d.render_landing(t, PROMPT_FIELDS, bad)
            assert False, "expected ValueError"
        except ValueError as e:
            assert "pr_branch" in str(e)
    try:
        d.render_landing("no blocks here", PROMPT_FIELDS, LANDING)
        assert False, "expected ValueError"
    except ValueError as e:
        assert "'landing' block" in str(e)


def test_a_worker_is_not_sent_to_this_repos_adrs():
    """The worker is in the TARGET repo, and step 1 sends it to that repo's
    `docs/adr/`: an ADR number from this repo would name the wrong document."""
    t = _prompt_template()
    for body in (d.render_worker_prompt(t, "fresh", PROMPT_FIELDS, reason="gate red"),
                 d.render_worker_prompt(t, "continue", PROMPT_FIELDS),
                 d.render_landing(t, PROMPT_FIELDS, LANDING)):
        assert not re.search(r"ADR-\d", body), re.findall(r"ADR-\d+", body)


def test_render_worker_prompt_never_ships_a_placeholder():
    t = _prompt_template()
    # every field goes in in one pass, so a title or reason that looks like a
    # placeholder is delivered verbatim rather than filled in
    odd = d.render_worker_prompt(t, "fresh", {**PROMPT_FIELDS, "title": "Support {branch} and {n}"},
                                 reason="it printed {worktree_path}")
    assert "— Support {branch} and {n}\n" in odd and "it printed {worktree_path}" in odd

    def refuses(*args, why):
        try:
            d.render_worker_prompt(*args)
            assert False, f"expected ValueError ({why})"
        except ValueError as e:
            assert why in str(e), e

    refuses(t, "resume", PROMPT_FIELDS, why="variant")
    refuses(t, "fresh", {k: v for k, v in PROMPT_FIELDS.items() if k != "branch"}, why="branch")
    refuses("no blocks here", "fresh", PROMPT_FIELDS, why="'prompt' block")
    block = lambda name, body: f"<!--afk:block {name}-->\n{body}\n<!--/afk:block-->\n"
    partial = block("prompt", "{opening} {step1} {retry_reason}") + block("opening.fresh", "hi")
    refuses(partial, "fresh", PROMPT_FIELDS, why="step1.fresh")
    # a template naming a slot it never fills is caught, not sent
    looping = (block("prompt", "{opening} {step1}") + block("opening.fresh", "O")
               + block("step1.fresh", "see {opening}"))
    refuses(looping, "fresh", PROMPT_FIELDS, why="unfilled")
    # and so is a placeholder no field answers to — in any of the three briefs
    unknown = block("prompt", "{opening} {step1} {nope}") + block("opening.fresh", "O") + block("step1.fresh", "S")
    refuses(unknown, "fresh", PROMPT_FIELDS, why="unfilled placeholder(s) {nope}")
    refuses(block("prompt", "{opening} {step1} {pr}") + block("opening.fresh", "O") + block("step1.fresh", "S"),
            "fresh", PROMPT_FIELDS, why="unfilled placeholder(s) {pr}")
    for render in (lambda: d.render_landing(block("landing", "{n} {nope}"), PROMPT_FIELDS, LANDING),
                   lambda: d.render_batch_brief(block("batch", "{batch} {nope}"), BATCH_FIELDS)):
        try:
            render()
            assert False, "expected ValueError"
        except ValueError as e:
            assert "unfilled placeholder(s) {nope}" in str(e), e
    # the minimal well-formed template renders
    ok = (block("prompt", "{opening}|{step1}|{n}{retry_reason}") + block("opening.fresh", "O")
          + block("step1.fresh", "S") + block("retry_reason", " because {reason}"))
    assert d.render_worker_prompt(ok, "fresh", PROMPT_FIELDS) == "O|S|31\n"
    assert d.render_worker_prompt(ok, "fresh", PROMPT_FIELDS, reason="R") == "O|S|31 because R\n"


BATCH_FIELDS = {"batch": "me-100", "repo": "acme/widgets", "target": "main",
                "branch": "u/afk-batch-me-100", "worktree_path": "/w/batch",
                "members": [{"issue": 1, "pr": 10, "title": "one"}, {"issue": 2, "pr": 20, "title": "two"}],
                "afk_path": "/s/afk.py", "config": '{"retry": 2}', "launcher_terminal": "term_1"}


def test_no_field_value_is_scanned_for_placeholders():
    """A value is written into the brief and never read again: whatever
    placeholder-shaped text it holds arrives verbatim, for every field of every
    brief. Held by rendering each field twice — once carrying every known
    placeholder, once with the braces swapped for brackets no template reads —
    and finding the two renderings equal but for the brackets."""
    t = _prompt_template()
    names = sorted(set(re.findall(r"\{([a-z_0-9]+)\}", t)))
    assert {"title", "branch", "pr", "reason", "land_command", "batch", "members"} <= set(names)
    braces = " ".join("${%s}" % n for n in names)
    inert = braces.replace("{", "\u27e6").replace("}", "\u27e7")
    restore = lambda body: body.replace("\u27e6", "{").replace("\u27e7", "}")

    def held(render, field):
        assert braces in restore(render(inert)) or field == "launcher_terminal", field
        assert render(braces) == restore(render(inert)), field

    for k in d.PROMPT_FIELDS:
        with_k = lambda v: {**PROMPT_FIELDS, k: f"{PROMPT_FIELDS[k]}{v}"}
        held(lambda v: d.render_worker_prompt(t, "fresh", with_k(v), reason="red")
             + d.render_worker_prompt(t, "continue", with_k(v))
             + d.render_landing(t, with_k(v), LANDING), k)
    held(lambda v: d.render_worker_prompt(t, "fresh", PROMPT_FIELDS, reason=f"red{v}"), "reason")
    for k in d.LANDING_FIELDS:
        held(lambda v: d.render_landing(t, PROMPT_FIELDS, {**LANDING, k: f"{LANDING[k]}{v}"}), k)
    for k in d.BATCH_FIELDS:
        if k == "members":
            for part in ("issue", "pr", "title"):
                held(lambda v: d.render_batch_brief(t, {**BATCH_FIELDS, k: [
                    {**m, part: f"{m[part]}{v}"} for m in BATCH_FIELDS[k]]}), f"members.{part}")
        else:
            held(lambda v: d.render_batch_brief(t, {**BATCH_FIELDS, k: f"{BATCH_FIELDS[k]}{v}"}), k)


def test_a_gate_command_with_shell_variables_reaches_the_worker_as_configured():
    """`${branch}` and `${title}` in `gate.local_command` are the shell's, not the
    template's: the prompt renders, and the gate line and the land command's
    config carry the one command the config holds."""
    t = _prompt_template()
    command = 'BRANCH=${branch} make test && echo "${title}" {pr}'
    config = json.dumps({"gate": {"local_command": command}})
    fields = {**PROMPT_FIELDS, "local_command": command, "config": config}
    gate = d.gate_command(fields["afk_path"], command)
    for variant in d.PROMPT_VARIANTS:
        assert d.render_worker_prompt(t, variant, fields).count(gate) == 1, variant
    land = d.land_command(fields["afk_path"], 31, "acme/widgets", config)
    assert d.render_landing(t, fields, LANDING).count(land) == 1
    carried = lambda line: json.loads(shlex.split(line)[-1])["gate"]["local_command"]
    assert carried(gate) == carried(land) == command


def test_fingerprint():
    issues = [
        {"number": 1, "labels": ["ready-for-agent"], "updatedAt": "2026-07-01T00:00:00Z"},
        {"number": 2, "labels": ["epic"], "updatedAt": "2026-07-02T00:00:00Z"},
    ]
    prs = [{"number": 7, "headRefOid": "abc", "updatedAt": "2026-07-03T00:00:00Z",
            "statusCheckRollup": [{"name": "ci", "status": "COMPLETED", "conclusion": "SUCCESS"}]}]
    claims = [{"number": 1, "instance": "me", "sha": "s1"}]
    issues, prs, claims = _gathered(issues, prs, claims)
    fp = d.fingerprint(issues, prs, claims)

    # canonical: row order, label order and fields outside the digest never move it
    assert fp == d.fingerprint(list(reversed(issues)), prs, claims)
    assert fp == d.fingerprint([{**issues[0], "id": 99}, issues[1]], prs, claims)
    assert fp != d.fingerprint([{**issues[0], "title": "retitled"}, issues[1]], prs, claims)
    two = [{**issues[0], "labels": ["a", "b"]}, issues[1]]
    assert d.fingerprint(two, prs, claims) == \
        d.fingerprint([{**issues[0], "labels": ["b", "a"]}, issues[1]], prs, claims)

    # every decision-relevant change moves it
    assert fp != d.fingerprint(issues[:1], prs, claims)                       # an issue closed
    relabeled = [{**issues[0], "labels": []}, issues[1]]
    assert fp != d.fingerprint(relabeled, prs, claims)                        # ready label pulled
    touched = [{**issues[0], "updatedAt": "2026-07-09T00:00:00Z"}, issues[1]]
    assert fp != d.fingerprint(touched, prs, claims)                          # blocker comment posted
    ci_red = [{**prs[0], "statusCheckRollup": [{"name": "ci", "status": "COMPLETED", "conclusion": "FAILURE"}]}]
    assert fp != d.fingerprint(issues, ci_red, claims)                        # CI finished red
    pushed = [{**prs[0], "headRefOid": "def"}]
    assert fp != d.fingerprint(issues, pushed, claims)                        # worker pushed
    assert fp != d.fingerprint(issues, prs, [{"number": 1, "instance": "peer", "sha": "s2"}])  # reclaimed
    assert fp != d.fingerprint(issues, prs, [{**claims[0], "instance": "peer"}])   # another owner
    edged = [{**issues[0], "blocked_by": 1}, issues[1]]
    assert fp != d.fingerprint(edged, prs, claims)                            # a blocker recorded
    # a PR becomes an issue's only through what it closes, and GitHub may list
    # that after the PR itself
    linked = [{**prs[0], "closingIssuesReferences": [{"number": 1}]}]
    assert fp != d.fingerprint(issues, linked, claims)                        # the PR closes #1
    assert fp == d.fingerprint(issues, [{**prs[0], "closingIssuesReferences": []}], claims)

    # a PR's checks enter as the one word a tick acts on. Still running — queued, in
    # progress, the first of several done green — the claim is `awaiting_ci`
    # throughout, and the digest does not move; the verdict arriving moves it
    def checks(*rows):
        return [{**prs[0], "statusCheckRollup": [
            {"name": f"c{i}", "status": status, "conclusion": conclusion}
            for i, (status, conclusion) in enumerate(rows)]}]

    queued = d.fingerprint(issues, checks(("QUEUED", None), ("QUEUED", None)), claims)
    assert queued == d.fingerprint(issues, checks(("IN_PROGRESS", None), ("QUEUED", None)), claims)
    assert queued == d.fingerprint(
        issues, checks(("COMPLETED", "SUCCESS"), ("IN_PROGRESS", None)), claims)
    green = d.fingerprint(issues, checks(("COMPLETED", "SUCCESS"), ("COMPLETED", "SUCCESS")), claims)
    red = d.fingerprint(issues, checks(("COMPLETED", "SUCCESS"), ("COMPLETED", "FAILURE")), claims)
    assert len({queued, green, red}) == 3 and green == fp
    assert fp != d.fingerprint(issues, checks(), claims)                      # no checks at all
    # heartbeats are not an input at all — the launcher's own skip-cycle refresh
    # can't move the digest (that is what keeps the gate from defeating itself).


# --- the digest and the working set (ADR-0007) ------------------------------ #
#
# Generated worlds: a small backlog, its PRs and its claims, drawn so that every
# branch of the assembly is taken — a claim on a closed issue, two PRs closing one
# issue, a turn held, a turn of a dead fleet's, a batch.

_LABELS = ["ready-for-agent", "epic", "afk-attempt/1", "afk-attempt/2", d.ATTEMPT_STARTING, "bug"]
_ROLLUPS = [None, [], [{"conclusion": "SUCCESS"}], [{"conclusion": "FAILURE"}],
            [{"status": "QUEUED", "conclusion": None}],
            [{"conclusion": "SUCCESS"}, {"state": "PENDING"}, {"conclusion": "SKIPPED"}]]
_INSTANCES = ["me", "peerA", "peerB", None]

# One more value of each field of a gathered row, for a world to be changed by:
# a field with none here is a field these tests do not cover, and they say so.
_OTHER_VALUE = {
    "Issue": {"number": lambda rng: rng.randrange(50, 60), "id": lambda rng: rng.randrange(10**6),
              "title": lambda rng: rng.choice(["a", "b", "c"]),
              "labels": lambda rng: rng.sample(_LABELS, rng.randrange(len(_LABELS))),
              "updatedAt": lambda rng: f"T{rng.randrange(9)}",
              "blocked_by": lambda rng: rng.randrange(3)},
    "PullRequest": {"number": lambda rng: rng.randrange(60, 70),
                    "title": lambda rng: rng.choice(["a", "b", "c"]),
                    "headRefName": lambda rng: rng.choice(["x", "y"]),
                    "headRefOid": lambda rng: rng.choice(["aaa", "bbb"]),
                    "baseRefName": lambda rng: rng.choice(["main", "dev"]),
                    "updatedAt": lambda rng: f"T{rng.randrange(9)}",
                    "statusCheckRollup": lambda rng: rng.choice(_ROLLUPS),
                    "closingIssuesReferences": lambda rng: [
                        {"number": n} for n in rng.sample(range(1, 9), rng.randrange(3))]},
    "Claim": {"number": lambda rng: rng.randrange(70, 80),
              "instance": lambda rng: rng.choice(_INSTANCES),
              "host": lambda rng: rng.choice(["h1", "h2", None]),
              "ts": lambda rng: rng.randrange(1000),
              "sha": lambda rng: rng.choice(["s1", "s2", "s3"])},
}


def _gathered_row(rng, kind, number):
    return {"number": number, **{field: value(rng) for field, value in _OTHER_VALUE[kind].items()
                                 if field != "number"}}


def _world(rng):
    """(issues, prs, claims), and what the working set reads besides them
    (`OUTSIDE_THE_DIGEST`, as keyword arguments)."""
    now = 100_000
    numbers = rng.sample(range(1, 9), rng.randrange(1, 8))
    issues = [_gathered_row(rng, "Issue", n) for n in numbers]
    prs = [_gathered_row(rng, "PullRequest", n) for n in rng.sample(range(20, 26), rng.randrange(5))]
    claimed = rng.sample(range(1, 11), rng.randrange(6))
    claims = [_gathered_row(rng, "Claim", n) for n in claimed]
    turns = {}
    for n in claimed:
        turn = rng.choice([
            None, d.single_turn(None, rng.choice(["me", "peerB"]), now),
            d.next_turn(d.single_turn(None, "me", now), stopped="awaiting_ci", head="aaa"),
            d.next_turn(None, instance=rng.choice(["me", "peerB"]), at=now + n, batch="b1",
                        members=[{"issue": n, "pr": 20}], phase=rng.choice(d.BATCH_PHASES))])
        if turn:
            turns[n] = d.latest_turn([{"id": n, "body": d.turn_comment(turn)}])
    outside = {
        "now": now, "me": "me",
        "heartbeats": {i: now - rng.choice([10, TTL + 99]) for i in ("me", "peerA", "peerB")
                       if rng.random() < 0.8},
        "turns": turns,
        "closed": [n for n in claimed if n not in numbers and rng.random() < 0.7],
        "landed": [n for n in claimed if n in numbers and rng.random() < 0.3],
        "config": d.resolve_config({"epic_labels": ["epic"], "concurrency": rng.randrange(1, 5),
                                    "gate": {"ci": rng.choice(["required", "local"])}}),
    }
    return (issues, prs, claims), outside


def _shuffled(rng, rows):
    """`rows` in another order, every list inside a row in another order too."""
    rows = [{k: rng.sample(v, len(v)) if isinstance(v, list) else v for k, v in row.items()}
            for row in rows]
    return rng.sample(rows, len(rows))


def _changed(rng, lists):
    """`lists` with ONE thing different: a field of one row, a row gone, or a row
    more → (the new lists, what was changed)."""
    kinds = ("Issue", "PullRequest", "Claim")
    at = rng.randrange(3)
    rows = lists[at]
    if rows and rng.random() < 0.8:
        k, field = rng.randrange(len(rows)), rng.choice(sorted(_OTHER_VALUE[kinds[at]]))
        new = [*rows[:k], {**rows[k], field: _OTHER_VALUE[kinds[at]][field](rng)}, *rows[k + 1:]]
        what = f"{kinds[at]}.{field}"
    elif rows and rng.random() < 0.5:
        k = rng.randrange(len(rows))
        new, what = rows[:k] + rows[k + 1:], f"{kinds[at]} gone"
    else:
        new = [*rows, _gathered_row(rng, kinds[at], _OTHER_VALUE[kinds[at]]["number"](rng) + 100)]
        what = f"{kinds[at]} more"
    return (*lists[:at], new, *lists[at + 1:]), what


def test_the_working_set_does_not_depend_on_the_order_rows_arrive_in():
    """Any shuffle of every input list — the rows, and the labels, checks and
    closed issues inside a row — assembles the same working set, digest and
    dispatch order included."""
    import random
    rng = random.Random(120)
    dispatched = set()
    for _ in range(400):
        (issues, prs, claims), outside = _world(rng)
        ws = d.assemble_working_set(issues, prs, claims, **outside)
        for _ in range(4):
            again = {**outside, "closed": rng.sample(outside["closed"], len(outside["closed"])),
                     "heartbeats": dict(rng.sample(sorted(outside["heartbeats"].items()),
                                                   len(outside["heartbeats"]))),
                     "turns": dict(rng.sample(sorted(outside["turns"].items()),
                                              len(outside["turns"])))}
            assert d.assemble_working_set(_shuffled(rng, issues), _shuffled(rng, prs),
                                          _shuffled(rng, claims), **again) == ws
        order = [i["number"] for i in ws["frontier"]["dispatch"]]
        assert order == sorted(order)
        dispatched.add(len(order))
    assert max(dispatched) >= 2, dispatched     # worlds where the order is a question at all


def test_one_digest_means_one_working_set():
    """Two gathers with one digest assemble one working set, given the same
    OUTSIDE_THE_DIGEST: whatever a change to a gathered row does to the working
    set, it does to the digest first."""
    import random
    rng = random.Random(7)
    kept, moved = {}, {}
    for _ in range(600):
        lists, outside = _world(rng)
        ws = d.assemble_working_set(*lists, **outside)
        for _ in range(12):
            other, what = _changed(rng, lists)
            ws2 = d.assemble_working_set(*other, **outside)
            if ws2["fingerprint"] == ws["fingerprint"]:
                assert ws2 == ws, what
                kept[what] = kept.get(what, 0) + 1
            else:
                moved[what] = moved.get(what, 0) + 1
    # the generator covers every field of every gathered row ...
    fields = {f"{kind.__name__}.{f}" for kind in (d.Issue, d.PullRequest, d.Claim)
              for f in typing.get_type_hints(kind)}
    assert fields == {f"{kind}.{f}" for kind, of in _OTHER_VALUE.items() for f in of}
    assert fields <= set(kept) | set(moved), fields - set(kept) - set(moved)
    # ... and both sides of the claim are real: fields the working set never reads
    # leave the digest alone, and the ones it reads are seen to move it
    assert {"Issue.id", "Claim.host", "PullRequest.headRefName",
            "PullRequest.statusCheckRollup"} <= set(kept), sorted(kept)
    assert {"Issue.title", "Issue.labels", "Issue.blocked_by", "Claim.instance",
            "PullRequest.closingIssuesReferences", "Issue gone", "Claim more"} <= set(moved)


def test_what_the_working_set_reads_outside_the_digest_is_named():
    """Every input of the working set is one the digest holds, or is named in
    OUTSIDE_THE_DIGEST with why a skipped cycle may go without it — and ADR-0007
    lists exactly those."""
    import inspect
    digested = set(inspect.signature(d.fingerprint).parameters)
    reads = set(inspect.signature(d.assemble_working_set).parameters)
    assert digested == {"issues", "prs", "claims"} and digested <= reads
    assert reads - digested == set(d.OUTSIDE_THE_DIGEST)

    # the glossary and the ADRs live in the source repo only
    adr = os.path.join(os.path.dirname(__file__), "..", "..", "..", "docs", "adr",
                       "0007-fingerprint-gated-ticks.md")
    if os.path.exists(adr):
        with open(adr) as f:
            text = f.read()
        table = text[text.index("## What the working set reads outside the digest"):]
        table = table[:table.index("\n## ", 3)]
        listed = set(re.findall(r"^\| `(\w+)`", table, re.M))
        assert listed == set(d.OUTSIDE_THE_DIGEST), listed ^ set(d.OUTSIDE_THE_DIGEST)


def test_unseen_prs_are_the_claims_whose_pr_is_not_the_one_the_tick_worked_from():
    mine = [{"number": 1, "pr": None}, {"number": 2, "pr": 20}, {"number": 3, "pr": 30},
            {"number": 4, "pr": None}]

    def closing(pr_number, issue):
        return {"number": pr_number, "closingIssuesReferences": [{"number": issue}]}

    assert d.unseen_prs(mine, [closing(20, 2), closing(30, 3)]) == []
    # opened while the tick ran; replaced by a later attempt; a PR of nobody's claim
    assert d.unseen_prs(mine, [closing(10, 1), closing(20, 2), closing(31, 3), closing(90, 9)]) \
        == [1, 3]
    # a PR the tick closed, or one that landed, leaves nothing to act on
    assert d.unseen_prs(mine, []) == []


def test_fingerprint_gate():
    # first cycle: no baseline → always tick
    assert d.fingerprint_gate("", "aaa", 0, 6) == {"action": "tick", "reason": "first", "skips": 0}
    assert d.fingerprint_gate(None, "aaa", 4, 6) == {"action": "tick", "reason": "first", "skips": 0}
    # changed → tick, streak resets
    assert d.fingerprint_gate("aaa", "bbb", 3, 6) == {"action": "tick", "reason": "changed", "skips": 0}
    # unchanged → skip, streak grows
    assert d.fingerprint_gate("aaa", "aaa", 0, 6) == {"action": "skip", "reason": "unchanged", "skips": 1}
    assert d.fingerprint_gate("aaa", "aaa", 4, 6) == {"action": "skip", "reason": "unchanged", "skips": 5}
    # the safety net: the Nth consecutive skip becomes a forced full tick
    assert d.fingerprint_gate("aaa", "aaa", 5, 6) == {"action": "tick", "reason": "forced", "skips": 0}
    # force_after=1 disables skipping entirely
    assert d.fingerprint_gate("aaa", "aaa", 0, 1) == {"action": "tick", "reason": "forced", "skips": 0}


def test_pr_checks_state():
    assert d.pr_checks_state(None) is None and d.pr_checks_state([]) is None  # no CI yet → tick judges
    assert d.pr_checks_state([{"status": "COMPLETED", "conclusion": "SUCCESS"},
                              {"state": "SUCCESS"}]) == "green"
    assert d.pr_checks_state([{"conclusion": "SKIPPED"}, {"conclusion": "NEUTRAL"}]) == "green"
    assert d.pr_checks_state([{"conclusion": "SUCCESS"}, {"conclusion": "FAILURE"}]) == "red"
    assert d.pr_checks_state([{"state": "ERROR"}]) == "red"
    assert d.pr_checks_state([{"status": "IN_PROGRESS", "conclusion": None},
                              {"conclusion": "SUCCESS"}]) == "pending"
    assert d.pr_checks_state([{"state": "PENDING"}]) == "pending"
    assert d.pr_checks_state([{"conclusion": "STALE"}]) == "pending"   # not conclusively ok → never green


def test_assemble_working_set():
    now = 100_000
    issues = [
        {"number": 1, "title": "ready", "labels": ["ready-for-agent"], "updatedAt": "T1"},
        {"number": 2, "title": "blocked", "labels": ["ready-for-agent"], "updatedAt": "T2",
         "blocked_by": 1},
        {"number": 3, "title": "mine green", "labels": ["ready-for-agent"], "updatedAt": "T3"},
        {"number": 4, "title": "mine coding", "labels": ["afk-attempt/1"], "updatedAt": "T4"},
        {"number": 5, "title": "peer live", "labels": ["ready-for-agent"], "updatedAt": "T5"},
        {"number": 6, "title": "peer dead", "labels": [], "updatedAt": "T6"},
    ]
    prs = [{"number": 30, "headRefOid": "aaa", "updatedAt": "T7",
            "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}],
            "closingIssuesReferences": [{"number": 3}]}]
    claims = [
        {"number": 3, "instance": "me", "sha": "s3"},
        {"number": 4, "instance": "me", "sha": "s4"},
        {"number": 5, "instance": "peerA", "sha": "s5"},
        {"number": 6, "instance": "peerB", "sha": "s6"},
    ]
    heartbeats = {"me": now - 10, "peerA": now - 100, "peerB": now - TTL - 999}
    cfg = d.resolve_config({"epic_labels": ["epic", "prd"]})
    issues, prs, claims = _gathered(issues, prs, claims)
    ws = d.assemble_working_set(issues, prs, claims, heartbeats, "me", now, cfg)

    # frontier: the join (claimed / has_open_pr / blockers) grafted in code, titles ride along
    assert ws["frontier"]["dispatch"] == [{"number": 1, "title": "ready"}]
    reasons = {e["number"]: e["reason"] for e in ws["frontier"]["excluded"]}
    assert "1 open blocker" in reasons[2]
    assert "already claimed" in reasons[3] and "already claimed" in reasons[5]

    # mine: subclassified with PR + checks + the attempt number — Act consumes this directly
    mine = {m["number"]: m for m in ws["mine"]}
    assert mine[3]["status"] == "awaiting_turn" and mine[3]["pr"] == 30 and mine[3]["checks"] == "green"
    assert mine[4]["status"] == "no_pr" and mine[4]["pr"] is None
    assert mine[4]["attempt"] == 1 and mine[3]["attempt"] == 0
    assert "attempt_labels" not in mine[4]            # one shape: the number
    # each row carries the board phase it renders as — the tick never translates
    assert mine[3]["board_phase"] == "awaiting_turn" and mine[4]["board_phase"] == "claimed"

    # peers: live one identified and left alone; stale one carries the sha reclaim needs
    assert ws["peer_live"] == [{"number": 5, "instance": "peerA"}]
    assert ws["stale"] == [{"number": 6, "instance": "peerB", "sha": "s6"}]
    assert ws["stale_closed"] == []

    # the digest is the SAME function over the SAME observables the gate hashes
    assert ws["fingerprint"] == d.fingerprint(issues, prs, claims)
    assert ws["now"] == now

    # missing blocked_by entries default to 0 — safe: only frontier candidates need real counts
    ws2 = d.assemble_working_set([{**i, "blocked_by": 0} for i in issues], prs, claims,
                                 heartbeats, "me", now, cfg)
    assert {e["number"] for e in ws2["frontier"]["dispatch"]} == {1, 2}

    # gate.ci: local — a PR whose remote checks are RED still awaits its turn,
    # because those checks are not the gate; the landing runs the local one
    # instead (ADR-0012). Everything else about the working set is unchanged.
    red = [{**prs[0], "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "FAILURE"}]}]
    local_cfg = {**cfg, "gate": {**cfg["gate"], "ci": "local", "local_command": "make test"}}
    strict = d.assemble_working_set(issues, red, claims, heartbeats, "me", now, cfg)
    local = d.assemble_working_set(issues, red, claims, heartbeats, "me", now, local_cfg)
    assert {m["number"]: m["status"] for m in strict["mine"]} == {3: "failure", 4: "no_pr"}
    assert {m["number"]: m["status"] for m in local["mine"]} == {3: "awaiting_turn", 4: "no_pr"}
    assert {m["number"]: m["board_phase"] for m in strict["mine"]} == {3: "ci_failed", 4: "claimed"}
    assert {m["number"]: m["board_phase"] for m in local["mine"]} == {3: "awaiting_turn", 4: "claimed"}

    # the merge queue: `merge_order` is the ready rows in the order turns are
    # granted — the PR that holds the turn first, then PR number — and a `landing`
    # row carries where its `afk land` last stopped
    more = [*prs, {**prs[0], "number": 20, "closingIssuesReferences": [{"number": 4}]}]
    ws3 = d.assemble_working_set(issues, more, claims, heartbeats, "me", now, cfg)
    assert ws["merge_order"] == [3] and ws3["merge_order"] == [4, 3]
    assert all(m["stopped"] is None for m in ws3["mine"])
    turn = d.latest_turn([{"id": 1, "body": d.turn_comment(d.next_turn(
        d.single_turn(None, "me", now), stopped="awaiting_ci", head="aaa"))}])
    held = d.assemble_working_set(issues, more, claims, heartbeats, "me", now, cfg,
                                  turns={3: turn})
    rows = {m["number"]: (m["status"], m["board_phase"], m["stopped"]) for m in held["mine"]}
    assert rows == {3: ("landing", "landing", "awaiting_ci"), 4: ("awaiting_turn", "awaiting_turn", None)}
    assert held["merge_order"] == [3, 4] and held["free_slots"] == ws3["free_slots"] == 1

    # free_slots: how many more workers this tick may dispatch — the config's
    # concurrency less what I already hold, never negative
    assert ws["free_slots"] == cfg["concurrency"] - 2 == 1
    assert d.assemble_working_set(issues, prs, claims, heartbeats, "me", now,
                                  {**cfg, "concurrency": 1})["free_slots"] == 0
    assert d.assemble_working_set(issues, prs, [], heartbeats, "me", now, cfg)["free_slots"] == 3

    # a claim of mine whose issue is CLOSED (so it is absent from `issues`): status
    # `closed`, no board phase — instead of a title-less `no_pr` a tick would wait
    # on, or re-dispatch a worker for, forever
    gone = claims + [{"number": 9, "instance": "me", "sha": "s9"}]
    ws3 = d.assemble_working_set(issues, prs, gone, heartbeats, "me", now, cfg, closed=[9])
    row = {m["number"]: m for m in ws3["mine"]}[9]
    assert (row["status"], row["board_phase"], row["title"]) == ("closed", None, None)
    assert {m["number"]: m["status"] for m in ws3["mine"]} == {3: "awaiting_turn", 4: "no_pr", 9: "closed"}
    assert ws3["free_slots"] == 0                       # it still holds a slot until released

    # a STALE claim whose issue is closed is a phantom lock, not work to take over:
    # it leaves `stale` (reclaim + dispatch) for `stale_closed` (release), sha and all.
    # A live peer's claim on a closed issue is that peer's to release — untouched.
    ws4 = d.assemble_working_set(issues, prs, claims, heartbeats, "me", now, cfg, closed=[5, 6])
    assert ws4["stale"] == []
    assert ws4["stale_closed"] == [{"number": 6, "instance": "peerB", "sha": "s6"}]
    assert ws4["peer_live"] == ws["peer_live"] and ws4["mine"] == ws["mine"]

    # the lease the partition uses is the fleet's one lease: once that much time has
    # passed since the live peer's beat, it is stale too
    late = d.assemble_working_set(issues, prs, claims, heartbeats, "me", now + TTL, cfg)
    assert [s["number"] for s in late["stale"]] == [5, 6] and late["peer_live"] == []

    # several open PRs closing one issue → the highest PR number is the live attempt
    two_prs = prs + [{"number": 31, "headRefOid": "bbb", "updatedAt": "T8",
                      "statusCheckRollup": [{"status": "IN_PROGRESS", "conclusion": None}],
                      "closingIssuesReferences": [{"number": 3}]}]
    m3 = d.assemble_working_set(issues, two_prs, claims, heartbeats, "me", now, cfg)["mine"][0]
    assert (m3["pr"], m3["status"]) == (31, "awaiting_ci")
    assert local["frontier"] == strict["frontier"] and local["stale"] == strict["stale"]


def test_parse_config_yaml():
    text = """
# leading comment
ready_label: ready-for-agent          # trailing comment
epic_labels: [epic, prd]
concurrency: 5
escalate_label: "needs-#human"            # quoted value with a # inside, then a comment
gate:
  ci: required
  adversarial_verify_prompt: re-derive it
retry: 3
"""
    p = d.parse_config_yaml(text)
    assert p["ready_label"] == "ready-for-agent"
    assert p["epic_labels"] == ["epic", "prd"]
    assert p["concurrency"] == 5 and p["escalate_label"] == "needs-#human"
    assert p["gate"] == {"ci": "required", "adversarial_verify_prompt": "re-derive it"}
    assert p["retry"] == 3          # top-level scalar after a section closes it

    # a whole markdown file: the first ```yaml fence is the config
    assert d.parse_config_yaml("intro\n```yaml\nretry: 1\n```\nnotes") == {"retry": 1}

    # parsing IS validation: typo'd keys, wrong shapes, and a
    # launcher-held fact in a file are all refused, never silently ignored
    for bad in ("readylabel: x",            # unknown top-level key
                "gate:\n  cii: x",          # unknown nested key
                "retry: soon",              # wrong type
                "gate: on",                 # scalar for a section
                "  ci: required",           # indented key outside a section
                "worker_command: ckimi"):  # never a config key
        try:
            d.parse_config_yaml(bad)
            assert False, f"expected ValueError for {bad!r}"
        except ValueError:
            pass

    # a key that named the base branch fails loudly with its note — never silently
    # dropped, which would leave a config file quietly lying to its author
    # (ADR-0009/ADR-0012): no file names the branch a launch confirms (ADR-0042)
    for retired in ("merge:\n  target: trunk", "base_branch: trunk"):
        try:
            d.parse_config_yaml(retired)
            assert False, f"expected ValueError for {retired!r}"
        except ValueError as e:
            assert "not set in a file" in str(e) and "ADR-0042" in str(e)


def test_resolve_config():
    full = d.resolve_config({})
    assert full["concurrency"] == 3 and full["gate"]["ci"] == "required"
    r = d.resolve_config({"concurrency": 5, "gate": {"local_command": "make test"}})
    assert r["concurrency"] == 5
    # deep-merge keeps sibling defaults; untouched keys stay whole
    assert r["gate"]["local_command"] == "make test" and r["gate"]["ci"] == "required"
    # the settled fields ride in the canonical config, though no file sets them —
    # and the base branch has no default: "" until a launch settles it (ADR-0042)
    assert r["claim_namespace"] == "refs/afk" and "claim_namespace" not in d.CONFIG_DEFAULTS
    assert r["base_branch"] == "" and "base_branch" not in d.CONFIG_DEFAULTS
    # idempotent: resolving canonical config is a no-op
    assert d.resolve_config(r) == r


def test_a_canonical_config_is_always_accepted_and_resolving_it_again_changes_nothing():
    """Whatever `afk config`, `afk probe` or `--set` can make of a config is JSON
    the `--config` route takes back whole: the schema check refuses nothing a
    resolution produced, and a second resolution is the first."""
    rng = random.Random(113)
    words = ["", "a", "ready-for-agent", "make test && echo 'ok'", "需要人", "[a, b]", "3", "true"]
    samples = {int: lambda: rng.randrange(-2, 50), str: lambda: rng.choice(words),
               list: lambda: rng.sample(words, rng.randrange(0, 4))}
    for _ in range(200):
        partial: dict = {}
        for dotted, default in _leaves({**d.CONFIG_DEFAULTS, **d.CONFIG_SETTLED}):
            if rng.random() < 0.5:
                section, _, key = dotted.rpartition(".")
                (partial.setdefault(section, {}) if section else partial)[key] = samples[type(default)]()
        canonical = d.resolve_config(partial)
        assert set(canonical) == set(d.CONFIG_DEFAULTS) | set(d.CONFIG_SETTLED)
        assert d.resolve_config(canonical) == canonical
        assert d.resolve_config(json.loads(json.dumps(canonical))) == canonical     # as --config carries it


def test_json_config_is_held_to_the_schema_where_the_file_cannot_say_it():
    """What only JSON can spell — the file's text is typed by its key, JSON's
    values come typed — is refused by the same check, each key naming itself."""
    for bad, why in (([], "expected a JSON object"), ("retry: 3", "expected a JSON object"),
                     (None, "expected a JSON object"),
                     ({"ready_label": 3}, "'ready_label': expected a string"),
                     ({"retry": None}, "'retry': expected an integer"),
                     ({"retry": True}, "'retry': expected an integer"),
                     ({"retry": 2.0}, "'retry': expected an integer"),
                     ({"epic_labels": ["epic", 1]}, "'epic_labels': expected [a, b, ...]"),
                     ({"gate": None}, "'gate' is a section"),
                     ({"gate": ["ci"]}, "'gate' is a section"),
                     ({"gate": {"local_command": ["make"]}}, "'gate.local_command': expected a string"),
                     ({"base_branch": None}, "'base_branch': expected a string"),
                     ({"claim_namespace": ["refs/afk"]}, "'claim_namespace': expected a string"),
                     ({"merge": "x"}, "unknown key 'merge'")):
        try:
            d.resolve_config(bad)
            assert False, f"expected ValueError for {bad!r}"
        except ValueError as e:
            assert why in str(e), (bad, str(e))


def _leaves(table, prefix=""):
    for k, v in table.items():
        if isinstance(v, dict):
            yield from _leaves(v, f"{k}.")
        else:
            yield prefix + k, v


def test_override_config_types_every_key_like_the_file_does():
    """`--set key=value` reaches EVERY key the schema has, dotted for sections, and
    types it by the key's default — so there is no per-flag table to forget."""
    samples = {bool: ("false", False), int: ("123", 123), list: ("[a, b]", ["a", "b"]),
               str: ("some value", "some value")}
    seen = 0
    for dotted, default in _leaves({**d.CONFIG_DEFAULTS, **d.CONFIG_SETTLED}):
        raw, want = samples[type(default)]
        if default == want:                               # make the override visible
            raw, want = ("true", True) if isinstance(default, bool) else (raw + "x", want + "x")
        cfg = d.resolve_config({})
        assert d.override_config(cfg, [f"{dotted}={raw}"]) is cfg     # in place, returned
        section, _, key = dotted.rpartition(".")
        got = (cfg[section] if section else cfg)[key]
        assert got == want and type(got) is type(default), (dotted, got)
        # …and nothing else moved
        other = d.resolve_config({})
        (other[section] if section else other)[key] = want
        assert cfg == other, dotted
        seen += 1
    assert seen == len(list(_leaves(d.CONFIG_DEFAULTS))) + len(d.CONFIG_SETTLED) == 10

    cfg = d.resolve_config({})
    assert d.override_config(cfg, None) == d.resolve_config({}) == d.override_config(cfg, [])
    # several at once, later wins; a string value is verbatim — `=`, quotes and all
    d.override_config(cfg, ["retry=1", "retry=5", 'gate.local_command=make X="a b" && echo \'ok\''])
    assert cfg["retry"] == 5 and cfg["gate"]["local_command"] == 'make X="a b" && echo \'ok\''
    assert d.override_config(cfg, ["escalate_label="])["escalate_label"] == ""

    for bad in ("retry",                # no `=`
                "retyr=3",              # unknown key
                "gate=x",               # a section is not a value
                "gate.cii=local",       # unknown key in a section
                "nope.ci=local",        # unknown section
                "retry.ci=1",           # a scalar is not a section
                "retry=soon",           # wrong type, by the file's own rules
                "concurrency=many",
                "epic_labels=a,b",
                "=3", ""):
        try:
            d.override_config(d.resolve_config({}), [bad])
            assert False, f"expected ValueError for --set {bad!r}"
        except ValueError:
            pass


# A value as its author wrote it, and what it is read as (None: refused, by the
# key's name). Strings are written under `gate.local_command`, lists under
# `epic_labels`.
_STRINGS_AS_WRITTEN = [
    # the issue's examples: a quote at the end, a # glued to a word
    ('echo "hi"', 'echo "hi"'),
    ("pytest -k 'a or b'", "pytest -k 'a or b'"),
    ("curl http://h/#frag", "curl http://h/#frag"),
    # quotes delimit only when they wrap the whole value
    ('"pnpm build && pnpm test"', "pnpm build && pnpm test"),
    ("'single'", "single"),
    ('""', ""),
    ('" kept edges "', " kept edges "),
    ('"it\'s"', "it's"),                        # the other quote, inside a wrapped value
    ("'say \"hi\"'", 'say "hi"'),
    ('make X="a b" test', 'make X="a b" test'),         # embedded
    ("it's", "it's"),                           # an apostrophe opens nothing to be closed
    ('say "hi" twice', 'say "hi" twice'),
    # a comment starts only at whitespace-then-#, outside quotes
    ("make test   # the suite", "make test"),
    ("make test\t# the suite", "make test"),
    ("make#test", "make#test"),
    ("a#b # c", "a#b"),
    ('"needs #human"  # why', "needs #human"),
    ('"a#b"#c', None),                          # glued: no comment, so the quote closes early
    ("echo 'a # b'", "echo 'a # b'"),           # an embedded quote holds its #
    ("echo 'a # b' # c", "echo 'a # b'"),
    ("echo \"it's # in\" # c", "echo \"it's # in\""),
    # what the dialect cannot hold
    ('"a" and "b"', None),                      # opens with a quote that closes early
    ('"$PY" -m pytest', None),
    ('"unclosed', None),
    ("'", None),
    ("it's # comment or value?", None),         # a # after a quote that never closes
]
_LISTS_AS_WRITTEN = [
    ('[a, "b,c"]', ["a", "b,c"]),               # the issue's example
    ("[a, b]", ["a", "b"]),
    ("[]", []),
    ("[a, ]", ["a"]),
    ("['a, b', c]", ["a, b", "c"]),
    ('["a" , \'b\']', ["a", "b"]),
    ('[""]', [""]),
    ('[" a "]', [" a "]),
    ("[wayfinder:map, it's]", ["wayfinder:map", "it's"]),   # a quote inside an item is its own
    ('[a"b", c]', ['a"b"', "c"]),
    ("[a#b, c]   # why", ["a#b", "c"]),
    ('["a # b", c] # why', ["a # b", "c"]),
    ('["a" b, c]', None),                       # an item's quote closes before its end
    ('["a, b]', None),
    ("a, b", None),                             # not a list
]


def _refused(read, key):
    try:
        got = read()
    except ValueError as e:
        assert repr(key) in str(e), e           # the message names the key
        return
    assert False, f"expected ValueError, got {got!r}"


def test_a_config_value_is_read_as_written():
    for written, want in _STRINGS_AS_WRITTEN:
        def read():
            return d.parse_config_yaml(f"gate:\n  local_command: {written}")["gate"]["local_command"]
        if want is None:
            _refused(read, "gate.local_command")
        else:
            assert read() == want, written
    for written, want in _LISTS_AS_WRITTEN:
        def read():
            return d.parse_config_yaml(f"epic_labels: {written}\nretry: 1")["epic_labels"]
        if want is None:
            _refused(read, "epic_labels")
        else:
            assert read() == want, written

    # a comment after any other kind of value, and after a section's own line
    assert d.parse_config_yaml("retry: 4 # few\ngate:  # the gate\n  ci: local # ours") == {
        "retry": 4, "gate": {"ci": "local"}}
    for bad in ("retry: 4#few", 'retry: "4"'):
        _refused(lambda: d.parse_config_yaml(bad), "retry")
    try:
        d.parse_config_yaml("retry # how many: 4")      # no comment before the colon
        assert False, "expected ValueError"
    except ValueError as e:
        assert "unparseable line" in str(e)


def test_set_and_the_file_agree_on_a_value():
    """`--set` types a value as the file does. The two exceptions are documented
    (`override_config`): a string is verbatim, and nothing is a comment."""
    def by_set(key, written):
        section, _, leaf = key.rpartition(".")
        cfg = d.override_config(d.resolve_config({}), [f"{key}={written}"])
        return (cfg[section] if section else cfg)[leaf]

    for written, want in _LISTS_AS_WRITTEN:
        if " #" in written.split("]")[-1]:      # a comment is the file's, not the value's
            continue
        if want is None:
            _refused(lambda: by_set("epic_labels", written), "epic_labels")
        else:
            assert by_set("epic_labels", written) == want, written
    for written in ("3", " 3 ", "-1"):
        assert by_set("retry", written) == d.parse_config_yaml(f"retry: {written}")["retry"]
    for bad in ("soon", '"3"', "3#x", ""):
        _refused(lambda: by_set("retry", bad), "retry")
        _refused(lambda: d.parse_config_yaml(f"retry: {bad}"), "retry")
    # a string is the exception: the shell already unquoted it, so every character is its own
    for written, _ in _STRINGS_AS_WRITTEN:
        assert by_set("gate.local_command", written) == written


KIMI = "https://api.kimi.com/coding/"

# real `alias` output, both dialects
ALIAS_TEXT = """\
cc='claude --dangerously-skip-permissions'
ckimi='(eval "$(mytokens env kimi)" && claude --dangerously-skip-permissions)'
cdx='codex -a never -s danger-full-access'
alias ll='ls -lah'
alias cwrap="direnv exec . claude"
"""


def test_parse_aliases_and_candidates():
    al = d.parse_aliases(ALIAS_TEXT)
    assert al["cc"] == "claude --dangerously-skip-permissions"
    assert al["ll"] == "ls -lah"                       # bash's `alias n='v'` form too
    assert al["cwrap"] == "direnv exec . claude"

    got = {c["name"]: c["wraps_env"] for c in d.launch_candidates(al)}
    assert set(got) == {"cc", "ckimi", "cwrap"}        # codex/ll are not Claude Code
    assert got["ckimi"] is True and got["cwrap"] is True
    assert got["cc"] is False                          # bare `claude` carries no provider


def test_resolve_worker_command_asks_only_when_it_matters():
    # stock launcher: never asked, and the worker gets the stock command
    r = d.resolve_worker_command(None)
    assert (r["status"], r["command"], r["yolo"]) == \
        ("stock", "claude --dangerously-skip-permissions", True)
    assert d.resolve_worker_command("   ")["status"] == "stock"   # blank is not custom

    # custom provider, no answer yet → ask; NEVER silently fall back to stock
    r = d.resolve_worker_command(KIMI)
    assert (r["status"], r["command"]) == ("ask", None)
    assert KIMI in r["detail"]


def test_resolve_worker_command_checks_the_answer():
    alias_type = 'ckimi is an alias for (eval "$(mytokens env kimi)" && claude --dangerously-skip-permissions)'

    # the answer is used verbatim — never parsed, never appended to
    r = d.resolve_worker_command(KIMI, "ckimi", alias_type)
    assert (r["status"], r["command"], r["yolo"]) == ("confirmed", "ckimi", True)
    assert r["first_word"] == "ckimi"

    # the typo case: unchecked, this starts no worker at all, so the claim goes
    # PR-less into the retry ladder and escalates — on a missing letter
    r = d.resolve_worker_command(KIMI, "ckim", "")
    assert (r["status"], r["command"]) == ("unresolved", None)
    assert "ckim" in r["detail"]

    # an opaque command is honoured whatever it is — no coupling to any wrapper
    r = d.resolve_worker_command(KIMI, "direnv exec . claude --dangerously-skip-permissions",
                                 "direnv is /opt/homebrew/bin/direnv")
    assert (r["status"], r["first_word"], r["yolo"]) == ("confirmed", "direnv", True)


def test_worker_command_yolo_is_advisory_and_honest():
    # definitely missing: `claude` is its own whole story and the flag is absent
    r = d.resolve_worker_command(None, "claude", "claude is /Users/me/.local/bin/claude")
    assert (r["status"], r["yolo"]) == ("confirmed", False)

    # an alias resolution shows its whole expansion, so absence there is a fact too
    r = d.resolve_worker_command(KIMI, "cplain", "cplain is an alias for (eval x && claude)")
    assert r["yolo"] is False

    # a script could carry the flag inside — unknown, never accused of missing it
    r = d.resolve_worker_command(KIMI, "mywrap", "mywrap is /opt/bin/mywrap")
    assert r["yolo"] is None


def test_template_matches_defaults():
    # the gate on the one unavoidable hand-sync (ADR-0009): the shipped
    # template must parse clean, and every value it shows must BE the default —
    # a drifted hand-edit turns this red.
    import os
    tpl = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "..", "references", "config-template.md")
    with open(tpl) as f:
        text = f.read()
    parsed = d.parse_config_yaml(text)
    # a key with a closed set of values spells that set out, exactly
    assert " | ".join(d.GATE_CI_MODES) in text, "template does not list the gate.ci modes"
    full = d.resolve_config({})
    for k, v in parsed.items():
        if isinstance(v, dict):
            for sk, sv in v.items():
                assert full[k][sk] == sv, f"template drifted at {k}.{sk}: {sv!r}"
        else:
            assert full[k] == v, f"template drifted at {k}: {v!r}"
    # and the template shows every key the schema knows (nothing undocumented)
    assert set(parsed) == set(d.CONFIG_DEFAULTS)
    assert set(parsed["gate"]) == set(d.CONFIG_DEFAULTS["gate"]), "template missing gate keys"


def test_detect_runtime_from_env():
    assert d.detect_runtime({"QODERCN_CLI": "1"}) == "qoderclicn"
    assert d.detect_runtime({"QODERCN_CLI": "true"}) == "qoderclicn"
    assert d.detect_runtime({"QODERCN_CLI": " 1 "}) == "qoderclicn"
    assert d.detect_runtime({}) == "claude"
    assert d.detect_runtime({"QODERCN_CLI": ""}) == "claude"
    assert d.detect_runtime({"QODERCN_CLI": "0"}) == "claude"
    assert d.detect_runtime({"QODERCN_CLI": "false"}) == "claude"
    assert d.detect_runtime({"ANTHROPIC_BASE_URL": "http://x"}) == "claude"


def test_qoderclicn_runtime_is_always_stock():
    alias_type = "ckimi is an alias for (eval x && claude --dangerously-skip-permissions)"
    # no custom providers exist for qoderclicn, so it is never asked — whatever the
    # launcher's ANTHROPIC_BASE_URL says, and whatever answer is supplied (ADR-0014)
    for args in ((None,), (KIMI,), (KIMI, "ckimi", alias_type), (None, "ckim", "")):
        r = d.resolve_worker_command(*args, runtime="qoderclicn")
        assert (r["status"], r["command"], r["yolo"]) == \
            ("stock", d.WORKER_COMMAND_DEFAULT_QODERCN, True), args
        assert r["runtime"] == "qoderclicn" and r["first_word"] == "qoderclicn"
        assert r["base_url"] is None
    # every verdict names the runtime it was settled for; claude is the default
    assert d.resolve_worker_command(None)["runtime"] == "claude"
    assert d.resolve_worker_command(KIMI)["runtime"] == "claude"
    assert d.resolve_worker_command(KIMI, "ckimi", alias_type)["runtime"] == "claude"


def test_launch_candidates_stays_claude_only():
    al = {"cc": "claude --dangerously-skip-permissions",
          "qc": "qoderclicn --dangerously-skip-permissions",
          "unrelated": "vim"}
    got = {c["name"] for c in d.launch_candidates(al)}
    assert "cc" in got
    assert "qc" not in got
    assert "unrelated" not in got


def test_a_key_that_named_the_only_way_there_is_is_refused_with_its_note():
    """`claim`, `dependencies` and `worker` each had one legal value and no
    reader, so any other value was accepted and changed nothing. They are gone,
    and a file or a `--set` still carrying one is told to delete it."""
    for key, value in (("claim", "ref"), ("dependencies", "native"), ("worker", "orca")):
        assert key not in d.CONFIG_DEFAULTS
        for attempt in (lambda: d.parse_config_yaml(f"{key}: {value}"),
                        lambda: d.override_config(d.resolve_config({}), [f"{key}={value}"])):
            try:
                attempt()
                assert False, f"expected ValueError for the removed {key} key"
            except ValueError as e:
                assert f"{key!r} was removed" in str(e) and "Delete the key" in str(e)


def test_a_record_that_states_nothing_reads_as_its_kinds_blank():
    """What a reader gives back for a field the marker does not state is the
    field type's `empty`, declared with the type — so a field added to a kind
    is in every record read of it, with no second list to add it to."""
    assert d.blank_record(d.VERDICT_RECORD) == {"n": None, "phase": None, "blocked_by": [],
                                                "reason": None}
    blank = d.blank_record(d.TURN_RECORD)
    assert set(blank) == set(d.TURN_RECORD.fields)
    assert (blank["allow_no_checks"], blank["released"], blank["members"]) == (False, False, [])
    assert all(v is None for k, v in blank.items()
               if k not in ("allow_no_checks", "released", "members"))
    # each read gets its own list: one record's members are never another's
    assert d.blank_record(d.TURN_RECORD)["members"] is not blank["members"]
    bare = d.latest_turn([{"id": 3, "body": "<!--afk:turn instance=fl-1-->"}])
    assert bare == {**blank, "instance": "fl-1", "comment_id": 3}
    assert d.latest_verdict([{"body": "<!--afk:verdict-->", "url": "u"}])["blocked_by"] == []


def test_a_cause_names_its_own_step_in_the_one_table():
    """What a tick does about a classified worker is the cause's row of
    WORKER_CAUSES: `worker_step` and `batch_step` read it, and neither keeps a
    second mapping to fall out of step with it."""
    cfg = d.resolve_config({})
    for cause, row in d.WORKER_CAUSES.items():
        # the words a human reads and the step the tick takes cannot disagree
        # about whether an attempt is spent, or whether anything happens at all
        assert (row.step == "fail") == (row.action == "next_attempt"), cause
        assert (row.step == "leave") == (row.action == "leave"), cause
        if row.batch_step:
            assert d.batch_step({"cause": cause}) == row.batch_step
        if row.step in ("leave", "dispatch", "park", "nudge", "restart"):
            assert d.worker_step(CALL, _mine(4), _worker(cause), cfg) == (row.step, None)


def test_a_closed_vocabulary_and_its_table_list_the_same_words():
    """A table keyed by a Literal cannot name a word the Literal lacks — the
    gate's type checker refuses it (ADR-0039) — but it can still lack one the
    Literal has: this is that half."""
    for vocabulary, table in ((d.GateCiMode, d.GATE_CI_MODES), (d.ClaimStatus, d.BOARD_PHASE_OF),
                              (d.WorkerCause, d.WORKER_CAUSES), (d.StatusPhase, d.STATUS_PHASES),
                              (d.BatchPhase, d._BATCH_DOING), (d.TickDid, d.TICK_DID),
                              (d.TickStep, d.TICK_STEPS)):
        assert set(typing.get_args(vocabulary)) == set(table), vocabulary



# The GitHub documentation each row of CLOSING_BODIES encodes (docs.github.com):
#   KEYWORDS    "Linking a pull request to an issue" § using a keyword — the nine
#               keywords; "can be followed by colons or in uppercase"; the syntax
#               table: `KEYWORD #ISSUE-NUMBER` in the same repository,
#               `KEYWORD OWNER/REPOSITORY#ISSUE-NUMBER` in a different one, and
#               "Multiple issues: use full syntax for each issue".
#   REFERENCES  "Autolinked references and URLs" § Issues and pull requests — what
#               "a reference to the issue" after a keyword may be: the URL, `#26`,
#               `GH-26`, `Username/Repository#26`.
#   CODE        "Basic writing and formatting syntax" § Quoting code — "the text
#               within the backticks will not be formatted" — and "Creating and
#               highlighting code blocks" § Fenced code blocks.
#   COMMENTS    "Basic writing and formatting syntax" § Hiding content with
#               comments — an HTML comment is hidden from the rendered Markdown.
#   PROSE       No rule exempts a quote (§ Quoting text formats what it quotes) or
#               a sentence's meaning: the keyword and its reference are all
#               GitHub reads.
CLOSING_BODIES = [
    # (body, the issues of acme/widgets it closes, the rule)
    ("Closes #7\n", [7], "KEYWORDS"),
    ("close #1 closed #2 fix #3 fixes #4 fixed #5 resolve #6 resolves #7 resolved #8",
     [1, 2, 3, 4, 5, 6, 7, 8], "KEYWORDS"),
    ("Closes: #10, CLOSES #11 and CLOSES: #12.", [10, 11, 12], "KEYWORDS"),
    ("Resolves #10, resolves #123", [10, 123], "KEYWORDS"),
    ("Closes #10, #123 and #124", [10], "KEYWORDS"),
    ("see #7; encloses #8; prefix #9; closes#10; closes 11", [], "KEYWORDS"),
    ("Fixes acme/widgets#100", [100], "KEYWORDS"),
    ("Fixes Acme/Widgets#100", [100], "KEYWORDS"),
    ("Fixes octo-org/octo-repo#100", [], "KEYWORDS"),
    ("Fixes acme/widgets-old#100, fixes other/widgets#101", [], "KEYWORDS"),
    ("Resolves #10, resolves octo-org/octo-repo#100", [10], "KEYWORDS"),
    ("Closes https://github.com/acme/widgets/issues/26", [26], "REFERENCES"),
    ("Closes https://github.com/jlord/sheetsee.js/issues/26", [], "REFERENCES"),
    ("Closes https://github.com/acme/widgets/labels/26", [], "REFERENCES"),
    ("Fixes GH-26", [26], "REFERENCES"),
    ("Run `gh pr create --body 'Closes #3'` to open it. Fixes #4", [4], "CODE"),
    ("``Closes #3 with a ` in it`` and fixes #4", [4], "CODE"),
    ("a stray ` then Closes #3\n\nand another ` much later", [3], "CODE"),
    ("```\nCloses #3\n```\nCloses #4", [4], "CODE"),
    ("```bash\ngit commit -m 'Fixes #3'\n```\n", [], "CODE"),
    ("~~~\nCloses #3\n```\nCloses #5\n~~~\nCloses #4", [4], "CODE"),
    ("````\n```\nCloses #3\n```\nCloses #5\n````\nCloses #4", [4], "CODE"),
    ("> ```\n> Closes #3\n> ```\nCloses #4", [4], "CODE"),
    ("Closes #4\n```\nCloses #3, never closed", [4], "CODE"),
    ("<!-- Closes #3 -->\nCloses #4", [4], "COMMENTS"),
    ("<!--\ntemplate: write `Closes #3`\n```\n-->\nCloses #4", [4], "COMMENTS"),
    ("<!--afk:turn fixes #3-->", [], "COMMENTS"),
    ("```\n<!--\n```\nCloses #4\n<!-- -->", [4], "CODE"),
    ("> Closes #3", [3], "PROSE"),
    ("This does not fix #3", [3], "PROSE"),
]


def test_issues_closed_by_reads_closing_references_as_github_documents_them():
    for body, numbers, rule in CLOSING_BODIES:
        assert d.issues_closed_by(body, None, "acme/widgets") == \
            [{"number": n} for n in numbers], (rule, body)


def test_issues_closed_by_adds_the_body_to_the_links_github_made():
    assert d.issues_closed_by(None, None, "o/r") == []
    assert d.issues_closed_by("Closes #7", [{"number": 7}, {"number": 3}], "o/r") == \
        [{"number": 3}, {"number": 7}]
    assert d.issues_closed_by("`Closes #7`", [{"number": 7}], "o/r") == [{"number": 7}]


def test_the_pr_body_the_worker_prompt_asks_for_closes_the_workers_issue():
    prompt = d.render_worker_prompt(_prompt_template(), "fresh", PROMPT_FIELDS)
    body = re.search(r'gh pr create .*?--body "(.*?)"', prompt, re.S)[1]
    assert d.issues_closed_by(body, None, PROMPT_FIELDS["repo"]) == \
        [{"number": PROMPT_FIELDS["n"]}]


def test_base_refusal_lets_the_base_branch_change_only_while_nothing_stands_on_it():
    heads = ["main", "release"]
    assert d.base_refusal("main", None, heads, 3, ["peer"]) is None      # the first one settled
    assert d.base_refusal("main", "main", heads, 3, ["peer"]) is None    # confirmed, not changed
    assert d.base_refusal("release", "main", heads, 0, []) is None
    assert "no branch 'gone'" in d.base_refusal("gone", None, heads, 0, [])
    assert "2 claim(s)" in d.base_refusal("release", "main", heads, 2, [])
    said = d.base_refusal("release", "main", heads, 0, ["b", "a"])
    assert "a, b" in said and "'main'" in said and "'release'" in said
