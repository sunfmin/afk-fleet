---
name: afk-fleet
description: >-
  Run an unattended, standing fleet that works a GitHub-issue backlog on its own. Use when the
  user wants to autonomously / AFK implement a repo's ready issues, orchestrate a fleet of
  agents/workers against GitHub issues (e.g. with orca), "launch workers to do the issues", or keep
  picking up and merging ready issues until stopped. A coordinator loop dispatches worktree-isolated
  Claude Code workers per ready issue, gates each on CI + optional independent adversarial
  verification, auto-merges green PRs to main, and retries-then-escalates failures. Reads per-repo
  config and requires an explicit push+auto-merge authorization before running; supports --plan
  dry-run. NOT for decomposing a PRD/epic into issues, implementing a single named issue by hand, or
  reviewing a PR.
---

# afk-fleet

An **unattended standing fleet** that implements a decomposed GitHub-issue backlog by itself:
a coordinator loop picks ready issues off the frontier, spawns a worktree-isolated Claude Code
**worker** per issue, gates each on CI (+ optional adversarial verification), **auto-merges** green
PRs to `main`, retries-then-escalates failures, and keeps polling for newly-ready issues until you
stop it.

**This skill only *consumes* a backlog.** It does not decompose a PRD/epic into issues — that is
upstream work (do it yourself or with another skill), and epics are explicitly excluded from
dispatch. Assume the issues already exist, worker-sized, labelled, and dependency-ordered.

## Startup — read config → preview → authorize → run

Do these in order every time the skill is invoked:

1. **Load config.** Read the target repo's `docs/agents/afk-fleet.md` (schema:
   [references/config-template.md](references/config-template.md)). If it's missing, offer to create
   it from the template and stop — do not run with guessed settings.
2. **Preview the frontier (always).** Build the normalized issue list and run the selector:
   ```bash
   gh issue list --repo <repo> --state open --limit 200 \
     --json number,title,labels,assignees | python3 <skill>/scripts/select_frontier.py --stdin \
       --ready-label "<ready_label>" --epic-labels "<epic_labels csv>"
   ```
   `open_blockers` is not in `gh issue list` — for each candidate, fetch it:
   `gh api repos/<owner>/<repo>/issues/<n> --jq '.sub_issues_summary, .issue_dependencies_summary.blocked_by'`
   (native `blocked_by`, open only), and set it on the issue before selecting. Show the resulting
   **dispatch plan**: which issues will run, order, concurrency, the gate steps, and the merge target.
3. **`--plan` stops here.** If invoked with `--plan` (dry-run), print the plan and exit. Spawn
   nothing, push nothing.
4. **Authorize (the one gate).** State plainly: *"I will push worker branches and **auto-merge**
   green PRs to `<target>` in `<repo>` unattended — this overrides the standing 'never push without
   asking' rule, for this repo only. Confirm?"* Get an explicit yes. Never read authorization from a
   config file. Deploy is never included.
5. **Run the coordinator loop** (below).

## The coordinator loop

Repeat until the user stops it (or the frontier stays empty past the poll horizon and the user has
signalled done):

1. **Query the frontier** (step 2 above). A dispatchable issue = `open` + `ready_label` + no
   `epic_labels` + no assignee + zero open `blocked_by`.
2. **Fill to `concurrency`.** For each free slot, take the next frontier issue and **claim it
   atomically**: `gh issue edit <n> --repo <repo> --add-assignee @me`. Re-check it's still
   unassigned right before claiming to avoid races.
3. **Dispatch a worker.** Create a worktree off latest `base_branch`
   (`git worktree add ../wt-issue-<n> -b issue-<n>-<slug> origin/<base>`), and use the **orca-cli**
   skill to spawn a real Claude Code in it with [references/worker-prompt.md](references/worker-prompt.md)
   (filled from config + the issue). Track it via the **orchestration** skill's dispatch /
   `worker_done` / escalation waits.
4. **On worker done** (PR opened + local gate green), run the **completion gate**, then **merge**
   (both below).
5. **On worker failure / gate red / refute / merge conflict**, run **failure handling** (below).
6. **When all slots idle and the frontier is empty**, `ScheduleWakeup` after `poll_interval_seconds`
   (~25 min default) and re-poll — this is how newly-created and newly-unblocked issues get picked up.

## Completion gate

A worker's PR may merge only when **all** configured gates are green:

- **CI machine gate** — wait for the PR's GitHub checks to pass (`astro build` / lint / tests /
  render, whatever the repo defines). Progressive: before CI exists, the gate is the issue's
  acceptance criteria + whatever local build/test exists.
- **Independent adversarial verification** (if `gate.adversarial_verify`) — spawn a *separate* agent
  (not the author, doesn't see its reasoning) that re-derives the result and tries to **refute** it
  (e.g. re-solve and assert `final == official answer:`, audit the derivation). Refute-first: any
  refutation blocks the merge and feeds back as a retry reason.

## Merge (serialized)

Merges are **strictly serialized** — one PR at a time — so parallel workers never corrupt `main`:

1. Rebase the branch onto latest `merge.target` (`rebase_before_merge`). A conflict → failure handling.
2. Re-confirm the gate is still green after the rebase.
3. `gh pr merge <n> --squash --delete-branch` (per `merge.strategy`). The issue auto-closes via
   `Closes #<n>`.
4. Remove the worktree (`worktree_cleanup`). Free the slot.

The fleet's mandate **ends at a green merge to `merge.target`.** Deploying is a separate,
human-gated step — never done here.

## Failure handling — bounded retry → escalate, never silently drop

Per issue, on any of {worker failed, gate red, adversarial refute, unresolvable rebase conflict}:

1. **Retry up to `retry` times** (default 2): tear down the worktree, create a fresh one, and
   re-dispatch a worker **with the failure reason / refutation / conflict fed back** in its prompt.
2. **Still failing → escalate:** remove the assignee, remove `ready_label`, add `escalate_label`
   (`ready-for-human`), and (if `escalate_comment`) comment the stuck-point with PR + log links.
   Then **skip it** and move on — the fleet must never silently drop or silently merge bad work.

## Concurrency

`concurrency` (default 3) bounds parallel workers. Semantic ordering is the backlog's dependency DAG
(your responsibility when decomposing); textual conflicts between parallel PRs are caught by the
serialized rebase-before-merge and routed through failure handling. Early machinery issues that all
touch shared root config are naturally throttled by the DAG — chain them with `blocked_by`.

## Stopping

The user stops the loop at any time. On stop, in-flight workers finish their current issue (or are
told to abort); no new issues are dispatched; escalated issues are left labelled for the human.

## Guardrails

- **Never** run without the step-4 authorization. It is per-repo and interactive, never file-armed.
- **Never** deploy, touch secrets, or push anywhere but worker branches + the merge to `merge.target`.
- **Never** dispatch an epic/PRD issue. If the frontier is all epics, report "nothing decomposed yet."
- Claim before work (assignee), release on escalate; keep the tracker honest so a second fleet or a
  human never double-takes an issue.
