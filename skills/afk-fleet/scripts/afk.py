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
worker, `merge` lands a PR, `hand-back` returns a sync conflict to its worker,
`fail` / `escalate` / `close` settle a claim. Each
performs its whole ordered sequence in one process, so an invariant like "relabel
before release" or "start from the fetched base tip" is code, not a paragraph.

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
        raise RuntimeError(f"{what} failed: {code or p.stderr.strip() or p.stdout.strip()[:200]}")
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


def _contains(repo, sha, head):
    """Does `head` contain commit `sha`? Asked of GitHub (one compare, no objects
    fetched), so it reads the same from any machine. Raises when it cannot be
    read: "could not look" is never "the worker has not answered"."""
    behind = _gh(["api", f"repos/{repo}/compare/{sha}...{head}", "--jq", ".behind_by"]).stdout
    return int(behind.strip()) == 0


def _open_handback(repo, pr):
    """The hand-back still open on a PR (`afk_decide.handback_open`) → its record,
    or None when the PR was never handed back or its head already contains the
    target tip the hand-back named."""
    handback = afk_decide.latest_handback(_issue_comments(repo, pr["number"]))
    if handback is None:
        return None
    head = pr["headRefOid"]
    contains = head != handback["head"] and _contains(repo, handback["tip"], head)
    return handback if afk_decide.handback_open(handback, head, contains) else None


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


def cmd_release(a):
    return _release(_remote(a), _cfg(a), a.number)


def _require_mine(rem, cfg, number, instance):
    """Refuse to settle a claim this fleet does not hold: every transition that
    merges, relabels or releases an issue acts on MY claim only (ADR-0003)."""
    ref = _claim_ref(cfg, number)
    owner = _read_marker(rem, ref) if _remote_sha(rem, ref) else None
    if owner is None or owner.get("instance") != instance:
        held = f"held by {owner.get('instance')!r}" if owner else "not claimed at all"
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
# the launcher's cycle                                                         #
# --------------------------------------------------------------------------- #

def _gather(a, cfg):
    """The ONE gatherer of the observable fleet inputs (ADR-0008): open issues,
    open PRs, and the claim/heartbeat ref scan. Both `rebuild` and the launcher's
    `cycle` gate read through here, so their views cannot drift. Issues leave here
    with `labels` as a list of names — the one shape afk_decide reads; the raw
    JSON lives and dies in this process."""
    issues = json.loads(_gh(["issue", "list", "--repo", a.repo, "--state", "open",
                             "--limit", "200", "--json", "number,title,labels,updatedAt"]).stdout)
    issues = [{**i, "labels": [lb["name"] for lb in i.get("labels") or []]} for i in issues]
    claims, heartbeats = _scan(_remote(a), cfg["claim_namespace"])
    return issues, _open_prs(a.repo), claims, heartbeats


def cmd_cycle(a):
    """One launcher cycle's mechanics, in two shapes (ADR-0017):

      afk cycle [--state S]             the top: digest what a rebuild would observe
                                        (ADR-0007) → tick-or-skip. A tick comes with
                                        the schema its summary must return in. On a
                                        skip it also refreshes the lease when the
                                        fleet holds claims, and returns the sleep.
      afk cycle --state S --summary T   the bottom, after a tick returned T: fold it
                                        into the state and return the sleep.

    `state` is opaque to the launcher: it hands back the last one verbatim. Only
    the verdict, the state and the sleep ever reach a context — the raw
    issue/PR/ref JSON lives and dies here."""
    cfg = _cfg(a)
    state = afk_decide.cycle_state(json.loads(a.state) if a.state else None)
    if a.summary is not None:
        return afk_decide.cycle_ticked(state, json.loads(a.summary), cfg)
    fp = None
    if cfg["fingerprint_gate"]:
        issues, prs, claims, _ = _gather(a, cfg)  # heartbeats: see afk_decide.fingerprint
        fp = afk_decide.fingerprint(issues, prs, claims)
    result = afk_decide.cycle_wake(state, fp, cfg)
    if result.pop("heartbeat", False):
        result["heartbeat"] = _beat(_remote(a), cfg, a.instance, _now(a))
    return result


# --------------------------------------------------------------------------- #
# observation: rebuild / no-pr / recovery                                      #
# --------------------------------------------------------------------------- #

