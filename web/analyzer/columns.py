"""Source column records and on-demand, single-column lineage."""

from bisect import bisect_left, bisect_right
from collections import defaultdict

from sqlglot import Dialect, exp
from sqlglot.lineage import lineage as sqlglot_lineage
from sqlglot.optimizer.scope import traverse_scope
from sqlglot.tokens import TokenType

from .ctes import REUSED_REASON, reused_names, select_for
from .deadcode import _resolve_column
from .lineage import _flatten
from .split import parse_batches


_PARSE_MEMO = None


def column_key(name):
    """Case-insensitive identifier value, accepting its source spelling."""
    if name.startswith('[') and name.endswith(']'):
        name = name[1:-1].replace(']]', ']')
    elif name.startswith('"') and name.endswith('"'):
        name = name[1:-1].replace('""', '"')
    return name.casefold()


def _tokens(batch):
    if '_column_tokens' not in batch:
        batch['_column_tokens'] = Dialect.get_or_raise('tsql').tokenize(batch['sql'])
    return batch['_column_tokens']


def _meta_span(node):
    if 'start' in node.meta and 'end' in node.meta:
        return node.meta['start'], node.meta['end'] + 1
    return None


def _select_evidence(expressions):
    """Collect own projection/FROM identifier offsets in one AST pass.

    Entering a nested SELECT resets the owner, so its identifiers cannot certify
    the enclosing list. Traversal order has no bearing on the source binding.
    """
    records = []
    pending = [(node, None, None) for node in expressions if node is not None]
    while pending:
        node, record, part = pending.pop()
        if isinstance(node, exp.Select):
            record = (node, [[] for _ in node.expressions], [])
            records.append(record)
            for key, value in node.args.items():
                for index, child in enumerate(value if isinstance(value, list) else [value]):
                    if isinstance(child, exp.Expression):
                        section = index if key == 'expressions' else -1 if key == 'from_' else None
                        pending.append((child, record, section))
            continue
        if record is not None and part is not None and isinstance(node, exp.Identifier):
            span = _meta_span(node)
            if span is not None:
                (record[2] if part == -1 else record[1][part]).append(span)
        pending.extend((child, record, part) for child in node.iter_expressions())
    return records


def _select_bounds(node, batch):
    select = node if isinstance(node, exp.Select) else node.find_ancestor(exp.Select)
    return _source_lists(batch)[1].get(id(select))


