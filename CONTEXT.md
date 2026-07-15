# afk-fleet

An unattended fleet that works a GitHub-issue backlog on its own, for days, without any session's
context growing without bound: a thin **launcher** spawns a fresh disposable **tick** each cycle;
each tick dispatches worktree-isolated **workers** per ready issue, gates them, and auto-merges green
PRs to the target branch.

## Language

**Launcher**:
The interactive session `/afk-fleet` is invoked in. It authorizes once, then loops: spawn a **tick**,
ingest its one-line summary, pace, repeat. It does no coordination itself, so its context stays flat
over a multi-day run. There is one.
_Avoid_: coordinator (deprecated — there is no single long-lived coordinator; a tick coordinates one
pass), orchestrator, manager, main agent

**Tick**:
One fresh-context, disposable reconciliation pass, run as an Agent subagent. It rebuilds the working
set from fleet state, acts once (merge green PRs, escalate exhausted, dispatch to free slots), returns
a compact summary, and dies — without waiting for the workers it dispatched. Runtime is bounded
because ticks are disposable, not because one session stays disciplined.
_Avoid_: batch (implies draining a whole wave), poll (a tick acts, not just observes), coordinator

**Worker**:
A fire-and-forget, ephemeral Claude Code session, isolated in one git worktree, that owns exactly one
issue, opens a PR, and reports done via GitHub. It never merges, and its terminal is never read for
its result.
_Avoid_: agent (too generic), subagent, child

**Fleet state**:
The authoritative record of the fleet's progress — what is claimed, in-flight, gated, merged,
escalated, and retried. It lives *outside* any context, in GitHub (assignees, labels, PR state,
branches). It is the single source of truth; no tick holds it in memory.
_Avoid_: progress, run state, memory

**Working set**:
A tick's disposable, in-context view of the fleet at one moment. It is derived from fleet state and
is dropped when the tick returns.
_Avoid_: coordinator memory, session state

**Frontier**:
The set of currently-dispatchable issues — `open` + `ready_label` + not an epic + unassigned + zero
open `blocked_by`. Recomputed from GitHub every tick.
_Avoid_: queue, backlog (the backlog is the whole issue set; the frontier is only the ready edge)

**Rebuild**:
The bounded pass, run at the top of every tick, that re-derives the whole working set from fleet
state: recompute the frontier, and reconstruct the in-flight set (`open & assignee=@me`,
sub-classified from each issue's PR + checks). Any tick — a fresh one or a later one — produces the
same working set from the same GitHub; this equivalence is the re-entrancy invariant that makes
disposable ticks safe.
_Avoid_: refresh, resync, reload

**Orphaned claim**:
An issue that is `open & assignee=@me` with no PR and no live worker — a claim whose worker crashed
or never started. Reconciled on every rebuild (torn down and re-dispatched, or released), never
assumed still-running.
_Avoid_: stuck issue, dead worker, zombie
