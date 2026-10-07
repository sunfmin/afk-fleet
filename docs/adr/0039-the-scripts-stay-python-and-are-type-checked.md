# ADR-0039 — The scripts stay Python, and the gate type-checks them

**Status:** accepted — adds `ruff` and `ty` to this repo's gate, and types the production scripts. Leaves the runtime dependencies of
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
   tests. `ty.toml` at the repo root says what is checked — every script but the tests — and as
   which Python: 3.9, macOS's own `python3`, the oldest the scripts run on.
3. **Every function states its types.** Parameters and return, nested functions included; the gate
   runs `uvx ruff@<version> check` with the `ANN` rules (`ruff.toml`), so one that does not is red.
   A JSON object — a config, a `gh` row, a result — is `afk_decide.Obj`; what its keys are stays
   where it is made.
   *(Replaced by [ADR-0040](0040-a-record-is-a-typeddict-read-by-subscript.md): a record is a
   TypedDict, read by subscript.)*
4. **A closed vocabulary is a `Literal`.** Declared beside its words in `afk_decide.py`: a tuple of
   them is read off the Literal (`get_args`), and a table of them is keyed by it, so a word
   misspelled where it is used is a type error. Where a chain of branches handles every word it
   ends in `afk_decide.assert_never`, and a word added without its branch is a type error too.
5. **The versions are pinned in the command.** `ty` is pre-1.0 and each release finds new errors; an
   unattended worker's gate must go red only for what the worker changed.

## Considered and rejected

- **Rewrite in Deno.** Above: the whole suite re-proved, to gain a type checker.
- **A test that shells out to `ty`.** Keeps the command unchanged, but hides a five-second check
  behind a ninety-second suite, and its failure inside a pytest report.
- **Check the tests too.** 26 more errors, all from untyped sandbox fixtures; the tests are held to
  passing.

## Consequences

- A type error fails the gate in seconds, before any test runs.
- A table keyed by a Literal cannot name a word the Literal lacks, but can lack one it has: one
  test holds each table to its Literal. The two assertions that listed `WORKER_CAUSES`' steps by
  hand are gone — a step that is not a `WorkerStep` no longer type-checks.
- What is typed is the vocabulary and the shapes of calls, not the keys of a JSON object: `Obj` is
  `dict[str, Any]`, and a misspelled key is still the tests' to catch.
- `from __future__ import annotations` heads each script, so a signature may say `str | None` on
  Python 3.9; a type evaluated at run time — an alias — is spelled `Optional[...]`.
- Bumping `ruff` or `ty` is a deliberate change: the pin moves together with whatever the new
  version finds.
