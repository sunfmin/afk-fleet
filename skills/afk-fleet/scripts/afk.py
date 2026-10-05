#!/usr/bin/env python3
"""
afk.py — the afk-fleet tool: deterministic muscle the LLM tick calls.

The tick (an LLM) orchestrates and judges; when it needs a *deterministic* action
it shells out to one of these subcommands and reads back JSON (ADR-0004).
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
fetched base tip" is code, not a paragraph.

Two subcommands are a worker's, not the tick's, run in its own worktree: `gate`
runs the local gate and puts a green run on record (ADR-0026), and `land` lands
the worker's PR on its landing turn — sync → gate → merge pinned to the gated
head — which is the only way a PR lands (ADR-0027).

Every subcommand that reads config REQUIRES the same `--config` (the canonical
JSON from `afk config`, then `afk probe`) and resolves it one way, in `_cfg`:
`--set key=value` → `--config` → CONFIG_DEFAULTS for the keys it omits (ADR-0009).

Invoked as:  <skill>/scripts/afk.py <subcommand> [flags]
"""
import argparse
import json
import os
import shlex
import socket
import subprocess
import sys
import time

import afk_decide

# --------------------------------------------------------------------------- #
# config + clock                                                              #
# --------------------------------------------------------------------------- #

def _cfg(a):
    """The effective config for a subcommand: the `--config` JSON (canonical or
    partial) resolved through CONFIG_DEFAULTS, any `--set key=value` laid on top,
    then validated — no subcommand runs on a config `afk config` would refuse."""
    cfg = afk_decide.resolve_config(json.loads(a.config))
    return afk_decide.validate_config(afk_decide.override_config(cfg, a.set))


def _now(a):
    return a.now if a.now is not None else int(time.time())


# --------------------------------------------------------------------------- #
# git/gh plumbing                                                             #
# --------------------------------------------------------------------------- #

# A stable identity for the tiny marker commits (claims/heartbeats carry no code).
_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "afk-fleet", "GIT_AUTHOR_EMAIL": "afk@fleet.local",
    "GIT_COMMITTER_NAME": "afk-fleet", "GIT_COMMITTER_EMAIL": "afk@fleet.local",
}

_SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_WORKER_PROMPT = os.path.join(_SKILL, "references", "worker-prompt.md")

_LOCAL_SCAN = "refs/afk-scan"  # where `scan` mirrors remote refs, read-only, disposable
_LOCAL_RECOVERY = "refs/afk-recovery"  # ditto for `recovery`'s branch-vs-base compare


def _claim_ref(cfg, number):
    return f"{afk_decide.CLAIM_NAMESPACES[cfg['claim_namespace']][0]}/{number}"


def _heartbeat_ref(cfg, instance):
    return f"{afk_decide.CLAIM_NAMESPACES[cfg['claim_namespace']][1]}/{instance}"


def _remote(a):
    """The git push/fetch target. `--repo owner/name` → its GitHub URL, so any ref
    op works from anywhere the gh commands do — no clone-with-the-right-origin
    required (falls back to the named `--remote`, default origin). One repo handle,
    whether git or gh reads it."""
    return f"https://github.com/{a.repo}.git" if a.repo else a.remote


def _git(args, check=True):
    p = subprocess.run(["git", *args], capture_output=True, text=True, env=_GIT_ENV)
    if check and p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {p.stderr.strip()}")
    return p


def _gh(args, check=True):
    p = subprocess.run(["gh", *args], capture_output=True, text=True, env=_GIT_ENV)
    if check and p.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args[:2])} failed: {p.stderr.strip()}")
    return p


def _marker_commit(kind, instance, ts, host=None):
    """A parentless commit on the empty tree whose subject is the marker
    `<kind> instance=<id> [host=<host>] ts=<epoch>` (`_parse_marker` reads it
    back). Its sha is what we push to a ref; it drags no repo history along."""
    parts = [kind, f"instance={instance}", *([f"host={host}"] if host else []), f"ts={int(ts)}"]
    empty_tree = _git(["hash-object", "-t", "tree", "/dev/null"]).stdout.strip()
    return _git(["commit-tree", empty_tree, "-m", " ".join(parts)]).stdout.strip()


def _parse_marker(subject):
    """`afk-claim instance=abc host=mac ts=123` → {'instance':'abc','host':'mac','ts':123}."""
    out = {}
    for tok in (subject or "").split():
        if "=" in tok:
            k, v = tok.split("=", 1)
            out[k] = int(v) if (k == "ts" and v.isdigit()) else v
    return out


def _read_marker(remote, refname):
    """Fetch one ref by name and return its parsed marker, or None if it could
    not be fetched (absent, or the remote is unreachable)."""
    p = _git(["fetch", remote, refname], check=False)
    if p.returncode != 0:
        return None
    subject = _git(["log", "-1", "--format=%s", "FETCH_HEAD"], check=False).stdout.strip()
    return _parse_marker(subject)


def _remote_sha(remote, refname):
    """The sha the remote has for one ref, "" if it has none. Raises when the
    remote cannot be read at all."""
    out = _git(["ls-remote", remote, refname]).stdout.split()
    return out[0] if out else ""


def _fetch_tip(rem, branch, cwd=None):
    """The sha at the tip of `branch` on the remote, with its objects fetched into
    this repo — what a worktree is created at, so a worker starts from what the
    REMOTE has now, never from a local branch that may be commits behind."""
    sha = _remote_sha(rem, f"refs/heads/{branch}")
    if not sha:
        raise RuntimeError(f"the remote has no branch {branch!r}")
    _git([*(["-C", cwd] if cwd else []), "fetch", "--quiet", rem, f"refs/heads/{branch}"])
    return sha


class OrcaError(RuntimeError):
    """An orca call that ran and answered `ok: false`; `code` is orca's own."""

    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


def _orca(args, timeout=60):
    """One `orca … --json` call → its `result` object. HARD: raises when orca
    cannot be run, exits non-zero, or answers `ok: false` — the Act half cannot
    start, find or remove a worker's worktree without it (ADR-0005). The one soft
    read is `_orca_worktree_rows`, which recovery must survive without."""
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


def _issue(repo, number):
    """One issue as {"number", "title", "state", "labels": [name...]}. Raises when
    it cannot be read."""
    p = _gh(["api", f"repos/{repo}/issues/{number}",
             "--jq", "{title, state, labels: [.labels[].name]}"])
    return {"number": number, **json.loads(p.stdout)}


_PR_FIELDS = "number,headRefName,headRefOid,updatedAt,statusCheckRollup,closingIssuesReferences"


def _open_prs(repo):
    """Every open PR, with the fields the working set, the merge and a fresh
    start all read."""
    return json.loads(_gh(["pr", "list", "--repo", repo, "--state", "open",
                           "--json", _PR_FIELDS]).stdout)


def _issue_comments(repo, number):
    """An issue's comments, oldest first, as [{"id", "body", "url"}...]."""
    p = _gh(["api", "--paginate", f"repos/{repo}/issues/{number}/comments",
             "--jq", ".[] | {id, body, url: .html_url}"])
    return [json.loads(ln) for ln in p.stdout.splitlines() if ln.strip()]


def _issue_state(repo, number):
    """"open" | "closed", or None if the issue could not be read."""
    p = _gh(["api", f"repos/{repo}/issues/{number}", "--jq", ".state"], check=False)
    return p.stdout.strip() or None if p.returncode == 0 else None


def _blocker(repo, number):
    """One issue a `blocked` verdict names, as `afk_decide.blocker_standings` reads
    it: {"state", "state_reason", "labels": [name...], "pull_request"}. None
    when it cannot be read — which is never "closed"."""
    p = _gh(["api", f"repos/{repo}/issues/{number}", "--jq",
             "{state, state_reason, labels: [.labels[].name], "
             "pull_request: (.pull_request != null)}"], check=False)
    return json.loads(p.stdout) if p.returncode == 0 and p.stdout.strip() else None


def _blocked_by(repo, number):
    """The issues GitHub records issue <number> as blocked by — its native
    dependency edges — as [{"number", "state"}...]. Raises when it cannot be read."""
    p = _gh(["api", "--paginate", f"repos/{repo}/issues/{number}/dependencies/blocked_by",
             "--jq", ".[] | {number, state}"])
    return [json.loads(ln) for ln in p.stdout.splitlines() if ln.strip()]


def _turn(repo, pr):
    """The landing turn recorded on a PR (`afk_decide.latest_turn`), whoever
    granted it; None when it has none. One comments read."""
    return afk_decide.latest_turn(_issue_comments(repo, pr["number"]))


def _held_turns(repo, prs, claims, instance):
    """{issue number: turn} for every claim of `instance` whose open PR holds its
    landing turn (`afk_decide.held_turn`). Keyed on claims, so a PR whose claim
    was released (escalated, parked) holds nothing; one comments read per claim
    of `instance` that has a PR."""
    held = {}
    for c in claims:
        pr = afk_decide.closing_pr(prs, c["number"]) if c["instance"] == instance else None
        turn = afk_decide.held_turn(_turn(repo, pr), instance) if pr else None
        if turn:
            held[c["number"]] = turn
    return held


def _record_turn(repo, pr, body, prev):
    """Write a PR's ONE landing-turn comment → its id: `prev` (`_turn` of the PR)
    is rewritten when there is one, whoever wrote it, so a PR never carries two
    turns to tell apart."""
    if prev:
        _gh(["api", "--method", "PATCH", f"repos/{repo}/issues/comments/{prev['comment_id']}",
             "-f", f"body={body}"])
        return prev["comment_id"]
    p = _gh(["api", "--method", "POST", f"repos/{repo}/issues/{pr['number']}/comments",
             "-f", f"body={body}"])
    return json.loads(p.stdout).get("id")


def _orca_worktree_rows():
    """The `result.worktrees` rows of `orca worktree list --json`, or [] when orca
    can't be reached. SOFT by design: the worktree signal is one input to a tiered
    recovery whose last tier needs no orca at all, so a machine without orca (or a
    momentarily unhappy one) must degrade to "no local worktree", never abort a
    recovery."""
    try:
        p = subprocess.run(["orca", "worktree", "list", "--json"],
                           capture_output=True, text=True, timeout=30)
        return list(json.loads(p.stdout)["result"]["worktrees"]) if p.returncode == 0 else []
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
        return []


def _worktree_progress(wt, rem, base):
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


def _remote_heads(remote):
    """Every branch name on the remote (one `ls-remote`). Raises when the remote
    cannot be read — an unreadable remote is not one with no branches."""
    rows = [ln.split() for ln in _git(["ls-remote", "--heads", remote]).stdout.splitlines()]
    return [r[1][len("refs/heads/"):] for r in rows
            if len(r) == 2 and r[1].startswith("refs/heads/")]


