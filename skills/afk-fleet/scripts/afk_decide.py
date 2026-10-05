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
import shlex

# --------------------------------------------------------------------------- #
# Config — one home for every key and default (ADR-0009)                       #
# --------------------------------------------------------------------------- #
#
# THE single source of truth for the config schema: every key the fleet knows,
# with its default and (via the default's type) its shape. The template in
# references/config-template.md is the human-facing rendering of this table —
# a fixture test keeps the two equal, so a hand-edit that drifts turns the
# suite red. The instance id and the worker launch command are deliberately NOT
# keys here: they are per-run, launcher-held facts, and the unknown-key error below is
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
        "trust_recorded_run": False,
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

# The completion gate's two modes (ADR-0012), each with what the status board calls
# that gate. `required` waits for the PR's GitHub checks; `local` never reads them
# and makes `gate.local_command` the gate, run by `afk land` against the exact
# tree that lands.
GATE_CI_MODES = {"required": "CI", "local": "本地门"}

# How `afk land` lands a PR — each is a `gh pr merge` flag of the same name.
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
    if gate.get("trust_recorded_run") and ci != "local":
        raise ValueError("config gate.trust_recorded_run: only gate.ci: 'local' has a landing "
                         "run of gate.local_command to skip (ADR-0026) — set gate.ci to 'local' "
                         "or leave this false")
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
    its author, and a launcher-held fact in a file is refused by construction); so does
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
                                 or f"config: unknown key {key!r} (note: the instance id and "
                                    f"the worker launch command are per-run facts, never config keys)")
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


def label_bars(labels, ready_label, epic_labels):
    """
    Every reason an issue's LABELS keep a fleet from dispatching it, as
    {kind: reason} in the order the frontier reports them; {} when its labels
    allow dispatch. The one home of the label rules: the frontier reads it, and
    so does the standing of a blocker (`blocker_standings`), so the two cannot
    disagree about what a fleet will work.

      not_ready  it lacks `ready_label` — the one bar a claim or an open PR
                 overrides (the issue is being worked anyway);
      epic       it carries an epic label — never dispatched, whatever else holds.
    """
    labels = set(labels or [])
    bars = {}
    if ready_label not in labels:
        bars["not_ready"] = f"no {ready_label} label"
    hit_epic = labels & {e.strip() for e in epic_labels if e.strip()}
    if hit_epic:
        bars["epic"] = f"epic label ({', '.join(sorted(hit_epic))})"
    return bars


def select_frontier(issues, ready_label, epic_labels):
    """
    Decide which issues are dispatchable RIGHT NOW. Shared by `--plan` and the live
    tick, so both compute the identical frontier (a stable published contract).

    `issues` are `_eligibility_rows`. One is dispatchable iff ALL hold: has
    ready_label · no epic label · not claimed · no open linked PR · zero open
    blocking dependencies.

    Returns {"dispatch": [num...], "excluded": [{"number","reason"}...]}.
    """
    dispatch, excluded = [], []
    for issue in issues:
        num = issue.get("number")
        bars = label_bars(issue.get("labels"), ready_label, epic_labels)
        if bars:
            reason = next(iter(bars.values()))
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
      now, ttl:   epoch seconds / `claim_lease_ttl_seconds`

    Returns {"mine":[n...], "peer_live":[n...], "stale":[n...]}:
      mine       — stamped with my instance; I reconcile these locally (a no-PR/
                   no-live-worker one is an *orphaned claim*, decided by the tick
                   with the worker liveness probe — not here).
      peer_live  — a peer owns it AND its heartbeat is within ttl → never touch.
      stale      — a peer owns it AND its heartbeat is missing/expired → the only
                   foreign claim I may reclaim (--force-with-lease takeover), or —
                   when its issue is already closed — delete (`assemble_working_set`
                   splits those out as `stale_closed`).
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


# Every `status` a `mine` row can carry — what `subclassify_pr` returns, and the
# vocabulary the tick's instructions route on (a test holds the docs to it).
CLAIM_STATUSES = ("awaiting_turn", "landing", "awaiting_ci", "failure", "no_pr", "closed")


def subclassify_pr(has_pr, checks_state, ci_mode, closed=False, landing=False):
    """
    Classify one of MY in-flight claims from its PR + checks → `(status,
    board_phase)`: what the tick does next, and what the status board shows a human
    meanwhile. Decided together because `gate.ci` bends both, in different ways.

      has_pr:       an open PR closes the issue
      checks_state: "green" | "red" | "pending" | None  (`pr_checks_state`)
      ci_mode:      gate.ci — "required" reads the checks; "local" never does
      closed:       the claimed issue is itself CLOSED — its worker landed the PR
                    (`afk land` cannot release the claim), or a human finished it
                    by hand
      landing:      the PR holds this fleet instance's landing turn (`held_turn`)

      status           the tick…                                board_phase
      closed           releases the leftover claim (and its      None (not re-rendered)
                       worktree)
      no_pr            asks `afk no-pr` why                      claimed
      landing          asks `afk no-pr` whether its worker is    landing
                       still at it — or, when the landing
                       stopped for the tick, `afk turn` again
      awaiting_ci      leaves it: checks exist and are still     pr_open
                       running
      failure          runs `afk fail`                           ci_failed
      awaiting_turn    grants it the landing turn (`afk turn`)   awaiting_turn
                       when it is first in `merge_order` and
                       no claim of mine is `landing`

    A PR that holds the turn is `landing` whatever its checks say: its worker is
    syncing, gating and pushing, so a pending or red run on the way is the
    landing's own business (`afk land` answers `awaiting_ci` / `gate_red`), never
    a second route into `afk fail`.

    In `local` mode (ADR-0012) there are no checks to wait on: gating is an
    **action `afk land` takes** (sync → the local gate → merge), not an
    observation the tick waits for. So every open PR without the turn is
    `awaiting_turn` — a red remote run, the repo's own `on: push` workflow the
    fleet does not gate on, must not park the claim in `failure` forever.

    A PR with **no checks at all** (`checks_state` None) is `awaiting_turn` in
    `required` mode too. Nothing is running, so nothing will ever arrive to wait
    for: `awaiting_ci` would park the claim forever in a repo that has no CI. What
    such a PR needs is the tick's judgment, and `afk turn` is where that is asked
    for (its `no_checks` outcome, answered with `--allow-no-checks`).
    (`merged` / `escalated`, the two terminal board phases, are set by `afk land`
    and the escalate step themselves.)
    """
    if closed:
        return "closed", None
    if not has_pr:
        return "no_pr", "claimed"
    if landing:
        return "landing", "landing"
    if ci_mode == "local" or checks_state in ("green", None):
        return "awaiting_turn", "awaiting_turn"
    if checks_state == "red":
        return "failure", "ci_failed"
    return "awaiting_ci", "pr_open"


# --------------------------------------------------------------------------- #
# The local completion gate + its bootstrap compatibility probe (ADR-0012)     #
# --------------------------------------------------------------------------- #
#
# In `gate.ci: local` the repo-local build/test command IS the completion gate:
# the worker runs it after its pre-PR sync, and `afk land` runs it again on the
# landing turn, after the landing's sync, in the same worktree. The invariant
# both runs serve: *what lands on the target branch was tested in the form it
# lands.* With `gate.trust_recorded_run` the second run is skipped when — and
# only when — a green run is on record for the exact head that lands
# (`gate_record_void`, ADR-0026). A red run comes back as a bounded excerpt,
# never a raw log.

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


def gate_record(head, command, clean, at):
    """
    What `afk gate` (and a green run of `afk land`) writes down after a GREEN run
    of the local gate — never after a red or timed-out one, so no record can be
    read as a pass that did not happen:

      head:    the commit the worktree was at, before and after the run
      command: the `gate.local_command` that ran, verbatim
      clean:   the worktree had nothing uncommitted and nothing untracked when the
               run started — what was tested is exactly `head`
      at:      when the run finished (epoch seconds)
    """
    return {"head": head, "command": command, "clean": bool(clean), "at": int(at)}


