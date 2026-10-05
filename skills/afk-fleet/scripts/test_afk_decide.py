#!/usr/bin/env python3
"""
Fixture tests for the afk-fleet decision core — pure, no git/gh/network.
Run: python3 test_afk_decide.py   (plain asserts, no test-framework dependency)

These cover the correctness-critical verdicts (esp. classify_claims: the
mine/peer_live/stale partition whose wrong answer silently corrupts state).
"""
import json
import os
import re
import shlex

import afk_decide as d

TTL = d.CONFIG_DEFAULTS["claim_lease_ttl_seconds"]  # the default lease


def test_select_frontier():
    issues = [
        {"number": 101, "labels": ["ready-for-agent"], "claimed": False, "has_open_pr": False, "open_blockers": 0},
        {"number": 102, "labels": ["ready-for-agent"], "open_blockers": 1},
        {"number": 103, "labels": ["ready-for-agent", "epic"], "open_blockers": 0},
        {"number": 104, "labels": ["ready-for-agent"], "claimed": True},
        {"number": 105, "labels": ["ready-for-agent"], "has_open_pr": True},
        {"number": 107, "labels": []},
    ]
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
    assert d.heartbeat_due(None, 1000, TTL) is True
    assert d.heartbeat_due(1000, 1000 + TTL // 3, TTL) is False
    assert d.heartbeat_due(1000, 1000 + TTL // 3 + 2, TTL) is True


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


def test_subclassify_pr():
    # → (status, board_phase): what the tick does next, and what the board shows
    assert d.subclassify_pr(False, None, "required") == ("no_pr", "claimed")
    assert d.subclassify_pr(True, "green", "required") == ("awaiting_turn", "awaiting_turn")
    assert d.subclassify_pr(True, "red", "required") == ("failure", "ci_failed")
    assert d.subclassify_pr(True, "pending", "required") == ("awaiting_ci", "pr_open")
    # no checks at all is not "checks pending": nothing is running, so waiting
    # would park the claim forever in a repo with no CI. It goes to `afk turn`,
    # whose `no_checks` outcome asks the tick
    assert d.subclassify_pr(True, None, "required") == ("awaiting_turn", "awaiting_turn")
    # with no PR the checks are nobody's: stale rollup data cannot invent a status
    assert d.subclassify_pr(False, "green", "required") == ("no_pr", "claimed")

    # gate.ci: local — there are no checks to WAIT on, because gating is an action
    # the landing takes (ADR-0012). Any open PR is awaiting its turn, and a red
    # remote run (the repo's own on:push CI, which the fleet does not gate on)
    # must never park the claim in `failure` forever.
    for checks in ("green", "red", "pending", None):
        assert d.subclassify_pr(True, checks, "local") == ("awaiting_turn", "awaiting_turn"), checks
    assert d.subclassify_pr(False, None, "local") == ("no_pr", "claimed")

    # every board phase it can produce is one the board renders — the tick never translates
    for ci in d.GATE_CI_MODES:
        for has_pr in (True, False):
            for checks in ("green", "red", "pending", None):
                assert d.subclassify_pr(has_pr, checks, ci)[1] in d.STATUS_PHASES

    # CLAIM_STATUSES is exactly what it can return: no status the docs were never
    # held to, and none listed that cannot happen
    seen = {d.subclassify_pr(has_pr, checks, ci, closed=closed, landing=landing)[0]
            for ci in d.GATE_CI_MODES for has_pr in (True, False)
            for checks in ("green", "red", "pending", None)
            for closed in (True, False) for landing in (True, False)}
    assert seen == set(d.CLAIM_STATUSES)

    # the issue is CLOSED but the claim is still mine — its worker landed the PR, or
    # an `afk close` crashed before releasing. Nothing else about it matters, and
    # there is no board to write: the only thing left to do is release.
    for ci in d.GATE_CI_MODES:
        for has_pr in (True, False):
            assert d.subclassify_pr(has_pr, "red", ci, closed=True) == ("closed", None)

    # a PR that holds the landing turn: whatever the checks say, in either mode, the
    # claim is `landing` — its worker is at it, and red or pending checks on a head
    # the sync just pushed are the landing's own to wait out (ADR-0027)
    for ci in d.GATE_CI_MODES:
        for checks in ("green", "red", "pending", None):
            assert d.subclassify_pr(True, checks, ci, landing=True) == \
                ("landing", "landing"), (ci, checks)
        assert d.subclassify_pr(True, "green", ci, closed=True, landing=True)[0] == "closed"
        assert d.subclassify_pr(False, None, ci, landing=True)[0] == "no_pr"
    assert {"landing", "awaiting_turn"} <= set(d.STATUS_PHASES)


def test_turn_record_round_trips_and_is_held_only_by_the_claims_owner():
    body = d.turn_comment("fl-1", 1234)
    # the marker leads, the same facts follow for a human
    assert body.startswith("<!--afk:turn instance=fl-1 at=1234-->\n")
    assert "holds the landing turn" in body and "`fl-1`" in body and "`afk land`" in body
    rec = d.latest_turn([{"id": 7, "body": "a human note"}, {"id": 8, "body": body}])
    assert rec == {"instance": "fl-1", "at": 1234, "verified": None, "allow_no_checks": False,
                   "stopped": None, "head": None, "comment_id": 8}
    assert d.latest_turn([]) is None and d.latest_turn([{"id": 1, "body": "x"}]) is None
    assert d.latest_turn([{"id": 1, "body": None}]) is None and d.latest_turn(None) is None

    # the tick's two judgments travel on the marker, and so does where the landing stopped
    full = d.turn_comment("fl-1", 2000.9, verified="v" * 40, allow_no_checks=True,
                          stopped="awaiting_ci", head="h" * 40)
    assert full.startswith(f"<!--afk:turn instance=fl-1 at=2000 verified={'v' * 40} "
                           f"allow_no_checks=1 stopped=awaiting_ci head={'h' * 40}-->\n")
    assert "stopped with `awaiting_ci` on `hhhhhhhhhhhh`" in full
    rec2 = d.latest_turn([{"id": 8, "body": body}, {"id": 9, "body": full},
                          {"id": 10, "body": "<!--afk:turn at=3-->"}])       # names nobody: no record
    assert rec2 == {"instance": "fl-1", "at": 2000, "verified": "v" * 40, "allow_no_checks": True,
                    "stopped": "awaiting_ci", "head": "h" * 40, "comment_id": 9}
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
    assert set(d.LAND_WAITS) < set(d.LAND_OUTCOMES) and "merged" not in d.LAND_WAITS
    for check, word in ((d.land_outcome, "granted"), (d.turn_outcome, "merged"),
                        (d.land_outcome, "handed_back")):
        try:
            check(word)
            raise AssertionError(word)
        except ValueError:
            pass


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
        return {"number": number, "status": status, "pr": pr}

    rows = [row(1, "awaiting_turn", 30), row(2, "no_pr", None), row(3, "awaiting_turn", 10),
            row(4, "landing", 40), row(5, "awaiting_ci", 5), row(6, "failure", 6),
            row(7, "closed", None)]
    assert d.turn_order(rows) == [4, 3, 1]
    assert d.turn_order(reversed(rows)) == [4, 3, 1]                # not input order
    assert d.turn_order([]) == [] and d.turn_order(rows[1:2]) == []



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
    assert d.CONFIG_DEFAULTS["claim_namespace"] in d.CLAIM_NAMESPACES
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

    # merge.strategy is a `gh pr merge --<strategy>` flag: a closed set, checked
    # here rather than discovered at the first merge
    for strategy in d.MERGE_STRATEGIES:
        d.validate_config(d.resolve_config({"merge": {"strategy": strategy}}))
    assert d.CONFIG_DEFAULTS["merge"]["strategy"] in d.MERGE_STRATEGIES
    for bad in ("fast-forward", "", "Squash", None):
        try:
            d.validate_config(d.resolve_config({"merge": {"strategy": bad}}))
            assert False, f"expected ValueError for merge.strategy {bad!r}"
        except ValueError as e:
            assert "merge.strategy" in str(e)


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

    # a timed-out run is RED, never green-by-default, whatever it exited with
    r = d.gate_verdict(0, "hung", timed_out=True)
    assert r["status"] == "red" and r["timed_out"] is True


def test_a_recorded_gate_run_counts_only_for_the_commit_and_command_it_ran():
    """ADR-0026. The merge skips its own run of the local gate on ONE proof: a green
    run of the command configured now, on a committed tree, at the head that would
    land. Everything else is void — and void means the merge gates."""
    rec = d.gate_record("abc123", "make test", True, 1700000000.9)
    assert rec == {"head": "abc123", "command": "make test", "clean": True, "at": 1700000000}
    assert d.gate_record_void(rec, "abc123", "make test") is None

    for record, head, command, why in (
            (None, "abc123", "make test", "no green run"),
            ({}, "abc123", "make test", "no green run"),
            ("green", "abc123", "make test", "no green run"),        # not a record at all
            ({**rec, "head": ""}, "", "make test", "no green run"),
            (rec, "def456", "make test", "not on the head that would land"),   # synced, or committed
            (rec, "abc123", "make test -short", "different command"),
            (rec, "abc123", " make test", "different command"),      # verbatim, not "close enough"
            ({**rec, "clean": False}, "abc123", "make test", "uncommitted or untracked"),
            ({k: v for k, v in rec.items() if k != "clean"}, "abc123", "make test", "uncommitted")):
        assert why in d.gate_record_void(record, head, command), (record, head, command)
    # the reason names both heads, so a human reading a tick's result can see what moved
    void = d.gate_record_void(rec, "def456", "make test")
    assert "abc123" in void and "def456" in void

    # on unless a repo turns it off — and so never an error in `required` mode,
    # which has no landing run of the command to skip: the key is inert there
    assert d.CONFIG_DEFAULTS["gate"]["trust_recorded_run"] is True
    for ci in d.GATE_CI_MODES:
        for trust in (True, False):
            d.validate_config(d.resolve_config(
                {"gate": {"ci": ci, "local_command": "make test", "trust_recorded_run": trust}}))

    # the line a worker gates with: the tool, carrying the command — quoted for a shell
    assert d.gate_command("/s/afk.py", " make test ") == \
        """/s/afk.py gate --config '{"gate": {"local_command": "make test"}}'"""
    import shlex
    odd = d.gate_command("/my skills/afk.py", """pnpm test -- --grep 'a "b"'""")
    argv = shlex.split(odd)
    assert argv[:3] == ["/my skills/afk.py", "gate", "--config"] and len(argv) == 4
    assert json.loads(argv[3]) == {"gate": {"local_command": """pnpm test -- --grep 'a "b"'"""}}
    assert "no gate.local_command configured" in d.gate_command("/s/afk.py", "  ")


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


def test_the_verdict_marker_round_trips_through_its_parser():
    # one writer, one reader: whatever `verdict_marker` spells, the parser reads back
    for phase in d.VERDICT_PHASES:
        got = d.parse_verdict_marker(d.verdict_marker(12, phase, [3, 4], "needs pages from #3"))
        assert got == {"found": True, "n": 12, "phase": phase, "blocked_by": [3, 4],
                       "reason": "needs pages from #3"}, phase
        bare = d.parse_verdict_marker(d.verdict_marker(7, phase))
        assert (bare["n"], bare["phase"], bare["blocked_by"], bare["reason"]) == (7, phase, [], None)
    # what the worker is shown is that same spelling, with placeholders
    shown = d.verdict_marker_format(31)
    assert shown == ("<!--afk:verdict n=31 phase=<already-satisfied|blocked|giving-up> "
                     "[blocked_by=<csv of issue numbers>] [reason=<short>]-->")


def test_parse_verdict_marker():
    # valid, every field; reason (last) keeps its spaces
    body = ("<!--afk:verdict n=12 phase=blocked blocked_by=3,4 reason=needs pages from #3-->\n"
            "Blocked on #3 and #4 — no PRs there yet.")
    p = d.parse_verdict_marker(body)
    assert p["found"] is True and p["n"] == 12 and p["phase"] == "blocked"
    assert p["blocked_by"] == [3, 4]
    assert p["reason"] == "needs pages from #3"

    # already-satisfied, no blocked_by / reason
    p = d.parse_verdict_marker("<!--afk:verdict n=7 phase=already-satisfied-->\nEmpty diff vs base.")
    assert p["phase"] == "already-satisfied" and p["blocked_by"] == [] and p["reason"] is None

    # giving-up, tolerant of extra whitespace around the marker + tokens
    assert d.parse_verdict_marker("<!--  afk:verdict   phase=giving-up  -->")["phase"] == "giving-up"

    # missing marker → None (a plain human comment is not a verdict)
    assert d.parse_verdict_marker("just a normal comment, no marker") is None
    assert d.parse_verdict_marker("") is None
    assert d.parse_verdict_marker(None) is None

    # malformed: marker present but no phase → found True, phase None. Parse is
    # lenient by design; classify_no_pr treats a None/unknown phase as failed, and
    # whether to trust the marker at all stays the tick's call.
    p = d.parse_verdict_marker("<!--afk:verdict n=9-->")
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


def _no_pr(progress, terminal, idle, verdict=None, blockers=None):
    """classify_no_pr with the terminal as the only recency signal, `idle` s ago."""
    r = d.classify_no_pr(progress, terminal, idle, verdict, blockers or {}, NOW, GRACE)
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

    # and the reading is what classify_no_pr takes: busy → coding, a stop within
    # grace → coding, a stop past grace → routed on the verdict
    for row, outcome in ((_ps("working", 900, 5), "coding"), (_ps("done", 10, 10), "coding"),
                         (_ps("done", GRACE, 1), "idle_stalled"), (None, "dead")):
        t, idle, _ = _reading(row)
        assert _no_pr(ZERO, t, idle)[0] == outcome, row


def test_classify_no_pr_coding_needs_a_live_signal():
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


def test_classify_no_pr_idle_seconds_is_the_most_recent_sign_of_life():
    def idle(progress, terminal_idle):
        return d.classify_no_pr(progress, "idle", terminal_idle, None, {}, NOW, GRACE)

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


def test_classify_no_pr_routes_idle_workers_on_their_verdict():
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
    # no verdict at all, a not-found verdict → it declared nothing: stalled, not failed
    assert _no_pr(ZERO, "idle", 600, None) == ("idle_stalled", "nudge")
    assert _no_pr(ZERO, "idle", 600, {"found": False}) == ("idle_stalled", "nudge")

    # dead: no live worker/terminal at all → orphan path, whatever it left behind
    assert _no_pr({**ZERO, "commits_ahead": 3}, "none", 5, _verdict("blocked", [1])) == \
        ("dead", "orphan")


def test_classify_no_pr_nudges_a_silent_worker_once_before_failing_it():
    """A worker idle past grace with NO verdict stopped without an outcome — most
    often it is waiting on a question nobody will answer. Failing it discards its
    work and sends a fresh worker into the same wall, so it is nudged first; a
    nudge is spent once (ADR-0018)."""
    def silent(idle, **nudge):
        r = d.classify_no_pr(ZERO, "idle", idle, None, {}, NOW, GRACE, **nudge)
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
        r = d.classify_no_pr(ZERO, terminal, 600, verdict, {}, NOW, GRACE, **nudge)
        return r["outcome"], r["action"]
    assert routed(_verdict("giving-up")) == ("idle_failed", "next_attempt")
    assert routed(_verdict("already-satisfied"), nudged_at=NOW - 9000) == ("idle_done", "close_release")
    assert routed(None, terminal="none") == ("dead", "orphan")

    # a landing turn is the same kind of sign of life (ADR-0027): one grace period
    # to start on it, then the same nudge → failure path — never a parked queue
    def turn(idle, at, **more):
        r = d.classify_no_pr({**ZERO, "commits_ahead": 3}, "idle", idle, None, {}, NOW, GRACE,
                             turn_at=at, **more)
        return r["outcome"], r["action"], r["idle_seconds"]
    assert turn(9000, NOW - 10) == ("coding", "leave", 10)
    assert turn(9000, NOW - GRACE) == ("idle_stalled", "nudge", GRACE)
    assert turn(9000, NOW - 2 * GRACE, nudged_at=NOW - GRACE) == \
        ("idle_failed", "next_attempt", GRACE)
    r = d.classify_no_pr(ZERO, "none", None, None, {}, NOW, GRACE, turn_at=NOW - 10)
    assert (r["outcome"], r["action"]) == ("dead", "orphan")      # a gone terminal is still dead
    # a worker whose landing stopped FOR THE TICK (CI, a verify, absent checks) is
    # waiting on the tick, not silent: never nudged, never failed, however long ago
    for stopped in d.LAND_WAITS:
        assert turn(9000, NOW - 5 * GRACE, turn_stopped=stopped)[:2] == ("coding", "leave"), stopped
        assert turn(9000, NOW - 5 * GRACE, turn_stopped=stopped, nudged_at=NOW - 3 * GRACE)[:2] == \
            ("coding", "leave")
    # …while one that stopped on something that is ITS to fix is silent like any other
    for stopped in ("conflict", "gate_red"):
        assert turn(9000, NOW - GRACE, turn_stopped=stopped)[:2] == ("idle_stalled", "nudge")
    # what the worker declared still wins, and so does a gone terminal
    r = d.classify_no_pr(ZERO, "idle", 9000, _verdict("giving-up"), {}, NOW, GRACE,
                         turn_at=NOW - 9000, turn_stopped="awaiting_ci")
    assert (r["outcome"], r["action"]) == ("idle_failed", "next_attempt")
    r = d.classify_no_pr(ZERO, "none", None, None, {}, NOW, GRACE, turn_stopped="awaiting_ci")
    assert (r["outcome"], r["action"]) == ("dead", "orphan")


def test_stall_reason_carries_where_the_worker_stopped():
    screen = ["", "● 要我按这段说明把 #41 从头做到开 PR 吗？", "   ", "x" * 500, "❯ "]
    assert d.stall_tail(screen) == ["● 要我按这段说明把 #41 从头做到开 PR 吗？", "x" * 200, "❯"]
    assert d.stall_tail([str(i) for i in range(100)], limit=3) == ["97", "98", "99"]
    assert d.stall_tail(None) == []
    reason = d.stall_reason("idle with no outcome\n", screen)
    assert reason.startswith("idle with no outcome\n\nThe previous worker stopped")
    assert "```\n● 要我按这段说明把 #41 从头做到开 PR 吗？\n" in reason and reason.endswith("❯\n```")
    assert d.stall_reason("idle with no outcome", []) == "idle with no outcome"   # nothing to add
    # the nudge is one short line — a long one is the very paste it is sent to break
    for text in (d.nudge_text(), d.nudge_text("/w/.git/afk-worker-prompt.md")):
        assert "\n" not in text and len(text) < 400 and "afk:verdict" in text
    assert "/w/.git/afk-worker-prompt.md" in d.nudge_text("/w/.git/afk-worker-prompt.md")


def test_classify_no_pr_blocked_routes_on_the_blockers_real_state():
    def blocked(named, states, progress=ZERO):
        return d.classify_no_pr(progress, "idle", 600, _verdict("blocked", named), states,
                                NOW, GRACE)

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
    assert d.classify_no_pr(ZERO, "idle", 600, _verdict("giving-up", [42]), {42: "open"},
                            NOW, GRACE)["pending_blockers"] == []


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
        row = {"number": 1, "labels": labels}
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
    return claims, heartbeats


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
    r = d.plan_takeover([{"number": 4, "instance": "ghost", "sha": "s4"}], {}, "ghost-target",
                        "me", now, TTL)
    assert r["action"] == "none"                     # …but only under its real id
    r = d.plan_takeover([{"number": 4, "instance": "ghost", "sha": "s4"}], {}, "ghost",
                        "me", now, TTL)
    assert r["action"] == "take" and r["fresh"] is False and r["heartbeat_age"] is None


def test_branch_regex_and_candidates():
    heads = [
        "master",
        "sunfmin/issue-9-continuation",          # orca's real shape: <user>/ prefix
        "issue-9-continuation-second-try",       # no prefix, same issue
        "sunfmin/issue-90-calibration",          # a DIFFERENT issue that starts with 9
        "sunfmin/issue-10-takeover",
        "sunfmin/feature/issue-9-nope",          # slug never spans a slash
    ]
    got = d.branch_candidates(heads, "issue-{number}-{slug}", 9)
    assert got == ["issue-9-continuation-second-try", "sunfmin/issue-9-continuation"], got

    # the number is the one field that is NOT a wildcard: 9 never matches 90
    assert d.branch_candidates(heads, "issue-{number}-{slug}", 90) == \
        ["sunfmin/issue-90-calibration"]
    assert d.branch_candidates(heads, "issue-{number}-{slug}", 11) == []
    assert d.branch_candidates(None, "issue-{number}-{slug}", 9) == []

    # a pattern with regex metacharacters in its literal part is matched literally
    assert d.branch_regex("wip.{number}", 9).match("wip.9")
    assert not d.branch_regex("wip.{number}", 9).match("wipX9")
    # a pattern with no {slug} still works, and a bare {number} needs the whole name
    assert d.branch_candidates(["afk/9", "afk/91"], "afk/{number}", 9) == ["afk/9"]


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

    body = d.escalation_comment("  the gate needs a secret CI has and I do not  ", 2, pr=31)
    assert "escalated to a human" in body and "after 2 retries" in body and "#31" in body
    assert body.endswith("the gate needs a secret CI has and I do not")
    assert "after 1 retry)" in d.escalation_comment("x", 1)
    assert "without a retry" in d.escalation_comment("x", 0) and "PR" not in d.escalation_comment("x", 0)


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


PACE_CFG = {"busy_interval_seconds": 90, "idle_interval_seconds": 1500,
            "idle_ticks_before_sleep": 3, "claim_lease_ttl_seconds": TTL,
            "fingerprint_gate": True, "force_tick_after_skips": 6}


def test_pace():
    cfg = PACE_CFG
    # did work → busy
    assert d.pace(True, 0, 0, cfg) == 90
    # in-flight → busy, and under the ttl/2 cap, however long the streak
    assert d.pace(False, 2, 9, cfg) == 90
    # idle but recently active (streak < threshold) → stay busy for stragglers
    assert d.pace(False, 0, 2, cfg) == 90
    # idle past threshold → idle interval
    assert d.pace(False, 0, 3, cfg) == 1500
    # ttl/2 cap actually bites when the interval would exceed it while holding a claim
    assert d.pace(False, 1, 0, {**cfg, "busy_interval_seconds": 999999}) == TTL // 2
    # pace re-applies no default: a config arriving here is canonical
    try:
        d.pace(False, 0, 9, {})
        assert False, "expected a KeyError: pace must not re-apply defaults"
    except KeyError:
        pass


FACTS = {"instance": "fl-1", "worker_command": "ckimi --yolo"}


def test_cycle_state_is_validated_not_guessed():
    # the first cycle is handed the run's two facts, and the state carries them from then on
    first = d.cycle_state(None, **FACTS)
    assert first == {**d.CYCLE_START, **FACTS} == d.cycle_state("", **FACTS)
    assert d.cycle_state(first) == first                      # …so a later cycle passes neither
    assert d.cycle_state(first, **FACTS) == first             # the same ones again are harmless
    st = {"fingerprint": "abc", "skips": 2, "empty_streak": 1, "in_flight": 0,
          "frontier_remaining": 4, "unsettled": True, "boards": {"7": "0a1b2c3d"}, **FACTS}
    assert d.cycle_state(st) == st
    assert d.cycle_state(None, **FACTS)["boards"] is not d.CYCLE_START["boards"]   # never shared
    # a caller that mangled the state must hear so — run on zeros, a fleet holding
    # claims would be paced as if it held none; without the facts it could start no worker
    no_facts = {k: v for k, v in st.items() if k not in FACTS}
    for bad in ({"fingerprint": "abc"}, {**st, "extra": 1}, [], "abc", {**st, "skips": "x"},
                no_facts, {**no_facts, "instance": "fl-1"}, {**st, "worker_command": ""},
                {**st, "instance": None}):
        try:
            d.cycle_state(bad)
            assert False, f"expected ValueError for {bad!r}"
        except ValueError:
            pass
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
    cfg, st = PACE_CFG, d.cycle_state(None, **FACTS)

    def ticked(state, **did):
        return d.cycle_ticked(state, _did(**did), cfg)

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
                          cfg, left="def", boards={4: "0a1b2c3d"})["state"]
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
    asked = d.cycle_ticked(r["state"], _did(), cfg, judgments=2)
    assert (asked["sleep_seconds"], asked["state"]["unsettled"]) == (0, True)
    assert asked["state"]["empty_streak"] == 0 and "2 judgments open" in asked["progress"]
    failed = d.cycle_ticked(r["state"], _did(), cfg, errors=1)
    assert (failed["sleep_seconds"], failed["state"]["unsettled"]) == (90, True)
    assert "1 error;" in failed["progress"]
    assert ticked(asked["state"])["state"]["unsettled"] is False     # a clean tick settles it


def test_cycle_drained_folds_the_stop_and_schedules_nothing():
    st = d.cycle_ticked(d.cycle_state(None, **FACTS), _did(in_flight=3), PACE_CFG)["state"]
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


def test_cycle_wake_gates_beats_and_paces_a_skipped_cycle():
    cfg, st = PACE_CFG, d.cycle_state(None, **FACTS)
    first = d.cycle_wake(st, "aaa", cfg)
    assert (first["action"], first["reason"]) == ("tick", "first")
    assert first["state"]["fingerprint"] == "aaa"
    # a tick owes its sleep to cycle_ticked, not to the gate
    assert set(first) == {"action", "reason", "state"}

    # unchanged + idle fleet → skip; each such skip is itself an empty cycle
    idle = d.cycle_ticked(first["state"], _did(), cfg)["state"]
    s1 = d.cycle_wake(idle, "aaa", cfg)
    assert (s1["action"], s1["reason"], s1["heartbeat"]) == ("skip", "unchanged", False)
    assert (s1["state"]["skips"], s1["state"]["empty_streak"], s1["sleep_seconds"]) == (1, 2, 90)
    assert s1["progress"] == "nothing moved; 0 in flight, 0 left on the frontier"
    s2 = d.cycle_wake(s1["state"], "aaa", cfg)
    assert (s2["state"]["empty_streak"], s2["sleep_seconds"]) == (3, 1500)

    # unchanged while HOLDING claims → skip, but beat, stay busy, and never count as empty
    held = d.cycle_ticked(first["state"], _did(in_flight=2), cfg)["state"]
    h = d.cycle_wake(held, "aaa", cfg)
    assert (h["action"], h["heartbeat"], h["sleep_seconds"]) == ("skip", True, 90)
    assert h["state"]["empty_streak"] == 0
    long_busy = {**cfg, "busy_interval_seconds": 999999}
    assert d.cycle_wake(held, "aaa", long_busy)["sleep_seconds"] == TTL // 2     # the lease cap
    # frontier left over (the tick had no free slot) is not empty either
    waiting = d.cycle_ticked(first["state"], _did(frontier_remaining=3), cfg)["state"]
    assert d.cycle_wake(waiting, "aaa", cfg)["state"]["empty_streak"] == 0

    # changed → tick; the Nth consecutive skip → a forced tick
    moved = d.cycle_wake(s2["state"], "bbb", cfg)
    assert (moved["action"], moved["reason"], moved["state"]["skips"]) == ("tick", "changed", 0)
    forced = d.cycle_wake({**idle, "skips": 5}, "aaa", cfg)
    assert (forced["action"], forced["reason"], forced["state"]["skips"]) == ("tick", "forced", 0)
    # the streak survives a tick decision: only cycle_ticked resets it
    assert moved["state"]["empty_streak"] == 3

    # the last tick left a judgment open or met an error: unchanged is not a skip
    owed = d.cycle_wake({**idle, "unsettled": True, "skips": 2}, "aaa", cfg)
    assert (owed["action"], owed["reason"], owed["state"]["skips"]) == ("tick", "unsettled", 0)

    # a wake that arrived while the last cycle was running: the digest that cycle
    # kept may already hold what the wake announced, so unchanged is not a skip
    woke = d.cycle_wake({**idle, "skips": 2}, "aaa", cfg, woke=True)
    assert (woke["action"], woke["reason"], woke["state"]["skips"]) == ("tick", "wake", 0)
    assert d.cycle_wake(idle, "bbb", cfg, woke=True)["reason"] == "changed"

    # fingerprint_gate off → always a tick, and nothing was gathered to digest
    off = d.cycle_wake({**idle, "skips": 3}, None, {**cfg, "fingerprint_gate": False})
    assert (off["action"], off["reason"]) == ("tick", "gate_off")
    assert off["state"]["fingerprint"] == "aaa" and off["state"]["skips"] == 0


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


def _mine(n, status="no_pr", **more):
    return {"number": n, "status": status, "pr": None, "stopped": None, **more}


def test_a_tick_asks_after_waiting_workers_and_grants_one_turn():
    mine = [_mine(1), _mine(2, "awaiting_turn", pr=22), _mine(3, "landing", pr=21),
            _mine(4, "landing", pr=20, stopped="awaiting_ci"), _mine(5, "awaiting_ci", pr=23),
            _mine(6, "failure", pr=24), _mine(7, "closed"),
            _mine(8, "landing", pr=19, stopped="gate_red")]
    # `afk no-pr` is asked about a PR-less claim, and a landing one that did not stop for the tick
    assert d.asks_after(mine) == [1, 3, 8]

    # the turn goes to the head of the merge queue, and only when its worker is not at it
    assert d.turn_due(mine, []) is None
    assert d.turn_due(mine, [2]) == 2                             # awaiting its turn
    assert d.turn_due(mine, [4, 2]) == 4                          # stopped for the tick: again
    assert d.turn_due(mine, [3, 2]) is None                       # landing: leave it, and #2 waits
    assert d.turn_due(mine, [8, 2]) is None                       # fixing a red gate in place


def test_turn_step_routes_every_outcome_or_returns_the_judgment():
    cfg = d.resolve_config({})
    verify = d.resolve_config({"gate": {"adversarial_verify": True,
                                        "adversarial_verify_prompt": "be harsh"}})

    def step(outcome, config=cfg):
        return d.turn_step(CALL, {"issue": 4, "pr": 30, "head": "abc123", "outcome": outcome},
                           config)

    assert step("granted") == ("granted", None)
    for outcome in ("waiting", "landing", "awaiting_ci"):
        assert step(outcome) == ("leave", None), outcome
    routed = {"granted", "waiting", "landing", "awaiting_ci", "gate_red", "no_checks", "needs_verify"}
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


def test_worker_step_routes_every_no_pr_action_or_returns_the_judgment():
    cfg = d.resolve_config({})

    def step(action, row=None, **worker):
        return d.worker_step(CALL, row or _mine(4), {"action": action, **worker}, cfg)

    routed = {"leave", "close_release", "redispatch", "park", "escalate", "nudge",
              "next_attempt", "orphan"}
    assert routed == {action for _, action in d.NO_PR_ROUTES}     # a new action needs a route

    assert step("leave") == ("leave", None)
    assert step("park") == ("park", None) and step("nudge") == ("nudge", None)
    # a claim with no worker left is CONTINUED, with no judgment asked: an unattended
    # run never releases it back to the frontier
    assert step("orphan") == step("redispatch") == ("dispatch", None)

    # already-satisfied: whether the diff really is empty stays a judgment
    do, j = step("close_release", worktree="/wt/4", worker_verdict=_declared("already-satisfied"))
    assert (do, j["kind"], j.get("bulky")) == ("judge", "empty_diff", None)
    assert j["context"] == {"worktree": "/wt/4", "base_branch": "main", "verdict": "https://gh/c/7"}
    sub, argv = _argv(j["if_yes"])
    assert sub == "close" and "--worker-command" not in argv
    assert _argv(j["if_no"])[0] == "fail"

    # an escalation whose reason is on record is carried out; one that is not is asked
    unmet = [{"number": 9, "standing": "unmet", "reason": "was closed as not planned"},
             {"number": 8, "standing": "waiting", "reason": None}]
    assert step("escalate", blockers=unmet, worker_verdict=_declared("blocked", blocked_by=[9, 8])) == \
        ("escalate", "blocked by a dependency nothing will resolve: #9 was closed as not planned")
    do, why = step("escalate", blockers=[], worker_verdict=_declared("blocked", reason="no design yet"))
    assert do == "escalate" and "no design yet" in why
    do, j = step("escalate", blockers=[], worker_verdict=_declared("blocked"))
    assert (do, j["kind"], j.get("bulky")) == ("judge", "reason", None)
    assert j["if_yes"] == j["if_no"] and _argv(j["if_yes"])[0] == "escalate"
    assert j["context"]["verdict"] == "https://gh/c/7"

    # a failure: the same line between a reason on record and one to be worded
    assert step("next_attempt", worker_verdict=_declared("giving-up", reason="flaky build")) == \
        ("fail", "its worker gave up: flaky build")
    do, j = step("next_attempt", worker_verdict=_declared("giving-up"))
    assert (do, j["kind"]) == ("judge", "reason") and _argv(j["if_yes"])[0] == "fail"
    do, why = step("next_attempt", worker_verdict=_declared("already-satisfied"))
    assert do == "fail" and "holds changes" in why
    do, why = step("next_attempt", worker_verdict=_declared("on-holiday"))
    assert do == "fail" and "on-holiday" in why
    do, why = step("next_attempt", worker_verdict=_declared(), nudged_at=NOW)
    assert do == "fail" and "after its nudge" in why
    do, why = step("next_attempt", worker_verdict=_declared(), nudged_at=None)
    assert do == "fail" and "no worktree" in why
    do, why = step("next_attempt", _mine(4, "landing", pr=30, stopped="conflict"),
                   worker_verdict=_declared(), nudged_at=NOW)
    assert (do, why) == ("fail", "given the landing turn and never landed: conflict")


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

    name = d.worktree_name("issue-{number}-{slug}", 31, "Fix the  Names inspector: tab (v2)!")
    assert name == "issue-31-fix-the-names-inspector-tab-v2"
    # the name it produces is one recovery recognises as this issue's branch
    assert d.branch_candidates([f"sunfmin/{name}", f"sunfmin/{name}-2", "sunfmin/issue-3-x"],
                               "issue-{number}-{slug}", 31) == [f"sunfmin/{name}", f"sunfmin/{name}-2"]
    assert d.worktree_name("issue-{number}-{slug}", 4, "中文标题") == "issue-4-work"
    assert d.worktree_name("issue-{number}-{slug}", 4, None) == "issue-4-work"
    long = d.worktree_name("issue-{number}-{slug}", 4, "word " * 40)
    assert len(long) <= len("issue-4-") + 40 and not long.endswith("-")
    assert d.worktree_name("afk/{number}", 9, "anything") == "afk/9"


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
    # a fresh start closes only what the FLEET opened for THIS issue: fleet-shaped
    # branch AND closes the issue — never a human's PR, never issue 30's
    assert [p["number"] for p in d.superseded_prs(prs, 3, "issue-{number}-{slug}")] == [30, 31]
    assert d.superseded_prs(prs, 4, "issue-{number}-{slug}") == []
    assert d.superseded_prs(None, 3, "issue-{number}-{slug}") == []


def _prompt_template():
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "..", "references", "worker-prompt.md")) as f:
        return f.read()


