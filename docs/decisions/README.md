# Decision records

Architecture decision records (ADRs) for `fli`. Each record captures **one
decision**, the context that forced it, what else was considered, and what it
costs. A record is written when the decision is made and is not rewritten
afterwards, except to mark it superseded.

An ADR is not a design or a plan. If a document has no rejected alternatives and
no consequences, it belongs somewhere else.

## Records

| # | Title | Status | Date |
|---|---|---|---|
| [001](001-optional-browser-backed-transport.md) | Add an optional browser-backed transport for requests the HTTP transport cannot serve | Proposed | 2026-09-18 |

No gaps in the sequence.

## Conventions

- **Location and name:** `docs/decisions/NNN-slug.md`. Three digits,
  zero-padded. The slug names the decision, not the topic. No dates in
  filenames — the date lives in the record's header, where it can be read
  without renaming the file.
- **Numbering:** allocated monotonically from the highest number already on
  disk. Check the directory, not your memory. **Numbers are never reused**, not
  even after a record is retired, so that a stale citation fails to resolve
  rather than silently landing on an unrelated decision. A gap in the sequence is
  therefore meaningful, must not be filled, and is explained in the table above.
- **Identifier:** the bare number. `ADR` is a type word, not part of the ID — so
  the file is `001-optional-browser-backed-transport.md` and the heading is
  `# 001 — …`, with no prefix. In prose, write `ADR 001` with a space.
- **Shape:** Context / Decision / Rationale — alternatives rejected /
  Consequences / Trigger for revisiting, under a `**Status:**` and `**Date:**`
  header.
- **Status vocabulary:** `Proposed` (say what is blocking it), `Accepted`,
  `Superseded by NNN`, `Deferred`, `Rejected`. Status describes the decision,
  not the work — an accepted decision is `Accepted` whether or not the code
  exists yet. Implementation progress belongs in the issue tracker.
- **Citing a record:** prefer the path (`docs/decisions/001-optional-browser-backed-transport.md`),
  which is self-verifying. If you cite by number outside this repo, carry the
  repo, the number **and** the title — `fli` ADR 001 — Add an optional
  browser-backed transport. A bare number identifies nothing once it leaves this
  directory.

## Publishing

These records are contributor-facing and are deliberately kept out of the
published site: `mkdocs.yml` carries `exclude_docs: decisions/`, so `make docs`
builds exactly the pages it built before — quick starts, examples, API
reference, the MCP guide — and emits no "not included in the nav" notice. The
records are read here, in the repository, alongside the code they explain.

If they should be published later, delete the `exclude_docs` entry and add a
nav section; that is a one-line change in either direction.
