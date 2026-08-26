WITH cte_base AS (
  SELECT id, amount, region FROM orders
), cte_dead AS (
  SELECT id FROM customers
), cte_used AS (
  SELECT id, amount FROM cte_base
)
SELECT id FROM cte_used;