def cmd_rebuild(a):
    """One read-only call → the tick's whole working set (ADR-0008). The
    per-issue blocked_by read is paid only by issues that pass every cheaper
    eligibility check, the per-issue state read only by a claim of mine whose
    issue is missing from the open list, and the hand-back read only by a claim of
    mine that has a PR. Strictly observation: nothing here writes a ref, a
    comment, or a PR."""
    cfg = _cfg(a)
    issues, prs, claims, heartbeats = _gather(a, cfg)
    blocked = {}
    for n in afk_decide.frontier_candidates(issues, prs, claims,
                                            cfg["ready_label"], cfg["epic_labels"]):
        v = _gh(["api", f"repos/{a.repo}/issues/{n}",
                 "--jq", ".issue_dependencies_summary.blocked_by"]).stdout.strip()
        blocked[n] = 0 if v in ("", "null") else int(v)
    listed = {i["number"] for i in issues}
    closed = [c["number"] for c in claims
              if c["instance"] == a.instance and c["number"] not in listed
              and _issue_state(a.repo, c["number"]) == "closed"]
    my_prs = {c["number"]: afk_decide.closing_pr(prs, c["number"])
              for c in claims if c["instance"] == a.instance}
    handed_back = [n for n, pr in my_prs.items() if pr and _open_handback(a.repo, pr)]
    return afk_decide.assemble_working_set(issues, prs, claims, heartbeats, blocked,
                                           a.instance, _now(a), cfg, closed=closed,
                                           handed_back=handed_back)


def _issue_worktree(repo, number):
    """This machine's orca worktree for an issue → (path, branch), each None when
    there is none. Soft, like the read behind it: no orca means no worktree. A
    path orca remembers but the disk no longer has is returned as-is — callers
    check `os.path.isdir`."""
    hit = afk_decide.find_orca_worktree(_orca_worktree_rows(), number, repo)
    return hit["path"], hit["branch"]


def cmd_no_pr(a):
    """Why does one of my claims have no PR — is its worker still coding, or did it
    finish without one? And, for a `handed_back` claim, the same question about
    the conflict its worker was handed: still resolving, or gone quiet? One call
    gathers everything the outcome is decided from: the
    issue's worktree on this machine (found through orca) and its git progress, the
    worker's `afk:verdict` marker on the issue, and the state of each issue that
    marker says it is blocked by. The tick supplies only what code cannot see — the
    orca terminal probe. Returns `afk_decide.classify_no_pr`'s outcome plus the
    signals it was decided from: `worktree`, `progress`, `worker_verdict` (what
    the worker declared), and when it was last told something — `nudged_at`,
    `handed_back_at`."""
    cfg = _cfg(a)
    path = a.worktree
    if path is not None and not os.path.isdir(path):
        raise ValueError(f"worktree not found: {path} (omit --worktree to let orca find it)")
    if path is None:
        found, _ = _issue_worktree(a.repo, a.number)
        path = found if found and os.path.isdir(found) else None
    progress = _worktree_progress(path, _remote(a), cfg["base_branch"]) if path else {}
    declared = afk_decide.latest_verdict(_issue_comments(a.repo, a.number))
    blocker_states = {n: _issue_state(a.repo, n) for n in declared["blocked_by"]}
    nudged_at = (_nudge(path) or {}).get("at")
    pr = afk_decide.closing_pr(_open_prs(a.repo), a.number)
    handed_back_at = ((_open_handback(a.repo, pr) if pr else None) or {}).get("at")
    return {"issue": a.number,
            **afk_decide.classify_no_pr(progress, a.terminal, a.terminal_idle_seconds, declared,
                                        blocker_states, _now(a),
                                        cfg["worker_idle_grace_seconds"],
                                        nudged_at=nudged_at, can_nudge=path is not None,
                                        handed_back_at=handed_back_at),
            "worktree": path, "progress": progress, "worker_verdict": declared,
            "nudged_at": nudged_at, "handed_back_at": handed_back_at}


