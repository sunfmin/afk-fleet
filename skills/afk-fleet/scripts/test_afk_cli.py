#!/usr/bin/env python3
"""
Integration tests for the afk-fleet subcommands that talk to GitHub's API, orca
and the login shell — run through the real CLI, end to end, and still offline.

Run: python3 test_afk_cli.py   (or under pytest, beside the other two suites)

`test_afk_decide.py` pins the pure verdicts and `test_afk_refs.py` the ref races;
what neither reaches is the seam between them: that `afk rebuild` asks gh for the
fields its verdicts read, that `afk no-pr` derives its inputs from a real worktree
and real comments, that a flag beats `--config` beats the defaults table on every
subcommand, and that the docs name subcommands and flags that exist.

Nothing is injected into afk.py to make that possible. The outside world is faked
where it actually lives — executables on PATH:

  gh    a stand-in backed by one JSON state file. It projects exactly the fields
        asked for (asking for one GitHub does not have is a KeyError), applies only
        the `--jq` filters it knows (a changed filter fails loudly rather than
        silently diverging), and logs every call so a test can assert what was —
        and was NOT — asked.
  orca  prints a canned `worktree list --json` document.
  $SHELL  a stand-in login shell that knows a fixed set of aliases.

git is real, against the same bare-repo sandbox as `test_afk_refs.py`; `--repo
owner/name` reaches it through a `url.<bare>.insteadOf` rewrite in the clone.
"""
import json
import os
import re
import sys
import time
from contextlib import contextmanager

import afk
import afk_decide
from test_afk_refs import ENV, T0, TTL, afk as run, afk_error, git, sandbox

REPO = "acme/widgets"
HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.dirname(HERE)

FAKE_GH = r'''#!%(python)s
import json, os, sys

path = os.environ["AFK_FAKE_GH"]
with open(path) as f:
    st = json.load(f)
argv = sys.argv[1:]
st.setdefault("calls", []).append(argv)


def finish(out=None, code=0, err=""):
    with open(path, "w") as f:
        json.dump(st, f)
    if out is not None:
        print(out)
    sys.stderr.write(err)
    sys.exit(code)


def opt(name, default=None):
    return argv[argv.index(name) + 1] if name in argv else default


if argv[:2] in (["issue", "list"], ["pr", "list"]):
    if opt("--repo") != st["repo"]:
        finish(code=1, err="fake gh: unknown repo %%s\n" %% opt("--repo"))
    assert opt("--state") == "open", argv
    rows = [r for r in st["issues" if argv[0] == "issue" else "prs"]
            if r.get("state", "open") == "open"]
    fields = opt("--json").split(",")
    finish(json.dumps([{k: r[k] for k in fields} for r in rows]))

assert argv[0] == "api", argv
endpoint = next(x for x in argv[1:] if x.startswith("repos/"))
prefix = "repos/%%s/" %% st["repo"]
if not endpoint.startswith(prefix):
    finish(code=1, err="gh: Not Found (HTTP 404)\n")
parts = endpoint[len(prefix):].split("/")
method, jq = opt("--method", "GET"), opt("--jq")
body = next((x[len("body="):] for x in argv if x.startswith("body=")), None)
comments = st.setdefault("comments", {})

if parts[0] == "issues" and parts[1:2] == ["comments"]:            # PATCH one comment
    assert method == "PATCH" and body is not None, argv
    for rows in comments.values():
        for c in rows:
            if str(c["id"]) == parts[2]:
                c["body"] = body
                finish(json.dumps({"id": c["id"]}))
    finish(code=1, err="gh: Not Found (HTTP 404)\n")

if parts[0] == "issues" and parts[2:] == ["comments"]:
    rows = comments.setdefault(parts[1], [])
    if method == "POST":
        assert body is not None, argv
        new = 1 + max([c["id"] for rs in comments.values() for c in rs], default=1000)
        rows.append({"id": new, "body": body, "html_url": "https://gh/c/%%d" %% new})
        finish(json.dumps({"id": new}))
    assert jq == ".[] | {id, body, url: .html_url}", "fake gh: unsupported jq %%r" %% jq
    finish("\n".join(json.dumps({"id": c["id"], "body": c["body"], "url": c["html_url"]})
                     for c in rows))

if parts[0] == "issues" and len(parts) == 2:
    row = next((r for r in st["issues"] if str(r["number"]) == parts[1]), None)
    if row is None:
        finish(code=1, err="gh: Not Found (HTTP 404)\n")
    if jq == ".state":
        finish(row.get("state", "open"))
    assert jq == ".issue_dependencies_summary.blocked_by", "fake gh: unsupported jq %%r" %% jq
    finish(json.dumps(row.get("blocked_by")))

if parts[0] == "branches" and parts[2:] == ["protection"]:
    prot = st.get("protection", {}).get(parts[1])
    if prot is None:
        finish(code=1, err="gh: Branch not protected (HTTP 404)\n")
    if "__error__" in prot:
        finish(code=1, err=prot["__error__"] + "\n")
    finish(json.dumps(prot))

finish(code=1, err="fake gh: unsupported call %%r\n" %% argv)
'''

FAKE_ORCA = r'''#!%(python)s
import os, sys
assert sys.argv[1:] == ["worktree", "list", "--json"], sys.argv
with open(os.environ["AFK_FAKE_ORCA"]) as f:
    sys.stdout.write(f.read())
sys.exit(int(os.environ.get("AFK_FAKE_ORCA_EXIT", "0")))
'''

