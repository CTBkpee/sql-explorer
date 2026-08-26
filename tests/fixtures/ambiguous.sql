WITH cte_base AS (
  SELECT id, amount FROM orders JOIN customers ON orders.id = customers.id
)
SELECT id FROM cte_base;
