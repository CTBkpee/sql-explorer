WITH cte_base AS (
    SELECT id, region FROM orders
), cte_left AS (
    SELECT id FROM cte_base
), cte_right AS (
    SELECT id FROM cte_base
), cte_joined AS (
    SELECT l.id FROM cte_left l JOIN cte_right r ON l.id = r.id
)
SELECT id FROM cte_joined;
