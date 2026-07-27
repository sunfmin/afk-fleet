#!/usr/bin/env python3
"""
Integration tests for afk-fleet's EFFECTFUL ref ops — the half `test_afk_decide.py`
cannot reach. Offline by construction: a bare git repo on local disk stands in for
GitHub, so there is no gh, no network, and no token anywhere in this file.

Run: python3 test_afk_refs.py   (or under pytest, beside test_afk_decide.py)

The pure decision core is fixture-tested; these ops are the *other* correctness
risk (ADR-0003, ADR-0004): a claim that two fleets both win double-works an issue,
a reclaim that is not a compare-and-swap lets two fleets "rescue" the same claim,
and a release that silently no-ops leaves a phantom lock that starves an issue
forever. None of those show up in a single-process happy path, so what is asserted
here is the *race*: claims pushed concurrently from two independent clones, and
force-with-lease takeovers replayed against a sha that has already moved.

Time is always injected (`--now`), never read from the wall clock, so lease
arithmetic is pinned exactly as it is in the pure fixtures.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

AFK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "afk.py")
TTL = 4500          # the default lease, ~75 min
T0 = 1_000_000      # the pinned clock

# A hermetic git: no user/system config, no credential prompt, a fixed identity —
# so this suite behaves identically on a laptop and in a bare CI container.
ENV = {**os.environ,
       "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
       "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
       # offline is ENFORCED, not just intended: any op that reached for an https
       # remote would be refused by git rather than quietly hitting the network
       "GIT_ALLOW_PROTOCOL": "file",
       "GIT_AUTHOR_NAME": "afk-test", "GIT_AUTHOR_EMAIL": "afk@test.local",
       "GIT_COMMITTER_NAME": "afk-test", "GIT_COMMITTER_EMAIL": "afk@test.local"}


def git(cwd, *args):
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env=ENV)
    if p.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed in {cwd}:\n{p.stderr}")
    return p.stdout.strip()


def afk(cwd, *args):
    """One afk subcommand in `cwd` → its parsed JSON object. Exit 0 is required:
    a lost race is still `{"won": false}` and still exit 0 (only an operational
    error is exit 3), so a non-zero here is a real defect, not an expected outcome."""
    p = subprocess.run([sys.executable, AFK, *args], cwd=cwd,
                       capture_output=True, text=True, env=ENV)
    try:
        out = json.loads(p.stdout)
    except json.JSONDecodeError:
        raise AssertionError(f"afk {' '.join(args)} printed no JSON "
                             f"(exit {p.returncode}):\n{p.stdout}\n{p.stderr}")
    assert p.returncode == 0, f"afk {' '.join(args)} exited {p.returncode}: {out}"
    return out


class Sandbox:
    """A bare repo plus N working clones, all on local paths. The bare repo IS the
    fleet-state substrate: claim refs, heartbeat refs and work branches live in it
    exactly as they would in GitHub, and each clone is one machine's fleet."""

    def __init__(self, clones=1, base="main"):
        self.root = tempfile.mkdtemp(prefix="afk-refs-")
        self.base = base
        self.bare = os.path.join(self.root, "bare.git")
        git(self.root, "init", "--bare", f"--initial-branch={base}", "bare.git")

        # one real commit, so a base branch exists for the branch-vs-base reads
        seed = os.path.join(self.root, "seed")
        git(self.root, "clone", "--quiet", self.bare, "seed")
        with open(os.path.join(seed, "README.md"), "w") as f:
            f.write("seed\n")
        git(seed, "add", "-A")
        git(seed, "commit", "-qm", "seed")
        git(seed, "push", "-q", "origin", f"HEAD:refs/heads/{base}")

        self.clones = []
        for i in range(clones):
            name = f"clone{i}"
            git(self.root, "clone", "--quiet", self.bare, name)
            self.clones.append(os.path.join(self.root, name))

    def remote_ref(self, ref):
        """The sha the bare repo has for `ref`, or "" if it has none."""
        out = git(self.root, "ls-remote", self.bare, ref)
        return out.split()[0] if out else ""


