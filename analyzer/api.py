"""Public frozen JSON contract for the T-SQL explorer."""

from sqlglot import exp

from .ctes import REUSED_REASON, discover, layers, reused_names
from .columns import collect_records, column_lineage
from .deadcode import analyze_dead
from .lineage import for_expression
from .split import parse_batches
from .unions import check

MERGE_REASON = "MERGE lineage is unreliable in sqlglot; not rendered"


def analyze(sql: str) -> dict:
    """Analyze T-SQL; invalid input and unavailable positions degrade to records."""
    raw_batches = parse_batches(sql)
    document = {"batches": [], "ctes": [], "edges": [], "layers": [], "lineage": [],
                "dead": {"ctes": [], "columns": []}, "unions": [], "diagnostics": [],
                "columns": [], "references": [], "terminal": []}
    for batch in raw_batches:
        expressions = [expression for expression in batch['expressions'] if expression is not None]
        if batch['parsed'] and not expressions:
            continue
        public = {key: batch[key] for key in ("index", "start", "end", "sql", "parsed", "statement_count", "diagnostics")}
        document["batches"].append(public)
        document["diagnostics"].extend({"batch": batch["index"], **diag} for diag in batch["diagnostics"])
        if not batch["parsed"]:
            continue
        try:
            if any(expression.find(exp.Merge) for expression in expressions):
                diagnostic = {"batch": batch["index"], "level": "refused", "reason": MERGE_REASON}
                document["diagnostics"].append(diagnostic); public["diagnostics"].append(diagnostic)
                continue
            warnings = [{"batch": batch["index"], "level": "warning",
                         "reason": f"CTE {name}: {REUSED_REASON}"}
                        for name in sorted(reused_names(expressions))]
            ctes, edges = discover(expressions, batch)
            columns, references, terminal, terminal_edges = collect_records(batch['expressions'], batch, ctes)
            batch_layers = [{"batch": batch["index"], "ctes": layer} for layer in layers(ctes, edges)]
            batch_lineage = [record for expression in expressions for record in for_expression(expression, batch["index"])]
            unions = check(expressions, batch["index"])
            dead = analyze_dead(expressions, ctes, batch_lineage, batch["index"])
        except Exception as exc:
            reason = ("this batch is nested too deeply to analyze" if isinstance(exc, RecursionError)
                      else "this batch could not be analyzed")
            diagnostic = {"batch": batch["index"], "level": "refused", "reason": reason}
            document["diagnostics"].append(diagnostic); public["diagnostics"].append(diagnostic)
            continue
        # Publish only after every analysis stage of this batch has succeeded.
        document["ctes"].extend(ctes); document["edges"].extend(edges)
        document["columns"].extend(columns); document["references"].extend(references)
        document["terminal"].extend(terminal); document["edges"].extend(terminal_edges)
        document["layers"].extend(batch_layers)
        document["lineage"].extend(batch_lineage)
        document["unions"].extend(unions)
        document["dead"]["ctes"].extend(dead["ctes"]); document["dead"]["columns"].extend(dead["columns"])
        document["diagnostics"].extend(warnings); public["diagnostics"].extend(warnings)
    return document