def _branch_ahead(remote, branch, base, slot):
    """How many commits `branch` is ahead of `base` ON THE REMOTE — the tier-2
    signal — or None when the branch was never pushed. git only (no gh), mirrored
    into a disposable local namespace, so it reads the same whether the remote is
    a GitHub URL or a bare path. `slot` (the issue number) keeps those temp refs
    per-issue, so two recoveries sharing one clone cannot read each other's mirror.
    Raises when the remote cannot be read or has no `base`: "could not look" must
    never read as "nothing pushed", which is what sends a claim to tier 3."""
    if not _remote_sha(remote, f"refs/heads/{branch}"):
        return None
    ours = f"{_LOCAL_RECOVERY}/{slot}"
    _git(["fetch", "--force", remote,
          f"refs/heads/{branch}:{ours}/branch", f"refs/heads/{base}:{ours}/base"])
    return int(_git(["rev-list", "--count", f"{ours}/base..{ours}/branch"]).stdout.strip())


def _newest_mtime(root):
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

def _mirrored_markers(local_ns):
    """(ref's last path segment, sha, parsed marker) for each mirrored ref."""
    rows = _git(["for-each-ref", "--format=%(refname) %(objectname)", local_ns],
                check=False).stdout.splitlines()
    for row in rows:
        refname, sha = row.split(" ", 1)
        subject = _git(["log", "-1", "--format=%s", sha], check=False).stdout.strip()
        yield refname.rsplit("/", 1)[-1], sha, _parse_marker(subject)


def _scan(remote, ns):
    """Mirror the remote claim+heartbeat refs into a disposable local namespace and
    read every marker. Returns (claims, heartbeats). Raises when the remote cannot
    be read: a fleet whose claims are unreadable must not look like one holding none."""
    claim_ns, hb_ns = afk_decide.CLAIM_NAMESPACES[ns]
    _git(["fetch", "--prune", remote,
          f"+{claim_ns}/*:{_LOCAL_SCAN}/claim/*",
          f"+{hb_ns}/*:{_LOCAL_SCAN}/heartbeat/*"])
    claims = [{"number": int(name), "instance": m.get("instance"), "host": m.get("host"),
               "ts": m.get("ts"), "sha": sha}
              for name, sha, m in _mirrored_markers(f"{_LOCAL_SCAN}/claim") if name.isdigit()]
    heartbeats = {name: m["ts"]
                  for name, _, m in _mirrored_markers(f"{_LOCAL_SCAN}/heartbeat") if "ts" in m}
    return claims, heartbeats


def cmd_scan(a):
    claims, heartbeats = _scan(_remote(a), _cfg(a)["claim_namespace"])
    return {"claims": claims, "heartbeats": heartbeats}


def cmd_classify_claims(a):
    cfg, now = _cfg(a), _now(a)
    claims, heartbeats = _scan(_remote(a), cfg["claim_namespace"])
    return {**afk_decide.classify_claims(claims, heartbeats, a.instance, now,
                                         cfg["claim_lease_ttl_seconds"]),
            "now": now}


def _claim(rem, cfg, number, instance, now, host):
    """Atomically create one claim ref → {"won", …}; `won: false` names the `owner`."""
    ref = _claim_ref(cfg, number)
    sha = _marker_commit("afk-claim", instance, now, host=host)
    # Create-only: the server rejects a ref that already exists → that is the CAS.
    p = _git(["push", rem, f"{sha}:{ref}"], check=False)
    if p.returncode == 0:
        return {"won": True, "issue": number, "ref": ref, "sha": sha, "instance": instance}
    owner = _read_marker(rem, ref)  # who beat us
    if owner is None:
        raise RuntimeError(f"claim push to {ref} failed and no such claim exists on the "
                           f"remote, so this is not a lost race: {p.stderr.strip()}")
    return {"won": False, "issue": number, "ref": ref,
            "owner": owner, "detail": p.stderr.strip()}


def cmd_claim(a):
    return _claim(_remote(a), _cfg(a), a.number, a.instance, _now(a), a.host)


def _force_take(rem, ref, number, expect_sha, instance, now, host):
    """The atomic re-stamp of ONE existing claim ref to `instance`: rejected unless
    the ref still points at the sha we read. The single mechanism behind both an
    unattended stale reclaim and a human-authorized takeover — they differ only in
    what gates the *choice* of claim (an expired lease vs a present human), never
    in the push, so a takeover is exactly as safe against a live peer."""
    sha = _marker_commit("afk-claim", instance, now, host=host)
    p = _git(["push", rem, f"--force-with-lease={ref}:{expect_sha}", f"{sha}:{ref}"], check=False)
    if p.returncode == 0:
        return {"won": True, "issue": number, "ref": ref, "sha": sha, "instance": instance}
    if _remote_sha(rem, ref) == expect_sha:
        raise RuntimeError(f"reclaim push to {ref} failed although the claim has not moved, "
                           f"so this is not a lost race: {p.stderr.strip()}")
    return {"won": False, "issue": number, "ref": ref, "detail": p.stderr.strip()}


def cmd_reclaim(a):
    return _force_take(_remote(a), _claim_ref(_cfg(a), a.number), a.number, a.expect_sha,
                       a.instance, _now(a), a.host)


def cmd_takeover(a):
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
    cfg, rem, now = _cfg(a), _remote(a), _now(a)
    ttl = cfg["claim_lease_ttl_seconds"]
    claims, heartbeats = _scan(rem, cfg["claim_namespace"])

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
        r = _force_take(rem, _claim_ref(cfg, c["number"]), c["number"], c["sha"],
                        a.instance, now, a.host)
        (taken if r["won"] else lost).append(r)
    return {**plan, "action": "taken", "as": a.instance,
            "taken": [t["issue"] for t in taken], "lost": lost,
            "detail": f"took {len(taken)}/{len(plan['claims'])} claim(s) from {a.source}"
                      + ("; a lost one means that fleet is not dead — its ref moved under us"
                         if lost else "")}


def _release(rem, cfg, number):
    """Delete one claim ref. Already gone counts as released — idempotent cleanup.
    A delete that failed with the claim still there is a phantom lock in the
    making, so it raises."""
    ref = _claim_ref(cfg, number)
    p = _git(["push", rem, "--delete", ref], check=False)
    if p.returncode != 0 and _remote_sha(rem, ref):
        raise RuntimeError(f"release failed and {ref} is still on the remote: "
                           f"{p.stderr.strip()}")
    return {"released": True, "issue": number, "ref": ref}


def _clear(rem, cfg, number, expect_sha):
    """Delete one claim ref that is NOT mine — a `stale_closed` row — only while it
    still points at the sha rebuild read: the same lease a reclaim takes it under,
    so a claim somebody took meanwhile is never deleted from under them. Already
    gone counts as released; a claim that moved raises."""
    ref = _claim_ref(cfg, number)
    p = _git(["push", rem, f"--force-with-lease={ref}:{expect_sha}", f":{ref}"], check=False)
    now_at = "" if p.returncode == 0 else _remote_sha(rem, ref)
    if now_at == expect_sha:
        raise RuntimeError(f"release failed and {ref} is still on the remote: "
                           f"{p.stderr.strip()}")
    if now_at:
        raise RuntimeError(f"{ref} moved since it was read (expected {expect_sha}, now "
                           f"{now_at}): somebody took the claim; nothing was changed")
    return {"released": True, "issue": number, "ref": ref}


def cmd_release(a):
    """Delete one claim ref on its own, for the cases no transition covers:

      afk release <n> --instance <id>
          a claim of MINE — an orphan-release, a `closed` row, the drain. Refuses
          a claim another instance holds. With `--repo`, a claim whose issue is
          CLOSED — the `closed` row: its worker landed the PR, and `afk land`
          can neither release the claim nor remove the worktree it runs in — has
          its worktree removed too (`cleanup`, when `worktree_cleanup`). An open
          issue's worktree is never touched: it may hold work.
      afk release <n> --instance <id> --expect-sha <sha>
          a `stale_closed` row of rebuild — a dead peer's claim on an issue that
          is already closed: a phantom lock, deleted instead of reclaimed."""
    cfg, rem = _cfg(a), _remote(a)
    if a.expect_sha:
        return _clear(rem, cfg, a.number, a.expect_sha)
    ref = _claim_ref(cfg, a.number)
    owner = _claim_owner(rem, ref)
    if owner not in (None, a.instance):
        raise RuntimeError(f"issue #{a.number} is not this fleet's claim ({ref} is held by "
                           f"{owner!r}); nothing was changed. A dead peer's "
                           f"claim on a closed issue — a `stale_closed` row — is released "
                           f"with --expect-sha <the sha rebuild reported>")
    released = _release(rem, cfg, a.number)
    if a.repo and cfg["worktree_cleanup"] and _issue_state(a.repo, a.number) == "closed":
        path, _ = _issue_worktree(a.repo, a.number)
        if path:
            released["cleanup"] = _remove_worktree(path)
    return released


def _claim_owner(rem, ref):
    """The instance id a claim ref is stamped with: None when there is no such
    claim, "" when there is one whose marker names nobody (or cannot be read) —
    which is never mine."""
    if not _remote_sha(rem, ref):
        return None
    return (_read_marker(rem, ref) or {}).get("instance") or ""


def _require_mine(rem, cfg, number, instance):
    """Refuse to settle a claim this fleet does not hold: every transition that
    merges, relabels or releases an issue acts on MY claim only (ADR-0003)."""
    ref = _claim_ref(cfg, number)
    owner = _claim_owner(rem, ref)
    if owner != instance:
        held = "not claimed at all" if owner is None else f"held by {owner!r}"
        raise RuntimeError(f"issue #{number} is not this fleet's claim ({ref} is {held}); "
                           f"nothing was changed")


def _beat(rem, cfg, instance, now):
    """Refresh my heartbeat ref if it is due (stateless: the old ts is read from
    the ref itself)."""
    ref = _heartbeat_ref(cfg, instance)
    last = (_read_marker(rem, ref) or {}).get("ts")
    if not afk_decide.heartbeat_due(last, now, cfg["claim_lease_ttl_seconds"]):
        return {"refreshed": False, "reason": "not due", "ts": last, "ref": ref}
    sha = _marker_commit("afk-heartbeat", instance, now)
    _git(["push", rem, "--force", f"{sha}:{ref}"])
    return {"refreshed": True, "ts": now, "ref": ref}


def cmd_heartbeat(a):
    return _beat(_remote(a), _cfg(a), a.instance, _now(a))


# --------------------------------------------------------------------------- #
# bootstrap: config / probe / worker-command                                   #
# --------------------------------------------------------------------------- #

