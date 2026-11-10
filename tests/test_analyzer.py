from pathlib import Path

from analyzer.api import analyze

FIXTURES = Path(__file__).parent / "fixtures"


def run(name):
    return analyze((FIXTURES / name).read_text())


def markers(document, kind, state):
    return [item for item in document["dead"][kind] if item["state"] == state]


def test_five_cte_chain_has_dependency_layers():
    result = run("five_cte_chain.sql")
    assert len(result["ctes"]) == 5
    assert len(result["edges"]) >= 5
    assert len(result["layers"]) == 5


def test_dead_cte_and_column_are_exactly_reported():
    result = run("dead_items.sql")
    assert [item["cte"] for item in markers(result, "ctes", "dead")] == ["b0:cte_dead"]
    assert [(item["cte"], item["column"]) for item in markers(result, "columns", "dead")] == [
        ("b0:cte_base", "region"), ("b0:cte_dead", "id"), ("b0:cte_used", "amount")]


def test_star_from_cte_expands_its_columns():
    result = run("star_cte.sql")
    copy = next(cte for cte in result["ctes"] if cte["name"] == "cte_copy")
    assert copy["columns"] == ["id", "amount"]
    base_markers = [item for item in result["dead"]["columns"] if item["cte"] == "b0:cte_base"]
    assert {item["column"] for item in base_markers if item["state"] == "live"} == {"id", "amount"}


def test_star_from_a_physical_table_is_unresolved():
    result = run("star_physical.sql")
    assert result["ctes"][0]["resolved"] is False
    assert {item["state"] for item in result["dead"]["ctes"]} == {"unknown"}


def test_create_or_alter_view_with_cte_is_discovered():
    result = run("view_ctes.sql")
    assert result["batches"][0]["parsed"]
    assert [cte["name"] for cte in result["ctes"]] == ["cte_base"]


def test_triple_union_alignment_accepts_matched_branches():
    assert run("union_matched.sql")["unions"] == []


def test_union_alignment_flags_count_and_alias_mismatch():
    findings = run("union_mismatched.sql")["unions"]
    assert any(item["kind"] == "column_count" for item in findings)
    assert any(item["kind"] == "alias" and item["position"] == 0 for item in findings)


def test_go_batches_parse_independently():
    result = run("go_batches.sql")
    assert len(result["batches"]) == 2
    assert all(batch["parsed"] for batch in result["batches"])


def test_bad_go_batch_does_not_prevent_later_batch_analysis():
    result = analyze("SELECT FROM;\nGO\nWITH cte_base AS (SELECT id FROM orders) SELECT id FROM cte_base;")
    assert result["batches"][0]["parsed"] is False
    assert result["batches"][1]["parsed"] is True
    assert [cte["name"] for cte in result["ctes"]] == ["cte_base"]


def test_merge_is_refused_and_has_no_lineage():
    result = run("merge.sql")
    assert {"level": "refused", "reason": "MERGE lineage is unreliable in sqlglot; not rendered"} == {
        key: value for key, value in result["diagnostics"][0].items() if key != "batch"
    }
    assert result["lineage"] == []


def test_ambiguous_column_is_unresolved_and_never_marked_dead():
    result = run("ambiguous.sql")
    assert any(not item["resolved"] for item in result["lineage"])
    assert not markers(result, "ctes", "dead")
    assert not markers(result, "columns", "dead")
    assert markers(result, "ctes", "unknown")


def test_mixed_case_final_reference_keeps_cte_column_live():
    result = analyze("WITH cte_a AS (SELECT id, Amount FROM orders) SELECT AMOUNT FROM cte_a;")
    marker = next(item for item in result["dead"]["columns"] if item["column"] == "Amount")
    assert marker["state"] == "live"


def test_mixed_case_where_and_order_by_references_keep_cte_column_live():
    result = analyze(
        "WITH cte_a AS (SELECT id, Amount FROM orders) "
        "SELECT id FROM cte_a WHERE AMOUNT > 0 ORDER BY aMoUnT;"
    )
    marker = next(item for item in result["dead"]["columns"] if item["column"] == "Amount")
    assert marker["state"] == "live"


def test_cte_and_physical_table_unqualified_column_is_unknown():
    result = analyze(
        "WITH cte_a AS (SELECT id, amount FROM orders) "
        "SELECT amount FROM cte_a JOIN customers c ON c.id = cte_a.id;"
    )
    marker = next(item for item in result["dead"]["columns"] if item["column"] == "amount")
    assert marker["state"] == "unknown"


