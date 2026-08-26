# SQL Explorer

Paste one T-SQL script. Get colour-coded CTEs with every reference highlighted, a
layered dependency graph, dead-code markers, and live editing.

**Everything runs in your browser.** The SQL is parsed locally by
[sqlglot](https://github.com/tobymao/sqlglot) compiled to WebAssembly via Pyodide.
Nothing is uploaded, nothing is stored, and the page makes no network request after
it loads. You can verify that in the browser's network tab.

Status: **under construction** — scaffold only. See `sqlx-001` for the build plan.

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
