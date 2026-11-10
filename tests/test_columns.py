"""Synthesized contract checks for column records, probes, and checklists."""

from pathlib import Path
from unittest.mock import patch

import pytest

from analyzer.api import analyze

FIXTURES = Path(__file__).parent / "fixtures"


def _r13_chain(n, m, qualified=True, wide=False):
    names = [f'c{i}' for i in range(m)]
    bodies = [f"cte_0 AS (SELECT {', '.join(names)} FROM base_table)"]
    for i in range(1, n):
        outputs = ', '.join(f'p.{name}' if qualified else name for name in names)
        bodies.append(f'cte_{i} AS (SELECT {outputs} FROM cte_{i - 1} p)')
    return 'WITH ' + ',\n'.join(bodies) + f"\nSELECT {', '.join(names) if wide else 'c0'} FROM cte_{n - 1};"


@pytest.mark.parametrize('n,m', [(12, 30), (30, 20)])
def test_r13_binding_work_is_linear(n, m):
    from analyzer import columns
    from analyzer.ctes import discover
    from analyzer.split import parse_batches
    batch = parse_batches(_r13_chain(n, m))[0]
    ctes, _ = discover(batch['expressions'], batch)
    calls = 0
    original = columns.column_key

    def counted(name):
        nonlocal calls
        calls += 1
        return original(name)

    with patch.object(columns, 'column_key', counted):
        records, refs, _, _ = columns.collect_records(batch['expressions'], batch, ctes)
    assert len(records) == n * m + 1
    assert len(refs) == n * m + 1
    assert calls <= 4 * (len(records) + len(refs)), calls
    assert sum(ref['binds_to'] is not None for ref in refs) == (n - 1) * m + 1


def test_r13_narrow_chain_time_guard():
    from time import perf_counter
    sql = _r13_chain(30, 20)
    start = perf_counter()
    doc = analyze(sql)
    elapsed = perf_counter() - start
    assert len(doc['columns']) == 601
    assert elapsed < 2, elapsed
    start = perf_counter()
    result = plan(sql, 'b0:cte_0', 'c3')
    elapsed = perf_counter() - start
    assert result['visible'] and result['path_count'] == 1
    assert elapsed < 2, elapsed


@pytest.mark.parametrize('last,leading', [
    ('CAST(amount AS INT) AS amt', ''),
    ('CAST(amount AS INT)', ''),
    ('CONVERT(INT, amount) AS c', ''),
    ('CASE WHEN amount IS NOT NULL THEN 1 ELSE 0 END AS flag', ''),
    ('CAST(\n amount AS DECIMAL(10,2)) AS amt', ''),
    ('amount AS amt', ''), ('amount + 1', ''), ('(amount)', ''),
    ('[amount]', ''), ('base.amount', ''), ('UPPER(region) AS r', ''),
    ('COALESCE(amount, 0)', ''), ('-amount AS neg', ''), ('1 AS one', ''),
    ("'x' AS s", ''), ('COUNT(*) OVER (PARTITION BY id) AS n', ''),
    ('amt = amount', ''), ('ISNULL(amount, 0) AS a2', ''),
    ('CAST(amount AS REAL) AS amt', ''),
    ('CAST(amount AS MONEY) AS amt', ''),
    ('CAST(amount AS SMALLDATETIME) AS amt', ''),
    ('TRY_CAST(amount AS INT) AS amt', ''),
    ('IIF(amount <> 0, 1, 0) AS flag', ''),
    ('IIF(amount != 0, 1, 0) AS flag', ''),
    ('IIF(amount NOT IN (1, 2), 1, 0) AS flag', ''),
    ('IIF(amount BETWEEN 1 AND 2, 1, 0) AS flag', ''),
    ('CAST([amount] AS INT) AS [Total Amount]', ''),
    ('CAST("amount" AS INT) AS "Total Amount"', ''),
    ('(SELECT CAST(amount AS INT) FROM base) AS amt', ''),
    ('CAST(  amount   AS   INT )   AS amt', ''),
    ('CAST(amount AS INT) AS amt', '/* before the projection */  '),
    ('cast(amount as int) as amt', ''),
    ('CAST(amount AS INT) AS amt', '-- before the projection\n '),
], ids=[f'form-{i}' for i in range(33)])
@pytest.mark.parametrize('terminal', [False, True], ids=['cte', 'terminal'])
def test_r14_anchor_starts_at_source_projection(last, leading, terminal):
    prefix = 'WITH base AS (SELECT id, amount, region FROM t),\na AS (SELECT id,\n '
    suffix = ' FROM base)\nSELECT id FROM a;'
    if terminal:
        prefix = 'WITH base AS (SELECT id, amount, region FROM t)\nSELECT id,\n '
        suffix = ' FROM base;'
    sql = prefix + leading + last + suffix
    expected = len(prefix + leading)
    hops = only_hops(plan(sql, 'b0:base', 'region'))
    assert hops[0]['kind'] == ('terminal' if terminal else 'cte')
    assert hops[0]['anchors'] == [{'line': 3 + leading.count('\n'), 'start': expected}]


@pytest.mark.parametrize('terminal', [False, True], ids=['cte', 'terminal'])
def test_r14_each_union_branch_anchors_its_cast(terminal):
    sql = 'WITH base AS (SELECT id, amount, region FROM t)'
    sql += '\n' if terminal else ',\na AS ('
    expected = []
    for i, last in enumerate(('CAST(amount AS INT) AS amt',
                              'CAST(\n amount AS DECIMAL(10,2)) AS amt',
                              'cast(amount as real) as amt')):
        sql += ('\nUNION ALL\n' if i else '') + 'SELECT id, /* before */ '
        expected.append({'line': sql.count('\n') + 1, 'start': len(sql)})
        sql += last + ' FROM base'
    sql += ';' if terminal else ')\nSELECT id FROM a;'
    hop = only_hops(plan(sql, 'b0:base', 'region'))[0]
    assert hop['anchors'] == expected


@pytest.mark.parametrize('modifier', ['TOP @take ', 'TOP (@take) ', 'TOP 1 ', 'TOP (1 + 1) '])
def test_r14_first_projection_skips_top_count(modifier):
    prefix = 'WITH base AS (SELECT id, amount, region FROM t)\nSELECT ' + modifier
    sql = prefix + 'amount FROM base;'
    hop = only_hops(plan(sql, 'b0:base', 'region'))[0]
    assert hop['anchors'] == [{'line': 2, 'start': len(prefix)}]


def fixture(name):
    return (FIXTURES / name).read_text()


def probe(sql, owner, column):
    from analyzer.columns import column_lineage
    return column_lineage(sql, owner, column)


def plan(sql, owner="b0:cte_base", column="region"):
    from analyzer.checklist import add_column_plan
    return add_column_plan(sql, owner, column)


def only_hops(result):
    assert result["visible"] is True
    assert result["path_count"] == 1
    assert result["truncated"] is False
    return result["paths"][0]["hops"]


@pytest.mark.parametrize("name", sorted(path.name for path in FIXTURES.glob("*.sql")))
def test_new_offsets_on_every_fixture(name):
    from test_analyzer import assert_offsets_are_exact
    assert_offsets_are_exact(fixture(name))


def test_records_have_exact_shapes_and_terminal_reads():
    sql = fixture("dead_items.sql")
    doc = analyze(sql)
    assert len(doc["columns"]) == 7
    assert len(doc["references"]) == 7
    assert len(doc["terminal"]) == 1
    for item in doc["columns"]:
        assert set(item) == {"id", "batch", "owner", "name", "ordinal", "start", "end", "resolved"}
        assert item["id"] == f"{item['owner']}/{item['ordinal']}"
    for item in doc["references"]:
        assert set(item) == {"batch", "start", "end", "name", "qualifier", "owner", "binds_to"}
        assert isinstance(item["qualifier"], str)
    terminal = doc["terminal"][0]
    assert set(terminal) == {"id", "batch", "statement_index", "reads", "columns", "start", "end"}
    assert terminal["id"] == "t0:0"
    assert terminal["reads"] == ["b0:cte_used"]
    assert {"source": "t0:0", "target": "b0:cte_used", "kind": "cte", "batch": 0} in doc["edges"]
    assert any(r["binds_to"] == "b0:cte_base/0" for r in doc["references"])


