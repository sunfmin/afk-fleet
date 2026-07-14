#!/usr/bin/env python3
"""
select_frontier.py — the afk-fleet dispatch contract, as one pure function.

Given a normalized list of issues, decide which are dispatchable RIGHT NOW.
This is the single source of truth for "can a worker take this issue?", shared by
`--plan` (dry-run preview) and the live coordinator loop. Keeping it pure and
fixture-driven is what makes the fleet's core logic testable without touching gh,
git worktrees, or the network.

An issue is dispatchable iff ALL hold:
  - state == "open"
  - the ready label is present               (default: ready-for-agent)
  - NO epic/PRD label is present             (default: epic, prd, wayfinder:map)
  - it has no assignee                       (assignee == the claim marker)
  - it has zero OPEN blocking dependencies   (GitHub native blocked_by, open only)

Input shape (normalized; the live loop builds this from `gh` + the dependencies API):
  [{"number": 101, "state": "open", "labels": ["ready-for-agent"],
    "assignees": [], "open_blockers": 0}, ...]

Usage:
  select_frontier.py --fixture issues.json
  gh issue list --json number,state,labels,assignees ... | select_frontier.py --stdin
  select_frontier.py --fixture f.json --ready-label ready-for-agent \
                     --epic-labels epic,prd,wayfinder:map

Prints JSON {"dispatch": [num...], "excluded": [{"number","reason"}...]} and exits 0.
Exit 2 on malformed input.
"""
import argparse
import json
import sys


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


def _assignee_count(issue):
    a = issue.get("assignees", [])
    if isinstance(a, int):
        return a
    return len(a or [])


def _open_blockers(issue):
    """
    open_blockers may be given directly (fixture / precomputed), or derived from a
    blocked_by list of {"state": ...} objects (open ones count).
    """
    if "open_blockers" in issue and issue["open_blockers"] is not None:
        return int(issue["open_blockers"])
    bb = issue.get("blocked_by")
    if isinstance(bb, list):
        return sum(1 for b in bb if (b or {}).get("state", "open") == "open")
    return 0


def classify(issues, ready_label, epic_labels):
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
        if _assignee_count(issue) > 0:
            excluded.append({"number": num, "reason": "already claimed (has assignee)"})
            continue
        ob = _open_blockers(issue)
        if ob > 0:
            excluded.append({"number": num, "reason": f"{ob} open blocker(s)"})
            continue
        dispatch.append(num)
    return {"dispatch": dispatch, "excluded": excluded}


def main():
    ap = argparse.ArgumentParser()
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--fixture", help="path to a JSON list of normalized issues")
    src.add_argument("--stdin", action="store_true", help="read the JSON issue list from stdin")
    ap.add_argument("--ready-label", default="ready-for-agent")
    ap.add_argument("--epic-labels", default="epic,prd,wayfinder:map",
                    help="comma-separated labels that mark an issue as an epic/PRD (never dispatched)")
    a = ap.parse_args()

    try:
        raw = sys.stdin.read() if a.stdin else open(a.fixture).read()
        issues = json.loads(raw)
        if not isinstance(issues, list):
            raise ValueError("expected a JSON array of issues")
    except (OSError, ValueError, json.JSONDecodeError) as e:
        sys.exit(f"select_frontier: bad input: {e}")

    result = classify(issues, a.ready_label, [s for s in a.epic_labels.split(",")])
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0)


if __name__ == "__main__":
    main()
