"""CTE metadata, dependencies, and deterministic DAG layers."""

from collections import defaultdict

from sqlglot import exp

_ID_START = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_#@")
_ID_BODY = _ID_START | set("0123456789$")


def _name_span(sql: str, name: str, start_from: int) -> tuple[int, int] | None:
    """First real occurrence of ``name`` in ``sql`` at or after ``start_from``.

    Scans identifiers while skipping line/block comments, string literals, and
    bracketed identifiers, so a CTE name that also appears inside a preceding
    comment (``-- cte_base: ...`` above ``WITH cte_base AS (``) or a string
    literal never anchors the definition. Mirrors ``web/highlight.js``'s
    ``identifierRanges`` so the Python offset and the browser scan agree on the
    same definition token. Returns the ``(start, end)`` slice of the match, or
    ``None`` when the name is not found as a real token.
    """
    wanted = name.lower()
    cursor, length = 0, len(sql)
    while cursor < length:
        char = sql[cursor]
        if char == "'" or (char == "N" and sql[cursor + 1:cursor + 2] == "'"):
            cursor += 2 if char == "N" else 1
            while cursor < length:
                if sql[cursor] == "'" and sql[cursor + 1:cursor + 2] == "'":
                    cursor += 2
                    continue
                if sql[cursor] == "'":
                    cursor += 1
                    break
                cursor += 1
            continue
        if sql[cursor:cursor + 2] == "--":
            newline = sql.find("\n", cursor + 2)
            cursor = length if newline < 0 else newline + 1
            continue
        if sql[cursor:cursor + 2] == "/*":
            close = sql.find("*/", cursor + 2)
            cursor = length if close < 0 else close + 2
            continue
        if char == "[":
            close = sql.find("]", cursor + 1)
            if close >= 0:
                if cursor >= start_from and sql[cursor + 1:close].lower() == wanted:
                    return cursor, close + 1
                cursor = close + 1
                continue
        if char in _ID_START:
            begin = cursor
            cursor += 1
            while cursor < length and sql[cursor] in _ID_BODY:
                cursor += 1
            if begin >= start_from and sql[begin:cursor].lower() == wanted:
                return begin, cursor
            continue
        cursor += 1
    return None


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
        span = _name_span(batch["sql"], name, seen_at)
        if span is not None:
            relative, relative_end = span
            seen_at = relative_end
        else:
            relative, relative_end = seen_at, seen_at + len(name)
        record = {"id": f"b{batch['index']}:{name}", "batch": batch["index"], "name": name,
                  "ordinal": ordinal, "start": batch["start"] + relative,
                  "end": batch["start"] + relative_end, "columns": _output_columns(cte),
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
