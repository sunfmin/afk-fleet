#!/usr/bin/env python3
"""
afk_decide.py — the afk-fleet decision core, as pure functions.

The single source of truth for every deterministic "should we?" verdict the fleet
makes. No I/O: each function takes normalized inputs (the effectful `afk.py` layer
gathers them from gh + git refs, and injects `now`) and returns a verdict. Purity
is what makes the correctness-critical logic — dispatch eligibility, claim
ownership/liveness, retry/escalate, pacing — testable with fixtures, without gh,
git, or the network. See ADR-0004 (deterministic mechanics → tested code tools;
orchestration + judgment → the LLM tick).

The dangerous ones (ADR-0003): `classify_claims` decides mine-vs-live-peer-vs-stale,
where a wrong verdict silently corrupts state (a phantom lock starves an issue; a
stolen live claim double-works it). Those get the hardest fixtures.

None of these functions read the clock — `now` is always an argument — so a fixture
pins behaviour deterministically.
"""
import hashlib
import json
import os
import re

# --------------------------------------------------------------------------- #
# Config — one home for every key and default (ADR-0009)                       #
# --------------------------------------------------------------------------- #
#
# THE single source of truth for the config schema: every key the fleet knows,
# with its default and (via the default's type) its shape. The template in
# references/config-template.md is the human-facing rendering of this table —
# a fixture test keeps the two equal, so a hand-edit that drifts turns the
# suite red. `authorize` and the instance id are deliberately NOT keys here:
# they are per-run, launcher-held facts, and the unknown-key error below is
# what keeps them out of files.

CONFIG_DEFAULTS = {
    # dispatch contract
    "ready_label": "ready-for-agent",
    "epic_labels": ["epic", "prd", "wayfinder:map"],
    "claim": "ref",
    "dependencies": "native",
    # workers
    "base_branch": "main",
    "branch_pattern": "issue-{number}-{slug}",
    "worker": "orca",
    "concurrency": 3,
    "worktree_cleanup": True,
    "worker_idle_grace_seconds": 300,
    # completion gate
    "gate": {
        "ci": "required",
        "local_command": "",
        "adversarial_verify": False,
        "adversarial_verify_prompt": "",
    },
    # merge
    "merge": {
        "strategy": "squash",
        "target": "main",
        "sync_before_merge": True,
        "delete_branch": True,
    },
    # failure handling
    "retry": 2,
    "escalate_label": "ready-for-human",
    "escalate_comment": True,
    # progress (human-facing)
    "progress_comment": True,
    # loop (launcher pacing)
    "busy_interval_seconds": 90,
    "idle_interval_seconds": 1500,
    "idle_ticks_before_sleep": 3,
    "claim_lease_ttl_seconds": 4500,
    "fingerprint_gate": True,
    "force_tick_after_skips": 6,
}


# The completion gate's two modes (ADR-0012). `required` waits for the PR's GitHub
# checks; `local` never reads them and makes `gate.local_command` the gate, re-run
# at merge time against the exact tree that lands.
GATE_CI_MODES = ("required", "local")

# Keys that were renamed, and why. A file still carrying the old name must fail
# LOUDLY with the migration note rather than be silently defaulted — a config that
# lies to its author is the failure mode ADR-0009 exists to prevent.
CONFIG_RENAMED = {
    "merge.rebase_before_merge": (
        "merge.sync_before_merge",
        "the merge path now MERGES origin/<base> into the branch instead of rebasing it "
        "(ADR-0012) — a rebase drops merge commits and re-ignites the conflicts already "
        "resolved inside them. Rename the key; its meaning and default (true) are unchanged."),
}


def _renamed(dotted):
    """The migration error text for a renamed key, or None if it isn't one."""
    hit = CONFIG_RENAMED.get(dotted)
    if not hit:
        return None
    new, why = hit
    return f"config: {dotted!r} was renamed to {new!r} — {why}"


def validate_config(cfg):
    """
    The semantic checks a per-key type cannot express, run on the CANONICAL config
    at load time (`afk config`) — i.e. at bootstrap, with the human present, where
    a bad combination can still be fixed instead of surfacing mid-run inside a
    tick. Raises ValueError; returns `cfg` unchanged so it can be used inline.
    """
    gate = cfg.get("gate") or {}
    ci = gate.get("ci")
    if ci not in GATE_CI_MODES:
        raise ValueError(f"config gate.ci: expected one of "
                         f"{' | '.join(GATE_CI_MODES)}, got {ci!r}")
    if ci == "local" and not (gate.get("local_command") or "").strip():
        raise ValueError("config gate.ci: 'local' requires a non-empty gate.local_command — in "
                         "local mode that command IS the completion gate (ADR-0012), so an empty "
                         "one would merge every PR unverified")
    return cfg


def _yaml_block(text):
    """The first ```yaml fence's body if `text` is a markdown file, else the
    text itself (already a bare block)."""
    if "```yaml" in text:
        return text.split("```yaml", 1)[1].split("```", 1)[0]
    return text


def _strip_comment(line):
    """Cut an unquoted trailing `# …` comment; quotes are respected."""
    out, quote = [], None
    for ch in line:
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            out.append(ch)
        elif ch == "#":
            break
        else:
            out.append(ch)
    return "".join(out).strip()


def _coerce(key, raw, default):
    """One scalar, typed by its default: bool, int, [a, b] list, or string."""
    if isinstance(default, bool):
        if raw in ("true", "True"):
            return True
        if raw in ("false", "False"):
            return False
        raise ValueError(f"config key {key!r}: expected true/false, got {raw!r}")
    if isinstance(default, int):
        try:
            return int(raw)
        except ValueError:
            raise ValueError(f"config key {key!r}: expected an integer, got {raw!r}")
    if isinstance(default, list):
        if not (raw.startswith("[") and raw.endswith("]")):
            raise ValueError(f"config key {key!r}: expected [a, b, ...], got {raw!r}")
        return [i.strip().strip("'\"") for i in raw[1:-1].split(",") if i.strip()]
    return raw.strip("'\"")


def parse_config_yaml(text):
    """
    Read the per-repo config — the ```yaml block in docs/agents/afk-fleet.md
    (a whole markdown file or a bare block both work). Schema-aware, zero-dep:
    it parses only the dialect this schema uses (`key: value` scalars, one
    inline `[a, b]` list, one-level `gate:`/`merge:` sections), and every key
    and type is checked against CONFIG_DEFAULTS — so parsing IS validation. An
    unknown key raises (a typo silently ignored would be a config that lies to
    its author, and `authorize:` in a file is refused by construction); so does
    a wrong shape. Returns the PARTIAL config — only the keys present.
    """
    partial = {}
    section = None
    for ln in _yaml_block(text).splitlines():
        if not ln.strip() or ln.lstrip().startswith("#"):
            continue
        indented = ln[0] in " \t"
        s = _strip_comment(ln)
        if not s:
            continue
        if ":" not in s:
            raise ValueError(f"config: unparseable line {ln.strip()!r}")
        key, _, raw = s.partition(":")
        key, raw = key.strip(), raw.strip()
        if indented:
            if section is None:
                raise ValueError(f"config: indented key {key!r} outside a gate:/merge: section")
            sub = CONFIG_DEFAULTS[section]
            if key not in sub:
                raise ValueError(_renamed(f"{section}.{key}")
                                 or f"config: unknown key {section}.{key}")
            partial.setdefault(section, {})[key] = _coerce(f"{section}.{key}", raw, sub[key])
        else:
            if key not in CONFIG_DEFAULTS:
                raise ValueError(_renamed(key)
                                 or f"config: unknown key {key!r} (note: authorize/instance "
                                    f"are per-run facts, never config keys)")
            default = CONFIG_DEFAULTS[key]
            if isinstance(default, dict):
                if raw:
                    raise ValueError(f"config key {key!r} is a section — write `{key}:` "
                                     f"with indented keys")
                section = key
                partial.setdefault(key, {})
            else:
                section = None
                partial[key] = _coerce(key, raw, default)
    return partial


