"""Adversarial claim-generation regressions against disposable REAL git remotes.

Only gh/orca are fakes. Callbacks inject peer mutations at the actual transition
boundaries; no test claims atomicity between a heartbeat read and a claim push,
or between git ownership checks and GitHub mutations.
"""
import os
from contextlib import contextmanager
from unittest.mock import patch

import pytest

import afk
import afk_decide
from test_afk_refs import ENV, T0, TTL, afk as cli, afk_error, sandbox
from test_afk_cli import R, ME, NOW, _merge, dispatch, issue, local_gate, with_pr, world


@contextmanager
def execution(cwd, env=ENV):
    previous = os.getcwd()
    os.chdir(cwd)
    try:
        with patch.dict(os.environ, env, clear=True), patch.object(afk, "_GIT_ENV", env):
            yield
    finally:
        os.chdir(previous)


@pytest.mark.parametrize("namespace,prefix", [("refs/afk", "refs/afk/claim"),
                                               ("refs/heads", "refs/heads/afk-claim")])
def test_new_claim_is_live_before_first_heartbeat_but_grace_expires(namespace, prefix):
    with sandbox(clones=2) as sb:
        owner, peer = sb.clones
        cfg = ("--set", f"claim_namespace={namespace}")
        claim = cli(owner, "claim", "42", "--instance", "owner", "--now", str(T0), *cfg)
        assert cli(peer, "scan", *cfg)["heartbeats"] == {}
        for now in (T0, T0 + TTL):
            view = cli(peer, "classify-claims", "--instance", "peer", "--now", str(now), *cfg)
            assert view["peer_live"] == [42] and view["stale"] == []
            refused = cli(peer, "reclaim", "42", "--instance", "peer", "--expect-sha",
                          claim["sha"], "--now", str(now), *cfg)
            assert refused["won"] is False and refused["reason"] == "owner live"
        expired = cli(peer, "classify-claims", "--instance", "peer",
                      "--now", str(T0 + TTL + 1), *cfg)
        assert expired["stale"] == [42]
        taken = cli(peer, "reclaim", "42", "--instance", "peer", "--expect-sha",
                    claim["sha"], "--now", str(T0 + TTL + 1), *cfg)
        assert taken["won"] and sb.remote_ref(f"{prefix}/42") == taken["sha"]
        # A reclaimed owner has the same bounded grace, with no heartbeat yet.
        assert cli(owner, "classify-claims", "--instance", "owner",
                   "--now", str(T0 + TTL + 1), *cfg)["peer_live"] == [42]


def test_reclaim_refuses_owner_that_renewed_after_stale_scan():
    with sandbox(clones=2) as sb:
        owner, peer = sb.clones
        claim = cli(owner, "claim", "42", "--instance", "owner", "--now", str(T0))
        assert cli(peer, "classify-claims", "--instance", "peer",
                   "--now", str(T0 + TTL + 1))["stale"] == [42]
        cli(owner, "heartbeat", "--instance", "owner", "--now", str(T0 + TTL + 2))
        result = cli(peer, "reclaim", "42", "--instance", "peer", "--expect-sha",
                     claim["sha"], "--now", str(T0 + TTL + 3))
        assert result["won"] is False and result["reason"] == "owner live"
        assert sb.remote_ref(claim["ref"]) == claim["sha"]


def test_unreadable_heartbeat_does_not_authorize_reclaim():
    with sandbox() as sb:
        w = sb.clones[0]
        claim = cli(w, "claim", "42", "--instance", "owner", "--now", str(T0))
        cli(w, "heartbeat", "--instance", "owner", "--now", str(T0 + TTL + 1))
        args = afk.build_parser().parse_args([
            "reclaim", "42", "--instance", "peer", "--expect-sha", claim["sha"],
            "--now", str(T0 + TTL + 2), "--config", "{}"])
        real_git = afk._git
        def unavailable(argv, check=True):
            if argv[0] == "ls-remote" and argv[-1] == "refs/afk/heartbeat/owner":
                raise RuntimeError("heartbeat remote unavailable")
            return real_git(argv, check=check)
        with execution(w), patch.object(afk, "_git", unavailable):
            with pytest.raises(RuntimeError, match="heartbeat remote unavailable"):
                afk.cmd_reclaim(args)
        assert sb.remote_ref(claim["ref"]) == claim["sha"]


@pytest.mark.parametrize("during_push", [False, True])
def test_compare_and_delete_preserves_successor(during_push):
    with sandbox(clones=2) as sb:
        owner, peer = sb.clones
        claim = cli(owner, "claim", "42", "--instance", "owner", "--now", str(T0))
        successor = []
        def take():
            successor.append(cli(peer, "reclaim", "42", "--instance", "peer",
                                 "--expect-sha", claim["sha"], "--now", str(T0 + TTL + 1)))
            assert successor[-1]["won"]
        if not during_push:
            take()
        real_git = afk._git
        def interleaved(argv, check=True):
            if during_push and argv[0] == "push" and argv[-1] == f":{claim['ref']}":
                take()
            return real_git(argv, check=check)
        with execution(owner), patch.object(afk, "_git", interleaved):
            result = afk._release("origin", afk_decide.resolve_config({}), 42, claim["sha"])
        assert result["released"] is False and result["reason"] == "claim changed"
        assert sb.remote_ref(claim["ref"]) == successor[0]["sha"]


