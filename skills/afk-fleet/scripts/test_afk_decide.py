#!/usr/bin/env python3
"""
Fixture tests for the afk-fleet decision core — pure, no git/gh/network.
Run: python3 test_afk_decide.py   (plain asserts, no test-framework dependency)

These cover the correctness-critical verdicts (esp. classify_claims: the
mine/peer_live/stale partition whose wrong answer silently corrupts state).
"""
import afk_decide as d

TTL = 4500  # ~75 min, the default lease


def test_select_frontier():
    issues = [
        {"number": 101, "state": "open", "labels": ["ready-for-agent"], "claimed": False, "has_open_pr": False, "open_blockers": 0},
        {"number": 102, "state": "open", "labels": ["ready-for-agent"], "open_blockers": 1},
        {"number": 103, "state": "open", "labels": ["ready-for-agent", "epic"], "open_blockers": 0},
        {"number": 104, "state": "open", "labels": ["ready-for-agent"], "claimed": True},
        {"number": 105, "state": "open", "labels": ["ready-for-agent"], "has_open_pr": True},
        {"number": 106, "state": "closed", "labels": ["ready-for-agent"]},
        {"number": 107, "state": "open", "labels": []},
    ]
    r = d.select_frontier(issues, "ready-for-agent", ["epic", "prd"])
    assert r["dispatch"] == [101], r
    reasons = {e["number"]: e["reason"] for e in r["excluded"]}
    assert "1 open blocker" in reasons[102]
    assert "epic label (epic)" == reasons[103]
    assert "already claimed" in reasons[104]
    assert "open linked PR" in reasons[105]
    assert reasons[106] == "not open"
    assert "no ready-for-agent label" == reasons[107]


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
    assert d.subclassify_pr("none", None) == "no_pr"
    assert d.subclassify_pr("open", "green") == "awaiting_merge"
    assert d.subclassify_pr("open", "red") == "failure"
    assert d.subclassify_pr("open", "pending") == "awaiting_ci"
    assert d.subclassify_pr("open", None) == "awaiting_ci"

    # gate.ci: local — there are no checks to WAIT on, because gating is an action
    # the tick takes at merge time (ADR-0012). Any open PR is awaiting_merge, and a
    # red remote run (the repo's own on:push CI, which the fleet does not gate on)
    # must never park the claim in `failure` forever.
    for checks in ("green", "red", "pending", None):
        assert d.subclassify_pr("open", checks, "local") == "awaiting_merge", checks
    assert d.subclassify_pr("none", None, "local") == "no_pr"      # no PR is still no PR
    # the default is unchanged for every existing repo
    assert d.subclassify_pr("open", "red") == d.subclassify_pr("open", "red", "required")


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

    # an unknown mode is refused, never treated as "required"
    try:
        d.validate_config(d.resolve_config({"gate": {"ci": "optional"}}))
        assert False, "expected ValueError for an unknown gate.ci"
    except ValueError as e:
        assert "gate.ci" in str(e) and "required" in str(e)


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
    assert d.latest_verdict(["hello", "world — no markers here"]) == empty

    # multiple markers across comments → the LAST (chronological, gh's default order) wins,
    # and its comment_url rides along
    comments = [
        {"body": "<!--afk:verdict n=5 phase=blocked blocked_by=2-->", "comment_url": "u1"},
        {"body": "some human chatter in between"},
        {"body": "<!--afk:verdict n=5 phase=giving-up-->", "comment_url": "u2"},
    ]
    r = d.latest_verdict(comments)
    assert r["found"] is True and r["phase"] == "giving-up" and r["comment_url"] == "u2"

    # bare strings and html_url/url fallbacks both accepted
    assert d.latest_verdict(["<!--afk:verdict phase=already-satisfied-->"])["phase"] == "already-satisfied"
    assert d.latest_verdict([{"body": "<!--afk:verdict phase=blocked-->", "html_url": "h"}])["comment_url"] == "h"
    assert d.latest_verdict([{"body": "<!--afk:verdict phase=blocked-->", "url": "u"}])["comment_url"] == "u"


