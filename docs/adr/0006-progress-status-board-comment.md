# Human-facing progress is one upserted status-board comment, derived from state — not an appended log

**Status:** accepted — applies [ADR-0004](0004-deterministic-mechanics-as-tools.md) (the render is a
pure `afk` tool) on top of [ADR-0001](0001-disposable-coordinator-context.md) /
[ADR-0002](0002-launcher-and-disposable-ticks.md) (disposable, re-entrant ticks) and
[ADR-0003](0003-cooperative-multi-fleet-claims.md) (claims own the issue).

## Context

A person watching a run wants to glance at an issue and tell **how far along it is**. Today the
issue surface shows labels, the linked PR + its checks, and — only on give-up — an escalation comment.
The real blind spot is structural: the **claim** lives in the hidden `refs/afk/*` namespace and the
fleet **dropped the assignee** as a signal (ADR-0003), so for the entire "claimed, worker coding, no
PR yet" phase the issue page shows *nothing new*. That is exactly the stretch a reader can't see.

The obvious fix — have the worker/tick **append a comment** at each step — collides with the fleet's
core properties. Ticks are **disposable and re-entrant** and workers are **re-dispatched on retry**
(ADR-0001/0002); a naive append posts duplicate "opened PR / CI red / retrying" comments every time a
tick re-runs or an issue is retried, turning the issue into noise. And any progress channel a **tick
reads back** would become a *second home* for facts the fleet already holds authoritatively (claim ref
+ PR + checks + `afk-attempt` label) — drift, and a re-entrancy hazard.

## Decision

Add an optional, **human-only** progress **status board**: a **single** comment per issue that the
**owning instance's tick upserts each rebuild**.

- **Human-read only.** Machine decisions keep reading only the existing SSOT (claim / PR / checks /
  labels). The board is *never parsed back* by a tick, so it can't drift them.
- **A rendering, not a new fact.** `phase` is derived from state the tick already computed this pass
  (`subclassify` + the merge/retry/escalate decision): `no_pr`+live→`claimed`, `awaiting_ci`→`pr_open`,
  `failure`→`ci_failed`, `awaiting_merge`→`awaiting_merge`, plus terminal `merged`/`escalated`.
- **Upsert, not append.** One comment tagged `<!--afk:status-->`, found-or-created by that marker and
  overwritten **only when the rendered text changed**. The body carries **no wall-clock time**, so
  identical lifecycle state renders identical text — re-entrant ticks and retries never churn or spam it.
- **Rendered as a GitHub task list** (`- [x]`/`- [ ]`) so the issue shows a progress meter, directly
  answering "how far along" — with the invisible claimed-no-PR phase now the first ticked box.
- **Written by the tick, ownership follows the claim.** A `peer_live` claim's board is left alone; on a
  stale **reclaim** the new owner takes over rendering. Terminal phases (`merged`, `escalated`) are
  upserted **before the claim is released**, while the issue is still one of `mine`.
- **Coexists with the durable comments.** The worker's blocker comment and the `escalate_comment` stay
  appended, durable, re-readable handoff records; the board merely *points to* them.
- **Extracted as an `afk` tool** (ADR-0004): pure `render_status_board` in `afk_decide.py`
  (fixture-tested), the find-or-create-by-marker + write-if-changed effect in `afk.py`.
- **Config-gated, default on** (`progress_comment: true`), matching `escalate_comment`. Editing a
  comment sends no notification (only the first create does), so the whole run disturbs subscribers once.

## Considered and rejected

- **Append a comment per milestone** (the first instinct). Reads as a native timeline, but is not
  idempotent under disposable/re-entrant ticks + retries — it spams. Making it idempotent needs a
  per-milestone marker scan and guard; more machinery than one upserted board, for a noisier result.
- **A machine-read progress channel** (a tick consumes the comments). Creates a second home for facts
  already authoritative in claim/PR/checks/labels → drift, and breaks the re-entrancy invariant.
- **Worker writes the board.** The worker knows only its own slice — not retry/escalate/merge — and is
  fire-and-forget; it would fragment ownership of a single board. The tick already derives every phase.
- **A status label or an issue-body section instead of a comment.** A label is *machine* state and would
  blur the human/machine line; editing the body fights a human's own edits. A progression reads
  naturally as one comment.
- **Put a timestamp / "last updated" in the body.** Defeats write-only-on-change (every tick would
  differ and rewrite), reintroducing churn. Omitted deliberately.
- **Do nothing — labels + PR already narrate.** They do for the PR-onward phases, but not for the
  claimed-but-no-PR phase, which is invisible (hidden ref namespace + dropped assignee) — the actual gap.

## Consequences

- New: `render_status_board` + `STATUS_MARKER`/`STATUS_PHASES` in `afk_decide.py` (with a fixture test),
  and the `afk status <n> --repo <r> --state <json>` subcommand (`--print` renders without touching gh).
- New config key `progress_comment` (default `true`); `<!--afk:status-->` joins the reserved,
  fleet-managed set (don't hand-edit).
- The board is a **derived projection** of fleet state, re-rendered each tick — it is *not* part of the
  durable state a fresh tick reconstructs from, and never a tick input. The re-entrancy invariant
  (ADR-0001) is unaffected.
- Runtime deps unchanged: `gh` (already used) issues the comment create/edit.
