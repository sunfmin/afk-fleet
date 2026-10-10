# Completion gate

Disclosed reference for [`afk-fleet`](../SKILL.md): the invariants behind the landing gate and the
adversarial-verify procedure. `afk land` — the worker's — applies the configured machine gate itself; read this for
*why* the gate runs the way it does, and for the adversarial procedure when
`gate.adversarial_verify_prompt` is set.

A PR may land only when **all** configured gates are green. Which **machine gate** applies is
`gate.ci` ([ADR-0012](../../../docs/adr/0012-local-completion-gate.md)):

- **`required` (default) — the CI machine gate.** Wait for the PR's GitHub checks. `afk turn` grants
  the landing turn only when they are green, and `afk land` lands the PR only when they are green
  **on the head that lands**: if its sync moved the head, the old checks describe a tree that will
  not merge, so it waits — in that same run — for CI to speak about the head it pushed, then merges
  or returns `gate_red`. Only when that wait runs out (`--checks-timeout`, default 1800 s) does it return
  `awaiting_ci`: the worker stops, and the next pass's `afk turn` tells it to land again once CI has spoken. On red, read the failing-log excerpt for the `afk fail` reason in an **ephemeral sub-read** that
  returns only `{status: red, reason}`; raw logs never enter the launcher. Progressive: before CI exists a
  PR has no checks at all — `rebuild` reports it `awaiting_turn` (there is nothing to wait for), `afk turn` returns `no_checks`, the gate is then the issue's acceptance
  criteria + whatever local build/test exists, and `--allow-no-checks` is how you say it passed.
- **`local` — `gate.local_command` *is* the completion gate.** GitHub checks are **never read** in this
  mode (`rebuild` reports every open PR as `awaiting_turn`: gating is an **action the landing takes**,
  not an observation waited on). It runs twice in a PR's life — the worker runs it after its
  pre-PR sync, and **the landing runs it again** in the same worktree, on the head that lands — because
  the pre-PR pass tested pre-sync code, and two PRs can each be locally green yet conflict semantically. The
  invariant both runs serve: *what lands on the target branch was tested in the form it lands.* The
  landing's run happens inside `afk land`, after its sync and before `gh pr merge`. A target that
  moved while it ran would make the merge commit a tree no run saw, so the landing reads the
  target's tip again before merging and stops with `target_moved` instead — its next run syncs and
  gates again. A red run is the
  worker's own `outcome: gate_red` with `gate: {status, exit_code, excerpt, omitted_lines, timed_out}`,
  and it fixes the code and lands again — you are not involved, and no attempt is spent. A run
  that outlives `--gate-timeout` is red, never green by default. The `excerpt` is also
  **posted as a PR comment**, so if the worker gives up or goes silent, your `afk fail` reason is
  re-read from where the failure lives. A green landing carries `gate: {status, source, head, …}`:
  `source: run` — the gate ran here, on `head`. Adopting this mode is
  the repo's claim that its command is CI-equivalent, and it is expected to scope remote CI away from
  worker branches; bootstrap **hard-errors** when `base_branch` requires status checks (see
  [Bootstrap](../SKILL.md#bootstrap-once-with-the-human-present) step 2).
- **A recorded gate run — a tree is gated once**
  ([ADR-0030](../../../docs/adr/0030-a-gate-run-is-recorded-on-the-remote-under-the-tree-it-tested.md); `local` only). When the landing's sync is a no-op, the
  landing's run would test the very content the worker's pre-PR run did. The worker runs the gate
  **through `afk gate`** — its prompt hands it that line — which runs `gate.local_command` in the
  worker's worktree and, on green on a committed tree, puts the run on record **on the remote**: one
  ref, `refs/afk/gate/<tree>-<hash of the command>`, named for the tree it tested and the command
  that ran (a green run by `afk land` itself is recorded the same way). `afk land` skips its own
  run when — and only when — a record stands for **the tree that would land** and the command
  configured now, no more than a day old. It is found from anywhere that content is about to land:
  a worktree recreated from the pushed branch, another machine, another commit holding the same
  files. Anything else and the landing runs the gate exactly as above: the sync moved the head, the
  worker committed afterwards, the command changed, the worker typed the bare command (no record),
  the run was over uncommitted or untracked files or left a tracked file changed (no record), the
  same tree was run red or timed out since (that deletes the record), the remote refused the ref.
  **The landing's own run is held to the rule a record is written by** — `afk land` and
  `afk land --batch` alike: a green run counts only when the worktree was exactly its commit before
  the run (nothing uncommitted, nothing untracked) and is exactly its commit after it. Otherwise the
  landing **refuses** — an `"error"` naming the paths, nothing merged, nothing recorded, no attempt
  spent: with files lying around it does not run the gate at all, and a run that rewrote a tracked
  file, committed, or littered passed on something other than the commit that would land. The
  worker commits or discards them and lands again. A recorded run needs neither check: it is of the
  tree, whatever lies around it. The outcome says which happened:
  `gate.source: recorded` with `recorded_at`, or `gate.source: run` with `not_trusted: <why>`. The
  record is `afk`'s own, made from an exit code it observed; a worker reporting "the gate is
  green" proves nothing and leaves none. There is no switch. What is given up is a second,
  independent sample on unchanged content — a flaky test that passed once, or a gate that depends
  on the machine, is not asked again for a day.
- **A merge batch — one landing run for several PRs**
  ([ADR-0029](../../../docs/adr/0029-a-merge-batch-lands-n-prs-behind-one-gate-run.md); `local`
  only, with no `gate.adversarial_verify_prompt`; not an option). The landing's run is the fleet's landing throughput: N finished PRs are N runs.
  So when two or more finished PRs may land together the landing turn goes to all
  of them as a **merge batch**: a batch worker, in a worktree of the batch's own, stacks them on the
  target's tip — one merge commit per PR, in merge order — and `afk land --batch` runs
  `gate.local_command` **once, on the stack**, then pushes the stack to the target as a
  fast-forward. The invariant is kept literally — the commit the target is moved to is the commit
  the gate passed on — but what the gate proves is the **stack**, not each PR alone: the
  intermediate commits were never gated by themselves. The stack's tree is recorded and looked up
  like any other (ADR-0030): a stack already gated green — a landing cut short after its gate — is
  not gated again. A red run lands nothing and
  is the batch worker's `outcome: gate_red`: it fixes the stack with one more commit on top and runs
  the command again — nobody bisects for the PR at fault. The push is the only lock: a target that
  moved while the gate ran refuses it (`target_moved`), nothing lands, and the same command
  re-stacks and gates again. A PR that conflicts with the stack is left out and lands on a single
  turn. Never batched: a PR that owes an adversarial verify (so with `gate.adversarial_verify_prompt` set,
  none is), one whose own worker is still working, a peer's. The target must accept a direct push:
  bootstrap **hard-errors** when its protection requires pull request reviews, restricts pushes, or
  is locked.
- **Independent adversarial verification** (if `gate.adversarial_verify_prompt` is set) — a *separate* agent (not
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
