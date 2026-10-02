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
    "claim_namespace": "refs/afk",
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


# The two places claim + heartbeat refs can live, as namespace → (claim ref prefix,
# heartbeat ref prefix). `refs/afk` is hidden from branch listings and `on: push`
# CI; `refs/heads` is the fallback for a remote whose rules forbid non-branch refs,
# where the same markers are ordinary `afk-claim/*` / `afk-heartbeat/*` branches
# (ADR-0003). A closed set: any other prefix would be a third layout no probe,
# warning or doc describes.
CLAIM_NAMESPACES = {
    "refs/afk": ("refs/afk/claim", "refs/afk/heartbeat"),
    "refs/heads": ("refs/heads/afk-claim", "refs/heads/afk-heartbeat"),
}
BRANCH_NAMESPACE = "refs/heads"

# The completion gate's two modes (ADR-0012). `required` waits for the PR's GitHub
# checks; `local` never reads them and makes `gate.local_command` the gate, re-run
# at merge time against the exact tree that lands.
GATE_CI_MODES = ("required", "local")

# How `afk merge` lands a PR — each is a `gh pr merge` flag of the same name.
MERGE_STRATEGIES = ("squash", "merge", "rebase")

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
    every time one is resolved — first at load time (`afk config`), at bootstrap,
    with the human present, where a bad combination can still be fixed; and again
    on every subcommand, so nothing downstream ever sees an invalid config.
    Raises ValueError; returns `cfg` unchanged so it can be used inline.
    """
    ns = cfg.get("claim_namespace")
    if ns not in CLAIM_NAMESPACES:
        raise ValueError(f"config claim_namespace: expected one of "
                         f"{' | '.join(CLAIM_NAMESPACES)}, got {ns!r}")
    gate = cfg.get("gate") or {}
    ci = gate.get("ci")
    if ci not in GATE_CI_MODES:
        raise ValueError(f"config gate.ci: expected one of "
                         f"{' | '.join(GATE_CI_MODES)}, got {ci!r}")
    if ci == "local" and not (gate.get("local_command") or "").strip():
        raise ValueError("config gate.ci: 'local' requires a non-empty gate.local_command — in "
                         "local mode that command IS the completion gate (ADR-0012), so an empty "
                         "one would merge every PR unverified")
    strategy = (cfg.get("merge") or {}).get("strategy")
    if strategy not in MERGE_STRATEGIES:
        raise ValueError(f"config merge.strategy: expected one of "
                         f"{' | '.join(MERGE_STRATEGIES)}, got {strategy!r}")
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


def override_config(cfg, assignments):
    """Lay `key=value` overrides (the CLI's `--set`) onto a canonical config, in
    place, and return it. Keys are the config file's own — dotted for a section
    (`gate.ci=local`) — and values are typed by the key's default exactly as the
    file's are, except that a string is taken verbatim (the shell already
    unquoted it). An unknown key, or an item with no `=`, raises ValueError."""
    for item in assignments or []:
        dotted, eq, raw = item.partition("=")
        section, _, key = dotted.strip().rpartition(".")
        table = CONFIG_DEFAULTS.get(section) if section else CONFIG_DEFAULTS
        if not eq or not isinstance(table, dict) or isinstance(table.get(key), (dict, type(None))):
            raise ValueError(f"--set: expected <config key>=<value>, got {item!r}")
        default = table[key]
        value = raw if isinstance(default, str) else _coerce(dotted.strip(), raw.strip(), default)
        (cfg[section] if section else cfg)[key] = value
    return cfg

# --------------------------------------------------------------------------- #
# Dispatch eligibility — "can a worker take this issue right now?"             #
# --------------------------------------------------------------------------- #
#
# Issues arrive from afk.py's gatherer as OPEN issues with `labels` already a
# list of names; the three eligibility facts that are not on the issue itself
# (claimed / has_open_pr / open_blockers) are grafted by `_eligibility_rows`.

def _eligibility_rows(issues, prs, claims, blocked_by):
    """Each issue + the three eligibility facts `select_frontier` reads:
    `claimed` (a claim ref exists, any owner — not the assignee, ADR-0003),
    `has_open_pr` (an open PR closes it — the open-PR guard) and
    `open_blockers` (from `blocked_by`, {issue number: count}; missing → 0)."""
    claimed = {c.get("number") for c in claims}
    pr_for = _closing_pr_map(prs)
    return [{**i,
             "claimed": i.get("number") in claimed,
             "has_open_pr": i.get("number") in pr_for,
             "open_blockers": int(blocked_by.get(i.get("number"), 0))}
            for i in issues]


def select_frontier(issues, ready_label, epic_labels):
    """
    Decide which issues are dispatchable RIGHT NOW. Shared by `--plan` and the live
    tick, so both compute the identical frontier (a stable published contract).

    `issues` are `_eligibility_rows`. One is dispatchable iff ALL hold: has
    ready_label · no epic label · not claimed · no open linked PR · zero open
    blocking dependencies.

    Returns {"dispatch": [num...], "excluded": [{"number","reason"}...]}.
    """
    epic_set = {e.strip() for e in epic_labels if e.strip()}
    dispatch, excluded = [], []
    for issue in issues:
        num = issue.get("number")
        labels = set(issue.get("labels") or [])
        hit_epic = labels & epic_set
        if ready_label not in labels:
            reason = f"no {ready_label} label"
        elif hit_epic:
            reason = f"epic label ({', '.join(sorted(hit_epic))})"
        elif issue.get("claimed"):
            reason = "already claimed (afk-claim ref exists)"
        elif issue.get("has_open_pr"):
            reason = "has an open linked PR"
        elif issue.get("open_blockers"):
            reason = f"{issue['open_blockers']} open blocker(s)"
        else:
            dispatch.append(num)
            continue
        excluded.append({"number": num, "reason": reason})
    return {"dispatch": dispatch, "excluded": excluded}


def frontier_candidates(issues, prs, claims, ready_label, epic_labels):
    """The issue numbers that pass every eligibility check EXCEPT open blockers —
    the only ones whose blocker count is worth a per-issue API read. `afk rebuild`
    fetches counts for exactly these, then `assemble_working_set` decides."""
    return select_frontier(_eligibility_rows(issues, prs, claims, {}),
                           ready_label, epic_labels)["dispatch"]


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


def subclassify_pr(has_pr, checks_state, ci_mode, closed=False):
    """
    Classify one of MY in-flight claims from its PR + checks → `(status,
    board_phase)`: what the tick does next, and what the status board shows a human
    meanwhile. Decided together because `gate.ci` bends both, in different ways.

      has_pr:       an open PR closes the issue
      checks_state: "green" | "red" | "pending" | None  (`pr_checks_state`)
      ci_mode:      gate.ci — "required" reads the checks; "local" never does
      closed:       the claimed issue is itself CLOSED — a merge whose tick died
                    before releasing, or a human finishing it by hand

      status           the tick…                                board_phase
      closed           releases the leftover claim               None (not re-rendered)
      no_pr            asks `afk no-pr` why                      claimed
      awaiting_ci      leaves it                                 pr_open
      failure          runs `afk fail`                           ci_failed
      awaiting_merge   runs `afk merge`                          awaiting_merge
                       (`local`: an open PR, whatever its        (`local`: pr_open)
                       remote checks say)

    In `local` mode (ADR-0012) there are no checks to wait on: gating is an
    **action the tick takes at merge time** (sync → re-run the local gate → merge),
    not an observation it waits for. So every open PR is `awaiting_merge` — a red
    remote run, the repo's own `on: push` workflow the fleet does not gate on, must
    not park the claim in `failure` forever — while its board stays at `pr_open`:
    the gate that sequence runs has not passed yet, and the board must not show a
    green gate nobody has run. (`merged` / `escalated`, the two terminal board
    phases, are set by the merge and escalate steps themselves.)
    """
    if closed:
        return "closed", None
    if not has_pr:
        return "no_pr", "claimed"
    if ci_mode == "local":
        return "awaiting_merge", "pr_open"
    if checks_state == "green":
        return "awaiting_merge", "awaiting_merge"
    if checks_state == "red":
        return "failure", "ci_failed"
    return "awaiting_ci", "pr_open"


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


def checks_gate(checks_state, pushed, allow_no_checks=False):
    """
    The `gate.ci: required` half of `afk merge`'s gate: may this PR merge on what
    its GitHub checks say, right now?

      checks_state:    `pr_checks_state` of the PR as read BEFORE the merge-time sync
      pushed:          the sync just pushed new commits to the PR — those checks
                       describe a tree that is no longer the one that would land
      allow_no_checks: the tick's judgment that a repo with no CI at all may merge
                       on its acceptance criteria (the progressive gate)

    Returns "green" | "awaiting_ci" | "gate_red" | "no_checks". A sync that moved
    the head always waits: CI must run on the tree that lands, and a later tick's
    merge finds the sync a no-op and reads the fresh verdict.
    """
    if checks_state is None:
        return "green" if allow_no_checks else "no_checks"
    if pushed:
        return "awaiting_ci"
    return {"green": "green", "red": "gate_red"}.get(checks_state, "awaiting_ci")


def gate_comment(verdict, command):
    """The PR comment a red merge-time local gate leaves behind, so the retry's
    worker re-reads the failure from where it lives (ADR-0012)."""
    how = (f"timed out (exit {verdict['exit_code']})" if verdict["timed_out"]
           else f"exit {verdict['exit_code']}")
    omitted = (f"\n\n_({verdict['omitted_lines']} earlier line(s) omitted)_"
               if verdict["omitted_lines"] else "")
    return (f"**afk-fleet merge-time gate: red** — `{command}` → {how}, run after syncing "
            f"with the merge target.\n\n```\n{verdict['excerpt']}\n```{omitted}")


# --------------------------------------------------------------------------- #
# no_pr reconciliation — disambiguating a FINISHED worker from a CODING one    #
# --------------------------------------------------------------------------- #
#
# `subclassify_pr` only says a claim has no PR yet. A worker that finished
# without one and went idle looks identical, to a terminal probe, to one still
# coding, so three signals disambiguate (all gathered by `afk no-pr`):
#   1. git PROGRESS in the worktree (commits ahead / dirty tree / last activity);
#   2. the worker's VERDICT marker on the issue — its declared reason for opening
#      no PR: `already-satisfied` (done in base, empty diff), `blocked` (a
#      dependency gap, see blocked_by) or `giving-up` (a failure it could not fix);
#   3. the terminal's busy / idle / none state from the orca probe.
# `classify_no_pr` is the pure join. Two words are kept apart throughout: the
# VERDICT is what the worker declared in its marker (an input); the OUTCOME is
# what this code concludes from all three signals. Whether to TRUST the marker
# stays the tick's call.

_VERDICT_MARKER_RE = re.compile(r"<!--\s*afk:verdict\b(.*?)-->", re.DOTALL)


def parse_verdict_marker(body):
    """
    Parse the FIRST afk:verdict marker in one comment body → a verdict dict, or
    None if the body carries no marker. The marker (worker-prompt.md, LAYER 1) is:

      <!--afk:verdict n=<issue> phase=<already-satisfied|blocked|giving-up> \
          [blocked_by=<csv of issue numbers>] [reason=<short>]-->

    LENIENT — a marker with a missing/unknown field still parses
    ({"found": True, "phase": None|<raw>}); what an unrecognised phase means is
    `classify_no_pr`'s call. `reason` (if present) must be the last field — it captures to the end
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
    wins). `comments` are [{"body", "url"}...], oldest first (the gh default).
    Returns:
      {"found": bool, "phase": str|None, "blocked_by": [int], "reason": str|None,
       "comment_url": str|None}
    """
    result = {"found": False, "phase": None, "blocked_by": [],
              "reason": None, "comment_url": None}
    for c in comments or []:
        parsed = parse_verdict_marker(c.get("body"))
        if parsed:
            result = {"found": True, "phase": parsed["phase"],
                      "blocked_by": parsed["blocked_by"], "reason": parsed["reason"],
                      "comment_url": c.get("url")}
    return result


def classify_no_pr(progress, terminal, terminal_idle_seconds, worker_verdict, blocker_states,
                   now, grace_seconds, nudged_at=None, can_nudge=True):
    """
    The outcome for one of MY `no_pr` claims, from the raw signals.

      progress:        the worktree's git progress {"commits_ahead", "dirty",
                       "last_commit_ts", "worktree_mtime_ts"}; {} / None if unreadable.
      terminal:        the orca probe — "busy" | "idle" (connected, not working) |
                       "none" (no live worker/terminal at all).
      terminal_idle_seconds: seconds since the terminal last showed activity;
                       None if the probe could not say.
      worker_verdict:  the `latest_verdict` dict (or None) — what the worker declared.
      blocker_states:  {issue number: "open"|"closed"} for the verdict's blocked_by.
                       Anything not provably "closed" counts as still open.
      now, grace_seconds: epoch seconds / `worker_idle_grace_seconds`.
      nudged_at:       epoch seconds this worker was nudged (`afk nudge`), None if
                       it never was. A nudge is spent once: the second silence fails.
      can_nudge:       False when there is nowhere to record a nudge (no worktree
                       on this machine) — the silence then fails at once.

    Returns {"outcome", "action", "idle_seconds", "open_blockers"} — `action` is
    what the tick does, `outcome` the reason it is grouped under:
      coding       leave         — busy, OR last activity within grace.
      idle_done    close_release — idle past grace + `already-satisfied` + NO changes
                                   on the branch: the tick verifies the empty diff,
                                   closes + releases.
      idle_blocked redispatch    — …+ `blocked`, and every blocked_by is now closed.
      idle_blocked escalate      — …+ `blocked`, and a blocked_by is still open (or
                                   the verdict names none, so nothing can ever clear).
      idle_stalled nudge         — …+ NO verdict at all, never nudged: the worker
                                   stopped without an outcome — typically waiting on
                                   a question nobody will answer. `afk nudge` tells
                                   it to carry on; no attempt is spent (ADR-0018).
      idle_failed  next_attempt  — …+ `giving-up`, an unknown phase, an
                                   `already-satisfied` refuted by work on the branch,
                                   or no verdict even after a nudge → failure
                                   handling (`afk fail`).
      dead         orphan        — no live worker/terminal → recovery by continuation.

    `idle_seconds` is the time since the MOST RECENT sign of life (last commit,
    newest file mtime, terminal activity, the nudge — a nudged worker gets a whole
    grace period to answer it); None when none is known, which is never
    "within grace". `open_blockers` is the still-open subset of blocked_by.
    """
    progress = progress or {}
    seen = [t for t in (progress.get("last_commit_ts"), progress.get("worktree_mtime_ts"))
            if t is not None]
    if terminal_idle_seconds is not None:
        seen.append(int(now) - int(terminal_idle_seconds))
    if nudged_at is not None:
        seen.append(int(nudged_at))
    idle_seconds = max(0, int(now) - int(max(seen))) if seen else None

    def out(outcome, action, open_blockers=()):
        return {"outcome": outcome, "action": action, "idle_seconds": idle_seconds,
                "open_blockers": list(open_blockers)}

    # dead first: no worker means it cannot be "coding", whatever it left behind.
    if terminal == "none":
        return out("dead", "orphan")

    # coding: only LIVE signals count. Commits ahead / a dirty tree are standing
    # facts — true until the branch merges — never signs of life (ADR-0013).
    if terminal == "busy" or (idle_seconds is not None and idle_seconds < grace_seconds):
        return out("coding", "leave")

    # idle past grace: route on the declared reason.
    verdict = worker_verdict or {}
    phase = verdict.get("phase") if verdict.get("found") else None
    if phase == "already-satisfied":
        # "nothing needed doing" is refuted by work sitting on the branch.
        has_changes = int(progress.get("commits_ahead") or 0) > 0 or bool(progress.get("dirty"))
        if has_changes:
            return out("idle_failed", "next_attempt")
        return out("idle_done", "close_release")
    if phase == "blocked":
        named = verdict.get("blocked_by") or []
        still_open = [n for n in named if (blocker_states or {}).get(n) != "closed"]
        if still_open or not named:
            return out("idle_blocked", "escalate", still_open)
        return out("idle_blocked", "redispatch")
    if not verdict.get("found") and nudged_at is None and can_nudge:
        return out("idle_stalled", "nudge")
    return out("idle_failed", "next_attempt")


# How much of a stalled worker's screen is carried into a failure reason.
STALL_TAIL_LINES = 30
_STALL_LINE_CHARS = 200


def nudge_text(brief=None):
    """The one line `afk nudge` types at a worker that stopped without an outcome.
    Short on purpose: a long text arrives as a paste the worker asks to have
    confirmed, which is the stall this is sent to break."""
    task = f"your task brief ({brief})" if brief else "your task"
    return (f"You stopped without an outcome. Nobody is watching this terminal, so do not wait "
            f"for a confirmation or an answer: continue {task} to the end, and finish with a PR "
            f"or an afk:verdict marker comment.")


def stall_tail(lines, limit=STALL_TAIL_LINES):
    """The last `limit` non-blank lines of a terminal screen, each cut to a
    bounded width — what a stalled worker was last saying, small enough to carry."""
    kept = [ln.rstrip()[:_STALL_LINE_CHARS] for ln in (lines or []) if str(ln).strip()]
    return kept[-limit:]


def stall_reason(reason, tail):
    """A failure reason with the stalled worker's last screen appended, so the retry
    (or the human it escalates to) reads WHERE it stopped, not just that it did."""
    tail = stall_tail(tail)
    if not tail:
        return reason
    return (f"{reason.rstrip()}\n\nThe previous worker stopped without an outcome and stayed "
            f"silent after one nudge. Its terminal ended with:\n\n```\n" + "\n".join(tail) + "\n```")


# --------------------------------------------------------------------------- #
# Takeover — the human-authorized, lease-skipping reclaim (ADR-0011)           #
# --------------------------------------------------------------------------- #
#
# The lease is the *unattended* line between "dead" and "alive but slow". A
# human watching a fleet die is a faster oracle: takeover lists the instances
# GitHub still remembers and force-takes one's claims with the same atomic
# --force-with-lease push a stale reclaim uses, only skipping the staleness
# gate. It never counts as a retry: it answers "did the FLEET die?", not "is
# this WORK failing?".

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
# A claim whose worker died — an *orphaned claim*, or a *stale claim* reclaimed
# from a peer — is recovered from its durable progress, tiered by what survived:
# a worktree still on THIS machine, else the branch the worker pushed, else
# nothing. The selection is mechanics; "is this recovered state sane to build
# on" stays tick judgment. Only tier 3 tears anything down.

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
        # compared numerically: a str/int drift in orca's JSON would silently
        # downgrade every tier-1 recovery and lose the uncommitted work it saves
        try:
            if int(w.get("linkedIssue")) != int(number):
                continue
        except (TypeError, ValueError):
            continue
        if w.get("isMainWorktree") or w.get("isArchived"):
            continue
        if repo and w.get("projectId") != f"github:{repo}":
            continue
        hits.append(w)
    if not hits:
        return {"found": False, "path": None, "branch": None}
    best = max(hits, key=lambda w: int(w.get("lastActivityAt") or 0))
    branch = best.get("branch") or None
    if branch and branch.startswith("refs/heads/"):
        branch = branch[len("refs/heads/"):]
    return {"found": True, "path": best.get("path") or None, "branch": branch}


def find_orca_repo(repos, repo):
    """
    The repo orca knows the target by, from `orca repo list --json`'s
    `result.repos` rows → {"id", "path"}: the id `orca worktree create --repo
    id:<id>` needs, and the checkout whose object store a new worktree is cut from.
    A row matches when its `gitRemoteIdentity.canonicalKey` is
    `github.com/<owner>/<name>` (compared case-insensitively, as GitHub does). None
    when orca has no such repo.
    """
    want = f"github.com/{repo}".lower()
    for r in repos or []:
        key = ((r.get("gitRemoteIdentity") or {}).get("canonicalKey") or "").lower()
        if key == want and r.get("id") and r.get("path"):
            return {"id": r["id"], "path": r["path"]}
    return None


def worktree_name(branch_pattern, number, title):
    """`branch_pattern` filled for one issue — the NAME hint handed to `orca
    worktree create --name` (orca derives the real branch from it, ADR-0005). The
    slug is the title lowercased to `[a-z0-9-]`, at most 40 characters; a title
    with nothing usable (all CJK, say) slugs to `work`."""
    slug = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")[:40].rstrip("-")
    return (branch_pattern.replace("{number}", str(number))
            .replace("{slug}", slug or "work"))


def furthest_ahead(ahead_by_branch):
    """Of several remote branches matching one issue (an earlier attempt left one
    behind), the one furthest ahead of base — ties, and unmeasurable counts
    (None), go to the first by name. None when there is no candidate."""
    names = sorted(ahead_by_branch)
    return max(names, key=lambda b: ahead_by_branch[b] or 0) if names else None


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
      3  dispatch_fresh   nothing survived → re-dispatch from base. The ONLY
                          tier that tears down.

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
# The worker prompt — one template, filled by code (`afk dispatch`)            #
# --------------------------------------------------------------------------- #
#
# references/worker-prompt.md is the template: named blocks between
# `<!--afk:block NAME-->` and `<!--/afk:block-->`. The `prompt` block is the body;
# it names three slots — {opening} and {step1}, each filled from the block of
# that name for the chosen variant (`opening.fresh`, `step1.continue`, …), and
# {retry_reason}, filled from the `retry_reason` block only when a failure reason
# is handed over. Everything else in braces is a field.

_BLOCK_RE = re.compile(r"<!--afk:block ([a-z0-9_.]+)-->\n(.*?)\n?<!--/afk:block-->", re.DOTALL)
PROMPT_VARIANTS = ("fresh", "continue")
PROMPT_FIELDS = ("n", "title", "repo", "base_branch", "local_command", "branch", "worktree_path")
_NO_LOCAL_COMMAND = "true   # (no gate.local_command configured: run the repo's own build/test, if any)"


def render_worker_prompt(template, variant, fields, reason=None):
    """
    The prompt one worker is started with, from the template file's text.

      template: the text of references/worker-prompt.md
      variant:  "fresh" (a clean checkout of the base) or "continue" (the worktree
                or branch already carries a dead worker's progress — ADR-0011)
      fields:   {name: value} for every one of PROMPT_FIELDS. An empty
                `local_command` renders as a no-op with a note.
      reason:   why the previous attempt failed, when this is a retry; None otherwise

    Raises ValueError on a template missing a block, a missing field, or a
    placeholder left unfilled — a worker must never be started on a prompt with a
    literal `{branch}` in it.
    """
    if variant not in PROMPT_VARIANTS:
        raise ValueError(f"unknown worker prompt variant: {variant!r}")
    blocks = dict(_BLOCK_RE.findall(template or ""))

    def block(name):
        if name not in blocks:
            raise ValueError(f"worker prompt template has no {name!r} block")
        return blocks[name]

    missing = [k for k in PROMPT_FIELDS if k not in fields]
    if missing:
        raise ValueError(f"worker prompt: missing field(s) {', '.join(missing)}")
    values = {k: str(fields[k]) for k in PROMPT_FIELDS}
    values["local_command"] = values["local_command"].strip() or _NO_LOCAL_COMMAND

    text = block("prompt")
    slots = {"opening": block(f"opening.{variant}"), "step1": block(f"step1.{variant}"),
             "retry_reason": block("retry_reason") if reason else ""}
    for name, body in slots.items():
        text = text.replace("{" + name + "}", body)
    free_text = {"title": values.pop("title"), "reason": (reason or "").strip()}
    for name, value in values.items():
        text = text.replace("{" + name + "}", value)
    left = sorted(set(re.findall(r"\{(?:%s)\}" % "|".join((*values, *slots)), text)))
    if left:
        raise ValueError(f"worker prompt: unfilled placeholder(s) {', '.join(left)}")
    # the free-text values go in last, so a title that happens to contain
    # "{branch}" is never itself substituted into
    for name, value in free_text.items():
        text = text.replace("{" + name + "}", value)
    return text.strip() + "\n"


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

# The closed set of lifecycle phases the board renders. Happy path plus three
# off-ramps that reuse the same checkboxes + an annotation: ci_failed, escalated,
# and closed (the worker found the issue already satisfied — `afk close`).
STATUS_PHASES = ("claimed", "pr_open", "ci_failed", "awaiting_merge", "merged", "escalated",
                 "closed")

# Happy-path milestones, in order — these are the task-list checkboxes.
_STATUS_STEPS = (
    ("claimed",        "已认领 · worker 实现中"),
    ("pr_open",        "PR 已开{pr} · 等 {gate}"),
    ("awaiting_merge", "门已绿 · 待合并"),
    ("merged",         "已合并"),
)

# What the board calls the machine gate, per gate.ci mode (ADR-0012).
_GATE_NAME = {"required": "CI", "local": "本地门"}

# How far along the happy path each phase has reached (index of the last DONE
# step). escalated is a terminal give-up handled specially in `render_status_board`.
_PHASE_REACHED = {
    "claimed": 0, "pr_open": 1, "ci_failed": 1, "awaiting_merge": 2, "merged": 3,
    "escalated": 1, "closed": 0,
}


def _status_current_line(phase, gate, attempt, retry_max):
    """The single ▸/✅/⚠️ 'where are we now' line under the checklist."""
    if phase == "claimed":
        return "▸ 当前:worker 实现中,尚无 PR"
    if phase == "pr_open":
        return f"▸ 当前:等 {gate}"
    if phase == "ci_failed":
        return f"▸ 当前:{gate} 失败,修复重试中({attempt}/{retry_max}) —— 见下方 {gate} 与评论"
    if phase == "awaiting_merge":
        return "▸ 当前:门已绿,待合并"
    if phase == "merged":
        return "✅ 已合并,完成"
    if phase == "closed":
        return "✅ 主干已满足此需求,无需改动 —— 已关闭"
    return "⚠️ 已升级给人处理 —— 见下方评论"   # escalated


def render_status_board(phase, gate_ci, retry_max, instance=None, pr=None, attempt=0):
    """
    Render the human-facing progress *status board* comment body. Pure: a function
    of the discrete lifecycle state the tick already derived from fleet state; no
    I/O and no clock, so identical state → identical body (this is what lets the
    upsert write only when it changed, and keeps re-entrant ticks from spamming).
    Human-read only — never parsed back as a source of truth.

      phase:     one of STATUS_PHASES
      gate_ci:   config `gate.ci` — names the gate being waited on
      retry_max: config `retry` — shown with `attempt`, for ci_failed only
      instance:  owning fleet-instance id, shown in the header when given
      pr:        the PR number, once one is open
      attempt:   the claim's current attempt (`current_attempt`)

    Returns the full markdown body, led by STATUS_MARKER (the find-or-create anchor).
    """
    if phase not in STATUS_PHASES:
        raise ValueError(f"unknown status phase: {phase!r}")
    gate = _GATE_NAME[gate_ci]
    reached = _PHASE_REACHED[phase]
    escalated = phase == "escalated"

    def done(i, key):
        if escalated:              # terminal give-up: only what truly happened stays ticked
            return i == 0 or (key == "pr_open" and bool(pr))
        return i <= reached

    header = "**afk-fleet 进度**" + (f" · 认领方 `{instance}`" if instance else "")
    lines = [STATUS_MARKER, header, ""]
    for i, (key, label) in enumerate(_STATUS_STEPS):
        label = label.format(pr=f" (#{pr})" if pr else "", gate=gate)
        lines.append(f"- [{'x' if done(i, key) else ' '}] {label}")
    lines.append("")
    lines.append(_status_current_line(phase, gate, attempt, retry_max))
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Retry accounting + launcher pacing                                          #
# --------------------------------------------------------------------------- #

_ATTEMPT_PREFIX = "afk-attempt/"


def current_attempt(labels):
    """The attempt an issue is on, from its label names: the highest n across its
    `afk-attempt/<n>` labels, 0 when it has none (never retried). The ONE reader
    of that label format — `afk rebuild` puts the number on every `mine` row, and
    everything downstream (`next_attempt`, the status board) takes the number."""
    attempts = [0]
    for lb in labels or []:
        if isinstance(lb, str) and lb.startswith(_ATTEMPT_PREFIX):
            n = lb[len(_ATTEMPT_PREFIX):]
            if n.isdigit():
                attempts.append(int(n))
    return max(attempts)


def attempt_labels(labels):
    """Every `afk-attempt/*` label among an issue's label names — what a retry
    swaps out and an escalation strips, however many a hand-edit left behind."""
    return sorted(lb for lb in labels or []
                  if isinstance(lb, str) and lb.startswith(_ATTEMPT_PREFIX))


def next_attempt(attempt, retry_max):
    """
    Retry-or-escalate for a failed issue on attempt `attempt` (`current_attempt`).

      {"action":"retry","attempt":<n+1>,"to_label":"afk-attempt/<n+1>"}
      {"action":"escalate","attempt":<n>}                when n >= retry_max

    `afk fail` is the one caller, and the one writer of the label: it applies
    `to_label` and removes every `attempt_labels` the issue carried.
    """
    if attempt >= retry_max:
        return {"action": "escalate", "attempt": attempt}
    return {"action": "retry", "attempt": attempt + 1,
            "to_label": f"{_ATTEMPT_PREFIX}{attempt + 1}"}


def escalation_comment(reason, attempt, pr=None):
    """The durable hand-off comment an escalation appends to the issue (ADR-0006
    keeps it apart from the status board, which only points here). The stuck-point
    wording is the tick's; this frames it."""
    tried = f"after {attempt} retr{'y' if attempt == 1 else 'ies'}" if attempt else "without a retry"
    link = f" Last PR: #{pr}." if pr else ""
    return (f"**afk-fleet: escalated to a human** ({tried}).{link}\n\n"
            f"{(reason or '').strip()}")


def escalation_labels(labels, config):
    """The label edit that hands an issue to a human: `(add, remove)`. Removes
    `ready_label` and every attempt label the issue actually carries (never one it
    does not — gh refuses to remove an absent label), adds `escalate_label`."""
    present = set(labels or [])
    remove = sorted(present & {config["ready_label"], *attempt_labels(labels)})
    return [config["escalate_label"]], remove


def pace(did_work, in_flight, empty_streak, config):
    """
    The launcher's next sleep, in seconds.

      did_work:     the tick that just ran merged / dispatched / reclaimed / escalated
      in_flight:    claims this fleet holds
      empty_streak: consecutive empty cycles so far (`cycle_ticked` / `cycle_wake`)
      config:       read for busy_interval_seconds, idle_interval_seconds,
                    idle_ticks_before_sleep and claim_lease_ttl_seconds

    - did work, or holding claims → busy interval;
    - else stay busy until `idle_ticks_before_sleep` empty cycles, then idle interval;
    - HARD CAP: while holding any claim, never exceed ttl/2, so the per-instance
      heartbeat cannot lapse and get a live claim reclaimed (ADR-0003).
    """
    busy = int(config["busy_interval_seconds"])
    if did_work or in_flight > 0 or empty_streak < int(config["idle_ticks_before_sleep"]):
        interval = busy       # working, or recently active — stay responsive
    else:
        interval = int(config["idle_interval_seconds"])
    if in_flight > 0:
        interval = min(interval, int(config["claim_lease_ttl_seconds"]) // 2)
    return int(interval)


# --------------------------------------------------------------------------- #
# The launcher's cycle — gate, heartbeat, streaks and sleep as one state machine#
# --------------------------------------------------------------------------- #
#
# Everything the launcher carries between cycles is ONE opaque value, the cycle
# state, which `afk cycle` hands back and takes again:
#
#   fingerprint         the digest the last gate computed (ADR-0007)
#   skips               consecutive skipped cycles
#   empty_streak        consecutive EMPTY cycles — a tick that did nothing with
#                       nothing in flight and nothing on the frontier, or a skip
#                       while that was still so
#   in_flight           claims held, per the last tick's summary
#   frontier_remaining  dispatchable issues the last tick left undispatched
#
# The launcher never reads or edits a field; it is state for this code alone.

CYCLE_START = {"fingerprint": "", "skips": 0, "empty_streak": 0,
               "in_flight": 0, "frontier_remaining": 0}

_SUMMARY_WORK = ("merged", "dispatched", "reclaimed", "escalated")


def cycle_state(raw):
    """The cycle state from what the launcher handed back (None / "" on the first
    cycle → CYCLE_START). Raises ValueError on anything that is not a state this
    code produced — a launcher that mangled it must hear so, not run on zeros."""
    if raw is None or raw == "":
        return dict(CYCLE_START)
    if not isinstance(raw, dict) or set(raw) != set(CYCLE_START):
        raise ValueError(f"--state is not a cycle state (pass back the `state` the previous "
                         f"`afk cycle` returned, verbatim): {raw!r}")
    return {"fingerprint": str(raw["fingerprint"]),
            **{k: int(raw[k]) for k in CYCLE_START if k != "fingerprint"}}


def cycle_wake(state, current_fp, config):
    """
    The top of one launcher cycle: tick, or skip?

      state:      the cycle state (`cycle_state`)
      current_fp: `fingerprint` of what a rebuild would observe now; None when
                  `fingerprint_gate` is off (nothing was gathered)

    Returns {"action": "tick"|"skip", "reason", "state"} and, on a skip, the two
    things a skipped cycle still owes: `sleep_seconds`, and `heartbeat` — True when
    the fleet holds claims, so the effect layer refreshes the lease no tick will.
    On a tick the launcher spawns one and reports back through `cycle_ticked`,
    which is what returns that cycle's sleep.

    A skipped cycle extends the empty streak only while nothing is in flight and
    nothing is left on the frontier: unchanged state then proves the cycle empty.
    """
    if not config["fingerprint_gate"]:
        return {"action": "tick", "reason": "gate_off", "state": {**state, "skips": 0}}
    gate = fingerprint_gate(state["fingerprint"], current_fp, state["skips"],
                            config["force_tick_after_skips"])
    new = {**state, "fingerprint": current_fp, "skips": gate["skips"]}
    if gate["action"] == "tick":
        return {"action": "tick", "reason": gate["reason"], "state": new}
    if new["in_flight"] == 0 and new["frontier_remaining"] == 0:
        new["empty_streak"] += 1
    return {"action": "skip", "reason": gate["reason"], "state": new,
            "heartbeat": new["in_flight"] > 0,
            "sleep_seconds": pace(False, new["in_flight"], new["empty_streak"], config)}


def cycle_ticked(state, summary, config):
    """
    The bottom of a cycle that ran a tick: fold the tick's summary into the cycle
    state and say how long to sleep.

      summary: the tick's return — {"merged":[], "escalated":[], "dispatched":[],
               "reclaimed":[], "in_flight": int, "frontier_remaining": int, ...}

    `in_flight` and `frontier_remaining` are REQUIRED: a summary missing either
    would read as an idle fleet holding nothing, and pace it past its own lease.
    Returns {"state", "sleep_seconds"}.
    """
    if not isinstance(summary, dict):
        raise ValueError(f"--summary must be the tick's summary object, got {summary!r}")
    for key in ("in_flight", "frontier_remaining"):
        if not isinstance(summary.get(key), int) or isinstance(summary.get(key), bool):
            raise ValueError(f"--summary needs an integer {key!r} (the tick's return schema)")
    did_work = any(summary.get(k) for k in _SUMMARY_WORK)
    in_flight, remaining = summary["in_flight"], summary["frontier_remaining"]
    empty = not did_work and in_flight == 0 and remaining == 0
    new = {**state, "in_flight": in_flight, "frontier_remaining": remaining,
           "empty_streak": state["empty_streak"] + 1 if empty else 0}
    return {"state": new,
            "sleep_seconds": pace(did_work, in_flight, new["empty_streak"], config)}


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


def detect_runtime(env):
    """The agent runtime a launcher with environment `env` runs under —
    'qoderclicn' or 'claude'. One fleet instance runs one runtime (ADR-0014):
    qoderclicn sets QODERCN_CLI=1 in every child process; its absence means
    Claude (the default)."""
    if (env.get("QODERCN_CLI") or "").strip() in ("1", "true"):
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


def resolve_worker_command(base_url, supplied=None, resolved=None, runtime="claude"):
    """
    Settle the one string every worker is started with.

      runtime:  `detect_runtime`'s answer. qoderclicn has no custom providers, so
                it is always `stock` with its own default — never asked, and a
                supplied command is ignored (ADR-0014).
      base_url: the launcher's own ANTHROPIC_BASE_URL (None/"" = stock Anthropic).
      supplied: the human's answer, verbatim, or None if they haven't been asked.
      resolved: what the shell says `first_word(supplied)` is — the `type` output
                (an alias's full expansion, a function body, a path), or None if
                it resolves to nothing. Only meaningful when `supplied` is given.

    Returns {status, command, base_url, first_word, yolo, detail, runtime}. `command` is
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
    if runtime == "qoderclicn":
        base_url, supplied = None, None
    fw = first_word(supplied) if supplied else ""

    def out(status, command=None, yolo=None, detail=""):
        return {"status": status, "command": command, "base_url": base_url or None,
                "first_word": fw or None, "yolo": yolo, "detail": detail, "runtime": runtime}

    if runtime == "qoderclicn":
        fw = "qoderclicn"
        return out("stock", command=WORKER_COMMAND_DEFAULT_QODERCN, yolo=True)
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
# one tick; a missed change waits at most `force_after`
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

    Canonicalizes (sorts, keeps only the fields above) so row order and extra
    fields never move the digest. Returns a 16-hex digest.
    """
    def check_row(c):
        return [c.get("name") or c.get("context") or "",
                c.get("status") or "",
                c.get("conclusion") or c.get("state") or ""]

    canon = {
        "issues": sorted([i.get("number"), sorted(i.get("labels") or []), i.get("updatedAt") or ""]
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
    "unchanged", "skips": <new streak>} — `cycle_wake` folds both into the cycle
    state the launcher carries.
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
# refs, open-blocker counts) into the tick's whole working set: graft the
# eligibility facts, match each claim to its PR, partition mine/peer_live/stale.
# The gh/git gather lives in afk.py; why a `no_pr` claim has no PR is a separate,
# machine-dependent question (`afk no-pr`).

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
            n = ref.get("number")
            cur = m.get(n)
            if cur is None or (p.get("number") or 0) > (cur.get("number") or 0):
                m[n] = p
    return m


def closing_pr(prs, number):
    """The open PR that closes issue <number> (the latest, when several do), or None."""
    return _closing_pr_map(prs).get(number)


def superseded_prs(prs, number, branch_pattern):
    """The open PRs a FRESH start of issue <number> supersedes: the ones that
    close it from a branch shaped like the fleet's own (`branch_regex`). A PR a
    human opened from some other branch is never one of them — the fleet closes
    only what the fleet opened."""
    rx = branch_regex(branch_pattern, number)
    return [p for p in prs or []
            if any(ref.get("number") == number for ref in p.get("closingIssuesReferences") or [])
            and rx.match(p.get("headRefName") or "")]


def assemble_working_set(issues, prs, claims, heartbeats, blocked_by, me, now, config,
                         closed=()):
    """
    The tick's whole working set from the raw observables. Pure — afk.py's
    `rebuild` gathers, this assembles, and a fixture pins the join.

      issues:      open issues {number, title, labels: [name...], updatedAt}
      prs:         gh pr list rows (number, headRefOid, updatedAt,
                   statusCheckRollup, closingIssuesReferences)
      claims:      [{"number","instance","sha",...}]  (ref-scan shape)
      heartbeats:  {instance: last_ts}
      blocked_by:  {issue number: open blocker count}; missing → 0. Only
                   `frontier_candidates` need real counts — every other issue
                   already fails a cheaper eligibility check first.
      me, now:     my instance id / epoch seconds
      config:      the canonical config — read for ready_label, epic_labels,
                   claim_lease_ttl_seconds, gate.ci and concurrency
      closed:      the numbers of MY claims whose issue is closed (`issues` holds
                   only open ones, so `afk rebuild` asks about each of mine that is
                   missing from it)

    Returns:
      {"frontier": {"dispatch": [{"number","title"}...], "excluded": [...]},
       "mine": [{"number","title","status","board_phase","pr","checks",
                 "attempt"}...],
       "peer_live": [{"number","instance"}...],
       "stale": [{"number","instance","sha"}...],   # sha feeds reclaim --expect-sha
       "free_slots": <how many workers may be dispatched: concurrency - len(mine)>,
       "fingerprint": <digest of the same observables the gate hashes>,
       "now": now}

    `status` and `board_phase` are `subclassify_pr`'s pair; `attempt` is
    `current_attempt` — the number `afk status` takes.
    """
    ttl, ci_mode = config["claim_lease_ttl_seconds"], config["gate"]["ci"]
    by_num = {i.get("number"): i for i in issues}
    pr_for = _closing_pr_map(prs)

    frontier = select_frontier(_eligibility_rows(issues, prs, claims, blocked_by),
                               config["ready_label"], config["epic_labels"])
    frontier["dispatch"] = [{"number": n, "title": by_num.get(n, {}).get("title")}
                            for n in frontier["dispatch"]]

    part = classify_claims(claims, heartbeats, me, now, ttl)
    by_claim = {c.get("number"): c for c in claims}

    mine = []
    for n in part["mine"]:
        pr = pr_for.get(n)
        checks = pr_checks_state(pr.get("statusCheckRollup")) if pr else None
        issue = by_num.get(n, {})
        status, board_phase = subclassify_pr(pr is not None, checks, ci_mode,
                                             closed=n in set(closed))
        mine.append({"number": n, "title": issue.get("title"),
                     "status": status, "board_phase": board_phase,
                     "pr": pr.get("number") if pr else None, "checks": checks,
                     "attempt": current_attempt(issue.get("labels"))})

    return {"frontier": frontier,
            "mine": mine,
            "peer_live": [{"number": n, "instance": by_claim.get(n, {}).get("instance")}
                          for n in part["peer_live"]],
            "stale": [{"number": n, "instance": by_claim.get(n, {}).get("instance"),
                       "sha": by_claim.get(n, {}).get("sha")}
                      for n in part["stale"]],
            "free_slots": max(0, int(config["concurrency"]) - len(mine)),
            "fingerprint": fingerprint(issues, prs, claims),
            "now": now}