def cmd_nudge(a):
    """Tell a worker that stopped without an outcome to carry on (`afk no-pr` →
    `idle_stalled` / `nudge`) — the step before failure handling, and one that
    spends no attempt and discards nothing (ADR-0018). Reads the tail of its
    screen (where it stopped), types one line at it, and records the nudge in the
    worktree, so the next silence is a failure and not a second nudge.

      {"issue", "action": "nudged", "terminal", "terminal_tail": [...]}"""
    cfg, rem = _cfg(a), _remote(a)
    _require_mine(rem, cfg, a.number, a.instance)
    path = a.worktree or _issue_worktree(a.repo, a.number)[0]
    if not path or not os.path.isdir(path):
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
    path, _ = _issue_worktree(repo, number)
    path = path if path and os.path.isdir(path) else None
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


def _worker_file(path, name):
    """A fleet-private file about the worker in a worktree, kept in the worktree's
    own git dir: never staged by the worker's `git add -A`, gone when the worktree
    is."""
    git_dir = _git(["-C", path, "rev-parse", "--absolute-git-dir"]).stdout.strip()
    return os.path.join(git_dir, name)


def _write_brief(path, prompt):
    """Write a worker's instructions to the worktree's brief file → its path. A
    worker under a new brief — a new worker, or one a sync conflict was handed
    back to — has not been nudged, whatever came before."""
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


def _handback_fields(pr, handback):
    """The HANDBACK_FIELDS of a worker prompt, from a PR and its hand-back record."""
    return {"pr": pr["number"], "pr_branch": pr["headRefName"], "target": handback["target"],
            "target_tip": handback["tip"], "files": handback["files"]}


def _prompt_fields(a, cfg, issue, path, branch):
    """The PROMPT_FIELDS of a worker prompt for one issue in one worktree. The
    launcher's terminal is read off the environment, never passed in: a tick is a
    subagent of the launcher, so the handle orca gave that terminal is the one
    this process inherited (ADR-0020)."""
    return {"n": issue["number"], "title": issue["title"], "repo": a.repo,
            "base_branch": cfg["base_branch"], "local_command": cfg["gate"]["local_command"],
            "branch": branch, "worktree_path": path,
            "launcher_terminal": os.environ.get("ORCA_TERMINAL_HANDLE", "")}


