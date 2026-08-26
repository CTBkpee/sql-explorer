CREATE OR ALTER VIEW report_view AS
WITH cte_base AS (
  SELECT id, amount FROM orders
)
SELECT id, amount FROM cte_base;
