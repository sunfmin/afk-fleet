#!/usr/bin/env python3
"""
Integration tests for afk-fleet's EFFECTFUL ref ops — the half `test_afk_decide.py`
cannot reach. Offline by construction: a bare git repo on local disk stands in for
GitHub, so there is no gh, no network, and no token anywhere in this file.

Run: under pytest — the command is `gate.local_command` in docs/agents/afk-fleet.md

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

import afk_decide

AFK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "afk.py")
TTL = afk_decide.CLAIM_LEASE_TTL_SECONDS
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


ME = ("--instance", "me")

# The two subcommands that run before a config exists, and so take none.
NO_CONFIG = ("config", "worker-command")


def settled(config):
    """A `--config` JSON with the sandbox's `main` as its base branch, unless it
    is not JSON at all (a test of that refusal)."""
    try:
        return json.dumps({"base_branch": "main", **json.loads(config)})
    except json.JSONDecodeError:
        return config


def run_afk(cwd, *args, env=None, bare=False):
    """One afk subcommand in `cwd` → (exit code, its parsed JSON object).

    The CLI refuses a call without `--config`; a test that does not care which
    config it runs on says so here, once — it gets `{}`, i.e. the defaults table
    (lease = TTL). And no config has a base branch until a launch settles one
    (`afk probe`, ADR-0042): a call that names none runs on the sandbox's `main`.
    `bare=True` sends exactly `args`, for the tests of those refusals."""
    if not bare and args[0] not in NO_CONFIG and "--config" not in args:
        args = (*args, "--config", "{}")
    if not bare and args[0] not in (*NO_CONFIG, "probe"):
        at = args.index("--config") + 1
        args = (*args[:at], settled(args[at]), *args[at + 1:])
    p = subprocess.run([sys.executable, AFK, *args], cwd=cwd,
                       capture_output=True, text=True, env=env or ENV)
    try:
        return p.returncode, json.loads(p.stdout)
    except json.JSONDecodeError:
        raise AssertionError(f"afk {' '.join(args)} printed no JSON "
                             f"(exit {p.returncode}):\n{p.stdout}\n{p.stderr}")


def afk(cwd, *args, env=None):
    """One afk subcommand that must SUCCEED → its JSON. A lost race is still
    `{"won": false}` and still exit 0 (only an operational error is exit 3), so a
    non-zero here is a real defect, not an expected outcome."""
    code, out = run_afk(cwd, *args, env=env)
    assert code == 0, f"afk {' '.join(args)} exited {code}: {out}"
    return out


def afk_error(cwd, *args, env=None, bare=False):
    """One afk subcommand that must FAIL operationally → its error text. Exit 3 and
    an `{"error": …}` object are the whole contract a tick can rely on."""
    code, out = run_afk(cwd, *args, env=env, bare=bare)
    assert code == 3 and set(out) == {"error"}, f"afk {' '.join(args)}: exit {code}: {out}"
    return out["error"]


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

    def all_refs(self):
        """Every ref name the bare repo holds — to assert an op left no litter."""
        return {ln.split()[1] for ln in git(self.root, "ls-remote", self.bare).splitlines()
                if ln.split()[1] != "HEAD"}

    def forbid(self, prefix):
        """Make the bare repo REJECT every push under `prefix` — what a GitHub org
        ruleset forbidding non-branch refs does (`! [remote rejected]`)."""
        hook = os.path.join(self.bare, "hooks", "update")
        with open(hook, "w") as f:
            f.write(f'#!/bin/sh\ncase "$1" in {prefix}*) echo "ruleset: $1 forbidden" >&2; '
                    f'exit 1;; esac\nexit 0\n')
        os.chmod(hook, 0o755)


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
            assert afk(w, "heartbeat", "--instance", inst, "--now", str(ts))["refreshed"], inst

        r = afk(w, "classify-claims", "--instance", "me", "--now", str(T0))
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
                   "--now", str(T0))["mine"] == [5]
        assert afk(b, "classify-claims", "--instance", "fleet-b",
                   "--now", str(T0))["mine"] == []


def test_heartbeat_refreshes_only_when_due():
    """The lease is refreshed at ~ttl/3, not every tick — and the tool holds no
    state: it re-reads the previous timestamp from the ref itself."""
    with sandbox() as sb:
        w = sb.clones[0]
        first = afk(w, "heartbeat", "--instance", "me", "--now", str(T0))
        assert first["refreshed"] is True and first["ts"] == T0
        assert first["ref"] == "refs/afk/heartbeat/me"
        written = sb.remote_ref("refs/afk/heartbeat/me")

        # not yet due: a no-op that reports the EXISTING ts and touches no ref
        early = afk(w, "heartbeat", "--instance", "me",
                    "--now", str(T0 + TTL // 3))
        assert early == {"refreshed": False, "reason": "not due", "ts": T0,
                         "ref": "refs/afk/heartbeat/me"}
        assert sb.remote_ref("refs/afk/heartbeat/me") == written

        # past ttl/3: refreshed, and the ref carries the new ts
        due = afk(w, "heartbeat", "--instance", "me",
                  "--now", str(T0 + TTL // 3 + 2))
        assert due["refreshed"] is True and due["ts"] == T0 + TTL // 3 + 2
        assert sb.remote_ref("refs/afk/heartbeat/me") != written
        assert afk(w, "scan")["heartbeats"]["me"] == due["ts"]

        # heartbeats are PER INSTANCE, so a second fleet's beat is a separate ref
        assert afk(w, "heartbeat", "--instance", "other", "--now", str(T0))["refreshed"] is True
        assert set(afk(w, "scan")["heartbeats"]) == {"me", "other"}


def test_release_is_idempotent_and_the_claim_is_really_gone():
    """A skipped or half-done delete is a phantom lock that silently starves an
    issue, so release must be safe to repeat and must actually disappear."""
    with sandbox() as sb:
        w = sb.clones[0]
        afk(w, "claim", "9", "--instance", "me", "--now", str(T0))
        afk(w, "claim", "10", "--instance", "me", "--now", str(T0))

        first = afk(w, "release", "9", *ME)
        assert first == {"released": True, "issue": 9, "ref": "refs/afk/claim/9"}
        # already gone counts as released — the terminal transitions call this blind
        assert afk(w, "release", "9", *ME)["released"] is True
        assert afk(w, "release", "404", *ME)["released"] is True

        assert sb.remote_ref("refs/afk/claim/9") == ""
        # the scan's local mirror is pruned too, so a later tick cannot see a ghost
        assert [c["number"] for c in afk(w, "scan")["claims"]] == [10]
        assert afk(w, "classify-claims", "--instance", "me", "--now", str(T0))["mine"] == [10]

        # released → claimable again, by anyone
        assert afk(w, "claim", "9", "--instance", "peer", "--now", str(T0 + 1))["won"] is True


def test_a_release_that_did_not_delete_the_claim_is_an_error():
    """`released` means the claim is GONE. A delete the server refused, or one that
    never reached it, leaves a phantom lock — reported as `released: false` with
    exit 0 it would be skimmed past by a tick that calls release blind."""
    with sandbox() as sb:
        w = sb.clones[0]
        claim = afk(w, "claim", "9", "--instance", "me", "--now", str(T0))

        err = afk_error(w, "release", "9", *ME, "--remote", "no-such-remote")
        assert "no-such-remote" in err
        sb.forbid("refs/afk/")                                  # the server refuses the delete
        err = afk_error(w, "release", "9", *ME)
        assert "still on the remote" in err and "refs/afk/claim/9" in err
        assert sb.remote_ref("refs/afk/claim/9") == claim["sha"]      # and it really is
        # …and the same holds for the lease-checked delete of a peer's phantom lock
        err = afk_error(w, "release", "9", "--instance", "peer", "--expect-sha", claim["sha"])
        assert "still on the remote" in err
        assert sb.remote_ref("refs/afk/claim/9") == claim["sha"]

        # the refusal is about THIS ref still existing, not about the push failing:
        # with the same server rule in force, a claim that is already gone is released
        assert afk(w, "release", "404", *ME)["released"] is True
        assert afk(w, "release", "404", *ME, "--expect-sha", claim["sha"])["released"] is True


def test_release_deletes_only_my_claim_or_the_exact_claim_it_was_shown():
    """`afk release` takes `--instance` like every other transition, and uses it:
    a claim another instance holds is refused. The one foreign claim it deletes is
    a `stale_closed` row — a dead peer's claim on a closed issue — and only under
    the lease of the sha rebuild read, so a claim taken meanwhile survives."""
    with sandbox() as sb:
        w = sb.clones[0]
        peer = afk(w, "claim", "9", "--instance", "peer", "--now", str(T0))

        err = afk_error(w, "release", "9", *ME)
        assert "not this fleet's claim" in err and "'peer'" in err and "--expect-sha" in err
        assert sb.remote_ref("refs/afk/claim/9") == peer["sha"]
        # the calling convention is the transitions': no --instance, no release
        assert "--instance" in afk_error(w, "release", "9")

        # somebody took the claim after it was read: the stale sha deletes nothing
        taken = afk(w, "reclaim", "9", "--instance", "third", "--expect-sha", peer["sha"],
                    "--now", str(T0 + 1))
        err = afk_error(w, "release", "9", *ME, "--expect-sha", peer["sha"])
        assert "moved" in err and "it was left alone" in err
        assert sb.remote_ref("refs/afk/claim/9") == taken["sha"]

        # shown the sha it has now, the phantom lock is gone — and stays gone, quietly
        cleared = afk(w, "release", "9", *ME, "--expect-sha", taken["sha"])
        assert cleared == {"released": True, "issue": 9, "ref": "refs/afk/claim/9"}
        assert sb.remote_ref("refs/afk/claim/9") == ""
        assert afk(w, "release", "9", *ME, "--expect-sha", taken["sha"])["released"] is True


def test_an_unreadable_remote_is_an_error_not_an_empty_fleet():
    """A scan that could not fetch must not return "no claims": read that way, a
    tick sees nothing of its own in flight, paces down to idle and stops beating,
    while every issue it holds looks free to dispatch again."""
    with sandbox() as sb:
        w = sb.clones[0]
        afk(w, "claim", "5", "--instance", "me", "--now", str(T0))
        assert [c["number"] for c in afk(w, "scan")["claims"]] == [5]   # the mirror is now warm

        bad = ("--remote", "no-such-remote")
        assert "fetch" in afk_error(w, "scan", *bad)             # …and is not served stale
        assert "fetch" in afk_error(w, "classify-claims", "--instance", "me", *bad)
        assert "fetch" in afk_error(w, "takeover", "--list", "--instance", "me", *bad)
        assert "fetch" in afk_error(w, "takeover", "--from", "x", "--instance", "me", *bad)
        # an EMPTY namespace on a reachable remote is still just empty
        assert afk(w, "scan", "--set", "claim_namespace=refs/heads") == \
            {"claims": [], "heartbeats": {}}


def test_probe_prefers_the_hidden_namespace_and_cleans_up():
    with sandbox() as sb:
        w = sb.clones[0]
        before = sb.all_refs()
        r = afk(w, "probe", "--now", str(T0))
        # one fact, said once: `blocked`. Where the refs live is in the config.
        assert set(r) == {"blocked", "config", "base"} and r["blocked"] is False, r
        # the config it hands back is canonical, with the namespace that works
        assert r["config"]["claim_namespace"] == "refs/afk"
        assert r["config"] == afk(w, "config", "--defaults")
        # it leaves nothing behind: a lingering probe ref would be fleet litter
        assert sb.all_refs() == before


def test_probe_says_whether_gate_runs_can_be_recorded_and_sweeps_the_expired():
    """ADR-0030. With a local gate the probe says, with the human present, whether
    a gate run can be put on record on this remote — a warning when it cannot,
    never an error — and sweeps the records past their day."""
    with sandbox() as sb:
        w = sb.clones[0]
        local = ("--set", "gate.ci=local", "--set", "gate.local_command=make test", "--now", str(T0),
                 "--base-branch", sb.base)
        tree = git(w, "rev-parse", "HEAD^{tree}")

        def record(command, at, message=None):
            message = message or afk_decide.record_message(
                afk_decide.GATE_RUN_RECORD, afk_decide.gate_record(tree, command, at))
            sha = git(w, "commit-tree", tree, "-m", message)
            ref = afk_decide.gate_record_ref(tree, command)
            git(w, "push", "-q", "origin", f"{sha}:{ref}")
            return ref

        fresh = record("make test", T0 - afk_decide.GATE_RECORD_TTL)
        stale = record("make old", T0 - afk_decide.GATE_RECORD_TTL - 1)
        # one written before records shared an encoding (a JSON body): it cannot be
        # read, so it is no record — swept like any other ref that is not one
        unreadable = record("make older", T0, "afk-gate green\n\n" + json.dumps(
            afk_decide.gate_record(tree, "make older", T0)))
        junk = "refs/afk/gate/not-a-record"
        git(w, "push", "-q", "origin", f"HEAD:{junk}")
        before = sb.all_refs()
        r = afk(w, "probe", *local)["gate_records"]
        assert (r["verdict"], r["pruned"]) == ("ok", 3) and "refs/afk/gate" in r["detail"], r
        # … and the base branch it was given is on record (ADR-0042)
        assert sb.all_refs() == before - {stale, unreadable, junk} | {"refs/afk/base"}
        assert fresh in sb.all_refs()
        assert not git(w, "for-each-ref", "refs/afk-gate")           # no mirror left behind

        # a remote that refuses the records: said, and the launch goes on
        sb.forbid("refs/afk/gate/")
        r = afk(w, "probe", *local)["gate_records"]
        assert r["verdict"] == "warn" and "remote rejected" in r["detail"] \
            and "every landing runs the gate itself" in r["detail"], r
        # a gate that is GitHub's checks has no records to speak of
        assert "gate_records" not in afk(w, "probe", "--now", str(T0))


def test_probe_falls_back_when_the_server_rejects_the_hidden_namespace():
    """An org ruleset forbidding non-branch refs: the probe finds the namespace that
    DOES work and folds it into the config, so every later call inherits it through
    `--config` — no separate flag for a tick to forget."""
    with sandbox() as sb:
        w = sb.clones[0]
        sb.forbid("refs/afk/")
        before = sb.all_refs()
        r = afk(w, "probe", "--now", str(T0))
        assert set(r) == {"blocked", "detail", "config", "base"} and r["blocked"] is True, r
        assert "remote rejected" in r["detail"]
        assert r["config"]["claim_namespace"] == "refs/heads"
        assert sb.all_refs() == before

        # carrying ONLY that config, the whole claim lifecycle lands on branches
        cfg = ("--config", json.dumps(r["config"]))
        claim = afk(w, "claim", "12", "--instance", "me", "--now", str(T0), *cfg)
        assert claim["won"] and claim["ref"] == "refs/heads/afk-claim/12"
        hb = afk(w, "heartbeat", "--instance", "me", "--now", str(T0), *cfg)
        assert hb["refreshed"] and hb["ref"] == "refs/heads/afk-heartbeat/me"
        assert afk(w, "classify-claims", "--instance", "me", "--now", str(T0), *cfg)["mine"] == [12]
        assert afk(w, "release", "12", *ME, *cfg)["released"] is True
        assert sb.remote_ref("refs/heads/afk-claim/12") == ""

        # on the config the probe was GIVEN, the blocked namespace is an error —
        # never a quiet "a peer won the race" that would leave the fleet idling forever
        err = afk_error(w, "claim", "13", "--instance", "me", "--now", str(T0))
        assert "not a lost race" in err and "refs/afk/claim/13" in err
        # …and with no config at all there is nothing to run on: the call is refused
        # outright rather than quietly sent to the default namespace
        err = afk_error(w, "release", "12", *ME, bare=True)
        assert "--config" in err


def test_probes_run_at_once_agree_on_the_namespace():
    """The claim is a lock only while every fleet on a repo keeps it in the same
    place, so the namespace a probe answers is the remote's alone: launches probing
    at the same instant all get it, and none of them leaves a ref behind. Probes
    that met on one ref had one keep `refs/afk` while another fell back to
    branches — two fleets, two locks. The same for whether gate runs can be put
    on record."""
    local = ("--set", "gate.ci=local", "--set", "gate.local_command=make test",
             "--now", str(T0), "--base-branch", "main")
    for _ in range(3):
        with sandbox(clones=4) as sb:
            before = sb.all_refs()
            # the base branch is on record already: launches that put it there at
            # the same instant are told to probe again, which is not this test's
            afk(sb.clones[0], "probe", *local)
            with ThreadPoolExecutor(max_workers=4) as pool:
                probes = [f.result() for f in
                          [pool.submit(afk, clone, "probe", *local) for clone in sb.clones]]
            for r in probes:
                assert r["blocked"] is False and r["config"]["claim_namespace"] == "refs/afk", r
                assert (r["gate_records"]["verdict"], r["gate_records"]["pruned"]) == ("ok", 0), r
            assert sb.all_refs() == before | {"refs/afk/base"}

        # …and on a remote that forbids the hidden namespace, they all fall back
        with sandbox(clones=4) as sb:
            sb.forbid("refs/afk/")
            before = sb.all_refs()
            with ThreadPoolExecutor(max_workers=4) as pool:
                probes = [f.result() for f in
                          [pool.submit(afk, clone, "probe", "--now", str(T0))
                           for clone in sb.clones]]
            for r in probes:
                assert r["blocked"] is True and r["config"]["claim_namespace"] == "refs/heads", r
            assert sb.all_refs() == before


def test_a_probe_ref_left_by_a_killed_launch_changes_no_later_probe():
    """A launch killed between its probe's push and its delete leaves the ref. The
    next probe answers as if it were not there — it pushes to a ref of its own —
    and deletes it, so nobody has to by hand."""
    with sandbox() as sb:
        w = sb.clones[0]
        local = ("--set", "gate.ci=local", "--set", "gate.local_command=make test",
                 "--now", str(T0), "--base-branch", sb.base)
        before = sb.all_refs()
        # `…/probe` is the one ref every probe used to push to
        for left in ("refs/afk/claim/probe", "refs/afk/claim/probe-killed",
                     "refs/afk/gate/probe", "refs/afk/gate/probe-killed"):
            git(w, "push", "-q", "origin", f"HEAD:{left}")
        r = afk(w, "probe", *local)
        assert r["blocked"] is False and r["config"]["claim_namespace"] == "refs/afk", r
        assert (r["gate_records"]["verdict"], r["gate_records"]["pruned"]) == ("ok", 0), r
        assert sb.all_refs() == before | {"refs/afk/base"}

        # the same where the claim refs are branches
        sb.forbid("refs/afk/")
        before = sb.all_refs()
        hook = os.path.join(sb.bare, "hooks", "update")
        os.rename(hook, hook + ".off")
        git(w, "push", "-q", "origin", "HEAD:refs/heads/afk-claim/probe-killed")
        os.rename(hook + ".off", hook)
        r = afk(w, "probe", "--now", str(T0))
        assert r["blocked"] is True and r["config"]["claim_namespace"] == "refs/heads", r
        assert sb.all_refs() == before


def test_probe_errors_when_no_namespace_is_usable_or_the_remote_is_unreachable():
    with sandbox() as sb:
        w = sb.clones[0]
        # an unreachable remote says NOTHING about which namespace is allowed, so
        # it must not be reported as `blocked` and silently switch namespaces
        err = afk_error(w, "probe", "--remote", "no-such-remote", "--now", str(T0))
        assert "probe push" in err and "refs/afk/claim/probe" in err
        # the server rejecting BOTH namespaces is an error with the reason attached
        sb.forbid("refs/")
        err = afk_error(w, "probe", "--now", str(T0))
        assert "both refs/afk and refs/heads" in err and "remote rejected" in err


def test_a_failed_push_is_an_error_not_a_lost_race():
    """`won: false` means exactly one thing — a peer holds the claim. A fleet that
    cannot push (auth, network, a forbidden namespace) must fail loudly: read as a
    lost race, it would skip every issue and idle forever with nothing wrong on screen."""
    with sandbox() as sb:
        w = sb.clones[0]
        bad = ("--remote", "no-such-remote")
        assert "not a lost race" in afk_error(w, "claim", "7", "--instance", "me", *bad)
        assert sb.remote_ref("refs/afk/claim/7") == ""

        # same for a reclaim: the claim has NOT moved, so a failed push is not a loss
        claim = afk(w, "claim", "8", "--instance", "dead-peer", "--now", str(T0))
        sb.forbid("refs/afk/")
        err = afk_error(w, "reclaim", "8", "--instance", "me", "--expect-sha", claim["sha"])
        assert "has not moved" in err and "remote rejected" in err
        assert sb.remote_ref("refs/afk/claim/8") == claim["sha"]      # untouched
        # …and an unreachable remote cannot even be asked whether it moved
        afk_error(w, "reclaim", "8", "--instance", "me", "--expect-sha", claim["sha"], *bad)
        # a heartbeat that cannot be written is an error too (a silent miss lapses the lease)
        assert "remote rejected" in afk_error(w, "heartbeat", "--instance", "me", "--now", str(T0))


def test_every_kind_of_record_kept_on_a_ref_round_trips_through_the_remote():
    """ADR-0031. A claim, a heartbeat and a recorded gate run are written by one
    mechanism and read back by it: written in one clone, pushed, fetched in
    another, read back equal — whatever a value holds."""
    import afk as tool
    with sandbox(clones=2) as sb:
        a, b = sb.clones
        tree = git(a, "rev-parse", "HEAD^{tree}")
        command = "make test && echo 100% > 'out file'\n# naïve"
        for kind, record, of_tree in (
                (afk_decide.CLAIM_RECORD, {"instance": "fl-1", "host": "mac.local", "ts": T0}, None),
                (afk_decide.CLAIM_RECORD, {"instance": "fl-1", "ts": T0}, None),     # no host
                (afk_decide.HEARTBEAT_RECORD, {"instance": "fl-1", "ts": T0}, None),
                (afk_decide.GATE_RUN_RECORD, afk_decide.gate_record(tree, command, T0), tree)):
            sha = tool._record_commit(kind, record, tree=of_tree, path=a)
            git(a, "push", "-q", "--force", "origin", f"{sha}:refs/afk/round-trip")
            git(b, "fetch", "-q", "origin", "refs/afk/round-trip")
            assert tool._read_record(kind, "FETCH_HEAD", path=b) == record, kind.word
            # a record of one kind is not a record of another
            others = [k for k in (afk_decide.CLAIM_RECORD, afk_decide.HEARTBEAT_RECORD,
                                  afk_decide.GATE_RUN_RECORD) if k is not kind]
            assert all(tool._read_record(k, "FETCH_HEAD", path=b) is None for k in others)
        # the gate run's commit is OF the tree it tested; the others drag nothing along
        assert git(b, "rev-parse", "FETCH_HEAD^{tree}") == tree


def test_a_claim_and_a_heartbeat_already_on_the_remote_are_still_read():
    """A fleet that updates mid-run loses no claim: the refs a fleet wrote before
    records shared one encoding are read as what they are, and what is written
    now is the same bytes — so a peer that has not updated reads it too."""
    claim_subject = "afk-claim instance=fl-7fbd5e host=Felixs-MacBook-Pro.local ts=1000000"
    heartbeat_subject = "afk-heartbeat instance=fl-7fbd5e ts=1000000"
    with sandbox() as sb:
        w = sb.clones[0]
        empty = git(w, "hash-object", "-t", "tree", os.devnull)
        claim = git(w, "commit-tree", empty, "-m", claim_subject)
        beat = git(w, "commit-tree", empty, "-m", heartbeat_subject)
        git(w, "push", "-q", "origin", f"{claim}:refs/afk/claim/71",
            f"{beat}:refs/afk/heartbeat/fl-7fbd5e")

        assert afk(w, "scan") == {
            "claims": [{"number": 71, "instance": "fl-7fbd5e", "host": "Felixs-MacBook-Pro.local",
                        "ts": T0, "sha": claim}],
            "heartbeats": {"fl-7fbd5e": T0}}
        part = afk(w, "classify-claims", "--instance", "fl-7fbd5e", "--now", str(T0 + TTL))
        assert part["mine"] == [71] and part["stale"] == []
        # its owner is still live to a peer, and a peer that races for it loses to it
        assert afk(w, "classify-claims", "--instance", "peer", "--now", str(T0 + TTL))["peer_live"] == [71]
        lost = afk(w, "claim", "71", "--instance", "peer", "--now", str(T0))
        assert lost["won"] is False and lost["owner"] == {
            "instance": "fl-7fbd5e", "host": "Felixs-MacBook-Pro.local", "ts": T0}

        # and what is written today is, byte for byte, what was written before
        won = afk(w, "claim", "72", "--instance", "fl-7fbd5e", "--host", "Felixs-MacBook-Pro.local",
                  "--now", str(T0))
        git(w, "fetch", "-q", "origin", "refs/afk/claim/72")
        assert git(w, "log", "-1", "--format=%B", won["sha"]) == claim_subject
        afk(w, "heartbeat", "--instance", "other", "--now", str(T0))
        git(w, "fetch", "-q", "origin", "refs/afk/heartbeat/other")
        assert git(w, "log", "-1", "--format=%B", "FETCH_HEAD") == "afk-heartbeat instance=other ts=1000000"


def test_malformed_refs_in_the_namespace_are_ignored_not_fatal():
    """Anything can be pushed under a ref namespace. A claim ref that is not an issue
    number, or a marker with no fields, must not take the scan (and so every tick) down."""
    with sandbox() as sb:
        w = sb.clones[0]
        afk(w, "claim", "5", "--instance", "me", "--now", str(T0))
        empty = git(w, "hash-object", "-t", "tree", os.devnull)
        junk = git(w, "commit-tree", empty, "-m", "not a marker at all")
        # a claim that names nobody, and a heartbeat that says no time: neither is a record
        nobody = git(w, "commit-tree", empty, "-m", f"afk-claim host=mac ts={T0}")
        timeless = git(w, "commit-tree", empty, "-m", "afk-heartbeat instance=me ts=soon")
        git(w, "push", "-q", "origin", f"{junk}:refs/afk/claim/not-a-number",
            f"{junk}:refs/afk/claim/6", f"{junk}:refs/afk/heartbeat/ghost",
            f"{nobody}:refs/afk/claim/7", f"{timeless}:refs/afk/heartbeat/me")

        scan = afk(w, "scan")
        by = {c["number"]: c for c in scan["claims"]}
        assert set(by) == {5, 6, 7}                    # the non-numeric ref is not a claim
        assert by[6]["instance"] is None and by[6]["ts"] is None and by[6]["sha"] == junk
        # a record missing a required field is no record at all — not one with a hole in it
        assert by[7] == {"number": 7, "instance": None, "host": None, "ts": None, "sha": nobody}
        assert scan["heartbeats"] == {}                # a heartbeat with no ts is no heartbeat
        # an ownerless claim is nobody's: reclaimable as stale, never "mine"
        part = afk(w, "classify-claims", "--instance", "me", "--now", str(T0))
        assert part["mine"] == [5] and part["stale"] == [6, 7] and part["peer_live"] == []


def test_every_ref_op_round_trips_under_the_refs_heads_fallback():
    """When an org ruleset forbids non-branch refs, every op must work unchanged
    under `refs/heads/afk-*` — the fallback is only worth having if it is complete."""
    NS = ("--set", "claim_namespace=refs/heads")
    with sandbox() as sb:
        w = sb.clones[0]

        # probed under the fallback, the verdict is honest about what it means:
        # ordinary branches, so `on: push` CI will fire on claim churn
        before = sb.all_refs()
        r = afk(w, "probe", *NS, "--now", str(T0))
        # chosen, not fallen back to: nothing was rejected, so nothing is `blocked`
        assert r["blocked"] is False and r["config"]["claim_namespace"] == "refs/heads"
        assert sb.all_refs() == before
        # the two namespaces are the only two: a third layout is refused before any push
        for ns in ("refs/heads/afk", "refs/x"):
            assert "claim_namespace" in afk_error(w, "claim", "12", "--instance", "me",
                                                  "--set", f"claim_namespace={ns}")
        assert sb.all_refs() == before

        claim = afk(w, "claim", "12", "--instance", "me", *NS, "--now", str(T0))
        assert claim["won"] and claim["ref"] == "refs/heads/afk-claim/12"
        assert sb.remote_ref("refs/heads/afk-claim/12") == claim["sha"]
        assert sb.remote_ref("refs/afk/claim/12") == ""          # nothing in the hidden ns

        hb = afk(w, "heartbeat", "--instance", "me", *NS, "--now", str(T0))
        assert hb["refreshed"] and hb["ref"] == "refs/heads/afk-heartbeat/me"

        scan = afk(w, "scan", *NS)
        assert [c["number"] for c in scan["claims"]] == [12]
        assert scan["heartbeats"] == {"me": T0}
        assert afk(w, "classify-claims", "--instance", "me", *NS,
                   "--now", str(T0))["mine"] == [12]

        # a losing create and a lease-checked takeover both behave the same here
        assert afk(w, "claim", "12", "--instance", "peer", *NS, "--now", str(T0))["won"] is False
        taken = afk(w, "reclaim", "12", "--instance", "peer", *NS,
                    "--expect-sha", scan["claims"][0]["sha"], "--now", str(T0 + 1))
        assert taken["won"] is True
        assert afk(w, "reclaim", "12", "--instance", "third", *NS,
                   "--expect-sha", scan["claims"][0]["sha"], "--now", str(T0 + 2))["won"] is False

        assert afk(w, "release", "12", "--instance", "peer", *NS)["released"] is True
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
            "--now", str(T0 - TTL - 99))
        afk(w, "heartbeat", "--instance", "live-fleet", "--now", str(T0 - 10))

        rows = {r["instance"]: r for r in
                afk(w, "takeover", "--list", "--instance", "new-fleet",
                    "--now", str(T0))["instances"]}
        assert rows["dead-fleet"]["claims"] == [21, 22] and rows["dead-fleet"]["fresh"] is False
        assert rows["dead-fleet"]["heartbeat_age"] == TTL + 99
        assert rows["live-fleet"]["fresh"] is True and rows["live-fleet"]["claim_count"] == 1

        # a live-looking fleet: warned about, and NOTHING is taken
        held = afk(w, "takeover", "--from", "live-fleet", "--instance", "new-fleet",
                   "--now", str(T0))
        assert held["action"] == "confirm" and held["taken"] == []
        assert sb.remote_ref("refs/afk/claim/23")
        assert afk(w, "classify-claims", "--instance", "new-fleet",
                   "--now", str(T0))["mine"] == []

        # the dead one transfers wholesale, re-stamped with my instance id
        took = afk(w, "takeover", "--from", "dead-fleet", "--instance", "new-fleet",
                   "--now", str(T0))
        assert took["action"] == "taken" and took["taken"] == [21, 22] and took["lost"] == []
        assert took["as"] == "new-fleet" and took["instance"] == "dead-fleet"
        part = afk(w, "classify-claims", "--instance", "new-fleet",
                   "--now", str(T0))
        assert part["mine"] == [21, 22] and part["peer_live"] == [23]

        # --yes is the informed human override of the fresh-heartbeat hold
        forced = afk(w, "takeover", "--from", "live-fleet", "--instance", "new-fleet", "--yes",
                     "--now", str(T0))
        assert forced["action"] == "taken" and forced["taken"] == [23]
        assert afk(w, "classify-claims", "--instance", "new-fleet",
                   "--now", str(T0))["mine"] == [21, 22, 23]

        # and taking from myself is refused, not silently re-stamped
        assert afk(w, "takeover", "--from", "new-fleet", "--instance", "new-fleet",
                   "--now", str(T0))["action"] == "error"
        # `--instance` is MY id on every subcommand, takeover included; without a
        # `--from` there is nothing to take, and that is said rather than guessed
        assert "--from" in afk_error(w, "takeover", "--instance", "new-fleet")
        # the lease the freshness check uses is the fleet's one lease: a beat that old is stale
        rows = {r["instance"]: r for r in
                afk(w, "takeover", "--list", "--instance", "x",
                    "--now", str(T0 + TTL + 1))["instances"]}
        assert rows["live-fleet"]["fresh"] is False


def test_recovery_reads_pushed_progress_from_the_remote_alone():
    """`afk recovery` (ADR-0011) tier 2 vs tier 3: with no local worktree, the only
    evidence a dead worker left is its pushed branch, which has to be recognised
    from the issue number — the claim ref never records a branch name."""
    with sandbox() as sb:
        w = sb.clones[0]
        cfg = json.dumps({"base_branch": sb.base})

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

        # an earlier attempt left a second, shorter branch behind: the one FURTHEST
        # ahead is the progress worth continuing, whatever its name sorts as
        git(w, "checkout", "-q", "-b", "aaa/issue-31-first-try", sb.base)
        with open(os.path.join(w, "old.txt"), "w") as f:
            f.write("old\n")
        git(w, "add", "-A")
        git(w, "commit", "-qm", "old attempt")
        git(w, "push", "-q", "origin", "HEAD")
        r = afk(w, "recovery", "--issue", "31", "--no-worktree", "--config", cfg)
        assert r["branch"]["candidates"] == ["aaa/issue-31-first-try",
                                             "sunfmin/issue-31-continuation"]
        assert r["branch"]["name"] == "sunfmin/issue-31-continuation"
        assert r["branch"]["commits_ahead"] == 2 and r["tier"] == 2
        # a branch named outright is measured as given, with no discovery
        r = afk(w, "recovery", "--issue", "31", "--no-worktree", "--config", cfg,
                "--branch", "aaa/issue-31-first-try")
        assert (r["branch"]["name"], r["branch"]["commits_ahead"]) == ("aaa/issue-31-first-try", 1)
        assert r["branch"]["candidates"] == []

        # a branch that was never pushed has nothing ahead — measured as unknown, tier 3
        r = afk(w, "recovery", "--issue", "31", "--no-worktree", "--config", cfg,
                "--branch", "sunfmin/issue-31-never-pushed")
        assert (r["branch"]["commits_ahead"], r["tier"]) == (None, 3)
        # …but a remote that cannot be READ is not "nothing pushed": tier 3 is the one
        # tier that tears a worktree down, so "could not look" must never select it
        for how in ((), ("--branch", "sunfmin/issue-31-continuation")):
            err = afk_error(w, "recovery", "--issue", "31", "--no-worktree", "--config", cfg,
                            "--remote", "no-such-remote", *how)
            assert "no-such-remote" in err, err
        # a base branch the remote does not have is a config mistake, said as one
        err = afk_error(w, "recovery", "--issue", "31", "--no-worktree", "--config", cfg,
                        "--set", "base_branch=no-such-base")
        assert "no-such-base" in err
