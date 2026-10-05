# Completion gate

Disclosed reference for [`afk-fleet`](../SKILL.md): the invariants behind the landing gate and the
adversarial-verify procedure. `afk land` — the worker's — applies the configured machine gate itself; read this for
*why* the gate runs the way it does, and for the adversarial procedure when
`gate.adversarial_verify` is on.

A PR may land only when **all** configured gates are green. Which **machine gate** applies is
`gate.ci` ([ADR-0012](../../../docs/adr/0012-local-completion-gate.md)):

- **`required` (default) — the CI machine gate.** Wait for the PR's GitHub checks. `afk turn` grants
  the landing turn only when they are green, and `afk land` lands the PR only when they are green
  **on the head that lands**: if its sync moved the head, the old checks describe a tree that will
  not merge, so it returns `awaiting_ci`, the worker stops, and the next pass's `afk turn` tells it to land
  again once CI has spoken about that head. On red, read the failing-log excerpt for the `afk fail` reason in an **ephemeral sub-read** that
  returns only `{status: red, reason}`; raw logs never enter the tick. Progressive: before CI exists a
  PR has no checks at all — `rebuild` reports it `awaiting_turn` (there is nothing to wait for), `afk turn` returns `no_checks`, the gate is then the issue's acceptance
  criteria + whatever local build/test exists, and `--allow-no-checks` is how you say it passed.
- **`local` — `gate.local_command` *is* the completion gate.** GitHub checks are **never read** in this
  mode (`rebuild` reports every open PR as `awaiting_turn`: gating is an **action the landing takes**,
  not an observation waited on). It runs twice in a PR's life — the worker runs it after its
  pre-PR sync, and **the landing runs it again** in the same worktree, on the head that lands — because
  the pre-PR pass tested pre-sync code, and two PRs can each be locally green yet conflict semantically. The
  invariant both runs serve: *what lands on the target branch was tested in the form it lands.* The
  landing's run happens inside `afk land`, after its sync and before `gh pr merge`; a red one is the
  worker's own `outcome: gate_red` with `gate: {status, exit_code, excerpt, omitted_lines, timed_out}`,
  and it fixes the code and lands again — you are not involved, and no attempt is spent. A run
  that outlives `--gate-timeout` is red, never green by default. The `excerpt` is also
  **posted as a PR comment**, so if the worker gives up or goes silent, your `afk fail` reason is
  re-read from where the failure lives. A green landing carries `gate: {status, source, head, …}`:
  `source: run` — the gate ran here, on `head`. Adopting this mode is
  the repo's claim that its command is CI-equivalent, and it is expected to scope remote CI away from
  worker branches; bootstrap **hard-errors** when `merge.target` requires status checks (see
  [Bootstrap](../SKILL.md#bootstrap-once-with-the-human-present) step 2).
- **`gate.trust_recorded_run` — the two runs become one when nothing moved**
  ([ADR-0026](../../../docs/adr/0026-a-recorded-gate-run-stands-in-for-the-merge-time-run.md); `local`
  only, opt-in). When the landing's sync is a no-op, the landing's run tests the very commit the
  worker's pre-PR run did. The worker runs the gate **through `afk gate`** — its prompt hands it that line —
  which runs `gate.local_command` in the worker's worktree and, on green, records the head it ran on
  and the command it ran in the worktree's git dir (a green run by `afk land` itself is recorded the
  same way). With the option on, `afk land` skips its own run
  when — and only when — that record proves the gate passed on **the exact head that would land**:
  the recorded head is that head, the recorded command is the one configured now, and the tree it ran
  on had nothing uncommitted or untracked. Anything else voids the record — the sync moved the head,
  the worker committed afterwards, the command changed, the worker typed the bare command (no record),
  a red or timed-out run came after (it drops the record), the worktree was recreated on this machine
  — and the landing runs the gate exactly as above. The outcome says which happened:
  `gate.source: recorded` with the `head` it was recorded on and `recorded_at`, or `gate.source: run`
  with `not_trusted: <why the record was not enough>`. The record is `afk`'s own, made from an exit
  code it observed; a worker reporting "the gate is green" proves nothing and leaves none. What is
  given up is the second run's independence on an unchanged commit — a flaky test that passed once
  is not asked again — so the default is off.
- **`merge.batch` — one landing run for several PRs**
  ([ADR-0028](../../../docs/adr/0028-a-merge-batch-lands-n-prs-behind-one-gate-run.md); `local`
  only, opt-in). The landing's run is the fleet's landing throughput: N finished PRs are N runs.
  With the option on, when two or more finished PRs may land together the landing turn goes to all
  of them as a **merge batch**: a batch worker, in a worktree of the batch's own, stacks them on the
  target's tip — one squash commit per PR, in merge order — and `afk land --batch` runs
  `gate.local_command` **once, on the stack**, then pushes the stack to the target as a
  fast-forward. The invariant is kept literally — the commit the target is moved to is the commit
  the gate passed on — but what the gate proves is the **stack**, not each PR alone: the
  intermediate commits were never gated by themselves. A recorded run is never trusted here
  (`gate.trust_recorded_run` does not apply: no record is of a stack). A red run lands nothing and
  is the batch worker's `outcome: gate_red`: it fixes the stack with one more commit on top and runs
  the command again — nobody bisects for the PR at fault. The push is the only lock: a target that
  moved while the gate ran refuses it (`target_moved`), nothing lands, and the same command
  re-stacks and gates again. A PR that conflicts with the stack is left out and lands on a single
  turn. Never batched: a PR that owes an adversarial verify (so with `gate.adversarial_verify` on,
  none is), one whose own worker is still working, a peer's. The target must accept a direct push:
  bootstrap **hard-errors** when its protection requires pull request reviews, restricts pushes, or
  is locked.
- **Independent adversarial verification** (if `gate.adversarial_verify`) — a *separate* agent (not
  the author, doesn't see its reasoning) re-derives the result and tries to **refute** it (e.g.
  re-solve and assert `final == official answer:`, audit the derivation). Refute-first: any
  refutation blocks the landing, is **posted as a PR review comment** (durable, re-readable on retry),
  and feeds back as a retry reason (`afk fail --reason`). A worker never verifies itself, so it is
  settled **before** the turn, on the PR's exact head: `afk turn` returns `needs_verify` with that
  `head`, which the pass hands back as an `adversarial_verify` judgment; verify it, then run its
  `if_yes` — `afk turn --verified <head>`. The verified head travels with the
  turn, and `afk land` checks it after its machine gate — run or trusted from the record, it makes no
  difference: a verification of any other head does not count, so a landing whose sync moved the
  branch stops with `needs_verify`, and the next pass's `afk turn` asks again with the new `head`.