def cmd_config(a):
    """One home for config (ADR-0009): read the target repo's config file (the
    ```yaml block in docs/agents/afk-fleet.md), validate every key against the
    schema (unknown key / wrong shape → error — with the human present at
    bootstrap), fill defaults, and print the canonical JSON the launcher
    injects into every tick. `--defaults` prints the pure defaults table."""
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


def _branch_protection(repo, branch):
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


def _usable_namespace(rem, wanted, now):
    """The first claim namespace the remote lets us push under — `wanted`, else
    the branch fallback — as (namespace, rejection|None). Only a
    push the SERVER rejected (an org ruleset forbidding `refs/afk/*`) moves on
    to the fallback; a push that never reached a verdict (auth, network) raises,
    because that says nothing about which namespace is allowed."""
    rejection = None
    for ns in dict.fromkeys([wanted, afk_decide.BRANCH_NAMESPACE]):
        ref = _claim_ref({"claim_namespace": ns}, "probe")
        sha = _marker_commit("afk-probe", "probe", now)
        p = _git(["push", rem, f"{sha}:{ref}"], check=False)
        if p.returncode == 0:
            _git(["push", rem, "--delete", ref], check=False)
            return ns, rejection
        if "[remote rejected]" not in p.stderr:
            raise RuntimeError(f"probe push to {ref} failed: {p.stderr.strip()}")
        rejection = rejection or p.stderr.strip()
    raise RuntimeError(f"the remote rejects claim refs under both {wanted} and "
                       f"{afk_decide.BRANCH_NAMESPACE}: {rejection}")


def cmd_probe(a):
    """The bootstrap compatibility probe — two questions, both answered with the
    human present so a misfit is fixed here rather than mid-run (ADR-0009's tradition):

    1. **Claim namespace** — can we push under the configured `claim_namespace`
       (`refs/afk`)? Else fall back to branches (`refs/heads/afk-claim/*`) and say
       so: `blocked`, with the server's rejection as `detail` — claim churn will
       then fire `on: push` CI. The result's `config` is the canonical config with
       the namespace that actually works: the launcher holds THAT config from here
       on, so every later call inherits the namespace through `--config`.
    2. **Branch protection** (only when `gate.ci: local`, ADR-0012) — does the merge
       target REQUIRE status checks? Then `gh pr merge` is rejected however green the
       local gate is, so that combination is a hard `error` at bootstrap; an
       inconclusive read is a `warn`."""
    cfg = _cfg(a)
    ns, rejection = _usable_namespace(_remote(a), cfg["claim_namespace"], _now(a))
    cfg["claim_namespace"] = ns
    result = {"blocked": rejection is not None, "config": cfg}
    if rejection:
        result["detail"] = rejection

    ci_mode, target = cfg["gate"]["ci"], cfg["merge"]["target"]
    if ci_mode == "local":
        if not a.repo:
            result["protection"] = {"branch": target, "verdict": "warn", "required_checks": [],
                                    "detail": "gate.ci is 'local' but --repo was not given, so "
                                              "branch protection could not be checked"}
        else:
            prot, unavailable = _branch_protection(a.repo, target)
            result["protection"] = {"branch": target,
                                    **afk_decide.protection_verdict(ci_mode, prot, unavailable)}
    return result


def _login_shell(script, timeout=20):
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


def cmd_worker_command(a):
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

def _gather(a, cfg):
    """The ONE gatherer of the observable fleet inputs (ADR-0008): open issues,
    open PRs, and the claim/heartbeat ref scan. Both `rebuild` and the `cycle`
    gate read through here, so their views cannot drift. Issues leave here
    with `labels` as a list of names — the one shape afk_decide reads; the raw
    JSON lives and dies in this process."""
    issues = json.loads(_gh(["issue", "list", "--repo", a.repo, "--state", "open",
                             "--limit", "200", "--json", "number,title,labels,updatedAt"]).stdout)
    issues = [{**i, "labels": [lb["name"] for lb in i.get("labels") or []]} for i in issues]
    claims, heartbeats = _scan(_remote(a), cfg["claim_namespace"])
    return issues, _open_prs(a.repo), claims, heartbeats


def cmd_cycle(a):
    """One cycle, whole, in one call (ADR-0017): digest what a rebuild would
    observe (ADR-0007) → tick-or-skip, and on a tick RUN it — the rebuild and
    every transition whose next step is a table lookup (`_tick`) — then fold
    what it did into the state and return the sleep.

      {"action": "tick"|"skip", "reason", "state", "sleep_seconds", "progress",
       "judgments": [...]}        (+ "heartbeat" on a skip that holds claims,
                                   "errors" when a transition of the tick failed)

    `state` is opaque to the caller: it hands back the last one verbatim, and
    after the first cycle that is where the instance id and the worker launch
    command come from. No `--state` is a first cycle, which always ticks. What the
    tick could not decide comes back as `judgments`, each with the `afk` command
    for either answer; the caller runs one and opens the next cycle at once
    (`sleep_seconds` is then 0). Only that ever reaches a context — the raw
    issue/PR/ref JSON lives and dies here."""
    cfg = _cfg(a)
    state = afk_decide.cycle_state(json.loads(a.state) if a.state else None,
                                   a.instance, a.worker_command)
    a.instance, a.worker_command = state["instance"], state["worker_command"]
    gathered = _gather(a, cfg) if cfg["fingerprint_gate"] else None
    fp = afk_decide.fingerprint(*gathered[:3]) if gathered else None  # heartbeats: see fingerprint
    woke = afk_decide.cycle_wake(state, fp, cfg)
    if woke["action"] == "skip":
        if not woke.pop("heartbeat"):
            return {**woke, "judgments": []}
        return {**woke, "judgments": [],
                "heartbeat": _beat(_remote(a), cfg, a.instance, _now(a))}
    did, judgments, errors = _tick(a, cfg, _rebuild(a, cfg, gathered))
    return {"action": "tick", "reason": woke["reason"],
            **afk_decide.cycle_ticked(woke["state"], did, cfg, len(judgments), len(errors)),
            "judgments": judgments, **({"errors": errors} if errors else {})}


def _tick(a, cfg, ws):
    """One reconciliation pass over the working set `ws`, in code → (did,
    judgments, errors). In order: `no-pr` for the claims waiting on a worker →
    the landing turn → nudge / fail / park / escalate where the reason is on
    record → release `closed` rows and `stale_closed` phantom locks → reclaim
    `stale` → start workers (continuations first, then the frontier into the
    free slots) → heartbeat → status boards.

    Each transition is the subcommand's own function, so the tick and a human
    typing `afk park` run one code path. One that fails is recorded in `errors`
    and settles nothing: its claim is still held, and the next cycle ticks. A
    failure to START a worker also ends the starting for this tick — it is orca
    or the remote that is unwell, and every further dispatch would take a claim
    it cannot staff. Re-entrant like any tick: killed at any point, the next
    one rebuilds from GitHub."""
    call = {"afk_path": os.path.abspath(__file__), "repo": a.repo, "instance": a.instance,
            "worker_command": a.worker_command, "config": json.dumps(cfg, ensure_ascii=False)}
    did = {k: [] for k in afk_decide.TICK_DID}
    judgments, errors = [], []
    mine = {r["number"]: r for r in ws["mine"]}
    settled, touched = set(), set()      # claims released / claims whose board a transition wrote

    def run(step, fn, number=None, **fields):
        try:
            return fn(argparse.Namespace(**{**vars(a), "number": number, **fields}))
        except (OSError, ValueError, RuntimeError) as e:
            errors.append({"step": step, **({"issue": number} if number else {}),
                           "error": str(e)})
            return None

    # --- observe: the claims waiting on their worker ---
    asked = afk_decide.asks_after(ws["mine"])
    seen = run("no-pr", cmd_no_pr, numbers=asked, worktree=None) if asked else None
    routes = [(w["issue"], *afk_decide.worker_step(call, mine[w["issue"]], w, cfg))
              for w in (seen or {}).get("workers", [])]

    # --- the landing turn: at most one grant a tick ---
    due = afk_decide.turn_due(ws["mine"], ws["merge_order"])
    turn = run("turn", cmd_turn, due, allow_no_checks=False, verified=None) if due else None
    if turn:
        do, asks = afk_decide.turn_step(call, turn, cfg)
        if do == "granted":
            did["granted"].append(due)
            touched.add(due)
        elif do == "judge":
            judgments.append(asks)

    # --- nudge / fail / park / escalate, or the judgment that stands in for one ---
    judgments += [afk_decide.failure_judgment(call, r) for r in ws["mine"]
                  if r["status"] == "failure"]
    for number, do, detail in routes:
        if do == "judge":
            judgments.append(detail)
        elif do == "nudge" and run("nudge", cmd_nudge, number, worktree=None):
            did["nudged"].append(number)
        elif do == "park" and run("park", cmd_park, number):
            did["parked"].append(number)
            settled.add(number)
        elif do in ("fail", "escalate"):
            fn = cmd_fail if do == "fail" else cmd_escalate
            done = run(do, fn, number, reason=detail)
            if done and done["action"] == "escalate":
                did["escalated"].append(number)
                settled.add(number)
            elif done:
                did["retried"].append(number)
                touched.add(number)

    # --- release what outlived its issue ---
    for row in ws["mine"]:
        if row["status"] == "closed" and run("release", cmd_release, row["number"],
                                             expect_sha=None):
            did["cleared"].append(row["number"])
            settled.add(row["number"])
    for row in ws["stale_closed"]:
        if run("release", cmd_release, row["number"], expect_sha=row["sha"]):
            did["cleared"].append(row["number"])

    # --- start workers: continuations of claims already held, then the frontier ---
    starting, taken = True, 0

    def start(number):
        nonlocal starting
        worker = run("dispatch", cmd_dispatch, number, start="auto") if starting else None
        starting = starting and worker is not None
        return worker

    for number, do, _ in routes:
        if do == "dispatch" and (start(number) or {}).get("started"):
            did["dispatched"].append(number)
            touched.add(number)
    for row in ws["stale"]:
        took = run("reclaim", cmd_reclaim, row["number"], expect_sha=row["sha"]) \
            if starting else None
        if took and took["won"]:
            taken += 1
            if (start(row["number"]) or {}).get("started"):
                did["reclaimed"].append(row["number"])
    slots = ws["free_slots"] + len(settled) - taken
    left = len(ws["frontier"]["dispatch"])
    for issue in ws["frontier"]["dispatch"]:
        if slots <= 0 or not starting:
            break
        worker = start(issue["number"])
        if worker:
            left -= 1                    # started, or a peer won it: off the frontier either way
        if worker and worker["started"]:
            did["dispatched"].append(issue["number"])
            taken, slots = taken + 1, slots - 1

    did["in_flight"] = len(mine) - len(settled) + taken
    did["frontier_remaining"] = left

    # --- the lease, and what a human reads on each issue ---
    if did["in_flight"]:
        run("heartbeat", cmd_heartbeat)
    if cfg["progress_comment"]:
        for row in ws["mine"]:
            if row["board_phase"] and row["number"] not in settled | touched:
                run("status", cmd_status, row["number"], phase=row["board_phase"],
                    pr=row["pr"], attempt=row["attempt"])
    return did, judgments, errors


