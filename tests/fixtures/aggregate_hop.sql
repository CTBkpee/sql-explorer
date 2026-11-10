WITH cte_base AS (
    SELECT id, amount, region FROM orders
), cte_group AS (
    SELECT id, SUM(amount) AS total FROM cte_base GROUP BY id
)
SELECT id FROM cte_group;