def resolve_config(partial):
    """Partial config → the complete canonical config: every key present,
    defaults filled from CONFIG_DEFAULTS (one level deep for gate/merge).
    Idempotent — resolving an already-canonical config is a no-op."""
    out = {}
    for k, dv in CONFIG_DEFAULTS.items():
        if isinstance(dv, dict):
            merged = dict(dv)
            merged.update(partial.get(k) or {})
            out[k] = merged
        elif k in partial:
            out[k] = partial[k]
        else:
            out[k] = list(dv) if isinstance(dv, list) else dv
    return out

# --------------------------------------------------------------------------- #
# Dispatch eligibility — "can a worker take this issue right now?"             #
# (migrated from the former select_frontier.py; unchanged contract)           #
# --------------------------------------------------------------------------- #

def _label_names(issue):
    """Accept labels as [{"name": ...}] (gh) or ["..."] (fixture)."""
    out = []
    for lb in issue.get("labels", []) or []:
        if isinstance(lb, dict):
            name = lb.get("name")
            if name:
                out.append(name)
        elif isinstance(lb, str):
            out.append(lb)
    return out


def _claimed(issue):
    """True iff an afk-claim/<n> lock ref exists (any owner). Single source of
    truth for 'already taken' — not the assignee (ADR-0003)."""
    return bool(issue.get("claimed", False))


def _has_open_pr(issue):
    """True iff an open PR already closes this issue — durable in-flight evidence
    independent of the claim ref (the open-PR guard, ADR-0003)."""
    return bool(issue.get("has_open_pr", False))


def _open_blockers(issue):
    """open_blockers given directly, or derived from a blocked_by list of
    {"state": ...} objects (open ones count)."""
    if "open_blockers" in issue and issue["open_blockers"] is not None:
        return int(issue["open_blockers"])
    bb = issue.get("blocked_by")
    if isinstance(bb, list):
        return sum(1 for b in bb if (b or {}).get("state", "open") == "open")
    return 0


def select_frontier(issues, ready_label, epic_labels):
    """
    Decide which issues are dispatchable RIGHT NOW. Shared by `--plan` and the live
    tick, so both compute the identical frontier (a stable published contract).

    An issue is dispatchable iff ALL hold:
      state == "open" · has ready_label · no epic label · not claimed (no
      afk-claim ref) · no open linked PR · zero open blocking dependencies.

    Returns {"dispatch": [num...], "excluded": [{"number","reason"}...]}.
    """
    epic_set = {e.strip() for e in epic_labels if e.strip()}
    dispatch, excluded = [], []
    for issue in issues:
        num = issue.get("number")
        labels = set(_label_names(issue))
        if issue.get("state", "open") != "open":
            excluded.append({"number": num, "reason": "not open"})
            continue
        if ready_label not in labels:
            excluded.append({"number": num, "reason": f"no {ready_label} label"})
            continue
        hit_epic = labels & epic_set
        if hit_epic:
            excluded.append({"number": num, "reason": f"epic label ({', '.join(sorted(hit_epic))})"})
            continue
        if _claimed(issue):
            excluded.append({"number": num, "reason": "already claimed (afk-claim ref exists)"})
            continue
        if _has_open_pr(issue):
            excluded.append({"number": num, "reason": "has an open linked PR"})
            continue
        ob = _open_blockers(issue)
        if ob > 0:
            excluded.append({"number": num, "reason": f"{ob} open blocker(s)"})
            continue
        dispatch.append(num)
    return {"dispatch": dispatch, "excluded": excluded}


# --------------------------------------------------------------------------- #
# Claim ownership + owner-liveness — the correctness-critical partition        #
# --------------------------------------------------------------------------- #

def is_stale(last_ts, now, ttl):
    """A claim's owner is presumed dead when its heartbeat is missing or older
    than `ttl`. Missing (None) counts as stale — an owner that never beat."""
    if last_ts is None:
        return True
    return (now - int(last_ts)) > ttl


def heartbeat_due(last_ts, now, ttl):
    """Refresh my own heartbeat once it is older than ttl/3 (or never beat). Beating
    at ttl/3 keeps a comfortable 3x margin under the lease while staying cheap."""
    if last_ts is None:
        return True
    return (now - int(last_ts)) > ttl / 3.0


def classify_claims(claims, heartbeats, me, now, ttl):
    """
    Partition every afk-claim ref by ownership and owner-liveness — the verdict a
    wrong answer would silently corrupt (ADR-0003).

      claims:     [{"number": int, "instance": str}, ...]  (from refs/afk/claim/*)
      heartbeats: {instance_id: last_ts_epoch}             (from refs/afk/heartbeat/*)
      me:         my instance id
      now, ttl:   epoch seconds / claim_lease_ttl seconds

    Returns {"mine":[n...], "peer_live":[n...], "stale":[n...]}:
      mine       — stamped with my instance; I reconcile these locally (a no-PR/
                   no-live-worker one is an *orphaned claim*, decided by the tick
                   with the worker liveness probe — not here).
      peer_live  — a peer owns it AND its heartbeat is within ttl → never touch.
      stale      — a peer owns it AND its heartbeat is missing/expired → the only
                   foreign claim I may reclaim (--force-with-lease takeover).
    A claim whose marker names no instance is treated as a peer's and, lacking a
    heartbeat, is reclaimable.
    """
    mine, peer_live, stale = [], [], []
    for c in claims:
        n = c.get("number")
        inst = c.get("instance")
        if inst is not None and inst == me:
            mine.append(n)
        elif is_stale(heartbeats.get(inst), now, ttl):
            stale.append(n)
        else:
            peer_live.append(n)
    return {"mine": sorted(mine), "peer_live": sorted(peer_live), "stale": sorted(stale)}


def subclassify_pr(pr_state, checks_state, ci_mode="required"):
    """
    Classify one of MY in-flight claims from its PR + checks. Whether a "no_pr"
    claim is an *orphan* needs the worker liveness probe (orca-cli) — that stays
    the tick's judgment, combining this verdict with the probe result.

      pr_state:     "open" if an open PR closes the issue, else anything (→ no_pr)
      checks_state: "green" | "red" | "pending" | None
      ci_mode:      gate.ci — "required" reads the checks; "local" never does
    Returns: "awaiting_merge" | "failure" | "awaiting_ci" | "no_pr".

    In `local` mode (ADR-0012) an open PR is always *awaiting_merge*: there are no
    checks to wait on, because gating is an **action the tick takes at merge time**
    (sync → re-run the local gate → merge), not an observation it waits for. A red
    remote run — the repo's own `on: push` workflow, which the fleet does not gate
    on — must not park the claim in `failure` forever.
    """
    if pr_state != "open":
        return "no_pr"
    if ci_mode == "local":
        return "awaiting_merge"
    if checks_state == "green":
        return "awaiting_merge"
    if checks_state == "red":
        return "failure"
    return "awaiting_ci"


