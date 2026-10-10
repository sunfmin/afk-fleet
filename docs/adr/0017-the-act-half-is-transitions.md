# The Act half is transitions, not recipes: one call per change of a claim's state

**Status:** accepted; its `merge` transition is **superseded by [ADR-0027](0027-a-worker-lands-its-own-pr-on-a-landing-turn.md)** — the tick grants a landing turn (`afk turn`) and the worker lands the PR (`afk land`); its `afk cycle` row is **amended** (see [Amendment](#amendment-afk-cycle-runs-the-tick-in-code)) — the tick itself now runs inside it, in an order the pure core decides ([second amendment](#amendment-the-order-of-a-tick-is-decided-in-the-pure-core)); every other transition stands — extends [ADR-0004](0004-deterministic-mechanics-as-tools.md) from the
Observe half of a tick to the Act half, and [ADR-0008](0008-rebuild-as-one-observation-tool.md)'s
"one call, one answer" from `rebuild` to everything a tick *does*. Supersedes the parts of
[ADR-0016](0016-the-seam-enforces-its-own-rules.md) that named `afk next-attempt`, `afk pace` and
`afk fingerprint` (rule 5, rule 6), and replaces `afk gate-run` from
[ADR-0012](0012-local-completion-gate.md) with a step inside `afk merge`. The decisions those ADRs
made stand; the subcommands that carried them are gone.

## Decision

After ADR-0008 the *Observe* half of a tick was one tool call. The *Act* half was still prose: a tick
read a recipe in SKILL.md and typed `git`, `gh` and `orca` commands in an order the recipe described.
Every ordering rule, every flag that must not be forgotten, lived in a paragraph an LLM re-derived
each pass. Each of the following is now **one subcommand that performs its whole ordered sequence in
one process**, deciding through pure functions in `afk_decide.py`, and stopping with an `outcome`
exactly where the next move is judgment:

| Transition | Sequence (in this order) | Stops for the tick's judgment at |
|---|---|---|
| `afk dispatch` | read issue → claim (won / held / lost) → continuation tier → orca worktree at the right commit → agent started with the worker launch command → wait until ready → prompt rendered, delivered, submitted → status board | `--start fresh` (is the recovered state sane to build on?) |
| `afk merge` | require my claim → find the PR → worktree (the worker's, else recreated at the PR head) → sync by merge → push → machine gate on that head → adversarial-verify check → `gh pr merge --match-head-commit` → status board → release → cleanup | `conflict`, `gate_red`, `awaiting_ci`, `no_checks` (`--allow-no-checks`), `needs_verify` (`--verified <head>`) |
| `afk fail` | require my claim → read the attempt → **retry**: swap the attempt label, discard the failed attempt (PR, branch, worktree), start a fresh worker with the reason; or **escalate** | `--reason` |
| `afk escalate` | require my claim → status board → relabel → comment → **release last** | `--reason` |
| `afk close` | require my claim → status board → close the issue → release → cleanup | (the empty-diff check before calling it) |
| `afk cycle` | *(amended)* one call, the whole cycle: digest → tick-or-skip (+ the skipped cycle's heartbeat and sleep); on a tick, the pass itself — rebuild → `no-pr` → at most one `turn` → `nudge` / `fail` / `park` / `escalate` where the reason is on record → `release` → `reclaim` → `dispatch` → `heartbeat` → `status` — then fold what it did → sleep and a progress line | the `judgments` it returns: `empty_diff`, `no_checks`, `adversarial_verify`, `reason` |

Removed: `afk fingerprint`, `afk pace` (→ `afk cycle`), `afk next-attempt` (→ `afk fail`),
`afk gate-run` (→ `afk merge`). `afk claim` stays as the low-level step `dispatch` performs; `afk
status` stays for the non-terminal phases; `afk recovery` stays as the read-only look at what
`dispatch` would continue.

The supporting decisions:

1. **The launcher holds one opaque value.** `afk cycle` returns a `state` (`fingerprint`, `skips`,
   `empty_streak`, `in_flight`, `frontier_remaining` — and, since the amendment, `unsettled`, the
   instance id and the worker launch command) the launcher hands back verbatim. It keeps no
   counter and does no arithmetic. A mangled state is an error, never a fleet paced on zeros.
2. **A skipped cycle counts toward idleness.** A skip with nothing in flight and nothing left on the
   frontier extends the empty streak exactly like an empty tick. Holding a claim, or frontier the
   last tick could not take, resets it.
3. **A worker starts from the remote's tip, by sha.** `dispatch` fetches the tip of the base (or of
   the pushed branch, tier 2) into the checkout orca cuts worktrees from, hands orca the **sha**, and
   then asserts the worktree contains it (`merge --ff-only` if not). Worktree progress is likewise
   measured against the remote base tip, not the local branch of that name.
4. **The worker prompt is a template filled by code.** `references/worker-prompt.md` is named blocks
   with two variants (fresh / continue) and a retry-reason block; `render_worker_prompt` fills it with
   the branch and path orca actually returned and refuses to produce a prompt with a placeholder left
   in it.
5. **A fresh start discards the previous attempt.** `--start fresh` and a retry close the PRs the
   fleet opened for the issue (fleet-shaped head branch **and** closing the issue — never a human's
   PR), delete its work branches on the remote, and remove its worktree. Continuation (`--start
   auto`, the default) tears nothing down.
6. **Escalation relabels before it releases.** The claim is deleted last.
7. **What merges is what was gated.** In `required` mode checks count only when the sync did not
   move the head; `--verified` must name the head that lands; `gh pr merge` is pinned to that head
   with `--match-head-commit`.
8. **A claim that outlived its issue is visible.** `rebuild` reports a claim of mine whose issue is
   closed as `status: closed` (no board phase) — the tick releases it. The working set also carries
   `free_slots`.
9. **Every settling transition refuses a claim that is not mine** (`_require_mine`), and none
   reports success for a step that failed: a failing `gh pr merge`, relabel or close is exit 3 with
   the claim still held.
10. **orca is a hard dependency of the Act half** and stays a soft one for observation: `dispatch`,
    `merge`'s worktree recreation and a discard raise when orca fails; `recovery` and `no-pr` degrade
    to "no worktree on this machine".

## Why

Reading the prose as the program it was turned up defects no fixture could catch, because the pure
functions were right and the bug was in how prose wired them:

- **The idle cadence was unreachable.** `pace` read `empty_streak` from the tick summary. Nothing
  produced it: the tick's return schema had no such field and the launcher was told to keep "three
  small values", none of them a streak. A quiet fleet re-ticked every `busy_interval` forever.
- **Escalation opened a window onto the frontier.** The recipe released the claim, *then* removed
  `ready_label`. Between the two, a PR-less issue was unclaimed and ready: a peer could dispatch the
  issue a human had just been handed.
- **The retry label had a reader and no writer.** `current_attempt` parsed `afk-attempt/<n>`;
  swapping it was a sentence ("swap the label"), with a `from_label` that was null on the first retry
  and a `gh` call the label had to already exist for.
- **A retry looped on its own failure.** The recipe tore down the worktree and re-dispatched, but left
  the red PR open — so the next rebuild classified the claim as `failure` again, on the PR the retry
  was supposed to replace, and spent the next attempt without a worker having run.
- **A crash between merge and release left a ghost.** The issue closed, the claim stayed, and the
  row came back as a title-less `no_pr` — a worker to wait for, or re-dispatch.
- **"Start from the latest base" was three shell lines** and a paragraph explaining why two of them
  could be refused.

Each is an *ordering or wiring* property, and each is now a line of code under a test that fails when
the order is wrong: the escalate test makes the relabel fail and asserts the claim is still held; the
merge test makes `gh pr merge` fail and asserts nothing was released; the dispatch test advances the
base on the remote only and asserts where the worktree's HEAD is.

The second reason is the tick's context. A recipe is paid for in tokens every pass, and so is every
intermediate result (`orca worktree create`'s JSON, a `gh pr merge` transcript). A transition returns
one small object.

The line between mechanics and judgment (ADR-0004) is unchanged — it is easier to see. Everything a
tick still decides is now an **argument or an outcome** of a transition: the reason a failure is
given, whether a PR with no checks may merge, whether a verification passed, how a conflict resolves,
whether recovered state is worth continuing.

## Consequences

- SKILL.md's Act sections shrink to a call and an outcome table each. A tick types no raw `git`,
  `gh pr merge`, `gh issue edit`, `orca worktree` or `orca terminal create`; the one orca command it
  still runs is the liveness probe.
- The tests' stand-ins for the outside world became stateful (ADR-0016's "fake it where it lives"
  still holds): the fake `gh` applies label edits, closes and merges — refusing what GitHub refuses —
  and a merge moves the real target branch in the bare repo; the fake `orca` creates real git
  worktrees and records what each terminal was started with and told. Orca's response shapes were
  pinned against orca 1.4.
- An error in the middle of `dispatch` can leave a claim with no worker. That is deliberate and
  self-healing: the claim is mine, the next rebuild shows it as `no_pr`, the probe says there is no
  terminal, and `afk dispatch` continues it (`claim: held`).
- A retry now destroys the failed attempt's PR and branch. The failure is preserved where a human
  reads it — the reason in the next worker's prompt, the gate excerpt and the "superseded" note on the
  closed PR — not in an open PR nobody will merge.
- An escalated issue keeps its worktree and its PR: they are the evidence the human was handed.
  `worktree_cleanup` governs merges and closes only.
- Tier 2 continues on a **new** branch name (orca never reuses one), carried in the prompt; the old
  remote branch stays until the PR's merge or a fresh start removes it.

## Considered and rejected

- **Keep the recipes, add a linter for them.** A test that prose mentions `--enter` cannot test that
  the tick types it, or in which order.
- **One `afk tick` that does everything.** The judgment points are real; a single call would either
  have to embed an LLM or return a state machine the tick drives blindly. Transitions put the seam
  where the judgment is. *(Revisited by the amendment below: once the transitions existed, the
  single call needs neither — it returns the judgments as data.)*
- **Inject fakes into `afk.py` for the new effects.** Rejected again, for ADR-0016's reason: the
  seam under test is the process boundary, and the outside world is faked as executables on `PATH`.
- **Have `merge` resolve conflicts by re-dispatching a worker automatically.** A conflict the tick
  can read and resolve in one edit should not cost a retry; the tick decides, and `afk fail` is one
  call away.

## Amendment: `afk cycle` runs the tick in code

After this ADR, ADR-0021 and ADR-0022, almost everything a tick did was routing: `rebuild` gave each
claim a `status`, `afk no-pr` an `action`, and the next `afk` call was fixed by a table — a table
that lived in ~300 lines of SKILL.md a fresh LLM context re-read every tick. `afk cycle` now **runs
that table**: one call opens the cycle, decides tick-or-skip, and on a tick performs the rebuild and
every transition whose next step is a lookup, then folds its own account of what it did into the
state and returns the pace. `--summary` and `summary_schema` are gone: nothing carries a summary
between two calls.

- **What code cannot decide is returned, not decided** — as `judgments: [{issue, kind, question,
  context, if_yes, if_no}]`, each answer one runnable `afk` transition. There is no "resume with
  answers" interface: every answer is a transition, so the next cycle rebuilds from GitHub and does
  not ask again. A cycle with open judgments returns `sleep_seconds: 0`, and leaves the state
  `unsettled`, so the next cycle ticks even when the answer moved nothing the digest hashes. For a
  `reason` judgment the transition is already fixed and both answers are the same command: what is
  asked for is its `--reason` text.
- **A reason on record is not a judgment.** The pass fails, parks or escalates by itself wherever
  the reason is already written down — the worker's own `reason=`, the standing of the blockers it
  named, a silence that outlasted its nudge — and asks only where it must be re-read (a CI log, a
  verdict that gave none).
- **Two judgments are retired for unattended runs**, each to its safe default: an orphaned claim is
  always continued, never released back to the frontier; recovered state is always continued
  (`--start fresh` stays a flag for a human).
- **The two launcher-held facts ride in the state.** The instance id and the worker launch command
  are passed on the first cycle and carried in `state` from then on; a state without them is exit 3.
  The procedure being code and the facts being in the one value handed back is what lets a later
  change run the cycle from a session whose context may be compacted.
- **A failed transition settles nothing.** It is reported in the cycle's `errors`, its claim stays
  held, the rest of the pass goes on — except that a failure to *start* a worker ends the starting
  for that pass, so a fleet whose orca is down does not take claims it cannot staff — and the state
  is `unsettled`.
- **Judgments owed together are asked together.** `afk turn` records nothing until every judgment
  about a PR is in, so a PR with no checks under `gate.adversarial_verify` is asked one
  `adversarial_verify` whose yes carries both flags; a `no_checks` whose yes changed nothing would be
  asked forever.

The tick was still an Agent subagent at this amendment: its instructions were "run `afk cycle`,
answer the judgments". [ADR-0028](0028-the-launcher-runs-each-cycle-itself.md) removes it: the
launcher makes the call itself, and `afk cycle --drain` is the stop.

## Amendment: the order of a tick is decided in the pure core

The first amendment moved the tick's table into code, but into the wrong half of it. Every *step*
was decided by a pure function with a fixture — what to do about a stopped worker
(`worker_step`), about a turn's result (`turn_step`), about a batch's worker (`batch_step`), which
PR is due the turn (`turn_due`). How those steps were *put together* was not: the order they ran
in, the slot count, "at most one landing turn a tick", "a start that fails to begin ends the
starting", which claims count as settled and which had their board written — all of it lived in
`afk.py`, interleaved with the `gh`, `git` and `orca` calls. It could only be tested end to end,
against stand-ins for GitHub and orca, one subprocess per call. That is this ADR's own *Why* over
again, one level up: the pure functions were right, and a bug could hide in how they were wired.

The orchestration is now `afk_decide.tick_plan(working set, call, config)`:

- **It is asked in stages.** Later choices depend on earlier results — did the reclaim win, did the
  start begin, what did the turn answer — so the plan hands out ONE step (`{"do": <a TICK_STEPS
  key>, **arguments}`), is told how it ended, and only then says what comes next. It is a
  generator, which lets the order read top to bottom as the one procedure it is; `follow` is the
  loop that drives it.
- **The effectful side only carries it out.** `afk._tick` is a table from each step name to the
  transition that performs it, and an answer per step: `(result, None)`, or `(None, what it
  raised)`. It asks the decision core for nothing but the plan, branches on nothing, and keeps no
  count — a fixture reads its source to hold it to that.
- **The books are the plan's.** `TickBooks` — what was done, which claims were settled, taken or
  begun — moved with it, so `did`, `in_flight`, `frontier_remaining` and the slots still free are
  figures of the pure core, read back from one record.
- **A failed step is still an answer.** The plan records it in `errors` under the step's name,
  settles nothing, and goes on; a start that fails to begin ends the starting. Both are now rules
  with fixtures rather than the shape of a `try`.

Nothing a tick decides changed, no subcommand takes a different flag or prints a different result,
and `afk cycle` returns what it returned. What changed is where the proof lives: the order, the
slot accounting and each of the rules above are pinned by fixtures that start no process, and the
end-to-end tick tests that remain prove that a plan is *carried out* — that `park` parks and a lost
claim is answered as lost — not what the plan is.

Considered and rejected: **a function per stage** (`after_observe`, `after_turn`, …), each taking
the outcomes of the last — it splits one procedure across a state object the caller must thread
through in the right order, which is an ordering rule on the effectful side again; and **returning
the whole plan up front**, which cannot be done honestly: how many frontier issues are begun
depends on which starts a peer won.