def _source_lists(batch):
    """Bind source lists only through consistent parser identifier offsets.

    The token pass records lexical ownership, including nested queries. The AST
    pass supplies evidence; neither SELECT count nor statement order is a key.
    All offsets here are batch-relative, then rebased by the public span callers.
    """
    if '_projection_lists' in batch:
        return batch['_projection_lists']
    tokens = _tokens(batch)
    spans, bounds, frames, active, parentheses, pairs, evidence = {}, {}, [], [], [], {}, {}
    depth = 0
    stops = {TokenType.FROM, TokenType.INTO, TokenType.WHERE, TokenType.GROUP_BY,
             TokenType.HAVING, TokenType.ORDER_BY, TokenType.UNION, TokenType.INTERSECT,
             TokenType.EXCEPT, TokenType.SEMICOLON, TokenType.R_PAREN}
    ends = {TokenType.UNION, TokenType.INTERSECT, TokenType.EXCEPT,
            TokenType.SEMICOLON, TokenType.R_PAREN}

    def finish(frame, limit):
        if frame['mode'] != 'projection':
            return
        ranges = frame['ranges']
        ranges.append((frame['first'], limit))
        frame['limit'] = limit
        # SELECT modifiers precede the first projection, but belong to no projection.
        first, end = ranges[0]
        if first < end and tokens[first].token_type in (TokenType.DISTINCT, TokenType.ALL):
            first += 1
        if first < end and tokens[first].token_type == TokenType.TOP:
            first += 1
            if first in pairs:
                first = pairs[first] + 1
            else:
                while first < end and tokens[first].token_type == TokenType.PARAMETER:
                    first += 1
                first += 1
            if first < end and tokens[first].token_type == TokenType.PERCENT:
                first += 1
            if first + 1 < end and tokens[first].text.casefold() == 'with' and tokens[first + 1].text.casefold() == 'ties':
                first += 2
        ranges[0] = first, end
        frame['mode'] = 'other'

    for index, token in enumerate(tokens):
        kind = token.token_type
        if kind == TokenType.SELECT:
            if active and active[-1]['depth'] == depth:
                finish(active.pop(), index)
            frame = dict(begin=index, first=index + 1, ranges=[], depth=depth,
                         mode='projection', number=len(frames))
            frames.append(frame)
            active.append(frame)
        elif active:
            frame = active[-1]
            if frame['depth'] == depth:
                if kind in stops:
                    finish(frame, index)
                    frame['mode'] = 'from' if kind == TokenType.FROM else 'other'
                    if kind in ends:
                        active.pop()
                elif kind == TokenType.COMMA and frame['mode'] == 'projection':
                    frame['ranges'].append((frame['first'], index))
                    frame['first'] = index + 1
            if active:
                frame = active[-1]
                if frame['mode'] in ('projection', 'from'):
                    part = len(frame['ranges']) if frame['mode'] == 'projection' else -1
                    evidence[(token.start, token.end + 1)] = (frame['number'], part)
        if kind == TokenType.L_PAREN:
            parentheses.append(index)
            depth += 1
        elif kind == TokenType.R_PAREN:
            depth -= 1
            if parentheses:
                pairs[parentheses.pop()] = index
    for frame in active:
        finish(frame, len(tokens))

    for select, projections, source in _select_evidence(batch.get('expressions', [])):
        candidate, valid = None, True
        for part, identifiers in [*enumerate(projections), (-1, source)]:
            for span in identifiers:
                match = evidence.get(span)
                if match is None or match[1] != part or (candidate is not None and candidate != match[0]):
                    valid = False
                    break
                candidate = match[0]
            if not valid:
                break
        if not valid or candidate is None:
            continue
        frame = frames[candidate]
        ranges = frame['ranges']
        if len(ranges) != len(projections) or any(left >= right for left, right in ranges):
            continue
        char_ranges = [(tokens[left].start, tokens[right - 1].end + 1) for left, right in ranges]
        if any(not (left <= start < end <= right)
               for (left, right), identifiers in zip(char_ranges, projections)
               for start, end in identifiers):
            continue
        for item, span in zip(select.expressions, char_ranges):
            spans[id(item)] = span
        limit = frame['limit']
        bounds[id(select)] = (tokens[frame['begin']].start,
                              tokens[limit].start if limit < len(tokens) else len(batch['sql']),
                              frame['depth'])
    batch['_projection_lists'] = spans, bounds
    return spans, bounds


def _scan_index(batch):
    if '_span_index' not in batch:
        available = [token for token in _tokens(batch) if token.text.casefold() != 'as']
        words = [token.text.casefold() for token in available]
        positions = defaultdict(list)
        for index, word in enumerate(words):
            positions[word].append(index)
        depths, depth = {}, 0
        for token in _tokens(batch):
            depths[token.start] = depth
            depth += (token.token_type == TokenType.L_PAREN) - (token.token_type == TokenType.R_PAREN)
        batch['_span_index'] = available, words, positions, [token.start for token in available], {}, depths
    return batch['_span_index']


def _scan_span(node, batch, allow_known=True):
    """Match parser tokens locally when a node has no source metadata."""
    pattern = Dialect.get_or_raise('tsql').tokenize(node.sql(dialect='tsql'))
    # The generator writes AS even when the original used an implicit alias.
    pattern = [token.text.casefold() for token in pattern if token.text.casefold() != 'as']
    available, words, positions, starts, cached, depths = _scan_index(batch)
    known = [span for child in node.walk() if (span := _meta_span(child))]
    candidates = []
    indices = positions.get(pattern[0], []) if pattern else []
    if known:
        first_known = bisect_right(starts, min(left for left, _ in known)) - 1
        indices = indices[bisect_left(indices, first_known - len(pattern) + 1):
                          bisect_right(indices, first_known)]
    cache_key = tuple(pattern)
    if not known and cache_key in cached:
        candidates = cached[cache_key]
        indices = []
    for index in indices:
        if words[index:index + len(pattern)] != pattern:
            continue
        start, end = available[index].start, available[index + len(pattern) - 1].end + 1
        if all(start <= left and right <= end for left, right in known):
            candidates.append((start, end))
    if not known:
        cached[cache_key] = candidates
    if len(candidates) > 1:
        bounds = _select_bounds(node, batch)
        if bounds is not None:
            begin, limit, wanted_depth = bounds
            local = candidates[bisect_left(candidates, (begin,)):bisect_left(candidates, (limit,))]
            candidates = [span for span in local if depths[span[0]] == wanted_depth]
        select = node.find_ancestor(exp.Select)
        item = node
        while item.parent is not None and item.parent is not select:
            item = item.parent
        if len(candidates) > 1 and select is not None:
            ranks = batch.setdefault('_projection_ranks', {})
            if id(select) not in ranks:
                groups = defaultdict(list)
                for other in select.expressions:
                    groups[other.sql(dialect='tsql')].append(other)
                ranks[id(select)] = {id(other): (index, len(group))
                                     for group in groups.values() for index, other in enumerate(group)}
            index, count = ranks[id(select)].get(id(item), (0, 0))
            if count == len(candidates):
                candidates = [candidates[index]]
    if len(candidates) == 1:
        return candidates[0]
    if known and allow_known:
        return min(left for left, _ in known), max(right for _, right in known)
    raise ValueError('source position is unavailable')


