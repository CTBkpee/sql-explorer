"""Flatten sqlglot lineage trees into a JSON-safe graph."""

from sqlglot import exp
from sqlglot.lineage import lineage as sqlglot_lineage


def _flatten(root) -> tuple[list[dict], list[dict], bool]:
    nodes, edges, visited = [], [], set()

    def visit(node):
        node_id = str(id(node))
        if node_id in visited:
            return node_id
        visited.add(node_id)
        unresolved = isinstance(node.source, exp.Placeholder) or isinstance(node.expression, exp.Placeholder)
        nodes.append({"id": node_id, "name": node.name, "expression": node.expression.sql(dialect="tsql"),
                      "resolved": not unresolved})
        for child in node.downstream:
            child_id = visit(child)
            edges.append({"source": node_id, "target": child_id})
        return node_id

    visit(root)
    unresolved_ids = {node["id"] for node in nodes if not node["resolved"]}
    changed = True
    while changed:
        changed = False
        for edge in edges:
            if edge["target"] in unresolved_ids and edge["source"] not in unresolved_ids:
                unresolved_ids.add(edge["source"]); changed = True
    for node in nodes:
        node["resolved"] = node["id"] not in unresolved_ids
    return nodes, edges, not unresolved_ids


def for_expression(expression, batch_index: int) -> list[dict]:
    """Build lineage records for the statement's final SELECT outputs."""
    select = expression if isinstance(expression, exp.Select) else expression.find(exp.Select)
    if not select:
        return []
    result = []
    for item in select.expressions:
        output = item.alias_or_name
        if not output or output == "*":
            result.append({"batch": batch_index, "output": output or "*", "nodes": [], "edges": [], "resolved": False})
            continue
        try:
            root = sqlglot_lineage(output, expression, dialect="tsql")
            nodes, edges, resolved = _flatten(root)
            result.append({"batch": batch_index, "output": output, "nodes": nodes, "edges": edges, "resolved": resolved})
        except Exception as exc:
            result.append({"batch": batch_index, "output": output, "nodes": [], "edges": [],
                           "resolved": False, "error": str(exc)})
    return result
