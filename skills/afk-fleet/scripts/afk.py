#!/usr/bin/env python3
"""
afk.py — the afk-fleet tool: deterministic muscle the LLM tick calls.

The tick (an LLM) orchestrates and judges; when it needs a *deterministic* action
it shells out to one of these subcommands and reads back JSON (ADR-0004).
Every subcommand prints one JSON object to stdout:

  exit 0  it ran. A lost claim race is an outcome, not an error: `{"won": false}`.
  exit 3  an operational error (git/gh failed, bad input) → `{"error": "..."}`.
          A push that failed for any reason OTHER than losing the race is exit 3,
          never `won: false` — a fleet that cannot push must not look merely unlucky.

Two layers. Every decision is a pure function in afk_decide.py (no I/O, time
injected, fixture-tested). This file only gathers their inputs — git refs, a
worktree's git, gh, orca, the login shell — and applies their effects.

Every subcommand takes the same `--config` (the canonical JSON from `afk config`,
then `afk probe`) and resolves it one way, in `_cfg`: flag → `--config` →
CONFIG_DEFAULTS (ADR-0009).

Invoked as:  python3 <skill>/scripts/afk.py <subcommand> [flags]
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

# Override flags, by argparse dest → the config key (path) each one overrides.
_FLAG_OVERRIDES = {
    "ns": ("claim_namespace",),
    "ttl": ("claim_lease_ttl_seconds",),
    "ready_label": ("ready_label",),
    "epic_labels": ("epic_labels",),
    "ci": ("gate", "ci"),
    "command": ("gate", "local_command"),
    "target": ("merge", "target"),
    "base": ("base_branch",),
    "retry": ("retry",),
    "grace": ("worker_idle_grace_seconds",),
    "force_after": ("force_tick_after_skips",),
}


def _cfg(a):
    """The effective config for a subcommand: `--config` JSON (canonical or
    partial) resolved through CONFIG_DEFAULTS, with any override flag this
    subcommand was given laid on top."""
    cfg = afk_decide.resolve_config(json.loads(a.config) if a.config else {})
    for dest, path in _FLAG_OVERRIDES.items():
        value = getattr(a, dest, None)
        if value is None:
            continue
        node = cfg
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = value
    return cfg


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

_LOCAL_SCAN = "refs/afk-scan"  # where `scan` mirrors remote refs, read-only, disposable
_LOCAL_RECOVERY = "refs/afk-recovery"  # ditto for `recovery`'s branch-vs-base compare


def _ns_paths(base):
    """Map the claim base namespace to (claim_ns, heartbeat_ns).
    `refs/afk` → hidden namespace; `refs/heads` → the branch fallback (ADR-0003)."""
    if base == "refs/heads":
        return "refs/heads/afk-claim", "refs/heads/afk-heartbeat"
    return base + "/claim", base + "/heartbeat"


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


def _issue_comments(repo, number):
    """An issue's comments, oldest first, as [{"id", "body", "url"}...]."""
    p = _gh(["api", "--paginate", f"repos/{repo}/issues/{number}/comments",
             "--jq", ".[] | {id, body, url: .html_url}"])
    return [json.loads(ln) for ln in p.stdout.splitlines() if ln.strip()]


def _issue_state(repo, number):
    """"open" | "closed", or None if the issue could not be read."""
    p = _gh(["api", f"repos/{repo}/issues/{number}", "--jq", ".state"], check=False)
    return p.stdout.strip() or None if p.returncode == 0 else None


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


def _worktree_progress(wt, base):
    """One worktree's git progress: commits ahead of `base`, dirty tree, last
    commit + newest file mtime. `commits_ahead` is None when the count could not be
    read at all (a missing base ref, a broken worktree) — unreadable is NOT zero,
    and `select_recovery` relies on the difference."""
    ahead = _git(["-C", wt, "rev-list", "--count", f"{base}..HEAD"], check=False).stdout.strip()
    dirty = _git(["-C", wt, "status", "--porcelain"], check=False).stdout.strip()
    ct = _git(["-C", wt, "log", "-1", "--format=%ct"], check=False).stdout.strip()
    return {"commits_ahead": int(ahead) if ahead.isdigit() else None,
            "dirty": bool(dirty),
            "last_commit_ts": int(ct) if ct.isdigit() else None,
            "worktree_mtime_ts": _newest_mtime(wt)}


