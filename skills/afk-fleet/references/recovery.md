# Recovery by continuation (a dead claim is continued, never restarted)

Disclosed reference for [`afk-fleet`](../SKILL.md), reached only on a **dead claim** — an orphaned
claim of mine, a stale claim reclaimed from a dead peer, or one inherited through a takeover — and
only when you doubt what would be continued. Recovery itself is not a procedure you run: `afk dispatch
--issue <n>` selects the tier and acts on it, and the pass inside `afk cycle` calls it for every dead
claim without asking. This file is the tier table it follows, and the one override it leaves to a
human.

A claim whose worker died is recovered *from its durable progress*, never re-dispatched from base
while progress exists. Workers push after every completed step (see
[worker-prompt](worker-prompt.md)), so that progress is real and reachable: the local
worktree if it is still on this machine, else the branch tip on GitHub ([ADR-0011](../../../docs/adr/0011-takeover-and-progress-preservation.md)).

`afk dispatch` asks `orca worktree list` whether a worktree for this issue is still here, finds the
issue's branch on the remote among the fleet's own (below), compares it against the remote's
`base_branch`, and then acts:

| tier | action | what `afk dispatch` does | prompt |
|---|---|---|---|
| **1** | `reuse_worktree` | The worktree is still on this machine: it is **kept**. The dead worker's terminal is closed and a new worker started *inside it*, on the same branch. Lossless: even uncommitted work survives. | continue |
| **2** | `recreate_at_tip` | No local worktree, but the branch is ahead of base: orca recreates one at the **pushed branch tip** and the worker continues there (on a new branch name — orca never reuses one — which the prompt carries). Loss is bounded to "since the last push". | continue |
| **3** | `dispatch_fresh` | Nothing survived: a new worktree at the remote base tip. | fresh |

**What marks a branch as the fleet's.** The fleet says so on the issue: each time it has orca cut a
worktree for the issue — a first dispatch, a tier-2 recreation, a retry — it posts one comment there
carrying `<!--afk:branch name=<branch>-->`, the name orca gave (`<user>/issue-<n>-<slug>`, with a
`-2`, `-3`… suffix when the name was taken — a continuation's branch is a new one, recorded the same
way). The branches recorded on the issue, plus the branch of the worktree orca links to the issue on
this machine, are the fleet's own for it ([ADR-0043](../../../docs/adr/0043-a-branch-is-the-fleets-because-the-fleet-recorded-it.md)). **Nothing is read off the name**: a branch a person pushed as
`hotfix/issue-<n>-…` is not continued from, and not deleted when the attempt is discarded; neither
is a PR opened from it closed. Deleting that comment is how to take a branch away from the fleet.

Nothing is torn down on any tier. `prompt` names the [worker-prompt](worker-prompt.md) variant that
was delivered: its **continue-mode variant** (inspect the existing progress first, treat it as partial
work toward the *same* acceptance criteria) or the fresh one. A surviving worktree with provably
nothing in it gets the fresh prompt — tier 1 is about never destroying a worktree, not about
pretending there is progress.

**The claim is kept** throughout (you already own the issue — the result says `"claim": "held"`), and
the `afk-attempt/<n>` counter is neither read nor incremented: continuation answers *"did the
**worker** die?"*, the retry ladder answers *"is this **work** failing?"* — different axes (ADR-0011).
Because progress accumulates across continuations, a claim recovered repeatedly *converges* instead of
looping.

**An unattended run always continues.** The tier selection is mechanics, and a tick acts on it with
no judgment asked: an orphaned claim is never released back to the frontier, and recovered state is
never discarded. Whether that state is sane to build on is a human's read. To look, ask without acting:

```bash
<skill>/scripts/afk.py recovery --issue <n> --repo <repo> --config '<config json>'
```

It returns the same `{tier, action, prompt, worktree, branch, reason}` the dispatch would act on —
deliberately a **separate call, not part of `rebuild`**: `rebuild` is the one machine-independent
observation the launcher's cycle gate shares (ADR-0008), while this asks *this machine* what it still
has. If what it shows plainly is not worth continuing (a wrecked tree, a branch carrying a wrong
approach), discard it and start over: `afk dispatch --issue <n> --start fresh` closes the fleet's PR
for the issue, deletes the fleet's own branches for it (and no other), removes the worktree, and
starts from base.

**A landing turn is continued the same way.** When the claim's PR holds the fleet's landing turn
(ADR-0027), the worker `afk dispatch` starts is put **on the turn**: in the worktree still here, else
one recreated at the PR's head — never from base — and briefed only to land the PR with `afk land`,
not with the task. The result says so (`prompt: landing`, `landing: <pr>`). `afk turn` itself takes
this path when the worker's terminal is already gone. A claim whose PR gave its turn up and is still
being fixed (`fixing`, ADR-0045) is continued onto that same brief: off the turn `afk land` gates
what would land and merges nothing.

**A merge batch is continued too** (ADR-0029). Its worker has no claim, so `afk dispatch` is not
its path: the pass asks after it with `afk no-pr --batch`, and when its terminal is gone
`afk turn --batch` starts a new batch worker — in the batch's worktree if it is on this machine (fix
commits and all), else in one cut from the batch's pushed branch, which holds the stack as of its
last `afk land --batch`; a batch that had stacked nothing yet starts again at the target's tip. The
result says which (`delivery: worktree | branch | fresh`, `again: true`). The command it is briefed
with rebuilds the stack from the target's tip every time and reads the batch's members from the
turn markers on its PRs, so a continued batch needs nothing remembered and nothing copied into its
new worktree. A batch recorded by a fleet that **died** is different: the fleet that takes its claims
abandons it (`afk turn --abandon`), and the PRs land on that fleet's single turns.

**This is not the retry path.** A red gate / adversarial refute / `giving-up` verdict goes through
`afk fail`, which discards the attempt and starts *fresh* with the failure reason (see
[Failure handling](../SKILL.md#failure-handling--bounded-retry--escalate-never-silently-drop)) — there the previous
attempt is precisely the thing that failed, so starting from base is deliberate.
