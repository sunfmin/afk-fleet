# afk-fleet config (per-repo)

Copy this into the **target repo** at `docs/agents/afk-fleet.md`. The fleet reads it at bootstrap
through `afk config --file …`, which validates every key against the schema (an unknown key or
wrong shape is an **error**, caught before anything runs) and emits the canonical JSON every tick
and tool consumes (ADR-0009). Everything is repo-specific here; the skill core is repo-agnostic.
Anything omitted uses the default shown. One thing is intentionally NOT a config key, and the
validator refuses it by construction (there is no `authorize` key either — invoking the skill is the
authorization, ADR-0023):

- **the worker launch command** — the string workers are started with (`ckimi`, `direnv exec . claude`,
  …) is *machine-local*, while this file is checked into the target repo and shared with the team: on a
  teammate's machine it is `command not found`, or worse, a same-named alias pointing at a different
  provider. It is settled at bootstrap by `afk worker-command` — passed with the invocation or asked for — and
  held only by the launcher (ADR-0010).

A key is here because repos really differ in it. What every fleet does alike is **not** a key, and a
file that still sets one is refused with a note saying so (ADR-0038): the pacing and the claim lease,
the worktree name, that a landing syncs with `base_branch` before it gates, that a landed branch and
worktree are removed, that every claimed issue carries a status board and every escalation a comment,
and where claim refs live (`afk probe` settles that at every bootstrap).

Nor is the **base branch** — the branch workers cut from, open their PR against and land on. It has
no default and is in no file: the human confirms it at every launch (asked, or passed as
`/afk-fleet --base-branch <name>`), and it is kept on the remote, where every launcher on the repo
reads the same one (ADR-0042). `base_branch` below means that branch.

```yaml
# --- dispatch contract ---
ready_label: ready-for-agent          # a child issue is dispatchable when it carries this
                                      #   (a human reserves an issue by REMOVING this label)
epic_labels: [epic, prd, wayfinder:map]   # never dispatched (a PRD is not a worker task), and an
                                      #   issue blocked by one is never waited on

# --- workers ---
concurrency: 3                        # max workers running at once

# --- completion gate ---
gate:
  ci: required                         # required | local  (ADR-0012)
                                      #   required — wait for the PR's GitHub checks to go green
                                      #   local    — never read GitHub checks: local_command IS the gate,
                                      #              run by the worker pre-PR and RE-RUN by its landing
                                      #              (`afk land`) against the exact tree that lands.
                                      #              Requires a non-empty local_command (load-time error).
  local_command: ""                    # the repo's build/test command (e.g. "pnpm build && pnpm test").
                                      #   In `required` mode: the worker's pre-PR filter. In `local` mode:
                                      #   the completion gate itself, at both ends.
  adversarial_verify_prompt: ""        # for content repos: what an independent agent checks before a PR
                                      #   lands (e.g. "re-solve; assert final == official answer"). Any
                                      #   text turns the verify on — it re-derives the result and refutes
                                      #   wrong output (refute-first); empty, there is none

# --- failure handling ---
retry: 2                               # per-issue retries; count tracked via an afk-attempt/<n> label on the issue
escalate_label: ready-for-human        # applied (with ready_label removed, claim ref deleted) on give-up,
                                      #   with a comment naming the stuck-point + PR/log links. Never
                                      #   ready_label itself, nor a label under afk-attempt/ (load-time error)
```

## Notes

- **A value is read as written.** Quotes delimit a value only when they wrap the whole of it —
  `"pnpm build && pnpm test"` is the command between them, and `pytest -k 'a or b'` keeps both of
  its quotes. A comment starts at a `#` that follows whitespace and is outside quotes, so
  `curl http://h/#frag` keeps its `#`. A list is `[a, b]` on one line, and a quoted item keeps its
  comma: `[a, "b,c"]` is two items. There is no escape: a wrapped value holds anything but its own
  quote character. What that cannot hold — `"$PY" -m pytest`, whose opening quote closes before the
  end — is an **error** naming the key, never a value quietly read as something else.
- **This template is pinned to the code.** The defaults table in `afk_decide.py`
  (`CONFIG_DEFAULTS`) is the single source of truth; a fixture test parses this file's yaml block
  and fails if any value here drifts from that table (ADR-0009). Edit defaults there, then mirror
  them here.
- **Progressive gate.** Before the repo has a build/test/CI, the gate degrades to "the issue's own
  acceptance criteria + whatever build/test exists." `gate.ci: required` starts enforcing once the
  earliest issues have stood up CI.
- **Adversarial verify** is the pluggable, domain-specific half. Leave `gate.adversarial_verify_prompt`
  empty for plain software repos; write one for content/correctness repos where a machine gate can't
  catch a wrong answer.
