#!/usr/bin/env python3
"""
Integration tests for the afk-fleet subcommands that talk to GitHub's API, orca
and the login shell — run through the real CLI, end to end, and still offline.

Run: python3 test_afk_cli.py   (or under pytest, beside the other two suites)

`test_afk_decide.py` pins the pure verdicts and `test_afk_refs.py` the ref races;
what neither reaches is the seam between them: that `afk rebuild` asks gh for the
fields its verdicts read, that `afk dispatch` puts a worker on the commit the
REMOTE has and submits its prompt, that `afk land` gates the tree that lands and
merges it only on a landing turn, that `afk escalate` relabels before it releases,
that `--set` beats `--config` beats the defaults table — and that the docs name
subcommands and flags that exist.

Nothing is injected into afk.py to make that possible. The outside world is faked
where it actually lives — executables on PATH:

  gh    a stand-in backed by one JSON state file (native dependency edges
        included: an issue's open-blocker count is derived from them). It projects exactly the fields
        asked for (asking for one GitHub does not have is a KeyError), applies only
        the `--jq` filters it knows (a changed filter fails loudly rather than
        silently diverging), refuses what GitHub refuses (an unknown label, a merge
        pinned to a head the branch has left), and logs every call so a test can
        assert what was — and was NOT — asked. A merge moves the real target
        branch in the bare repo.
  orca  a stand-in backed by one JSON document, in orca's own response shapes
        (pinned against orca 1.4). `worktree create` makes a REAL git worktree on a
        `tester/<name>` branch; terminals record the command they were started
        with and every prompt sent to them, so a test reads what a worker was
        actually told.
  $SHELL  a stand-in login shell that knows a fixed set of aliases.

git is real, against the same bare-repo sandbox as `test_afk_refs.py`; `--repo
owner/name` reaches it through a `url.<bare>.insteadOf` rewrite in the clone.
"""
import json
import os
import re
import shlex
import subprocess
import sys
import time
from contextlib import contextmanager

import afk
import afk_decide
from test_afk_refs import ENV, NO_CONFIG, T0, TTL, afk as run, afk_error, git, sandbox

REPO = "acme/widgets"
LAUNCHER = "term_launcher"      # the orca terminal the launcher runs in (ADR-0020)
HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.dirname(HERE)
AFK = os.path.join(HERE, "afk.py")