def gate_record_void(record, head, command):
    """
    Why a recorded gate run does NOT stand in for the landing's own run — or
    None when it does, which is the only case `afk land` skips its own (ADR-0026).

      record:  the worktree's `gate_record`, None when it has none (never gated
               through `afk gate`, a red run since, a worktree recreated here)
      head:    the head that would land, after the landing's sync
      command: the `gate.local_command` configured now

    The record proves the gate passed on one commit, with one command. Anything
    that makes the tree that lands a different tree — the landing's sync brought
    the target in, the worker committed afterwards — or the test a different test
    voids it, and so does a run over uncommitted or untracked files, which tested
    something no commit holds. Void is the safe side: the landing runs the gate.
    """
    if not isinstance(record, dict) or not record.get("head"):
        return "no green run of the gate is on record in this worktree"
    if record.get("command") != command:
        return (f"the recorded run was of a different command ({record.get('command')!r}) "
                f"than the gate.local_command configured now")
    if not record.get("clean"):
        return "the recorded run was on a tree with uncommitted or untracked files"
    if record["head"] != head:
        return (f"the recorded run was on {record['head']}, not on the head that would land "
                f"({head}) — a sync or a later commit moved it")
    return None


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
    The `gate.ci: required` machine gate, for `afk turn` and `afk land` alike:
    may this PR land on what its GitHub checks say, right now?

      checks_state:    `pr_checks_state` of the PR as read BEFORE any sync
      pushed:          the landing's sync just pushed new commits to the PR — those
                       checks describe a tree that is no longer the one that would land
      allow_no_checks: the tick's judgment that a repo with no CI at all may land
                       on its acceptance criteria (the progressive gate)

    Returns "green" | "awaiting_ci" | "gate_red" | "no_checks". A sync that moved
    the head always waits: CI must run on the tree that lands, and the next
    `afk land` finds the sync a no-op and reads the fresh verdict.
    """
    if checks_state is None:
        return "green" if allow_no_checks else "no_checks"
    if pushed:
        return "awaiting_ci"
    return {"green": "green", "red": "gate_red"}.get(checks_state, "awaiting_ci")


def gate_comment(verdict, command):
    """The PR comment a red landing gate leaves behind, so whoever words the
    failure — the worker fixing it, or the tick failing a worker that gave up —
    re-reads it from where it lives (ADR-0012)."""
    how = (f"timed out (exit {verdict['exit_code']})" if verdict["timed_out"]
           else f"exit {verdict['exit_code']}")
    omitted = (f"\n\n_({verdict['omitted_lines']} earlier line(s) omitted)_"
               if verdict["omitted_lines"] else "")
    return (f"**afk-fleet landing gate: red** — `{command}` → {how}, run after syncing "
            f"with the merge target.\n\n```\n{verdict['excerpt']}\n```{omitted}")


# --------------------------------------------------------------------------- #
# no_pr reconciliation — disambiguating a FINISHED worker from a CODING one    #
# --------------------------------------------------------------------------- #
#
# `subclassify_pr` only says a claim has no PR yet — or that its PR holds the
# landing turn and has not landed. Either way a worker that finished and went
# idle looks identical, to a terminal probe, to one still working, so three
# signals disambiguate (all gathered by `afk no-pr`):
#   1. git PROGRESS in the worktree (commits ahead / dirty tree / last activity);
#   2. the worker's VERDICT marker on the issue — its declared reason for opening
#      no PR: `already-satisfied` (done in base, empty diff), `blocked` (a
#      dependency gap, see blocked_by) or `giving-up` (a failure it could not fix);
#      for `blocked`, also the STANDING of each issue it names (`blocker_standings`);
#   3. the terminal's busy / idle / none state from the orca probe.
# `classify_no_pr` is the pure join. Two words are kept apart throughout: the
# VERDICT is what the worker declared in its marker (an input); the OUTCOME is
# what this code concludes from all three signals. Whether to TRUST the marker
# stays the tick's call.

_VERDICT_MARKER_RE = re.compile(r"<!--\s*afk:verdict\b(.*?)-->", re.DOTALL)

# The phases a worker may declare in its marker (worker-prompt.md asks for exactly
# these; anything else `classify_no_pr` treats as a failure).
VERDICT_PHASES = ("already-satisfied", "blocked", "giving-up")
_SATISFIED, _BLOCKED, _GIVING_UP = VERDICT_PHASES

# What the orca liveness probe can say about a worker's terminal.
TERMINAL_STATES = ("busy", "idle", "none")

# Every (outcome, action) `classify_no_pr` can return — the vocabulary the tick's
# instructions route on (a test holds the docs to it).
NO_PR_ROUTES = (("coding", "leave"), ("idle_done", "close_release"),
                ("idle_blocked", "redispatch"), ("idle_blocked", "park"),
                ("idle_blocked", "escalate"),
                ("idle_stalled", "nudge"), ("idle_failed", "next_attempt"),
                ("dead", "orphan"))


def _verdict_text(n, phase, blocked_by, reason, optional=str):
    """The marker's one spelling: its fields, their names and their order."""
    parts = [f"n={n}", f"phase={phase}",
             *([optional(f"blocked_by={blocked_by}")] if blocked_by else []),
             *([optional(f"reason={reason}")] if reason else [])]
    return f"<!--afk:verdict {' '.join(parts)}-->"


def verdict_marker(n, phase, blocked_by=(), reason=None):
    """The marker a worker posts for one verdict — what `parse_verdict_marker`
    reads back (a test round-trips every phase)."""
    return _verdict_text(n, phase, ",".join(str(b) for b in blocked_by), reason)


def verdict_marker_format(n):
    """The marker as the worker prompt shows it to issue <n>'s worker: its own
    number filled in, the rest as placeholders, optional fields in brackets."""
    return _verdict_text(n, f"<{'|'.join(VERDICT_PHASES)}>", "<csv of issue numbers>", "<short>",
                         optional=lambda field: f"[{field}]")


def parse_verdict_marker(body):
    """
    Parse the FIRST afk:verdict marker in one comment body → a verdict dict, or
    None if the body carries no marker. The marker is `verdict_marker`'s:

      <!--afk:verdict n=<issue> phase=<one of VERDICT_PHASES> \
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


# Where one issue a `blocked` verdict names stands (ADR-0022):
#   closed   done — it no longer blocks anything;
#   waiting  open, and the backlog will resolve it with no human: a fleet holds
#            it, a PR is open for it, or it carries `ready_label`;
#   unmet    nothing will resolve it — `reason` says why.
BLOCKER_STANDINGS = ("closed", "waiting", "unmet")
_CLOSED, _WAITING, _UNMET = BLOCKER_STANDINGS

# The `state_reason`s of a closed issue whose work was NOT done.
_CLOSED_UNDONE = {"not_planned": "was closed as not planned",
                  "duplicate": "was closed as a duplicate"}


def _blocker_standing(blocker, claimed, has_open_pr, config):
    """One named blocker's `(standing, reason)`, before the cycle check."""
    if blocker is None:
        return _UNMET, "could not be read (it may not exist)"
    if blocker.get("pull_request"):
        return _UNMET, "is a pull request, not an issue"
    if blocker.get("state") == "closed":
        undone = _CLOSED_UNDONE.get(blocker.get("state_reason"))
        return (_UNMET, undone) if undone else (_CLOSED, None)
    bars = label_bars(blocker.get("labels"), config["ready_label"], config["epic_labels"])
    if set(bars) - {"not_ready"}:          # a bar no claim overrides: nothing will dispatch it
        return _UNMET, f"will never be dispatched: {'; '.join(bars.values())}"
    if bars and not (claimed or has_open_pr):
        return _UNMET, f"is open but no fleet will work it (unclaimed, {bars['not_ready']})"
    return _WAITING, None


