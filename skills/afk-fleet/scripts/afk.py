#!/usr/bin/env python3
"""
afk.py — the afk-fleet tool: deterministic muscle the LLM tick calls.

The tick (an LLM) orchestrates and judges; when it needs a *deterministic* action
it shells out to one of these subcommands and reads back JSON (ADR-0004, choice 甲).
Every subcommand prints one JSON object to stdout. Exit 0 = ran (a lost claim race
is `{"won": false}`, still exit 0); exit 3 = an operational/git error.

Two layers:
  - pure verdicts (`config`, `frontier`, `pace`, `next-attempt`, `subclassify`,
    `classify-no-pr`, and the decision halves of `rebuild`, `classify-claims`,
    `recovery`, `takeover`, `gate-run`, `probe`, `verdict`, `worker-command`, and
    `fingerprint`) come from afk_decide.py — no I/O, fixture-tested; time is always
    injected, never read here. Config resolution is one order everywhere:
    flag → `--config` JSON → CONFIG_DEFAULTS (ADR-0009).
  - effectful ops (`scan`, `claim`, `reclaim`, `release`, `heartbeat`, `probe`,
    `takeover`, `worker-status`, `recovery`, `gate-run`, `verdict`,
    `worker-command`) drive git refs / worktree git / gh / orca / the login shell.
    The ref ops among them (`scan`/`claim`/`reclaim`/`release`/`heartbeat`/`probe`/
    `takeover`/`recovery`) are covered by `test_afk_refs.py` — a bare local repo
    standing in for GitHub, so the races are asserted offline, without gh.

Invoked as:  python3 <skill>/scripts/afk.py <subcommand> [flags]
"""
import argparse
import json
import os
import socket
import subprocess
import sys
import time

import afk_decide

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
    repo = getattr(a, "repo", None)
    return f"https://github.com/{repo}.git" if repo else a.remote


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


def _marker_commit(message):
    """A parentless commit on the empty tree, carrying `message`. Its sha is what
    we push to a ref; it drags none of the repo history along."""
    empty_tree = _git(["hash-object", "-t", "tree", "/dev/null"]).stdout.strip()
    return _git(["commit-tree", empty_tree, "-m", message]).stdout.strip()


def _marker_text(kind, instance, ts, host=None):
    parts = [kind, f"instance={instance}", f"ts={int(ts)}"]
    if host:
        parts.insert(2, f"host={host}")
    return " ".join(parts)


def _parse_marker(subject):
    """`afk-claim instance=abc host=mac ts=123` → {'instance':'abc','host':'mac','ts':123}."""
    out = {}
    for tok in (subject or "").split():
        if "=" in tok:
            k, v = tok.split("=", 1)
            out[k] = int(v) if (k == "ts" and v.isdigit()) else v
    return out


def _read_marker(remote, refname):
    """Fetch one ref by name and return its parsed marker, or None if absent."""
    p = _git(["fetch", remote, refname], check=False)
    if p.returncode != 0:
        return None
    subject = _git(["log", "-1", "--format=%s", "FETCH_HEAD"], check=False).stdout.strip()
    return _parse_marker(subject)


