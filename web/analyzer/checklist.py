"""Read-only routes for surfacing a visible column in terminal SELECT lists."""

from collections import defaultdict
from bisect import bisect_right
from functools import lru_cache

from sqlglot import exp

from .columns import collect_records, expression_span, select_for
from .ctes import REUSED_REASON, discover, query_expression, reused_names
from .split import parse_batches

MAX_PATHS = 256
NOT_VISIBLE = ("this column is not visible in the pasted script; adding it from a "
               "physical table needs a schema — v2")


def _name(value):
    """Compare logical names while accepting their source-spelled delimiters."""
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1].replace("]]", "]")
    elif value.startswith('"') and value.endswith('"'):
        value = value[1:-1].replace('""', '"')
    return value.casefold()


def _branches(body):
    """Select-list branches only; a nested subquery is not a UNION branch."""
    branches, pending = [], [body]
    while pending:
        body = query_expression(pending.pop())
        if isinstance(body, exp.SetOperation):
            pending.extend((body.expression, body.this))
        else:
            select = select_for(body)
            if select is not None:
                branches.append(select)
    return branches


def _star(select, incoming):
    """Whether this route's immediate source is covered, and qualifier validity."""
    sources = {}
    from_clause = select.args.get("from_")
    relations = ([from_clause.this] if from_clause is not None else [])
    relations += [join.this for join in select.args.get("joins", [])]
    for relation in relations:
        source = _name(relation.name) if isinstance(relation, exp.Table) else None
        sources[_name(relation.alias_or_name)] = source
        if source:
            sources[source] = source
    covered, invalid = False, False
    for item in select.expressions:
        if isinstance(item, exp.Star):
            covered = True
        elif isinstance(item, exp.Column) and item.is_star:
            qualifier = _name(item.table)
            if qualifier not in sources:
                invalid = True
            elif sources[qualifier] == incoming:
                covered = True
    return covered, invalid


def _anchor(item, batch, line_starts):
    try:
        start, _ = expression_span(item, batch)
    except ValueError:
        return None
    if start is None or not batch["start"] <= start < batch["end"]:
        return None
    return {"line": bisect_right(line_starts, start), "start": start}


def _hop(owner, column, owners, line_starts, incoming=None):
    body, batch, record, kind = owners[owner]
    hop = {"owner": owner[2], "kind": kind, "anchors": [],
           "needs_edit": True, "caveats": []}
    if any(isinstance(node, exp.Group) for node in
           body.walk(prune=lambda node: isinstance(node, exp.With))):
        hop["caveats"].append("group_by")
    if not record.get("resolved", True):
        hop["caveats"].append("unresolved")
        return hop
    branches = _branches(body)
    if not branches:
        hop["caveats"].append("unresolved")
        return hop
    star_states = [_star(branch, incoming) for branch in branches]
    if any(invalid for _, invalid in star_states):
        hop["caveats"].append("unresolved")
        return hop
    stars = [covered for covered, _ in star_states]
    if all(stars):
        hop["needs_edit"] = False
        return hop
    projected = [any(_name(item.alias_or_name) == column for item in branch.expressions)
                 for branch in branches]
    if all(star or present for star, present in zip(stars, projected)):
        hop["needs_edit"] = False
        hop["caveats"].append("already_projected")
        return hop
    if any(projected):
        hop["caveats"].append("already_projected_partial")
    for branch, star, present in zip(branches, stars, projected):
        if star or present:
            continue
        anchor = _anchor(branch.expressions[-1], batch, line_starts) if branch.expressions else None
        if anchor is None:
            hop["anchors"] = []
            hop["caveats"].append("unresolved")
            return hop
        hop["anchors"].append(anchor)
    return hop


