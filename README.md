# SQL Explorer

Paste one T-SQL script. SQL Explorer colours its CTEs and references, draws the
dependency graph, flags safe-to-identify dead code, checks UNION shape, and updates
as you edit.

## Architecture and confidentiality

Everything runs in the browser. Pyodide runs Python in WebAssembly and sqlglot parses
the script locally. Both the runtime and parser are vendored with this site and served
from the same origin: there is no CDN, analytics, telemetry, persistence, or server
round trip. Your SQL never leaves the machine; the browser network tab makes that
directly inspectable.

This is deliberately a static, client-side design. It keeps pasted scripts private
while making the tool straightforward to host and audit.

## Scope

Version 1 analyses one pasted T-SQL script at a time. Version 2 is the line for
multi-object analysis, temp-table and table-variable support, schema-aware physical
table star expansion, and richer per-column workflows.

## Known limits

- `MERGE` is refused because sqlglot does not provide reliable lineage for it.
- Temp tables and table variables are not followed across statements in version 1.
- `SELECT *` from a physical table remains unresolved without a supplied schema.

When the parser cannot resolve something, the tool reports it as unknown rather than
guessing.

## Future AI seam

The analysis result is a JSON document, so an optional assistant could attach after
parsing: for example, it could explain the already-derived graph or propose an edit
plan. It is intentionally absent from version 1. Enabling it would require an explicit
user-controlled data-sharing choice, a clear provider and retention policy, and a
separate review of what script content may leave the browser.

## Python analysis contract

`analyzer.api.analyze(sql: str) -> dict` is the frozen P1 contract. It is JSON
serializable and always has exactly these top-level keys:

- `batches`: GO-split batch records (`index`, character `start`/`end`, `sql`, parse
  state, statement count, and local diagnostics). Offsets index the original script
  exactly: `script[start:end] == sql` for every batch. The `GO` separator line belongs to
  no batch.
- `ctes`: CTE records (`id`, batch, name, ordinal, source name offsets, projected
  `columns`, and `resolved`). `script[start:end]` is the CTE's own name as written.
- `edges`: CTE read edges (`source`, `target`, `kind`, `batch`); target is either a
  CTE id or a physical table name.
- `layers`: dependency-first CTE layer records (`batch`, `ctes`).
- `lineage`: final-select output lineage records (`batch`, `output`, flattened
  `nodes`, `edges`, and `resolved`; an error is included when lineage cannot run).
  Every lineage node has `id`, `name`, `expression`, and `resolved`.
- `dead`: `{ctes, columns}` marker lists. Each marker has `state` of `live`, `dead`,
  or `unknown`. `unknown` is mandatory when resolution is incomplete. References are
  collected over sqlglot's scope tree (`traverse_scope`), so a column read from a
  correlated subquery, an `APPLY`, a derived table, a set-operation branch or a later
  CTE counts wherever it appears, and T-SQL's case-insensitive names are matched
  casefolded. A CTE's markers become `unknown` — never `dead` — when: a lineage node is
  a placeholder; the CTE selects `*` from a physical table; the statement has a shape
  sqlglot does not scope (for example `UPDATE`/`DELETE` clauses); a qualifier resolves
  to no source; or an unqualified name could bind to more than one source, or to a
  source whose columns are not known (a physical table in a mixed `FROM`, or a
  correlated subquery over a physical table). `dead` is only emitted when every column
  node of the statement was placed.
- `unions`: UNION shape warnings (`column_count` or `alias`).
- `diagnostics`: parse errors and explicit refusals. MERGE produces a `refused`
  diagnostic and no lineage for that batch.

`SELECT *` from a CTE expands to that CTE's projected column list. A physical-table
star is unresolved; the analyzer never guesses a schema. The only supported dialect is
T-SQL, via sqlglot 30.17.0.

## Licence

MIT — see `LICENSE`.