- **Merge batches need no key.** With `gate.ci: local` and no adversarial verify, two or more finished
  PRs that wait for the landing turn together always land as one batch (ADR-0029, ADR-0034) — stacked
  on `base_branch` with one merge commit per PR, gated ONCE, pushed as a fast-forward. That needs a
  `base_branch` that accepts a direct push (checked at bootstrap). A PR always lands as a MERGE COMMIT:
  no squash, no rebase.
- **Set `gate.local_command` as soon as the repo can build.** The fleet's most expensive failure is
  a retry: a red CI gate tears the worker down and a *fresh* worker re-reads the issue, the docs,
  and the failure from scratch. A local `build && test` gate catches most failures inside the same
  worker session — a few fix-up edits instead of a full re-spawn plus a CI round-trip.
- **`gate.ci: local` — when to switch, and what the repo owes it (ADR-0012).** Workers push after
  every completed step (progress preservation), so on `required` every one of those pushes fires the
  repo's `on: push` / `on: pull_request` workflows while the fleet reads only the last run — and then
  the landing waits for yet another full run. In `local` mode the local command is the
  whole gate, run twice: by the worker after its pre-PR **sync**, and by its landing after
  the landing's sync — or once, when nothing moved in between (next note). Three obligations come with it:
  - **Scope remote CI away from worker branches** (e.g. trigger `on: push` for the target branch only,
    and drop `on: pull_request`). The fleet cannot edit your workflows — if you leave them broad you
    keep paying the congestion, you just stop reading it.
  - **Do not require status checks on `base_branch`.** `gh pr merge` would be rejected however green
    the local gate is, and the only bypass (`--admin`) also overrides human review, so the fleet
    refuses to use it. Bootstrap probes the protection and **hard-errors** on this combination.
  - **Own the environment parity.** `ci: local` is a claim that `local_command` is CI-equivalent. If
    your CI needs a Linux-only toolchain, service containers, or secrets, stay on `required`.
- **A tree is gated once (ADR-0030).** PRs land one at a time, so the gate's run time is the
  fleet's throughput: a 6-minute gate lands about ten PRs an hour. A green run made through
  `afk gate` or `afk land` on a committed tree is put on record on the remote
  (`refs/afk/gate/<tree>-<hash of the command>`), and a landing skips its own run whenever a record of the
  command configured now stands for the tree that would land — in this worktree, a recreated one,
  or on another machine. A sync that moved the head, a later commit, a changed command, a red run
  of the same tree since, or a record more than a day old, and the landing runs the gate itself.
  There is no switch: what this gives up is a second, independent sample of a flaky or
  machine-dependent gate on unchanged content. A gate that leaves untracked, un-ignored files
  behind — or rewrites a tracked one — makes every run "not on a committed tree": never recorded,
  and refused by the landing. Ignore its artifacts, and keep fixers out of the gate. A remote that refuses
  `refs/afk/gate/*` is a bootstrap warning: nothing is recorded and every landing gates.
- **Fingerprint gate.** Each cycle, `afk cycle` (code, zero LLM tokens) runs its reconciliation pass
  only when the digest of observable state moved. On a skipped cycle no tick runs — its only cost is
  the one `afk cycle` call — which, while the fleet holds claims, refreshes the lease itself, so
  skipping never lapses a lease. Correctness never depends on the gate: a full tick runs at least
  every sixth cycle, so a missed change waits at most that long (ADR-0007).
- **Deploy is out of scope.** The fleet's mandate ends at a green merge to `base_branch`. Deploying
  (secrets, live infra) is never done by the fleet.
- **Reserved labels, refs & the status comment.** The fleet manages, durably in GitHub, the
  `afk-attempt/<n>` labels (retry count) and `afk-attempt/starting` (a retry under way), the hidden `refs/afk/*` ref namespace — `refs/afk/claim/<n>` (the
  claim, one per owned issue), `refs/afk/heartbeat/<id>` (per-instance liveness) and `refs/afk/base` (the base branch, as last confirmed) — and the single status-board comment tagged `<!--afk:status-->` (found and
  overwritten by that marker each tick), beside the `<!--afk:escalation …-->` marker that leads an escalation's comment. This is what keeps ticks stateless and lets fleets cooperate
  (see the skill's "Why it runs forever" and ADR-0003). Don't hand-edit them or reuse the
  `afk-attempt/*` / `refs/afk/*` prefixes or the `<!--afk:status-->` marker. If an org ruleset forbids
  non-branch refs, bootstrap's `afk probe` falls back to `refs/heads/afk-claim/*` for the run (the
  config it returns carries that as `claim_namespace`, a field no file sets) and warns that
  `on: push` CI will then fire.