def test_classify_no_pr():
    GRACE = 300
    zero = {"commits_ahead": 0, "dirty": False, "last_commit_ts": None, "worktree_mtime_ts": None}

    def v(phase, blocked_by=None):
        return {"found": True, "phase": phase, "blocked_by": blocked_by or [],
                "reason": None, "comment_url": "u"}

    # coding — terminal busy: left alone even with a giving-up verdict + zero progress
    assert d.classify_no_pr(zero, False, 9999, v("giving-up"), False, GRACE) == \
        {"outcome": "coding", "action": "leave"}
    # coding — idle but commits_ahead>0: real progress beats idle+verdict
    assert d.classify_no_pr({**zero, "commits_ahead": 2}, True, 9999,
                            v("already-satisfied"), False, GRACE)["outcome"] == "coding"
    # coding — idle but dirty worktree
    assert d.classify_no_pr({**zero, "dirty": True}, True, 9999, None, False, GRACE)["outcome"] == "coding"
    # coding — idle, zero progress, but activity within the grace window (a worker between steps)
    assert d.classify_no_pr(zero, True, 120, None, False, GRACE)["outcome"] == "coding"

    # idle_done — idle, zero progress, past grace, verdict already-satisfied → close + release
    assert d.classify_no_pr(zero, True, 600, v("already-satisfied"), False, GRACE) == \
        {"outcome": "idle_done", "action": "close_release"}

    # idle_blocked — dep now closed → re-dispatch (keep the claim)
    assert d.classify_no_pr(zero, True, 600, v("blocked", [42]), False, GRACE) == \
        {"outcome": "idle_blocked", "action": "redispatch"}
    # idle_blocked — a dep still open → escalate the DAG gap
    assert d.classify_no_pr(zero, True, 600, v("blocked", [42]), True, GRACE) == \
        {"outcome": "idle_blocked", "action": "escalate"}

    # idle_failed — verdict giving-up
    assert d.classify_no_pr(zero, True, 600, v("giving-up"), False, GRACE) == \
        {"outcome": "idle_failed", "action": "next_attempt"}
    # idle_failed — NO verdict at all after grace (the worker just stopped, no marker)
    assert d.classify_no_pr(zero, True, 600, None, False, GRACE) == \
        {"outcome": "idle_failed", "action": "next_attempt"}
    assert d.classify_no_pr(zero, True, 600, {"found": False}, False, GRACE)["outcome"] == "idle_failed"
    # idle_failed — unknown/garbage phase after grace → failed (safe default, not silently trusted)
    assert d.classify_no_pr(zero, True, 600, v("weird-phase"), False, GRACE)["outcome"] == "idle_failed"
    # idle_failed — idle_seconds unknown (None) is NOT treated as within grace
    assert d.classify_no_pr(zero, True, None, None, False, GRACE)["outcome"] == "idle_failed"

    # dead — no live worker/terminal at all → orphan path, even with leftover commits
    assert d.classify_no_pr({**zero, "commits_ahead": 3}, None, 600, None, False, GRACE) == \
        {"outcome": "dead", "action": "orphan"}


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


def test_next_attempt():
    assert d.next_attempt([], 2) == {"action": "retry", "from_label": None, "to_label": "afk-attempt/1"}
    assert d.next_attempt(["afk-attempt/1", "ready-for-agent"], 2) == \
        {"action": "retry", "from_label": "afk-attempt/1", "to_label": "afk-attempt/2"}
    assert d.next_attempt(["afk-attempt/2"], 2) == {"action": "escalate", "from_label": "afk-attempt/2"}
    # highest label wins even if out of order; junk suffix ignored
    assert d.next_attempt(["afk-attempt/x", "afk-attempt/1", "afk-attempt/3"], 2) == \
        {"action": "escalate", "from_label": "afk-attempt/3"}


