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
  marker commit carrying `instance=<id> host=<host>`; the ref name is the issue number *only*. Creating
  a ref that already exists is **rejected by the server** — that rejection *is* the compare-and-swap.
  Won → the dispatch goes on to start the worker; lost (the result names the current `owner`) → a peer
  has it, and nothing is started. A claim that is already **mine** (reclaimed, taken over, orphaned)
  is `held`, and the dispatch continues it. A
  push that failed with **no** claim on the remote is not a lost race: the tool exits 3 with
  `{"error": …}` instead, so an auth or network failure can never pass for bad luck. The claim ref is
  immutable after creation.
  ```bash
  sha=$(git commit-tree $(git hash-object -t tree /dev/null) -m "afk-claim instance=$ID host=$(hostname)")
  git push origin "$sha:refs/afk/claim/$n"    # rejected because the ref exists ⇒ lost the race
  ```
- **Owner check → rides in `afk rebuild`.** The ref scan reads every `afk-claim/*` marker, and the
  working set arrives already partitioned into `mine` / `peer_live` / `stale` (in-flight = `mine`).
  (`afk scan` / `afk classify-claims` remain as standalone debug surfaces over the same core.)
- **Heartbeat (the lease) → `afk heartbeat --instance <id>`** in a tick, and inside `afk cycle` on a
  cycle that spawns none. One ref `afk-heartbeat/<id>`
  carries a timestamp; the tool refreshes it **only if due** (`now - ts > ttl/3`) by force-pushing a
  new marker (it reads the old ts itself, so this stays stateless). **Per instance, not per claim**
  (claim refs never churn); a fleet holding no claims never beats.
- **Reclaim a stale peer claim → `afk reclaim <n> --instance <id> --expect-sha <sha>`.** Only the
  `stale` list is reclaimable. The takeover is atomic (two reclaimers can't both win):
  ```bash
  git push origin --force-with-lease="refs/afk/claim/$n:$sha_i_read" "$my_sha:refs/afk/claim/$n"
  ```
  A peer with a *fresh* heartbeat is left strictly alone — it reconciles its own dead workers locally.
  A reclaimed claim is then recovered by **continuation**, not restarted (ADR-0011). The
  lease-skipping, human-authorized sibling is [`--takeover`](../SKILL.md#takeover-mode---takeover).
- **Release / cleanup** — the last step of every transition that ends a claim: `afk merge` (after the
  PR landed), `afk escalate` (after the relabel — released first, a PR-less issue still carrying
  `ready_label` would be back on the frontier for a peer to dispatch), `afk park` (after the
  `blocked_by` edge is recorded — for the same reason), `afk close`. `afk release <n>`
  (idempotent: a claim already gone counts as released) is the same step on its own, for an
  **orphan-release**, a `closed` row, and the drain. A delete that fails with the claim still on the
  remote exits 3 — `released` is never reported for a claim that is still there. On **graceful stop**, the drain tick releases claims with **no PR yet** and
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