def test_terminal_spans_use_only_parser_identifier_bounds():
    from sqlglot import exp
    from analyzer.split import parse_batches
    sql = "SELECT 1;\nGO\nWITH cte_base AS (SELECT [Order Id] FROM orders) SELECT [Order Id] FROM cte_base;"
    terminals = analyze(sql)["terminal"]
    for batch in parse_batches(sql):
        for index, statement in enumerate(batch["expressions"]):
            meta = [node.meta for node in statement.find_all(exp.Identifier) if "start" in node.meta]
            terminal = next(t for t in terminals if t["id"] == f"t{batch['index']}:{index}")
            assert terminal["start"] == batch["start"] + min((m["start"] for m in meta), default=0)
            assert terminal["end"] == batch["start"] + max((m["end"] + 1 for m in meta), default=0)


def test_lone_star_has_exact_offset_and_is_unresolved():
    sql = "SELECT *;"
    doc = analyze(sql)
    column = doc["columns"][0]
    assert column["name"] == "*"
    assert sql[column["start"]:column["end"]] == "*"
    assert column["resolved"] is False
    assert doc["terminal"][0]["start"] == doc["terminal"][0]["end"] == 0


def test_repeated_metadata_free_stars_keep_their_own_offsets():
    sql = "SELECT *; SELECT *;"
    doc = analyze(sql)
    assert [c["start"] for c in doc["columns"]] == [sql.index("*"), sql.rindex("*")]


def test_offsets_and_terminal_indices_across_go_and_crlf():
    from test_analyzer import assert_offsets_are_exact
    sql = "SELECT [Order Id] FROM orders;\r\nGO\r\nWITH cte_base AS (SELECT [Order Id], amount AS [Total Amount] FROM orders) SELECT [Total Amount] FROM cte_base; SELECT *;"
    doc = assert_offsets_are_exact(sql)
    assert [t["id"] for t in doc["terminal"]] == ["t0:0", "t1:0", "t1:1"]
    assert any(c["name"] == "[Total Amount]" for c in doc["columns"])


def test_reference_binding_is_scope_based_and_conservative():
    sql = ("WITH cte_base AS (SELECT id, amount FROM orders) "
           "SELECT a.id FROM cte_base a WHERE EXISTS "
           "(SELECT 1 FROM customers c WHERE c.id = a.amount AND amount > 0);")
    refs = analyze(sql)["references"]
    correlated = next(r for r in refs if r["qualifier"] == "a" and r["name"] == "amount")
    assert correlated["binds_to"] == "b0:cte_base/1"
    assert correlated["owner"] == "t0:0"
    assert next(r for r in refs if not r["qualifier"] and r["start"] == sql.rindex("amount"))["binds_to"] is None


@pytest.mark.parametrize("owner,column,base", [
    ("b0:cte_base", "region", "orders.region"),
    ("b0:cte_dead", "id", "customers.id"),
    ("t0:0", "id", "orders.id"),
])
def test_column_lineage_resolves_intermediate_unread_and_terminal(owner, column, base):
    result = probe(fixture("dead_items.sql"), owner, column)
    assert result["resolved"] is True
    assert any(n["name"].casefold() == base for n in result["nodes"])


def test_column_lineage_uses_the_requested_go_batch():
    sql = "WITH cte_base AS (SELECT region FROM customers) SELECT region FROM cte_base;\nGO\nWITH cte_base AS (SELECT region FROM orders) SELECT region FROM cte_base;"
    result = probe(sql, "b1:cte_base", "region")
    assert result["resolved"] is True
    assert any(n["name"] == "orders.region" for n in result["nodes"])
    assert not any(n["name"] == "customers.region" for n in result["nodes"])


@pytest.mark.parametrize("owner,column", [("b0:missing", "id"), ("b0:cte_base", "discount"), ("t0:99", "id")])
def test_column_lineage_unknown_returns_error(owner, column):
    result = probe(fixture("dead_items.sql"), owner, column)
    assert result["resolved"] is False
    assert result["nodes"] == result["edges"] == []
    assert result["error"]


def test_column_lineage_memo_and_result_equivalence():
    from analyzer import columns
    sql = "WITH cte_memo AS (SELECT region FROM orders) SELECT region FROM cte_memo; -- memo check"
    with patch.object(columns, "parse_batches", wraps=columns.parse_batches) as parser:
        first = probe(sql, "b0:cte_memo", "region")
        second = probe(sql, "b0:cte_memo", "region")
        assert parser.call_count == 1
        probe(sql + "\n", "b0:cte_memo", "region")
        assert parser.call_count == 2
        probe(sql, "b0:cte_memo", "region")
        assert parser.call_count == 3

    def normalise(result):
        ids = {n["id"]: index for index, n in enumerate(result["nodes"])}
        return ([{**n, "id": ids[n["id"]]} for n in result["nodes"]],
                [{key: ids[value] for key, value in edge.items()} for edge in result["edges"]], result["resolved"])
    assert normalise(first) == normalise(second)


def test_correlated_probe_reaches_the_outer_source():
    sql = ("WITH cte_base AS (SELECT id, amount FROM orders), "
           "cte_used AS (SELECT a.id, (SELECT MAX(c.amount) FROM customers c WHERE c.id = a.amount) AS total "
           "FROM cte_base a) SELECT total FROM cte_used;")
    result = probe(sql, "b0:cte_used", "total")
    assert result["resolved"] is True
    assert any(n["name"].casefold() == "c.amount" and n["expression"] == "customers AS c"
               for n in result["nodes"])


def test_unresolved_correlated_projection_is_never_claimed_resolved():
    sql = ("WITH cte_base AS (SELECT id, amount FROM orders), "
           "cte_used AS (SELECT a.id, (SELECT a.amount FROM customers c WHERE c.id = a.id) AS total "
           "FROM cte_base a) SELECT total FROM cte_used;")
    result = probe(sql, "b0:cte_used", "total")
    assert result["resolved"] is False
    assert result["nodes"] == result["edges"] == []
    assert result["error"]


def test_analyze_makes_only_existing_final_output_lineage_calls():
    from analyzer import columns, lineage
    with patch.object(lineage, "sqlglot_lineage", wraps=lineage.sqlglot_lineage) as call, \
            patch.object(columns, "sqlglot_lineage", side_effect=AssertionError("unexpected probe")) as extra:
        doc = analyze(fixture("dead_items.sql"))
        assert call.call_count == len(doc["lineage"]) == 1
        assert extra.call_count == 0


def test_three_union_branches_each_get_an_anchor():
    sql = fixture("union_branches.sql")
    hop = only_hops(plan(sql))[0]
    assert hop["owner"] == "b0:cte_union"
    assert len(hop["anchors"]) == 3
    assert [a["start"] for a in hop["anchors"]] == [i + 7 for i in range(len(sql)) if sql.startswith("SELECT id FROM cte_base", i)]
    for anchor in hop["anchors"]:
        assert anchor["line"] == sql.count("\n", 0, anchor["start"]) + 1


def test_group_by_hop_has_caveat_and_alias_anchor():
    sql = fixture("aggregate_hop.sql")
    hop = only_hops(plan(sql))[0]
    assert "group_by" in hop["caveats"]
    assert len(hop["anchors"]) == 1
    assert sql[hop["anchors"][0]["start"]:].startswith("SUM(amount)")
    assert "group_by" not in only_hops(plan(sql))[-1]["caveats"]


def test_diamond_returns_both_labelled_ordered_routes():
    result = plan(fixture("diamond_paths.sql"))
    assert result["path_count"] == 2
    assert result["truncated"] is False
    assert len({p["label"] for p in result["paths"]}) == 2
    assert [[h["owner"] for h in p["hops"]] for p in result["paths"]] == [
        ["b0:cte_left", "b0:cte_joined", "t0:0"], ["b0:cte_right", "b0:cte_joined", "t0:0"]]