def test_render_status_board():
    # ci_failed: PR opened, gate red, retrying — the two happy steps ticked, the
    # rest open, and the current-line names the attempt count.
    body = d.render_status_board({"phase": "ci_failed", "instance": "fl-abc",
                                  "pr": 123, "attempt": 2, "retry_max": 2})
    assert body.startswith(d.STATUS_MARKER)          # marker leads → find-or-create anchor
    assert "认领方 `fl-abc`" in body
    assert "- [x] 已认领 · worker 实现中" in body
    assert "- [x] PR 已开 (#123) · 等 CI" in body
    assert "- [ ] 门已绿 · 待合并" in body
    assert "- [ ] 已合并" in body
    assert "CI 失败,修复重试中(2/2)" in body

    # claimed: the invisible phase this whole feature exists to surface.
    claimed = d.render_status_board({"phase": "claimed", "instance": "x"})
    assert claimed.count("- [x]") == 1 and "尚无 PR" in claimed

    # merged: every step ticked, none open.
    merged = d.render_status_board({"phase": "merged", "pr": 7})
    assert merged.count("- [x]") == 4 and "- [ ]" not in merged
    assert "已合并,完成" in merged

    # escalated: terminal give-up — only what truly happened stays ticked.
    esc = d.render_status_board({"phase": "escalated", "pr": 9})
    assert esc.count("- [x]") == 2 and "已升级给人处理" in esc      # 认领 + PR
    assert d.render_status_board({"phase": "escalated"}).count("- [x]") == 1  # no PR → only 认领

    # determinism: identical state → identical body (write-only-on-change relies on it).
    assert d.render_status_board({"phase": "pr_open", "pr": 5}) == \
        d.render_status_board({"phase": "pr_open", "pr": 5})

    # unknown phase is rejected, not silently rendered.
    try:
        d.render_status_board({"phase": "bogus"})
        assert False, "expected ValueError for unknown phase"
    except ValueError:
        pass


def test_pace():
    cfg = {"busy_interval_seconds": 90, "idle_interval_seconds": 1500,
           "idle_ticks_before_sleep": 3, "claim_lease_ttl_seconds": TTL}
    # did work → busy
    assert d.pace({"merged": [1], "in_flight": 0, "empty_streak": 0}, cfg) == 90
    # in-flight → busy, and under the ttl/2 cap
    assert d.pace({"in_flight": 2, "empty_streak": 9}, cfg) == 90
    # idle but recently active (streak < threshold) → stay busy for stragglers
    assert d.pace({"in_flight": 0, "empty_streak": 2}, cfg) == 90
    # idle past threshold → idle interval
    assert d.pace({"in_flight": 0, "empty_streak": 3}, cfg) == 1500
    # ttl/2 cap actually bites when the idle interval would exceed it while holding a claim
    cap_cfg = {**cfg, "idle_interval_seconds": 999999}
    assert d.pace({"in_flight": 1, "empty_streak": 0}, {**cap_cfg, "busy_interval_seconds": 999999}) == TTL // 2


