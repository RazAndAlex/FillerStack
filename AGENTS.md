# AGENTS.md

## Agent skills

### Issue tracker

Issues live as markdown files under a local `.scratch/<feature-slug>/` directory. The tracker is working material: it is git-ignored and is not published with the repository. See `docs/agents/issue-tracker.md`.

### Triage labels

Five canonical roles mapped to the default label strings (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: `CONTEXT.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.

<!-- BEGIN PROJECT_MEMORY_V1 -->
## Project memory

Before substantial work, read `.project/OVERVIEW.md`, `.project/STATE.md`, relevant entries in `.project/DECISIONS.md`, and `.project/OPEN_QUESTIONS.md`. Inspect the implementation and Git state because memory is a navigational summary.

After substantial work, update material state, append a concise `.project/RECENT_WORK.md` entry, record durable decisions, and resolve or add open questions. Add task contracts, receipts, or review handoffs only when they materially help another agent continue.
<!-- END PROJECT_MEMORY_V1 -->