def test_routes_reach_each_terminal_and_probe_is_statement_scoped():
    sql = "WITH cte_base AS (SELECT region FROM orders) SELECT region FROM cte_base; SELECT region FROM cte_base;"
    result = plan(sql)
    assert {p["terminal"] for p in result["paths"]} == {"t0:0"}
    assert result["path_count"] == 1
    assert any(n["name"] == "orders.region" for n in probe(sql, "b0:cte_base", "region")["nodes"])


def test_star_pass_through_has_no_edit_or_anchor():
    sql = "WITH cte_base AS (SELECT id, region FROM orders), cte_copy AS (SELECT * FROM cte_base) SELECT id FROM cte_copy;"
    hop = only_hops(plan(sql))[0]
    assert hop["needs_edit"] is False
    assert hop["anchors"] == []


def test_unresolved_hop_claims_no_anchor():
    sql = "WITH cte_base AS (SELECT id, region FROM orders), cte_copy AS (SELECT * FROM cte_base JOIN customers c ON c.id = cte_base.id) SELECT id FROM cte_copy;"
    hop = only_hops(plan(sql))[0]
    assert "unresolved" in hop["caveats"]
    assert hop["anchors"] == []
    assert "line" not in hop


def test_already_projected_hop_and_missing_terminal():
    hops = only_hops(plan(fixture("dead_items.sql"), column="amount"))
    assert hops[0]["owner"] == "b0:cte_used"
    assert hops[0]["needs_edit"] is False
    assert hops[0]["anchors"] == []
    assert "already_projected" in hops[0]["caveats"]
    assert hops[1]["needs_edit"] is True


def test_partial_union_anchors_only_missing_branches():
    sql = fixture("union_branches.sql").replace("SELECT id FROM cte_base", "SELECT id, region FROM cte_base", 1)
    hop = only_hops(plan(sql))[0]
    assert hop["needs_edit"] is True
    assert "already_projected_partial" in hop["caveats"]
    assert len(hop["anchors"]) == 2
    assert all(sql[a["start"]:].startswith("id FROM") for a in hop["anchors"])


def test_every_union_branch_already_projects_casefolded_target():
    sql = fixture("union_branches.sql").replace("SELECT id FROM cte_base", "SELECT id, REGION FROM cte_base")
    hop = only_hops(plan(sql))[0]
    assert hop["needs_edit"] is False
    assert hop["anchors"] == []
    assert "already_projected" in hop["caveats"]


def test_terminal_qualified_reference_can_start_a_route():
    hops = only_hops(plan("SELECT o.id FROM orders o WHERE o.region = 'north';", "t0:0"))
    assert [h["owner"] for h in hops] == ["t0:0"]
    assert hops[0]["needs_edit"] is True


def test_qualified_reference_target_starts_at_its_owner():
    sql = "WITH cte_base AS (SELECT o.id FROM orders o WHERE o.region = 'north') SELECT id FROM cte_base;"
    hops = only_hops(plan(sql))
    assert [h["owner"] for h in hops] == ["b0:cte_base", "t0:0"]
    assert hops[0]["needs_edit"] is True


def test_absent_or_unqualified_only_target_is_rejected():
    sql = "WITH cte_base AS (SELECT id FROM orders WHERE region = 'north') SELECT id FROM cte_base;"
    for column in ("discount", "region"):
        result = plan(sql, column=column)
        assert result == {"visible": False, "reason": "this column is not visible in the pasted script; adding it from a physical table needs a schema — v2", "paths": [], "path_count": 0, "truncated": False}


def stacked_diamonds(count):
    parts = ["cte_base AS (SELECT id, region FROM orders)"]
    previous = "cte_base"
    for index in range(count):
        left, right, joined = (f"cte_{side}_{index}" for side in ("left", "right", "joined"))
        parts.extend([f"{left} AS (SELECT id FROM {previous})", f"{right} AS (SELECT id FROM {previous})",
                      f"{joined} AS (SELECT l.id FROM {left} l JOIN {right} r ON l.id = r.id)"])
        previous = joined
    return "WITH " + ",\n".join(parts) + f" SELECT id FROM {previous};"


@pytest.mark.parametrize("count,total,truncated", [(8, 256, False), (9, 512, True)])
def test_all_paths_guard_counts_exactly(count, total, truncated):
    result = plan(stacked_diamonds(count))
    assert result["path_count"] == total
    assert len(result["paths"]) == 256
    assert result["truncated"] is truncated
    assert all(len(p["hops"]) == count * 2 + 1 for p in result["paths"])


def test_checklist_makes_zero_lineage_calls():
    from analyzer import columns, lineage
    with patch.object(lineage, "sqlglot_lineage", side_effect=AssertionError("unexpected lineage")) as call, \
            patch.object(columns, "sqlglot_lineage", side_effect=AssertionError("unexpected probe")) as extra:
        assert plan(fixture("dead_items.sql"))["visible"]
        assert call.call_count == 0
        assert extra.call_count == 0


def test_anchor_lines_use_whole_script_after_go():
    sql = "SELECT 1;\r\nGO\r\n" + fixture("union_branches.sql")
    hops = only_hops(plan(sql, owner="b1:cte_base"))
    assert len(hops[0]["anchors"]) == 3
    for hop in hops:
        for anchor in hop["anchors"]:
            assert anchor["line"] == sql.count("\n", 0, anchor["start"]) + 1
            assert sql[anchor["start"]:].startswith("id FROM")


# Correction-pass rules. All SQL below is synthesized.
def test_r1_positionless_projections_keep_other_records():
    for value in ('CURRENT_TIMESTAMP', 'CURRENT_USER', 'SYSTEM_USER', 'CURRENT_DATE'):
        doc = analyze(f'SELECT {value} FROM t;')
        assert len(doc['columns']) == 1
        assert doc['columns'][0]['resolved'] is False
        doc = analyze(f'SELECT id, {value}, region FROM t;')
        assert [c['resolved'] for c in doc['columns']] == [True, False, True]


def test_r1_empty_and_comment_scripts():
    for sql in ('-- nothing here\n', '', ' \n\t', '/* empty */\n', '-- first\n-- second\n'):
        doc = analyze(sql)
        assert set(doc) == {'batches', 'ctes', 'edges', 'layers', 'lineage', 'dead',
                            'unions', 'diagnostics', 'columns', 'references', 'terminal'}
        assert all(value == ([] if key != 'dead' else {'ctes': [], 'columns': []})
                   for key, value in doc.items())


def test_r1_varied_tsql_sweep_is_json_safe():
    import json
    statements = [
        'SELECT CURRENT_TIMESTAMP FROM t;', 'SELECT SUM(amount) FROM orders;',
        'SELECT COUNT(*) AS total FROM orders;',
        'SELECT ROW_NUMBER() OVER (ORDER BY id) AS row_num FROM orders;',
        "SELECT CASE WHEN amount > 0 THEN 'yes' ELSE 'no' END FROM orders;",
        'SELECT CAST(NULL AS decimal(10,2)) FROM orders;', 'SELECT 1;',
        "SELECT 'north', NULL FROM orders;", 'SELECT a + b FROM orders;',
        'SELECT (SELECT MAX(amount) FROM orders) AS peak;',
        'SELECT TOP 3 id FROM orders;', 'SELECT DISTINCT region FROM orders;',
        'SELECT o.id FROM orders o CROSS APPLY (SELECT o.amount AS value) x;',
        'SELECT d.id FROM (SELECT id FROM orders) d;',
        'SELECT id INTO #rows FROM orders;', 'SELECT id FROM #rows;',
        'SELECT @total = SUM(amount) FROM orders;',
        'UPDATE orders SET amount = 1 WHERE id = 2;', 'DELETE FROM orders WHERE id = 2;',
        'INSERT INTO orders (id, amount) VALUES (1, 2);',
        'CREATE TABLE orders (id int, amount decimal(10,2));',
        'DECLARE @total int;', 'SET @total = 2;', 'EXEC dbo.read_orders;',
        'CREATE VIEW v AS SELECT id FROM orders UNION ALL SELECT id FROM archive;',
        'WITH a AS (SELECT id FROM orders), b AS (SELECT id FROM a) SELECT id FROM b;',
        'WITH a AS (WITH b AS (SELECT id FROM orders) SELECT id FROM b) SELECT id FROM a;',
        'SELECT UPPER(region) FROM orders;',
        'SELECT id FROM orders EXCEPT SELECT id FROM archive;',
        'SELECT id FROM orders INTERSECT SELECT id FROM archive;',
        'SELECT CURRENT_USER, SYSTEM_USER FROM orders;',
    ]
    assert len(statements) >= 25
    for sql in statements:
        doc = analyze(sql)
        assert not any(d["level"] == "refused" for d in doc["diagnostics"])
        json.dumps(doc)


