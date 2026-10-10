# ADR-0040 — A record is a TypedDict, read by subscript

**Status:** accepted — replaces the last sentence of decision 3 of
[ADR-0039](0039-the-scripts-stay-python-and-are-type-checked.md) ("a JSON object is
`afk_decide.Obj`; what its keys are stays where it is made"). The rest of ADR-0039 stands.

## Context

ADR-0039 put every function's types under the gate but left every record — the config, a landing
turn, a `mine` row, a gh row — as `Obj = dict[str, Any]`. So the keys, which are most of what the
decision core reads, were unchecked: `row["stauts"]` passed the gate.

Declaring the records was half of it. Measured on the pinned `ty`: a misspelled key in a subscript
is an error, a literal that leaves a required key out is an error, a word outside a field's
`Literal` is an error — and a misspelled key in `.get("…")` is not. The code read records mostly
through `.get`, each one a default for a key that was in fact always there.

## Decision

1. **A record the fleet makes is a TypedDict**, declared in `afk_decide.py` under "Types": the
   config, a turn, a verdict, a `mine` row and the working set, a merge batch and its members, the
   cycle state, a call, a judgment, a worker's reading, its classification and its `afk no-pr` row,
   git progress, a recovery plan and the two signals it is selected from.
2. **A row gh returns is one too, where our own projection names its keys** — `Issue`, `IssueRead`,
   `PullRequest`, `Comment` (afk.py's `_ISSUES_JQ`, `_ISSUE_JQ`, `_PR_FIELDS`), and `Claim`, which
   the ref scan builds. The projection is the promise that each key is there.
3. **A declared record is read by subscript, never `.get`.** A key that may really be absent is not
   defaulted: the record is `None` when there is none ("no turn", "progress not read", "the issue is
   closed and was not gathered"), and the code asks. A required key that is missing is a `KeyError`
   at the read, not a `None` that travels.
4. **What has no projection stays `Obj`, read with `.get` where it comes in**: what orca answers,
   the entries of a check rollup, a branch protection, a record's fields as its marker states them.
   `.get` is also still how a real mapping is looked up (`heartbeats.get(instance)`).
5. **A subcommand's result stays `Obj`**: only JSON reads it.
6. **Fixtures are whole.** A test builds a record from a blank that names every key (`_mine`,
   `_worker`, `_whole(_ISSUE, …)`, `next_turn(None, …)`), and says only what it is about.

## Consequences

- `afk no-pr` reports `progress: null`, not `{}`, for a worker whose reading alone settled it — the
  progress was not read, and `null` says so.
- The tests are still not type-checked (ADR-0039): 282 diagnostics when they are, nearly all of
  them fixtures that are deliberately looser than the records. They are held to running green.
- Only `ty` is the gate. `pyright` and `mypy` at their defaults still report findings of their own
  (11 and 26); none was found to be a live bug, and they are not run by the gate.