def test_same_instance_same_timestamp_reacquisition_is_a_new_generation():
    with sandbox() as sb:
        w = sb.clones[0]
        old = cli(w, "claim", "42", "--instance", "owner", "--now", str(T0))
        repeated = cli(w, "claim", "42", "--instance", "owner", "--now", str(T0))
        assert repeated["won"] is False  # identical marker data is not a second win
        assert cli(w, "release", "42", "--expect-sha", old["sha"])["released"]
        new = cli(w, "claim", "42", "--instance", "owner", "--now", str(T0))
        assert new["sha"] != old["sha"]
        assert cli(w, "release", "42", "--expect-sha", old["sha"])["released"] is False
        with execution(w), pytest.raises(afk.ClaimLostError, match="generation changed"):
            afk._require_claim("origin", afk_decide.resolve_config({}), 42, "owner", old["sha"])
        assert sb.remote_ref(new["ref"]) == new["sha"]


def test_release_requires_explicit_observed_generation():
    with sandbox() as sb:
        claim = cli(sb.clones[0], "claim", "42", "--instance", "owner", "--now", str(T0))
        assert "--expect-sha" in afk_error(sb.clones[0], "release", "42")
        assert sb.remote_ref(claim["ref"]) == claim["sha"]


def test_takeover_warns_about_new_claim_without_a_heartbeat():
    with sandbox() as sb:
        w = sb.clones[0]
        claim = cli(w, "claim", "42", "--instance", "owner", "--now", str(T0))
        result = cli(w, "takeover", "--from", "owner", "--instance", "peer", "--now", str(T0))
        assert result["action"] == "confirm" and result["taken"] == []
        assert sb.remote_ref(claim["ref"]) == claim["sha"]


def test_takeover_during_gate_prevents_merge_and_successor_cleanup():
    with world(issues=[issue(1, "ready-for-agent")]) as w:
        worker, _ = with_pr(w, 1, 10)
        real_gate = afk._run_gate
        successor = []
        def gate_then_take(*args, **kwargs):
            result = real_gate(*args, **kwargs)
            taken = w.afk("takeover", "--from", "me", "--instance", "peer", "--yes", *NOW, *R)
            assert taken["taken"] == [1]
            successor.append(w.sb.remote_ref("refs/afk/claim/1"))
            return result
        args = afk.build_parser().parse_args([*_merge(1, *local_gate("true")), "--config", "{}"])
        with execution(w.cwd, w.env), patch.object(afk, "_run_gate", gate_then_take):
            result = afk.cmd_merge(args)
        assert result["outcome"] == "claim_lost" and result["released"] is False
        assert not result["merged"]
        assert not [c for c in w.calls() if c[:2] == ["pr", "merge"]]
        assert w.sb.remote_ref("refs/afk/claim/1") == successor[0]
        assert os.path.isdir(worker["worktree"])


def test_takeover_during_issue_close_preserves_successor_and_worktree():
    with world(issues=[issue(1, "ready-for-agent")]) as w:
        worker = w.afk(*dispatch(1))
        real_gh = afk._gh
        def close_then_take(argv, check=True):
            result = real_gh(argv, check=check)
            if argv[:2] == ["issue", "close"]:
                taken = w.afk("takeover", "--from", "me", "--instance", "peer", "--yes", *NOW, *R)
                assert taken["taken"] == [1]
            return result
        args = afk.build_parser().parse_args([
            "close", "--issue", "1", *ME, *R, *NOW, "--config", "{}"])
        with execution(w.cwd, w.env), patch.object(afk, "_gh", close_then_take):
            result = afk.cmd_close(args)
        assert result["action"] == "closed" and result["released"] is False
        assert result["reason"] == "claim changed" and w.claimed_by(1) == "peer"
        assert os.path.isdir(worker["worktree"])


def test_takeover_during_terminal_wait_prevents_prompt_submission():
    with world(issues=[issue(1, "ready-for-agent")]) as w:
        real_orca = afk._orca
        def wait_then_take(argv, timeout=60):
            result = real_orca(argv, timeout=timeout)
            if argv[:2] == ["terminal", "wait"]:
                taken = w.afk("takeover", "--from", "me", "--instance", "peer", "--yes", *NOW, *R)
                assert taken["taken"] == [1]
            return result
        args = afk.build_parser().parse_args([*dispatch(1), "--config", "{}"])
        with execution(w.cwd, w.env), patch.object(afk, "_orca", wait_then_take):
            with pytest.raises(afk.ClaimLostError):
                afk.cmd_dispatch(args)
        assert "terminal send" not in w.orca_calls()
        assert w.claimed_by(1) == "peer"


def test_takeover_during_escalation_read_prevents_relabel_and_release():
    with world(issues=[issue(1, "ready-for-agent")]) as w:
        w.afk("claim", "1", *ME, *NOW, *R)
        real_prs = afk._open_prs
        def read_then_take(repo):
            result = real_prs(repo)
            taken = w.afk("takeover", "--from", "me", "--instance", "peer", "--yes", *NOW, *R)
            assert taken["taken"] == [1]
            return result
        args = afk.build_parser().parse_args([
            "escalate", "--issue", "1", *ME, *R, *NOW, "--reason", "blocked", "--config", "{}"])
        with execution(w.cwd, w.env), patch.object(afk, "_open_prs", read_then_take):
            with pytest.raises(afk.ClaimLostError):
                afk.cmd_escalate(args)
        assert w.issue(1)["labels"] == ["ready-for-agent"]
        assert w.comments(1) == [] and w.claimed_by(1) == "peer"
