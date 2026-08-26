WITH cte_all AS (
  SELECT id, amount FROM orders
  UNION ALL SELECT id, amount FROM customers
  UNION ALL SELECT id, amount FROM archive
)
SELECT id, amount FROM cte_all;
