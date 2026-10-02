# Cooperative multi-fleet

Disclosed reference for [`afk-fleet`](../SKILL.md): how several fleet instances cooperate on one repo.
The operative rules (claim before work, release on every terminal transition, reclaim only stale) live
in the skill's Guardrails; this file carries the mechanism and the raw git each subcommand runs.

Several fleet instances — on several machines, even under one shared GitHub account — may work the
same repo at once. The assignee can't arbitrate them (under a shared account it can't say *who* owns
an issue), so ownership lives in atomic **git refs** under the hidden `refs/afk/*` namespace and
liveness in a **per-instance lease**. See
[ADR-0003](../../../docs/adr/0003-cooperative-multi-fleet-claims.md).

All of the mechanics below are `afk.py` subcommands (see the [Tools table](tools.md)); the raw git
each one runs is shown so the mechanism is legible, but the tick calls the tool.

- **Instance id** — minted once per launcher run at bootstrap, injected into every tick. It stamps
  every claim this fleet makes (`--instance <id>`) and names this fleet's heartbeat.
- **Claim → the first step of `afk dispatch`** (`afk claim <n> --instance <id>` is the same step on
  its own). Internally it creates `afk-claim/<n>` pointing at a
  marker commit carrying `instance=<id> host=<host> ts=<epoch>` and a unique generation;
  the ref name is the issue number *only*. Creating
  a ref that already exists is **rejected by the server** — that rejection *is* the compare-and-swap.
  Won → the dispatch goes on to start the worker; lost (the result names the current `owner`) → a peer
  has it, and nothing is started. A claim that is already **mine** (reclaimed, taken over, orphaned)
  is `held`, and the dispatch continues it. A
  push that failed with **no** claim on the remote is not a lost race: the tool exits 3 with
  `{"error": …}` instead, so an auth or network failure can never pass for bad luck. The claim ref is
  immutable for that ownership generation. Its timestamp grants a bounded initial lease of one
  TTL even before the first heartbeat; a missing heartbeat becomes stale only after that grace.
  ```bash
  sha=$(git commit-tree $(git hash-object -t tree /dev/null) -m "afk-claim instance=$ID host=$(hostname) ts=$(date +%s) generation=$(uuidgen)")
  git push origin --force-with-lease="refs/afk/claim/$n:" "$sha:refs/afk/claim/$n"
  ```
- **Owner check → rides in `afk rebuild`.** The ref scan reads every `afk-claim/*` marker, and the
  working set arrives already partitioned into `mine` / `peer_live` / `stale` (in-flight = `mine`).
  (`afk scan` / `afk classify-claims` remain as standalone debug surfaces over the same core.)
- **Heartbeat (the lease) → `afk heartbeat --instance <id>`** in a tick, and inside `afk cycle` on a
  cycle that spawns none. One ref `afk-heartbeat/<id>`
  carries a timestamp; the tool refreshes it **only if due** (`now - ts > ttl/3`) by force-pushing a
  new marker (it reads the old ts itself, so this stays stateless). **Per instance, not per claim**
  (claim refs never churn); a fleet holding no claims never beats. Dispatch and merge also refresh
  the heartbeat before potentially long work. Initial claim grace does not replace later heartbeats.
- **Reclaim a stale peer claim → `afk reclaim <n> --instance <id> --expect-sha <sha>`.** Only the
  `stale` list is reclaimable. The command re-reads the exact claim and its owner's heartbeat,
  refusing a changed generation or a renewed/fresh owner. The claim update is atomic (two
  reclaimers using the same observed SHA can't both win):
  ```bash
  git push origin --force-with-lease="refs/afk/claim/$n:$sha_i_read" "$my_sha:refs/afk/claim/$n"
  ```
  A peer with a fresh heartbeat or initial claim grace is left alone — it reconciles its own dead
  workers locally. An unreadable heartbeat is an error, never evidence that its owner is dead.
  A reclaimed claim is then recovered by **continuation**, not restarted (ADR-0011). The
  lease-skipping, human-authorized sibling is [`--takeover`](../SKILL.md#takeover-mode---takeover).
- **Release / cleanup** — the last step of every transition that ends a claim: `afk merge` (after the
  PR landed), `afk escalate` (after the relabel — released first, a PR-less issue still carrying
  `ready_label` would be back on the frontier for a peer to dispatch), `afk close`.
  `afk release <n> --expect-sha <sha>`
  (idempotent: a claim already gone counts as released) is the same step on its own, for an
  **orphan-release**, a `closed` row, and the drain. Pass the SHA from that `mine` row in `rebuild`
  (or the observed `scan` row); unguarded release is refused. Deletion uses
  `git push origin --force-with-lease="refs/afk/claim/$n:$sha" ":refs/afk/claim/$n"`.
  A successor's different SHA returns `released: false, reason: "claim changed"`; do not retry
  using the successor's SHA or remove its worktree. A delete refused while the expected claim is
  still on the remote exits 3. On **graceful stop**, the drain tick releases claims with **no PR yet** and
  **retains** those with an open PR (a peer inherits it once the lease expires — merging it if it is
  finished, **continuing** it if it is not).
  The **open-PR guard** — an issue with an open linked PR is never in the frontier — is what makes
  releasing safe: a still-finishing orphan's PR is never re-dispatched, and a human's PR is left alone.
  A skipped delete is a **phantom lock** that silently starves an issue — the canonical definition of
  that failure lives here.
- **Namespace fallback** — where the refs live is the config key `claim_namespace`: `refs/afk`
  (default) or `refs/heads`, and nothing else. If bootstrap's `afk probe` finds an org ruleset
  rejecting `refs/afk/*`, it reports `blocked` and returns the config with `claim_namespace: refs/heads` — claims become `refs/heads/afk-claim/*`, heartbeats
  `refs/heads/afk-heartbeat/*` — and the launcher warns that `on: push` CI fires on claim churn. Every
  later call inherits the namespace through `--config`; there is no separate flag to carry.

## Ownership fences and limits

The settling transitions (`merge`, `close`, `escalate`, `fail`) and dispatch capture the claim SHA.
Merge rechecks that generation after the gate and immediately before the merge request; the other
settling/startup paths recheck before later consequential steps, including prompt submission after
terminal readiness. This does not cover every mutation: `hand-back` and `nudge` retain their entry
owner check, and standalone `status` does not authenticate ownership. A changed owner or a
release/reacquire by the same instance is a different generation. `merge` returns `claim_lost`
with `merged` and `released` facts; leave
the successor alone, preserve its worktree, and rebuild rather than retrying or declaring failure
on its behalf. `merged: true` means `gh pr merge` already returned success; it does not claim
ownership was retained or add merge-queue completion verification. Compare-and-delete still prevents
the old owner erasing a successor.

These are conservative fences, not a linearizable distributed lease. Heartbeat and claim are
separate refs: renewal between the final liveness read and the reclaim push can still race.
Likewise GitHub mutation and git ownership checks are separate transactions, so a takeover in the
last check-to-action window cannot cancel an already-started merge, label edit, issue close, or
worker command. There is no independent background heartbeat during a blocked tool or gate; an
operation longer than the TTL can lose its claim and must stop at the next fence. A human override
can always transfer a fresh claim. Strict cross-service exclusion needs a separate server-enforced
coordination protocol; the claim CAS alone does not provide it.

## CLI compatibility

Standalone release now requires `--expect-sha` from `rebuild.mine[].sha` or the observed scan row.
Update the skill and restart existing launchers together; an old caller that omits the generation
is refused rather than deleting an unobserved claim. A `released: false` result is ownership loss,
not an operational failure to retry against the new owner.