def depends_on(start, target, edges):
    """Does issue `start` depend — directly or through any chain of open
    blockers — on issue `target`? `edges` is {issue number: [its open blockers]}."""
    seen, todo = set(), [start]
    while todo:
        n = todo.pop()
        if n == target:
            return True
        if n not in seen:
            seen.add(n)
            todo.extend(edges.get(n) or [])
    return False


def blocker_standings(number, named, config, *, blockers, claimed, open_pr, edges):
    """
    Where each issue a `blocked` verdict names stands — will the dependency
    issue <number>'s worker discovered resolve on its own, or must a human look?

      named:    the verdict's blocked_by, in order
      config:   the canonical config — read for ready_label and epic_labels
      blockers: {n: {"state", "state_reason", "labels": [name...], "pull_request": bool}};
                a missing or None entry is an issue that could not be read
      claimed:  the issue numbers a claim ref exists for — ANY owner: mine and a
                live peer's are being worked, a stale one is reclaimed and continued
      open_pr:  the issue numbers an open PR closes
      edges:    {n: [its open blockers]}, covering everything reachable from a
                `waiting` blocker (`afk.py` walks it) — the cycle check

    Returns [{"number", "standing": one of BLOCKER_STANDINGS, "reason": str|None}...].
    A `waiting` blocker that itself depends on <number> is `unmet`: recording
    <number> as blocked by it would close a cycle neither side ever leaves.
    """
    rows = []
    for n in named:
        standing, reason = _blocker_standing((blockers or {}).get(n), n in claimed, n in open_pr,
                                             config)
        if n == number:
            standing, reason = _UNMET, "is the issue itself"
        elif standing == _WAITING and depends_on(n, number, edges or {}):
            standing, reason = _UNMET, (f"already depends on #{number}: waiting on it would "
                                        f"close a cycle")
        rows.append({"number": n, "standing": standing, "reason": reason})
    return rows


def blocked_route(named, standings):
    """
    What a `blocked` verdict comes to, from the standing of each blocker it names.

      named:     the verdict's blocked_by
      standings: {issue number: one of BLOCKER_STANDINGS}; anything else — a
                 missing entry, an unknown word — is `unmet`

    Returns {"action", "pending_blockers"}:
      redispatch  every named blocker is closed: the dependency cleared.
      park        every named blocker is closed or `waiting`: record the
                  dependency and wait for it (`afk park`).
      escalate    one is `unmet`, or none was named — nothing will ever clear it.
    `pending_blockers` is the named blockers not `closed` — still open, or
    closed without the work being done.
    """
    standings = standings or {}
    pending = [n for n in named if standings.get(n) != _CLOSED]
    if not named or any(standings.get(n) != _WAITING for n in pending):
        return {"action": "escalate", "pending_blockers": pending}
    return {"action": "park" if pending else "redispatch", "pending_blockers": pending}


def park_refusal(verdict, standings):
    """
    Why a claim cannot be parked, or None when it can — `afk park`'s own
    check of what `afk no-pr` told the tick, worded so the tick knows what to do
    instead.

      verdict:   the `latest_verdict` dict
      standings: `blocker_standings`' rows for its blocked_by
    """
    if not verdict.get("found") or verdict.get("phase") != _BLOCKED:
        return "its worker's latest verdict is not `blocked`"
    named = verdict.get("blocked_by") or []
    action = blocked_route(named, {b["number"]: b["standing"] for b in standings})["action"]
    if action == "park":
        return None
    if action == "redispatch":
        return "every blocker it named is closed — dispatch it again instead"
    if not named:
        return "its `blocked` verdict names no blocker — escalate it instead"
    unmet = "; ".join(f"#{b['number']} {b['reason']}" for b in standings if b["standing"] == _UNMET)
    return f"nothing will resolve {unmet} — escalate it instead"


def read_worker_state(row, now, grace_seconds, tui_idle=None):
    """
    A worker's state, read from its worktree's row of `orca worktree ps --json` —
    the `terminal` / `terminal_idle_seconds` reading `classify_no_pr` takes,
    made in code instead of by a tick looking at a screen (ADR-0021).

      row: {liveTerminalCount, lastOutputAt, agents: [{state, stateStartedAt,
           parentPaneKey}]} (orca's clocks are epoch MILLISECONDS), or None when
           orca lists no such worktree. `state` is what the agent's own hooks
           reported: working | waiting | blocked | done.
      now, grace_seconds: epoch seconds / `worker_idle_grace_seconds`.
      tui_idle: for a runtime that reports NO state (qoderclicn), whether orca
           sees its terminal idle — `orca terminal wait --for tui-idle` answered
           (True) or timed out (False). None when not asked, read as idle.

    Returns {"terminal", "terminal_idle_seconds", "state"}:
      none  no live terminal in the worktree — the worker is gone.
      busy  the agent reports `working` AND the terminal produced output within
            grace. Both, because a stop report the runtime lost would otherwise
            read `working` forever — a claim parked on a state that cannot change
            (ADR-0013).
            A runtime that reports no state is busy when orca does not see its
            terminal idle (`tui_idle` False).
      idle  anything else, timed from when the agent reported it stopped. A lost
            stop report is timed from the terminal's last output; a runtime that
            reports no state is not timed at all (None) — its idle screen redraws
            on a timer, so its output says nothing — and the worktree's own
            clocks decide.
    `state` is the report it was read from: the top-level agent that changed
    state last (an older pane's report, a subagent's, do not speak for it).
    """
    if not row or not row.get("liveTerminalCount"):
        return {"terminal": "none", "terminal_idle_seconds": None, "state": None}

    def ago(ms):
        return max(0, int(now) - int(ms) // 1000) if ms else None

    output_idle = ago(row.get("lastOutputAt"))
    agents = [x for x in row.get("agents") or [] if x.get("state") and not x.get("parentPaneKey")]
    lead = max(agents, key=lambda x: x.get("stateStartedAt") or 0, default=None)
    state = lead["state"] if lead else None

    def out(terminal, idle):
        return {"terminal": terminal, "terminal_idle_seconds": idle, "state": state}

    if state == "working":
        live = output_idle is not None and output_idle < grace_seconds
        return out("busy" if live else "idle", output_idle)
    if state is not None:
        stopped = ago(lead.get("stateStartedAt"))
        return out("idle", output_idle if stopped is None else stopped)
    return out("busy" if tui_idle is False else "idle", None)


def classify_no_pr(progress, terminal, terminal_idle_seconds, worker_verdict, blocker_states,
                   now, grace_seconds, nudged_at=None, can_nudge=True, turn_at=None,
                   turn_stopped=None):
    """
    The outcome for one of MY claims that is waiting on its WORKER — a `no_pr`
    claim, or a `landing` one — from the raw signals.

      progress:        the worktree's git progress {"commits_ahead", "dirty",
                       "last_commit_ts", "worktree_mtime_ts"}; {} / None if unreadable.
      terminal:        the orca probe — "busy" | "idle" (connected, not working) |
                       "none" (no live worker/terminal at all).
      terminal_idle_seconds: seconds since the terminal last showed activity;
                       None if the probe could not say.
      worker_verdict:  the `latest_verdict` dict (or None) — what the worker declared.
      blocker_states:  {issue number: one of BLOCKER_STANDINGS} for the verdict's
                       blocked_by (`blocker_standings`). Anything else is `unmet`.
      now, grace_seconds: epoch seconds / `worker_idle_grace_seconds`.
      nudged_at:       epoch seconds this worker was nudged (`afk nudge`), None if
                       it never was. A nudge is spent once: the second silence fails.
      can_nudge:       False when there is nowhere to record a nudge (no worktree
                       on this machine) — the silence then fails at once.
      turn_at:         epoch seconds this worker was last told to land its PR
                       (`afk turn`), or its `afk land` last stopped; None when its
                       PR holds no turn. A sign of life like the nudge: the worker
                       gets a whole grace period to start on it, and after that
                       its silence takes the same nudge → failure path as any other.
      turn_stopped:    the LAND_OUTCOMES word this worker's `afk land` last stopped
                       with, None when it has not stopped (or holds no turn). One of
                       LAND_WAITS means the worker is idle because the next move is
                       the tick's — its quiet is not a silence, and is never nudged
                       or failed here (`afk turn` is what moves it).

    Returns {"outcome", "action", "idle_seconds", "pending_blockers"} — `action` is
    what the tick does, `outcome` the reason it is grouped under:
      coding       leave         — busy, OR last activity within grace, OR its
                                   landing stopped for the tick (`turn_stopped`).
      idle_done    close_release — idle past grace + `already-satisfied` + NO changes
                                   on the branch: the tick verifies the empty diff,
                                   closes + releases.
      idle_blocked redispatch    — …+ `blocked`, and every blocked_by is now closed.
      idle_blocked park          — …+ `blocked`, and every blocked_by still open is
                                   one the backlog will resolve: `afk park` records
                                   the dependency and waits for it (ADR-0022).
      idle_blocked escalate      — …+ `blocked`, and a blocked_by is one nothing will
                                   resolve (or the verdict names none, so nothing
                                   can ever clear).
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
    newest file mtime, terminal activity, the nudge, the landing turn — a nudged
    worker, or one just given its turn, gets a whole grace period to answer); None when none is
    known, which is never
    "within grace". `pending_blockers` is `blocked_route`'s: the blocked_by not yet
    done, [] for any other verdict.
    """
    progress = progress or {}
    seen = [t for t in (progress.get("last_commit_ts"), progress.get("worktree_mtime_ts"))
            if t is not None]
    if terminal_idle_seconds is not None:
        seen.append(int(now) - int(terminal_idle_seconds))
    for told_at in (nudged_at, turn_at):
        if told_at is not None:
            seen.append(int(told_at))
    idle_seconds = max(0, int(now) - int(max(seen))) if seen else None

    def out(outcome, action, pending_blockers=()):
        assert (outcome, action) in NO_PR_ROUTES, (outcome, action)
        return {"outcome": outcome, "action": action, "idle_seconds": idle_seconds,
                "pending_blockers": list(pending_blockers)}

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
    # …unless it declared none and its landing stopped for the tick: that quiet
    # is the tick's to end (`afk turn`), not a silence.
    if not verdict.get("found") and turn_stopped in LAND_WAITS:
        return out("coding", "leave")
    if phase == _SATISFIED:
        # "nothing needed doing" is refuted by work sitting on the branch.
        has_changes = int(progress.get("commits_ahead") or 0) > 0 or bool(progress.get("dirty"))
        if has_changes:
            return out("idle_failed", "next_attempt")
        return out("idle_done", "close_release")
    if phase == _BLOCKED:
        route = blocked_route(verdict.get("blocked_by") or [], blocker_states)
        return out("idle_blocked", route["action"], route["pending_blockers"])
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
            f"for a confirmation or an answer: continue {task} to the end, and finish with the "
            f"outcome it asks for — a PR, a landing, or an afk:verdict marker comment.")


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
# The landing turn — a worker lands its own PR, one PR at a time (ADR-0027)    #
# --------------------------------------------------------------------------- #
#
# A finished PR is landed by the worker that wrote it (`afk land`), on a landing
# turn its fleet instance grants (`afk turn`) — one turn at a time. The turn is
# recorded where it concerns, as ONE marker comment on the PR:
#
#   <!--afk:turn instance=<id> at=<epoch> [verified=<sha>] [allow_no_checks=1]
#       [stopped=<outcome> head=<sha>]-->
#
# `instance` is the fleet instance that granted it: `afk land` refuses a marker
# that does not name the instance holding the claim, so a turn does not survive
# a takeover. `verified` and `allow_no_checks` are the tick's two judgments, made
# BEFORE the turn is granted and carried to the landing. `stopped` is where
# `afk land` last stopped short of merging, on which `head`. The record lives
# and dies with the PR: merged or closed, the turn is free.