@contextmanager
def sandbox(clones=1):
    sb = Sandbox(clones)
    try:
        yield sb
    finally:
        shutil.rmtree(sb.root, ignore_errors=True)


def test_claim_race_has_exactly_one_winner():
    """Two fleets, two clones, one issue, pushed at the same instant. The server
    rejecting the second create IS the compare-and-swap (ADR-0003) — nothing else
    arbitrates, so this is the assertion the whole claim design rests on."""
    with sandbox(clones=2) as sb:
        a, b = sb.clones
        with ThreadPoolExecutor(max_workers=2) as pool:
            fa = pool.submit(afk, a, "claim", "7", "--instance", "fleet-a", "--now", str(T0))
            fb = pool.submit(afk, b, "claim", "7", "--instance", "fleet-b", "--now", str(T0))
            ra, rb = fa.result(), fb.result()

        won = [r for r in (ra, rb) if r["won"]]
        lost = [r for r in (ra, rb) if not r["won"]]
        assert len(won) == 1 and len(lost) == 1, (ra, rb)

        # the loser is told WHO owns it, so it can skip the issue rather than guess
        assert lost[0]["owner"]["instance"] == won[0]["instance"], (ra, rb)
        assert won[0]["ref"] == "refs/afk/claim/7" and lost[0]["ref"] == "refs/afk/claim/7"
        # and the ref really points at the winner's marker — not the loser's
        assert sb.remote_ref("refs/afk/claim/7") == won[0]["sha"]

        # a third attempt, unraced, still loses: the claim ref is immutable once created
        third = afk(a, "claim", "7", "--instance", "fleet-c", "--now", str(T0 + 5))
        assert third["won"] is False and third["owner"]["instance"] == won[0]["instance"]


def test_classify_claims_partitions_real_refs():
    """mine / peer_live / stale over REAL claim + heartbeat refs. A wrong verdict
    here is the dangerous one: a stolen live claim double-works an issue."""
    with sandbox() as sb:
        w = sb.clones[0]
        for n, inst in ((1, "me"), (2, "peer-live"), (3, "peer-dead"), (4, "peer-silent")):
            assert afk(w, "claim", str(n), "--instance", inst, "--now", str(T0))["won"], n

        # heartbeats written at pinned times: fresh, long expired, and (peer-silent)
        # never written at all — an owner that never beat counts as dead
        for inst, ts in (("me", T0), ("peer-live", T0 - 60), ("peer-dead", T0 - TTL - 60)):
            assert afk(w, "heartbeat", "--instance", inst, "--now", str(ts),
                       "--ttl", str(TTL))["refreshed"], inst

        r = afk(w, "classify-claims", "--instance", "me", "--now", str(T0), "--ttl", str(TTL))
        assert r["mine"] == [1], r
        assert r["peer_live"] == [2], r
        assert r["stale"] == [3, 4], r

        # the scan behind it reports each claim's owner, host and the sha reclaim needs
        claims = {c["number"]: c for c in afk(w, "scan")["claims"]}
        assert claims[3]["instance"] == "peer-dead" and claims[3]["sha"]
        assert claims[3]["host"] and claims[3]["ts"] == T0
        assert afk(w, "scan")["heartbeats"]["peer-dead"] == T0 - TTL - 60


