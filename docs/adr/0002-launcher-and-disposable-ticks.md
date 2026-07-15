# Unbounded runtime: a launcher spawning disposable reconciliation ticks

**Status:** accepted — supersedes the *single long-lived coordinator* premise of
[ADR-0001](0001-disposable-coordinator-context.md); keeps its *fleet-state-in-GitHub* foundation.

## Decision

There is no long-lived coordinator. A thin **launcher** (the interactive session) loops: spawn one
fresh-context **tick** (an Agent subagent), ingest a one-line summary, pace, repeat. Each **tick**
does exactly one reconciliation pass against GitHub — merge green in-flight PRs, escalate exhausted
ones, dispatch to fill free slots — then returns a compact summary and dies, **without waiting** for
the workers it dispatched. Runtime is unbounded because ticks are disposable, not because one session
stays disciplined.

ADR-0001 already put all fleet state in GitHub and made a context a re-entrant *working set*. That is
exactly what makes ticks disposable: any fresh tick rebuilds the identical working set, so throwing a
tick away after one pass loses nothing. This ADR changes only **who** holds that working set and for
**how long** — from one long-lived session to a stream of short ones.

## Why

ADR-0001 made boundedness *disciplinary*: one session that recomputes each poll, drops its working
set at idle, and leans on auto-compaction. Two gaps remained. The session still accrues tokens every
busy poll, and — decisively — **`ScheduleWakeup` does not reset context**; it resumes the *same*
cached session. So "reset at the idle boundary" dropped the working set but not the token history;
auto-compaction was the only real backstop. Disposable ticks make the reset **structural**: each
tick is a brand-new context, discarded on return, and the launcher only ever holds a constant
per-tick summary.

## Considered and rejected

- **Keep the single long-lived coordinator (ADR-0001 as-is).** Boundedness stays disciplinary and
  `ScheduleWakeup`-bound; "彻底" (definitive) it is not. Superseded here.
- **Context-free shell/cron launcher** spawning headless `claude -p` ticks. The most literally
  bounded (the launcher has no context), but it removes the human from the loop and forces the
  one-time push+auto-merge authorization to be file- or flag-armed — which we refuse. Rejected in
  favour of an interactive launcher.
- **Drain-a-wave tick** (stay alive until the dispatched workers merge). Longer-lived tick, bigger
  tick context, no cross-tick merge latency. Rejected: a reconciliation tick (observe → act → exit)
  keeps each tick's work constant and small; at most one tick-interval of merge latency is cheap under
  adaptive pacing.
- **Fixed-K / token-budget tick cutoff.** A hard bound, but exits at unnatural points (mid-merge).
  Rejected in favour of the natural reconciliation boundary.

## Consequences

- Published modes: `/afk-fleet` = **launcher** (default); `--tick` = one reconciliation pass (what the
  launcher spawns, also runnable headless); `--plan` = dry-run preview.
- **Authorization is per-run, launcher-held.** Confirmed interactively once at bootstrap (the human is
  present), injected into each tick's spawn prompt, gone when the launcher stops. Not a config key; no
  dead-man re-confirm (that would fight permanent unattended runtime). A cold `--tick` without an
  injected authorization dispatches + gates but **holds merges**.
- **Pacing is adaptive**: busy interval (~1.5 min) when the last tick had work or in-flight PRs, idle
  interval (~25 min) otherwise; N empty ticks → idle cadence.
- Merges are eventually-consistent within one tick interval, not immediate — the accepted cost of
  structural boundedness.
- ADR-0001's rules 1–5 (re-entrancy, workers-never-read, in-flight-reconstructed, retry-label +
  reason-re-read, bulky-reads-delegated) carry over unchanged and now run *inside a tick*. Its rule 6
  ("reset taken at the idle boundary + auto-compaction") is replaced by structural per-tick reset.