def _graph(sql):
    columns, references, edges, terminals, owners, ambiguous = [], [], [], set(), {}, set()
    for batch in parse_batches(sql):
        if not batch["parsed"]:
            raise ValueError("this script could not be analyzed")
        if any(statement.find(exp.Merge)
               for statement in batch["expressions"] if statement is not None):
            continue
        ambiguous.update(f"b{batch['index']}:{name}" for name in reused_names(batch['expressions']))
        for index, statement in enumerate(batch['expressions']):
            if statement is None:
                continue
            # Private graph keys include statement context; public ids stay frozen.
            context = (batch['index'], index)
            key = lambda owner: (*context, owner)
            ctes, cte_edges = discover([statement], batch)
            cols, refs, terminal_records, terminal_edges = collect_records(
                [statement], batch, ctes, statement_offset=index)
            columns.extend(cols)
            references.extend(refs)
            edges.extend((key(edge['target']), key(edge['source']))
                         for edge in cte_edges + terminal_edges if edge['kind'] == 'cte')
            for cte, record in zip(statement.find_all(exp.CTE), ctes):
                owners[key(record['id'])] = (cte.this, batch, record, "cte")
            for terminal in terminal_records:
                owner = key(terminal['id'])
                terminals.add(owner)
                owners[owner] = (statement, batch, terminal, "terminal")
    children = defaultdict(list)
    seen_edges = set()
    for parent, child in edges:
        if parent in owners and child in owners and (parent, child) not in seen_edges:
            seen_edges.add((parent, child))
            children[parent].append(child)
    return columns, references, terminals, owners, children, ambiguous


def add_column_plan(sql: str, owner_id: str, column: str) -> dict:
    """Return a checklist shape for every script, including analysis failures."""
    try:
        return _add_column_plan(sql, owner_id, column)
    except Exception:
        return {"visible": False, "reason": "this script could not be analyzed", "paths": [],
                "path_count": 0, "truncated": False}


def _add_column_plan(sql: str, owner_id: str, column: str) -> dict:
    """Return every downstream route, with an explicit enumeration guard."""
    result = {"visible": False, "reason": NOT_VISIBLE, "paths": [],
              "path_count": 0, "truncated": False}
    columns, references, terminals, owners, children, ambiguous = _graph(sql)
    if owner_id.casefold() in ambiguous:
        result['reason'] = REUSED_REASON
        return result
    owner_key = next((key for key in owners if key[2] == owner_id), None)
    wanted = _name(column)
    projected = any(item["owner"] == owner_id and _name(item["name"]) == wanted
                    for item in columns)
    referenced = any(item["owner"] == owner_id and item["qualifier"] and
                     _name(item["name"]) == wanted for item in references)
    if owner_key is None or not (projected or referenced):
        return result
    result.update(visible=True, reason="")
    active = set()

    @lru_cache(maxsize=None)
    def count(owner):
        if owner in active:
            raise ValueError("dependency graph contains a cycle; routes could not be determined")
        if owner in terminals:
            return 1
        active.add(owner)
        total = sum(count(child) for child in children[owner])
        active.remove(owner)
        return total

    try:
        result["path_count"] = count(owner_key)
    except ValueError as exc:
        result["reason"] = str(exc)
        return result
    result["truncated"] = result["path_count"] > MAX_PATHS
    hop_cache = {}
    line_starts = [0] + [index + 1 for index, char in enumerate(sql) if char == '\n']

    def visit(owner, route):
        if len(result["paths"]) >= MAX_PATHS or not count(owner):
            return
        route.append(owner)
        if owner in terminals:
            hops = []
            for position in range(1 if projected else 0, len(route)):
                hop_id = route[position]
                incoming = _name(route[position - 1][2].split(":", 1)[1]) if position else None
                cache_key = (hop_id, incoming)
                if cache_key not in hop_cache:
                    hop_cache[cache_key] = _hop(hop_id, wanted, owners, line_starts, incoming)
                hops.append(hop_cache[cache_key])
            result["paths"].append({
                "label": f"Route {len(result['paths']) + 1}: " + " → ".join(key[2] for key in route),
                "terminal": owner[2], "hops": hops})
            route.pop()
            return
        for child in children[owner]:
            visit(child, route)
            if len(result["paths"]) >= MAX_PATHS:
                break
        route.pop()

    visit(owner_key, [])
    return result
