# ADR-0038 — A config key is one repos really differ in

**Status:** accepted — removes sixteen config keys and the `merge:` section. Amends
[ADR-0009](0009-one-home-for-config.md): the schema still has one home, but that home now holds nine
keys, and what was a key and is not is a constant beside it or simply what the code does.

## Context

The config had grown to 25 keys. Four repos run the fleet; across all four, the only keys ever set
to something other than the default were `concurrency`, `base_branch` / `merge.target`,
`gate.ci`, `gate.local_command`, the adversarial verify, and (once) `epic_labels`. Every other key
was copied from the template into each repo's file at whatever the default was that day, where it
stopped following the default and started failing to load when a key was renamed.

Most of the unused keys were not choices at all:

- **A value that breaks an invariant.** `merge.sync_before_merge: false` gated a head that had not
  merged the target and then landed a tree nobody had gated — what ADR-0012 and ADR-0030 exist to
  prevent. `claim_lease_ttl_seconds` and the two intervals had to stand in a fixed relation (a held
  claim must be re-beaten well inside its lease), which a file could break and a `min()` in `pace`
  existed only to patch.
- **Two keys for one fact.** `merge.target` and `base_branch` were always equal: a worker opens its
  PR against `base_branch`, and `gh pr merge` lands a PR on its own base and nowhere else.
  `gate.adversarial_verify: true` with an empty `gate.adversarial_verify_prompt` asked a verifier to
  check nothing in particular. `fingerprint_gate: false` was `force_tick_after_skips: 1`.
- **A switch nobody would want off.** An escalation without the comment saying where it got stuck;
  a claimed issue without its status board; a landed PR's branch and worktree kept around.
- **A fact the code finds by itself.** `claim_namespace`: `afk probe` tests the remote at every
  bootstrap and falls back by itself.
- **A hint the fleet depends on.** `branch_pattern` is how a dead worker's branch is found again;
  changing it mid-run orphaned every branch made under the old one.

## Decision

A key is in the schema only if repos really differ in it. Nine are:

    ready_label  epic_labels  base_branch  concurrency
    gate.ci  gate.local_command  gate.adversarial_verify_prompt
    retry  escalate_label

Everything else is decided once, in the code:

| was a key | now |
|---|---|
| `merge.target` | `base_branch` — the one name for the repo's trunk |
| `gate.adversarial_verify` | on exactly when `gate.adversarial_verify_prompt` is not empty (`afk_decide.verifies`) |
| `merge.sync_before_merge` | a landing always syncs before it gates |
| `merge.delete_branch` | a landed or superseded PR's branch is always deleted |
| `worktree_cleanup` | a landed or closed issue's worktree is always removed; an escalated one's is always kept |
| `escalate_comment`, `progress_comment` | always written |
| `fingerprint_gate` | always on |
| `branch_pattern`, `worker_idle_grace_seconds`, `busy_interval_seconds`, `idle_interval_seconds`, `idle_ticks_before_sleep`, `claim_lease_ttl_seconds`, `force_tick_after_skips` | constants in `afk_decide.py`, beside `CONFIG_DEFAULTS` |
| `claim_namespace` | settled by `afk probe`; still a field of the canonical config the launcher holds (`CONFIG_SETTLED`), which is how every later call learns it, but no file may set it |

A file or a `--set` that still carries a removed key is refused with a note naming this ADR, as
every removed key has been since ADR-0009 — except `--set claim_namespace=…`, which stays, for tests
and for forcing the fallback by hand.

## Consequences

- **A file written against the old template stops loading**, key by key, until the removed keys are
  deleted. That is the point of the notes: a key that is silently ignored is a config that lies to
  its author.
- **A repo whose remote refuses `refs/afk/*` is probed and warned at every bootstrap**; it can no
  longer record `refs/heads` to skip that.
- **`pace` no longer caps the sleep at half the lease.** While a claim is held the sleep is the busy
  interval, which is far inside it; a test holds that relation between the constants instead.
- **Tests that turned the fingerprint gate off to make every cycle tick pass `--wake`**, which is
  how a cycle is made to tick in a real run.
- A second repo that needs one of these to differ is the evidence that it is a key after all — it
  comes back then, with that repo as its reason.
