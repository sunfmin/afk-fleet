# Completion gate

Disclosed reference for [`afk-fleet`](../SKILL.md): the invariants behind the merge gate and the
adversarial-verify procedure. `afk merge` applies the configured machine gate itself; read this for
*why* the gate runs the way it does, and for the adversarial procedure when
`gate.adversarial_verify` is on.

A PR may merge only when **all** configured gates are green. Which **machine gate** applies is
`gate.ci` ([ADR-0012](../../../docs/adr/0012-local-completion-gate.md)):

- **`required` (default) — the CI machine gate.** Wait for the PR's GitHub checks. `afk merge` lands
  a PR only when they are green **on the head that lands**: the PR/check snapshot's `headRefOid`
  must equal the commit passed to `gh pr merge --match-head-commit`. If a worker push or the sync
  moved that head, it returns `awaiting_ci` and a later tick reads the current head's checks.
  On red, read the failing-log excerpt for the `afk fail` reason in an **ephemeral sub-read** that
  returns only `{status: red, reason}`; raw logs never enter the tick. Progressive: before CI exists a
  PR has no checks at all — rebuild routes it to `awaiting_merge` (board: `pr_open`), so `afk merge`
  can return `no_checks`; the gate is then the issue's acceptance
  criteria + whatever local build/test exists, and `--allow-no-checks` is how you say it passed.
- **`local` — `gate.local_command` *is* the completion gate.** GitHub checks are **never read** in this
  mode (`rebuild` reports every open PR as `awaiting_merge`: gating is an **action taken at merge
  time**, not an observation waited on). It runs twice in a PR's life — the **worker** runs it after its
  pre-PR sync, and the **tick re-runs it at merge time** in the branch's worktree — because the worker's
  pass tested pre-sync code, and two PRs can each be locally green yet conflict semantically. The
  invariant both runs serve: *what lands on the target branch was tested in the form it lands.* The
  merge-time run happens inside `afk merge`, after its sync and before `gh pr merge`; a red one comes
  back as `outcome: gate_red` with `gate: {status, exit_code, excerpt, omitted_lines, timed_out}` —
  deliberately the same compact shape as the CI sub-read, so a raw log never enters the tick. A run
  that outlives `--gate-timeout` is red, never green by default. The `excerpt` has already been
  **posted as a PR comment** by then, so the next attempt re-reads the failure from where it lives
  rather than from a dead tick's context. Adopting this mode is
  the repo's claim that its command is CI-equivalent, and it is expected to scope remote CI away from
  worker branches; bootstrap **hard-errors** when `merge.target` requires status checks (see
  [Bootstrap](../SKILL.md#bootstrap-once-with-the-human-present) step 2).
- **Independent adversarial verification** (if `gate.adversarial_verify`) — a *separate* agent (not
  the author, doesn't see its reasoning) re-derives the result and tries to **refute** it (e.g.
  re-solve and assert `final == official answer:`, audit the derivation). Refute-first: any
  refutation blocks the merge, is **posted as a PR review comment** (durable, re-readable on retry),
  and feeds back as a retry reason (`afk fail --reason`). It runs **after** the machine gate, on the
  exact head that would land: `afk merge` returns `needs_verify` with that `head`; verify it, then
  re-run `afk merge --verified <head>`. A verification of any other head does not count — a sync that
  moved the branch asks again.

## Local worktree and environment requirements

`afk merge` refuses staged/unstaged tracked changes and non-ignored untracked files before it
gates or pushes, including with `merge.sync_before_merge: false`. After the local gate, and again
before requesting the merge, it checks that HEAD is unchanged and the worktree/index remain clean.
Gate commands that leave source edits or untracked output stop the transition; no worker files are
automatically deleted. Commit intended source changes and explicitly ignore disposable build output.

This is a clean-worktree guard, not a hermetic sandbox. Stop concurrent writers while gating; a
temporary edit that is restored between checks cannot be detected. Ignored dependencies, build
artifacts and the process environment remain the repository's responsibility: use a command that
rebuilds from committed inputs, or use `required` CI when local environment parity is uncertain.
Head pinning also does not atomically pin the target branch; server-side protection/merge-queue
policy is needed to enforce integration against a concurrently advancing target.