def test_fingerprint():
    issues = [
        {"number": 1, "labels": [{"name": "ready-for-agent"}], "updatedAt": "2026-07-01T00:00:00Z"},
        {"number": 2, "labels": ["epic"], "updatedAt": "2026-07-02T00:00:00Z"},
    ]
    prs = [{"number": 7, "headRefOid": "abc", "updatedAt": "2026-07-03T00:00:00Z",
            "statusCheckRollup": [{"name": "ci", "status": "COMPLETED", "conclusion": "SUCCESS"}]}]
    claims = [{"number": 1, "instance": "me", "sha": "s1"}]
    fp = d.fingerprint(issues, prs, claims)

    # canonical: row order and label representation (gh dicts vs fixture strings) never move it
    assert fp == d.fingerprint(list(reversed(issues)), prs, claims)
    assert fp == d.fingerprint(
        [{"number": 1, "labels": ["ready-for-agent"], "updatedAt": "2026-07-01T00:00:00Z"}, issues[1]],
        prs, claims)

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
        {"number": 2, "title": "blocked", "labels": ["ready-for-agent"], "updatedAt": "T2"},
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
    ws = d.assemble_working_set(issues, prs, claims, heartbeats, {2: 1},
                                "me", now, TTL, "ready-for-agent", ["epic", "prd"])

    # frontier: the join (claimed / has_open_pr / blockers) grafted in code, titles ride along
    assert ws["frontier"]["dispatch"] == [{"number": 1, "title": "ready"}]
    reasons = {e["number"]: e["reason"] for e in ws["frontier"]["excluded"]}
    assert "1 open blocker" in reasons[2]
    assert "already claimed" in reasons[3] and "already claimed" in reasons[5]

    # mine: subclassified with PR + checks + attempt labels — Act consumes this directly
    mine = {m["number"]: m for m in ws["mine"]}
    assert mine[3]["status"] == "awaiting_merge" and mine[3]["pr"] == 30 and mine[3]["checks"] == "green"
    assert mine[4]["status"] == "no_pr" and mine[4]["pr"] is None
    assert mine[4]["attempt_labels"] == ["afk-attempt/1"]

    # peers: live one identified and left alone; stale one carries the sha reclaim needs
    assert ws["peer_live"] == [{"number": 5, "instance": "peerA"}]
    assert ws["stale"] == [{"number": 6, "instance": "peerB", "sha": "s6"}]

    # the digest is the SAME function over the SAME observables the gate hashes
    assert ws["fingerprint"] == d.fingerprint(issues, prs, claims)
    assert ws["now"] == now

    # missing blocked_by entries default to 0 — safe: only frontier candidates need real counts
    ws2 = d.assemble_working_set(issues, prs, claims, heartbeats, {},
                                 "me", now, TTL, "ready-for-agent", ["epic", "prd"])
    assert {e["number"] for e in ws2["frontier"]["dispatch"]} == {1, 2}

    # gate.ci: local — a PR whose remote checks are RED is still awaiting_merge,
    # because those checks are not the gate; the tick re-runs the local one at merge
    # time instead (ADR-0012). Everything else about the working set is unchanged.
    red = [{**prs[0], "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "FAILURE"}]}]
    strict = d.assemble_working_set(issues, red, claims, heartbeats, {2: 1}, "me", now, TTL,
                                    "ready-for-agent", ["epic", "prd"])
    local = d.assemble_working_set(issues, red, claims, heartbeats, {2: 1}, "me", now, TTL,
                                   "ready-for-agent", ["epic", "prd"], "local")
    assert {m["number"]: m["status"] for m in strict["mine"]} == {3: "failure", 4: "no_pr"}
    assert {m["number"]: m["status"] for m in local["mine"]} == {3: "awaiting_merge", 4: "no_pr"}
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

    # parsing IS validation: typo'd keys, wrong shapes, and file-armed
    # authorize are all refused, never silently ignored
    for bad in ("readylabel: x",            # unknown top-level key
                "gate:\n  cii: x",          # unknown nested key
                "retry: soon",              # wrong type
                "gate: on",                 # scalar for a section
                "  ci: required",           # indented key outside a section
                "authorize: true"):         # never a config key
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


def test_pace_omission_is_uniform():
    # pace resolves partial config through the one defaults table (ADR-0009):
    # omission defaults instead of crashing, and the ttl/2 cap can no longer
    # be silently disabled by a missing claim_lease_ttl_seconds.
    assert d.pace({"in_flight": 0, "empty_streak": 9}, {}) == 1500
    assert d.pace({"in_flight": 1, "empty_streak": 0}, {"busy_interval_seconds": 999999}) == TTL // 2


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
        parsed = d.parse_config_yaml(f.read())
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


def run():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    run()