def test_stale_reclaim_is_an_atomic_compare_and_swap():
    """A reclaim is `--force-with-lease` against the sha the reclaimer READ, so two
    fleets that both saw the same stale claim cannot both take it."""
    with sandbox(clones=2) as sb:
        a, b = sb.clones
        assert afk(a, "claim", "5", "--instance", "dead-peer", "--now", str(T0))["won"]

        # both fleets read the same sha (the same stale claim, seen at the same time)
        seen = {c["number"]: c["sha"] for c in afk(a, "scan")["claims"]}[5]
        assert seen == sb.remote_ref("refs/afk/claim/5")

        first = afk(a, "reclaim", "5", "--instance", "fleet-a",
                    "--expect-sha", seen, "--now", str(T0))
        assert first["won"] and first["sha"] != seen

        # the second reclaimer's lease is now stale → it MUST lose, and the ref must
        # still be the first taker's. (Without the lease, both would "win".)
        second = afk(b, "reclaim", "5", "--instance", "fleet-b",
                     "--expect-sha", seen, "--now", str(T0 + 1))
        assert second["won"] is False, second
        assert sb.remote_ref("refs/afk/claim/5") == first["sha"]

        # ownership moved wholesale: it is now fleet-a's own claim to reconcile
        assert afk(a, "classify-claims", "--instance", "fleet-a",
                   "--now", str(T0), "--ttl", str(TTL))["mine"] == [5]
        assert afk(b, "classify-claims", "--instance", "fleet-b",
                   "--now", str(T0), "--ttl", str(TTL))["mine"] == []


