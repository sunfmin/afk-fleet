# ADR-0039 — The scripts stay Python, and the gate type-checks them

**Status:** accepted — adds `ty` to this repo's gate. Leaves the runtime dependencies of
[ADR-0004](0004-deterministic-mechanics-as-tools.md) as they are: `python3` + `git` + `gh`.

## Context

The question was whether the scripts would be better written in Deno. Measured on 2026-10-07
(14-core Apple M4 Pro, Python 3.14.6, Deno 2.9.1):

- The scripts are 7,300 lines of production code and 9,100 of tests (211 cases), with no
  third-party dependency.
- `afk.py --help` starts in 0.07–0.09 s; a minimal Deno script in 0.03 s warm. A tick's time is
  `git` and `gh`, not the interpreter.
- The suite takes 90 s of wall clock, 270 s of it system time: the tests fork `git` in sandbox
  repos. No language changes that.

So Deno's strengths — locked dependencies, single-file distribution, startup — buy nothing here,
and it would add a runtime to install on every machine a fleet runs on. What it would really give
is a type checker over the decision logic. Python has one.

Run over the production scripts for the first time, `ty` found 17 errors. None was a live bug, but
several were the shape of one: a field declared `str` that defaults to `None`; a `pr` subscripted
wherever a separate `turn` was truthy, the two tied only by an assignment forty lines up;
`batch_branch_regex()` called with neither argument failing as a `TypeError` inside `re`.

## Decision

1. **The scripts stay Python**, standard library only.
2. **The gate type-checks them.** `gate.local_command` runs `uvx ty@<version> check` before the
   tests. `ty.toml` at the repo root says what is checked: every script but the tests.
3. **The version is pinned in the command.** `ty` is pre-1.0 and each release finds new errors; an
   unattended worker's gate must go red only for what the worker changed.

## Considered and rejected

- **Rewrite in Deno.** Above: the whole suite re-proved, to gain a type checker.
- **A test that shells out to `ty`.** Keeps the command unchanged, but hides a five-second check
  behind a ninety-second suite, and its failure inside a pytest report.
- **Check the tests too.** 26 more errors, all from untyped sandbox fixtures; the tests are held to
  passing.

## Consequences

- A type error fails the gate in seconds, before any test runs.
- The scripts are mostly unannotated, so `ty` checks what it can infer: `None`-safety and call
  shapes, not yet the closed sets (`LAND_OUTCOMES`, `TICK_STEPS`, …), which are still tuples of
  `str`. Typing those as `Literal` and routing on them with `assert_never` is the next step, taken
  where a set is next changed rather than all at once.
- Bumping `ty` is a deliberate change: the pin moves together with whatever the new version finds.
