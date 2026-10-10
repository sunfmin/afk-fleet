#!/usr/bin/env python3
"""
afk.py — the afk-fleet tool: the fleet's deterministic muscle.

Every *deterministic* action is one of these subcommands (ADR-0004), and `cycle`
runs a whole reconciliation pass of them — the tick — in code; the LLM is handed
only the judgments that pass could not make, each with the subcommand to run.
Every subcommand prints one JSON object to stdout:

  exit 0  it ran. A lost claim race is an outcome, not an error: `{"won": false}`.
  exit 3  it could not do its job (git/gh failed, bad input, a bad command line)
          → `{"error": "..."}`. An error is never dressed as an outcome: a push
          that failed for any reason OTHER than losing the race is exit 3, never
          `won: false`; a remote that cannot be read is exit 3, never an empty
          fleet; a claim that could not be deleted is exit 3, never `released`.

Two layers. Every decision is a pure function in afk_decide.py (no I/O, time
injected, fixture-tested). This file only gathers their inputs — git refs, a
worktree's git, gh, orca, the login shell — and applies their effects.

The tick's Act half is transitions, not recipes (ADR-0017): `dispatch` starts a
worker, `turn` gives a finished PR the landing turn, `fail` / `escalate` /
`park` / `close` settle a claim. Each performs its whole ordered sequence in one
process, so an invariant like "relabel before release" or "start from the
fetched base tip" is code, not a paragraph. `cycle` performs the pass that
routes a claim to one of them, and returns the rest as judgments.

Two subcommands are a worker's, not the tick's, run in its own worktree: `gate`
runs the local gate and puts a green run on record (ADR-0026), and `land` lands
the worker's PR on its landing turn — sync → gate → merge pinned to the gated
head — which is the only way a PR lands (ADR-0027) — or, as a merge batch's
worker, a whole stack of PRs behind one gate run (`land --batch`, ADR-0029).

Every subcommand that reads config REQUIRES the same `--config` (the canonical
JSON from `afk config`, then `afk probe`) and resolves it one way, in `_cfg`:
`--set key=value` → `--config` → CONFIG_DEFAULTS for the keys it omits (ADR-0009).

Invoked as:  <skill>/scripts/afk.py <subcommand> [flags]
"""
from __future__ import annotations

import argparse
import concurrent.futures
import dataclasses
import functools
import json
import os
import shlex
import socket
import subprocess
import sys
import time
import uuid
from typing import (Any, Callable, Iterable, Iterator, Literal, Mapping, NoReturn, Sequence,
                    TypeVar)

import afk_decide

Obj = afk_decide.Obj    # a JSON object: a payload of gh or orca, a row, a result
# The records the two scripts hand each other, declared in the core (ADR-0039).
from afk_decide import (BatchMember, BatchWorkerRow, BoardBatch, BranchSignal, Claim, Comment,
                        Issue, IssueRead, PullRequest, WorktreeSignal, Call, Config, Progress, Seen,  # noqa: E402
                        Stacked, Standing, Turn, WorkerReading, WorkerRow, WorkingSet)
Answer = afk_decide.Answer              # how a step of a tick's plan ended
StartOutcome = afk_decide.StartOutcome  # how beginning a dispatch ended
_T = TypeVar("_T")

# --------------------------------------------------------------------------- #
# config + clock                                                              #
# --------------------------------------------------------------------------- #

def _cfg(a: argparse.Namespace) -> Config:
    """The effective config for a subcommand: the `--config` JSON (canonical or
    partial) resolved through CONFIG_DEFAULTS, any `--set key=value` laid on top,
    then validated — no subcommand runs on a config `afk config` would refuse."""
    cfg = afk_decide.resolve_config(json.loads(a.config))
    return afk_decide.validate_config(afk_decide.override_config(cfg, a.set))


# What a subcommand could not do its job for — exit 3, `{"error": …}`.
_FAILURES = (OSError, ValueError, RuntimeError)


# The command line ends at the subcommand's own function (`cmd_*`): it reads the
# flags, and calls what does the work with named arguments. A tick calls those
# same functions — a transition is never handed a command line, a real one or
# one made up for it. What every one of them is given alike is a `_Run`; what
# only some read — the instance id, the host, the agent — is in the signature
# of each that reads it.

@dataclasses.dataclass(frozen=True)
class _Run:
    """What a subcommand runs on, the same for every transition of a tick: the
    target repo, the config, and the clock — each set by every caller (`_run`)
    — and what the run has learned of the status boards."""
    repo: str           # owner/name — None only for a git-ref op given `--remote`
    rem: str            # the git push/fetch target (`_remote`)
    cfg: Config            # the effective config (`_cfg`)
    clock: int | None   # `--now`, None outside tests: the time is read when asked
    # {issue number: `afk_decide.board_key`} of the status board each issue is
    # known to carry: what a cycle's state remembered from the last tick, and
    # every one written or found in place since (`_upsert_board`).
    boards: dict[int, str] = dataclasses.field(default_factory=dict)

    def now(self) -> int:
        return self.clock if self.clock is not None else int(time.time())


@dataclasses.dataclass(frozen=True)
class _GateLimits:
    """What one run of the local gate is held to."""
    timeout: int            # seconds before the run is called red
    excerpt_lines: int      # trailing log lines a red run's excerpt keeps; 0: none


@dataclasses.dataclass(frozen=True)
class _Agent:
    """How this run starts a worker's agent: the worker launch command, verbatim
    (ADR-0010), and the seconds a started agent is given to accept a prompt."""
    command: str
    ready_timeout: int


# --------------------------------------------------------------------------- #
# git/gh plumbing                                                             #
# --------------------------------------------------------------------------- #

# A stable identity for the tiny record commits (claims/heartbeats carry no code).
_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "afk-fleet", "GIT_AUTHOR_EMAIL": "afk@fleet.local",
    "GIT_COMMITTER_NAME": "afk-fleet", "GIT_COMMITTER_EMAIL": "afk@fleet.local",
}

_SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_WORKER_PROMPT = os.path.join(_SKILL, "references", "worker-prompt.md")

_LOCAL_SCAN = "refs/afk-scan"  # where `scan` mirrors remote refs, read-only, disposable
_LOCAL_RECOVERY = "refs/afk-recovery"  # ditto for `recovery`'s branch-vs-base compare
_LOCAL_GATE = "refs/afk-gate"  # ditto for `probe`'s sweep of expired gate records


def _claim_ref(cfg: Config, number: int | str) -> str:
    return f"{afk_decide.CLAIM_NAMESPACES[cfg['claim_namespace']][0]}/{number}"


def _heartbeat_ref(cfg: Config, instance: str) -> str:
    return f"{afk_decide.CLAIM_NAMESPACES[cfg['claim_namespace']][1]}/{instance}"


def _base(cfg: Config) -> str:
    """The base branch of the run: what workers cut from, open their PR against
    and land on. Raises on a config no launch has settled one in (ADR-0042) —
    nothing is ever cut from, or landed on, a branch nobody confirmed."""
    if not cfg["base_branch"]:
        raise ValueError("the config names no base branch: it is settled at bootstrap, by "
                         "`afk probe --base-branch <name>` (ADR-0042) — pass the config that "
                         "returned")
    return cfg["base_branch"]


def _remote(a: argparse.Namespace) -> str:
    """The git push/fetch target. `--repo owner/name` → its GitHub URL, so any ref
    op works from anywhere the gh commands do — no clone-with-the-right-origin
    required (falls back to the named `--remote`, default origin). One repo handle,
    whether git or gh reads it."""
    return f"https://github.com/{a.repo}.git" if a.repo else a.remote


def _run(a: argparse.Namespace) -> _Run:
    """The `_Run` of a subcommand's command line — any that takes `--config`."""
    return _Run(repo=a.repo, rem=_remote(a), cfg=_cfg(a), clock=a.now)


def _agent(a: argparse.Namespace) -> _Agent:
    """The `_Agent` of a command line that may start a worker (`starts_worker`)."""
    return _Agent(command=a.worker_command, ready_timeout=a.ready_timeout)


def _git(args: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    p = subprocess.run(["git", *args], capture_output=True, text=True, env=_GIT_ENV)
    if check and p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {p.stderr.strip()}")
    return p


def _gh(args: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    p = subprocess.run(["gh", *args], capture_output=True, text=True, env=_GIT_ENV)
    if check and p.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args[:2])} failed: {p.stderr.strip()}")
    return p


# One process is one tick, or one transition typed by hand: seconds long, and
# everything in it acts on one view of GitHub. So what a process has read it does
# not read again — the open PRs, the claim scan, an issue, an issue's comments
# are each one round trip however many steps ask (`_once`). Every read after a
# write sees it, and that is the WRITE's doing: each helper that writes to GitHub
# or to the remote's refs carries what it wrote into the reads already made
# (`_claim_written`, `_comment`) or drops the ones it made stale (`_forget`). A
# call site makes the write and reads on; it names no read. The two reads that
# must be new each time — the fingerprint a cycle closes on, a landing polling
# its checks — say so where they are made (`fresh=True`).
_READS: dict[tuple, Any] = {}


def _once(key: tuple, read: Callable[[], _T]) -> _T:
    """`read()`, made at most once per process for `key`."""
    if key not in _READS:
        _READS[key] = read()
    return _READS[key]


def _forget(*keys: tuple) -> None:
    """Drop reads a write made stale; with no keys, every read."""
    for key in keys or list(_READS):
        _READS.pop(key, None)


def _record_commit(kind: afk_decide.RecordKind, record: Obj, tree: str | None = None,
                   path: str = ".") -> str:
    """A parentless commit that carries one record → its sha, which is what gets
    pushed to a ref. Every record the fleet keeps on a ref is written here, and
    read back by `_read_record`; the encoding is `afk_decide.record_message`'s.
    The commit is of `tree` — the empty tree, unless the record is of a tree —
    so it drags no repo history along."""
    tree = tree or _git(["-C", path, "hash-object", "-t", "tree", "/dev/null"]).stdout.strip()
    return _git(["-C", path, "commit-tree", tree,
                 "-m", afk_decide.record_message(kind, record)]).stdout.strip()


def _read_record(kind: afk_decide.RecordKind, rev: str, path: str = ".") -> Obj | None:
    """The record of `kind` the commit `rev` carries, or None when it carries
    none: not a record, not of this kind, or missing a field the kind requires
    (`afk_decide.read_record`)."""
    return afk_decide.read_record(
        kind, _git(["-C", path, "log", "-1", "--format=%s", rev], check=False).stdout)


def _remote_sha(remote: str, refname: str) -> str:
    """The sha the remote has for one ref, "" if it has none. Raises when the
    remote cannot be read at all."""
    out = _git(["ls-remote", remote, refname]).stdout.split()
    return out[0] if out else ""


_NO_REMOTE_REF = "couldn't find remote ref"       # git's words for a ref the remote lacks


def _fetch_tip(rem: str, branch: str, cwd: str | None = None) -> str:
    """The sha at the tip of `branch` on the remote, with its objects fetched into
    this repo — what a worktree is created at, so a worker starts from what the
    REMOTE has now, never from a local branch that may be commits behind. One
    round trip: the fetch that brings the objects also says where the tip is."""
    at = ["-C", cwd] if cwd else []
    p = _git([*at, "fetch", "--quiet", rem, f"refs/heads/{branch}"], check=False)
    if p.returncode != 0:
        if _NO_REMOTE_REF in p.stderr:
            raise RuntimeError(f"the remote has no branch {branch!r}")
        raise RuntimeError(f"git fetch {rem} refs/heads/{branch} failed: {p.stderr.strip()}")
    return _git([*at, "rev-parse", "FETCH_HEAD"]).stdout.strip()


class OrcaError(RuntimeError):
    """An orca call that ran and answered `ok: false`; `code` is orca's own."""

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