def _orca_worktree_rows(injected=None):
    """The `result.worktrees` rows of `orca worktree list --json`, or [] when orca
    can't be reached. SOFT by design: the worktree signal is one input to a tiered
    recovery whose last tier needs no orca at all, so a machine without orca (or a
    momentarily unhappy one) must degrade to "no local worktree", never abort a
    recovery. `injected` accepts the whole doc, its `result`, or the bare rows."""
    doc = injected
    if doc is None:
        try:
            p = subprocess.run(["orca", "worktree", "list", "--json"],
                               capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            return []
        if p.returncode != 0:
            return []
        try:
            doc = json.loads(p.stdout)
        except json.JSONDecodeError:
            return []
    if isinstance(doc, list):
        return doc
    if not isinstance(doc, dict):
        return []
    inner = doc.get("result") if isinstance(doc.get("result"), dict) else doc
    rows = inner.get("worktrees")
    return rows if isinstance(rows, list) else []


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


def _issue_num_from_ref(refname):
    tail = refname.rstrip("/").rsplit("/", 1)[-1]
    return int(tail) if tail.isdigit() else None


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
# effectful: scan / claim / reclaim / release / heartbeat / probe             #
# --------------------------------------------------------------------------- #

def _scan(remote, base):
    """Mirror the remote claim+heartbeat refs into a disposable local namespace and
    read every marker. Returns (claims, heartbeats)."""
    claim_ns, hb_ns = _ns_paths(base)
    _git(["fetch", "--prune", remote,
          f"+{claim_ns}/*:{_LOCAL_SCAN}/claim/*",
          f"+{hb_ns}/*:{_LOCAL_SCAN}/heartbeat/*"], check=False)

    claims = []
    rows = _git(["for-each-ref", "--format=%(refname) %(objectname)",
                 f"{_LOCAL_SCAN}/claim"], check=False).stdout.splitlines()
    for row in rows:
        refname, sha = (row.split(" ", 1) + [""])[:2]
        n = _issue_num_from_ref(refname)
        if n is None:
            continue
        subj = _git(["log", "-1", "--format=%s", sha], check=False).stdout.strip()
        m = _parse_marker(subj)
        claims.append({"number": n, "instance": m.get("instance"),
                       "host": m.get("host"), "ts": m.get("ts"), "sha": sha})

    heartbeats = {}
    rows = _git(["for-each-ref", "--format=%(refname) %(objectname)",
                 f"{_LOCAL_SCAN}/heartbeat"], check=False).stdout.splitlines()
    for row in rows:
        refname, sha = (row.split(" ", 1) + [""])[:2]
        inst = refname.rstrip("/").rsplit("/", 1)[-1]
        subj = _git(["log", "-1", "--format=%s", sha], check=False).stdout.strip()
        ts = _parse_marker(subj).get("ts")
        if ts is not None:
            heartbeats[inst] = ts
    return claims, heartbeats


def cmd_scan(a):
    claims, heartbeats = _scan(_remote(a), a.ns)
    return {"claims": claims, "heartbeats": heartbeats}


def cmd_classify_claims(a):
    if a.claims_json is not None:              # test-injected — no git
        claims = json.loads(a.claims_json)
        heartbeats = json.loads(a.heartbeats_json or "{}")
    else:
        claims, heartbeats = _scan(_remote(a), a.ns)
    now = a.now if a.now is not None else int(time.time())
    ttl = a.ttl if a.ttl is not None else _cfg(a)["claim_lease_ttl_seconds"]
    result = afk_decide.classify_claims(claims, heartbeats, a.instance, now, ttl)
    result["now"] = now
    return result


def cmd_claim(a):
    claim_ns, _ = _ns_paths(a.ns)
    ref = f"{claim_ns}/{a.number}"
    rem = _remote(a)
    now = a.now if a.now is not None else int(time.time())
    sha = _marker_commit(_marker_text("afk-claim", a.instance, now, host=a.host))
    # Create-only: the server rejects a ref that already exists → that is the CAS.
    p = _git(["push", rem, f"{sha}:{ref}"], check=False)
    if p.returncode == 0:
        return {"won": True, "issue": a.number, "ref": ref, "sha": sha, "instance": a.instance}
    owner = _read_marker(rem, ref)  # who beat us
    return {"won": False, "issue": a.number, "ref": ref,
            "owner": owner, "detail": p.stderr.strip()}


def _force_take(rem, ns, number, expect_sha, instance, now, host):
    """The atomic re-stamp of ONE existing claim ref to `instance`: rejected unless
    the ref still points at the sha we read. The single mechanism behind both an
    unattended stale reclaim and a human-authorized takeover — they differ only in
    what gates the *choice* of claim (an expired lease vs a present human), never
    in the push, so a takeover is exactly as safe against a live peer."""
    ref = f"{ns}/{number}"
    sha = _marker_commit(_marker_text("afk-claim", instance, now, host=host))
    p = _git(["push", rem, f"--force-with-lease={ref}:{expect_sha}", f"{sha}:{ref}"], check=False)
    if p.returncode == 0:
        return {"won": True, "issue": number, "ref": ref, "sha": sha, "instance": instance}
    return {"won": False, "issue": number, "ref": ref, "detail": p.stderr.strip()}


def cmd_reclaim(a):
    claim_ns, _ = _ns_paths(a.ns)
    now = a.now if a.now is not None else int(time.time())
    return _force_take(_remote(a), claim_ns, a.number, a.expect_sha, a.instance, now, a.host)


def cmd_takeover(a):
    """The human-authorized, lease-skipping reclaim of a DEAD fleet instance's
    claims (ADR-0011). Two shapes:

      --list                  every instance GitHub still remembers — from the claim
                              markers (`instance=<id> host=<host>`) and the heartbeat
                              refs — with heartbeat age, host and claim count. A
                              launcher forgets its own id when it dies; the repo does
                              not, so the human need not have written it down.
      --instance <dead-id>    force-take that instance's claims with the SAME atomic
                              --force-with-lease push a stale reclaim uses, only
                              skipping the staleness gate, re-stamping each with
                              `--as <my-id>`. A target whose heartbeat is still fresh
                              returns `confirm` and takes nothing until `--yes`.

    NOTE the flag asymmetry, unique to this subcommand: `--instance` is the
    instance being taken FROM (the dead one), `--as` is mine. Takeover neither
    reads nor increments `afk-attempt/<n>`: it answers "did the fleet die?", not
    "is this work failing?". What each taken claim then *does* is continuation
    (`afk recovery`), not a fresh re-dispatch."""
    claim_ns, _ = _ns_paths(a.ns)
    rem = _remote(a)
    claims, heartbeats = _scan(rem, a.ns)
    now = a.now if a.now is not None else int(time.time())
    ttl = a.ttl if a.ttl is not None else _cfg(a)["claim_lease_ttl_seconds"]

    if a.list:
        return {"instances": afk_decide.group_instances(claims, heartbeats, a.me, now, ttl),
                "me": a.me, "now": now, "ttl": ttl}
    if not a.instance:
        raise ValueError("--instance <dead instance id> is required unless --list")
    if not a.me:
        raise ValueError("--as <my instance id> is required: every taken claim is re-stamped with it")

    plan = afk_decide.plan_takeover(claims, heartbeats, a.instance, a.me, now, ttl, a.yes)
    if plan["action"] != "take":
        return {**plan, "taken": [], "lost": []}

    taken, lost = [], []
    for c in plan["claims"]:
        r = _force_take(rem, claim_ns, c["number"], c["sha"], a.me, now, a.host)
        (taken if r["won"] else lost).append(r)
    return {**plan, "action": "taken", "as": a.me,
            "taken": [t["issue"] for t in taken], "lost": lost,
            "detail": f"took {len(taken)}/{len(plan['claims'])} claim(s) from {a.instance}"
                      + ("; a lost one means that fleet is not dead — its ref moved under us"
                         if lost else "")}


def cmd_release(a):
    claim_ns, _ = _ns_paths(a.ns)
    ref = f"{claim_ns}/{a.number}"
    p = _git(["push", _remote(a), "--delete", ref], check=False)
    # Already gone counts as released — idempotent cleanup.
    ok = p.returncode == 0 or "remote ref does not exist" in p.stderr or "deleted" in p.stderr
    return {"released": bool(ok), "issue": a.number, "ref": ref, "detail": p.stderr.strip()}


def cmd_heartbeat(a):
    _, hb_ns = _ns_paths(a.ns)
    ref = f"{hb_ns}/{a.instance}"
    rem = _remote(a)
    now = a.now if a.now is not None else int(time.time())
    ttl = a.ttl if a.ttl is not None else _cfg(a)["claim_lease_ttl_seconds"]
    cur = _read_marker(rem, ref)
    last = cur.get("ts") if cur else None
    if not afk_decide.heartbeat_due(last, now, ttl):
        return {"refreshed": False, "reason": "not due", "ts": last, "ref": ref}
    sha = _marker_commit(_marker_text("afk-heartbeat", a.instance, now))
    p = _git(["push", rem, "--force", f"{sha}:{ref}"], check=False)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip())
    return {"refreshed": True, "ts": now, "ref": ref}