# --------------------------------------------------------------------------- #
# The local completion gate + its bootstrap compatibility probe (ADR-0012)     #
# --------------------------------------------------------------------------- #
#
# In `gate.ci: local` the repo-local build/test command IS the completion gate:
# the worker runs it after its pre-PR sync, and the tick re-runs it at merge time,
# after the merge-time sync, in the branch's worktree. The invariant both runs
# serve: *what lands on the target branch was tested in the form it lands.* The
# verdict shape below is deliberately the SAME {status, excerpt} the ephemeral
# CI-log sub-read returns in `required` mode, so the tick has one gate branch, and
# a raw log never enters its context either way.

GATE_EXCERPT_LINES = 40


def gate_verdict(exit_code, output, max_lines=GATE_EXCERPT_LINES, timed_out=False):
    """
    One local-gate run → `{status, exit_code, excerpt, omitted_lines, timed_out}`.

    Pure so the one thing that could quietly poison a merge — "which exit code
    counts as green" — is fixture-pinned: ONLY 0 is green, and a run that timed out
    is red, never green-by-default. The excerpt is the LAST `max_lines` lines
    (where build/test runners put the failure summary), bounded so a 50k-line log
    reaches the PR comment as a readable tail instead of flooding it.
    """
    lines = [ln.rstrip() for ln in (output or "").splitlines()]
    tail = lines[-max_lines:] if max_lines and max_lines > 0 else lines
    return {"status": "green" if (int(exit_code) == 0 and not timed_out) else "red",
            "exit_code": int(exit_code),
            "timed_out": bool(timed_out),
            "excerpt": "\n".join(tail).strip(),
            "omitted_lines": max(0, len(lines) - len(tail))}


def protection_verdict(ci_mode, protection, unavailable=None):
    """
    Is the merge target's branch protection compatible with the configured gate?
    Read at bootstrap, with the human present (ADR-0012).

      ci_mode:     gate.ci ("required" | "local")
      protection:  the target branch's protection object, or None if it has none
      unavailable: why protection could not be read (no admin rights, an API
                   error); None when the read succeeded

    Returns {"verdict": "ok"|"error"|"warn", "required_checks": [...], "detail"}.

      error  `ci: local` + the target REQUIRES status checks. `gh pr merge` is
             rejected no matter how green the local gate is, and the only bypass —
             `--admin` — also overrides human review, far too much power for an
             unattended fleet. So this is a hard error at bootstrap, not a
             surprise on the first merge.
      warn   the probe itself was inconclusive: continue, but say so.
      ok     nothing incompatible. In `required` mode required checks are exactly
             what the fleet waits for, so they are never a problem.
    """
    if ci_mode != "local":
        return {"verdict": "ok", "required_checks": [],
                "detail": f"gate.ci is {ci_mode!r} — required checks are the gate, not an obstacle"}
    if unavailable:
        return {"verdict": "warn", "required_checks": [],
                "detail": f"could not read branch protection ({unavailable}) — if the target "
                          f"requires status checks, merges will be rejected"}
    rsc = (protection or {}).get("required_status_checks") or {}
    checks = list(rsc.get("contexts") or [])
    for c in rsc.get("checks") or []:
        name = c.get("context") if isinstance(c, dict) else c
        if name and name not in checks:
            checks.append(name)
    if checks:
        return {"verdict": "error", "required_checks": sorted(checks),
                "detail": "gate.ci is 'local' but the target branch requires status checks "
                          f"({', '.join(sorted(checks))}) — every merge would be rejected. Either "
                          f"drop the required checks on that branch or use gate.ci: required."}
    return {"verdict": "ok", "required_checks": [],
            "detail": "target branch requires no status checks — a local gate can merge"}


# --------------------------------------------------------------------------- #
# no_pr reconciliation — disambiguating a FINISHED worker from a CODING one    #
# --------------------------------------------------------------------------- #
#
# `subclassify_pr` only says a claim has no PR yet; deciding *why* used to be a
# binary the tick did with an orca liveness probe alone (connected terminal +
# active-looking title). That is blind: a worker that ran to completion, decided
# there was no PR to open, posted its reason, and went idle looks IDENTICAL to
# one still coding — both are "a connected terminal with a title" — so the claim
# is parked forever. Three signals disambiguate, none of them terminal chrome:
#   1. git PROGRESS in the worktree (commits ahead of base / dirty tree / recent
#      file activity) — the decisive coding-vs-finished signal (afk worker-status);
#   2. the worker's explicit VERDICT marker on the issue (afk verdict) — the
#      single machine-readable source of truth for *why* it opened no PR;
#   3. terminal idle-vs-busy from the orca probe — still the tick's judgment.
# `classify_no_pr` is the pure join of the three (fixture-tested); parsing the
# marker is pure too. Whether to TRUST the marker stays the tick's call.

VERDICT_MARKER = "<!--afk:verdict"

# The closed set of reasons a worker may declare for opening no PR. `already-satisfied`
# = the issue is already done in base (empty diff); `blocked` = a runtime dependency
# gap (see blocked_by); `giving-up` = a genuine failure the worker could not resolve.
VERDICT_PHASES = ("already-satisfied", "blocked", "giving-up")

_VERDICT_MARKER_RE = re.compile(r"<!--\s*afk:verdict\b(.*?)-->", re.DOTALL)


def parse_verdict_marker(body):
    """
    Parse the FIRST afk:verdict marker in one comment body → a verdict dict, or
    None if the body carries no marker. The marker (worker-prompt.md, LAYER 1) is:

      <!--afk:verdict n=<issue> phase=<already-satisfied|blocked|giving-up> \
          [blocked_by=<csv of issue numbers>] [reason=<short>]-->

    Deterministic and LENIENT — a marker with a missing/unknown field still parses
    ({"found": True, "phase": None|<raw>}); whether to trust it, and what an
    unrecognised phase means, is the tick's / `classify_no_pr`'s call, never this
    parser's. `reason` (if present) must be the last field — it captures to the end
    of the marker so a short human phrase with spaces survives. Returns:
      {"found": True, "n": int|None, "phase": str|None, "blocked_by": [int], "reason": str|None}
    """
    if not body:
        return None
    m = _VERDICT_MARKER_RE.search(body)
    if not m:
        return None
    attrs = m.group(1)
    reason = None
    rm = re.search(r"\breason=(.*)$", attrs, re.DOTALL)
    if rm:
        reason = rm.group(1).strip() or None
        attrs = attrs[:rm.start()]        # keep reason from swallowing nothing else

    def _grab(pat):
        mm = re.search(pat, attrs)
        return mm.group(1) if mm else None

    n_raw = _grab(r"\bn=(\d+)")
    bb_raw = _grab(r"\bblocked_by=([0-9,\s]*)")
    return {"found": True,
            "n": int(n_raw) if n_raw else None,
            "phase": _grab(r"\bphase=([A-Za-z][\w-]*)"),
            "blocked_by": [int(x) for x in re.split(r"[,\s]+", (bb_raw or "").strip()) if x],
            "reason": reason}


def latest_verdict(comments):
    """
    The LATEST afk:verdict across an issue's comments (multiple markers → latest
    wins). `comments` is expected oldest-first (the gh default), each a
    {"body","comment_url"/"html_url"/"url","created_at"} dict or a bare body
    string; the last marker-bearing comment in that order is the live verdict.
    Pure — afk.py's `verdict` fetches, this parses. Returns:
      {"found": bool, "phase": str|None, "blocked_by": [int], "reason": str|None,
       "comment_url": str|None}
    """
    result = {"found": False, "phase": None, "blocked_by": [],
              "reason": None, "comment_url": None}
    for c in comments or []:
        if isinstance(c, str):
            body, url = c, None
        else:
            body = c.get("body") or ""
            url = c.get("comment_url") or c.get("html_url") or c.get("url")
        parsed = parse_verdict_marker(body)
        if parsed:
            result = {"found": True, "phase": parsed["phase"],
                      "blocked_by": parsed["blocked_by"], "reason": parsed["reason"],
                      "comment_url": url}
    return result


