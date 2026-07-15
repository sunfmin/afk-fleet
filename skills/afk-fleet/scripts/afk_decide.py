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


def subclassify_pr(pr_state, checks_state):
    """
    Classify one of MY in-flight claims from its PR + checks. Whether a "no_pr"
    claim is an *orphan* needs the worker liveness probe (orca-cli) — that stays
    the tick's judgment, combining this verdict with the probe result.

      pr_state:     "open" if an open PR closes the issue, else anything (→ no_pr)
      checks_state: "green" | "red" | "pending" | None
    Returns: "awaiting_merge" | "failure" | "awaiting_ci" | "no_pr".
    """
    if pr_state != "open":
        return "no_pr"
    if checks_state == "green":
        return "awaiting_merge"
    if checks_state == "red":
        return "failure"
    return "awaiting_ci"


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
    """
    busy = int(config["busy_interval_seconds"])
    idle = int(config["idle_interval_seconds"])
    threshold = int(config.get("idle_ticks_before_sleep", 3))
    ttl = config.get("claim_lease_ttl_seconds")

    did_work = bool(summary.get("merged") or summary.get("dispatched") or summary.get("reclaimed"))
    in_flight = int(summary.get("in_flight", 0))
    empty_streak = int(summary.get("empty_streak", 0))

    if did_work or in_flight > 0:
        interval = busy
    elif empty_streak >= threshold:
        interval = idle
    else:
        interval = busy  # recently active — stay responsive for stragglers

    if in_flight > 0 and ttl:
        interval = min(interval, int(ttl) // 2)
    return int(interval)
