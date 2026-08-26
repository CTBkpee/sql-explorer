"""Conservative dead-code analysis for CTE projections.

Reference collection rides on sqlglot's own scope machinery instead of a hand-rolled walk.
`traverse_scope` yields every scope of a statement (root query, CTE bodies, derived tables,
correlated subqueries, APPLY laterals, set-operation branches) and `Scope.walk()` yields the
nodes that belong to exactly that scope. Each column node is resolved against the sources of
its own scope first and then outward along the correlation chain (a subquery sees its
enclosing query, an APPLY sees the sources to its left), which is how T-SQL binds names.

The invariant this module exists to protect: a column is only ever reported `dead` when every
column node of the statement was placed. Anything the walk cannot account for — a statement
shape sqlglot does not scope, a qualifier that resolves nowhere, an unqualified name that
could bind to more than one source or to a source whose columns are not known — downgrades
the affected CTEs to `unknown`. Saying `unknown` too often is acceptable; saying `dead` once
when a reference exists is not.
"""

from sqlglot import exp
from sqlglot.optimizer.scope import Scope, traverse_scope


class Unplaceable(Exception):
    """A statement contains references the scope walk cannot account for."""


class _Source:
    """One distinct selectable source of a scope level, as the binder sees it."""

    __slots__ = ("cte", "columns", "opaque")

    def __init__(self, cte, columns, opaque):
        self.cte = cte            # lowercased CTE name, or None for anything that is not a CTE
        self.columns = columns    # casefolded output names when known
        self.opaque = opaque      # True when the output column list is not fully known


def analyze_dead(expressions, ctes: list[dict], lineage: list[dict], batch_index: int) -> dict:
    by_name = {cte["name"].lower(): cte for cte in ctes}
    refs = {name: set() for name in by_name}
    unknown = {name for name, cte in by_name.items() if not cte["resolved"]}

    for expression in expressions:
        # T-SQL CTEs are statement-scoped: only the statement that defines a CTE can read it.
        owned = {cte.alias_or_name.lower() for cte in expression.find_all(exp.CTE)} & set(by_name)
        if not owned:
            continue
        try:
            _collect(expression, by_name, refs, unknown)
        except Exception:  # noqa: BLE001 - any failure to place references is "could not tell"
            unknown.update(owned)

    for item in lineage:
        if not item["resolved"]:
            for node in item.get("nodes", []):
                for name, cte in by_name.items():
                    if cte["name"].lower() in node["name"].lower():
                        unknown.add(name)
            # An unresolved final output can be influenced by any referenced CTE.
            unknown.update(by_name)

    incoming = {name: 0 for name in by_name}
    definitions = [cte for expression in expressions for cte in expression.find_all(exp.CTE)]
    for expression in expressions:
        for table in expression.find_all(exp.Table):
            if table.name.lower() in incoming and not table.find_ancestor(exp.CTE):
                incoming[table.name.lower()] += 1
    # CTE-to-CTE reads count as references, excluding each CTE's own defining source.
    for cte_node in definitions:
        own = cte_node.alias_or_name.lower()
        for table in cte_node.this.find_all(exp.Table):
            if table.name.lower() in incoming and table.name.lower() != own:
                incoming[table.name.lower()] += 1

    cte_markers, column_markers = [], []
    for name, cte in by_name.items():
        state = "unknown" if name in unknown else ("dead" if incoming[name] == 0 else "live")
        cte_markers.append({"batch": batch_index, "cte": cte["id"], "state": state})
        for column in cte["columns"]:
            state = "unknown" if name in unknown else ("dead" if column.casefold() not in refs[name] else "live")
            column_markers.append({"batch": batch_index, "cte": cte["id"], "column": column, "state": state})
    return {"ctes": cte_markers, "columns": column_markers}


# --- reference collection over sqlglot scopes -------------------------------------------------

def _collect(expression, by_name, refs, unknown) -> None:
    """Attribute every column node of one statement, or raise Unplaceable."""
    scopes = _unique(traverse_scope(expression))
    if not scopes:
        raise Unplaceable("sqlglot does not scope this statement")

    # Every CTE definition must have been traversed as a scope, or its body was never read.
    covered_ctes = {id(_defining_cte(scope)) for scope in scopes if scope.is_cte}
    for cte in expression.find_all(exp.CTE):
        if id(cte) not in covered_ctes:
            raise Unplaceable(f"CTE {cte.alias_or_name} was not scoped")

    seen_columns = set()
    for scope in scopes:
        for node in scope.walk():
            if isinstance(node, exp.Column):
                seen_columns.add(id(node))
                _resolve_column(node, scope, by_name, refs, unknown)
            elif isinstance(node, exp.Star) and not isinstance(node.parent, exp.Column):
                for source in _level_sources(scope, by_name):
                    if source.cte:
                        refs[source.cte].update(source.columns)

    # Every column node must have been walked by exactly one scope. A leftover means the
    # statement has a shape the scope walk does not cover (DML clauses, for instance).
    for column in expression.find_all(exp.Column):
        if id(column) not in seen_columns:
            raise Unplaceable(f"column {column.sql()} was not placed in any scope")


