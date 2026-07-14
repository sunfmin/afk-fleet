# afk-fleet

An unattended standing fleet that works a GitHub-issue backlog on its own: a long-lived
coordinator dispatches worktree-isolated workers per ready issue, gates them, and auto-merges
green PRs to the target branch.

## Language

**Coordinator**:
The single long-lived Claude Code session that runs the standing loop — polls the frontier,
dispatches workers, gates, merges, and re-polls until stopped. There is exactly one.
_Avoid_: orchestrator, manager, controller, main agent

**Worker**:
An ephemeral Claude Code session, isolated in one git worktree, that owns exactly one issue,
opens a PR, and reports done. It never merges.
_Avoid_: agent (too generic), subagent, child

**Fleet state**:
The authoritative record of the fleet's progress — what is claimed, in-flight, gated, merged,
escalated, and retried. It lives *outside* any context, in GitHub (assignees, labels, PR state,
branches). It is the single source of truth; the coordinator never holds it in memory.
_Avoid_: progress, run state, memory

**Working set**:
The coordinator's disposable, in-context view of the fleet at one moment. It is derived from
fleet state and may be compacted, dropped, or rebuilt at any time without losing anything.
_Avoid_: coordinator memory, session state

**Frontier**:
The set of currently-dispatchable issues — `open` + `ready_label` + not an epic + unassigned +
zero open `blocked_by`. Recomputed from GitHub every poll.
_Avoid_: queue, backlog (the backlog is the whole issue set; the frontier is only the ready edge)

**Rebuild**:
The bounded pass, run at the top of every poll, that re-derives the whole working set from fleet
state: recompute the frontier, and reconstruct the in-flight set (`open & assignee=@me`,
sub-classified from each issue's PR + checks). A fresh coordinator and a continued one produce the
same working set from the same GitHub — this equivalence is the re-entrancy invariant.
_Avoid_: refresh, resync, reload

**Orphaned claim**:
An issue that is `open & assignee=@me` with no PR and no live worker — a claim whose worker crashed
or never started. Reconciled on every rebuild (torn down and re-dispatched, or released), never
assumed still-running.
_Avoid_: stuck issue, dead worker, zombie