PROMPT_FIELDS = {"n": 31, "title": "Names inspector tab", "repo": "acme/widgets",
                 "base_branch": "main", "local_command": "make test",
                 "afk_path": "/skills/afk fleet/scripts/afk.py",
                 "config": '{"merge": {"target": "main"}}',
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
    config = json.dumps({"merge": {"target": "main"}, "note": "it's {branch} {title} {pr}"})
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
    # free text is substituted LAST, so a title or reason that looks like a
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
    # the minimal well-formed template renders
    ok = (block("prompt", "{opening}|{step1}|{n}{retry_reason}") + block("opening.fresh", "O")
          + block("step1.fresh", "S") + block("retry_reason", " because {reason}"))
    assert d.render_worker_prompt(ok, "fresh", PROMPT_FIELDS) == "O|S|31\n"
    assert d.render_worker_prompt(ok, "fresh", PROMPT_FIELDS, reason="R") == "O|S|31 because R\n"


def test_fingerprint():
    issues = [
        {"number": 1, "labels": ["ready-for-agent"], "updatedAt": "2026-07-01T00:00:00Z"},
        {"number": 2, "labels": ["epic"], "updatedAt": "2026-07-02T00:00:00Z"},
    ]
    prs = [{"number": 7, "headRefOid": "abc", "updatedAt": "2026-07-03T00:00:00Z",
            "statusCheckRollup": [{"name": "ci", "status": "COMPLETED", "conclusion": "SUCCESS"}]}]
    claims = [{"number": 1, "instance": "me", "sha": "s1"}]
    fp = d.fingerprint(issues, prs, claims)

    # canonical: row order, label order and fields outside the digest never move it
    assert fp == d.fingerprint(list(reversed(issues)), prs, claims)
    assert fp == d.fingerprint([{**issues[0], "title": "retitled"}, issues[1]], prs, claims)
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
    edged = [{**issues[0], "blocked_by": 1}, issues[1]]
    assert fp != d.fingerprint(edged, prs, claims)                            # a blocker recorded

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
    cfg = d.resolve_config({"epic_labels": ["epic", "prd"], "claim_lease_ttl_seconds": TTL})
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
    turn = d.latest_turn([{"id": 1, "body": d.turn_comment("me", now, stopped="awaiting_ci",
                                                           head="aaa")}])
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

    # the lease the partition uses is the CONFIG's: shorten it and the live peer goes stale
    short = d.assemble_working_set(issues, prs, claims, heartbeats, "me", now,
                                   {**cfg, "claim_lease_ttl_seconds": 50})
    assert [s["number"] for s in short["stale"]] == [5, 6] and short["peer_live"] == []

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
worktree_cleanup: false
branch_pattern: "issue-{number}-{slug}"   # quoted value with a # inside comment
gate:
  ci: required
  adversarial_verify: true
merge:
  strategy: rebase
retry: 3
"""
    p = d.parse_config_yaml(text)
    assert p["ready_label"] == "ready-for-agent"
    assert p["epic_labels"] == ["epic", "prd"]
    assert p["concurrency"] == 5 and p["worktree_cleanup"] is False
    assert p["branch_pattern"] == "issue-{number}-{slug}"
    assert p["gate"] == {"ci": "required", "adversarial_verify": True}
    assert p["merge"] == {"strategy": "rebase"}
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

    # a RENAMED key fails loudly with its migration note — never silently defaulted,
    # which would leave a config file quietly lying to its author (ADR-0009/ADR-0012)
    try:
        d.parse_config_yaml("merge:\n  rebase_before_merge: true")
        assert False, "expected ValueError for the retired rebase_before_merge key"
    except ValueError as e:
        assert "merge.sync_before_merge" in str(e) and "ADR-0012" in str(e)
    assert d.parse_config_yaml("merge:\n  sync_before_merge: false") == \
        {"merge": {"sync_before_merge": False}}


def test_resolve_config():
    full = d.resolve_config({})
    assert full["concurrency"] == 3 and full["gate"]["ci"] == "required"
    r = d.resolve_config({"concurrency": 5, "gate": {"adversarial_verify": True}})
    assert r["concurrency"] == 5
    # deep-merge keeps sibling defaults; untouched sections stay whole
    assert r["gate"]["adversarial_verify"] is True and r["gate"]["ci"] == "required"
    assert r["merge"]["strategy"] == "squash"
    # idempotent: resolving canonical config is a no-op
    assert d.resolve_config(r) == r


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
    for dotted, default in _leaves(d.CONFIG_DEFAULTS):
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
    assert seen == len(list(_leaves(d.CONFIG_DEFAULTS))) > 25

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
                "worktree_cleanup=yes",
                "epic_labels=a,b",
                "=3", ""):
        try:
            d.override_config(d.resolve_config({}), [bad])
            assert False, f"expected ValueError for --set {bad!r}"
        except ValueError:
            pass


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
    for choices in (d.CLAIM_NAMESPACES, d.GATE_CI_MODES, d.MERGE_STRATEGIES):
        assert " | ".join(choices) in text, f"template does not list {' | '.join(choices)}"
    full = d.resolve_config({})
    for k, v in parsed.items():
        if isinstance(v, dict):
            for sk, sv in v.items():
                assert full[k][sk] == sv, f"template drifted at {k}.{sk}: {sv!r}"
        else:
            assert full[k] == v, f"template drifted at {k}: {v!r}"
    # and the template shows every key the schema knows (nothing undocumented)
    assert set(parsed) == set(d.CONFIG_DEFAULTS)
    for k in ("gate", "merge"):
        assert set(parsed[k]) == set(d.CONFIG_DEFAULTS[k]), f"template missing keys in {k}:"


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


def test_qoderclicn_stock_default():
    assert d.WORKER_COMMAND_DEFAULT_QODERCN == "qoderclicn --dangerously-skip-permissions"
    assert d.WORKER_COMMAND_DEFAULT == "claude --dangerously-skip-permissions"


def test_launch_candidates_stays_claude_only():
    al = {"cc": "claude --dangerously-skip-permissions",
          "qc": "qoderclicn --dangerously-skip-permissions",
          "unrelated": "vim"}
    got = {c["name"] for c in d.launch_candidates(al)}
    assert "cc" in got
    assert "qc" not in got
    assert "unrelated" not in got


def run():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    run()
