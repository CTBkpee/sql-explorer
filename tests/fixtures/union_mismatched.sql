SELECT id AS record_id, amount FROM orders
UNION ALL SELECT id AS other_id FROM customers
UNION ALL SELECT id AS record_id, amount FROM archive;