# A login shell that knows two aliases. Like zsh, an unresolvable `type` prints
# "not found" on STDOUT and exits 1 — the trap `_login_shell` exists to avoid.
FAKE_SHELL = r'''#!%(python)s
import sys
assert sys.argv[1] == "-ic", sys.argv
script = sys.argv[2]
ALIASES = {"ckimi": "(eval x && claude --dangerously-skip-permissions)", "cplain": "claude"}
if script == "alias":
    print("\n".join("%%s='%%s'" %% kv for kv in ALIASES.items()) + "\nll='ls -lah'")
    sys.exit(0)
assert script.startswith("type -- "), script
word = script[len("type -- "):]
if word in ALIASES:
    print("%%s is an alias for %%s" %% (word, ALIASES[word]))
    sys.exit(0)
print("%%s not found" %% word)
sys.exit(1)
'''


def issue(n, *labels, **extra):
    """One issue as GitHub's API returns it (labels are objects, not names)."""
    return {"number": n, "title": f"issue {n}", "updatedAt": f"T{n}",
            "labels": [{"name": lb, "color": "ededed"} for lb in labels], **extra}


def pr(n, closes, conclusion="SUCCESS", **extra):
    return {"number": n, "headRefOid": f"sha{n}", "updatedAt": f"P{n}",
            "statusCheckRollup": [{"name": "ci", "status": "COMPLETED", "conclusion": conclusion}],
            "closingIssuesReferences": [{"number": closes, "url": "u"}], **extra}


class World:
    """One sandbox clone wired to the fakes: `afk(...)` runs the real CLI in it."""

    def __init__(self, sb, **state):
        self.sb, self.cwd = sb, sb.clones[0]
        git(self.cwd, "config", f"url.{sb.bare}.insteadOf", f"https://github.com/{REPO}.git")
        bindir = os.path.join(sb.root, "bin")
        os.mkdir(bindir)
        for name, src in (("gh", FAKE_GH), ("orca", FAKE_ORCA), ("fakeshell", FAKE_SHELL)):
            exe = os.path.join(bindir, name)
            with open(exe, "w") as f:
                f.write(src % {"python": sys.executable})
            os.chmod(exe, 0o755)
        self.gh_file = os.path.join(sb.root, "gh.json")
        self.orca_file = os.path.join(sb.root, "orca.json")
        self.env = {**ENV, "PATH": bindir + os.pathsep + ENV["PATH"],
                    "AFK_FAKE_GH": self.gh_file, "AFK_FAKE_ORCA": self.orca_file,
                    "SHELL": os.path.join(bindir, "fakeshell")}
        for leak in ("QODERCN_CLI", "ANTHROPIC_BASE_URL"):
            self.env.pop(leak, None)
        self.set(**{"repo": REPO, "issues": [], "prs": [], "comments": {}, **state})
        self.orca([])

    def set(self, **state):
        cur = self.state() if os.path.exists(self.gh_file) else {}
        with open(self.gh_file, "w") as f:
            json.dump({**cur, **state}, f)

    def state(self):
        with open(self.gh_file) as f:
            return json.load(f)

    def calls(self, reset=True):
        """Every gh invocation since the last look, as argv lists."""
        calls = self.state().get("calls", [])
        if reset:
            self.set(calls=[])
        return calls

    def orca(self, worktrees):
        with open(self.orca_file, "w") as f:
            json.dump({"id": "x", "ok": True, "result": {"worktrees": worktrees, "totalCount":
                                                         len(worktrees)}}, f)

    def afk(self, *args, env=None):
        return run(self.cwd, *args, env={**self.env, **(env or {})})

    def error(self, *args, env=None):
        return afk_error(self.cwd, *args, env={**self.env, **(env or {})})

    def commit(self, name, branch=None):
        if branch:
            git(self.cwd, "checkout", "-q", "-b", branch)
        with open(os.path.join(self.cwd, name), "w") as f:
            f.write(name + "\n")
        git(self.cwd, "add", "-A")
        git(self.cwd, "commit", "-qm", name)


@contextmanager
def world(**state):
    """A fresh sandbox wired to the fakes, with `state` as GitHub's initial state."""
    with sandbox() as sb:
        yield World(sb, **state)


R = ("--repo", REPO)


# --------------------------------------------------------------------------- #
# rebuild / fingerprint                                                        #
# --------------------------------------------------------------------------- #