def _remote_heads(remote):
    """Every branch name on the remote (one `ls-remote`), or None if it failed."""
    p = _git(["ls-remote", "--heads", remote], check=False)
    if p.returncode != 0:
        return None
    heads = []
    for line in p.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].startswith("refs/heads/"):
            heads.append(parts[1][len("refs/heads/"):])
    return heads


def _branch_ahead(remote, branch, base, slot):
    """How many commits `branch` is ahead of `base` ON THE REMOTE — the tier-2
    signal. git only (no gh), mirrored into a disposable local namespace, so it
    reads the same whether the remote is a GitHub URL or a bare path. `slot` (the
    issue number) keeps those temp refs per-issue, so two recoveries sharing one
    clone cannot read each other's mirror. Returns (count|None, detail)."""
    ours = f"{_LOCAL_RECOVERY}/{slot}"
    p = _git(["fetch", "--force", remote,
              f"{branch}:{ours}/branch", f"{base}:{ours}/base"], check=False)
    if p.returncode != 0:
        return None, p.stderr.strip()
    out = _git(["rev-list", "--count", f"{ours}/base..{ours}/branch"],
               check=False).stdout.strip()
    return (int(out) if out.isdigit() else None), ""


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
    read every marker. Returns (claims, heartbeats)."""
    claim_ns, hb_ns = _ns_paths(ns)
    _git(["fetch", "--prune", remote,
          f"+{claim_ns}/*:{_LOCAL_SCAN}/claim/*",
          f"+{hb_ns}/*:{_LOCAL_SCAN}/heartbeat/*"], check=False)
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


def _claim_ref(cfg, number):
    return f"{_ns_paths(cfg['claim_namespace'])[0]}/{number}"


def cmd_claim(a):
    ref, rem = _claim_ref(_cfg(a), a.number), _remote(a)
    sha = _marker_commit("afk-claim", a.instance, _now(a), host=a.host)
    # Create-only: the server rejects a ref that already exists → that is the CAS.
    p = _git(["push", rem, f"{sha}:{ref}"], check=False)
    if p.returncode == 0:
        return {"won": True, "issue": a.number, "ref": ref, "sha": sha, "instance": a.instance}
    owner = _read_marker(rem, ref)  # who beat us
    if owner is None:
        raise RuntimeError(f"claim push to {ref} failed and no such claim exists on the "
                           f"remote, so this is not a lost race: {p.stderr.strip()}")
    return {"won": False, "issue": a.number, "ref": ref,
            "owner": owner, "detail": p.stderr.strip()}


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


def cmd_release(a):
    ref = _claim_ref(_cfg(a), a.number)
    p = _git(["push", _remote(a), "--delete", ref], check=False)
    # Already gone counts as released — idempotent cleanup.
    ok = p.returncode == 0 or "remote ref does not exist" in p.stderr or "deleted" in p.stderr
    return {"released": bool(ok), "issue": a.number, "ref": ref, "detail": p.stderr.strip()}


def cmd_heartbeat(a):
    cfg, rem, now = _cfg(a), _remote(a), _now(a)
    ref = f"{_ns_paths(cfg['claim_namespace'])[1]}/{a.instance}"
    last = (_read_marker(rem, ref) or {}).get("ts")
    if not afk_decide.heartbeat_due(last, now, cfg["claim_lease_ttl_seconds"]):
        return {"refreshed": False, "reason": "not due", "ts": last, "ref": ref}
    sha = _marker_commit("afk-heartbeat", a.instance, now)
    _git(["push", rem, "--force", f"{sha}:{ref}"])
    return {"refreshed": True, "ts": now, "ref": ref}


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
    # Load time is the ONE place semantic validation runs (ADR-0009/ADR-0012): the
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
    the `refs/heads` branch fallback — as (namespace, rejection|None). Only a
    push the SERVER rejected (an org ruleset forbidding `refs/afk/*`) moves on
    to the fallback; a push that never reached a verdict (auth, network) raises,
    because that says nothing about which namespace is allowed."""
    rejection = None
    for ns in dict.fromkeys([wanted, "refs/heads"]):
        ref = f"{_ns_paths(ns)[0]}/probe"
        sha = _marker_commit("afk-probe", "probe", now)
        p = _git(["push", rem, f"{sha}:{ref}"], check=False)
        if p.returncode == 0:
            _git(["push", rem, "--delete", ref], check=False)
            return ns, rejection
        if "[remote rejected]" not in p.stderr:
            raise RuntimeError(f"probe push to {ref} failed: {p.stderr.strip()}")
        rejection = rejection or p.stderr.strip()
    raise RuntimeError(f"the remote rejects claim refs under both {wanted} and refs/heads: "
                       f"{rejection}")


