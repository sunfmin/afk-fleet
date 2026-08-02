# Completion gate

Disclosed reference for [`afk-fleet`](../SKILL.md): the invariants behind the merge gate and the
adversarial-verify procedure. The executable per-mode actions stay inline in the skill's Merge step;
read this for *why* the gate runs the way it does, and for the adversarial procedure when
`gate.adversarial_verify` is on.

A PR may merge only when **all** configured gates are green. Which **machine gate** applies is
`gate.ci` ([ADR-0012](../../../docs/adr/0012-local-completion-gate.md)):

- **`required` (default) — the CI machine gate.** Wait for the PR's GitHub checks. Read the checks
  (and, on red, the failing-log excerpt) in an **ephemeral sub-read** that returns only
  `{status: green|red, reason}`; raw logs never enter the tick. Progressive: before CI exists, the gate
  is the issue's acceptance criteria + whatever local build/test exists.
- **`local` — `gate.local_command` *is* the completion gate.** GitHub checks are **never read** in this
  mode (`rebuild` reports every open PR as `awaiting_merge`: gating is an **action taken at merge
  time**, not an observation waited on). It runs twice in a PR's life — the **worker** runs it after its
  pre-PR sync, and the **tick re-runs it at merge time** in the branch's worktree — because the worker's
  pass tested pre-sync code, and two PRs can each be locally green yet conflict semantically. The
  invariant both runs serve: *what lands on the target branch was tested in the form it lands.* One
  call, deliberately the same compact shape as the CI sub-read, so a raw log never enters the tick:
  ```bash
  python3 <skill>/scripts/afk.py gate-run --worktree <path> --config '<config json>'
  # → {status: green|red, exit_code, excerpt, omitted_lines, timed_out}
  ```
  A red run's `excerpt` is **posted as a PR comment** before the retry ladder, so the next attempt
  re-reads the failure from where it lives rather than from a dead tick's context. Adopting this mode is
  the repo's claim that its command is CI-equivalent, and it is expected to scope remote CI away from
  worker branches; bootstrap **hard-errors** when `merge.target` requires status checks (see
  [Bootstrap](../SKILL.md#bootstrap-once-with-the-human-present) step 2).
- **Independent adversarial verification** (if `gate.adversarial_verify`) — a *separate* agent (not
  the author, doesn't see its reasoning) re-derives the result and tries to **refute** it (e.g.
  re-solve and assert `final == official answer:`, audit the derivation). Refute-first: any
  refutation blocks the merge, is **posted as a PR review comment** (durable, re-readable on retry),
  and feeds back as a retry reason.
