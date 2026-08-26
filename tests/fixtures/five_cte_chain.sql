WITH cte_one AS (
  SELECT id, amount, region FROM orders
), cte_two AS (
  SELECT id, amount, region FROM cte_one
), cte_three AS (
  SELECT id, amount FROM cte_two
), cte_four AS (
  SELECT id, amount FROM cte_three
), cte_five AS (
  SELECT id, amount FROM cte_four
)
SELECT id, amount FROM cte_five;
