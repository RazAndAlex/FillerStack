# AGENTS.md

## Agent skills

### Issue tracker

Issues live as markdown files under `.scratch/<feature-slug>/` in this repo. See `docs/agents/issue-tracker.md`.

### Triage labels

Five canonical roles mapped to the default label strings (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: `CONTEXT.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.

## Project memory

Before substantial work, read `.project/STATE.md`, `.project/DECISIONS.md`,
`.project/OPEN_QUESTIONS.md`, and `.project/RECENT_WORK.md`. Treat them as a
handoff, then verify the relevant claims against the actual code, issue files,
ADRs, and Git state. After meaningful work, update the memory files affected by
the change; do not record secrets, private raw evidence, generated data, or
unsupported assumptions.
