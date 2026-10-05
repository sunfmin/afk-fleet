# Completion gate

Disclosed reference for [`afk-fleet`](../SKILL.md): the invariants behind the merge gate and the
adversarial-verify procedure. `afk merge` applies the configured machine gate itself; read this for
*why* the gate runs the way it does, and for the adversarial procedure when
`gate.adversarial_verify` is on.

A PR may merge only when **all** configured gates are green. Which **machine gate** applies is
`gate.ci` ([ADR-0012](../../../docs/adr/0012-local-completion-gate.md)):

- **`required` (default) — the CI machine gate.** Wait for the PR's GitHub checks. `afk merge` lands
  a PR only when they are green **on the head that lands**: if its sync moved the head, the old
  checks describe a tree that will not merge, so it returns `awaiting_ci` and a later tick merges.
  On red, read the failing-log excerpt for the `afk fail` reason in an **ephemeral sub-read** that
  returns only `{status: red, reason}`; raw logs never enter the tick. Progressive: before CI exists a
  PR has no checks at all — `afk merge` returns `no_checks`, the gate is then the issue's acceptance
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
  rather than from a dead tick's context. A green merge carries `gate: {status, source, head, …}`:
  `source: run` — the gate ran here, on `head`. Adopting this mode is
  the repo's claim that its command is CI-equivalent, and it is expected to scope remote CI away from
  worker branches; bootstrap **hard-errors** when `merge.target` requires status checks (see
  [Bootstrap](../SKILL.md#bootstrap-once-with-the-human-present) step 2).
- **`gate.trust_recorded_run` — the two runs become one when nothing moved**
  ([ADR-0026](../../../docs/adr/0026-a-recorded-gate-run-stands-in-for-the-merge-time-run.md); `local`
  only, opt-in). When the merge-time sync is a no-op, the merge-time run tests the very commit the
  worker's run did. The worker runs the gate **through `afk gate`** — its prompt hands it that line —
  which runs `gate.local_command` in the worker's worktree and, on green, records the head it ran on
  and the command it ran in the worktree's git dir. With the option on, `afk merge` skips its own run
  when — and only when — that record proves the gate passed on **the exact head that would land**:
  the recorded head is that head, the recorded command is the one configured now, and the tree it ran
  on had nothing uncommitted or untracked. Anything else voids the record — the sync moved the head,
  the worker committed afterwards, the command changed, the worker typed the bare command (no record),
  a red or timed-out run came after (it drops the record), the worktree was recreated on this machine
  — and the merge runs the gate exactly as above. The outcome says which happened:
  `gate.source: recorded` with the `head` it was recorded on and `recorded_at`, or `gate.source: run`
  with `not_trusted: <why the record was not enough>`. The record is `afk`'s own, made from an exit
  code it observed; a worker reporting "the gate is green" proves nothing and leaves none. What is
  given up is the second run's independence on an unchanged commit — a flaky test that passed once
  is not asked again — so the default is off.
- **Independent adversarial verification** (if `gate.adversarial_verify`) — a *separate* agent (not
  the author, doesn't see its reasoning) re-derives the result and tries to **refute** it (e.g.
  re-solve and assert `final == official answer:`, audit the derivation). Refute-first: any
  refutation blocks the merge, is **posted as a PR review comment** (durable, re-readable on retry),
  and feeds back as a retry reason (`afk fail --reason`). It runs **after** the machine gate — run
  or trusted from the record, it makes no difference — on the exact head that would land: `afk merge` returns `needs_verify` with that `head`; verify it, then
  re-run `afk merge --verified <head>`. A verification of any other head does not count — a sync that
  moved the branch asks again.
