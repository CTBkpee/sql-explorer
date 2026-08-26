MERGE orders AS target
USING (SELECT id, amount FROM customers) AS source
ON target.id = source.id
WHEN MATCHED THEN UPDATE SET amount = source.amount;