def _terminal_anchors(sql, owner='b0:a', column='region'):
    hops = only_hops(plan(sql, owner, column))
    return hops[-1]['anchors']


def test_r2_create_view_has_all_leaf_anchors():
    sql = ('CREATE VIEW dbo.v AS WITH a AS (SELECT id, region FROM t) '
           'SELECT id FROM a UNION ALL SELECT id FROM a UNION ALL SELECT id FROM a;')
    anchors = _terminal_anchors(sql)
    assert [a['start'] for a in anchors] == [m.start() + 7 for m in __import__('re').finditer('SELECT id FROM a', sql)]


def test_r2_insert_wrappers_have_all_leaf_anchors():
    for target in ('tgt', 'tgt (id)', 'dbo.tgt ([id])'):
        sql = (f'WITH a AS (SELECT id, region FROM t) INSERT INTO {target} '
               'SELECT id FROM a UNION ALL SELECT id FROM a;')
        assert len(_terminal_anchors(sql)) == 2


def test_r2_parenthesized_mixed_branches_and_other_wrappers():
    query = 'SELECT id FROM a UNION (SELECT id FROM a UNION ALL SELECT id FROM a) UNION ALL SELECT id FROM a'
    for prefix in ('CREATE VIEW v AS ', 'CREATE TABLE tgt AS '):
        sql = prefix + 'WITH a AS (SELECT id, region FROM t) ' + query + ';'
        assert len(_terminal_anchors(sql)) == 4
    for prefix in ('CREATE VIEW v AS ', ''):
        sql = prefix + 'WITH a AS (SELECT id, region FROM t) SELECT id INTO #rows FROM a UNION ALL SELECT id FROM a UNION SELECT id FROM a;'
        assert len(_terminal_anchors(sql)) == 3
    sql = 'WITH a AS (SELECT id, region FROM t), b AS (' + query + ') SELECT id FROM b;'
    assert len(only_hops(plan(sql, 'b0:a'))[0]['anchors']) == 4


def test_r3_qualified_star_matches_route_source():
    for source, star in (('l JOIN r ON l.id = r.id', 'l'),
                         ('l AS a JOIN r AS b ON a.id = b.id', 'a'),
                         ('l AS A JOIN r AS B ON A.id = B.id', 'a'),
                         ('l AS a JOIN r AS b ON a.id = b.id', 'L')):
        sql = (f'WITH l AS (SELECT id, extra FROM t1), r AS (SELECT id, region FROM t2), '
               f'j AS (SELECT {star}.*, 1 AS rid FROM {source}) SELECT id FROM j;')
        missing = only_hops(plan(sql, 'b0:r', 'region'))[0]
        assert missing['needs_edit'] is True and len(missing['anchors']) == 1
        covered = only_hops(plan(sql, 'b0:l', 'extra'))[0]
        assert covered['needs_edit'] is False and covered['anchors'] == []


def test_r3_unknown_star_qualifier_is_unresolved():
    sql = ('WITH l AS (SELECT id FROM t1), r AS (SELECT id, region FROM t2), '
           'j AS (SELECT missing.*, r.id FROM l JOIN r ON l.id = r.id) SELECT id FROM j;')
    hop = only_hops(plan(sql, 'b0:r', 'region'))[0]
    assert hop['needs_edit'] is True and hop['anchors'] == [] and 'unresolved' in hop['caveats']


def test_r4_write_targets_are_excluded_but_real_reads_remain():
    cases = [
        ('CREATE VIEW dbo.v AS WITH a AS (SELECT id, region FROM t) SELECT id FROM a UNION SELECT id FROM a;', ['b0:a']),
        ('WITH a AS (SELECT id FROM t) INSERT INTO tgt SELECT id FROM a;', ['b0:a']),
        ('WITH a AS (SELECT id FROM t) INSERT INTO tgt (id) SELECT id FROM a;', ['b0:a']),
        ('SELECT id INTO #x FROM t;', ['t']),
        ('INSERT INTO tgt SELECT id FROM tgt;', ['tgt']),
        ('CREATE TABLE tgt AS SELECT id FROM t;', ['t']),
        ('UPDATE tgt SET amount = (SELECT amount FROM orders);', ['orders']),
        ('DELETE FROM tgt WHERE id IN (SELECT id FROM orders);', ['orders']),
        ('UPDATE tgt SET amount = 1 FROM tgt JOIN orders o ON o.id = tgt.id;', ['tgt', 'orders']),
        ('DELETE FROM tgt WHERE id IN (SELECT id FROM tgt);', ['tgt']),
    ]
    for sql, expected in cases:
        doc = analyze(sql)
        assert doc['terminal'][0]['reads'] == expected, sql
        assert [e['target'] for e in doc['edges'] if e['source'] == 't0:0'] == expected


def test_r5_leftmost_branch_names_are_shared_by_all_entry_points():
    for operator in ('UNION ALL', 'UNION', 'EXCEPT', 'INTERSECT'):
        sql = (f'WITH a AS (SELECT id AS order_key FROM t {operator} SELECT id FROM t '
               f'{operator} SELECT id AS other FROM t) SELECT order_key FROM a;')
        doc = analyze(sql)
        record = next(c for c in doc['columns'] if c['id'] == 'b0:a/0')
        assert record['name'] == 'order_key'
        assert sql[record['start']:record['end']] == 'order_key'
        assert record['start'] == sql.index('order_key')
        assert doc['ctes'][0]['columns'] == ['order_key']
        assert 'error' not in probe(sql, 'b0:a', 'order_key')
        assert plan(sql, 'b0:a', 'order_key')['visible'] is True


_REUSED = ('WITH c AS (SELECT id FROM t1) SELECT id FROM c;\n'
           'WITH c AS (SELECT name FROM t2) SELECT name FROM c;\nSELECT x FROM c;')
_REUSE_REASON = ('this CTE name is defined by more than one statement in the batch; '
                 'rename one or separate the statements with GO')


def test_r6_reused_name_warning_and_unique_column_ids():
    for sql in (_REUSED, _REUSED.replace('WITH c AS (SELECT name', 'WITH C AS (SELECT name')):
        doc = analyze(sql)
        warnings = [d for d in doc['diagnostics'] if d['level'] == 'warning']
        assert len(warnings) == 1 and warnings[0]['batch'] == 0 and 'c' in warnings[0]['reason'].casefold()
        assert not any(c['owner'].casefold() == 'b0:c' for c in doc['columns'])
        ids = [c['id'] for c in doc['columns']]
        assert len(ids) == len(set(ids))
        assert all(r['binds_to'] is None for r in doc['references'])
    for path in FIXTURES.glob('*.sql'):
        doc = analyze(path.read_text())
        assert not any(d['level'] == 'warning' for d in doc['diagnostics'])
        ids = [c['id'] for c in doc['columns']]
        assert len(ids) == len(set(ids)), path.name


def test_r6_reads_and_routes_stay_in_their_statement():
    doc = analyze(_REUSED)
    assert [t['reads'] for t in doc['terminal']] == [['b0:c'], ['b0:c'], ['c']]
    assert next(e for e in doc['edges'] if e['source'] == 't0:2')['kind'] == 'table'
    sql = 'WITH a AS (SELECT id, region FROM t) SELECT id FROM a; SELECT id FROM a;'
    result = plan(sql, 'b0:a', 'region')
    assert result['path_count'] == 1 and result['paths'][0]['terminal'] == 't0:0'


def test_r6_ambiguous_owner_refuses_but_terminal_lineage_resolves():
    result = probe(_REUSED, 'b0:c', 'id')
    assert result['nodes'] == [] and result['edges'] == [] and result['resolved'] is False and result['error']
    result = plan(_REUSED, 'b0:c', 'id')
    assert result == {'visible': False, 'reason': _REUSE_REASON, 'paths': [], 'path_count': 0, 'truncated': False}
    result = probe(_REUSED, 't0:1', 'name')
    assert result['resolved'] is True
    text = str(result['nodes'])
    assert 't2' in text and 't1' not in text