def _resolve_column(column: exp.Column, scope: Scope, by_name, refs, unknown) -> None:
    qualifier = column.table
    if qualifier:
        source = _lookup_qualifier(qualifier, scope)
        if source is None:
            if qualifier.lower() in by_name:
                cte = qualifier.lower()      # a CTE name sqlglot did not link; still a reference
            else:
                raise Unplaceable(f"qualifier {qualifier} resolves to no source")
        else:
            cte = _cte_name(source, by_name)
            if cte is None:
                return                       # physical table, derived table or APPLY output
        if isinstance(column.this, exp.Star):
            refs[cte].update(_known_columns(by_name[cte]))
        else:
            refs[cte].add(column.name.casefold())
        return
    _bind_unqualified(column.name.casefold(), scope, by_name, refs, unknown)


def _lookup_qualifier(qualifier: str, scope: Scope):
    """Innermost-first search for an alias or name through every enclosing scope."""
    wanted = qualifier.casefold()
    current = scope
    while current is not None:
        for key, value in current.sources.items():
            if key.casefold() == wanted:
                return value
        current = current.parent
    return None


def _bind_unqualified(name: str, scope: Scope, by_name, refs, unknown) -> None:
    """Bind a bare column name the way T-SQL does: innermost scope that can own it wins."""
    levels = _correlation_levels(scope)
    for index, level in enumerate(levels):
        candidates = [source for source in _level_sources(level, by_name)
                      if source.opaque or name in source.columns]
        if not candidates:
            continue
        single = candidates[0]
        if len(candidates) == 1 and single.cte and not single.opaque:
            refs[single.cte].add(name)
            return
        # Ambiguous at this level, or a source whose columns are not known: the name may bind
        # here or further out, and nothing in the script says which.
        unknown.update(source.cte for source in candidates if source.cte)
        for level_out in levels[index + 1:]:
            unknown.update(source.cte for source in _level_sources(level_out, by_name) if source.cte)
        return


def _correlation_levels(scope: Scope) -> list[Scope]:
    """Scopes whose sources a bare name may bind to, innermost first."""
    levels = []
    current = scope
    while current is not None:
        levels.append(current)
        if not (current.is_subquery or current.is_udtf or current.is_union):
            break
        current = current.parent
    return levels


def _level_sources(scope: Scope, by_name) -> list[_Source]:
    """Distinct FROM/JOIN sources of one scope, as the binder sees them."""
    result, seen = [], set()
    for node, source in scope.selected_sources.values():
        if id(source) in seen:
            continue
        seen.add(id(source))
        cte = _cte_name(source, by_name)
        if cte is not None:
            columns = _known_columns(by_name[cte])
            result.append(_Source(cte, columns, columns is None))
            continue
        if isinstance(source, exp.Table):
            result.append(_Source(None, None, True))
            continue
        declared = [column.casefold() for column in (node.alias_column_names or [])]
        columns = set(declared) if declared else _output_names(source.expression)
        result.append(_Source(None, columns, columns is None))
    for source in result:
        if source.opaque:
            source.columns = set()
    return result


def _cte_name(source, by_name):
    """Lowercased CTE name for a scope source, or None when it is not a CTE."""
    if isinstance(source, Scope):
        if not source.is_cte:
            return None
        cte = _defining_cte(source)
        if cte is None:
            raise Unplaceable("CTE scope without a defining CTE node")
        name = cte.alias_or_name.lower()
    elif isinstance(source, exp.Table):
        name = source.name.lower()      # sqlglot links CTE reads case-sensitively; T-SQL does not
    else:
        return None
    return name if name in by_name else None


def _defining_cte(scope: Scope):
    expression = scope.expression
    return expression if isinstance(expression, exp.CTE) else expression.find_ancestor(exp.CTE)


def _known_columns(cte: dict):
    """Casefolded projected columns of a discovered CTE, or None when they are not fully known."""
    if not cte["resolved"] or any(column in ("", "*") for column in cte["columns"]):
        return None
    return {column.casefold() for column in cte["columns"]}


def _output_names(expression):
    """Casefolded output names of a derived query, or None when they are not fully known."""
    while isinstance(expression, (exp.Subquery, exp.Lateral, exp.SetOperation)):
        expression = expression.this
    if not isinstance(expression, exp.Select):
        return None
    names = []
    for item in expression.expressions:
        if isinstance(item, exp.Star) or isinstance(getattr(item, "this", None), exp.Star):
            return None
        name = item.alias_or_name
        if not name:
            return None
        names.append(name.casefold())
    return set(names)


def _unique(scopes):
    result, seen = [], set()
    for scope in scopes:
        if id(scope) not in seen:
            seen.add(id(scope)); result.append(scope)
    return result