# --------------------------------------------------------------------------- #
# observation: rebuild / no-pr / recovery                                      #
# --------------------------------------------------------------------------- #

def _rebuild(a, cfg, gathered=None):
    """The working set (`afk_decide.assemble_working_set`), from `gathered` — a
    `_gather` the caller already made — or a fresh one. The per-issue blocked_by
    read is paid only by issues that pass every cheaper eligibility check, the
    per-issue state read only by a claim whose issue is missing from the open
    list, and the landing-turn read only by a claim of mine that has a PR."""
    issues, prs, claims, heartbeats = gathered or _gather(a, cfg)
    blocked = {}
    for n in afk_decide.frontier_candidates(issues, prs, claims,
                                            cfg["ready_label"], cfg["epic_labels"]):
        v = _gh(["api", f"repos/{a.repo}/issues/{n}",
                 "--jq", ".issue_dependencies_summary.blocked_by"]).stdout.strip()
        blocked[n] = 0 if v in ("", "null") else int(v)
    listed = {i["number"] for i in issues}
    closed = [c["number"] for c in claims
              if c["number"] not in listed and _issue_state(a.repo, c["number"]) == "closed"]
    return afk_decide.assemble_working_set(
        issues, prs, claims, heartbeats, blocked, a.instance, _now(a), cfg, closed=closed,
        turns=_held_turns(a.repo, prs, claims, a.instance))


def cmd_rebuild(a):
    """One read-only call → the tick's whole working set (ADR-0008) — what a tick
    acts on, and what `--plan` prints instead. Strictly observation: nothing here
    writes a ref, a comment, or a PR."""
    return _rebuild(a, _cfg(a))


def _issue_worktree(repo, number):
    """This machine's orca worktree for an issue → (path, branch), each None when
    there is none. Soft, like the read behind it: no orca means no worktree. A
    path orca remembers but the disk no longer has is returned as-is — a caller
    that will read the worktree asks `_live_worktree` instead."""
    hit = afk_decide.find_orca_worktree(_orca_worktree_rows(), number, repo)
    return hit["path"], hit["branch"]


def _live_worktree(repo, number):
    """The path of an issue's worktree that is really on this machine's disk, None
    when orca knows none or the directory is gone — what every caller that reads
    or works IN the worktree wants. (`_issue_worktree` is for the ones that must
    also see a path orca still remembers: removing it, reporting it.)"""
    path, _ = _issue_worktree(repo, number)
    return path if path and os.path.isdir(path) else None


def cmd_no_pr(a):
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
    Returns {"workers": [`afk_decide.classify_no_pr`'s outcome plus those signals
    and the `worker_state` it was read from, one per --issue, in order]}."""
    cfg = _cfg(a)
    if a.worktree is not None:
        if len(a.numbers) != 1:
            raise ValueError("--worktree names one worker's worktree: give it with one --issue")
        if not os.path.isdir(a.worktree):
            raise ValueError(f"worktree not found: {a.worktree} (omit --worktree to let orca find it)")
    # HARD reads: an orca that cannot be asked is never "the worker is gone" —
    # read that way it would start a second worker beside a live one
    rows = _orca(["worktree", "list"]).get("worktrees") or []
    states = _worker_states()
    now, grace = _now(a), cfg["worker_idle_grace_seconds"]
    workers = []
    for number in a.numbers:
        path = a.worktree
        if path is None:
            found = afk_decide.find_orca_worktree(rows, number, a.repo)["path"]
            path = found if found and os.path.isdir(found) else None
        reading = _worker_reading(states, path, now, grace)
        workers.append({"issue": number,
                        **_worker_outcome(a, cfg, number, path, reading, now, grace),
                        "worker_state": reading["state"]})
    return {"workers": workers}


# Far above any one machine's worktree count: a page that stops short is an error.
_PS_LIMIT = 10000


def _worker_states():
    """Every worktree's row of `orca worktree ps`, by path — what each worker's
    runtime reported (ADR-0021). HARD: an orca that cannot be asked is never "no
    worker there"."""
    ps = _orca(["worktree", "ps", "--limit", str(_PS_LIMIT)])
    if ps.get("truncated"):
        raise RuntimeError(f"orca worktree ps truncated at {_PS_LIMIT} rows")
    return {r.get("path"): r for r in ps.get("worktrees") or []}


def _worker_reading(states, path, now, grace):
    """`afk_decide.read_worker_state` for the worker in the worktree at `path`
    (None: this machine has no worktree, so no worker), from `_worker_states`."""
    row = states.get(path) if path else None
    reading = afk_decide.read_worker_state(row, now, grace)
    if reading["terminal"] != "none" and reading["state"] is None:
        # a runtime that reports nothing: ask orca whether its terminal is idle
        reading = afk_decide.read_worker_state(row, now, grace, _tui_idle(path))
    return reading


def _worker_outcome(a, cfg, number, path, reading, now, grace):
    """`classify_no_pr` for one worker, gathering only what its reading leaves
    open: busy or gone is settled by the reading alone, so it costs no git and
    no GitHub."""
    nudged_at = (_nudge(path) or {}).get("at")
    settled = afk_decide.classify_no_pr({}, reading["terminal"], reading["terminal_idle_seconds"],
                                        None, {}, now, grace, nudged_at=nudged_at)
    if settled["outcome"] in ("coding", "dead"):
        return {**settled, "worktree": path, "progress": {}, "worker_verdict": None,
                "blockers": [], "nudged_at": nudged_at, "turn_at": None}
    progress = _worktree_progress(path, _remote(a), cfg["base_branch"]) if path else {}
    declared = afk_decide.latest_verdict(_issue_comments(a.repo, number))
    prs = _open_prs(a.repo)
    blockers = _blocker_standings(a, cfg, number, declared["blocked_by"], prs)
    pr = afk_decide.closing_pr(prs, number)
    turn = (_turn(a.repo, pr) if pr else None) or {}
    return {**afk_decide.classify_no_pr(progress, reading["terminal"],
                                        reading["terminal_idle_seconds"], declared,
                                        {b["number"]: b["standing"] for b in blockers},
                                        now, grace,
                                        nudged_at=nudged_at, can_nudge=path is not None,
                                        turn_at=turn.get("at"),
                                        turn_stopped=turn.get("stopped")),
            "worktree": path, "progress": progress, "worker_verdict": declared,
            "blockers": blockers, "nudged_at": nudged_at, "turn_at": turn.get("at")}


# More issues than any real dependency chain holds: a walk that gets here is an
# error, never "no cycle found".
_DEPENDENCY_WALK_LIMIT = 200


def _blocker_standings(a, cfg, number, named, prs):
    """Where each issue a `blocked` verdict names stands
    (`afk_decide.blocker_standings`), gathering what that takes: the blocker
    itself, the claim refs, the open PRs, and — from every blocker still open —
    the chain of open blockers behind it, for the cycle check. `afk no-pr` and
    `afk park` both read through here, so the transition parks exactly what the
    observation said was parkable."""
    if not named:
        return []
    blockers = {n: _blocker(a.repo, n) for n in named}
    claims, _ = _scan(_remote(a), cfg["claim_namespace"])
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
        edges[n] = [e["number"] for e in _blocked_by(a.repo, n) if e["state"] == "open"]
        todo.extend(edges[n])
    return afk_decide.blocker_standings(
        number, named, cfg, blockers=blockers, claimed={c["number"] for c in claims},
        open_pr={n for n in named if afk_decide.closing_pr(prs, n)}, edges=edges)


def cmd_nudge(a):
    """Tell a worker that stopped without an outcome to carry on (`afk no-pr` →
    `idle_stalled` / `nudge`) — the step before failure handling, and one that
    spends no attempt and discards nothing (ADR-0018). Reads the tail of its
    screen (where it stopped), types one line at it, and records the nudge in the
    worktree, so the next silence is a failure and not a second nudge.

      {"issue", "action": "nudged", "terminal", "terminal_tail": [...]}"""
    cfg, rem = _cfg(a), _remote(a)
    _require_mine(rem, cfg, a.number, a.instance)
    path = a.worktree or _live_worktree(a.repo, a.number)
    if not path or not os.path.isdir(path):      # the isdir is for a --worktree given by hand
        raise RuntimeError(f"issue #{a.number} has no worktree on this machine — there is no "
                           f"worker here to nudge")
    if _nudge(path) is not None:
        raise RuntimeError(f"the worker on issue #{a.number} was already nudged — a second "
                           f"silence is a failure (`afk fail`), not another nudge")
    handle = _live_terminal(path)
    if handle is None:
        raise RuntimeError(f"no live terminal in {path} — the worker is dead, not stalled "
                           f"(`afk dispatch` continues it)")
    tail = _terminal_tail(handle)
    brief = _worker_file(path, _WORKER_BRIEF)
    sent = _orca(["terminal", "send", "--terminal", handle, "--enter", "--text",
                  afk_decide.nudge_text(brief if os.path.exists(brief) else None)]).get("send") or {}
    if not sent.get("accepted"):
        raise RuntimeError(f"terminal {handle} did not accept the nudge")
    with open(_worker_file(path, _NUDGE_MARK), "w") as f:
        json.dump({"at": _now(a), "tail": tail}, f)
    return {"issue": a.number, "action": "nudged", "terminal": handle, "terminal_tail": tail}


def _stalled_reason(repo, number, reason):
    """`reason`, plus where the worker stopped when this failure follows a nudge
    it never answered: its screen as it is now, else as it was when nudged. Soft —
    a failure is never blocked on reading a terminal."""
    path = _live_worktree(repo, number)
    nudge = _nudge(path)
    if nudge is None:
        return reason
    try:
        handle = _live_terminal(path)
        tail = _terminal_tail(handle) if handle else None
    except RuntimeError:
        tail = None
    return afk_decide.stall_reason(reason, tail or nudge.get("tail"))


