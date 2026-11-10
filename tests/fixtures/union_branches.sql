WITH cte_base AS (
    SELECT id, region FROM orders
), cte_union AS (
    SELECT id FROM cte_base
    UNION ALL
    SELECT id FROM cte_base
    UNION ALL
    SELECT id FROM cte_base
)
SELECT id FROM cte_union;