_TURN_MARKER_RE = re.compile(r"<!--\s*afk:turn\b(.*?)-->", re.DOTALL)

# Every `outcome` `afk land` can stop with — the vocabulary the worker's prompt
# routes on (a test holds the prompt and the docs to it).
LAND_OUTCOMES = ("merged", "conflict", "gate_red", "awaiting_ci", "needs_verify", "no_checks")

# The landing outcomes where the next move is the TICK's, not the worker's: the
# worker wakes the launcher and stops, and is told to land again (`afk turn`).
LAND_WAITS = ("awaiting_ci", "needs_verify", "no_checks")

# Every `outcome` `afk turn` can stop with — the vocabulary the tick's
# instructions route on (a test holds the docs to it).
TURN_OUTCOMES = ("granted", "waiting", "landing", "awaiting_ci", "gate_red", "no_checks",
                 "needs_verify")


def land_outcome(outcome):
    """`outcome`, refused unless it is one of LAND_OUTCOMES: `afk land` cannot
    stop with a word the worker was never told how to act on."""
    if outcome not in LAND_OUTCOMES:
        raise ValueError(f"not a landing outcome: {outcome!r}")
    return outcome


def turn_outcome(outcome):
    """`outcome`, refused unless it is one of TURN_OUTCOMES."""
    if outcome not in TURN_OUTCOMES:
        raise ValueError(f"not a turn outcome: {outcome!r}")
    return outcome


def turn_comment(instance, at, verified=None, allow_no_checks=False, stopped=None, head=None):
    """The PR comment that records a landing turn: the marker `latest_turn` reads
    back, then the same facts worded for a human reading the PR.

      instance:        the fleet instance granting the turn
      at:              when the worker was last told, or last stopped (epoch
                       seconds) — the sign of life its silence is timed from
      verified:        the head an adversarial verify passed, if one did
      allow_no_checks: the tick judged a PR with no checks at all may land
      stopped, head:   the LAND_OUTCOMES word `afk land` last stopped with short
                       of merging, and the head it stopped on; None while the
                       worker has not stopped
    """
    parts = [f"instance={instance}", f"at={int(at)}",
             *([f"verified={verified}"] if verified else []),
             *(["allow_no_checks=1"] if allow_no_checks else []),
             *([f"stopped={stopped}", f"head={head}"] if stopped else [])]
    state = (f"Its last `afk land` stopped with `{stopped}` on `{(head or '')[:12]}`."
             if stopped else "The worker has been told to land it.")
    return (f"<!--afk:turn {' '.join(parts)}-->\n"
            f"**afk-fleet: this PR holds the landing turn** of fleet instance `{instance}`. "
            f"The worker that wrote the branch lands it with `afk land` — sync with the target, "
            f"gate, merge — and the next PR's turn comes when this one has landed or failed. "
            f"{state}")


def latest_turn(comments):
    """
    The landing turn recorded on a PR, from its comments ([{"id", "body"}...],
    oldest first) → {"instance", "at", "verified", "allow_no_checks", "stopped",
    "head", "comment_id"}, or None when the PR was never granted one. The latest
    marker wins; one that names no instance is not a record — nobody could hold it.
    """
    found = None
    for c in comments or []:
        m = _TURN_MARKER_RE.search(c.get("body") or "")
        if not m:
            continue
        attrs = dict(tok.split("=", 1) for tok in m.group(1).split() if "=" in tok)
        if not attrs.get("instance"):
            continue
        at = attrs.get("at", "")
        stopped = attrs.get("stopped")
        found = {"instance": attrs["instance"], "at": int(at) if at.isdigit() else None,
                 "verified": attrs.get("verified"),
                 "allow_no_checks": attrs.get("allow_no_checks") == "1",
                 "stopped": stopped if stopped in LAND_OUTCOMES else None,
                 "head": attrs.get("head") if stopped in LAND_OUTCOMES else None,
                 "comment_id": c.get("id")}
    return found