def cmd_probe(a):
    """The bootstrap compatibility probe — two questions, both answered with the
    human present so a misfit is fixed here rather than mid-run (ADR-0009's tradition):

    1. **Claim namespace** — can we push under the configured `claim_namespace`
       (`refs/afk`)? Else fall back to branches (`refs/heads/afk-claim/*`) and flag
       that `on: push` CI will fire. The result's `config` is the canonical config
       with the namespace that actually works: the launcher holds THAT config from
       here on, so every later call inherits the namespace through `--config`.
    2. **Branch protection** (only when `gate.ci: local`, ADR-0012) — does the merge
       target REQUIRE status checks? Then `gh pr merge` is rejected however green the
       local gate is, so that combination is a hard `error` at bootstrap; an
       inconclusive read is a `warn`."""
    cfg = _cfg(a)
    ns, rejection = _usable_namespace(_remote(a), cfg["claim_namespace"], _now(a))
    cfg["claim_namespace"] = ns
    result = {"namespace": ns, "hidden": ns != "refs/heads", "ci_on_push": ns == "refs/heads",
              "blocked": rejection is not None, "config": cfg}
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
# observation: fingerprint / rebuild / no-pr / recovery                        #
# --------------------------------------------------------------------------- #

def _gather(a, cfg):
    """The ONE gatherer of the observable fleet inputs (ADR-0008): open issues,
    open PRs, and the claim/heartbeat ref scan. Both `rebuild` and the
    launcher's `fingerprint` gate read through here, so their views cannot
    drift. Issues leave here with `labels` as a list of names — the one shape
    afk_decide reads; the raw JSON lives and dies in this process."""
    issues = json.loads(_gh(["issue", "list", "--repo", a.repo, "--state", "open",
                             "--limit", "200", "--json", "number,title,labels,updatedAt"]).stdout)
    issues = [{**i, "labels": [lb["name"] for lb in i.get("labels") or []]} for i in issues]
    prs = json.loads(_gh(["pr", "list", "--repo", a.repo, "--state", "open",
                          "--json",
                          "number,headRefOid,updatedAt,statusCheckRollup,closingIssuesReferences"]).stdout)
    claims, heartbeats = _scan(_remote(a), cfg["claim_namespace"])
    return issues, prs, claims, heartbeats


def cmd_fingerprint(a):
    """The launcher's zero-LLM cycle gate (ADR-0007): digest what `rebuild`
    would observe (same gatherer, no blocked_by reads — updatedAt covers those)
    and return skip-or-tick. Only the digest + verdict ever reach a context."""
    cfg = _cfg(a)
    issues, prs, claims, _ = _gather(a, cfg)  # heartbeats: see afk_decide.fingerprint
    fp = afk_decide.fingerprint(issues, prs, claims)
    return {"fingerprint": fp,
            **afk_decide.fingerprint_gate(a.last, fp, a.skips, cfg["force_tick_after_skips"])}


