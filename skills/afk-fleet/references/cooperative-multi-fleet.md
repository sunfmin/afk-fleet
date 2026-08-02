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
- **Claim → `afk claim <n> --instance <id>`.** Internally it creates `afk-claim/<n>` pointing at a
  marker commit carrying `instance=<id> host=<host>`; the ref name is the issue number *only*. Creating
  a ref that already exists is **rejected by the server** — that rejection *is* the compare-and-swap.
  `{"won": true}` → proceed; `{"won": false}` (with the current `owner`) → a peer has it, skip. The
  claim ref is immutable after creation.
  ```bash
  sha=$(git commit-tree $(git hash-object -t tree /dev/null) -m "afk-claim instance=$ID host=$(hostname)")
  git push origin "$sha:refs/afk/claim/$n"    # nonzero exit ⇒ lost the race, back off
  ```
- **Owner check → rides in `afk rebuild`.** The ref scan reads every `afk-claim/*` marker, and the
  working set arrives already partitioned into `mine` / `peer_live` / `stale` (in-flight = `mine`).
  (`afk scan` / `afk classify-claims` remain as standalone debug surfaces over the same core.)
- **Heartbeat (the lease) → `afk heartbeat --instance <id> --ttl <s>`.** One ref `afk-heartbeat/<id>`
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
- **Release / cleanup → `afk release <n>`** (idempotent) on **merge**, **escalate**, and
  **orphan-release**. On **graceful stop**, the drain tick releases claims with **no PR yet** and
  **retains** those with an open PR (a peer inherits it once the lease expires — merging it if it is
  finished, **continuing** it if it is not).
  The **open-PR guard** — an issue with an open linked PR is never in the frontier — is what makes
  releasing safe: a still-finishing orphan's PR is never re-dispatched, and a human's PR is left alone.
  A skipped delete is a **phantom lock** that silently starves an issue — the canonical definition of
  that failure lives here.
- **Namespace fallback** — if bootstrap's probe shows an org ruleset forbids `refs/afk/*`, fall back to
  `refs/heads/afk-claim/*` + `refs/heads/afk-heartbeat/*` and warn that `on: push` CI fires on claim
  churn.
