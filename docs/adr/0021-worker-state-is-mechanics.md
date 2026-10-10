# ADR-0021 — A worker's state is read from orca in code; a busy worker costs no GitHub read

**Status:** accepted — moves the "liveness probe" from the tick's judgment
([ADR-0004](0004-deterministic-mechanics-as-tools.md), [ADR-0008](0008-rebuild-as-one-observation-tool.md),
[ADR-0017](0017-the-act-half-is-transitions.md)) into `afk no-pr`, and replaces the terminal reading
that [ADR-0013](0013-liveness-is-recency-not-accumulated-work.md) and
[ADR-0018](0018-nudge-a-silent-worker-before-failing-it.md) took as an argument. Their routing of a
stopped worker is unchanged.

## Context

Asking after one PR-less claim took two steps. The tick looked at the worker's terminal with orca and
decided `busy`, `idle` or `none` — prose, several turns, a different reading from one tick to the
next. It then called `afk no-pr --terminal <…>`, which gathered **everything** before deciding
anything: a fetch of the base, the issue's comments, the open PRs, the hand-back marker, each
blocker's state. Measured, one such call is about 8 s, almost all of it network.

The usual answer is "still coding", and `classify_no_pr` returned it for a busy terminal without
looking at any of what had been gathered. The cost was paid on every tick, for every worker, to
confirm the common case.

Meanwhile orca already holds the answer. `orca worktree ps --json` (0.1 s, every worktree on the
machine in one call) carries, per worktree, the state each agent's **own hooks** reported —
`working`, `waiting`, `blocked`, `done` — with the time it changed, and the terminal's last output.
It is not inferred from titles or screens.

## Decision

1. **The worker state is mechanics.** `afk no-pr` reads it itself; the tick passes issue numbers and
   nothing else. `--terminal` and `--terminal-idle-seconds` are gone, and a tick types no orca
   command at all.
2. **Busy needs two signals.** A worker is busy when its runtime reports `working` **and** its
   terminal produced output within `worker_idle_grace_seconds`. A stop report that was lost (a crash,
   an interrupt orca did not see) would otherwise read `working` forever — the parked claim ADR-0013
   removed. With a stale terminal it is timed from the last output and routed like any silence.
3. **Stopped is timed from the stop.** `done`, `waiting` and `blocked` are all "stopped", idle since
   the state changed. `done` means only that the turn ended: the worker may have left a verdict, be
   waiting on a question, or have given up, and ADR-0013/0018's routing decides which. `waiting` gets
   no special path — it is an open question dialog, and a line typed into one is a keystroke, so it
   waits out the grace period and is nudged the usual way.
4. **Gather only what the reading leaves open.** Busy, or gone (no live terminal in the worktree),
   is settled from orca alone: no fetch, no GitHub. Only a stopped worker has its progress, verdict,
   blockers and hand-back read. Measured on live workers: 0.28 s busy, 7.8 s stopped.
5. **One call for every claim.** `afk no-pr --issue <n> --issue <m> …` → `{workers: […]}`: one orca
   read serves them all. It stays a separate call from `afk rebuild`, which remains
   machine-independent (ADR-0008).
6. **A runtime that reports no state is asked after through orca's own idle detection.** qoderclicn
   posts no hooks, so its worktree has no agent row. For such a worker the tool runs
   `orca terminal wait --for tui-idle` with a 2 s timeout: answered → idle, timed by the worktree's
   own clocks (commits, file writes); timed out → busy.
7. **An orca that cannot be asked is an error.** Read as "no terminal" it would start a second worker
   beside a live one. Both reads are hard, and a truncated `ps` page is an error too.

## Consequences

- A tick with N coding workers makes one `no-pr` call of a few hundred milliseconds instead of N
  probes and N eight-second gathers.
- The fleet now depends on orca's agent-status hooks. If they are absent (a worker launch command
  that points Claude at a config dir orca did not install into) the worker has no agent row and
  takes decision 6's path — slower by up to 2 s per busy worker, never wrong.
- A `ps` row is per **worktree**, not per terminal. A worker's worktree normally holds one terminal;
  a human who opens a second agent in it lends that agent's state to the reading. The top-level
  agent that changed state last speaks.
- The tick no longer has a say in whether a worker is alive. What stays judgment is what was always
  contestable: the empty-diff check, the reason given for a failure.

## Measured

- An idle Claude Code terminal emits nothing (`lastOutputAt` unchanged 52 min); a working one
  reports `working` and repaints continuously.
- An idle qoderclicn terminal **repaints about every 60 s**. Output recency would therefore read it
  as alive forever, which is why decision 6 does not use it. `tui-idle` answers for it in 0.1 s.
- **Not verified:** a *working* qoderclicn making `tui-idle` time out — the machine this was measured
  on had no model configured for it. orca detects it from the terminal title, as it does at dispatch
  (`afk dispatch` already waits on `tui-idle` before delivering the prompt). Check it on the first
  qoderclicn run.

## Considered and rejected

- **Replace the whole check with busy/stopped.** Stopped is not finished: it would lose the verdict
  routing and the nudge, and bring back ADR-0013's #139.
- **Output recency as the fallback.** Refuted by the qoderclicn measurement above.
- **A separate short threshold for "busy".** Any terminal activity within grace already classifies as
  coding; a second constant would only send more workers down the slow path.
- **A cap on how long `working` is believed.** Another number to tune, and it would interrupt real
  long runs; decision 2 covers the stuck report without one.
- **Nudge a `waiting` worker at once.** See decision 3.
- **Fold the read into `afk rebuild`.** It would make the working set depend on the machine
  (ADR-0008).
