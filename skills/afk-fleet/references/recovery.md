# Recovery by continuation (a dead claim is continued, never restarted)

Disclosed reference for [`afk-fleet`](../SKILL.md), reached only on a **dead claim** — an orphaned
claim of mine, a stale claim reclaimed from a dead peer, or one inherited through a takeover. Read it
before acting on any of those: it carries the per-tier action table and the continue/fresh prompt
choice the tick must make.

A claim whose worker died is recovered *from its durable progress*, never re-dispatched from base
while progress exists. Workers push after every completed step (see
[worker-prompt](worker-prompt.md)), so that progress is real and reachable: the local
worktree if it is still on this machine, else the branch tip on GitHub ([ADR-0011](../../../docs/adr/0011-takeover-and-progress-preservation.md)).

One call decides which, per dead claim:

```bash
python3 <skill>/scripts/afk.py recovery --issue <n> --repo <repo> --config '<config json>'
```

It asks `orca worktree list` whether a worktree for this issue is still here, recognises the issue's
branch on the remote from `branch_pattern` (the claim ref records the issue, not the branch), compares
it against `base_branch`, and returns `{tier, action, prompt, worktree, branch, reason}`. It is
deliberately a **separate call, not part of `rebuild`**: `rebuild` is the one machine-independent
observation the launcher's fingerprint gate shares (ADR-0008), while this asks *this machine* what it
still has — and only a dead claim ever needs asking.

| tier | action | what the tick does | prompt |
|---|---|---|---|
| **1** | `reuse_worktree` | The worktree is still on this machine: **do not `orca worktree rm` it.** Start a new worker *inside it*, on the same branch — `orca terminal create --worktree issue:<n> --command "<worker_command>"`. Lossless: even uncommitted work survives. | continue |
| **2** | `recreate_at_tip` | No local worktree, but the branch is ahead of base: recreate one at the **branch tip** (`orca worktree create … --base-branch <that branch>`) and continue there. Loss is bounded to "since the last push". | continue |
| **3** | `dispatch_fresh` | Nothing survived: today's behaviour — tear down any leftover (`orca worktree rm --worktree issue:<n> --force`) and dispatch from base. **The only tier that tears anything down.** | fresh |

`prompt` names the [worker-prompt](worker-prompt.md) variant to deliver: its
**continue-mode variant** (inspect the existing progress first, treat it as partial work toward the
*same* acceptance criteria) or the fresh one. A surviving worktree with provably nothing in it gets the
fresh prompt — tier 1 is about never destroying a worktree, not about pretending there is progress.

**Keep the claim** throughout (you already own the issue), and note that the `afk-attempt/<n>` counter
is neither read nor incremented: continuation answers *"did the **worker** die?"*, the retry ladder
answers *"is this **work** failing?"* — different axes (ADR-0011). Because progress accumulates across
continuations, a claim recovered repeatedly *converges* instead of looping.

**Judgment stays with the tick.** The tier selection is mechanics; whether the recovered state is sane
to build on is your read, exactly like orphan-vs-alive. If it plainly is not (a wrecked tree, a branch
carrying a wrong approach), fall back to tier 3 by hand.

**This is not the retry path.** A red gate / adversarial refute / `giving-up` verdict still tears the
worktree down and re-dispatches *fresh* with the failure reason (see
[Failure handling](../SKILL.md#failure-handling--bounded-retry--escalate-never-silently-drop)) — there the previous
attempt is precisely the thing that failed, so starting from base is deliberate.