def held_turn(turn, owner):
    """`turn` (`latest_turn`) when it is held by the claim's owner, else None.

      owner: the instance id the claim ref is stamped with; None or "" when
             there is no claim, or one that names nobody

    A turn granted by another instance — the fleet the claim was taken over
    from — is nobody's: the new owner grants its own.
    """
    return turn if turn and owner and turn["instance"] == owner else None


def turn_gate(ci_mode, checks_state, allow_no_checks, adversarial_verify, verified, head):
    """
    May a landing turn be granted (or its worker told to land again) on what is
    known of the PR right now? The tick's judgments are settled HERE, before the
    worker is told — a worker never verifies itself.

      ci_mode:            gate.ci
      checks_state:       `pr_checks_state` of the PR's current head
      allow_no_checks:    the tick's judgment that a PR with no checks may land
      adversarial_verify: gate.adversarial_verify
      verified:           the head the tick says an adversarial verify passed
      head:               the PR's current head

    Returns "ready", or the TURN_OUTCOMES word it is refused with: `awaiting_ci`
    / `gate_red` / `no_checks` (`required` only — in `local` the machine gate is
    run by `afk land`, so there is nothing to wait for), then `needs_verify`.
    """
    if ci_mode != "local":
        checks = checks_gate(checks_state, False, allow_no_checks)
        if checks != "green":
            return checks
    if adversarial_verify and verified != head:
        return "needs_verify"
    return "ready"


def turn_order(rows):
    """
    The order landing turns are granted in — the merge queue: the issue numbers
    of the `mine` rows whose PR is ready, a PR that already holds a turn first,
    then the lower PR number.

      rows: `mine` rows {"number", "status", "pr"}; only `landing` and
            `awaiting_turn` ones are in the queue
    """
    ready = [r for r in rows if r["status"] in ("landing", "awaiting_turn")]
    return [r["number"] for r in sorted(ready, key=lambda r: (r["status"] != "landing", r["pr"]))]


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


def short_branch(ref):
    """`refs/heads/x/y` → `x/y`; a name that is already short is returned as is.
    orca reports a worktree's branch either way."""
    ref = ref or ""
    return ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref


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
    return {"found": True, "path": best.get("path") or None,
            "branch": short_branch(best.get("branch")) or None}


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
# is handed over. The `landing` block is a brief of its own — `render_landing` —
# pointed at when the worker is given its landing turn: the one command a worker
# lands its PR with and what each of its outcomes asks for (ADR-0027). It is the
# only place either is spelled; the body says no more than that the PR lands
# later, on its turn. Everything else in braces is a
# field — four of them derived: {wake_command}, the line a worker runs to wake
# the launcher once its outcome is on GitHub, built from the `launcher_terminal`
# field (ADR-0020), {gate_command}, the line a worker runs the local gate with —
# `afk gate`, built from the `afk_path` and `local_command` fields, so a green
# run is on record for the landing (ADR-0026) — {land_command}, `afk land` with
# the run's config, built from `afk_path`, `n`, `repo` and `config`, and
# {verdict_marker}, the marker a worker that opens no PR must post
# (`verdict_marker_format`).

_BLOCK_RE = re.compile(r"<!--afk:block ([a-z0-9_.]+)-->\n(.*?)\n?<!--/afk:block-->", re.DOTALL)
PROMPT_VARIANTS = ("fresh", "continue")
PROMPT_FIELDS = ("n", "title", "repo", "base_branch", "local_command", "afk_path", "config",
                 "branch", "worktree_path", "launcher_terminal")
LANDING_FIELDS = ("pr", "pr_branch", "target")
_PROMPT_SLOTS = ("opening", "step1", "retry_reason")
_PROMPT_DERIVED = ("wake_command", "gate_command", "land_command", "verdict_marker")
_NO_LOCAL_COMMAND = "true   # (no gate.local_command configured: run the repo's own build/test, if any)"
_NO_WAKE = "true   # (no coordinator terminal to wake: it finds your outcome at its next poll)"
_TERMINAL_HANDLE_RE = re.compile(r"[A-Za-z0-9_.:-]+")


def wake_line(number):
    """The one line a wake types at the launcher's terminal. It names the issue for
    the human scrolling back, and nothing the launcher may act on (ADR-0020)."""
    return f"afk-wake #{number}"


def wake_command(launcher_terminal, number):
    """
    The command a worker runs once its outcome is on GitHub — a PR, a verdict
    marker, a landing that merged or stopped for the tick — to wake the launcher out of its sleep
    (ADR-0020), so the next cycle opens now instead of a busy interval later.

      launcher_terminal: the orca handle of the terminal the launcher runs in; ""
                         or None when it runs in none (a headless tick)

    The wake is a hint and carries no state: the cycle it triggers reads GitHub
    like any other, and a wake that is lost costs only the wait it would have
    saved. With no handle — or one that is not a bare handle, since this string is
    run in the worker's shell — the command is a no-op with a note.
    """
    handle = (launcher_terminal or "").strip()
    if not _TERMINAL_HANDLE_RE.fullmatch(handle):
        return _NO_WAKE
    return f'orca terminal send --terminal {handle} --text "{wake_line(number)}" --enter'


def gate_command(afk_path, local_command):
    """
    The command a worker runs the local gate with: `afk gate`, carrying the
    configured `gate.local_command` as its config, so the run is made — and, when
    green, recorded — by the tool rather than reported by the worker (ADR-0026).

      afk_path:      the afk executable on this machine (the worker runs here)
      local_command: `gate.local_command`; empty → a no-op with a note

    The command travels inside the line, so a config that changes after the worker
    was briefed records a command the landing no longer recognises: void, not wrong.
    """
    command = (local_command or "").strip()
    if not command:
        return _NO_LOCAL_COMMAND
    config = json.dumps({"gate": {"local_command": command}}, ensure_ascii=False)
    return f"{shlex.quote(afk_path)} gate --config {shlex.quote(config)}"


def _prompt_blocks(template):
    blocks = dict(_BLOCK_RE.findall(template or ""))

    def block(name):
        if name not in blocks:
            raise ValueError(f"worker prompt template has no {name!r} block")
        return blocks[name]
    return block


def land_command(afk_path, number, repo, config):
    """
    The one command a worker lands its PR with, on its landing turn: `afk land`,
    carrying the run's config — the merge target and strategy, the gate, the
    claim namespace the turn is checked against (ADR-0027).

      afk_path: the afk executable on this machine (the worker runs here)
      config:   the run's canonical config, as its JSON text
    """
    return (f"{shlex.quote(afk_path)} land --issue {number} --repo {shlex.quote(repo)} "
            f"--config {shlex.quote(config)}")


def _fill_prompt(text, fields, landing=None, reason=None):
    """Fill every field of an assembled prompt text. Raises ValueError on a missing
    field or a placeholder left unfilled; the free-text values (title, reason, the
    land command's config) go in last and in one pass, so one that happens to
    contain "{branch}" is never itself substituted into."""
    missing = [k for k in PROMPT_FIELDS if k not in fields]
    missing += [k for k in LANDING_FIELDS if landing is not None and k not in landing]
    if missing:
        raise ValueError(f"worker prompt: missing field(s) {', '.join(missing)}")
    values = {k: str(fields[k]) for k in PROMPT_FIELDS}
    values["land_command"] = land_command(values["afk_path"], fields["n"], values["repo"],
                                          values.pop("config"))
    values["gate_command"] = gate_command(values.pop("afk_path"), values["local_command"])
    values["local_command"] = values["local_command"].strip() or _NO_LOCAL_COMMAND
    values["wake_command"] = wake_command(values.pop("launcher_terminal"), fields["n"])
    values["verdict_marker"] = verdict_marker_format(fields["n"])
    # the land command carries the whole config, and a config may hold braces
    free_text = {"title": values.pop("title"), "reason": (reason or "").strip(),
                 "land_command": values.pop("land_command")}
    if landing is not None:
        values.update({k: str(landing[k]) for k in LANDING_FIELDS})
    for name, value in values.items():
        text = text.replace("{" + name + "}", value)
    known = (*PROMPT_FIELDS, *LANDING_FIELDS, *_PROMPT_SLOTS, *_PROMPT_DERIVED)
    left = sorted(set(re.findall(r"\{(?:%s)\}" % "|".join(known), text))
                  - {"{%s}" % k for k in free_text})
    if left:
        raise ValueError(f"worker prompt: unfilled placeholder(s) {', '.join(left)}")
    text = re.sub(r"\{(%s)\}" % "|".join(free_text), lambda m: free_text[m.group(1)], text)
    return text.strip() + "\n"