def test_r6_hops_use_local_body_even_for_an_ambiguous_id():
    sql = ('WITH a AS (SELECT id, region FROM t1), c AS (SELECT id FROM a) SELECT id FROM c;\n'
           'WITH c AS (SELECT name FROM t2) SELECT name FROM c;')
    hops = only_hops(plan(sql, 'b0:a', 'region'))
    assert [h['owner'] for h in hops] == ['b0:c', 't0:0']
    assert hops[0]['anchors'][0]['start'] == sql.index('SELECT id FROM a') + 7
    assert all('unresolved' not in h['caveats'] for h in hops)
    for distinct in ('WITH a AS (SELECT id FROM t) SELECT id FROM a; WITH b AS (SELECT id FROM u) SELECT id FROM b;',
                     _REUSED.replace(';\n', ';\nGO\n', 1)):
        assert not any(d['level'] == 'warning' for d in analyze(distinct)['diagnostics'])


def test_r7_unnamed_projection_is_unresolved_with_its_own_span():
    for expression in ('CAST(NULL AS decimal(10,2))', '1', 'a + b', 'UPPER(x)',
                       'COALESCE(UPPER(x), region)', 'CASE WHEN id = 1 THEN amount ELSE 0 END'):
        sql = f'SELECT id AS first_key, {expression}, region AS last_key FROM t;'
        records = analyze(sql)['columns']
        item = records[1]
        assert item['resolved'] is False
        assert item['name'] != '10,2'
        assert sql[item['start']:item['end']] == item['name']
        if item['name']:
            assert item['start'] == sql.index(expression)
            assert item['end'] <= sql.index(expression) + len(expression)
        else:
            assert item['start'] == item['end'] == analyze(sql)['terminal'][0]['start']
        assert records[0]['resolved'] is True and records[2]['resolved'] is True
    sql = 'SELECT CAST(x AS int) AS y FROM t;'
    assert [(c['name'], c['resolved']) for c in analyze(sql)['columns']] == [('y', True)]


def test_r8_parser_offset_guard_is_live_on_every_fixture():
    import test_analyzer
    for path in FIXTURES.glob('*.sql'):
        test_analyzer.assert_offsets_are_exact(path.read_text())
    # Check the output's particular Identifier, not merely any identifier-shaped slice.
    from sqlglot import exp
    from analyzer.split import parse_batches
    sql = ('SELECT 1;\nGO\nWITH a AS (SELECT id AS [Order Key] FROM t UNION ALL '
           'SELECT id FROM t UNION ALL SELECT id AS other FROM t) SELECT [Order Key] FROM a;')
    batch = parse_batches(sql)[1]
    body = next(batch['expressions'][0].find_all(exp.CTE)).this
    while isinstance(body, exp.SetOperation):
        body = body.this
    meta = body.expressions[0].args['alias'].meta
    expected_start = batch['start'] + meta['start']
    expected_end = batch['start'] + meta['end'] + 1
    record = next(c for c in analyze(sql)['columns'] if c['id'] == 'b1:a/0')
    assert (record['start'], record['end'], record['name']) == (
        expected_start, expected_end, sql[expected_start:expected_end])
    sql = 'SELECT [Order Id], amount AS [Total] FROM orders;\nGO\nSELECT region FROM customers;'
    for kind in ('columns', 'references'):
        doc = analyze(sql)
        item = doc[kind][0]
        item['start'] += 1
        item['end'] += 1
        item['name'] = sql[item['start']:item['end']]
        with patch.object(test_analyzer, 'analyze', return_value=doc):
            with pytest.raises(AssertionError):
                test_analyzer.assert_offsets_are_exact(sql)


def test_r1_token_errors_become_diagnostics():
    import json
    for sql in ("SELECT 'unfinished", 'SELECT [unfinished', '/* unfinished'):
        doc = analyze(sql)
        assert doc['diagnostics'][0]['level'] == 'error'
        assert doc['columns'] == []
        json.dumps(doc)


@pytest.mark.parametrize('sql, reads', [
    ('WITH a AS (SELECT id FROM s) DELETE o FROM orders o JOIN a ON o.id = a.id;', ['orders', 'b0:a']),
    ('DELETE t FROM t JOIN u ON t.id = u.id;', ['t', 'u']),
    ('DELETE o FROM orders o;', ['orders']),
    ('DELETE t FROM t;', ['t']),
    ('DELETE FROM x WHERE id IN (SELECT id FROM y);', ['y']),
    ('DELETE FROM x;', []),
    ('UPDATE o SET o.amount = a.amount FROM orders o JOIN a ON o.id = a.id;', ['orders', 'a']),
    ('UPDATE orders SET amount = (SELECT MAX(amount) FROM s);', ['s']),
    ('UPDATE orders SET amount = 1;', []),
    ('DELETE o FROM orders o JOIN (SELECT id FROM customers) c ON c.id = o.id JOIN regions r ON r.id = o.id;', ['orders', 'customers', 'regions']),
    ('UPDATE o SET amount = (SELECT MAX(amount) FROM s) FROM orders o JOIN a ON a.id = o.id;', ['s', 'orders', 'a']),
    ('delete [O] from [dbo].[orders] [O] join [customers] [C] on [O].id = [C].id;', ['orders', 'customers']),
    ('WITH a AS (SELECT id FROM s) UPDATE o SET amount = 1 FROM dbo.orders o JOIN a ON o.id = a.id JOIN customers c ON c.id = o.id JOIN regions r ON r.id = c.id JOIN customers d ON d.id = o.id;', ['orders', 'b0:a', 'customers', 'regions']),
], ids=['delete-cte', 'delete-same-name', 'delete-alias', 'delete-identical-target', 'delete-subquery', 'delete-simple',
        'update-from', 'update-subquery', 'update-simple', 'delete-derived-order', 'update-subquery-order',
        'delete-brackets', 'update-three-joins'])
def test_r9_write_sources_keep_ordered_unique_reads(sql, reads):
    doc = analyze(sql)
    assert not doc['diagnostics']
    assert doc['terminal'][0]['reads'] == reads
    edges = [e for e in doc['edges'] if e['source'] == 't0:0']
    assert [e['target'] for e in edges] == reads
    assert all(e['kind'] == ('cte' if e['target'].startswith('b0:') else 'table') for e in edges)
    if sql.startswith('WITH a') and 'DELETE' in sql:
        assert plan(sql, 'b0:a', 'id')['path_count'] == 1


@pytest.mark.parametrize('sql', [
    'TRUNCATE TABLE x;', 'DROP TABLE x;', 'DROP VIEW v;',
    'truncate table [dbo].[Orders];', 'drop view [dbo].[ViewOne];',
], ids=['truncate', 'drop-table', 'drop-view', 'truncate-brackets', 'drop-schema-view'])
def test_r10_target_only_statements_have_no_reads(sql):
    doc = analyze(sql)
    assert not doc['diagnostics']
    assert doc['terminal'][0]['reads'] == []
    assert not any(e['source'] == 't0:0' for e in doc['edges'])


def _r11_script(shape):
    if shape == 'parentheses':
        return 'SELECT ' + '(' * 200 + '1' + ')' * 200 + ' FROM t;'
    if shape == 'derived':
        return 'SELECT id FROM (' * 300 + 'SELECT id FROM t' + ') x' * 300 + ';'
    if shape == 'union':
        return ' UNION ALL '.join(['SELECT id FROM t'] * 2000) + ';'
    if shape == 'bracketed-parentheses':
        return 'select ' + '(' * 250 + '[id]' + ')' * 250 + ' from [dbo].[orders];'
    return 'WITH a AS (' + 'SELECT id FROM (' * 320 + 'SELECT id FROM orders' + ') x' * 320 + ') SELECT id FROM a;'


