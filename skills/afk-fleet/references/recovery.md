# Recovery by continuation (a dead claim is continued, never restarted)

Disclosed reference for [`afk-fleet`](../SKILL.md), reached only on a **dead claim** — an orphaned
claim of mine, a stale claim reclaimed from a dead peer, or one inherited through a takeover — and
only when you doubt what would be continued. Recovery itself is not a procedure you run: `afk dispatch
--issue <n>` selects the tier and acts on it. This file is the tier table it follows, and the one
judgment it leaves to you.

A claim whose worker died is recovered *from its durable progress*, never re-dispatched from base
while progress exists. Workers push after every completed step (see
[worker-prompt](worker-prompt.md)), so that progress is real and reachable: the local
worktree if it is still on this machine, else the branch tip on GitHub ([ADR-0011](../../../docs/adr/0011-takeover-and-progress-preservation.md)).

`afk dispatch` asks `orca worktree list` whether a worktree for this issue is still here, recognises
the issue's branch on the remote from `branch_pattern` (the claim ref records the issue, not the
branch), compares it against the remote's `base_branch`, and then acts:

| tier | action | what `afk dispatch` does | prompt |
|---|---|---|---|
| **1** | `reuse_worktree` | The worktree is still on this machine: it is **kept**. The dead worker's terminal is closed and a new worker started *inside it*, on the same branch. Lossless: even uncommitted work survives. | continue |
| **2** | `recreate_at_tip` | No local worktree, but the branch is ahead of base: orca recreates one at the **pushed branch tip** and the worker continues there (on a new branch name — orca never reuses one — which the prompt carries). Loss is bounded to "since the last push". | continue |
| **3** | `dispatch_fresh` | Nothing survived: a new worktree at the remote base tip. | fresh |

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

**Judgment stays with the tick.** The tier selection is mechanics; whether the recovered state is sane
to build on is your read, exactly like orphan-vs-alive. To look before acting, ask without acting:

```bash
<skill>/scripts/afk.py recovery --issue <n> --repo <repo> --config '<config json>'
```

It returns the same `{tier, action, prompt, worktree, branch, reason}` the dispatch would act on —
deliberately a **separate call, not part of `rebuild`**: `rebuild` is the one machine-independent
observation the launcher's cycle gate shares (ADR-0008), while this asks *this machine* what it still
has. If what it shows plainly is not worth continuing (a wrecked tree, a branch carrying a wrong
approach), discard it and start over: `afk dispatch --issue <n> --start fresh` closes the fleet's PR
for the issue, deletes its work branches, removes the worktree, and starts from base.

**A hand-back is continued the same way.** When the claim's PR carries an open hand-back — a sync
conflict returned to its worker (ADR-0019) — the worker `afk dispatch` starts gets the continue-mode
prompt **with the hand-back instruction appended**: merge the target in, resolve, re-run the gate,
push to the same PR. The result says so (`handed_back: <pr>`). `afk hand-back` itself takes this path
when the worker's terminal is already gone.

**This is not the retry path.** A red gate / adversarial refute / `giving-up` verdict goes through
`afk fail`, which discards the attempt and starts *fresh* with the failure reason (see
[Failure handling](../SKILL.md#failure-handling--bounded-retry--escalate-never-silently-drop)) — there the previous
attempt is precisely the thing that failed, so starting from base is deliberate.
