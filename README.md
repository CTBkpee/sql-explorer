# SQL Explorer

Paste one T-SQL script. SQL Explorer colours its CTEs and references, draws the
dependency graph, flags safe-to-identify dead code, checks UNION shape, and updates
as you edit.

Version 1.1 added per-column lineage across the source and graph, plus an add-a-column checklist for every downstream route.

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
- Multi-object analysis and following temp tables or table variables across statements
  remain version 2 work.
- `SELECT *` from a physical table remains unresolved without a supplied schema.
- The checklist cannot route a column that is not visible in the pasted script;
  adding one from a physical table requires a schema, planned for version 2.
- Already-projected detection matches output names case-insensitively; check that
  a matching name is the column you mean. It does not prove matching lineage.

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
serializable, never raises on SQL input, and always has exactly these top-level keys.
This guarantee applies to every `str` input for `analyze`, `column_lineage` and
`add_column_plan`. Parser failures, including `RecursionError`, become per-batch
`error` diagnostics; other batches continue normally. If any analysis stage raises
an Exception, that batch gets exactly one `refused` diagnostic, with reason
"this batch is nested too deeply to analyze" for `RecursionError`, or
"this batch could not be analyzed" otherwise. Analysis is all or nothing per batch:
its `ctes`, `edges`, `layers`, `lineage`, `unions`, `dead`, `columns`, `references`
and `terminal` contributions are published only after every stage succeeds.
A refused batch stays in `batches` and contributes nothing to those other keys;
its diagnostic also appears in its batch record. A projection whose source position is
unavailable becomes one unresolved record, preserving the other records. Empty,
whitespace-only and comment-only input returns empty collections (including the
two lists inside `dead`), with every key present:

- `batches`: GO-split batch records (`index`, character `start`/`end`, `sql`, parse
  state, statement count, and local diagnostics). Offsets index the original script
  exactly: `script[start:end] == sql` for every batch. The `GO` separator line belongs to
  no batch.
- `ctes`: CTE records (`id`, batch, name, ordinal, source name offsets, projected
  `columns`, and `resolved`). `script[start:end]` is the CTE's own name as written.
- `edges`: CTE and terminal read edges (`source`, `target`, `kind`, `batch`);
  target is either a CTE id or a physical table name, with kind `cte` or `table`.
- `layers`: dependency-first CTE layer records (`batch`, `ctes`).
- `lineage`: final-select output lineage records (`batch`, `output`, flattened
  `nodes`, `edges`, and `resolved`; an error is included when lineage cannot run).
  Records exist only for statements whose outermost query is a SELECT, after the
  documented query wrappers are unwrapped. UPDATE, DELETE, IF and CREATE PROCEDURE
  statements get no `lineage` records. Every lineage node has `id`, `name`,
  `expression`, and `resolved`.
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
- `diagnostics`: parse errors, warnings and explicit refusals. MERGE produces a
  `refused` diagnostic and no lineage for that batch. A CTE name defined in more
  than one statement of a batch, or more than once in a single WITH, adds exactly
  one `{batch, level: "warning", reason}` diagnostic per reused casefolded name;
  the reason names that CTE.
- `columns`: one record per projection of every CTE and terminal SELECT:
  `{id, batch, owner, name, ordinal, start, end, resolved}`. `owner` is a CTE id or
  terminal id; `id` is `{owner}/{ordinal}` with a zero-based ordinal. Offsets index
  the original script with an exclusive `end`: `script[start:end] == name`, including
  identifier quoting as written. A star projection has name `*`, its actual star
  offset, and `resolved: false`. An unaliased projection that is neither a bare
  column nor a star has no output name and is unresolved. Its span covers text
  inside its own projection, never a nested argument or type fragment. If no
  position is known, `name` is empty and `start == end ==` the owner's start.
  An aliased expression has its alias as output name and can be resolved.
  Column ids are unique within a document. Owners with a CTE name reused by
  multiple statements in one batch, or within one WITH, contribute no `columns`
  records; references that would bind to their columns have `binds_to: null`.
- `references`: every parser column occurrence:
  `{batch, start, end, name, qualifier, owner, binds_to}`. Offsets use the same exact
  name slices as `columns`; `qualifier` is the table alias or name, or an empty string.
  `owner` is the enclosing CTE or terminal id. `binds_to` is a `columns` id when the
  existing scope binder places it, otherwise `null`.
- `terminal`: one record per statement outside CTE bodies:
  `{id, batch, statement_index, reads, columns, start, end}`. The id is
  `t{batch}:{statement_index}`, where the statement index is zero-based within the
  batch, distinct from CTE ids `b{batch}:{name}`. The terminal query is unwrapped
  from CREATE VIEW, CREATE TABLE AS, INSERT SELECT or SELECT INTO; its output
  SELECT is the leftmost leaf of that query's set operators. `reads` contains CTE ids or
  physical table names, just like `edges.target`. `columns` lists its output names.
  Reads and terminal-sourced edges exclude write targets (CREATE, INSERT INTO,
  SELECT INTO, UPDATE and DELETE), unless the same relation also occurs as a read
  source in FROM, JOIN or a subquery. In `DELETE alias FROM <sources>` and
  `UPDATE alias SET ... FROM <sources>`, every FROM and JOIN relation is a read,
  including the target's own table; the write alias itself is excluded. Simple
  `DELETE FROM x` and `UPDATE x SET ...` exclude `x`; subquery sources remain reads
  in every form. Reads keep first-appearance order with no duplicates.
  `TRUNCATE TABLE`, `DROP TABLE` and `DROP VIEW` name only a target and read nothing.
  CTE names are visible only in their defining statement. Elsewhere the same name
  is a physical table. Read edges, routes and
  lineage stay in the owner's statement; casefolded name matching retains the
  declared CTE spelling in its frozen id.
  The span is best effort: minimum start and maximum exclusive end of parser-known
  identifier offsets in the statement, or the batch start for both when none exist.
  It is not an exact statement slice.

