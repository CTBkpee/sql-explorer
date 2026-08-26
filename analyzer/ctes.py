"""CTE metadata, dependencies, and deterministic DAG layers."""

import re
from collections import defaultdict

from sqlglot import exp


def _output_columns(cte: exp.CTE) -> list[str]:
    declared = [column.name for column in (cte.args.get("alias") or exp.TableAlias()).columns]
    if declared:
        return declared
    query = cte.this
    select = query if isinstance(query, exp.Select) else query.find(exp.Select)
    if not select:
        return []
    return [item.alias_or_name or "*" for item in select.expressions]


def discover(expressions: list[exp.Expression], batch: dict) -> tuple[list[dict], list[dict]]:
    """Return CTE records and outward read edges for one parsed batch."""
    cte_nodes = [cte for expression in expressions for cte in expression.find_all(exp.CTE)]
    names = {cte.alias_or_name.lower() for cte in cte_nodes}
    seen_at = 0
    records, edges = [], []
    for ordinal, cte in enumerate(cte_nodes):
        name = cte.alias_or_name
        match = re.search(r"\b" + re.escape(name) + r"\b", batch["sql"][seen_at:], re.I)
        relative = seen_at + (match.start() if match else 0)
        if match:
            seen_at = relative + len(name)
        record = {"id": f"b{batch['index']}:{name}", "batch": batch["index"], "name": name,
                  "ordinal": ordinal, "start": batch["start"] + relative,
                  "end": batch["start"] + relative + len(name), "columns": _output_columns(cte),
                  "resolved": True}
        records.append(record)
        for table in cte.this.find_all(exp.Table):
            target = table.name
            if target.lower() == name.lower():
                continue
            edges.append({"batch": batch["index"], "source": record["id"],
                          "target": f"b{batch['index']}:{target}" if target.lower() in names else target,
                          "kind": "cte" if target.lower() in names else "table"})
    # sqlglot cannot infer physical schemas, but a star from an earlier CTE has a known shape.
    by_name = {record["name"].lower(): record for record in records}
    for cte, record in zip(cte_nodes, records):
        select = cte.this if isinstance(cte.this, exp.Select) else cte.this.find(exp.Select)
        if not select or not any(isinstance(item, exp.Star) for item in select.expressions):
            continue
        direct_tables = [table for table in select.find_all(exp.Table)
                         if table.find_ancestor(exp.Select) is select]
        if len(direct_tables) == 1 and direct_tables[0].name.lower() in by_name:
            record["columns"] = list(by_name[direct_tables[0].name.lower()]["columns"])
        else:
            record["resolved"] = False
    return records, _unique_edges(edges)


def _unique_edges(edges: list[dict]) -> list[dict]:
    result, seen = [], set()
    for edge in edges:
        key = tuple(edge.items())
        if key not in seen:
            result.append(edge); seen.add(key)
    return result


def layers(ctes: list[dict], edges: list[dict]) -> list[list[str]]:
    """Dependency-first layers; cycles are placed deterministically at the end."""
    ids = {item["id"] for item in ctes}
    deps = defaultdict(set)
    for edge in edges:
        if edge["source"] in ids and edge["target"] in ids:
            deps[edge["source"]].add(edge["target"])
    remaining = set(ids); result = []
    while remaining:
        ready = sorted(node for node in remaining if not (deps[node] & remaining))
        if not ready:
            ready = sorted(remaining)
        result.append(ready)
        remaining.difference_update(ready)
    return result