def cmd_rebuild(a):
    """One read-only call → the tick's whole working set (ADR-0008). The
    per-issue blocked_by read is paid only by issues that pass every cheaper
    eligibility check. Strictly observation: nothing here writes a ref, a
    comment, or a PR."""
    cfg = _cfg(a)
    issues, prs, claims, heartbeats = _gather(a, cfg)
    blocked = {}
    for n in afk_decide.frontier_candidates(issues, prs, claims,
                                            cfg["ready_label"], cfg["epic_labels"]):
        v = _gh(["api", f"repos/{a.repo}/issues/{n}",
                 "--jq", ".issue_dependencies_summary.blocked_by"]).stdout.strip()
        blocked[n] = 0 if v in ("", "null") else int(v)
    return afk_decide.assemble_working_set(issues, prs, claims, heartbeats, blocked,
                                           a.instance, _now(a), cfg)


def cmd_no_pr(a):
    """Why does one of my claims have no PR — is its worker still coding, or did it
    finish without one? One call gathers everything the 5-way verdict needs: the
    worktree's git progress, the worker's `afk:verdict` marker on the issue, and
    the state of each issue that marker says it is blocked by. The tick supplies
    only what code cannot see — the orca terminal probe. Returns
    `afk_decide.classify_no_pr`'s verdict plus the signals it was decided from."""
    cfg = _cfg(a)
    progress = {}
    if a.worktree:
        if not os.path.isdir(a.worktree):
            raise ValueError(f"worktree not found: {a.worktree} (omit --worktree if there is none)")
        progress = _worktree_progress(a.worktree, cfg["base_branch"])
    verdict = afk_decide.latest_verdict(_issue_comments(a.repo, a.number))
    blocker_states = {n: _issue_state(a.repo, n) for n in verdict["blocked_by"]}
    return {"issue": a.number,
            **afk_decide.classify_no_pr(progress, a.terminal, a.terminal_idle_seconds, verdict,
                                        blocker_states, _now(a),
                                        cfg["worker_idle_grace_seconds"]),
            "progress": progress, "verdict": verdict}


def cmd_recovery(a):
    """Per DEAD claim: does recoverable progress exist, and where? → the tiered
    continuation verdict (ADR-0011). This is what makes an orphaned-claim
    reconciliation, a stale-claim reclaim, and a takeover *continue* the dead
    worker's work instead of restarting it.

    Two signals, both mechanics: (1) is a worktree for this issue still on THIS
    machine — asked of `orca worktree list` (soft: no orca → "no worktree", never
    an abort), overridable with `--worktree`/`--no-worktree`; (2) is the issue's
    branch ahead of base on the remote — the branch is recognised from
    `branch_pattern` (the claim ref records the issue, not the branch) and the
    compare is plain git. `afk_decide.select_recovery` then picks the tier.

    Both signals are always gathered, even when the worktree already settles the
    tier: a *pristine* worktree over a branch that carries pushed commits still has
    something to continue, and the honest prompt depends on knowing that."""
    cfg, rem = _cfg(a), _remote(a)
    base = cfg["base_branch"]

    # --- tier-1 signal: a worktree for this issue, still on this machine ---
    path, branch = a.worktree, a.branch
    if path is None and not a.no_worktree:
        hit = afk_decide.find_orca_worktree(_orca_worktree_rows(), a.number, a.repo)
        path, branch = hit["path"], branch or hit["branch"]
    worktree = {"present": False, "path": path, "commits_ahead": None,
                "dirty": False, "last_commit_ts": None, "worktree_mtime_ts": None}
    if path and os.path.isdir(path):
        worktree = {"present": True, "path": path, **_worktree_progress(path, base)}

    # --- tier-2 signal: the branch the dead worker pushed ---
    candidates = [] if branch else afk_decide.branch_candidates(
        _remote_heads(rem), cfg["branch_pattern"], a.number)
    measured = {b: _branch_ahead(rem, b, base, a.number)
                for b in ([branch] if branch else candidates)}
    branch = branch or afk_decide.furthest_ahead({b: n for b, (n, _) in measured.items()})
    ahead, detail = measured.get(branch, (None, "no branch on the remote matches this issue"))
    branch_sig = {"name": branch, "commits_ahead": ahead, "candidates": candidates,
                  "detail": detail}

    return {"issue": a.number, "base": base, "worktree": worktree, "branch": branch_sig,
            **afk_decide.select_recovery(worktree, branch_sig)}