def _recovery(cfg, rem, repo, number, path=None, branch=None, no_worktree=False):
    """What survived a dead worker, and the tier it selects (ADR-0011).

    Two signals, both mechanics: (1) is a worktree for this issue still on THIS
    machine — asked of `orca worktree list` (soft: no orca → "no worktree", never
    an abort), overridable with `path` / `no_worktree`; (2) is the issue's branch
    ahead of base on the remote — the branch is recognised from `branch_pattern`
    (the claim ref records the issue, not the branch) and the compare is plain
    git. `afk_decide.select_recovery` then picks the tier.

    Both signals are always gathered, even when the worktree already settles the
    tier: a *pristine* worktree over a branch that carries pushed commits still has
    something to continue, and the honest prompt depends on knowing that."""
    base = cfg["base_branch"]

    # --- tier-1 signal: a worktree for this issue, still on this machine ---
    if path is None and not no_worktree:
        path, found_branch = _issue_worktree(repo, number)
        branch = branch or found_branch
    present = bool(path and os.path.isdir(path))
    worktree = {"present": present, "path": path,
                **(_worktree_progress(path, rem, base) if present else {})}

    # --- tier-2 signal: the branch the dead worker pushed ---
    candidates = [] if branch else afk_decide.branch_candidates(
        _remote_heads(rem), cfg["branch_pattern"], number)
    ahead = {b: _branch_ahead(rem, b, base, number)
             for b in ([branch] if branch else candidates)}
    branch = branch or afk_decide.furthest_ahead(ahead)
    branch_sig = {"name": branch, "commits_ahead": ahead.get(branch), "candidates": candidates}

    return {"issue": number, "base": base, "worktree": worktree, "branch": branch_sig,
            **afk_decide.select_recovery(worktree, branch_sig)}


def cmd_recovery(a):
    """Per DEAD claim: does recoverable progress exist, and where? → the tiered
    continuation verdict (ADR-0011), read-only. `afk dispatch` makes this same read
    and acts on it; call this one first only to inspect what it would continue
    from — the tick's "is this state sane to build on" judgment."""
    return _recovery(_cfg(a), _remote(a), a.repo, a.number,
                     path=a.worktree, branch=a.branch, no_worktree=a.no_worktree)


# --------------------------------------------------------------------------- #
# act: starting a worker — dispatch                                            #
# --------------------------------------------------------------------------- #

def _create_worktree(a, cfg, rem, issue, at_branch):
    """Have orca create the worktree + branch for an issue at the REMOTE's current
    tip of `at_branch` (ADR-0005: orca owns both, and names the branch) → (path,
    branch, sha). The tip is fetched into the checkout orca cuts worktrees from and
    handed over as a sha rather than a branch name — and then ASSERTED: the
    worktree must contain it, however orca resolved the ref. A worker started on a
    base that is commits behind builds on files that have already moved."""
    orca_repo = afk_decide.find_orca_repo(_orca(["repo", "list"]).get("repos"), a.repo)
    if not orca_repo:
        raise RuntimeError(f"orca knows no repo for {a.repo} — add this checkout once with "
                           f"`orca repo add --path <path>`")
    sha = _fetch_tip(rem, at_branch, cwd=orca_repo["path"])
    name = afk_decide.worktree_name(cfg["branch_pattern"], issue["number"], issue["title"])
    wt = _orca(["worktree", "create", "--repo", f"id:{orca_repo['id']}", "--name", name,
                "--no-parent", "--base-branch", sha,
                "--issue", str(issue["number"])]).get("worktree") or {}
    path, branch = wt.get("path"), wt.get("branch")
    if not path or not os.path.isdir(path):
        raise RuntimeError(f"orca worktree create returned no usable path: {path!r}")
    if _git(["-C", path, "merge-base", "--is-ancestor", sha, "HEAD"], check=False).returncode != 0:
        _git(["-C", path, "merge", "--ff-only", sha])
    return path, afk_decide.short_branch(branch), sha


def _remove_worktree(path):
    """Have orca remove a worktree (and its terminals). Soft: by the time this
    runs the transition that mattered — a merge, a close — is already durable, so a
    failed cleanup is reported, never raised."""
    try:
        _orca(["worktree", "rm", "--worktree", f"path:{path}", "--force"])
        return {"removed": True, "path": path}
    except RuntimeError as e:
        return {"removed": False, "path": path, "detail": str(e)}


def _discard_attempt(a, cfg, rem, number):
    """Throw the previous attempt away, for a FRESH start: close the PRs the fleet
    opened for the issue, delete its work branches on the remote, remove its
    worktree. What a retry means (the previous attempt is the thing that failed),
    and why it is never done to a claim that merely lost its worker (ADR-0011).

    Closing the PR is what keeps the claim from re-entering the retry ladder on
    the same red PR next tick; deleting the branches is what keeps a later
    continuation from resuming the attempt that was discarded."""
    closed = []
    for pr in afk_decide.superseded_prs(_open_prs(a.repo), number, cfg["branch_pattern"]):
        _gh(["pr", "close", str(pr["number"]), "--repo", a.repo, "--delete-branch", "--comment",
             "afk-fleet: superseded — this attempt failed and the issue is being retried "
             "from a clean base."])
        closed.append(pr["number"])
    deleted = []
    for branch in afk_decide.branch_candidates(_remote_heads(rem), cfg["branch_pattern"], number):
        _git(["push", rem, "--delete", f"refs/heads/{branch}"])
        deleted.append(branch)
    path, _ = _issue_worktree(a.repo, number)
    removed = _remove_worktree(path) if path else None
    if removed and not removed["removed"]:
        raise RuntimeError(f"could not remove the previous attempt's worktree {path}: "
                           f"{removed['detail']}")
    return {"closed_prs": closed, "deleted_branches": deleted, "removed_worktree": path}


_WORKER_BRIEF = "afk-worker-prompt.md"
_BRIEF_POINTER = ("Your task brief is the file {brief} — read it now and carry it out end to end. "
                  "It is my instruction to you; do not ask me to confirm.")


_NUDGE_MARK = "afk-nudge.json"
_GATE_RECORD = "afk-gate.json"


def _worker_file(path, name):
    """A fleet-private file about the worker in a worktree, kept in the worktree's
    own git dir: never staged by the worker's `git add -A`, gone when the worktree
    is."""
    git_dir = _git(["-C", path, "rev-parse", "--absolute-git-dir"]).stdout.strip()
    return os.path.join(git_dir, name)


def _write_brief(path, prompt):
    """Write a worker's instructions to the worktree's brief file → its path. A
    worker under a new brief — a new worker, or one just given its landing turn —
    has not been nudged, whatever came before."""
    brief = _worker_file(path, _WORKER_BRIEF)
    with open(brief, "w") as f:
        f.write(prompt)
    mark = _worker_file(path, _NUDGE_MARK)
    if os.path.exists(mark):
        os.remove(mark)
    return brief


def _nudge(path):
    """The nudge recorded for the worker in a worktree → {"at", "tail"}, or None
    when it was never nudged (or there is no worktree to have recorded one in)."""
    if not path:
        return None
    try:
        with open(_worker_file(path, _NUDGE_MARK)) as f:
            return json.load(f)
    except (OSError, ValueError, RuntimeError):
        return None


def _live_terminal(path):
    """The handle of the live worker terminal in a worktree — the one that spoke
    last, when a worktree somehow has several — or None."""
    rows = _orca(["terminal", "list", "--worktree", f"path:{path}"]).get("terminals") or []
    live = [t for t in rows if t.get("connected", True) and t.get("writable", True)]
    return max(live, key=lambda t: t.get("lastOutputAt") or 0)["handle"] if live else None


# How long orca is given to say a terminal is idle; it answers at once when it is.
_TUI_IDLE_PROBE_MS = 2000


def _tui_idle(path):
    """Does orca see the worker terminal in a worktree idle? Its own reading of
    the terminal (title, prompt), for a runtime that reports no state. True when
    it answers within the probe, False when the probe times out — the worker is
    at it. No live terminal reads as idle: there is nothing busy to wait for."""
    handle = _live_terminal(path)
    if handle is None:
        return True
    try:
        wait = _orca(["terminal", "wait", "--terminal", handle, "--for", "tui-idle",
                      "--timeout-ms", str(_TUI_IDLE_PROBE_MS)]).get("wait") or {}
    except OrcaError as e:
        if e.code == "timeout":
            return False
        raise
    return bool(wait.get("satisfied"))


def _terminal_tail(handle):
    """The last lines of a worker's rendered screen, bounded. Read for ONE purpose:
    saying where a silent worker stopped (ADR-0018) — never for its result, which
    is only ever a PR or a verdict marker."""
    shown = _orca(["terminal", "read", "--terminal", handle, "--screen",
                   "--limit", str(afk_decide.STALL_TAIL_LINES * 2)]).get("terminal") or {}
    return afk_decide.stall_tail(shown.get("tail"))


def _start_terminal(path, worker_command, prompt, ready_timeout):
    """Start the worker in a worktree and submit its prompt → the terminal handle.
    Four steps, each of which has failed silently when a tick typed it: the agent
    is started with the run's OPAQUE worker launch command (never `--agent`,
    ADR-0010), it is waited for until its TUI is idle, the prompt goes to a brief
    FILE with only a one-line pointer typed at the agent — a whole prompt sent as
    text arrives as one paste, which the agent reads as quoted material and asks
    to have confirmed instead of starting — and that pointer is sent WITH
    `--enter` — typed but unsubmitted, a worker sits idle forever, indistinguishable
    from one that finished."""
    brief = _write_brief(path, prompt)
    prompt = _BRIEF_POINTER.format(brief=brief)
    term = _orca(["terminal", "create", "--worktree", f"path:{path}",
                  "--command", worker_command]).get("terminal") or {}
    handle = term.get("handle")
    if not handle:
        raise RuntimeError("orca terminal create returned no terminal handle")
    wait = _orca(["terminal", "wait", "--terminal", handle, "--for", "tui-idle",
                  "--timeout-ms", str(ready_timeout * 1000)],
                 timeout=ready_timeout + 30).get("wait") or {}
    if not wait.get("satisfied"):
        raise RuntimeError(f"the worker in {path} was not ready for a prompt within "
                           f"{ready_timeout}s (terminal {handle})")
    sent = _orca(["terminal", "send", "--terminal", handle, "--text", prompt,
                  "--enter"]).get("send") or {}
    if not sent.get("accepted"):
        raise RuntimeError(f"terminal {handle} did not accept the worker prompt")
    return handle


def _landing_fields(cfg, pr):
    """The LANDING_FIELDS of a landing brief, from the config and the PR."""
    return {"pr": pr["number"], "pr_branch": pr["headRefName"], "target": cfg["merge"]["target"]}


def _prompt_fields(a, cfg, issue, path, branch):
    """The PROMPT_FIELDS of a worker prompt for one issue in one worktree. The
    launcher's terminal is read off the environment, never passed in: a tick is a
    subagent of the launcher, so the handle orca gave that terminal is the one
    this process inherited (ADR-0020). The config travels whole, as the JSON
    `afk land` is run with: the worker lands on the settings the tick ran on."""
    return {"n": issue["number"], "title": issue["title"], "repo": a.repo,
            "base_branch": cfg["base_branch"], "local_command": cfg["gate"]["local_command"],
            "afk_path": os.path.abspath(__file__),
            "config": json.dumps(cfg, ensure_ascii=False), "branch": branch,
            "worktree_path": path,
            "launcher_terminal": os.environ.get("ORCA_TERMINAL_HANDLE", "")}