def classify_no_pr(progress, terminal_idle, idle_seconds, verdict, blocked_by_open,
                   grace_seconds):
    """
    The 5-way verdict for one of MY `no_pr` claims — the fix for the finished-vs-coding
    blind spot. Pure join of the three disambiguating signals; the tick gathers them
    (`afk worker-status`, `afk verdict`, the orca liveness probe) and calls this.

      progress:        {"commits_ahead": int, "dirty": bool, "last_commit_ts": …,
                        "worktree_mtime_ts": …} from `afk worker-status` ({} if unknown).
      terminal_idle:   False = orca probe found a BUSY worker; True = a connected but
                       IDLE worker; None = NO live worker/terminal at all.
      idle_seconds:    seconds since the worker's last observable activity (max of
                       last_commit_ts / worktree_mtime_ts / terminal activity); None = unknown.
      verdict:         the `latest_verdict` dict (or None) — the worker's declared reason.
      blocked_by_open: True iff any issue in verdict.blocked_by is still open (the tick
                       re-checks each; only consulted for a `blocked` verdict).
      grace_seconds:   `worker_idle_grace_seconds` — quiet window before "idle" is trusted.

    Returns {"outcome": <str>, "action": <str>}:
      coding       leave         — busy, OR activity within grace. **Not** "has commits":
                                   see the standing-vs-live note below.
      idle_done    close_release — idle past grace + phase already-satisfied + NO changes on
                                   the branch: the tick verifies the empty diff, closes + releases.
      idle_blocked redispatch    — …+ phase blocked, and every blocked_by is now closed:
                                   the DAG cleared, re-dispatch (keep the claim).
      idle_blocked escalate      — …+ phase blocked, but a blocked_by is still open:
                                   a real DAG gap — escalate (add escalate_label, comment it).
      idle_failed  next_attempt  — …+ phase giving-up, an unknown phase, or NO verdict at
                                   all after grace → failure handling (`afk next-attempt`).
      dead         orphan        — no live worker/terminal → existing orphan path.
    """
    progress = progress or {}
    # A STANDING fact ("this branch has work on it"), NOT a sign of life. Used only to
    # contradict an `already-satisfied` verdict — never to prove the worker is alive.
    has_changes = int(progress.get("commits_ahead") or 0) > 0 or bool(progress.get("dirty"))

    # dead first: no worker means it cannot be "coding", whatever it left behind.
    if terminal_idle is None:
        return {"outcome": "dead", "action": "orphan"}

    # coding: only **live** signals count — a busy terminal, or observed activity inside the
    # grace window (`idle_seconds` is already the max-recency of last_commit_ts /
    # worktree_mtime_ts / terminal activity, so recent commits are covered here).
    #
    # `has_changes` deliberately does NOT appear. It is monotonic: once a worker has one
    # commit, `commits_ahead > 0` stays true until the branch merges, and `dirty` stays true
    # forever if the worker died mid-edit. Including it made the whole idle+verdict path
    # unreachable for any worker that had ever committed — a worker that committed, went
    # idle, and never opened a PR or left a verdict was re-classified `coding` on every
    # future tick, holding its claim indefinitely and never reaching the retry ladder.
    # Observed live: gaokaowiki #139 sat at commits_ahead=4, dirty=false, tui-idle, 33 min
    # past its last commit, no PR, no verdict — and stayed `coding`. See ADR-0013.
    within_grace = (idle_seconds is not None and grace_seconds is not None
                    and idle_seconds < grace_seconds)
    if terminal_idle is False or within_grace:
        return {"outcome": "coding", "action": "leave"}

    # idle past grace: route on the declared reason.
    phase = verdict.get("phase") if (verdict and verdict.get("found")) else None
    if phase == "already-satisfied":
        # "nothing needed doing" is refuted by work sitting on the branch. Trust the branch,
        # not the claim: route it through failure handling instead of closing the issue on a
        # diff the tick would then fail to verify as empty.
        if has_changes:
            return {"outcome": "idle_failed", "action": "next_attempt"}
        return {"outcome": "idle_done", "action": "close_release"}
    if phase == "blocked":
        return {"outcome": "idle_blocked",
                "action": "escalate" if blocked_by_open else "redispatch"}
    return {"outcome": "idle_failed", "action": "next_attempt"}


# --------------------------------------------------------------------------- #
# Takeover — the human-authorized, lease-skipping reclaim (ADR-0011)           #
# --------------------------------------------------------------------------- #
#
# The lease (above) is the *unattended* line between "dead" and "alive but slow":
# a peer may reclaim a claim only once its owner's heartbeat has expired past
# `claim_lease_ttl` (~75 min). But a fleet dies wholesale, from a quota hard stop,
# with a human watching — and that human IS the oracle for "it is really dead".
# Takeover is their fast path: list the instances GitHub still remembers (the
# launcher forgot its own id when it died; the claim markers and heartbeat refs
# did not), pick one, and force-take its claims with the *same* atomic
# --force-with-lease push as a stale reclaim, only skipping the staleness gate.
# Still atomic against a not-actually-dead fleet — the second pusher is rejected.
# The lease is untouched (ADR-0011 rejected shortening the TTL for a rare event),
# and a takeover never counts as a retry: it answers "did the FLEET die?", not
# "is this WORK failing?".

def group_instances(claims, heartbeats, me, now, ttl):
    """
    Every fleet instance discoverable in fleet state, with what it holds and how
    stale its lease is — the takeover picker's input (`afk takeover --list`).

      claims:     [{"number","instance","host","sha"}...] (the ref scan)
      heartbeats: {instance: last_ts}
      me:         my own instance id (marked, never a takeover target)

    Returns rows [{"instance","host","claims","claim_count","heartbeat_ts",
    "heartbeat_age","fresh","is_me"}...], the ones holding most claims first
    (then by id, so the listing is stable). An instance with a heartbeat but no
    claims is still listed — that is a fleet that drained cleanly or is idle, and
    seeing it is how a human tells it apart from the one that died mid-flight. A
    claim whose marker names no instance appears under `instance: null`; it needs
    no takeover, being already reclaimable as stale.
    """
    by = {}
    for c in claims or []:
        inst = c.get("instance")
        row = by.setdefault(inst, {"instance": inst, "host": None, "claims": []})
        row["claims"].append(c.get("number"))
        row["host"] = row["host"] or c.get("host")
    for inst in (heartbeats or {}):
        by.setdefault(inst, {"instance": inst, "host": None, "claims": []})

    out = []
    for inst, row in by.items():
        ts = (heartbeats or {}).get(inst)
        nums = sorted(n for n in row["claims"] if n is not None)
        out.append({"instance": inst, "host": row["host"], "claims": nums,
                    "claim_count": len(nums),
                    "heartbeat_ts": ts,
                    "heartbeat_age": None if ts is None else int(now) - int(ts),
                    "fresh": ts is not None and not is_stale(ts, now, ttl),
                    "is_me": inst is not None and inst == me})
    out.sort(key=lambda r: (-r["claim_count"], r["instance"] is None, str(r["instance"])))
    return out