def _start_worker(a, cfg, rem, issue, start, reason=None):
    """Put a worker on an issue this fleet already holds the claim for.

    `start` is "auto" — continue from whatever progress survives (the worktree
    still here, else the pushed branch, else a fresh start from base: the
    continuation tiers of ADR-0011) — or "fresh": discard the previous attempt and
    start from base, which is what a retry is. A continued worker whose PR
    carries an open hand-back is started ON it: the sync conflict is its
    instruction, and it never opens a second PR (ADR-0019). Returns the tier taken
    plus where the worker now is: {tier, action, prompt, reason, worktree, branch,
    terminal}."""
    number = issue["number"]
    if issue["state"] != "open":
        raise RuntimeError(f"issue #{number} is {issue['state']}, not open — there is nothing "
                           f"to retry (release the claim instead)")
    discarded, pr, handback = None, None, None
    if start == "fresh":
        discarded = _discard_attempt(a, cfg, rem, number)
        plan = {"tier": 3, "action": "dispatch_fresh", "prompt": "fresh",
                "reason": "fresh start: the previous attempt was discarded"}
    else:
        rec = _recovery(cfg, rem, a.repo, number)
        plan = {k: rec[k] for k in ("tier", "action", "prompt", "reason")}
        pr = afk_decide.closing_pr(_open_prs(a.repo), number)
        handback = _open_handback(a.repo, pr) if pr else None

    if plan["action"] == "reuse_worktree":
        path = rec["worktree"]["path"]
        branch = _git(["-C", path, "rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()
        # whatever agent was here is dead or idle: two in one worktree would fight
        try:
            _orca(["terminal", "close", "--worktree", f"path:{path}", "--all"])
        except RuntimeError:
            pass
    else:
        tip = rec["branch"]["name"] if plan["action"] == "recreate_at_tip" else cfg["base_branch"]
        path, branch, _ = _create_worktree(a, cfg, rem, issue, tip)

    with open(_WORKER_PROMPT) as f:
        prompt = afk_decide.render_worker_prompt(
            f.read(), plan["prompt"], _prompt_fields(a, cfg, issue, path, branch), reason=reason,
            handback=_handback_fields(pr, handback) if handback else None)
    handle = _start_terminal(path, a.worker_command, prompt, a.ready_timeout)
    if cfg["progress_comment"]:
        if handback:
            _upsert_board(a.repo, number, cfg, "handed_back", instance=a.instance,
                          pr=pr["number"])
        else:
            _upsert_board(a.repo, number, cfg, "claimed", instance=a.instance)
    return {**plan, "worktree": path, "branch": branch, "terminal": handle,
            **({"handed_back": pr["number"]} if handback else {}),
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
# act: settling a claim — merge / fail / escalate / close                      #
# --------------------------------------------------------------------------- #

def _upsert_board(repo, number, cfg, phase, instance=None, pr=None, attempt=0):
    """Upsert the human-facing progress status board comment (idempotent, ADR-0006).
    Renders the body from the given phase (pure), then find-or-create by marker
    and write ONLY when the body changed — so re-entrant/disposable ticks and
    retry re-dispatches never spam the issue."""
    body = afk_decide.render_status_board(phase, cfg["gate"]["ci"], cfg["retry"],
                                          instance=instance, pr=pr, attempt=attempt)
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
    them (`afk merge`, `afk escalate`, `afk close`), before it releases the claim."""
    return _upsert_board(a.repo, a.number, _cfg(a), a.phase,
                         instance=a.instance, pr=a.pr, attempt=a.attempt)


def _run_gate(cfg, worktree, timeout, excerpt_lines):
    """Run the configured local gate in a worktree → `afk_decide.gate_verdict` —
    the completion gate itself in `gate.ci: local` mode, run at MERGE time against
    the exact tree that lands (ADR-0012). *What* to run is config, *where* is the
    branch's worktree, and *whether it passed* is an exit code — no judgment. A run
    that times out is red, never green-by-default; the tick gets a verdict and a
    bounded excerpt, never a raw log."""
    cmd = cfg["gate"]["local_command"]

    def _text(s):
        return s.decode("utf-8", "replace") if isinstance(s, bytes) else (s or "")

    timed_out, rc = False, 0
    try:
        p = subprocess.run(cmd, shell=True, cwd=worktree, capture_output=True,
                           text=True, timeout=timeout, env=_GIT_ENV)
        out, rc = _text(p.stdout) + _text(p.stderr), p.returncode
    except subprocess.TimeoutExpired as e:
        out = _text(e.stdout) + _text(e.stderr) + f"\n[afk] gate timed out after {timeout}s"
        timed_out, rc = True, 124
    return {**afk_decide.gate_verdict(rc, out, excerpt_lines, timed_out), "command": cmd}


def _unmerged(path):
    """The files a merge in progress in a worktree has left conflicted."""
    out = _git(["-C", path, "diff", "--name-only", "--diff-filter=U"]).stdout
    return [ln for ln in out.splitlines() if ln]


def _sync(rem, path, target):
    """Merge the remote's `target` tip into the worktree's branch — never a rebase
    (ADR-0012) → the conflicted file list, empty when the sync is clean. A
    conflict is LEFT IN PLACE: what becomes of it is the tick's judgment —
    `afk hand-back` reads it from the worktree and returns it to the worker, or
    the tick commits a resolution there and a re-run of `afk merge` picks up from
    it. Runs under the caller's own git identity (the merge commit lands on a PR)."""
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


def cmd_merge(a):
    """Land one of my claims' PRs — the whole serialized merge sequence in one call
    (ADR-0017), stopping with an `outcome` wherever the tick's judgment is needed:

      merged        synced, gate re-confirmed on the tree that lands, merged, status
                    board upserted, claim released, worktree removed.
      conflict      the sync conflicted; the merge is left in progress in `worktree`
                    with `files` unmerged. `afk hand-back` returns it to the worker
                    that wrote the branch (the default); or resolve + commit there
                    and re-run, when it is purely mechanical.
      handed_back   an earlier conflict on this PR is with its worker and the PR
                    head does not contain the target tip it named yet. Nothing was
                    touched; leave it (`afk no-pr` watches the worker).
      gate_red      local mode: the merge-time gate was red (its excerpt is now a
                    PR comment). required mode: the PR's checks are red. → `afk fail`.
      awaiting_ci   required mode: checks pending, or the sync just pushed and CI
                    must run on the new head. Leave it; a later tick merges.
      no_checks     required mode, and the PR has no checks at all — the
                    progressive gate. Re-run with `--allow-no-checks` if the tick
                    judges the acceptance criteria met.
      needs_verify  `gate.adversarial_verify` is on and `--verified` does not name
                    the head that would land. Run the verifier on `head`, then
                    re-run with `--verified <head>`.

    The invariant every path keeps: what lands on the target was gated in the form
    it lands (ADR-0012) — `gh pr merge` is pinned to the gated head."""
    cfg, rem = _cfg(a), _remote(a)
    _require_mine(rem, cfg, a.number, a.instance)
    pr = afk_decide.closing_pr(_open_prs(a.repo), a.number)
    if pr is None:
        raise RuntimeError(f"no open PR closes issue #{a.number} — nothing to merge")
    branch, target = pr["headRefName"], cfg["merge"]["target"]
    out = {"issue": a.number, "pr": pr["number"]}

    def stop(outcome, **more):
        return {**out, "outcome": afk_decide.merge_outcome(outcome), **more}

    if _open_handback(a.repo, pr):         # before the worktree: the worker is in it
        return stop("handed_back",
                    detail="a sync conflict on this PR was handed back to its worker and is not "
                           "resolved yet; nothing was touched")

    # --- the branch's worktree: the worker's, else one recreated at the PR head ---
    path, _ = _issue_worktree(a.repo, a.number)
    recreated = not (path and os.path.isdir(path))
    if recreated:
        path, _, pr_tip = _create_worktree(a, cfg, rem, _issue(a.repo, a.number), branch)
    else:
        pr_tip = _fetch_tip(rem, branch, cwd=path)
        here = _git(["-C", path, "rev-parse", "HEAD"]).stdout.strip()
        if here != pr_tip and _git(["-C", path, "merge-base", "--is-ancestor", here, pr_tip],
                                   check=False).returncode == 0:
            _git(["-C", path, "merge", "--ff-only", pr_tip])   # the worktree was behind its PR
    out["worktree"] = path

    # --- sync: merge the target in, push what that produced ---
    if cfg["merge"]["sync_before_merge"]:
        files = _sync(rem, path, target)
        if files:
            return stop("conflict", files=files,
                        detail=f"merging {target} into {branch} conflicted; the merge is in "
                               f"progress in the worktree — `afk hand-back` returns it to the "
                               f"worker")
    head = _git(["-C", path, "rev-parse", "HEAD"]).stdout.strip()
    pushed = head != pr_tip
    if pushed:
        _git(["-C", path, "push", rem, f"HEAD:refs/heads/{branch}"])
    out.update(head=head, synced=pushed)

    # --- the machine gate, against exactly `head` ---
    if cfg["gate"]["ci"] == "local":
        gate = _run_gate(cfg, path, a.gate_timeout, a.excerpt_lines)
        if gate["status"] != "green":
            _gh(["pr", "comment", str(pr["number"]), "--repo", a.repo, "--body",
                 afk_decide.gate_comment(gate, gate["command"])])
            return stop("gate_red", gate=gate)
    else:
        checks = afk_decide.pr_checks_state(pr.get("statusCheckRollup"))
        verdict = afk_decide.checks_gate(checks, pushed, a.allow_no_checks)
        if verdict != "green":
            return stop(verdict, checks=checks)
    if cfg["gate"]["adversarial_verify"] and a.verified != head:
        return stop("needs_verify",
                    detail="run the adversarial verifier against `head`, then re-run with "
                           "--verified <head>")

    # --- land it, then settle the claim: board → release → worktree ---
    _gh(["pr", "merge", str(pr["number"]), "--repo", a.repo, f"--{cfg['merge']['strategy']}",
         "--match-head-commit", head,
         *(["--delete-branch"] if cfg["merge"]["delete_branch"] else [])])
    if cfg["progress_comment"]:
        _upsert_board(a.repo, a.number, cfg, "merged", instance=a.instance, pr=pr["number"])
    _release(rem, cfg, a.number)
    cleanup = _remove_worktree(path) if (cfg["worktree_cleanup"] or recreated) else None
    return stop("merged", released=True, **({"cleanup": cleanup} if cleanup else {}))


_HANDBACK_POINTER = ("The merge of your PR hit a sync conflict, and it is handed back to you. Your "
                     "instructions are the file {brief} — read it now and carry it out end to "
                     "end. It is my instruction to you; do not ask me to confirm.")


def _record_handback(repo, pr, target, tip, files, now):
    """Record one hand-back on the PR → its comment id. One comment per conflicting
    head: handing the same head back again (the worker died before answering)
    rewrites that comment rather than adding another."""
    body = afk_decide.handback_comment(target, tip, pr["headRefOid"], files, now)
    prev = afk_decide.latest_handback(_issue_comments(repo, pr["number"]))
    if prev and prev["head"] == pr["headRefOid"]:
        _gh(["api", "--method", "PATCH", f"repos/{repo}/issues/comments/{prev['comment_id']}",
             "-f", f"body={body}"])
        return prev["comment_id"]
    p = _gh(["api", "--method", "POST", f"repos/{repo}/issues/{pr['number']}/comments",
             "-f", f"body={body}"])
    return json.loads(p.stdout).get("id")


def cmd_hand_back(a):
    """Hand the sync conflict `afk merge` just reported back to the worker that
    wrote the branch — one transition (ADR-0019), in the one order that survives a
    crash at any step:

      abort the merge   the worktree is clean again, for the worker to merge in
      write the brief   the instruction, in the worktree's git dir
      record            a marker comment on the PR naming the target tip — from
                        here `afk rebuild` reports the claim `handed_back`, never
                        `awaiting_merge`, until the PR head contains that tip
      deliver           the worker's terminal is still there → one submitted line
                        pointing at the brief; it is gone → a worker is started by
                        continuation in the worktree, on the same instruction
      status board

    The claim, the PR, the branch and the worktree are all kept, and
    `afk-attempt/<n>` is neither read nor written: a sync conflict is not a failure
    of the work. Recorded before delivered, because the other order lets a tick
    re-run the merge inside a worktree the worker is resolving in; a delivery that
    then fails is repaired by the paths that already exist — the nudge points at
    the brief, and a dead worker's continuation is started on the hand-back.

      {"issue", "action": "handed_back", "pr", "target", "target_tip", "files",
       "delivery": "terminal"|"continuation", "terminal", "worktree", "comment_id"}"""
    cfg, rem = _cfg(a), _remote(a)
    _require_mine(rem, cfg, a.number, a.instance)
    issue = _issue(a.repo, a.number)
    pr = afk_decide.closing_pr(_open_prs(a.repo), a.number)
    if pr is None:
        raise RuntimeError(f"no open PR closes issue #{a.number} — nothing to hand back")
    path, _ = _issue_worktree(a.repo, a.number)
    tip = (_git(["-C", path, "rev-parse", "-q", "--verify", "MERGE_HEAD"], check=False)
           .stdout.strip() if path and os.path.isdir(path) else "")
    if not tip:
        raise RuntimeError(f"issue #{a.number} has no sync conflict in progress on this machine "
                           f"— a hand-back acts on the `conflict` outcome of `afk merge`; run "
                           f"that first")
    target, files = cfg["merge"]["target"], _unmerged(path)
    _git(["-C", path, "merge", "--abort"])
    out = {"issue": a.number, "action": "handed_back", "pr": pr["number"], "target": target,
           "target_tip": tip, "files": files, "worktree": path}

    handle = _live_terminal(path)
    if handle:
        branch = _git(["-C", path, "rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()
        with open(_WORKER_PROMPT) as f:
            brief = _write_brief(path, afk_decide.render_handback(
                f.read(), _prompt_fields(a, cfg, issue, path, branch),
                _handback_fields(pr, {"target": target, "tip": tip, "files": files})))
    out["comment_id"] = _record_handback(a.repo, pr, target, tip, files, _now(a))
    if not handle:          # the worker is gone: its continuation is started on the record
        worker = _start_worker(a, cfg, rem, issue, "auto")
        return {**out, "delivery": "continuation", "terminal": worker["terminal"],
                "worktree": worker["worktree"]}
    sent = _orca(["terminal", "send", "--terminal", handle, "--enter", "--text",
                  _HANDBACK_POINTER.format(brief=brief)]).get("send") or {}
    if not sent.get("accepted"):
        raise RuntimeError(f"terminal {handle} did not accept the hand-back")
    if cfg["progress_comment"]:
        _upsert_board(a.repo, a.number, cfg, "handed_back", instance=a.instance, pr=pr["number"])
    return {**out, "delivery": "terminal", "terminal": handle}


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
    """One of my claims FAILED — its checks or merge-time gate are red, a sync
    conflict handed back to its worker went unanswered, a verifier refuted it, or
    its worker gave up or went quiet with no outcome. The retry ladder, as one
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
    gap (`afk no-pr` → `idle_blocked` / `escalate`: a blocker that is still open, or
    none named). Same ordered transition `afk fail` ends in; the attempt count is
    reported, not consulted."""
    cfg, rem = _cfg(a), _remote(a)
    _require_mine(rem, cfg, a.number, a.instance)
    issue = _issue(a.repo, a.number)
    return _escalate(a, cfg, rem, issue, afk_decide.current_attempt(issue["labels"]))


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
        """The flags of a subcommand that may start a worker (dispatch, hand-back, fail)."""
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

    # --- the launcher's cycle ---
    p = command("cycle", cmd_cycle, remote="gh",
                help="one launcher cycle: tick-or-skip at the top (with the skipped cycle's "
                     "heartbeat and sleep), or — with --summary — the sleep after a tick")
    mine(p)
    p.add_argument("--state", default=None, metavar="json",
                   help="the `state` the previous `afk cycle` returned, verbatim (omit on "
                        "the first cycle)")
    p.add_argument("--summary", default=None, metavar="json",
                   help="the summary JSON of the tick that just ran: folds it into the "
                        "state and returns the sleep")

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

    p = command("release", cmd_release, "delete a claim ref (idempotent)", remote="refs")
    p.add_argument("number", type=int, metavar="n")

    p = command("heartbeat", cmd_heartbeat, "refresh my heartbeat if due", remote="refs")
    mine(p)

    # --- observation ---
    p = command("rebuild", cmd_rebuild, "gather + assemble the tick's working set (read-only)",
                remote="gh")
    mine(p)

    p = command("no-pr", cmd_no_pr, remote="gh",
                help="why one of my claims has no PR (or has not answered a hand-back) → "
                     + " / ".join(dict.fromkeys(o for o, _ in afk_decide.NO_PR_ROUTES))
                     + ", gathered and decided in one call")
    issue(p)
    p.add_argument("--terminal", choices=list(afk_decide.TERMINAL_STATES), required=True,
                   help="the orca probe: " + " | ".join(afk_decide.TERMINAL_STATES)
                        + " (none = no live worker)")
    p.add_argument("--terminal-idle-seconds", type=int, default=None, metavar="s",
                   help="seconds since the terminal last showed activity, if the probe says")
    p.add_argument("--worktree", default=None, metavar="path",
                   help="the worker's worktree, to override the one orca reports for the issue")

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

    p = command("merge", cmd_merge, remote="gh",
                help="land one of my claims' PRs: sync → gate → merge → status board → "
                     "release → cleanup, or the outcome that needs the tick's judgment")
    issue(p)
    mine(p)
    p.add_argument("--verified", default=None, metavar="head",
                   help="the head sha an adversarial verify passed (gate.adversarial_verify)")
    p.add_argument("--allow-no-checks", action="store_true",
                   help="gate.ci required: merge a PR that has no checks at all (the tick's "
                        "progressive-gate judgment)")
    p.add_argument("--gate-timeout", type=int, default=1800, metavar="s",
                   help="seconds before the local gate is called red (default %(default)s)")
    p.add_argument("--excerpt-lines", type=int, default=afk_decide.GATE_EXCERPT_LINES, metavar="k",
                   help="how many trailing log lines a red gate's excerpt keeps")

    p = command("hand-back", cmd_hand_back, remote="gh",
                help="return the sync conflict `afk merge` reported to the worker that wrote "
                     "the branch: abort the merge → instruct the worker (or continue a dead "
                     "one) → record it on the PR → status board; no attempt is spent")
    issue(p)
    starts_worker(p)

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