def _start_worker(a, cfg, rem, issue, start, reason=None):
    """Put a worker on an issue this fleet already holds the claim for.

    `start` is "auto" — continue from whatever progress survives (the worktree
    still here, else the pushed branch, else a fresh start from base: the
    continuation tiers of ADR-0011) — or "fresh": discard the previous attempt and
    start from base, which is what a retry is. A continued worker whose PR holds
    this fleet's landing turn is started ON it, briefed only to land the PR — in
    the worktree still here, else one recreated at the PR's head, never from base
    (ADR-0027). Returns the tier taken plus where the worker now is: {tier,
    action, prompt, reason, worktree, branch, terminal}."""
    number = issue["number"]
    if issue["state"] != "open":
        raise RuntimeError(f"issue #{number} is {issue['state']}, not open — there is nothing "
                           f"to retry (release the claim instead)")
    discarded, pr, turn = None, None, None
    if start == "fresh":
        discarded = _discard_attempt(a, cfg, rem, number)
        plan = {"tier": 3, "action": "dispatch_fresh", "prompt": "fresh",
                "reason": "fresh start: the previous attempt was discarded"}
    else:
        rec = _recovery(cfg, rem, a.repo, number)
        plan = {k: rec[k] for k in ("tier", "action", "prompt", "reason")}
        pr = afk_decide.closing_pr(_open_prs(a.repo), number)
        turn = afk_decide.held_turn(_turn(a.repo, pr), a.instance) if pr else None

    if plan["action"] == "reuse_worktree":
        path = rec["worktree"]["path"]
        branch = _git(["-C", path, "rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()
        # whatever agent was here is dead or idle: two in one worktree would fight
        try:
            _orca(["terminal", "close", "--worktree", f"path:{path}", "--all"])
        except RuntimeError:
            pass
    elif turn:
        plan = {"tier": 2, "action": "recreate_at_tip", "prompt": "continue",
                "reason": f"no local worktree; PR #{pr['number']} holds the landing turn — "
                          f"recreate at its head"}
        path, branch, _ = _create_worktree(a, cfg, rem, issue, pr["headRefName"])
    else:
        tip = rec["branch"]["name"] if plan["action"] == "recreate_at_tip" else cfg["base_branch"]
        path, branch, _ = _create_worktree(a, cfg, rem, issue, tip)

    fields = _prompt_fields(a, cfg, issue, path, branch)
    with open(_WORKER_PROMPT) as f:
        if turn:
            plan["prompt"] = "landing"
            prompt = afk_decide.render_landing(f.read(), fields, _landing_fields(cfg, pr))
        else:
            prompt = afk_decide.render_worker_prompt(f.read(), plan["prompt"], fields,
                                                     reason=reason)
    handle = _start_terminal(path, a.worker_command, prompt, a.ready_timeout)
    if cfg["progress_comment"]:
        if turn:
            _upsert_board(a.repo, number, cfg, "landing", instance=a.instance, pr=pr["number"])
        else:
            _upsert_board(a.repo, number, cfg, "claimed", instance=a.instance)
    return {**plan, "worktree": path, "branch": branch, "terminal": handle,
            **({"landing": pr["number"]} if turn else {}),
            **({"discarded": discarded} if discarded else {})}


def cmd_dispatch(a):
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
    cfg, rem = _cfg(a), _remote(a)
    issue = _issue(a.repo, a.number)
    if issue["state"] != "open":           # before the claim: never lock a closed issue
        raise RuntimeError(f"issue #{a.number} is {issue['state']}, not open — nothing to dispatch")
    claim = _claim(rem, cfg, a.number, a.instance, _now(a), a.host)
    if not claim["won"] and claim["owner"].get("instance") != a.instance:
        return {"issue": a.number, "started": False, "claim": "lost", "owner": claim["owner"]}
    started = _start_worker(a, cfg, rem, issue, a.start)
    return {"issue": a.number, "started": True,
            "claim": "won" if claim["won"] else "held", **started}


# --------------------------------------------------------------------------- #
# act: the landing turn, and settling a claim — fail / escalate / park / close #
# --------------------------------------------------------------------------- #

def _upsert_board(repo, number, cfg, phase, instance=None, pr=None, attempt=0, blocked_by=()):
    """Upsert the human-facing progress status board comment (idempotent, ADR-0006).
    Renders the body from the given phase (pure), then find-or-create by marker
    and write ONLY when the body changed — so re-entrant/disposable ticks and
    retry re-dispatches never spam the issue."""
    body = afk_decide.render_status_board(phase, cfg["gate"]["ci"], cfg["retry"],
                                          instance=instance, pr=pr, attempt=attempt,
                                          blocked_by=blocked_by)
    comments = f"repos/{repo}/issues/{number}/comments"
    board = next((c for c in _issue_comments(repo, number)
                  if afk_decide.STATUS_MARKER in (c["body"] or "")), None)
    if board is None:
        p = _gh(["api", "--method", "POST", comments, "-f", f"body={body}"])
        return {"action": "created", "issue": number, "comment_id": json.loads(p.stdout).get("id")}
    if board["body"].strip() == body.strip():
        return {"action": "unchanged", "issue": number, "comment_id": board["id"]}
    _gh(["api", "--method", "PATCH", f"repos/{repo}/issues/comments/{board['id']}",
         "-f", f"body={body}"])
    return {"action": "updated", "issue": number, "comment_id": board["id"]}


def cmd_status(a):
    """Upsert one claim's status board at a NON-terminal phase — a `mine` row's
    `board_phase`. The terminal phases are written by the transition that reaches
    them (`afk land`, `afk escalate`, `afk park`, `afk close`)."""
    return _upsert_board(a.repo, a.number, _cfg(a), a.phase,
                         instance=a.instance, pr=a.pr, attempt=a.attempt)


def _run_gate(cfg, worktree, timeout, excerpt_lines, live=False):
    """Run the configured local gate in a worktree → `afk_decide.gate_verdict` —
    the completion gate itself in `gate.ci: local` mode, run by `afk land` against
    the exact tree that lands (ADR-0012). *What* to run is config, *where* is the
    branch's worktree, and *whether it passed* is an exit code — no judgment. A run
    that times out is red, never green-by-default; the caller gets a verdict and a
    bounded excerpt, never a raw log. `live` is `afk gate`'s run: the log goes
    straight to the worker's terminal — on stderr, the JSON stays alone on stdout —
    and the excerpt is empty, since the worker has the whole of it."""
    cmd = cfg["gate"]["local_command"]

    def _text(s):
        return s.decode("utf-8", "replace") if isinstance(s, bytes) else (s or "")

    pipes = ({"stdout": sys.stderr, "stderr": sys.stderr} if live
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
    return {**afk_decide.gate_verdict(rc, out, excerpt_lines, timed_out), "command": cmd}


def _gate_record(path):
    """The `afk_decide.gate_record` in a worktree, None when it has none."""
    try:
        with open(_worker_file(path, _GATE_RECORD)) as f:
            return json.load(f)
    except (OSError, ValueError, RuntimeError):
        return None


def _gate_run(cfg, path, timeout, excerpt_lines, now, live=False):
    """One run of the local gate in a worktree, put on record when green →
    (`_run_gate`'s verdict, the head it ran on, the uncommitted paths, clean).
    The previous record is dropped before the run starts, so a red or timed-out
    run leaves nothing that could be read as green; a green run over uncommitted
    or untracked files is recorded as such and never trusted — it tested a tree
    no commit holds. The record is `afk`'s, made from an exit code it saw."""
    mark = _worker_file(path, _GATE_RECORD)
    if os.path.exists(mark):
        os.remove(mark)
    head = _git(["-C", path, "rev-parse", "HEAD"]).stdout.strip()
    dirty = _git(["-C", path, "status", "--porcelain"]).stdout.splitlines()
    gate = _run_gate(cfg, path, timeout, excerpt_lines, live=live)
    clean = False
    if gate["status"] == "green":
        clean = not dirty and _git(["-C", path, "rev-parse", "HEAD"]).stdout.strip() == head
        with open(mark, "w") as f:
            json.dump(afk_decide.gate_record(head, gate["command"], clean, now), f)
    return gate, head, dirty, clean


def cmd_gate(a):
    """A WORKER's run of the local gate, in the worktree it is called from — what
    a worker runs before it opens its PR, in place of typing `gate.local_command`
    itself. The log streams to the worker's terminal; a GREEN run is put on record
    in the worktree's git dir with the head it ran on, which is what lets
    `afk land` skip running the same command on the same commit again
    (`gate.trust_recorded_run`, ADR-0026). A worker saying "the gate is green"
    leaves no record.

      {"status": "green"|"red", "exit_code", "timed_out", "command", "head",
       "recorded": <this run is on record as a pass of `head`>, "detail"}"""
    cfg = _cfg(a)
    if not cfg["gate"]["local_command"].strip():
        raise ValueError("gate.local_command is empty — there is no local gate to run")
    path = _git(["rev-parse", "--show-toplevel"]).stdout.strip()
    gate, head, dirty, clean = _gate_run(cfg, path, a.gate_timeout, 0, _now(a), live=True)
    out = {k: gate[k] for k in ("status", "exit_code", "timed_out", "command")}
    if gate["status"] != "green":
        return {**out, "head": head, "recorded": False,
                "detail": "the gate is red — nothing is on record; fix it and run this again"}
    if not clean:
        return {**out, "head": head, "recorded": False, "uncommitted": dirty[:20],
                "detail": "green, but not on a committed tree — the worktree had uncommitted or "
                          "untracked files, so this run proves nothing about a commit. Commit "
                          "them (or ignore the gate's own artifacts) and run this again"}
    return {**out, "head": head, "recorded": True,
            "detail": f"green on {head} and on record — push it; any further commit needs "
                      f"another run"}


def _unmerged(path):
    """The files a merge in progress in a worktree has left conflicted."""
    out = _git(["-C", path, "diff", "--name-only", "--diff-filter=U"]).stdout
    return [ln for ln in out.splitlines() if ln]


def _sync(rem, path, target):
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


def cmd_turn(a):
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

      granted       the worker was told (`delivery`: "terminal" | "continuation").
                    `again` is true when the PR already held the turn and its
                    `afk land` had stopped for the tick: it was told to land again.
      waiting       another claim of mine (`holder`) holds the turn. Nothing was
                    touched; this PR's turn comes when that one has landed or failed.
      landing       this PR already holds the turn and its worker has not stopped
                    for the tick. Nothing was touched; `afk no-pr` watches it.
      awaiting_ci   required mode: the PR's checks are still running. Leave it.
      gate_red      required mode: the PR's checks are red → `afk fail`.
      no_checks     required mode, and the PR has no checks at all — the
                    progressive gate. Re-run with `--allow-no-checks` if the tick
                    judges the acceptance criteria met.
      needs_verify  `gate.adversarial_verify` is on and `--verified` does not name
                    the PR's `head`. Run the verifier on `head`, then re-run with
                    `--verified <head>`."""
    cfg, rem = _cfg(a), _remote(a)
    _require_mine(rem, cfg, a.number, a.instance)
    issue = _issue(a.repo, a.number)
    prs = _open_prs(a.repo)
    pr = afk_decide.closing_pr(prs, a.number)
    if pr is None:
        raise RuntimeError(f"no open PR closes issue #{a.number} — there is nothing to land")
    head = pr["headRefOid"]
    out = {"issue": a.number, "pr": pr["number"], "head": head}

    def stop(outcome, **more):
        return {**out, "outcome": afk_decide.turn_outcome(outcome), **more}

    held = _held_turns(a.repo, prs, _scan(rem, cfg["claim_namespace"])[0], a.instance)
    others = sorted(n for n in held if n != a.number)
    if others:
        return stop("waiting", holder=others[0],
                    detail=f"issue #{others[0]}'s PR holds this fleet's landing turn; nothing was "
                           f"touched — this PR's turn comes when that one has landed or failed")
    prev = _turn(a.repo, pr)
    mine = held.get(a.number)
    if mine and mine["stopped"] not in afk_decide.LAND_WAITS:
        return stop("landing",
                    detail="this PR already holds the landing turn and its worker has not "
                           "stopped for you; nothing was touched — `afk no-pr` watches it")
    # a judgment made about this PR stands: a re-delivery need not repeat it
    allow = a.allow_no_checks or bool(mine and mine["allow_no_checks"])
    verified = a.verified or (mine or {}).get("verified")
    checks = afk_decide.pr_checks_state(pr.get("statusCheckRollup"))
    ready = afk_decide.turn_gate(cfg["gate"]["ci"], checks, allow,
                                 cfg["gate"]["adversarial_verify"], verified, head)
    if ready != "ready":
        return stop(ready, checks=checks)

    path = _live_worktree(a.repo, a.number)
    handle = _live_terminal(path) if path else None
    if handle:
        branch = _git(["-C", path, "rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()
        with open(_WORKER_PROMPT) as f:
            brief = _write_brief(path, afk_decide.render_landing(
                f.read(), _prompt_fields(a, cfg, issue, path, branch), _landing_fields(cfg, pr)))
    out["comment_id"] = _record_turn(
        a.repo, pr, afk_decide.turn_comment(a.instance, _now(a), verified=verified,
                                            allow_no_checks=allow), prev)
    out["again"] = bool(mine)
    if not handle:          # the worker is gone: its continuation is started on the turn
        worker = _start_worker(a, cfg, rem, issue, "auto")
        return stop("granted", delivery="continuation", terminal=worker["terminal"],
                    worktree=worker["worktree"])
    sent = _orca(["terminal", "send", "--terminal", handle, "--enter", "--text",
                  _TURN_POINTER.format(brief=brief)]).get("send") or {}
    if not sent.get("accepted"):
        raise RuntimeError(f"terminal {handle} did not accept the landing turn")
    if cfg["progress_comment"]:
        _upsert_board(a.repo, a.number, cfg, "landing", instance=a.instance, pr=pr["number"])
    return stop("granted", delivery="terminal", terminal=handle, worktree=path)


def cmd_land(a):
    """A WORKER lands its own PR, in the worktree it is called from — the only way
    a PR lands (ADR-0027). Refused (exit 3, nothing changed) unless the PR holds
    the landing turn of the fleet instance that holds the issue's claim: the check
    guards against a worker that strays, not a malicious one — worker and launcher
    share one `gh` credential. Then, in order: sync by merging the target in
    (never a rebase) → push → the machine gate on that exact head → the verify
    check → `gh pr merge` pinned to the gated head → status board. It stops with
    an `outcome`:

      merged        landed. The worker sends its wake and stops.
      conflict      the sync conflicted; the merge is left in progress with
                    `files` unmerged. The worker resolves them, COMMITS, and
                    runs this again.
      gate_red      local mode: the gate was red on the synced head (`gate.excerpt`,
                    also a PR comment). required mode: the PR's checks are red.
                    The worker fixes the code, commits, and runs this again.
      awaiting_ci   required mode: checks pending, or the sync just pushed and CI
                    must run on the new head.
      needs_verify  `gate.adversarial_verify` is on and the head that would land
                    is not the one the tick verified — the sync moved it.
      no_checks     required mode, the PR has no checks at all, and the tick has
                    not said it may land so.

    On the last three the next move is the tick's: the worker sends its wake and
    stops, the turn stays its own, and it is told to run this again (`afk turn`).
    No outcome spends an attempt, closes the PR or gives the turn up; every one
    but `merged` is written onto the PR's turn comment (`stopped`), which is what
    `afk rebuild` reports. The claim is NOT released and the worktree is not
    removed here — this runs inside that worktree, and a worker holds no instance
    id: the next cycle sees a claim whose issue is closed and settles both.

    The invariant every path keeps: what lands on the target was gated in the form
    it lands (ADR-0012). In local mode `gate.source` says where that proof came
    from: "run" — the gate ran here — or, with `gate.trust_recorded_run`,
    "recorded" — a green run is on record for exactly `gate.head`, so it was not
    run again (ADR-0026); `gate.not_trusted` says why a record was not enough."""
    cfg, rem = _cfg(a), _remote(a)
    path = _git(["rev-parse", "--show-toplevel"]).stdout.strip()
    pr = afk_decide.closing_pr(_open_prs(a.repo), a.number)
    if pr is None:
        raise RuntimeError(f"no open PR closes issue #{a.number} — there is nothing to land (if "
                           f"it has already merged, send your wake and stop)")
    owner = _claim_owner(rem, _claim_ref(cfg, a.number))
    turn = afk_decide.held_turn(_turn(a.repo, pr), owner)
    if turn is None:
        raise RuntimeError(f"PR #{pr['number']} does not hold the landing turn of the fleet "
                           f"instance that holds issue #{a.number}'s claim; nothing was changed. "
                           f"Do not land it any other way — you are told when its turn comes")
    branch, target = pr["headRefName"], cfg["merge"]["target"]
    out = {"issue": a.number, "pr": pr["number"]}

    def stop(outcome, **more):
        """Stop short of merging, and say so on the PR's turn comment."""
        at = _git(["-C", path, "rev-parse", "HEAD"]).stdout.strip()
        _record_turn(a.repo, pr, afk_decide.turn_comment(
            owner, _now(a), verified=turn["verified"], allow_no_checks=turn["allow_no_checks"],
            stopped=afk_decide.land_outcome(outcome), head=at), turn)
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
    if cfg["merge"]["sync_before_merge"]:
        files = _sync(rem, path, target)
        if files:
            return stop("conflict", files=files, worktree=path,
                        detail=f"merging {target} into {branch} conflicted; the merge is in "
                               f"progress here — resolve every file, `git add` it, COMMIT the "
                               f"merge, and run this again")
    head = _git(["-C", path, "rev-parse", "HEAD"]).stdout.strip()
    pushed = head != pr_tip
    if pushed:
        _git(["-C", path, "push", rem, f"HEAD:refs/heads/{branch}"])
    out.update(head=head, synced=pushed)

    # --- the machine gate, against exactly `head` ---
    if cfg["gate"]["ci"] == "local":
        command, record, void = cfg["gate"]["local_command"], None, None
        if cfg["gate"]["trust_recorded_run"]:
            record = _gate_record(path)
            void = afk_decide.gate_record_void(record, head, command)
        if cfg["gate"]["trust_recorded_run"] and void is None:
            out["gate"] = {"status": "green", "source": "recorded", "head": head,
                           "command": command, "recorded_at": record["at"]}
        else:
            gate, *_ = _gate_run(cfg, path, a.gate_timeout, a.excerpt_lines, _now(a))
            gate = {**gate, "source": "run", "head": head, **({"not_trusted": void} if void else {})}
            if gate["status"] != "green":
                _gh(["pr", "comment", str(pr["number"]), "--repo", a.repo, "--body",
                     afk_decide.gate_comment(gate, gate["command"])])
                return stop("gate_red", gate=gate,
                            detail="the gate is red on the synced head — fix the code, commit, "
                                   "and run this again")
            out["gate"] = {k: gate[k] for k in ("status", "source", "head", "command", "not_trusted")
                           if k in gate}
    else:
        checks = afk_decide.pr_checks_state(pr.get("statusCheckRollup"))
        verdict = afk_decide.checks_gate(checks, pushed, turn["allow_no_checks"])
        if verdict == "gate_red":
            return stop(verdict, checks=checks,
                        detail="the PR's checks are red — fix the code, commit, and run this again")
        if verdict != "green":
            return stop(verdict, checks=checks,
                        detail="send your wake and stop — you are told to run this again once "
                               "the checks on this head are in")
    if cfg["gate"]["adversarial_verify"] and turn["verified"] != head:
        return stop("needs_verify",
                    detail="the head that would land is not the one that was verified — send "
                           "your wake and stop; you are told to run this again once it is")

    # --- land it. The claim and this worktree are the next cycle's to settle ---
    _gh(["pr", "merge", str(pr["number"]), "--repo", a.repo, f"--{cfg['merge']['strategy']}",
         "--match-head-commit", head,
         *(["--delete-branch"] if cfg["merge"]["delete_branch"] else [])])
    if cfg["progress_comment"]:
        _upsert_board(a.repo, a.number, cfg, "merged", instance=owner, pr=pr["number"])
    return {**out, "outcome": afk_decide.land_outcome("merged"),
            "detail": "landed — send your wake and stop"}


def _escalate(a, cfg, rem, issue, attempt):
    """Hand an issue to a human, in the one order that leaves no gap: status board
    → labels → comment → release. The claim is released LAST: released first, a
    PR-less issue still carrying `ready_label` is back on the frontier for a peer
    to dispatch before the relabel lands."""
    number = issue["number"]
    pr = afk_decide.closing_pr(_open_prs(a.repo), number)
    pr_number = pr["number"] if pr else None
    if cfg["progress_comment"]:
        _upsert_board(a.repo, number, cfg, "escalated", instance=a.instance,
                      pr=pr_number, attempt=attempt)
    add, remove = afk_decide.escalation_labels(issue["labels"], cfg)
    _ensure_label(a.repo, add[0])
    _edit_labels(a.repo, number, add, remove)
    comment_id = None
    if cfg["escalate_comment"]:
        p = _gh(["api", "--method", "POST", f"repos/{a.repo}/issues/{number}/comments", "-f",
                 f"body={afk_decide.escalation_comment(a.reason, attempt, pr_number)}"])
        comment_id = json.loads(p.stdout).get("id")
    _release(rem, cfg, number)
    return {"issue": number, "action": "escalate", "attempt": attempt, "pr": pr_number,
            "labels": {"added": add, "removed": remove}, "comment_id": comment_id,
            "released": True}


def _ensure_label(repo, name):
    """Make sure a label exists before it is applied (gh refuses to add an unknown
    one). Created without `--force`, so a label that already exists keeps its
    colour and description — that failure is the expected case and is ignored."""
    _gh(["label", "create", name, "--repo", repo], check=False)


def _edit_labels(repo, number, add, remove):
    args = ["issue", "edit", str(number), "--repo", repo]
    for lb in add:
        args += ["--add-label", lb]
    for lb in remove:
        args += ["--remove-label", lb]
    _gh(args)


def cmd_fail(a):
    """One of my claims FAILED — its checks are red, a verifier refuted it, or its
    worker gave up or went quiet with no outcome: no PR, or a landing turn it did
    not land. A failed PR is closed, which is also what frees its landing turn. The retry ladder, as one
    transition (ADR-0017): read the attempt off the issue's `afk-attempt/<n>`
    label, then either

      retry     swap the label up by one, discard the failed attempt (close its PR,
                delete its branch, remove its worktree) and start a FRESH worker
                under the same claim, handed `--reason`; or
      escalate  when the attempts are exhausted: status board → relabel → comment
                `--reason` → release the claim.

    `--reason` is the tick's judgment — the failure, re-read from where it lives.
    This is the one writer of the attempt label, as `current_attempt` is its one
    reader."""
    cfg, rem = _cfg(a), _remote(a)
    _require_mine(rem, cfg, a.number, a.instance)
    issue = _issue(a.repo, a.number)
    a.reason = _stalled_reason(a.repo, a.number, a.reason)   # before the worktree is discarded
    decision = afk_decide.next_attempt(afk_decide.current_attempt(issue["labels"]), cfg["retry"])
    if decision["action"] == "escalate":
        return _escalate(a, cfg, rem, issue, decision["attempt"])
    _ensure_label(a.repo, decision["to_label"])
    _edit_labels(a.repo, a.number, [decision["to_label"]],
                 [lb for lb in afk_decide.attempt_labels(issue["labels"])
                  if lb != decision["to_label"]])
    worker = _start_worker(a, cfg, rem, issue, "fresh", a.reason)
    return {"issue": a.number, "action": "retry", "attempt": decision["attempt"],
            "retry_max": cfg["retry"], "worker": worker}


def cmd_escalate(a):
    """Hand one of my claims straight to a human, outside the retry ladder — a DAG
    gap (`afk no-pr` → `idle_blocked` / `escalate`: a blocker nothing will resolve,
    or none named). Same ordered transition `afk fail` ends in; the attempt count is
    reported, not consulted."""
    cfg, rem = _cfg(a), _remote(a)
    _require_mine(rem, cfg, a.number, a.instance)
    issue = _issue(a.repo, a.number)
    return _escalate(a, cfg, rem, issue, afk_decide.current_attempt(issue["labels"]))


def cmd_park(a):
    """Leave one of my claims waiting on the dependency its worker discovered
    (`afk no-pr` → `idle_blocked` / `park`), in one order (ADR-0022): record a
    native `blocked_by` edge to each blocker still open → status board → release
    the claim → remove the worktree, when its branch holds no work.

    The edge goes first: from then on the frontier excludes the issue for as long
    as a blocker is open, so the release puts nothing back on it — and returns the
    issue by itself the tick after the last one closes. `ready_label` and the
    attempt labels are not touched. The standings are read again here, and a
    claim `afk no-pr` would not call parkable now is refused untouched."""
    cfg, rem = _cfg(a), _remote(a)
    _require_mine(rem, cfg, a.number, a.instance)
    declared = afk_decide.latest_verdict(_issue_comments(a.repo, a.number))
    standings = _blocker_standings(a, cfg, a.number, declared["blocked_by"], _open_prs(a.repo))
    refusal = afk_decide.park_refusal(declared, standings)
    if refusal:
        raise ValueError(f"issue #{a.number} is not parkable: {refusal}; nothing was changed")
    waiting = [b["number"] for b in standings if b["standing"] == "waiting"]
    path = _live_worktree(a.repo, a.number)
    progress = _worktree_progress(path, rem, cfg["base_branch"]) if path else {}

    recorded = {e["number"] for e in _blocked_by(a.repo, a.number)}
    added = [n for n in waiting if n not in recorded]
    for n in added:
        blocker_id = _gh(["api", f"repos/{a.repo}/issues/{n}", "--jq", ".id"]).stdout.strip()
        _gh(["api", "--method", "POST", f"repos/{a.repo}/issues/{a.number}/dependencies/blocked_by",
             "-F", f"issue_id={blocker_id}"])
    if cfg["progress_comment"]:
        _upsert_board(a.repo, a.number, cfg, "parked", blocked_by=waiting)
    _release(rem, cfg, a.number)
    empty = progress.get("commits_ahead") == 0 and not progress.get("dirty")
    cleanup = _remove_worktree(path) if (path and empty and cfg["worktree_cleanup"]) else None
    return {"issue": a.number, "action": "parked", "blocked_by": waiting, "edges_added": added,
            "released": True, **({"cleanup": cleanup} if cleanup else {})}


def cmd_close(a):
    """Close one of my claims whose issue needed no change (`afk no-pr` →
    `idle_done`), after the tick has verified the empty diff against base: status
    board → close the issue → release the claim → remove the worktree."""
    cfg, rem = _cfg(a), _remote(a)
    _require_mine(rem, cfg, a.number, a.instance)
    if cfg["progress_comment"]:
        _upsert_board(a.repo, a.number, cfg, "closed", instance=a.instance)
    _gh(["issue", "close", str(a.number), "--repo", a.repo, "--reason", "completed"])
    _release(rem, cfg, a.number)
    path, _ = _issue_worktree(a.repo, a.number)
    cleanup = _remove_worktree(path) if (path and cfg["worktree_cleanup"]) else None
    return {"issue": a.number, "action": "closed", "released": True,
            **({"cleanup": cleanup} if cleanup else {})}


# --------------------------------------------------------------------------- #
# arg wiring                                                                  #
# --------------------------------------------------------------------------- #

class _Parser(argparse.ArgumentParser):
    """argparse whose usage errors are the CLI's one error shape — `{"error": …}`,
    exit 3 — so a missing `--config` reads exactly like any other failure."""

    def error(self, message):
        print(json.dumps({"error": f"{self.prog}: {message}"}, ensure_ascii=False))
        sys.exit(3)


def build_parser():
    """The whole CLI. `.subcommands` maps each subcommand name to its parser —
    the interface the docs are checked against (test_afk_cli.py)."""
    ap = _Parser(prog="afk", description="afk-fleet deterministic tool")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ap.subcommands = sub.choices

    def command(name, fn, help, remote=None, needs_config=True):
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
                                "claim_lease_ttl_seconds=60 or gate.ci=local (repeatable; "
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

    def mine(p):
        p.add_argument("--instance", required=True, metavar="id", help="my fleet instance id")

    def stamp(p):
        mine(p)
        p.add_argument("--host", default=socket.gethostname())

    def issue(p):
        p.add_argument("--issue", dest="number", type=int, required=True, metavar="n")

    def starts_worker(p):
        """The flags of a subcommand that may start a worker (dispatch, turn, fail)."""
        stamp(p)
        p.add_argument("--worker-command", required=True, metavar="cmd",
                       help="the run's worker launch command, verbatim (ADR-0010)")
        p.add_argument("--ready-timeout", type=int, default=120, metavar="s",
                       help="seconds to wait for the started agent to accept a prompt")

    # --- bootstrap ---
    p = command("config", cmd_config, "parse + validate the repo config file → canonical JSON",
                needs_config=False)
    p.add_argument("--file", default=None, metavar="path", help="path to the target repo's docs/agents/afk-fleet.md")
    p.add_argument("--defaults", action="store_true", help="print the pure defaults table")

    command("probe", cmd_probe, remote="refs",
            help="bootstrap probe: the usable claim namespace (folded into the returned "
                 "config), and target-branch protection when gate.ci is local")

    p = command("worker-command", cmd_worker_command, needs_config=False,
                help="settle the command workers are started with: ask-or-not + candidates, "
                     "or --check a human's answer")
    p.add_argument("--check", default=None, metavar="cmd",
                   help="a candidate command: resolve its first word in the login shell "
                        "and report whether it runs (and looks unattended)")

    # --- the worker's own ---
    p = command("gate", cmd_gate,
                help="a WORKER's run of the local gate, in the worktree it is called from: "
                     "the log streams to its terminal, and a green run on a committed tree "
                     "is put on record for `afk land`")
    p.add_argument("--gate-timeout", type=int, default=1800, metavar="s",
                   help="seconds before the local gate is called red (default %(default)s)")

    p = command("land", cmd_land, remote="gh",
                help="a WORKER lands its own PR on its landing turn, in the worktree it is "
                     "called from: sync → push → gate → merge pinned to the gated head → "
                     "status board; refused without the turn")
    issue(p)
    p.add_argument("--gate-timeout", type=int, default=1800, metavar="s",
                   help="seconds before the local gate is called red (default %(default)s)")
    p.add_argument("--excerpt-lines", type=int, default=afk_decide.GATE_EXCERPT_LINES, metavar="k",
                   help="how many trailing log lines a red gate's excerpt keeps")

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
    p.add_argument("--ready-timeout", type=int, default=120, metavar="s",
                   help="seconds to wait for a started agent to accept a prompt")

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
    p.add_argument("--issue", dest="numbers", type=int, action="append", required=True,
                   metavar="n", help="one of my claims waiting on its worker; repeat for each")
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
                     "the turn) → status board, or the outcome that needs the tick's judgment")
    issue(p)
    starts_worker(p)
    p.add_argument("--verified", default=None, metavar="head",
                   help="the head sha an adversarial verify passed (gate.adversarial_verify)")
    p.add_argument("--allow-no-checks", action="store_true",
                   help="gate.ci required: let a PR that has no checks at all land (the tick's "
                        "progressive-gate judgment)")

    p = command("nudge", cmd_nudge, remote="gh",
                help="tell one of my workers that stopped without an outcome to carry on — "
                     "once, spending no attempt; the next silence is a failure")
    issue(p)
    mine(p)
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


def main():
    a = build_parser().parse_args()
    try:
        result = a.fn(a)
    except (OSError, ValueError, RuntimeError) as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False))
        sys.exit(3)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0)


if __name__ == "__main__":
    main()