`analyzer.columns.column_lineage(sql, owner_id, column) -> {nodes, edges, resolved}`
returns the same flattened graph shape as a `lineage` record for a column owned by
a CTE or terminal. Any failure returns `{nodes: [], edges: [], resolved: false, error}`
without raising. It uses a probe SELECT with the statement's WITH clause for a CTE,
and keeps a single in-memory parse memo keyed by the exact SQL text. Node ids are
local to one result. A reused CTE owner id returns the failure shape above;
a unique terminal id still resolves through the CTE of its own statement.
`analyze()` stays cheap: it makes no additional lineage calls for these keys.
Building `columns`, `references` and `terminal` is linear in the number of records.

The browser uses hybrid eager/on-click column lineage. After rendering and first
paint, it starts an eager pass only when both `eligible <= EAGER_COLUMN_LIMIT`
and `eligible * sql.length <= EAGER_WORK_LIMIT`. Here `eligible` counts all resolved
`columns` records in the document, including terminal outputs. The shipped values
in `web/columns.js` are `EAGER_COLUMN_LIMIT = 24`, `EAGER_WORK_LIMIT = 30000` and
`EAGER_TIME_BUDGET_MS = 750`.
The pass yields between calls and checks elapsed wall-clock time before each call;
at the budget it stops starting calls, leaving unreached columns to compute on click.
An in-progress call finishes normally, so 750 ms is not a hard limit on that call.
Completing every eligible column eagerly is not guaranteed. Above either admission
limit, columns compute on click from the start. Both triggers share one cache per
analysis: clicking a cached column makes no Python call. Editing cancels the old
pass and discards its cache. Parse-error documents do not run a pass.

`analyzer.checklist.add_column_plan(sql, owner_id, column) ->
{visible, reason, paths, path_count, truncated}` computes a read-only checklist
without calling lineage. A target must be a projected `columns` record of its
owner or a qualified `references` record occurring there. Otherwise it returns
`visible: false`, empty paths, count zero, no truncation, and reason exactly
"this column is not visible in the pasted script; adding it from a physical table needs a schema — v2".

A reused CTE owner id instead returns `visible: false`, empty paths, count zero,
no truncation, and reason exactly
"this CTE name is defined by more than one statement in the batch; rename one or separate the statements with GO".
The same refusal applies to a name defined twice in one WITH. The reason above
remains unchanged for compatibility. The frozen CTE id remains `b{batch}:{name}`;
reuse across GO batches is unambiguous.

When parsing or checklist analysis cannot proceed, `add_column_plan` returns
`{visible: false, reason: "this script could not be analyzed", paths: [],
path_count: 0, truncated: false}`. `column_lineage` retains its documented failure
shape, including `error`. These entry points never raise for a `str` script.

UNION, EXCEPT and INTERSECT take their output names from the first (leftmost)
branch. This is shared by `ctes[].columns`, `columns`, terminal output names, and
owner-column validation in both entry points.

Each path is `{label, terminal, hops}`, ordered from the producing node toward a
reachable terminal; every route is counted by DAG dynamic programming. At most
`MAX_PATHS = 256` routes are enumerated; a larger exact `path_count` always sets
`truncated: true`. A projected target starts its hops at the next node downstream;
a qualified reference absent from the owner's outputs starts at the owner.
Each hop is `{owner, kind, anchors, needs_edit, caveats}`, where kind is `cte` or
`terminal`. An anchor is `{line, start}` at the first character of the last projection
in a branch's SELECT list, found from the source text, with a whole-script offset
and 1-based whole-script line; preceding whitespace and comments are excluded.
An anchor is emitted only when parser-supplied offsets match the SELECT list to its statement; otherwise the hop is `unresolved` with no anchors.
Set-operator hops include one anchor per leaf branch needing an edit, flattened
left to right through wrappers and parentheses. An unresolved CTE has
caveat `unresolved` and no anchors; star pass-through has `needs_edit: false` and
no anchors. A bare `*` covers every arriving source; `x.*` covers only the
source named by `x` (alias or relation name, casefolded). For a different source,
normal edit and already-projected rules apply. An unknown star qualifier adds
`unresolved` with no anchors. These rules take precedence over a case-insensitive output name match:
all branches already projecting the name gives `needs_edit: false`, no anchors,
and `already_projected`; only some gives `needs_edit: true`, anchors for missing
branches, and `already_projected_partial`. This matches names, not lineage.
An aggregate body also carries caveat `group_by`.

`SELECT *` from a CTE expands to that CTE's projected column list. A physical-table
star is unresolved; the analyzer never guesses a schema. The only supported dialect is
T-SQL, via sqlglot 30.17.0.

## Licence

MIT — see `LICENSE`.
