# Coordinator context is disposable; fleet state lives in GitHub

**Status:** accepted — its *single long-lived coordinator* premise is superseded by
[ADR-0002](0002-launcher-and-disposable-ticks.md) (launcher + disposable ticks); its
*fleet-state-in-GitHub* foundation (rules 1–5) stands and is what makes disposable ticks safe.

## Decision

The coordinator is a single long-lived session, but its context is a **disposable working set**,
not the fleet's memory. All **fleet state** — claimed, in-flight, gated, merged, escalated, retried
— lives in GitHub (assignees, labels, PR state, branches), which is the single source of truth. At
any moment the coordinator can be compacted, killed, or restarted and rebuild the identical working
set from GitHub with zero loss.

This is what makes an unattended, multi-day standing fleet possible at all: the alternative — the
context *being* the fleet's memory — has the context-window ceiling as a hard expiry date.

## Consequences

The decision is only real if it is enforced. Six rules do that:

1. **Re-entrancy invariant.** A fresh coordinator, given only the repo + config, rebuilds the same
   working set from GitHub and continues. Everything below exists to keep this true.
2. **Workers are never read.** A worker's result is observed as its **PR** (branch `issue-<n>-*`,
   body `Closes #<n>`); a blocker is observed as an **issue comment** it posts. The coordinator
   touches the worker terminal only for a bounded liveness probe — never to read transcript content.
3. **In-flight is reconstructed, not remembered.** In-flight = `open & assignee=@me`, sub-classified
   purely from each issue's PR + checks. `open & assignee=@me & no-PR & no-live-worker` is an
   *orphaned claim*, reconciled on every rebuild (re-dispatched or released) — this is crash recovery
   for free.
4. **Retry count is a label; failure reason is re-read.** Attempt count lives as an `afk-attempt/<n>`
   label (not in memory, not a checkpoint file — that would be a second source of truth). The failure
   reason fed to a retry is re-read from where it already lives: PR checks (CI), a PR review comment
   (adversarial refutation), or reproduced (rebase conflict).
5. **Bulky reads are delegated.** The frontier query (`gh … | select_frontier.py`) and the gate/CI
   read run in ephemeral subagents that return only a compact structured result
   (`{dispatch, excluded}`, `{status, reason}`). Raw JSON and CI logs live and die in the subagent,
   never in the coordinator. Cost: a few seconds of spawn latency per poll — free at a ~25-min cadence.
6. **Reset is taken, not merely allowed.** Each poll *recomputes* from GitHub rather than referencing
   the prior poll's in-context copy, so transient output is never carried forward. The idle
   `ScheduleWakeup` boundary (all slots free, frontier empty) is the designated drop-and-re-enter
   point — the working set is near-empty there anyway. Harness auto-compaction is the lossless safety
   net for a busy stretch that grows long before an idle boundary arrives.

## Considered and rejected

- **Durable context** (the context *is* the fleet's memory): can only slow growth, never reset;
  guaranteed to hit the window ceiling on a long run.
- **Checkpoint file** for retry count / in-flight: creates a second source of truth outside GitHub,
  which drifts. Rejected in favour of labels + reconstruction.
- **Read-and-summarize worker terminals:** the raw transcript transits the working set before it is
  summarized, and summaries drift. Rejected — GitHub artifacts are already durable and bounded.