def test_rebuild_assembles_the_working_set_from_gh_and_refs():
    issues = [issue(1, "ready-for-agent"),
              issue(2, "ready-for-agent", blocked_by=1),          # a candidate, but blocked
              issue(3, "ready-for-agent", "afk-attempt/1"),       # mine, PR green
              issue(4, "ready-for-agent"),                        # mine, no PR
              issue(5, "ready-for-agent"),                        # a live peer's
              issue(6, "ready-for-agent"),                        # a dead peer's
              issue(7, "ready-for-agent", "epic"),
              issue(8),                                           # not ready
              issue(9, "ready-for-agent", state="closed")]        # gh never lists it
    with world(issues=issues, prs=[pr(30, closes=3)]) as w:
        for n, inst in ((3, "me"), (4, "me"), (5, "peer-live"), (6, "peer-dead")):
            assert w.afk("claim", str(n), "--instance", inst, "--now", str(T0), *R)["won"]
        w.afk("heartbeat", "--instance", "peer-live", "--now", str(T0 - 60), *R)
        w.afk("heartbeat", "--instance", "peer-dead", "--now", str(T0 - TTL - 60), *R)
        w.calls()

        ws = w.afk("rebuild", "--instance", "me", "--now", str(T0), *R)

        assert ws["frontier"]["dispatch"] == [{"number": 1, "title": "issue 1"}]
        reasons = {e["number"]: e["reason"] for e in ws["frontier"]["excluded"]}
        assert "1 open blocker" in reasons[2] and "epic" in reasons[7]
        assert "no ready-for-agent label" == reasons[8]
        assert "already claimed" in reasons[3] and "already claimed" in reasons[5]
        assert 9 not in reasons                                    # closed: never seen at all

        mine = {m["number"]: m for m in ws["mine"]}
        assert set(mine) == {3, 4}
        assert (mine[3]["status"], mine[3]["board_phase"], mine[3]["pr"], mine[3]["checks"]) == \
            ("awaiting_merge", "awaiting_merge", 30, "green")
        assert mine[3]["attempt_labels"] == ["afk-attempt/1"]      # read off gh's label objects
        assert (mine[4]["status"], mine[4]["board_phase"], mine[4]["pr"]) == ("no_pr", "claimed", None)
        assert ws["peer_live"] == [{"number": 5, "instance": "peer-live"}]
        assert [(s["number"], s["instance"]) for s in ws["stale"]] == [(6, "peer-dead")]
        assert ws["stale"][0]["sha"] == w.sb.remote_ref("refs/afk/claim/6")
        assert ws["now"] == T0

        # the per-issue blocked_by read is paid ONLY by issues that passed every
        # cheaper check — two of nine here, not one per open issue
        blocker_reads = sorted(c[1] for c in w.calls() if c[0] == "api")
        assert blocker_reads == [f"repos/{REPO}/issues/1", f"repos/{REPO}/issues/2"], blocker_reads

        # the stale sha it reported is exactly what reclaim's compare-and-swap needs
        took = w.afk("reclaim", "6", "--instance", "me", "--expect-sha", ws["stale"][0]["sha"],
                     "--now", str(T0), *R)
        assert took["won"] is True
        ws = w.afk("rebuild", "--instance", "me", "--now", str(T0), *R)
        assert [m["number"] for m in ws["mine"]] == [3, 4, 6] and ws["stale"] == []


def test_rebuild_reads_the_dispatch_contract_from_config_and_flags():
    issues = [issue(1, "go"), issue(2, "go", "big"), issue(3, "ready-for-agent")]
    with world(issues=issues, prs=[pr(30, closes=1, conclusion="FAILURE")]) as w:
        w.afk("claim", "1", "--instance", "me", "--now", str(T0), *R)
        cfg = json.dumps({"ready_label": "go", "epic_labels": ["big"],
                          "gate": {"ci": "local", "local_command": "true"}})

        ws = w.afk("rebuild", "--instance", "me", "--now", str(T0), *R, "--config", cfg)
        assert ws["frontier"]["dispatch"] == []
        reasons = {e["number"]: e["reason"] for e in ws["frontier"]["excluded"]}
        assert reasons[2] == "epic label (big)" and reasons[3] == "no go label"
        # gate.ci: local — red remote checks are not the gate, and the board must not
        # claim a green gate the merge sequence has not run yet
        assert (ws["mine"][0]["status"], ws["mine"][0]["board_phase"]) == ("awaiting_merge", "pr_open")

        # a flag beats the config it was given alongside
        ws = w.afk("rebuild", "--instance", "me", "--now", str(T0), *R, "--config", cfg,
                   "--ready-label", "ready-for-agent", "--epic-labels", "x,y", "--ci", "required")
        assert ws["frontier"]["dispatch"] == [{"number": 3, "title": "issue 3"}]
        assert (ws["mine"][0]["status"], ws["mine"][0]["board_phase"]) == ("failure", "ci_failed")

        # --repo is the one repo handle, and a wrong one is an error, not an empty fleet
        assert "unknown repo" in w.error("rebuild", "--instance", "me", "--repo", "acme/other")


def test_fingerprint_gate_skips_until_observable_state_moves():
    with world(issues=[issue(1, "ready-for-agent")], prs=[pr(30, closes=2)]) as w:
        first = w.afk("fingerprint", *R)
        assert (first["action"], first["reason"], first["skips"]) == ("tick", "first", 0)
        fp = first["fingerprint"]
        # the digest is the SAME one rebuild reports: one gatherer, one function
        assert w.afk("rebuild", "--instance", "me", *R)["fingerprint"] == fp

        skip = w.afk("fingerprint", *R, "--last", fp, "--skips", "0")
        assert (skip["action"], skip["reason"], skip["skips"], skip["fingerprint"]) == \
            ("skip", "unchanged", 1, fp)
        # the forced full tick: default every 6, from --config, or the flag — flag wins
        assert w.afk("fingerprint", *R, "--last", fp, "--skips", "5")["reason"] == "forced"
        short = json.dumps({"force_tick_after_skips": 2})
        assert w.afk("fingerprint", *R, "--last", fp, "--skips", "1", "--config", short)["reason"] == "forced"
        assert w.afk("fingerprint", *R, "--last", fp, "--skips", "1", "--config", short,
                     "--force-after", "9")["action"] == "skip"

        # each kind of movement a tick would act on moves the digest
        w.afk("claim", "1", "--instance", "peer", "--now", str(T0), *R)
        claimed = w.afk("fingerprint", *R, "--last", fp)
        assert (claimed["action"], claimed["reason"]) == ("tick", "changed")
        w.set(prs=[pr(30, closes=2, conclusion="FAILURE")])
        red = w.afk("fingerprint", *R, "--last", claimed["fingerprint"])
        assert red["reason"] == "changed"
        w.set(issues=[issue(1)])                                   # ready label pulled
        assert w.afk("fingerprint", *R, "--last", red["fingerprint"])["reason"] == "changed"
        # …and a heartbeat does not: the launcher's own skip-cycle refresh must not defeat the gate
        now_fp = w.afk("fingerprint", *R)["fingerprint"]
        w.afk("heartbeat", "--instance", "peer", "--now", str(T0), *R)
        assert w.afk("fingerprint", *R, "--last", now_fp)["action"] == "skip"