@pytest.mark.parametrize('shape', ['parentheses', 'derived', 'union', 'bracketed-parentheses', 'cte-derived'])
@pytest.mark.parametrize('tail', [False, True], ids=['alone', 'next-batch'])
def test_r11_deep_scripts_return_shapes_and_keep_other_batches(shape, tail):
    import json
    sql = _r11_script(shape) + ('\nGO\nSELECT id FROM t;' if tail else '')
    doc = analyze(sql)
    json.dumps(doc)
    assert len(doc['batches']) == (2 if tail else 1)
    assert any(d['batch'] == 0 and d['level'] in ('error', 'refused') for d in doc['diagnostics'])
    assert not any(d['batch'] == 1 for d in doc['diagnostics'])
    for key in ('ctes', 'edges', 'layers', 'lineage', 'unions', 'columns', 'references', 'terminal'):
        assert all(r['batch'] == 1 for r in doc[key])
    assert all(r['batch'] == 1 for records in doc['dead'].values() for r in records)
    if tail:
        assert [t['id'] for t in doc['terminal']] == ['t1:0']
        assert [(c['owner'], c['name']) for c in doc['columns']] == [('t1:0', 'id')]
    result = plan(sql, 't0:0', 'id')
    if result['visible']:
        # A flat UNION's terminal needs no edit, so the checklist can still proceed.
        assert shape == 'union'
        assert result['reason'] == '' and result['path_count'] == 1
        assert result['paths'][0]['terminal'] == 't0:0'
        assert result['paths'][0]['hops'] == [] and result['truncated'] is False
    else:
        assert result == {'visible': False, 'reason': 'this script could not be analyzed',
                          'paths': [], 'path_count': 0, 'truncated': False}
    result = probe(sql, 't0:0', 'id')
    assert result['nodes'] == [] and result['edges'] == [] and result['resolved'] is False and result['error']
    json.dumps(result)


@pytest.mark.parametrize('stage', ['reused_names', 'discover', 'collect_records', 'layers',
                                    'for_expression', 'check', 'analyze_dead'])