def test_heartbeat_refreshes_only_when_due():
    """The lease is refreshed at ~ttl/3, not every tick — and the tool holds no
    state: it re-reads the previous timestamp from the ref itself."""
    with sandbox() as sb:
        w = sb.clones[0]
        first = afk(w, "heartbeat", "--instance", "me", "--now", str(T0), "--ttl", str(TTL))
        assert first["refreshed"] is True and first["ts"] == T0
        assert first["ref"] == "refs/afk/heartbeat/me"
        written = sb.remote_ref("refs/afk/heartbeat/me")

        # not yet due: a no-op that reports the EXISTING ts and touches no ref
        early = afk(w, "heartbeat", "--instance", "me",
                    "--now", str(T0 + TTL // 3), "--ttl", str(TTL))
        assert early == {"refreshed": False, "reason": "not due", "ts": T0,
                         "ref": "refs/afk/heartbeat/me"}
        assert sb.remote_ref("refs/afk/heartbeat/me") == written

        # past ttl/3: refreshed, and the ref carries the new ts
        due = afk(w, "heartbeat", "--instance", "me",
                  "--now", str(T0 + TTL // 3 + 2), "--ttl", str(TTL))
        assert due["refreshed"] is True and due["ts"] == T0 + TTL // 3 + 2
        assert sb.remote_ref("refs/afk/heartbeat/me") != written
        assert afk(w, "scan")["heartbeats"]["me"] == due["ts"]

        # heartbeats are PER INSTANCE, so a second fleet's beat is a separate ref
        assert afk(w, "heartbeat", "--instance", "other", "--now", str(T0),
                   "--ttl", str(TTL))["refreshed"] is True
        assert set(afk(w, "scan")["heartbeats"]) == {"me", "other"}


def test_release_is_idempotent_and_the_claim_is_really_gone():
    """A skipped or half-done delete is a phantom lock that silently starves an
    issue, so release must be safe to repeat and must actually disappear."""
    with sandbox() as sb:
        w = sb.clones[0]
        afk(w, "claim", "9", "--instance", "me", "--now", str(T0))
        afk(w, "claim", "10", "--instance", "me", "--now", str(T0))

        first = afk(w, "release", "9")
        assert first["released"] is True and first["ref"] == "refs/afk/claim/9"
        # already gone counts as released — the terminal transitions call this blind
        assert afk(w, "release", "9")["released"] is True
        assert afk(w, "release", "404")["released"] is True

        assert sb.remote_ref("refs/afk/claim/9") == ""
        # the scan's local mirror is pruned too, so a later tick cannot see a ghost
        assert [c["number"] for c in afk(w, "scan")["claims"]] == [10]
        assert afk(w, "classify-claims", "--instance", "me", "--now", str(T0))["mine"] == [10]

        # released → claimable again, by anyone
        assert afk(w, "claim", "9", "--instance", "peer", "--now", str(T0 + 1))["won"] is True


def test_probe_prefers_the_hidden_namespace_and_cleans_up():
    with sandbox() as sb:
        w = sb.clones[0]
        assert afk(w, "probe", "--now", str(T0)) == {
            "namespace": "refs/afk", "hidden": True, "ci_on_push": False, "blocked": False}
        # it leaves nothing behind: a lingering probe ref would be fleet litter
        assert sb.remote_ref("refs/afk/probe") == ""


def test_every_ref_op_round_trips_under_the_refs_heads_fallback():
    """When an org ruleset forbids non-branch refs, every op must work unchanged
    under `refs/heads/afk-*` — the fallback is only worth having if it is complete."""
    NS = ("--ns", "refs/heads")
    with sandbox() as sb:
        w = sb.clones[0]

        # probed under the fallback, the verdict is honest about what it means:
        # ordinary branches, so `on: push` CI will fire on claim churn
        assert afk(w, "probe", *NS, "--now", str(T0)) == {
            "namespace": "refs/heads", "hidden": False, "ci_on_push": True, "blocked": False}
        assert sb.remote_ref("refs/heads/probe") == ""

        claim = afk(w, "claim", "12", "--instance", "me", *NS, "--now", str(T0))
        assert claim["won"] and claim["ref"] == "refs/heads/afk-claim/12"
        assert sb.remote_ref("refs/heads/afk-claim/12") == claim["sha"]
        assert sb.remote_ref("refs/afk/claim/12") == ""          # nothing in the hidden ns

        hb = afk(w, "heartbeat", "--instance", "me", *NS, "--now", str(T0), "--ttl", str(TTL))
        assert hb["refreshed"] and hb["ref"] == "refs/heads/afk-heartbeat/me"

        scan = afk(w, "scan", *NS)
        assert [c["number"] for c in scan["claims"]] == [12]
        assert scan["heartbeats"] == {"me": T0}
        assert afk(w, "classify-claims", "--instance", "me", *NS,
                   "--now", str(T0), "--ttl", str(TTL))["mine"] == [12]

        # a losing create and a lease-checked takeover both behave the same here
        assert afk(w, "claim", "12", "--instance", "peer", *NS, "--now", str(T0))["won"] is False
        taken = afk(w, "reclaim", "12", "--instance", "peer", *NS,
                    "--expect-sha", scan["claims"][0]["sha"], "--now", str(T0 + 1))
        assert taken["won"] is True
        assert afk(w, "reclaim", "12", "--instance", "third", *NS,
                   "--expect-sha", scan["claims"][0]["sha"], "--now", str(T0 + 2))["won"] is False

        assert afk(w, "release", "12", *NS)["released"] is True
        assert afk(w, "scan", *NS)["claims"] == []
        assert sb.remote_ref("refs/heads/afk-claim/12") == ""


def test_takeover_lists_and_force_takes_a_dead_fleet():
    """`afk takeover` (ADR-0011): a dead fleet is discoverable from its markers +
    heartbeat refs alone, its claims transfer wholesale, and a fleet that still
    looks ALIVE is held back until a human says otherwise."""
    with sandbox() as sb:
        w = sb.clones[0]
        for n in (21, 22):
            assert afk(w, "claim", str(n), "--instance", "dead-fleet", "--now", str(T0))["won"]
        assert afk(w, "claim", "23", "--instance", "live-fleet", "--now", str(T0))["won"]
        afk(w, "heartbeat", "--instance", "dead-fleet",
            "--now", str(T0 - TTL - 99), "--ttl", str(TTL))
        afk(w, "heartbeat", "--instance", "live-fleet", "--now", str(T0 - 10), "--ttl", str(TTL))

        rows = {r["instance"]: r for r in
                afk(w, "takeover", "--list", "--as", "new-fleet",
                    "--now", str(T0), "--ttl", str(TTL))["instances"]}
        assert rows["dead-fleet"]["claims"] == [21, 22] and rows["dead-fleet"]["fresh"] is False
        assert rows["dead-fleet"]["heartbeat_age"] == TTL + 99
        assert rows["live-fleet"]["fresh"] is True and rows["live-fleet"]["claim_count"] == 1

        # a live-looking fleet: warned about, and NOTHING is taken
        held = afk(w, "takeover", "--instance", "live-fleet", "--as", "new-fleet",
                   "--now", str(T0), "--ttl", str(TTL))
        assert held["action"] == "confirm" and held["taken"] == []
        assert sb.remote_ref("refs/afk/claim/23")
        assert afk(w, "classify-claims", "--instance", "new-fleet",
                   "--now", str(T0), "--ttl", str(TTL))["mine"] == []

        # the dead one transfers wholesale, re-stamped with my instance id
        took = afk(w, "takeover", "--instance", "dead-fleet", "--as", "new-fleet",
                   "--now", str(T0), "--ttl", str(TTL))
        assert took["action"] == "taken" and took["taken"] == [21, 22] and took["lost"] == []
        part = afk(w, "classify-claims", "--instance", "new-fleet",
                   "--now", str(T0), "--ttl", str(TTL))
        assert part["mine"] == [21, 22] and part["peer_live"] == [23]

        # --yes is the informed human override of the fresh-heartbeat hold
        forced = afk(w, "takeover", "--instance", "live-fleet", "--as", "new-fleet", "--yes",
                     "--now", str(T0), "--ttl", str(TTL))
        assert forced["action"] == "taken" and forced["taken"] == [23]
        assert afk(w, "classify-claims", "--instance", "new-fleet",
                   "--now", str(T0), "--ttl", str(TTL))["mine"] == [21, 22, 23]

        # and taking from myself is refused, not silently re-stamped
        assert afk(w, "takeover", "--instance", "new-fleet", "--as", "new-fleet",
                   "--now", str(T0))["action"] == "error"


def test_recovery_reads_pushed_progress_from_the_remote_alone():
    """`afk recovery` (ADR-0011) tier 2 vs tier 3: with no local worktree, the only
    evidence a dead worker left is its pushed branch, which has to be recognised
    from the issue number — the claim ref never records a branch name."""
    with sandbox() as sb:
        w = sb.clones[0]
        cfg = json.dumps({"base_branch": sb.base, "branch_pattern": "issue-{number}-{slug}"})

        # nothing pushed → tier 3, the old fresh re-dispatch
        r = afk(w, "recovery", "--issue", "31", "--no-worktree", "--config", cfg)
        assert r["tier"] == 3 and r["action"] == "dispatch_fresh" and r["prompt"] == "fresh"
        assert r["branch"]["name"] is None and r["worktree"]["present"] is False

        # a dead worker's branch, orca-shaped (<user>/ prefix), two commits ahead
        git(w, "checkout", "-q", "-b", "sunfmin/issue-31-continuation")
        for i in (1, 2):
            with open(os.path.join(w, f"step{i}.txt"), "w") as f:
                f.write(f"step {i}\n")
            git(w, "add", "-A")
            git(w, "commit", "-qm", f"step {i}")
        git(w, "push", "-q", "origin", "HEAD")

        r = afk(w, "recovery", "--issue", "31", "--no-worktree", "--config", cfg)
        assert (r["tier"], r["action"], r["prompt"]) == (2, "recreate_at_tip", "continue"), r
        assert r["branch"]["name"] == "sunfmin/issue-31-continuation"
        assert r["branch"]["commits_ahead"] == 2
        assert r["branch"]["candidates"] == ["sunfmin/issue-31-continuation"]

        # another issue's branch is never mistaken for this one
        assert afk(w, "recovery", "--issue", "3", "--no-worktree", "--config", cfg)["tier"] == 3

        # a worktree still on this machine wins: tier 1, reused in place, never removed
        r = afk(w, "recovery", "--issue", "31", "--worktree", w, "--config", cfg)
        assert (r["tier"], r["action"], r["prompt"]) == (1, "reuse_worktree", "continue"), r
        assert r["worktree"]["present"] is True and r["worktree"]["commits_ahead"] == 2


def run():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    run()
