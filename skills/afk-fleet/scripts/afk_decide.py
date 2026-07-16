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
                         ready_label, epic_labels):
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
                     "status": subclassify_pr("open" if pr else "none", checks),
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
