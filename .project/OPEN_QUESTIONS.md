# Open questions

Updated: 2026-08-14

## GitHub publication target

The repository has no `origin` and no GitHub publication history. Before the first
push, confirm the exact authenticated `owner/repository`, private visibility,
`main` as the branch, and permission to upload the complete committed history.
Under the current memory-only policy that history contains only the scoped project
memory commits; all other local files remain untracked and local.

Why it matters: creating or selecting the wrong remote is an external change, and
the first push must not silently broaden the publication payload.

Known option: derive a private repository name from the project directory after
checking the authenticated GitHub account and exact-name availability.

## Dashboard visual gate

The information architecture and data requirements are specified, but the final
visual direction and interaction behavior require user review of a first mock.

Why it matters: M10 is not complete until the L0/L1/L2 dashboard is useful for
diagnosis, not merely capable of displaying stored data.

Known options: start with the ratified hierarchy—OEE and machine state, then a
35-valve diagnostic grid, then per-valve trends—and iterate after visual review.

## Post-M10 calibration scope

It is not yet decided which deferred M11 work should come first: broader alert
calibration across multi-fault/severity cases, Tail Time/Tail Pulse physical
calibration, or operational exposure of valve-controller groups.

Why it matters: these items improve different kinds of diagnostic confidence and
should not expand M10 before its visual acceptance gate closes.

Known option: close M10 first, then prioritize from observed dashboard limitations
and demo evidence.