# --------------------------------------------------------------------------- #
# act: gate-run / status / next-attempt / pace                                 #
# --------------------------------------------------------------------------- #

def cmd_gate_run(a):
    """Run the configured local gate in a worktree → `{status, excerpt}` — the
    completion gate itself in `gate.ci: local` mode, re-run at MERGE time against
    the exact tree that lands (ADR-0012). Deliberately the same compact shape as the
    ephemeral CI-log sub-read `required` mode uses: the tick gets a verdict and a
    bounded excerpt, never a raw log in its context.

    Mechanics all the way down (ADR-0004): *what* to run is config, *where* is the
    branch's worktree, and *whether it passed* is an exit code — no judgment. A run
    that times out is red, never green-by-default."""
    cmd = _cfg(a)["gate"]["local_command"]
    if not (cmd or "").strip():
        raise ValueError("no gate command to run: pass --command, or set gate.local_command "
                         "(required whenever gate.ci is 'local')")
    if not os.path.isdir(a.worktree):
        raise ValueError(f"worktree not found: {a.worktree}")

    def _text(s):
        return s.decode("utf-8", "replace") if isinstance(s, bytes) else (s or "")

    timed_out, rc = False, 0
    try:
        p = subprocess.run(cmd, shell=True, cwd=a.worktree, capture_output=True,
                           text=True, timeout=a.timeout, env=_GIT_ENV)
        out, rc = _text(p.stdout) + _text(p.stderr), p.returncode
    except subprocess.TimeoutExpired as e:
        out = _text(e.stdout) + _text(e.stderr) + f"\n[afk] gate timed out after {a.timeout}s"
        timed_out, rc = True, 124
    return {**afk_decide.gate_verdict(rc, out, a.excerpt_lines, timed_out),
            "command": cmd, "worktree": a.worktree}


def cmd_status(a):
    """Upsert the human-facing progress status board comment (idempotent, ADR-0006).
    Renders the body from the given phase (pure), then find-or-create by marker
    and write ONLY when the body changed — so re-entrant/disposable ticks and
    retry re-dispatches never spam the issue."""
    cfg = _cfg(a)
    body = afk_decide.render_status_board(
        {"phase": a.phase, "instance": a.instance, "pr": a.pr, "attempt": a.attempt,
         "retry_max": cfg["retry"], "ci": cfg["gate"]["ci"]})
    comments = f"repos/{a.repo}/issues/{a.number}/comments"
    board = next((c for c in _issue_comments(a.repo, a.number)
                  if afk_decide.STATUS_MARKER in (c["body"] or "")), None)
    if board is None:
        p = _gh(["api", "--method", "POST", comments, "-f", f"body={body}"])
        return {"action": "created", "issue": a.number, "comment_id": json.loads(p.stdout).get("id")}
    if board["body"].strip() == body.strip():
        return {"action": "unchanged", "issue": a.number, "comment_id": board["id"]}
    _gh(["api", "--method", "PATCH", f"repos/{a.repo}/issues/comments/{board['id']}",
         "-f", f"body={body}"])
    return {"action": "updated", "issue": a.number, "comment_id": board["id"]}


def cmd_next_attempt(a):
    return afk_decide.next_attempt(a.labels, _cfg(a)["retry"])


def cmd_pace(a):
    return {"seconds": afk_decide.pace(json.loads(a.summary), _cfg(a))}


# --------------------------------------------------------------------------- #
# arg wiring                                                                  #
# --------------------------------------------------------------------------- #

def _csv(s):
    return [x for x in s.split(",") if x]


