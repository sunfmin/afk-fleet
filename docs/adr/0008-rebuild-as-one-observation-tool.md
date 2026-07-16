# The Rebuild's deterministic half is one read-only tool call: `afk rebuild`

**Status:** accepted — applies the [ADR-0004](0004-deterministic-mechanics-as-tools.md) bar to the
tick's *gather*, and closes the second-gatherer drift opened by
[ADR-0007](0007-fingerprint-gated-ticks.md).

## Context

The tick's Rebuild was prose orchestrating shallow calls: an ephemeral sub-read for the 200-issue
list, a hand-grafted join of three fields (`claimed` from `afk scan`, `has_open_pr` from open PRs'
closing refs, `open_blockers` from a per-candidate `gh api` loop) fed to `afk frontier`, then
`classify-claims` and a per-claim `subclassify` with its own PR/checks gathering. What a fresh-context
LLM had to learn to *call* the frontier was larger than what the frontier decides — a shallow module
— and every piece of it is deterministic gathering, none judgment. Worse, ADR-0007's `afk
fingerprint` gathers the same observables a second time in its own code path: two gatherers of one
fleet state, already divergent (the gate sees `blocked_by` only via `updatedAt`; the Rebuild read it
natively).

## Decision

One deep observation module. `afk rebuild --repo <r> --instance <id> --ttl <s> --ready-label …
--epic-labels …` returns the **complete working set** in one read-only call:

- `frontier.dispatch` (with titles) + `frontier.excluded` (with reasons — the plan tick renders
  these), `mine` subclassified (`awaiting_merge` / `awaiting_ci` / `failure` / `no_pr`) with PR
  number, checks state, and `afk-attempt/*` labels, `peer_live`, `stale` **with the sha `reclaim
  --expect-sha` needs**, the `fingerprint` digest, and `now`.
- **Strictly read-only.** Rebuild observes; Act (and only Act) writes. `--plan` = rebuild + format is
  therefore side-effect-free by construction and needs no authorization; the same holds for a gate's
  forced tick.
- **One internal gatherer.** `_gather()` (issues + PRs + ref scan) backs both `rebuild` and
  `fingerprint`; the digest is one pure function over the same canonical inputs in both paths, so
  the gate's view and the tick's view cannot drift. The gate keeps its cheap ADR-0007 interface and
  never pays the `blocked_by` reads (`updatedAt` covers those for digest purposes).
- **Blockers are fetched provisionally.** Rebuild runs the frontier once with blockers assumed 0;
  only the provisional dispatch set pays the per-issue `blocked_by` read, then the final assembly
  runs with real counts. Every other issue already failed a cheaper eligibility check.
- **The judgment line is unmoved.** Rebuild stops at `no_pr`; the orca liveness probe and the
  orphan-vs-alive verdict stay in the tick. Rebuild depends on git+gh only, and its output is
  machine-independent — required by the re-entrancy invariant ("any fresh tick produces the same
  working set from the same GitHub"), which a local-probe result would break.
- **The absorbed subcommands are demoted, not deleted.** `frontier`, `scan`, `classify-claims`,
  `subclassify` leave SKILL.md's Tools table (the tick's interface) but remain in the binary as
  debug surfaces over the same pure core.
- **Pure assembly, fixture-tested.** `assemble_working_set` (+ `pr_checks_state`) in `afk_decide.py`
  is the join that used to live in prose; `cmd_rebuild` is gather + assemble. All prior fixtures
  survive unchanged — rebuild *composes* the already-tested verdicts.

## Naming

The glossary already names this pass (**Rebuild**) and its output (**working set**); the tool takes
the existing name rather than coining "snapshot." The tool is the *mechanics half* of the glossary's
Rebuild — the same relationship `afk frontier` had to the Frontier concept.

## Considered and rejected

- **`afk snapshot` / `afk working-set`.** New vocabulary for a pass the glossary already names;
  CONTEXT.md's own rule is not to invent synonyms. Rejected.
- **Heartbeat piggyback** (rebuild refreshes the lease since every tick needs both). Rejected: plan
  ticks and unauthorized cold runs would then write a ref; read-only-by-construction is what makes
  `--plan` safe with zero authorization.
- **The gate calls full `rebuild` and compares digests.** Uniform but pays per-candidate
  `blocked_by` API reads on every launcher wake-up (~90 s cadence when busy) for frontier verdicts
  the gate never consumes. Rejected for the shared internal gatherer with two right-sized interfaces.
- **`rebuild --digest-only` replacing `fingerprint`.** Renames the interface (and config vocabulary)
  ADR-0007 just established, for no added depth. Rejected.
- **Liveness probe inside rebuild.** Adds an orca dependency to a git+gh module and makes the output
  depend on which machine ran it — breaking working-set equivalence across fresh ticks. Rejected.
- **Deleting the absorbed subcommands.** Leanest binary, but loses the hand-debugging surface
  (`afk scan` especially) for no interface gain — the Tools table shrinks either way. Demoted instead.
- **Taking `--config` JSON now.** Anticipates the (unbuilt) `afk config` candidate and forks the
  config-passing convention while `pace` alone uses `--config`. Flags now; one uniform convention
  change if/when `afk config` lands.

## Consequences

- New: `assemble_working_set`, `pr_checks_state`, `_closing_pr_map` in `afk_decide.py`
  (fixture-tested); `_gather` + `cmd_rebuild` in `afk.py`; `fingerprint` refactored onto `_gather`.
- SKILL.md's tick step 1 collapses from a three-bullet gather recipe (with inline `gh` incantations)
  to one call + the judgment that consumes it; the Tools table loses four rows and gains one.
- The tick no longer spawns an ephemeral sub-read for the frontier — the bulky JSON now dies inside
  a subprocess instead of a subagent context (ADR-0001 rule 5, strengthened: delegated to code).
- The stale-claim sha travels in the working set, so `reclaim --expect-sha` no longer depends on the
  tick remembering to run/parse a separate `scan`.
- `checks: null` (no CI configured) reaches the tick explicitly — the progressive gate's "no CI yet"
  case remains its judgment.
- CONTEXT.md's **Rebuild** entry records the mechanics/judgment split; the re-entrancy invariant is
  strengthened, not weakened (same inputs → same working set, now byte-stable through one code path).
