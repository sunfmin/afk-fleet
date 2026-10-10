# Deterministic mechanics live in tested code tools; the LLM tick orchestrates and judges

**Status:** accepted — extends [ADR-0002](0002-launcher-and-disposable-ticks.md) (the tick is still a
fresh-context LLM pass) and generalises the `select_frontier` precedent. Folds the standalone
`select_frontier.py` from [ADR-0003](0003-cooperative-multi-fleet-claims.md) into the tool.

## Context

The fleet is a doc-driven skill: `SKILL.md` is prose an LLM tick executes. `select_frontier.py` was
the one exception — the dispatch contract extracted as a pure, fixture-tested function because
`--plan` and the live loop must compute the *identical* frontier. The multi-fleet claim protocol
(ADR-0003) then added a large pile of deterministic-but-fiddly logic — atomic claim/reclaim/release
ref algebra, the mine-vs-live-peer-vs-stale partition, heartbeat/lease arithmetic, retry accounting,
pacing — all of it currently living as prose that a *fresh-context* LLM re-derives every tick, for
days. Prose re-interpreted N times is the opposite of a single source of truth, and several of these
operations fail **silently and catastrophically** when done slightly wrong (a phantom lock starves an
issue; a botched `--force-with-lease` steals live work; a missed race double-dispatches).

## Decision

Extract **every deterministic operation** into an `afk` tool the tick calls; keep **orchestration and
judgment** in the LLM tick. Concretely:

- **Bar for "code, not prose"**: an operation is a tool when getting it wrong is both *likely* (an LLM
  re-deriving it each tick drifts) and *silently catastrophic*, and it is shaped as a stable contract
  wherever it can be pure. (All three lenses considered — deterministic / correctness-critical /
  published-contract — collapse to: extract the deterministic mechanics.)
- **The LLM tick orchestrates; tools are muscle.** The tick decides *what* to do and interleaves
  judgment (gate, orphan-vs-alive, retry wording, escalation, human auth); it shells out to `afk
  <subcommand>` for the fixed mechanics and reads back JSON. We explicitly do **not** invert this into
  a script that calls the LLM as a subroutine.
- **Packaging**: one CLI, two layers — `afk_decide.py` (pure verdicts, no I/O, `now` injected,
  fixture-tested) and `afk.py` (thin git/gh effect layer + argparse subcommands, JSON out). The former
  is the single source of truth for every deterministic verdict; `select_frontier` moves into it.

## Why the LLM stays the orchestrator

ADR-0002 rejected a context-free shell/cron launcher because it removes the human and forces the
push+auto-merge authorization to be file-armed. A *script-orchestrates-LLM* tick slides toward that
same reject: once the control flow is code, the LLM becomes a callable and "just run it headless"
follows. Keeping the LLM as orchestrator preserves the interactive launcher, the per-run
authorization, and the tick's real value — interleaving judgment with mechanics per repo/issue. Tools
invoked *by* the LLM are the opposite move: they give the judgment loop more reliable muscle without
removing the judgment.

## Considered and rejected

- **Leave it all as prose** (extract nothing beyond `select_frontier`). Simplest, zero new surface,
  maximally portable — but bets fleet correctness on an LLM re-emitting `--force-with-lease` and
  race-detection flawlessly across thousands of ticks, where the failures are silent. Rejected.
- **Extract only the pure decision core, defer the effectful ref ops.** Safer to test first, but
  leaves the *most dangerous* operations (the actual git-ref races) as prose — exactly the ones the
  bar says to extract. Rejected in favour of extracting both now (with the effectful ops' full
  concurrency suite tracked as a follow-up integration test).
- **Script-orchestrates-LLM tick** (a `afk tick` that runs the whole pass, calling back for judgment).
  Rejected — see "Why", it fights ADR-0002.
- **Many standalone scripts** (one per operation, `select_frontier`-style). Duplicates ref-parsing /
  marker / time helpers and sprawls `scripts/`; worse for the LLM to learn than one `--json` CLI.
  Rejected for the single layered CLI.

## Consequences

- New: `scripts/afk_decide.py` (pure core), `scripts/afk.py` (CLI), `scripts/test_afk_decide.py`
  (fixture suite). `scripts/select_frontier.py` is removed; `afk frontier` preserves its interface.
- Runtime deps unchanged in spirit: `python3` (already required) + `git`/`gh` (already used).
- **Purity discipline**: `afk_decide.py` never reads the clock — `now` is always an argument — so
  fixtures pin behaviour. Time and all I/O live only in `afk.py`.
- **Test split**: pure verdicts are fixture-tested in `scripts/test_afk_decide.py`; the effectful **ref
  ops** are covered by `scripts/test_afk_refs.py` — a bare git repo on local disk standing in for
  GitHub, so the races that matter (concurrent claim, replayed `--force-with-lease`) are asserted
  offline, with no gh and no network (`GIT_ALLOW_PROTOCOL=file` enforces that). Both suites are
  ordinary `test_*` files, so one `pytest scripts` runs everything.
- The tick's judgment steps stay prose and are *not* tools: the gate, adversarial verify, sync-conflict
  resolution, the liveness-probe read, whether a recovered worktree is sane to build on, escalation
  wording, and the human authorization.