def render_worker_prompt(template, variant, fields, reason=None):
    """
    The prompt one worker is started with, from the template file's text.

      template: the text of references/worker-prompt.md
      variant:  "fresh" (a clean checkout of the base) or "continue" (the worktree
                or branch already carries a dead worker's progress — ADR-0011)
      fields:   {name: value} for every one of PROMPT_FIELDS. An empty
                `local_command` renders as a no-op with a note (`gate_command`),
                and so does the wake when `launcher_terminal` is empty
                (`wake_command`).
      reason:   why the previous attempt failed, when this is a retry; None otherwise

    Raises ValueError on a template missing a block, a missing field, or a
    placeholder left unfilled — a worker must never be started on a prompt with a
    literal `{branch}` in it.
    """
    if variant not in PROMPT_VARIANTS:
        raise ValueError(f"unknown worker prompt variant: {variant!r}")
    block = _prompt_blocks(template)
    text = block("prompt")
    slots = {"opening": block(f"opening.{variant}"), "step1": block(f"step1.{variant}"),
             "retry_reason": block("retry_reason") if reason else ""}
    for name, body in slots.items():
        text = text.replace("{" + name + "}", body)
    text = re.sub(r"\n{3,}", "\n\n", text)      # an unfilled slot leaves no gap behind
    return _fill_prompt(text, fields, reason=reason)


def render_landing(template, fields, landing):
    """
    The brief a worker is pointed at when its PR is given the landing turn: the
    template's `landing` block — the `afk land` command and its outcome table —
    filled from the same `fields` as `render_worker_prompt` plus `landing`,
    {name: value} for every one of LANDING_FIELDS. It is the whole brief either way: for the worker that wrote
    the branch and is still there, and for one started in its worktree because
    it is gone — that one is briefed only to land the PR (ADR-0027).
    """
    block = _prompt_blocks(template)
    return _fill_prompt(block("landing"), fields, landing)


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

# Happy-path milestones, in order — these are the task-list checkboxes.
_STATUS_STEPS = (
    ("claimed",  "已认领 · worker 实现中"),
    ("pr_open",  "PR 已开{pr} · 等 {gate}"),
    ("landing",  "轮到落地 · worker 同步、过门、合并"),
    ("merged",   "已合并"),
)

# The closed set of lifecycle phases the board renders, each with everything the
# board says about it: how far along the happy path it has reached (the index of
# the last DONE step) and its single ▸/✅/⚠️ 'where are we now' line. Happy path
# plus five off-ramps that reuse the same checkboxes + an annotation: ci_failed,
# awaiting_turn (the PR is ready and waits for the landing turn — one PR lands
# at a time), escalated (a terminal give-up, ticked specially in
# `render_status_board`), closed (the worker found the issue already satisfied —
# `afk close`), and parked (the worker found an open dependency; the claim is
# released until it closes — `afk park`).
_NOTHING_REACHED = -1      # no step ticked: the phase is before, or outside, the happy path
_PHASES = {
    "claimed":        (0, "▸ 当前:worker 实现中,尚无 PR"),
    "pr_open":        (1, "▸ 当前:等 {gate}"),
    "ci_failed":      (1, "▸ 当前:{gate} 失败,修复重试中({attempt}/{retry_max}) —— 见下方 {gate} 与评论"),
    "awaiting_turn":  (1, "▸ 当前:PR 已就绪,排队等落地轮次 —— 一次只落地一个 PR,轮到后由 worker 自己合并"),
    "landing":        (2, "▸ 当前:已轮到落地,worker 正在与目标分支同步、过门并合并 —— 见 PR 评论"),
    "merged":         (3, "✅ 已合并,完成"),
    "escalated":      (1, "⚠️ 已升级给人处理 —— 见下方评论"),
    "closed":         (0, "✅ 主干已满足此需求,无需改动 —— 已关闭"),
    "parked":         (_NOTHING_REACHED, "⏸ 等待依赖 {blockers} 关闭 —— 关闭后自动重新派发,无需人工处理"),
}
STATUS_PHASES = tuple(_PHASES)