# --------------------------------------------------------------------------- #
# no-pr                                                                        #
# --------------------------------------------------------------------------- #

def _marker(phase, extra=""):
    return f"<!--afk:verdict n=4 phase={phase}{extra}-->\nexplanation"


def _comment(cid, body):
    return {"id": cid, "body": body, "html_url": f"https://gh/c/{cid}"}


def test_no_pr_gathers_every_signal_and_decides_in_one_call():
    with world(issues=[issue(4, "ready-for-agent"), issue(41), issue(42)]) as w:
        cfg = json.dumps({"base_branch": w.sb.base, "worker_idle_grace_seconds": 300})
        base = ("no-pr", "--issue", "4", "--worktree", w.cwd, *R, "--config", cfg)
        real_now = int(time.time())
        soon, later = str(real_now + 30), str(real_now + 5000)

        # a clean worktree at base, no verdict, touched seconds ago → still coding
        r = w.afk(*base, "--terminal", "idle", "--now", soon)
        assert (r["outcome"], r["action"]) == ("coding", "leave"), r
        assert r["progress"]["commits_ahead"] == 0 and r["progress"]["dirty"] is False
        assert 0 <= r["idle_seconds"] < 300 and r["verdict"]["found"] is False
        assert r["issue"] == 4 and r["open_blockers"] == []

        # idle_seconds is derived HERE, from the freshest of commit / file / terminal
        # clocks: the tick supplies a terminal reading, never arithmetic
        r = w.afk(*base, "--terminal", "idle", "--now", later)
        assert 4900 < r["idle_seconds"] < 5100                    # the worktree's own clocks
        assert (r["outcome"], r["action"]) == ("idle_failed", "next_attempt")
        r = w.afk(*base, "--terminal", "idle", "--now", later, "--terminal-idle-seconds", "12")
        assert r["idle_seconds"] == 12 and r["outcome"] == "coding"   # the terminal is fresher
        # busy and none are the terminal's alone to say
        assert w.afk(*base, "--terminal", "busy", "--now", later)["outcome"] == "coding"
        assert w.afk(*base, "--terminal", "none", "--now", soon)["action"] == "orphan"
        # grace: flag beats config
        r = w.afk(*base, "--terminal", "idle", "--now", later, "--grace", "99999")
        assert r["outcome"] == "coding"

        # the worker declared itself blocked on #41 and #42: their REAL state routes it
        w.set(comments={"4": [_comment(1, "a human note"),
                              _comment(2, _marker("giving-up")),
                              _comment(3, _marker("blocked", " blocked_by=41,42 reason=needs both"))]})
        w.calls()
        r = w.afk(*base, "--terminal", "idle", "--now", later)
        assert (r["outcome"], r["action"], r["open_blockers"]) == ("idle_blocked", "escalate", [41, 42])
        assert r["verdict"]["phase"] == "blocked" and r["verdict"]["reason"] == "needs both"
        assert r["verdict"]["comment_url"] == "https://gh/c/3"     # the LATEST marker wins
        states = sorted(c[1] for c in w.calls() if "--jq" in c and ".state" in c)
        assert states == [f"repos/{REPO}/issues/41", f"repos/{REPO}/issues/42"]

        w.set(issues=[issue(4), issue(41, state="closed"), issue(42)])
        r = w.afk(*base, "--terminal", "idle", "--now", later)
        assert (r["action"], r["open_blockers"]) == ("escalate", [42])
        w.set(issues=[issue(4), issue(41, state="closed"), issue(42, state="closed")])
        r = w.afk(*base, "--terminal", "idle", "--now", later)
        assert (r["outcome"], r["action"], r["open_blockers"]) == ("idle_blocked", "redispatch", [])
        # a blocker that cannot be read at all is not provably closed
        w.set(issues=[issue(4), issue(41, state="closed")])       # #42 is now a 404
        assert w.afk(*base, "--terminal", "idle", "--now", later)["open_blockers"] == [42]

        # already-satisfied over a pristine branch → close + release…
        w.set(comments={"4": [_comment(9, _marker("already-satisfied"))]})
        r = w.afk(*base, "--terminal", "idle", "--now", later)
        assert (r["outcome"], r["action"]) == ("idle_done", "close_release")
        # …but real work on the branch refutes it: commits, or just a dirty tree
        with open(os.path.join(w.cwd, "wip.txt"), "w") as f:
            f.write("uncommitted\n")
        later = str(int(time.time()) + 5000)
        r = w.afk(*base, "--terminal", "idle", "--now", later)
        assert r["progress"]["dirty"] is True and r["progress"]["commits_ahead"] == 0
        assert (r["outcome"], r["action"]) == ("idle_failed", "next_attempt")
        w.commit("done.txt", branch="sunfmin/issue-4-x")
        later = str(int(time.time()) + 5000)
        r = w.afk(*base, "--terminal", "idle", "--now", later)
        assert r["progress"]["commits_ahead"] == 2 - 1 and r["progress"]["dirty"] is False
        assert (r["outcome"], r["action"]) == ("idle_failed", "next_attempt")
        # the base it counts against is config's, and --base overrides it
        r = w.afk(*base, "--terminal", "idle", "--now", later, "--base", "HEAD")
        assert r["progress"]["commits_ahead"] == 0 and r["outcome"] == "idle_done"


