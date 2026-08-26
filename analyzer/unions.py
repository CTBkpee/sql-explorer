"""UNION branch shape checks."""

from sqlglot import exp


def check(expressions: list[exp.Expression], batch_index: int) -> list[dict]:
    findings = []
    for union in (node for expression in expressions for node in expression.find_all(exp.Union)):
        branches = _branches(union)
        shapes = [[item.alias_or_name or "*" for item in branch.expressions] for branch in branches]
        counts = [len(shape) for shape in shapes]
        if len(set(counts)) > 1:
            findings.append({"batch": batch_index, "kind": "column_count", "counts": counts})
        for position in range(min(counts, default=0)):
            aliases = [shape[position] for shape in shapes]
            if len(set(alias.lower() for alias in aliases)) > 1:
                findings.append({"batch": batch_index, "kind": "alias", "position": position,
                                 "aliases": aliases})
    return findings


def _branches(node):
    if isinstance(node.this, exp.Union):
        return _branches(node.this) + [node.expression]
    return [node.this, node.expression]