@pytest.mark.parametrize('error, reason', [
    (RecursionError, 'this batch is nested too deeply to analyze'),
    (RuntimeError, 'this batch could not be analyzed'),
], ids=['recursion', 'other-exception'])
def test_r11_analysis_failure_discards_the_entire_batch(stage, error, reason):
    import analyzer.api as api
    import json
    original = getattr(api, stage)
    calls = 0

    def fail_first(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise error('synthetic failure')
        return original(*args, **kwargs)

    sql = ('WITH a AS (SELECT id FROM t), a AS (SELECT id FROM u) '
           'SELECT id FROM a;\nGO\nSELECT id FROM t;')
    with patch.object(api, stage, side_effect=fail_first):
        doc = analyze(sql)
    assert doc['diagnostics'] == [{'batch': 0, 'level': 'refused', 'reason': reason}]
    assert doc['batches'][0]['parsed'] is True
    assert doc['batches'][0]['diagnostics'] == doc['diagnostics']
    expected = analyze('SELECT id FROM t;')
    for key in ('ctes', 'edges', 'layers', 'unions', 'columns', 'references', 'terminal'):
        assert not any(r['batch'] == 0 for r in doc[key])
        assert len(doc[key]) == len(expected[key])
    assert not any(r['batch'] == 0 for r in doc['lineage'])
    assert all(r['batch'] == 1 for records in doc['dead'].values() for r in records)
    assert len(doc['batches']) == 2 and len(doc['terminal']) == len(doc['columns']) == 1
    json.dumps(doc)


@pytest.mark.parametrize('error', [RecursionError, RuntimeError], ids=['recursion', 'other-exception'])
def test_r11_parser_failure_is_local_to_its_batch(error):
    import analyzer.split as split
    original = split.sqlglot.parse

    def parse_one(sql, **kwargs):
        if 'orders' in sql:
            raise error('synthetic parse failure')
        return original(sql, **kwargs)

    with patch.object(split.sqlglot, 'parse', side_effect=parse_one):
        doc = analyze('SELECT id FROM orders;\nGO\nSELECT id FROM t;')
    assert [d['level'] for d in doc['diagnostics']] == ['error']
    assert doc['diagnostics'][0]['batch'] == 0
    assert doc['batches'][0]['parsed'] is False
    assert [t['id'] for t in doc['terminal']] == ['t1:0']
    assert [c['owner'] for c in doc['columns']] == ['t1:0']


def test_r11_r1_cases_and_merge_do_not_hide_regressions():
    for expression in ('CURRENT_TIMESTAMP', 'CURRENT_USER', 'SYSTEM_USER', 'CURRENT_DATE'):
        doc = analyze(f'SELECT id, {expression}, region FROM t;')
        assert not any(d['level'] == 'refused' for d in doc['diagnostics'])
        assert len(doc['columns']) == 3
    for sql in ('', ' ', '-- comment only', '/* comment only */'):
        assert analyze(sql)['diagnostics'] == []
    sql = 'MERGE INTO orders o USING customers c ON o.id = c.id WHEN MATCHED THEN DELETE;'
    doc = analyze(sql)
    assert [d['level'] for d in doc['diagnostics']] == ['refused']
    assert doc['lineage'] == []


@pytest.mark.parametrize('sql, owner', [
    ('WITH a AS (SELECT id FROM t1), a AS (SELECT id FROM t2) SELECT id FROM a;', 'b0:a'),
    ('with A as (select id from orders), a as (select id from customers) select id from A;', 'b0:a'),
    ('WITH [A] AS (SELECT id FROM dbo.orders), [A] AS (SELECT id FROM dbo.customers), b AS (SELECT id FROM [A]) SELECT id FROM b;', 'b0:A'),
], ids=['duplicate', 'casefold', 'bracketed-cte'])
def test_r12_duplicate_with_names_are_reused(sql, owner):
    doc = analyze(sql)
    warnings = [d for d in doc['diagnostics'] if d['level'] == 'warning']
    assert len(warnings) == 1 and warnings[0]['batch'] == 0
    assert 'CTE a:' in warnings[0]['reason']
    assert not any(c['owner'].casefold() == owner.casefold() for c in doc['columns'])
    ids = [c['id'] for c in doc['columns']]
    assert len(ids) == len(set(ids))
    assert all(r['binds_to'] is None or not r['binds_to'].casefold().startswith(owner.casefold() + '/')
               for r in doc['references'])
    assert probe(sql, owner, 'id')['resolved'] is False
    assert plan(sql, owner, 'id') == {'visible': False, 'reason': _REUSE_REASON,
                                    'paths': [], 'path_count': 0, 'truncated': False}


@pytest.mark.parametrize('stage', ['_graph', '_hop'])
@pytest.mark.parametrize('error', [RecursionError, RuntimeError], ids=['recursion', 'other-exception'])
def test_r11_checklist_failure_returns_exact_shape(stage, error):
    with patch('analyzer.checklist.' + stage, side_effect=error('synthetic failure')):
        result = plan('WITH a AS (SELECT id FROM orders) SELECT id FROM a;', 'b0:a', 'id')
    assert result == {'visible': False, 'reason': 'this script could not be analyzed',
                      'paths': [], 'path_count': 0, 'truncated': False}


def test_r15_command_prefix_reproduction():
    sql = ('BEGIN SELECT 1; END;\nWITH base AS (SELECT id, region FROM t),\n'
           'a AS (SELECT id, amount FROM base)\nSELECT id, amount FROM a;')
    hops = only_hops(plan(sql, 'b0:base', 'region'))
    assert [h['anchors'] for h in hops] == [[{'line': 3, 'start': 79}],
                                          [{'line': 4, 'start': 108}]]
    assert all('unresolved' not in h['caveats'] for h in hops)


_R15_PREFIXES = [
    'BEGIN SELECT 1; END;',
    'GRANT SELECT ON t TO u;',
    'REVOKE SELECT ON t FROM u;',
    'DECLARE c CURSOR FOR SELECT id, amount FROM t;',
    'BEGIN TRY SELECT 1; END TRY BEGIN CATCH SELECT 2; END CATCH;',
    "EXEC('SELECT 1, 2');",
    "SELECT 'SELECT x, y FROM z' AS s;",
    '-- SELECT a, b FROM c\n/* SELECT d, e */',
    'DECLARE @n int = (SELECT COUNT(*) FROM t);',
    'DECLARE @m int; SET @m = (SELECT MAX(id) FROM t);',
    'INSERT INTO z SELECT id, amount FROM t;',
    'SELECT id, amount INTO #tmp FROM t;',
    'WITH q AS (SELECT id, x FROM t) SELECT id, x FROM q;',
    "PRINT 'x';",
    'SELECT [select], id FROM t;',
    'SELECT 1, 2;',
]
_R15_SHAPES = ['chain', 'union', 'view', 'join', 'cast', 'where']


def _r15_statement(shape='chain', suffix='', earlier='', last='amount',
                   a_from=None, a_tail='', terminal_tail='', base_columns='id, region'):
    base, a = 'base' + suffix, 'a' + suffix
    sql = ('CREATE VIEW v' + suffix + ' AS\n' if shape == 'view' else '')
    sql += f'WITH {base} AS (SELECT {base_columns} FROM t),\n{a} AS (SELECT id, '
    sql += earlier
    expected = [[{'line': sql.count('\n') + 1, 'start': len(sql)}]]
    sql += ('CAST(amount AS INT) AS amt' if shape == 'cast' else last)
    source = a_from or (f'{base} JOIN (SELECT id, z FROM t3) j ON j.id = {base}.id'
                        if shape == 'join' else base)
    sql += f' FROM {source}{a_tail})\n'
    expected.append([])
    for i in range(3 if shape == 'union' else 1):
        sql += ('\nUNION ALL\n' if i else '') + 'SELECT id, '
        expected[-1].append({'line': sql.count('\n') + 1, 'start': len(sql)})
        sql += f'amount FROM {a}'
    sql += (' WHERE id IN (SELECT id FROM t7)' if shape == 'where' else terminal_tail) + ';'
    return sql, expected, f'b0:{base}'


def _r15_check(sql, owner, expected, offset=0, line_shift=0, exact=True):
    from analyzer.split import parse_batches
    if any(not b['parsed'] for b in parse_batches(sql)):
        assert plan(sql, owner, 'region') == {
            'visible': False, 'reason': 'this script could not be analyzed',
            'paths': [], 'path_count': 0, 'truncated': False}
        return 0
    hops = only_hops(plan(sql, owner, 'region'))
    assert len(hops) == len(expected)
    unresolved = 0
    for hop, anchors in zip(hops, expected):
        if 'unresolved' in hop['caveats']:
            assert not exact, (sql, hop)
            assert hop['anchors'] == []
            unresolved += 1
        else:
            assert hop['anchors'] == [dict(start=a['start'] + offset,
                                           line=a['line'] + line_shift) for a in anchors], (sql, hop)
    return unresolved


@pytest.mark.parametrize('shape', _R15_SHAPES)
def test_r15_statement_independence(shape):
    sql, constructed, owner = _r15_statement(shape)
    _r15_check(sql, owner, constructed)
    reference = [h['anchors'] for h in only_hops(plan(sql, owner, 'region'))]
    for text in _R15_PREFIXES:
        prefix = text + '\n'
        _r15_check(prefix + sql, owner, reference, len(prefix), prefix.count('\n'))
        prefix += 'GO\n'
        _r15_check(prefix + sql, owner.replace('b0:', 'b1:'), reference,
                   len(prefix), prefix.count('\n'))
    prefix = 'IF 1=1 SELECT 1 ELSE SELECT 2;\n'
    _r15_check(prefix + sql, owner, reference, len(prefix), prefix.count('\n'), exact=False)
    _r15_check(prefix + 'GO\n' + sql, owner.replace('b0:', 'b1:'), reference,
               len(prefix + 'GO\n'), prefix.count('\n') + 1)
    for tail in ['SELECT 1, 2;', 'BEGIN SELECT 1; END;', 'GRANT SELECT ON t TO u;']:
        _r15_check(sql + '\n' + tail, owner, reference)


def test_r15_different_width_lists_after_command():
    sql, expected, owner = _r15_statement(base_columns='id, amount, region')
    prefix = 'BEGIN SELECT 1; END;\n'
    _r15_check(prefix + sql, owner, expected, len(prefix), 1)


@pytest.mark.parametrize('options', [
    dict(a_tail=' WHERE id IN (SELECT id FROM t2)'),
    dict(a_tail=' WHERE EXISTS (SELECT 1, 2 FROM t2 WHERE t2.id = base.id)'),
    dict(earlier='(SELECT MAX(x) FROM t2) AS mx, '),
    dict(earlier='CASE WHEN EXISTS (SELECT 1, 2 FROM t5) THEN 1 ELSE 0 END AS f, '),
    dict(earlier='ROW_NUMBER() OVER (ORDER BY (SELECT NULL)) AS rn, '),
    dict(a_from='(SELECT id, amount, region FROM base) d'),
    dict(a_from='base CROSS APPLY (SELECT TOP 1 y, w FROM t4 WHERE t4.id = base.id) ca'),
    dict(a_from='(SELECT id, amount FROM (SELECT id, amount, region FROM base) i) o'),
    dict(last='(SELECT MAX(x) FROM t2) AS mx'),
    dict(last='CASE WHEN id IN (SELECT id FROM t6) THEN amount END AS a2'),
    dict(a_tail=' GROUP BY id, amount HAVING COUNT(*) > (SELECT 1)'),
    dict(terminal_tail=' ORDER BY (SELECT 1)'),
], ids=['in', 'exists', 'scalar', 'case', 'window', 'derived', 'apply', 'deep-derived',
        'last-scalar', 'last-case', 'having', 'order'])
def test_r15_nested_select_pins(options):
    sql, expected, owner = _r15_statement(**options)
    _r15_check(sql, owner, expected)


@pytest.mark.parametrize('projection', ['1, 2', 'COUNT(*)'])
def test_r15_from_evidence_without_projection_identifiers(projection):
    prefix = 'BEGIN SELECT 1; END;\n'
    start = 'WITH a AS (SELECT id, region FROM t) SELECT '
    last = projection.split(', ')[-1]
    sql = start + projection + ' FROM a;'
    expected = [[{'start': len(start) + len(projection) - len(last), 'line': 1}]]
    _r15_check(sql, 'b0:a', expected)
    _r15_check(prefix + sql, 'b0:a', expected, len(prefix), 1)


@pytest.mark.parametrize('conditional', [False, True], ids=['exact-prefixes', 'with-if'])
def test_r15_random_batches_fixed_seed(capsys, conditional):
    import random
    rng = random.Random(150042)
    unresolved = checked = refused = exact_unresolved = 0
    prefixes = _R15_PREFIXES + (['IF 1=1 SELECT 1 ELSE SELECT 2;'] if conditional else [])
    # Unique CTE names preserve the frozen R6 refusal for reused names; repeats
    # of exactly identical statements are covered separately below.
    for trial in range(320):
        pieces, targets = [], []
        for index in range(rng.randint(1, 4)):
            if rng.random() < .55:
                sql, expected, owner = _r15_statement(rng.choice(_R15_SHAPES),
                                                     suffix=f'_{trial}_{index}')
                _r15_check(sql, owner, expected)
                reference = [h['anchors'] for h in only_hops(plan(sql, owner, 'region'))]
                prefix = ''.join(pieces)
                targets.append((owner, reference, len(prefix), prefix.count('\n')))
            else:
                sql = rng.choice(prefixes)
            pieces.append(sql + '\n')
        sql = ''.join(pieces)
        from analyzer.split import parse_batches
        refused += any(not b['parsed'] for b in parse_batches(sql))
        exact = 'IF 1=1 SELECT' not in sql
        for owner, expected, offset, lines in targets:
            count = _r15_check(sql, owner, expected, offset, lines, exact=exact)
            unresolved += count
            exact_unresolved += count if exact else 0
            checked += 1
    assert checked >= 300
    assert exact_unresolved == 0
    with capsys.disabled():
        print(f'R15 random (conditional={conditional}): 320 batches, {checked} statements checked, '
              f'{unresolved} unresolved hops ({exact_unresolved} in exact batches), '
              f'{refused} unparseable batches')


@pytest.mark.parametrize('last', [
    'id AS id', '[amount] AS [Total Amount]', '"amount" AS "Total Amount"',
    '[am]]ount] AS [out]]put]', '"am""ount" AS "out""put"',
    "CASE WHEN amount > 0 THEN 'SELECT x, y FROM z' ELSE 'FROM' END AS value",
    '/* SELECT x, y */ amount /* FROM t */ AS value',
    '(SELECT amount FROM t2) AS value',
    'CASE WHEN EXISTS (SELECT id FROM t2) THEN amount ELSE id END AS value',
    'amount + amount AS value',
], ids=['duplicate', 'bracketed', 'quoted', 'escaped-bracket', 'escaped-quote',
        'string', 'comment', 'nested', 'nested-case', 'repeated-column'])
def test_r15_self_attack_projection_evidence(last):
    leading = '/* SELECT x, y */ ' if last.startswith('/* SELECT') else ''
    sql, expected, owner = _r15_statement(earlier=leading, last=last[len(leading):])
    _r15_check(sql, owner, expected)
    # Identical SELECT text in an earlier Command cannot certify this node.
    prefix = 'DECLARE c CURSOR FOR SELECT id, ' + last + ' FROM base;\n'
    _r15_check(prefix + sql, owner, expected, len(prefix), prefix.count('\n'))


@pytest.mark.parametrize('modifier', ['DISTINCT ', 'ALL ', 'TOP 2 ',
                                      'DISTINCT TOP (2) PERCENT WITH TIES '])
def test_r15_self_attack_modifiers(modifier):
    prefix = 'BEGIN SELECT 1; END;\n'
    start = 'WITH base AS (SELECT id, region FROM t)\nSELECT ' + modifier
    sql = start + 'id FROM base;'
    expected = [[{'start': len(start), 'line': 2}]]
    _r15_check(sql, 'b0:base', expected)
    _r15_check(prefix + sql, 'b0:base', expected, len(prefix), 1)


@pytest.mark.parametrize('projection', ['*', 'base.*', 'id, *', 'id, base.*'])
def test_r15_self_attack_stars_pin(projection):
    for prefix in ['', 'BEGIN SELECT 1; END;\n']:
        sql = prefix + 'WITH base AS (SELECT id, region FROM t) SELECT ' + projection + ' FROM base;'
        hop = only_hops(plan(sql, 'b0:base', 'region'))[0]
        assert hop['anchors'] == [] and hop['needs_edit'] is False and hop['caveats'] == []


@pytest.mark.parametrize('sql', [
    'SELECT id, id FROM t;\nSELECT id, id FROM t;',
    'SELECT 1, 2 FROM t;\nSELECT 1, 2 FROM t;',
    'SELECT COUNT(*) FROM t;\nSELECT COUNT(*) FROM t;',
    'SELECT id, (SELECT id FROM t) AS value FROM t;\n'
    'SELECT id, (SELECT id FROM t) AS value FROM t;',
])
def test_r15_self_attack_identical_statements(sql):
    from analyzer.columns import expression_span
    from analyzer.split import parse_batches
    alone = sql.split(';')[0] + ';'
    batch = parse_batches(alone)[0]
    expected = expression_span(batch['expressions'][0].expressions[-1], batch)
    for prefix in ['', 'GRANT SELECT ON t TO u;\n']:
        batch = parse_batches(prefix + sql)[0]
        selects = [s for s in batch['expressions'] if s is not None and s.key == 'select']
        assert len(selects) == 2
        for i, select in enumerate(selects):
            assert expression_span(select.expressions[-1], batch) == tuple(
                offset + len(prefix) + i * (len(alone) + 1) for offset in expected)
    # Exact CTE repetition keeps the existing explicit reused-name refusal.
    statement, _, owner = _r15_statement()
    result = plan(statement + '\n' + statement, owner, 'region')
    assert not result['visible'] and result['reason'] == _REUSE_REASON


@pytest.mark.parametrize('change', ['missing', 'wrong-projection', 'wrong-list',
                                   'nested-only', pytest.param('width', id='width-pin'),
                                   'from-conflict'])
def test_r15_self_attack_inconsistent_evidence_is_unresolved(change):
    from sqlglot import exp
    from analyzer import checklist
    from analyzer.split import parse_batches
    from analyzer.columns import expression_span
    sql = ('WITH base AS (SELECT id, region FROM t) '
           'SELECT id, (SELECT amount FROM t2) AS value FROM base;')
    batch = parse_batches(sql)[0]
    select = batch['expressions'][0]
    own = [n for item in select.expressions for n in item.walk(
        prune=lambda n: isinstance(n, exp.Select)) if isinstance(n, exp.Identifier)]
    source = list(select.args['from_'].find_all(exp.Identifier))
    if change in ('missing', 'nested-only'):
        for n in own + source:
            n.meta.clear()
        if change == 'missing':
            for n in select.find_all(exp.Identifier):
                n.meta.clear()
    elif change == 'wrong-projection':
        own[-1].meta.update(own[0].meta)
    elif change == 'wrong-list':
        own[0].meta.update(select.args['with_'].find(exp.Select).expressions[0].this.meta)
    elif change == 'from-conflict':
        source[0].meta.update(select.args['with_'].find(exp.Select).expressions[0].this.meta)
    elif change == 'width':
        select.append('expressions', exp.Literal.number(3))
    with pytest.raises(ValueError, match='source projection is unavailable'):
        expression_span(select.expressions[-1], batch)
    key = (0, 0, 't0:0')
    hop = checklist._hop(key, 'region', {key: (select, batch, {}, 'terminal')}, [0], 'base')
    assert hop['anchors'] == [] and 'unresolved' in hop['caveats']


@pytest.mark.parametrize('sql', ['SELECT 1, 2;', 'SELECT COUNT(*);',
                                'SELECT (SELECT id FROM t);',
                                'SELECT COUNT(*) FROM (SELECT id FROM t);',
                                'SELECT 1 WHERE EXISTS (SELECT id FROM t);'])
def test_r15_self_attack_no_own_identifier_has_no_binding(sql):
    from analyzer.columns import expression_span
    from analyzer.split import parse_batches
    batch = parse_batches(sql)[0]
    with pytest.raises(ValueError, match='source projection is unavailable'):
        expression_span(batch['expressions'][0].expressions[-1], batch)


@pytest.mark.parametrize('n,m', [(12, 30), (30, 20), (50, 30)])
def test_r15_self_attack_source_binding_work_is_linear(n, m):
    from analyzer import columns
    from analyzer.split import parse_batches
    from sqlglot import exp
    batch = parse_batches('BEGIN SELECT 1; END;\n' + _r13_chain(n, m))[0]
    count = 0
    original = exp.Expression.iter_expressions

    def counted(node, *args, **kwargs):
        nonlocal count
        count += 1
        yield from original(node, *args, **kwargs)

    nodes = sum(1 for statement in batch['expressions'] if statement is not None
                for _ in statement.walk())
    with patch.object(exp.Expression, 'iter_expressions', counted):
        spans, _ = columns._source_lists(batch)
    assert len(spans) == n * m + 1
    assert count <= nodes


def test_r15_self_attack_ast_order_is_not_evidence():
    from analyzer.columns import expression_span
    from analyzer.split import parse_batches
    sql = 'SELECT id, amount FROM t; SELECT region FROM customers;'
    batch = parse_batches(sql)[0]
    first, second = batch['expressions']
    batch['expressions'].reverse()
    assert expression_span(first.expressions[-1], batch) == (len('SELECT id, '), len('SELECT id, amount'))
    start = len('SELECT id, amount FROM t; SELECT ')
    assert expression_span(second.expressions[-1], batch) == (start, start + len('region'))


@pytest.mark.parametrize('source', ['(SELECT id FROM a) d',
                                  'a CROSS APPLY (SELECT id FROM t2) d'])
def test_r15_self_attack_from_alias_and_nested_query(source):
    prefix = 'GRANT SELECT ON t TO u;\n'
    start = 'WITH a AS (SELECT id, region FROM t) SELECT '
    sql = start + 'COUNT(*) FROM ' + source + ';'
    expected = [[{'start': len(start), 'line': 1}]]
    _r15_check(sql, 'b0:a', expected)
    _r15_check(prefix + sql, 'b0:a', expected, len(prefix), 1)
