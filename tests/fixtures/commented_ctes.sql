-- cte_orders: pull the raw order rows from the orders table
WITH cte_orders AS (
    SELECT id, amount, region FROM orders WHERE amount > 0  -- cte_orders rows per region
),
/* cte_region rolls cte_orders up; the label 'cte_region summary' is only comment text */
cte_region AS (
    SELECT region, SUM(amount) AS total FROM cte_orders GROUP BY region
)
SELECT region, total FROM cte_region ORDER BY total DESC;