def plan_takeover(claims, heartbeats, target, me, now, ttl, confirmed=False):
    """
    Whether `afk takeover --instance <target>` may proceed, and over which claims.
    Pure: the effectful layer scans the refs, this decides, then it pushes.

    Returns {"action", "instance", "claims", "fresh", "detail"} where `claims` is
    [{"number","sha","host"}...] (the sha each force-with-lease push needs):

      take     go — force-take these claims, skipping the staleness gate.
      confirm  the target's heartbeat is still FRESH: taking it steals live work if
               the human is wrong, so an explicit confirmation is required first.
               Surfaced as a warning rather than a refusal — the human may
               legitimately know better (a wedged process, a heartbeat written by a
               now-dead tick), and the push stays atomic underneath (ADR-0011).
      none     nothing to take: no such instance, or it holds no claims.
      error    the target is THIS fleet — its claims are already mine.
    """
    rows = sorted(({"number": c.get("number"), "sha": c.get("sha"), "host": c.get("host")}
                   for c in claims or [] if c.get("instance") == target),
                  key=lambda r: (r["number"] is None, r["number"]))
    ts = (heartbeats or {}).get(target)
    fresh = ts is not None and not is_stale(ts, now, ttl)
    known = ts is not None or bool(rows)

    def out(action, detail):
        return {"action": action, "instance": target, "claims": rows,
                "fresh": fresh, "heartbeat_age": None if ts is None else int(now) - int(ts),
                "detail": detail}

    if target is not None and target == me:
        return out("error", "that is this fleet's own instance id — its claims are already mine")
    if not rows:
        return out("none", "no instance by that id holds any claim" if not known
                           else "that instance holds no claims (nothing to take)")
    if fresh and not confirmed:
        return out("confirm",
                   f"that fleet's heartbeat is only {int(now) - int(ts)}s old (lease {int(ttl)}s) — "
                   f"it looks ALIVE; forcing a takeover steals its live work if you are wrong")
    return out("take", f"taking {len(rows)} claim(s) from {target}"
                       + (" (fresh heartbeat, human-confirmed)" if fresh else ""))


# --------------------------------------------------------------------------- #
# Continuation — recovering a dead claim FROM ITS PROGRESS (ADR-0011)          #
# --------------------------------------------------------------------------- #
#
# When a claim's worker has died — an *orphaned claim* reconciled locally, or a
# *stale claim* reclaimed from a peer — the fleet used to tear the worktree down
# and re-dispatch from a fresh base, discarding every bit of partial work. The
# default is now CONTINUATION: recover the claim from its durable progress,
# tiered by what actually survived the death. The selection below is pure
# mechanics — deterministic from two signals the effectful layer gathers (is a
# worktree for this issue still on THIS machine, and is its branch ahead of base
# on the remote) — while "is this recovered state sane to build on" stays tick
# judgment, exactly like the orphan-vs-alive read.
#
# Only tier 3 tears anything down; that is the whole point of ADR-0011.

_PLACEHOLDER_RE = re.compile(r"\{(number|slug)\}")


def branch_regex(branch_pattern, number):
    """
    `branch_pattern` + an issue number → the regex that matches the branch orca
    ACTUALLY created for it. Two things are wildcards, by construction: orca
    prefixes the branch with `<user>/` (ADR-0005 — the fleet reads the name back
    rather than dictating it), and the slug is whatever the dispatching tick
    passed. The number is not: it is the one field that identifies the issue.
    """
    out, pos = [], 0
    pattern = branch_pattern or ""
    for m in _PLACEHOLDER_RE.finditer(pattern):
        out.append(re.escape(pattern[pos:m.start()]))
        out.append(str(number) if m.group(1) == "number" else "[^/]*")
        pos = m.end()
    out.append(re.escape(pattern[pos:]))
    return re.compile(r"^(?:[^/]+/)?" + "".join(out) + r"$")


def branch_candidates(heads, branch_pattern, number):
    """The remote branch names that could be issue <number>'s work branch, sorted.
    Used when NO local worktree survived: the claim ref records the issue, not the
    branch, so tier 2 has to recognise the branch by its name."""
    rx = branch_regex(branch_pattern, number)
    return sorted(h for h in (heads or []) if h and rx.match(h))


def find_orca_worktree(worktrees, number, repo=None):
    """
    The orca worktree belonging to issue <number> on THIS machine, from
    `orca worktree list --json`'s `result.worktrees` rows — the tier-1 signal.
    Pure so the shape of orca's JSON is fixture-pinned rather than re-derived.

      worktrees: rows carrying {linkedIssue, path, branch, projectId,
                 isMainWorktree, isArchived, lastActivityAt}
      repo:      "owner/name" — when given, a row must belong to it (orca's
                 `projectId` is `github:owner/name`), so a same-numbered issue in
                 another repo's worktree is never mistaken for this one.

    Returns {"found": bool, "path": str|None, "branch": str|None}. Several
    matches (a stale leftover plus a live one) → the most recently active.
    """
    hits = []
    for w in worktrees or []:
        # `linkedIssue` is an int in orca's JSON today; compared numerically anyway,
        # because a str/int drift at this boundary would silently downgrade every
        # tier-1 recovery to tier 2/3 — i.e. quietly lose the uncommitted work that
        # tier 1 exists to save.
        try:
            if int(w.get("linkedIssue")) != int(number):
                continue
        except (TypeError, ValueError):
            continue
        if w.get("isMainWorktree") or w.get("isArchived"):
            continue
        if repo and (w.get("projectId") or "") not in (f"github:{repo}", repo):
            continue
        hits.append(w)
    if not hits:
        return {"found": False, "path": None, "branch": None}
    best = max(hits, key=lambda w: int(w.get("lastActivityAt") or 0))
    branch = best.get("branch") or None
    if branch and branch.startswith("refs/heads/"):
        branch = branch[len("refs/heads/"):]
    return {"found": True, "path": best.get("path") or None, "branch": branch}


RECOVERY_ACTIONS = ("reuse_worktree", "recreate_at_tip", "dispatch_fresh")


def select_recovery(worktree, branch):
    """
    How to recover ONE claim whose worker has died — the continue-vs-fresh
    selection of ADR-0011, tiered by what survived. Pure: `afk recovery` gathers
    the two signals, this decides, and a fixture pins every tier.

      worktree: this machine's worktree for the issue (None / {} if there is none)
                {"present": bool, "commits_ahead": int|None, "dirty": bool}
      branch:   the issue's branch on the remote (None / {} if unknown)
                {"name": str|None, "commits_ahead": int|None}   (ahead of base)

    Returns {"tier", "action", "prompt", "reason"}:
      1  reuse_worktree   a worktree is still HERE → spawn the new worker inside
                          it, on the same branch, and do NOT `orca worktree rm`
                          it. Lossless: it carries even uncommitted work.
      2  recreate_at_tip  no local worktree, but the dead worker pushed → recreate
                          one at the branch tip and continue there. Loss is
                          bounded to "since the last push".
      3  dispatch_fresh   nothing survived → the old behaviour, re-dispatch from
                          base. The ONLY tier that tears down.

    `prompt` picks the worker-prompt variant: `continue` (inspect the existing
    progress first) or `fresh`. A surviving worktree that is *provably* pristine
    — zero commits ahead, a clean tree, and nothing pushed on its branch either —
    gets the fresh prompt: there is nothing to continue, and telling a worker
    otherwise sends it looking for work that isn't there. Unreadable progress
    (`commits_ahead: None`) is NOT pristine: we never hand out a fresh prompt over
    a worktree we could not read.
    """
    wt, br = worktree or {}, branch or {}
    pushed = int(br.get("commits_ahead") or 0)
    if wt.get("present"):
        ahead = wt.get("commits_ahead")
        known = ahead is not None
        pristine = known and int(ahead) == 0 and not bool(wt.get("dirty")) and pushed == 0
        if pristine:
            reason = "local worktree present but pristine — reuse it, nothing to continue"
        elif not known:
            reason = "local worktree present, progress unreadable — reuse it and inspect"
        elif int(ahead) == 0 and not wt.get("dirty"):
            reason = (f"local worktree present and clean, but its branch carries {pushed} "
                      f"pushed commit(s) ahead of base — reuse it and continue from them")
        else:
            reason = (f"local worktree present with {int(ahead)} commit(s) ahead"
                      + (" and uncommitted changes" if wt.get("dirty") else ""))
        return {"tier": 1, "action": "reuse_worktree",
                "prompt": "fresh" if pristine else "continue", "reason": reason}

    name, ahead = br.get("name"), br.get("commits_ahead")
    if name and ahead is not None and int(ahead) > 0:
        return {"tier": 2, "action": "recreate_at_tip", "prompt": "continue",
                "reason": f"no local worktree; branch {name} is {int(ahead)} commit(s) "
                          f"ahead of base — recreate at its tip"}
    return {"tier": 3, "action": "dispatch_fresh", "prompt": "fresh",
            "reason": "no local worktree and nothing pushed ahead of base — "
                      "re-dispatch from base"}