def test_no_pr_without_a_worktree_and_with_bad_input():
    with world(issues=[issue(4)], comments={"4": [_comment(1, _marker("giving-up"))]}) as w:
        # no worktree at all (it lives on another machine): progress is unknown, the
        # terminal reading is the only clock, and the verdict still routes
        r = w.afk("no-pr", "--issue", "4", "--terminal", "idle", "--terminal-idle-seconds", "900", *R)
        assert r["progress"] == {} and r["idle_seconds"] == 900
        assert (r["outcome"], r["action"]) == ("idle_failed", "next_attempt")
        r = w.afk("no-pr", "--issue", "4", "--terminal", "idle", *R)
        assert r["idle_seconds"] is None and r["outcome"] == "idle_failed"

        # a worktree path that does not exist is a mistake, never "no progress":
        # read as empty it could close an issue whose branch holds real work
        err = w.error("no-pr", "--issue", "4", "--terminal", "idle", "--worktree", "/no/such/dir", *R)
        assert "worktree not found" in err
        # gh failing is an error too, not an absent verdict
        assert "gh api" in w.error("no-pr", "--issue", "4", "--terminal", "idle",
                                   "--repo", "acme/other")


# --------------------------------------------------------------------------- #
# status board                                                                 #
# --------------------------------------------------------------------------- #

def test_status_upserts_exactly_one_comment_and_writes_only_on_change():
    with world(issues=[issue(4)], comments={"4": [_comment(1, "a human comment")]}) as w:
        def writes():
            return [c for c in w.calls() if "--method" in c]

        def board():
            rows = [c for c in w.state()["comments"]["4"] if afk_decide.STATUS_MARKER in c["body"]]
            assert len(rows) == 1, rows
            return rows[0]

        w.calls()
        r = w.afk("status", "4", "--phase", "claimed", "--instance", "fl-1", *R)
        assert r["action"] == "created" and r["issue"] == 4
        assert len(writes()) == 1
        assert "认领方 `fl-1`" in board()["body"] and "尚无 PR" in board()["body"]
        cid = board()["id"]
        assert r["comment_id"] == cid

        # a re-entrant tick with the same state touches nothing
        r = w.afk("status", "4", "--phase", "claimed", "--instance", "fl-1", *R)
        assert (r["action"], r["comment_id"]) == ("unchanged", cid)
        assert writes() == []

        # the lifecycle moves → the SAME comment is edited in place, never appended
        r = w.afk("status", "4", "--phase", "pr_open", "--instance", "fl-1", "--pr", "77", *R)
        assert (r["action"], r["comment_id"]) == ("updated", cid)
        assert [c[c.index("--method") + 1] for c in writes()] == ["PATCH"]
        assert "- [x] PR 已开 (#77) · 等 CI" in board()["body"]
        assert len(w.state()["comments"]["4"]) == 2               # the human's + the board

        # retry_max comes from config `retry` — the tick passes only the attempt
        cfg = json.dumps({"retry": 5})
        w.afk("status", "4", "--phase", "ci_failed", "--pr", "77", "--attempt", "2", *R, "--config", cfg)
        assert "CI 失败,修复重试中(2/5)" in board()["body"]
        # …and so does the gate's name: a local gate is never called CI
        cfg = json.dumps({"gate": {"ci": "local", "local_command": "make test"}})
        w.afk("status", "4", "--phase", "pr_open", "--pr", "77", *R, "--config", cfg)
        assert "等 本地门" in board()["body"] and "CI" not in board()["body"]

        for phase in ("merged", "escalated"):                      # the terminal phases
            assert w.afk("status", "4", "--phase", phase, "--pr", "77", *R)["action"] == "updated"
        assert "已升级给人处理" in board()["body"]
        # an issue with no comments yet, and an unknown phase
        assert w.afk("status", "5", "--phase", "claimed", *R)["action"] == "created"


# --------------------------------------------------------------------------- #
# bootstrap: probe protection / worker-command / config                        #
# --------------------------------------------------------------------------- #