def test_three_source_unqualified_column_is_unknown_for_each_cte():
    result = analyze(
        "WITH cte_a AS (SELECT id, amount FROM orders), "
        "cte_b AS (SELECT id, amount FROM orders) "
        "SELECT amount FROM cte_a "
        "JOIN cte_b ON cte_b.id = cte_a.id "
        "JOIN customers c ON c.id = cte_a.id;"
    )
    markers_by_cte = {item["cte"]: item["state"] for item in result["dead"]["columns"] if item["column"] == "amount"}
    assert markers_by_cte == {"b0:cte_a": "unknown", "b0:cte_b": "unknown"}


# --- reference resolution over sqlglot scopes (rounds 1-3 reproducers) -----------------------

def column_state(document, column, cte="b0:cte_a"):
    return next(item["state"] for item in document["dead"]["columns"]
                if item["cte"] == cte and item["column"] == column)


def test_mixed_case_group_by_and_having_references_keep_columns_live():
    result = analyze(
        "WITH cte_a AS (SELECT id, Amount, Region FROM orders) "
        "SELECT id FROM cte_a GROUP BY ID, REGION HAVING SUM(AMOUNT) > 0;"
    )
    assert column_state(result, "Amount") == "live"
    assert column_state(result, "Region") == "live"


def test_bracketed_mixed_case_references_keep_columns_live():
    result = analyze(
        "WITH cte_a AS (SELECT id, Amount FROM orders) "
        "SELECT [AMOUNT] FROM cte_a WHERE [cte_a].[ID] > 0;"
    )
    assert column_state(result, "Amount") == "live"
    assert column_state(result, "id") == "live"


def test_mixed_case_cte_name_in_from_is_still_a_reference():
    # sqlglot links FROM to a CTE case-sensitively; T-SQL does not.
    result = analyze("WITH cte_a AS (SELECT id, amount FROM orders) SELECT Amount FROM CTE_A WHERE Cte_A.id > 0;")
    assert column_state(result, "amount") == "live"
    assert column_state(result, "id") == "live"
    assert result["dead"]["ctes"] == [{"batch": 0, "cte": "b0:cte_a", "state": "live"}]


def test_exists_correlated_reference_keeps_column_live():
    result = analyze(
        "WITH cte_a AS (SELECT id, amount FROM orders) SELECT id FROM cte_a "
        "WHERE EXISTS (SELECT 1 FROM customers c WHERE c.id = cte_a.id AND cte_a.amount > 0);"
    )
    assert column_state(result, "amount") == "live"


def test_aliased_cte_correlated_reference_keeps_column_live():
    result = analyze(
        "WITH cte_a AS (SELECT id, amount FROM orders) SELECT id FROM cte_a a "
        "WHERE EXISTS (SELECT 1 FROM customers c WHERE c.id = a.id AND a.amount > 0);"
    )
    assert column_state(result, "amount") == "live"


def test_scalar_correlated_subquery_in_select_list_keeps_column_live():
    result = analyze(
        "WITH cte_a AS (SELECT id, amount FROM orders) "
        "SELECT id, (SELECT COUNT(*) FROM customers c WHERE c.id = a.amount) AS n FROM cte_a a;"
    )
    assert column_state(result, "amount") == "live"


def test_not_exists_sole_correlated_reference_keeps_column_live():
    result = analyze(
        "WITH cte_a AS (SELECT id, region FROM orders) SELECT id FROM cte_a "
        "WHERE NOT EXISTS (SELECT 1 FROM customers c WHERE c.region = cte_a.region);"
    )
    assert column_state(result, "region") == "live"


def test_correlated_reference_does_not_mask_a_genuinely_dead_sibling():
    result = analyze(
        "WITH cte_a AS (SELECT id, amount, region FROM orders) SELECT id FROM cte_a "
        "WHERE EXISTS (SELECT 1 FROM customers c WHERE c.id = cte_a.amount);"
    )
    assert column_state(result, "amount") == "live"
    assert column_state(result, "region") == "dead"


def test_derived_table_over_cte_keeps_columns_live():
    result = analyze(
        "WITH cte_a AS (SELECT id, amount FROM orders) "
        "SELECT d.id FROM (SELECT id, amount FROM cte_a) d WHERE d.amount > 0;"
    )
    assert column_state(result, "amount") == "live"


