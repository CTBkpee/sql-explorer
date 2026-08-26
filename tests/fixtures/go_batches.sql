SELECT id FROM orders;
GO
WITH cte_base AS (SELECT id FROM customers)
SELECT id FROM cte_base;
