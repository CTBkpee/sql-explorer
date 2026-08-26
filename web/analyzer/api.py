"""Public frozen JSON contract for the T-SQL explorer."""

from sqlglot import exp

from .ctes import discover, layers
from .deadcode import analyze_dead
from .lineage import for_expression
from .split import parse_batches
from .unions import check

MERGE_REASON = "MERGE lineage is unreliable in sqlglot; not rendered"


def analyze(sql: str) -> dict:
    """Analyze T-SQL without raising for an invalid individual GO batch."""
    raw_batches = parse_batches(sql)
    document = {"batches": [], "ctes": [], "edges": [], "layers": [], "lineage": [],
                "dead": {"ctes": [], "columns": []}, "unions": [], "diagnostics": []}
    for batch in raw_batches:
        public = {key: batch[key] for key in ("index", "start", "end", "sql", "parsed", "statement_count", "diagnostics")}
        document["batches"].append(public)
        document["diagnostics"].extend({"batch": batch["index"], **diag} for diag in batch["diagnostics"])
        if not batch["parsed"]:
            continue
        if any(expression.find(exp.Merge) for expression in batch["expressions"]):
            diagnostic = {"batch": batch["index"], "level": "refused", "reason": MERGE_REASON}
            document["diagnostics"].append(diagnostic); public["diagnostics"].append(diagnostic)
            continue
        ctes, edges = discover(batch["expressions"], batch)
        document["ctes"].extend(ctes); document["edges"].extend(edges)
        document["layers"].extend({"batch": batch["index"], "ctes": layer} for layer in layers(ctes, edges))
        batch_lineage = [record for expression in batch["expressions"] for record in for_expression(expression, batch["index"])]
        document["lineage"].extend(batch_lineage)
        document["unions"].extend(check(batch["expressions"], batch["index"]))
        dead = analyze_dead(batch["expressions"], ctes, batch_lineage, batch["index"])
        document["dead"]["ctes"].extend(dead["ctes"]); document["dead"]["columns"].extend(dead["columns"])
    return document