def test_cross_apply_correlated_reference_keeps_column_live():
    result = analyze(
        "WITH cte_a AS (SELECT id, amount FROM orders) SELECT a.id FROM cte_a a "
        "CROSS APPLY (SELECT TOP 1 c.id FROM customers c WHERE c.id = a.amount) x;"
    )
    assert column_state(result, "amount") == "live"


def test_cte_referenced_from_a_subquery_inside_a_later_cte():
    result = analyze(
        "WITH cte_a AS (SELECT id, amount FROM orders), "
        "cte_b AS (SELECT id FROM customers c WHERE EXISTS (SELECT 1 FROM cte_a WHERE cte_a.amount = c.id)) "
        "SELECT id FROM cte_b;"
    )
    assert column_state(result, "amount") == "live"
    assert column_state(result, "id") == "dead"
    assert column_state(result, "id", "b0:cte_b") == "live"


def test_union_inside_a_cte_body_references_both_columns():
    result = analyze(
        "WITH cte_a AS (SELECT id, amount FROM orders), "
        "cte_b AS (SELECT id FROM cte_a UNION ALL SELECT amount FROM cte_a) SELECT id FROM cte_b;"
    )
    assert column_state(result, "id") == "live"
    assert column_state(result, "amount") == "live"


def test_union_inside_a_correlated_subquery_keeps_columns_live():
    result = analyze(
        "WITH cte_a AS (SELECT id, amount FROM orders) SELECT id FROM cte_a "
        "WHERE EXISTS (SELECT 1 FROM customers c WHERE c.id = cte_a.amount "
        "UNION ALL SELECT 1 FROM archive x WHERE x.id = cte_a.id);"
    )
    assert column_state(result, "amount") == "live"


def test_insert_select_with_correlated_reference_keeps_column_live():
    result = analyze(
        "WITH cte_a AS (SELECT id, amount FROM orders) INSERT INTO target_rows (id) "
        "SELECT id FROM cte_a WHERE EXISTS (SELECT 1 FROM customers c WHERE c.id = cte_a.amount);"
    )
    assert column_state(result, "amount") == "live"


def test_unqualified_column_in_correlated_subquery_over_physical_table_is_unknown():
    # `amount` may bind to customers or fall outward to cte_a; nothing in the script says which.
    result = analyze(
        "WITH cte_a AS (SELECT id, amount FROM orders) SELECT id FROM cte_a "
        "WHERE EXISTS (SELECT 1 FROM customers c WHERE amount > 0);"
    )
    assert column_state(result, "amount") == "unknown"
    assert not markers(result, "columns", "dead")


def test_unqualified_column_binds_to_the_innermost_cte_that_projects_it():
    result = analyze(
        "WITH cte_a AS (SELECT id, amount FROM orders), cte_b AS (SELECT id, region FROM customers) "
        "SELECT id FROM cte_a WHERE EXISTS (SELECT 1 FROM cte_b WHERE amount > 0 AND region = 'x');"
    )
    assert column_state(result, "amount") == "live"
    assert column_state(result, "region", "b0:cte_b") == "live"
    assert column_state(result, "id", "b0:cte_b") == "dead"


def test_statement_shape_the_scope_walk_cannot_place_is_unknown_not_dead():
    # sqlglot does not scope UPDATE's SET/WHERE clauses; the analyzer must say so, not guess.
    result = analyze(
        "WITH cte_a AS (SELECT id, amount FROM orders) UPDATE o SET o.flag = 1 "
        "FROM orders o JOIN cte_a ON cte_a.id = o.id WHERE cte_a.amount > 0;"
    )
    assert {item["state"] for item in result["dead"]["columns"]} == {"unknown"}
    assert not markers(result, "columns", "dead")


def test_unresolvable_qualifier_is_unknown_not_dead():
    result = analyze("WITH cte_a AS (SELECT id, amount FROM orders) SELECT id FROM cte_a WHERE zz.amount > 0;")
    assert column_state(result, "amount") == "unknown"
    assert not markers(result, "columns", "dead")


def test_recursive_cte_self_reference_does_not_produce_false_dead():
    result = analyze(
        "WITH cte_r AS (SELECT id, amount FROM orders WHERE id = 1 "
        "UNION ALL SELECT r.id, r.amount FROM cte_r r JOIN orders o ON o.id = r.id) SELECT id FROM cte_r;"
    )
    assert not markers(result, "columns", "dead")


# --- character offsets are exact slices of the original script -------------------------------