def _cfg(a):
    """The effective config for a subcommand: `--config` JSON (canonical or
    partial — resolved through CONFIG_DEFAULTS either way; ADR-0009), else pure
    defaults. Individual flags override on top: flag → config → defaults."""
    partial = json.loads(a.config) if getattr(a, "config", None) else {}
    return afk_decide.resolve_config(partial)


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


def _gather(a):
    """The ONE gatherer of the observable fleet inputs (ADR-0008): open issues,
    open PRs, and the claim/heartbeat ref scan. Both `rebuild` and the
    launcher's `fingerprint` gate read through here, so their views cannot
    drift. Raw JSON lives and dies in this process; fields beyond what the
    digest canonicalizes are harmless — afk_decide.fingerprint ignores them."""
    issues = json.loads(_gh(["issue", "list", "--repo", a.repo, "--state", "open",
                             "--limit", "200", "--json", "number,title,labels,updatedAt"]).stdout)
    prs = json.loads(_gh(["pr", "list", "--repo", a.repo, "--state", "open",
                          "--json",
                          "number,headRefOid,updatedAt,statusCheckRollup,closingIssuesReferences"]).stdout)
    claims, heartbeats = _scan(_remote(a), a.ns)
    return issues, prs, claims, heartbeats


def cmd_fingerprint(a):
    """The launcher's zero-LLM cycle gate (ADR-0007): digest what `rebuild`
    would observe (same gatherer, no blocked_by reads — updatedAt covers those)
    and return skip-or-tick. Only the digest + verdict ever reach a context."""
    if a.state_json is not None:               # test-injected — no gh/git
        st = json.loads(a.state_json)
        issues, prs, claims = st.get("issues", []), st.get("prs", []), st.get("claims", [])
    else:
        if not a.repo:
            raise ValueError("--repo owner/name is required unless --state-json")
        issues, prs, claims, _ = _gather(a)  # heartbeats deliberately
        # unused — see afk_decide.fingerprint
    fp = afk_decide.fingerprint(issues, prs, claims)
    force_after = a.force_after if a.force_after is not None else _cfg(a)["force_tick_after_skips"]
    verdict = afk_decide.fingerprint_gate(a.last, fp, a.skips, force_after)
    return {"fingerprint": fp, **verdict}