def test_probe_checks_branch_protection_only_for_a_local_gate():
    local = json.dumps({"gate": {"ci": "local", "local_command": "make test"},
                        "merge": {"target": "main"}})
    with world() as w:
        # required mode: checks ARE the gate, so protection is not even read
        r = w.afk("probe", *R, "--now", str(T0))
        assert "protection" not in r and w.calls() == []

        # local mode, unprotected target → fine
        r = w.afk("probe", *R, "--config", local, "--now", str(T0))
        assert (r["protection"]["verdict"], r["protection"]["branch"]) == ("ok", "main")
        assert r["config"]["gate"]["ci"] == "local"               # the config rides back whole

        # local mode + REQUIRED status checks → every merge would be rejected: hard error
        w.set(protection={"main": {"required_status_checks": {"contexts": ["ci/build"],
                                                              "checks": [{"context": "ci/lint"}]}},
                          "release": {"__error__": "gh: Resource not accessible (HTTP 403)"}})
        r = w.afk("probe", *R, "--config", local, "--now", str(T0))
        assert r["protection"]["verdict"] == "error"
        assert r["protection"]["required_checks"] == ["ci/build", "ci/lint"]
        # an unreadable protection is a warning, never a guess; --target picks the branch
        r = w.afk("probe", *R, "--config", local, "--target", "release", "--now", str(T0))
        assert (r["protection"]["verdict"], r["protection"]["branch"]) == ("warn", "release")
        assert "403" in r["protection"]["detail"]
        # no --repo → the refs still probe (via origin), protection is flagged unchecked
        r = w.afk("probe", "--config", local, "--now", str(T0))
        assert r["protection"]["verdict"] == "warn" and "--repo" in r["protection"]["detail"]


def test_worker_command_settles_the_launch_command_from_env_and_shell():
    with world() as w:
        # a stock Claude launcher is never asked
        r = w.afk("worker-command")
        assert (r["status"], r["command"], r["runtime"]) == \
            ("stock", "claude --dangerously-skip-permissions", "claude")
        assert "candidates" not in r

        # a custom provider → ask, offering the login shell's Claude-starting aliases
        kimi = {"ANTHROPIC_BASE_URL": "https://api.kimi.com/coding/"}
        r = w.afk("worker-command", env=kimi)
        assert (r["status"], r["command"], r["base_url"]) == ("ask", None, kimi["ANTHROPIC_BASE_URL"])
        assert {c["name"]: c["wraps_env"] for c in r["candidates"]} == {"ckimi": True, "cplain": False}

        # --check resolves the answer in that same shell
        r = w.afk("worker-command", "--check", "ckimi", env=kimi)
        assert (r["status"], r["command"], r["yolo"]) == ("confirmed", "ckimi", True)
        r = w.afk("worker-command", "--check", "cplain", env=kimi)
        assert (r["status"], r["yolo"]) == ("confirmed", False)   # no unattended flag: warn
        # the typo: the shell prints "ckim not found" on STDOUT and exits 1 — it must
        # read as unresolved, not as a resolution whose text happens to say "not found"
        r = w.afk("worker-command", "--check", "ckim --dangerously-skip-permissions", env=kimi)
        assert (r["status"], r["command"], r["first_word"]) == ("unresolved", None, "ckim")
        # a first word with shell metacharacters is passed as ONE word, never run —
        # checked against a REAL shell, since only a real one would run it
        r = w.afk("worker-command", "--check", "x;touch${IFS}pwned", env={**kimi, "SHELL": "/bin/sh"})
        assert r["status"] == "unresolved" and not os.path.exists(os.path.join(w.cwd, "pwned"))
        assert w.afk("worker-command", "--check", "ls -la", env={**kimi, "SHELL": "/bin/sh"})["status"] == "confirmed"
        # a shell that cannot be started degrades to "unresolved"/no candidates, never a crash
        r = w.afk("worker-command", env={**kimi, "SHELL": "/no/such/shell"})
        assert r["status"] == "ask" and r["candidates"] == []

        # qoderclicn: always stock, whatever the provider env or the answer says (ADR-0014)
        qoder = {**kimi, "QODERCN_CLI": "1"}
        for args in ((), ("--check", "ckimi")):
            r = w.afk("worker-command", *args, env=qoder)
            assert (r["status"], r["command"], r["runtime"]) == \
                ("stock", "qoderclicn --dangerously-skip-permissions", "qoderclicn"), r


def test_config_file_loads_validates_and_round_trips():
    with world() as w:
        defaults = w.afk("config", "--defaults")
        assert defaults == afk_decide.resolve_config({})
        # the shipped template IS the defaults table
        assert w.afk("config", "--file", os.path.join(SKILL, "references", "config-template.md")) == defaults

        path = os.path.join(w.sb.root, "afk-fleet.md")

        def load(yaml):
            with open(path, "w") as f:
                f.write("# config\n\n```yaml\n" + yaml + "\n```\n")
            return path

        cfg = w.afk("config", "--file", load("retry: 4\nclaim_namespace: refs/heads\ngate:\n  ci: local\n"
                                             "  local_command: make test"))
        assert cfg["retry"] == 4 and cfg["claim_namespace"] == "refs/heads"
        assert cfg["gate"] == {**defaults["gate"], "ci": "local", "local_command": "make test"}
        # canonical JSON fed back as --config is a fixed point for every consumer
        assert w.afk("next-attempt", "--labels", "afk-attempt/3", "--config", json.dumps(cfg))["action"] == "retry"

        for bad, why in (("retyr: 4", "unknown key"),
                         ("gate:\n  ci: local", "local_command"),
                         ("claim_namespace: afk", "claim_namespace"),
                         ("authorize: true", "per-run")):
            assert why in w.error("config", "--file", load(bad)), bad
        assert "--file" in w.error("config")
        assert "No such file" in w.error("config", "--file", "/no/such/file.md")


# --------------------------------------------------------------------------- #
# recovery via orca / gate-run                                                 #
# --------------------------------------------------------------------------- #

def _orca_row(issue_no, path, **extra):
    return {"linkedIssue": issue_no, "path": path, "branch": "refs/heads/sunfmin/issue-31-x",
            "projectId": f"github:{REPO}", "isMainWorktree": False, "isArchived": False,
            "lastActivityAt": 100, **extra}