def render_status_board(phase, gate_ci, retry_max, instance=None, pr=None, attempt=0,
                        blocked_by=()):
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
      blocked_by: the open blockers the issue waits on, for parked only

    Returns the full markdown body, led by STATUS_MARKER (the find-or-create anchor).
    """
    if phase not in STATUS_PHASES:
        raise ValueError(f"unknown status phase: {phase!r}")
    gate = GATE_CI_MODES[gate_ci]
    reached, current = _PHASES[phase]
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
    lines.append(current.format(gate=gate, attempt=attempt, retry_max=retry_max,
                                blockers="、".join(f"#{n}" for n in blocked_by)))
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

      did_work:     the tick that just ran granted a turn / dispatched / reclaimed / escalated
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
# The cycle — gate, heartbeat, streaks and sleep as one state machine          #
# --------------------------------------------------------------------------- #
#
# Everything carried between cycles is ONE opaque value, the cycle state, which
# `afk cycle` hands back and takes again:
#
#   fingerprint         the digest the last gate computed (ADR-0007)
#   skips               consecutive skipped cycles
#   empty_streak        consecutive EMPTY cycles — a tick that did nothing with
#                       nothing in flight and nothing on the frontier, or a skip
#                       while that was still so
#   in_flight           claims held as the last tick ended
#   frontier_remaining  dispatchable issues the last tick left undispatched
#   unsettled           the last tick left a judgment open or met an error: the
#                       next cycle ticks whatever the digest says
#   instance            the fleet instance id      } the run's two launcher-held
#   worker_command      the worker launch command  } facts, passed once
#
# The caller never reads or edits a field; it is state for this code alone.

CYCLE_START = {"fingerprint": "", "skips": 0, "empty_streak": 0,
               "in_flight": 0, "frontier_remaining": 0, "unsettled": False}
CYCLE_FACTS = ("instance", "worker_command")

# What a tick reports having done, per issue number — each with the word the
# progress line uses for it. The first six are WORK: any of them keeps the fleet
# on the busy interval (`cycle_ticked`).
TICK_WORK = {
    "granted": "landing turn to",
    "dispatched": "dispatched",
    "reclaimed": "reclaimed",
    "cleared": "cleared",
    "escalated": "escalated",
    "parked": "parked",
}
TICK_DID = {**TICK_WORK, "retried": "retried", "nudged": "nudged"}
TICK_COUNTS = ("in_flight", "frontier_remaining")


def cycle_state(raw, instance=None, worker_command=None):
    """The cycle state from what the caller handed back (None / "" on the first
    cycle → CYCLE_START plus the two facts, which the first cycle must be given).
    Raises ValueError on anything that is not a state this code produced — a
    caller that mangled it must hear so, not run on zeros — and on a fact passed
    again that disagrees with the one the state carries."""
    given = {"instance": instance, "worker_command": worker_command}
    if raw is None or raw == "":
        missing = [f"--{k.replace('_', '-')}" for k, v in given.items() if not v]
        if missing:
            raise ValueError(f"the first cycle (no --state) needs {' and '.join(missing)}: "
                             f"they are carried in the state from then on")
        return {**CYCLE_START, **given}
    if not isinstance(raw, dict) or set(raw) != {*CYCLE_START, *CYCLE_FACTS} \
            or not all(isinstance(raw[k], str) and raw[k] for k in CYCLE_FACTS):
        raise ValueError(f"--state is not a cycle state carrying the instance id and the worker "
                         f"launch command (pass back the `state` the previous `afk cycle` "
                         f"returned, verbatim): {raw!r}")
    for key, value in given.items():
        if value and value != raw[key]:
            raise ValueError(f"--{key.replace('_', '-')} {value!r} is not the one --state carries "
                             f"({raw[key]!r}): omit it after the first cycle")
    return {"fingerprint": str(raw["fingerprint"]), "unsettled": bool(raw["unsettled"]),
            **{k: int(raw[k]) for k in ("skips", "empty_streak", *TICK_COUNTS)},
            **{k: raw[k] for k in CYCLE_FACTS}}


def cycle_wake(state, current_fp, config):
    """
    The top of one cycle: tick, or skip?

      state:      the cycle state (`cycle_state`)
      current_fp: `fingerprint` of what a rebuild would observe now; None when
                  `fingerprint_gate` is off (nothing was gathered)

    Returns {"action": "tick"|"skip", "reason", "state"}. A skip is the whole
    cycle, so it carries what a cycle owes: `sleep_seconds`, `progress`, and
    `heartbeat` — True when the fleet holds claims, so the effect layer refreshes
    the lease no tick will. On a tick the pass runs and `cycle_ticked` closes it.

    A cycle after a tick that left something `unsettled` — an open judgment, an
    error — ticks whatever the digest says: nothing may have moved, and the
    judgment is still owed. A skipped cycle extends the empty streak only while
    nothing is in flight and nothing is left on the frontier: unchanged state
    then proves the cycle empty.
    """
    if not config["fingerprint_gate"]:
        return {"action": "tick", "reason": "gate_off", "state": {**state, "skips": 0}}
    gate = fingerprint_gate(state["fingerprint"], current_fp, state["skips"],
                            config["force_tick_after_skips"])
    new = {**state, "fingerprint": current_fp, "skips": gate["skips"]}
    if gate["action"] == "tick":
        return {"action": "tick", "reason": gate["reason"], "state": new}
    if state["unsettled"]:
        return {"action": "tick", "reason": "unsettled", "state": {**new, "skips": 0}}
    if new["in_flight"] == 0 and new["frontier_remaining"] == 0:
        new["empty_streak"] += 1
    return {"action": "skip", "reason": gate["reason"], "state": new,
            "heartbeat": new["in_flight"] > 0,
            "sleep_seconds": pace(False, new["in_flight"], new["empty_streak"], config),
            "progress": f"nothing moved; {_standing(new)}"}


def cycle_ticked(state, did, config, judgments=0, errors=0):
    """
    The bottom of a cycle that ran a tick: fold what the tick did into the cycle
    state and say how long to sleep.

      did:       the tick's own account — a list of issue numbers per TICK_DID
                 key, and the two integers of TICK_COUNTS
      judgments: how many judgments the tick returned instead of deciding
      errors:    how many of its transitions failed

    Returns {"state", "sleep_seconds", "progress"}. A tick with open judgments
    sleeps 0: the caller answers them and opens the next cycle at once. One that
    left a judgment or an error is `unsettled`, and never counts as empty.
    """
    did_work = any(did.get(k) for k in TICK_WORK)
    in_flight, remaining = (int(did[k]) for k in TICK_COUNTS)
    unsettled = bool(judgments or errors)
    empty = not did_work and in_flight == 0 and remaining == 0 and not unsettled
    new = {**state, "in_flight": in_flight, "frontier_remaining": remaining,
           "unsettled": unsettled, "empty_streak": state["empty_streak"] + 1 if empty else 0}
    parts = [f"{word} {', '.join(f'#{n}' for n in did[k])}"
             for k, word in TICK_DID.items() if did.get(k)]
    parts += [f"{count} {noun}{'' if count == 1 else 's'}{tail}"
              for count, noun, tail in ((judgments, "judgment", " open"), (errors, "error", ""))
              if count]
    return {"state": new,
            "sleep_seconds": 0 if judgments else pace(did_work, in_flight,
                                                      new["empty_streak"], config),
            "progress": "; ".join([*parts, _standing(new)])}


def _standing(state):
    return (f"{state['in_flight']} in flight, "
            f"{state['frontier_remaining']} left on the frontier")


# --------------------------------------------------------------------------- #
# The tick's routing — which transition each row gets, and what is a judgment  #
# --------------------------------------------------------------------------- #
#
# After `rebuild` gives each claim a `status` and `no-pr` an `action`, the next
# `afk` call is a table lookup: `afk cycle` runs it. What code cannot decide is
# RETURNED as a judgment — a question, and for each answer the one transition to
# run. Every answer is a transition, so the next cycle does not ask again.

JUDGMENT_KINDS = ("empty_diff", "no_checks", "adversarial_verify", "reason")


def afk_command(call, sub, number, *flags):
    """One runnable `afk` transition on issue <number>, as a shell line.

      call:  {"afk_path", "repo", "config" (the run's config, as JSON),
              "instance", "worker_command"} — what every such line repeats
      flags: the transition's own, last — a `--reason` is the final argument
    """
    argv = [call["afk_path"], sub, "--issue", str(number), "--instance", call["instance"]]
    if sub in ("dispatch", "turn", "fail"):
        argv += ["--worker-command", call["worker_command"]]
    return shlex.join([*argv, "--repo", call["repo"], "--config", call["config"], *flags])


def judgment(kind, number, question, context, if_yes, if_no, bulky=False):
    """One judgment a tick returns instead of deciding. `bulky` marks one whose
    answer takes reading something long (a diff under review, a CI log): the
    caller delegates it to an ephemeral subagent that returns one line."""
    assert kind in JUDGMENT_KINDS, kind
    return {"issue": number, "kind": kind, "question": question, "context": context,
            "if_yes": if_yes, "if_no": if_no, **({"bulky": True} if bulky else {})}


def reason_judgment(call, number, sub, default, where, context=None, bulky=False):
    """A `reason` judgment: the transition is already fixed — `afk fail` or `afk
    escalate` — and what is asked for is its wording. Both answers are therefore
    the SAME command, runnable as it stands with `default`; the answer is the
    text put in place of it, after `--reason`."""
    command = afk_command(call, sub, number, "--reason", default)
    doing = "failure" if sub == "fail" else "escalation"
    return judgment("reason", number,
                    f"Word the {doing} reason for issue #{number}, re-read from {where}, and put "
                    f"it in place of the text after --reason.",
                    {"where": where, **(context or {})}, command, command, bulky=bulky)


def asks_after(mine):
    """The claims a tick asks `afk no-pr` about, in one call: every `no_pr` row,
    and every `landing` row whose worker has not stopped for the tick."""
    return [r["number"] for r in mine
            if r["status"] == "no_pr"
            or (r["status"] == "landing" and r["stopped"] not in LAND_WAITS)]


def turn_due(mine, merge_order):
    """The ONE issue a tick runs `afk turn` on, or None: the head of the merge
    queue — unless its PR holds the turn and its worker is at it."""
    row = next((r for r in mine if merge_order and r["number"] == merge_order[0]), None)
    if row is None or (row["status"] == "landing" and row["stopped"] not in LAND_WAITS):
        return None
    return row["number"]


def failure_judgment(call, row):
    """The judgment for a `failure` row — its PR's checks are red: the reason
    lives in a CI log, which is bulky to read."""
    return reason_judgment(call, row["number"], "fail", f"the checks of PR #{row['pr']} are red",
                           f"the failing checks of PR #{row['pr']}", {"pr": row["pr"]}, bulky=True)


def turn_step(call, result, config):
    """
    What a tick does with `afk turn`'s result → (do, judgment):

      ("granted", None)   the worker was told: count it
      ("leave", None)     waiting / landing / awaiting_ci: nothing this tick
      ("judge", {...})    gate_red → `reason`; no_checks → `no_checks`;
                          needs_verify → `adversarial_verify`

    With `gate.adversarial_verify` on, a PR with no checks at all owes both
    judgments, and `afk turn` records neither until both are in: they are asked
    as ONE `adversarial_verify` whose yes carries both flags — a head that
    survives the verify is one whose acceptance criteria are met.
    """
    number, pr, head, outcome = result["issue"], result["pr"], result["head"], result["outcome"]
    if outcome == "granted":
        return "granted", None
    context = {"pr": pr, "head": head}
    if outcome == "gate_red":
        return "judge", reason_judgment(call, number, "fail", f"the checks of PR #{pr} are red",
                                        f"the failing checks of PR #{pr}", context, bulky=True)
    verify = config["gate"]["adversarial_verify"]
    if outcome == "needs_verify" or (outcome == "no_checks" and verify):
        flags = [*(["--allow-no-checks"] if outcome == "no_checks" else []), "--verified", head]
        bare = " It has no checks at all, so the verify is its only gate." \
            if outcome == "no_checks" else ""
        return "judge", judgment(
            "adversarial_verify", number,
            f"Does head {head} of PR #{pr} survive the adversarial verify?{bare}",
            {**context, "prompt": config["gate"]["adversarial_verify_prompt"]},
            afk_command(call, "turn", number, *flags),
            afk_command(call, "fail", number, "--reason",
                        f"the adversarial verify refuted head {head} of PR #{pr}"), bulky=True)
    if outcome == "no_checks":
        return "judge", judgment(
            "no_checks", number,
            f"PR #{pr} has no checks at all — are issue #{number}'s acceptance criteria met?",
            context, afk_command(call, "turn", number, "--allow-no-checks"),
            afk_command(call, "fail", number, "--reason",
                        f"PR #{pr} has no checks, and it does not meet the issue's acceptance "
                        f"criteria"))
    return "leave", None


def worker_step(call, row, worker, config):
    """
    What a tick does about one claim `afk no-pr` was asked about → (do, detail):

      ("leave", None)       the worker is at it
      ("dispatch", None)    `redispatch` (its blockers closed) or `orphan` (no
                            worker left): `afk dispatch` — an orphaned claim is
                            ALWAYS continued, never released back
      ("park", None)        `afk park`
      ("nudge", None)       `afk nudge`
      ("escalate", reason)  `afk escalate`: the reason is on record
      ("fail", reason)      `afk fail`: the reason is on record
      ("judge", {...})      `close_release` → `empty_diff`; a failure or an
                            escalation whose reason is NOT on record → `reason`

      row:    the claim's `mine` row      worker: its `afk no-pr` row
    """
    number, action = row["number"], worker["action"]
    verdict = worker.get("worker_verdict") or {}
    said = {"verdict": verdict.get("comment_url")}
    if action == "leave":
        return "leave", None
    if action in ("redispatch", "orphan"):
        return "dispatch", None
    if action in ("park", "nudge"):
        return action, None
    if action == "close_release":
        base = config["base_branch"]
        return "judge", judgment(
            "empty_diff", number,
            f"Is the diff of issue #{number}'s branch against {base} really empty? Its worker "
            f"declared `already-satisfied`.",
            {"worktree": worker.get("worktree"), "base_branch": base, **said},
            afk_command(call, "close", number),
            afk_command(call, "fail", number, "--reason",
                        f"its worker declared `already-satisfied`, but the branch's diff "
                        f"against {base} is not empty"))
    if action == "escalate":
        unmet = [f"#{b['number']} {b['reason']}" for b in worker.get("blockers") or []
                 if b["standing"] == "unmet"]
        if unmet:
            return "escalate", f"blocked by a dependency nothing will resolve: {'; '.join(unmet)}"
        if verdict.get("reason"):
            return "escalate", f"its worker reported blocked, naming no blocker: {verdict['reason']}"
        return "judge", reason_judgment(
            call, number, "escalate", "its worker reported blocked without naming a blocker",
            "its worker's verdict comment", said)
    assert action == "next_attempt", action
    phase = verdict.get("phase") if verdict.get("found") else None
    if phase == _SATISFIED:
        return "fail", "its worker declared `already-satisfied`, but the branch holds changes"
    if phase == _GIVING_UP:
        if verdict.get("reason"):
            return "fail", f"its worker gave up: {verdict['reason']}"
        return "judge", reason_judgment(call, number, "fail", "its worker gave up",
                                        "its worker's verdict comment", said)
    if phase is not None or verdict.get("found"):
        return "fail", f"its worker's verdict names no phase the fleet knows ({phase!r})"
    if row["status"] == "landing":
        return "fail", (f"given the landing turn and never landed: "
                        f"{row.get('stopped') or 'no `afk land` outcome'}")
    if worker.get("nudged_at") is not None:
        return "fail", "idle with no PR and no verdict a grace period after its nudge"
    return "fail", "idle with no PR and no verdict, and no worktree here to nudge it in"


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
# never composes — supplied by the human, with the invocation or at bootstrap.
# That choice is what keeps the credential out of the fleet
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
                         closed=(), turns=None):
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
      closed:      the numbers of the claims whose issue is closed (`issues` holds
                   only open ones, so `afk rebuild` asks about each claim that is
                   missing from it). One of mine becomes a `closed` row; a stale
                   peer's moves from `stale` to `stale_closed`
      turns:       {number: its `held_turn`} for MY claims whose PR holds my
                   landing turn (`afk rebuild` asks about each of mine that has
                   a PR)

    Returns:
      {"frontier": {"dispatch": [{"number","title"}...], "excluded": [...]},
       "mine": [{"number","title","status","board_phase","pr","checks",
                 "attempt","stopped"}...],
       "merge_order": [number...],   # the `landing` and `awaiting_turn` rows (`turn_order`)
       "peer_live": [{"number","instance"}...],
       "stale": [{"number","instance","sha"}...],   # sha feeds reclaim --expect-sha
       "stale_closed": [{"number","instance","sha"}...],  # sha feeds release --expect-sha
       "free_slots": <how many workers may be dispatched: concurrency - len(mine)>,
       "fingerprint": <digest of the same observables the gate hashes>,
       "now": now}

    `status` and `board_phase` are `subclassify_pr`'s pair; `attempt` is
    `current_attempt` — the number `afk status` takes; `stopped` is the
    LAND_OUTCOMES word a `landing` row's `afk land` last stopped with, None while
    it has not stopped and on every other row.

    `stale` holds only work to continue: a stale claim on an OPEN issue. One whose
    issue is already closed — its worker landed it, or its fleet closed it, and the
    fleet died before releasing — is a phantom lock with nothing behind it, and is listed in
    `stale_closed` instead, to be deleted rather than taken and dispatched.
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
    closed = set(closed)

    def stale_rows(numbers):
        return [{"number": n, "instance": by_claim.get(n, {}).get("instance"),
                 "sha": by_claim.get(n, {}).get("sha")} for n in numbers]

    turns = turns or {}
    mine = []
    for n in part["mine"]:
        pr = pr_for.get(n)
        checks = pr_checks_state(pr.get("statusCheckRollup")) if pr else None
        issue = by_num.get(n, {})
        status, board_phase = subclassify_pr(pr is not None, checks, ci_mode,
                                             closed=n in closed, landing=n in turns)
        mine.append({"number": n, "title": issue.get("title"),
                     "status": status, "board_phase": board_phase,
                     "pr": pr.get("number") if pr else None, "checks": checks,
                     "attempt": current_attempt(issue.get("labels")),
                     "stopped": turns[n]["stopped"] if status == "landing" else None})

    return {"frontier": frontier,
            "mine": mine,
            "merge_order": turn_order(mine),
            "peer_live": [{"number": n, "instance": by_claim.get(n, {}).get("instance")}
                          for n in part["peer_live"]],
            "stale": stale_rows(n for n in part["stale"] if n not in closed),
            "stale_closed": stale_rows(n for n in part["stale"] if n in closed),
            "free_slots": max(0, int(config["concurrency"]) - len(mine)),
            "fingerprint": fingerprint(issues, prs, claims),
            "now": now}