def cmd_rebuild(a):
    """One read-only call → the tick's whole working set (ADR-0008). Gather,
    run a provisional frontier with blockers assumed 0, fetch open-blocker
    counts for just those candidates, then assemble. The per-issue blocked_by
    read is paid only by issues that pass every cheaper eligibility check.
    Strictly observation: nothing here writes a ref, a comment, or a PR."""
    cfg = _cfg(a)
    ready = a.ready_label or cfg["ready_label"]
    epic = a.epic_labels.split(",") if a.epic_labels else cfg["epic_labels"]
    ttl = a.ttl if a.ttl is not None else cfg["claim_lease_ttl_seconds"]
    if a.state_json is not None:               # test-injected — no gh/git
        st = json.loads(a.state_json)
        issues, prs = st.get("issues", []), st.get("prs", [])
        claims, heartbeats = st.get("claims", []), st.get("heartbeats", {})
        blocked = {int(k): v for k, v in (st.get("blocked_by") or {}).items()}
    else:
        if not a.repo:
            raise ValueError("--repo owner/name is required unless --state-json")
        issues, prs, claims, heartbeats = _gather(a)
        claimed = {c.get("number") for c in claims}
        pr_nums = {r.get("number") for p in prs
                   for r in (p.get("closingIssuesReferences") or [])}
        prov = afk_decide.select_frontier(
            [{**i, "claimed": i.get("number") in claimed,
              "has_open_pr": i.get("number") in pr_nums, "open_blockers": 0}
             for i in issues], ready, epic)
        blocked = {}
        for n in prov["dispatch"]:
            v = _gh(["api", f"repos/{a.repo}/issues/{n}",
                     "--jq", ".issue_dependencies_summary.blocked_by"]).stdout.strip()
            blocked[n] = 0 if v in ("", "null") else int(v)
    now = a.now if a.now is not None else int(time.time())
    return afk_decide.assemble_working_set(issues, prs, claims, heartbeats, blocked,
                                           a.instance, now, ttl, ready, epic,
                                           a.ci or cfg["gate"]["ci"])


def _find_status_comment(repo, number):
    """Find the fleet's existing status-board comment by its marker.
    Returns (comment_id, body), or (None, None) if there isn't one yet."""
    jq = f'.[] | select((.body // "") | contains("{afk_decide.STATUS_MARKER}")) | {{id, body}}'
    p = _gh(["api", "--paginate", f"repos/{repo}/issues/{number}/comments", "--jq", jq], check=False)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip())
    for line in p.stdout.splitlines():
        line = line.strip()
        if line:
            obj = json.loads(line)
            return obj.get("id"), obj.get("body", "")
    return None, None


