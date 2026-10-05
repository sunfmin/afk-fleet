# afk-fleet

The repo for the `afk-fleet` Claude Code skill (lives in `skills/afk-fleet/`).

## Agent skills

### Issue tracker

Issues and PRDs live in this repo's **GitHub Issues**, via the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

Default five-role vocabulary — `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix` (each label string equals its role name). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context — `CONTEXT.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.

## After opening a PR

Every PR opened in this repo is landed and installed in the same breath — no waiting for me to ask (this overrides the global "open only, never merge" rule):

1. **Merge + delete the branch**: `gh pr merge <n> --squash --delete-branch`. Checks red or a merge conflict -> fix it first; never merge red.
2. **Sync local**: `git checkout master && git pull --ff-only`.
3. **Install the skill**: `npx skills update afk-fleet -g -y`, so `~/.agents/skills/afk-fleet/` runs what just merged. Confirm `updatedAt` moved in `~/.agents/.skill-lock.json`.