def _orca(args: list[str], timeout: int = 60) -> Obj:
    """One `orca … --json` call → its `result` object. HARD: raises when orca
    cannot be run, exits non-zero, or answers `ok: false` — the Act half cannot
    start, find or remove a worker's worktree without it (ADR-0005). Called only
    by the worker module (`_Workers`, `_Worktree`), which is also where the one
    soft read is: `_Worktree._rows`, which recovery must survive without."""
    what = f"orca {' '.join(args[:2])}"
    try:
        p = subprocess.run(["orca", *args, "--json"], capture_output=True, text=True,
                           timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        raise RuntimeError(f"{what} could not run: {e}")
    try:
        doc = json.loads(p.stdout)
    except ValueError:
        doc = None
    if p.returncode != 0 or not isinstance(doc, dict) or not doc.get("ok"):
        err = doc.get("error") if isinstance(doc, dict) else None
        code = err.get("code") if isinstance(err, dict) else None
        raise OrcaError(f"{what} failed: {code or p.stderr.strip() or p.stdout.strip()[:200]}",
                        code)
    return doc.get("result") or {}


# One issue is one read, whoever asks and for whichever of its fields: the
# claim's issue (`_issue`), whether it is closed (`_issue_state`), a blocker a
# verdict names (`_blocker`), the id an edge is recorded with (`_add_blocker`).
_ISSUE_JQ = ("{id, title, state, state_reason, labels: [.labels[].name], "
             "pull_request: (.pull_request != null)}")


def _read_issue(repo: str, number: int) -> IssueRead | RuntimeError:
    """One issue as {"number", "id", "title", "state", "state_reason",
    "labels": [name...], "pull_request": bool} — or, when it could not be read,
    the error that says why: a caller either raises it (`_issue`) or takes it
    for "unknown". An open issue the gather listed is not read again."""
    def read() -> IssueRead | RuntimeError:
        try:
            p = _gh(["api", f"repos/{repo}/issues/{number}", "--jq", _ISSUE_JQ])
            return {"number": number, **json.loads(p.stdout)}       # `_ISSUE_JQ`'s keys
        except (RuntimeError, ValueError) as e:
            return RuntimeError(str(e))
    return _once(("issue", repo, number), read)


def _issue(repo: str, number: int) -> IssueRead:
    """One issue (`_read_issue`). Raises when it cannot be read."""
    issue = _read_issue(repo, number)
    if isinstance(issue, Exception):
        raise issue
    return issue


# Every open issue with what the frontier and the fingerprint read, one object a
# line. GitHub's REST issue list carries the open-blocker count on every row, and
# lists pull requests among the issues — which are left out here.
_ISSUES_JQ = (".[] | select(.pull_request == null) | {number, id, title, "
              "labels: [.labels[].name], updatedAt: .updated_at, "
              "blocked_by: (.issue_dependencies_summary.blocked_by // 0)}")


def _open_issues(repo: str) -> list[Issue]:
    """Every open issue — all of them, page after page — as {"number", "id",
    "title", "labels": [name...], "updatedAt", "blocked_by": <open blocker
    count>}: the one shape afk_decide reads. Each is also what `_read_issue`
    would say of it."""
    def read() -> list[Issue]:      # the keys are `_ISSUES_JQ`'s
        p = _gh(["api", "--paginate", f"repos/{repo}/issues?state=open&per_page=100",
                 "--jq", _ISSUES_JQ])
        return [json.loads(ln) for ln in p.stdout.splitlines() if ln.strip()]
    issues = _once(("issues", repo), read)
    for i in issues:
        _READS.setdefault(("issue", repo, i["number"]),
                          {"number": i["number"], "id": i["id"], "title": i["title"],
                           "state": "open", "state_reason": None, "labels": i["labels"],
                           "pull_request": False})
    return issues


_PR_FIELDS = ("number,title,headRefName,headRefOid,baseRefName,updatedAt,statusCheckRollup,"
              "closingIssuesReferences")


def _open_prs(repo: str, fresh: bool = False) -> list[PullRequest]:
    """Every open PR, with the fields the working set, the merge and a fresh
    start all read. `fresh`: read now, whatever this process read before."""
    def read() -> list[PullRequest]:
        rows = json.loads(_gh(["pr", "list", "--repo", repo, "--state", "open",
                               "--json", _PR_FIELDS + ",body"]).stdout)
        for row in rows:
            row["closingIssuesReferences"] = afk_decide.issues_closed_by(
                row.pop("body"), row["closingIssuesReferences"], repo)
        return rows
    if fresh:
        _forget(("prs", repo))
    return _once(("prs", repo), read)


def _issue_comments(repo: str, number: int, fresh: bool = False) -> list[Comment]:
    """An issue's comments, oldest first, as [{"id", "body", "url"}...] — a PR's
    too: its landing turn is one of them. `fresh`: read now, whatever this
    process read before."""
    def read() -> list[Comment]:
        p = _gh(["api", "--paginate", f"repos/{repo}/issues/{number}/comments",
                 "--jq", ".[] | {id, body, url: .html_url}"])
        return [json.loads(ln) for ln in p.stdout.splitlines() if ln.strip()]
    if fresh:
        _forget(("comments", repo, number))
    return _once(("comments", repo, number), read)


def _comment(repo: str, number: int, body: str, comment_id: int | None = None) -> int:
    """Write one comment on an issue or a PR → its id: a new one, or `comment_id`
    rewritten. The comments this process already read of it are kept in step."""
    read = _READS.get(("comments", repo, number))
    if comment_id is not None:
        _gh(["api", "--method", "PATCH", f"repos/{repo}/issues/comments/{comment_id}",
             "-f", f"body={body}"])
        for c in read or []:
            if c["id"] == comment_id:
                c["body"] = body
        return comment_id
    p = _gh(["api", "--method", "POST", f"repos/{repo}/issues/{number}/comments",
             "-f", f"body={body}"])
    new = json.loads(p.stdout)
    if read is not None:
        read.append({"id": new.get("id"), "body": body, "url": new.get("html_url")})
    return new.get("id")


def _issue_state(repo: str, number: int) -> str | None:
    """"open" | "closed", or None if the issue could not be read."""
    issue = _read_issue(repo, number)
    return None if isinstance(issue, Exception) else issue["state"]


def _issue_written(repo: str, number: int) -> None:
    """Drop what this process read of an issue it just wrote to: the issue, and
    the list of open issues that carries its labels and blockers."""
    _forget(("issue", repo, number), ("issues", repo))


def _edit_labels(repo: str, number: int, add: list[str], remove: list[str]) -> None:
    _gh(["issue", "edit", str(number), "--repo", repo,
         *(x for lb in add for x in ("--add-label", lb)),
         *(x for lb in remove for x in ("--remove-label", lb))])
    _issue_written(repo, number)


def _close_issue(repo: str, number: int) -> None:
    """Close an issue as completed."""
    _gh(["issue", "close", str(number), "--repo", repo, "--reason", "completed"])
    _issue_written(repo, number)


def _add_blocker(repo: str, number: int, blocker: int) -> None:
    """Record issue <number> as blocked by <blocker> — a native dependency edge."""
    blocker_id = _issue(repo, blocker)["id"]
    _gh(["api", "--method", "POST", f"repos/{repo}/issues/{number}/dependencies/blocked_by",
         "-F", f"issue_id={blocker_id}"])
    _issue_written(repo, number)


def _pr_comment(repo: str, number: int, body: str) -> None:
    """Add one comment to a PR."""
    _gh(["pr", "comment", str(number), "--repo", repo, "--body", body])
    _forget(("comments", repo, number))


def _close_pr(repo: str, rem: str, number: int, comment: str) -> None:
    """Close an open PR unmerged, with the comment that says why, and take its
    branch off the remote with it."""
    _gh(["pr", "close", str(number), "--repo", repo, "--comment", comment, "--delete-branch"])
    _forget(("prs", repo), ("comments", repo, number), ("heads", rem))


def _aim_pr(run: _Run, pr: PullRequest) -> None:
    """Point an open PR at the run's base branch, if it is open against another:
    one opened before the base branch changed (ADR-0042). GitHub merges a PR
    into, and shows it merged on, the branch it is open against — so a landing
    aims it first, and then syncs with and gates on the base it will land on."""
    if pr["baseRefName"] != _base(run.cfg):
        _gh(["pr", "edit", str(pr["number"]), "--repo", run.repo, "--base", _base(run.cfg)])
        _forget(("prs", run.repo))


def _merge_pr(repo: str, rem: str, pr: PullRequest, head: str) -> None:
    """Merge an open PR — a merge commit, never a squash or a rebase — only
    while its head is still `head`, and delete its branch. The issues it closes
    are closed here: GitHub closes them itself only when the PR landed on the
    repo's default branch."""
    _gh(["pr", "merge", str(pr["number"]), "--repo", repo, "--merge",
         "--match-head-commit", head, "--delete-branch"])
    _forget(("prs", repo), ("heads", rem))
    for ref in pr["closingIssuesReferences"] or []:
        _close_issue(repo, ref["number"])


def _blocker(repo: str, number: int) -> IssueRead | None:
    """One issue a `blocked` verdict names, as `afk_decide.blocker_standings` reads
    it: {"state", "state_reason", "labels": [name...], "pull_request"}. None
    when it cannot be read — which is never "closed"."""
    issue = _read_issue(repo, number)
    return None if isinstance(issue, Exception) else issue


def _blocked_by(repo: str, number: int) -> list[Obj]:
    """The issues GitHub records issue <number> as blocked by — its native
    dependency edges — as [{"number", "state"}...]. Raises when it cannot be read."""
    p = _gh(["api", "--paginate", f"repos/{repo}/issues/{number}/dependencies/blocked_by",
             "--jq", ".[] | {number, state}"])
    return [json.loads(ln) for ln in p.stdout.splitlines() if ln.strip()]


def _turn(repo: str, pr_number: int) -> Turn | None:
    """The landing turn recorded on PR `pr_number` (`afk_decide.latest_turn`),
    whoever granted it; None when it has none. One comments read."""
    return afk_decide.latest_turn(_issue_comments(repo, pr_number))


def _claim_turns(repo: str, prs: list[PullRequest], claims: list[Claim],
                 instance: str) -> dict[int, Turn]:
    """{issue number: turn} for every claim of `instance` whose open PR carries a
    turn marker, whoever wrote it — `afk_decide.held_turn` says which of them
    hold its landing turn. Keyed on claims, so a PR whose claim was released
    (escalated, parked) holds nothing; one comments read per claim of
    `instance` that has a PR."""
    turns: dict[int, Turn] = {}
    for c in claims:
        pr = afk_decide.closing_pr(prs, c["number"]) if c["instance"] == instance else None
        turn = _turn(repo, pr["number"]) if pr else None
        if turn:
            turns[c["number"]] = turn
    return turns


def _single_turn(run: _Run, number: int, pr_number: int,
                 fresh: bool = False) -> tuple[str | None, Turn | None]:
    """(the instance that holds issue <number>'s claim, the landing turn ONE PR
    holds from it) — the turn is None when PR `pr_number` holds none from that
    instance, or holds a merge batch's (`afk_decide.held_turn`). `fresh`: the
    claims and the PR's comments are read now, whatever this process read
    before — what a landing asks again once its gate has run."""
    if fresh:
        _scan(run, fresh=True)
        _issue_comments(run.repo, pr_number, fresh=True)
    owner = _claim_owner(run, number)
    turn = afk_decide.held_turn(_turn(run.repo, pr_number), owner)
    return owner, None if turn is None or turn["batch"] else turn


def _record_turn(repo: str, pr_number: int, turn: Turn) -> int:
    """Write PR `pr_number`'s ONE landing-turn comment from its record → its id.
    `turn` is the record read from the PR (`_turn`) plus what changed
    (`afk_decide.next_turn` and its kin): the comment it was read from is
    rewritten when there was one, whoever wrote it, so a PR never carries two
    turns to tell apart."""
    return _comment(repo, pr_number, afk_decide.turn_comment(turn), turn["comment_id"])


def _worktree_progress(wt: str, rem: str, base: str) -> Progress:
    """One worktree's git progress: commits ahead of `base`, dirty tree, last
    commit + newest file mtime. `base` is measured where it actually is — the
    REMOTE's tip, fetched here — never the local branch of that name, which a
    checkout that has not pulled leaves commits behind (and against which a
    worktree freshly cut from the remote tip would read as "ahead" with no work in
    it). `commits_ahead` is None when the count could not be read at all (a broken
    worktree) — unreadable is NOT zero, and `select_recovery` relies on the
    difference. Raises when the remote has no such base."""
    tip = _fetch_tip(rem, base, cwd=wt)
    ahead = _git(["-C", wt, "rev-list", "--count", f"{tip}..HEAD"], check=False).stdout.strip()
    dirty = _git(["-C", wt, "status", "--porcelain"], check=False).stdout.strip()
    ct = _git(["-C", wt, "log", "-1", "--format=%ct"], check=False).stdout.strip()
    return {"commits_ahead": int(ahead) if ahead.isdigit() else None,
            "dirty": bool(dirty),
            "last_commit_ts": int(ct) if ct.isdigit() else None,
            "worktree_mtime_ts": _newest_mtime(wt)}


def _remote_heads(remote: str) -> list[str]:
    """Every branch name on the remote (one `ls-remote`). Raises when the remote
    cannot be read — an unreadable remote is not one with no branches."""
    def read() -> list[str]:
        rows = [ln.split() for ln in _git(["ls-remote", "--heads", remote]).stdout.splitlines()]
        return [r[1][len("refs/heads/"):] for r in rows
                if len(r) == 2 and r[1].startswith("refs/heads/")]
    return _once(("heads", remote), read)


def _push_branch(repo: str, rem: str, path: str, sha: str, branch: str, force: bool = False,
                 check: bool = True) -> subprocess.CompletedProcess[str]:
    """Push `sha` from the repo at `path` to a branch on the remote → git's
    result. An open PR of that branch has a new head."""
    p = _git(["-C", path, "push", *(["--force"] if force else []), rem,
              f"{sha}:refs/heads/{branch}"], check=check)
    if p.returncode == 0:
        _forget(("heads", rem), ("prs", repo))
    return p


def _delete_branch(rem: str, branch: str, check: bool = True) -> None:
    """Delete one branch on the remote. `check=False`: a branch already gone is
    what was wanted."""
    _git(["push", rem, "--delete", f"refs/heads/{branch}"], check=check)
    _forget(("heads", rem))


def _branch_ahead(remote: str, branch: str, base: str, slot: int) -> int | None:
    """How many commits `branch` is ahead of `base` ON THE REMOTE — the tier-2
    signal — or None when the branch was never pushed. git only (no gh), mirrored
    into a disposable local namespace, so it reads the same whether the remote is
    a GitHub URL or a bare path. `slot` (the issue number) keeps those temp refs
    per-issue, so two recoveries sharing one clone cannot read each other's mirror.
    Raises when the remote cannot be read or has no `base`: "could not look" must
    never read as "nothing pushed", which is what sends a claim to tier 3. One
    round trip: the fetch that mirrors the branch is also what says it is absent."""
    ours = f"{_LOCAL_RECOVERY}/{slot}"
    p = _git(["fetch", "--force", remote,
              f"refs/heads/{branch}:{ours}/branch", f"refs/heads/{base}:{ours}/base"], check=False)
    if p.returncode != 0:
        if f"{_NO_REMOTE_REF} refs/heads/{branch}" in p.stderr:
            return None
        raise RuntimeError(f"git fetch {remote} of {branch!r} and {base!r} failed: "
                           f"{p.stderr.strip()}")
    return int(_git(["rev-list", "--count", f"{ours}/base..{ours}/branch"]).stdout.strip())


def _newest_mtime(root: str) -> int | None:
    """The newest file/dir mtime anywhere under `root`, excluding .git — a
    filesystem-level 'when did this worktree last change' independent of git
    (catches edits a worker hasn't committed). None on an empty/missing tree."""
    newest = None
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for name in (*dirnames, *filenames):
            try:
                mt = os.lstat(os.path.join(dirpath, name)).st_mtime
            except OSError:
                continue
            if newest is None or mt > newest:
                newest = mt
    return int(newest) if newest is not None else None


# --------------------------------------------------------------------------- #
# claim refs: scan / claim / reclaim / takeover / release / heartbeat          #
# --------------------------------------------------------------------------- #

def _mirrored_records(kind: afk_decide.RecordKind,
                      local_ns: str) -> Iterator[tuple[str, str, Obj | None]]:
    """(ref's last path segment, sha, its record of `kind` or None) for each
    mirrored ref."""
    rows = _git(["for-each-ref", "--format=%(refname) %(objectname)", local_ns],
                check=False).stdout.splitlines()
    for row in rows:
        refname, sha = row.split(" ", 1)
        yield refname.rsplit("/", 1)[-1], sha, _read_record(kind, sha)


def _claim_row(number: int, sha: str, record: Obj | None) -> Claim:
    """The claim on issue <number> as the scan lists it. The ref is the lock, so
    a claim ref that carries no claim record is still a claim — one that names
    nobody, which is never mine and is stale to everyone."""
    record = record or {}
    return {"number": number, "instance": record.get("instance"), "host": record.get("host"),
            "ts": record.get("ts"), "sha": sha}


def _scan_key(run: _Run) -> tuple[str, str, str]:
    return ("scan", run.rem, run.cfg["claim_namespace"])


def _scan(run: _Run, fresh: bool = False) -> tuple[list[Claim], dict[str, int]]:
    """Mirror the remote claim+heartbeat refs into a disposable local namespace and
    read every record. Returns (claims, heartbeats). Raises when the remote cannot
    be read: a fleet whose claims are unreadable must not look like one holding none.
    `fresh`: read now, whatever this process read before."""
    def read() -> tuple[list[Claim], dict[str, int]]:
        claim_ns, hb_ns, _ = afk_decide.CLAIM_NAMESPACES[run.cfg["claim_namespace"]]
        _git(["fetch", "--prune", run.rem,
              f"+{claim_ns}/*:{_LOCAL_SCAN}/claim/*",
              f"+{hb_ns}/*:{_LOCAL_SCAN}/heartbeat/*"])
        claims = [_claim_row(int(name), sha, record) for name, sha, record
                  in _mirrored_records(afk_decide.CLAIM_RECORD, f"{_LOCAL_SCAN}/claim")
                  if name.isdigit()]
        heartbeats = {name: record["ts"] for name, _, record
                      in _mirrored_records(afk_decide.HEARTBEAT_RECORD, f"{_LOCAL_SCAN}/heartbeat")
                      if record}
        return claims, heartbeats
    if fresh:
        _forget(_scan_key(run))
    return _once(_scan_key(run), read)


def _claim_written(run: _Run, number: int, row: Obj | None = None) -> None:
    """Carry a claim ref this process just wrote into the scan it has made, if it
    made one: `row` is the claim now on issue <number>, None when it was deleted."""
    scan = _READS.get(_scan_key(run))
    if scan:
        scan[0][:] = [c for c in scan[0] if c["number"] != number] + ([row] if row else [])


def cmd_scan(a: argparse.Namespace) -> Obj:
    run = _run(a)
    claims, heartbeats = _scan(run)
    return {"claims": claims, "heartbeats": heartbeats}


def cmd_classify_claims(a: argparse.Namespace) -> Obj:
    run = _run(a)
    now = run.now()
    claims, heartbeats = _scan(run)
    return {**afk_decide.classify_claims(claims, heartbeats, a.instance, now,
                                         afk_decide.CLAIM_LEASE_TTL_SECONDS),
            "now": now}


def _claim(run: _Run, number: int, instance: str, host: str) -> Obj:
    """Atomically create one claim ref → {"won", …}; `won: false` names the `owner`."""
    rem, ref = run.rem, _claim_ref(run.cfg, number)
    record = {"instance": instance, "host": host, "ts": int(run.now())}
    sha = _record_commit(afk_decide.CLAIM_RECORD, record)
    # Create-only: the server rejects a ref that already exists → that is the CAS.
    p = _git(["push", rem, f"{sha}:{ref}"], check=False)
    if p.returncode == 0:
        _claim_written(run, number, {"number": number, **record, "sha": sha})
        return {"won": True, "issue": number, "ref": ref, "sha": sha, "instance": instance}
    _forget(_scan_key(run))      # it did not show this claim
    if _git(["fetch", rem, ref], check=False).returncode != 0:
        raise RuntimeError(f"claim push to {ref} failed and no such claim exists on the "
                           f"remote, so this is not a lost race: {p.stderr.strip()}")
    owner = _read_record(afk_decide.CLAIM_RECORD, "FETCH_HEAD") or {}  # who beat us
    return {"won": False, "issue": number, "ref": ref,
            "owner": owner, "detail": p.stderr.strip()}


def cmd_claim(a: argparse.Namespace) -> Obj:
    run = _run(a)
    return _claim(run, a.number, a.instance, a.host)


def _force_take(run: _Run, number: int, expect_sha: str, instance: str, host: str) -> Obj:
    """The atomic re-stamp of ONE existing claim ref to `instance`: rejected unless
    the ref still points at the sha we read. The single mechanism behind both an
    unattended stale reclaim and a human-authorized takeover — they differ only in
    what gates the *choice* of claim (an expired lease vs a present human), never
    in the push, so a takeover is exactly as safe against a live peer."""
    rem, ref = run.rem, _claim_ref(run.cfg, number)
    record = {"instance": instance, "host": host, "ts": int(run.now())}
    sha = _record_commit(afk_decide.CLAIM_RECORD, record)
    p = _git(["push", rem, f"--force-with-lease={ref}:{expect_sha}", f"{sha}:{ref}"], check=False)
    if p.returncode == 0:
        _claim_written(run, number, {"number": number, **record, "sha": sha})
        return {"won": True, "issue": number, "ref": ref, "sha": sha, "instance": instance}
    _forget(_scan_key(run))      # the claim is not where it showed it
    if _remote_sha(rem, ref) == expect_sha:
        raise RuntimeError(f"reclaim push to {ref} failed although the claim has not moved, "
                           f"so this is not a lost race: {p.stderr.strip()}")
    return {"won": False, "issue": number, "ref": ref, "detail": p.stderr.strip()}


def cmd_reclaim(a: argparse.Namespace) -> Obj:
    run = _run(a)
    return _force_take(run, a.number, a.expect_sha, a.instance, a.host)


def cmd_takeover(a: argparse.Namespace) -> Obj:
    """The human-authorized, lease-skipping reclaim of a DEAD fleet instance's
    claims (ADR-0011). Two shapes:

      --list              every instance GitHub still remembers — from the claim
                          markers and the heartbeat refs — with heartbeat age, host
                          and claim count. A launcher forgets its own id when it
                          dies; the repo does not.
      --from <dead-id>    force-take that instance's claims with the SAME atomic
                          --force-with-lease push a stale reclaim uses, only
                          skipping the staleness gate, re-stamping each with
                          `--instance` (mine). A target whose heartbeat is still
                          fresh returns `confirm` and takes nothing until `--yes`.

    Takeover neither reads nor increments `afk-attempt/<n>`: it answers "did the
    fleet die?", not "is this work failing?". What each taken claim then *does* is
    continuation (`afk recovery`), not a fresh re-dispatch."""
    run = _run(a)
    now = run.now()
    ttl = afk_decide.CLAIM_LEASE_TTL_SECONDS
    claims, heartbeats = _scan(run)

    if a.list:
        return {"instances": afk_decide.group_instances(claims, heartbeats, a.instance, now, ttl),
                "me": a.instance, "now": now, "ttl": ttl}
    if not a.source:
        raise ValueError("--from <dead instance id> is required unless --list")

    plan = afk_decide.plan_takeover(claims, heartbeats, a.source, a.instance, now, ttl, a.yes)
    if plan["action"] != "take":
        return {**plan, "taken": [], "lost": []}

    taken, lost = [], []
    for c in plan["claims"]:
        r = _force_take(run, c["number"], c["sha"], a.instance, a.host)
        (taken if r["won"] else lost).append(r)
    return {**plan, "action": "taken", "as": a.instance,
            "taken": [t["issue"] for t in taken], "lost": lost,
            "detail": f"took {len(taken)}/{len(plan['claims'])} claim(s) from {a.source}"
                      + ("; a lost one means that fleet is not dead — its ref moved under us"
                         if lost else "")}


def _release(run: _Run, number: int) -> Obj:
    """Delete one claim ref. Already gone counts as released — idempotent cleanup.
    A delete that failed with the claim still there is a phantom lock in the
    making, so it raises."""
    rem, ref = run.rem, _claim_ref(run.cfg, number)
    p = _git(["push", rem, "--delete", ref], check=False)
    if p.returncode != 0 and _remote_sha(rem, ref):
        raise RuntimeError(f"release failed and {ref} is still on the remote: "
                           f"{p.stderr.strip()}")
    _claim_written(run, number)
    return {"released": True, "issue": number, "ref": ref}


def _clear(run: _Run, number: int, expect_sha: str) -> Obj:
    """Delete one claim ref that is NOT mine — a `stale_closed` row — only while it
    still points at the sha rebuild read: the same lease a reclaim takes it under,
    so a claim somebody took meanwhile is never deleted from under them. Already
    gone counts as released; a claim that moved raises."""
    rem, ref = run.rem, _claim_ref(run.cfg, number)
    p = _git(["push", rem, f"--force-with-lease={ref}:{expect_sha}", f":{ref}"], check=False)
    now_at = "" if p.returncode == 0 else _remote_sha(rem, ref)
    if now_at == expect_sha:
        raise RuntimeError(f"release failed and {ref} is still on the remote: "
                           f"{p.stderr.strip()}")
    if now_at:
        raise RuntimeError(f"{ref} moved since it was read (expected {expect_sha}, now "
                           f"{now_at}): somebody took the claim; nothing was changed")
    _claim_written(run, number)
    return {"released": True, "issue": number, "ref": ref}


def cmd_release(a: argparse.Namespace) -> Obj:
    """Delete one claim ref on its own, for the cases no transition covers:

      afk release <n> --instance <id>
          a claim of MINE — an orphan-release, a `closed` row, the drain. Refuses
          a claim another instance holds. With `--repo`, a claim whose issue is
          CLOSED — the `closed` row: its worker landed the PR, and `afk land`
          can neither release the claim nor remove the worktree it runs in — has
          its worktree removed too (`cleanup`). An open
          issue's worktree is never touched: it may hold work. A PR a merge
          batch landed that GitHub still does not show merged is closed here
          (`closed_pr`), and the landing is fast-forwarded into the fleet's own
          checkout (`synced`, ADR-0037). An OPEN issue whose landing is
          already on the target — a merge batch pushed it and was cut before
          it closed the issue: a `landed` row — is closed first, its status
          board says merged, and it is settled like any closed one.
      afk release <n> --instance <id> --expect-sha <sha>
          a `stale_closed` row of rebuild — a dead peer's claim on an issue that
          is already closed: a phantom lock, deleted instead of reclaimed."""
    return _release_claim(_run(a), a.instance, a.number, expect_sha=a.expect_sha)


def _release_claim(run: _Run, instance: str, number: int,
                   expect_sha: str | None = None) -> Obj:
    """`afk release` — of `instance`'s own claim on issue <number>
    (`_release_mine`), or, with `expect_sha`, of a dead peer's `stale_closed`
    row read at that sha (`_clear`). Two operations behind one subcommand: only
    the first asks whose the claim is, and only it cleans up after a landing."""
    if expect_sha:
        return _clear(run, number, expect_sha)
    return _release_mine(run, instance, number)


def _release_mine(run: _Run, instance: str, number: int) -> Obj:
    """Release `instance`'s claim on issue <number> — refused for a claim
    another instance holds — and, when the issue is CLOSED, settle what its
    landing left behind (`_settle_landed`). An OPEN issue a merge batch landed
    (`_cut_landing`) is closed first, and so settled the same way."""
    owner = _claim_owner(run, number)
    if owner not in (None, instance):
        raise RuntimeError(f"issue #{number} is not this fleet's claim "
                           f"({_claim_ref(run.cfg, number)} is held by {owner!r}); nothing was "
                           f"changed. A dead peer's claim on a closed issue — a `stale_closed` "
                           f"row — is released with --expect-sha <the sha rebuild reported>")
    claim = next((c for c in _scan(run)[0] if c["number"] == number), None)
    landed = _cut_landing(run, claim) if run.repo and claim else None
    if landed:          # the issue first: closed, it is settled below whatever cuts this short
        _finish_member(run, instance, {"issue": number, "pr": landed})
    released = _release(run, number)
    if run.repo and _issue_state(run.repo, number) == "closed":
        released.update(_settle_landed(run, number))
    return released


def _settle_landed(run: _Run, number: int) -> Obj:
    """What only the tick can do for a claim whose issue is closed — `afk land`
    runs inside the worktree and holds no instance id → {"closed_pr"?,
    "cleanup"?}: close the PR a merge batch landed that GitHub still shows open
    (`_close_landed_pr`), remove the worktree, and
    bring the landing into the fleet's own checkout (`_sync_checkout`). An open
    issue's worktree is never touched: it may hold work."""
    settled = {}
    closed_pr = _close_landed_pr(run, number)
    if closed_pr:
        settled["closed_pr"] = closed_pr
    worktree = _Worktree.of_issue(run.repo, number)
    if worktree.remembered:
        settled["cleanup"] = worktree.remove()
    settled["synced"] = _sync_checkout(run)
    return settled


def _sync_checkout(run: _Run) -> Obj:
    """Fast-forward the merge target's LOCAL branch, in the checkout orca cuts
    worktrees from — the one the launcher runs in and its human reads — to the
    remote's tip, so what the fleet landed is on this machine without anyone
    pulling (ADR-0037) → {"branch", "tip", "updated": bool}, or {"branch",
    "skipped": why}.

    Only ever a fast-forward: checked out, the branch is merged `--ff-only`
    (uncommitted changes the landing does not touch stay as they are); not
    checked out, its ref is moved by a fetch, which refuses anything else. A
    local branch that diverged, changes in the way, no such local branch, no
    orca — each is `skipped` and nothing is changed: SOFT, a checkout that
    cannot follow never fails the release it rides on."""
    target = _base(run.cfg)
    try:
        orca_repo = _Worktree.source(run.repo)
        if not orca_repo:
            return {"branch": target, "skipped": f"orca knows no repo for {run.repo}"}
        at = ["-C", orca_repo["path"]]
        local = f"refs/heads/{target}"
        before = _git([*at, "rev-parse", "-q", "--verify", local], check=False).stdout.strip()
        if not before:
            return {"branch": target, "skipped": f"this checkout has no local branch {target!r}"}
        tip = _fetch_tip(run.rem, target, cwd=orca_repo["path"])
        if before != tip:
            here = _git([*at, "symbolic-ref", "-q", "--short", "HEAD"], check=False).stdout.strip()
            moved = _git([*at, "merge", "--ff-only", "--quiet", tip] if here == target else
                         [*at, "fetch", "--quiet", run.rem, f"{local}:{local}"], check=False)
            if moved.returncode != 0:
                return {"branch": target, "skipped": moved.stderr.strip().splitlines()[0]}
        urls = _git([*at, "config", "--get-regexp", r"^remote\..*\.url$"], check=False).stdout
        named = {key[len("remote."):-len(".url")]: url
                 for key, url in (line.split(None, 1) for line in urls.splitlines())}
        for name in afk_decide.remotes_of(named, run.repo):
            _git([*at, "update-ref", f"refs/remotes/{name}/{target}", tip], check=False)
        return {"branch": target, "tip": tip, "updated": before != tip}
    except _FAILURES as e:
        return {"branch": target, "skipped": str(e)}


def _claim_of(run: _Run, number: int) -> Claim | None:
    """Issue <number>'s claim, as the scan has it; None when it has none."""
    return next((c for c in _scan(run)[0] if c["number"] == number), None)


def _claim_owner(run: _Run, number: int) -> str | None:
    """The instance id issue <number>'s claim is stamped with, as the scan has
    it: None when there is no such claim, "" when there is one whose marker names
    nobody — which is never mine."""
    claim = _claim_of(run, number)
    return None if claim is None else claim["instance"] or ""


def _require_mine(run: _Run, number: int, instance: str) -> None:
    """Refuse to settle a claim this fleet does not hold: every transition that
    merges, relabels or releases an issue acts on MY claim only (ADR-0003)."""
    ref = _claim_ref(run.cfg, number)
    owner = _claim_owner(run, number)
    if owner != instance:
        held = "not claimed at all" if owner is None else f"held by {owner!r}"
        raise RuntimeError(f"issue #{number} is not this fleet's claim ({ref} is {held}); "
                           f"nothing was changed")


def _beat(run: _Run, instance: str) -> Obj:
    """Refresh my heartbeat ref if it is due (stateless: the old ts is read from
    the refs, in the scan)."""
    rem, cfg, now = run.rem, run.cfg, run.now()
    ref = _heartbeat_ref(cfg, instance)
    heartbeats = _scan(run)[1]
    last = heartbeats.get(instance)
    if not afk_decide.heartbeat_due(last, now, afk_decide.CLAIM_LEASE_TTL_SECONDS):
        return {"refreshed": False, "reason": "not due", "ts": last, "ref": ref}
    sha = _record_commit(afk_decide.HEARTBEAT_RECORD, {"instance": instance, "ts": now})
    _git(["push", rem, "--force", f"{sha}:{ref}"])
    heartbeats[instance] = now
    return {"refreshed": True, "ts": now, "ref": ref}


def cmd_heartbeat(a: argparse.Namespace) -> Obj:
    run = _run(a)
    return _beat(run, a.instance)


# --------------------------------------------------------------------------- #
# bootstrap: config / probe / worker-command                                   #
# --------------------------------------------------------------------------- #

def cmd_config(a: argparse.Namespace) -> Config:
    """One home for config (ADR-0009): read the target repo's config file (the
    ```yaml block in docs/agents/afk-fleet.md), validate every key against the
    schema (unknown key / wrong shape → error — with the human present at
    bootstrap), fill defaults, and print the canonical JSON the launcher
    hands every `afk` call. `--defaults` prints the pure defaults table."""
    if a.defaults:
        return afk_decide.resolve_config({})
    if not a.file:
        raise ValueError("--file <path to docs/agents/afk-fleet.md> is required unless --defaults")
    with open(a.file) as f:
        cfg = afk_decide.resolve_config(afk_decide.parse_config_yaml(f.read()))
    # Load time is where semantic validation matters most (ADR-0009/ADR-0012): the
    # human is present here, so `gate.ci: local` with no local_command is refused
    # where it can be fixed — not discovered by a tick about to merge unverified.
    return afk_decide.validate_config(cfg)


def _branch_protection(repo: str, branch: str) -> tuple[Obj | None, str | None]:
    """One branch's protection → (protection|None, unavailable_detail|None). A
    branch with NO protection is a successful read of "nothing", not an error —
    only a read that genuinely failed (no admin rights, an API error) is
    inconclusive, and `protection_verdict` warns rather than guesses on those."""
    p = _gh(["api", f"repos/{repo}/branches/{branch}/protection"], check=False)
    if p.returncode == 0:
        try:
            return json.loads(p.stdout), None
        except json.JSONDecodeError:
            return None, "unparseable branch-protection response"
    err = ((p.stderr or "") + (p.stdout or "")).strip()
    if "Branch not protected" in err:
        return None, None
    return None, err or f"gh exited {p.returncode}"


# What a probe's ref is named, under the prefix it asks about: `probe-<a name of
# this probe's own>`. No two probes name the same ref, so two launches probing at
# once never meet on one — a push the server turns down is turned down for where
# the ref is, never for a ref another probe put there — and a probe killed before
# its delete leaves a ref no later probe pushes to. What is left that way is
# deleted by the next probe (`_drop_probes`).
_PROBE_LEAF = "probe"


def _push_probe(rem: str, prefix: str, now: int) -> tuple[str, subprocess.CompletedProcess[str]]:
    """Ask the remote whether it takes a new ref under `prefix`, by pushing one →
    (the ref, the push). The ref is this probe's alone (`_PROBE_LEAF`)."""
    ref = f"{prefix}/{_PROBE_LEAF}-{uuid.uuid4().hex}"
    sha = _record_commit(afk_decide.PROBE_RECORD, {"ts": now})
    return ref, _git(["push", "--quiet", rem, f"{sha}:{ref}"], check=False)


def _drop_probes(rem: str, prefix: str, own: str) -> None:
    """Delete this probe's ref, then every probe ref left under `prefix` — a
    launch killed between its push and its delete. One of those may be a probe
    still running: it has its answer already, and its own delete finds nothing.
    Never raises: a ref that stays is litter the next probe deletes."""
    _git(["push", "--quiet", rem, "--delete", own], check=False)
    left = [row.split()[1] for row in _git(["ls-remote", rem, f"{prefix}/{_PROBE_LEAF}*"],
                                           check=False).stdout.splitlines()]
    if left:
        _git(["push", "--quiet", rem, "--delete", *left], check=False)


def _usable_namespace(rem: str, wanted: str, now: int) -> tuple[str, str | None]:
    """The first claim namespace the remote lets us push under — `wanted`, else
    the branch fallback — as (namespace, rejection|None). Only a
    push the SERVER rejected (an org ruleset forbidding `refs/afk/*`) moves on
    to the fallback; a push that never reached a verdict (auth, network) raises,
    because that says nothing about which namespace is allowed. The answer is the
    remote's alone: every fleet on a repo must lock in the same place, so neither
    another probe running now nor one that died may change it (`_PROBE_LEAF`)."""
    rejection = None
    for ns in dict.fromkeys([wanted, afk_decide.BRANCH_NAMESPACE]):
        prefix = afk_decide.CLAIM_NAMESPACES[ns][0]
        ref, p = _push_probe(rem, prefix, now)
        if p.returncode == 0:
            _drop_probes(rem, prefix, ref)
            return ns, rejection
        if "[remote rejected]" not in p.stderr:
            raise RuntimeError(f"probe push to {ref} failed: {p.stderr.strip()}")
        rejection = rejection or p.stderr.strip()
    raise RuntimeError(f"the remote rejects claim refs under both {wanted} and "
                       f"{afk_decide.BRANCH_NAMESPACE}: {rejection}")


def cmd_probe(a: argparse.Namespace) -> Obj:
    """The bootstrap compatibility probe — four questions, all answered with the
    human present so a misfit is fixed here rather than mid-run (ADR-0009's tradition):

    1. **Claim namespace** — can we push under the config's `claim_namespace`
       (`refs/afk`, unless a `--set` says otherwise)? Else fall back to branches (`refs/heads/afk-claim/*`) and say
       so: `blocked`, with the server's rejection as `detail` — claim churn will
       then fire `on: push` CI. The result's `config` is the canonical config with
       the namespace that actually works: the launcher holds THAT config from here
       on, so every later call inherits the namespace through `--config`.
    2. **Base branch** (ADR-0042) — which branch does every PR of this run land
       on? Never assumed: `--base-branch <name>` is the human's answer at this
       launch. Without it the result's `base` is `{"status": "ask", "recorded",
       "default_branch"}` — what the remote has on record from the last launch,
       and the repo's default branch — questions 3 and 4 are not asked, and the
       probe is run again with the answer. With it, the answer is checked
       (`afk_decide.base_refusal`), put on record on the remote, and returned in
       `config`: `base` is `{"status": "settled", "branch", …}`.
    3. **Branch protection** (only when `gate.ci: local`, ADR-0012) — does the merge
       target REQUIRE status checks? Then `gh pr merge` is rejected however green the
       local gate is, so that combination is a hard `error` at bootstrap; an
       inconclusive read is a `warn`. Where merge batches form, so is a target
       that refuses a direct push — a merge batch lands by pushing (ADR-0029).
    4. **Gate records** (only when `gate.ci: local`, ADR-0030) — can a green gate
       run be put on record on the remote? A `warn` if not: every landing then
       runs the gate itself. Records past their day are swept here."""
    run = _run(a)
    cfg = run.cfg
    ns, rejection = _usable_namespace(run.rem, cfg["claim_namespace"], run.now())
    cfg["claim_namespace"] = ns
    result = {"blocked": rejection is not None, "config": cfg}
    if rejection:
        result["detail"] = rejection

    result["base"] = _settle_base(run, a.base_branch)
    if result["base"]["status"] == "ask":
        return result       # nothing is checked against a base nobody confirmed

    ci_mode, target = cfg["gate"]["ci"], _base(cfg)
    if ci_mode == "local":
        if not run.repo:
            result["protection"] = {"branch": target, "verdict": "warn", "required_checks": [],
                                    "detail": "gate.ci is 'local' but --repo was not given, so "
                                              "branch protection could not be checked"}
        else:
            prot, unavailable = _branch_protection(run.repo, target)
            result["protection"] = {"branch": target,
                                    **afk_decide.protection_verdict(ci_mode, prot, unavailable,
                                                                    batch=afk_decide.batches_form(cfg))}
        result["gate_records"] = _probe_gate_records(run.rem, run.now())
    return result


def _settle_base(run: _Run, answer: str | None) -> Obj:
    """The base branch of this run (`cmd_probe`'s second question), written into
    `run.cfg` → `base`. With no `answer` it is only read: the config carries what
    the remote has on record — none: "" — which is enough to look (`--plan`) and
    never to launch on. With one, the record on the remote becomes the answer —
    or the probe fails, saying why it cannot (`afk_decide.base_refusal`)."""
    rem, ref = run.rem, afk_decide.CLAIM_NAMESPACES[run.cfg["claim_namespace"]][2]
    was = _remote_sha(rem, ref)
    record = None
    if was:
        _git(["fetch", "--quiet", "--no-tags", rem, ref])
        record = _read_record(afk_decide.BASE_RECORD, "FETCH_HEAD")
    recorded = record["branch"] if record else None
    default = _gh(["repo", "view", run.repo, "--json", "defaultBranchRef",
                   "--jq", ".defaultBranchRef.name"]).stdout.strip() if run.repo else None
    found = {"recorded": recorded, "default_branch": default}
    if answer is None:
        run.cfg["base_branch"] = recorded or ""
        return {"status": "ask", **found}
    now = run.now()
    claims, heartbeats = _scan(run)
    live = [i for i, ts in heartbeats.items()
            if not afk_decide.is_stale(ts, now, afk_decide.CLAIM_LEASE_TTL_SECONDS)]
    refusal = afk_decide.base_refusal(answer, recorded, _remote_heads(rem), len(claims), live)
    if refusal:
        raise ValueError(refusal)
    if answer != recorded:
        sha = _record_commit(afk_decide.BASE_RECORD, {"branch": answer, "ts": now})
        # only while the record is still the one read: two launches answering at once
        p = _git(["push", rem, f"--force-with-lease={ref}:{was}", f"{sha}:{ref}"], check=False)
        if p.returncode != 0:
            raise RuntimeError(f"the base branch's record ({ref}) moved while this launch was "
                               f"settling it — another launch answered too. Run the probe "
                               f"again: {p.stderr.strip()}")
    run.cfg["base_branch"] = answer
    return {"status": "settled", "branch": answer, **found}


def _probe_gate_records(rem: str, now: int) -> Obj:
    """Can a gate run be put on record on this remote, and which records have
    expired → {"verdict": "ok"|"warn", "pruned", "detail"}. Never an error: a
    remote that refuses the records only costs every landing its own run of the
    gate — worth a word with the human present, not worth stopping a launch."""
    ns = afk_decide.GATE_RECORD_NAMESPACE
    own, p = _push_probe(rem, ns, now)
    if p.returncode != 0:
        return {"verdict": "warn", "pruned": 0,
                "detail": f"the remote refuses refs under {ns} ({p.stderr.strip()}): no gate "
                          f"run can be put on record, so every landing runs the gate itself"}
    probes, expired = [own], []
    if _git(["fetch", "--quiet", "--no-tags", "--prune", rem, f"+{ns}/*:{_LOCAL_GATE}/*"],
            check=False).returncode == 0:
        for name in _git(["for-each-ref", "--format=%(refname)", _LOCAL_GATE]).stdout.split():
            ref = f"{ns}/{name[len(_LOCAL_GATE) + 1:]}"
            record = _read_record(afk_decide.GATE_RUN_RECORD, name)
            _git(["update-ref", "-d", name], check=False)
            if ref.startswith(f"{ns}/{_PROBE_LEAF}"):
                probes.append(ref)      # a probe's ref, ours or one left behind: no record
            elif afk_decide.gate_record_void(record, now):
                expired.append(ref)
    _git(["push", "--quiet", rem, "--delete", *dict.fromkeys(probes + expired)], check=False)
    return {"verdict": "ok", "pruned": len(expired),
            "detail": f"gate runs are put on record under {ns}"}


def _login_shell(script: str, timeout: int = 20) -> str:
    """Run `script` in the user's INTERACTIVE login shell — the only place aliases
    and rc-defined functions exist, and exactly the shell orca gives a worker
    (verified: orca terminals run `zsh -l` with `-i` set). Never raises: a shell
    that is missing, slow, or noisy must degrade to "unknown", not abort bootstrap.

    Empty on a NON-ZERO exit, which is the whole point for `type`: zsh prints
    "ckim not found" on **stdout** and exits 1, so trusting stdout alone would
    wave the one typo this check exists to catch straight through to dispatch."""
    shell = os.environ.get("SHELL") or "/bin/zsh"
    try:
        p = subprocess.run([shell, "-ic", script], capture_output=True, text=True,
                           timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return ""
    return p.stdout if p.returncode == 0 else ""


def cmd_worker_command(a: argparse.Namespace) -> Obj:
    """Settle the one command every worker is started with, so a launcher on a
    custom provider does not dispatch workers that silently fall back to stock
    Anthropic (ADR-0010), and a qoderclicn launcher dispatches qoderclicn workers
    (ADR-0014).

    Bare: `stock` (nothing to ask) or `ask` — with the Claude-starting aliases
    found in the user's login shell, so the human picks rather than types.
    `--check "<cmd>"` resolves the human's answer's first word in that same shell
    and reports whether it runs at all, plus whether an unattended flag is visible.

    The command is OPAQUE: never parsed, never composed, never appended to. That is
    what keeps every credential inside whatever wrapper the human already trusts —
    the fleet copies no env, writes no file, and puts no key on any command line."""
    fw = afk_decide.first_word(a.check)
    resolved = _login_shell(f"type -- {shlex.quote(fw)}") if fw else None
    result = afk_decide.resolve_worker_command(
        os.environ.get("ANTHROPIC_BASE_URL"), a.check, resolved,
        afk_decide.detect_runtime(os.environ))
    if result["status"] == "ask":
        result["candidates"] = afk_decide.launch_candidates(
            afk_decide.parse_aliases(_login_shell("alias")))
    return result


# --------------------------------------------------------------------------- #
# the cycle: gate, tick, pace                                                  #
# --------------------------------------------------------------------------- #

# What `_gather` reads: the open issues, the open PRs, the claims, the heartbeats.
_Gathered = tuple[list[Issue], list[PullRequest], list[Claim], dict[str, int]]


def _gather(run: _Run, fresh: bool = False) -> _Gathered:
    """The ONE gatherer of the observable fleet inputs (ADR-0008): open issues,
    open PRs, and the claim/heartbeat ref scan. Both `rebuild` and the `cycle`
    gate read through here, so their views cannot drift. The three reads do not
    depend on each other, so they are made at once: a gather costs the slowest
    of them, not their sum. The raw JSON lives and dies in this process.
    `fresh`: read now — nothing this process read before is kept."""
    if fresh:
        _forget()
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        issues = pool.submit(_open_issues, run.repo)
        prs = pool.submit(_open_prs, run.repo)
        scan = pool.submit(_scan, run)
        return issues.result(), prs.result(), *scan.result()


def cmd_cycle(a: argparse.Namespace) -> Obj:
    """One cycle, whole, in one call (ADR-0017): digest what a rebuild would
    observe (ADR-0007) → tick-or-skip, and on a tick RUN it — the rebuild and
    every transition whose next step is a table lookup (`_tick`) — then fold
    what it did into the state and return the sleep.

      {"action": "tick"|"skip", "reason", "state", "sleep_seconds", "progress",
       "judgments": [...]}        (+ "heartbeat" on a skip that holds claims,
                                   "errors" when a transition of the tick failed)

    `state` is opaque to the caller: it hands back the last one verbatim, and
    after the first cycle that is where the instance id and the worker launch
    command come from. No `--state` is a first cycle, which always ticks. The
    state a tick hands back holds the digest of the fleet as that tick LEFT it —
    gathered again after its last write — so its own boards, refs and markers do
    not cause the next tick. A PR of one of my claims that opened meanwhile is
    inside that digest unseen, and that is said: the state is left unsettled and
    `sleep_seconds` is 0, so the next cycle ticks at once and gives it its turn.
    Anything else that happened meanwhile is what `--wake` and the forced tick
    are for (ADR-0007). What the
    tick could not decide comes back as `judgments`, each with the `afk` command
    for either answer; the caller runs one and opens the next cycle at once
    (`sleep_seconds` is then 0). Only that ever reaches a context — the raw
    issue/PR/ref JSON lives and dies here.

    `--drain` is the last cycle of a run, the launcher's stop: no gate and no
    tick — `_drain` releases my claims that no open PR stands behind and keeps
    the rest → `"action": "drain"`, `sleep_seconds` null."""
    run = _run(a)
    state = afk_decide.cycle_state(json.loads(a.state) if a.state else None,
                                   a.instance, a.worker_command)
    instance = state["instance"]
    agent = _Agent(command=state["worker_command"], ready_timeout=a.ready_timeout)
    if a.drain:
        released, kept, errors = _drain(run, instance, _rebuild(run, instance))
        return {"action": "drain", "reason": "stop",
                **afk_decide.cycle_drained(state, released, kept, len(errors)),
                "judgments": [], **({"errors": errors} if errors else {})}
    gathered = _gather(run)
    fp = afk_decide.fingerprint(*gathered[:3])      # heartbeats: see fingerprint
    woke = afk_decide.cycle_wake(state, fp, woke=a.wake)
    if woke["action"] == "skip":
        if not woke.pop("heartbeat"):
            return {**woke, "judgments": []}
        return {**woke, "judgments": [],
                "heartbeat": _beat(run, instance)}
    run.boards.update({int(n): key for n, key in state["boards"].items()})
    ws = _rebuild(run, instance, gathered)
    did, judgments, errors = _tick(run, instance, a.host, agent, ws)
    left, unseen = None, []
    try:
        after = _gather(run, fresh=True)
        left = afk_decide.fingerprint(*after[:3])
        unseen = afk_decide.unseen_prs(ws["mine"], after[1])
    except (OSError, RuntimeError):
        pass            # keep the opening digest: the next cycle reads `changed`, and ticks
    return {"action": "tick", "reason": woke["reason"],
            **afk_decide.cycle_ticked(woke["state"], did, len(judgments), len(errors),
                                      left=left, boards=dict(run.boards), unseen=len(unseen)),
            "judgments": judgments, **({"errors": errors} if errors else {})}


def _drain(run: _Run, instance: str, ws: WorkingSet) -> tuple[list[int], list[int], list[Obj]]:
    """The stop: release every claim of mine that no open PR stands behind — a
    worker still coding, an orphan, a claim that outlived its issue — and keep
    the rest → (released, kept, errors). A kept claim's PR is landed by a peer,
    or a later run, once this instance's lease lapses; a release that failed is
    recorded in `errors` and its claim counted as kept, which it is. Nothing
    is dispatched, granted or failed, and no worker is touched: the ones in
    flight finish on their own."""
    released, kept, errors = [], [], []
    for row in ws["mine"]:
        if row["pr"] and row["status"] != "closed":
            kept.append(row["number"])
            continue
        try:
            _release_mine(run, instance, row["number"])
            released.append(row["number"])
        except _FAILURES as e:
            errors.append({"step": "release", "issue": row["number"], "error": str(e)})
            kept.append(row["number"])
    return released, kept, errors


def _tick(run: _Run, instance: str, host: str, agent: _Agent,
          ws: WorkingSet) -> tuple[Obj, list[Obj], list[Obj]]:
    """One reconciliation pass over the working set `ws`, in code → (did,
    judgments, errors). What it runs, and in what order, is not decided here:
    `afk_decide.tick_plan` hands out one step at a time and is told how each
    ended, and this carries each one out — the table below is every step a plan
    can name and the transition that performs it. No rule about what comes
    next, no slot count and no tally lives on this side.
    `run.boards` is left remembering only the claims the tick ended holding.

    Each transition is the function its subcommand calls, so the tick and a
    human typing `afk park` run one code path. One that fails — whatever it
    raised — is answered as a failure, never raised past the plan: the plan
    records it in `errors`, settles nothing, and goes on.

    Workers are started at once, not one by one: each start is begun in turn —
    claim, worktree, the agent's terminal — and then every agent is waited for
    and handed its prompt together (`finish`), so filling N slots takes about
    as long as filling one. Re-entrant like any tick: killed at any point, the
    next one rebuilds from GitHub."""
    cfg = run.cfg
    finish = {}                         # issue → the rest of the start begun for it

    def answered(fn: Callable[..., Any], **args: Any) -> Answer:
        """One step's answer, as the plan reads it: (result, None), or (None, why it failed)."""
        try:
            return fn(**args), None
        except Exception as e:      # whatever it is: the rest of the tick is still owed
            return None, str(e) if isinstance(e, _FAILURES) else f"{type(e).__name__}: {e}"

    def begin(issue: int) -> StartOutcome:
        start = _begin_dispatch(run, instance, host, agent, issue, start="auto")
        finish[issue] = start.finish
        return start.outcome

    def finish_all(issues: list[int]) -> list[Answer]:
        """Each start's own answer, in order: one may fail where the others ran."""
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(issues)) as pool:
            return list(pool.map(lambda n: answered(finish[n]), issues))

    steps = {
        "no-pr": lambda issues=None, batch=None: _workers_seen(run, numbers=issues, batch_id=batch),
        "turn": lambda issue: _grant_turn(run, instance, agent, issue),
        "batch-turn": lambda: _turn_batch(run, instance, agent, working_set=ws),
        "abandon": lambda batch: _abandon_batch(run, instance, batch),
        "restart": lambda issue: _grant_turn(run, instance, agent, issue, restart=True),
        "nudge": lambda issue=None, batch=None: _nudge_worker(run, instance, number=issue,
                                                              batch_id=batch),
        "park": lambda issue: _park_claim(run, instance, issue),
        "fail": lambda issue, reason: _fail_claim(run, instance, agent, issue, reason=reason),
        "escalate": lambda issue, reason: _escalate_claim(run, instance, issue, reason=reason),
        "release": lambda issue, expect_sha=None: _release_claim(run, instance, issue,
                                                                 expect_sha=expect_sha),
        "reclaim": lambda issue, sha: _force_take(run, issue, sha, instance, host),
        "begin": begin,
        "finish": finish_all,
        "heartbeat": lambda: _beat(run, instance),
        "status": lambda issue, phase, pr, attempt: _upsert_board(run, issue, phase, instance=instance, pr=pr, attempt=attempt),
        "sweep": lambda live: _sweep_batches(run, instance, live),
    }

    def carry_out(step: Obj) -> Answer:
        return answered(steps[step["do"]], **{k: v for k, v in step.items() if k != "do"})

    call: Call = {"afk_path": os.path.abspath(__file__), "repo": run.repo, "instance": instance,
            "worker_command": agent.command, "config": json.dumps(cfg, ensure_ascii=False)}
    done = afk_decide.follow(afk_decide.tick_plan(ws, call, cfg), carry_out)
    for number in set(run.boards) - done["held"]:
        del run.boards[number]
    return done["did"], done["judgments"], done["errors"]


# --------------------------------------------------------------------------- #
# observation: rebuild / no-pr / recovery                                      #
# --------------------------------------------------------------------------- #

def _rebuild(run: _Run, instance: str, gathered: _Gathered | None = None) -> WorkingSet:
    """The working set (`afk_decide.assemble_working_set`), from `gathered` — a
    `_gather` the caller already made — or a fresh one. The frontier costs no
    read of its own: every issue's open-blocker count came with the issue list.
    A per-issue state read is paid only by a claim whose issue is missing from
    the open list, and the landing-turn read only by a claim of mine that has a PR.
    The target is fetched once when a claim of mine has an open issue and no
    open PR, and asked about each such claim in this repo (`_cut_landing`)."""
    cfg = run.cfg
    issues, prs, claims, heartbeats = gathered or _gather(run)
    listed = {i["number"] for i in issues}
    closed = [c["number"] for c in claims
              if c["number"] not in listed and _issue_state(run.repo, c["number"]) == "closed"]
    landed = [c["number"] for c in claims
              if c["instance"] == instance and _cut_landing(run, c)]
    return afk_decide.assemble_working_set(
        issues, prs, claims, heartbeats, instance, run.now(), cfg, closed=closed,
        turns=_claim_turns(run.repo, prs, claims, instance), landed=landed)


def cmd_rebuild(a: argparse.Namespace) -> WorkingSet:
    """One read-only call → the tick's whole working set (ADR-0008) — what a tick
    acts on, and what `--plan` prints instead. Strictly observation: nothing here
    writes a ref, a comment, or a PR."""
    return _rebuild(_run(a), a.instance)


# --------------------------------------------------------------------------- #
# the worker of an issue, or of a merge batch                                  #
# --------------------------------------------------------------------------- #
#
# The one place orca is asked about a worker, an issue's and a merge batch's
# alike: where its worktree is, what it is doing, and everything the fleet does
# to it. No transition types an orca command. How orca is read follows from what
# the answer is for, so no caller picks:
#
#   what a worker is DOING    `_Workers` — HARD. Its answer may be "the worker is
#                             gone", and an orca that cannot be asked, read that
#                             way, starts a second worker beside a live one
#                             (ADR-0021): it is an error instead.
#   WHERE a worktree is, to   `_Worktree.of_issue`, `.of_batch`, `.of_batches` —
#   act on it or recover      SOFT. No orca is "no worktree here": a recovery has
#   from it                   tiers that need none (ADR-0011).
#   what is DONE to a worker  `_Worktree.cut`, `.put`, `.tell`, `.screen` — HARD:
#                             a worker cannot be started or told without orca
#                             (ADR-0005). `.remove` alone reports instead of
#                             raising: what it cleans up after is already durable.

def _on_disk(path: str | None) -> str | None:
    """`path` when it is a directory on this machine's disk, else None — the one
    place a path orca remembers but the disk no longer has reads as "no worktree
    here"."""
    return path if path and os.path.isdir(path) else None


def _batch_path(rows: list[Obj], repo: str, batch: str) -> str | None:
    """The path of a merge batch's worktree that is really on this machine's
    disk, None when there is none — from orca's worktree `rows` however they
    were read."""
    return next((path for hit in afk_decide.batch_worktrees(rows, repo, batch=batch)
                 for path in [_on_disk(hit["path"])] if path), None)


_WORKER_BRIEF = "afk-worker-prompt.md"
_BRIEF_POINTER = ("Your task brief is the file {brief} — read it now and carry it out end to end. "
                  "It is my instruction to you; do not ask me to confirm.")
_NUDGE_MARK = "afk-nudge.json"

# How long orca is given to say a terminal is idle; it answers at once when it is.
_TUI_IDLE_PROBE_MS = 2000


def _worker_file(path: str, name: str) -> str:
    """A fleet-private file about the worker in a worktree, kept in the worktree's
    own git dir: never staged by the worker's `git add -A`, gone when the worktree
    is."""
    git_dir = _git(["-C", path, "rev-parse", "--absolute-git-dir"]).stdout.strip()
    return os.path.join(git_dir, name)


class WorkerNotTold(RuntimeError):
    """The worker's terminal did not accept what it was told."""


class _Worktree:
    """The worktree of one worker on this machine — an issue's or a merge
    batch's — and everything the fleet does to the worker in it: tell it one
    line (`tell`), put a new worker in it on a brief (`put`), read where it
    stopped (`screen`), remove it (`remove`).

    `path` is the worktree on this disk, None when there is none here;
    `remembered` is the path orca (or whoever named it) gave, kept for the
    callers that must still see a directory the disk has lost: removing it,
    reporting it. `branch` is the branch orca named for it, where orca was the
    one asked; `batch` the merge batch it is the worktree of, if any."""

    def __init__(self, path: str | None, branch: str | None = None,
                 batch: str | None = None) -> None:
        self.remembered, self.path = path, _on_disk(path)
        self.branch, self.batch = branch, batch

    # --- where it is -------------------------------------------------------

    @staticmethod
    def _rows() -> list[Obj]:
        """Orca's worktree rows, or [] when orca cannot be asked. SOFT by design:
        the worktree signal is one input to a tiered recovery whose last tier
        needs no orca at all, so a machine without orca (or a momentarily unhappy
        one) must degrade to "no local worktree", never abort a recovery."""
        try:
            return list(_orca(["worktree", "list"], timeout=30).get("worktrees") or [])
        except (RuntimeError, TypeError, AttributeError):
            return []

    @classmethod
    def of_issue(cls, repo: str, number: int) -> _Worktree:
        """The worktree orca remembers here for issue <number>."""
        hit = afk_decide.find_orca_worktree(cls._rows(), number, repo)
        return cls(hit["path"], hit["branch"])

    @classmethod
    def of_batch(cls, repo: str, batch: str) -> _Worktree:
        """The worktree of merge batch `batch` that is really on this disk."""
        return cls(_batch_path(cls._rows(), repo, batch), batch=batch)

    @classmethod
    def of_batches(cls, repo: str, instance: str) -> list[_Worktree]:
        """Every worktree orca remembers here for a merge batch of `instance`,
        whether or not the disk still has it."""
        return [cls(hit["path"], hit["branch"], batch=hit["batch"])
                for hit in afk_decide.batch_worktrees(cls._rows(), repo, instance=instance)]

    @classmethod
    def at(cls, path: str | None) -> _Worktree:
        """The worktree at a path named by hand, or already known."""
        return cls(path)

    @staticmethod
    def source(repo: str) -> Obj | None:
        """The checkout orca cuts `repo`'s worktrees from → {"id", "path"}, or
        None when orca knows no such repo. It is also the fleet's own checkout:
        the one the launcher runs in."""
        return afk_decide.find_orca_repo(_orca(["repo", "list"]).get("repos"), repo)

    @classmethod
    def cut(cls, run: _Run, at_branch: str, issue: IssueRead | None = None,
            batch: str | None = None) -> _Worktree:
        """Have orca create a worktree + branch at the REMOTE's current tip of
        `at_branch` (ADR-0005: orca owns both, and names the branch) — for
        `issue`, linked to it, or for merge batch `batch`, linked to none. The
        tip is fetched into the checkout orca cuts worktrees from and handed over
        as a sha rather than a branch name — and then ASSERTED: the worktree must
        contain it, however orca resolved the ref. A worker started on a base
        that is commits behind builds on files that have already moved."""
        if issue is not None:
            name = afk_decide.worktree_name(issue["number"],
                                            issue["title"])
        elif batch is not None:
            name = afk_decide.batch_name(batch)
        else:
            raise ValueError("a worktree is cut for an issue or for a merge batch")
        orca_repo = cls.source(run.repo)
        if not orca_repo:
            raise RuntimeError(f"orca knows no repo for {run.repo} — add this checkout once with "
                               f"`orca repo add --path <path>`")
        sha = _fetch_tip(run.rem, at_branch, cwd=orca_repo["path"])
        wt = _orca(["worktree", "create", "--repo", f"id:{orca_repo['id']}", "--name", name,
                    "--no-parent", "--base-branch", sha,
                    *(["--issue", str(issue["number"])] if issue is not None else [])
                    ]).get("worktree") or {}
        path, branch = wt.get("path"), wt.get("branch")
        if not path or not os.path.isdir(path):
            raise RuntimeError(f"orca worktree create returned no usable path: {path!r}")
        if _git(["-C", path, "merge-base", "--is-ancestor", sha, "HEAD"],
                check=False).returncode != 0:
            _git(["-C", path, "merge", "--ff-only", sha])
        return cls(path, afk_decide.short_branch(branch), batch=batch)

    @property
    def _here(self) -> str:
        """`path`, for what is only ever done in a worktree that is on this disk."""
        if self.path is None:
            raise RuntimeError(f"no worktree on this machine at {self.remembered!r}")
        return self.path

    def checked_out(self) -> str:
        """The branch checked out in it now, whatever orca named it at first."""
        return _git(["-C", self._here, "rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()

    # --- what the worktree remembers of its worker ---------------------------

    @property
    def brief(self) -> str | None:
        """The file the worker here was last briefed with, None when there is none."""
        brief = _worker_file(self._here, _WORKER_BRIEF)
        return brief if os.path.exists(brief) else None

    def write_brief(self, prompt: str) -> str:
        """Write the worker's instructions to the worktree's brief file → its
        path. A worker under a new brief — a new worker, or one just given its
        landing turn — has not been nudged, whatever came before."""
        brief = _worker_file(self._here, _WORKER_BRIEF)
        with open(brief, "w") as f:
            f.write(prompt)
        mark = _worker_file(self._here, _NUDGE_MARK)
        if os.path.exists(mark):
            os.remove(mark)
        return brief

    @property
    def nudge(self) -> Obj | None:
        """The nudge recorded for the worker here → {"at", "tail"}, or None when
        it was never nudged (or there is no worktree to have recorded one in)."""
        if not self.path:
            return None
        try:
            with open(_worker_file(self.path, _NUDGE_MARK)) as f:
                return json.load(f)
        except (OSError, ValueError, RuntimeError):
            return None

    def record_nudge(self, at: int, tail: list[str] | None) -> None:
        """Record that the worker here was nudged at `at`, its screen then `tail`."""
        with open(_worker_file(self._here, _NUDGE_MARK), "w") as f:
            json.dump({"at": at, "tail": tail}, f)

    # --- the worker in it --------------------------------------------------

    @functools.cached_property
    def terminal(self) -> str | None:
        """The handle of the live worker terminal here — the one that spoke last,
        when a worktree somehow has several — or None: no worker is here."""
        if not self.path:
            return None
        rows = _orca(["terminal", "list", "--worktree", f"path:{self.path}"]).get("terminals") or []
        live = [t for t in rows if t.get("connected", True) and t.get("writable", True)]
        return max(live, key=lambda t: t.get("lastOutputAt") or 0)["handle"] if live else None

    def seems_idle(self) -> bool:
        """Does orca see the worker terminal here idle? Its own reading of the
        terminal (title, prompt), for a runtime that reports no state. True when
        it answers within the probe, False when the probe times out — the worker
        is at it. No live terminal reads as idle: there is nothing busy to wait
        for."""
        if self.terminal is None:
            return True
        try:
            wait = _orca(["terminal", "wait", "--terminal", self.terminal, "--for", "tui-idle",
                          "--timeout-ms", str(_TUI_IDLE_PROBE_MS)]).get("wait") or {}
        except OrcaError as e:
            if e.code == "timeout":
                return False
            raise
        return bool(wait.get("satisfied"))

    def screen(self) -> list[str] | None:
        """The last lines of the worker's rendered screen, bounded; None when no
        worker is here. Read for ONE purpose: saying where a silent worker
        stopped (ADR-0018) — never for its result, which is only ever a PR or a
        verdict marker."""
        if self.terminal is None:
            return None
        shown = _orca(["terminal", "read", "--terminal", self.terminal, "--screen",
                       "--limit", str(afk_decide.STALL_TAIL_LINES * 2)]).get("terminal") or {}
        return afk_decide.stall_tail(shown.get("tail"))

    def tell(self, line: str, what: str) -> str:
        """Say one line to the worker here → its terminal's handle. Sent WITH
        `--enter`: typed but unsubmitted, a worker sits idle forever,
        indistinguishable from one that finished. `what` names the line in the
        one error a terminal that did not take it raises."""
        terminal = self.terminal
        if terminal is None:
            raise RuntimeError(f"no live terminal in {self.path} to take {what}")
        sent = _orca(["terminal", "send", "--terminal", terminal, "--text", line,
                      "--enter"]).get("send") or {}
        if not sent.get("accepted"):
            raise WorkerNotTold(f"terminal {terminal} did not accept {what}")
        return terminal

    def put(self, agent: _Agent, prompt: str) -> Callable[[], str]:
        """Put a new worker in this worktree, on `prompt` → the rest of its start,
        to call: wait for the agent until its TUI is idle, then submit its prompt
        → its terminal's handle. A start is split there so that a tick opens
        every terminal it will, and then waits on them all at once.

        Whatever terminal was here first is closed, so the worker's is the only
        one in the worktree: an agent — dead or idle, two in one worktree would
        fight — or the bare shell orca opens in every worktree it cuts, which
        would otherwise be the tab the worktree opens on. The new one is started with the run's OPAQUE worker
        launch command (never `--agent`, ADR-0010), and the prompt goes to a
        brief FILE with only a one-line pointer for the agent — a whole prompt
        sent as text arrives as one paste, which the agent reads as quoted
        material and asks to have confirmed instead of starting."""
        try:
            _orca(["terminal", "close", "--worktree", f"path:{self.path}", "--all"])
        except RuntimeError:
            pass
        brief = self.write_brief(prompt)
        term = _orca(["terminal", "create", "--worktree", f"path:{self.path}",
                      "--command", agent.command]).get("terminal") or {}
        handle = term.get("handle")
        if not handle:
            raise RuntimeError("orca terminal create returned no terminal handle")
        self.terminal = handle
        ready_timeout = agent.ready_timeout

        def submit() -> str:
            wait = _orca(["terminal", "wait", "--terminal", handle, "--for", "tui-idle",
                          "--timeout-ms", str(ready_timeout * 1000)],
                         timeout=ready_timeout + 30).get("wait") or {}
            if not wait.get("satisfied"):
                raise RuntimeError(f"the worker in {self.path} was not ready for a prompt within "
                                   f"{ready_timeout}s (terminal {handle})")
            return self.tell(_BRIEF_POINTER.format(brief=brief), "the worker prompt")
        return submit

    def remove(self) -> Obj:
        """Have orca remove the worktree (and its terminals). Soft: by the time
        this runs the transition that mattered — a merge, a close — is already
        durable, so a failed cleanup is reported, never raised."""
        try:
            _orca(["worktree", "rm", "--worktree", f"path:{self.remembered}", "--force"])
            return {"removed": True, "path": self.remembered}
        except RuntimeError as e:
            return {"removed": False, "path": self.remembered, "detail": str(e)}


# Far above any one machine's worktree count: a page that stops short is an error.
_PS_LIMIT = 10000


class _Worker:
    """One worker as `_Workers` saw it: where its worktree is on this machine,
    and what it is doing."""

    def __init__(self, path: str | None, reading: WorkerReading, now: int, grace: int) -> None:
        self.path = path            # its worktree, on this disk — None: none here, so no worker
        self.reading = reading      # `afk_decide.read_worker_state`: busy, idle or none
        self.now, self.grace = now, grace  # the clock and the grace period it was read with

    @property
    def busy(self) -> bool:
        return self.reading["terminal"] == "busy"

    @functools.cached_property
    def nudged_at(self) -> int | None:
        """When this worker was nudged, None when it never was."""
        return (_Worktree.at(self.path).nudge or {}).get("at")

    @property
    def settled(self) -> Seen | None:
        """Its classification when the reading alone decides it — busy, or gone
        (`afk_decide.settled_by_worker_state`) — else None: only then is anything
        in git or on GitHub worth reading for it."""
        return afk_decide.settled_by_worker_state(self.reading, self.now, self.grace,
                                                  self.nudged_at)


class _Workers:
    """What the workers on this machine are doing, as their runtimes reported it
    to orca (ADR-0021) — one read of orca for every worker then asked after, an
    issue's (`of_issue`) or a merge batch's (`of_batch`).

    HARD, and a truncated `ps` page is an error too: an orca that cannot be asked
    is never "no worktree" or "no worker there"."""

    def __init__(self, run: _Run) -> None:
        self._repo = run.repo
        self.now, self.grace = run.now(), afk_decide.WORKER_IDLE_GRACE_SECONDS

    @functools.cached_property
    def _rows(self) -> list[Obj]:
        return _orca(["worktree", "list"]).get("worktrees") or []

    @functools.cached_property
    def _states(self) -> dict[str, Obj]:
        ps = _orca(["worktree", "ps", "--limit", str(_PS_LIMIT)])
        if ps.get("truncated"):
            raise RuntimeError(f"orca worktree ps truncated at {_PS_LIMIT} rows")
        return {r.get("path"): r for r in ps.get("worktrees") or []}

    def of_issue(self, number: int) -> _Worker:
        """The worker on issue <number>."""
        return self.at(afk_decide.find_orca_worktree(self._rows, number, self._repo)["path"])

    def of_batch(self, batch: str) -> _Worker:
        """The batch worker of merge batch `batch`."""
        return self.at(_batch_path(self._rows, self._repo, batch))

    def at(self, path: str | None) -> _Worker:
        """The worker in the worktree at `path`, wherever that path came from: one
        the disk does not have holds no worker."""
        path = _on_disk(path)
        row = self._states.get(path) if path else None
        reading = afk_decide.read_worker_state(row, self.now, self.grace)
        if reading["terminal"] != "none" and reading["state"] is None:
            # a runtime that reports nothing: ask orca whether its terminal is idle
            reading = afk_decide.read_worker_state(row, self.now, self.grace,
                                                    _Worktree.at(path).seems_idle())
        return _Worker(path, reading, self.now, self.grace)


def cmd_no_pr(a: argparse.Namespace) -> Obj:
    """For each of my claims waiting on its worker — no PR yet, or a landing turn
    not yet landed — is the worker still at it, or did it stop, and why? One call
    for every such claim of the tick.

    The worker state is read first, from orca's own record of what the worker's
    runtime reported (ADR-0021): a worker that is busy, or one that is gone, is
    decided from that alone — nothing on GitHub or in git is read for it. Only a
    worker that stopped gets the rest gathered: the worktree's git progress, its
    `afk:verdict` marker, the standing of each issue that marker says it is blocked
    by (`blockers`), and when it was last told something (`nudged_at`,
    `turn_at`). For a busy or gone worker `turn_at` is therefore null because it
    was NOT READ — never evidence that its PR holds no turn; the claim's `status`
    in `afk rebuild` is what says that.
    Returns {"workers": [the outcome and action the worker's classification
    comes to (`afk_decide.WORKER_CAUSES`) plus those signals and the
    `worker_state` it was read from, one per --issue, in order]}.

    `--batch <batch>` asks the same of a merge batch's worker (`_batch_worker`):
    one row, with `batch` in place of `issue`."""
    return {"workers": [{k: v for k, v in w.items() if k != "cause"}
                        for w in _workers_seen(_run(a), a.numbers, batch_id=a.batch_id,
                                               worktree=a.worktree)["workers"]]}


def _workers_seen(run: _Run, numbers: list[int] | None = None, batch_id: str | None = None,
                  worktree: str | None = None) -> Obj:
    """`afk no-pr`'s rows as the tick reads them — for the claims on `numbers`,
    or for the worker of merge batch `batch_id`: each also carries the `cause`
    its worker was classified with, which is what the tick routes on
    (`afk_decide.worker_step`, `batch_step`)."""
    if bool(numbers) == bool(batch_id):
        raise ValueError("afk no-pr takes --issue <n> (repeatable), or --batch <batch>")
    numbers = numbers or []
    if worktree is not None and not batch_id:
        if len(numbers) != 1:
            raise ValueError("--worktree names one worker's worktree: give it with one --issue")
        if not _on_disk(worktree):
            raise ValueError(f"worktree not found: {worktree} (omit --worktree to let orca find it)")
    workers = _Workers(run)
    if batch_id:
        return {"workers": [_batch_worker(run, batch_id, workers.of_batch(batch_id))]}
    seen = []
    for number in numbers:
        worker = workers.of_issue(number) if worktree is None else workers.at(worktree)
        seen.append({"issue": number, **_worker_outcome(run, number, worker),
                     "worker_state": worker.reading["state"]})
    return {"workers": seen}


def _worker_outcome(run: _Run, number: int, worker: _Worker) -> WorkerRow:
    """One worker's classification, gathering only what its reading leaves open:
    busy or gone is settled by the reading alone (`_Worker.settled`), so it costs
    no git and no GitHub."""
    cfg, path, nudged_at = run.cfg, worker.path, worker.nudged_at
    settled = worker.settled
    if settled:
        return {**settled, "worktree": path, "progress": None, "worker_verdict": None,
                "blockers": [], "nudged_at": nudged_at, "turn_at": None}
    now, grace = worker.now, worker.grace
    progress = _worktree_progress(path, run.rem, _base(cfg)) if path else None
    declared = afk_decide.latest_verdict(_issue_comments(run.repo, number))
    prs = _open_prs(run.repo)
    blockers = _blocker_standings(run, number, declared["blocked_by"], prs)
    pr = afk_decide.closing_pr(prs, number)
    turn = _turn(run.repo, pr["number"]) if pr else None
    return {**afk_decide.classify_stopped(progress, worker.reading["terminal_idle_seconds"],
                                          declared,
                                          {b["number"]: b["standing"] for b in blockers},
                                          now, grace, nudged_at=nudged_at,
                                          can_nudge=path is not None, turn=turn),
            "worktree": path, "progress": progress, "worker_verdict": declared,
            "blockers": blockers, "nudged_at": nudged_at,
            "turn_at": turn["at"] if turn else None}


# More issues than any real dependency chain holds: a walk that gets here is an
# error, never "no cycle found".
_DEPENDENCY_WALK_LIMIT = 200


def _blocker_standings(run: _Run, number: int, named: list[int],
                       prs: list[PullRequest]) -> list[Standing]:
    """Where each issue a `blocked` verdict names stands
    (`afk_decide.blocker_standings`), gathering what that takes: the blocker
    itself, the claim refs, the open PRs, and — from every blocker still open —
    the chain of open blockers behind it, for the cycle check. `afk no-pr` and
    `afk park` both read through here, so the transition parks exactly what the
    observation said was parkable."""
    if not named:
        return []
    cfg = run.cfg
    blockers = {n: _blocker(run.repo, n) for n in named}
    claims, _ = _scan(run)
    edges = {}
    todo = [n for n, b in blockers.items()
            if b and b["state"] == "open" and not b["pull_request"]]
    while todo:
        n = todo.pop()
        if n == number or n in edges:
            continue
        if len(edges) >= _DEPENDENCY_WALK_LIMIT:
            raise RuntimeError(f"issue #{number}: the dependency chain behind its blockers is "
                               f"longer than {_DEPENDENCY_WALK_LIMIT} issues")
        edges[n] = [e["number"] for e in _blocked_by(run.repo, n) if e["state"] == "open"]
        todo.extend(edges[n])
    return afk_decide.blocker_standings(
        number, named, cfg, blockers=blockers, claimed={c["number"] for c in claims},
        open_pr={n for n in named if afk_decide.closing_pr(prs, n)}, edges=edges)


def cmd_nudge(a: argparse.Namespace) -> Obj:
    """Tell a worker that stopped without an outcome to carry on (`afk no-pr` →
    `idle_stalled` / `nudge`) — the step before failure handling, and one that
    spends no attempt and discards nothing (ADR-0018). Reads the tail of its
    screen (where it stopped), types one line at it, and records the nudge in the
    worktree, so the next silence is a failure and not a second nudge.

      {"issue", "action": "nudged", "terminal", "terminal_tail": [...]}

    `--batch <batch>` nudges a merge batch's worker instead (`batch` in place of
    `issue`): its next silence abandons the batch."""
    return _nudge_worker(_run(a), a.instance, number=a.number, batch_id=a.batch_id,
                         worktree=a.worktree)


def _nudge_worker(run: _Run, instance: str, number: int | None = None,
                  batch_id: str | None = None, worktree: str | None = None) -> Obj:
    """`afk nudge` — of the worker on `instance`'s claim on issue <number>, or of
    the one on merge batch `batch_id`; `worktree` overrides where orca says it is."""
    if number is None and batch_id is not None:
        _require_my_batch(run, instance, batch_id)
        who, found = f"merge batch {batch_id}", functools.partial(_Worktree.of_batch, run.repo,
                                                                  batch_id)
    elif number is not None and batch_id is None:
        _require_mine(run, number, instance)
        who, found = f"issue #{number}", functools.partial(_Worktree.of_issue, run.repo, number)
    else:
        raise ValueError("afk nudge takes exactly one of --issue <n>, --batch <batch>")
    wt = _Worktree.at(worktree) if worktree else found()
    if not wt.path:
        raise RuntimeError(f"{who} has no worktree on this machine — there is no "
                           f"worker here to nudge")
    if wt.nudge is not None:
        raise RuntimeError(f"the worker on {who} was already nudged — a second "
                           f"silence is a failure (`afk fail`, or for a batch `afk turn "
                           f"--abandon`), not another nudge")
    if wt.terminal is None:
        raise RuntimeError(f"no live terminal in {wt.path} — the worker is dead, not stalled "
                           f"(`afk dispatch` continues it)")
    tail = wt.screen()
    handle = wt.tell(afk_decide.nudge_text(wt.brief), "the nudge")
    wt.record_nudge(run.now(), tail)
    return {**({"batch": batch_id} if batch_id else {"issue": number}),
            "action": "nudged", "terminal": handle, "terminal_tail": tail}


def _stalled_reason(repo: str, number: int, reason: str) -> str:
    """`reason`, plus where the worker stopped when this failure follows a nudge
    it never answered: its screen as it is now, else as it was when nudged. Soft —
    a failure is never blocked on reading a terminal."""
    wt = _Worktree.of_issue(repo, number)
    nudge = wt.nudge
    if nudge is None:
        return reason
    try:
        tail = wt.screen()
    except RuntimeError:
        tail = None
    return afk_decide.stall_reason(reason, tail or nudge.get("tail"))


def _recovery(run: _Run, number: int, path: str | None = None, branch: str | None = None,
              no_worktree: bool = False, landing_pr: int | None = None) -> Obj:
    """What survived a dead worker, and the tier it selects (ADR-0011).

    Two signals, both mechanics: (1) is a worktree for this issue still on THIS
    machine — asked of `orca worktree list` (soft: no orca → "no worktree", never
    an abort), overridable with `path` / `no_worktree`; (2) is the issue's branch
    ahead of base on the remote — the branch is recognised from its name
    (the claim ref records the issue, not the branch) and the compare is plain
    git. `afk_decide.select_recovery` then picks the tier.

    Both signals are always gathered, even when the worktree already settles the
    tier: a *pristine* worktree over a branch that carries pushed commits still has
    something to continue, and the honest prompt depends on knowing that."""
    cfg, rem, repo = run.cfg, run.rem, run.repo
    base = _base(cfg)

    # --- tier-1 signal: a worktree for this issue, still on this machine ---
    if path is None and not no_worktree:
        wt = _Worktree.of_issue(repo, number)
        branch = branch or wt.branch
    else:
        wt = _Worktree.at(path)
    present = wt.path is not None
    worktree: WorktreeSignal = {"present": present, "path": wt.remembered}
    if wt.path is not None:
        worktree = {**worktree, **_worktree_progress(wt.path, rem, base)}

    # --- tier-2 signal: the branch the dead worker pushed ---
    candidates = [] if branch else afk_decide.branch_candidates(
        _remote_heads(rem), number)
    ahead = {b: _branch_ahead(rem, b, base, number)
             for b in ([branch] if branch else candidates)}
    branch = branch or afk_decide.furthest_ahead(ahead)
    branch_sig: BranchSignal = {"name": branch, "candidates": candidates,
                                "commits_ahead": ahead.get(branch) if branch else None}

    return {"issue": number, "base": base, "worktree": worktree, "branch": branch_sig,
            **afk_decide.select_recovery(worktree, branch_sig, landing_pr=landing_pr)}


def cmd_recovery(a: argparse.Namespace) -> Obj:
    """Per DEAD claim: does recoverable progress exist, and where? → the tiered
    continuation verdict (ADR-0011), read-only. `afk dispatch` makes this same read
    and acts on it; call this one first only to inspect what it would continue
    from — the tick's "is this state sane to build on" judgment."""
    return _recovery(_run(a), a.number, path=a.worktree, branch=a.branch,
                     no_worktree=a.no_worktree)


# --------------------------------------------------------------------------- #
# act: starting a worker — dispatch                                            #
# --------------------------------------------------------------------------- #

def _discard_attempt(run: _Run, number: int) -> Obj:
    """Throw the previous attempt away, for a FRESH start: close the PRs the fleet
    opened for the issue, delete its work branches on the remote, remove its
    worktree. What a retry means (the previous attempt is the thing that failed),
    and why it is never done to a claim that merely lost its worker (ADR-0011).

    Closing the PR is what keeps the claim from re-entering the retry ladder on
    the same red PR next tick; deleting the branches is what keeps a later
    continuation from resuming the attempt that was discarded."""
    rem = run.rem
    closed = []
    for pr in afk_decide.superseded_prs(_open_prs(run.repo), number):
        _close_pr(run.repo, rem, pr["number"],
                  "afk-fleet: superseded — this attempt failed and the issue is being retried "
                  "from a clean base.")
        closed.append(pr["number"])
    deleted = []
    for branch in afk_decide.branch_candidates(_remote_heads(rem), number):
        _delete_branch(rem, branch)
        deleted.append(branch)
    wt = _Worktree.of_issue(run.repo, number)
    path = wt.remembered
    removed = wt.remove() if path else None
    if removed and not removed["removed"]:
        raise RuntimeError(f"could not remove the previous attempt's worktree {path}: "
                           f"{removed['detail']}")
    return {"closed_prs": closed, "deleted_branches": deleted, "removed_worktree": path}


def _landing_fields(cfg: Config, pr: PullRequest) -> Obj:
    """The LANDING_FIELDS of a landing brief, from the config and the PR."""
    return {"pr": pr["number"], "pr_branch": pr["headRefName"], "target": _base(cfg)}


def _prompt_fields(run: _Run, issue: IssueRead, path: str | None, branch: str | None) -> Obj:
    """The PROMPT_FIELDS of a worker prompt for one issue in one worktree. The
    launcher's terminal is read off the environment, never passed in: the launcher
    runs `afk cycle` itself, so the handle orca gave its terminal is the one
    this process inherited (ADR-0020, ADR-0028). The config travels whole, as the JSON
    `afk land` is run with: the worker lands on the settings the tick ran on."""
    cfg = run.cfg
    return {"n": issue["number"], "title": issue["title"], "repo": run.repo,
            "base_branch": _base(cfg), "local_command": cfg["gate"]["local_command"],
            "afk_path": os.path.abspath(__file__),
            "config": json.dumps(cfg, ensure_ascii=False), "branch": branch,
            "worktree_path": path,
            "launcher_terminal": os.environ.get("ORCA_TERMINAL_HANDLE", "")}


# `afk dispatch --start`: continue from what survives, or discard it first.
_StartFrom = Literal["auto", "fresh"]


def _start_worker(run: _Run, instance: str, agent: _Agent, issue: IssueRead, start: _StartFrom,
                  reason: str | None = None) -> Obj:
    """Put a worker on an issue this fleet already holds the claim for, start to
    finish (`_begin_worker`, then the rest it returns)."""
    return _begin_worker(run, instance, agent, issue, start, reason)()


def _begin_worker(run: _Run, instance: str, agent: _Agent, issue: IssueRead, start: _StartFrom,
                  reason: str | None = None) -> Callable[[], Obj]:
    """Begin putting a worker on an issue `instance` already holds the claim for:
    everything up to its agent's terminal being open. Returns the rest as a
    callable — wait for the agent, submit its prompt, upsert the status board —
    which a tick starting several workers runs for all of them at once.

    `start` is "auto" — continue from whatever progress survives (the worktree
    still here, else the pushed branch, else a fresh start from base: the
    continuation tiers of ADR-0011) — or "fresh": discard the previous attempt and
    start from base, which is what a retry is. A continued worker whose PR holds
    this fleet's landing turn is started ON it, briefed only to land the PR — in
    the worktree still here, else one recreated at the PR's head, never from base
    (ADR-0027). The callable returns the tier taken plus where the worker now
    is: {tier, action, prompt, reason, worktree, branch, terminal}."""
    cfg = run.cfg
    number = issue["number"]
    if issue["state"] != "open":
        raise RuntimeError(f"issue #{number} is {issue['state']}, not open — there is nothing "
                           f"to retry (release the claim instead)")
    discarded, landing = None, None      # landing: the PR this worker is started on the turn of
    if start == "fresh":
        discarded = _discard_attempt(run, number)
        plan = afk_decide.select_recovery(None, None, fresh=True)
    else:
        pr = afk_decide.closing_pr(_open_prs(run.repo), number)
        turn = afk_decide.held_turn(_turn(run.repo, pr["number"]), instance) if pr else None
        if pr and turn and not turn["batch"]:    # a merge batch's turn is its batch worker's
            landing = pr
        rec = _recovery(run, number, landing_pr=landing["number"] if landing else None)
        plan = {k: rec[k] for k in ("tier", "action", "prompt", "reason")}

    if plan["action"] == "reuse_worktree":
        wt = _Worktree.at(rec["worktree"]["path"])
        branch = wt.checked_out()
    else:
        if plan["action"] == "dispatch_fresh":
            tip = _base(cfg)
        else:                            # recreate_at_tip: the PR's head, else the pushed branch
            tip = landing["headRefName"] if landing else rec["branch"]["name"]
        wt = _Worktree.cut(run, tip, issue=issue)
        branch = wt.branch

    path = wt.path
    fields = _prompt_fields(run, issue, path, branch)
    with open(_WORKER_PROMPT) as f:
        if landing:
            prompt = afk_decide.render_landing(f.read(), fields, _landing_fields(cfg, landing))
        else:
            variant = plan["prompt"]
            assert variant != "landing", plan       # that brief is a PR's that holds the turn
            prompt = afk_decide.render_worker_prompt(f.read(), variant, fields, reason=reason)
    submit = wt.put(agent, prompt)

    def ready() -> Obj:
        handle = submit()
        if afk_decide.attempt_starting(issue["labels"]):    # the counted attempt has its worker
            _edit_labels(run.repo, number, [], [afk_decide.ATTEMPT_STARTING])
        if landing:
            _upsert_board(run, number, "landing", instance=instance, pr=landing["number"])
        else:
            _upsert_board(run, number, "claimed", instance=instance)
        return {**plan, "worktree": path, "branch": branch, "terminal": handle,
                **({"landing": landing["number"]} if landing else {}),
                **({"discarded": discarded} if discarded else {})}
    return ready


def cmd_dispatch(a: argparse.Namespace) -> Obj:
    """Start a worker on one issue — the whole sequence, in one call (ADR-0017):
    claim it (or confirm the claim is already mine), find what progress survives,
    have orca create or reuse the worktree at the right commit, start the agent
    with the run's worker launch command, wait for it, fill and SUBMIT the worker
    prompt, and upsert the status board.

      {"started": true, "claim": "won"|"held", tier, action, prompt, worktree,
       branch, terminal}      a worker is running
      {"started": false, "claim": "lost", "owner": {…}}   a peer holds the issue

    One call covers every way a worker is started: a frontier issue (claim "won",
    tier 3), a reclaimed / taken-over / orphaned claim ("held", tier 1–3 by what
    survived), a re-dispatch after its blockers closed. `--start fresh` instead
    discards whatever exists first — the tick's "this recovered state is not sane
    to build on" judgment. A failed attempt is not dispatched from here: `afk fail`
    counts the retry and starts it."""
    return _begin_dispatch(_run(a), a.instance, a.host, _agent(a), a.number,
                           start=a.start).finish()


@dataclasses.dataclass(frozen=True)
class _Start:
    """What beginning a dispatch came to: `outcome` names it, and `finish` is
    what is left of it."""
    outcome: StartOutcome    # afk_decide.BEGUN | LOST
    # Call it → `afk dispatch`'s result. BEGUN: it is the rest of the start;
    # LOST: nothing is left to do, and it only hands the result over.
    finish: Callable[[], Obj]


def _begin_dispatch(run: _Run, instance: str, host: str, agent: _Agent, number: int,
                    start: _StartFrom = "auto") -> _Start:
    """`afk dispatch` of issue <number> up to the agent's terminal being open →
    a `_Start`: begun, with the rest of it to call (`_begin_worker`); or lost,
    when a peer holds the issue, with that result itself. A start that fails
    raises. A claim the scan already shows as `instance`'s is not pushed again;
    one that is pushed is stamped with `host`."""
    _base(run.cfg)                         # before the claim: nothing is claimed for no base
    issue = _issue(run.repo, number)
    if issue["state"] != "open":           # before the claim: never lock a closed issue
        raise RuntimeError(f"issue #{number} is {issue['state']}, not open — nothing to dispatch")
    won = False
    if _claim_owner(run, number) != instance:
        claim = _claim(run, number, instance, host)
        won = claim["won"]
        if not won and claim["owner"].get("instance") != instance:
            lost = {"issue": number, "started": False, "claim": "lost", "owner": claim["owner"]}
            return _Start(afk_decide.LOST, finish=lambda: lost)
    ready = _begin_worker(run, instance, agent, issue, start)
    return _Start(afk_decide.BEGUN, finish=lambda: {"issue": number, "started": True,
                                          "claim": "won" if won else "held", **ready()})


# --------------------------------------------------------------------------- #
# act: the landing turn, and settling a claim — fail / escalate / park / close #
# --------------------------------------------------------------------------- #

def _upsert_board(run: _Run, number: int, phase: afk_decide.StatusPhase,
                  instance: str | None = None, pr: int | None = None, attempt: int = 0,
                  blocked_by: Iterable[int] = (), batch: BoardBatch | None = None) -> Obj:
    """Upsert the human-facing progress status board comment (idempotent, ADR-0006).
    Renders the body from the given phase (pure), then find-or-create by marker
    and write ONLY when the body changed — so re-entrant/disposable ticks and
    retry re-dispatches never spam the issue. A body that is the one the issue is
    already known to carry (`run.boards`) is not even read for: `known`."""
    repo, cfg = run.repo, run.cfg
    body = afk_decide.render_status_board(phase, cfg["gate"]["ci"], cfg["retry"],
                                          instance=instance, pr=pr, attempt=attempt,
                                          blocked_by=blocked_by, batch=batch)
    key = afk_decide.board_key(body)
    if run.boards.get(number) == key:
        return {"action": "known", "issue": number}
    _, board = afk_decide.latest_record(afk_decide.STATUS_RECORD, _issue_comments(repo, number))
    if board is None:
        done = {"action": "created", "comment_id": _comment(repo, number, body)}
    elif board["body"].strip() == body.strip():
        done = {"action": "unchanged", "comment_id": board["id"]}
    else:
        done = {"action": "updated", "comment_id": _comment(repo, number, body, board["id"])}
    run.boards[number] = key
    return {"issue": number, **done}


def cmd_status(a: argparse.Namespace) -> Obj:
    """Upsert one claim's status board at a NON-terminal phase — a `mine` row's
    `board_phase`. The terminal phases are written by the transition that reaches
    them (`afk land`, `afk escalate`, `afk park`, `afk close`)."""
    run = _run(a)
    return _upsert_board(run, a.number, a.phase,
                         instance=a.instance, pr=a.pr, attempt=a.attempt)


def _run_gate_command(cfg: Config, worktree: str, limits: _GateLimits, live: bool = False) -> Obj:
    """Run the configured local gate in a worktree → `afk_decide.gate_verdict` —
    the completion gate itself in `gate.ci: local` mode, run by `afk land` against
    the exact tree that lands (ADR-0012). *What* to run is config, *where* is the
    branch's worktree, and *whether it passed* is an exit code — no judgment. A run
    that times out is red, never green-by-default; the caller gets a verdict and a
    bounded excerpt, never a raw log. `live` is `afk gate`'s run: the log goes
    straight to the worker's terminal — on stderr, the JSON stays alone on stdout —
    and the excerpt is empty, since the worker has the whole of it."""
    cmd, timeout = cfg["gate"]["local_command"], limits.timeout

    def _text(s: str | bytes | None) -> str:
        return s.decode("utf-8", "replace") if isinstance(s, bytes) else (s or "")

    pipes: dict = ({"stdout": sys.stderr, "stderr": sys.stderr} if live
                   else {"capture_output": True, "text": True})
    timed_out, rc = False, 0
    try:
        p = subprocess.run(cmd, shell=True, cwd=worktree, timeout=timeout, env=_GIT_ENV, **pipes)
        out, rc = _text(p.stdout) + _text(p.stderr), p.returncode
    except subprocess.TimeoutExpired as e:
        out = _text(e.stdout) + _text(e.stderr) + f"\n[afk] gate timed out after {timeout}s"
        timed_out, rc = True, 124
    if live and timed_out:
        print(out.strip(), file=sys.stderr)
    return {**afk_decide.gate_verdict(rc, out, limits.excerpt_lines, timed_out), "command": cmd}


def _read_gate_record(rem: str, path: str, tree: str, command: str) -> Obj | None:
    """The `afk_decide.gate_record` the remote holds for a tree and a command,
    None when it holds none — or could not be asked, or holds at that name a
    commit that is not a recorded gate run, which are all the same answer: the
    gate runs. One round trip."""
    ref = afk_decide.gate_record_ref(tree, command)
    if _git(["-C", path, "fetch", "--quiet", "--no-tags", rem, ref], check=False).returncode != 0:
        return None
    return _read_record(afk_decide.GATE_RUN_RECORD, "FETCH_HEAD", path)


def _write_gate_record(rem: str, path: str, tree: str, command: str, now: int) -> str | None:
    """Put a green run on record on the remote → None, or why it could not be
    written. The record is a parentless commit OF the tested tree, at the ref
    named for the tree and the command (`afk_decide.gate_record_ref`). Soft: a
    record that cannot be written costs the next landing a run of the gate,
    nothing else."""
    sha = _record_commit(afk_decide.GATE_RUN_RECORD, afk_decide.gate_record(tree, command, now),
                         tree=tree, path=path)
    p = _git(["-C", path, "push", "--quiet", "--force", rem,
              f"{sha}:{afk_decide.gate_record_ref(tree, command)}"], check=False)
    return None if p.returncode == 0 else (p.stderr.strip() or f"git push exited {p.returncode}")


def _drop_gate_record(rem: str, path: str, tree: str, command: str) -> None:
    """Take a tree's record off the remote. Soft: none there is what was wanted."""
    _git(["-C", path, "push", "--quiet", rem, "--delete",
          afk_decide.gate_record_ref(tree, command)], check=False)


def _run_and_record_gate(run: _Run, path: str, limits: _GateLimits,
                         live: bool = False) -> tuple[Obj, str, list[str], str | None]:
    """One run of the local gate in a worktree, put on record when green →
    (`_run_gate_command`'s verdict, the head it ran on, the uncommitted paths,
    why the run is NOT on record — None when it is). Only a green run on a committed tree
    is recorded: over uncommitted or untracked files it tested a tree no commit
    holds. A red or timed-out run on a committed tree takes that tree's record
    away — the latest run of a tree is the one believed. The record is `afk`'s,
    made from an exit code it saw (ADR-0030)."""
    rem = run.rem
    head = _git(["-C", path, "rev-parse", "HEAD"]).stdout.strip()
    tree = _git(["-C", path, "rev-parse", "HEAD^{tree}"]).stdout.strip()
    dirty = _git(["-C", path, "status", "--porcelain"]).stdout.splitlines()
    gate = _run_gate_command(run.cfg, path, limits, live=live)
    if gate["status"] != "green":
        if not dirty:
            _drop_gate_record(rem, path, tree, gate["command"])
        return gate, head, dirty, "the gate is red"
    if dirty or _git(["-C", path, "rev-parse", "HEAD"], check=False).stdout.strip() != head:
        return gate, head, dirty, "the run was not on a committed tree"
    return gate, head, dirty, _write_gate_record(rem, path, tree, gate["command"], run.now())


def _gated(run: _Run, path: str, limits: _GateLimits) -> Obj:
    """Is the committed tree of a worktree gated green by the configured local
    gate? — the one question a landing asks of it, a single PR's and a merge
    batch's alike. Answered from the recorded gate run the remote holds for that
    tree and that command, else by a run made now, which is put on record in its
    turn (`_run_and_record_gate`, ADR-0030):

      {"status": "green", "source": "recorded", "head", "command", "recorded_at"}
      {"status": "green", "source": "run", "head", "command", "not_trusted"}
      {**`_run_gate_command`'s red verdict, "source": "run", "head", "not_trusted"}

    `head` is the commit that was asked about; `not_trusted` is why no record
    stood in for the run."""
    command = run.cfg["gate"]["local_command"]
    head, tree = _git(["-C", path, "rev-parse", "HEAD", "HEAD^{tree}"]).stdout.split()
    record = _read_gate_record(run.rem, path, tree, command)
    void = afk_decide.gate_record_void(record, run.now())
    if record and void is None:
        return {"status": "green", "source": "recorded", "head": head, "command": command,
                "recorded_at": record["at"]}
    gate, *_ = _run_and_record_gate(run, path, limits)
    if gate["status"] != "green":
        return {**gate, "source": "run", "head": head, "not_trusted": void}
    return {"status": "green", "source": "run", "head": head, "command": command,
            "not_trusted": void}


def cmd_gate(a: argparse.Namespace) -> Obj:
    """A WORKER's run of the local gate, in the worktree it is called from — what
    a worker runs before it opens its PR, in place of typing `gate.local_command`
    itself. The log streams to the worker's terminal; a GREEN run on a committed
    tree is put on record on the remote, under the tree it tested, which is what
    lets `afk land` — in this worktree, a recreated one, or on another machine —
    skip running the same command on the same tree again (ADR-0030). A worker
    saying "the gate is green" leaves no record.

      {"status": "green"|"red", "exit_code", "timed_out", "command", "head",
       "recorded": <this run is on record as a pass of `head`'s tree>, "detail"}"""
    run = _run(a)
    cfg = run.cfg
    if not cfg["gate"]["local_command"].strip():
        raise ValueError("gate.local_command is empty — there is no local gate to run")
    path = _git(["rev-parse", "--show-toplevel"]).stdout.strip()
    gate, head, dirty, unrecorded = _run_and_record_gate(
        run, path, _GateLimits(a.gate_timeout, excerpt_lines=0), live=True)
    out = {k: gate[k] for k in ("status", "exit_code", "timed_out", "command")}
    if gate["status"] != "green":
        return {**out, "head": head, "recorded": False,
                "detail": "the gate is red — nothing is on record; fix it and run this again"}
    if dirty:
        return {**out, "head": head, "recorded": False, "uncommitted": dirty[:20],
                "detail": "green, but not on a committed tree — the worktree had uncommitted or "
                          "untracked files, so this run proves nothing about a commit. Commit "
                          "them (or ignore the gate's own artifacts) and run this again"}
    if unrecorded:
        return {**out, "head": head, "recorded": False, "not_recorded": unrecorded,
                "detail": f"green on {head}, but the record could not be written — push it and "
                          f"go on; the landing runs the gate itself"}
    return {**out, "head": head, "recorded": True,
            "detail": f"green on {head} and on record — push it; any further commit needs "
                      f"another run"}


def _unmerged(path: str) -> list[str]:
    """The files a merge in progress in a worktree has left conflicted."""
    out = _git(["-C", path, "diff", "--name-only", "--diff-filter=U"]).stdout
    return [ln for ln in out.splitlines() if ln]


def _sync(rem: str, path: str, target: str) -> list[str]:
    """Merge the remote's `target` tip into the worktree's branch — never a rebase
    (ADR-0012) → the conflicted file list, empty when the sync is clean. A
    conflict is LEFT IN PLACE, for the worker whose worktree this is to resolve
    and commit; the next `afk land` picks up from that commit. Runs under the
    caller's own git identity (the merge commit lands on a PR)."""
    files = _unmerged(path)
    if files:
        return files
    dirty = _git(["-C", path, "status", "--porcelain", "--untracked-files=no"]).stdout.strip()
    if dirty:
        raise RuntimeError(f"the worktree {path} has uncommitted changes to tracked files — "
                           f"what would be gated is not what would land. Commit them (a "
                           f"resolved sync conflict must be committed) or discard them:\n{dirty}")
    sha = _fetch_tip(rem, target, cwd=path)
    p = subprocess.run(["git", "-C", path, "merge", "--no-edit", sha],
                       capture_output=True, text=True)
    if p.returncode != 0:
        files = _unmerged(path)
        if not files:
            raise RuntimeError(f"git merge of {target} into {path} failed: "
                               f"{(p.stderr or p.stdout).strip()}")
    return files


_TURN_POINTER = ("Your PR has the landing turn: land it now. Your instructions are the file "
                 "{brief} — read it now and carry it out end to end. It is my instruction to "
                 "you; do not ask me to confirm.")


def cmd_turn(a: argparse.Namespace) -> Obj:
    """Give one of my claims' PRs the LANDING TURN — the tick's half of a landing
    (ADR-0027), one transition in the one order that survives a crash at any step:

      refuse          another claim of mine holds the turn (`waiting`), or the
                      tick's judgments are not in: the PR's checks (`required`),
                      a PR with no checks at all (`--allow-no-checks`), the
                      adversarial verify (`--verified <head>`)
      write the brief the landing instruction, in the worktree's git dir
      record          ONE marker comment on the PR naming my instance — from here
                      `afk rebuild` reports the claim `landing`, and `afk land`
                      in its worktree is allowed to merge it
      deliver         the worker's terminal is still there → one submitted line
                      pointing at the brief; it is gone → a worker is started by
                      continuation in the same worktree (or one recreated at the
                      PR's head), briefed only to land the PR
      status board

    The tick never merges and there is no launcher-side fallback: the worker lands
    its own PR with `afk land`. Turns go out one at a time, so every other ready
    PR waits and none is synced against a tip about to move. Recorded before
    delivered: a delivery that then fails leaves a `landing` claim the existing
    paths repair — the nudge points at the brief, and a dead worker's
    continuation (`afk dispatch`) is started on the turn.

    `--restart` replaces the worker of a PR that holds the turn (`afk no-pr` →
    `restart`, ADR-0035): the idle session is closed and the delivery is made
    again as for a terminal that is gone. Nothing else is touched; the restart
    is written on the turn marker (`restarted`), and a second one on the same
    turn is refused. When it is asked for, and what follows a second silence,
    are `afk_decide.WORKER_CAUSES`'s rows.

      granted       the worker was told (`delivery`: "terminal" | "continuation").
                    `again` is true when the PR already held the turn and its
                    `afk land` had stopped for the tick: it was told to land again;
                    `restarted` (epoch seconds) is set when `--restart` did this.
      waiting       another claim of mine (`holder`) holds the turn. Nothing was
                    touched; this PR's turn comes when that one has landed, failed
                    or been escalated (a released claim holds no turn).
      landing       this PR already holds the turn and its worker has not stopped
                    for the tick. Nothing was touched; `afk no-pr` watches it.
      awaiting_ci   required mode: the PR's checks are still running. Leave it.
      gate_red      required mode: the PR's checks are red → `afk fail`.
      no_checks     required mode, and the PR has no checks at all — the
                    progressive gate. Re-run with `--allow-no-checks` if the tick
                    judges the acceptance criteria met.
      needs_verify  an adversarial verify is owed and `--verified` does not name
                    the PR's `head`. Run the verifier on `head`, then re-run with
                    `--verified <head>`.

    Two more shapes give the turn to a MERGE BATCH instead of to one PR
    (ADR-0029) — `--batch` (`_turn_batch`: form one from the PRs waiting, or
    continue the one that holds the turn) and `--abandon <batch>`
    (`_abandon_batch`). They add `too_few` and `abandoned` to the outcomes."""
    run, agent = _run(a), _agent(a)
    if sum(1 for given in (a.number is not None, a.form_batch, a.abandon) if given) != 1:
        raise ValueError("afk turn takes exactly one of --issue <n>, --batch, --abandon <batch>")
    if a.restart and a.number is None:
        raise ValueError("afk turn --restart takes --issue <n>: it restarts one PR's worker onto "
                         "the turn that PR holds")
    if a.form_batch:
        return _turn_batch(run, a.instance, agent)
    if a.abandon:
        return _abandon_batch(run, a.instance, a.abandon)
    return _grant_turn(run, a.instance, agent, a.number,
                       allow_no_checks=a.allow_no_checks, verified=a.verified, restart=a.restart)


def _grant_turn(run: _Run, instance: str, agent: _Agent, number: int,
                allow_no_checks: bool = False, verified: str | None = None,
                restart: bool = False) -> Obj:
    """`afk turn --issue <number>` — the landing turn to the PR of `instance`'s
    claim on it. `allow_no_checks` and `verified` are the tick's two judgments,
    when it has made them; `restart` replaces the silent worker of a PR that
    already holds the turn (`--restart`)."""
    cfg = run.cfg
    _require_mine(run, number, instance)
    issue = _issue(run.repo, number)
    prs = _open_prs(run.repo)
    pr = afk_decide.closing_pr(prs, number)
    if pr is None:
        raise RuntimeError(f"no open PR closes issue #{number} — there is nothing to land")
    head = pr["headRefOid"]
    out = {"issue": number, "pr": pr["number"], "head": head}

    def stop(outcome: afk_decide.TurnOutcome, **more: Any) -> Obj:
        return {**out, "outcome": afk_decide.turn_outcome(outcome), **more}

    turns = _claim_turns(run.repo, prs, _scan(run)[0], instance)
    held = {n: t for n, t in turns.items() if afk_decide.held_turn(t, instance)}
    others = sorted(n for n in held if n != number)
    held_here = held.get(number)
    if restart:
        if not held_here:
            raise RuntimeError(f"PR #{pr['number']} does not hold this fleet's landing turn — "
                               f"there is no turn to restart issue #{number}'s worker onto "
                               f"(`afk turn --issue {number}` grants one)")
        if held_here["restarted"]:
            raise RuntimeError(f"the worker on issue #{number} was already restarted onto this "
                               f"turn once — a second silence is escalated (`afk escalate`), "
                               f"with the PR kept, not restarted again")
    elif others:
        return stop("waiting", holder=others[0],
                    detail=f"issue #{others[0]}'s PR holds this fleet's landing turn; nothing was "
                           f"touched — this PR's turn comes when that one has landed, failed or been escalated")
    prev = _turn(run.repo, pr["number"])
    if not restart and held_here and held_here["stopped"] not in afk_decide.LAND_WAITS:
        return stop("landing",
                    detail="this PR already holds the landing turn and its worker has not "
                           "stopped for you; nothing was touched — `afk no-pr` watches it")
    # a judgment made about this PR stands: a re-delivery need not repeat it
    allow = allow_no_checks or bool(held_here and held_here["allow_no_checks"])
    verified = verified or (held_here["verified"] if held_here else None)
    checks = afk_decide.pr_checks_state(pr["statusCheckRollup"])
    ready = afk_decide.turn_gate(cfg["gate"]["ci"], checks, allow,
                                 afk_decide.verifies(cfg), verified, head)
    if ready != "ready":
        return stop(ready, checks=checks)

    wt = _Worktree.of_issue(run.repo, number)
    # a restart tells the worker there nothing: it is replaced, as if its terminal were gone
    there = wt.terminal is not None and not restart
    if there:
        with open(_WORKER_PROMPT) as f:
            brief = wt.write_brief(afk_decide.render_landing(
                f.read(), _prompt_fields(run, issue, wt.path, wt.checked_out()),
                _landing_fields(cfg, pr)))
    now = run.now()
    restarted = now if restart else (held_here["restarted"] if held_here else None)
    out["comment_id"] = _record_turn(run.repo, pr["number"], afk_decide.single_turn(
        prev, instance, now, verified=verified, allow_no_checks=allow, restarted=restarted))
    out["again"] = bool(held_here)
    if restart:
        out["restarted"] = now
    if not there:           # the worker is gone (or replaced): its continuation is started on the turn
        worker = _start_worker(run, instance, agent, issue, "auto")
        return stop("granted", delivery="continuation", terminal=worker["terminal"],
                    worktree=worker["worktree"])
    handle = wt.tell(_TURN_POINTER.format(brief=brief), "the landing turn")
    _upsert_board(run, number, "landing", instance=instance, pr=pr["number"])
    return stop("granted", delivery="terminal", terminal=handle, worktree=wt.path)


def _await_checks(repo: str, number: int, head: str, had_checks: bool, timeout: float,
                  poll: float) -> afk_decide.ChecksState | None:
    """Wait for a PR's checks to speak on `head`, the head that would land → their
    `pr_checks_state` there; "pending" when `timeout` seconds ran out first. The
    landing waits here itself rather than going round the launcher: nothing
    between a push and its checks finishing is a judgment (ADR-0027). Each look
    is one fresh read of the open PRs."""
    deadline = time.monotonic() + timeout
    while True:
        pr = next((p for p in _open_prs(repo, fresh=True) if p["number"] == number), None)
        if pr is None:
            raise RuntimeError(f"PR #{number} is no longer open — it was closed or merged while "
                               f"its checks were awaited; nothing was merged here")
        checks = afk_decide.pr_checks_state(pr["statusCheckRollup"])
        if not afk_decide.checks_owed(checks, pr["headRefOid"] == head, had_checks):
            return checks
        left = deadline - time.monotonic()
        if left <= 0:
            return "pending"
        time.sleep(min(poll, left))


def cmd_land(a: argparse.Namespace) -> Obj:
    """A WORKER lands its own PR, in the worktree it is called from — the only way
    a PR lands (ADR-0027). Refused (exit 3, nothing changed) unless the PR holds
    the landing turn of the fleet instance that holds the issue's claim: the check
    guards against a worker that strays, not a malicious one — worker and launcher
    share one `gh` credential. Then, in order: sync by merging the target in
    (never a rebase) → push → the machine gate on that exact head (in `required`
    mode, the PR's checks on it, waited for) → the verify check → the turn and
    the target's tip, read again → `gh pr merge` pinned to the gated head →
    status board. It stops with an `outcome`:

      merged        landed. The worker sends its wake and stops.
      conflict      the sync conflicted; the merge is left in progress with
                    `files` unmerged. The worker resolves them, COMMITS, and
                    runs this again.
      gate_red      local mode: the gate was red on the synced head (`gate.excerpt`,
                    also a PR comment). required mode: the PR's checks are red.
                    The worker fixes the code, commits, and runs this again.
      target_moved  the target moved while the gate ran (or the checks were
                    waited for): the head that was gated no longer holds its
                    tip, so merging it would land a tree no gate saw. Nothing
                    was merged. The worker runs this again, which syncs with
                    the new tip and gates that.
      awaiting_ci   required mode: the checks on the head that would land were
                    still running when `--checks-timeout` ran out. Up to then
                    this waits for them itself — after a sync that pushed a
                    new head too — and goes on to merge, or to `gate_red`.
      needs_verify  an adversarial verify is owed and the head that would land
                    is not the one the tick verified — the sync moved it.
      no_checks     required mode, the PR has no checks at all, and the tick has
                    not said it may land so.

    On the last three the next move is the tick's: the worker sends its wake and
    stops, the turn stays its own, and it is told to run this again (`afk turn`).
    No outcome spends an attempt, closes the PR or gives the turn up; every one
    but `merged` is written onto the PR's turn comment (`stopped`), which is what
    `afk rebuild` reports. A turn that is no longer this landing's once the gate
    has run — the claim was released or taken over, the turn granted afresh — is
    refused like one never held: exit 3, nothing merged, nothing written. The claim is NOT released and the worktree is not
    removed here — this runs inside that worktree, and a worker holds no instance
    id: the next cycle sees a claim whose issue is closed and settles both.

    The invariant every path keeps: what lands on the target was gated in the form
    it lands (ADR-0012). The merge is pinned to the PR's head and to nothing on
    the target, so the target's tip is read again right before it; what is left
    is the instant between that read and GitHub making the merge commit, in
    which only someone outside this fleet instance's turns can move the target
    (ADR-0027). In local mode `gate.source` says where that proof came
    from: "run" — the gate ran here, `gate.not_trusted` saying why no record
    stood in for it — or "recorded" — a green run of the configured command is on
    record on the remote for the tree of `gate.head`, so it was not run again,
    wherever that run was made (ADR-0030).

    `--batch <batch>` is a merge batch's worker landing its whole batch instead
    (`_land_batch`, ADR-0029), with outcomes of its own."""
    run = _run(a)
    cfg, rem = run.cfg, run.rem
    if (a.number is None) == (a.batch_id is None):
        raise ValueError("afk land takes exactly one of --issue <n>, --batch <batch>")
    limits = _GateLimits(a.gate_timeout, a.excerpt_lines)
    if a.batch_id:
        return _land_batch(run, a.batch_id, limits, a.merged_timeout)
    path = _git(["rev-parse", "--show-toplevel"]).stdout.strip()
    pr = afk_decide.closing_pr(_open_prs(run.repo), a.number)
    if pr is None:
        raise RuntimeError(f"no open PR closes issue #{a.number} — there is nothing to land (if "
                           f"it has already merged, send your wake and stop)")
    owner, turn = _single_turn(run, a.number, pr["number"])
    if turn is None:
        raise RuntimeError(f"PR #{pr['number']} does not hold the landing turn of the fleet "
                           f"instance that holds issue #{a.number}'s claim; nothing was changed. "
                           f"Do not land it any other way — you are told when its turn comes")
    branch, target = pr["headRefName"], _base(cfg)
    pr_number: int = pr["number"]
    _aim_pr(run, pr)
    out = {"issue": a.number, "pr": pr_number}

    def stop(outcome: afk_decide.LandOutcome, **more: Any) -> Obj:
        """Stop short of merging, and say so on the PR's turn comment."""
        at = _git(["-C", path, "rev-parse", "HEAD"]).stdout.strip()
        _record_turn(run.repo, pr_number, afk_decide.next_turn(
            turn, at=run.now(), stopped=afk_decide.land_outcome(outcome), head=at))
        return {**out, "outcome": outcome, **more}

    # --- the PR's head, as this worktree has it: commits the worker made since
    # (a resolution, a fix) are pushed below; a worktree behind its PR catches up ---
    pr_tip = _fetch_tip(rem, branch, cwd=path)
    here = _git(["-C", path, "rev-parse", "HEAD"]).stdout.strip()
    merging = _git(["-C", path, "rev-parse", "-q", "--verify", "MERGE_HEAD"],
                   check=False).returncode == 0
    if here != pr_tip and not merging and _git(
            ["-C", path, "merge-base", "--is-ancestor", here, pr_tip], check=False).returncode == 0:
        _git(["-C", path, "merge", "--ff-only", pr_tip])

    # --- sync: merge the target in, push what that produced ---
    files = _sync(rem, path, target)
    if files:
        return stop("conflict", files=files, worktree=path,
                    detail=f"merging {target} into {branch} conflicted; the merge is in "
                           f"progress here — resolve every file, `git add` it, COMMIT the "
                           f"merge, and run this again")
    head = _git(["-C", path, "rev-parse", "HEAD"]).stdout.strip()
    pushed = head != pr_tip
    if pushed:
        _push_branch(run.repo, rem, path, head, branch)
    out.update(head=head, synced=pushed)

    # --- the machine gate, against exactly `head` ---
    if cfg["gate"]["ci"] == "local":
        gate = _gated(run, path, limits)
        if gate["status"] != "green":
            _pr_comment(run.repo, pr["number"], afk_decide.gate_comment(gate, gate["command"]))
            return stop("gate_red", gate=gate,
                        detail="the gate is red on the synced head — fix the code, commit, "
                               "and run this again")
        out["gate"] = gate
    else:
        checks = afk_decide.pr_checks_state(pr["statusCheckRollup"])
        # the listing was read before the sync: its checks are this head's only if
        # its head is — a listing still at an earlier head is waited out like a push
        if pushed or checks == "pending" or pr["headRefOid"] != head:
            checks = _await_checks(run.repo, pr["number"], head, had_checks=checks is not None,
                                   timeout=a.checks_timeout, poll=a.checks_poll)
        checks_say = afk_decide.checks_gate(checks, turn["allow_no_checks"])
        if checks_say == "gate_red":
            return stop(checks_say, checks=checks,
                        detail="the PR's checks are red — fix the code, commit, and run this again")
        if checks_say == "awaiting_ci":
            return stop(checks_say, checks=checks,
                        detail=f"the checks on this head were still running after "
                               f"{a.checks_timeout}s — send your wake and stop; you are told to "
                               f"run this again once they are in")
        if checks_say != "green":
            return stop(checks_say, checks=checks,
                        detail="send your wake and stop — you are told to run this again once "
                               "that is settled")
    if afk_decide.verifies(cfg) and turn["verified"] != head:
        return stop("needs_verify",
                    detail="the head that would land is not the one that was verified — send "
                           "your wake and stop; you are told to run this again once it is")

    # --- a gate run or a wait for checks is long: what the merge rests on is read
    # again. The turn first — a refusal there writes nothing, the turn not being
    # this landing's to write on ---
    if _single_turn(run, a.number, pr_number, fresh=True) != (owner, turn):
        raise RuntimeError(f"PR #{pr_number} no longer holds the landing turn this landing "
                           f"started on (it was released, taken over or granted afresh while "
                           f"the gate ran); nothing was merged. Do not land it any other way — "
                           f"you are told when its turn comes")
    tip = _remote_sha(rem, f"refs/heads/{target}")
    if _git(["-C", path, "merge-base", "--is-ancestor", tip, head], check=False).returncode != 0:
        return stop("target_moved",
                    detail=f"{target} moved while the gate ran: the head that was gated does "
                           f"not hold its tip, and nothing was merged — run this again; it "
                           f"syncs with the new tip and gates that")

    # --- land it. The claim and this worktree are the next cycle's to settle ---
    _merge_pr(run.repo, rem, pr, head)
    _upsert_board(run, a.number, "merged", instance=owner, pr=pr["number"])
    return {**out, "outcome": afk_decide.land_outcome("merged"),
            "detail": "landed — send your wake and stop"}


# --------------------------------------------------------------------------- #
# act: the merge batch — N ready PRs behind one gate run (ADR-0029)            #
# --------------------------------------------------------------------------- #
#
# A batch is one landing turn, held by several PRs at once and landed by a
# worker of the batch's own, in a worktree of the batch's own. Its record is the
# turn marker on every member PR; its stack is that worktree's branch — read
# back from git every time, never from a file. The worktree keeps nothing of its
# batch: which batch it is the worktree of is its branch's name, and which PRs
# the batch holds is read off the markers (`_members_of_batch`).


def _members_of_batch(run: _Run, batch: str,
                      landed_pr: Callable[[int], int | None] = lambda issue: None
                      ) -> list[BatchMember]:
    """The PRs merge batch `batch` holds, in stack order → [{"issue", "pr"}...],
    read from the turn marker they carry — the one home of a batch's membership
    (ADR-0029); [] when no PR carries it. Any member's marker names them all, so
    one is looked for among the PRs of the claims: a claim's open PR, or — for a
    claim whose PR the batch already closed — the one `landed_pr(issue)` names.
    The claims of the instance that formed the batch are asked first: one
    comments read per PR asked, and usually the first is a member."""
    claims = sorted(_scan(run)[0],
                    key=lambda c: not afk_decide.batch_formed_by(batch, c["instance"]))
    prs = _open_prs(run.repo)
    for c in claims:
        pr = afk_decide.closing_pr(prs, c["number"])
        number = pr["number"] if pr else landed_pr(c["number"])
        turn = _turn(run.repo, number) if number else None
        if turn and turn["batch"] == batch:
            return turn["members"]
    return []


def _require_my_batch(run: _Run, instance: str, batch: str) -> Turn:
    """The turn marker of a batch one of MY claims' PRs is in → that marker.
    Raises when no PR of mine carries it: a batch is only ever moved by the
    fleet that holds its members' claims."""
    turns = _claim_turns(run.repo, _open_prs(run.repo),
                         _scan(run)[0], instance)
    found = next((t for t in turns.values() if t["batch"] == batch and not t["released"]), None)
    if found is None:
        raise RuntimeError(f"no PR of this fleet's claims holds a turn in merge batch "
                           f"{batch!r}; nothing was changed")
    return found


def _record_batch(run: _Run, instance: str, batch: str, members: Sequence[BatchMember],
                  phase: afk_decide.BatchPhase) -> None:
    """Write the batch's turn on every member PR — ONE marker each, rewritten in
    place — and say so on each member's status board."""
    repo, now = run.repo, run.now()
    board: BoardBatch = {"prs": [m["pr"] for m in members], "phase": phase}
    for m in members:
        _record_turn(repo, m["pr"], afk_decide.batch_turn(
            _turn(repo, m["pr"]), instance, now, batch, members, phase))
        _upsert_board(run, m["issue"], "landing", instance=instance, pr=m["pr"], batch=board)


def _unbatch(run: _Run, instance: str, batch: str, member: BatchMember, why: afk_decide.Unbatched,
             board: bool = True) -> None:
    """Replace a member PR's batch marker by the one that says it left the batch:
    it holds no turn, waits for a single one, and is never batched again."""
    _record_turn(run.repo, member["pr"], afk_decide.unbatched_turn(
        _turn(run.repo, member["pr"]), instance, run.now(), batch, why))
    if board:
        _upsert_board(run, member["issue"], "awaiting_turn", instance=instance,
                      pr=member["pr"])


def _delete_batch_branches(rem: str, batch: str, keep: str | None = None) -> list[str]:
    """Delete a batch's pushed branches on the remote, all but `keep` → the names
    deleted. Soft: a branch that is already gone is what was wanted."""
    gone = [b for b in afk_decide.batch_branches(_remote_heads(rem), batch) if b != keep]
    for branch in gone:
        _delete_branch(rem, branch, check=False)
    return gone


def _turn_batch(run: _Run, instance: str, agent: _Agent, working_set: WorkingSet | None = None) -> Obj:
    """`afk turn --batch` — give the landing turn to a MERGE BATCH (ADR-0029),
    read off `working_set`: the tick's, else one rebuilt here.

      form      no turn is out and two or more of my claims' PRs are eligible
                (`afk_decide.batch_candidates`, less the ones whose own worker
                is still working): record the batch's turn on every member PR →
                have orca create a worktree of the batch's own at the target's
                tip → start a batch worker in it on the batch brief → status
                boards. A member's own branch and worktree are not touched, and
                its worker is told nothing.
      continue  a batch of mine already holds the turn and its worker has no
                terminal: a new batch worker is started in the batch's worktree
                if it is here, else in one cut from the batch's pushed branch
                (else from the target's tip — nothing was stacked yet).

    The batch worker holds no claim and takes no dispatch slot.

      granted   a batch worker was started (`batch`, `issues`, `prs`,
                `delivery`: "fresh" | "worktree" | "branch"; `again` when the
                batch already held the turn).
      landing   my batch holds the turn and its worker is there. Nothing was touched.
      waiting   one PR (`holder`) holds the turn, or a dead fleet's batch
                (`batch`) is still on record on claims I took. Nothing was touched.
      too_few   fewer than two PRs are eligible. Nothing was touched: the turn
                goes to one PR (`afk turn --issue`)."""
    cfg = run.cfg
    ws = working_set or _rebuild(run, instance)

    def stop(outcome: afk_decide.BatchTurnOutcome, **more: Any) -> Obj:
        return {"outcome": afk_decide.batch_turn_outcome(outcome), **more}

    who, what = afk_decide.turn_holder(ws, instance)
    if who == "dead":
        dead = what[0]
        return stop("waiting", batch=dead["id"],
                    detail=f"merge batch {dead['id']} of fleet instance {dead['instance']} is "
                           f"still on record on PRs of claims this fleet took; nothing was "
                           f"touched — abandon it first (`afk turn --abandon`)")
    if who == "mine":
        mine = what
        if _Worktree.of_batch(run.repo, mine["id"]).terminal:
            return stop("landing", batch=mine["id"],
                        detail="this batch already holds the landing turn and its worker is "
                               "there; nothing was touched — `afk no-pr --batch` watches it")
        # only the PRs that still carry the batch's marker: one left out since is not put back
        marked = ((m, _turn(run.repo, m["pr"])) for m in mine["members"])
        members = [m for m, turn in marked if turn and turn["batch"] == mine["id"]]
        return _start_batch_worker(run, instance, agent, mine["id"], members,
                                   mine["phase"] or afk_decide.BATCH_PHASES[0], again=True)
    if who == "single":
        holder = what
        return stop("waiting", holder=holder,
                    detail=f"issue #{holder}'s PR holds this fleet's landing turn; nothing was "
                           f"touched — no batch is formed while a turn is out")
    eligible = afk_decide.batch_candidates(ws["mine"], ws["merge_order"], cfg)
    # a PR whose own worker is still working may yet move
    workers = _Workers(run)
    busy = [n for n in eligible if workers.of_issue(n).busy]
    picked = afk_decide.batch_candidates(ws["mine"], ws["merge_order"], cfg, busy=busy)
    if not picked:
        return stop("too_few", busy=busy,
                    detail="fewer than two PRs are eligible for a merge batch; nothing was "
                           "touched — the turn goes to one PR")
    rows = {r["number"]: r for r in ws["mine"]}
    members: list[BatchMember] = [
        {"issue": n, "pr": pr} for n in picked if (pr := rows[n]["pr"]) is not None]
    return _start_batch_worker(run, instance, agent, afk_decide.batch_id(instance, run.now()),
                               members, afk_decide.BATCH_PHASES[0], again=False)


def _start_batch_worker(run: _Run, instance: str, agent: _Agent, batch: str,
                        members: list[BatchMember],
                        phase: afk_decide.BatchPhase, again: bool) -> Obj:
    """Record a batch's turn and put a batch worker on it → `afk turn --batch`'s
    `granted`. Recorded before delivered: a start that then fails leaves a batch
    whose worker has no terminal, which the next cycle continues."""
    cfg, rem = run.cfg, run.rem
    target = _base(cfg)
    _record_batch(run, instance, batch, members, phase)
    wt = _Worktree.of_batch(run.repo, batch)
    if wt.path:
        delivery, branch = "worktree", wt.checked_out()
    else:
        pushed = afk_decide.batch_branches(_remote_heads(rem), batch)
        delivery = "branch" if pushed else "fresh"
        wt = _Worktree.cut(run, pushed[-1] if pushed else target, batch=batch)
        branch = wt.branch
    path = wt.path
    titles = {p["number"]: p["title"] for p in _open_prs(run.repo)}
    with open(_WORKER_PROMPT) as f:
        prompt = afk_decide.render_batch_brief(f.read(), {
            "batch": batch, "repo": run.repo, "target": target, "branch": branch,
            "members": [{**m, "title": titles.get(m["pr"], "")} for m in members],
            "afk_path": os.path.abspath(__file__),
            "config": json.dumps(cfg, ensure_ascii=False), "worktree_path": path,
            "launcher_terminal": os.environ.get("ORCA_TERMINAL_HANDLE", "")})
    handle = wt.put(agent, prompt)()
    return {"outcome": afk_decide.batch_turn_outcome("granted"), "batch": batch,
            "issues": [m["issue"] for m in members], "prs": [m["pr"] for m in members],
            "again": again, "delivery": delivery, "terminal": handle, "worktree": path}


def _abandon_batch(run: _Run, instance: str, batch: str) -> Obj:
    """`afk turn --abandon <batch>` — give a batch up with nothing landed
    (ADR-0029): every member PR's marker is replaced by one that holds no turn
    and says so → its pushed branch is deleted → the batch's worktree removed.
    The members are back to `awaiting_turn`, take single landing turns, and are
    not batched again. For a batch whose worker stayed silent after its nudge —
    and for a DEAD fleet's batch on claims this fleet took: any batch a PR of
    mine is in may be abandoned by me. The markers go first: they are what
    `afk land --batch` checks before it pushes anything.

    An abandon that arrives after the batch's push abandons nothing that
    landed: a member whose merge commit is on the target keeps its marker and
    is finished instead (`_finish_member`) — `landed`, never in `issues` — and
    the next cycle settles it from its closed issue.

      {"outcome": "abandoned", "batch", "issues", "prs", "landed",
       "deleted_branches", "cleanup"}"""
    rem = run.rem
    found = _require_my_batch(run, instance, batch)
    claims = _scan(run)[0]
    mine = {c["number"] for c in claims if c["instance"] == instance}
    still_open = {p["number"] for p in _open_prs(run.repo)}
    left, landed = [], []
    for m in found["members"]:
        turn = _turn(run.repo, m["pr"]) if m["pr"] in still_open else None
        if not turn or turn["batch"] != batch or turn["released"]:
            continue
        if _landed_commit(".", _target_tip(run), m["pr"]):
            _finish_member(run, found["instance"], m)
            landed.append(m)
            continue
        _unbatch(run, instance, batch, m, "abandoned", board=m["issue"] in mine)
        left.append(m)
    deleted = _delete_batch_branches(rem, batch)
    wt = _Worktree.of_batch(run.repo, batch)
    cleanup = wt.remove() if wt.path else None
    return {"outcome": afk_decide.batch_turn_outcome("abandoned"), "batch": batch,
            "issues": [m["issue"] for m in left], "prs": [m["pr"] for m in left],
            "landed": [m["issue"] for m in landed],
            "deleted_branches": deleted, **({"cleanup": cleanup} if cleanup else {})}


def _batch_worker(run: _Run, batch: str, worker: _Worker) -> BatchWorkerRow:
    """`afk no-pr --batch` — is the batch's worker still at it? The same reading
    and the same ladder as any worker holding a turn (`classify_stopped`), from the
    batch's worktree: busy, or within grace of its turn or of its last
    `afk land --batch`, it is left; gone (`dead` / `orphan`), it is continued;
    silent past grace it is nudged once, and silent again the batch is
    abandoned (`afk_decide.batch_step`)."""
    cfg, path, nudged_at = run.cfg, worker.path, worker.nudged_at
    turn_at, progress = None, None
    seen = worker.settled
    if not seen:
        now, grace = worker.now, worker.grace
        turns = (_turn(run.repo, m["pr"]) for m in _members_of_batch(run, batch))
        turn_at = max((t["at"] for t in turns if t and t["at"]), default=None)
        progress = _worktree_progress(path, run.rem, _base(cfg)) if path else None
        # a batch's worker declares no verdict and names no blocker — and a batch's
        # turn is never restarted: its second silence abandons the batch
        seen = afk_decide.classify_stopped(progress, worker.reading["terminal_idle_seconds"],
                                           None, {}, now, grace, nudged_at=nudged_at,
                                           turn=afk_decide.next_turn(None, at=turn_at, batch=batch))
    return {"batch": batch, **seen, "worktree": path, "progress": progress,
            "nudged_at": nudged_at, "turn_at": turn_at, "worker_state": worker.reading["state"]}


def _landed_pr(path: str, tip: str, issue: int) -> int | None:
    """The PR whose merge commit on the target's own line (`tip`, already
    fetched into `path`) closes `issue` — `afk_decide.stack_message`'s
    `Closes #<issue>` under a subject ending ` (#<pr>)` — or None."""
    subject = _git(["-C", path, "log", "-1", "--first-parent", "--format=%s", "-E",
                    f"--grep=^Closes #{issue}$", tip]).stdout.strip()
    return afk_decide.stacked_pr(subject)


def _landed_commit(path: str, tip: str, pr_number: int) -> str | None:
    """The commit on the target (`tip`, already fetched into `path`) that landed
    a batched PR — the one on the target's own line whose subject ends
    ` (#<pr>)` — or None."""
    out = _git(["-C", path, "log", "-1", "--first-parent", "--format=%H%x09%s", "--fixed-strings",
                f"--grep= (#{pr_number})", tip]).stdout.strip()
    sha, _, subject = out.partition("\t")
    return sha if sha and subject.endswith(f" (#{pr_number})") else None


def _target_tip(run: _Run) -> str:
    """The tip of the merge target on the remote, fetched into this repo — once
    for the whole process, for the reads that ask it what has landed."""
    target = _base(run.cfg)
    return _once(("tip", run.rem, target), lambda: _fetch_tip(run.rem, target))


def _cut_landing(run: _Run, claim: Claim) -> int | None:
    """The PR a merge batch landed `claim`'s still-open issue with, when the
    batch's finishing was cut short after its push — the one cut `afk land
    --batch` cannot finish itself: GitHub shows the PR merged, so no open PR
    carries the batch's turn marker and nothing lists the batch any more
    (ADR-0029) → that PR's number, else None. The target itself is asked: the
    merge commit on its own line that closes the issue, by a PR that was in a
    batch under this claim (`afk_decide.landed_under`). None too for an issue
    that is closed, or that an open PR closes: those are settled, or still
    landing, by the paths that already exist."""
    number = claim["number"]
    if _issue_state(run.repo, number) != "open" or afk_decide.closing_pr(_open_prs(run.repo),
                                                                         number):
        return None
    pr = _landed_pr(".", _target_tip(run), number)
    return pr if pr and afk_decide.landed_under(_turn(run.repo, pr), claim) else None


def _finish_member(run: _Run, instance: str | None, member: BatchMember) -> None:
    """Finish one PR a merge batch landed, as far as its issue goes: the status
    board says merged and the issue is closed — which GitHub does itself only
    on the default branch. Each step is skipped when already done; the claim,
    the worktree and a PR GitHub still shows open are the next cycle's, from
    the closed issue (`_settle_landed`)."""
    _upsert_board(run, member["issue"], "merged", instance=instance, pr=member["pr"])
    if _issue_state(run.repo, member["issue"]) == "open":
        _close_issue(run.repo, member["issue"])


def _close_landed_pr(run: _Run, number: int) -> int | None:
    """Close the open PR of a CLOSED issue when a merge batch landed it and
    GitHub does not show it merged → the PR number, None when there is nothing
    of the kind. The target itself is asked: a PR whose merge commit is not on
    it is left alone."""
    pr = afk_decide.closing_pr(_open_prs(run.repo), number)
    turn = _turn(run.repo, pr["number"]) if pr else None
    if not pr or not turn or not turn["batch"] or turn["released"]:
        return None
    target = _base(run.cfg)
    commit = _landed_commit(".", _fetch_tip(run.rem, target), pr["number"])
    if not commit:
        return None
    _close_batched_pr(run, pr, commit, turn["batch"], [m["pr"] for m in turn["members"]])
    return pr["number"]


def _close_batched_pr(run: _Run, pr: PullRequest, commit: str, batch: str, prs: list[int]) -> None:
    """Close one PR a batch landed, with the comment that names its commit."""
    cfg = run.cfg
    _close_pr(run.repo, run.rem, pr["number"],
              afk_decide.batch_landed_comment(commit, _base(cfg), batch, prs))


def _stack_pr(rem: str, path: str, pr: PullRequest,
              issue: int) -> tuple[str | None, str | None, list[str]]:
    """Stack one PR onto the batch worktree's HEAD with ONE merge commit, whose
    second parent is the PR's own head → (commit, None, []), or (None, why,
    files) when it is left out: it conflicts with the stack so far (`conflict`,
    with the files), or changes nothing against it (`no_changes`). A PR left
    out leaves the worktree as it found it. The PR's head is on the stack
    unchanged, which is what makes GitHub show the PR merged once the stack is
    on the target; the merge commit is the caller's own."""
    head = _fetch_tip(rem, pr["headRefName"], cwd=path)
    p = subprocess.run(["git", "-C", path, "merge", "--no-ff", "--no-commit", head],
                       capture_output=True, text=True)
    files = _unmerged(path)
    if p.returncode != 0 or files:
        _git(["-C", path, "merge", "--abort"], check=False)
        _git(["-C", path, "reset", "-q", "--hard", "HEAD"])
        if not files:
            raise RuntimeError(f"git merge of PR #{pr['number']} into {path} failed: "
                               f"{(p.stderr or p.stdout).strip()}")
        return None, "conflict", files
    if not _git(["-C", path, "status", "--porcelain", "--untracked-files=no"]).stdout.strip():
        _git(["-C", path, "merge", "--abort"], check=False)    # none is open when it was up to date
        return None, "no_changes", []
    p = subprocess.run(["git", "-C", path, "commit", "-q", "--no-verify",
                        "-m", afk_decide.stack_message(pr["title"], pr["number"], issue)],
                       capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"could not commit PR #{pr['number']} onto the stack: "
                           f"{(p.stderr or p.stdout).strip()}")
    return _git(["-C", path, "rev-parse", "HEAD"]).stdout.strip(), None, []


def _batch_turns(run: _Run, batch: str, listed: Sequence[BatchMember]
                 ) -> tuple[list[BatchMember], str]:
    """The members of `listed` that hold the batch's turn right now → (members,
    the instance that granted it — "" when there are none). A PR whose marker
    says it left the batch is simply not one of them; any other PR that does
    not carry the batch's marker — or whose claim is not held by the instance
    the marker names — means the batch does not hold the turn, and raises:
    nothing may land on it."""
    members, instance = [], ""
    for m in listed:
        turn = _turn(run.repo, m["pr"])
        if turn and turn["released"]:
            continue
        owner = _claim_owner(run, m["issue"])
        if not turn or turn["batch"] != batch or afk_decide.held_turn(turn, owner) is None:
            raise RuntimeError(f"PR #{m['pr']} does not hold the landing turn of merge batch "
                               f"{batch} for the fleet instance that holds issue "
                               f"#{m['issue']}'s claim; nothing was changed. Do not land "
                               f"anything any other way — send your wake and stop")
        members.append(m)
        instance = turn["instance"]
    return members, instance


def _land_batch(run: _Run, batch: str, limits: _GateLimits, merged_timeout: float) -> Obj:
    """`afk land --batch <batch>` — a BATCH WORKER stacks, gates and lands its
    merge batch, in the batch's worktree (ADR-0029). Refused (exit 3, nothing
    changed) unless every member PR carries the batch's turn marker, naming the
    fleet instance that holds its issue's claim. Then, in order:

      stack   reset to the target's tip and put each member on it in merge
              order, ONE merge commit per PR; a PR that conflicts with the
              stack so far is left out — its marker says so, it takes a single
              turn later and is not batched again. The fix commits this worktree
              already carried are put back on top.
      push    the stack to the batch's own branch — durable progress (ADR-0011)
      gate    `gate.local_command`, once, on the stack — not at all when a
              green run is already on record for the stack's tree (ADR-0030)
      land    push the stack to the target as a FAST-FORWARD — the only lock
              there is: a target that moved refuses it
      finish  each member: status board, close the issue; GitHub shows the PR
              merged, and its branch is deleted once it does

    The stack is rebuilt from the target's tip on every run, so a run after a
    red gate (a fix commit on top) and a run after the target moved are the same
    run. It stops with an `outcome`:

      landed        the stack is on the target (`landed`, `left_out` — the
                    PRs this run left out — `fix_commits`; `unmerged`: the PRs
                    GitHub did not yet show merged). Send the wake and stop.
      gate_red      the gate was red on the stack; nothing landed (`gate.excerpt`).
                    Fix the stack — one more commit on top — and run this again.
      target_moved  the fast-forward was refused; nothing landed. Run this again.
      too_small     fewer than two members could be stacked; the batch is
                    dissolved and nothing landed. Send the wake and stop.

    The invariant every path keeps: the target is only ever moved to a commit
    the gate passed on, and what the gate proved is the stack in the form it
    lands. No claim is released and no worktree removed here — the next cycle
    settles each member from its closed issue, or, cut before that issue was
    closed, from its commit on the target (`_cut_landing`), and removes the
    batch's worktree.

    Which PRs the batch holds is read from their turn markers on every run
    (`_members_of_batch`): the worktree keeps no list, so one recreated from the
    batch's pushed branch lands the same members."""
    cfg, rem = run.cfg, run.rem
    target = _base(cfg)
    path = _git(["rev-parse", "--show-toplevel"]).stdout.strip()
    branch = _git(["-C", path, "rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()
    if not afk_decide.batch_branches([branch], batch):
        raise RuntimeError(f"this worktree is not merge batch {batch}'s; nothing was changed")
    tip = _fetch_tip(rem, target, cwd=path)
    listed = _members_of_batch(run, batch, lambda issue: _landed_pr(path, tip, issue))
    if not listed:
        raise RuntimeError(f"no PR carries the turn marker of merge batch {batch}: it does not "
                           f"hold the landing turn; nothing was changed. Do not land anything "
                           f"any other way — send your wake and stop")
    left_out = []

    def result(outcome: afk_decide.BatchOutcome, members: Sequence[BatchMember],
               **more: Any) -> Obj:
        return {"outcome": afk_decide.batch_outcome(outcome), "batch": batch,
                "issues": [m["issue"] for m in members], "prs": [m["pr"] for m in members],
                "left_out": left_out, **more}

    # --- a landing whose finishing was cut short: the target already holds the stack ---
    landed: list[Stacked] = [{**m, "commit": c} for m in listed
                                         for c in [_landed_commit(path, tip, m["pr"])] if c]
    if landed:
        return _finish_batch(run, batch, landed, result, merged_timeout,
                             {p["number"]: p["headRefName"] for p in _open_prs(run.repo)})

    members, instance = _batch_turns(run, batch, listed)
    dirty = _git(["-C", path, "status", "--porcelain", "--untracked-files=no"]).stdout.strip()
    if dirty:
        raise RuntimeError(f"the batch worktree {path} has uncommitted changes to tracked "
                           f"files — what would be gated is not what would land. Commit your "
                           f"fix (or discard it) and run this again:\n{dirty}")
    prs = {p["number"]: p for p in _open_prs(run.repo)}
    closed = [m["pr"] for m in members if m["pr"] not in prs]
    if closed:
        raise RuntimeError(f"PR(s) {closed} of merge batch {batch} are no longer open; nothing "
                           f"was changed — send your wake and stop")
    for m in members:
        _aim_pr(run, prs[m["pr"]])

    # --- stack: every member on the target's tip, then the fixes carried so far ---
    log = _git(["-C", path, "log", "--first-parent", "--reverse", "--format=%H%x09%s",
                f"{tip}..HEAD"]).stdout
    commits = [(sha, subject) for sha, _, subject
               in (ln.partition("\t") for ln in log.splitlines())]
    _, fixes = afk_decide.read_stack(commits, {m["pr"] for m in listed})
    _git(["-C", path, "reset", "-q", "--hard", tip])
    stacked: list[Stacked] = []
    for m in members:
        commit, why, files = _stack_pr(rem, path, prs[m["pr"]], m["issue"])
        if commit:
            stacked.append({**m, "commit": commit})
            continue
        _unbatch(run, instance, batch, m, "left_out")
        left_out.append({**m, "reason": why, "files": files})
    if len(stacked) < 2:
        for m in stacked:
            _unbatch(run, instance, batch, m, "dissolved")
        _git(["-C", path, "reset", "-q", "--hard", tip])
        _delete_batch_branches(rem, batch)
        return result("too_small", [],
                      detail="fewer than two PRs could be stacked: the batch is dissolved and "
                             "nothing landed — send your wake and stop")
    kept = []
    for sha in fixes:
        p = subprocess.run(["git", "-C", path, "cherry-pick", sha], capture_output=True, text=True)
        if p.returncode == 0:
            kept.append(sha)
        else:                            # it no longer applies to this stack: the gate will say
            _git(["-C", path, "cherry-pick", "--abort"], check=False)
            _git(["-C", path, "reset", "-q", "--hard", "HEAD"])
    head = _git(["-C", path, "rev-parse", "HEAD"]).stdout.strip()
    _push_branch(run.repo, rem, path, head, branch, force=True)
    _delete_batch_branches(rem, batch, keep=branch)
    more = {"head": head, "target": target, "fix_commits": len(kept)}

    # --- the gate, once, on the stack ---
    _record_batch(run, instance, batch, stacked, "gating")
    gate = _gated(run, path, limits)
    abandoned = not os.path.isdir(path)  # a gate run is long: an abandon removes the worktree
    if gate["status"] != "green":
        _record_batch(run, instance, batch, stacked, "fixing")
        red = {k: v for k, v in gate.items() if k not in ("source", "head", "not_trusted")}
        return result("gate_red", stacked, gate=red, **more,
                      detail="the gate is red on the stack and nothing landed — fix the stack "
                             "with one more commit on top (do not hunt for the PR at fault), "
                             "and run this again")

    # --- land: a gate run is long, so the turn is checked again; then the fast-forward ---
    still = [] if abandoned else _batch_turns(run, batch, stacked)[0]
    if len(still) != len(stacked):
        raise RuntimeError(f"merge batch {batch} no longer holds the landing turn (it was "
                           f"abandoned while the gate ran); nothing landed — send your wake "
                           f"and stop")
    p = _push_branch(run.repo, rem, path, head, target, check=False)
    if p.returncode != 0:
        if _remote_sha(rem, f"refs/heads/{target}") == tip:
            raise RuntimeError(f"the push of the batch to {target} failed although {target} "
                               f"has not moved: {p.stderr.strip()}")
        _record_batch(run, instance, batch, stacked, "stacking")
        return result("target_moved", stacked, **more,
                      detail=f"{target} moved while the batch was gating: the fast-forward was "
                             f"refused and nothing landed — run this again; the batch is "
                             f"re-stacked on the new tip and gated again")
    return _finish_batch(run, batch, stacked, result, merged_timeout,
                         {m["pr"]: prs[m["pr"]]["headRefName"] for m in stacked}, **more)


def _await_merged(repo: str, numbers: list[int], timeout: float, poll: float = 2) -> set[int]:
    """Wait for GitHub to show the PRs `numbers` merged — it does so by itself,
    a moment after a push puts their heads on the target → the ones still open
    when `timeout` seconds ran out. Each look is one fresh read of the open PRs."""
    deadline = time.monotonic() + timeout
    while True:
        still_open = {p["number"] for p in _open_prs(repo, fresh=True)} & set(numbers)
        left = deadline - time.monotonic()
        if not still_open or left <= 0:
            return still_open
        time.sleep(min(poll, left))


def _finish_batch(run: _Run, batch: str, landed: Sequence[Stacked],
                  result: Callable[..., Obj],
                  merged_timeout: float, branches: dict[int, str], **more: Any) -> Obj:
    """Finish every PR a batch landed. Each step is skipped when already done,
    so a finishing that was cut short is finished by the next run. Issues first:
    a member whose issue is closed is settled by the next cycle whatever happens
    here — and so is one whose issue this was cut before closing, once GitHub
    shows its PR merged (`_cut_landing`): no run of this is owed for it. The PRs are GitHub's to finish: each one's head is on the target, so
    it shows the PR merged, and the PR's branch is deleted once it does —
    deleted sooner, the PR would read closed instead. One still open after
    `merged_timeout` seconds is left as it is, branch and all: the next cycle's
    release closes it if it is open even then (`_close_landed_pr`). `branches`
    is {PR number: its branch} for the PRs still open as the finishing began —
    a PR GitHub had already merged by then is not in it, and keeps its branch."""
    rem = run.rem
    granted = _turn(run.repo, landed[0]["pr"])
    instance = granted["instance"] if granted else None
    for m in landed:
        _finish_member(run, instance, m)
    _delete_batch_branches(rem, batch)
    still_open = _await_merged(run.repo, [m["pr"] for m in landed], merged_timeout)
    heads = _remote_heads(rem)
    for m in landed:
        branch = branches.get(m["pr"])
        if m["pr"] not in still_open and branch in heads:
            _delete_branch(rem, branch, check=False)
    return result("landed", landed, **more, unmerged=sorted(still_open),
                  landed=[{k: m[k] for k in ("issue", "pr", "commit")} for m in landed],
                  detail="landed — send your wake and stop")


def _sweep_batches(run: _Run, instance: str, live: Iterable[str]) -> Obj:
    """Remove what a merge batch of mine left behind once it no longer holds a
    turn — landed, dissolved or abandoned: its worktree on this machine (never
    one whose worker is still busy: it is finishing) and its pushed branch.
    `live` are the ids of the batches that still do."""
    rem = run.rem
    stale = [w for w in _Worktree.of_batches(run.repo, instance)
             if (w.batch is None or w.batch not in live) and w.remembered]
    removed = []
    if stale:
        workers = _Workers(run)
        for w in stale:
            if not workers.at(w.remembered).busy:
                removed.append(w.remove())
    rx = afk_decide.batch_branch_regex(instance=instance)
    gone = [h for h in _remote_heads(rem) for m in [rx.match(h)] if m and m.group(1) not in live]
    for branch in gone:
        _delete_branch(rem, branch, check=False)
    return {"removed": removed, "deleted_branches": gone}


def _workless_worktree(run: _Run, number: int) -> _Worktree | None:
    """Issue <number>'s worktree on this machine when its branch holds no work —
    nothing committed past the base, nothing uncommitted — else None: the one
    a transition that releases an open issue's claim may remove, worker and
    all. A worktree with work in it is evidence, and is never removed here."""
    wt = _Worktree.of_issue(run.repo, number)
    path = wt.path
    progress = _worktree_progress(path, run.rem, _base(run.cfg)) if path else None
    empty = bool(progress and progress["commits_ahead"] == 0 and not progress["dirty"])
    return wt if empty else None


def _escalation_begun(run: _Run, number: int) -> afk_decide.Escalation | None:
    """The escalation my claim on issue <number> is already in — one cut short
    after its comment, the claim still held — else None."""
    claim = _claim_of(run, number)
    if claim is None:
        return None
    return afk_decide.escalation_begun(_issue_comments(run.repo, number), claim["sha"])


def _escalate(run: _Run, instance: str, issue: IssueRead, attempt: int, reason: str) -> Obj:
    """Hand an issue to a human, in the one order that leaves no gap: status board
    → comment → labels → release → remove the worktree, when its branch holds no
    work. The claim is released before anything is removed, and after the
    relabel: released first, a PR-less issue still carrying `ready_label` is
    back on the frontier for a peer to dispatch before the relabel lands. A
    worktree with work in it stays for the human, with its PR; an empty one is
    only an idle worker (ADR-0041).

    Any write of it can be refused with the claim still held, and it is then run
    again — by hand, or by the tick that finds the same failure. The comment goes
    before the relabel because the relabel strips the attempt count: the comment
    records whose escalation it is and after how many retries, so a run that
    finds it posts no second one, reports the count the labels no longer say,
    and does what is left, each step of which is safe to repeat (ADR-0033)."""
    cfg = run.cfg
    number = issue["number"]
    idle = _workless_worktree(run, number)
    pr = afk_decide.closing_pr(_open_prs(run.repo), number)
    pr_number = pr["number"] if pr else None
    begun = _escalation_begun(run, number)
    if begun:
        attempt = begun["attempt"]
    _upsert_board(run, number, "escalated", instance=instance,
                  pr=pr_number, attempt=attempt)
    claim = _claim_of(run, number)
    if claim is None:
        raise RuntimeError(f"issue #{number} is not claimed; nothing was changed")
    comment_id = begun["comment_id"] if begun else _comment(
        run.repo, number,
        afk_decide.escalation_comment(reason, attempt, claim["sha"], pr_number))
    add, remove = afk_decide.escalation_labels(issue["labels"], cfg)
    _ensure_label(run.repo, add[0])
    _edit_labels(run.repo, number, add, remove)
    _release(run, number)
    cleanup = idle.remove() if idle else None
    return {"issue": number, "action": "escalate", "attempt": attempt, "pr": pr_number,
            "labels": {"added": add, "removed": remove}, "comment_id": comment_id,
            "released": True, **({"cleanup": cleanup} if cleanup else {})}


def _ensure_label(repo: str, name: str) -> None:
    """Make sure a label exists before it is applied (gh refuses to add an unknown
    one). Created without `--force`, so a label that already exists keeps its
    colour and description — that failure is the expected case and is ignored."""
    _gh(["label", "create", name, "--repo", repo], check=False)


def cmd_fail(a: argparse.Namespace) -> Obj:
    """One of my claims FAILED — its checks are red, a verifier refuted it, or its
    worker gave up or went quiet with no PR and no outcome (which silences come
    here is `afk_decide.WORKER_CAUSES`'s to say: a landing turn's never does).
    A failed PR is closed, which is also what frees its landing turn. The retry
    ladder, as one transition (ADR-0017): read the attempt off the issue's `afk-attempt/<n>`
    label, then either

      retry     swap the label up by one, discard the failed attempt (close its PR,
                delete its branch, remove its worktree) and start a FRESH worker
                under the same claim, handed `--reason`; or
      escalate  when the attempts are exhausted: status board → comment
                `--reason` → relabel → release the claim.

    `--reason` is the tick's judgment — the failure, re-read from where it lives.
    This is the one writer of the attempt label, as `current_attempt` is its one
    reader.

    A failure spends ONE attempt, however often this has to run to finish. The
    edit that swaps the label up also adds `afk-attempt/starting` — "this failure
    is counted, its fresh worker has not started" — and starting a worker on the
    issue removes it. Everything a retry does after that edit can be refused (the
    PR's close, a branch's deletion, orca) with the claim still held and the
    failure still there; run again, by hand or by the next tick, it finds the
    label, adds nothing, and does what is left. A failure of the fresh attempt
    finds no such label and is counted. The one step that is not covered is that
    removal: refused after the worker has started, it leaves the label on a
    running attempt, whose next failure is then retried without being counted.

    An escalation is finished the same way. Its relabel strips the count, so a
    failure whose escalation was cut short after that reads as attempt 0; the
    escalation's comment, posted first, says it is this claim's, and run again
    this escalates — it never starts the ladder over on an issue already
    handed to a human."""
    return _fail_claim(_run(a), a.instance, _agent(a), a.number, a.reason)


def _fail_claim(run: _Run, instance: str, agent: _Agent, number: int, reason: str) -> Obj:
    """`afk fail` — of `instance`'s claim on issue <number>, for `reason`; a retry's
    worker is started as `agent`."""
    cfg = run.cfg
    _require_mine(run, number, instance)
    issue = _issue(run.repo, number)
    reason = _stalled_reason(run.repo, number, reason)     # before the worktree is discarded
    labels = issue["labels"]
    decision = afk_decide.next_attempt(afk_decide.current_attempt(labels), cfg["retry"],
                                       counted=afk_decide.attempt_starting(labels),
                                       escalation=_escalation_begun(run, number))
    if decision["action"] == "escalate":
        return _escalate(run, instance, issue, decision["attempt"], reason)
    add, remove = afk_decide.retry_labels(labels, decision["to_label"])
    if add or remove:
        for label in add:
            _ensure_label(run.repo, label)
        _edit_labels(run.repo, number, add, remove)
        relabelled: IssueRead = {**issue, "labels": [lb for lb in labels if lb not in remove] + add}
        issue = relabelled
    worker = _start_worker(run, instance, agent, issue, "fresh", reason)
    return {"issue": number, "action": "retry", "attempt": decision["attempt"],
            "retry_max": cfg["retry"], "worker": worker}


def cmd_escalate(a: argparse.Namespace) -> Obj:
    """Hand one of my claims straight to a human, outside the retry ladder — where
    `afk no-pr` says `escalate` (`afk_decide.WORKER_CAUSES`: a DAG gap, a decision
    only the issue's owner can make, or a landing turn nobody could get a worker
    to perform). Same ordered transition `afk fail` ends in; the attempt count is
    reported, not consulted, and no attempt is spent: the PR stays open, the
    branch and the worktree stay — unless the branch holds no work at all, where
    the worktree is removed with its idle worker. After an unanswered nudge the worker's last
    screen is appended to the reason, as `afk fail` appends it."""
    return _escalate_claim(_run(a), a.instance, a.number, a.reason)


def _escalate_claim(run: _Run, instance: str, number: int, reason: str) -> Obj:
    """`afk escalate` — of `instance`'s claim on issue <number>, for `reason`."""
    _require_mine(run, number, instance)
    issue = _issue(run.repo, number)
    return _escalate(run, instance, issue, afk_decide.current_attempt(issue["labels"]),
                     _stalled_reason(run.repo, number, reason))


def cmd_park(a: argparse.Namespace) -> Obj:
    """Leave one of my claims waiting on the dependency its worker discovered
    (`afk no-pr` → `idle_blocked` / `park`), in one order (ADR-0022): record a
    native `blocked_by` edge to each blocker still open → status board → release
    the claim → remove the worktree, when its branch holds no work.

    The edge goes first: from then on the frontier excludes the issue for as long
    as a blocker is open, so the release puts nothing back on it — and returns the
    issue by itself the tick after the last one closes. `ready_label` and the
    attempt labels are not touched. The standings are read again here, and a
    claim `afk no-pr` would not call parkable now is refused untouched."""
    return _park_claim(_run(a), a.instance, a.number)


def _park_claim(run: _Run, instance: str, number: int) -> Obj:
    """`afk park` — of `instance`'s claim on issue <number>."""
    _require_mine(run, number, instance)
    declared = afk_decide.latest_verdict(_issue_comments(run.repo, number))
    standings = _blocker_standings(run, number, declared["blocked_by"], _open_prs(run.repo))
    refusal = afk_decide.park_refusal(declared, standings)
    if refusal:
        raise ValueError(f"issue #{number} is not parkable: {refusal}; nothing was changed")
    waiting = [b["number"] for b in standings if b["standing"] == "waiting"]
    idle = _workless_worktree(run, number)

    recorded = {e["number"] for e in _blocked_by(run.repo, number)}
    added = [n for n in waiting if n not in recorded]
    for n in added:
        _add_blocker(run.repo, number, n)
    _upsert_board(run, number, "parked", blocked_by=waiting)
    _release(run, number)
    cleanup = idle.remove() if idle else None
    return {"issue": number, "action": "parked", "blocked_by": waiting, "edges_added": added,
            "released": True, **({"cleanup": cleanup} if cleanup else {})}


def cmd_close(a: argparse.Namespace) -> Obj:
    """Close one of my claims whose issue needed no change (`afk no-pr` →
    `idle_done`), after the tick has verified the empty diff against base: status
    board → close the issue → release the claim → remove the worktree."""
    run = _run(a)
    _require_mine(run, a.number, a.instance)
    _upsert_board(run, a.number, "closed", instance=a.instance)
    _close_issue(run.repo, a.number)
    _release(run, a.number)
    wt = _Worktree.of_issue(run.repo, a.number)
    cleanup = wt.remove() if wt.remembered else None
    return {"issue": a.number, "action": "closed", "released": True,
            **({"cleanup": cleanup} if cleanup else {})}


# --------------------------------------------------------------------------- #
# arg wiring                                                                  #
# --------------------------------------------------------------------------- #

def _count(text: str) -> int:
    """A flag's value that counts something: an integer, never negative."""
    n = int(text)
    if n < 0:
        raise argparse.ArgumentTypeError(f"{text} is negative")
    return n


class _Parser(argparse.ArgumentParser):
    """argparse whose usage errors are the CLI's one error shape — `{"error": …}`,
    exit 3 — so a missing `--config` reads exactly like any other failure."""

    subcommands: dict       # subcommand name → its parser (`build_parser`)

    def error(self, message: str) -> NoReturn:
        print(json.dumps({"error": f"{self.prog}: {message}"}, ensure_ascii=False))
        sys.exit(3)


def build_parser() -> _Parser:
    """The whole CLI. `.subcommands` maps each subcommand name to its parser —
    the interface the docs are checked against (test_afk_cli.py)."""
    ap = _Parser(prog="afk", description="afk-fleet deterministic tool")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ap.subcommands = sub.choices

    def command(name: str, fn: Callable[[argparse.Namespace], Mapping[str, Any]], help: str,
                remote: Literal["refs", "gh"] | None = None,
                needs_config: bool = True) -> argparse.ArgumentParser:
        """One subcommand. All but the two that run before a config exists
        (`needs_config=False`) require --config and take --set / --now. `remote`
        adds the repo handle: "refs" for git-ref ops (--repo, else --remote),
        "gh" when gh needs it (--repo required)."""
        p = sub.add_parser(name, help=help)
        p.set_defaults(fn=fn)
        if needs_config:
            p.add_argument("--config", required=True,
                           help="the run's config JSON, from `afk config` / `afk probe` (keys "
                                "it omits fall back to the defaults table — ADR-0009)")
            p.add_argument("--set", action="append", metavar="KEY=VALUE",
                           help="override one config key for this call, e.g. "
                                "concurrency=1 or gate.ci=local (repeatable; "
                                "for tests and hand-debugging)")
            p.add_argument("--now", type=int, default=None, help="epoch override (tests)")
        if remote:
            p.add_argument("--repo", default=None, required=(remote == "gh"),
                           help="owner/name of the target repo — the one repo handle: git-ref "
                                "ops push/fetch to its GitHub URL, gh ops read it directly")
        if remote == "refs":
            p.add_argument("--remote", default="origin",
                           help="git remote for ref ops when --repo is not given")
        return p

    def mine(p: argparse.ArgumentParser) -> None:
        p.add_argument("--instance", required=True, metavar="id", help="my fleet instance id")

    def stamp(p: argparse.ArgumentParser) -> None:
        mine(p)
        p.add_argument("--host", default=socket.gethostname())

    def issue(p: argparse.ArgumentParser, required: bool = True) -> None:
        p.add_argument("--issue", dest="number", type=int, required=required, default=None,
                       metavar="n")

    def starts_worker(p: argparse.ArgumentParser) -> None:
        """The flags of a subcommand that may start a worker — one of
        `afk_decide.STARTS_WORKER`, which is what a judgment's command is
        written from."""
        assert p.prog.split()[-1] in afk_decide.STARTS_WORKER, p.prog
        stamp(p)
        p.add_argument("--worker-command", required=True, metavar="cmd",
                       help="the run's worker launch command, verbatim (ADR-0010)")
        ready_timeout(p)

    # A default two subcommands take is defined once, here: the flag is added by
    # one function, so the subcommands that take it cannot disagree.
    def ready_timeout(p: argparse.ArgumentParser) -> None:
        p.add_argument("--ready-timeout", type=int, default=120, metavar="s",
                       help="seconds to wait for a started agent to accept a prompt "
                            "(default %(default)s)")

    def gate_timeout(p: argparse.ArgumentParser) -> None:
        p.add_argument("--gate-timeout", type=int, default=1800, metavar="s",
                       help="seconds before the local gate is called red (default %(default)s)")

    # --- bootstrap ---
    p = command("config", cmd_config, "parse + validate the repo config file → canonical JSON",
                needs_config=False)
    p.add_argument("--file", default=None, metavar="path", help="path to the target repo's docs/agents/afk-fleet.md")
    p.add_argument("--defaults", action="store_true", help="print the pure defaults table")

    p = command("probe", cmd_probe, remote="refs",
                help="bootstrap probe: the usable claim namespace and the base branch (both "
                     "folded into the returned config), and that branch's protection when "
                     "gate.ci is local")
    p.add_argument("--base-branch", default=None, metavar="name",
                   help="the branch every PR of this run lands on, as the human confirmed it "
                        "at this launch (ADR-0042); without it the probe reports what to ask")

    p = command("worker-command", cmd_worker_command, needs_config=False,
                help="settle the command workers are started with: ask-or-not + candidates, "
                     "or --check a human's answer")
    p.add_argument("--check", default=None, metavar="cmd",
                   help="a candidate command: resolve its first word in the login shell "
                        "and report whether it runs (and looks unattended)")

    # --- the worker's own ---
    p = command("gate", cmd_gate, remote="refs",
                help="a WORKER's run of the local gate, in the worktree it is called from: "
                     "the log streams to its terminal, and a green run on a committed tree "
                     "is put on record on the remote for `afk land`")
    gate_timeout(p)

    p = command("land", cmd_land, remote="gh",
                help="a WORKER lands its own PR on its landing turn, in the worktree it is "
                     "called from: sync → push → gate → merge pinned to the gated head → "
                     "status board; refused without the turn. --batch: a merge batch's "
                     "worker stacks, gates once and lands its whole batch")
    issue(p, required=False)
    p.add_argument("--batch", dest="batch_id", default=None, metavar="batch",
                   help="land a merge batch, in the batch's worktree, instead of one PR: "
                        "the batch's id, as its brief gives it")
    gate_timeout(p)
    p.add_argument("--excerpt-lines", type=_count, default=afk_decide.GATE_EXCERPT_LINES,
                   metavar="k", help="how many trailing log lines a red gate's excerpt keeps "
                                     "(default %(default)s; 0: none)")
    p.add_argument("--merged-timeout", type=int, default=60, metavar="s",
                   help="--batch: seconds to wait for GitHub to show the landed PRs merged "
                        "before leaving their branches in place (default %(default)s)")
    p.add_argument("--checks-timeout", type=int, default=1800, metavar="s",
                   help="gate.ci required: seconds to wait for the checks on the head that "
                        "would land before stopping with awaiting_ci (default %(default)s)")
    p.add_argument("--checks-poll", type=float, default=15, metavar="s",
                   help="seconds between looks at those checks (default %(default)s)")

    # --- the cycle ---
    p = command("cycle", cmd_cycle, remote="gh",
                help="one whole cycle: tick-or-skip, and on a tick the pass itself — rebuild "
                     "and every routed transition — returning the state, the sleep, a "
                     "progress line and the judgments it could not make")
    p.add_argument("--state", default=None, metavar="json",
                   help="the `state` the previous `afk cycle` returned, verbatim (omit on "
                        "the first cycle, which always ticks)")
    p.add_argument("--instance", default=None, metavar="id",
                   help="my fleet instance id — the first cycle only; then --state carries it")
    p.add_argument("--host", default=socket.gethostname())
    p.add_argument("--worker-command", default=None, metavar="cmd",
                   help="the run's worker launch command, verbatim (ADR-0010) — the first "
                        "cycle only; then --state carries it")
    ready_timeout(p)
    p.add_argument("--drain", action="store_true",
                   help="the last cycle of a run: release my claims with no open PR, keep "
                        "the rest, and do nothing else")
    p.add_argument("--wake", action="store_true",
                   help="a wake arrived while the previous cycle was running: tick whatever "
                        "the fingerprint says")

    # --- claim refs ---
    command("scan", cmd_scan, "debug: read all claim + heartbeat refs", remote="refs")

    p = command("classify-claims", cmd_classify_claims, remote="refs",
                help="debug: partition claims into mine/peer_live/stale (rebuild does this)")
    mine(p)

    p = command("claim", cmd_claim, remote="refs",
                help="low-level: atomically create a claim ref → {won} (dispatch does this)")
    p.add_argument("number", type=int, metavar="n")
    stamp(p)

    p = command("reclaim", cmd_reclaim, "force-with-lease take of a stale claim → {won}",
                remote="refs")
    p.add_argument("number", type=int, metavar="n")
    stamp(p)
    p.add_argument("--expect-sha", required=True, metavar="sha", help="the sha you read; the take fails if it moved")

    p = command("takeover", cmd_takeover, remote="refs",
                help="list the fleet instances GitHub remembers, or force-take a dead "
                     "one's claims (skips the staleness gate)")
    stamp(p)
    p.add_argument("--list", action="store_true",
                   help="show every discoverable instance: heartbeat age, host, claim count")
    p.add_argument("--from", dest="source", default=None, metavar="dead-id",
                   help="the dead instance whose claims to take")
    p.add_argument("--yes", action="store_true",
                   help="confirm a takeover of an instance whose heartbeat is still FRESH "
                        "(it looks alive; you are asserting you know it is dead)")

    p = command("release", cmd_release, remote="refs",
                help="delete a claim ref of mine (idempotent) — or, with --expect-sha, a "
                     "dead peer's on a closed issue; a claim of mine whose issue is "
                     "closed has its worktree removed too")
    p.add_argument("number", type=int, metavar="n")
    mine(p)
    p.add_argument("--expect-sha", default=None, metavar="sha",
                   help="release a `stale_closed` row: the sha rebuild reported; the delete "
                        "fails if the claim moved")

    p = command("heartbeat", cmd_heartbeat, "refresh my heartbeat if due", remote="refs")
    mine(p)

    # --- observation ---
    p = command("rebuild", cmd_rebuild, "gather + assemble the tick's working set (read-only)",
                remote="gh")
    mine(p)

    p = command("no-pr", cmd_no_pr, remote="gh",
                help="why my claims have no PR (or have not landed on their landing turn) → "
                     + " / ".join(dict.fromkeys(o for o, _ in afk_decide.NO_PR_ROUTES))
                     + " for each, read from orca's worker state and decided in one call")
    p.add_argument("--issue", dest="numbers", type=int, action="append", default=None,
                   metavar="n", help="one of my claims waiting on its worker; repeat for each")
    p.add_argument("--batch", dest="batch_id", default=None, metavar="batch",
                   help="ask after the worker of the merge batch that holds the turn, instead")
    p.add_argument("--worktree", default=None, metavar="path",
                   help="the worker's worktree, to override the one orca reports (one --issue)")

    p = command("recovery", cmd_recovery, remote="refs",
                help="read-only: does a dead claim have recoverable progress, and where? → "
                     "the tiered continuation verdict `afk dispatch` would act on")
    issue(p)
    p.add_argument("--branch", default=None, metavar="branch",
                   help="the issue's work branch, when already known (skips discovery)")
    p.add_argument("--worktree", default=None, metavar="path",
                   help="path to the issue's worktree, when already known (skips the orca read)")
    p.add_argument("--no-worktree", action="store_true",
                   help="assert no local worktree survives (skips the orca read)")

    # --- act ---
    p = command("dispatch", cmd_dispatch, remote="gh",
                help="start a worker on an issue: claim → worktree at the right commit → "
                     "agent → submitted prompt → status board, in one call")
    issue(p)
    starts_worker(p)
    p.add_argument("--start", choices=["auto", "fresh"], default="auto",
                   help="auto: continue from whatever progress survives (default); fresh: "
                        "discard the previous attempt and start from base")

    p = command("turn", cmd_turn, remote="gh",
                help="give one of my claims' PRs the landing turn — one at a time: record it "
                     "on the PR → tell the worker to land it (or continue a dead one onto "
                     "the turn) → status board, or the outcome that needs the tick's "
                     "judgment. --batch gives it to a merge batch; --abandon gives a batch up")
    issue(p, required=False)
    starts_worker(p)
    p.add_argument("--batch", dest="form_batch", action="store_true",
                   help="give the turn to a merge batch: form one from the PRs waiting for "
                        "it, or continue the one that holds it and has no worker")
    p.add_argument("--abandon", default=None, metavar="batch",
                   help="give a merge batch up with nothing landed: its PRs go back to "
                        "waiting and take single turns")
    p.add_argument("--verified", default=None, metavar="head",
                   help="the head sha an adversarial verify passed (gate.adversarial_verify_prompt)")
    p.add_argument("--allow-no-checks", action="store_true",
                   help="gate.ci required: let a PR that has no checks at all land (the tick's "
                        "progressive-gate judgment)")
    p.add_argument("--restart", action="store_true",
                   help="the PR holds the turn and its worker is silent after its nudge: close "
                        "that session and start a worker onto the turn by continuation, in the "
                        "same worktree — once per turn; nothing is closed, deleted or counted")

    p = command("nudge", cmd_nudge, remote="gh",
                help="tell one of my workers that stopped without an outcome to carry on — "
                     "once, spending no attempt; the next silence is a failure")
    issue(p, required=False)
    mine(p)
    p.add_argument("--batch", dest="batch_id", default=None, metavar="batch",
                   help="nudge the worker of a merge batch instead of an issue's")
    p.add_argument("--worktree", default=None, metavar="path",
                   help="the worker's worktree, to override the one orca reports for the issue")

    p = command("fail", cmd_fail, remote="gh",
                help="one of my claims failed: count the attempt, then retry it fresh or "
                     "escalate it — the whole retry ladder in one call")
    issue(p)
    starts_worker(p)
    p.add_argument("--reason", required=True, metavar="text",
                   help="why it failed, re-read from where it lives: handed to the retry's "
                        "worker, or — when the attempts are exhausted — commented for a human")

    p = command("escalate", cmd_escalate, remote="gh",
                help="hand one of my claims to a human: status board → relabel → comment → "
                     "release")
    issue(p)
    mine(p)
    p.add_argument("--reason", required=True, metavar="text", help="the stuck point, worded for a human")

    p = command("park", cmd_park, remote="gh",
                help="leave one of my claims waiting on the open blockers its worker named: "
                     "record the dependency → status board → release → cleanup")
    issue(p)
    mine(p)

    p = command("close", cmd_close, remote="gh",
                help="close one of my claims whose issue needed no change: status board → "
                     "close → release → cleanup")
    issue(p)
    mine(p)

    p = command("status", cmd_status, remote="gh",
                help="upsert one claim's status board at a non-terminal phase (idempotent)")
    p.add_argument("number", type=int, metavar="n")
    p.add_argument("--phase", required=True, choices=list(afk_decide.STATUS_PHASES), metavar="phase",
                   help="the lifecycle phase — a `mine` row's board_phase")
    p.add_argument("--instance", default=None, metavar="id", help="owning fleet instance id (shown in the header)")
    p.add_argument("--pr", type=int, default=None, metavar="pr", help="the PR number, once one is open")
    p.add_argument("--attempt", type=int, default=0, metavar="k",
                   help="the `mine` row's attempt (shown for ci_failed)")

    return ap


def main() -> None:
    a = build_parser().parse_args()
    try:
        result = a.fn(a)
    except _FAILURES as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False))
        sys.exit(3)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0)


if __name__ == "__main__":
    main()