def expression_span(item, batch):
    """Absolute source span of an entire projection, for checklist anchors."""
    span = _source_lists(batch)[0].get(id(item))
    if span is None:
        raise ValueError('source projection is unavailable')
    start, end = span
    return batch['start'] + start, batch['start'] + end


def projection_span(item, batch):
    """Absolute source span of a projection's output name."""
    if isinstance(item, exp.Alias):
        node = item.args['alias']
    elif isinstance(item, exp.Column):
        node = item.this
    else:
        node = item
    span = _meta_span(node)
    if span is None:
        span = _scan_span(node, batch)
    return batch['start'] + span[0], batch['start'] + span[1]


def _owner(node, batch_index, terminal_id):
    cte = node.find_ancestor(exp.CTE)
    return f'b{batch_index}:{cte.alias_or_name}' if cte is not None else terminal_id


def _projected(body, owner, batch, resolved=True, declared=None, owner_start=None):
    select = select_for(body)
    if select is None:
        return []
    items = declared or select.expressions
    if owner_start is None:
        spans = [span for node in body.find_all(exp.Identifier) if (span := _meta_span(node))]
        owner_start = batch['start'] + min((left for left, _ in spans), default=0)
    records = []
    for ordinal, item in enumerate(items):
        named = isinstance(item, (exp.Alias, exp.Column, exp.Identifier)) and not item.is_star
        positioned = True
        try:
            if named or item.is_star:
                start, end = projection_span(item, batch)
            else:
                left, right = _scan_span(item, batch, allow_known=False)
                start, end = batch['start'] + left, batch['start'] + right
        except ValueError:
            start = end = owner_start
            positioned = False
        name = batch['sql'][start - batch['start']:end - batch['start']]
        records.append({'id': f'{owner}/{ordinal}', 'batch': batch['index'], 'owner': owner,
                        'name': name, 'ordinal': ordinal, 'start': start, 'end': end,
                        'resolved': bool(resolved and named and positioned)})
    return records


def _is_read(table, statement):
    """Exclude write-target syntax, keeping a separate FROM occurrence of it."""
    if table.find_ancestor(exp.Into) is not None:
        return False
    if isinstance(statement, (exp.Drop, exp.TruncateTable)):
        return False
    if isinstance(statement, exp.Delete) and statement.args.get('tables'):
        return not any(table is target for target in statement.args['tables'])
    if isinstance(statement, (exp.Create, exp.Insert, exp.Update, exp.Delete)):
        target = statement.this
        node = table
        while node is not None:
            if node is target:
                return False
            node = node.parent
    return True


