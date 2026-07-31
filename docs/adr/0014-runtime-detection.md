# Runtime detection: one fleet run, one runtime

**Status:** accepted — amends the stock-detection path of [ADR-0010](0010-worker-launch-command.md).

## Decision

A fleet instance runs one **runtime** — the agent binary that executes its workers. The runtime is
detected at bootstrap from the launcher's own environment and determines the stock **worker launch
command** default:

- `QODERCN_CLI=1` in the environment → runtime `qoderclicn`, stock default
  `qoderclicn --dangerously-skip-permissions`. Always stock: no custom provider, no wrapping, no
  alias scanning, the human is never asked.
- Otherwise → runtime `claude`, and the full ADR-0010 provider-parity flow applies
  (`ANTHROPIC_BASE_URL` check, alias scanning, conditional ask).

Detection lives inside `afk worker-command` (the same tool that already reads the environment for
`ANTHROPIC_BASE_URL`). The returned JSON gains a `runtime` field. No mixing within a run: a
qoderclicn launcher dispatches qoderclicn workers; a Claude launcher dispatches Claude workers.

## Why

The fleet was built for Claude Code. qoderclicn is a second runtime with the same interaction model
(a TUI agent in an orca terminal) but no custom-provider ecosystem — it always uses its built-in
model, never wrapped. The entire ADR-0010 ask/alias-scan machinery exists to solve a problem
qoderclicn does not have (provider parity across wrapper aliases), so for qoderclicn it collapses to
a constant.

The detection signal (`QODERCN_CLI=1`) is set by qoderclicn itself in every child process — a brand
identification env var, structurally equivalent to Claude Code's session env vars. It is always
present inside a qoderclicn session and never present outside one, making it a reliable,
zero-configuration self-identification signal. No PATH probing, no `--runtime` flag, no config key.

Empirically verified before committing to this design:

- orca recognizes a qoderclicn terminal as a TUI agent (title `✦ Gemini CLI`);
- `orca terminal wait --for tui-idle` times out when qoderclicn is busy and satisfies immediately
  when idle — the liveness probe the `no_pr` five-way classification depends on works unchanged;
- `orca terminal send --text --enter` delivers a prompt to qoderclicn's input;
- qoderclicn supports `--dangerously-skip-permissions` (already in `YOLO_FLAGS`);
- the worker-prompt is runtime-neutral (no Claude-specific tool names or capabilities).

## Considered and rejected

- **A `--runtime` flag on `afk worker-command`.** Makes the launcher responsible for a fact the tool
  can discover itself from the environment it is already reading. Rejected: the tool is
  self-contained, and the launcher should not need to know its own runtime to call it.

- **PATH probing (`which qoderclicn`).** Ambiguous in a bare shell where both binaries exist; the
  question is not "what's installed" but "what am I running under." The env var answers that
  directly.

- **A `runtime` config key.** Same objection as `worker_command` in ADR-0010: the config is
  repo-shared, and the runtime is a machine-local property of the launcher session.

- **Runtime per worker (mixing within a run).** Technically possible (the command is opaque), but
  adds a degree of freedom no use case demands and complicates the domain model. Rejected: one
  fleet instance, one runtime.

## Consequences

- `afk_decide.py` gains `detect_runtime(env)` and `WORKER_COMMAND_DEFAULT_QODERCN`; `afk.py`'s
  `cmd_worker_command` short-circuits for qoderclicn before touching `ANTHROPIC_BASE_URL`.
- `launch_candidates()` and the `seen_whole` check remain Claude-only — they are unreachable from
  the qoderclicn path, and generalizing them adds code for a path that cannot execute.
- SKILL.md, CONTEXT.md, and the worker-prompt preamble use "runtime" and "coding agent" instead of
  "Claude Code" where they describe the generic role; "Claude" appears only where the Claude-specific
  ask flow is described.
- The domain model gains one term: **runtime** (values: `claude`, `qoderclicn`).
- A third runtime, if it ever appears, extends the detection with one more env check — no plugin
  registry needed.