def build_parser():
    """The whole CLI. `.subcommands` maps each subcommand name to its parser —
    the interface the docs are checked against (test_afk_cli.py)."""
    ap = argparse.ArgumentParser(prog="afk", description="afk-fleet deterministic tool")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ap.subcommands = sub.choices

    def command(name, fn, help, remote=None):
        """One subcommand. Every one takes --config and --now; `remote` adds the
        repo handle: "refs" for git-ref ops (--repo optional), "gh" when gh needs
        it (--repo required)."""
        p = sub.add_parser(name, help=help)
        p.set_defaults(fn=fn)
        p.add_argument("--config", default=None,
                       help="canonical config JSON from `afk config` / `afk probe` (override "
                            "flags win; omitted keys fall back to the defaults table — ADR-0009)")
        p.add_argument("--now", type=int, default=None, help="epoch override (tests)")
        if remote:
            p.add_argument("--repo", default=None, required=(remote == "gh"),
                           help="owner/name of the target repo — the one repo handle: git-ref "
                                "ops push/fetch to its GitHub URL, gh ops read it directly")
            p.add_argument("--remote", default="origin",
                           help="git remote for ref ops when --repo is not given")
            p.add_argument("--ns", default=None,
                           help="claim namespace override (default: config claim_namespace)")
        return p

    def mine(p):
        p.add_argument("--instance", required=True, help="my fleet instance id")

    def stamp(p):
        mine(p)
        p.add_argument("--host", default=socket.gethostname())

    ttl_help = "claim_lease_ttl_seconds override"

    # --- bootstrap ---
    p = command("config", cmd_config, "parse + validate the repo config file → canonical JSON")
    p.add_argument("--file", default=None, help="path to the target repo's docs/agents/afk-fleet.md")
    p.add_argument("--defaults", action="store_true", help="print the pure defaults table")

    p = command("probe", cmd_probe, remote="refs",
                help="bootstrap probe: the usable claim namespace (folded into the returned "
                     "config), and target-branch protection when gate.ci is local")
    p.add_argument("--target", default=None, help="merge.target override")

    p = command("worker-command", cmd_worker_command,
                "settle the command workers are started with: ask-or-not + candidates, "
                "or --check a human's answer")
    p.add_argument("--check", default=None,
                   help="a candidate command: resolve its first word in the login shell "
                        "and report whether it runs (and looks unattended)")

    # --- claim refs ---
    p = command("scan", cmd_scan, "debug: read all claim + heartbeat refs", remote="refs")

    p = command("classify-claims", cmd_classify_claims, remote="refs",
                help="debug: partition claims into mine/peer_live/stale (rebuild does this)")
    mine(p)
    p.add_argument("--ttl", type=int, default=None, help=ttl_help)

    p = command("claim", cmd_claim, "atomically create a claim ref → {won}", remote="refs")
    p.add_argument("number", type=int)
    stamp(p)

    p = command("reclaim", cmd_reclaim, "force-with-lease take of a stale claim → {won}",
                remote="refs")
    p.add_argument("number", type=int)
    stamp(p)
    p.add_argument("--expect-sha", required=True, help="the sha you read; the take fails if it moved")

    p = command("takeover", cmd_takeover, remote="refs",
                help="list the fleet instances GitHub remembers, or force-take a dead "
                     "one's claims (skips the staleness gate)")
    stamp(p)
    p.add_argument("--list", action="store_true",
                   help="show every discoverable instance: heartbeat age, host, claim count")
    p.add_argument("--from", dest="source", default=None,
                   help="the dead instance whose claims to take")
    p.add_argument("--yes", action="store_true",
                   help="confirm a takeover of an instance whose heartbeat is still FRESH "
                        "(it looks alive; you are asserting you know it is dead)")
    p.add_argument("--ttl", type=int, default=None, help=ttl_help)

    p = command("release", cmd_release, "delete a claim ref (idempotent)", remote="refs")
    p.add_argument("number", type=int)

    p = command("heartbeat", cmd_heartbeat, "refresh my heartbeat if due", remote="refs")
    mine(p)
    p.add_argument("--ttl", type=int, default=None, help=ttl_help)

    # --- observation ---
    p = command("fingerprint", cmd_fingerprint, remote="gh",
                help="digest observable state; skip-or-tick verdict for the launcher")
    p.add_argument("--last", default="", help="the previous cycle's digest (empty on the first cycle)")
    p.add_argument("--skips", type=int, default=0, help="consecutive skipped cycles so far")
    p.add_argument("--force-after", type=int, default=None,
                   help="force_tick_after_skips override")

    p = command("rebuild", cmd_rebuild, "gather + assemble the tick's working set (read-only)",
                remote="gh")
    mine(p)
    p.add_argument("--ttl", type=int, default=None, help=ttl_help)
    p.add_argument("--ready-label", default=None, help="ready_label override")
    p.add_argument("--epic-labels", type=_csv, default=None, help="epic_labels override (csv)")
    p.add_argument("--ci", default=None, choices=list(afk_decide.GATE_CI_MODES),
                   help="gate.ci override")

    p = command("no-pr", cmd_no_pr, remote="gh",
                help="why one of my claims has no PR → coding / idle_done / idle_blocked / "
                     "idle_failed / dead, gathered and decided in one call")
    p.add_argument("--issue", dest="number", type=int, required=True)
    p.add_argument("--terminal", choices=["busy", "idle", "none"], required=True,
                   help="the orca probe: busy | idle | none (no live worker)")
    p.add_argument("--terminal-idle-seconds", type=int, default=None,
                   help="seconds since the terminal last showed activity, if the probe says")
    p.add_argument("--worktree", default=None,
                   help="the worker's worktree (omit only if none exists)")
    p.add_argument("--base", default=None, help="base_branch override")
    p.add_argument("--grace", type=int, default=None, help="worker_idle_grace_seconds override")

    p = command("recovery", cmd_recovery, remote="refs",
                help="does a dead claim have recoverable progress, and where? → the "
                     "tiered continuation verdict")
    p.add_argument("--issue", dest="number", type=int, required=True)
    p.add_argument("--base", default=None, help="base_branch override")
    p.add_argument("--branch", default=None,
                   help="the issue's work branch, when already known (skips discovery)")
    p.add_argument("--worktree", default=None,
                   help="path to the issue's worktree, when already known (skips the orca read)")
    p.add_argument("--no-worktree", action="store_true",
                   help="assert no local worktree survives (skips the orca read)")

    # --- act ---
    p = command("gate-run", cmd_gate_run,
                "run the configured local gate in a worktree → {status, excerpt}")
    p.add_argument("--worktree", required=True, help="worktree to run the gate in")
    p.add_argument("--command", default=None, help="gate.local_command override")
    p.add_argument("--timeout", type=int, default=1800,
                   help="seconds before the run is called red (default 1800)")
    p.add_argument("--excerpt-lines", type=int, default=afk_decide.GATE_EXCERPT_LINES,
                   help="how many trailing log lines the excerpt keeps")

    p = command("status", cmd_status, remote="gh",
                help="upsert the human-facing progress status board comment (idempotent)")
    p.add_argument("number", type=int)
    p.add_argument("--phase", required=True, choices=list(afk_decide.STATUS_PHASES),
                   help="the lifecycle phase — a `mine` row's board_phase, or merged / escalated")
    p.add_argument("--instance", default=None, help="owning fleet instance id (shown in the header)")
    p.add_argument("--pr", type=int, default=None, help="the PR number, once one is open")
    p.add_argument("--attempt", type=int, default=0, help="current attempt n (shown for ci_failed)")

    p = command("next-attempt", cmd_next_attempt, "retry-or-escalate from afk-attempt labels")
    p.add_argument("--labels", type=_csv, required=True, help="the issue's label names (csv)")
    p.add_argument("--retry", type=int, default=None, help="retry override")

    p = command("pace", cmd_pace, "next launcher sleep in seconds")
    p.add_argument("--summary", required=True, help="the last tick summary JSON")

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