def collect_records(expressions, batch, ctes, statement_offset=0):
    """Cheap additive records for a parsed batch; never calls lineage."""
    columns, references, terminals, edges = [], [], [], []
    cte_nodes = [cte for statement in expressions if statement is not None
                 for cte in statement.find_all(exp.CTE)]
    by_node = {id(node): record for node, record in zip(cte_nodes, ctes)}
    ambiguous = reused_names(expressions)
    for expression in expressions:
        if expression is None:
            continue
        for cte in expression.find_all(exp.CTE):
            owner = f"b{batch['index']}:{cte.alias_or_name}"
            if cte.alias_or_name.casefold() in ambiguous:
                continue
            declared = (cte.args.get('alias') or exp.TableAlias()).columns
            record = by_node[id(cte)]
            columns.extend(_projected(cte.this, owner, batch, record['resolved'], declared, record['start']))
    by_column = {}
    for record in columns:
        key = record['owner'], column_key(record['name'])
        # A repeated output name cannot identify one column record.
        by_column[key] = record['id'] if key not in by_column else None
    for statement_index, expression in enumerate(expressions, start=statement_offset):
        if expression is None:
            continue
        terminal_id = f"t{batch['index']}:{statement_index}"
        statement_ctes = {cte.alias_or_name.casefold(): by_node[id(cte)]
                          for cte in expression.find_all(exp.CTE)}
        projected = _projected(expression, terminal_id, batch)
        columns.extend(projected)
        reads = []
        seen_reads = set()
        tables = sorted(expression.find_all(exp.Table),
                        key=lambda table: (_meta_span(table.this) or (len(batch['sql']), 0))[0])
        for table in tables:
            if table.find_ancestor(exp.CTE) is not None or not _is_read(table, expression):
                continue
            cte = statement_ctes.get(table.name.casefold()) if not table.db and not table.catalog else None
            target = cte['id'] if cte else table.name
            if target not in seen_reads:
                seen_reads.add(target)
                reads.append(target)
                edges.append({'batch': batch['index'], 'source': terminal_id, 'target': target,
                              'kind': 'cte' if cte else 'table'})
        spans = [span for node in expression.find_all(exp.Identifier) if (span := _meta_span(node))]
        start = batch['start'] + min((left for left, _ in spans), default=0)
        end = batch['start'] + max((right for _, right in spans), default=0)
        terminals.append({'id': terminal_id, 'batch': batch['index'], 'statement_index': statement_index,
                          'reads': reads, 'columns': [record['name'] for record in projected],
                          'start': start, 'end': end})
        scopes = {}
        try:
            for scope in traverse_scope(expression):
                for node in scope.walk():
                    if isinstance(node, exp.Column):
                        scopes[id(node)] = scope
        except Exception:
            pass
        for node in expression.find_all(exp.Column):
            start, end = projection_span(node, batch)
            binds_to = None
            scope = scopes.get(id(node))
            if scope is not None:
                refs = defaultdict(set)
                unknown = set()
                try:
                    _resolve_column(node, scope, statement_ctes, refs, unknown)
                    wanted = node.name.casefold()
                    matches = [by_column[key] for name, names in refs.items()
                               if wanted in names and name not in unknown
                               and (key := (statement_ctes[name]['id'], wanted)) in by_column]
                    if len(matches) == 1:
                        binds_to = matches[0]
                except Exception:
                    pass
            references.append({'batch': batch['index'], 'start': start, 'end': end,
                               'name': batch['sql'][start - batch['start']:end - batch['start']],
                               'qualifier': node.table,
                               'owner': _owner(node, batch['index'], terminal_id), 'binds_to': binds_to})
    return columns, references, terminals, edges


def _memo_batches(sql):
    global _PARSE_MEMO
    if _PARSE_MEMO is None or _PARSE_MEMO[0] != sql:
        _PARSE_MEMO = (sql, parse_batches(sql))
    return _PARSE_MEMO[1]


def column_lineage(sql, owner_id, column):
    """Return one output's lineage, using a CTE probe or the original terminal."""
    try:
        for batch in _memo_batches(sql):
            if not batch['parsed']:
                continue
            if owner_id.casefold() in {f"b{batch['index']}:{name}" for name in reused_names(batch['expressions'])}:
                raise ValueError(REUSED_REASON)
            for statement_index, statement in enumerate(batch['expressions']):
                if statement is None or statement.find(exp.Merge):
                    continue
                cte = next((cte for cte in statement.find_all(exp.CTE)
                            if f"b{batch['index']}:{cte.alias_or_name}" == owner_id), None)
                terminal = owner_id == f"t{batch['index']}:{statement_index}"
                if cte is None and not terminal:
                    continue
                select = select_for(cte.this if cte is not None else statement)
                declared = (cte.args.get('alias') or exp.TableAlias()).columns if cte is not None else []
                names = [item.name for item in declared] if declared else [item.alias_or_name for item in select.expressions] if select is not None else []
                actual = next((name for name in names if column_key(name) == column_key(column)), None)
                if not actual or actual == '*':
                    raise ValueError('unknown column or unresolved projection')
                if cte is not None:
                    probe = exp.select(exp.column(actual, quoted=True)).from_(exp.Table(this=exp.to_identifier(cte.alias_or_name, quoted=True)))
                    with_clause = statement.find(exp.With)
                    if with_clause is not None:
                        probe.set('with_', with_clause.copy())
                else:
                    probe = statement.copy()
                root = sqlglot_lineage(actual, probe, dialect='tsql')
                nodes, edges, resolved = _flatten(root)
                if not resolved:
                    return {'nodes': [], 'edges': [], 'resolved': False, 'error': 'column lineage is unresolved'}
                return {'nodes': nodes, 'edges': edges, 'resolved': resolved}
        raise ValueError('unknown owner')
    except Exception as exc:
        return {'nodes': [], 'edges': [], 'resolved': False, 'error': str(exc)}