FAKE_GH = r'''#!%(python)s
import json, os, subprocess, sys

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


def opts(name):
    return [argv[i + 1] for i, x in enumerate(argv) if x == name]


def bare(*args):
    """git against the bare repo standing in for GitHub's copy of the code."""
    return subprocess.run(["git", "--git-dir", st["bare"], *args],
                          capture_output=True, text=True).stdout.strip()


def head_of(pr):
    """A PR's head is wherever its branch points NOW — as on GitHub."""
    return bare("rev-parse", "-q", "--verify", "refs/heads/" + pr["headRefName"]) or pr["headRefOid"]


def known_labels():
    return set(st.get("labels", [])) | {lb["name"] for i in st["issues"] for lb in i["labels"]}


if " ".join(argv[:2]) in st.get("fail", []):
    finish(code=1, err="fake gh: injected failure\n")

if argv[0] in ("issue", "pr", "label") and opt("--repo") != st["repo"]:
    finish(code=1, err="fake gh: unknown repo %%s\n" %% opt("--repo"))

if argv[:2] in (["issue", "list"], ["pr", "list"]):
    assert opt("--state") == "open", argv
    rows = [r for r in st["issues" if argv[0] == "issue" else "prs"]
            if r.get("state", "open") == "open"]
    if argv[0] == "pr":
        rows = [{**r, "headRefOid": head_of(r)} for r in rows]
    fields = opt("--json").split(",")
    finish(json.dumps([{k: r[k] for k in fields} for r in rows]))

if argv[:2] == ["label", "create"]:
    if argv[2] in known_labels():
        finish(code=1, err="label with name %%r already exists\n" %% argv[2])
    st.setdefault("labels", []).append(argv[2])
    finish()

if argv[0] == "issue" and argv[1] in ("edit", "close"):
    row = next((r for r in st["issues"] if str(r["number"]) == argv[2]), None)
    if row is None or row.get("state", "open") != "open":
        finish(code=1, err="fake gh: no open issue %%s\n" %% argv[2])
    if argv[1] == "close":
        assert opt("--reason") == "completed", argv
        row["state"] = "closed"
        finish()
    have = [lb["name"] for lb in row["labels"]]
    for name in opts("--add-label"):
        if name not in known_labels():
            finish(code=1, err="'%%s' not found\n" %% name)
    for name in opts("--remove-label"):                  # stricter than GitHub, on purpose
        if name not in have:
            finish(code=1, err="fake gh: issue does not carry %%r\n" %% name)
    have = [n for n in have if n not in opts("--remove-label")]
    have += [n for n in opts("--add-label") if n not in have]
    row["labels"] = [{"name": n, "color": "ededed"} for n in have]
    finish()

if argv[0] == "pr" and argv[1] in ("merge", "close", "comment"):
    row = next((r for r in st["prs"] if str(r["number"]) == argv[2]), None)
    if row is None or row.get("state", "open") != "open":
        finish(code=1, err="fake gh: no open pull request %%s\n" %% argv[2])
    notes = st.setdefault("pr_comments", {}).setdefault(argv[2], [])
    ref = "refs/heads/" + row["headRefName"]
    if argv[1] == "comment":
        notes.append(opt("--body"))
        finish()
    if argv[1] == "close":
        notes.append(opt("--comment"))
        row["state"] = "closed"
        if "--delete-branch" in argv:
            bare("update-ref", "-d", ref)
        finish()
    strategy = [x[2:] for x in argv if x in ("--squash", "--merge", "--rebase")]
    assert len(strategy) == 1, argv
    if opt("--match-head-commit") != head_of(row):
        finish(code=1, err="GraphQL: Head branch was modified. Review and try the merge again.\n")
    # the head already contains the target (the sync merged it in), so landing it
    # is a fast-forward of the target — the same tree a real squash would produce
    bare("update-ref", "refs/heads/" + st["base"], head_of(row))
    row.update(state="merged", merged={"strategy": strategy[0], "head": head_of(row),
                                       "delete_branch": "--delete-branch" in argv})
    if "--delete-branch" in argv:
        bare("update-ref", "-d", ref)
    for ref_issue in row["closingIssuesReferences"]:
        for i in st["issues"]:
            if i["number"] == ref_issue["number"]:
                i["state"] = "closed"
    finish()

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

def issue_row(number):
    row = next((r for r in st["issues"] if str(r["number"]) == str(number)), None)
    if row is None:
        finish(code=1, err="gh: Not Found (HTTP 404)\n")
    return row


def issue_id(row):
    return row.get("id", 9000 + row["number"])


if parts[0] == "issues" and parts[2:] == ["dependencies", "blocked_by"]:
    issue_row(parts[1])
    edges = st.setdefault("deps", {}).setdefault(parts[1], [])        # blocker numbers
    if method == "POST":
        new = next((x[len("issue_id="):] for x in argv if x.startswith("issue_id=")), None)
        assert new is not None and argv[argv.index("issue_id=" + new) - 1] == "-F", argv
        blocker = next((r for r in st["issues"] if str(issue_id(r)) == new), None)
        if blocker is None or blocker["number"] in edges:               # as GitHub: 422
            finish(code=1, err="gh: Validation Failed (HTTP 422)\n")
        edges.append(blocker["number"])
        finish(json.dumps({"id": issue_id(blocker)}))
    assert jq == ".[] | {number, state}", "fake gh: unsupported jq %%r" %% jq
    finish("\n".join(json.dumps({"number": n, "state": issue_row(n).get("state", "open")})
                     for n in edges))

if parts[0] == "issues" and len(parts) == 2:
    row = issue_row(parts[1])
    if jq == ".state":
        finish(row.get("state", "open"))
    if jq == ".id":
        finish(str(issue_id(row)))
    if jq == ("{state, state_reason, labels: [.labels[].name], "
              "pull_request: (.pull_request != null)}"):
        finish(json.dumps({"state": row.get("state", "open"),
                           "state_reason": row.get("state_reason"),
                           "labels": [lb["name"] for lb in row["labels"]],
                           "pull_request": "pull_request" in row}))
    if jq == "{title, state, labels: [.labels[].name]}":
        finish(json.dumps({"title": row["title"], "state": row.get("state", "open"),
                           "labels": [lb["name"] for lb in row["labels"]]}))
    assert jq == ".issue_dependencies_summary.blocked_by", "fake gh: unsupported jq %%r" %% jq
    edges = st.get("deps", {}).get(parts[1])
    if edges is None:                     # a count the test set by hand, or none at all
        finish(json.dumps(row.get("blocked_by")))
    finish(json.dumps(len([n for n in edges if issue_row(n).get("state", "open") == "open"])))

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
import json, os, subprocess, sys

path = os.environ["AFK_FAKE_ORCA"]
argv = sys.argv[1:]
with open(path) as f:
    raw = f.read()
if argv == ["worktree", "list", "--json"]:       # verbatim, so a test can make it garbage
    sys.stdout.write(raw)
    sys.exit(int(os.environ.get("AFK_FAKE_ORCA_EXIT", "0")))

assert argv[-1] == "--json", argv
doc = json.loads(raw)
fake, rows = doc["fake"], doc["result"]["worktrees"]
fake["calls"].append(argv[:-1])


def finish(result=None, error=None):
    with open(path, "w") as f:
        json.dump(doc, f)
    print(json.dumps({"id": "x", "ok": error is None, "result": result,
                      **({"error": {"code": error, "message": error}} if error else {})}))
    sys.exit(1 if error else 0)


def opt(name):
    return argv[argv.index(name) + 1]


def git(cwd, *args):
    p = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True)
    assert p.returncode == 0, (args, p.stderr)
    return p.stdout.strip()


def worktree():
    sel = opt("--worktree")
    assert sel.startswith("path:"), argv
    return next((r for r in rows if r["path"] == sel[len("path:"):]), None)


def terminal():
    return next((t for t in fake["terminals"] if t["handle"] == opt("--terminal") and t["open"]), None)


cmd = argv[:2]
if cmd == ["repo", "list"]:
    finish({"repos": fake["repos"]})

if cmd == ["worktree", "create"]:
    repo = next((r for r in fake["repos"] if "id:" + r["id"] == opt("--repo")), None)
    if repo is None:
        finish(error="selector_not_found")
    assert "--no-parent" in argv, argv
    taken = git(repo["path"], "for-each-ref", "--format=%%(refname:short)", "refs/heads").splitlines()
    name, n = opt("--name"), 1
    while "tester/" + name + ("" if n == 1 else "-%%d" %% n) in taken:     # orca never reuses a branch
        n += 1
    name += "" if n == 1 else "-%%d" %% n
    wt = os.path.join(fake["root"], name.replace("/", "-"))
    git(repo["path"], "worktree", "add", "-q", "-b", "tester/" + name, wt,
        fake.get("stale_base") or opt("--base-branch"))
    rows.append({"linkedIssue": int(opt("--issue")), "path": wt, "branch": "refs/heads/tester/" + name,
                 "projectId": fake["project"], "isMainWorktree": False, "isArchived": False,
                 "lastActivityAt": len(fake["calls"])})
    finish({"worktree": {"path": wt, "branch": "refs/heads/tester/" + name,
                         "head": git(wt, "rev-parse", "HEAD"), "baseRef": opt("--base-branch")}})

if cmd == ["worktree", "rm"]:
    row = worktree()
    if row is None:
        finish(error="selector_not_found")
    if fake.get("rm_fails"):
        finish(error="worktree_busy")
    assert "--force" in argv, argv
    if os.path.isdir(row["path"]):
        git(fake["repos"][0]["path"], "worktree", "remove", "--force", row["path"])
    rows.remove(row)
    for t in fake["terminals"]:
        t["open"] = t["open"] and t["worktreePath"] != row["path"]
    finish({"removed": True})

if cmd == ["terminal", "create"]:
    row = worktree()
    if row is None:
        finish(error="selector_not_found")
    handle = "term-%%d" %% (len(fake["terminals"]) + 1)
    fake["terminals"].append({"handle": handle, "worktreePath": row["path"],
                              "command": opt("--command"), "sent": [], "open": True})
    finish({"terminal": {"handle": handle, "worktreePath": row["path"]}})

if cmd == ["terminal", "wait"]:
    assert opt("--for") == "tui-idle" and int(opt("--timeout-ms")) > 0, argv
    if terminal() is None:
        finish(error="terminal_handle_stale")
    if terminal().get("tui_busy"):                   # as real orca: a timeout is an error
        finish(error="timeout")
    ready = not fake.get("never_ready")
    finish({"wait": {"satisfied": ready, "status": "idle" if ready else "timeout"}})

if cmd == ["terminal", "send"]:
    term = terminal()
    if term is None:
        finish(error="terminal_handle_stale")
    term["sent"].append({"text": opt("--text"), "enter": "--enter" in argv})
    finish({"send": {"accepted": True}})

if cmd == ["worktree", "ps"]:
    assert int(opt("--limit")) > 0, argv
    out = []
    for r in rows:
        terms = [t for t in fake["terminals"] if t["open"] and t["worktreePath"] == r["path"]]
        out.append({"path": r["path"], "linkedIssue": r.get("linkedIssue"),
                    "liveTerminalCount": len(terms),
                    "lastOutputAt": max((t.get("lastOutputAt") or 0 for t in terms), default=0) or None,
                    "agents": [t["agent"] for t in terms if t.get("agent")]})
    finish({"worktrees": out, "totalCount": len(out), "truncated": fake.get("ps_truncated", False)})

if cmd == ["terminal", "list"]:
    row = worktree()
    finish({"terminals": [{"handle": t["handle"], "worktreePath": t["worktreePath"],
                           "connected": True, "writable": True, "lastOutputAt": i}
                          for i, t in enumerate(fake["terminals"])
                          if t["open"] and row and t["worktreePath"] == row["path"]]})

if cmd == ["terminal", "read"]:
    term = terminal()
    if term is None:
        finish(error="terminal_handle_stale")
    assert "--screen" in argv and int(opt("--limit")) > 0, argv
    finish({"terminal": {"handle": term["handle"], "tail": term.get("screen", []),
                         "source": "screen"}})

if cmd == ["terminal", "close"]:
    row = worktree()
    assert "--all" in argv, argv
    for t in fake["terminals"]:
        t["open"] = t["open"] and not (row and t["worktreePath"] == row["path"])
    finish({"closed": True})

finish(error="fake orca: unsupported call %%r" %% (argv,))
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
    """One open PR. `conclusion=None` is a PR with no checks at all. Its head is
    wherever `headRefName` points in the bare repo, `headRefOid` when it is not there."""
    checks = [{"name": "ci", "status": "COMPLETED", "conclusion": conclusion}] if conclusion else []
    if conclusion == "PENDING":
        checks = [{"name": "ci", "status": "IN_PROGRESS", "conclusion": None}]
    return {"number": n, "headRefName": f"tester/issue-{closes}-x", "headRefOid": f"sha{n}",
            "updatedAt": f"P{n}", "statusCheckRollup": checks,
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
                    "SHELL": os.path.join(bindir, "fakeshell"),
                    # the terminal the launcher (and so every tick) runs in
                    "ORCA_TERMINAL_HANDLE": LAUNCHER}
        for leak in ("QODERCN_CLI", "ANTHROPIC_BASE_URL"):
            self.env.pop(leak, None)
        self.set(**{"repo": REPO, "bare": sb.bare, "base": sb.base, "issues": [], "prs": [],
                    "comments": {}, "labels": ["ready-for-agent"], **state})
        self._fake = {"repos": [{"id": "repo-1", "path": self.cwd, "displayName": "widgets",
                                 "gitRemoteIdentity": {"canonicalKey": f"github.com/{REPO}"}}],
                      "project": f"github:{REPO}", "root": os.path.join(sb.root, "wt"),
                      "terminals": [], "calls": []}
        self.orca([])

    # --- GitHub -----------------------------------------------------------
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

    def issue(self, n):
        row = next(i for i in self.state()["issues"] if i["number"] == n)
        return {"state": row.get("state", "open"), "labels": [lb["name"] for lb in row["labels"]]}

    def pr(self, n):
        return next(p for p in self.state()["prs"] if p["number"] == n)

    def comments(self, n):
        return [c["body"] for c in self.state()["comments"].get(str(n), [])]

    def board(self, n):
        """The issue's ONE status board comment body ("" when it has none)."""
        rows = [b for b in self.comments(n) if afk_decide.STATUS_MARKER in b]
        assert len(rows) <= 1, rows
        return rows[0] if rows else ""

    def open_pr(self, number, closes, branch, conclusion="SUCCESS"):
        self.set(prs=self.state()["prs"] + [pr(number, closes, conclusion, headRefName=branch)])

    def claimed_by(self, n):
        """The instance holding issue n's claim on the remote, None when unclaimed."""
        if not self.sb.remote_ref(f"refs/afk/claim/{n}"):
            return None
        return next(c["instance"] for c in self.afk("scan", *R)["claims"] if c["number"] == n)

    # --- orca -------------------------------------------------------------
    def orca(self, worktrees=None, **knobs):
        """Set orca's worktree rows and/or the fake's knobs (`never_ready`,
        `stale_base`, `rm_fails`, `repos`), keeping everything else it remembers."""
        if os.path.exists(self.orca_file):
            try:
                with open(self.orca_file) as f:
                    doc = json.load(f)
                self._fake = doc["fake"]
                worktrees = doc["result"]["worktrees"] if worktrees is None else worktrees
            except (ValueError, KeyError, TypeError):
                pass                                  # a test wrote garbage there: start over
        self._fake.update(knobs)
        rows = worktrees or []
        with open(self.orca_file, "w") as f:
            json.dump({"id": "x", "ok": True, "fake": self._fake,
                       "result": {"worktrees": rows, "totalCount": len(rows)}}, f)

    def orca_doc(self):
        with open(self.orca_file) as f:
            return json.load(f)

    def worktrees(self):
        return self.orca_doc()["result"]["worktrees"]

    def terminals(self):
        return self.orca_doc()["fake"]["terminals"]

    def orca_calls(self, reset=True):
        """Every stateful orca invocation since the last look, as `"<noun> <verb>"`."""
        calls = [" ".join(c[:2]) for c in self.orca_doc()["fake"]["calls"]]
        if reset:
            self.orca(calls=[])
        return calls

    def worker(self, output=None, state=None, since=None, n=-1, tui_busy=False):
        """What the worker in terminal `n` reported to orca: its terminal's last
        output at `output`, its agent `state` since `since` (epoch seconds). For
        one that reports no state, `tui_busy` is orca not seeing its terminal idle."""
        terms = self.terminals()
        terms[n]["tui_busy"] = tui_busy
        terms[n]["lastOutputAt"] = output * 1000 if output is not None else None
        terms[n]["agent"] = {"state": state, "stateStartedAt": (since or 0) * 1000,
                             "parentPaneKey": None} if state else None
        self.orca(terminals=terms)

    def no_pr(self, *args):
        """`afk no-pr` for ONE issue → that worker's row."""
        (row,) = self.afk("no-pr", *args)["workers"]
        return row

    # --- the CLI ----------------------------------------------------------
    def afk(self, *args, env=None, cwd=None):
        return run(cwd or self.cwd, *args, env={**self.env, **(env or {})})

    def error(self, *args, env=None, bare=False, cwd=None):
        return afk_error(cwd or self.cwd, *args, env={**self.env, **(env or {})}, bare=bare)

    # --- the code ---------------------------------------------------------
    def commit(self, name, branch=None):
        if branch:
            git(self.cwd, "checkout", "-q", "-b", branch)
        with open(os.path.join(self.cwd, name), "w") as f:
            f.write(name + "\n")
        git(self.cwd, "add", "-A")
        git(self.cwd, "commit", "-qm", name)

    def work(self, path, name, text=None, push=True):
        """What a worker does in its worktree: commit a file, push its branch → sha."""
        with open(os.path.join(path, name), "w") as f:
            f.write((text or name) + "\n")
        git(path, "add", "-A")
        git(path, "commit", "-qm", f"work: {name}")
        if push:
            git(path, "push", "-q", "origin", "HEAD")
        return git(path, "rev-parse", "HEAD")

    def advance_base(self, name, text=None):
        """Someone else lands a commit on the base branch — on the REMOTE only: no
        clone here has fetched it → its sha."""
        seed = os.path.join(self.sb.root, "seed")
        git(seed, "pull", "-q", "origin", self.sb.base)
        with open(os.path.join(seed, name), "w") as f:
            f.write((text or name) + "\n")
        git(seed, "add", "-A")
        git(seed, "commit", "-qm", f"base: {name}")
        git(seed, "push", "-q", "origin", f"HEAD:refs/heads/{self.sb.base}")
        return git(seed, "rev-parse", "HEAD")

    def remote_files(self, ref):
        """The file names in the tree the remote has at `ref`."""
        p = subprocess.run(["git", "--git-dir", self.sb.bare, "ls-tree", "--name-only", ref],
                           capture_output=True, text=True, env=ENV)
        return set(p.stdout.split())


@contextmanager
def world(**state):
    """A fresh sandbox wired to the fakes, with `state` as GitHub's initial state."""
    with sandbox() as sb:
        yield World(sb, **state)


R = ("--repo", REPO)
ME = ("--instance", "me")
NOW = ("--now", str(T0))
WORKER = "ckimi --dangerously-skip-permissions"     # the run's worker launch command: opaque


def dispatch(n, *extra, instance="me"):
    """The argv of one `afk dispatch`. A claim marker is a commit of (instance, host,
    ts), so a re-dispatch at the very same `--now` pushes the identical sha and reads
    as `won` again; a test asserting `held` passes a later one, as a later tick would."""
    return ("dispatch", "--issue", str(n), "--instance", instance, "--worker-command", WORKER,
            *R, *NOW, *extra)


def local_gate(command):
    return ("--set", "gate.ci=local", "--set", f"gate.local_command={command}")


def with_pr(w, n, pr_number, conclusion="SUCCESS", **work):
    """Issue n as a tick finds it when its PR is ready: dispatched, its worker committed and
    pushed, and a PR closing it is open → (the dispatch result, the PR head sha)."""
    d = w.afk(*dispatch(n, *work.pop("gate", ()), instance=work.pop("instance", "me")))
    head = w.work(d["worktree"], work.pop("name", f"feature{n}.txt"), **work)
    w.open_pr(pr_number, closes=n, branch=d["branch"], conclusion=conclusion)
    return d, head


# --------------------------------------------------------------------------- #
# rebuild / cycle                                                              #
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
            ("awaiting_turn", "awaiting_turn", 30, "green")
        assert mine[3]["attempt"] == 1 and mine[4]["attempt"] == 0  # read off gh's label objects
        assert (mine[4]["status"], mine[4]["board_phase"], mine[4]["pr"]) == ("no_pr", "claimed", None)
        assert ws["peer_live"] == [{"number": 5, "instance": "peer-live"}]
        assert [(s["number"], s["instance"]) for s in ws["stale"]] == [(6, "peer-dead")]
        assert ws["stale"][0]["sha"] == w.sb.remote_ref("refs/afk/claim/6")
        assert ws["now"] == T0

        # the per-issue blocked_by read is paid ONLY by issues that passed every
        # cheaper check — two of nine here, not one per open issue — and the
        # landing-turn read only by a claim of mine that has a PR
        api = [c for c in w.calls() if c[0] == "api"]
        blocker_reads = sorted(c[1] for c in api if ".issue_dependencies_summary.blocked_by" in c)
        assert blocker_reads == [f"repos/{REPO}/issues/1", f"repos/{REPO}/issues/2"], blocker_reads
        assert [c[2] for c in api if c[1] == "--paginate"] == [f"repos/{REPO}/issues/30/comments"]
        assert len(api) == 3

        # the stale sha it reported is exactly what reclaim's compare-and-swap needs
        took = w.afk("reclaim", "6", "--instance", "me", "--expect-sha", ws["stale"][0]["sha"],
                     "--now", str(T0), *R)
        assert took["won"] is True
        ws = w.afk("rebuild", "--instance", "me", "--now", str(T0), *R)
        assert [m["number"] for m in ws["mine"]] == [3, 4, 6]
        assert ws["stale"] == [] and ws["stale_closed"] == []


def test_rebuild_reads_the_dispatch_contract_from_config_and_set():
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
        # claim a green gate the landing has not run yet
        assert (ws["mine"][0]["status"], ws["mine"][0]["board_phase"]) == ("awaiting_turn", "awaiting_turn")

        # --set beats the config it was given alongside
        ws = w.afk("rebuild", "--instance", "me", "--now", str(T0), *R, "--config", cfg,
                   "--set", "ready_label=ready-for-agent", "--set", "epic_labels=[x, y]",
                   "--set", "gate.ci=required")
        assert ws["frontier"]["dispatch"] == [{"number": 3, "title": "issue 3"}]
        assert (ws["mine"][0]["status"], ws["mine"][0]["board_phase"]) == ("failure", "ci_failed")

        # --repo is the one repo handle, and a wrong one is an error, not an empty fleet
        assert "unknown repo" in w.error("rebuild", "--instance", "me", "--repo", "acme/other")


def test_rebuild_reports_free_slots_and_a_claim_whose_issue_is_closed():
    """A landed PR (or an `afk close` that died before releasing) leaves a
    claim on a CLOSED issue. gh's open list no longer has the issue, so the row used
    to read as a title-less `no_pr` — a worker to wait on, or re-dispatch, forever."""
    issues = [issue(1, "ready-for-agent"), issue(2, "ready-for-agent", state="closed"),
              issue(3, "ready-for-agent")]
    with world(issues=issues) as w:
        assert w.afk("rebuild", *ME, *R, *NOW)["free_slots"] == 3
        for n in (2, 3):
            w.afk("claim", str(n), *ME, *NOW, *R)
        w.calls()
        ws = w.afk("rebuild", *ME, *R, *NOW)
        rows = {m["number"]: (m["status"], m["board_phase"], m["title"]) for m in ws["mine"]}
        assert rows == {2: ("closed", None, None), 3: ("no_pr", "claimed", "issue 3")}
        assert ws["free_slots"] == 1 and ws["frontier"]["dispatch"] == [{"number": 1, "title": "issue 1"}]
        # the state read is paid only by a claim missing from the open list
        assert [c[1] for c in w.calls() if ".state" in c] == [f"repos/{REPO}/issues/2"]
        assert w.afk("rebuild", *ME, *R, *NOW, "--set", "concurrency=1")["free_slots"] == 0

        # the one thing left to do for it
        w.afk("release", "2", *ME, *R)
        ws = w.afk("rebuild", *ME, *R, *NOW)
        assert [m["number"] for m in ws["mine"]] == [3] and ws["free_slots"] == 2


def test_rebuild_sets_a_dead_peers_claim_on_a_closed_issue_apart_from_work_to_reclaim():
    """A fleet that merged or closed an issue and died before releasing leaves a
    phantom lock. Listed under `stale` it reads as work to take over and dispatch;
    it is `stale_closed` instead — nothing to continue, one release to clear."""
    issues = [issue(1, "ready-for-agent"),
              issue(2, "ready-for-agent", state="closed"),         # the dead peer finished it
              issue(3, "ready-for-agent"),                         # the dead peer was mid-flight
              issue(4, "ready-for-agent", state="closed")]         # a live peer is mid-merge
    with world(issues=issues) as w:
        for n, inst in ((2, "peer-dead"), (3, "peer-dead"), (4, "peer-live")):
            w.afk("claim", str(n), "--instance", inst, *NOW, *R)
        w.afk("heartbeat", "--instance", "peer-dead", "--now", str(T0 - TTL - 60), *R)
        w.afk("heartbeat", "--instance", "peer-live", "--now", str(T0 - 60), *R)

        ws = w.afk("rebuild", *ME, *R, *NOW)
        sha = {n: w.sb.remote_ref(f"refs/afk/claim/{n}") for n in (2, 3)}
        assert ws["stale"] == [{"number": 3, "instance": "peer-dead", "sha": sha[3]}]
        assert ws["stale_closed"] == [{"number": 2, "instance": "peer-dead", "sha": sha[2]}]
        # a live peer's claim is its own to release, closed issue or not
        assert ws["peer_live"] == [{"number": 4, "instance": "peer-live"}]
        assert ws["mine"] == [] and ws["free_slots"] == 3

        # one call clears it — no reclaim, no second rebuild, no worker
        w.calls(), w.orca_calls()
        assert w.afk("release", "2", *ME, "--expect-sha", sha[2], *R)["released"] is True
        assert w.claimed_by(2) is None and w.orca_calls() == []
        ws = w.afk("rebuild", *ME, *R, *NOW)
        assert ws["stale_closed"] == [] and [s["number"] for s in ws["stale"]] == [3]


def test_rebuild_and_cycle_fail_when_the_claim_refs_cannot_be_read():
    """gh answering while git cannot fetch (an expired git credential beside a live
    gh token) used to assemble a working set with NO claims in it: nothing of mine
    in flight, every claimed issue back on the frontier, and a fingerprint that
    never moves. It is an error, on both commands that share the gatherer."""
    with world(issues=[issue(1, "ready-for-agent")]) as w:
        w.afk("claim", "1", *ME, *NOW, *R)
        assert [m["number"] for m in w.afk("rebuild", *ME, *R)["mine"]] == [1]

        git(w.cwd, "config", "--unset", f"url.{w.sb.bare}.insteadOf")   # git loses the remote
        assert "fetch" in w.error("rebuild", *ME, *R)
        assert "fetch" in w.error("cycle", *ME, "--worker-command", WORKER, *R)


def cycle(w, state=None, *extra, now=T0):
    """One `afk cycle` → its result. The first is handed the run's two facts; every
    later one only the `state` the last returned — as a caller holds nothing else."""
    carried = ("--state", json.dumps(state)) if state else (*ME, "--worker-command", WORKER)
    return w.afk("cycle", *R, "--now", str(now), *carried, *extra)


def answer(w, command):
    """Run the command a judgment handed back, exactly as written → its JSON."""
    p = subprocess.run(command, shell=True, cwd=w.cwd, capture_output=True, text=True, env=w.env)
    assert p.returncode == 0, f"{command}\n{p.stdout}\n{p.stderr}"
    return json.loads(p.stdout)


def say(w, n, body):
    """A comment on issue n — what a worker's verdict marker is."""
    comments = w.state()["comments"]
    new = 1 + max([c["id"] for rows in comments.values() for c in rows], default=1000)
    comments.setdefault(str(n), []).append(_comment(new, body))
    w.set(comments=comments)


def verdict(w, n, phase, *blocked_by, reason=None):
    say(w, n, afk_decide.verdict_marker(n, phase, blocked_by, reason) + "\nexplanation")


def test_cycle_gates_ticks_paces_and_beats_through_a_whole_run():
    """The loop as it actually runs: ONE `afk cycle` per cycle, and an opaque `state`
    threaded between them. The caller holds no counter, does no arithmetic and
    carries no summary: a tick runs inside the call, and folds what it did itself."""
    with world(issues=[issue(1, "ready-for-agent")], prs=[pr(30, closes=2)]) as w:
        hb = "refs/afk/heartbeat/me"
        before = w.afk("rebuild", *ME, *R)["fingerprint"]

        # cycle 1: no state at all → the first cycle always ticks, and the tick is
        # this call: #1 is dispatched, the lease published, the sleep returned
        first = cycle(w)
        assert (first["action"], first["reason"]) == ("tick", "first")
        assert set(first) == {"action", "reason", "state", "sleep_seconds", "progress", "judgments"}
        assert first["progress"] == "dispatched #1; 1 in flight, 0 left on the frontier"
        assert (first["judgments"], first["sleep_seconds"]) == ([], 90)
        assert w.claimed_by(1) == "me" and w.sb.remote_ref(hb)
        assert [t["command"] for t in w.terminals()] == [WORKER]
        # the digest is the SAME one rebuild reports: one gatherer, one function
        assert first["state"]["fingerprint"] == before

        # cycle 2: the claim moved the digest → tick; its worker is coding: left alone
        woke = cycle(w, first["state"])
        assert (woke["action"], woke["reason"], woke["sleep_seconds"]) == ("tick", "changed", 90)
        assert woke["progress"] == "1 in flight, 0 left on the frontier"
        held = woke["state"]

        # cycle 3: nothing moved, but the fleet HOLDS a claim → skip, in the same
        # one call, with the lease looked after and the sleep returned
        w.calls()
        skip = cycle(w, held)
        assert (skip["action"], skip["reason"], skip["sleep_seconds"]) == ("skip", "unchanged", 90)
        assert set(skip) == {"action", "reason", "state", "sleep_seconds", "progress", "judgments",
                             "heartbeat"}
        assert skip["progress"] == "nothing moved; 1 in flight, 0 left on the frontier"
        assert skip["heartbeat"]["refreshed"] is False                # the tick beat at T0
        assert skip["state"]["empty_streak"] == 0                     # holding a claim is not empty
        # a skipped cycle is cheap by construction: the two lists, no per-issue read
        assert [c[:2] for c in w.calls()] == [["issue", "list"], ["pr", "list"]]
        # the beat is stateless and self-limiting…
        late = cycle(w, skip["state"], now=T0 + TTL // 2)
        assert (late["action"], late["heartbeat"]["refreshed"]) == ("skip", True)
        # …and does not itself move the digest it is gated on
        assert cycle(w, late["state"])["action"] == "skip"
        # the sleep under a held claim is capped at half the lease, whatever the config
        assert cycle(w, held, "--set", "busy_interval_seconds=99999")["sleep_seconds"] == TTL // 2

        # each kind of movement a tick would act on wakes it
        w.set(prs=[pr(30, closes=2, conclusion="FAILURE")])
        red = cycle(w, late["state"])
        assert (red["action"], red["reason"], red["state"]["skips"]) == ("tick", "changed", 0)
        w.set(issues=[issue(1)])                                      # ready label pulled
        assert cycle(w, red["state"])["reason"] == "changed"

        # the fleet goes quiet: #1's PR landed, so its claim outlived its issue
        w.set(issues=[issue(1, state="closed")])
        idle = cycle(w, red["state"])
        assert idle["progress"] == "cleared #1; 0 in flight, 0 left on the frontier"
        assert (idle["state"]["empty_streak"], idle["sleep_seconds"]) == (0, 90)   # work: not empty
        assert w.claimed_by(1) is None and w.worktrees() == []
        e1 = cycle(w, idle["state"])                                  # the release moved the digest
        assert (e1["action"], e1["state"]["empty_streak"], e1["sleep_seconds"]) == ("tick", 1, 90)
        s2 = cycle(w, e1["state"])
        assert (s2["action"], s2["state"]["empty_streak"], s2["sleep_seconds"]) == ("skip", 2, 90)
        assert "heartbeat" not in s2                                  # holding nothing: no beat
        s3 = cycle(w, s2["state"])
        assert (s3["state"]["empty_streak"], s3["sleep_seconds"]) == (3, 1500)   # idle, at last
        assert cycle(w, s2["state"], "--set", "idle_ticks_before_sleep=9")["sleep_seconds"] == 90

        # the forced full tick: default every 6 skips, from --config, or --set — --set wins
        assert cycle(w, {**s3["state"], "skips": 5})["reason"] == "forced"
        short = ("--config", json.dumps({"force_tick_after_skips": 3}))
        assert cycle(w, s3["state"], *short)["reason"] == "forced"
        assert cycle(w, s3["state"], *short, "--set", "force_tick_after_skips=9")["action"] == "skip"
        # an empty TICK counts exactly like an empty skip
        assert cycle(w, {**s2["state"], "skips": 5})["sleep_seconds"] == 1500
        # the gate switched off: always a tick
        off = cycle(w, s3["state"], "--set", "fingerprint_gate=false")
        assert (off["action"], off["reason"], off["state"]["skips"]) == ("tick", "gate_off", 0)

        # a state the caller mangled is an error, never a fleet paced on zeros
        assert "--state" in w.error("cycle", *R, "--state", json.dumps({"fingerprint": "x"}))
        w.error("cycle", *R, "--state", "{not json")
        assert "gh issue list failed" in w.error("cycle", *ME, "--worker-command", WORKER,
                                                 "--repo", "acme/other")


def test_the_cycle_state_carries_the_instance_and_the_worker_launch_command():
    """The run's two launcher-held facts are passed once. Afterwards a caller that
    kept nothing but `state` — a launcher after a context compaction — still
    dispatches as the same fleet instance, with the same worker launch command."""
    with world(issues=[issue(1, "ready-for-agent"), issue(2, "ready-for-agent")]) as w:
        # a first cycle cannot run without them: nothing would say who claims, or how
        assert "--worker-command" in w.error("cycle", *R, *ME)
        assert "--instance" in w.error("cycle", *R, "--worker-command", WORKER)
        w.calls()
        first = w.afk("cycle", *R, *NOW, "--instance", "fl-9", "--worker-command", WORKER,
                      "--set", "concurrency=1")
        assert first["progress"] == "dispatched #1; 1 in flight, 1 left on the frontier"
        state = first["state"]
        assert (state["instance"], state["worker_command"]) == ("fl-9", WORKER)

        # only the state: #2 is claimed by fl-9 and its worker started with WORKER
        nxt = w.afk("cycle", *R, *NOW, "--state", json.dumps(state))
        assert nxt["progress"] == "dispatched #2; 2 in flight, 0 left on the frontier"
        assert w.claimed_by(2) == "fl-9" and [t["command"] for t in w.terminals()] == [WORKER] * 2
        assert nxt["state"]["instance"] == "fl-9" and nxt["state"]["worker_command"] == WORKER

        # a state without them is an error — exit 3 — never a guess at either
        for gone in ("instance", "worker_command"):
            bare = {k: v for k, v in state.items() if k != gone}
            assert "--state" in w.error("cycle", *R, "--state", json.dumps(bare)), gone
        # …and so is one handed back beside a DIFFERENT fact: two fleets, one state
        assert "fl-9" in w.error("cycle", *R, "--state", json.dumps(state), *ME)
        assert "--worker-command" in w.error("cycle", *R, "--state", json.dumps(state),
                                             "--worker-command", "claude")


def test_a_compacted_launcher_carries_on_from_the_repo_the_config_and_the_last_state():
    """The launcher runs each cycle itself, in a context auto-compaction may cut
    at any point. What it must still hold afterwards is three values — the repo,
    the config and the last `state` — and nothing else: no instance id, no worker
    launch command, no rule. From those alone the next cycle is the same fleet
    instance's: it claims as that instance, starts workers with the same command,
    hands back judgments whose commands carry both, and drains its own claims."""
    with world(issues=[issue(n, "ready-for-agent") for n in (1, 2, 3)]) as w:
        config = json.dumps({"concurrency": 2})
        first = w.afk("cycle", *R, *NOW, "--config", config, "--instance", "fl-7",
                      "--worker-command", WORKER)
        assert first["progress"] == "dispatched #1, #2; 2 in flight, 1 left on the frontier"
        # the compaction: all that survives is this one line of text
        note = json.dumps({"repo": REPO, "config": config, "state": first["state"]})
        del first, config

        def launcher(note, *extra, now=T0):
            """One cycle by a launcher that kept only `note` → (its result, the next note)."""
            kept = json.loads(note)
            assert set(kept) == {"repo", "config", "state"}
            r = w.afk("cycle", "--repo", kept["repo"], "--config", kept["config"],
                      "--state", json.dumps(kept["state"]), "--now", str(now), *extra)
            return r, json.dumps({**kept, "state": r["state"]})

        # #1 landed, and #2's worker says there was nothing to do: the pass settles
        # the first, fills the slot it freed, and asks about the second
        w.set(issues=[issue(1, "ready-for-agent", state="closed"),
                      issue(2, "ready-for-agent"), issue(3, "ready-for-agent")])
        verdict(w, 2, "already-satisfied")
        r, note = launcher(note, now=int(time.time()) + 5000)
        assert r["progress"] == ("dispatched #3; cleared #1; 1 judgment open; "
                                 "2 in flight, 0 left on the frontier")
        assert w.claimed_by(3) == "fl-7" and w.claimed_by(1) is None
        assert [t["command"] for t in w.terminals()] == [WORKER] * 3
        # the judgment is answered with what it carries, not with what was remembered
        (j,) = r["judgments"]
        assert (j["issue"], j["kind"], r["sleep_seconds"]) == (2, "empty_diff", 0)
        assert "--instance fl-7 " in j["if_yes"] and "--instance fl-7 " in j["if_no"]
        assert shlex.quote(WORKER) in j["if_no"]                     # a retry starts a worker
        assert answer(w, j["if_yes"])["action"] == "closed" and w.claimed_by(2) is None

        # answered → the next cycle at once, and it does not ask again
        r, note = launcher(note, now=int(time.time()))
        assert r["judgments"] == [] and r["state"]["instance"] == "fl-7"
        assert r["state"]["worker_command"] == WORKER

        # the stop, from the same three values
        r, note = launcher(note, "--drain")
        assert (r["action"], r["progress"]) == ("drain", "drained; released #3")
        assert w.claimed_by(3) is None


def test_the_drain_releases_claims_with_no_pr_and_keeps_those_with_one():
    """`afk cycle --drain` is the launcher's stop, in code: a claim no open PR
    stands behind is released — its worker still coding, or its issue already
    closed — and one with an open PR is kept, for a peer or a later run to land
    once the lease lapses. Nothing else happens: no turn, no dispatch, no worker
    told anything, no peer's claim touched."""
    issues = [issue(n, "ready-for-agent") for n in (1, 2, 3, 4)]
    issues += [issue(5, "ready-for-agent", state="closed")]
    with world(issues=issues) as w:
        with_pr(w, 1, 10, conclusion="PENDING")                      # finished, checks running
        w.afk(*dispatch(2))                                          # still coding
        w.afk("claim", "4", "--instance", "peer-live", *NOW, *R)
        w.afk("heartbeat", "--instance", "peer-live", "--now", str(T0 - 60), *R)
        r = cycle(w, None, "--set", "concurrency=2")
        assert r["progress"] == "2 in flight, 1 left on the frontier"
        w.afk("claim", "5", *ME, *NOW, *R)                           # mine, outlived its issue
        sent = [len(t["sent"]) for t in w.terminals()]

        done = cycle(w, r["state"], "--drain", "--set", "concurrency=2")
        assert set(done) == {"action", "reason", "state", "sleep_seconds", "progress", "judgments"}
        assert (done["action"], done["reason"]) == ("drain", "stop")
        assert done["progress"] == "drained; released #2, #5; kept #1"
        # nothing follows a drain: no sleep, nothing to answer
        assert done["sleep_seconds"] is None and done["judgments"] == []
        assert done["state"]["in_flight"] == 1 and done["state"]["instance"] == "me"
        assert w.claimed_by(2) is None and w.claimed_by(5) is None
        assert w.claimed_by(1) == "me" and "state" not in w.pr(10)   # the PR is left open
        # the frontier was not worked, the peer not touched, no worker told anything
        assert w.claimed_by(3) is None and w.claimed_by(4) == "peer-live"
        assert [len(t["sent"]) for t in w.terminals()] == sent
        # an open issue's worktree may hold work: the drain removes none
        assert {wt["linkedIssue"] for wt in w.worktrees()} == {1, 2}

        # draining twice is harmless, and so is a drain that was never preceded by a cycle
        assert cycle(w, done["state"], "--drain")["progress"] == "drained; kept #1"
        assert cycle(w, None, "--drain")["progress"] == "drained; kept #1"


def test_a_tick_in_code_settles_every_row_the_rebuild_routes():
    """One `afk cycle` does what a tick used to read 300 lines to do: release a
    claim that outlived its issue, delete a dead peer's phantom lock under its sha,
    reclaim a dead peer's work and continue it, continue an orphaned claim with
    nothing asked, leave a landing worker alone, and fill what slots are then free
    from the frontier, in its order."""
    issues = [issue(n, "ready-for-agent") for n in (1, 2, 3, 6, 7, 8, 9)]
    issues += [issue(4, "ready-for-agent", state="closed"),          # mine: its PR landed
               issue(5, "ready-for-agent", state="closed")]          # a dead peer finished it
    with world(issues=issues) as w:
        with_pr(w, 7, 70)
        assert w.afk(*_turn(7))["outcome"] == "granted"              # #7 is landing
        for n, inst in ((4, "me"), (8, "me"), (5, "peer-dead"), (6, "peer-dead"), (9, "peer-live")):
            w.afk("claim", str(n), "--instance", inst, *NOW, *R)     # #8: mine, and no worker
        w.afk("heartbeat", "--instance", "peer-dead", "--now", str(T0 - TTL - 60), *R)
        w.afk("heartbeat", "--instance", "peer-live", "--now", str(T0 - 60), *R)
        phantom = w.sb.remote_ref("refs/afk/claim/5")
        told, turns = len(w.terminals()[0]["sent"]), _turns(w, 70)

        r = cycle(w, None, "--set", "concurrency=5")
        assert r["judgments"] == [] and "errors" not in r
        assert r["progress"] == ("dispatched #8, #1, #2; reclaimed #6; cleared #4, #5; "
                                 "5 in flight, 1 left on the frontier")
        # a `closed` row released; a `stale_closed` lock deleted — it was the sha read
        assert w.claimed_by(4) is None and w.claimed_by(5) is None
        assert phantom and not w.sb.remote_ref("refs/afk/claim/5")
        # a `stale` claim reclaimed, then dispatched; an orphan continued, unasked
        assert w.claimed_by(6) == "me" and w.claimed_by(8) == "me"
        # free slots filled in frontier order: 5 − {4,7,8} + the one #4 freed − #6 = 2
        assert w.claimed_by(1) == w.claimed_by(2) == "me" and w.claimed_by(3) is None
        # every one of them has a worker, started with the run's launch command
        started = {wt["linkedIssue"] for wt in w.worktrees()}
        assert started == {1, 2, 6, 7, 8}
        assert [t["command"] for t in w.terminals()] == [WORKER] * 5
        # the `landing` row was left alone: not told again, its turn not rewritten
        assert len(w.terminals()[0]["sent"]) == told and _turns(w, 70) == turns
        assert _mine(w, (), 7)[0] == "landing"
        # a live peer's claim is never touched
        assert w.claimed_by(9) == "peer-live"
        # each claim still held shows where it stands to a human reading the issue
        assert "(#70)" in w.board(7) and w.board(8) and w.board(6)

        # the next cycle finds every worker coding: nothing to do, nothing asked
        nxt = cycle(w, r["state"], "--set", "concurrency=5")
        assert (nxt["action"], nxt["judgments"]) == ("tick", [])
        assert nxt["progress"] == "5 in flight, 1 left on the frontier"


def test_a_tick_in_code_carries_out_every_no_pr_action_whose_reason_is_on_record():
    """What `afk no-pr` concludes about a stopped worker is a table lookup away from
    a transition, and the tick runs it: re-dispatch, park, escalate, nudge, fail —
    then gives the one landing turn, leaves what is still running, and fills the
    slots the settled claims freed."""
    issues = [issue(n, "ready-for-agent") for n in (1, 2, 3, 4, 5, 6, 7, 8, 12)]
    issues += [issue(11, "ready-for-agent", state="closed")]
    with world(issues=issues) as w:
        t = int(time.time()) + 5000                # every worker has been quiet past the grace
        for n in (1, 2, 3, 4, 5, 6):
            w.afk(*dispatch(n))
        with_pr(w, 7, 70, conclusion="PENDING")
        with_pr(w, 8, 80)
        verdict(w, 1, "blocked", 11)                               # its blocker has closed
        verdict(w, 2, "blocked", 12)                               # the backlog will resolve it
        verdict(w, 3, "blocked", 13)                               # nothing will: no such issue
        verdict(w, 5, "giving-up", reason="the fixture is unbuildable")
        w.worker(output=t - 1, state="working", since=t - 60, n=5)  # #6 is at it
        told = [len(term["sent"]) for term in w.terminals()]

        r = cycle(w, None, "--set", "concurrency=8", now=t)
        assert r["judgments"] == [] and "errors" not in r, r
        assert r["progress"] == ("landing turn to #8; dispatched #1, #12; escalated #3; parked #2; "
                                 "retried #5; nudged #4; 7 in flight, 0 left on the frontier")
        # park: the edge recorded, the claim released, ready_label kept
        assert w.state()["deps"]["2"] == [12] and w.claimed_by(2) is None
        assert w.issue(2)["labels"] == ["ready-for-agent"]
        # escalate: relabelled for a human, with the reason the blockers row carried
        assert w.issue(3)["labels"] == ["ready-for-human"] and w.claimed_by(3) is None
        assert "#13 could not be read" in w.comments(3)[-1]
        # fail: the attempt counted, a fresh worker handed the worker's own reason
        assert w.issue(5)["labels"] == ["ready-for-agent", "afk-attempt/1"]
        assert "the fixture is unbuildable" in _told(_worker_of(w, 5))
        # nudge: one line typed at the silent worker, nothing spent
        assert len(w.terminals()[3]["sent"]) == told[3] + 1 and w.issue(4)["labels"] == ["ready-for-agent"]
        # leave: the busy worker, and the PR whose checks are still running
        assert len(w.terminals()[5]["sent"]) == told[5] and len(w.terminals()[6]["sent"]) == told[6]
        assert _turns(w, 70) == [] and len(_turns(w, 80)) == 1
        # the two claims it settled freed two slots: the re-dispatch needed none
        assert w.claimed_by(12) == "me" and w.claimed_by(1) == "me"

        # a grace period on, the nudged worker is still silent: that is a failure
        # whose reason is on record too
        later = cycle(w, r["state"], "--set", "concurrency=8", now=t + 400)
        assert "retried #4" in later["progress"] and w.issue(4)["labels"][-1] == "afk-attempt/1"
        assert "after its nudge" in _told(_worker_of(w, 4))


def _worker_of(w, n):
    """The terminal of the worker now on issue n."""
    path = next(wt["path"] for wt in w.worktrees() if wt["linkedIssue"] == n)
    return [t for t in w.terminals() if t["open"] and t["worktreePath"] == path][-1]


def _judged(r):
    return {j["issue"]: j for j in r["judgments"]}


def test_a_tick_returns_each_judgment_with_a_command_for_either_answer():
    """What code cannot decide comes back as a judgment: a question, and the one
    `afk` transition for each answer — runnable as handed over. Every answer is a
    transition, so whichever is run, the next cycle does not ask again."""
    for pick in ("if_yes", "if_no"):
        for verify in ((), ("--set", "gate.adversarial_verify=true")):
            issues = [issue(n, "ready-for-agent") for n in (1, 2, 3)]
            with world(issues=issues) as w:
                w.afk(*dispatch(1))
                verdict(w, 1, "already-satisfied")                  # idle, nothing on its branch
                with_pr(w, 2, 20, conclusion="FAILURE")             # its checks are red
                _, head = with_pr(w, 3, 30, conclusion=None)        # no checks at all
                t = int(time.time()) + 5000

                r = cycle(w, None, *verify, now=t)
                asked = _judged(r)
                assert {n: j["kind"] for n, j in asked.items()} == {
                    1: "empty_diff", 2: "reason", 3: "adversarial_verify" if verify else "no_checks"}
                # judgments open: answer them and come straight back
                assert r["sleep_seconds"] == 0 and "3 judgments open" in r["progress"]
                for j in asked.values():
                    assert set(j) - {"bulky"} == {"issue", "kind", "question", "context",
                                                  "if_yes", "if_no"}
                # the two whose answer means reading something long say so
                assert sorted(n for n, j in asked.items() if j.get("bulky")) == ([2, 3] if verify else [2])
                assert asked[1]["context"]["worktree"] == w.worktrees()[0]["path"]
                assert asked[3]["context"]["head"] == head
                # nothing was decided for the caller: every claim is as it was
                assert w.issue(1)["state"] == "open" and "state" not in w.pr(20)
                assert _turns(w, 30) == [] and all(w.claimed_by(n) == "me" for n in (1, 2, 3))
                # a `reason` judgment's transition is fixed; the answer is its wording
                assert asked[2]["if_yes"] == asked[2]["if_no"]

                # the caller that never answered is asked again, digest unmoved or not
                again = cycle(w, r["state"], *verify, now=t)
                assert (again["reason"], _judged(again).keys()) == ("unsettled", asked.keys())

                done = {n: answer(w, j[pick]) for n, j in asked.items()}
                assert done[2]["action"] == "retry" and w.pr(20)["state"] == "closed"
                if pick == "if_yes":
                    assert done[1]["action"] == "closed" and w.issue(1)["state"] == "closed"
                    assert done[3]["outcome"] == "granted" and len(_turns(w, 30)) == 1
                    turn = afk_decide.latest_turn([{"body": b} for b in _turns(w, 30)])
                    assert turn["allow_no_checks"] and turn["verified"] == (head if verify else None)
                else:
                    assert done[1]["action"] == done[3]["action"] == "retry"
                    assert w.pr(30)["state"] == "closed" and w.claimed_by(1) == "me"

                after = cycle(w, again["state"], *verify, now=int(time.time()))
                assert after["action"] == "tick" and after["judgments"] == [], after
                assert after["sleep_seconds"] == 90 and after["state"]["unsettled"] is False


def test_a_failed_transition_is_reported_and_leaves_its_claim_held():
    """An `{"error": …}` from one transition of a tick is not "nothing to do": it
    is reported in the result, the claim it was about stays held, the rest of the
    tick goes on, and the next cycle ticks again whatever the digest says."""
    with world(issues=[issue(n, "ready-for-agent") for n in (1, 2)]) as w:
        t = int(time.time()) + 5000
        w.afk(*dispatch(1))
        w.afk(*dispatch(2))
        verdict(w, 1, "blocked", 13)                               # → escalate: no such issue
        verdict(w, 2, "giving-up", reason="cannot build")          # → fail
        w.set(fail=["issue edit"])                                 # GitHub refuses a relabel

        r = cycle(w, None, now=t)
        assert [(e["step"], e["issue"]) for e in r["errors"]] == [("escalate", 1), ("fail", 2)]
        assert all("gh issue edit failed" in e["error"] for e in r["errors"])
        assert r["progress"] == "2 errors; 2 in flight, 0 left on the frontier"
        # nothing settled: both claims held, nobody handed a half-escalated issue
        assert w.claimed_by(1) == w.claimed_by(2) == "me"
        assert w.issue(1)["labels"] == w.issue(2)["labels"] == ["ready-for-agent"]
        assert (r["sleep_seconds"], r["state"]["unsettled"]) == (90, True)

        # nothing observable moved, and it is NOT skipped: the work is still owed
        w.set(fail=[])
        nxt = cycle(w, r["state"], now=t)
        assert (nxt["action"], nxt["reason"]) == ("tick", "unsettled") and "errors" not in nxt
        assert nxt["progress"] == "escalated #1; retried #2; 1 in flight, 0 left on the frontier"

    # a worker that cannot be STARTED ends the starting for this tick: the remote
    # or orca is unwell, and every further dispatch would take a claim it cannot staff
    with world(issues=[issue(n, "ready-for-agent") for n in (1, 2)]) as w:
        w.orca(never_ready=True)
        r = cycle(w, None, "--ready-timeout", "1")
        assert [(e["step"], e["issue"]) for e in r["errors"]] == [("dispatch", 1)]
        assert "not ready" in r["errors"][0]["error"]
        assert w.claimed_by(1) == "me" and w.claimed_by(2) is None
        # the claim it took is held, and the next tick finds it with no worker… which
        # here is one whose terminal is still there: left to its grace period
        w.orca(never_ready=False)
        nxt = cycle(w, r["state"], "--ready-timeout", "1")
        assert "errors" not in nxt and "dispatched #2" in nxt["progress"]
        assert w.claimed_by(1) == w.claimed_by(2) == "me"


# --------------------------------------------------------------------------- #
# no-pr                                                                        #
# --------------------------------------------------------------------------- #

def _orca_row(issue_no, path, **extra):
    return {"linkedIssue": issue_no, "path": path, "branch": "refs/heads/sunfmin/issue-31-x",
            "projectId": f"github:{REPO}", "isMainWorktree": False, "isArchived": False,
            "lastActivityAt": 100, **extra}


def _marker(phase, extra=""):
    return f"<!--afk:verdict n=4 phase={phase}{extra}-->\nexplanation"


def _comment(cid, body):
    return {"id": cid, "body": body, "html_url": f"https://gh/c/{cid}"}


def test_no_pr_gathers_every_signal_and_decides_in_one_call():
    with world(issues=[issue(4, "ready-for-agent"), issue(41), issue(42)]) as w:
        cfg = json.dumps({"base_branch": w.sb.base, "worker_idle_grace_seconds": 300})
        base = ("--issue", "4", *R, "--config", cfg)
        w.orca([_orca_row(4, w.cwd)],
               terminals=[{"handle": "term-1", "worktreePath": w.cwd, "sent": [], "open": True}])
        real_now = int(time.time())
        soon, later = str(real_now + 30), str(real_now + 5000)

        # a worker that reports nothing; a clean worktree at base touched seconds
        # ago, no verdict → still coding
        r = w.no_pr(*base, "--now", soon)
        assert (r["outcome"], r["action"]) == ("coding", "leave"), r
        assert r["progress"]["commits_ahead"] == 0 and r["progress"]["dirty"] is False
        assert 0 <= r["idle_seconds"] < 300 and r["worker_verdict"]["found"] is False
        # the tool's conclusion is `outcome`/`action`; what the WORKER declared is
        # `worker_verdict` — never one bare "verdict" that could be read as either
        assert set(r) == {"issue", "outcome", "action", "idle_seconds", "pending_blockers",
                          "worktree", "progress", "worker_verdict", "blockers", "nudged_at",
                          "turn_at", "worker_state"}
        assert r["issue"] == 4 and r["pending_blockers"] == [] and r["worktree"] == w.cwd
        assert r["blockers"] == []
        assert r["worker_state"] is None

        # idle_seconds is derived HERE, from the freshest of commit / file / terminal
        # clocks: the tick supplies nothing, not even a reading
        r = w.no_pr(*base, "--now", later)
        assert 4900 < r["idle_seconds"] < 5100                    # the worktree's own clocks
        # silent with no verdict: stalled, to be nudged — not yet a failure
        assert (r["outcome"], r["action"], r["nudged_at"]) == ("idle_stalled", "nudge", None)
        # a runtime that reports no state is asked after through orca's own idle
        # detection — never its output, which an idle qoderclicn redraws on a timer
        w.worker(output=int(later) - 12)
        assert w.no_pr(*base, "--now", later)["outcome"] == "idle_stalled"
        w.worker(tui_busy=True)
        w.calls()
        r = w.no_pr(*base, "--now", later)
        assert (r["outcome"], r["worker_state"]) == ("coding", None) and w.calls() == []

        # ADR-0021: a worker its runtime reports `working` is busy, and a busy worker
        # costs nothing more — no GitHub, no git — however long since its last commit
        w.worker(output=int(later) - 2, state="working", since=real_now)
        w.calls()
        r = w.no_pr(*base, "--now", later)
        assert (r["outcome"], r["action"], r["worker_state"]) == ("coding", "leave", "working")
        assert (r["progress"], r["worker_verdict"]) == ({}, None)
        assert w.calls() == []
        # …unless the terminal has said nothing for a grace period: a lost stop report
        w.worker(output=int(later) - 400, state="working", since=real_now)
        r = w.no_pr(*base, "--now", later)
        assert (r["outcome"], r["worker_state"]) == ("idle_stalled", "working")
        # a worker that STOPPED is timed from when it stopped, whatever it redraws
        w.worker(output=int(later) - 1, state="done", since=int(later) - 400)
        r = w.no_pr(*base, "--now", later)
        assert (r["outcome"], r["idle_seconds"], r["worker_state"]) == ("idle_stalled", 400, "done")
        w.worker(output=int(later) - 1, state="waiting", since=int(later) - 100)
        assert w.no_pr(*base, "--now", later)["outcome"] == "coding"     # stopped within grace
        # grace: --set beats config
        w.worker(output=int(later) - 400, state="done", since=int(later) - 400)
        r = w.no_pr(*base, "--now", later, "--set", "worker_idle_grace_seconds=99999")
        assert r["outcome"] == "coding"
        # no live terminal: gone — also decided from orca alone
        terms = w.terminals()
        terms[0]["open"] = False
        w.orca(terminals=terms)
        w.calls()
        r = w.no_pr(*base, "--now", soon)
        assert (r["outcome"], r["action"], r["worker_state"]) == ("dead", "orphan", None)
        assert w.calls() == []
        terms[0]["open"] = True
        w.orca(terminals=terms)
        w.worker()

        # the worker declared itself blocked on #41 and #42: their REAL state routes it
        # (neither is anything a fleet will work — the parkable case has its own test)
        w.set(comments={"4": [_comment(1, "a human note"),
                              _comment(2, _marker("giving-up")),
                              _comment(3, _marker("blocked", " blocked_by=41,42 reason=needs both"))]})
        w.calls()
        r = w.no_pr(*base, "--now", later)
        assert (r["outcome"], r["action"], r["pending_blockers"]) == ("idle_blocked", "escalate", [41, 42])
        assert r["worker_verdict"]["phase"] == "blocked"
        assert r["worker_verdict"]["reason"] == "needs both"
        assert r["worker_verdict"]["comment_url"] == "https://gh/c/3"   # the LATEST marker wins
        assert [(b["number"], b["standing"]) for b in r["blockers"]] == [(41, "unmet"), (42, "unmet")]
        assert "no ready-for-agent label" in r["blockers"][0]["reason"]
        reads = sorted(c[1] for c in w.calls() if "--jq" in c and "state_reason" in c[-1])
        assert reads == [f"repos/{REPO}/issues/41", f"repos/{REPO}/issues/42"]

        w.set(issues=[issue(4), issue(41, state="closed"), issue(42)])
        r = w.no_pr(*base, "--now", later)
        assert (r["action"], r["pending_blockers"]) == ("escalate", [42])
        w.set(issues=[issue(4), issue(41, state="closed"), issue(42, state="closed")])
        r = w.no_pr(*base, "--now", later)
        assert (r["outcome"], r["action"], r["pending_blockers"]) == ("idle_blocked", "redispatch", [])
        # a blocker that cannot be read at all is not provably closed
        w.set(issues=[issue(4), issue(41, state="closed")])       # #42 is now a 404
        assert w.no_pr(*base, "--now", later)["pending_blockers"] == [42]

        # already-satisfied over a pristine branch → close + release…
        w.set(comments={"4": [_comment(9, _marker("already-satisfied"))]})
        r = w.no_pr(*base, "--now", later)
        assert (r["outcome"], r["action"]) == ("idle_done", "close_release")
        # …but real work on the branch refutes it: commits, or just a dirty tree
        with open(os.path.join(w.cwd, "wip.txt"), "w") as f:
            f.write("uncommitted\n")
        later = str(int(time.time()) + 5000)
        r = w.no_pr(*base, "--now", later)
        assert r["progress"]["dirty"] is True and r["progress"]["commits_ahead"] == 0
        assert (r["outcome"], r["action"]) == ("idle_failed", "next_attempt")
        w.commit("done.txt", branch="sunfmin/issue-4-x")
        later = str(int(time.time()) + 5000)
        r = w.no_pr(*base, "--now", later)
        assert r["progress"]["commits_ahead"] == 2 - 1 and r["progress"]["dirty"] is False
        assert (r["outcome"], r["action"]) == ("idle_failed", "next_attempt")
        # the base it counts against is the config's — and it is the REMOTE's tip of
        # that branch, not the local one: a checkout that has not pulled must not make
        # a worktree cut from the fresh tip look like it carries work
        git(w.cwd, "push", "-q", "origin", "HEAD:refs/heads/release")
        r = w.no_pr(*base, "--now", later, "--set", "base_branch=release")
        assert r["progress"]["commits_ahead"] == 0 and r["outcome"] == "idle_done"
        git(w.cwd, "branch", "-q", "-f", "release", "HEAD~1")        # a stale LOCAL release
        r = w.no_pr(*base, "--now", later, "--set", "base_branch=release")
        assert r["progress"]["commits_ahead"] == 0
        assert "no branch 'gone'" in w.error("no-pr", *base, "--set", "base_branch=gone")


def test_a_silent_worker_is_nudged_once_and_its_screen_explains_the_failure():
    """A worker that stops to ask a question nobody will answer is idle with no
    verdict. Failing it straight away discards its work and sends a fresh worker
    into the same wall; so it is told to carry on first — once, spending no attempt
    — and if it stays silent, what its screen said goes into the failure reason."""
    asking = ["● 这些都是对外可见的操作，我需要你确认一下。", "", "  要我从头做到开 PR 吗？", "❯ "]
    with world(issues=[issue(7, "ready-for-agent")]) as w:
        d = w.afk(*dispatch(7))
        wt, real_now = d["worktree"], int(time.time())
        cfg = ("--config", json.dumps({"base_branch": w.sb.base, "worker_idle_grace_seconds": 300}))
        nudge = ("nudge", "--issue", "7", *ME, *R, *cfg)

        def no_pr(at, busy=False):
            if busy:                     # its runtime reports it working, output just now
                w.worker(output=at - 1, state="working", since=at - 60)
            r = w.no_pr("--issue", "7", *R, *cfg, "--now", str(at))
            return r["outcome"], r["action"]

        def screen(lines):
            terms = w.terminals()
            terms[-1]["screen"] = lines
            w.orca(terminals=terms)

        t1 = real_now + 5000
        assert no_pr(t1) == ("idle_stalled", "nudge")
        screen(asking)
        w.orca_calls()
        r = w.afk(*nudge, "--now", str(t1))
        assert r == {"issue": 7, "action": "nudged", "terminal": d["terminal"],
                     "terminal_tail": [asking[0], asking[2], "❯"]}
        assert w.orca_calls() == ["terminal list", "terminal read", "terminal send"]
        brief, said = w.terminals()[-1]["sent"]
        assert said["enter"] is True and "\n" not in said["text"]
        assert "do not wait for a confirmation" in said["text"] and "afk:verdict" in said["text"]
        assert brief["text"].split(" — ")[0].split(" ")[-1] in said["text"]   # names the same brief
        # nothing was spent or discarded, and the worktree stays clean of fleet files
        assert w.issue(7)["labels"] == ["ready-for-agent"] and w.claimed_by(7) == "me"
        assert git(wt, "status", "--porcelain") == ""

        # the nudge buys one grace period…
        r = w.no_pr("--issue", "7", *R, *cfg, "--now", str(t1 + 60))
        assert (r["outcome"], r["idle_seconds"], r["nudged_at"]) == ("coding", 60, t1)
        # …and it is spent once: the second silence is a failure, by outcome and by rule
        assert no_pr(t1 + 300) == ("idle_failed", "next_attempt")
        assert "already nudged" in w.error(*nudge)
        # a worker that answered the nudge is simply coding, or done
        assert no_pr(t1 + 9000, busy=True) == ("coding", "leave")

        # the failure reason carries where it stopped — its screen as it is NOW
        screen(["● 我还是需要你确认。"])
        r = w.afk(*_fail(7, "idle past grace with no PR and no verdict"))
        assert r["action"] == "retry"
        told = _told(w.terminals()[-1])
        assert "idle past grace with no PR and no verdict" in told
        assert "stayed silent after one nudge" in told and "● 我还是需要你确认。" in told
        # the retry's worker is a new worker: it has not been nudged
        assert no_pr(t1 + 9000) == ("idle_stalled", "nudge")

    with world(issues=[issue(8, "ready-for-agent")]) as w:
        d = w.afk(*dispatch(8))
        cfg = ("--config", json.dumps({"base_branch": w.sb.base}))
        nudge = ("nudge", "--issue", "8", *R, *cfg)
        # only my own claim, only a worker that is still there
        assert "not this fleet's claim" in w.error(*nudge, "--instance", "peer")
        terms = w.terminals()
        terms[-1].update(screen=["● 确认一下？"], open=False)
        w.orca(terminals=terms)
        assert "no live terminal" in w.error(*nudge, *ME)
        # a worker restarted in the SAME worktree starts un-nudged, and when the
        # terminal is gone by failure time the screen saved at the nudge still speaks
        terms[-1]["open"] = True
        w.orca(terminals=terms)
        w.afk(*nudge, *ME)
        terms = w.terminals()
        terms[-1]["open"] = False
        w.orca(terminals=terms)
        r = w.afk(*_fail(8, "went quiet", "--set", "retry=0"))
        assert r["action"] == "escalate" and "● 确认一下？" in w.comments(8)[-1]
        w.afk("claim", "8", *ME, "--now", str(T0 + 1), *R)
        w.set(issues=[issue(8, "ready-for-agent")])
        r = w.afk(*dispatch(8)[:-2], "--now", str(T0 + 2))
        assert r["action"] == "reuse_worktree"
        assert w.no_pr("--issue", "8", *R, *cfg,
                       "--now", str(int(time.time()) + 5000))["action"] == "nudge"
        assert "no worktree on this machine" in w.error(
            "nudge", "--issue", "8", *ME, *R, *cfg, "--worktree", "/no/such/dir")


def test_no_pr_without_a_worktree_and_with_bad_input():
    with world(issues=[issue(4)], comments={"4": [_comment(1, _marker("giving-up"))]}) as w:
        # no worktree on this machine: no worker here either — gone, whatever it
        # declared, and nothing more is read to say so
        r = w.no_pr("--issue", "4", *R)
        assert (r["outcome"], r["action"], r["worktree"], r["progress"]) == \
            ("dead", "orphan", None, {})
        assert w.calls() == []

        # the worktree is found through orca — the tick passes no path
        term = {"handle": "term-1", "worktreePath": w.cwd, "sent": [], "open": True}
        w.orca([_orca_row(4, w.cwd), _orca_row(4, w.cwd + "-other", projectId="github:acme/other")],
               terminals=[term])
        r = w.no_pr("--issue", "4", *R)
        assert r["worktree"] == w.cwd and r["progress"]["commits_ahead"] == 0
        r = w.no_pr("--issue", "4", *R, "--now", str(int(time.time()) + 5000))
        assert (r["outcome"], r["action"]) == ("idle_failed", "next_attempt")   # giving-up
        # one orca remembers but the disk no longer has is no worktree
        w.orca([_orca_row(4, os.path.join(w.sb.root, "gone"))])
        r = w.no_pr("--issue", "4", *R)
        assert r["worktree"] is None and r["outcome"] == "dead"

        # a worktree path GIVEN that does not exist is a mistake, never "no progress":
        # read as empty it could close an issue whose branch holds real work
        err = w.error("no-pr", "--issue", "4", "--worktree", "/no/such/dir", *R)
        assert "worktree not found" in err
        # and it names ONE worker's worktree
        err = w.error("no-pr", "--issue", "4", "--issue", "5", "--worktree", w.cwd, *R)
        assert "one --issue" in err
        # a remote that cannot be read is an error too, not an absent verdict
        w.orca([_orca_row(4, w.cwd)])
        assert "acme/other" in w.error("no-pr", "--issue", "4", "--worktree", w.cwd,
                                       "--repo", "acme/other")
        # and so is an orca that cannot be asked — never "the worker is gone", which
        # would start a second worker beside a live one
        assert "orca worktree" in w.error("no-pr", "--issue", "4", *R,
                                          env={"AFK_FAKE_ORCA_EXIT": "1"})
        w.orca(ps_truncated=True)
        assert "truncated" in w.error("no-pr", "--issue", "4", *R)


def test_no_pr_asks_after_every_worker_in_one_call_and_only_reads_github_for_stopped_ones():
    """ADR-0021: a tick asks once for all its PR-less claims. The busy ones — the
    usual case — are settled from orca's worker state; only a worker that stopped
    has its verdict, blockers and PR read from GitHub."""
    with world(issues=[issue(1, "ready-for-agent"), issue(2, "ready-for-agent")]) as w:
        w.afk(*dispatch(1))
        w.afk(*dispatch(2, "--now", str(T0 + 1)))
        now = int(time.time()) + 5000
        w.worker(output=now - 3, state="working", since=now - 900, n=0)
        w.worker(output=now - 3, state="done", since=now - 600, n=1)
        w.calls()
        r = w.afk("no-pr", "--issue", "2", "--issue", "1", *R, "--now", str(now))
        assert [(x["issue"], x["outcome"], x["worker_state"]) for x in r["workers"]] == \
            [(2, "idle_stalled", "done"), (1, "coding", "working")]
        read = [c for c in w.calls() if c[:1] == ["api"] or c[:2] == ["pr", "list"]]
        assert read and all("issues/1/" not in " ".join(c) for c in read), read


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
        assert w.afk("rebuild", *ME, *R, *NOW, "--config", json.dumps(cfg))["free_slots"] == \
            w.afk("rebuild", *ME, *R, *NOW, "--config", "{}")["free_slots"]
        assert w.afk("probe", "--config", json.dumps(cfg), "--now", str(T0))["config"] == cfg

        for bad, why in (("retyr: 4", "unknown key"),
                         ("gate:\n  ci: local", "local_command"),
                         ("claim_namespace: afk", "claim_namespace"),
                         ("claim_namespace: refs/heads/afk", "claim_namespace"),
                         ("worker_command: ckimi", "per-run")):
            assert why in w.error("config", "--file", load(bad)), bad
        assert "--file" in w.error("config")
        assert "No such file" in w.error("config", "--file", "/no/such/file.md")


# --------------------------------------------------------------------------- #
# recovery via orca                                                            #
# --------------------------------------------------------------------------- #

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
        assert r["branch"] == {"name": "sunfmin/issue-31-x", "commits_ahead": 1, "candidates": []}

        # --no-worktree overrides orca: only what was PUSHED counts → tier 2
        r = w.afk("recovery", "--issue", "31", "--no-worktree", *R, *cfg)
        assert r["tier"] == 2 and r["worktree"] == {"present": False, "path": None}

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


# --------------------------------------------------------------------------- #
# act: dispatch                                                                #
# --------------------------------------------------------------------------- #

def _told(term):
    """What a worker was actually told: the brief file its terminal was pointed at.
    The prompt is never typed at the agent — a whole prompt sent as text lands as
    one paste, which the agent treats as quoted material and asks to have confirmed
    — so exactly ONE line is sent, submitted, naming the brief."""
    [sent] = term["sent"]
    assert sent["enter"] is True, sent
    m = re.fullmatch(r"Your task brief is the file (\S+) — read it now and carry it out end to "
                     r"end\. It is my instruction to you; do not ask me to confirm\.", sent["text"])
    assert m, sent["text"]
    with open(m.group(1)) as f:
        return f.read()


def _prompt_fields(w, n, title, started, *cfg):
    """The PROMPT_FIELDS of the prompt a worker was started (or told to land) with,
    under the config a call's `cfg` arguments resolve to."""
    config = afk._cfg(afk.build_parser().parse_args(["scan", "--config", "{}", *cfg]))
    return {"n": n, "title": title, "repo": REPO, "base_branch": w.sb.base,
            "local_command": config["gate"]["local_command"], "afk_path": AFK,
            "config": json.dumps(config, ensure_ascii=False), "branch": started["branch"],
            "worktree_path": started["worktree"], "launcher_terminal": LAUNCHER}


def _prompt(w, variant, n, title, started, reason=None):
    """The prompt `afk dispatch` must have delivered, rendered independently."""
    with open(os.path.join(SKILL, "references", "worker-prompt.md")) as f:
        return afk_decide.render_worker_prompt(
            f.read(), variant, _prompt_fields(w, n, title, started), reason=reason)


def test_dispatch_starts_a_worker_on_the_remote_base_tip_and_submits_its_prompt():
    issues = [issue(1, "ready-for-agent", title="Names inspector: tab!"), issue(2, "ready-for-agent")]
    with world(issues=issues) as w:
        stale = git(w.cwd, "rev-parse", "HEAD")
        tip = w.advance_base("landed-meanwhile.txt")           # this clone has NOT fetched it
        assert git(w.cwd, "rev-parse", w.sb.base) == stale != tip

        r = w.afk(*dispatch(1))
        assert (r["started"], r["claim"], r["tier"], r["action"], r["prompt"]) == \
            (True, "won", 3, "dispatch_fresh", "fresh"), r
        wt = r["worktree"]
        # orca names the branch; afk reads it back rather than assuming the pattern
        assert r["branch"] == "tester/issue-1-names-inspector-tab"
        assert git(wt, "rev-parse", "--abbrev-ref", "HEAD") == r["branch"]
        # the worker starts from what the REMOTE has, not from this clone's stale base
        assert git(wt, "rev-parse", "HEAD") == tip
        assert w.orca_calls() == ["repo list", "worktree create", "terminal create",
                                  "terminal wait", "terminal send"]
        assert [(t["linkedIssue"], t["path"]) for t in w.worktrees()] == [(1, wt)]

        # the agent was started with the run's worker launch command, verbatim —
        # and the prompt was SUBMITTED, not just typed
        [term] = w.terminals()
        assert (term["handle"], term["command"], term["worktreePath"]) == (r["terminal"], WORKER, wt)
        told = _told(term)
        assert told == _prompt(w, "fresh", 1, "Names inspector: tab!", r)
        assert f"`{r['branch']}`" in told and f"`{wt}`" in told
        assert "Closes #1" in told and "{" + "branch" + "}" not in told
        # and it is told how to wake the launcher that dispatched it (ADR-0020)
        assert f'orca terminal send --terminal {LAUNCHER} --text "afk-wake #1" --enter' in told
        # the brief lives in the worktree's git dir: a worker's `git add -A` never stages it
        assert git(wt, "status", "--porcelain") == ""

        # the claim is mine, and the invisible phase is on the issue for a human
        assert w.claimed_by(1) == "me"
        assert "认领方 `me`" in w.board(1) and "尚无 PR" in w.board(1)
        ws = w.afk("rebuild", *ME, *R, *NOW)
        assert [(m["number"], m["status"]) for m in ws["mine"]] == [(1, "no_pr")]
        assert ws["free_slots"] == 2 and [f["number"] for f in ws["frontier"]["dispatch"]] == [2]
        # a worktree cut from the fresh tip is pristine, whatever the stale local base says
        r_np = w.no_pr("--issue", "1", *R)
        assert (r_np["worktree"], r_np["progress"]["commits_ahead"]) == (wt, 0)

        # however orca resolved the base it was handed, the worktree ends up containing
        # the fetched tip — asserted by code, not hoped for
        w.orca(stale_base=stale)
        r2 = w.afk(*dispatch(2, "--set", "progress_comment=false"))
        assert git(r2["worktree"], "rev-parse", "HEAD") == tip and w.board(2) == ""


def test_dispatch_claims_first_and_never_starts_a_worker_it_cannot_own():
    issues = [issue(n, "ready-for-agent") for n in (1, 3, 4)] + [issue(2, "ready-for-agent", state="closed")]
    with world(issues=issues) as w:
        # a peer holds it → an OUTCOME (exit 0), and nothing at all was started
        w.afk("claim", "1", "--instance", "peer", *NOW, *R)
        r = w.afk(*dispatch(1))
        assert (r["started"], r["claim"], r["owner"]["instance"]) == (False, "lost", "peer")
        assert set(r) == {"issue", "started", "claim", "owner"}
        assert w.orca_calls() == [] and w.worktrees() == [] and w.board(1) == ""

        # a closed or unknown issue is refused BEFORE the claim: never a lock on nothing
        assert "closed" in w.error(*dispatch(2))
        assert "gh api" in w.error(*dispatch(99))
        assert w.claimed_by(2) is None and w.claimed_by(99) is None

        # orca cannot serve this repo → an error — with the claim held, so the next
        # tick sees a claim of mine with no worker and simply dispatches it again
        w.orca(repos=[])
        assert "orca knows no repo" in w.error(*dispatch(3))
        assert w.claimed_by(3) == "me" and w.worktrees() == []
        w.orca(repos=[{"id": "repo-1", "path": w.cwd,
                       "gitRemoteIdentity": {"canonicalKey": f"github.com/{REPO}"}}])
        r = w.afk(*dispatch(3, "--now", str(T0 + 90)))          # the next tick
        assert (r["started"], r["claim"], r["tier"]) == (True, "held", 3)

        # the agent never became ready → an error naming it; the worktree it made is
        # reused, not duplicated, by the retry
        w.orca(never_ready=True)
        assert "not ready" in w.error(*dispatch(4, "--ready-timeout", "1"))
        assert w.claimed_by(4) == "me" and len(w.worktrees()) == 2
        assert w.terminals()[-1]["sent"] == []                   # nothing was typed at it
        w.orca(never_ready=False)
        r = w.afk(*dispatch(4, "--now", str(T0 + 90)))
        # a pristine worktree gets the FRESH prompt: there is nothing to continue
        assert (r["claim"], r["tier"], r["action"], r["prompt"]) == ("held", 1, "reuse_worktree", "fresh")
        assert len(w.worktrees()) == 2
        assert [t["open"] for t in w.terminals() if t["worktreePath"] == r["worktree"]] == [False, True]


def test_dispatch_continues_from_whatever_progress_survived():
    """ADR-0011's tiers, acted on rather than described: the same `afk dispatch`
    that starts a frontier issue resumes a claim whose worker died."""
    with world(issues=[issue(7, "ready-for-agent")]) as w:
        first = w.afk(*dispatch(7))
        pushed = w.work(first["worktree"], "step1.txt")
        local = w.work(first["worktree"], "step2.txt", push=False)   # committed, never pushed
        wip = os.path.join(first["worktree"], "wip.txt")
        with open(wip, "w") as f:
            f.write("uncommitted\n")
        w.orca_calls()

        # tier 1: the worktree is still here → reuse it, lossless, on its own branch
        r = w.afk(*dispatch(7, "--now", str(T0 + 90)))
        assert (r["claim"], r["tier"], r["action"], r["prompt"]) == ("held", 1, "reuse_worktree", "continue")
        assert (r["worktree"], r["branch"]) == (first["worktree"], first["branch"])
        assert git(r["worktree"], "rev-parse", "HEAD") == local and os.path.exists(wip)
        # the dead worker's terminal is closed before a second agent enters the worktree
        assert w.orca_calls() == ["terminal close", "terminal create", "terminal wait", "terminal send"]
        old, new = w.terminals()
        assert (old["open"], new["open"], new["handle"]) == (False, True, r["terminal"])
        assert _told(new) == _prompt(w, "continue", 7, "issue 7", r)
        assert _told(new).startswith("You are an afk-fleet worker **continuing**")
        assert git(r["worktree"], "status", "--porcelain") == "?? wip.txt"   # the brief is not in it

        # tier 2: the worktree is gone (the worker ran on another machine) → recreate
        # at the PUSHED tip; orca gives it a new branch name, and the prompt carries it
        git(w.cwd, "worktree", "remove", "--force", first["worktree"])
        w.orca([])
        r2 = w.afk(*dispatch(7, "--now", str(T0 + 180)))
        assert (r2["tier"], r2["action"], r2["prompt"]) == (2, "recreate_at_tip", "continue")
        assert git(r2["worktree"], "rev-parse", "HEAD") == pushed
        assert r2["branch"] == first["branch"] + "-2"
        assert f"`{r2['branch']}`" in _told(w.terminals()[-1])
        w.work(r2["worktree"], "step3.txt")

        # --start fresh: the tick judged the recovered state unsafe to build on → the
        # previous attempt is discarded (its PR, its branches, its worktree), base again
        w.open_pr(70, closes=7, branch=first["branch"])
        w.set(prs=w.state()["prs"] + [pr(71, closes=7, headRefName="alice/hotfix")])   # a human's
        r3 = w.afk(*dispatch(7, "--start", "fresh", "--now", str(T0 + 270)))
        assert (r3["claim"], r3["tier"], r3["action"], r3["prompt"]) == ("held", 3, "dispatch_fresh", "fresh")
        assert r3["discarded"] == {"closed_prs": [70], "deleted_branches": [r2["branch"]],
                                   "removed_worktree": r2["worktree"]}
        assert w.pr(70)["state"] == "closed" and w.pr(71).get("state", "open") == "open"
        assert not [ref for ref in w.sb.all_refs() if "issue-7" in ref]
        assert not os.path.isdir(r2["worktree"]) and [t["path"] for t in w.worktrees()] == [r3["worktree"]]
        assert git(r3["worktree"], "rev-parse", "HEAD") == w.sb.remote_ref(f"refs/heads/{w.sb.base}")
        # a worktree that cannot be removed stops the fresh start: two attempts must
        # never share an issue
        w.orca(rm_fails=True)
        assert "could not remove" in w.error(*dispatch(7, "--start", "fresh"))


# --------------------------------------------------------------------------- #
# act: the landing turn (`afk turn`) and the landing (`afk land`) — ADR-0027   #
# --------------------------------------------------------------------------- #

TRUST = ("--set", "gate.trust_recorded_run=true")      # the default, spelled out
RERUN = ("--set", "gate.trust_recorded_run=false")     # the landing always gates itself


def _turn(n, *extra, now=T0, instance="me"):
    """The argv of one `afk turn` — the tick's half of a landing."""
    return ("turn", "--issue", str(n), "--instance", instance, "--worker-command", WORKER,
            *R, "--now", str(now), *extra)


def _land(w, n, wt, *extra, now=T0):
    """`afk land` as a worker runs it: in its own worktree, with no instance id."""
    return w.afk("land", "--issue", str(n), *R, "--now", str(now), *extra, cwd=wt)


def _land_error(w, n, wt, *extra):
    return w.error("land", "--issue", str(n), *R, *NOW, *extra, cwd=wt)


def _brief(wt):
    """The brief the worker in a worktree is working from."""
    with open(os.path.join(git(wt, "rev-parse", "--absolute-git-dir"), "afk-worker-prompt.md")) as f:
        return f.read()


def _gate(w, wt, command, *extra):
    """`afk gate` as a worker runs it: in its own worktree, on a given command."""
    return w.afk("gate", "--set", f"gate.local_command={command}", *extra, cwd=wt)


def _template():
    with open(os.path.join(SKILL, "references", "worker-prompt.md")) as f:
        return f.read()


def _landing_brief(w, n, started, pr_number, pr_branch, *cfg):
    """The landing brief `afk turn` must have written, rendered independently."""
    return afk_decide.render_landing(
        _template(), _prompt_fields(w, n, f"issue {n}", started, *cfg),
        {"pr": pr_number, "pr_branch": pr_branch, "target": w.sb.base})


def _mine(w, gate, n):
    [row] = [m for m in w.afk("rebuild", *ME, *R, *NOW, *gate)["mine"] if m["number"] == n]
    return row["status"], row["board_phase"], row["stopped"]


def _turns(w, pr_number):
    return [c for c in w.comments(pr_number) if "<!--afk:turn" in c]


def _merging(wt):
    return subprocess.run(["git", "rev-parse", "-q", "--verify", "MERGE_HEAD"], cwd=wt,
                          capture_output=True, env=ENV).returncode == 0


def _close_terminals(w):
    terms = w.terminals()
    for t in terms:
        t["open"] = False
    w.orca(terminals=terms)


def test_a_worker_lands_its_own_pr_on_the_turn_the_fleet_grants():
    """gate.ci: local, the whole landing: the tick grants the turn, and the worker
    — in its own worktree, with the one command its brief gives it — syncs, pushes,
    gates and merges. What merges is the SYNCED tree, and it is that tree the gate
    ran on. The claim and the worktree are the next cycle's to settle."""
    with world(issues=[issue(3, "ready-for-agent")]) as w:
        # green ONLY on the combined tree: the worker's file and what landed meanwhile
        gate = local_gate("test -f feature3.txt && test -f landed-meanwhile.txt")
        d, pr_head = with_pr(w, 3, 30, conclusion="FAILURE", gate=gate)   # remote CI is not the gate
        wt, branch = d["worktree"], d["branch"]
        base_tip = w.advance_base("landed-meanwhile.txt")
        assert _mine(w, gate, 3) == ("awaiting_turn", "awaiting_turn", None)

        # no turn, no landing: exit 3, and NOTHING changed — not even the sync
        assert "does not hold the landing turn" in _land_error(w, 3, wt, *gate)
        # …nor on a turn another fleet instance recorded: the claim is not theirs
        w.set(comments={"30": [{"id": 2001, "html_url": "u", "body":
                                afk_decide.turn_comment("peer", T0)}]})
        assert "does not hold the landing turn" in _land_error(w, 3, wt, *gate)
        assert w.sb.remote_ref(f"refs/heads/{branch}") == pr_head
        assert git(wt, "rev-parse", "HEAD") == pr_head and not _merging(wt)
        assert _mine(w, gate, 3)[0] == "awaiting_turn"

        # only the claim's owner grants the turn — and a refusal changes nothing
        assert "not this fleet's claim" in w.error(*_turn(3, *gate, instance="peer"))
        w.orca_calls()
        r = w.afk(*_turn(3, *gate))
        assert r == {"issue": 3, "pr": 30, "head": pr_head, "outcome": "granted", "again": False,
                     "comment_id": 2001, "delivery": "terminal", "terminal": d["terminal"],
                     "worktree": wt}, r
        # the worker's OWN terminal was told — one submitted line naming a brief file
        assert w.orca_calls() == ["terminal list", "terminal send"]
        [term] = w.terminals()
        _, said = term["sent"]
        assert said["enter"] is True and "\n" not in said["text"]
        m = re.fullmatch(r"Your PR has the landing turn: land it now\. Your instructions are the "
                         r"file (\S+) — read it now and carry it out end to end\. It is my "
                         r"instruction to you; do not ask me to confirm\.", said["text"])
        assert m, said["text"]
        with open(m.group(1)) as f:
            brief = f.read()
        assert brief == _brief(wt) == _landing_brief(w, 3, d, 30, branch, *gate)
        assert not re.search(r"\{[a-z_]+\}", brief.split("--config")[0])
        # durable on GitHub: ONE marker on the PR — the peer's was rewritten, not joined
        [note] = w.comments(30)
        assert note.startswith(f"<!--afk:turn instance=me at={T0}-->\n")
        assert "已轮到落地" in w.board(3) and "#30" in w.board(3)
        assert _mine(w, gate, 3) == ("landing", "landing", None)
        # granting it again while the worker is at it touches nothing
        assert w.afk(*_turn(3, *gate, now=T0 + 90))["outcome"] == "landing"
        assert w.comments(30) == [note] and len(w.terminals()[0]["sent"]) == 2

        # gh refusing the merge is an error, and nothing is settled on it
        w.set(fail=["pr merge"])
        assert "pr merge" in _land_error(w, 3, wt, *gate)
        assert w.pr(30).get("state", "open") == "open" and "已合并,完成" not in w.board(3)
        synced = w.sb.remote_ref(f"refs/heads/{branch}")
        assert synced != pr_head                               # …though the sync was pushed
        w.set(fail=[])

        # the brief names ONE command to land with, carrying the run's config — and
        # that line lands the PR as written
        [line] = [ln.strip() for ln in brief.splitlines() if " land --issue " in ln]
        assert line.startswith(f"{AFK} land --issue 3 --repo {REPO} --config ")
        p = subprocess.run(line, shell=True, cwd=wt, capture_output=True, text=True, env=w.env)
        r = json.loads(p.stdout)
        assert p.returncode == 0 and (r["outcome"], r["pr"], r["synced"], r["head"]) == \
            ("merged", 30, False, synced), r
        # the landing gh refused had gated this very head, green: that run is on
        # record, so this one does not gate it again (ADR-0026)
        assert r["gate"] == {"status": "green", "source": "recorded", "head": synced,
                             "command": "test -f feature3.txt && test -f landed-meanwhile.txt",
                             "recorded_at": r["gate"]["recorded_at"]}
        # gh was pinned to the gated head, with the configured strategy
        assert w.pr(30)["merged"] == {"strategy": "squash", "head": synced, "delete_branch": True}
        # what landed contains both sides, by MERGE (the base tip is an ancestor)
        assert w.sb.remote_ref(f"refs/heads/{w.sb.base}") == synced
        assert {"feature3.txt", "landed-meanwhile.txt"} <= w.remote_files(w.sb.base)
        git(w.cwd, "fetch", "-q", "origin", w.sb.base)
        assert git(w.cwd, "merge-base", base_tip, synced) == base_tip
        assert "已合并,完成" in w.board(3) and "#30" in w.board(3) and "`me`" in w.board(3)
        assert w.issue(3)["state"] == "closed" and not w.sb.remote_ref(f"refs/heads/{branch}")

        # the landing settles neither the claim nor the worktree it ran in: the next
        # cycle reads a claim whose issue is closed, and releasing it removes both
        assert w.claimed_by(3) == "me" and os.path.isdir(wt)
        ws = w.afk("rebuild", *ME, *R, *NOW, *gate)
        assert [(m["number"], m["status"]) for m in ws["mine"]] == [(3, "closed")]
        assert ws["merge_order"] == []
        w.orca_calls()
        r = w.afk("release", "3", *ME, *R, *gate)
        assert (r["released"], r["cleanup"]) == (True, {"removed": True, "path": wt}), r
        assert w.orca_calls() == ["worktree rm"]
        assert w.claimed_by(3) is None and w.worktrees() == [] and not os.path.isdir(wt)
        assert w.afk("rebuild", *ME, *R, *NOW, *gate)["mine"] == []


def test_release_removes_a_worktree_only_for_a_claim_whose_issue_is_closed():
    with world(issues=[issue(1, "ready-for-agent"), issue(2, "ready-for-agent")]) as w:
        d1, d2 = w.afk(*dispatch(1)), w.afk(*dispatch(2))
        # an open issue's worktree may hold work: releasing its claim leaves it alone
        r = w.afk("release", "1", *ME, *R)
        assert r["released"] is True and "cleanup" not in r and os.path.isdir(d1["worktree"])
        # closed, but the config keeps worktrees
        w.set(issues=[issue(1, "ready-for-agent"), issue(2, "ready-for-agent", state="closed")])
        r = w.afk("release", "2", *ME, *R, "--set", "worktree_cleanup=false")
        assert r["released"] is True and "cleanup" not in r and os.path.isdir(d2["worktree"])


def test_a_red_gate_on_the_turn_is_the_workers_to_fix_and_spends_nothing():
    with world(issues=[issue(4, "ready-for-agent")]) as w:
        d, pr_head = with_pr(w, 4, 40)
        wt, branch = d["worktree"], d["branch"]
        base_tip = w.advance_base("landed-meanwhile.txt")
        red = local_gate("test -f feature4.txt && test -f landed-meanwhile.txt && "
                         "echo 'FAIL TestNames' >&2 && test -f fixed.txt")
        assert w.afk(*_turn(4, *red))["outcome"] == "granted"

        # it runs IN the worktree, on the synced tree; stderr is part of the log
        r = _land(w, 4, wt, *red, now=T0 + 10)
        assert (r["outcome"], r["synced"], r["pr"]) == ("gate_red", True, 40), r
        assert (r["gate"]["status"], r["gate"]["exit_code"], r["gate"]["excerpt"]) == \
            ("red", 1, "FAIL TestNames")
        # the sync was pushed (the PR shows what was gated); the target was not touched
        assert r["head"] == w.sb.remote_ref(f"refs/heads/{branch}") != pr_head
        assert w.sb.remote_ref(f"refs/heads/{w.sb.base}") == base_tip
        # the failure is durable where a later tick — and a human — can re-read it
        [note] = w.state()["pr_comments"]["40"]
        assert "FAIL TestNames" in note and "exit 1" in note
        # no attempt spent, the PR open, the turn kept — and the stop is on the marker
        assert w.issue(4)["labels"] == ["ready-for-agent"] and w.claimed_by(4) == "me"
        assert w.pr(40).get("state", "open") == "open"
        [mark] = _turns(w, 40)
        assert mark.startswith(f"<!--afk:turn instance=me at={T0 + 10} stopped=gate_red "
                               f"head={r['head']}-->\n")
        assert _mine(w, red, 4) == ("landing", "landing", "gate_red")
        # the tick has nothing to add: the worker is fixing it
        assert w.afk(*_turn(4, *red, now=T0 + 90))["outcome"] == "landing"

        # a hung gate is RED, never green-by-default; the excerpt is a bounded tail
        r = _land(w, 4, wt, *local_gate("sleep 30"), "--gate-timeout", "1")
        assert (r["outcome"], r["gate"]["timed_out"], r["synced"]) == ("gate_red", True, False)
        assert "timed out after 1s" in r["gate"]["excerpt"]
        r = _land(w, 4, wt, *local_gate("seq 1 200; exit 1"), "--excerpt-lines", "3")
        assert (r["gate"]["excerpt"], r["gate"]["omitted_lines"]) == ("198\n199\n200", 197)
        assert len(w.state()["pr_comments"]["40"]) == 3 and len(_turns(w, 40)) == 1

        # the worker fixes the code, commits, and lands again — its commit is pushed
        fixed = w.work(wt, "fixed.txt", push=False)
        r = _land(w, 4, wt, *red, "--set", "merge.strategy=rebase",
                  "--set", "merge.delete_branch=false")
        assert (r["outcome"], r["synced"], r["head"]) == ("merged", True, fixed), r
        assert w.pr(40)["merged"] == {"strategy": "rebase", "head": fixed, "delete_branch": False}
        assert w.sb.remote_ref(f"refs/heads/{branch}") == fixed       # config decides what stays


def test_a_sync_conflict_on_the_turn_is_resolved_in_place_by_the_worker():
    with world(issues=[issue(2, "ready-for-agent"), issue(8, "ready-for-agent")]) as w:
        d, pr_head = with_pr(w, 2, 20, name="shared.txt", text="from the worker")
        base_tip = w.advance_base("shared.txt", text="from someone else")
        gate = local_gate("true")
        wt = d["worktree"]
        w.afk(*_turn(2, *gate))

        r = _land(w, 2, wt, *gate)
        assert (r["outcome"], r["files"]) == ("conflict", ["shared.txt"]), r
        assert os.path.samefile(r["worktree"], wt)
        assert "head" not in r and "gate" not in r               # nothing was gated or pushed
        assert w.sb.remote_ref(f"refs/heads/{d['branch']}") == pr_head
        assert w.sb.remote_ref(f"refs/heads/{w.sb.base}") == base_tip
        # the merge is left IN PROGRESS, for the worker whose worktree this is
        assert _merging(wt) and git(wt, "rev-parse", "MERGE_HEAD") == base_tip
        assert _mine(w, gate, 2) == ("landing", "landing", "conflict")
        assert w.issue(2)["labels"] == ["ready-for-agent"]       # a conflict is not a failure
        # re-entrant: a re-run reports the same conflict rather than piling on a merge
        assert _land(w, 2, wt, *gate)["files"] == ["shared.txt"]

        # resolved but not committed: what would be gated is not what would land
        with open(os.path.join(wt, "shared.txt"), "w") as f:
            f.write("resolved: both\n")
        git(wt, "add", "shared.txt")
        assert "uncommitted" in _land_error(w, 2, wt, *gate)
        git(wt, "commit", "-qm", "merge main: keep both")

        r = _land(w, 2, wt, *gate)
        assert (r["outcome"], r["synced"]) == ("merged", True), r
        p = subprocess.run(["git", "--git-dir", w.sb.bare, "show", f"{w.sb.base}:shared.txt"],
                           capture_output=True, text=True, env=ENV)
        assert p.stdout == "resolved: both\n"

        # a claim with no PR has nothing to land, for the tick or for the worker
        w.afk("claim", "8", *ME, *NOW, *R)
        assert "nothing to land" in w.error(*_turn(8, *gate))
        assert "nothing to land" in _land_error(w, 8, wt, *gate)


def test_turns_are_granted_one_at_a_time_in_merge_order():
    """Three ready PRs: exactly one holds the turn at a time, granted in
    `merge_order`; the next is granted the cycle after the first lands or is
    failed. Mutually conflicting PRs therefore resolve once each — none is synced
    against a tip that is about to move."""
    gate = local_gate("true")
    with world(issues=[issue(n, "ready-for-agent") for n in (1, 2, 3, 4)]) as w:
        d = {}
        d[2], _ = with_pr(w, 2, 20, name="shared.txt", text="from #2")     # dispatched first…
        d[1], _ = with_pr(w, 1, 10, name="shared.txt", text="from #1")     # …lower PR number
        d[3], head3 = with_pr(w, 3, 30, name="shared.txt", text="from #3")
        with_pr(w, 4, 40, instance="peer")                                  # a peer's, not mine

        def rows():
            ws = w.afk("rebuild", *ME, *R, *NOW, *gate)
            return {m["number"]: m["status"] for m in ws["mine"]}, ws["merge_order"]

        assert rows() == ({1: "awaiting_turn", 2: "awaiting_turn", 3: "awaiting_turn"}, [1, 2, 3])
        # waiting for the turn is visible on the issue
        w.afk("status", "3", "--phase", "awaiting_turn", "--pr", "30", *ME, *R, *gate)
        assert "排队等落地轮次" in w.board(3) and "#30" in w.board(3)

        assert w.afk(*_turn(1, *gate))["outcome"] == "granted"
        w.orca_calls()
        r = w.afk(*_turn(2, *gate))
        assert (r["outcome"], r["holder"], r["pr"]) == ("waiting", 1, 20), r
        assert w.afk(*_turn(3, *gate))["outcome"] == "waiting"
        # a waiting PR is untouched: no marker, no word to its worker, not synced
        assert w.orca_calls() == [] and _turns(w, 20) == [] and _turns(w, 30) == []
        assert len(w.terminals()[2]["sent"]) == 1
        assert git(d[3]["worktree"], "rev-parse", "HEAD") == head3
        # the PR that holds the turn leads the order; waiting holds its slot
        state, order = rows()
        assert (state, order) == ({1: "landing", 2: "awaiting_turn", 3: "awaiting_turn"}, [1, 2, 3])
        assert w.afk("rebuild", *ME, *R, *NOW, *gate)["free_slots"] == 0
        # turns are per fleet instance: a peer's is its own to grant
        assert w.afk(*_turn(4, *gate, instance="peer"))["outcome"] == "granted"
        assert w.afk("rebuild", "--instance", "peer", *R, *NOW, *gate)["merge_order"] == [4]

        # #1 lands: the next cycle gives #2 its turn, synced against a target that
        # already holds #1 — ONE resolution
        assert _land(w, 1, d[1]["worktree"], *gate)["outcome"] == "merged"
        state, order = rows()
        assert (state, order) == ({1: "closed", 2: "awaiting_turn", 3: "awaiting_turn"}, [2, 3])
        assert w.afk(*_turn(2, *gate))["outcome"] == "granted"
        assert _land(w, 2, d[2]["worktree"], *gate)["outcome"] == "conflict"
        assert w.afk(*_turn(3, *gate))["outcome"] == "waiting"

        # #2 is FAILED instead (its worker never resolved it): its PR closes, and
        # that frees the turn — #3 spent no attempt of its own waiting
        assert w.afk(*_fail(2, "landing turn never landed", *gate))["action"] == "retry"
        assert w.pr(20)["state"] == "closed"
        state, order = rows()
        assert (state[2], state[3], order) == ("no_pr", "awaiting_turn", [3])
        assert w.afk(*_turn(3, *gate))["outcome"] == "granted"
        assert w.issue(3)["labels"] == ["ready-for-agent"]
        assert _land(w, 3, d[3]["worktree"], *gate)["outcome"] == "conflict"


def test_in_required_mode_the_turn_waits_for_checks_on_the_head_that_lands():
    with world(issues=[issue(n, "ready-for-agent") for n in range(1, 6)]) as w:
        heads, d = {}, {}
        for n, conclusion in ((1, "SUCCESS"), (2, "FAILURE"), (3, "PENDING"), (4, None), (5, "SUCCESS")):
            d[n], heads[n] = with_pr(w, n, n * 10, conclusion=conclusion)
        base0 = w.sb.remote_ref(f"refs/heads/{w.sb.base}")

        def branch_tip(n):
            return w.sb.remote_ref("refs/heads/" + w.pr(n * 10)["headRefName"])

        def land(n, *extra):
            return _land(w, n, d[n]["worktree"], *extra)

        # the tick's judgments come BEFORE the turn: red → fail it, pending → leave
        # it, none → the tick's call. Nothing is granted, nobody is told
        for n, outcome, checks in ((2, "gate_red", "red"), (3, "awaiting_ci", "pending"),
                                   (4, "no_checks", None)):
            r = w.afk(*_turn(n))
            assert (r["outcome"], r["checks"], r["head"]) == (outcome, checks, heads[n]), (n, r)
            assert _turns(w, n * 10) == [] and len(w.terminals()[n - 1]["sent"]) == 1
        # (a PR with no checks is in the order: its turn is where the tick is asked)
        assert w.afk("rebuild", *ME, *R, *NOW)["merge_order"] == [1, 4, 5]

        # no checks at all: the turn is granted only on the tick's explicit judgment,
        # which travels with the turn to the landing
        assert w.afk(*_turn(4, "--allow-no-checks"))["outcome"] == "granted"
        assert _turns(w, 40)[0].startswith(f"<!--afk:turn instance=me at={T0} allow_no_checks=1-->")
        r = land(4)
        assert (r["outcome"], r["head"], r["synced"]) == ("merged", heads[4], False), r
        assert w.pr(40)["merged"]["head"] == heads[4] and "gate" not in r
        assert not any(w.state()["pr_comments"].values())        # required mode publishes nothing

        # the base moved (PR 40 landed): PR 10's green checks ran on a tree that will
        # NOT land. The sync is pushed and CI must speak about the new head first.
        assert w.afk(*_turn(1))["outcome"] == "granted"
        r = land(1)
        assert (r["outcome"], r["checks"], r["synced"]) == ("awaiting_ci", "green", True), r
        assert r["head"] == branch_tip(1) != heads[1]
        # the turn is KEPT while CI runs: nobody else is granted one meanwhile
        assert w.claimed_by(1) == "me" and w.pr(10).get("state", "open") == "open"
        row = next(m for m in w.afk("rebuild", *ME, *R, *NOW)["mine"] if m["number"] == 1)
        assert (row["status"], row["stopped"]) == ("landing", "awaiting_ci")
        assert w.afk(*_turn(5))["outcome"] == "waiting"
        # a worker that stopped for the tick is not silent: it is not nudged or failed
        t0 = int(time.time()) + 5000
        np = w.no_pr("--issue", "1", *R, "--now", str(t0))
        assert (np["outcome"], np["action"]) == ("coding", "leave"), np

        # CI is green on the synced head (the fake's rollup still says SUCCESS): the
        # tick tells the worker to land again — the same turn, the same marker
        w.orca_calls()
        r = w.afk(*_turn(1, now=T0 + 90))
        assert (r["outcome"], r["again"], r["delivery"]) == ("granted", True, "terminal"), r
        assert w.orca_calls() == ["terminal list", "terminal send"]
        [mark] = _turns(w, 10)
        assert mark.startswith(f"<!--afk:turn instance=me at={T0 + 90}-->")
        r = land(1)
        assert (r["outcome"], r["synced"], r["head"]) == ("merged", False, branch_tip(1) or r["head"])
        assert {"feature1.txt", "feature4.txt"} <= w.remote_files(w.sb.base)

        # checks that went red or pending on a head the worker pushed stop the landing
        w.afk(*_turn(5))
        prs = w.state()["prs"]
        for p in prs:
            if p["number"] == 50:
                p["statusCheckRollup"] = pr(50, 5, "FAILURE")["statusCheckRollup"]
        w.set(prs=prs)
        # …a RED run is stale once the sync moved the head: it is not this head's verdict
        assert land(5)["outcome"] == "awaiting_ci"
        r = land(5)
        assert (r["outcome"], r["checks"], r["synced"]) == ("gate_red", "red", False), r
        # sync_before_merge: false → the PR head lands as it is, on its own checks
        w.set(prs=[{**p, "statusCheckRollup": pr(50, 5)["statusCheckRollup"]}
                   if p["number"] == 50 else p for p in w.state()["prs"]])
        r = land(5, "--set", "merge.sync_before_merge=false")
        assert (r["outcome"], r["synced"]) == ("merged", False), r
        assert base0 != w.sb.remote_ref(f"refs/heads/{w.sb.base}")


def test_the_adversarial_verify_is_settled_before_the_turn_and_pinned_to_the_head():
    """A worker never verifies itself: the tick's verifier speaks about a head
    BEFORE the turn is granted, and a landing whose sync moved that head stops
    and waits for it to speak again."""
    on = (*local_gate("true"), "--set", "gate.adversarial_verify=true")
    with world(issues=[issue(6, "ready-for-agent")]) as w:
        d, pr_head = with_pr(w, 6, 60)
        wt = d["worktree"]
        r = w.afk(*_turn(6, *on))
        assert (r["outcome"], r["head"]) == ("needs_verify", pr_head) and _turns(w, 60) == []
        assert w.afk(*_turn(6, *on, "--verified", "0" * 40))["outcome"] == "needs_verify"
        assert w.afk(*_turn(6, *on, "--verified", pr_head))["outcome"] == "granted"
        assert _turns(w, 60)[0].startswith(f"<!--afk:turn instance=me at={T0} verified={pr_head}-->")

        # the target moved: what would land is not what was verified
        w.advance_base("landed-meanwhile.txt")
        r = _land(w, 6, wt, *on)
        assert (r["outcome"], r["synced"], r["gate"]["status"]) == ("needs_verify", True, "green"), r
        synced = r["head"]
        assert synced != pr_head and w.pr(60).get("state", "open") == "open"
        assert _mine(w, on, 6) == ("landing", "landing", "needs_verify")
        # the tick verifies the new head and tells the worker to land again; a
        # re-grant that names no head, or the old one, is refused with the new one
        r = w.afk(*_turn(6, *on, now=T0 + 90))
        assert (r["outcome"], r["head"]) == ("needs_verify", synced)
        r = w.afk(*_turn(6, *on, "--verified", synced, now=T0 + 90))
        assert (r["outcome"], r["again"]) == ("granted", True)
        r = _land(w, 6, wt, *on)
        assert (r["outcome"], r["head"], r["synced"]) == ("merged", synced, False), r


# --------------------------------------------------------------------------- #
# the worker's own gate run, and the landing that trusts it (ADR-0026)         #
# --------------------------------------------------------------------------- #

def test_a_recorded_worker_gate_run_is_not_repeated_by_the_landing():
    """#36. The worker gates through the line its brief gives it, the target does
    not move, and `afk land` lands the PR on that record: the gate command ran
    ONCE across the PR and its landing — not twice on the same commit."""
    with world(issues=[issue(3, "ready-for-agent"), issue(4, "ready-for-agent")]) as w:
        runs = os.path.join(w.sb.root, "gate-runs")
        command = f"echo gate-log; echo run >> {runs}"
        gate = local_gate(command)

        def count():
            with open(runs) as f:
                return len(f.read().split())

        d, head = with_pr(w, 3, 30, gate=gate)
        wt = d["worktree"]
        # the brief hands the worker ONE line to gate with — the tool, carrying the
        # configured command — and that line runs as written
        brief = _brief(wt)
        [line] = [ln.strip() for ln in brief.splitlines() if " gate --config " in ln]
        assert line == afk_decide.gate_command(AFK, command) and line.startswith(AFK)
        p = subprocess.run(line, shell=True, cwd=wt, capture_output=True, text=True, env=w.env)
        ran = json.loads(p.stdout)                               # stdout is the JSON alone…
        assert p.returncode == 0 and "gate-log" in p.stderr      # …the log went to the terminal
        assert ran == {"status": "green", "exit_code": 0, "timed_out": False, "command": command,
                       "head": head, "recorded": True, "detail": ran["detail"]}, ran
        assert count() == 1 and git(wt, "status", "--porcelain") == ""

        # the record survives the landing brief replacing the worker's first one
        w.afk(*_turn(3, *gate, *TRUST))
        r = _land(w, 3, wt, *gate, *TRUST)
        assert (r["outcome"], r["synced"], r["head"]) == ("merged", False, head), r
        # the outcome says the gate was trusted, not run, and names the recorded head
        assert r["gate"] == {"status": "green", "source": "recorded", "head": head,
                             "command": command, "recorded_at": r["gate"]["recorded_at"]}
        assert count() == 1 and w.pr(30)["merged"]["head"] == head

        # turned off: the same record, and the landing gates for itself anyway
        d, _ = with_pr(w, 4, 40, gate=gate)
        git(d["worktree"], "pull", "-q", "--no-edit", "origin", w.sb.base)   # the worker's own sync
        head = git(d["worktree"], "rev-parse", "HEAD")
        git(d["worktree"], "push", "-q", "origin", "HEAD")
        assert _gate(w, d["worktree"], command)["recorded"] is True and count() == 2
        w.afk(*_turn(4, *gate, *RERUN))
        r = _land(w, 4, d["worktree"], *gate, *RERUN)
        assert (r["outcome"], r["gate"]) == \
            ("merged", {"status": "green", "source": "run", "head": head, "command": command})
        assert count() == 3


def test_a_recorded_gate_run_is_void_unless_it_is_of_the_head_that_lands():
    """With `gate.trust_recorded_run` on, the landing still runs the gate whenever
    the record does not prove THIS command passed on THIS commit: no record, a red
    or timed-out run, a dirty tree, another command, a later commit, a sync that
    moved the head. (The turn here was granted on a verify of some older head, so
    `needs_verify` holds the PR open between probes: the verify check still comes
    after the machine gate, pinned to the head.)"""
    with world(issues=[issue(7, "ready-for-agent")]) as w:
        runs = os.path.join(w.sb.root, "gate-runs")
        command = f"echo run >> {runs}; test ! -f broken.txt"
        on = (*local_gate(command), *TRUST, "--set", "gate.adversarial_verify=true")
        d, head = with_pr(w, 7, 70)
        wt = d["worktree"]
        w.set(comments={"70": [{"id": 2001, "html_url": "u", "body":
                                afk_decide.turn_comment("me", T0, verified="0" * 40)}]})

        def count():
            if not os.path.exists(runs):
                return 0
            with open(runs) as f:
                return len(f.read().split())

        def probe(why, *extra, outcome="needs_verify"):
            """One `afk land`: it must RUN the gate, and say why the record was void."""
            before = count()
            r = _land(w, 7, wt, *on, *extra)
            assert (r["outcome"], r["gate"]["source"]) == (outcome, "run"), r
            assert why in r["gate"]["not_trusted"], r["gate"]
            assert count() == before + 1
            return r

        # no record: the worker typed the bare command, or never gated
        probe("no green run")
        # …and a landing's own green run IS a record: the next one need not repeat it
        before = count()
        r = _land(w, 7, wt, *on)
        assert (r["outcome"], r["gate"]["source"], count()) == ("needs_verify", "recorded", before)

        # a red run leaves nothing — not even the green record that was there before it
        w.work(wt, "broken.txt")
        g = _gate(w, wt, command)
        assert (g["status"], g["exit_code"], g["recorded"]) == ("red", 1, False), g
        probe("no green run", outcome="gate_red")
        git(wt, "rm", "-q", "broken.txt")
        git(wt, "commit", "-qm", "fix")
        git(wt, "push", "-q", "origin", "HEAD")
        # …nor does one that timed out
        assert _gate(w, wt, command)["recorded"] is True
        g = _gate(w, wt, "sleep 30", "--gate-timeout", "1")
        assert (g["status"], g["timed_out"], g["recorded"]) == ("red", True, False), g
        probe("no green run")

        # green over an untracked file: it tested a tree no commit holds
        with open(os.path.join(wt, "not-added.txt"), "w") as f:
            f.write("the test only passes with this\n")
        g = _gate(w, wt, command)
        assert (g["status"], g["recorded"], g["uncommitted"]) == ("green", False, ["?? not-added.txt"])
        probe("uncommitted or untracked")
        os.remove(os.path.join(wt, "not-added.txt"))

        # green, clean — of ANOTHER command than the one configured now
        assert _gate(w, wt, "true")["recorded"] is True
        probe("different command")

        # the worker committed after the recorded run
        g = _gate(w, wt, command)
        w.work(wt, "afterthought.txt")
        assert g["head"] in probe("a later commit moved it")["gate"]["not_trusted"]

        # the landing's sync moved the head: what lands is not what the worker gated
        g = _gate(w, wt, command)
        w.advance_base("landed-meanwhile.txt")
        r = probe("not on the head that would land")
        assert r["synced"] is True and r["head"] != g["head"]

        # a record of exactly the head that lands: trusted — and still verified first
        g = _gate(w, wt, command)
        before = count()
        r = _land(w, 7, wt, *on)
        assert (r["outcome"], r["gate"]["source"], r["head"]) == ("needs_verify", "recorded", g["head"])
        assert w.afk(*_turn(7, *on, "--verified", r["head"]))["again"] is True
        r = _land(w, 7, wt, *on)
        assert (r["outcome"], r["gate"]["source"], r["gate"]["head"]) == ("merged", "recorded", g["head"])
        assert count() == before and w.pr(70)["merged"]["head"] == g["head"]


# --------------------------------------------------------------------------- #
# a turn nobody lands, and a turn whose worker is gone                         #
# --------------------------------------------------------------------------- #

def test_a_worker_silent_on_its_turn_is_nudged_once_then_failed_and_the_turn_moves_on():
    """A turn can never park the queue: a worker that goes silent on it — idle past
    grace, nothing landed — is nudged once and then failed, like any other
    silence. Only THEN is an attempt spent; its PR closes, and the next PR gets
    the turn."""
    gate = local_gate("true")
    with world(issues=[issue(5, "ready-for-agent"), issue(6, "ready-for-agent")]) as w:
        d, _ = with_pr(w, 5, 50)
        with_pr(w, 6, 60)
        t0 = int(time.time()) + 5000
        cfg = ("--config", json.dumps({"base_branch": w.sb.base, "worker_idle_grace_seconds": 300}))

        def no_pr(at):
            r = w.no_pr("--issue", "5", *R, *cfg, "--now", str(at))
            return r["outcome"], r["action"], r["turn_at"]

        # a worker nudged BEFORE its turn is under a new instruction now: nudgeable again
        w.afk("nudge", "--issue", "5", *ME, *R, *cfg, "--now", str(t0 - 9000))
        assert w.afk(*_turn(5, *gate, now=t0))["outcome"] == "granted"
        # the turn is a sign of life: one grace period to start on it
        assert no_pr(t0 + 299) == ("coding", "leave", t0)
        assert no_pr(t0 + 300) == ("idle_stalled", "nudge", t0)
        w.afk("nudge", "--issue", "5", *ME, *R, *cfg, "--now", str(t0 + 300))
        # the nudge points at the brief the turn wrote, not the original task
        said = w.terminals()[0]["sent"][-1]["text"]
        with open(re.search(r"\((\S+)\)", said).group(1)) as f:
            assert " land --issue 5 " in f.read()
        assert no_pr(t0 + 599)[:2] == ("coding", "leave")
        assert no_pr(t0 + 600)[:2] == ("idle_failed", "next_attempt")
        assert _mine(w, gate, 5)[0] == "landing" and w.issue(5)["labels"] == ["ready-for-agent"]
        assert w.afk(*_turn(6, *gate))["outcome"] == "waiting"

        # a worker still AT its landing is busy, and is left alone however long ago
        # the turn was (busy is settled from orca alone, so the turn was not read)
        w.worker(output=t0 + 900, state="working", since=t0 + 800, n=0)
        np = w.no_pr("--issue", "5", *R, *cfg, "--now", str(t0 + 910))
        assert (np["outcome"], np["worker_state"], np["turn_at"]) == ("coding", "working", None)
        w.worker(output=t0, state="done", since=t0, n=0)

        r = w.afk(*_fail(5, "given the landing turn and never landed", *gate))
        assert (r["action"], r["attempt"]) == ("retry", 1)
        assert w.issue(5)["labels"] == ["ready-for-agent", "afk-attempt/1"]
        assert w.pr(50)["state"] == "closed"
        # the retry is a new attempt with no PR: the old turn went with the PR
        assert _mine(w, gate, 5) == ("no_pr", "claimed", None)
        assert w.afk(*_turn(6, *gate))["outcome"] == "granted"


def test_a_turn_with_no_terminal_is_delivered_by_continuation_never_from_base():
    """The worker finished and its terminal is gone (closed, the machine restarted,
    the claim taken over from another machine). The turn then STARTS a worker by
    continuation — in the worktree still here, else one recreated at the PR head —
    briefed only to land the PR. There is no launcher-side merge to fall back on."""
    gate = local_gate("true")
    with world(issues=[issue(6, "ready-for-agent"), issue(7, "ready-for-agent")]) as w:
        d, pr_head = with_pr(w, 6, 60, gate=gate)
        wt, branch = d["worktree"], d["branch"]
        w.advance_base("landed-meanwhile.txt")
        _close_terminals(w)
        w.orca_calls()

        r = w.afk(*_turn(6, *gate))
        assert (r["outcome"], r["delivery"], r["worktree"]) == ("granted", "continuation", wt), r
        assert w.orca_calls() == ["terminal list", "terminal close", "terminal create",
                                  "terminal wait", "terminal send"]
        _, new = w.terminals()
        assert (new["handle"], new["command"], new["worktreePath"]) == (r["terminal"], WORKER, wt)
        told = _told(new)
        assert told == _landing_brief(w, 6, d, 60, branch, *gate)
        # briefed ONLY to land: not the task, not a new PR
        assert told.startswith("## Your PR holds the landing turn") and "Closes #6" not in told
        # in the existing worktree, at the PR head — nothing reset to base, nothing synced
        assert git(wt, "rev-parse", "HEAD") == pr_head and git(wt, "status", "--porcelain") == ""
        assert len(w.worktrees()) == 1 and "已轮到落地" in w.board(6)
        assert w.issue(6)["labels"] == ["ready-for-agent"] and _mine(w, gate, 6)[0] == "landing"

        # the continued worker dies too: the claim reads as dead, and the plain
        # orphan recovery — `afk dispatch` — starts its successor ON the turn
        _close_terminals(w)
        np = w.no_pr("--issue", "6", *R, *gate)
        assert (np["outcome"], np["action"]) == ("dead", "orphan")
        r = w.afk(*dispatch(6, *gate, "--now", str(T0 + 90)))
        assert (r["claim"], r["action"], r["prompt"], r["landing"]) == \
            ("held", "reuse_worktree", "landing", 60)
        assert _told(w.terminals()[-1]) == told and len(w.comments(60)) == 1
        assert _land(w, 6, wt, *gate)["outcome"] == "merged"
        w.afk("release", "6", *ME, *R, *gate)

        # no worktree on this machine at all (a takeover from another machine): one is
        # recreated at the PR's HEAD — the brief carries orca's new local branch, the
        # landing pushes to the PR's branch
        d7, head7 = with_pr(w, 7, 70, gate=gate)
        base_tip = w.advance_base("landed-later.txt")
        git(w.cwd, "worktree", "remove", "--force", d7["worktree"])
        w.orca([row for row in w.worktrees() if row["linkedIssue"] != 7])
        r = w.afk(*_turn(7, *gate))
        assert (r["outcome"], r["delivery"]) == ("granted", "continuation")
        wt7 = r["worktree"]
        assert wt7 != d7["worktree"] and git(wt7, "rev-parse", "HEAD") == head7
        told = _told(w.terminals()[-1])
        assert f"**Your branch:** `{d7['branch']}-2`" in told and f"`{d7['branch']}`" in told
        r = _land(w, 7, wt7, *gate, "--set", "merge.delete_branch=false")
        assert (r["outcome"], r["synced"]) == ("merged", True), r
        assert w.sb.remote_ref(f"refs/heads/{d7['branch']}") == r["head"] != head7
        assert {"feature7.txt", "landed-later.txt"} <= w.remote_files(w.sb.base)
        git(w.cwd, "fetch", "-q", "origin", w.sb.base)
        assert git(w.cwd, "merge-base", base_tip, r["head"]) == base_tip

    # a delivery that fails AFTER the record is repaired by the paths that exist: the
    # claim is already `landing`, and the orphan's continuation is started on the turn
    with world(issues=[issue(8, "ready-for-agent")]) as w:
        with_pr(w, 8, 80)
        _close_terminals(w)
        w.orca(never_ready=True)
        assert "not ready" in w.error(*_turn(8, *gate, "--ready-timeout", "1"))
        assert _mine(w, gate, 8)[0] == "landing" and len(w.comments(80)) == 1
        assert w.issue(8)["labels"] == ["ready-for-agent"] and w.claimed_by(8) == "me"
        w.orca(never_ready=False)
        r = w.afk(*dispatch(8, *gate, "--now", str(T0 + 90)))
        assert (r["action"], r["prompt"], r["landing"]) == ("reuse_worktree", "landing", 80)


# --------------------------------------------------------------------------- #
# act: fail / escalate / close                                                 #
# --------------------------------------------------------------------------- #

def _fail(n, reason, *extra):
    return ("fail", "--issue", str(n), *ME, "--worker-command", WORKER, *R, *NOW,
            "--reason", reason, *extra)


def test_fail_retries_from_a_clean_base_then_escalates_when_exhausted():
    """The retry ladder as one transition. `afk fail` is the ONE writer of the
    attempt label — it used to exist only as a sentence — and a retry discards the
    failed attempt, so the claim does not loop on the same red PR."""
    with world(issues=[issue(5, "ready-for-agent", "bug")]) as w:
        first, _ = with_pr(w, 5, 50, conclusion="FAILURE")
        row = w.afk("rebuild", *ME, *R, *NOW)["mine"][0]
        assert (row["status"], row["attempt"], row["pr"]) == ("failure", 0, 50)
        assert "afk-attempt/1" not in w.state()["labels"]         # the label does not exist yet

        r = w.afk(*_fail(5, "CI red: TestNames fails in names_test.go"))
        assert (r["action"], r["attempt"], r["retry_max"]) == ("retry", 1, 2), r
        r = r["worker"]                                          # where the new worker is
        assert (r["tier"], r["action"], r["prompt"]) == (3, "dispatch_fresh", "fresh")
        # the attempt is counted on the issue, where every fleet and human can read it
        assert w.issue(5)["labels"] == ["ready-for-agent", "bug", "afk-attempt/1"]
        # the failed attempt is gone: PR closed, branch deleted, worktree removed
        assert w.pr(50)["state"] == "closed" and "superseded" in w.state()["pr_comments"]["50"][0]
        assert not w.sb.remote_ref(f"refs/heads/{first['branch']}")
        assert not os.path.isdir(first["worktree"])
        assert r["discarded"]["closed_prs"] == [50]
        # a new worker, from the base tip, under the SAME claim, told why it is here
        assert git(r["worktree"], "rev-parse", "HEAD") == w.sb.remote_ref(f"refs/heads/{w.sb.base}")
        assert w.claimed_by(5) == "me"
        told = _told(w.terminals()[-1])
        assert told == _prompt(w, "fresh", 5, "issue 5", r,
                               reason="CI red: TestNames fails in names_test.go")
        assert "## Why the previous attempt failed" in told
        # …and the next rebuild sees a worker coding, not the same failure again
        row = w.afk("rebuild", *ME, *R, *NOW)["mine"][0]
        assert (row["status"], row["attempt"], row["pr"]) == ("no_pr", 1, None)

        # second failure: the label is SWAPPED, never stacked
        w.work(r["worktree"], "attempt1.txt")
        w.open_pr(51, closes=5, branch=r["branch"], conclusion="FAILURE")
        r2 = w.afk(*_fail(5, "still red"))
        assert (r2["action"], r2["attempt"]) == ("retry", 2)
        r2 = r2["worker"]
        assert w.issue(5)["labels"] == ["ready-for-agent", "bug", "afk-attempt/2"]

        # third: exhausted → escalated, with no worker started and the evidence kept
        w.work(r2["worktree"], "attempt2.txt")
        w.open_pr(52, closes=5, branch=r2["branch"], conclusion="FAILURE")
        w.orca_calls()
        r3 = w.afk(*_fail(5, "third time red: needs a human"))
        assert {k: r3[k] for k in ("action", "attempt", "pr", "labels", "released")} == \
            {"action": "escalate", "attempt": 2, "pr": 52, "released": True,
             "labels": {"added": ["ready-for-human"], "removed": ["afk-attempt/2", "ready-for-agent"]}}
        assert w.issue(5)["labels"] == ["bug", "ready-for-human"]
        assert w.claimed_by(5) is None and w.orca_calls() == []
        assert w.pr(52).get("state", "open") == "open" and os.path.isdir(r2["worktree"])
        assert "已升级给人处理" in w.board(5)
        note = w.comments(5)[-1]
        assert "third time red: needs a human" in note and "#52" in note and "after 2 retries" in note
        ws = w.afk("rebuild", *ME, *R, *NOW)
        assert ws["mine"] == [] and ws["frontier"]["dispatch"] == []

    # retry: 0 → the first failure escalates; and a failure is only mine to declare
    with world(issues=[issue(6, "ready-for-agent")]) as w:
        w.afk("claim", "6", "--instance", "peer", *NOW, *R)
        assert "not this fleet's claim" in w.error(*_fail(6, "x"))
        w.afk("release", "6", "--instance", "peer", *R)
        w.afk("claim", "6", *ME, *NOW, *R)
        r = w.afk(*_fail(6, "gave up", "--set", "retry=0"))
        assert (r["action"], r["attempt"], r["pr"]) == ("escalate", 0, None)
        assert w.orca_calls() == []


def test_escalate_relabels_before_it_releases():
    """Released first, a PR-less issue still carrying the ready label is back on
    the frontier — a peer dispatches the issue a human was just handed."""
    issues = [issue(8, "ready-for-agent", "afk-attempt/1"), issue(9, "ready-for-agent"), issue(10)]
    with world(issues=issues) as w:
        for n in (8, 9):
            w.afk("claim", str(n), *ME, *NOW, *R)

        def escalate(n, *extra, instance="me"):
            return ("escalate", "--issue", str(n), "--instance", instance, *R, *NOW,
                    "--reason", "blocked by #41, which is still open", *extra)

        assert "held by 'me'" in w.error(*escalate(8, instance="peer"))
        assert "not claimed at all" in w.error(*escalate(10))

        # the relabel FAILS → the claim must still be held: nothing got back on the frontier
        w.set(fail=["issue edit"])
        assert "issue edit" in w.error(*escalate(8))
        assert w.claimed_by(8) == "me" and "ready-for-agent" in w.issue(8)["labels"]
        assert w.afk("rebuild", "--instance", "peer", *R, *NOW)["frontier"]["dispatch"] == []

        w.set(fail=[])
        w.calls()
        r = w.afk(*escalate(8))
        assert (r["action"], r["attempt"], r["pr"], r["released"]) == ("escalate", 1, None, True)
        assert w.issue(8)["labels"] == ["ready-for-human"] and w.claimed_by(8) is None
        # one order: status board → labels → comment (the release is the last thing)
        writes = [" ".join(c[:2]) if c[0] != "api" else "comment " + c[c.index("--method") + 1]
                  for c in w.calls() if c[0] != "api" or "--method" in c]
        assert writes == ["pr list", "label create", "issue edit", "comment POST"], writes
        assert "已升级给人处理" in w.board(8)                       # written by the failed run
        assert w.comments(8)[-1].endswith("blocked by #41, which is still open")
        assert "after 1 retry)" in w.comments(8)[-1]
        # a peer's next rebuild does not see it as dispatchable
        assert w.afk("rebuild", "--instance", "peer", *R, *NOW)["frontier"]["dispatch"] == \
            [{"number": 9, "title": "issue 9"}][:0]

        # both human-facing writes are config
        r = w.afk(*escalate(9, "--set", "escalate_comment=false", "--set", "progress_comment=false",
                            "--set", "escalate_label=needs-human"))
        assert r["comment_id"] is None and w.comments(9) == []
        assert w.issue(9)["labels"] == ["needs-human"] and "needs-human" in w.state()["labels"]


def _park(n, *extra, instance="me"):
    return ("park", "--issue", str(n), "--instance", instance, *R, *NOW, *extra)


def test_a_worker_blocked_on_workable_backlog_is_parked_until_the_blocker_closes():
    """A `blocked` verdict names a dependency the backlog never declared. When the
    blocker is ordinary, workable backlog — most visibly one this same fleet is
    already working — handing the issue to a human is the wrong outcome: the edge
    is recorded on GitHub and the frontier contract does the waiting (ADR-0022)."""
    issues = [issue(135, "ready-for-agent"), issue(136, "ready-for-agent", "afk-attempt/1"),
              issue(137), issue(138), issue(139, "ready-for-agent"),
              issue(140, "ready-for-agent", "epic"), issue(141, "ready-for-agent")]
    with world(issues=issues) as w:
        cfg = ("--config", json.dumps({"base_branch": w.sb.base, "worker_idle_grace_seconds": 300}))
        w.afk(*dispatch(135))
        d136, d141 = w.afk(*dispatch(136)), w.afk(*dispatch(141))
        w.afk("claim", "137", "--instance", "peer", *NOW, *R)
        later = str(int(time.time()) + 5000)
        marks = iter(range(1, 100))

        def stops_blocked(n, by):
            """Issue n's worker stops, declaring itself blocked by `by`."""
            comments = w.state()["comments"]
            extra = f" blocked_by={by}" if by else ""
            comments.setdefault(str(n), []).append(
                _comment(next(marks), f"<!--afk:verdict n={n} phase=blocked{extra}-->"))
            w.set(comments=comments)
            r = w.no_pr("--issue", str(n), *R, *cfg, "--now", later)
            assert r["outcome"] == "idle_blocked", r
            return r["action"], r["pending_blockers"], [b["standing"] for b in r["blockers"]]

        # --- what nothing will resolve is still a human's -------------------------
        # no blocker named; unclaimed with no ready label; an epic; a dependency cycle
        assert stops_blocked(136, "") == ("escalate", [], [])
        assert "names no blocker — escalate it instead" in w.error(*_park(136))
        assert stops_blocked(136, "138") == ("escalate", [138], ["unmet"])
        assert stops_blocked(136, "140") == ("escalate", [140], ["unmet"])
        w.set(deps={"139": [136]})                                   # #139 waits on #136…
        assert stops_blocked(136, "139") == ("escalate", [139], ["unmet"])   # …so not vice versa
        r = w.no_pr("--issue", "136", *R, *cfg, "--now", later)
        assert "cycle" in r["blockers"][0]["reason"]                 # the reason names the gap
        # one workable blocker does not excuse another that is not
        assert stops_blocked(136, "135,138") == ("escalate", [135, 138], ["waiting", "unmet"])
        # …and `afk park` refuses what `afk no-pr` would not call parkable, touching nothing
        assert "not this fleet's claim" in w.error(*_park(136, instance="peer"))
        err = w.error(*_park(136))
        assert "not parkable" in err and "nothing was changed" in err
        assert "#138 is open but no fleet will work it" in err and "#135" not in err
        assert w.claimed_by(136) == "me" and "136" not in w.state()["deps"]

        # --- a blocker a fleet is working is waited on ------------------------------
        assert stops_blocked(136, "137") == ("park", [137], ["waiting"])     # a live peer holds it
        assert stops_blocked(136, "135") == ("park", [135], ["waiting"])     # in flight under ME

        # the edge FAILS to record → the claim must still be held: released without
        # it, an issue still carrying the ready label is straight back on the frontier
        w.set(fail=["api --method"])
        assert "failed" in w.error(*_park(136))
        assert w.claimed_by(136) == "me"
        w.set(fail=[])

        w.calls()
        r = w.afk(*_park(136))
        assert r == {"issue": 136, "action": "parked", "blocked_by": [135], "edges_added": [135],
                     "released": True, "cleanup": {"removed": True, "path": d136["worktree"]}}
        # one order: the dependency edge, then the status board (the release is last)
        writes = [(c[c.index("--method") + 1], c[3].split("/", 4)[-1])
                  for c in w.calls() if "--method" in c]
        assert [m for m, _ in writes] == ["POST", "PATCH"], writes
        assert writes[0][1] == "136/dependencies/blocked_by" and "comments" in writes[1][1], writes
        assert w.state()["deps"]["136"] == [135]                     # the fact lives in GitHub
        assert "等待依赖 #135 关闭" in w.board(136) and "认领方" not in w.board(136)
        assert w.claimed_by(136) is None and not os.path.isdir(d136["worktree"])
        # neither the ready label nor the attempt count was touched: nobody re-adds anything
        assert w.issue(136)["labels"] == ["ready-for-agent", "afk-attempt/1"]

        # from here the ordinary frontier contract does the rest. While #135 is open,
        # #136 is excluded — so a worker that would only report `blocked` again is
        # never started: the park cannot loop
        ws = w.afk("rebuild", *ME, *R, *NOW)
        assert 136 not in [i["number"] for i in ws["frontier"]["dispatch"]]
        assert {"number": 136, "reason": "1 open blocker(s)"} in ws["frontier"]["excluded"]
        assert 136 not in [m["number"] for m in ws["mine"]]
        # …and the tick after #135 closes it is dispatchable again, with no human touch
        w.set(issues=[{**i, "state": "closed"} if i["number"] == 135 else i
                      for i in w.state()["issues"]])
        ws = w.afk("rebuild", *ME, *R, *NOW)
        assert 136 in [i["number"] for i in ws["frontier"]["dispatch"]]

        # --- a branch that holds work is kept; an edge already there is not re-added -
        w.work(d141["worktree"], "half.txt")
        w.set(deps={**w.state()["deps"], "141": [139]})
        later = str(int(time.time()) + 5000)
        assert stops_blocked(141, "139,135") == ("park", [139], ["waiting", "closed"])
        r = w.afk(*_park(141, "--set", "progress_comment=false"))
        assert r == {"issue": 141, "action": "parked", "blocked_by": [139], "edges_added": [],
                     "released": True}
        assert os.path.isdir(d141["worktree"]) and w.claimed_by(141) is None
        assert w.state()["deps"]["141"] == [139] and "等待依赖" not in w.board(141)


def test_close_settles_an_issue_that_needed_no_change():
    with world(issues=[issue(6, "ready-for-agent"), issue(7, "ready-for-agent")]) as w:
        d6, d7 = w.afk(*dispatch(6)), w.afk(*dispatch(7))
        close = lambda n, *extra: ("close", "--issue", str(n), *ME, *R, *NOW, *extra)

        assert "not this fleet's claim" in w.error("close", "--issue", "6", "--instance", "peer", *R)
        # the close fails → still claimed: an open issue is never left unowned AND ready
        w.set(fail=["issue close"])
        assert "issue close" in w.error(*close(6))
        assert w.claimed_by(6) == "me" and w.issue(6)["state"] == "open"

        w.set(fail=[])
        r = w.afk(*close(6))
        assert r == {"issue": 6, "action": "closed", "released": True,
                     "cleanup": {"removed": True, "path": d6["worktree"]}}
        assert w.issue(6)["state"] == "closed" and w.claimed_by(6) is None
        assert "无需改动" in w.board(6) and not os.path.isdir(d6["worktree"])

        r = w.afk(*close(7, "--set", "worktree_cleanup=false"))
        assert r == {"issue": 7, "action": "closed", "released": True}
        assert os.path.isdir(d7["worktree"]) and w.afk("rebuild", *ME, *R, *NOW)["mine"] == []


# --------------------------------------------------------------------------- #
# the CLI's own contracts                                                      #
# --------------------------------------------------------------------------- #

def test_every_failure_is_one_json_error():
    with world() as w:
        # every operational failure is exit 3 with one {"error": …} object — bad JSON
        # in, a missing file, a failing gh — so a tick never has to parse a traceback
        w.error("cycle", *R, "--state", "{not json")
        w.error("rebuild", *ME, *R, "--config", "{not json")
        assert "gh issue list failed" in w.error("rebuild", *ME, "--repo", "acme/other")
        # …and so is a bad command line: argparse's usage error is the same one shape
        assert "--instance" in w.error("rebuild", *R)
        assert "--reason" in w.error("escalate", "--issue", "4", *ME, *R)
        assert "--worker-command" in w.error("dispatch", "--issue", "4", *ME, *R)
        assert "invalid choice" in w.error("status", "4", "--phase", "bogus", *R)
        assert "invalid choice" in w.error("no-such-subcommand", bare=True)
        # the recipes that became transitions are gone, not aliased
        for gone in ("fingerprint", "pace", "next-attempt", "gate-run", "merge", "hand-back"):
            assert "invalid choice" in w.error(gone, bare=True), gone


def _minimal_argv(name, sub):
    """The shortest valid command line for one subcommand, minus `--config`."""
    positional = {"claim": ["1"], "reclaim": ["1"], "release": ["1"], "status": ["1"]}
    required = {"--instance": "me", "--repo": REPO, "--issue": "1", "--terminal": "idle",
                "--expect-sha": "s", "--phase": "claimed", "--worker-command": WORKER,
                "--reason": "why"}
    argv = [name, *positional.get(name, [])]
    for act in sub._actions:
        if act.required and act.option_strings and act.option_strings[0] != "--config":
            argv += [act.option_strings[0], required[act.option_strings[0]]]
    return argv


def test_config_is_required_and_resolves_one_way_on_every_subcommand():
    """ADR-0009's one resolution order — `--set` → `--config` → the defaults table —
    holds on EVERY subcommand that reads config, because there is one mechanism
    (`_cfg`) and no per-subcommand flag to wire or forget. And the carrier itself
    cannot be dropped: a call with no `--config` is refused, not run on defaults."""
    parser = afk.build_parser()
    takes_config = [n for n in parser.subcommands if n not in NO_CONFIG]
    assert len(takes_config) == len(parser.subcommands) - 2 >= 15

    with world() as w:
        for name in takes_config:
            sub = parser.subcommands[name]
            argv = _minimal_argv(name, sub)
            # refused through the real CLI: exit 3, one JSON error, naming the flag
            err = w.error(*argv, bare=True)
            assert "--config" in err and name in err, (name, err)

            def cfg(*extra, sub_argv=argv):
                return afk._cfg(parser.parse_args([*sub_argv, *extra]))

            assert cfg("--config", "{}") == afk_decide.resolve_config({}), name
            given = ("--config", json.dumps({"retry": 7, "gate": {"adversarial_verify": True}}))
            got = cfg(*given)
            assert got["retry"] == 7 and got["gate"]["adversarial_verify"] is True, name
            assert got["gate"]["ci"] == "required" and got["concurrency"] == 3      # omitted → default
            got = cfg(*given, "--set", "retry=9", "--set", "merge.target=rel")
            assert (got["retry"], got["merge"]["target"]) == (9, "rel"), name
            assert got["gate"]["adversarial_verify"] is True                  # --set is an overlay
            # the three inputs are the whole interface: no per-key override flags
            flags = set(sub._option_string_actions)
            assert {"--config", "--set", "--now"} <= flags, name
            assert not flags & {"--ns", "--ttl", "--retry", "--base", "--grace", "--ci", "--target",
                                "--command", "--force-after", "--ready-label", "--epic-labels"}, name
        # the two bootstrap subcommands run before a config exists, and take none
        for name in NO_CONFIG:
            assert not {"--config", "--set"} & set(parser.subcommands[name]._option_string_actions)

        # what `_cfg` hands a subcommand is always a config `afk config` would accept:
        # --config and --set are validated like the file is
        for bad, why in ((("--config", json.dumps({"gate": {"ci": "local"}})), "local_command"),
                         (("--config", json.dumps({"claim_namespace": "refs/x"})), "claim_namespace"),
                         (("--config", "{}", "--set", "gate.ci=optional"), "gate.ci"),
                         (("--config", "{}", "--set", "merge.strategy=octopus"), "merge.strategy"),
                         (("--config", "{}", "--set", "retyr=3"), "--set"),
                         (("--config", "{}", "--set", "retry=soon"), "retry"),
                         (("--config", "{not json"), "")):
            assert why in w.error("rebuild", *ME, *R, *bad), bad
        # …while a valid combination split across the two is accepted as a whole
        assert w.afk("rebuild", *ME, *R, "--config", json.dumps({"gate": {"ci": "local"}}),
                     "--set", "gate.local_command=make test")["free_slots"] == 3

    # only ref ops have a second way to name the remote; gh ops need --repo, full stop
    for name, sub in parser.subcommands.items():
        flags = set(sub._option_string_actions)
        if "--remote" in flags:
            assert "--repo" in flags and not sub._option_string_actions["--repo"].required, name
        elif "--repo" in flags:
            assert sub._option_string_actions["--repo"].required, name


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
    refs = os.path.join(SKILL, "references")
    docs = [os.path.join(SKILL, "SKILL.md")]
    docs += [os.path.join(refs, f) for f in sorted(os.listdir(refs)) if f.endswith(".md")]
    # the glossary lives in the source repo only — an installed skill ships without it
    context = os.path.join(SKILL, "..", "..", "CONTEXT.md")
    docs += [context] if os.path.exists(context) else []

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

    # the tool is ONE word: executable, and never documented behind an interpreter — a
    # tick that copies `python3 …/afk.py` into a variable gets exit 127 under zsh
    assert os.access(os.path.join(SKILL, "scripts", "afk.py"), os.X_OK)
    for path in docs:
        with open(path) as f:
            behind = re.findall(r"(?:python3?|uv run|bash|sh) +<skill>/scripts/afk\.py", f.read())
        assert not behind, f"{os.path.basename(path)}: {behind}"

    # the scanner itself: both spellings, fenced and inline, and nothing that merely
    # contains "afk" (the skill's name, a ref, a marker, a path)
    sample = ("Run `afk claim <n> --instance <id>`, not `/afk-fleet --tick` or `afk-claim/<n>`.\n"
              "```bash\n<skill>/scripts/afk.py no-pr --issue <n> \\\n     --terminal idle\n```\n"
              "See `docs/agents/afk-fleet.md` and `<!--afk:verdict …-->`; `afk` alone is not a call.")
    assert list(_documented_invocations(sample)) == \
        [("no-pr", ["--issue", "--terminal"]), ("claim", ["--instance"])]

    with open(os.path.join(refs, "tools.md")) as f:
        rows = [ln for ln in f.read().splitlines() if ln.startswith("| `afk ")]
    listed = [re.match(r"\| `afk ([a-z-]+)", ln).group(1) for ln in rows]
    assert sorted(listed) == sorted(subs), set(listed) ^ set(subs)


def _skill_docs():
    """{basename: text} of every document a tick or a human reads this skill from:
    SKILL.md, its references, and — in the source repo only, an installed skill
    ships without them — the glossary and the flow doc."""
    refs = os.path.join(SKILL, "references")
    paths = [os.path.join(SKILL, "SKILL.md")]
    paths += [os.path.join(refs, f) for f in sorted(os.listdir(refs)) if f.endswith(".md")]
    paths += [p for p in (os.path.join(SKILL, "..", "..", "CONTEXT.md"),
                          os.path.join(SKILL, "..", "..", "docs", "flows.md")) if os.path.exists(p)]
    docs = {}
    for path in paths:
        with open(path) as f:
            docs[os.path.basename(path)] = f.read()
    return docs


def test_the_docs_name_exactly_the_words_the_code_returns():
    """The pass routes on a `status`, an `outcome`, an `action` in code now; what a
    tick still acts on is a judgment's `kind`, and what a worker acts on is a
    landing's `outcome` — words each knows only from the docs. One the code
    returns and the docs never mention is a judgment, or a landing, with no
    instruction; so each vocabulary has one home in afk_decide and the docs are
    held to it."""
    docs = _skill_docs()
    skill, tools = docs["SKILL.md"], docs["tools.md"]

    def row(sub):
        return next(ln for ln in tools.splitlines() if ln.startswith(f"| `afk {sub}"))

    # the tick's judgment table IS the set, row for row — and so is the worker's
    # outcome table, for `afk land`, in the landing brief — the one place it is spelled
    table = re.findall(r"^\| `(\w+)` \|", skill, re.M)
    assert table == ["kind", *afk_decide.JUDGMENT_KINDS], table        # header, then rows
    for kind in afk_decide.JUDGMENT_KINDS:
        assert f"`{kind}`" in row("cycle"), kind
    prompt = docs["worker-prompt.md"]
    land = re.search(r"<!--afk:block landing-->\n(.*?)<!--/afk:block-->", prompt, re.S).group(1)
    assert re.findall(r"^\| `(\w+)` \|", prompt, re.M) == \
        re.findall(r"^\| `(\w+)` \|", land, re.M) == ["outcome", *afk_decide.LAND_OUTCOMES]
    assert "{land_command}" in land
    for outcome in afk_decide.LAND_OUTCOMES:
        assert outcome in row("land"), outcome

    # the routing words are the reference's: every one a subcommand can answer with
    # is listed on that subcommand's row, for the human reading a result
    for status in afk_decide.CLAIM_STATUSES:
        assert f"`{status}`" in row("rebuild"), status
    for outcome, _ in afk_decide.NO_PR_ROUTES:
        assert outcome in row("no-pr"), outcome
    for outcome in afk_decide.TURN_OUTCOMES:
        assert outcome in row("turn"), outcome
    # nothing a tick or a worker reads names a way to land that no longer exists,
    # or a summary handed between two calls
    for name, text in docs.items():
        for gone in ("afk merge", "afk hand-back", "handed_back", "awaiting_merge", "worker_busy",
                     "`queued`", "`unblocked`", "--summary", "summary_schema"):
            assert gone not in text, (name, gone)

    # the verdict marker is written into the worker prompt by the code that parses
    # it — the template never spells it — and each phase is explained to both readers
    assert "{verdict_marker}" in docs["worker-prompt.md"]
    assert "<!--afk:verdict n=" not in docs["worker-prompt.md"]
    for phase in afk_decide.VERDICT_PHASES:
        assert f"`{phase}`" in skill and f"**`{phase}`**" in docs["worker-prompt.md"], phase

    # a cycle's instructions are the short form: one call, then the judgments. The
    # routing table a tick used to re-read every pass is code under test, not prose
    tick = skill[skill.index("## A cycle ("):skill.index("## Cooperative multi-fleet")]
    assert " cycle --repo <repo> --config" in tick and len(tick.splitlines()) < 90
    assert "idle_stalled" not in skill and "merge_order" not in skill


def test_the_docs_have_the_launcher_run_each_cycle_itself():
    """A tick is a pass in code, not a context (ADR-0028): no document a launcher
    or a human reads this skill from tells anyone to spawn one. The one subagent
    left is the ephemeral reader of a `bulky` judgment, and the stop is a cycle."""
    docs = _skill_docs()
    skill = docs["SKILL.md"]
    for name, text in docs.items():
        spawned = re.findall(r".*(?<!re-)\bspawn.*", text)       # a worker may be re-spawned
        assert not spawned, (name, spawned)
        assert "fresh-context" not in text and "disposable tick" not in text, name
    # two roles hold a context; the tick is described beside them, not among them
    roles = re.findall(r"^\| \*\*(\w+)\*\* \|", skill[:skill.index("## Why it runs forever")], re.M)
    assert roles == ["launcher", "worker"], roles
    # every paragraph that names a subagent or the Agent tool is the bulky judgment's
    named = [para for para in re.split(r"\n(?=- |\n)", skill)
             if re.search(r"subagent|\bAgent\b", para)]
    assert len(named) == 1 and "`bulky: true`" in named[0], named
    # the stop is the drain, and the drain is `afk cycle`
    loop = skill[skill.index("### Loop"):skill.index("## Tools (")]
    assert " cycle --drain --repo <repo> --config" in loop and "afk release" not in loop
    assert "`afk cycle --drain`" in docs["cooperative-multi-fleet.md"]
    assert "--drain" in next(ln for ln in docs["tools.md"].splitlines()
                             if ln.startswith("| `afk cycle"))
    # the glossary agrees: the launcher runs the tick, and the facts ride in the state
    context = docs.get("CONTEXT.md")
    if context is not None:                       # an installed skill ships without it
        assert "ADR-0028" in context and "does no coordination" not in context


def test_the_tools_table_is_the_one_the_parser_generates():
    """tools.md's first column is rendered from `build_parser()` by
    `gen_tools_doc.py`; a flag added without re-running it turns this red."""
    import gen_tools_doc
    with open(gen_tools_doc.TOOLS_MD) as f:
        text = f.read()
    assert gen_tools_doc.render(text) == text, "run scripts/gen_tools_doc.py"
    subs = afk.build_parser().subcommands
    # the generator's own reading of a parser: positionals, required, optional, choices
    assert gen_tools_doc.usage("reclaim", subs["reclaim"]) == \
        "afk reclaim <n> --instance <id> --expect-sha <sha>"
    assert gen_tools_doc.usage("dispatch", subs["dispatch"]) == \
        ("afk dispatch --issue <n> --instance <id> --worker-command <cmd> "
         "[--ready-timeout <s>] [--start <auto\\|fresh>]")
    assert gen_tools_doc.usage("land", subs["land"]) == \
        "afk land --issue <n> [--gate-timeout <s>] [--excerpt-lines <k>]"


def test_the_docs_restate_config_only_as_the_schema_has_it():
    """Prose that names a config key or quotes its default is a copy of
    CONFIG_DEFAULTS: a key that does not exist is one a human sets to no effect,
    and a stale default is a pace or a lease nobody runs on."""
    docs = _skill_docs()
    defaults = afk_decide.resolve_config({})

    quoted = 0
    for name, text in docs.items():
        for key, value in re.findall(r"`([a-z_]+(?:\.[a-z_]+)?)`[^`\n]{0,30}?\bdefault ([\w/-]+)", text):
            section, _, leaf = key.rpartition(".")
            table = defaults.get(section, {}) if section else defaults
            if leaf not in table or isinstance(table[leaf], dict):
                continue
            assert value == json.dumps(table[leaf]).strip('"'), f"{name}: `{key}` default {value}"
            quoted += 1
    assert quoted >= 4, quoted            # the scan really found the restated defaults

    # a duration key is written with its unit, as the file has it: `claim_lease_ttl`
    # is not a key, and a config that sets it is refused
    stems = [k[:-len("_seconds")] for k in defaults if k.endswith("_seconds")]
    short = re.compile(r"\b(%s)(?!_seconds)\b" % "|".join(stems))
    for name, text in docs.items():
        assert not short.findall(text), f"{name}: {sorted(set(short.findall(text)))}"


def test_every_flow_anchor_still_names_something():
    """docs/flows.md anchors each step to `path:Symbol`. A renamed function leaves
    the doc pointing at nothing — and reading as if it still held."""
    flows = _skill_docs().get("flows.md")
    if flows is None:                     # an installed skill ships without it
        return
    root = os.path.join(SKILL, "..", "..")
    anchors = sorted(set(re.findall(r"`([A-Za-z0-9_./\-]+\.[A-Za-z0-9]+):([A-Za-z_][A-Za-z0-9_]*)`",
                                    flows)))
    assert len(anchors) > 20, len(anchors)
    for path, symbol in anchors:
        with open(os.path.join(root, path)) as f:
            assert re.search(r"(?<!\w)%s(?!\w)" % re.escape(symbol), f.read()), f"{path}:{symbol}"


def run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    run_all()