def test_recovery_finds_this_machines_worktree_through_orca():
    with world() as w:
        cfg = ("--config", json.dumps({"base_branch": w.sb.base}))
        w.commit("step1.txt", branch="sunfmin/issue-31-x")
        git(w.cwd, "push", "-q", "origin", "HEAD")
        with open(os.path.join(w.cwd, "wip.txt"), "w") as f:
            f.write("uncommitted\n")

        # orca knows a worktree linked to this issue in THIS repo → tier 1, with the
        # branch read back from orca rather than guessed from the pattern
        w.orca([_orca_row(None, "/main", isMainWorktree=True),
                _orca_row(31, "/elsewhere", projectId="github:acme/other", lastActivityAt=999),
                _orca_row(31, w.cwd)])
        r = w.afk("recovery", "--issue", "31", *R, *cfg)
        assert (r["tier"], r["action"], r["prompt"]) == (1, "reuse_worktree", "continue"), r
        assert r["worktree"]["path"] == w.cwd and r["worktree"]["dirty"] is True
        assert r["worktree"]["commits_ahead"] == 1
        assert r["branch"] == {"name": "sunfmin/issue-31-x", "commits_ahead": 1,
                               "candidates": [], "detail": ""}

        # --no-worktree overrides orca: only what was PUSHED counts → tier 2
        r = w.afk("recovery", "--issue", "31", "--no-worktree", *R, *cfg)
        assert (r["tier"], r["worktree"]["present"]) == (2, False)

        # orca is a SOFT dependency: absent rows, a failing orca, or garbage output all
        # degrade to "no local worktree" (tier 2 here) — never an aborted recovery
        w.orca([_orca_row(99, w.cwd)])
        assert w.afk("recovery", "--issue", "31", *R, *cfg)["tier"] == 2
        w.orca([_orca_row(31, w.cwd)])
        assert w.afk("recovery", "--issue", "31", *R, *cfg, env={"AFK_FAKE_ORCA_EXIT": "1"})["tier"] == 2
        for garbage in ("not json", "[]", '{"result": null}', '{"ok": true}'):
            with open(w.orca_file, "w") as f:
                f.write(garbage)
            assert w.afk("recovery", "--issue", "31", *R, *cfg)["tier"] == 2, garbage
        # a worktree orca remembers but the disk no longer has is not "present"
        w.orca([_orca_row(31, os.path.join(w.sb.root, "gone"))])
        r = w.afk("recovery", "--issue", "31", *R, *cfg)
        assert (r["tier"], r["worktree"]["present"]) == (2, False)


def test_gate_run_is_green_only_on_exit_zero():
    with world() as w:
        cfg = json.dumps({"gate": {"ci": "local", "local_command": "echo built && echo tested"}})
        r = w.afk("gate-run", "--worktree", w.cwd, "--config", cfg)
        assert (r["status"], r["exit_code"], r["timed_out"]) == ("green", 0, False)
        assert r["excerpt"] == "built\ntested" and r["command"] == "echo built && echo tested"

        # it runs IN the worktree, and stderr is part of the log
        r = w.afk("gate-run", "--worktree", w.cwd, "--command", "ls README.md && echo oops >&2 && exit 7")
        assert (r["status"], r["exit_code"]) == ("red", 7) and r["excerpt"] == "README.md\noops"
        # the excerpt is a bounded tail
        r = w.afk("gate-run", "--worktree", w.cwd, "--command", "seq 1 200; exit 1", "--excerpt-lines", "3")
        assert r["excerpt"] == "198\n199\n200" and r["omitted_lines"] == 197
        # a hung gate is RED, never green-by-default
        r = w.afk("gate-run", "--worktree", w.cwd, "--command", "sleep 30", "--timeout", "1")
        assert (r["status"], r["timed_out"]) == ("red", True) and "timed out after 1s" in r["excerpt"]

        assert "no gate command" in w.error("gate-run", "--worktree", w.cwd)
        assert "worktree not found" in w.error("gate-run", "--worktree", "/no/such", "--config", cfg)


# --------------------------------------------------------------------------- #
# the CLI's own contracts                                                      #
# --------------------------------------------------------------------------- #

def test_pure_subcommands_and_the_json_error_contract():
    with world() as w:
        assert w.afk("next-attempt", "--labels", "ready-for-agent,afk-attempt/1") == \
            {"action": "retry", "from_label": "afk-attempt/1", "to_label": "afk-attempt/2"}
        assert w.afk("next-attempt", "--labels", "afk-attempt/2")["action"] == "escalate"
        assert w.afk("next-attempt", "--labels", "")["to_label"] == "afk-attempt/1"

        busy = json.dumps({"merged": [3], "in_flight": 1, "empty_streak": 0})
        idle = json.dumps({"in_flight": 0, "empty_streak": 9})
        assert w.afk("pace", "--summary", busy) == {"seconds": 90}
        assert w.afk("pace", "--summary", idle) == {"seconds": 1500}
        cfg = json.dumps({"idle_interval_seconds": 60})
        assert w.afk("pace", "--summary", idle, "--config", cfg) == {"seconds": 60}

        # every operational failure is exit 3 with one {"error": …} object — bad JSON
        # in, a missing file, a failing gh — so a tick never has to parse a traceback
        w.error("pace", "--summary", "{not json")
        w.error("pace", "--summary", busy, "--config", "{not json")
        assert "gh issue list failed" in w.error("fingerprint", "--repo", "acme/other")


