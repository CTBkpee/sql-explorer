# SQL Explorer

Paste one T-SQL script. Get colour-coded CTEs with every reference highlighted, a
layered dependency graph, dead-code markers, and live editing.

**Everything runs in your browser.** The SQL is parsed locally by
[sqlglot](https://github.com/tobymao/sqlglot) compiled to WebAssembly via Pyodide.
Nothing is uploaded, nothing is stored, and the page makes no network request after
it loads. You can verify that in the browser's network tab.

Status: **under construction** — scaffold only. See `sqlx-001` for the build plan.

## Licence

MIT — see `LICENSE`.
