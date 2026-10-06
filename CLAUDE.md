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

The config these steps read is the resolved one — `skills/afk-fleet/scripts/afk.py config --file docs/agents/afk-fleet.md` — since the file itself sets only what differs from the defaults.

1. **Gate**: run that config's `gate.local_command` on the head that will land. This repo has no GitHub checks (`gate.ci: local`), so this run is the only thing that can be red — the suite also holds the docs to the code. Red -> fix it first; never merge red.
2. **Merge**: `gh pr merge <n> --merge` — a merge commit, as the fleet itself lands every PR (ADR-0034) — with whether the branch is deleted taken from `merge.delete_branch`. A merge conflict -> fix it first, then gate again.
3. **Sync local**: check out `merge.target`, then `git pull --ff-only`.
4. **Install the skill**: `npx skills update afk-fleet -g -y`, so `~/.agents/skills/afk-fleet/` runs what just merged. Confirm `updatedAt` moved in `~/.agents/.skill-lock.json` — unless the PR left `skills/afk-fleet/` untouched, where "All global skills are up to date" is the expected answer.