# --------------------------------------------------------------------------- #
# Human-facing progress status board — render only (ADR-0006)                  #
# --------------------------------------------------------------------------- #
#
# The fleet's machine state (claim ref + PR + checks + afk-attempt label) is the
# single source of truth for *decisions*. But a human reading the issue can't see
# the claim — it lives in the hidden refs/afk/* namespace — and the assignee is
# unused, so the whole "claimed, worker coding, no PR yet" phase is invisible on
# the issue surface. The status board projects that lifecycle onto the issue as
# ONE comment the owning tick upserts each rebuild. It is a *rendering* of state
# the tick already derived — never a second source of truth, never read back by a
# tick. Rendered as a GitHub task list so the issue shows a progress meter.
#
# Idempotent by construction: the body carries NO wall-clock time, so identical
# lifecycle state renders identical text; the effectful layer then writes only
# when the text actually changed, and re-entrant/disposable ticks never spam.

STATUS_MARKER = "<!--afk:status-->"

# The closed set of lifecycle phases the board renders. Happy path plus two
# off-ramps (ci_failed, escalated) that reuse the same checkboxes + an annotation.
STATUS_PHASES = ("claimed", "pr_open", "ci_failed", "awaiting_merge", "merged", "escalated")

# Happy-path milestones, in order — these are the task-list checkboxes.
_STATUS_STEPS = (
    ("claimed",        "已认领 · worker 实现中"),
    ("pr_open",        "PR 已开 · 等 CI"),
    ("awaiting_merge", "门已绿 · 待合并"),
    ("merged",         "已合并"),
)

# How far along the happy path each phase has reached (index of the last DONE
# step). escalated is a terminal give-up handled specially in `render_status_board`.
_PHASE_REACHED = {
    "claimed": 0, "pr_open": 1, "ci_failed": 1, "awaiting_merge": 2, "merged": 3,
    "escalated": 1,
}


def _status_current_line(phase, attempt, retry_max):
    """The single ▸/✅/⚠️ 'where are we now' line under the checklist."""
    if phase == "claimed":
        return "▸ 当前:worker 实现中,尚无 PR"
    if phase == "pr_open":
        return "▸ 当前:等 CI"
    if phase == "ci_failed":
        return f"▸ 当前:CI 失败,修复重试中({attempt}/{retry_max}) —— 见下方 CI 与评论"
    if phase == "awaiting_merge":
        return "▸ 当前:门已绿,待合并"
    if phase == "merged":
        return "✅ 已合并,完成"
    return "⚠️ 已升级给人处理 —— 见下方评论"   # escalated


def render_status_board(state):
    """
    Render the human-facing progress *status board* comment body. Pure: a function
    of the discrete lifecycle state the tick already derived from fleet state; no
    I/O and no clock, so identical state → identical body (this is what lets the
    upsert write only when it changed, and keeps re-entrant ticks from spamming).
    Human-read only — never parsed back as a source of truth.

      state = {
        "phase":     one of STATUS_PHASES (required),
        "instance":  owning fleet-instance id (str, optional — shown in the header),
        "pr":        PR number (int) or None,
        "attempt":   current attempt n (int, default 0)  — shown only for ci_failed,
        "retry_max": max retries (int, default 2)         — shown only for ci_failed,
      }
    Returns the full markdown body, led by STATUS_MARKER (the find-or-create anchor).
    """
    phase = state.get("phase")
    if phase not in STATUS_PHASES:
        raise ValueError(f"unknown status phase: {phase!r}")
    pr = state.get("pr")
    attempt = int(state.get("attempt", 0) or 0)
    retry_max = int(state.get("retry_max", 2) or 0)
    inst = state.get("instance")
    reached = _PHASE_REACHED[phase]
    escalated = phase == "escalated"

    def done(i, key):
        if escalated:              # terminal give-up: only what truly happened stays ticked
            return i == 0 or (key == "pr_open" and bool(pr))
        return i <= reached

    header = "**afk-fleet 进度**" + (f" · 认领方 `{inst}`" if inst else "")
    lines = [STATUS_MARKER, header, ""]
    for i, (key, label) in enumerate(_STATUS_STEPS):
        if key == "pr_open" and pr:
            label = f"PR 已开 (#{pr}) · 等 CI"
        lines.append(f"- [{'x' if done(i, key) else ' '}] {label}")
    lines.append("")
    lines.append(_status_current_line(phase, attempt, retry_max))
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Retry accounting + launcher pacing                                          #
# --------------------------------------------------------------------------- #

def next_attempt(attempt_labels, retry_max):
    """
    Retry-or-escalate from the `afk-attempt/<n>` labels on a failed issue. The
    current attempt is the max n across those labels (none → 0).

      {"action":"retry","from_label":<cur|None>,"to_label":"afk-attempt/<n+1>"}
      {"action":"escalate","from_label":<cur|None>}   when n >= retry_max
    """
    n, cur = 0, None
    for lb in attempt_labels or []:
        if isinstance(lb, str) and lb.startswith("afk-attempt/"):
            try:
                v = int(lb.split("/", 1)[1])
            except ValueError:
                continue
            if v >= n:
                n, cur = v, lb
    if n >= retry_max:
        return {"action": "escalate", "from_label": cur}
    return {"action": "retry", "from_label": cur, "to_label": f"afk-attempt/{n + 1}"}


