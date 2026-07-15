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


def run():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    run()