def assert_offsets_are_exact(sql):
    result = analyze(sql)
    for batch in result["batches"]:
        assert sql[batch["start"]:batch["end"]] == batch["sql"]
    for cte in result["ctes"]:
        assert sql[cte["start"]:cte["end"]].lower() == cte["name"].lower()
    from sqlglot import exp
    from analyzer.split import parse_batches
    identifiers, stars, literals = set(), set(), set()
    for batch in parse_batches(sql):
        for statement in batch['expressions']:
            if statement is None:
                continue
            for node in statement.find_all(exp.Identifier):
                if 'start' in node.meta and 'end' in node.meta:
                    start = batch['start'] + node.meta['start']
                    end = batch['start'] + node.meta['end'] + 1
                    identifiers.add((start, end, sql[start:end]))
            for node in statement.find_all(exp.Star):
                if 'start' in node.meta and 'end' in node.meta:
                    start = batch['start'] + node.meta['start']
                    end = batch['start'] + node.meta['end'] + 1
                    stars.add((start, end, sql[start:end]))
            # Unnamed literals and stars have no Identifier; use their own parser meta.
            for node in statement.find_all(exp.Literal):
                if 'start' in node.meta and 'end' in node.meta:
                    start = batch['start'] + node.meta['start']
                    end = batch['start'] + node.meta['end'] + 1
                    literals.add((start, end, sql[start:end]))
    for item in result["columns"]:
        assert (item['start'], item['end'], item['name']) in identifiers | stars | literals
    for item in result["references"]:
        assert (item['start'], item['end'], item['name']) in identifiers | stars
    return result


def test_batch_and_cte_offsets_slice_the_original_script_across_go():
    sql = (FIXTURES / "go_batches.sql").read_text()
    result = assert_offsets_are_exact(sql)
    assert len(result["batches"]) == 2
    assert result["ctes"][0]["batch"] == 1


def test_offsets_survive_crlf_and_consecutive_go_lines():
    sql = "SELECT 1;\r\n\r\nGO\r\n\r\nGO\r\nWITH cte_x AS (SELECT id FROM orders) SELECT id FROM cte_x;\r\nGO\r\n"
    result = assert_offsets_are_exact(sql)
    assert all(batch["parsed"] for batch in result["batches"])
    assert [cte["name"] for cte in result["ctes"]] == ["cte_x"]


def test_browser_analyzer_copy_is_in_sync_with_the_source_package():
    # web/analyzer/ is a generated copy (tools/sync-analyzer.sh) that Pyodide loads.
    # A fix to analyzer/ that is not re-synced ships a stale analyzer to the browser
    # while the test suite stays green — run tools/sync-analyzer.sh after any edit.
    source = Path(__file__).resolve().parent.parent / "analyzer"
    served = Path(__file__).resolve().parent.parent / "web" / "analyzer"
    for path in sorted(source.glob("*.py")):
        assert (served / path.name).read_text() == path.read_text(), (
            f"{path.name} differs; re-run tools/sync-analyzer.sh")


def test_comment_line_above_a_cte_does_not_capture_the_definition_offset():
    # The exact gate reproducer: a `-- cte_base` comment sits directly above the
    # definition. A naive `\bcte_base\b` search anchors inside the comment and
    # jump-to-definition scrolls there; the offset must land on `WITH cte_base AS`.
    sql = "-- cte_base: pull the raw order rows\nWITH cte_base AS (SELECT id FROM orders) SELECT id FROM cte_base;"
    result = assert_offsets_are_exact(sql)
    cte = result["ctes"][0]
    assert cte["start"] == sql.index("WITH ") + len("WITH ")
    assert sql.index("cte_base") < cte["start"]  # the comment's occurrence is earlier and skipped


def test_definition_offsets_skip_preceding_comment_and_string_occurrences():
    sql = (FIXTURES / "commented_ctes.sql").read_text()
    result = assert_offsets_are_exact(sql)
    by_name = {cte["name"]: cte for cte in result["ctes"]}
    # cte_orders' name appears first inside the leading `--` comment line.
    assert sql[by_name["cte_orders"]["start"]:].startswith("cte_orders AS")
    assert sql.index("-- cte_orders") < by_name["cte_orders"]["start"]
    # cte_region's name appears first inside a `/* */` block comment and a string
    # literal ('cte_region summary'); the definition still anchors on the real token.
    assert sql[by_name["cte_region"]["start"]:].startswith("cte_region AS")
    assert sql.index("/* cte_region") < by_name["cte_region"]["start"]
    assert sql.index("'cte_region summary'") < by_name["cte_region"]["start"]