def pace(summary, config):
    """
    Next launcher sleep in seconds, from the last tick's summary + config.

      summary: {"merged":[],"dispatched":[],"reclaimed":[],"in_flight":int,
                "empty_streak":int}   (empty_streak: consecutive empty ticks so far)
      config:  {"busy_interval_seconds","idle_interval_seconds",
                "idle_ticks_before_sleep","claim_lease_ttl_seconds"}

    - did work (merged/dispatched/reclaimed) or in_flight>0 → busy interval;
    - else stay busy until `idle_ticks_before_sleep` empty ticks, then idle interval;
    - HARD CAP: while holding any claim (in_flight>0), never exceed ttl/2, so the
      per-instance heartbeat cannot lapse and get a live claim reclaimed (ADR-0003).

    `config` may be partial — it is resolved through CONFIG_DEFAULTS, so an
    omitted key defaults rather than crashing, and the ttl/2 cap can never be
    silently disabled by a missing key (ADR-0009).
    """
    config = resolve_config(config)
    busy = int(config["busy_interval_seconds"])
    idle = int(config["idle_interval_seconds"])
    threshold = int(config["idle_ticks_before_sleep"])
    ttl = int(config["claim_lease_ttl_seconds"])

    did_work = bool(summary.get("merged") or summary.get("dispatched") or summary.get("reclaimed"))
    in_flight = int(summary.get("in_flight", 0))
    empty_streak = int(summary.get("empty_streak", 0))

    if did_work or in_flight > 0:
        interval = busy
    elif empty_streak >= threshold:
        interval = idle
    else:
        interval = busy  # recently active — stay responsive for stragglers

    if in_flight > 0:
        interval = min(interval, ttl // 2)
    return int(interval)


# --------------------------------------------------------------------------- #
# Worker launch command — launcher/worker provider parity (ADR-0010)           #
# --------------------------------------------------------------------------- #
#
# A launcher started through a provider wrapper (`ckimi`, `csk`, a direnv, a
# script) carries that provider only in its ENV — the wrapper's NAME is gone by
# the time the process exists, and its argv is byte-identical to a stock
# `claude`. A worker orca starts in a fresh login shell inherits none of that
# env, so it silently falls back to stock Anthropic and runs that way,
# unattended, for days.
#
# What travels is therefore an OPAQUE command string the fleet never parses and
# never composes — supplied by the human at the bootstrap gate they are already
# standing at. That choice is what keeps the credential out of the fleet
# entirely: no env is copied, nothing is written to disk, no argv carries a key,
# and the fleet is coupled to no particular secret manager. What code *can*
# settle, it does: whether to ask at all (a stock launcher is never asked), which
# wrappers exist to offer, and whether the answer resolves to something runnable.

WORKER_COMMAND_DEFAULT = "claude --dangerously-skip-permissions"
WORKER_COMMAND_DEFAULT_QODERCN = "qoderclicn --dangerously-skip-permissions"

# orca's per-agent unattended flags. Used ONLY to warn: a worker started without
# one parks on a permission prompt forever, and the fleet reads that as a silent
# no-PR claim. Checked against the RESOLVED text (an alias hides its own flags).
YOLO_FLAGS = ("--dangerously-skip-permissions", "--dangerously-bypass-approvals-and-sandbox",
              "--yolo", "--yes-always", "--dangerously-allow-all", "--trust-all-tools",
              "--unrestricted", "--auto-approve")


def detect_runtime(env=None):
    """The agent runtime this launcher runs under — 'qoderclicn' or 'claude'.
    One fleet instance runs one runtime (ADR-0014). Detection is from the
    launcher's own environment: qoderclicn sets QODERCN_CLI=1 in every child
    process; its absence means Claude (the default)."""
    e = env if env is not None else os.environ
    if (e.get("QODERCN_CLI") or "").strip() in ("1", "true"):
        return "qoderclicn"
    return "claude"

_ALIAS_RE = re.compile(r"^(?:alias\s+)?([^=\s]+)=(.*)$")


def first_word(command):
    """The token whose resolvability decides whether the command can run at all."""
    parts = (command or "").strip().split()
    return parts[0] if parts else ""


def parse_aliases(text):
    """Shell `alias` output → {name: expansion}. Accepts both zsh's `n='v'` and
    bash's `alias n='v'`. Quotes are stripped; the expansion is only ever shown
    to a human or substring-searched, never executed by the fleet."""
    out = {}
    for line in (text or "").splitlines():
        m = _ALIAS_RE.match(line.strip())
        if not m:
            continue
        name, val = m.group(1), m.group(2).strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "'\"":
            val = val[1:-1]
        out[name] = val
    return out


def launch_candidates(aliases):
    """The shell aliases that start a Claude Code, as {name, expansion, wraps_env}
    — what the bootstrap offers the human. `wraps_env` marks the ones that do more
    than run `claude` bare, i.e. the ones that could carry a provider; a stock
    launcher's own alias is listed too, and marked False, rather than guessed at.
    Deliberately NOT a mapping from the launcher's provider to one alias: inferring
    that means executing each wrapper's env prefix, which would decrypt every
    provider's credentials to answer a question a human answers in one word."""
    out = []
    for name, exp in sorted((aliases or {}).items()):
        if "claude" not in exp:
            continue
        bare = exp.split()[0] == "claude" if exp.split() else False
        out.append({"name": name, "expansion": exp, "wraps_env": not bare})
    return out


def resolve_worker_command(base_url, supplied=None, resolved=None):
    """
    Settle the one string every worker is started with.

      base_url: the launcher's own ANTHROPIC_BASE_URL (None/"" = stock Anthropic).
      supplied: the human's answer, verbatim, or None if they haven't been asked.
      resolved: what the shell says `first_word(supplied)` is — the `type` output
                (an alias's full expansion, a function body, a path), or None if
                it resolves to nothing. Only meaningful when `supplied` is given.

    Returns {status, command, base_url, first_word, yolo, detail}. `command` is
    non-null only when the run may proceed — every other status is the launcher's
    cue to ask (again), never to quietly fall back to stock.

      stock       no custom provider and nothing supplied → the default command,
                  and the human is not asked at all.
      confirmed   a supplied command whose first word resolves → use it verbatim.
      unresolved  a supplied command whose first word resolves to nothing (the
                  typo case: `ckim`). Left unchecked this starts no worker, so the
                  claim goes PR-less into the retry ladder and escalates — three
                  issues burnt on a missing letter.
      ask         a custom provider and no answer yet.

    `yolo` is advisory, not a gate: True if an unattended flag is visible in the
    resolved text, False if it plainly is not, None when the resolution can't show
    it (a script path). A False is worth a warning — a worker that stops on a
    permission prompt is indistinguishable to the fleet from one that finished.
    """
    fw = first_word(supplied) if supplied else ""

    def out(status, command=None, yolo=None, detail=""):
        return {"status": status, "command": command, "base_url": base_url or None,
                "first_word": fw or None, "yolo": yolo, "detail": detail}

    if supplied:
        if not resolved:
            return out("unresolved", detail=f"{fw!r} resolves to nothing in an interactive shell")
        # An alias resolution shows its whole expansion and `claude` is its own
        # whole story, so a missing flag there is a fact. A path or a function in
        # another file could carry the flag inside — that is unknown, not absent.
        seen_whole = " is an alias for " in resolved or fw == "claude"
        visible = f"{supplied}\n{resolved}"
        yolo = True if any(f in visible for f in YOLO_FLAGS) else (False if seen_whole else None)
        return out("confirmed", command=supplied.strip(), yolo=yolo, detail=resolved.strip())
    if not (base_url or "").strip():
        return out("stock", command=WORKER_COMMAND_DEFAULT, yolo=True)
    return out("ask", detail=f"launcher is on a custom provider ({base_url})")


# --------------------------------------------------------------------------- #
# Fingerprint gate — skip ticks code can prove are no-ops (ADR-0007)           #
# --------------------------------------------------------------------------- #
#
# A tick is a fresh LLM context; spawning one just to conclude "still waiting"
# is the fleet's main steady-state token spend. The gate collapses everything a
# tick's Rebuild observes into a short digest; the launcher spawns a tick only
# when the digest moved (or a forced full pass is due). A false "changed" costs
# one tick — today's behaviour; a missed change waits at most `force_after`
# cycles. Correctness never depends on the gate.

def fingerprint(issues, prs, claims):
    """
    Digest the observable fleet inputs — open issues (number + labels +
    updatedAt, so label churn, closes, and fresh blocker comments all move it),
    open PRs (number + head sha + updatedAt + per-check status/conclusion, so
    pushes and CI finishing move it), and claim refs (number + sha, so peer
    claims/releases/reclaims move it).

    Heartbeats are deliberately NOT an input: the launcher refreshes its own
    lease on skipped cycles, which would move the digest every cycle and defeat
    the gate — and a lease *expiring* is a time-driven event no state hash can
    see anyway. The forced tick covers those.

    Accepts gh-shaped or fixture-shaped rows; canonicalizes (sorts, keeps only
    the fields above) so row order and representation never move the digest.
    Returns a 16-hex digest.
    """
    def check_row(c):
        return [c.get("name") or c.get("context") or "",
                c.get("status") or "",
                c.get("conclusion") or c.get("state") or ""]

    canon = {
        "issues": sorted([i.get("number"), sorted(_label_names(i)), i.get("updatedAt") or ""]
                         for i in issues),
        "prs": sorted([p.get("number"), p.get("headRefOid") or "", p.get("updatedAt") or "",
                       sorted(check_row(c) for c in (p.get("statusCheckRollup") or []))]
                      for p in prs),
        "claims": sorted([c.get("number"), c.get("sha") or ""] for c in claims),
    }
    blob = json.dumps(canon, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def fingerprint_gate(last, current, skips, force_after):
    """
    Skip-or-tick verdict for one launcher cycle.

      last:        the previous cycle's digest ("" / None on the first cycle)
      current:     the digest just computed
      skips:       consecutive skipped cycles so far
      force_after: run a full tick at least every N skips (>= 1; 1 disables
                   skipping entirely)

    Returns {"action": "tick"|"skip", "reason": "first"|"changed"|"forced"|
    "unchanged", "skips": <new streak>} — the launcher carries `skips` (and the
    digest) forward, exactly like the last tick summary.
    """
    if not last:
        return {"action": "tick", "reason": "first", "skips": 0}
    if current != last:
        return {"action": "tick", "reason": "changed", "skips": 0}
    if skips + 1 >= int(force_after):
        return {"action": "tick", "reason": "forced", "skips": 0}
    return {"action": "skip", "reason": "unchanged", "skips": skips + 1}


# --------------------------------------------------------------------------- #
# Working-set assembly — the Rebuild's deterministic half (ADR-0008)           #
# --------------------------------------------------------------------------- #
#
# One pure function turns the raw observables (issues, PRs, claim/heartbeat
# refs, open-blocker counts) into the tick's whole working set — the join the
# SKILL.md prose used to make every fresh tick re-derive (graft three fields,
# match each claim to its PR, partition mine/peer_live/stale). The gh/git
# gather lives in afk.py; the orphan-vs-alive read of a `no_pr` claim (the
# liveness probe) is deliberately NOT here — that is tick judgment.

_CHECK_RED = {"FAILURE", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED",
              "STARTUP_FAILURE", "ERROR"}
_CHECK_OK = {"SUCCESS", "NEUTRAL", "SKIPPED"}


def pr_checks_state(rollup):
    """Collapse a gh statusCheckRollup into "green" | "red" | "pending" | None.
    None = no checks at all — the progressive gate's "no CI yet" case, which the
    tick judges. Accepts CheckRun rows (status/conclusion) and StatusContext
    rows (state). Any red conclusion wins; anything not conclusively ok
    (running, PENDING, STALE, unknown) holds the verdict at pending."""
    if not rollup:
        return None
    state = "green"
    for c in rollup:
        concl = (c.get("conclusion") or c.get("state") or "").upper()
        if concl in _CHECK_RED:
            return "red"
        if concl not in _CHECK_OK:
            state = "pending"
    return state


def _closing_pr_map(prs):
    """issue number → the open PR that closes it. When several do, the highest
    PR number wins — the latest attempt is the live one."""
    m = {}
    for p in prs:
        for ref in p.get("closingIssuesReferences") or []:
            n = ref.get("number") if isinstance(ref, dict) else ref
            if n is None:
                continue
            cur = m.get(n)
            if cur is None or (p.get("number") or 0) > (cur.get("number") or 0):
                m[n] = p
    return m


def assemble_working_set(issues, prs, claims, heartbeats, blocked_by, me, now, ttl,
                         ready_label, epic_labels, ci_mode="required"):
    """
    The tick's whole working set from the raw observables. Pure — afk.py's
    `rebuild` gathers, this assembles, and a fixture pins the join.

      issues:      gh issue list rows (number, title, labels, updatedAt)
      prs:         gh pr list rows (number, headRefOid, updatedAt,
                   statusCheckRollup, closingIssuesReferences)
      claims:      [{"number","instance","sha",...}]  (ref-scan shape)
      heartbeats:  {instance: last_ts}
      blocked_by:  {issue number: open blocker count}; missing → 0. Only
                   frontier candidates need real counts — every other issue
                   already fails a cheaper eligibility check first.
      me/now/ttl:  as classify_claims
      ready_label/epic_labels: the dispatch contract
      ci_mode:     gate.ci — in `local` mode an open PR is awaiting_merge outright,
                   since the gate is a merge-time action, not an observation
                   (ADR-0012)

    Returns:
      {"frontier": {"dispatch": [{"number","title"}...], "excluded": [...]},
       "mine": [{"number","title","status","pr","checks","attempt_labels"}...],
       "peer_live": [{"number","instance"}...],
       "stale": [{"number","instance","sha"}...],   # sha feeds reclaim --expect-sha
       "fingerprint": <digest of the same observables the gate hashes>,
       "now": now}

    `status` is subclassify_pr's verdict (awaiting_merge / awaiting_ci /
    failure / no_pr); whether a no_pr claim is an orphan needs the liveness
    probe and stays with the tick.
    """
    by_num = {i.get("number"): i for i in issues}
    claimed = {c.get("number") for c in claims}
    pr_for = _closing_pr_map(prs)

    enriched = [{**i,
                 "claimed": i.get("number") in claimed,
                 "has_open_pr": i.get("number") in pr_for,
                 "open_blockers": int(blocked_by.get(i.get("number"), 0))}
                for i in issues]
    frontier = select_frontier(enriched, ready_label, epic_labels)
    frontier["dispatch"] = [{"number": n, "title": by_num.get(n, {}).get("title")}
                            for n in frontier["dispatch"]]

    part = classify_claims(claims, heartbeats, me, now, ttl)
    by_claim = {c.get("number"): c for c in claims}

    mine = []
    for n in part["mine"]:
        pr = pr_for.get(n)
        checks = pr_checks_state(pr.get("statusCheckRollup")) if pr else None
        issue = by_num.get(n, {})
        mine.append({"number": n, "title": issue.get("title"),
                     "status": subclassify_pr("open" if pr else "none", checks, ci_mode),
                     "pr": pr.get("number") if pr else None, "checks": checks,
                     "attempt_labels": [lb for lb in _label_names(issue)
                                        if lb.startswith("afk-attempt/")]})

    return {"frontier": frontier,
            "mine": mine,
            "peer_live": [{"number": n, "instance": by_claim.get(n, {}).get("instance")}
                          for n in part["peer_live"]],
            "stale": [{"number": n, "instance": by_claim.get(n, {}).get("instance"),
                       "sha": by_claim.get(n, {}).get("sha")}
                      for n in part["stale"]],
            "fingerprint": fingerprint(issues, prs, claims),
            "now": now}