def cmd_status(a):
    """Upsert the human-facing progress status board comment (idempotent, ADR-0006).
    Renders the body from the injected discrete state (pure), then find-or-create by
    marker and write ONLY when the body changed — so re-entrant/disposable ticks and
    retry re-dispatches never spam the issue. `--print` renders without touching gh."""
    state = json.loads(a.state)
    body = afk_decide.render_status_board(state)
    if a.print_only:
        return {"action": "render-only", "issue": a.number, "body": body}
    if not a.repo:
        raise ValueError("--repo owner/name is required unless --print")
    cid, cur = _find_status_comment(a.repo, a.number)
    if cid is None:
        p = _gh(["api", "--method", "POST", f"repos/{a.repo}/issues/{a.number}/comments",
                 "-f", f"body={body}"], check=True)
        return {"action": "created", "issue": a.number, "comment_id": json.loads(p.stdout).get("id")}
    if (cur or "").strip() == body.strip():
        return {"action": "unchanged", "issue": a.number, "comment_id": cid}
    _gh(["api", "--method", "PATCH", f"repos/{a.repo}/issues/comments/{cid}",
         "-f", f"body={body}"], check=True)
    return {"action": "updated", "issue": a.number, "comment_id": cid}


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
    Anthropic (ADR-0010), and so a qoderclicn launcher dispatches qoderclicn
    workers (ADR-0014).

    Bare: detect the runtime (qoderclicn or claude) from the launcher's own
    environment. qoderclicn is always stock (no wrapping, no custom provider) —
    return its default and never ask. Claude: read ANTHROPIC_BASE_URL and report
    `stock` (nothing to ask) or `ask` — with the Claude-starting aliases found in
    the user's login shell, so the human picks rather than types. `--check
    "<cmd>"` resolves the human's answer's first word in that same shell and
    reports whether it runs at all, plus whether an unattended flag is visible.

    The command is OPAQUE: never parsed, never composed, never appended to. That is
    what keeps every credential inside whatever wrapper the human already trusts —
    the fleet copies no env, writes no file, and puts no key on any command line."""
    runtime = afk_decide.detect_runtime(os.environ)
    if runtime == "qoderclicn":
        return {"status": "stock", "command": afk_decide.WORKER_COMMAND_DEFAULT_QODERCN,
                "base_url": None, "first_word": "qoderclicn", "yolo": True,
                "detail": "", "runtime": runtime}
    base = a.base_url if a.base_url is not None else os.environ.get("ANTHROPIC_BASE_URL")
    if a.check:
        fw = afk_decide.first_word(a.check)
        resolved = _login_shell(f"type -- {fw}") if fw else ""
        result = afk_decide.resolve_worker_command(base, a.check, resolved)
        result["runtime"] = runtime
        return result
    result = afk_decide.resolve_worker_command(base)
    result["runtime"] = runtime
    if result["status"] == "ask":
        aliases = afk_decide.parse_aliases(_login_shell("alias"))
        result["candidates"] = afk_decide.launch_candidates(aliases)
    return result


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


def cmd_probe(a):
    """The bootstrap compatibility probe — two questions, both answered with the
    human present so a misfit is fixed here rather than mid-run (ADR-0009's tradition):

    1. **Claim namespace** — can we push under `refs/afk/*`? Else fall back to
       branches (`refs/heads/afk-claim/*`) and flag that `on: push` CI will fire.
    2. **Branch protection** (only when `gate.ci: local`, ADR-0012) — does the merge
       target REQUIRE status checks? Then `gh pr merge` is rejected however green the
       local gate is, so that combination is a hard `error` at bootstrap; an
       inconclusive read is a `warn`."""
    ref = f"{a.ns}/probe"
    rem = _remote(a)
    sha = _marker_commit(_marker_text("afk-probe", "probe", a.now if a.now is not None else int(time.time())))
    p = _git(["push", rem, f"{sha}:{ref}"], check=False)
    if p.returncode == 0:
        _git(["push", rem, "--delete", ref], check=False)
        hidden = a.ns != "refs/heads"          # report the namespace actually probed
        result = {"namespace": a.ns, "hidden": hidden, "ci_on_push": not hidden, "blocked": False}
    else:
        result = {"namespace": "refs/heads", "hidden": False, "ci_on_push": True,
                  "blocked": True, "detail": p.stderr.strip()}

    cfg = _cfg(a)
    ci_mode = cfg["gate"]["ci"]
    if ci_mode == "local":
        target = a.target or cfg["merge"]["target"]
        if not a.repo:
            result["protection"] = {"branch": target, "verdict": "warn", "required_checks": [],
                                    "detail": "gate.ci is 'local' but --repo was not given, so "
                                              "branch protection could not be checked"}
        else:
            prot, unavailable = _branch_protection(a.repo, target)
            result["protection"] = {"branch": target,
                                    **afk_decide.protection_verdict(ci_mode, prot, unavailable)}
    return result


def cmd_gate_run(a):
    """Run the configured local gate in a worktree → `{status, excerpt}` — the
    completion gate itself in `gate.ci: local` mode, re-run at MERGE time against
    the exact tree that lands (ADR-0012). Deliberately the same compact shape as the
    ephemeral CI-log sub-read `required` mode uses: the tick gets a verdict and a
    bounded excerpt, never a raw log in its context.

    Mechanics all the way down (ADR-0004): *what* to run is config, *where* is the
    branch's worktree, and *whether it passed* is an exit code — no judgment. A run
    that times out is red, never green-by-default."""
    cfg = _cfg(a)
    cmd = a.command or cfg["gate"]["local_command"]
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


def cmd_worker_status(a):
    """The decisive PROGRESS signal for a `no_pr` claim, read straight from the
    worker's worktree with git only (no gh, no orca) — independent of terminal
    chrome, so it disambiguates a worker still coding from one that finished and
    went idle. Returns {commits_ahead, dirty, last_commit_ts, worktree_mtime_ts};
    the tick feeds it to `afk classify-no-pr`."""
    wt = a.worktree
    if not os.path.isdir(wt):
        raise ValueError(f"worktree not found: {wt}")
    base = a.base or _cfg(a)["base_branch"]
    return _worktree_progress(wt, base)


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
    cfg = _cfg(a)
    base = a.base or cfg["base_branch"]
    rem = _remote(a)

    # --- tier-1 signal: a worktree for this issue, still on this machine ---
    path, branch = a.worktree, a.branch
    if path is None and not a.no_worktree:
        hit = afk_decide.find_orca_worktree(_orca_worktree_rows(
            json.loads(a.orca_json) if a.orca_json is not None else None), a.number, a.repo)
        path = hit["path"]
        branch = branch or hit["branch"]
    worktree = {"present": False, "path": path, "commits_ahead": None,
                "dirty": False, "last_commit_ts": None, "worktree_mtime_ts": None}
    if path and os.path.isdir(path):
        worktree = {"present": True, "path": path, **_worktree_progress(path, base)}

    # --- tier-2 signal: the branch the dead worker pushed ---
    candidates = []
    if not branch:
        candidates = afk_decide.branch_candidates(_remote_heads(rem),
                                                  cfg["branch_pattern"], a.number)
    ahead, detail = None, ""
    if branch:
        ahead, detail = _branch_ahead(rem, branch, base, a.number)
    elif candidates:
        # Several branches can match one issue (an earlier attempt left one behind):
        # take the one furthest ahead of base, ties by name (candidates are sorted).
        for cand in candidates:
            n, d = _branch_ahead(rem, cand, base, a.number)
            if branch is None or (n or 0) > (ahead or 0):
                branch, ahead, detail = cand, n, d
    else:
        detail = "no branch on the remote matches this issue"
    branch_sig = {"name": branch, "commits_ahead": ahead, "candidates": candidates,
                  "detail": detail}

    return {"issue": a.number, "base": base, "worktree": worktree, "branch": branch_sig,
            **afk_decide.select_recovery(worktree, branch_sig)}


def cmd_verdict(a):
    """Fetch an issue's comments and return the LATEST parsed afk:verdict marker
    (afk_decide.latest_verdict) — the worker's machine-readable reason for opening
    no PR, the single source of truth the `no_pr` classification routes on. gh
    fetch here, deterministic parse in afk_decide. `--comments-json` injects
    comments to skip gh (tests)."""
    if a.comments_json is not None:                # test-injected — no gh
        comments = json.loads(a.comments_json)
    else:
        if not a.repo:
            raise ValueError("--repo owner/name is required unless --comments-json")
        jq = '.[] | {body: .body, comment_url: .html_url, created_at: .created_at}'
        p = _gh(["api", "--paginate", f"repos/{a.repo}/issues/{a.number}/comments",
                 "--jq", jq], check=True)
        comments = [json.loads(ln) for ln in p.stdout.splitlines() if ln.strip()]
    return afk_decide.latest_verdict(comments)


# --------------------------------------------------------------------------- #
# pure passthroughs                                                           #
# --------------------------------------------------------------------------- #

def _issues_in(a):
    raw = sys.stdin.read() if a.stdin else open(a.fixture).read()
    issues = json.loads(raw)
    if not isinstance(issues, list):
        raise ValueError("expected a JSON array of issues")
    return issues


def cmd_frontier(a):
    issues = _issues_in(a)
    cfg = _cfg(a)
    ready = a.ready_label or cfg["ready_label"]
    epic = a.epic_labels.split(",") if a.epic_labels else cfg["epic_labels"]
    return afk_decide.select_frontier(issues, ready, epic)


def cmd_pace(a):
    summary = json.loads(sys.stdin.read()) if a.stdin else json.loads(a.summary)
    config = json.loads(a.config)
    return {"seconds": afk_decide.pace(summary, config)}


def cmd_next_attempt(a):
    labels = json.loads(sys.stdin.read()) if a.stdin else \
        [s for s in (a.labels or "").split(",") if s]
    retry = a.retry if a.retry is not None else _cfg(a)["retry"]
    return afk_decide.next_attempt(labels, retry)


def cmd_subclassify(a):
    return {"status": afk_decide.subclassify_pr(a.pr, a.checks, a.ci or _cfg(a)["gate"]["ci"])}


def cmd_classify_no_pr(a):
    """The 5-way verdict for one of my `no_pr` claims (coding / idle_done /
    idle_blocked / idle_failed / dead), from the three disambiguating signals the
    tick gathered: git progress (`afk worker-status`), the declared verdict (`afk
    verdict`), and the orca liveness probe. `--terminal busy|idle|none` maps to the
    pure function's `terminal_idle` (False / True / None). Grace resolves flag →
    --config worker_idle_grace_seconds → default."""
    progress = json.loads(a.progress) if a.progress else {}
    verdict = json.loads(a.verdict) if a.verdict else None
    grace = a.grace if a.grace is not None else _cfg(a)["worker_idle_grace_seconds"]
    terminal_idle = {"busy": False, "idle": True, "none": None}[a.terminal]
    return afk_decide.classify_no_pr(progress, terminal_idle, a.idle_seconds,
                                     verdict, a.blocked_by_open, grace)


# --------------------------------------------------------------------------- #
# arg wiring                                                                  #
# --------------------------------------------------------------------------- #

def build_parser():
    ap = argparse.ArgumentParser(prog="afk", description="afk-fleet deterministic tool")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add_ns(p):
        p.add_argument("--repo", default=None,
                       help="owner/name of the target repo — the one repo handle: git-ref ops "
                            "push/fetch to its GitHub URL, gh ops read it directly "
                            "(falls back to --remote, default origin)")
        p.add_argument("--remote", default="origin")
        p.add_argument("--ns", default="refs/afk", help="claim base namespace (or refs/heads fallback)")

    def add_cfg(p):
        p.add_argument("--config", default=None,
                       help="canonical config JSON from `afk config` (individual flags override; "
                            "omitted values fall back to the one defaults table — ADR-0009)")

    # config — one home for every key and default (ADR-0009)
    p = sub.add_parser("config", help="parse + validate the repo config file → canonical JSON")
    p.add_argument("--file", default=None, help="path to the target repo's docs/agents/afk-fleet.md")
    p.add_argument("--defaults", action="store_true", help="print the pure defaults table")
    p.set_defaults(fn=cmd_config)

    # frontier
    p = sub.add_parser("frontier", help="dispatchable set (pure)")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--fixture"); src.add_argument("--stdin", action="store_true")
    add_cfg(p)
    p.add_argument("--ready-label", default=None)
    p.add_argument("--epic-labels", default=None)
    p.set_defaults(fn=cmd_frontier)

    # scan
    p = sub.add_parser("scan", help="read all claim + heartbeat refs (effectful)")
    add_ns(p); p.set_defaults(fn=cmd_scan)

    # classify-claims
    p = sub.add_parser("classify-claims", help="partition claims into mine/peer_live/stale")
    add_ns(p); add_cfg(p)
    p.add_argument("--instance", required=True)
    p.add_argument("--ttl", type=int, default=None)
    p.add_argument("--now", type=int, default=None, help="epoch override (tests)")
    p.add_argument("--claims-json", default=None, help="inject claims, skip git (tests)")
    p.add_argument("--heartbeats-json", default=None)
    p.set_defaults(fn=cmd_classify_claims)

    # claim / reclaim / release
    p = sub.add_parser("claim", help="atomically create a claim ref (effectful)")
    add_ns(p); p.add_argument("number", type=int)
    p.add_argument("--instance", required=True)
    p.add_argument("--host", default=socket.gethostname())
    p.add_argument("--now", type=int, default=None)
    p.set_defaults(fn=cmd_claim)

    p = sub.add_parser("reclaim", help="force-with-lease takeover of a stale claim (effectful)")
    add_ns(p); p.add_argument("number", type=int)
    p.add_argument("--instance", required=True)
    p.add_argument("--expect-sha", required=True, help="the sha you read; takeover fails if it moved")
    p.add_argument("--host", default=socket.gethostname())
    p.add_argument("--now", type=int, default=None)
    p.set_defaults(fn=cmd_reclaim)

    p = sub.add_parser("release", help="delete a claim ref (effectful, idempotent)")
    add_ns(p); p.add_argument("number", type=int); p.set_defaults(fn=cmd_release)

    # heartbeat
    p = sub.add_parser("heartbeat", help="refresh my heartbeat if due (effectful)")
    add_ns(p); add_cfg(p)
    p.add_argument("--instance", required=True)
    p.add_argument("--ttl", type=int, default=None)
    p.add_argument("--now", type=int, default=None)
    p.set_defaults(fn=cmd_heartbeat)

    # worker-command — launcher/worker provider parity (ADR-0010)
    p = sub.add_parser("worker-command",
                       help="settle the command workers are started with: ask-or-not + candidates, "
                            "or --check a human's answer")
    p.add_argument("--check", default=None,
                   help="a candidate command: resolve its first word in the login shell "
                        "and report whether it runs (and looks unattended)")
    p.add_argument("--base-url", default=None,
                   help="override the launcher's ANTHROPIC_BASE_URL (tests)")
    p.set_defaults(fn=cmd_worker_command)

    # probe — claim namespace + (in local-gate mode) target branch protection
    p = sub.add_parser("probe", help="bootstrap probe: claim namespace, and target-branch "
                                     "protection when gate.ci is local (effectful)")
    add_ns(p); add_cfg(p)
    p.add_argument("--target", default=None,
                   help="branch whose protection to check (default: config merge.target)")
    p.add_argument("--now", type=int, default=None)
    p.set_defaults(fn=cmd_probe)

    # gate-run — the local completion gate, run at merge time (ADR-0012)
    p = sub.add_parser("gate-run", help="run the configured local gate in a worktree → "
                                        "{status, excerpt} (effectful)")
    add_cfg(p)
    p.add_argument("--worktree", required=True, help="worktree to run the gate in")
    p.add_argument("--command", default=None,
                   help="override the command (default: config gate.local_command)")
    p.add_argument("--timeout", type=int, default=1800,
                   help="seconds before the run is called red (default 1800)")
    p.add_argument("--excerpt-lines", type=int, default=afk_decide.GATE_EXCERPT_LINES,
                   help="how many trailing log lines the excerpt keeps")
    p.set_defaults(fn=cmd_gate_run)

    # status — human-facing progress board (effectful, idempotent; ADR-0006)
    p = sub.add_parser("status", help="upsert the human-facing progress status board comment (effectful, idempotent)")
    p.add_argument("number", type=int)
    p.add_argument("--repo", default=None, help="owner/name (for gh api; required unless --print)")
    p.add_argument("--state", required=True,
                   help='JSON: {"phase":…,"instance":…,"pr":…,"attempt":…,"retry_max":…}')
    p.add_argument("--print", dest="print_only", action="store_true",
                   help="render the body only, do not touch GitHub")
    p.set_defaults(fn=cmd_status)

    # rebuild — one read-only call returns the tick's whole working set (ADR-0008)
    p = sub.add_parser("rebuild", help="gather + assemble the tick's working set (read-only)")
    add_ns(p); add_cfg(p)  # --repo (required here unless --state-json) comes from add_ns
    p.add_argument("--instance", required=True)
    p.add_argument("--ttl", type=int, default=None)
    p.add_argument("--ready-label", default=None)
    p.add_argument("--epic-labels", default=None)
    p.add_argument("--ci", default=None, choices=list(afk_decide.GATE_CI_MODES),
                   help="gate.ci override: in 'local' an open PR is awaiting_merge outright, "
                        "since gating is a merge-time action, not an observation (ADR-0012)")
    p.add_argument("--now", type=int, default=None, help="epoch override (tests)")
    p.add_argument("--state-json", default=None,
                   help='inject {"issues","prs","claims","heartbeats","blocked_by"}, skip gh/git (tests)')
    p.set_defaults(fn=cmd_rebuild)

    # fingerprint — the launcher's zero-LLM cycle gate (ADR-0007)
    p = sub.add_parser("fingerprint", help="digest observable state; skip-or-tick verdict for the launcher")
    add_ns(p); add_cfg(p)  # --repo (required here unless --state-json) comes from add_ns
    p.add_argument("--last", default="", help="the previous cycle's digest (empty on the first cycle)")
    p.add_argument("--skips", type=int, default=0, help="consecutive skipped cycles so far")
    p.add_argument("--force-after", type=int, default=None, help="full tick at least every N skips")
    p.add_argument("--state-json", default=None,
                   help='inject {"issues":[…],"prs":[…],"claims":[…]}, skip gh/git (tests)')
    p.set_defaults(fn=cmd_fingerprint)

    # pace
    p = sub.add_parser("pace", help="next launcher sleep in seconds (pure)")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--summary"); g.add_argument("--stdin", action="store_true")
    p.add_argument("--config", required=True, help="JSON config object")
    p.set_defaults(fn=cmd_pace)

    # next-attempt
    p = sub.add_parser("next-attempt", help="retry-or-escalate from afk-attempt labels (pure)")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--labels", help="comma-separated label names"); g.add_argument("--stdin", action="store_true")
    add_cfg(p)
    p.add_argument("--retry", type=int, default=None)
    p.set_defaults(fn=cmd_next_attempt)

    # subclassify
    p = sub.add_parser("subclassify", help="classify one of my claims from its PR + checks (pure)")
    add_cfg(p)
    p.add_argument("--pr", default="none", help='"open" if an open PR closes it, else none')
    p.add_argument("--checks", default=None, help="green|red|pending")
    p.add_argument("--ci", default=None, choices=list(afk_decide.GATE_CI_MODES),
                   help="gate.ci (default: from --config) — 'local' never reads checks")
    p.set_defaults(fn=cmd_subclassify)

    # worker-status — the decisive git PROGRESS signal for a no_pr claim (effectful, git-only)
    p = sub.add_parser("worker-status", help="read a worker worktree's git progress (effectful, git-only)")
    add_cfg(p)  # for the base_branch fallback
    p.add_argument("--worktree", required=True, help="path to the worker's worktree")
    p.add_argument("--base", default=None, help="base branch to count commits ahead of (default: config base_branch)")
    p.set_defaults(fn=cmd_worker_status)

    # takeover — the human-authorized, lease-skipping reclaim of a dead fleet (ADR-0011)
    p = sub.add_parser("takeover",
                       help="list the fleet instances GitHub remembers, or force-take a dead "
                            "one's claims (skips the staleness gate; effectful)")
    add_ns(p); add_cfg(p)
    p.add_argument("--list", action="store_true",
                   help="show every discoverable instance: heartbeat age, host, claim count")
    p.add_argument("--instance", default=None,
                   help="the instance to take FROM (the dead one). NOTE: every other subcommand's "
                        "--instance is your own id; here yours is --as")
    p.add_argument("--as", dest="me", default=None,
                   help="my instance id — every taken claim is re-stamped with it")
    p.add_argument("--yes", action="store_true",
                   help="confirm a takeover of an instance whose heartbeat is still FRESH "
                        "(it looks alive; you are asserting you know it is dead)")
    p.add_argument("--host", default=socket.gethostname())
    p.add_argument("--ttl", type=int, default=None)
    p.add_argument("--now", type=int, default=None)
    p.set_defaults(fn=cmd_takeover)

    # recovery — the tiered continuation verdict for one DEAD claim (ADR-0011)
    p = sub.add_parser("recovery",
                       help="does a dead claim have recoverable progress, and where? → the "
                            "tiered continuation verdict (effectful: orca list + git)")
    add_ns(p); add_cfg(p)
    p.add_argument("--issue", dest="number", type=int, required=True)
    p.add_argument("--base", default=None, help="base branch (default: config base_branch)")
    p.add_argument("--branch", default=None,
                   help="the issue's work branch, when already known (skips discovery)")
    p.add_argument("--worktree", default=None,
                   help="path to the issue's worktree, when already known (skips the orca read)")
    p.add_argument("--no-worktree", action="store_true",
                   help="assert no local worktree survives (skips the orca read)")
    p.add_argument("--orca-json", default=None,
                   help="inject `orca worktree list --json` (tests / no orca)")
    p.set_defaults(fn=cmd_recovery)

    # verdict — the worker's declared reason for opening no PR (effect gather + pure parse)
    p = sub.add_parser("verdict", help="latest parsed afk:verdict marker on an issue (effectful)")
    p.add_argument("--repo", default=None, help="owner/name (for gh api; required unless --comments-json)")
    p.add_argument("--issue", dest="number", type=int, required=True)
    p.add_argument("--comments-json", default=None, help="inject issue comments, skip gh (tests)")
    p.set_defaults(fn=cmd_verdict)

    # classify-no-pr — the 5-way no_pr verdict (pure) from the tick's gathered signals
    p = sub.add_parser("classify-no-pr", help="5-way no_pr verdict: coding/idle_done/idle_blocked/idle_failed/dead (pure)")
    add_cfg(p)
    p.add_argument("--progress", default=None, help="JSON from `afk worker-status` ({} if unknown)")
    p.add_argument("--terminal", choices=["busy", "idle", "none"], required=True,
                   help="orca liveness probe: busy | idle | none (no live worker)")
    p.add_argument("--idle-seconds", type=int, default=None,
                   help="seconds since the worker's last observable activity")
    p.add_argument("--verdict", default=None, help="JSON from `afk verdict` (omit if none)")
    p.add_argument("--blocked-by-open", action="store_true",
                   help="set iff any blocked_by issue is still open (blocked verdict only)")
    p.add_argument("--grace", type=int, default=None, help="worker_idle_grace_seconds override")
    p.set_defaults(fn=cmd_classify_no_pr)

    return ap


def main():
    a = build_parser().parse_args()
    try:
        result = a.fn(a)
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False))
        sys.exit(3)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0)


if __name__ == "__main__":
    main()