def test_flag_beats_config_beats_defaults_on_every_override():
    """ADR-0009's one resolution order, checked for EVERY override flag the CLI has —
    so a new flag cannot quietly resolve its own way."""
    parser = afk.build_parser()
    sample = {"ns": "refs/x", "ttl": "7", "ready_label": "go", "epic_labels": "a,b", "ci": "local",
              "command": "make", "target": "rel", "base": "dev", "retry": "9", "grace": "8",
              "force_after": "4"}
    positional = {"claim": ["1"], "reclaim": ["1", "--expect-sha", "s"], "release": ["1"],
                  "status": ["1", "--phase", "claimed"]}
    required = {"--instance": "me", "--repo": REPO, "--issue": "1", "--terminal": "idle",
                "--worktree": ".", "--labels": "x", "--summary": "{}", "--expect-sha": "s",
                "--phase": "claimed"}
    seen = set()
    for name, sub in parser.subcommands.items():
        need = [f for act in sub._actions if act.required for f in act.option_strings[:1]]
        argv = [name, *positional.get(name, [])]
        for f in need:
            if f not in argv:
                argv += [f, required[f]]
        for act in sub._actions:
            if act.dest not in afk._FLAG_OVERRIDES:
                continue
            seen.add(act.dest)
            path = afk._FLAG_OVERRIDES[act.dest]

            def read(cfg):
                for key in path:
                    cfg = cfg[key]
                return cfg

            default = read(afk_decide.CONFIG_DEFAULTS)             # the path must exist
            from_config = "cfg-value" if isinstance(default, str) else 123
            partial = from_config
            for key in reversed(path):
                partial = {key: partial}
            with_cfg = ["--config", json.dumps(partial)]

            assert read(afk._cfg(parser.parse_args(argv))) == default, (name, act.dest)
            assert read(afk._cfg(parser.parse_args(argv + with_cfg))) == from_config, (name, act.dest)
            flagged = read(afk._cfg(parser.parse_args(
                argv + with_cfg + [act.option_strings[0], sample[act.dest]])))
            assert flagged not in (default, from_config), (name, act.dest, flagged)
            assert type(flagged) is type(default), (name, act.dest, flagged)
    # every declared override is reachable from some subcommand
    assert seen == set(afk._FLAG_OVERRIDES), set(afk._FLAG_OVERRIDES) - seen

    # and every subcommand takes --config and --now, so "pass the config" has no exceptions
    for name, sub in parser.subcommands.items():
        assert {"--config", "--now"} <= set(sub._option_string_actions), name


def _documented_invocations(text):
    """(subcommand, [--flags]) for every `afk <sub> …` written as code in a markdown
    document — fenced blocks line by line, inline spans whole."""
    fenced = re.findall(r"```.*?```", text, re.S)
    chunks = [ln for block in fenced for ln in block.replace("\\\n", " ").splitlines()]
    chunks += [span.replace("\n", " ")
               for span in re.findall(r"`([^`]+)`", re.sub(r"```.*?```", "", text, flags=re.S))]
    for chunk in chunks:
        m = re.search(r"(?<![\w-])afk(?:\.py)? ([a-z][a-z-]*)(.*)", chunk)
        if m:
            yield m.group(1), re.findall(r"(?<![\w-])(--[a-z][a-z-]*)", m.group(2))


def test_the_docs_name_only_subcommands_and_flags_that_exist():
    """SKILL.md and its references are the tick's ONLY knowledge of this CLI — a stale
    subcommand or flag in prose is a call that fails at 3am. Every `afk …` written as
    code must parse, and tools.md must list every subcommand."""
    subs = afk.build_parser().subcommands
    docs = [os.path.join(SKILL, "SKILL.md"), os.path.join(SKILL, "..", "..", "CONTEXT.md")]
    refs = os.path.join(SKILL, "references")
    docs += [os.path.join(refs, f) for f in sorted(os.listdir(refs)) if f.endswith(".md")]

    checked = 0
    for path in docs:
        with open(path) as f:
            text = f.read()
        for sub, flags in _documented_invocations(text):
            where = f"{os.path.basename(path)}: `afk {sub}`"
            assert sub in subs, f"{where} is not a subcommand"
            for flag in flags:
                assert flag in subs[sub]._option_string_actions, f"{where} has no {flag}"
            checked += 1
    assert checked > 30, checked          # the scan really found the invocations

    # the scanner itself: both spellings, fenced and inline, and nothing that merely
    # contains "afk" (the skill's name, a ref, a marker, a path)
    sample = ("Run `afk claim <n> --instance <id>`, not `/afk-fleet --tick` or `afk-claim/<n>`.\n"
              "```bash\npython3 <skill>/scripts/afk.py no-pr --issue <n> \\\n     --terminal idle\n```\n"
              "See `docs/agents/afk-fleet.md` and `<!--afk:verdict …-->`; `afk` alone is not a call.")
    assert list(_documented_invocations(sample)) == \
        [("no-pr", ["--issue", "--terminal"]), ("claim", ["--instance"])]

    with open(os.path.join(refs, "tools.md")) as f:
        rows = [ln for ln in f.read().splitlines() if ln.startswith("| `afk ")]
    listed = [re.match(r"\| `afk ([a-z-]+)", ln).group(1) for ln in rows]
    assert sorted(listed) == sorted(subs), set(listed) ^ set(subs)


def run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    run_all()
