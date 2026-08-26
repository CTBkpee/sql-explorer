WITH cte_base AS (
  SELECT id, amount FROM orders
), cte_copy AS (
  SELECT * FROM cte_base
)
SELECT id FROM cte_copy;
