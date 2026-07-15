#!/usr/bin/env python3
"""
afk.py — the afk-fleet tool: deterministic muscle the LLM tick calls.

The tick (an LLM) orchestrates and judges; when it needs a *deterministic* action
it shells out to one of these subcommands and reads back JSON (ADR-0004, choice 甲).
Every subcommand prints one JSON object to stdout. Exit 0 = ran (a lost claim race
is `{"won": false}`, still exit 0); exit 3 = an operational/git error.

Two layers:
  - pure verdicts (`frontier`, `pace`, `next-attempt`, `subclassify`, and the
    classification half of `classify-claims`) come from afk_decide.py — no I/O,
    fixture-tested; time is always injected, never read here.
  - effectful ops (`scan`, `claim`, `reclaim`, `release`, `heartbeat`, `probe`)
    drive git refs / gh. Their real test is a scratch-repo integration suite
    (tracked separately) — here they are correct-by-construction and smoke-tested.

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


def _ns_paths(base):
    """Map the claim base namespace to (claim_ns, heartbeat_ns).
    `refs/afk` → hidden namespace; `refs/heads` → the branch fallback (ADR-0003)."""
    if base == "refs/heads":
        return "refs/heads/afk-claim", "refs/heads/afk-heartbeat"
    return base + "/claim", base + "/heartbeat"


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


def _issue_num_from_ref(refname):
    tail = refname.rstrip("/").rsplit("/", 1)[-1]
    return int(tail) if tail.isdigit() else None


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
    claims, heartbeats = _scan(a.remote, a.ns)
    return {"claims": claims, "heartbeats": heartbeats}


def cmd_classify_claims(a):
    if a.claims_json is not None:              # test-injected — no git
        claims = json.loads(a.claims_json)
        heartbeats = json.loads(a.heartbeats_json or "{}")
    else:
        claims, heartbeats = _scan(a.remote, a.ns)
    now = a.now if a.now is not None else int(time.time())
    result = afk_decide.classify_claims(claims, heartbeats, a.instance, now, a.ttl)
    result["now"] = now
    return result


def cmd_claim(a):
    claim_ns, _ = _ns_paths(a.ns)
    ref = f"{claim_ns}/{a.number}"
    now = a.now if a.now is not None else int(time.time())
    sha = _marker_commit(_marker_text("afk-claim", a.instance, now, host=a.host))
    # Create-only: the server rejects a ref that already exists → that is the CAS.
    p = _git(["push", a.remote, f"{sha}:{ref}"], check=False)
    if p.returncode == 0:
        return {"won": True, "issue": a.number, "ref": ref, "sha": sha, "instance": a.instance}
    owner = _read_marker(a.remote, ref)  # who beat us
    return {"won": False, "issue": a.number, "ref": ref,
            "owner": owner, "detail": p.stderr.strip()}


def cmd_reclaim(a):
    claim_ns, _ = _ns_paths(a.ns)
    ref = f"{claim_ns}/{a.number}"
    now = a.now if a.now is not None else int(time.time())
    sha = _marker_commit(_marker_text("afk-claim", a.instance, now, host=a.host))
    # Atomic takeover: rejected unless the ref still points at the sha we read.
    p = _git(["push", a.remote,
              f"--force-with-lease={ref}:{a.expect_sha}", f"{sha}:{ref}"], check=False)
    if p.returncode == 0:
        return {"won": True, "issue": a.number, "ref": ref, "sha": sha, "instance": a.instance}
    return {"won": False, "issue": a.number, "ref": ref, "detail": p.stderr.strip()}


def cmd_release(a):
    claim_ns, _ = _ns_paths(a.ns)
    ref = f"{claim_ns}/{a.number}"
    p = _git(["push", a.remote, "--delete", ref], check=False)
    # Already gone counts as released — idempotent cleanup.
    ok = p.returncode == 0 or "remote ref does not exist" in p.stderr or "deleted" in p.stderr
    return {"released": bool(ok), "issue": a.number, "ref": ref, "detail": p.stderr.strip()}


def cmd_heartbeat(a):
    _, hb_ns = _ns_paths(a.ns)
    ref = f"{hb_ns}/{a.instance}"
    now = a.now if a.now is not None else int(time.time())
    cur = _read_marker(a.remote, ref)
    last = cur.get("ts") if cur else None
    if not afk_decide.heartbeat_due(last, now, a.ttl):
        return {"refreshed": False, "reason": "not due", "ts": last, "ref": ref}
    sha = _marker_commit(_marker_text("afk-heartbeat", a.instance, now))
    p = _git(["push", a.remote, "--force", f"{sha}:{ref}"], check=False)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip())
    return {"refreshed": True, "ts": now, "ref": ref}


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


def cmd_probe(a):
    """Decide the claim namespace: can we push under refs/afk/*? Else fall back to
    branches (refs/heads/afk-claim/*) and flag that on:push CI will fire."""
    ref = f"{a.ns}/probe"
    sha = _marker_commit(_marker_text("afk-probe", "probe", a.now if a.now is not None else int(time.time())))
    p = _git(["push", a.remote, f"{sha}:{ref}"], check=False)
    if p.returncode != 0:
        return {"namespace": "refs/heads", "hidden": False, "ci_on_push": True,
                "blocked": True, "detail": p.stderr.strip()}
    _git(["push", a.remote, "--delete", ref], check=False)
    return {"namespace": "refs/afk", "hidden": True, "ci_on_push": False, "blocked": False}


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
    return afk_decide.select_frontier(issues, a.ready_label,
                                      [s for s in a.epic_labels.split(",")])


def cmd_pace(a):
    summary = json.loads(sys.stdin.read()) if a.stdin else json.loads(a.summary)
    config = json.loads(a.config)
    return {"seconds": afk_decide.pace(summary, config)}


def cmd_next_attempt(a):
    labels = json.loads(sys.stdin.read()) if a.stdin else \
        [s for s in (a.labels or "").split(",") if s]
    return afk_decide.next_attempt(labels, a.retry)


def cmd_subclassify(a):
    return {"status": afk_decide.subclassify_pr(a.pr, a.checks)}


# --------------------------------------------------------------------------- #
# arg wiring                                                                  #
# --------------------------------------------------------------------------- #

def build_parser():
    ap = argparse.ArgumentParser(prog="afk", description="afk-fleet deterministic tool")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add_ns(p):
        p.add_argument("--remote", default="origin")
        p.add_argument("--ns", default="refs/afk", help="claim base namespace (or refs/heads fallback)")

    # frontier
    p = sub.add_parser("frontier", help="dispatchable set (pure)")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--fixture"); src.add_argument("--stdin", action="store_true")
    p.add_argument("--ready-label", default="ready-for-agent")
    p.add_argument("--epic-labels", default="epic,prd,wayfinder:map")
    p.set_defaults(fn=cmd_frontier)

    # scan
    p = sub.add_parser("scan", help="read all claim + heartbeat refs (effectful)")
    add_ns(p); p.set_defaults(fn=cmd_scan)

    # classify-claims
    p = sub.add_parser("classify-claims", help="partition claims into mine/peer_live/stale")
    add_ns(p)
    p.add_argument("--instance", required=True)
    p.add_argument("--ttl", type=int, required=True)
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
    add_ns(p)
    p.add_argument("--instance", required=True)
    p.add_argument("--ttl", type=int, required=True)
    p.add_argument("--now", type=int, default=None)
    p.set_defaults(fn=cmd_heartbeat)

    # probe
    p = sub.add_parser("probe", help="pick the claim namespace (effectful)")
    add_ns(p); p.add_argument("--now", type=int, default=None); p.set_defaults(fn=cmd_probe)

    # status — human-facing progress board (effectful, idempotent; ADR-0006)
    p = sub.add_parser("status", help="upsert the human-facing progress status board comment (effectful, idempotent)")
    p.add_argument("number", type=int)
    p.add_argument("--repo", default=None, help="owner/name (for gh api; required unless --print)")
    p.add_argument("--state", required=True,
                   help='JSON: {"phase":…,"instance":…,"pr":…,"attempt":…,"retry_max":…}')
    p.add_argument("--print", dest="print_only", action="store_true",
                   help="render the body only, do not touch GitHub")
    p.set_defaults(fn=cmd_status)

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
    p.add_argument("--retry", type=int, default=2)
    p.set_defaults(fn=cmd_next_attempt)

    # subclassify
    p = sub.add_parser("subclassify", help="classify one of my claims from its PR + checks (pure)")
    p.add_argument("--pr", default="none", help='"open" if an open PR closes it, else none')
    p.add_argument("--checks", default=None, help="green|red|pending")
    p.set_defaults(fn=cmd_subclassify)

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
